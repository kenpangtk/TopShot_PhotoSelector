from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from topshot_engine import analysis, config, copy as copy_engine, db, imaging, ingest, jobs, preferences, rpc


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_image(path: Path, width: int = 900, height: int = 600, seed: int = 1, flat: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if flat:
        array = np.full((height, width, 3), 128, dtype=np.uint8)
    else:
        rng = np.random.default_rng(seed)
        base = rng.integers(20, 230, size=(height // 8, width // 8, 3), dtype=np.uint8)
        array = np.kron(base, np.ones((8, 8, 1), dtype=np.uint8))[:height, :width, :3]
    Image.fromarray(array, "RGB").save(path, format="JPEG", quality=90)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "appdata"
    monkeypatch.setenv("TOPSHOT_DATA_DIR", str(data))
    db.init_db()
    return tmp_path


def create_project(conn, source: Path, destination: Path | None = None, mode: str = "both") -> int:
    now = db.utc_now()
    cur = conn.execute(
        "INSERT INTO projects(name, source_dir, destination_dir, raw_jpeg_mode, status, created_at, updated_at) "
        "VALUES(?,?,?,?, 'ready', ?, ?)",
        (source.name, str(source), str(destination) if destination else None, mode, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_file(conn, project_id: int, path: Path, *, selected: bool = True, kind: str = "jpeg",
             sidecar: Path | None = None, pair_group: str | None = None, pair_role: str | None = None,
             hidden: bool = False) -> int:
    stat = path.stat() if path.exists() else None
    now = db.utc_now()
    cur = conn.execute(
        """
        INSERT INTO files(project_id, path, rel_path, dir, stem, ext, kind, size, mtime_ns, sidecar_path,
                          pair_group, pair_role, hidden_from_review, status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'ready', ?, ?)
        """,
        (
            project_id,
            str(path),
            path.name,
            str(path.parent),
            path.stem,
            path.suffix.lower(),
            kind,
            stat.st_size if stat else 10,
            stat.st_mtime_ns if stat else 1,
            str(sidecar) if sidecar else None,
            pair_group,
            pair_role,
            1 if hidden else 0,
            now,
            now,
        ),
    )
    file_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO decisions(user_id, file_id, decision, selected, user_set, updated_at) VALUES(?,?,?,?,0,?)",
        (1, file_id, "none", 1 if selected else 0, now),
    )
    conn.commit()
    return file_id


def test_scan_skips_videos_and_hidden_and_pairs_raw(env):
    source = env / "source"
    source.mkdir()
    make_image(source / "a.JPG")
    (source / "a.xmp").write_text("<xmp/>")
    (source / "a.CR2").write_bytes(b"raw-bytes")
    make_image(source / "sub" / "b.jpg")
    (source / "clip.mov").write_bytes(b"video")
    (source / "clip.MP4").write_bytes(b"video")
    (source / "note.png").write_bytes(b"png")
    (source / ".hidden.jpg").write_bytes(b"hidden")
    (source / ".secret").mkdir()
    make_image(source / ".secret" / "hidden.jpg")

    result = ingest.scan_source(source)
    assert result.videos and len(result.videos) == 2
    assert len(result.unsupported) == 1
    assert result.sidecars == 1
    assert {entry.path.name for entry in result.entries} == {"a.JPG", "a.CR2", "b.jpg"}
    raw = next(entry for entry in result.entries if entry.kind == "raw")
    jpeg = next(entry for entry in result.entries if entry.path.name == "a.JPG")
    assert raw.hidden and raw.pair_role == "raw"
    assert jpeg.pair_role == "jpeg" and not jpeg.hidden
    assert raw.sidecar and raw.sidecar.name == "a.xmp"
    assert jpeg.sidecar and jpeg.sidecar.name == "a.xmp"

    conn = db.connect()
    project_id = create_project(conn, source)
    stats = ingest.upsert_scan(conn, project_id, result, source)
    assert stats["new"] == 3
    assert stats["videos"] == 2
    rows = db.rows_to_dicts(conn.execute("SELECT * FROM files ORDER BY path").fetchall())
    raw_row = next(row for row in rows if row["kind"] == "raw")
    jpeg_row = next(row for row in rows if row["path"].endswith("a.JPG"))
    assert raw_row["hidden_from_review"] == 1
    assert raw_row["paired_file_id"] == jpeg_row["id"]
    assert jpeg_row["paired_file_id"] == raw_row["id"]

    # unchanged rescan preserves rows, decisions, and detects missing.
    again = ingest.upsert_scan(conn, project_id, ingest.scan_source(source), source)
    assert again["new"] == 0 and again["updated"] == 0
    (source / "sub" / "b.jpg").unlink()
    missing = ingest.upsert_scan(conn, project_id, ingest.scan_source(source), source)
    assert missing["missing"] == 1
    conn.close()


def test_proxy_sizes_and_orientation(env):
    source = env / "source"
    source.mkdir()
    path = source / "rotated.jpg"
    img = Image.new("RGB", (2000, 1000), (40, 80, 120))
    exif = Image.Exif()
    exif[274] = 6
    img.save(path, exif=exif)
    meta = imaging.generate_proxy(path, "jpeg", 7, 11)
    assert meta["width"] == 1000 and meta["height"] == 2000
    preview = Image.open(config.proxy_dir(7) / "11_p.jpg")
    thumb = Image.open(config.proxy_dir(7) / "11_t.jpg")
    assert preview.size == (800, 1600)
    assert max(thumb.size) == 320
    data = imaging.analyze_thumb(config.proxy_dir(7) / "11_t.jpg")
    assert "phash" in data and len(data["phash"]) == 16
    assert len(data["phash_dct"]) == 16
    assert data["face_count"] == 0
    assert data["smile_count"] == 0 and data["eye_count"] == 0
    assert data["exposure"] > 0


def test_pipeline_analysis_bursts_duplicates_and_preselection(env):
    source = env / "source"
    source.mkdir()
    make_image(source / "20260101_120000.jpg", seed=1)
    make_image(source / "20260101_120001.jpg", seed=1)  # identical -> burst + duplicate
    make_image(source / "20260101_120002.jpg", seed=2)
    make_image(source / "20260101_130000.jpg", seed=1)  # exact duplicate, later
    make_image(source / "20260101_140000.jpg", seed=3, flat=True)
    make_image(source / "20260101_150000.jpg", seed=4)
    conn = db.connect()
    project_id = create_project(conn, source)
    conn.close()

    job = jobs.manager.start_pipeline(project_id)
    deadline = time.time() + 60
    while time.time() < deadline:
        snapshot = jobs.manager.get(job["id"]).snapshot()
        if snapshot["status"] in ("done", "failed", "canceled"):
            break
        time.sleep(0.1)
    snapshot = jobs.manager.get(job["id"]).snapshot()
    assert snapshot["status"] == "done", snapshot
    assert snapshot["result"]["scan"]["videos"] == 0

    conn = db.connect()
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT f.*, d.selected FROM files f JOIN decisions d ON d.user_id=1 AND d.file_id=f.id ORDER BY f.path"
        ).fetchall()
    )
    assert len(rows) == 6
    for row in rows:
        assert row["proxy_ready"] == 1
        assert row["sharpness"] is not None
        assert row["suggestion"] in ("keep", "review", "reject", "alternate")
        assert row["face_count"] is not None
        assert row["interesting_score"] is not None and 0 <= row["interesting_score"] <= 1
        assert row["rank_score"] is not None and 0 <= row["rank_score"] <= 1
    flat = next(row for row in rows if row["path"].endswith("140000.jpg"))
    assert flat["suggestion"] in ("reject", "review")
    burst_rows = [row for row in rows if row["burst_id"] is not None]
    assert len(burst_rows) >= 2
    assert len({row["burst_id"] for row in burst_rows}) >= 1
    for burst_id in {row["burst_id"] for row in burst_rows}:
        members = [row for row in burst_rows if row["burst_id"] == burst_id]
        selected = sum(1 for row in members if row["selected"])
        if any(row["suggestion"] == "keep" for row in members):
            assert selected == 1
        else:
            assert selected == 0 and all(row["suggestion"] == "review" for row in members)
        assert any(row["burst_rank"] == 1 for row in members)
    duplicate = [row for row in rows if row["duplicate_group"] is not None]
    assert duplicate
    assert any(row["selected"] for row in rows)
    conn.close()

    # Keep target: select the top N composite-ranked photos and persist it.
    updated = rpc.project_update({"projectId": project_id, "keepTarget": 2})
    assert updated["project"]["keepTarget"] == 2
    target_data = rpc.photos_list({"projectId": project_id})
    assert target_data["counts"]["keep"] == 2
    assert target_data["counts"]["selected"] == 2
    assert 0 <= target_data["counts"]["people"] <= len(target_data["photos"])
    selected_rows = [photo for photo in target_data["photos"] if photo["selected"]]
    assert len(selected_rows) == 2
    assert all(photo["suggestion"] == "keep" for photo in selected_rows)

    # Manual edits survive preset changes, but re-applying a target re-culls
    # exactly N photos.
    manual = next(photo for photo in target_data["photos"] if not photo["selected"] and photo["suggestion"] != "alternate")
    rpc.photo_update({"fileId": manual["id"], "selected": True})
    rpc.project_update({"projectId": project_id, "preset": "balanced"})
    after_preset = rpc.photos_list({"projectId": project_id})
    kept = next(photo for photo in after_preset["photos"] if photo["id"] == manual["id"])
    assert kept["selected"] is True
    rpc.project_update({"projectId": project_id, "keepTarget": 1})
    after = rpc.photos_list({"projectId": project_id})
    assert after["counts"]["selected"] == 1
    assert after["counts"]["keep"] == 1


def test_copy_verified_collision_sidecar_and_repeat_skip(env):
    source = env / "source"
    dest = env / "dest"
    source.mkdir()
    dest.mkdir()
    image = source / "IMG_0001.jpg"
    make_image(image)
    sidecar = source / "IMG_0001.xmp"
    sidecar.write_text("<xmp>rating</xmp>")
    before = {path.name: sha(path) for path in source.iterdir()}
    (dest / "IMG_0001.jpg").write_bytes(b"different existing file")

    conn = db.connect()
    project_id = create_project(conn, source, dest)
    file_id = add_file(conn, project_id, image, sidecar=sidecar)
    conn.close()

    pre = copy_engine.preflight(db.connect(), project_id, [file_id], str(dest))
    assert pre["ok"] and pre["files"] == 2

    result = copy_engine.run_copy(project_id, [file_id], str(dest), threading.Event(), lambda data: None)
    assert result["status"] == "completed"
    assert result["counts"]["copied"] == 2
    assert result["counts"]["failed"] == 0
    copied = dest / "IMG_0001 (1).jpg"
    copied_sidecar = dest / "IMG_0001 (1).xmp"
    assert copied.read_bytes() == image.read_bytes()
    assert copied_sidecar.read_text() == "<xmp>rating</xmp>"
    assert (dest / "IMG_0001.jpg").read_bytes() == b"different existing file"
    assert {path.name: sha(path) for path in source.iterdir()} == before

    # Repeat run: identical files are skipped, no renamed duplicate is created.
    result2 = copy_engine.run_copy(project_id, [file_id], str(dest), threading.Event(), lambda data: None)
    assert result2["counts"]["skipped"] == 2
    assert result2["counts"]["copied"] == 0
    assert not (dest / "IMG_0001 (2).jpg").exists()

    conn = db.connect()
    logs = db.rows_to_dicts(
        conn.execute("SELECT * FROM copy_log WHERE run_id=? ORDER BY id", (result["runId"],)).fetchall()
    )
    assert all(log["src_sha256"] == log["dest_sha256"] for log in logs if log["status"] == "copied")
    run = conn.execute("SELECT * FROM copy_runs WHERE id=?", (result["runId"],)).fetchone()
    assert run["copied"] == 2 and run["skipped"] == 0
    conn.close()


def test_copy_pair_modes_and_missing(env):
    source = env / "source"
    dest = env / "dest"
    source.mkdir()
    dest.mkdir()
    jpeg = source / "PAIR.jpg"
    raw = source / "PAIR.CR2"
    make_image(jpeg)
    raw.write_bytes(b"raw-data")
    other = source / "OTHER.jpg"
    make_image(other, seed=2)
    missing = source / "GONE.jpg"

    conn = db.connect()
    project_id = create_project(conn, source, dest, mode="both")
    jpeg_id = add_file(conn, project_id, jpeg, pair_group="pair", pair_role="jpeg")
    raw_id = add_file(conn, project_id, raw, kind="raw", pair_group="pair", pair_role="raw", hidden=True)
    other_id = add_file(conn, project_id, other)
    missing_id = add_file(conn, project_id, missing)

    plan = copy_engine.build_copy_plan(conn, project_id, [jpeg_id])
    assert {image["id"] for image in plan} == {jpeg_id, raw_id}
    conn.execute("UPDATE projects SET raw_jpeg_mode='raw' WHERE id=?", (project_id,))
    conn.commit()
    assert [image["id"] for image in copy_engine.build_copy_plan(conn, project_id, [jpeg_id])] == [raw_id]
    conn.execute("UPDATE projects SET raw_jpeg_mode='jpeg' WHERE id=?", (project_id,))
    conn.commit()
    assert [image["id"] for image in copy_engine.build_copy_plan(conn, project_id, [jpeg_id])] == [jpeg_id]
    conn.close()

    result = copy_engine.run_copy(project_id, [other_id, missing_id], str(dest), threading.Event(), lambda data: None)
    assert result["counts"]["copied"] == 1
    assert result["counts"]["missing"] == 1
    assert (dest / "OTHER.jpg").exists()


def test_copy_rejects_destination_inside_source(env):
    source = env / "source"
    source.mkdir()
    inner = source / "out"
    inner.mkdir()
    image = source / "a.jpg"
    make_image(image)
    conn = db.connect()
    project_id = create_project(conn, source, inner)
    file_id = add_file(conn, project_id, image)
    conn.close()
    with pytest.raises(copy_engine.CopyError):
        copy_engine.run_copy(project_id, [file_id], str(inner), threading.Event(), lambda data: None)
    assert not any(inner.iterdir())


def test_copy_cancel_and_checksum_failure(env, monkeypatch):
    source = env / "source"
    dest = env / "dest"
    source.mkdir()
    dest.mkdir()
    image = source / "a.jpg"
    make_image(image)
    conn = db.connect()
    project_id = create_project(conn, source, dest)
    file_id = add_file(conn, project_id, image)
    conn.close()

    cancel = threading.Event()
    cancel.set()
    result = copy_engine.run_copy(project_id, [file_id], str(dest), cancel, lambda data: None)
    assert result["status"] == "canceled"
    assert not list(dest.iterdir())

    original = copy_engine.sha256_file

    def bad_hash(path: Path) -> str:
        if path.name.startswith(copy_engine.TEMP_PREFIX):
            return "0" * 64
        return original(path)

    monkeypatch.setattr(copy_engine, "sha256_file", bad_hash)
    result = copy_engine.run_copy(project_id, [file_id], str(dest), threading.Event(), lambda data: None)
    assert result["counts"]["failed"] == 1
    assert result["counts"]["copied"] == 0
    monkeypatch.setattr(copy_engine, "sha256_file", original)
    assert not [path for path in dest.iterdir() if not path.name.startswith(copy_engine.TEMP_PREFIX)]


def test_copy_cancel_mid_run_resumes_without_duplicates(env):
    source = env / "source"
    dest = env / "dest"
    source.mkdir()
    dest.mkdir()
    images = []
    for index in range(4):
        path = source / f"frame_{index}.jpg"
        make_image(path, seed=index + 10)
        images.append(path)
    conn = db.connect()
    project_id = create_project(conn, source, dest)
    file_ids = [add_file(conn, project_id, path) for path in images]
    conn.close()

    cancel = threading.Event()

    def progress(data):
        if data.get("done", 0) >= 1:
            cancel.set()

    result = copy_engine.run_copy(project_id, file_ids, str(dest), cancel, progress)
    assert result["status"] == "canceled"
    assert result["counts"]["copied"] >= 1
    first_run_files = {path.name for path in dest.iterdir()}
    assert first_run_files

    resumed = copy_engine.run_copy(project_id, file_ids, str(dest), threading.Event(), lambda data: None)
    assert resumed["status"] == "completed"
    assert resumed["counts"]["failed"] == 0
    assert resumed["counts"]["copied"] + resumed["counts"]["skipped"] == len(images)
    assert resumed["counts"]["skipped"] >= 1
    assert {path.name for path in dest.iterdir()} == {path.name for path in images}


def test_keep_target_avoids_near_duplicates_and_prefers_people_quality():
    def row(file_id, rank, phash, dct, quality=0.0):
        return {
            "id": file_id,
            "rel_path": f"frame_{file_id}.jpg",
            "stem": f"frame_{file_id}",
            "rank_score": rank,
            "people_quality": quality,
            "face_count": 1 if quality else 0,
            "eyes_open_score": 1.0 if quality else 0.0,
            "smile_score": 1.0 if quality else 0.0,
            "interesting_score": rank,
            "suggestion": "keep",
            "confidence": 0.55,
            "burst_id": None,
            "burst_size": None,
            "burst_rank": None,
            "duplicate_group": None,
            "duplicate_size": None,
            "status": "ready",
            "phash": phash,
            "phash_dct": dct,
        }

    rows = [
        row(1, 0.90, "0000000000000000", "0000000000000000"),
        row(2, 0.89, "0000000000000000", "0000000000000000"),
        row(3, 0.88, "ffffffffffffffff", "ffffffffffffffff"),
        row(4, 0.70, "00ff00ff00ff00ff", "0f0f0f0f0f0f0f0f", quality=1.0),
    ]
    chosen = analysis._apply_keep_target(rows, 2)
    assert 1 in chosen
    assert 2 not in chosen  # near-identical to 1
    assert 3 in chosen      # different scene
    assert rows[0]["suggestion"] == "keep"
    assert rows[1]["suggestion"] in ("review", "reject")


def test_group_pick_swaps_alternate_and_clears(env):
    source = env / "source"
    source.mkdir()
    paths = []
    for index in range(3):
        path = source / f"similar_{index}.jpg"
        make_image(path, seed=20 + index)
        paths.append(path)
    conn = db.connect()
    project_id = create_project(conn, source)
    ids = [add_file(conn, project_id, path) for path in paths]
    with conn:
        conn.execute("UPDATE files SET duplicate_group=1, duplicate_size=3 WHERE project_id=?", (project_id,))
        conn.execute("UPDATE files SET suggestion='keep' WHERE id=?", (ids[0],))
        conn.execute("UPDATE files SET suggestion='review' WHERE id IN (?,?)", (ids[1], ids[2]))
    conn.close()

    result = rpc.group_pick({"projectId": project_id, "kind": "duplicate", "groupId": 1, "fileId": ids[1]})
    photos = {photo["id"]: photo for photo in result["photos"]}
    assert photos[ids[1]]["selected"] is True
    assert photos[ids[1]]["decision"] == "keep"
    assert photos[ids[0]]["selected"] is False
    assert photos[ids[0]]["decision"] == "alternate"
    assert photos[ids[2]]["selected"] is False
    assert result["counts"]["selected"] == 1

    cleared = rpc.group_pick({"projectId": project_id, "kind": "duplicate", "groupId": 1, "fileId": None})
    assert all(photo["selected"] is False for photo in cleared["photos"])
    assert all(photo["decision"] == "alternate" for photo in cleared["photos"])


def test_documentation_scoring_demotes_text_and_appliance_shots(env):
    normal = {
        "vision_labels": '[["people",0.9],["adult",0.9],["sign",0.5]]',
        "vision_text_lines": 3,
        "vision_text_chars": 21,
        "vision_text_area": 0.01,
        "face_count": 2,
        "people_area": 0.06,
    }
    score, _ = analysis._documentation(normal)
    assert score < 0.3
    assert analysis._documentation({})[0] == 0.0
    assert analysis._documentation(
        {
            "vision_labels": '[["document",0.97],["screenshot",0.97]]',
            "vision_text_lines": 23,
            "vision_text_chars": 406,
            "vision_text_area": 0.12,
            "face_count": 0,
            "people_area": 0.0,
        }
    )[0] > 0.8
    assert analysis._documentation(
        {
            "vision_labels": '[["machine",0.91],["appliance",0.91],["laundry_machine",0.91]]',
            "vision_text_lines": 10,
            "vision_text_chars": 54,
            "vision_text_area": 0.005,
            "face_count": 0,
            "people_area": 0.0,
        }
    )[0] > 0.8
    # A low-confidence document label is enough when it is the top scene label.
    assert analysis._documentation(
        {
            "vision_labels": '[["document",0.14],["handwriting",0.12],["sticky_note",0.08]]',
            "vision_text_lines": 0,
            "vision_text_chars": 0,
            "face_count": 0,
            "people_area": 0.0,
        }
    )[0] >= 0.55
    # A dominant sign is documentation even with little OCR text.
    assert analysis._documentation(
        {
            "vision_labels": '[["structure",0.76],["fence",0.63],["sign",0.59]]',
            "vision_text_lines": 2,
            "vision_text_chars": 6,
            "face_count": 0,
            "people_area": 0.0,
        }
    )[0] >= 0.45


def test_documentation_shots_are_not_selected_by_keep_target(env):
    source = env / "source"
    source.mkdir()
    paths = [source / f"frame{index}.jpg" for index in range(4)]
    for index, path in enumerate(paths):
        make_image(path, seed=index + 1)
    conn = db.connect()
    project_id = create_project(conn, source)
    ids = [add_file(conn, project_id, path) for path in paths]
    hashes = ["ffffffffffffffff", "0000000000000000", "aaaaaaaaaaaaaaaa", "5555555555555555"]
    for index, file_id in enumerate(ids):
        conn.execute(
            """
            UPDATE files SET sharpness=?, colorfulness=?, contrast=?, edge_density=?,
                face_count=0, people_area=0, smile_count=0, eye_count=0, eyes_expected=0,
                exposure=128, phash=?, phash_dct=?
            WHERE id=?
            """,
            (
                50000 - index * 1000,
                40 - index,
                50,
                0.15,
                hashes[index],
                hashes[index],
                file_id,
            ),
        )
    # The sharpest frame is a screenshot; the target must skip it.
    conn.execute(
        """
        UPDATE files SET vision_labels=?, vision_text_lines=23, vision_text_chars=406,
            vision_text_area=0.12
        WHERE id=?
        """,
        ('[["document",0.97],["screenshot",0.97]]', ids[0]),
    )
    db.update_user_settings(conn, 1, project_id, {"keep_target": 2})
    conn.commit()
    summary = analysis.recompute(conn, project_id, "aggressive")
    rows = {
        row["id"]: row
        for row in db.rows_to_dicts(
            conn.execute("SELECT f.*, d.selected FROM files f JOIN decisions d ON d.user_id=1 AND d.file_id=f.id").fetchall()
        )
    }
    assert rows[ids[0]]["selected"] == 0
    assert rows[ids[0]]["suggestion"] == "review"
    assert "Documentation" in (rows[ids[0]]["reason"] or "")
    assert rows[ids[0]]["documentation_score"] > 0.8
    assert sum(1 for row in rows.values() if row["selected"]) == 2
    assert summary["documentation"] == 1

    # A manual selection is preserved and counts toward the target.
    manual = next(
        row["id"]
        for row in rows.values()
        if row["id"] != ids[0] and not row["selected"]
    )
    conn.execute(
        "UPDATE decisions SET selected=1, user_set=1 WHERE user_id=1 AND file_id=?",
        (manual,),
    )
    conn.commit()
    analysis.recompute(conn, project_id, "aggressive")
    after = db.rows_to_dicts(
        conn.execute("SELECT f.id, d.selected, d.user_set FROM files f JOIN decisions d ON d.user_id=1 AND d.file_id=f.id").fetchall()
    )
    assert sum(1 for row in after if row["selected"]) == 2
    assert next(row for row in after if row["id"] == manual)["selected"] == 1
    conn.close()


def test_profiles_isolate_decisions_settings_and_copy(env):
    source = env / "source"
    source.mkdir()
    a = source / "a.jpg"
    b = source / "b.jpg"
    make_image(a, seed=11)
    make_image(b, seed=12)
    dest1 = env / "dest1"
    dest2 = env / "dest2"
    dest1.mkdir()
    dest2.mkdir()
    conn = db.connect()
    project_id = create_project(conn, source)
    file_a = add_file(conn, project_id, a, selected=True)
    file_b = add_file(conn, project_id, b, selected=False)
    user2 = db.create_user(conn, "Alex")
    conn.commit()
    db.update_user_settings(conn, 1, project_id, {"keep_target": 1, "initialized": 1})
    db.update_user_settings(conn, user2["id"], project_id, {"keep_target": 1, "initialized": 1})
    conn.close()

    rpc.photo_update({"fileId": file_a, "userId": 1, "selected": True})
    rpc.photo_update({"fileId": file_b, "userId": 1, "selected": False})
    rpc.photo_update({"fileId": file_a, "userId": user2["id"], "selected": False})
    rpc.photo_update({"fileId": file_b, "userId": user2["id"], "selected": True})

    first = rpc.photos_list({"projectId": project_id, "userId": 1})
    second = rpc.photos_list({"projectId": project_id, "userId": user2["id"]})
    first_selected = {photo["id"] for photo in first["photos"] if photo["selected"]}
    second_selected = {photo["id"] for photo in second["photos"] if photo["selected"]}
    assert first_selected == {file_a}
    assert second_selected == {file_b}
    assert first["counts"]["selectedImages"] == 1
    assert second["counts"]["selectedImages"] == 1

    rpc.project_update({"projectId": project_id, "userId": 1, "destinationDir": str(dest1)})
    rpc.project_update({"projectId": project_id, "userId": user2["id"], "destinationDir": str(dest2)})
    assert rpc.project_list({"userId": 1})["projects"][0]["destinationDir"] == str(dest1)
    assert rpc.project_list({"userId": user2["id"]})["projects"][0]["destinationDir"] == str(dest2)

    preflight = rpc.copy_preflight({"projectId": project_id, "userId": user2["id"]})
    assert preflight["destination"] == str(dest2)
    assert preflight["images"] == 1

    rpc.users_delete({"userId": user2["id"]})
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE user_id=1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE user_id=?", (user2["id"],)).fetchone()[0] == 0
    conn.close()


def test_preference_events_and_models_are_per_profile(env):
    source = env / "source"
    source.mkdir()
    paths = [source / f"frame{index}.jpg" for index in range(20)]
    for index, path in enumerate(paths):
        make_image(path, seed=index + 20)
    conn = db.connect()
    project_id = create_project(conn, source)
    ids = [add_file(conn, project_id, path) for path in paths]
    user2 = db.create_user(conn, "Alex")
    conn.commit()
    rows = db.rows_to_dicts(conn.execute("SELECT * FROM files WHERE project_id=?", (project_id,)).fetchall())
    positives = rows[:10]
    negatives = rows[10:]
    for row in positives:
        row.update(
            sharpness_pct=0.9,
            colorfulness=60,
            contrast=70,
            edge_density=0.2,
            people_score=0.8,
            people_quality=0.9,
            interesting_score=0.85,
            face_count=2,
            people_area=0.08,
            documentation_score=0.0,
            smile_score=1.0,
            eyes_open_score=1.0,
        )
    for row in negatives:
        row.update(
            sharpness_pct=0.1,
            colorfulness=5,
            contrast=10,
            edge_density=0.9,
            people_score=0.0,
            people_quality=0.0,
            interesting_score=0.1,
            face_count=0,
            people_area=0.0,
            documentation_score=0.95,
            smile_score=0.0,
            eyes_open_score=0.0,
        )
    with conn:
        for row in positives:
            preferences.record_event(conn, user2["id"], project_id, row["id"], row, "select", 5)
        for row in negatives:
            preferences.record_event(conn, user2["id"], project_id, row["id"], row, "reject", 1)
    model = preferences.load_model(conn, user2["id"])
    assert model is not None and model["active"]
    assert preferences.load_model(conn, 1) is None
    assert preferences.summary(conn, 1)["events"] == 0
    assert preferences.summary(conn, user2["id"])["events"] == 20
    assert preferences.score(model, positives[0]) > 0
    assert preferences.score(model, negatives[0]) < 0

    with conn:
        preferences.reset(conn, user2["id"])
    assert preferences.load_model(conn, user2["id"]) is None
    assert preferences.summary(conn, user2["id"])["events"] == 0
    conn.close()
