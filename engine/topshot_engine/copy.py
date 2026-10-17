from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import db

CHUNK = 4 * 1024 * 1024
MAX_SUFFIX = 9999
TEMP_PREFIX = ".topshot-tmp-"
DISK_MARGIN_BYTES = 64 * 1024 * 1024


class CopyError(RuntimeError):
    pass


class ChecksumMismatch(CopyError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise CopyError(f"Unknown project {project_id}")
    return dict(row)


def _rows_for_ids(conn: sqlite3.Connection, project_id: int, file_ids: list[int]) -> list[dict[str, Any]]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT * FROM files WHERE project_id=? AND id IN ({placeholders})", (project_id, *file_ids)
    ).fetchall()
    if len(rows) != len(set(file_ids)):
        found = {row["id"] for row in rows}
        missing = sorted(set(file_ids) - found)
        raise CopyError(f"Selection contains unknown files: {missing}")
    return db.rows_to_dicts(rows)


def build_copy_plan(
    conn: sqlite3.Connection,
    project_id: int,
    file_ids: list[int],
    raw_mode: str | None = None,
) -> list[dict[str, Any]]:
    project = _project(conn, project_id)
    mode = raw_mode or project.get("raw_jpeg_mode") or "both"
    if mode not in ("both", "raw", "jpeg"):
        mode = "both"
    selected = _rows_for_ids(conn, project_id, file_ids)
    chosen: dict[int, dict[str, Any]] = {}

    for row in selected:
        group = row.get("pair_group")
        if not group:
            chosen[int(row["id"])] = row
            continue
        members = db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM files WHERE project_id=? AND pair_group=?", (project_id, group)
            ).fetchall()
        )
        jpeg = next((member for member in members if member.get("pair_role") == "jpeg"), None)
        raw = next((member for member in members if member.get("pair_role") == "raw"), None)
        if mode == "both":
            for member in members:
                chosen[int(member["id"])] = member
        elif mode == "raw":
            chosen[int((raw or row)["id"])] = raw or row
        else:
            chosen[int((jpeg or row)["id"])] = jpeg or row

    plan = []
    for row in sorted(chosen.values(), key=lambda item: item["path"]):
        plan.append(
            {
                "id": int(row["id"]),
                "path": row["path"],
                "name": os.path.basename(row["path"]),
                "size": int(row["size"]),
                "kind": row["kind"],
                "sidecar_path": row.get("sidecar_path"),
                "pair_group": row.get("pair_group"),
            }
        )
    return plan


def _dest_is_safe(source_dir: Path, destination: Path) -> str | None:
    try:
        source = source_dir.resolve()
        dest = destination.resolve()
    except OSError as exc:
        return str(exc)
    if dest == source:
        return "Destination cannot be the source folder."
    if source in dest.parents:
        return "Destination cannot be inside the source folder (sources are read-only)."
    if not dest.exists():
        return "Destination folder does not exist."
    if not dest.is_dir():
        return "Destination is not a folder."
    return None


def _writable_probe(dest: Path) -> tuple[bool, str | None]:
    if not os.access(dest, os.W_OK | os.X_OK):
        return False, "Destination folder is not writable."
    probe = dest / f"{TEMP_PREFIX}probe-{uuid.uuid4().hex}"
    try:
        with probe.open("xb") as handle:
            handle.write(b"topshot")
        probe.unlink()
        return True, None
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Destination is not writable: {exc}"


def _sidecar_destination_name(image_name: str, sidecar_path: str) -> str:
    sidecar = Path(sidecar_path)
    suffix = sidecar.suffix or ".xmp"
    return f"{Path(image_name).stem}{suffix}"


def _unique_sidecar_sources(plan: list[dict[str, Any]]) -> list[str]:
    sources = {image["sidecar_path"] for image in plan if image.get("sidecar_path")}
    return sorted(sources)


