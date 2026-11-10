"""The readable text log of everything the background service does."""

from __future__ import annotations

import threading
from contextlib import suppress
from datetime import datetime

import appdata
from appdata import TokenStore


_ROTATE_BYTES = 5 * 1024 * 1024
_ROTATE_CHECK_EVERY = 100


class ActivityLog:
    def __init__(self, token_store: TokenStore):
        self.token_store = token_store
        self._lock = threading.Lock()
        self._count = 0

    def write(self, event: str, payload: dict) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = str(payload.get("message") or event.replace("_", " "))
        token = self.token_store.get()
        if token:
            message = message.replace(token, "[redacted]")
        job_id = payload.get("job_id")
        line = f"{timestamp} [{event}] {message}\n"
        log_dir = appdata.log_dir()
        with self._lock, suppress(OSError):
            log_dir.mkdir(parents=True, exist_ok=True)
            self._count += 1
            app_log = log_dir / "app.log"
            if self._count % _ROTATE_CHECK_EVERY == 0:
                with suppress(OSError):
                    if app_log.stat().st_size > _ROTATE_BYTES:
                        app_log.replace(log_dir / "app.log.1")
            with app_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
            if job_id:
                with (log_dir / f"{job_id}.log").open("a", encoding="utf-8") as handle:
                    handle.write(line)
