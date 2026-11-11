#!/usr/bin/env python3
"""Tray controller: menu, notifications, and the local API. The task list lives in the Obsidian plugin.

The tray process stays light: it imports no Torch or ML package. The heavy work runs in
``transcribe.py`` child processes. It exits by itself once idle; the Explorer menu and the plugin start it again.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QLineEdit, QMenu, QMessageBox, QSystemTrayIcon

import appdata
import config
from background.api import ApiServer
from background.service import BackgroundService
from protocol import overall_progress

BLUE, GREEN, RED = "#3b82f6", "#22c55e", "#ef4444"
# Opens the task panel of the Obsidian plugin, which registers this URI.
PANEL_URI = "obsidian://meeting-transcriber"
MEDIA_FILTER = "音视频 (" + " ".join(f"*{ext}" for ext in sorted(config.MEDIA_EXTENSIONS)) + ")"
_ICON_FOR_EVENT = {"queued": BLUE, "started": BLUE, "stage": BLUE, "completed": GREEN, "failed": RED}


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _make_icon(color: str = BLUE) -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128):
        pixmap = QPixmap(s, s)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        outer = round(s * 3 / 32)
        painter.drawEllipse(outer, outer, s - 2 * outer, s - 2 * outer)
        painter.setBrush(QColor("#ffffff"))
        inner = round(s * 11 / 32)
        painter.drawEllipse(inner, inner, s - 2 * inner, s - 2 * inner)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _open(target: str) -> None:
    url = QUrl(target) if "://" in target else QUrl.fromLocalFile(target)
    QDesktopServices.openUrl(url)


def _open_panel() -> None:
    _open(PANEL_URI)


class TrayApp(QObject):
    event_received = Signal(str, object)

    def __init__(self, qt_app: QApplication, service=None):
        super().__init__()
        self.qt_app = qt_app
        self.lock = QLockFile(str(appdata.APP_DATA_DIR / "app.lock"))
        self.lock.setStaleLockTime(30000)
        self.service = service or BackgroundService(on_event=self.event_received.emit)
        self.api: ApiServer | None = None
        self.tray = QSystemTrayIcon(_make_icon(), qt_app)
        # A notification can only be clicked as a whole, so remember what the latest one offers.
        self._on_message_click = _open_panel
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._fill_menu)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(lambda: self._on_message_click())
        self.event_received.connect(self._handle_event)
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(5000)
        self.idle_timer.timeout.connect(self._exit_when_idle)

    def acquire(self) -> bool:
        appdata.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return self.lock.tryLock(100)

    def start(self) -> None:
        self.tray.show()
        self.service.start()
        self.api = ApiServer(self.service)
        self.api.start()
        self._update_tooltip()
        self.idle_timer.start()

    def _exit_when_idle(self) -> None:
        if self.service.idle_seconds() >= config.IDLE_EXIT_SECONDS:
            self.quit()

    def _notify(self, title: str, message: str, on_click, icon=QSystemTrayIcon.MessageIcon.Information) -> None:
        self._on_message_click = on_click
        self.tray.showMessage(title, message, icon, 6000)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            _open_panel()

    def _fill_menu(self) -> None:
        """Rebuilt on every open, so it always shows the current task."""
        menu, service = self.menu, self.service
        menu.clear()
        active = service.active_job()
        if active:
            overall = overall_progress(active["stage"], active["progress"])
            progress = f"（{overall:.0%}）" if overall else ""
            menu.addAction(f"正在转录：{Path(active['source_path']).name}{progress}").setEnabled(False)
        else:
            menu.addAction("空闲").setEnabled(False)
        menu.addAction("打开任务面板（Obsidian）", _open_panel)
        menu.addAction("转录文件…", self._choose_files)
        menu.addAction("取消当前任务", service.cancel_active).setEnabled(bool(active))
        menu.addSeparator()
        menu.addAction("设置 HuggingFace Token…", self._set_token)
        menu.addAction("清理音频缓存…", self._clear_audio_cache)
        menu.addSeparator()
        menu.addAction("打开转录文件夹", lambda: _open(str(config.TRANSCRIPT_DIR)))
        menu.addAction("打开日志文件夹", lambda: _open(str(appdata.log_dir())))
        menu.addAction("退出", self.quit)

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(None, "选择要转录的录音", str(Path.home() / "Downloads"), MEDIA_FILTER)
        if paths:
            rows = self.service.add_files(paths)
            self._notify("已加入队列", f"{len(rows)} 个文件等待转录", _open_panel)

    def _set_token(self) -> None:
        token, ok = QInputDialog.getText(None, "HuggingFace Token", "说话人分离需要 HuggingFace Token：",
                                         QLineEdit.EchoMode.Password)
        if ok and token.strip():
            self.service.set_token(token.strip())

    def _clear_audio_cache(self) -> None:
        size = _format_size(self.service.cache_size_bytes())
        reply = QMessageBox.question(
            None, "清理音频缓存",
            f"音频缓存共 {size}。只删除已结束任务的 WAV，识别缓存和转录稿不受影响。确定清理吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            result = self.service.clear_audio_cache()
            self._notify("音频缓存已清理", f"删除 {result.deleted_count} 个文件，释放 {_format_size(result.reclaimed_bytes)}",
                         lambda: None)

    def _handle_event(self, event: str, payload: dict) -> None:
        if event in _ICON_FOR_EVENT:
            self.tray.setIcon(_make_icon(_ICON_FOR_EVENT[event]))
        job = self.service.get_job(payload["job_id"]) if payload.get("job_id") else None
        name = Path((job or {}).get("source_path") or payload.get("path") or "").name
        if event == "recording_ready":
            path = payload["path"]
            self._notify("发现新录屏", f"{name}\n点击开始转录", lambda: self.service.accept_recording(path))
        elif event == "completed":
            self._notify("转录完成", f"{name or payload.get('message', '')}\n点击打开任务面板", _open_panel)
        elif event == "failed":
            self._notify("转录失败", payload.get("error") or payload.get("message", ""), _open_panel,
                         QSystemTrayIcon.MessageIcon.Critical)
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        active = self.service.active_job()
        state = f"正在转录 {Path(active['source_path']).name}" if active else "空闲，稍后自动退出"
        self.tray.setToolTip(f"Meeting Transcriber · {state}")

    def quit(self) -> None:
        self.idle_timer.stop()
        if self.service.active_job():
            answer = QMessageBox.question(None, "任务正在运行", "停止当前任务并退出吗？",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.idle_timer.start()
                return
        if self.api:
            self.api.stop()
        self.service.stop()
        self.tray.hide()
        self.lock.unlock()
        self.qt_app.quit()


def main() -> int:
    user32 = ctypes.windll.user32
    desktop = user32.OpenDesktopW("Default", 0, False, 0x01FF)
    if desktop:
        user32.SetThreadDesktop(desktop)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = TrayApp(app)
    if not controller.acquire():
        # The Explorer menu may start several trays at once; only a manual start needs to hear about it.
        if "--background" not in sys.argv:
            QMessageBox.information(None, "已在运行", "Meeting Transcriber 已经在系统托盘中运行。")
        return 0
    controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
