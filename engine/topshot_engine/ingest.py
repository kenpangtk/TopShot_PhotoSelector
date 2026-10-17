from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config, db


@dataclass
class ScanEntry:
    path: Path
    rel_path: str
    ext: str
    kind: str
    size: int
    mtime_ns: int
    sidecar: Path | None = None
    pair_group: str | None = None
    pair_role: str | None = None
    hidden: bool = False


@dataclass
class ScanResult:
    entries: list[ScanEntry] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    sidecars: int = 0
    errors: list[str] = field(default_factory=list)


def kind_for_ext(ext: str) -> str | None:
    if ext in config.JPEG_EXTENSIONS:
        return "jpeg"
    if ext in config.HEIC_EXTENSIONS:
        return "heic"
    if ext in config.RAW_EXTENSIONS:
        return "raw"
    return None


def scan_source(source_dir: Path, progress: Callable[[int], None] | None = None) -> ScanResult:
    result = ScanResult()
    source_dir = source_dir.resolve()
    sidecars: dict[tuple[str, str], list[str]] = {}
    raw_entries: list[tuple[Path, str, int, int]] = []
    seen_sidecars: set[str] = set()
    count = 0

    for root, dirs, files in os.walk(source_dir, followlinks=False, onerror=result.errors.append):  # type: ignore[arg-type]
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        root_path = Path(root)
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = root_path / name
            ext = path.suffix.lower()
            try:
                stat = path.stat()
            except OSError as exc:
                result.errors.append(f"{path}: {exc}")
                continue
            if not path.is_file():
                continue
            if ext in (".xmp", ".xml"):
                key = (root.lower(), path.stem.lower())
                sidecars.setdefault(key, []).append(str(path))
                sidecars.setdefault((root.lower(), path.name.lower()), []).append(str(path))
                if str(path) not in seen_sidecars:
                    seen_sidecars.add(str(path))
                    result.sidecars += 1
                continue
            kind = kind_for_ext(ext)
            if kind:
                raw_entries.append((path, kind, stat.st_size, stat.st_mtime_ns))
            elif ext in config.VIDEO_EXTENSIONS:
                result.videos.append(str(path))
            else:
                result.unsupported.append(str(path))
            count += 1
            if progress and count % 250 == 0:
                progress(count)

    def sidecar_for(path: Path) -> Path | None:
        parent = str(path.parent).lower()
        candidates = sidecars.get((parent, path.stem.lower()))
        if not candidates:
            candidates = sidecars.get((parent, path.name.lower()))
        if not candidates:
            return None
        return Path(sorted(candidates)[0])

    entries: list[ScanEntry] = []
    for path, kind, size, mtime_ns in raw_entries:
        entries.append(
            ScanEntry(
                path=path,
                rel_path=os.path.relpath(path, source_dir),
                ext=path.suffix.lower(),
                kind=kind,
                size=size,
                mtime_ns=mtime_ns,
                sidecar=sidecar_for(path),
            )
        )

    # RAW+JPEG pairing: only unambiguous same-basename pairs. JPEG is the visible
    # review asset; the RAW is hidden from review but copied per the pair setting.
    groups: dict[tuple[str, str], list[ScanEntry]] = {}
    for entry in entries:
        groups.setdefault((str(entry.path.parent), entry.path.stem.lower()), []).append(entry)
    for (parent, _stem), members in groups.items():
        raws = [e for e in members if e.kind == "raw"]
        jpegs = [e for e in members if e.kind == "jpeg"]
        if len(raws) == 1 and len(jpegs) == 1:
            raw, jpeg = raws[0], jpegs[0]
            group = f"{parent}/{jpeg.path.stem}"
            raw.pair_group = group
            raw.pair_role = "raw"
            raw.hidden = True
            jpeg.pair_group = group
            jpeg.pair_role = "jpeg"
            jpeg.hidden = False

    result.entries = entries
    if progress:
        progress(count)
    return result


