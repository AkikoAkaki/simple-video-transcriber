"""The core behind the tray: durable job state and the serial worker queue.

It imports no Qt, so the API and tests drive it directly.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import appdata
from appdata import TokenStore
from background import jobs
from background.activity_log import ActivityLog
from background.jobs import RETRYABLE, CacheCleanup, JobStore, job_options, public_job
from background.worker import WorkerController
from paths import SourceChangedError, is_media_file, source_identity
from protocol import JobOptions


RECENT_JOBS = 20
# Events after which the published history no longer matches the job store.
_JOB_CHANGES = frozenset({"service_started", "queued", "started", "cancel_requested", "completed", "failed", "cancelled"})


class BackgroundService:
    def __init__(self, on_event: Callable[[str, dict], None]):
        self.store = JobStore()
        self.token_store = TokenStore()
        self.on_event = on_event
        self._log = ActivityLog(self.token_store)
        self.worker = WorkerController(self.store, self.token_store, self._event)
        self._history_lock = threading.Lock()

    def start(self) -> None:
        self.store.recover_interrupted()
        self.worker.start()
        for row in self.store.queued():
            self.worker.enqueue(row["job_id"])
        self._event("service_started", {"message": "Background service started"})

    def stop(self) -> None:
        self.worker.stop()

    def idle_seconds(self) -> float:
        return self.worker.idle_seconds()

    def add_files(self, paths) -> list[dict]:
        """Queue each audio or video file once; folders, other files, and repeats are skipped."""
        rows, seen = [], set()
        for path in map(Path, paths):
            if not is_media_file(path):
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            row = self._enqueue(path, JobOptions())
            if row:
                rows.append(row)
        return rows

    def retry_job(self, job_id: str) -> dict | None:
        row = self.store.get(job_id)
        if not row or row["status"] not in RETRYABLE:
            raise ValueError("Select a failed or cancelled task to retry.")
        source = Path(row["source_path"])
        if not source.is_file():
            raise FileNotFoundError("The original recording was removed. Restore it before retrying.")
        if source_identity(source).key != row["source_key"]:
            raise SourceChangedError()
        return self._enqueue(source, job_options(row))

    def _enqueue(self, path: Path, options: JobOptions) -> dict | None:
        row = self.store.create_if_new(path, options)
        if row:
            self.worker.enqueue(row["job_id"])
        return row

    def active_job(self) -> dict | None:
        job_id = self.worker.active_job_id
        return self.store.get(job_id) if job_id else None

    def cancel_active(self) -> None:
        self.worker.cancel_active()

    def get_job(self, job_id: str) -> dict | None:
        return self.store.get(job_id)

    def recent_jobs(self, limit: int) -> list[dict]:
        return self.store.recent(limit)

    def set_token(self, token: str) -> None:
        self.token_store.set(token)

    def cache_size_bytes(self) -> int:
        return jobs.cache_size_bytes()

    def clear_audio_cache(self) -> CacheCleanup:
        return jobs.clear_audio_cache(self.store)

    def _event(self, event: str, payload: dict) -> None:
        self._log.write(event, payload)
        if event in _JOB_CHANGES:
            self._publish_history()
        self.on_event(event, payload)

    def _publish_history(self) -> None:
        with self._history_lock:
            try:
                appdata.publish_history([public_job(row) for row in self.store.recent(RECENT_JOBS)])
            except OSError as exc:
                self._log.write("log", {"message": f"Could not publish task history: {exc}"})
