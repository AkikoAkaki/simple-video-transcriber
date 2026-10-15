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
from paths import audio_cache_lock, fingerprint, partial_path, source_identity, wav_cache_path
from protocol import JobOptions


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNING = "completed_with_warning"
    FAILED = "failed"
    CANCELLED = "cancelled"


# A job that may still run, or is running, owns its audio cache.
ACTIVE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})
# The worker reports these names as its terminal events too.
TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNING, JobStatus.FAILED, JobStatus.CANCELLED})
RETRYABLE = frozenset({JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.COMPLETED_WITH_WARNING})
MAX_RECOVERIES = 5


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_options(row: dict, base: JobOptions | None = None) -> JobOptions:
    return JobOptions.parse(json.loads(row["options_json"] or "{}"), base)


class CacheCleanup(NamedTuple):
    deleted_count: int
    reclaimed_bytes: int


class JobStore:
    def __init__(self, path: Path | None = None):
        self.path = path or appdata.JOBS_DB
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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "options_json" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN options_json TEXT DEFAULT '{}'")

    def create_if_new(self, path: Path, options: JobOptions = JobOptions(), *,
                      from_watcher: bool = False) -> dict | None:
        try:
            source = source_identity(path)
        except OSError:
            return None
        stamp = now()
        options_json = json.dumps(asdict(options), ensure_ascii=False)
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE source_key = ?", (source.key,)).fetchone()
            if existing:
                if existing["status"] in ACTIVE:
                    return None
                if from_watcher:
                    # A stray filesystem event must never re-queue hours of GPU
                    # work and wipe the result pointer; re-runs are retries or imports.
                    return None
                job_id = existing["job_id"]
                conn.execute(
                    "UPDATE jobs SET status=?, stage=?, progress=0.0, message='Queued', "
                    "output_path='', error='', started_at='', finished_at='', updated_at=?, options_json=?, "
                    "retry_count=0 WHERE job_id = ?",
                    (JobStatus.QUEUED, JobStatus.QUEUED, stamp, options_json, job_id)
                )
                row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                return dict(row) if row else None

            job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            row = {
                "job_id": job_id,
                "source_path": source.path,
                "source_key": source.key,
                "source_size": source.size,
                "source_mtime_ns": source.mtime_ns,
                "status": JobStatus.QUEUED,
                "stage": JobStatus.QUEUED,
                "progress": 0.0,
                "message": "Queued",
                "output_path": "",
                "error": "",
                "created_at": stamp,
                "updated_at": stamp,
                "started_at": "",
                "finished_at": "",
                "retry_count": 0,
                "options_json": options_json,
            }
            try:
                conn.execute(
                    "INSERT INTO jobs ({}) VALUES ({})".format(
                        ",".join(row), ",".join("?" for _ in row)
                    ),
                    tuple(row.values()),
                )
            except sqlite3.IntegrityError:
                return None
        return row

    def update(self, job_id: str, **changes) -> dict | None:
        changes["updated_at"] = now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", (*changes.values(), job_id))
        return self.get(job_id)

    def start(self, job_id: str) -> bool:
        return self._transition(job_id, {JobStatus.QUEUED}, status=JobStatus.RUNNING, stage="starting",
                                message="Starting worker", started_at=now())

    def request_cancel(self, job_id: str, message: str) -> bool:
        return self._transition(job_id, {JobStatus.QUEUED, JobStatus.RUNNING},
                                status=JobStatus.CANCEL_REQUESTED, stage="cancelling", message=message)

    def cancel(self, job_id: str, message: str = "Cancelled") -> bool:
        return self._transition(job_id, {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED},
                                status=JobStatus.CANCELLED, message=message, finished_at=now())

    def finish(self, job_id: str, status: JobStatus, message: str, **changes) -> JobStatus | None:
        """Record a job's outcome and return the status it ended with; a cancelled job stays cancelled."""
        if self._transition(job_id, {JobStatus.QUEUED, JobStatus.RUNNING},
                            status=status, message=message, finished_at=now(), **changes):
            return status
        return JobStatus.CANCELLED if self.cancel(job_id) else None

    def record_progress(self, job_id: str, **changes) -> bool:
        return self._transition(job_id, {JobStatus.RUNNING}, **changes)

    def _transition(self, job_id: str, expected: Iterable[JobStatus], **changes) -> bool:
        statuses = tuple(expected)
        changes["updated_at"] = now()
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

    def recent(self, limit: int = 20) -> list[dict]:
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

    def recover_interrupted(self) -> int:
        stamp = now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, message='Cancelled during previous shutdown', "
                "updated_at=?, finished_at=? WHERE status=?",
                (JobStatus.CANCELLED, stamp, stamp, JobStatus.CANCEL_REQUESTED),
            )
            conn.execute(
                "UPDATE jobs SET status=?, message=?, updated_at=?, finished_at=? "
                "WHERE status=? AND retry_count >= ?",
                (JobStatus.FAILED, f"Failed after {MAX_RECOVERIES} recovery retries",
                 stamp, stamp, JobStatus.RUNNING, MAX_RECOVERIES),
            )
            cur = conn.execute(
                "UPDATE jobs SET status=?, stage=?, message='Recovered after restart', "
                "updated_at=?, retry_count=retry_count+1 WHERE status=? AND retry_count < ?",
                (JobStatus.QUEUED, JobStatus.QUEUED, stamp, JobStatus.RUNNING, MAX_RECOVERIES),
            )
        return cur.rowcount


def cache_size_bytes(cache_dir: Path) -> int:
    total = 0
    with suppress(OSError):
        for entry in cache_dir.iterdir():
            with suppress(OSError):
                if entry.is_file():
                    total += entry.stat().st_size
    return total


def clear_audio_cache(cache_dir: Path, store: JobStore) -> CacheCleanup:
    """Delete the WAVs, finished or partial, of jobs that reached a terminal status.

    Only files named after a terminal job's unique source key are candidates,
    so active jobs' audio, JSON caches, transcripts and untracked WAVs (CLI
    runs, older unfingerprinted names) are never touched.
    """
    guarding_wav: dict[Path, Path] = {}
    for source_path, source_key, status in store.cache_owners():
        if status in TERMINAL:
            wav = wav_cache_path(cache_dir, Path(source_path).stem, fingerprint(source_key))
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