def preflight(
    conn: sqlite3.Connection,
    project_id: int,
    file_ids: list[int],
    destination: str | None = None,
    raw_mode: str | None = None,
) -> dict[str, Any]:
    project = _project(conn, project_id)
    dest_text = destination or project.get("destination_dir")
    if not dest_text:
        return {
            "ok": False,
            "error": "Choose a destination folder first.",
            "sourceDir": project["source_dir"],
            "destination": None,
            "images": 0,
            "sidecars": 0,
            "files": 0,
            "bytes": 0,
            "missing": [],
            "writable": False,
        }
    dest = Path(dest_text).expanduser()
    source_dir = Path(project["source_dir"])
    plan = build_copy_plan(conn, project_id, file_ids, raw_mode)
    missing: list[dict[str, Any]] = []
    total_bytes = 0
    for image in plan:
        path = Path(image["path"])
        readable = False
        try:
            readable = path.is_file() and os.access(path, os.R_OK)
            size = path.stat().st_size
        except OSError:
            size = 0
        if not readable:
            missing.append({"id": image["id"], "name": image["name"], "path": image["path"], "kind": "image"})
            continue
        total_bytes += int(size)
    sidecars = _unique_sidecar_sources(plan)
    for sidecar in sidecars:
        try:
            if os.path.isfile(sidecar) and os.access(sidecar, os.R_OK):
                total_bytes += os.path.getsize(sidecar)
            else:
                missing.append({"id": None, "name": os.path.basename(sidecar), "path": sidecar, "kind": "sidecar"})
        except OSError:
            missing.append({"id": None, "name": os.path.basename(sidecar), "path": sidecar, "kind": "sidecar"})

    error = _dest_is_safe(source_dir, dest)
    writable, write_error = (False, error) if error else _writable_probe(dest)
    if write_error:
        error = write_error
    free_bytes = 0
    try:
        free_bytes = shutil.disk_usage(dest).free
    except OSError:
        pass
    if not error and free_bytes < total_bytes + DISK_MARGIN_BYTES:
        error = "Not enough free space in the destination folder."
    return {
        "ok": error is None,
        "error": error,
        "sourceDir": project["source_dir"],
        "destination": str(dest),
        "images": len(plan) - len([item for item in missing if item["kind"] == "image"]),
        "sidecars": len([item for item in sidecars if item not in {m["path"] for m in missing}]),
        "files": len(plan) + len(sidecars),
        "bytes": total_bytes,
        "missing": missing,
        "writable": writable,
        "freeBytes": free_bytes,
        "rawJpegMode": project.get("raw_jpeg_mode") or "both",
    }


def _candidate_names(dest_dir: Path, name: str) -> list[Path]:
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidates: list[Path] = []
    index = 0
    while index <= MAX_SUFFIX:
        candidate = dest_dir / (name if index == 0 else f"{stem} ({index}){suffix}")
        if not candidate.exists():
            break
        candidates.append(candidate)
        index += 1
    return candidates


def _find_identical(dest_dir: Path, name: str, src_path: Path, src_size: int) -> tuple[Path | None, str | None]:
    """Return an existing byte-identical destination and the source hash.

    Only hashes when a same-name/same-size candidate exists, so fresh runs do a
    single read of each source while repeat runs skip without creating suffixes.
    """
    candidates = [candidate for candidate in _candidate_names(dest_dir, name) if candidate.is_file()]
    size_matches: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.stat().st_size == src_size:
                size_matches.append(candidate)
        except OSError:
            continue
    if not size_matches:
        return None, None
    source_sha = sha256_file(src_path)
    for candidate in size_matches:
        try:
            if sha256_file(candidate) == source_sha:
                return candidate, source_sha
        except OSError:
            continue
    return None, source_sha


def _free_name(dest_dir: Path, name: str, reserved: set[str]) -> Path:
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 0
    while index <= MAX_SUFFIX:
        candidate = dest_dir / (name if index == 0 else f"{stem} ({index}){suffix}")
        if str(candidate).lower() not in reserved and not candidate.exists():
            return candidate
        index += 1
    raise CopyError(f"Could not find a free filename for {name}")


