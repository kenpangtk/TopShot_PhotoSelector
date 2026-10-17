from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

import numpy as np

from . import db, preferences

BURST_GAP_SECONDS = 2.0
BURST_HASH_DISTANCE = 24
DUPLICATE_HASH_DISTANCE = 8
DUPLICATE_DCT_DISTANCE = 8
DUPLICATE_WINDOW_SECONDS = 7200
DIVERSITY_HASH_DISTANCE = 12
DIVERSITY_DCT_DISTANCE = 10

# Scene labels (from the local macOS Vision framework) that indicate a
# documentation shot rather than a photograph worth delivering. Text-heavy
# frames need several OCR lines as well, so a scenic neon sign does not get
# penalized just for containing words.
DOC_TEXT_LABELS = {
    "document",
    "printed_page",
    "screenshot",
    "handwriting",
    "paper",
    "menu",
    "receipt",
    "price_tag",
    "barcode",
    "label",
    "sign",
    "signboard",
    "billboard",
    "poster",
    "whiteboard",
    "notebook",
    "newspaper",
    "magazine",
    "map",
    "text",
    "letter",
    "placard",
    "schedule",
    "timetable",
    "form",
    "certificate",
    "business_card",
    "wifi",
    "interface",
    "route",
}
DOC_OBJECT_LABELS = {
    "laundry_machine",
    "washing_machine",
    "dryer",
    "appliance",
    "oven",
    "refrigerator",
    "microwave",
    "stove",
    "dishwasher",
    "sink",
    "toilet",
    "bathtub",
    "fire_extinguisher",
    "vending_machine",
    "atm",
    "screen",
    "monitor",
    "television",
    "laptop",
    "keyboard",
    "computer",
    "computer_monitor",
    "printer",
    "cash_register",
    "scale",
    "control_panel",
    "keypad",
    "dashboard",
    "machinery",
    "electronics",
    "consumer_electronics",
    "remote_control",
    "tablet",
    "photocopier",
    "fax",
    "telephone",
    "cell_phone",
    "smartphone",
}
DOC_STRONG_LABELS = {
    "document",
    "printed_page",
    "screenshot",
    "handwriting",
    "sticky_note",
    "calendar",
    "receipt",
    "menu",
    "whiteboard",
    "notebook",
    "map",
}
DOC_SIGN_LABELS = {"sign", "signboard", "billboard", "poster"}
DOC_RETAIL_LABELS = {"interior_shop", "shelf", "retail", "storefront"}
DOC_PRODUCT_LABELS = {"container", "carton", "bottle", "product", "packaging", "produce"}
DOC_PERSON_LABELS = {"people", "adult", "crowd", "portrait", "selfie", "child", "family"}
DOCUMENTATION_SELECT_LIMIT = 0.45
DOCUMENTATION_RANK_PENALTY = 0.65

# Sharpness is variance-of-Laplacian on the 320px review thumb. Thresholds are
# deliberately strict for the conservative preset: only clear focus misses are
# suggested as rejects; everything borderline is surfaced as review.
THRESHOLDS = {
    "conservative": {"reject_abs": 800.0, "reject_pct": 0.003, "soft_abs": 1900.0, "soft_pct": 0.01},
    "balanced": {"reject_abs": 2000.0, "reject_pct": 0.008, "soft_abs": 4200.0, "soft_pct": 0.05},
    "aggressive": {"reject_abs": 4000.0, "reject_pct": 0.02, "soft_abs": 7500.0, "soft_pct": 0.10},
}


