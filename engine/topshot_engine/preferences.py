from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

import numpy as np

from . import db

FEATURE_NAMES = (
    "sharpness_pct",
    "sharpness_log",
    "colorfulness",
    "contrast",
    "edge_density",
    "people_score",
    "people_quality",
    "interesting_score",
    "face_count",
    "people_area",
    "documentation_score",
    "smile_score",
    "eyes_open_score",
    "exposure_quality",
)

MIN_EVENTS = 20
MIN_POSITIVES = 5
MIN_NEGATIVES = 5
MAX_ADJUSTMENT = 0.08
LABEL_PRIOR = 1.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _labels(row: dict[str, Any]) -> list[str]:
    raw = row.get("vision_labels")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    labels: list[str] = []
    if isinstance(parsed, list):
        for item in parsed[:8]:
            if isinstance(item, (list, tuple)) and item:
                labels.append(str(item[0]).lower())
    return labels


def features_for_row(row: dict[str, Any]) -> dict[str, Any]:
    sharpness = float(row.get("sharpness") or 0.0)
    exposure = float(row.get("exposure") or 128.0)
    exposure_quality = 1.0 if 35.0 <= exposure <= 225.0 else 0.5
    values = {
        "sharpness_pct": float(row.get("sharpness_pct") or 0.0),
        "sharpness_log": min(10.0, math.log1p(max(0.0, sharpness)) / 1.5),
        "colorfulness": min(2.0, float(row.get("colorfulness") or 0.0) / 50.0),
        "contrast": min(2.0, float(row.get("contrast") or 0.0) / 60.0),
        "edge_density": min(2.0, float(row.get("edge_density") or 0.0) * 8.0),
        "people_score": float(row.get("people_score") or 0.0),
        "people_quality": float(row.get("people_quality") or 0.0),
        "interesting_score": float(row.get("interesting_score") or 0.0),
        "face_count": min(6.0, math.log1p(max(0, int(row.get("face_count") or 0)))),
        "people_area": min(1.0, float(row.get("people_area") or 0.0) * 4.0),
        "documentation_score": float(row.get("documentation_score") or 0.0),
        "smile_score": float(row.get("smile_score") or 0.0),
        "eyes_open_score": float(row.get("eyes_open_score") or 0.0),
        "exposure_quality": exposure_quality,
    }
    return {"values": values, "labels": _labels(row)}


def _vector(features: dict[str, Any]) -> list[float]:
    values = features.get("values") or {}
    return [float(values.get(name) or 0.0) for name in FEATURE_NAMES]


def _sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-value))


