from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_COMPILE_ATTEMPTED = False

_NATIVE_DIR = Path(__file__).resolve().parent.parent / "native"
_SOURCE = _NATIVE_DIR / "vision_probe.swift"
_BINARY = _NATIVE_DIR / "bin" / "vision_probe"


def _binary_path() -> Path | None:
    override = os.environ.get("TOPSHOT_VISION_PROBE")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    return _BINARY if _BINARY.is_file() else None


def _compile() -> bool:
    global _COMPILE_ATTEMPTED
    with _LOCK:
        if _binary_path() is not None:
            return True
        if _COMPILE_ATTEMPTED or not _SOURCE.is_file():
            return False
        _COMPILE_ATTEMPTED = True
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            return False
        _BINARY.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [swiftc, "-O", str(_SOURCE), "-o", str(_BINARY)],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            _BINARY.chmod(0o755)
            return True
        except Exception:
            return False


def available() -> bool:
    return _binary_path() is not None or _compile()


def probe_paths(paths: list[str]) -> dict[str, dict[str, Any]]:
    """Classify and OCR images with the local macOS Vision framework.

    Returns a mapping keyed by the input path. Missing/failed entries are
    omitted so callers can retry them later. No image data leaves the machine.
    """
    if not paths:
        return {}
    if not available():
        return {}
    binary = _binary_path()
    if binary is None:
        return {}
    try:
        completed = subprocess.run(
            [str(binary), "--stdin"],
            input="\n".join(paths) + "\n",
            capture_output=True,
            text=True,
            timeout=max(120.0, len(paths) * 2.0),
        )
    except Exception:
        return {}
    results: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        path = record.get("path")
        if isinstance(path, str):
            results[path] = record
    return results
