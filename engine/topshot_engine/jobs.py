from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import analysis, config, copy as copy_engine, db, imaging, ingest, vision

STAGES = ("ingest", "proxy", "analyze", "vision", "cluster")


class Job:
    def __init__(self, project_id: int, kind: str, user_id: int = 1) -> None:
        self.id = uuid.uuid4().hex
        self.project_id = project_id
        self.user_id = user_id
        self.kind = kind
        self.status = "queued"
        self.stage = "queued"
        self.done = 0
        self.total = 0
        self.bytes_done = 0
        self.bytes_total = 0
        self.message = "Queued"
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.cancel_event = threading.Event()
        self.started_at = db.utc_now()
        self.finished_at: str | None = None
        self._started_monotonic = time.monotonic()
        self._lock = threading.Lock()

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = max(0.001, time.monotonic() - self._started_monotonic)
            eta = None
            if self.status == "running" and self.done > 0 and self.total > 0:
                eta = max(0.0, elapsed / self.done * (self.total - self.done))
            return {
                "id": self.id,
                "projectId": self.project_id,
                "userId": self.user_id,
                "kind": self.kind,
                "status": self.status,
                "stage": self.stage,
                "done": self.done,
                "total": self.total,
                "bytesDone": self.bytes_done,
                "bytesTotal": self.bytes_total,
                "message": self.message,
                "error": self.error,
                "result": self.result,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "elapsedSeconds": round(elapsed, 1),
                "etaSeconds": round(eta, 1) if eta is not None else None,
                "throughputBytesPerSecond": round(self.bytes_done / elapsed, 1) if self.bytes_done else None,
                "cancelRequested": self.cancel_event.is_set(),
            }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def active_for_project(self, project_id: int) -> Job | None:
        with self._lock:
            for job in self.jobs.values():
                if job.project_id == project_id and job.status in ("queued", "running"):
                    return job
        return None

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job {job_id}")
        job.cancel_event.set()
        job.update(message="Canceling…")
        return job.snapshot()

    # ------------------------------------------------------------------ pipeline
    def start_pipeline(self, project_id: int, user_id: int = 1) -> dict[str, Any]:
        active = self.active_for_project(project_id)
        if active is not None:
            return active.snapshot()
        job = Job(project_id, "pipeline", user_id)
        with self._lock:
            self.jobs[job.id] = job
        job.update(status="running", stage="ingest", message="Scanning source folder")
        thread = threading.Thread(target=self._run_pipeline, args=(job,), daemon=True, name=f"pipeline-{project_id}")
        thread.start()
        return job.snapshot()

    def _run_pipeline(self, job: Job) -> None:
        conn = db.connect()
        project_id = job.project_id
        try:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise RuntimeError("Project not found")
            source_dir = Path(project["source_dir"])
            if not source_dir.is_dir():
                raise RuntimeError(f"Source folder is missing: {source_dir}")
            with conn:
                conn.execute("UPDATE projects SET status='importing', updated_at=? WHERE id=?", (db.utc_now(), project_id))

            def scan_progress(found: int) -> None:
                job.update(message=f"Scanning… {found} files found", done=found, total=0)

            scan = ingest.scan_source(source_dir, scan_progress)
            if job.cancel_event.is_set():
                raise _Cancelled()
            job.update(stage="ingest", message="Recording files", done=len(scan.entries), total=len(scan.entries))
            stats = ingest.upsert_scan(conn, project_id, scan, source_dir)
            with conn:
                conn.execute(
                    "UPDATE projects SET last_scan_json=?, updated_at=? WHERE id=?",
                    (json.dumps(stats), db.utc_now(), project_id),
                )
            job.update(result={"scan": stats})
            if job.cancel_event.is_set():
                raise _Cancelled()

            file_rows = db.rows_to_dicts(
                conn.execute(
                    "SELECT id, path, kind FROM files WHERE project_id=? AND hidden_from_review=0 "
                    "AND status IN ('pending','ready') AND proxy_ready=0 ORDER BY id",
                    (project_id,),
                ).fetchall()
            )
            total_proxy = len(file_rows)
            job.update(
                stage="proxy",
                done=0,
                total=total_proxy,
                message=f"Generating proxies (0/{total_proxy})",
            )
            proxy_failures = 0
            workers = max(1, min(6, (os.cpu_count() or 4) - 1))
            if file_rows:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(imaging.generate_proxy, row["path"], row["kind"], project_id, row["id"]): row
                        for row in file_rows
                    }
                    completed = 0
                    for future in as_completed(futures):
                        row = futures[future]
                        completed += 1
                        if job.cancel_event.is_set():
                            for pending in futures:
                                pending.cancel()
                            break
                        now = db.utc_now()
                        try:
                            meta = future.result()
                            with conn:
                                conn.execute(
                                    """
                                    UPDATE files SET proxy_ready=1, width=?, height=?, captured_at=?, camera=?,
                                      orientation=?, status='ready', error=NULL, sharpness=NULL, sharpness_pct=NULL,
                                      exposure=NULL, phash=NULL, phash_dct=NULL, face_count=NULL, people_area=NULL,
                                      colorfulness=NULL, contrast=NULL, edge_density=NULL, smile_count=NULL,
                                      eye_count=NULL, eyes_expected=NULL, smile_score=NULL, eyes_open_score=NULL,
                                      people_quality=NULL, people_score=NULL, interesting_score=NULL,
                                      rank_score=NULL, documentation_score=NULL, documentation_detail=NULL,
                                      vision_labels=NULL, vision_text_lines=NULL, vision_text_chars=NULL,
                                      vision_text_area=NULL, vision_text_confidence=NULL,
                                      analyzed_at=NULL, analysis_error=NULL, updated_at=?
                                    WHERE id=?
                                    """,
                                    (
                                        meta.get("width"),
                                        meta.get("height"),
                                        meta.get("captured_at"),
                                        meta.get("camera"),
                                        meta.get("orientation"),
                                        now,
                                        row["id"],
                                    ),
                                )
                        except Exception as exc:
                            proxy_failures += 1
                            with conn:
                                conn.execute(
                                    "UPDATE files SET status='failed', error=?, analysis_error=?, updated_at=? WHERE id=?",
                                    (str(exc), str(exc), now, row["id"]),
                                )
                        job.update(
                            done=completed,
                            total=total_proxy,
                            message=f"Generating proxies ({completed}/{total_proxy})",
                        )
            if job.cancel_event.is_set():
                raise _Cancelled()

            analysis_rows = db.rows_to_dicts(
                conn.execute(
                    "SELECT id FROM files WHERE project_id=? AND hidden_from_review=0 "
                    "AND status='ready' AND (sharpness IS NULL OR phash_dct IS NULL OR smile_count IS NULL) ORDER BY id",
                    (project_id,),
                ).fetchall()
            )
            total_analysis = len(analysis_rows)
            job.update(stage="analyze", done=0, total=total_analysis, message="Scoring sharpness and duplicates")
            proxy_root = config.proxy_dir(project_id)
            if analysis_rows:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(
                            imaging.analyze_image,
                            proxy_root / f"{row['id']}_t.jpg",
                            proxy_root / f"{row['id']}_p.jpg",
                        ): row
                        for row in analysis_rows
                    }
                    completed = 0
                    for future in as_completed(futures):
                        row = futures[future]
                        completed += 1
                        if job.cancel_event.is_set():
                            for pending in futures:
                                pending.cancel()
                            break
                        now = db.utc_now()
                        try:
                            data = future.result()
                            with conn:
                                conn.execute(
                                    """
                                    UPDATE files SET sharpness=?, exposure=?, phash=?, phash_dct=?, face_count=?,
                                      people_area=?, colorfulness=?, contrast=?, edge_density=?, smile_count=?,
                                      eye_count=?, eyes_expected=?, analyzed_at=?,
                                      status='ready', analysis_error=NULL, updated_at=? WHERE id=?
                                    """,
                                    (
                                        data["sharpness"],
                                        data["exposure"],
                                        data["phash"],
                                        data["phash_dct"],
                                        data["face_count"],
                                        data["people_area"],
                                        data["colorfulness"],
                                        data["contrast"],
                                        data["edge_density"],
                                        data["smile_count"],
                                        data["eye_count"],
                                        data["eyes_expected"],
                                        now,
                                        now,
                                        row["id"],
                                    ),
                                )
                        except Exception as exc:
                            with conn:
                                conn.execute(
                                    "UPDATE files SET analysis_error=?, updated_at=? WHERE id=?",
                                    (str(exc), now, row["id"]),
                                )
                        job.update(
                            done=completed,
                            total=total_analysis,
                            message=f"Scoring sharpness ({completed}/{total_analysis})",
                        )
            if job.cancel_event.is_set():
                raise _Cancelled()

            vision_summary = self._run_vision_stage(job, conn, project_id, proxy_root)
            if job.cancel_event.is_set():
                raise _Cancelled()

            job.update(stage="cluster", done=0, total=1, message="Grouping bursts and duplicates")
            with conn:
                conn.execute("UPDATE projects SET status='analyzing', updated_at=? WHERE id=?", (db.utc_now(), project_id))
            settings = db.user_settings(conn, job.user_id, project_id)
            summary = analysis.recompute(
                conn,
                project_id,
                settings.get("preset") or "conservative",
                job.user_id,
                settings.get("keep_target"),
            )
            with conn:
                conn.execute("UPDATE projects SET status='ready', updated_at=? WHERE id=?", (db.utc_now(), project_id))
            result = dict(job.result or {})
            result["proxyFailures"] = proxy_failures
            result["vision"] = vision_summary
            result["analysis"] = summary
            job.update(
                status="done",
                stage="done",
                done=1,
                total=1,
                result=result,
                message="Analysis complete",
                finished_at=db.utc_now(),
            )
        except _Cancelled:
            try:
                settings = db.user_settings(conn, job.user_id, project_id)
                analysis.recompute(
                    conn,
                    project_id,
                    settings.get("preset") or "conservative",
                    job.user_id,
                    settings.get("keep_target"),
                )
            except Exception:
                pass
            with conn:
                conn.execute("UPDATE projects SET status='interrupted', updated_at=? WHERE id=?", (db.utc_now(), project_id))
            job.update(
                status="canceled",
                message="Canceled — progress is saved",
                finished_at=db.utc_now(),
            )
        except Exception as exc:
            traceback.print_exc()
            try:
                with conn:
                    conn.execute("UPDATE projects SET status='error', updated_at=? WHERE id=?", (db.utc_now(), project_id))
            except Exception:
                pass
            job.update(status="failed", error=str(exc), message="Analysis failed", finished_at=db.utc_now())
        finally:
            conn.close()

    def _run_vision_stage(self, job: Job, conn: sqlite3.Connection, project_id: int, proxy_root: Path) -> dict[str, Any]:
        """Tag scene labels and text with the local macOS Vision framework."""
        rows = db.rows_to_dicts(
            conn.execute(
                "SELECT id FROM files WHERE project_id=? AND hidden_from_review=0 AND status='ready' "
                "AND vision_labels IS NULL ORDER BY id",
                (project_id,),
            ).fetchall()
        )
        total = len(rows)
        if total == 0:
            return {"tagged": 0, "failed": 0, "unavailable": False}
        if not vision.available():
            job.update(
                stage="vision",
                done=0,
                total=total,
                message="Scene tagging unavailable — visual scoring only",
            )
            return {"tagged": 0, "failed": 0, "unavailable": True}

        tagged = 0
        failed = 0
        chunk_size = 128
        job.update(stage="vision", done=0, total=total, message=f"Tagging scenes (0/{total})")
        for start in range(0, total, chunk_size):
            if job.cancel_event.is_set():
                break
            chunk = rows[start : start + chunk_size]
            paths = [str(proxy_root / f"{row['id']}_p.jpg") for row in chunk]
            results = vision.probe_paths(paths)
            now = db.utc_now()
            with conn:
                for row, path in zip(chunk, paths):
                    record = results.get(path)
                    if record is None:
                        failed += 1
                        continue
                    labels = record.get("labels") or []
                    conn.execute(
                        """
                        UPDATE files SET vision_labels=?, vision_text_lines=?, vision_text_chars=?,
                          vision_text_area=?, vision_text_confidence=?, updated_at=? WHERE id=?
                        """,
                        (
                            json.dumps(labels, separators=(",", ":")),
                            int(record.get("textLines") or 0),
                            int(record.get("textChars") or 0),
                            float(record.get("textArea") or 0.0),
                            float(record.get("textConfidence") or 0.0),
                            now,
                            row["id"],
                        ),
                    )
                    tagged += 1
            done = min(total, start + len(chunk))
            job.update(
                done=done,
                total=total,
                message=f"Tagging scenes ({done}/{total})",
            )
        return {"tagged": tagged, "failed": failed, "unavailable": False}

    # ---------------------------------------------------------------------- copy
    def start_copy(
        self,
        project_id: int,
        user_id: int,
        file_ids: list[int],
        destination: str | None,
        raw_mode: str | None = None,
    ) -> dict[str, Any]:
        active = self.active_for_project(project_id)
        if active is not None:
            return active.snapshot()
        job = Job(project_id, "copy", user_id)
        with self._lock:
            self.jobs[job.id] = job
        job.update(status="running", stage="copy", message="Preparing verified copy")
        thread = threading.Thread(
            target=self._run_copy, args=(job, file_ids, destination, raw_mode), daemon=True, name=f"copy-{project_id}"
        )
        thread.start()
        return job.snapshot()

    def _run_copy(
        self,
        job: Job,
        file_ids: list[int],
        destination: str | None,
        raw_mode: str | None = None,
    ) -> None:
        def progress(data: dict[str, Any]) -> None:
            job.update(
                done=int(data.get("done") or 0),
                total=int(data.get("total") or 0),
                bytes_done=int(data.get("bytesDone") or 0),
                bytes_total=int(data.get("bytesTotal") or 0),
                message=data.get("message") or "Copying",
                result={"copy": {k: v for k, v in data.items() if k not in ("message",)}},
            )

        try:
            result = copy_engine.run_copy(
                job.project_id,
                file_ids,
                destination,
                job.cancel_event,
                progress,
                job.user_id,
                raw_mode,
            )
            job.update(
                status="canceled" if result.get("status") == "canceled" else "done",
                stage="done",
                message="Copy canceled" if result.get("status") == "canceled" else "Copy complete",
                result={"copy": result},
                done=result["counts"]["totalFiles"],
                total=result["counts"]["totalFiles"],
                finished_at=db.utc_now(),
            )
        except Exception as exc:
            traceback.print_exc()
            job.update(
                status="failed",
                error=str(exc),
                message="Copy failed",
                finished_at=db.utc_now(),
            )

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.status in ("queued", "running"):
                job.cancel_event.set()


class _Cancelled(Exception):
    pass


manager = JobManager()
