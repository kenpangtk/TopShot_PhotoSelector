from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS user_project_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    destination_dir TEXT,
    raw_jpeg_mode TEXT NOT NULL DEFAULT 'both',
    preset TEXT NOT NULL DEFAULT 'conservative',
    keep_target INTEGER,
    initialized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, project_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_dir TEXT NOT NULL UNIQUE,
    destination_dir TEXT,
    raw_jpeg_mode TEXT NOT NULL DEFAULT 'both',
    preset TEXT NOT NULL DEFAULT 'conservative',
    keep_target INTEGER,
    learning_enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'created',
    last_scan_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    dir TEXT NOT NULL,
    stem TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sidecar_path TEXT,
    pair_group TEXT,
    pair_role TEXT,
    paired_file_id INTEGER,
    hidden_from_review INTEGER NOT NULL DEFAULT 0,
    width INTEGER,
    height INTEGER,
    captured_at REAL,
    camera TEXT,
    orientation INTEGER,
    proxy_ready INTEGER NOT NULL DEFAULT 0,
    sharpness REAL,
    sharpness_pct REAL,
    exposure REAL,
    phash TEXT,
    phash_dct TEXT,
    face_count INTEGER,
    people_area REAL,
    colorfulness REAL,
    contrast REAL,
    edge_density REAL,
    smile_count INTEGER,
    eye_count INTEGER,
    eyes_expected INTEGER,
    smile_score REAL,
    eyes_open_score REAL,
    people_quality REAL,
    people_score REAL,
    interesting_score REAL,
    rank_score REAL,
    documentation_score REAL,
    documentation_detail TEXT,
    vision_labels TEXT,
    vision_text_lines INTEGER,
    vision_text_chars INTEGER,
    vision_text_area REAL,
    vision_text_confidence REAL,
    analyzed_at TEXT,
    analysis_error TEXT,
    suggestion TEXT,
    confidence REAL,
    reason TEXT,
    burst_id INTEGER,
    burst_size INTEGER,
    burst_rank INTEGER,
    duplicate_group INTEGER,
    duplicate_size INTEGER,
    suggested_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_files_review ON files(project_id, hidden_from_review, status);
CREATE INDEX IF NOT EXISTS idx_files_burst ON files(project_id, burst_id);
CREATE INDEX IF NOT EXISTS idx_files_pair ON files(project_id, pair_group);

CREATE TABLE IF NOT EXISTS decisions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    decision TEXT NOT NULL DEFAULT 'none',
    selected INTEGER NOT NULL DEFAULT 0,
    rating INTEGER,
    user_set INTEGER NOT NULL DEFAULT 0,
    suggestion TEXT,
    confidence REAL,
    reason TEXT,
    preference_score REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, file_id)
);


CREATE TABLE IF NOT EXISTS bursts (
    project_id INTEGER NOT NULL,
    burst_id INTEGER NOT NULL,
    cover_file_id INTEGER,
    size INTEGER NOT NULL,
    weak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, burst_id)
);

CREATE TABLE IF NOT EXISTS copy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    copied INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    bytes_copied INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS copy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES copy_runs(id) ON DELETE CASCADE,
    file_id INTEGER,
    kind TEXT NOT NULL,
    source_path TEXT,
    dest_path TEXT,
    src_sha256 TEXT,
    dest_sha256 TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_log_run ON copy_log(run_id);

CREATE TABLE IF NOT EXISTS preference_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    rating INTEGER,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_preference_events_user ON preference_events(user_id, id);
CREATE INDEX IF NOT EXISTS idx_preference_events_file ON preference_events(user_id, file_id);

