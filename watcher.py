"""Detect finished recordings in the watch folder."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
from appdata import AppSettings


ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33


class FileStamp(NamedTuple):
    size: int
    mtime_ns: int


def _stamp(stat: os.stat_result) -> FileStamp:
    return FileStamp(stat.st_size, stat.st_mtime_ns)


def _is_supported(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in config.WATCH_EXTENSIONS


class _WatchHandler(FileSystemEventHandler):
    def __init__(self, owner: "FileWatcher"):
        self.owner = owner

    def on_created(self, event):
        if not event.is_directory:
            self.owner.track(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.owner.track(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self.owner.moved(Path(event.src_path), Path(event.dest_path))


class FileWatcher:
    """Event-driven watcher with a stability debounce for files OBS is writing."""

    # Watchdog can drop events (buffer overflow, sleep/wake, network drives).
    # Reconcile the directory this often so a missed file is still picked up.
    RESCAN_INTERVAL_SECONDS = 60.0

    def __init__(self, settings: AppSettings, on_ready: Callable[[Path], object],
                 on_event: Callable[[str, dict], None]):
        self.settings = settings
        self.on_ready = on_ready
        self.on_event = on_event
        self._pending: dict[str, tuple[int, float]] = {}
        # Files present at startup or already queued. Events that leave the stamp
        # unchanged (playback touching the access time) must not queue an archive.
        self._seen_stamps: dict[str, FileStamp] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_rescan = time.monotonic()
        self._observer: Observer | None = None
        self._thread: threading.Thread | None = None
        self._resolved_watch_dir: Path | None = None

    def start(self) -> None:
        root = Path(self.settings.watch_dir).expanduser().resolve()
        with self._lock:
            if root != self._resolved_watch_dir:
                self._pending.clear()
                self._seen_stamps.clear()
            self._resolved_watch_dir = root
        if not root.exists():
            self.on_event("watch_error", {"message": f"Watch folder unavailable: {root}"})
            self._stop.clear()
            self._thread = threading.Thread(target=self._retry_until_available,
                                             args=(root,), name="watch-reconnect", daemon=True)
            self._thread.start()
            return
        self._start_existing(root)

    def _start_existing(self, root: Path) -> None:
        self._seen_stamps = {str(p.resolve()): _stamp(p.stat()) for p in root.iterdir() if _is_supported(p)}
        self._observer = Observer()
        self._observer.schedule(_WatchHandler(self), str(root), recursive=False)
        self._observer.start()
        self._stop.clear()
        self._last_rescan = time.monotonic()
        self._thread = threading.Thread(target=self._tick_loop, name="file-stability", daemon=True)
        self._thread.start()
        self.on_event("watch_started", {"path": str(root), "baseline_count": len(self._seen_stamps)})

    def _retry_until_available(self, root: Path) -> None:
        while not self._stop.wait(30.0):
            if root.exists():
                self._start_existing(root)
                return

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def moved(self, source: Path, dest: Path) -> None:
        """Carry a renamed file's known state, so renaming an archive does not queue it."""
        with self._lock:
            stamp = self._seen_stamps.pop(str(source.resolve()), None)
            if stamp is not None:
                self._seen_stamps[str(dest.resolve())] = stamp
        self.track(dest)

    def track(self, path: Path) -> None:
        try:
            path = path.resolve()
            if path.parent != self._resolved_watch_dir:
                return
            if not _is_supported(path):
                return
            stat = path.stat()
        except OSError:
            return
        key = str(path)
        with self._lock:
            if self._seen_stamps.get(key) == _stamp(stat):
                return
            self._seen_stamps.pop(key, None)
            old = self._pending.get(key)
            if old is None or old[0] != stat.st_size:
                self._pending[key] = (stat.st_size, time.time())
        if old is None:
            self.on_event("detected", {"path": str(path), "size": stat.st_size, "message": f"Detected {path.name}"})

    def rescan(self) -> None:
        """Pick up files whose watchdog events were lost.

        Undersized files stay silent until a later pass: they may still be
        growing, and an ``ignored`` event here would repeat on every pass.
        """
        root = self._resolved_watch_dir
        if root is None:
            return
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        with self._lock:
            handled = self._seen_stamps.keys() | self._pending.keys()
        min_bytes = self.settings.min_file_size_kb * 1024
        for entry in entries:
            try:
                if (not _is_supported(entry) or str(entry.resolve()) in handled
                        or entry.stat().st_size < min_bytes):
                    continue
            except OSError:
                continue
            self.track(entry)

    def tick_once(self, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            snapshot = list(self._pending.items())
        for key, (last_size, last_changed) in snapshot:
            path = Path(key)
            try:
                stat = path.stat()
            except OSError:
                with self._lock:
                    self._pending.pop(key, None)
                continue
            current = stat.st_size
            if current != last_size:
                with self._lock:
                    self._pending[key] = (current, now)
                continue
            if now - last_changed < self.settings.stable_seconds:
                continue
            try:
                with open(path, "r+b"):
                    pass
            except FileNotFoundError:
                with self._lock:
                    self._pending.pop(key, None)
                continue
            except OSError as exc:
                if getattr(exc, "winerror", None) in {None, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION}:
                    with self._lock:
                        self._pending[key] = (current, now)
                    continue
                # Another error, such as a read-only archive, still allows transcription if it is readable.
                try:
                    with open(path, "rb"):
                        pass
                except OSError:
                    with self._lock:
                        self._pending[key] = (current, now)
                    continue
            with self._lock:
                self._pending.pop(key, None)
            if current >= self.settings.min_file_size_kb * 1024:
                try:
                    self.on_ready(path)
                except Exception:
                    with self._lock:
                        self._pending[key] = (current, now)
                    raise
                with self._lock:
                    self._seen_stamps[key] = _stamp(stat)
            else:
                self.on_event("ignored", {"path": str(path), "message": "File is below minimum size"})

    def _tick_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.tick_once()
                if time.monotonic() - self._last_rescan >= self.RESCAN_INTERVAL_SECONDS:
                    self._last_rescan = time.monotonic()
                    self.rescan()
            except Exception as exc:
                self.on_event("watch_error", {"message": f"Directory scan failed: {exc}"})
