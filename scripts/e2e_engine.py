#!/usr/bin/env python3
"""End-to-end engine verification against the real test shoot.

Starts the local engine, imports the shoot, verifies proxies and analysis,
then performs a real verified copy (including a name collision and a repeat
run) into test-output/. Never writes into the source folder.

Usage:
    TOPSHOT_E2E_SOURCE=~/Pictures/TestShoot engine/.venv/bin/python scripts/e2e_engine.py [--data-dir PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
SOURCE = Path(os.environ.get("TOPSHOT_E2E_SOURCE", "~/Pictures/Topshot-Test")).expanduser()
OUTPUT = ROOT / "test-output"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Engine:
    def __init__(self, data_dir: str | None = None) -> None:
        env = os.environ.copy()
        if data_dir:
            env["TOPSHOT_DATA_DIR"] = data_dir
        self.proc = subprocess.Popen(
            [str(ENGINE / ".venv/bin/python"), "-u", "-m", "topshot_engine.server", "--port", "0"],
            cwd=ENGINE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"Engine failed to start:\n{stderr}")
        readiness = json.loads(line)
        self.port = readiness["port"]
        self.token = readiness["token"]
        self.base = f"http://127.0.0.1:{self.port}"
        print(f"Engine listening on {self.base}")

    def rpc(self, method: str, params: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base}/rpc",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
            headers={"Content-Type": "application/json", "X-Topshot-Token": self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"RPC transport failure: {exc}") from exc
        if "error" in payload:
            raise RuntimeError(f"{method}: {payload['error']['message']}")
        return payload["result"]

    def wait_job(self, job: dict, label: str) -> dict:
        deadline = time.time() + 60 * 60
        started = time.time()
        last_report = 0.0
        while time.time() < deadline:
            job = self.rpc("job.status", {"jobId": job["id"]})["job"]
            now = time.time()
            if now - last_report > 3 or job["status"] not in ("running", "queued"):
                elapsed = max(1, int(now - started))
                rate = job["done"] / elapsed if job["done"] else 0
                print(
                    f"  [{label}] {job['stage']:<8} {job['done']}/{job['total']} "
                    f"({rate:.1f}/s) {job['message']}"
                )
                last_report = now
            if job["status"] in ("done", "failed", "canceled"):
                return job
            time.sleep(0.5)
        raise TimeoutError(f"{label} timed out")

    def fetch(self, path: str, query: str = "") -> bytes:
        url = f"{self.base}{path}?token={self.token}{query}"
        with urllib.request.urlopen(url, timeout=30) as response:
            assert response.status == 200, response.status
            return response.read()

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--no-import", action="store_true", help="reuse an existing ready project")
    args = parser.parse_args()

    if not SOURCE.is_dir():
        print(f"Test source missing: {SOURCE}", file=sys.stderr)
        return 2
    video_count = sum(
        1
        for path in SOURCE.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mov", ".mp4", ".m4v", ".avi", ".mkv"}
    )
    png_count = sum(1 for path in SOURCE.rglob("*") if path.is_file() and path.suffix.lower() == ".png")
    OUTPUT.mkdir(exist_ok=True)

    engine = Engine(args.data_dir)
    summary: dict = {"source": str(SOURCE), "videosInSource": video_count, "pngInSource": png_count}
    try:
        print("app.info:", json.dumps(engine.rpc("app.info"), indent=2))

        created = engine.rpc("project.create", {"sourceDir": str(SOURCE), "name": SOURCE.name})
        project = created["project"]
        job = created.get("job")
        print(f"Project {project['id']} ({project['status']}) existing={created.get('existing')}")
        if job and not args.no_import:
            job = engine.wait_job(job, "pipeline")
            print("Pipeline status:", job["status"])
            if job["status"] != "done":
                print(json.dumps(job, indent=2))
                return 3
            summary["scan"] = job["result"].get("scan")
            summary["analysis"] = job["result"].get("analysis")
            summary["proxyFailures"] = job["result"].get("proxyFailures")

        data = engine.rpc("photos.list", {"projectId": project["id"]})
        photos = data["photos"]
        print("Counts:", json.dumps(data["counts"], indent=2))
        print("Bursts:", len(data["bursts"]))
        summary["photoCount"] = len(photos)
        summary["counts"] = data["counts"]
        summary["bursts"] = len(data["bursts"])

        assert len(photos) > 0, "no photos imported"
        video_ext = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
        assert not any(Path(photo["name"]).suffix.lower() in video_ext for photo in photos), "video imported"
        if data["counts"].get("keep", 0) == 0:
            raise AssertionError("AI preselection produced no keeps")
        if data["counts"].get("selected", 0) == 0:
            raise AssertionError("AI keeps were not preselected")
        scan = summary.get("scan") or project.get("lastScan") or {}
        if scan:
            assert scan.get("videos") == video_count, f"video skip count {scan.get('videos')} != {video_count}"
            assert scan.get("unsupported") == png_count, f"unsupported count {scan.get('unsupported')} != {png_count}"
            assert scan.get("supported") == len(photos), "supported != visible photos"
            print(
                f"Scan verified: {scan.get('supported')} supported, "
                f"{scan.get('videos')} videos skipped, {scan.get('unsupported')} unsupported skipped"
            )

        jpgs = [photo for photo in photos if photo["kind"] == "jpeg"]
        heics = [photo for photo in photos if photo["kind"] == "heic"]
        assert jpgs and heics, "expected both JPEG and HEIC"
        chosen = [jpgs[0], jpgs[len(jpgs) // 2], heics[0], heics[len(heics) // 2], max(jpgs, key=lambda p: p["size"])]
        chosen_ids = [photo["id"] for photo in chosen]
        print("Copy candidates:", [(photo["name"], photo["size"]) for photo in chosen])

        sampled = engine.fetch(chosen[0]["thumbUrl"])
        preview = engine.fetch(chosen[0]["previewUrl"])
        assert sampled.startswith(b"\xff\xd8"), "thumb is not JPEG"
        assert preview.startswith(b"\xff\xd8"), "preview is not JPEG"
        summary["thumbBytes"] = len(sampled)
        summary["previewBytes"] = len(preview)
        print(f"Proxy verified: thumb {len(sampled)} bytes, preview {len(preview)} bytes")

        before_hashes = {
            photo["id"]: sha256(SOURCE / photo["relPath"]) for photo in chosen
        }
        destination = OUTPUT / f"real-copy-{int(time.time())}"
        destination.mkdir(parents=True, exist_ok=True)
        collision_source = chosen[0]
        collision_path = destination / collision_source["name"]
        collision_bytes = b"Topshot collision sentinel - must never be overwritten\n"
        collision_path.write_bytes(collision_bytes)
        files_before = {path.name for path in destination.iterdir()}

        copy_job = engine.rpc(
            "copy.run",
            {
                "projectId": project["id"],
                "fileIds": chosen_ids,
                "destination": str(destination),
            },
        )["job"]
        copy_job = engine.wait_job(copy_job, "copy")
        print("Copy status:", copy_job["status"])
        if copy_job["status"] not in ("done", "canceled"):
            print(json.dumps(copy_job, indent=2))
            return 4
        result = copy_job["result"]["copy"]
        print("Copy result:", json.dumps(result["counts"], indent=2))
        assert result["counts"]["failed"] == 0, result["failures"]
        assert result["counts"]["copied"] == len(chosen), result["counts"]

        logs = engine.rpc("copy.last", {"projectId": project["id"]})["run"]["logs"]
        copied_logs = [log for log in logs if log["status"] == "copied"]
        assert len(copied_logs) == len(chosen)
        for log in copied_logs:
            src = Path(log["source_path"])
            dst = Path(log["dest_path"])
            assert dst.exists(), f"missing destination {dst}"
            src_hash = sha256(src)
            dst_hash = sha256(dst)
            assert src_hash == dst_hash == log["src_sha256"] == log["dest_sha256"], f"checksum mismatch {dst}"
            assert src.stat().st_size == dst.stat().st_size
        print(f"Checksum verified for {len(copied_logs)} copied files")

        assert collision_path.read_bytes() == collision_bytes, "collision sentinel overwritten"
        renamed = list(destination.glob(f"{Path(collision_source['name']).stem} (1)*"))
        assert renamed, "collision was not auto-renamed"
        assert sha256(renamed[0]) == before_hashes[collision_source["id"]]
        print(f"Collision safe: {collision_source['name']} -> {renamed[0].name}")

        for photo in chosen:
            assert sha256(SOURCE / photo["relPath"]) == before_hashes[photo["id"]], "source changed"

        files_after_first = {path.name for path in destination.iterdir()}
        repeat_job = engine.rpc(
            "copy.run",
            {
                "projectId": project["id"],
                "fileIds": chosen_ids,
                "destination": str(destination),
            },
        )["job"]
        repeat_job = engine.wait_job(repeat_job, "copy-repeat")
        repeat = repeat_job["result"]["copy"]
        print("Repeat copy:", json.dumps(repeat["counts"], indent=2))
        assert repeat["counts"]["copied"] == 0, repeat["counts"]
        assert repeat["counts"]["skipped"] >= len(chosen)
        files_repeat = {path.name for path in destination.iterdir()}
        assert files_repeat == files_after_first, "repeat run created duplicate files"
        print("Repeat run skipped identical files without creating new names")

        summary.update(
            {
                "copy": result,
                "repeat": repeat["counts"],
                "destination": str(destination),
                "collisionRenamedTo": renamed[0].name,
                "filesAfterFirst": sorted(files_after_first),
                "filesAfterRepeat": sorted(files_repeat),
                "sourceHashesBefore": {str(key): value for key, value in before_hashes.items()},
                "sourceHashesAfter": {
                    str(photo["id"]): sha256(SOURCE / photo["relPath"]) for photo in chosen
                },
            }
        )
        (OUTPUT / "e2e-summary.json").write_text(json.dumps(summary, indent=2))
        print("\nE2E PASS. Summary written to test-output/e2e-summary.json")
        return 0
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(main())
