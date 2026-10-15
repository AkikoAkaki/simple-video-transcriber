"""Lightweight, dependency-free paths for source-specific artifacts."""

from __future__ import annotations

import hashlib
import re
import os
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple


@contextmanager
def audio_cache_lock(wav_path: Path):
    """Exclusively occupy an audio cache; fail immediately if another process owns it."""
    lock_path = wav_path.with_name(f".{wav_path.stem}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError("Audio cache is in use by another task") from exc
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SourceIdentity(NamedTuple):
    """One version of a source file.

    The path alone is not enough: OBS or a manual workflow can replace a file
    at the same path. Including size and mtime prevents stale ML caches from
    being reused for the new contents while preserving cache hits for an
    unchanged file.
    """

    path: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return f"{self.path}|{self.size}|{self.mtime_ns}"


def source_identity(input_path: Path) -> SourceIdentity:
    input_path = Path(input_path).resolve()
    stat = input_path.stat()
    return SourceIdentity(str(input_path), stat.st_size, stat.st_mtime_ns)


def fingerprint(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12]


def source_fingerprint(input_path: Path) -> str:
    return fingerprint(source_identity(input_path).key)


def wav_cache_path(cache_dir: Path, source_stem: str, source_fp: str) -> Path:
    """The 16 kHz WAV a job converts its source to; its lock guards the partial file too."""
    return Path(cache_dir) / f"{source_stem}_{source_fp}_16k.wav"


def partial_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.part{path.suffix}")


class SourceChangedError(ValueError):
    def __init__(self) -> None:
        super().__init__("The recording at this path has changed. Import it as a new task.")


def transcript_path(input_path: Path, output_dir: Path, source_fp: str, title: str = "") -> Path:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")[:120] if title else input_path.stem
    return output_dir / f"{stem or input_path.stem}_{source_fp}.md"


class JobPaths(NamedTuple):
    wav: Path
    whisper_json: Path
    diarize_json: Path
    output_md: Path


def job_paths(input_path: Path, cache_dir: Path, output_dir: Path, source_fp: str, title: str = "") -> JobPaths:
    stem = input_path.stem
    return JobPaths(
        wav=wav_cache_path(cache_dir, stem, source_fp),
        whisper_json=cache_dir / f"_{stem}_{source_fp}_whisper.json",
        diarize_json=cache_dir / f"_{stem}_{source_fp}_diarize.json",
        output_md=transcript_path(input_path, output_dir, source_fp, title),
    )
