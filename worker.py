"""Run queued jobs one at a time in a persistent transcription worker process."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from appdata import AppSettings, TokenStore
from jobs import TERMINAL, JobStatus, JobStore, job_options
from paths import fingerprint
from process_tree import assign_to_job, create_kill_on_close_job, terminate_process_tree
from protocol import SERVER_READY, looks_like_error, parse_event


TRANSCRIBE_SCRIPT = Path(__file__).parent / "transcribe.py"
WORKER_START_TIMEOUT_SECONDS = 30.0


class WorkerController:
    def __init__(self, settings: AppSettings, store: JobStore, token_store: TokenStore,
                 on_event: Callable[[str, dict], None]):
        self.settings = settings
        self.store = store
        self.token_store = token_store
        self.on_event = on_event
        self._queue: deque[dict] = deque()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._active: dict | None = None
        self._stop = threading.Event()
        self._job = None
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> dict | None:
        with self._lock:
            return self._active

    def start(self) -> None:
        if os.name == "nt":
            try:
                self._job = create_kill_on_close_job()
            except OSError as exc:
                self.on_event("log", {"message": f"Worker will outlive a crashed tray: {exc}"})
        self._thread = threading.Thread(target=self._run_loop, name="transcription-worker", daemon=True)
        self._thread.start()

    def enqueue(self, row: dict) -> None:
        with self._lock:
            self._queue.append(row)
        self._emit(row["job_id"], "queued", {"message": "Queued"})

    def stop(self) -> None:
        self._stop.set()
        row = self._request_cancel("Cancellation requested on exit")
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._discard_proc(self._proc)
        if row:
            self._cancelled(row["job_id"], "Cancelled on exit")

    def cancel_active(self) -> None:
        self._request_cancel("Cancellation requested")

    def _request_cancel(self, message: str) -> dict | None:
        with self._lock:
            row = self._active
            if not row or not self.store.request_cancel(row["job_id"], message):
                return None
            proc, self._proc = self._proc, None
        if proc:
            terminate_process_tree(proc)
        self._emit(row["job_id"], "cancel_requested", {"message": message})
        return row

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
                row = self._queue.popleft() if self._queue else None
                self._active = row
            if not row:
                self._stop.wait(0.5)
                continue
            try:
                self._run_one_server(row)
            except Exception as exc:
                try:
                    self._finish(row["job_id"], JobStatus.FAILED, str(exc))
                except sqlite3.Error as store_exc:
                    self._emit(row["job_id"], "log", {"message": f"Could not save task failure: {store_exc}; {exc}"})
            finally:
                with self._lock:
                    self._active = None

    def _ensure_server_running(self, job_id: str) -> subprocess.Popen:
        with self._lock:
            if self.store.get(job_id)["status"] != JobStatus.RUNNING:
                raise RuntimeError("Task cancelled before worker startup")
            if self._proc is not None and self._proc.poll() is None:
                return self._proc

            popen_kwargs = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                [sys.executable, str(TRANSCRIBE_SCRIPT), "--server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=str(Path(__file__).parent),
                **popen_kwargs,
            )
            self._proc = proc
            if self._job is not None:
                try:
                    assign_to_job(self._job, proc.pid)
                except OSError as exc:
                    self.on_event("log", {"message": f"Worker will outlive a crashed tray: {exc}"})

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
        reader.join(timeout=WORKER_START_TIMEOUT_SECONDS)

        for line in startup["logs"]:
            self.on_event("log", {"message": f"[Worker Startup] {line}"})

        if reader.is_alive() or not startup["ready"]:
            self._discard_proc(proc)
            if reader.is_alive():
                raise TimeoutError(
                    f"Worker did not become ready within {WORKER_START_TIMEOUT_SECONDS:g} seconds"
                )
            detail = str(startup["error"]) if startup["error"] else f"exit code {proc.poll()}"
            raise RuntimeError(f"Worker exited before readiness confirmation ({detail})")

        return proc

    def _run_one_server(self, row: dict) -> None:
        job_id = row["job_id"]
        job = self.store.get(job_id) or row
        options = job_options(job, self.settings.job_options())
        out_dir = Path(self.settings.transcript_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.store.start(job_id):
            self._cancelled(job_id)
            return
        self._emit(job_id, "started", {"stage": "starting", "message": "Starting worker"})

        proc = None
        try:
            proc = self._ensure_server_running(job_id)
            task = {
                "command": "transcribe",
                "job_id": job_id,
                "input_path": job["source_path"],
                "source_fingerprint": fingerprint(job["source_key"]),
                "output_dir": str(out_dir),
                "token": self.token_store.get(),
                **asdict(options),
            }
            with self._lock:
                dispatch = self.store.get(job_id)["status"] == JobStatus.RUNNING
                if dispatch:
                    proc.stdin.write(json.dumps(task) + "\n")
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
                if looks_like_error(line):
                    crash_line = line
                self.on_event("log", {"job_id": job_id, "message": line})
            elif event["event"] == "parse_error":
                self.on_event("log", {"job_id": job_id, "message": event["message"]})
            elif event.get("job_id") and event["job_id"] != job_id:
                self.on_event("log", {"job_id": job_id,
                                      "message": f"Ignored stale worker event for job {event['job_id']}"})
            else:
                self._apply_worker_event(job_id, event)
                if event["event"] in TERMINAL:
                    return event["event"], None
        message = f"Worker crashed: {crash_line}" if crash_line else "Worker connection closed before a terminal event"
        code = proc.poll()
        return None, message if code is None else f"{message} (exit code {code})"

    def _settle(self, job_id: str, terminal: str | None, failure: str | None) -> None:
        latest = self.store.get(job_id)
        if terminal == JobStatus.CANCELLED or latest["status"] in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}:
            self._cancelled(job_id)
            return
        if terminal in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNING}:
            output = latest["output_path"]
            try:
                output_ok = bool(output) and Path(output).is_file() and Path(output).stat().st_size > 0
            except OSError as exc:
                failure = f"Could not check the transcript: {exc}"
            else:
                if output_ok:
                    warned = terminal == JobStatus.COMPLETED_WITH_WARNING or bool(latest["error"])
                    status = JobStatus.COMPLETED_WITH_WARNING if warned else JobStatus.COMPLETED
                    self._finish(job_id, status, "Completed with warnings" if warned else "Completed",
                                 progress=1.0, output_path=output, error=latest["error"])
                    return
                failure = f"Worker reported success but the transcript is missing or empty: {output or '(no path)'}"
        self._finish(job_id, JobStatus.FAILED, failure or latest["error"] or "Worker failed")

    def _apply_worker_event(self, job_id: str, event: dict) -> None:
        event = {**event, "job_id": job_id}
        kind = event["event"]
        changes = {"stage": event.get("stage") or kind, "message": event.get("message", kind)}
        if isinstance(event.get("progress"), (int, float)):
            changes["progress"] = max(0.0, min(1.0, float(event["progress"])))
        if kind == JobStatus.FAILED:
            changes["error"] = event.get("error", event.get("message", "Error"))
        elif kind in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNING} and "output_path" in event:
            changes["output_path"] = event["output_path"]
        elif kind == "warning":
            changes["error"] = event.get("message", "Warning")
        if self.store.record_progress(job_id, **changes) and kind not in TERMINAL:
            self.on_event(kind, event)
