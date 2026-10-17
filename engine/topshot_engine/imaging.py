from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageFile, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional at import time, required in practice
    pillow_heif = None

try:
    import rawpy
except Exception:  # pragma: no cover
    rawpy = None

from . import config

ImageFile.LOAD_TRUNCATED_IMAGES = True

PREVIEW_LONG_EDGE = 1600
THUMB_LONG_EDGE = 320
_TS_RE = re.compile(r"(20\d{6})[_-](\d{6})")


class ImageReadError(RuntimeError):
    pass


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00 ")
    return str(value).strip("\x00 ")


def _parse_exif_datetime(raw: Any, subsec: Any = None) -> float | None:
    text = _as_text(raw) if raw is not None else ""
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if subsec is not None:
            digits = re.sub(r"\D", "", _as_text(subsec))
            if digits:
                try:
                    return dt.timestamp() + float("0." + digits)
                except ValueError:
                    pass
        return dt.timestamp()
    return None


def filename_timestamp(path: Path) -> float | None:
    match = _TS_RE.search(path.stem)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def extract_metadata(img: Image.Image, path: Path) -> dict[str, Any]:
    captured_at = None
    camera = None
    orientation = None
    try:
        exif = img.getexif()
        if exif:
            orientation = exif.get(274)
            make = _as_text(exif.get(271) or "")
            model = _as_text(exif.get(272) or "")
            camera = " ".join(part for part in (make, model) if part) or None
            exif_ifd = exif.get_ifd(0x8769)
            captured_at = _parse_exif_datetime(
                exif_ifd.get(36867) or exif_ifd.get(36868) or exif.get(306),
                exif_ifd.get(37521) or exif_ifd.get(37522),
            )
    except Exception:
        pass
    if captured_at is None:
        captured_at = filename_timestamp(path)
    return {"captured_at": captured_at, "camera": camera, "orientation": orientation}


def _open_with_pillow(path: Path, kind: str) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(path) as opened:
        meta = extract_metadata(opened, path)
        if kind == "jpeg":
            try:
                opened.draft("RGB", (PREVIEW_LONG_EDGE, PREVIEW_LONG_EDGE))
            except Exception:
                pass
        opened.load()
        img = ImageOps.exif_transpose(opened)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        return img.copy(), meta


def _open_raw(path: Path) -> tuple[Image.Image, dict[str, Any]]:
    if rawpy is None:
        raise ImageReadError("RAW support unavailable: rawpy is not installed")
    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(BytesIO(thumb.data))
                img.load()
                if max(img.size) >= 720:
                    img = img.convert("RGB")
                    return img.copy(), {
                        "captured_at": filename_timestamp(path),
                        "camera": None,
                        "orientation": None,
                    }
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = Image.fromarray(thumb.data).convert("RGB")
                if max(img.size) >= 720:
                    return img.copy(), {
                        "captured_at": filename_timestamp(path),
                        "camera": None,
                        "orientation": None,
                    }
        except Exception:
            pass
        rgb = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=False)
    img = Image.fromarray(rgb).convert("RGB")
    return img, {
        "captured_at": filename_timestamp(path),
        "camera": None,
        "orientation": None,
    }


def open_preview_source(path: Path, kind: str) -> tuple[Image.Image, dict[str, Any]]:
    if kind == "raw":
        return _open_raw(path)
    if pillow_heif is None and kind == "heic":
        raise ImageReadError("HEIC support unavailable: pillow-heif is not installed")
    return _open_with_pillow(path, kind)


def _atomic_save_jpeg(img: Image.Image, dest: Path, quality: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        img.save(tmp, format="JPEG", quality=quality, optimize=False, progressive=False)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def generate_proxy(path: str | Path, kind: str, project_id: int, file_id: int) -> dict[str, Any]:
    """Generate 1600px preview + 320px thumb into the app cache (never the source)."""
    src = Path(path)
    out_dir = config.proxy_dir(project_id)
    preview_path = out_dir / f"{file_id}_p.jpg"
    thumb_path = out_dir / f"{file_id}_t.jpg"
    img, meta = open_preview_source(src, kind)
    width, height = img.size
    preview = img.copy()
    preview.thumbnail((PREVIEW_LONG_EDGE, PREVIEW_LONG_EDGE), Image.Resampling.LANCZOS)
    thumb = preview.copy()
    thumb.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE), Image.Resampling.LANCZOS)
    _atomic_save_jpeg(preview, preview_path, 86)
    _atomic_save_jpeg(thumb, thumb_path, 82)
    img.close()
    preview.close()
    thumb.close()
    return {
        **meta,
        "width": width,
        "height": height,
        "preview_path": str(preview_path),
        "thumb_path": str(thumb_path),
    }


_face_state = threading.local()


def _face_cascades() -> list[cv2.CascadeClassifier]:
    cascades = getattr(_face_state, "cascades", None)
    if cascades is None:
        cascades = []
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml"):
            path = Path(cv2.data.haarcascades) / name
            cascade = cv2.CascadeClassifier(str(path))
            if cascade.empty():
                raise ImageReadError(f"Could not load face cascade at {path}")
            cascades.append(cascade)
        _face_state.cascades = cascades
    return cascades


