"""Keep only UI actions that can lose recordings or delete data."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import tray_app


@pytest.fixture
def dashboard():
    qt = QApplication.instance() or QApplication([])
    svc = SimpleNamespace(
        settings=SimpleNamespace(model="large-v3-turbo", device="auto", watch_dir="", watcher_enabled=False),
        active_job=lambda: None, cancel_active=MagicMock(), recent_jobs=lambda limit=20: [],
        get_job=lambda job_id: None, get_token=lambda: "test-token", cache_size_bytes=lambda: 0,
        clear_audio_cache=MagicMock(return_value=SimpleNamespace(deleted_count=1, reclaimed_bytes=1024)),
        add_file=MagicMock(return_value={"job_id": "queued"}),
    )
    view = tray_app.Dashboard(SimpleNamespace(service=svc))
    yield view, svc
    view.hide()
    view.deleteLater()
    qt.processEvents()


def test_batch_import_does_not_drop_recordings(dashboard, tmp_path, monkeypatch):
    view, svc = dashboard
    paths = [tmp_path / "first.m4a", tmp_path / "second.mp4", tmp_path / "ignore.txt"]
    for path in paths:
        path.write_bytes(b"recording")
    monkeypatch.setattr(tray_app.QFileDialog, "getOpenFileNames",
                        lambda *args: ([str(p) for p in paths + [paths[0]]], ""))
    view._browse_file()
    view.manual_title.setText("Course")
    view._transcribe_manual_file()
    calls = svc.add_file.call_args_list
    assert [call.args[0] for call in calls] == paths[:2]
    assert [call.args[1]["title"] for call in calls] == ["Course - first", "Course - second"]


def test_declining_cache_deletion_keeps_files(dashboard, monkeypatch):
    view, svc = dashboard
    monkeypatch.setattr(tray_app.QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    view._clear_audio_cache()
    svc.clear_audio_cache.assert_not_called()
    monkeypatch.setattr(tray_app.QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    view._clear_audio_cache()
    svc.clear_audio_cache.assert_called_once()
