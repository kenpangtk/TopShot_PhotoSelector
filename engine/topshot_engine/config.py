from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Topshot"

JPEG_EXTENSIONS = {".jpg", ".jpeg", ".jpe"}
HEIC_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {
    ".cr2",
    ".cr3",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".raf",
    ".dng",
    ".orf",
    ".rw2",
    ".raw",
    ".pef",
    ".srw",
}
IMAGE_EXTENSIONS = JPEG_EXTENSIONS | HEIC_EXTENSIONS | RAW_EXTENSIONS
VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".3gp",
    ".mts",
    ".m2ts",
}
SIDECAR_EXTENSIONS = {".xmp", ".xml"}  # .xml is only associated when basename matches an image

RAW_JPEG_MODES = ("both", "raw", "jpeg")
PRESETS = ("conservative", "balanced", "aggressive")


def data_dir() -> Path:
    override = os.environ.get("TOPSHOT_DATA_DIR")
    path = Path(override).expanduser() if override else Path.home() / "Library" / "Application Support" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "topshot.sqlite3"


def proxy_dir(project_id: int) -> Path:
    path = data_dir() / "proxies" / str(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