CREATE TABLE IF NOT EXISTS user_preference_models (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    model_json TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate_profiles(conn)
        _ensure_columns(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


def _migrate_profiles(conn: sqlite3.Connection) -> None:
    """Seed the Default profile and upgrade pre-profile databases."""
    now = utc_now()
    conn.execute(
        "INSERT OR IGNORE INTO users(id, name, color, created_at, updated_at, last_active_at) "
        "VALUES(1, 'Default', NULL, ?, ?, ?)",
        (now, now, now),
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "user_id" not in columns:
        conn.execute("DROP TABLE IF EXISTS decisions_new")
        conn.execute(
            """
            CREATE TABLE decisions_new (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                decision TEXT NOT NULL DEFAULT 'none',
                selected INTEGER NOT NULL DEFAULT 0,
                rating INTEGER,
                user_set INTEGER NOT NULL DEFAULT 0,
                suggestion TEXT,
                confidence REAL,
                reason TEXT,
                preference_score REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, file_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO decisions_new(user_id, file_id, decision, selected, rating, user_set, updated_at)
            SELECT 1, file_id, decision, selected, rating, user_set, updated_at FROM decisions
            """
        )
        conn.execute("DROP TABLE decisions")
        conn.execute("ALTER TABLE decisions_new RENAME TO decisions")
    decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id, selected)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_file ON decisions(file_id)")
    for name, kind in {
        "suggestion": "TEXT",
        "confidence": "REAL",
        "reason": "TEXT",
        "preference_score": "REAL",
    }.items():
        if name not in decision_columns:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {kind}")
    copy_columns = {row[1] for row in conn.execute("PRAGMA table_info(copy_runs)")}
    if "user_id" not in copy_columns:
        conn.execute("ALTER TABLE copy_runs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        INSERT OR IGNORE INTO user_project_settings(
            user_id, project_id, destination_dir, raw_jpeg_mode, preset, keep_target, initialized, updated_at
        )
        SELECT 1, id, destination_dir, raw_jpeg_mode, preset, keep_target, 1, updated_at FROM projects
        """
    )


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "keep_target" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN keep_target INTEGER")
    if "learning_enabled" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN learning_enabled INTEGER NOT NULL DEFAULT 1")
    file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    additions = {
        "phash_dct": "TEXT",
        "face_count": "INTEGER",
        "people_area": "REAL",
        "colorfulness": "REAL",
        "contrast": "REAL",
        "edge_density": "REAL",
        "smile_count": "INTEGER",
        "eye_count": "INTEGER",
        "eyes_expected": "INTEGER",
        "smile_score": "REAL",
        "eyes_open_score": "REAL",
        "people_quality": "REAL",
        "people_score": "REAL",
        "interesting_score": "REAL",
        "rank_score": "REAL",
        "documentation_score": "REAL",
        "documentation_detail": "TEXT",
        "vision_labels": "TEXT",
        "vision_text_lines": "INTEGER",
        "vision_text_chars": "INTEGER",
        "vision_text_area": "REAL",
        "vision_text_confidence": "REAL",
    }
    for name, kind in additions.items():
        if name not in file_columns:
            conn.execute(f"ALTER TABLE files ADD COLUMN {name} {kind}")


def mark_interrupted_projects() -> None:
    """Jobs are in-memory; a restart leaves any active project resumable."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE projects SET status='interrupted', updated_at=? "
            "WHERE status IN ('importing','analyzing')",
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    )


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown profile {user_id}")
    return dict(row)


def create_user(conn: sqlite3.Connection, name: str, color: str | None = None) -> dict[str, Any]:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("Profile name is required")
    now = utc_now()
    try:
        cur = conn.execute(
            "INSERT INTO users(name, color, created_at, updated_at, last_active_at) VALUES(?,?,?,?,?)",
            (clean, color, now, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("A profile with that name already exists") from exc
    return get_user(conn, int(cur.lastrowid))


def update_user(conn: sqlite3.Connection, user_id: int, name: str | None = None, color: str | None = None) -> dict[str, Any]:
    get_user(conn, user_id)
    updates: list[str] = []
    values: list[Any] = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("Profile name is required")
        updates.append("name=?")
        values.append(clean)
    if color is not None:
        updates.append("color=?")
        values.append(color or None)
    if updates:
        updates.append("updated_at=?")
        values.append(utc_now())
        values.append(user_id)
        try:
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", tuple(values))
        except sqlite3.IntegrityError as exc:
            raise ValueError("A profile with that name already exists") from exc
    return get_user(conn, user_id)


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if count and int(count["n"]) <= 1:
        raise ValueError("The last profile cannot be deleted")
    get_user(conn, user_id)
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def touch_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_active_at=?, updated_at=? WHERE id=?", (utc_now(), utc_now(), user_id))


def user_settings(conn: sqlite3.Connection, user_id: int, project_id: int) -> dict[str, Any]:
    get_user(conn, user_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ValueError(f"Unknown project {project_id}")
    row = conn.execute(
        "SELECT * FROM user_project_settings WHERE user_id=? AND project_id=?",
        (user_id, project_id),
    ).fetchone()
    if row is None:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO user_project_settings(
                user_id, project_id, destination_dir, raw_jpeg_mode, preset, keep_target, initialized, updated_at
            ) VALUES(?,?,NULL,?,?,NULL,0,?)
            """,
            (user_id, project_id, project["raw_jpeg_mode"] or "both", project["preset"] or "conservative", now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM user_project_settings WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        ).fetchone()
    return dict(row) if row else {}


def update_user_settings(conn: sqlite3.Connection, user_id: int, project_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    user_settings(conn, user_id, project_id)
    allowed = {"destination_dir", "raw_jpeg_mode", "preset", "keep_target", "initialized"}
    updates: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key}=?")
        values.append(value)
    if updates:
        updates.append("updated_at=?")
        values.append(utc_now())
        values.append(user_id)
        values.append(project_id)
        conn.execute(
            f"UPDATE user_project_settings SET {', '.join(updates)} WHERE user_id=? AND project_id=?",
            tuple(values),
        )
    return user_settings(conn, user_id, project_id)


def user_project_rows(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            "SELECT project_id, destination_dir, raw_jpeg_mode, preset, keep_target, initialized "
            "FROM user_project_settings WHERE user_id=?",
            (user_id,),
        ).fetchall()
    )
