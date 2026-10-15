"""Per-user application data: settings, the HuggingFace token, and file locations.

Paths are module attributes read at call time, never bound in default
arguments, so tests and alternate installations can redirect them after import.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import config
from protocol import JobOptions


APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "SimpleVideoTranscriber"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
JOBS_DB = APP_DATA_DIR / "jobs.sqlite3"
LOG_DIR = APP_DATA_DIR / "logs"
TOKEN_FILE = APP_DATA_DIR / "hf_token.txt"


_JOB_FIELDS = {field.name for field in fields(JobOptions)}


@dataclass
class AppSettings:
    watch_dir: str = str(config.WATCH_DIR)
    transcript_dir: str = str(config.TRANSCRIPT_DIR)
    model: str = config.WHISPER_MODEL
    device: str = config.DEVICE
    language: str | None = config.LANGUAGE
    max_speakers: int | None = config.MAX_SPEAKERS
    num_speakers: int | None = config.NUM_SPEAKERS
    hotwords: str = config.HOTWORDS
    stable_seconds: int = 15
    min_file_size_kb: int = config.MIN_FILE_SIZE_KB
    watcher_enabled: bool = True

    def __post_init__(self) -> None:
        self.model = config.normalize_whisper_model(self.model)

    @classmethod
    def load(cls, path: Path | None = None) -> AppSettings:
        """Read saved settings; a hand-edited value a job would reject falls back to its default."""
        path = path or SETTINGS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        values = {}
        for name in cls.__dataclass_fields__.keys() & data.keys():
            if name in _JOB_FIELDS:
                try:
                    JobOptions.parse({name: data[name]})
                except (TypeError, ValueError):
                    continue
            values[name] = data[name]
        return cls(**values)

    def save(self, path: Path | None = None) -> None:
        path = path or SETTINGS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def job_options(self) -> JobOptions:
        return JobOptions.parse(asdict(self))


class TokenStore:
    """The HuggingFace token, in the OS keyring or, when keyring has no backend, a file."""

    SERVICE = "simple-video-transcriber"
    USER = "huggingface"

    def __init__(self, file_path: Path | None = None):
        self.file_path = file_path or TOKEN_FILE
        self._cached_token = None

    def get(self) -> str:
        if self._cached_token is not None:
            return self._cached_token
        value = ""
        try:
            import keyring
            value = keyring.get_password(self.SERVICE, self.USER)
            if value:
                value = value.strip()
        except Exception:
            pass
        if not value:
            try:
                value = self.file_path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
        self._cached_token = value
        return value

    def set(self, token: str) -> None:
        token = token.strip()
        self._cached_token = token
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import keyring
            if token:
                keyring.set_password(self.SERVICE, self.USER, token)
            else:
                keyring.delete_password(self.SERVICE, self.USER)
            if self.file_path.exists():
                self.file_path.unlink()
            return
        except Exception:
            pass
        if token:
            self.file_path.write_text(token, encoding="utf-8")
        elif self.file_path.exists():
            self.file_path.unlink()
