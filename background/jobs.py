"""Durable job state and the audio cache it owns."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, NamedTuple

import appdata
import config
from paths import audio_cache_lock, fingerprint, partial_path, source_identity, wav_cache_path
from protocol import JobOptions


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ACTIVE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})
_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
RETRYABLE = frozenset({JobStatus.FAILED, JobStatus.CANCELLED})
_MAX_RECOVERIES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_options(row: dict) -> JobOptions:
    return JobOptions.parse(json.loads(row["options_json"] or "{}"))


class CacheCleanup(NamedTuple):
    deleted_count: int
    reclaimed_bytes: int


class JobStore:
    def __init__(self):
        self.path = appdata.jobs_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_key TEXT NOT NULL UNIQUE,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    progress REAL,
                    message TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    options_json TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
                """
            )

    def create_if_new(self, path: Path, options: JobOptions = JobOptions()) -> dict | None:
        """Queue this version of a recording, re-running it if finished; None if it is already active."""
        try:
            source = source_identity(path)
        except OSError:
            return None
        stamp = _now()
        fresh = {"status": JobStatus.QUEUED, "stage": JobStatus.QUEUED, "progress": 0.0, "message": "Queued",
                 "output_path": "", "error": "", "updated_at": stamp, "started_at": "", "finished_at": "",
                 "retry_count": 0, "options_json": json.dumps(asdict(options), ensure_ascii=False)}
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE source_key = ?", (source.key,)).fetchone()
            if existing:
                if existing["status"] in _ACTIVE:
                    return None
                row = {**dict(existing), **fresh}
                conn.execute(f"UPDATE jobs SET {', '.join(f'{key} = ?' for key in fresh)} WHERE job_id = ?",
                             (*fresh.values(), row["job_id"]))
                return row
            row = {"job_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                   "source_path": source.path, "source_key": source.key, "source_size": source.size,
                   "source_mtime_ns": source.mtime_ns, "created_at": stamp, **fresh}
            try:
                conn.execute(f"INSERT INTO jobs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                             tuple(row.values()))
            except sqlite3.IntegrityError:
                return None
        return row

    def start(self, job_id: str) -> bool:
        return self._transition(job_id, {JobStatus.QUEUED}, status=JobStatus.RUNNING, stage="starting",
                                message="Starting worker", started_at=_now())

    def request_cancel(self, job_id: str, message: str) -> bool:
        return self._transition(job_id, {JobStatus.QUEUED, JobStatus.RUNNING},
                                status=JobStatus.CANCEL_REQUESTED, message=message)

    def cancel(self, job_id: str, message: str = "Cancelled") -> bool:
        return self._transition(job_id, {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED},
                                status=JobStatus.CANCELLED, message=message, finished_at=_now())

    def finish(self, job_id: str, status: JobStatus, message: str, **changes) -> JobStatus | None:
        """Record a job's outcome and return the status it ended with; a cancelled job stays cancelled."""
        if self._transition(job_id, {JobStatus.QUEUED, JobStatus.RUNNING},
                            status=status, message=message, finished_at=_now(), **changes):
            return status
        return JobStatus.CANCELLED if self.cancel(job_id) else None

    def record_progress(self, job_id: str, **changes) -> bool:
        return self._transition(job_id, {JobStatus.RUNNING}, **changes)

    def _transition(self, job_id: str, expected: Iterable[JobStatus], **changes) -> bool:
        statuses = tuple(expected)
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ? AND status IN ({placeholders})",
                (*changes.values(), job_id, *statuses),
            )
        return cur.rowcount == 1

    def get(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def knows(self, path: Path) -> bool:
        """Whether this version of the recording already has a task, finished or not."""
        try:
            key = source_identity(path).key
        except OSError:
            return False
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE source_key = ?", (key,)).fetchone() is not None

    def recent(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def queued(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at, job_id", (JobStatus.QUEUED,)
            ).fetchall()
        return [dict(row) for row in rows]

    def cache_owners(self) -> list[tuple[str, str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT source_path, source_key, status FROM jobs").fetchall()
        return [tuple(row) for row in rows]

    def recover_interrupted(self) -> None:
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, message='Cancelled during previous shutdown', "
                "updated_at=?, finished_at=? WHERE status=?",
                (JobStatus.CANCELLED, stamp, stamp, JobStatus.CANCEL_REQUESTED),
            )
            conn.execute(
                "UPDATE jobs SET status=?, message=?, updated_at=?, finished_at=? "
                "WHERE status=? AND retry_count >= ?",
                (JobStatus.FAILED, f"Failed after {_MAX_RECOVERIES} recovery retries",
                 stamp, stamp, JobStatus.RUNNING, _MAX_RECOVERIES),
            )
            conn.execute(
                "UPDATE jobs SET status=?, stage=?, message='Recovered after restart', "
                "updated_at=?, retry_count=retry_count+1 WHERE status=? AND retry_count < ?",
                (JobStatus.QUEUED, JobStatus.QUEUED, stamp, JobStatus.RUNNING, _MAX_RECOVERIES),
            )


def cache_size_bytes() -> int:
    total = 0
    with suppress(OSError):
        for entry in config.CACHE_DIR.iterdir():
            with suppress(OSError):
                if entry.is_file():
                    total += entry.stat().st_size
    return total


def clear_audio_cache(store: JobStore) -> CacheCleanup:
    """Delete the WAVs, finished or partial, of jobs that reached a terminal status.

    Only files named after a terminal job's unique source key are candidates,
    so active jobs' audio, JSON caches, transcripts and untracked WAVs (CLI
    runs, older unfingerprinted names) are never touched.
    """
    guarding_wav: dict[Path, Path] = {}
    for source_path, source_key, status in store.cache_owners():
        if status in _TERMINAL:
            wav = wav_cache_path(Path(source_path).stem, fingerprint(source_key))
            guarding_wav.update({wav: wav, partial_path(wav): wav})

    deleted_count = 0
    reclaimed_bytes = 0
    for item, wav in guarding_wav.items():
        if not item.is_file():
            continue
        try:
            with audio_cache_lock(wav):
                size = item.stat().st_size
                item.unlink()
        except OSError:
            continue
        deleted_count += 1
        reclaimed_bytes += size
    return CacheCleanup(deleted_count, reclaimed_bytes)