def load_model(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT model_json, event_count, active FROM user_preference_models WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        model = json.loads(row["model_json"])
    except (TypeError, ValueError):
        return None
    model.setdefault("event_count", int(row["event_count"] or 0))
    model.setdefault("active", bool(row["active"]))
    return model


def event_count(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM preference_events WHERE user_id=?", (user_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


def _learning_enabled(conn: sqlite3.Connection, project_id: int) -> bool:
    row = conn.execute("SELECT learning_enabled FROM projects WHERE id=?", (project_id,)).fetchone()
    return bool(row is None or row["learning_enabled"])


def record_event(
    conn: sqlite3.Connection,
    user_id: int,
    project_id: int,
    file_id: int,
    row: dict[str, Any],
    action: str,
    rating: int | None = None,
) -> None:
    if not _learning_enabled(conn, project_id):
        return
    features = features_for_row(row)
    conn.execute(
        "INSERT INTO preference_events(user_id, project_id, file_id, action, rating, features_json, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, project_id, file_id, action, rating, json.dumps(features, separators=(",", ":")), db.utc_now()),
    )
    train(conn, user_id)


def _latest_events(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT id, file_id, action, rating, features_json FROM preference_events "
            "WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    )
    seen: set[int] = set()
    latest: list[dict[str, Any]] = []
    for row in rows:
        file_id = int(row["file_id"])
        if file_id in seen:
            continue
        seen.add(file_id)
        latest.append(row)
    return latest


def _label_of_event(event: dict[str, Any]) -> int:
    action = (event.get("action") or "").lower()
    rating = event.get("rating")
    if action in ("select", "keep"):
        return 1
    if action in ("deselect", "reject", "alternate"):
        return -1
    if action == "rate" and rating is not None:
        if int(rating) >= 4:
            return 1
        if int(rating) <= 2:
            return -1
    return 0


def train(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    events = _latest_events(conn, user_id)
    examples: list[tuple[list[float], int, list[str]]] = []
    label_stats: dict[str, list[int]] = {}
    for event in events:
        label = _label_of_event(event)
        if label == 0:
            continue
        try:
            features = json.loads(event["features_json"])
        except (TypeError, ValueError):
            continue
        examples.append((_vector(features), label, list(features.get("labels") or [])))
        for name in features.get("labels") or []:
            stats = label_stats.setdefault(str(name), [0, 0])
            if label > 0:
                stats[0] += 1
            else:
                stats[1] += 1

    positives = sum(1 for _, label, _ in examples if label > 0)
    negatives = sum(1 for _, label, _ in examples if label < 0)
    label_rates = {
        name: (pos + LABEL_PRIOR) / (pos + neg + 2 * LABEL_PRIOR)
        for name, (pos, neg) in label_stats.items()
        if pos + neg >= 2
    }
    active = len(examples) >= MIN_EVENTS and positives >= MIN_POSITIVES and negatives >= MIN_NEGATIVES
    model: dict[str, Any] = {
        "active": bool(active),
        "events": len(examples),
        "positives": positives,
        "negatives": negatives,
        "label_rates": label_rates,
        "trained_at": db.utc_now(),
        "weights": [],
        "mean": [],
        "std": [],
        "bias": 0.0,
    }
    if active:
        x = np.asarray([values for values, _, _ in examples], dtype=np.float64)
        y = np.asarray([1.0 if label > 0 else 0.0 for _, label, _ in examples], dtype=np.float64)
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        std[std < 1e-6] = 1.0
        xs = (x - mean) / std
        weights = np.zeros(xs.shape[1], dtype=np.float64)
        bias = 0.0
        positive_weight = negatives / max(1, positives)
        for _ in range(500):
            logits = xs @ weights + bias
            probs = _sigmoid(logits)
            error = (probs - y) * np.where(y > 0, positive_weight, 1.0)
            grad_w = xs.T @ error / len(y) + 0.01 * weights
            grad_b = float(error.mean())
            weights -= 0.35 * grad_w
            bias -= 0.35 * grad_b
        model.update(
            {
                "weights": weights.tolist(),
                "mean": mean.tolist(),
                "std": std.tolist(),
                "bias": float(bias),
            }
        )
    payload = json.dumps(model, separators=(",", ":"))
    conn.execute(
        "INSERT INTO user_preference_models(user_id, model_json, event_count, active, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET model_json=excluded.model_json, "
        "event_count=excluded.event_count, active=excluded.active, updated_at=excluded.updated_at",
        (user_id, payload, len(events), 1 if active else 0, db.utc_now()),
    )
    return model


def score(model: dict[str, Any] | None, row: dict[str, Any]) -> float:
    """Return a capped rank adjustment in [-MAX_ADJUSTMENT, MAX_ADJUSTMENT]."""
    if not model or not model.get("active"):
        return 0.0
    features = features_for_row(row)
    vector = np.asarray([_vector(features)], dtype=np.float64)
    mean = np.asarray(model.get("mean") or [0.0] * len(FEATURE_NAMES), dtype=np.float64)
    std = np.asarray(model.get("std") or [1.0] * len(FEATURE_NAMES), dtype=np.float64)
    weights = np.asarray(model.get("weights") or [0.0] * len(FEATURE_NAMES), dtype=np.float64)
    std[std < 1e-6] = 1.0
    probability = float(_sigmoid(((vector - mean) / std) @ weights + float(model.get("bias") or 0.0))[0])
    rates = model.get("label_rates") or {}
    labels = features.get("labels") or []
    sampled = [float(rates[name]) for name in labels[:5] if name in rates]
    label_rate = sum(sampled) / len(sampled) if sampled else 0.5
    combined = 0.60 * (2.0 * probability - 1.0) + 0.40 * (2.0 * label_rate - 1.0)
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, MAX_ADJUSTMENT * combined))


def summary(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    model = load_model(conn, user_id)
    total = event_count(conn, user_id)
    if model is None:
        return {
            "events": total,
            "active": False,
            "needed": max(0, MIN_EVENTS - total),
            "positives": 0,
            "negatives": 0,
            "labels": [],
            "trainedAt": None,
        }
    rates = model.get("label_rates") or {}
    ranked = sorted(rates.items(), key=lambda item: (-item[1], item[0]))
    labels = [
        {"label": name, "rate": round(rate, 3)}
        for name, rate in ranked
        if rate >= 0.62 or rate <= 0.38
    ][:8]
    return {
        "events": int(model.get("events") or total),
        "active": bool(model.get("active")),
        "needed": max(0, MIN_EVENTS - int(model.get("events") or total)),
        "positives": int(model.get("positives") or 0),
        "negatives": int(model.get("negatives") or 0),
        "labels": labels,
        "trainedAt": model.get("trained_at"),
    }


def reset(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM preference_events WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM user_preference_models WHERE user_id=?", (user_id,))


def seed_existing(conn: sqlite3.Connection, user_id: int = 1) -> int:
    """Import explicit edits made before profiles existed into preference events."""
    key = f"preferences_seeded:{user_id}"
    seeded = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if seeded is not None:
        return 0
    existing = event_count(conn, user_id)
    if existing > 0:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", (key,))
        return 0
    rows = db.rows_to_dicts(
        conn.execute(
            """
            SELECT f.*, d.decision, d.selected, d.rating, d.user_set
            FROM decisions d
            JOIN files f ON f.id=d.file_id
            JOIN projects p ON p.id=f.project_id
            WHERE d.user_id=? AND d.user_set=1 AND p.learning_enabled=1
            ORDER BY d.updated_at, d.file_id
            """,
            (user_id,),
        ).fetchall()
    )
    inserted = 0
    with conn:
        for row in rows:
            decision = row.get("decision") or "none"
            selected = int(row.get("selected") or 0)
            rating = row.get("rating")
            if decision == "reject":
                action = "reject"
            elif decision == "alternate":
                action = "alternate"
            elif decision == "keep" or selected:
                action = "select"
            else:
                action = "deselect"
            conn.execute(
                "INSERT INTO preference_events(user_id, project_id, file_id, action, rating, features_json, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    row["project_id"],
                    row["id"],
                    action,
                    rating,
                    json.dumps(features_for_row(row), separators=(",", ":")),
                    db.utc_now(),
                ),
            )
            inserted += 1
        if inserted:
            train(conn, user_id)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", (key,))
    return inserted
