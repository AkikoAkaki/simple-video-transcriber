"""The always-on core behind the tray: watch folder, durable job state, and the serial worker queue.

It imports no Qt, so the API and tests drive it directly.
"""

from __future__ import annotations

import threading
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import appdata
import config
import jobs
from appdata import AppSettings, TokenStore
from jobs import RETRYABLE, CacheCleanup, JobStore, job_options
from paths import SourceChangedError, source_identity
from protocol import JobOptions
from watcher import FileWatcher
from worker import WorkerController


class BackgroundService:
    def __init__(self, settings: AppSettings | None = None,
                 on_event: Callable[[str, dict], None] | None = None):
        self.settings = settings or AppSettings.load()
        self.store = JobStore()
        self.token_store = TokenStore()
        self.on_event = on_event or (lambda _event, _payload: None)
        self._log_lock = threading.Lock()
        self._log_count = 0
        self.worker = WorkerController(self.settings, self.store, self.token_store, self._event)
        self.watcher = FileWatcher(self.settings, self._recording_ready, self._event)
        # Finished recordings from the watch folder wait here until the user chooses to transcribe them.
        self._offers: dict[str, Path] = {}
        self._offers_lock = threading.Lock()

    def start(self) -> None:
        self.store.recover_interrupted()
        self.worker.start()
        if self.settings.watcher_enabled:
            self.watcher.start()
        else:
            self._event("watch_stopped", {"message": "Watcher paused"})
        for row in self.store.queued():
            self.worker.enqueue(row)
        self._event("service_started", {"message": "Background service started"})

    def stop(self) -> None:
        self.watcher.stop()
        self.worker.stop()

    def set_watcher_enabled(self, enabled: bool) -> None:
        self.settings.watcher_enabled = enabled
        self.settings.save()
        if enabled:
            self.watcher.start()
        else:
            self.watcher.stop()
            self._event("watch_stopped", {"message": "Watcher paused"})

    def set_watch_dir(self, path: Path) -> None:
        was_enabled = self.settings.watcher_enabled
        self.watcher.stop()
        self.settings.watch_dir = str(path)
        self.settings.save()
        if was_enabled:
            self.watcher.start()

    def add_file(self, path: Path, overrides: dict | None = None, *, from_watcher: bool = False) -> dict | None:
        return self._enqueue(path, replace(self.settings.job_options(), **(overrides or {})), from_watcher)

    def add_files(self, paths) -> list[dict]:
        """Queue each audio or video file once; folders, other files, and repeats are skipped."""
        rows, seen = [], set()
        for path in map(Path, paths):
            if path.suffix.lower() not in config.WATCH_EXTENSIONS or not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            with self._offers_lock:
                self._offers.pop(key, None)
            row = self.add_file(path)
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
        if recording is None:
            raise ValueError("This recording is no longer waiting for a decision.")
        return self._enqueue(recording, self.settings.job_options(), from_watcher=True)

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
        return self._enqueue(source, job_options(row, self.settings.job_options()))

    def _enqueue(self, path: Path, options: JobOptions, from_watcher: bool = False) -> dict | None:
        row = self.store.create_if_new(path, options, from_watcher=from_watcher)
        if row:
            self.worker.enqueue(row)
        return row

    def active_job(self) -> dict | None:
        return self.worker.active

    def cancel_active(self) -> None:
        self.worker.cancel_active()

    def get_job(self, job_id: str) -> dict | None:
        return self.store.get(job_id)

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        return self.store.recent(limit)

    def set_token(self, token: str) -> None:
        self.token_store.set(token)

    def cache_size_bytes(self) -> int:
        return jobs.cache_size_bytes(config.CACHE_DIR)

    def clear_audio_cache(self) -> CacheCleanup:
        return jobs.clear_audio_cache(config.CACHE_DIR, self.store)

    def _event(self, event: str, payload: dict) -> None:
        self._write_log(event, payload)
        self.on_event(event, payload)

    def _write_log(self, event: str, payload: dict) -> None:
        """Persist a concise readable activity log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = str(payload.get("message") or event.replace("_", " "))
        token = self.token_store.get()
        if token:
            message = message.replace(token, "[redacted]")
        job_id = payload.get("job_id")
        line = f"{timestamp} [{event}] {message}\n"
        log_dir = appdata.LOG_DIR
        with self._log_lock, suppress(OSError):
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_count += 1
            app_log = log_dir / "app.log"
            if self._log_count % 100 == 0:
                with suppress(OSError):
                    if app_log.stat().st_size > 5 * 1024 * 1024:
                        app_log.replace(log_dir / "app.log.1")
            with app_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
            if job_id:
                with (log_dir / f"{job_id}.log").open("a", encoding="utf-8") as handle:
                    handle.write(line)