def _copy_verified(src: Path, dest_dir: Path, dest_name: str, reserved: set[str], expected_sha: str | None = None) -> tuple[Path, str, str, int]:
    tmp = dest_dir / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    src_digest = hashlib.sha256()
    size = 0
    try:
        with src.open("rb") as fin, tmp.open("xb") as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                src_digest.update(chunk)
                fout.write(chunk)
                size += len(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        src_sha = src_digest.hexdigest()
        if expected_sha and src_sha != expected_sha:
            raise ChecksumMismatch("Source changed while copying.")
        dest_sha = sha256_file(tmp)
        if dest_sha != src_sha:
            raise ChecksumMismatch("Destination checksum does not match source.")
        final = _free_name(dest_dir, dest_name, reserved)
        link_attempts = 0
        while True:
            try:
                os.link(tmp, final)
                break
            except FileExistsError:
                link_attempts += 1
                if link_attempts > MAX_SUFFIX:
                    raise CopyError(f"Could not find a free filename for {dest_name}")
                reserved.add(str(final).lower())
                final = _free_name(dest_dir, dest_name, reserved)
        reserved.add(str(final).lower())
        tmp.unlink()
        try:
            dir_fd = os.open(dest_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return final, src_sha, dest_sha, size
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _clean_stale_temps(dest_dir: Path) -> None:
    cutoff = time.time() - 3600
    try:
        for entry in dest_dir.iterdir():
            if entry.name.startswith(TEMP_PREFIX) and entry.is_file():
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def run_copy(
    project_id: int,
    file_ids: list[int],
    destination: str | None,
    cancel_event: threading.Event,
    progress: Callable[[dict[str, Any]], None],
    user_id: int = 1,
    raw_mode: str | None = None,
) -> dict[str, Any]:
    conn = db.connect()
    run_id = 0
    try:
        project = _project(conn, project_id)
        dest_text = destination or project.get("destination_dir")
        if not dest_text:
            raise CopyError("Choose a destination folder first.")
        dest_dir = Path(dest_text).expanduser()
        safety_error = _dest_is_safe(Path(project["source_dir"]), dest_dir)
        if safety_error:
            raise CopyError(safety_error)
        writable, write_error = _writable_probe(dest_dir)
        if not writable:
            raise CopyError(write_error or "Destination is not writable.")
        plan = build_copy_plan(conn, project_id, file_ids, raw_mode)
        if not plan:
            raise CopyError("No confirmed keeps selected for copying.")
        sidecar_sources = _unique_sidecar_sources(plan)
        total_images = len(plan)
        total_files = total_images + len(sidecar_sources)
        total_bytes = 0
        for image in plan:
            try:
                total_bytes += os.path.getsize(image["path"])
            except OSError:
                pass
        for sidecar in sidecar_sources:
            try:
                total_bytes += os.path.getsize(sidecar)
            except OSError:
                pass
        free = shutil.disk_usage(dest_dir).free
        if free < total_bytes + DISK_MARGIN_BYTES:
            raise CopyError("Not enough free space in the destination folder.")

        now = db.utc_now()
        with conn:
            cur = conn.execute(
                "INSERT INTO copy_runs(project_id, user_id, destination, status, total_files, started_at) "
                "VALUES(?, ?, ?, 'running', ?, ?)",
                (project_id, user_id, str(dest_dir), total_files, now),
            )
            run_id = int(cur.lastrowid)
        with conn:
            db.update_user_settings(conn, user_id, project_id, {"destination_dir": str(dest_dir)})
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))

        counters = {
            "copied": 0,
            "skipped": 0,
            "failed": 0,
            "missing": 0,
            "imagesCopied": 0,
            "sidecarsCopied": 0,
            "bytesCopied": 0,
            "totalImages": total_images,
            "totalFiles": total_files,
            "totalBytes": total_bytes,
        }
        reserved: set[str] = set()
        processed_sidecars: set[str] = set()
        processed = 0
        processed_bytes = 0
        failures: list[dict[str, Any]] = []
        _clean_stale_temps(dest_dir)
        cancel_event = cancel_event or threading.Event()

        def log(file_id: int | None, kind: str, source: str, dest: str | None, status: str,
                src_sha: str | None = None, dest_sha: str | None = None, size: int = 0,
                error: str | None = None) -> None:
            with conn:
                conn.execute(
                    "INSERT INTO copy_log(run_id, file_id, kind, source_path, dest_path, src_sha256, "
                    "dest_sha256, bytes, status, error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, file_id, kind, source, dest, src_sha, dest_sha, size, status, error, db.utc_now()),
                )

        def emit(image_name: str | None = None) -> None:
            progress(
                {
                    **counters,
                    "done": processed,
                    "total": total_files,
                    "bytesDone": processed_bytes,
                    "bytesTotal": total_bytes,
                    "message": image_name,
                    "failures": failures[-20:],
                }
            )

        cancelled = False
        for image in plan:
            if cancel_event.is_set():
                cancelled = True
                break
            src = Path(image["path"])
            name = image["name"]
            size_hint = int(image["size"])
            processed += 1
            if not src.is_file() or not os.access(src, os.R_OK):
                counters["missing"] += 1
                failures.append({"name": name, "error": "Source file is missing"})
                log(image["id"], "image", str(src), None, "missing", size=size_hint, error="Source file is missing")
                processed_bytes += size_hint
                emit(name)
                continue
            try:
                size = src.stat().st_size
                existing_path, src_sha = _find_identical(dest_dir, name, src, size)
                if existing_path is not None:
                    final = existing_path
                    dest_sha = src_sha
                    counters["skipped"] += 1
                    log(image["id"], "image", str(src), str(final), "skipped", src_sha, dest_sha, size)
                    with conn:
                        conn.execute("UPDATE files SET sha256=? WHERE id=?", (src_sha, image["id"]))
                else:
                    final, src_sha, dest_sha, size = _copy_verified(
                        src, dest_dir, name, reserved, expected_sha=src_sha
                    )
                    counters["copied"] += 1
                    counters["imagesCopied"] += 1
                    counters["bytesCopied"] += size
                    log(image["id"], "image", str(src), str(final), "copied", src_sha, dest_sha, size)
                    with conn:
                        conn.execute("UPDATE files SET sha256=? WHERE id=?", (src_sha, image["id"]))
                processed_bytes += size
                emit(name)

                sidecar = image.get("sidecar_path")
                if sidecar:
                    sidecar_path = Path(sidecar)
                    sidecar_name = _sidecar_destination_name(final.name, sidecar)
                    key = str((dest_dir / sidecar_name)).lower()
                    if key in processed_sidecars:
                        continue
                    processed_sidecars.add(key)
                    if not sidecar_path.is_file():
                        counters["missing"] += 1
                        failures.append({"name": sidecar_path.name, "error": "XMP sidecar is missing"})
                        log(image["id"], "sidecar", str(sidecar_path), None, "missing", error="XMP sidecar is missing")
                        continue
                    if cancel_event.is_set():
                        cancelled = True
                        break
                    try:
                        sidecar_size = sidecar_path.stat().st_size
                        skip_sidecar, src_sha = _find_identical(
                            dest_dir, sidecar_name, sidecar_path, sidecar_size
                        )
                        if skip_sidecar is not None:
                            counters["skipped"] += 1
                            log(image["id"], "sidecar", str(sidecar_path), str(skip_sidecar), "skipped",
                                src_sha, src_sha, sidecar_size)
                        else:
                            sidecar_final, src_sha, dest_sha, sidecar_size = _copy_verified(
                                sidecar_path, dest_dir, sidecar_name, reserved, expected_sha=src_sha
                            )
                            counters["copied"] += 1
                            counters["sidecarsCopied"] += 1
                            counters["bytesCopied"] += sidecar_size
                            log(image["id"], "sidecar", str(sidecar_path), str(sidecar_final), "copied",
                                src_sha, dest_sha, sidecar_size)
                        processed_bytes += sidecar_size
                    except Exception as exc:
                        counters["failed"] += 1
                        failures.append({"name": sidecar_name, "error": str(exc)})
                        log(image["id"], "sidecar", str(sidecar_path), None, "failed", error=str(exc))
            except Exception as exc:
                counters["failed"] += 1
                failures.append({"name": name, "error": str(exc)})
                log(image["id"], "image", str(src), None, "failed", error=str(exc))
                processed_bytes += size_hint
                emit(name)

        status = "canceled" if cancelled else ("failed" if counters["failed"] and not counters["copied"] and not counters["skipped"] else "completed")
        with conn:
            conn.execute(
                """
                UPDATE copy_runs SET status=?, copied=?, skipped=?, failed=?, missing=?, bytes_copied=?,
                    finished_at=? WHERE id=?
                """,
                (
                    status,
                    counters["copied"],
                    counters["skipped"],
                    counters["failed"],
                    counters["missing"],
                    counters["bytesCopied"],
                    db.utc_now(),
                    run_id,
                ),
            )
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (db.utc_now(), project_id))
        result = {
            "runId": run_id,
            "status": status,
            "destination": str(dest_dir),
            "counts": counters,
            "failures": failures,
        }
        emit("Done")
        return result
    except Exception as exc:
        if run_id:
            try:
                with conn:
                    conn.execute(
                        "UPDATE copy_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                        (str(exc), db.utc_now(), run_id),
                    )
            except Exception:
                pass
        raise
    finally:
        conn.close()


def last_copy(
    conn: sqlite3.Connection, project_id: int, user_id: int | None = None
) -> dict[str, Any] | None:
    if user_id is None:
        run = conn.execute(
            "SELECT * FROM copy_runs WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
        ).fetchone()
    else:
        run = conn.execute(
            "SELECT * FROM copy_runs WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (project_id, user_id),
        ).fetchone()
    if run is None:
        return None
    logs = conn.execute(
        "SELECT kind, source_path, dest_path, status, bytes, error, src_sha256, dest_sha256 "
        "FROM copy_log WHERE run_id=? ORDER BY id",
        (run["id"],),
    ).fetchall()
    data = dict(run)
    data["logs"] = db.rows_to_dicts(logs)
    return data