def _detail_cascades() -> tuple[list[cv2.CascadeClassifier], cv2.CascadeClassifier]:
    cascades = getattr(_face_state, "detail", None)
    if cascades is None:
        eye = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_eye.xml"))
        glasses = cv2.CascadeClassifier(
            str(Path(cv2.data.haarcascades) / "haarcascade_eye_tree_eyeglasses.xml")
        )
        smile = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_smile.xml"))
        if eye.empty() or glasses.empty() or smile.empty():
            raise ImageReadError("Could not load eye/smile cascades")
        cascades = ([eye, glasses], smile)
        _face_state.detail = cascades
    return cascades


def _dct_phash(gray: np.ndarray) -> str:
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)
    low = dct[:8, :8]
    median = float(np.median(low.flatten()[1:]))
    value = 0
    for bit in (low > median).flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    return intersection / float(aw * ah + bw * bh - intersection)


def _merge_faces(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda item: item[2] * item[3], reverse=True):
        if any(_iou(box, existing) > 0.35 for existing in merged):
            continue
        merged.append(box)
    return merged


def _colorfulness(rgb: np.ndarray) -> float:
    data = rgb.astype(np.float32)
    red, green, blue = data[:, :, 0], data[:, :, 1], data[:, :, 2]
    rg = red - green
    yb = 0.5 * (red + green) - blue
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def analyze_image(thumb_path: str | Path, preview_path: str | Path | None = None) -> dict[str, Any]:
    with Image.open(thumb_path) as opened:
        gray = opened.convert("L")
        gray.load()
        arr = np.asarray(gray, dtype=np.uint8)
    if arr.size == 0:
        raise ImageReadError("empty thumbnail")
    lap = cv2.Laplacian(arr, cv2.CV_64F, ksize=3)
    sharpness = float(lap.var())
    exposure = float(arr.mean())
    clipped = float(((arr <= 2).sum() + (arr >= 253).sum()) / arr.size)
    small = Image.fromarray(arr).resize((9, 8), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    dct_hash = _dct_phash(arr)

    face_count = 0
    smile_count = 0
    eye_count = 0
    eyes_expected = 0
    people_area = 0.0
    colorfulness = 0.0
    contrast = float(arr.std())
    edge_density = 0.0
    if preview_path and Path(preview_path).is_file():
        with Image.open(preview_path) as opened:
            rgb_image = opened.convert("RGB")
            rgb_image.load()
            long_edge = max(rgb_image.size)
            if long_edge > 900:
                scale = 900 / long_edge
                rgb_image = rgb_image.resize(
                    (max(1, int(rgb_image.width * scale)), max(1, int(rgb_image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            rgb = np.asarray(rgb_image, dtype=np.uint8)
        if rgb.size:
            colorfulness = _colorfulness(rgb)
            gray_preview = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            contrast = float(gray_preview.std())
            edges = cv2.Canny(gray_preview, 80, 160)
            edge_density = float((edges > 0).mean())
            try:
                equalized = cv2.equalizeHist(gray_preview)
                boxes: list[tuple[int, int, int, int]] = []
                for cascade in _face_cascades():
                    detected = cascade.detectMultiScale(
                        equalized, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
                    )
                    boxes.extend(tuple(int(value) for value in box) for box in detected)
                faces = _merge_faces(boxes)
                face_count = int(len(faces))
                eyes_expected = face_count * 2
                if face_count:
                    area = sum(w * h for (_x, _y, w, h) in faces)
                    people_area = float(area / (gray_preview.shape[0] * gray_preview.shape[1]))
                    eye_cascades, smile_cascade = _detail_cascades()
                    for (x, y, w, h) in faces:
                        eye_roi = equalized[y : y + int(h * 0.62), x : x + w]
                        smile_roi = equalized[y + int(h * 0.35) : y + h, x : x + w]
                        if eye_roi.size:
                            eye_boxes: list[tuple[int, int, int, int]] = []
                            for eye_cascade in eye_cascades:
                                detected_eyes = eye_cascade.detectMultiScale(
                                    eye_roi,
                                    scaleFactor=1.1,
                                    minNeighbors=6,
                                    minSize=(max(8, int(w * 0.10)), max(8, int(h * 0.07))),
                                )
                                eye_boxes.extend(
                                    tuple(int(value) for value in box) for box in detected_eyes
                                )
                            eye_count += min(2, len(_merge_faces(eye_boxes)))
                        if smile_roi.size:
                            detected_smiles = smile_cascade.detectMultiScale(
                                smile_roi,
                                scaleFactor=1.1,
                                minNeighbors=24,
                                minSize=(max(12, int(w * 0.25)), max(8, int(h * 0.10))),
                            )
                            if len(detected_smiles):
                                smile_count += 1
            except Exception:
                face_count = 0
                people_area = 0.0
                smile_count = 0
                eye_count = 0
                eyes_expected = 0

    return {
        "sharpness": sharpness,
        "exposure": exposure,
        "clipped": clipped,
        "phash": f"{value:016x}",
        "phash_dct": dct_hash,
        "face_count": face_count,
        "people_area": people_area,
        "colorfulness": colorfulness,
        "contrast": contrast,
        "edge_density": edge_density,
        "smile_count": smile_count,
        "eye_count": eye_count,
        "eyes_expected": eyes_expected,
    }


def analyze_thumb(thumb_path: str | Path) -> dict[str, Any]:
    return analyze_image(thumb_path, None)