def upsert_scan(conn: sqlite3.Connection, project_id: int, scan: ScanResult, source_dir: Path) -> dict[str, Any]:
    now = db.utc_now()
    scanned_paths: set[str] = set()
    stats = {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "missing": 0,
        "videos": len(scan.videos),
        "unsupported": len(scan.unsupported),
        "sidecars": scan.sidecars,
        "supported": len(scan.entries),
        "video_samples": [os.path.basename(p) for p in scan.videos[:20]],
        "unsupported_samples": [os.path.basename(p) for p in scan.unsupported[:20]],
        "errors": list(scan.errors[:50]),
    }
    ids_by_path: dict[str, int] = {}

    with conn:
        for entry in scan.entries:
            path_text = str(entry.path)
            scanned_paths.add(path_text)
            existing = conn.execute(
                "SELECT id, size, mtime_ns, status FROM files WHERE project_id=? AND path=?",
                (project_id, path_text),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO files(project_id, path, rel_path, dir, stem, ext, kind, size, mtime_ns,
                                      sidecar_path, pair_group, pair_role, hidden_from_review,
                                      status, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)
                    """,
                    (
                        project_id,
                        path_text,
                        entry.rel_path,
                        str(entry.path.parent),
                        entry.path.stem,
                        entry.ext,
                        entry.kind,
                        entry.size,
                        entry.mtime_ns,
                        str(entry.sidecar) if entry.sidecar else None,
                        entry.pair_group,
                        entry.pair_role,
                        1 if entry.hidden else 0,
                        now,
                        now,
                    ),
                )
                ids_by_path[path_text] = int(cur.lastrowid)
                stats["new"] += 1
            else:
                file_id = int(existing["id"])
                ids_by_path[path_text] = file_id
                changed = int(existing["size"]) != entry.size or int(existing["mtime_ns"]) != entry.mtime_ns
                if changed:
                    conn.execute(
                        """
                        UPDATE files SET size=?, mtime_ns=?, rel_path=?, dir=?, stem=?, ext=?, kind=?,
                          sidecar_path=?, pair_group=?, pair_role=?, hidden_from_review=?,
                          proxy_ready=0, width=NULL, height=NULL, captured_at=NULL, camera=NULL, orientation=NULL,
                          sharpness=NULL, sharpness_pct=NULL, exposure=NULL, phash=NULL, analyzed_at=NULL,
                          analysis_error=NULL, suggestion=NULL, confidence=NULL, reason=NULL,
                          burst_id=NULL, burst_size=NULL, burst_rank=NULL, duplicate_group=NULL,
                          duplicate_size=NULL, suggested_at=NULL, status='pending', error=NULL, sha256=NULL,
                          updated_at=?
                        WHERE id=?
                        """,
                        (
                            entry.size,
                            entry.mtime_ns,
                            entry.rel_path,
                            str(entry.path.parent),
                            entry.path.stem,
                            entry.ext,
                            entry.kind,
                            str(entry.sidecar) if entry.sidecar else None,
                            entry.pair_group,
                            entry.pair_role,
                            1 if entry.hidden else 0,
                            now,
                            file_id,
                        ),
                    )
                    stats["updated"] += 1
                else:
                    conn.execute(
                        """
                        UPDATE files SET sidecar_path=?, pair_group=?, pair_role=?, hidden_from_review=?,
                          rel_path=?, status=CASE WHEN status='missing' THEN 'pending' ELSE status END,
                          updated_at=?
                        WHERE id=?
                        """,
                        (
                            str(entry.sidecar) if entry.sidecar else None,
                            entry.pair_group,
                            entry.pair_role,
                            1 if entry.hidden else 0,
                            entry.rel_path,
                            now,
                            file_id,
                        ),
                    )
                    stats["unchanged"] += 1

        existing_rows = conn.execute(
            "SELECT id, path, status FROM files WHERE project_id=?", (project_id,)
        ).fetchall()
        for row in existing_rows:
            if row["path"] not in scanned_paths and row["status"] != "missing":
                conn.execute(
                    "UPDATE files SET status='missing', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                stats["missing"] += 1

        # Resolve pair partner ids now that all rows exist.
        for entry in scan.entries:
            if not entry.pair_group:
                continue
            file_id = ids_by_path.get(str(entry.path))
            if not file_id:
                continue
            partner_role = "raw" if entry.pair_role == "jpeg" else "jpeg"
            partner = conn.execute(
                "SELECT id FROM files WHERE project_id=? AND pair_group=? AND pair_role=? LIMIT 1",
                (project_id, entry.pair_group, partner_role),
            ).fetchone()
            conn.execute(
                "UPDATE files SET paired_file_id=? WHERE id=?",
                (partner["id"] if partner else None, file_id),
            )
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))

    stats["source_dir"] = str(source_dir)
    return stats
