"""Keep the loss-prevention suite small and isolate application state."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    files = [Path(__file__), *(ROOT / "tests").rglob("*.py")]
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    if len(items) > 24 or lines > 600:
        raise pytest.UsageError(
            f"Test budget exceeded: {len(items)}/24 cases, {lines}/600 lines. "
            "Remove redundant coverage; budget increases require explicit user approval (AGENTS.md)."
        )


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    import config
    import service

    for name, leaf in (("APP_DATA_DIR", "appdata"), ("SETTINGS_FILE", "settings.json"),
                       ("JOBS_DB", "jobs.sqlite3"), ("TOKEN_FILE", "token.txt"), ("LOG_DIR", "logs")):
        monkeypatch.setattr(service, name, tmp_path / leaf)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "HF_TOKEN", "")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(service.TokenStore, "get", lambda self: "test-token")
