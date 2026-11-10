"""Per-user application data: settings, secrets, and file locations.

Every path derives from ``APP_DATA_DIR`` when it is used, so redirecting that one
attribute moves all of them. The folder name and the ``api_port`` and ``api_token`` file names are also
read by obsidian-plugin/src/api.ts.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import config
from paths import write_atomic


APP_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "SimpleVideoTranscriber"


def jobs_db() -> Path:
    return APP_DATA_DIR / "jobs.sqlite3"


def log_dir() -> Path:
    return APP_DATA_DIR / "logs"


def models_dir() -> Path:
    return APP_DATA_DIR / "models"


def _settings_file() -> Path:
    return APP_DATA_DIR / "settings.json"


@dataclass
class AppSettings:
    watch_dir: str = str(config.WATCH_DIR)
    watcher_enabled: bool = True

    @classmethod
    def load(cls) -> AppSettings:
        try:
            data = json.loads(_settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    def save(self) -> None:
        write_atomic(_settings_file(), json.dumps(asdict(self), ensure_ascii=False, indent=2))


class TokenStore:
    """The HuggingFace token: the OS keyring, a file when keyring has no backend, then ``HF_TOKEN``."""

    SERVICE = "simple-video-transcriber"
    USER = "huggingface"

    def __init__(self):
        self._cached_token = None

    @property
    def _file(self) -> Path:
        return APP_DATA_DIR / "hf_token.txt"

    def get(self) -> str:
        if self._cached_token is not None:
            return self._cached_token
        value = ""
        try:
            import keyring
            value = (keyring.get_password(self.SERVICE, self.USER) or "").strip()
        except Exception:
            pass
        if not value:
            try:
                value = self._file.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
        value = value or os.environ.get("HF_TOKEN", "").strip()
        self._cached_token = value
        return value

    def set(self, token: str) -> None:
        token = token.strip()
        self._cached_token = token
        self._file.parent.mkdir(parents=True, exist_ok=True)
        try:
            import keyring
            if token:
                keyring.set_password(self.SERVICE, self.USER, token)
            else:
                keyring.delete_password(self.SERVICE, self.USER)
            self._file.unlink(missing_ok=True)
            return
        except Exception:
            pass
        if token:
            self._file.write_text(token, encoding="utf-8")
        else:
            self._file.unlink(missing_ok=True)


def load_api_secret(create: bool = False) -> str:
    """The secret every local API request must carry; a web page cannot read this file."""
    path = APP_DATA_DIR / "api_token"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if not value and create:
        value = secrets.token_urlsafe(32)
        write_atomic(path, value)
    return value


def publish_api_port(port: int) -> None:
    """Tell clients outside this codebase, such as the Obsidian plugin, where the running tray listens."""
    write_atomic(APP_DATA_DIR / "api_port", str(port))
