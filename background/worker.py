"""Run queued jobs one at a time in a persistent transcription worker process."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import config
from appdata import TokenStore
from background.jobs import JobStatus, JobStore, job_options
from background.process_tree import assign_to_job, create_kill_on_close_job, terminate_process_tree
from paths import fingerprint
from protocol import EVENT_COMPLETED, EVENT_FAILED, SERVER_READY, JobRequest, parse_event


_WORKER_START_TIMEOUT_SECONDS = 30.0
_ERROR_MARKERS = ("error", "traceback", "could not load", "cannot load", "invalid handle", "fatal")


def _looks_like_error(line: str) -> bool:
    lower = line.lower()
    return any(marker in lower for marker in _ERROR_MARKERS)


class WorkerController:
    def __init__(self, store: JobStore, token_store: TokenStore, on_event: Callable[[str, dict], None]):
        self.store = store
        self.token_store = token_store
        self.on_event = on_event
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._active: str | None = None
        self._stop = threading.Event()
        self._job = None
        self._thread: threading.Thread | None = None
        self._idle_since = time.monotonic()

    @property
    def active_job_id(self) -> str | None:
        with self._lock:
            return self._active

    def start(self) -> None:
        try:
            self._job = create_kill_on_close_job()
        except OSError as exc:
            self.on_event("log", {"message": f"Worker will outlive a crashed tray: {exc}"})
        self._thread = threading.Thread(target=self._run_loop, name="transcription-worker", daemon=True)
        self._thread.start()

    def idle_seconds(self) -> float:
        """How long nothing has been running or waiting; 0 while there is work."""
        with self._lock:
            if self._active or self._queue:
                return 0.0
            return time.monotonic() - self._idle_since

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            self._queue.append(job_id)
        self._emit(job_id, "queued", {"message": "Queued"})

    def stop(self) -> None:
        self._stop.set()
        job_id = self._request_cancel("Cancellation requested on exit")
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._discard_proc(self._proc)
        if job_id:
            self._cancelled(job_id, "Cancelled on exit")

    def cancel_active(self) -> None:
        self._request_cancel("Cancellation requested")

    def _request_cancel(self, message: str) -> str | None:
        with self._lock:
            job_id = self._active
            if not job_id or not self.store.request_cancel(job_id, message):
                return None
            proc, self._proc = self._proc, None
        if proc:
            terminate_process_tree(proc)
        self._emit(job_id, "cancel_requested", {"message": message})
        return job_id

    def _cancelled(self, job_id: str, message: str = "Cancelled") -> None:
        if self.store.cancel(job_id, message):
            self._emit(job_id, JobStatus.CANCELLED, {"message": message})

    def _finish(self, job_id: str, status: JobStatus, message: str, **changes) -> None:
        if status == JobStatus.FAILED:
            changes["error"] = message
        ended = self.store.finish(job_id, status, message, **changes)
        if ended == JobStatus.CANCELLED:
            self._emit(job_id, ended, {"message": "Cancelled"})
        elif ended:
            self._emit(job_id, ended, {"message": message, **changes})

    def _emit(self, job_id: str, event: str, payload: dict) -> None:
        self.on_event(event, {"job_id": job_id, "event": event, **payload})

    def _discard_proc(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        with self._lock:
            if self._proc is proc:
                self._proc = None
        terminate_process_tree(proc)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                job_id = self._queue.popleft() if self._queue else None
                self._active = job_id
            if not job_id:
                self._retire_idle_worker()
                self._stop.wait(0.5)
                continue
            try:
                self._run_job(job_id)
            except Exception as exc:
                try:
                    self._finish(job_id, JobStatus.FAILED, str(exc))
                except sqlite3.Error as store_exc:
                    self._emit(job_id, "log", {"message": f"Could not save task failure: {store_exc}; {exc}"})
            finally:
                with self._lock:
                    self._active = None
                    self._idle_since = time.monotonic()

    def _retire_idle_worker(self) -> None:
        """Python and CUDA keep a finished worker's memory; only exiting returns it to the system."""
        with self._lock:
            proc = self._proc
            if proc is None or time.monotonic() - self._idle_since < config.IDLE_EXIT_SECONDS:
                return
        self._discard_proc(proc)
        self.on_event("log", {"message": "Worker exited after being idle; models released"})

    def _ensure_server_running(self, job_id: str) -> subprocess.Popen:
        with self._lock:
            if self.store.get(job_id)["status"] != JobStatus.RUNNING:
                raise RuntimeError("Task cancelled before worker startup")
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            proc = self._spawn()
            self._proc = proc
        self._await_ready(proc)
        return proc

    def _spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(config.ROOT_DIR / "transcribe.py"), "--server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=str(config.ROOT_DIR),
            # Below normal, so the desktop stays responsive while a long job runs.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
        )
        if self._job is not None:
            try:
                assign_to_job(self._job, proc.pid)
            except OSError as exc:
                self.on_event("log", {"message": f"Worker will outlive a crashed tray: {exc}"})
        return proc

    def _await_ready(self, proc: subprocess.Popen) -> None:
        startup = {"ready": False, "logs": [], "error": None}

        def read_startup() -> None:
            try:
                for raw in proc.stdout:
                    line = raw.rstrip()
                    if line == SERVER_READY:
                        startup["ready"] = True
                        return
                    startup["logs"].append(line)
            except Exception as exc:
                startup["error"] = exc

        reader = threading.Thread(target=read_startup, name="worker-startup", daemon=True)
        reader.start()
        reader.join(timeout=_WORKER_START_TIMEOUT_SECONDS)

        for line in startup["logs"]:
            self.on_event("log", {"message": f"[Worker Startup] {line}"})

        if reader.is_alive() or not startup["ready"]:
            self._discard_proc(proc)
            if reader.is_alive():
                raise TimeoutError(f"Worker did not become ready within {_WORKER_START_TIMEOUT_SECONDS:g} seconds")
            detail = str(startup["error"]) if startup["error"] else f"exit code {proc.poll()}"
            raise RuntimeError(f"Worker exited before readiness confirmation ({detail})")

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        request = JobRequest(job_id=job_id, input_path=job["source_path"], output_dir=str(config.TRANSCRIPT_DIR),
                             options=job_options(job), token=self.token_store.get(),
                             source_fingerprint=fingerprint(job["source_key"]))

        if not self.store.start(job_id):
            self._cancelled(job_id)
            return
        self._emit(job_id, "started", {"stage": "starting", "message": "Starting worker"})

        proc = None
        try:
            proc = self._ensure_server_running(job_id)
            with self._lock:
                dispatch = self.store.get(job_id)["status"] == JobStatus.RUNNING
                if dispatch:
                    proc.stdin.write(request.to_line() + "\n")
                    proc.stdin.flush()
            if not dispatch:
                self._discard_proc(proc)
                self._cancelled(job_id)
                return
            terminal, failure = self._read_until_terminal(proc, job_id)
        except Exception as exc:
            terminal, failure = None, str(exc)
        if terminal is None:
            # The process may still be alive in an unknown state; never hand it the next job.
            self._discard_proc(proc)
        self._settle(job_id, terminal, failure)

    def _read_until_terminal(self, proc: subprocess.Popen, job_id: str) -> tuple[str | None, str | None]:
        """Apply worker events until the job's terminal event; return it, or why there was none."""
        crash_line = ""
        for raw in proc.stdout:
            line = raw.rstrip()
            event = parse_event(line)
            if event is None:
                if _looks_like_error(line):
                    crash_line = line
                self.on_event("log", {"job_id": job_id, "message": line})
            elif event["event"] == "parse_error":
                self.on_event("log", {"job_id": job_id, "message": event["message"]})
            elif event.get("job_id") and event["job_id"] != job_id:
                self.on_event("log", {"job_id": job_id,
                                      "message": f"Ignored stale worker event for job {event['job_id']}"})
            else:
                self._apply_worker_event(job_id, event)
                if event["event"] in (EVENT_COMPLETED, EVENT_FAILED):
                    return event["event"], None
        message = f"Worker crashed: {crash_line}" if crash_line else "Worker connection closed before a terminal event"
        code = proc.poll()
        return None, message if code is None else f"{message} (exit code {code})"

    def _settle(self, job_id: str, terminal: str | None, failure: str | None) -> None:
        latest = self.store.get(job_id)
        if latest["status"] in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}:
            self._cancelled(job_id)
            return
        if terminal == EVENT_COMPLETED:
            output = latest["output_path"]
            try:
                output_ok = bool(output) and Path(output).is_file() and Path(output).stat().st_size > 0
            except OSError as exc:
                failure = f"Could not check the transcript: {exc}"
            else:
                if output_ok:
                    self._finish(job_id, JobStatus.COMPLETED, "Completed", progress=1.0)
                    return
                failure = f"Worker reported success but the transcript is missing or empty: {output or '(no path)'}"
        self._finish(job_id, JobStatus.FAILED, failure or latest["error"] or "Worker failed")

    def _apply_worker_event(self, job_id: str, event: dict) -> None:
        event = {**event, "job_id": job_id}
        kind = event["event"]
        changes = {"stage": event.get("stage") or kind, "message": event.get("message", kind)}
        if isinstance(event.get("progress"), (int, float)):
            changes["progress"] = max(0.0, min(1.0, float(event["progress"])))
        if kind == EVENT_FAILED:
            changes["error"] = event.get("error", event.get("message", "Error"))
        elif kind == EVENT_COMPLETED and "output_path" in event:
            changes["output_path"] = event["output_path"]
        if self.store.record_progress(job_id, **changes) and kind not in (EVENT_COMPLETED, EVENT_FAILED):
            self.on_event(kind, event)
