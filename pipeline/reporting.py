"""How a running job tells the outside world what it is doing."""

from __future__ import annotations

import contextlib
import threading

from protocol import EVENT_COMPLETED, EVENT_FAILED, STAGES, STEP_LABELS, format_event


_OUTPUT_LOCK = threading.Lock()
_HEARTBEAT_SECONDS = 10.0


def log(line: str) -> None:
    """Recognition and diarization may report at once; keep each output line whole."""
    with _OUTPUT_LOCK:
        print(line, flush=True)


class Reporter:
    """Reports nothing but plain log lines; subclasses decide what else a listener sees."""

    def stage(self, stage: str, progress: float, message: str) -> None:
        log(message)

    def progress(self, stage: str, progress: float | None, message: str) -> None:
        pass

    def heartbeat(self, stage: str, message: str):
        return contextlib.nullcontext()

    def completed(self, output_path: str, message: str) -> None:
        log(message)


class ConsoleReporter(Reporter):
    """The command line: numbered steps."""

    def stage(self, stage: str, progress: float, message: str) -> None:
        log(f"[{STAGES[stage].step + 1}/{len(STEP_LABELS)}] {message}")


class BackgroundReporter(Reporter):
    """A stage running beside another, which owns the job's progress, only logs."""

    def __init__(self, name: str):
        self.name = name

    def stage(self, stage: str, progress: float, message: str) -> None:
        log(f"      [{self.name}] {message}")


class EventReporter(Reporter):
    """The persistent worker: structured events for the service that started this job."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def _emit(self, event: str, **payload) -> None:
        log(format_event({"event": event, "job_id": self.job_id, **payload}))

    def stage(self, stage: str, progress: float, message: str) -> None:
        self._emit("stage", stage=stage, progress=progress, message=message)

    def progress(self, stage: str, progress: float | None, message: str) -> None:
        self._emit("progress", stage=stage, progress=progress, message=message)

    @contextlib.contextmanager
    def heartbeat(self, stage: str, message: str):
        stop = threading.Event()

        def run():
            while not stop.wait(_HEARTBEAT_SECONDS):
                self._emit("heartbeat", stage=stage, message=message)

        thread = threading.Thread(target=run, name=f"{stage}-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def completed(self, output_path: str, message: str) -> None:
        self._emit(EVENT_COMPLETED, stage="completed", progress=1.0, message=message, output_path=output_path)

    def failed(self, message: str) -> None:
        self._emit(EVENT_FAILED, stage="worker", message=message, error=message)
