"""Where each source recording's artifacts live, and how they are written safely."""

from __future__ import annotations

import hashlib
import msvcrt
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import config


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
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("Audio cache is in use by another task") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class SourceIdentity(NamedTuple):
    """One version of a source file.

    The path alone is not enough: OBS or a manual workflow can replace a file
    at the same path.
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


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in config.MEDIA_EXTENSIONS and path.is_file()


def wav_cache_path(source_stem: str, source_fp: str) -> Path:
    """The 16 kHz WAV a job converts its source to; its lock guards the partial file too."""
    return config.CACHE_DIR / f"{source_stem}_{source_fp}_16k.wav"


def partial_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.part{path.suffix}")


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` in one step, so a failed write leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.stem}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def job_paths(input_path: Path, output_dir: Path, source_fp: str, title: str = "") -> JobPaths:
    stem = input_path.stem
    return JobPaths(
        wav=wav_cache_path(stem, source_fp),
        whisper_json=config.CACHE_DIR / f"_{stem}_{source_fp}_whisper.json",
        diarize_json=config.CACHE_DIR / f"_{stem}_{source_fp}_diarize.json",
        output_md=transcript_path(input_path, output_dir, source_fp, title),
    )
