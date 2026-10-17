from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__, analysis, config, copy as copy_engine, db, jobs, preferences


def _project(conn: sqlite3.Connection, project_id: int, user_id: int = 1) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown project {project_id}")
    settings = db.user_settings(conn, user_id, project_id)
    data = dict(row)
    data["lastScan"] = json.loads(data["last_scan_json"]) if data.get("last_scan_json") else None
    data.pop("last_scan_json", None)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE project_id=? AND hidden_from_review=0", (project_id,)
    ).fetchone()
    data["photoCount"] = int(count["n"]) if count else 0
    return {
        "id": data["id"],
        "userId": user_id,
        "name": data["name"],
        "sourceDir": data["source_dir"],
        "destinationDir": settings.get("destination_dir"),
        "rawJpegMode": settings.get("raw_jpeg_mode") or "both",
        "preset": settings.get("preset") or "conservative",
        "keepTarget": settings.get("keep_target"),
        "initialized": bool(settings.get("initialized")),
        "status": data["status"],
        "photoCount": data["photoCount"],
        "lastScan": data["lastScan"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _effective_state(suggestion: str | None, decision: str | None) -> str:
    if decision == "reject":
        return "reject"
    if decision == "alternate":
        return "review"
    if decision == "keep":
        return "keep"
    if suggestion == "keep":
        return "keep"
    if suggestion == "reject":
        return "reject"
    return "review"


def _photo(row: dict[str, Any]) -> dict[str, Any]:
    suggestion = row.get("user_suggestion") or row["suggestion"] or "review"
    confidence = row.get("user_confidence")
    if confidence is None:
        confidence = row["confidence"]
    reason = row.get("user_reason") or row["reason"]
    base_rank = float(row["rank_score"] or 0.0)
    preference = float(row.get("preference_score") or 0.0)
    return {
        "id": row["id"],
        "name": row["stem"] + row["ext"],
        "relPath": row["rel_path"],
        "size": row["size"],
        "width": row["width"],
        "height": row["height"],
        "capturedAt": row["captured_at"],
        "camera": row["camera"],
        "suggestion": suggestion,
        "effectiveState": _effective_state(suggestion, row.get("decision")),
        "confidence": confidence,
        "reason": reason,
        "selected": bool(row["selected"]),
        "decision": row["decision"] or "none",
        "rating": row["rating"],
        "burstId": row["burst_id"],
        "burstSize": row["burst_size"],
        "burstRank": row["burst_rank"],
        "duplicateGroup": row["duplicate_group"],
        "duplicateSize": row["duplicate_size"],
        "pairRole": row["pair_role"],
        "pairedFileId": row["paired_file_id"],
        "kind": row["kind"],
        "status": row["status"],
        "error": row["error"] or row["analysis_error"],
        "sidecar": bool(row["sidecar_path"]),
        "sha256": row["sha256"],
        "sharpness": row["sharpness"],
        "sharpnessPct": row["sharpness_pct"],
        "peopleCount": row["face_count"] or 0,
        "peopleScore": row["people_score"],
        "smileCount": row["smile_count"] or 0,
        "eyeCount": row["eye_count"] or 0,
        "eyesExpected": row["eyes_expected"] or 0,
        "smileScore": row["smile_score"],
        "eyesOpenScore": row["eyes_open_score"],
        "peopleQuality": row["people_quality"],
        "interestingScore": row["interesting_score"],
        "rankScore": base_rank + preference,
        "baseRankScore": base_rank,
        "preferenceScore": preference,
        "documentationScore": row.get("documentation_score"),
        "documentationDetail": row.get("documentation_detail"),
        "thumbUrl": f"/proxy/{row['id']}/thumb",
        "previewUrl": f"/proxy/{row['id']}/preview",
    }


def _visible_rows(conn: sqlite3.Connection, project_id: int, user_id: int = 1) -> list[dict[str, Any]]:
    rows = db.rows_to_dicts(
        conn.execute(
            """
            SELECT f.*, d.selected, d.decision, d.rating, d.user_set,
                   d.suggestion AS user_suggestion, d.confidence AS user_confidence,
                   d.reason AS user_reason, d.preference_score
            FROM files f LEFT JOIN decisions d ON d.user_id=? AND d.file_id=f.id
            WHERE f.project_id=? AND f.hidden_from_review=0
            ORDER BY f.captured_at IS NULL, f.captured_at, f.rel_path COLLATE NOCASE
            """,
            (user_id, project_id),
        ).fetchall()
    )
    for row in rows:
        row["selected"] = int(row["selected"] or 0)
    return rows


def _counts_for(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "all": len(rows),
        "keep": 0,
        "review": 0,
        "reject": 0,
        "selected": 0,
        "people": 0,
        "duplicates": 0,
        "documentation": 0,
    }
    for row in rows:
        state = _effective_state(
            row.get("user_suggestion") or row.get("suggestion"), row.get("decision")
        )
        counts[state] = counts.get(state, 0) + 1
        if row["selected"]:
            counts["selected"] += 1
        if int(row.get("face_count") or 0) > 0:
            counts["people"] += 1
        if row.get("duplicate_group"):
            counts["duplicates"] += 1
        if float(row.get("documentation_score") or 0.0) >= analysis.DOCUMENTATION_SELECT_LIMIT:
            counts["documentation"] += 1
    return counts


def _selection_summary(conn: sqlite3.Connection, project: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, int]:
    mode = project.get("rawJpegMode") or "both"
    selected = [row for row in rows if row["selected"] and row["status"] != "missing"]
    seen_groups: set[str] = set()
    chosen: list[dict[str, Any]] = []
    pair_cache: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        group = row.get("pair_group")
        if group:
            if group in seen_groups:
                continue
            seen_groups.add(group)
            if group not in pair_cache:
                pair_cache[group] = db.rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM files WHERE project_id=? AND pair_group=?",
                        (project["id"], group),
                    ).fetchall()
                )
            members = pair_cache[group]
            jpeg = next((m for m in members if m.get("pair_role") == "jpeg"), None)
            raw = next((m for m in members if m.get("pair_role") == "raw"), None)
            if mode == "both":
                chosen.extend(members)
            elif mode == "raw":
                chosen.append(raw or row)
            else:
                chosen.append(jpeg or row)
        else:
            chosen.append(row)
    sidecars = {row["sidecar_path"] for row in chosen if row.get("sidecar_path")}
    image_bytes = sum(int(row["size"]) for row in chosen)
    sidecar_bytes = 0
    for sidecar in sidecars:
        try:
            sidecar_bytes += os.path.getsize(sidecar)
        except OSError:
            pass
    return {
        "selectedImages": len(chosen),
        "selectedBytes": image_bytes + sidecar_bytes,
        "sidecars": len(sidecars),
    }