def _hash_distance(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 999
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 999


def _camera_key(camera: str | None) -> str:
    return (camera or "").strip().lower()


def _same_camera(a: str | None, b: str | None) -> bool:
    ka, kb = _camera_key(a), _camera_key(b)
    return not ka or not kb or ka == kb


def _percentile_ranks(values: list[float]) -> list[float]:
    if not values:
        return []
    arr = np.log1p(np.asarray(values, dtype=np.float64))
    order = arr.argsort(kind="mergesort")
    ranks = np.empty(len(arr), dtype=np.float64)
    ranks[order] = np.arange(len(arr), dtype=np.float64)
    if len(arr) <= 1:
        return [0.5]
    return list(ranks / (len(arr) - 1))


def _attach_percentiles(rows: list[dict[str, Any]], key: str, target: str) -> None:
    values = [float(row.get(key) or 0.0) for row in rows]
    for row, pct in zip(rows, _percentile_ranks(values)):
        row[target] = float(pct)


def _people_score(count: int, area: float) -> float:
    if count <= 0:
        return 0.0
    return min(1.0, 0.40 + 0.15 * min(count, 4) + 3.0 * area)


def _parse_vision_labels(row: dict[str, Any]) -> list[tuple[str, float]]:
    raw = row.get("vision_labels")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    labels: list[tuple[str, float]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    labels.append((str(item[0]).lower(), float(item[1])))
                except (TypeError, ValueError):
                    continue
    return labels


def _documentation(row: dict[str, Any]) -> tuple[float, str | None]:
    """Score how likely a frame is a note-taking/documentation shot.

    Combines local scene labels, OCR text density, and a penalty for frames
    whose subject is clearly people. Returns (score, short detail).
    """
    labels = _parse_vision_labels(row)
    top5 = labels[:5]
    top3 = labels[:3]
    text_label = max((conf for label, conf in top5 if label in DOC_TEXT_LABELS), default=0.0)
    object_label = max((conf for label, conf in top3 if label in DOC_OBJECT_LABELS), default=0.0)
    lines = int(row.get("vision_text_lines") or 0)
    chars = int(row.get("vision_text_chars") or 0)
    area = float(row.get("vision_text_area") or 0.0)
    lines_score = min(1.0, max(0.0, (lines - 1) / 9.0))
    area_score = min(1.0, max(0.0, area / 0.04))
    text_dominance = lines_score * (0.35 + 0.65 * area_score)
    if chars < 20:
        text_dominance *= 0.5
    text_score = max(
        text_label * (0.40 + 0.60 * text_dominance),
        text_dominance * 0.78,
    )
    score = max(text_score, object_label)

    # Low-confidence document labels are still meaningful when they are the
    # strongest scene label (e.g. a notice taped to a wall).
    top2 = labels[:2]
    strong_label = max((conf for label, conf in top2 if label in DOC_STRONG_LABELS), default=0.0)
    if strong_label >= 0.10:
        score = max(score, 0.55)
    sign_label = max((conf for label, conf in top3 if label in DOC_SIGN_LABELS), default=0.0)
    if sign_label >= 0.50:
        score = max(score, min(0.75, sign_label * 0.80))
    retail_label = max((conf for label, conf in top3 if label in DOC_RETAIL_LABELS), default=0.0)
    product_label = max((conf for label, conf in top5 if label in DOC_PRODUCT_LABELS), default=0.0)
    if retail_label >= 0.40 and product_label >= 0.25:
        score = max(score, 0.50)

    person_label = max((conf for label, conf in top5 if label in DOC_PERSON_LABELS), default=0.0)
    people_area = float(row.get("people_area") or 0.0)
    faces = int(row.get("face_count") or 0)
    if faces > 0 and people_area >= 0.02:
        score *= 0.30
    elif person_label >= 0.70:
        score *= 0.45
    elif person_label >= 0.45:
        score *= 0.70
    score = max(0.0, min(1.0, score))
    if score < 0.18:
        return 0.0, None

    candidates = [
        (conf, label)
        for label, conf in top5
        if label in DOC_TEXT_LABELS or label in DOC_OBJECT_LABELS
    ]
    detail: str | None = None
    if candidates:
        conf, label = max(candidates)
        if conf >= 0.35:
            detail = label.replace("_", " ")
    if detail is None and retail_label >= 0.40 and product_label >= 0.25:
        detail = "retail display"
    if detail is None and text_dominance >= 0.35:
        detail = "text-heavy frame"
    return score, detail


def _detail(row: dict[str, Any]) -> str:
    parts: list[str] = []
    count = int(row.get("face_count") or 0)
    if count == 1:
        parts.append("1 person")
    elif count > 1:
        parts.append(f"{count} people")
    if count:
        eyes_open = float(row.get("eyes_open_score") or 0.0)
        smile = float(row.get("smile_score") or 0.0)
        if eyes_open >= 0.75:
            parts.append("eyes open")
        if smile >= 0.5:
            parts.append("smiling")
        elif smile > 0:
            parts.append("some smiles")
    interesting = float(row.get("interesting_score") or 0.0)
    if interesting >= 0.72:
        parts.append("vivid, high-contrast scene")
    elif interesting >= 0.55:
        parts.append("colorful scene")
    return " · ".join(parts)


def _near_duplicate(a: dict[str, Any], b: dict[str, Any], hash_limit: int, dct_limit: int) -> bool:
    return (
        _hash_distance(a.get("phash"), b.get("phash")) <= hash_limit
        or _hash_distance(a.get("phash_dct"), b.get("phash_dct")) <= dct_limit
    )


def _build_bursts(rows: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    timed = sorted(
        (row for row in rows if row.get("captured_at") is not None),
        key=lambda row: (row["captured_at"], row["id"]),
    )
    bursts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in timed:
        if not current:
            current = [row]
            continue
        prev = current[-1]
        gap = float(row["captured_at"]) - float(prev["captured_at"])
        # Rapid frames from the same body are a burst even when expressions
        # differ; after ~1.2s also require perceptual similarity so unrelated
        # quick shots are not welded together.
        similar = _hash_distance(prev.get("phash"), row.get("phash")) <= BURST_HASH_DISTANCE
        if (
            gap <= BURST_GAP_SECONDS
            and _same_camera(prev.get("camera"), row.get("camera"))
            and (gap <= 1.2 or similar)
        ):
            current.append(row)
        else:
            if len(current) >= 2:
                bursts.append(current)
            current = [row]
    if len(current) >= 2:
        bursts.append(current)

    by_id: dict[int, list[dict[str, Any]]] = {}
    for index, members in enumerate(bursts, start=1):
        # Best pick is the same composite used for ranking: sharpness, visual
        # interest, and whether people are present.
        members.sort(key=lambda row: (-(row.get("rank_score") or 0.0), row["id"]))
        by_id[index] = members
    return bursts, by_id


class _UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self.parent = {file_id: file_id for file_id in ids}

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _apply_keep_target(
    analyzed: list[dict[str, Any]],
    target: int,
    burst_best: dict[int, dict[str, Any]] | None = None,
    duplicate_best: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    """Select the best N frames, one per burst/duplicate group, with diversity."""
    ranked = sorted(
        analyzed,
        key=lambda row: (
            -(row.get("rank_score") or 0.0),
            -(row.get("people_quality") or 0.0),
            row["id"],
        ),
    )
    rank_index = {row["id"]: index + 1 for index, row in enumerate(ranked)}
    if burst_best is None:
        burst_best = {}
        for row in analyzed:
            burst = row.get("burst_id")
            if not burst:
                continue
            current = burst_best.get(burst)
            if current is None or (row.get("rank_score") or 0.0) > (current.get("rank_score") or 0.0):
                burst_best[burst] = row
    if duplicate_best is None:
        duplicate_best = {}
        for row in analyzed:
            group = row.get("duplicate_group")
            if not group:
                continue
            current = duplicate_best.get(group)
            if current is None or (row.get("rank_score") or 0.0) > (current.get("rank_score") or 0.0):
                duplicate_best[group] = row

    primary: list[dict[str, Any]] = []
    for row in ranked:
        if row.get("suggestion") == "reject":
            continue
        if row.get("duplicate_group"):
            if duplicate_best.get(row["duplicate_group"]) is not row:
                continue
        elif row.get("burst_id") and burst_best.get(row["burst_id"]) is not row:
            continue
        primary.append(row)

    # Manual selections survive a re-cull and count toward the target, so the
    # AI fills the remaining slots instead of overshooting.
    chosen: list[dict[str, Any]] = [
        row
        for row in ranked
        if row.get("selected") and row.get("user_set") and row.get("status") != "missing"
    ]
    chosen_ids: set[int] = {row["id"] for row in chosen}

    def try_add(row: dict[str, Any]) -> bool:
        if row["id"] in chosen_ids or row.get("suggestion") == "reject":
            return False
        if row.get("user_set"):
            return False
        if float(row.get("documentation_score") or 0.0) >= DOCUMENTATION_SELECT_LIMIT:
            return False
        if any(
            _near_duplicate(row, other, DIVERSITY_HASH_DISTANCE, DIVERSITY_DCT_DISTANCE)
            for other in chosen
        ):
            return False
        chosen.append(row)
        chosen_ids.add(row["id"])
        return True

    for row in primary:
        if try_add(row) and len(chosen) >= target:
            break
    if len(chosen) < target:
        for row in ranked:
            if try_add(row) and len(chosen) >= target:
                break

    for row in analyzed:
        detail = _detail(row)
        if row["id"] in chosen_ids:
            row["suggestion"] = "keep"
            row["confidence"] = max(float(row.get("confidence") or 0.55), 0.72)
            if row.get("burst_id") and row.get("burst_rank") == 1 and not row.get("duplicate_group"):
                reason = f"Best of {row.get('burst_size')}-frame burst"
            elif row.get("duplicate_group") and row.get("duplicate_size", 1) > 1:
                reason = f"Best of {row.get('duplicate_size')} similar frames"
            elif detail:
                reason = f"Top {target} keep target · {detail}"
            else:
                reason = f"Top {target} keep target · rank {rank_index[row['id']]}"
            if detail and not detail in reason:
                reason += f" · {detail}"
            row["reason"] = reason
        elif row.get("suggestion") == "reject":
            continue
        elif float(row.get("documentation_score") or 0.0) >= DOCUMENTATION_SELECT_LIMIT:
            row["suggestion"] = "review"
            row["confidence"] = 0.40
            detail_text = f" · {row['documentation_detail']}" if row.get("documentation_detail") else ""
            row["reason"] = f"Documentation/text photo — not culled as a keep{detail_text}"
        elif row.get("duplicate_group"):
            row["suggestion"] = "review"
            row["confidence"] = 0.5
            if duplicate_best.get(row["duplicate_group"]) is row:
                row["reason"] = (
                    f"Best of {row.get('duplicate_size')} similar frames — outside keep target"
                )
            else:
                row["reason"] = "Outside keep target — similar to a selected frame"
        elif row.get("burst_id"):
            row["suggestion"] = "alternate"
            row["confidence"] = 0.65
            row["reason"] = (
                f"Outside keep target — alternate {row.get('burst_rank')} of "
                f"{row.get('burst_size')}-frame burst"
            )
        else:
            row["suggestion"] = "reject"
            row["confidence"] = 0.6
            row["reason"] = f"Outside keep target — rank {rank_index[row['id']]} of {len(analyzed)}"
    return chosen_ids

def recompute(
    conn: sqlite3.Connection,
    project_id: int,
    preset: str | None = None,
    user_id: int = 1,
    keep_target: int | None = None,
) -> dict[str, Any]:
    settings = db.user_settings(conn, user_id, project_id)
    preset = preset or settings.get("preset") or "conservative"
    preset = preset if preset in THRESHOLDS else "conservative"
    thresholds = THRESHOLDS[preset]
    now = db.utc_now()
    keep_target = (
        int(keep_target)
        if keep_target is not None
        else (int(settings["keep_target"]) if settings.get("keep_target") else None)
    )
    rows = db.rows_to_dicts(
        conn.execute(
            """
            SELECT f.id, f.rel_path, f.stem, f.captured_at, f.camera, f.sharpness, f.phash, f.phash_dct,
                   f.exposure, f.face_count, f.people_area, f.colorfulness, f.contrast, f.edge_density,
                   f.smile_count, f.eye_count, f.eyes_expected, f.status, f.analysis_error,
                   f.vision_labels, f.vision_text_lines, f.vision_text_chars,
                   f.vision_text_area, f.vision_text_confidence,
                   d.decision, d.selected, d.user_set
            FROM files f
            LEFT JOIN decisions d ON d.user_id=? AND d.file_id=f.id
            WHERE f.project_id=? AND f.hidden_from_review=0
              AND f.status IN ('ready','failed','missing')
            ORDER BY f.captured_at IS NULL, f.captured_at, f.id
            """,
            (user_id, project_id),
        )
    )

    analyzed = [row for row in rows if row.get("sharpness") is not None]
    _attach_percentiles(analyzed, "sharpness", "sharpness_pct")
    _attach_percentiles(analyzed, "colorfulness", "color_pct")
    _attach_percentiles(analyzed, "contrast", "contrast_pct")
    _attach_percentiles(analyzed, "edge_density", "edge_pct")

    for row in analyzed:
        faces = int(row.get("face_count") or 0)
        eyes_expected = int(row.get("eyes_expected") or (faces * 2))
        eyes = int(row.get("eye_count") or 0)
        smiles = int(row.get("smile_count") or 0)
        eyes_open_score = min(1.0, eyes / eyes_expected) if eyes_expected > 0 else 0.0
        smile_score = min(1.0, smiles / faces) if faces > 0 else 0.0
        people_quality = (
            0.35 + 0.35 * eyes_open_score + 0.30 * smile_score if faces > 0 else 0.0
        )
        people = _people_score(faces, float(row.get("people_area") or 0.0))
        interesting = (
            0.30 * float(row.get("color_pct") or 0.0)
            + 0.25 * float(row.get("contrast_pct") or 0.0)
            + 0.20 * float(row.get("edge_pct") or 0.0)
            + 0.15 * float(row.get("sharpness_pct") or 0.0)
            + 0.10 * people
        )
        exposure = float(row.get("exposure") or 128.0)
        exposure_quality = 1.0 if 35.0 <= exposure <= 225.0 else 0.5
        if faces > 0:
            # People photos are judged on presence plus open eyes and smiles.
            rank = (
                0.38 * float(row.get("sharpness_pct") or 0.0)
                + 0.18 * interesting
                + 0.16 * people
                + 0.23 * people_quality
                + 0.05 * exposure_quality
            )
        else:
            # Landscapes and detail shots are judged on how good they look.
            rank = (
                0.55 * float(row.get("sharpness_pct") or 0.0)
                + 0.40 * interesting
                + 0.05 * exposure_quality
            )
        documentation_score, documentation_detail = _documentation(row)
        row["documentation_score"] = documentation_score
        row["documentation_detail"] = documentation_detail
        if documentation_score > 0:
            rank *= 1.0 - DOCUMENTATION_RANK_PENALTY * documentation_score
        row["smile_score"] = float(smile_score)
        row["eyes_open_score"] = float(eyes_open_score)
        row["people_quality"] = float(people_quality)
        row["people_score"] = people
        row["interesting_score"] = float(interesting)
        row["rank_score"] = float(rank)
        row["base_rank"] = float(rank)

    bursts, burst_map = _build_bursts(analyzed)
    for burst_id, members in burst_map.items():
        for rank, member in enumerate(members, start=1):
            member["burst_id"] = burst_id
            member["burst_size"] = len(members)
            member["burst_rank"] = rank

    # Duplicate grouping: same/different burst near-identical frames within an hour.
    with_time = sorted(
        (row for row in analyzed if row.get("captured_at") is not None and row.get("phash")),
        key=lambda row: (row["captured_at"], row["id"]),
    )
    uf = _UnionFind([row["id"] for row in with_time])
    for index, row in enumerate(with_time):
        for other in with_time[index + 1 :]:
            if float(other["captured_at"]) - float(row["captured_at"]) > DUPLICATE_WINDOW_SECONDS:
                break
            if _near_duplicate(
                row, other, DUPLICATE_HASH_DISTANCE, DUPLICATE_DCT_DISTANCE
            ):
                uf.union(row["id"], other["id"])

    clusters: dict[int, list[dict[str, Any]]] = {}
    for row in with_time:
        clusters.setdefault(uf.find(row["id"]), []).append(row)
    duplicate_groups: list[list[dict[str, Any]]] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        burst_ids = {member.get("burst_id") for member in members}
        if len(burst_ids) == 1 and next(iter(burst_ids)) is not None:
            continue
        members.sort(key=lambda row: (-(row.get("rank_score") or 0.0), row["id"]))
        duplicate_groups.append(members)
    for group_index, members in enumerate(duplicate_groups, start=1):
        for member in members:
            member["duplicate_group"] = group_index
            member["duplicate_size"] = len(members)

    # Baseline suggestions are shared; personalized suggestions/selection are
    # written to the active profile's decision rows below.
    model = preferences.load_model(conn, user_id)
    for row in analyzed:
        adjustment = preferences.score(model, row)
        row["preference_score"] = adjustment
        row["rank_score"] = float(row.get("base_rank") or 0.0) + adjustment

    def suggestion_for(row: dict[str, Any]) -> tuple[str, float, str]:
        pct = float(row.get("sharpness_pct") or 0.0)
        sharpness = float(row.get("sharpness") or 0.0)
        exposure = row.get("exposure")
        detail = _detail(row)
        if row.get("status") == "failed":
            return "review", 0.15, "Could not analyze this file — review manually"
        documentation_score, documentation_detail = _documentation(row)
        if documentation_score >= DOCUMENTATION_SELECT_LIMIT:
            detail_text = f" · {documentation_detail}" if documentation_detail else ""
            return "review", 0.40, f"Documentation/text photo — not culled as a keep{detail_text}"
        if row.get("burst_id"):
            members = burst_map[row["burst_id"]]
            top = members[0]
            weak = (top.get("sharpness_pct") or 0.0) < 0.12 and (top.get("sharpness") or 0.0) < 30
            if weak:
                return "review", 0.4, f"Weak burst ({len(members)} frames) — review all frames"
            if row.get("burst_rank") == 1:
                reason = f"Best of {len(members)}-frame burst"
                if detail:
                    reason += f" · {detail}"
                return "keep", 0.82, reason
            return "alternate", 0.7, f"Alternate {row['burst_rank']} of {len(members)}-frame burst"
        if sharpness < thresholds["reject_abs"] or (
            pct < thresholds["reject_pct"] and sharpness < thresholds["reject_abs"] * 3
        ):
            return "reject", 0.9 if sharpness < thresholds["reject_abs"] else 0.8, "Soft focus — very low detail"
        if sharpness < thresholds["soft_abs"] or (
            pct < thresholds["soft_pct"] and sharpness < thresholds["soft_abs"] * 2.5
        ):
            return "review", 0.5, "Possibly soft — review at 100%"
        if exposure is not None and float(exposure) < 14:
            return "review", 0.7, "Severely underexposed"
        if exposure is not None and float(exposure) > 248:
            return "review", 0.6, "Nearly blown out"
        reason = f"Sharp{(' · ' + detail) if detail else '; no duplicate or softness issue detected'}"
        return "keep", 0.55, reason

    for row in analyzed:
        suggestion, confidence, reason = suggestion_for(row)
        row["suggestion"] = suggestion
        row["confidence"] = confidence
        row["reason"] = reason
        row["base_suggestion"] = suggestion
        row["base_confidence"] = confidence
        row["base_reason"] = reason

    for members in duplicate_groups:
        best = members[0]
        for member in members[1:]:
            # Burst members keep their keep/alternate role; duplicates are a
            # cross-burst advisory and never override the burst pick.
            if member.get("burst_id") or member.get("suggestion") in ("alternate", "reject", "review"):
                continue
            member["suggestion"] = "review"
            member["confidence"] = 0.5
            member["reason"] = f"Possible duplicate of {best['rel_path']}"
            member["base_suggestion"] = "review"
            member["base_confidence"] = 0.5
            member["base_reason"] = member["reason"]

    chosen_ids: set[int] | None = None
    if keep_target:
        personal_burst_best: dict[int, dict[str, Any]] = {}
        for row in analyzed:
            burst = row.get("burst_id")
            if not burst:
                continue
            current = personal_burst_best.get(burst)
            if current is None or (row.get("rank_score") or 0.0) > (current.get("rank_score") or 0.0):
                personal_burst_best[burst] = row
        personal_duplicate_best: dict[int, dict[str, Any]] = {}
        for row in analyzed:
            group = row.get("duplicate_group")
            if not group:
                continue
            current = personal_duplicate_best.get(group)
            if current is None or (row.get("rank_score") or 0.0) > (current.get("rank_score") or 0.0):
                personal_duplicate_best[group] = row
        chosen_ids = _apply_keep_target(
            analyzed, keep_target, personal_burst_best, personal_duplicate_best
        )

    # Files without analysis still appear in review; they are never preselected.
    unanalyzed = [row for row in rows if row.get("sharpness") is None]
    for row in unanalyzed:
        if row.get("status") == "missing":
            row["suggestion"] = "review"
            row["confidence"] = 0.0
            row["reason"] = "Source file is missing"
        else:
            row["suggestion"] = "review"
            row["confidence"] = 0.1
            row["reason"] = row.get("analysis_error") or "File could not be analyzed"
        row["base_suggestion"] = row["suggestion"]
        row["base_confidence"] = row["confidence"]
        row["base_reason"] = row["reason"]

    with conn:
        conn.execute("DELETE FROM bursts WHERE project_id=?", (project_id,))
        for burst_id, members in burst_map.items():
            top = members[0]
            weak = (top.get("sharpness_pct") or 0.0) < 0.12 and (top.get("sharpness") or 0.0) < 30
            conn.execute(
                "INSERT INTO bursts(project_id, burst_id, cover_file_id, size, weak) VALUES(?,?,?,?,?)",
                (project_id, burst_id, top["id"], len(members), 1 if weak else 0),
            )

        for row in rows:
            conn.execute(
                """
                UPDATE files SET sharpness_pct=?, smile_score=?, eyes_open_score=?, people_quality=?,
                    people_score=?, interesting_score=?, rank_score=?, documentation_score=?, documentation_detail=?,
                    suggestion=?, confidence=?, reason=?, burst_id=?, burst_size=?, burst_rank=?,
                    duplicate_group=?, duplicate_size=?, suggested_at=?
                WHERE id=?
                """,
                (
                    row.get("sharpness_pct"),
                    row.get("smile_score"),
                    row.get("eyes_open_score"),
                    row.get("people_quality"),
                    row.get("people_score"),
                    row.get("interesting_score"),
                    row.get("base_rank", row.get("rank_score")),
                    row.get("documentation_score"),
                    row.get("documentation_detail"),
                    row.get("base_suggestion", row.get("suggestion")),
                    row.get("base_confidence", row.get("confidence")),
                    row.get("base_reason", row.get("reason")),
                    row.get("burst_id"),
                    row.get("burst_size"),
                    row.get("burst_rank"),
                    row.get("duplicate_group"),
                    row.get("duplicate_size"),
                    now,
                    row["id"],
                ),
            )
            if chosen_ids is not None:
                desired_selected = 1 if row["id"] in chosen_ids else 0
            else:
                desired_selected = 1 if row.get("suggestion") == "keep" else 0
            existing = conn.execute(
                "SELECT selected, decision, user_set FROM decisions WHERE user_id=? AND file_id=?",
                (user_id, row["id"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO decisions(user_id, file_id, decision, selected, rating, user_set, "
                    "suggestion, confidence, reason, preference_score, updated_at) "
                    "VALUES(?, ?, 'none', ?, NULL, 0, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        row["id"],
                        desired_selected,
                        row.get("suggestion"),
                        row.get("confidence"),
                        row.get("reason"),
                        row.get("preference_score"),
                        now,
                    ),
                )
            else:
                if not int(existing["user_set"]):
                    conn.execute(
                        "UPDATE decisions SET selected=?, suggestion=?, confidence=?, reason=?, "
                        "preference_score=?, updated_at=? WHERE user_id=? AND file_id=?",
                        (
                            desired_selected,
                            row.get("suggestion"),
                            row.get("confidence"),
                            row.get("reason"),
                            row.get("preference_score"),
                            now,
                            user_id,
                            row["id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE decisions SET suggestion=?, confidence=?, reason=?, preference_score=?, "
                        "updated_at=? WHERE user_id=? AND file_id=?",
                        (
                            row.get("suggestion"),
                            row.get("confidence"),
                            row.get("reason"),
                            row.get("preference_score"),
                            now,
                            user_id,
                            row["id"],
                        ),
                    )
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        db.update_user_settings(conn, user_id, project_id, {"initialized": 1})

    counts = Counter(row.get("suggestion") for row in rows)
    return {
        "analyzed": len(analyzed),
        "bursts": len(burst_map),
        "duplicates": len(duplicate_groups),
        "people": sum(1 for row in analyzed if int(row.get("face_count") or 0) > 0),
        "documentation": sum(
            1
            for row in analyzed
            if float(row.get("documentation_score") or 0.0) >= DOCUMENTATION_SELECT_LIMIT
        ),
        "keepTarget": keep_target,
        "keeps": counts.get("keep", 0),
        "review": counts.get("review", 0),
        "rejects": counts.get("reject", 0),
        "alternates": counts.get("alternate", 0),
        "failed": sum(1 for row in rows if row.get("status") == "failed"),
        "missing": sum(1 for row in rows if row.get("status") == "missing"),
    }
