"""Keep only tray actions that can delete data."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication, QMessageBox

import tray_app


def test_declining_cache_deletion_keeps_files(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    svc = SimpleNamespace(cache_size_bytes=lambda: 1024, clear_audio_cache=MagicMock(
        return_value=SimpleNamespace(deleted_count=1, reclaimed_bytes=1024)))
    tray = tray_app.TrayApp(qt, service=svc)
    monkeypatch.setattr(tray_app.QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    tray._clear_audio_cache()
    svc.clear_audio_cache.assert_not_called()
    monkeypatch.setattr(tray_app.QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    tray._clear_audio_cache()
    svc.clear_audio_cache.assert_called_once()
