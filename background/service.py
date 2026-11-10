"""The always-on core behind the tray: watch folder, durable job state, and the serial worker queue.

It imports no Qt, so the API and tests drive it directly.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from appdata import AppSettings, TokenStore
from background import jobs
from background.activity_log import ActivityLog
from background.jobs import RETRYABLE, CacheCleanup, JobStore, job_options
from background.watcher import FileWatcher
from background.worker import WorkerController
from paths import SourceChangedError, is_media_file, source_identity
from protocol import JobOptions


class BackgroundService:
    def __init__(self, on_event: Callable[[str, dict], None]):
        self.settings = AppSettings.load()
        self.store = JobStore()
        self.token_store = TokenStore()
        self.on_event = on_event
        self._log = ActivityLog(self.token_store)
        self.worker = WorkerController(self.store, self.token_store, self._event)
        self.watcher = FileWatcher(self._recording_ready, self._event)
        self._offers: dict[str, Path] = {}
        self._offers_lock = threading.Lock()

    def start(self) -> None:
        self.store.recover_interrupted()
        self.worker.start()
        if self.settings.watcher_enabled:
            self._watch()
        else:
            self._event("watch_stopped", {"message": "Watcher paused"})
        for row in self.store.queued():
            self.worker.enqueue(row["job_id"])
        self._event("service_started", {"message": "Background service started"})

    def stop(self) -> None:
        self.watcher.stop()
        self.worker.stop()

    def _watch(self) -> None:
        self.watcher.start(Path(self.settings.watch_dir))

    def set_watcher_enabled(self, enabled: bool) -> None:
        self.settings.watcher_enabled = enabled
        self.settings.save()
        if enabled:
            self._watch()
        else:
            self.watcher.stop()
            self._event("watch_stopped", {"message": "Watcher paused"})

    def set_watch_dir(self, path: Path) -> None:
        was_enabled = self.settings.watcher_enabled
        self.watcher.stop()
        self.settings.watch_dir = str(path)
        self.settings.save()
        if was_enabled:
            self._watch()

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
            with self._offers_lock:
                self._offers.pop(key, None)
            row = self._enqueue(path, JobOptions())
            if row:
                rows.append(row)
        return rows

    def _recording_ready(self, path: Path) -> None:
        """Offer a finished recording instead of queueing it: most OBS recordings need no transcript."""
        if self.store.knows(path):
            return
        key = str(path.resolve())
        with self._offers_lock:
            if key in self._offers:
                return
            self._offers[key] = path
        self._event("recording_ready", {"path": key, "message": f"New recording: {path.name}"})

    def offered_recordings(self) -> list[str]:
        with self._offers_lock:
            return list(self._offers)

    def accept_recording(self, path: str) -> dict | None:
        with self._offers_lock:
            recording = self._offers.pop(path, None)
        return self._enqueue(recording, JobOptions()) if recording else None

    def dismiss_recording(self, path: str) -> None:
        with self._offers_lock:
            self._offers.pop(path, None)

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
        self.on_event(event, payload)