def photos_list(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        project = _project(conn, project_id, user_id)
        settings = db.user_settings(conn, user_id, project_id)
        if project["status"] == "ready" and project["photoCount"] > 0 and not settings.get("initialized"):
            with conn:
                analysis.recompute(
                    conn,
                    project_id,
                    settings.get("preset") or "conservative",
                    user_id,
                    settings.get("keep_target"),
                )
            project = _project(conn, project_id, user_id)
        rows = _visible_rows(conn, project_id, user_id)
        counts = _counts_for(rows)
        summary = _selection_summary(conn, project, rows)
        bursts = db.rows_to_dicts(
            conn.execute(
                "SELECT burst_id, cover_file_id, size, weak FROM bursts WHERE project_id=? ORDER BY burst_id",
                (project_id,),
            ).fetchall()
        )
        return {
            "project": project,
            "photos": [_photo(row) for row in rows],
            "counts": {**counts, **summary},
            "bursts": [
                {
                    "id": burst["burst_id"],
                    "coverId": burst["cover_file_id"],
                    "size": burst["size"],
                    "weak": bool(burst["weak"]),
                }
                for burst in bursts
            ],
        }
    finally:
        conn.close()


def project_list(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM projects ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return {"projects": [_project(conn, int(row["id"]), user_id) for row in rows]}
    finally:
        conn.close()


def project_create(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params.get("userId") or 1)
    source_text = (params.get("sourceDir") or "").strip()
    if not source_text:
        raise ValueError("sourceDir is required")
    source = Path(source_text).expanduser().resolve()
    if not source.exists():
        raise ValueError(f"Source folder does not exist: {source}")
    if not source.is_dir():
        raise ValueError("Choose a folder, not a file.")
    conn = db.connect()
    try:
        existing = conn.execute("SELECT id FROM projects WHERE source_dir=?", (str(source),)).fetchone()
        if existing:
            project_id = int(existing["id"])
            db.user_settings(conn, user_id, project_id)
            project = _project(conn, project_id, user_id)
            job = jobs.manager.start_pipeline(project_id, user_id) if project["status"] != "ready" else None
            if project["status"] == "ready":
                settings = db.user_settings(conn, user_id, project_id)
                if not settings.get("initialized") and project["photoCount"] > 0:
                    with conn:
                        analysis.recompute(
                            conn,
                            project_id,
                            settings.get("preset") or "conservative",
                            user_id,
                            settings.get("keep_target"),
                        )
                project = _project(conn, project_id, user_id)
            return {"project": project, "job": job, "existing": True}
        now = db.utc_now()
        name = (params.get("name") or source.name or "Untitled shoot").strip()
        with conn:
            cur = conn.execute(
                "INSERT INTO projects(name, source_dir, status, created_at, updated_at) VALUES(?,?, 'created', ?, ?)",
                (name, str(source), now, now),
            )
            project_id = int(cur.lastrowid)
        with conn:
            db.user_settings(conn, user_id, project_id)
        project = _project(conn, project_id, user_id)
        job = jobs.manager.start_pipeline(project_id, user_id)
        return {"project": project, "job": job, "existing": False}
    finally:
        conn.close()


def project_update(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        project = _project(conn, project_id, user_id)
        updates: list[str] = []
        values: list[Any] = []
        settings_updates: dict[str, Any] = {}
        if "name" in params and params["name"]:
            updates.append("name=?")
            values.append(str(params["name"]).strip() or project["name"])
        if "destinationDir" in params:
            destination = params["destinationDir"]
            if destination:
                destination_path = Path(str(destination)).expanduser().resolve()
                if not destination_path.is_dir():
                    raise ValueError("Destination folder does not exist.")
                settings_updates["destination_dir"] = str(destination_path)
            else:
                settings_updates["destination_dir"] = None
        if "rawJpegMode" in params:
            mode = str(params["rawJpegMode"])
            if mode not in config.RAW_JPEG_MODES:
                raise ValueError("rawJpegMode must be both, raw, or jpeg")
            settings_updates["raw_jpeg_mode"] = mode
        if "preset" in params:
            preset = str(params["preset"])
            if preset not in config.PRESETS:
                raise ValueError("Unknown preset")
            settings_updates["preset"] = preset
        if "keepTarget" in params:
            target = params["keepTarget"]
            if target in (None, "", 0, "0"):
                settings_updates["keep_target"] = None
            else:
                try:
                    target_value = int(target)
                except (TypeError, ValueError):
                    raise ValueError("keepTarget must be a positive number")
                if target_value < 1:
                    raise ValueError("keepTarget must be a positive number")
                settings_updates["keep_target"] = target_value
        with conn:
            if updates:
                updates.append("updated_at=?")
                values.append(db.utc_now())
                values.append(project_id)
                conn.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id=?", tuple(values))
            if settings_updates:
                db.update_user_settings(conn, user_id, project_id, settings_updates)
        if "keepTarget" in params:
            # Changing this profile's target is an explicit re-cull: clear its
            # manual markers so the requested N is exact.
            with conn:
                conn.execute(
                    "UPDATE decisions SET user_set=0, decision='none', rating=NULL, updated_at=? "
                    "WHERE user_id=? AND file_id IN (SELECT id FROM files WHERE project_id=?)",
                    (db.utc_now(), user_id, project_id),
                )
        if "preset" in params or "keepTarget" in params:
            settings = db.user_settings(conn, user_id, project_id)
            with conn:
                analysis.recompute(
                    conn,
                    project_id,
                    settings.get("preset") or "conservative",
                    user_id,
                    settings.get("keep_target"),
                )
        return {"project": _project(conn, project_id, user_id)}
    finally:
        conn.close()


def project_rescan(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    return {"job": jobs.manager.start_pipeline(project_id, user_id)}


def photo_update(params: dict[str, Any]) -> dict[str, Any]:
    file_id = int(params["fileId"])
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown photo")
        row_dict = dict(row)
        decision = params.get("decision")
        if decision is not None and decision not in ("none", "keep", "reject", "alternate"):
            raise ValueError("decision must be none, keep, reject, or alternate")
        rating = params.get("rating", "unchanged")
        if rating == "unchanged":
            rating_value = "unchanged"
        elif rating is None:
            rating_value = None
        else:
            rating_value = int(rating)
            if rating_value < 1 or rating_value > 5:
                raise ValueError("rating must be 1-5 or null")
        selected = params.get("selected")
        now = db.utc_now()
        current = conn.execute(
            "SELECT * FROM decisions WHERE user_id=? AND file_id=?", (user_id, file_id)
        ).fetchone()
        old_selected = int(current["selected"]) if current else 0
        old_decision = current["decision"] if current else "none"
        old_rating = current["rating"] if current else None
        new_selected = old_selected
        new_decision = old_decision
        new_rating = old_rating
        if decision is not None:
            new_decision = decision
            if decision in ("reject", "alternate"):
                new_selected = 0
            elif decision == "keep":
                new_selected = 1
        if rating_value != "unchanged" and rating_value != old_rating:
            new_rating = rating_value
            if rating_value is not None:
                new_decision = "keep"
                new_selected = 1
        if selected is not None:
            new_selected = 1 if selected else 0
        changed = (new_selected, new_decision, new_rating) != (old_selected, old_decision, old_rating)
        with conn:
            conn.execute(
                "INSERT INTO decisions(user_id, file_id, decision, selected, rating, user_set, updated_at) "
                "VALUES(?,?,?,?,?,1,?) ON CONFLICT(user_id, file_id) DO UPDATE SET "
                "decision=excluded.decision, selected=excluded.selected, rating=excluded.rating, "
                "user_set=1, updated_at=excluded.updated_at",
                (user_id, file_id, new_decision, new_selected, new_rating, now),
            )
            if changed:
                if new_selected != old_selected:
                    action = "select" if new_selected else "deselect"
                elif new_decision != old_decision:
                    action = new_decision if new_decision in ("keep", "reject", "alternate") else "clear"
                else:
                    action = "rate"
                project_id = int(row_dict["project_id"])
                preferences.record_event(
                    conn, user_id, project_id, file_id, row_dict, action, new_rating
                )
                conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        row = conn.execute(
            """
            SELECT f.*, d.selected, d.decision, d.rating, d.user_set,
                   d.suggestion AS user_suggestion, d.confidence AS user_confidence,
                   d.reason AS user_reason, d.preference_score
            FROM files f LEFT JOIN decisions d ON d.user_id=? AND d.file_id=f.id WHERE f.id=?
            """,
            (user_id, file_id),
        ).fetchone()
        project_id = int(row["project_id"])
        project = _project(conn, project_id, user_id)
        rows = _visible_rows(conn, project_id, user_id)
        counts = _counts_for(rows)
        summary = _selection_summary(conn, project, rows)
        return {
            "photo": _photo(dict(row)),
            "counts": {**counts, **summary},
        }
    finally:
        conn.close()


def group_pick(params: dict[str, Any]) -> dict[str, Any]:
    """Pick one photo from a similar/burst group, or clear the pick with fileId=null."""
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    kind = str(params.get("kind") or "")
    group_id = params.get("groupId")
    file_id = params.get("fileId")
    if kind not in ("duplicate", "burst"):
        raise ValueError("kind must be duplicate or burst")
    if group_id is None:
        raise ValueError("groupId is required")
    conn = db.connect()
    try:
        column = "duplicate_group" if kind == "duplicate" else "burst_id"
        member_rows = db.rows_to_dicts(
            conn.execute(
                f"SELECT * FROM files WHERE project_id=? AND {column}=? ORDER BY id",
                (project_id, int(group_id)),
            ).fetchall()
        )
        if not member_rows:
            raise ValueError("Group not found")
        member_ids = {int(member["id"]) for member in member_rows}
        chosen_id = int(file_id) if file_id is not None else None
        if chosen_id is not None and chosen_id not in member_ids:
            raise ValueError("Photo is not part of this group")
        now = db.utc_now()
        with conn:
            for member in member_rows:
                member_id = int(member["id"])
                chosen = chosen_id == member_id
                conn.execute(
                    "INSERT INTO decisions(user_id, file_id, decision, selected, rating, user_set, updated_at) "
                    "VALUES(?,?,?,?,NULL,1,?) "
                    "ON CONFLICT(user_id, file_id) DO UPDATE SET decision=excluded.decision, "
                    "selected=excluded.selected, user_set=1, updated_at=excluded.updated_at",
                    (
                        user_id,
                        member_id,
                        "keep" if chosen else "alternate",
                        1 if chosen else 0,
                        now,
                    ),
                )
                if chosen_id is None:
                    action = "deselect"
                else:
                    action = "keep" if chosen else "alternate"
                preferences.record_event(conn, user_id, project_id, member_id, member, action, None)
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        photos = []
        for member_id in sorted(member_ids):
            row = conn.execute(
                """
                SELECT f.*, d.selected, d.decision, d.rating, d.user_set,
                       d.suggestion AS user_suggestion, d.confidence AS user_confidence,
                       d.reason AS user_reason, d.preference_score
                FROM files f LEFT JOIN decisions d ON d.user_id=? AND d.file_id=f.id WHERE f.id=?
                """,
                (user_id, member_id),
            ).fetchone()
            if row is not None:
                photos.append(_photo(dict(row)))
        project = _project(conn, project_id, user_id)
        rows = _visible_rows(conn, project_id, user_id)
        counts = _counts_for(rows)
        summary = _selection_summary(conn, project, rows)
        return {"photos": photos, "counts": {**counts, **summary}}
    finally:
        conn.close()


def copy_preflight(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    file_ids = [int(value) for value in params.get("fileIds") or []]
    destination = params.get("destination")
    conn = db.connect()
    try:
        settings = db.user_settings(conn, user_id, project_id)
        if not file_ids:
            rows = conn.execute(
                "SELECT d.file_id FROM decisions d JOIN files f ON f.id=d.file_id "
                "WHERE d.user_id=? AND f.project_id=? AND d.selected=1 AND f.status!='missing' "
                "AND f.hidden_from_review=0",
                (user_id, project_id),
            ).fetchall()
            file_ids = [int(row["file_id"]) for row in rows]
        destination = destination or settings.get("destination_dir")
        return copy_engine.preflight(
            conn,
            project_id,
            file_ids,
            destination,
            settings.get("raw_jpeg_mode") or "both",
        )
    finally:
        conn.close()


def copy_run(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    file_ids = [int(value) for value in params.get("fileIds") or []]
    destination = params.get("destination")
    conn = db.connect()
    try:
        settings = db.user_settings(conn, user_id, project_id)
        if not file_ids:
            rows = conn.execute(
                "SELECT d.file_id FROM decisions d JOIN files f ON f.id=d.file_id "
                "WHERE d.user_id=? AND f.project_id=? AND d.selected=1 AND f.status!='missing' "
                "AND f.hidden_from_review=0",
                (user_id, project_id),
            ).fetchall()
            file_ids = [int(row["file_id"]) for row in rows]
        if not file_ids:
            raise ValueError("No confirmed keeps selected for copying.")
        destination = destination or settings.get("destination_dir")
        if destination:
            destination_path = Path(str(destination)).expanduser().resolve()
            if not destination_path.is_dir():
                raise ValueError("Destination folder does not exist.")
            destination = str(destination_path)
        job = jobs.manager.start_copy(
            project_id,
            user_id,
            file_ids,
            destination,
            settings.get("raw_jpeg_mode") or "both",
        )
        return {"job": job}
    finally:
        conn.close()


def copy_last(params: dict[str, Any]) -> dict[str, Any]:
    project_id = int(params["projectId"])
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        return {"run": copy_engine.last_copy(conn, project_id, user_id)}
    finally:
        conn.close()


def users_list(_: dict[str, Any]) -> dict[str, Any]:
    conn = db.connect()
    try:
        users = db.list_users(conn)
        for user in users:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM decisions WHERE user_id=? AND selected=1", (user["id"],)
            ).fetchone()
            user["selectedImages"] = int(count["n"]) if count else 0
        return {"users": [
            {
                "id": user["id"],
                "name": user["name"],
                "color": user["color"],
                "selectedImages": user["selectedImages"],
                "lastActiveAt": user["last_active_at"],
                "createdAt": user["created_at"],
            }
            for user in users
        ]}
    finally:
        conn.close()


def users_create(params: dict[str, Any]) -> dict[str, Any]:
    conn = db.connect()
    try:
        with conn:
            user = db.create_user(conn, str(params.get("name") or ""), params.get("color"))
        return {"user": {
            "id": user["id"],
            "name": user["name"],
            "color": user["color"],
            "selectedImages": 0,
            "lastActiveAt": user["last_active_at"],
            "createdAt": user["created_at"],
        }}
    finally:
        conn.close()


def users_update(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params["userId"])
    conn = db.connect()
    try:
        with conn:
            user = db.update_user(
                conn,
                user_id,
                str(params["name"]) if "name" in params else None,
                params.get("color"),
            )
        return {"user": {
            "id": user["id"],
            "name": user["name"],
            "color": user["color"],
            "lastActiveAt": user["last_active_at"],
            "createdAt": user["created_at"],
        }}
    finally:
        conn.close()


def users_delete(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params["userId"])
    conn = db.connect()
    try:
        with conn:
            db.delete_user(conn, user_id)
        return {"deleted": user_id}
    finally:
        conn.close()


def users_activate(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params["userId"])
    conn = db.connect()
    try:
        with conn:
            db.touch_user(conn, user_id)
        return {"ok": True}
    finally:
        conn.close()


def preferences_summary(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params.get("userId") or 1)
    conn = db.connect()
    try:
        return {"preferences": preferences.summary(conn, user_id)}
    finally:
        conn.close()


def preferences_reset(params: dict[str, Any]) -> dict[str, Any]:
    user_id = int(params.get("userId") or 1)
    project_id = params.get("projectId")
    conn = db.connect()
    try:
        with conn:
            preferences.reset(conn, user_id)
            if project_id is not None:
                settings = db.user_settings(conn, user_id, int(project_id))
                analysis.recompute(
                    conn,
                    int(project_id),
                    settings.get("preset") or "conservative",
                    user_id,
                    settings.get("keep_target"),
                )
        return {"preferences": preferences.summary(conn, user_id)}
    finally:
        conn.close()


def job_status(params: dict[str, Any]) -> dict[str, Any]:
    job = jobs.manager.get(str(params["jobId"]))
    if job is None:
        raise ValueError("Unknown job")
    return {"job": job.snapshot()}


def job_cancel(params: dict[str, Any]) -> dict[str, Any]:
    return {"job": jobs.manager.cancel(str(params["jobId"]))}


def app_info(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "dataDir": str(config.data_dir()),
        "modelsEnabled": False,
        "privacy": "All processing is local. No photos, filenames, or metadata are uploaded.",
    }


METHODS = {
    "app.info": app_info,
    "users.list": users_list,
    "users.create": users_create,
    "users.update": users_update,
    "users.delete": users_delete,
    "users.activate": users_activate,
    "preferences.summary": preferences_summary,
    "preferences.reset": preferences_reset,
    "project.list": project_list,
    "project.create": project_create,
    "project.update": project_update,
    "project.rescan": project_rescan,
    "photos.list": photos_list,
    "photo.update": photo_update,
    "group.pick": group_pick,
    "copy.preflight": copy_preflight,
    "copy.run": copy_run,
    "copy.last": copy_last,
    "job.status": job_status,
    "job.cancel": job_cancel,
}


def handle(method: str, params: dict[str, Any] | None) -> Any:
    function = METHODS.get(method)
    if function is None:
        raise MethodNotFound(method)
    return function(params or {})


class MethodNotFound(Exception):
    pass
