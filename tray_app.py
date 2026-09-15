#!/usr/bin/env python3
"""Always-on tray controller and on-demand dashboard.

The tray process is intentionally light: it does not import Torch or any ML
package.  The heavy work remains in ``transcribe.py`` child processes.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from service import APP_DATA_DIR, BackgroundService
import config


try:
    from PySide6.QtCore import QObject, QLockFile, Qt, QUrl, Signal
    from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap, QTextCursor, QTextCharFormat
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QFileDialog,
        QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
        QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
        QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QStyle, QSystemTrayIcon,
        QVBoxLayout, QWidget,
    )
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


if QT_AVAILABLE:

    class Bridge(QObject):
        event_received = Signal(str, object)


    def _make_icon(color: str = "#3b82f6") -> QIcon:
        icon = QIcon()
        for s in (16, 24, 32, 48, 64, 128):
            pixmap = QPixmap(s, s)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            outer_margin = round(s * (3 / 32))
            outer_size = s - 2 * outer_margin
            painter.drawEllipse(outer_margin, outer_margin, outer_size, outer_size)
            painter.setBrush(QColor("#ffffff"))
            inner_margin = round(s * (11 / 32))
            inner_size = s - 2 * inner_margin
            painter.drawEllipse(inner_margin, inner_margin, inner_size, inner_size)
            painter.end()
            icon.addPixmap(pixmap)
        return icon


    def _card(title: str, collapsible: bool = False) -> tuple[QGroupBox, QVBoxLayout]:
        group = QGroupBox("" if collapsible else title)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        if collapsible:
            group.setObjectName("collapsibleSection")
            toggle = QPushButton(title)
            toggle.setIcon(group.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
            toggle.setCheckable(True)
            toggle.setObjectName("sectionToggle")
            layout.addWidget(toggle)
            content = QWidget()
            inner = QVBoxLayout(content)
            inner.setContentsMargins(0, 4, 0, 0)
            content.setVisible(False)
            toggle.toggled.connect(content.setVisible)
            toggle.toggled.connect(lambda expanded: toggle.setIcon(group.style().standardIcon(
                QStyle.StandardPixmap.SP_ArrowDown if expanded else QStyle.StandardPixmap.SP_ArrowRight)))
            layout.addWidget(content)
            return group, inner
        return group, layout


    class Dashboard(QMainWindow):
        def __init__(self, app: "TrayApp"):
            super().__init__()
            self.app = app
            self._selected_manual_files = []
            self.setWindowTitle("Simple Video Transcriber")
            self.setMinimumSize(640, 620)
            self.resize(820, 980)
            self.setObjectName("window")
            self.setAcceptDrops(True)
            self._build()
            self.refresh()

        def _build(self):
            root = QWidget()
            root_layout = QVBoxLayout(root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            header = QFrame()
            header.setObjectName("header")
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(24, 20, 24, 18)
            title_box = QVBoxLayout()
            title = QLabel("Simple Video Transcriber")
            title.setObjectName("title")
            title_box.addWidget(title)
            header_layout.addLayout(title_box)
            header_layout.addStretch()
            self.status_label = QLabel("Starting…")
            self.status_label.setToolTip("Starting the background service")
            self.status_label.setObjectName("statusPill")
            header_layout.addWidget(self.status_label, alignment=Qt.AlignmentFlag.AlignTop)
            root_layout.addWidget(header)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(16, 12, 16, 16)
            body_layout.setSpacing(8)

            drop, drop_layout = _card("Manual transcription")
            row = QHBoxLayout()
            self.manual_file = QLabel("Drop video/audio files here or browse")
            self.manual_file.setObjectName("dropHint")
            self.manual_file.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row.addWidget(self.manual_file)
            browse = QPushButton("Browse files")
            browse.clicked.connect(self._browse_file)
            row.addWidget(browse)
            drop_layout.addLayout(row)

            self.manual_title = QLineEdit()
            self.manual_title.setPlaceholderText("Optional title; used as a prefix when selecting multiple files")
            title_form = QFormLayout()
            title_form.addRow("Title", self.manual_title)
            drop_layout.addLayout(title_form)
            options, options_layout = _card("Transcription options", collapsible=True)
            options_form = QFormLayout()
            options_form.setSpacing(6)

            self.manual_lang = QComboBox()
            self.manual_lang.addItems(["auto", "zh", "en", "ja"])

            self.manual_pipeline = QComboBox()
            self.manual_pipeline.addItems(["Full pipeline", "Transcribe only", "Re-diarize only"])

            self.manual_speakers = QComboBox()
            self.manual_speakers.addItems(["auto", "2", "3", "4", "5"])

            self.manual_exact_speakers = QComboBox()
            self.manual_exact_speakers.addItems(["auto", "2", "3", "4", "5"])

            self.manual_hotwords = QPlainTextEdit()
            self.manual_hotwords.setPlaceholderText("Optional: names or technical terms, separated by commas")
            self.manual_hotwords.setFixedHeight(54)

            options_form.addRow("Language", self.manual_lang)
            options_form.addRow("Pipeline", self.manual_pipeline)
            options_form.addRow("Max speakers", self.manual_speakers)
            options_form.addRow("Exact speakers", self.manual_exact_speakers)
            options_form.addRow("Names / terms", self.manual_hotwords)
            options_layout.addLayout(options_form)
            drop_layout.addWidget(options)

            action_row = QHBoxLayout()
            self.transcribe_btn = QPushButton("Transcribe")
            self.transcribe_btn.setObjectName("accentButton")
            self.transcribe_btn.clicked.connect(self._transcribe_manual_file)
            action_row.addWidget(self.transcribe_btn)
            action_row.addStretch()
            drop_layout.addLayout(action_row)

            body_layout.addWidget(drop)

            watch, watch_layout = _card("Automatic OBS watcher", collapsible=True)
            path_row = QHBoxLayout()
            self.watch_path = QLabel()
            self.watch_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.watch_path.setObjectName("pathLabel")
            path_row.addWidget(self.watch_path, 1)
            choose_folder = QPushButton("Change")
            choose_folder.clicked.connect(self._choose_watch_folder)
            path_row.addWidget(choose_folder)
            watch_layout.addLayout(path_row)
            control_row = QHBoxLayout()
            self.watch_toggle = QCheckBox("Watch in background")
            self.watch_toggle.setChecked(True)
            self.watch_toggle.toggled.connect(self._toggle_watcher)
            control_row.addWidget(self.watch_toggle)
            self.watch_detail = QLabel()
            self.watch_detail.setObjectName("muted")
            control_row.addWidget(self.watch_detail)
            control_row.addStretch()
            watch_layout.addLayout(control_row)

            current, current_layout = _card("Current task")
            self.current_name = QLabel("No active task")
            self.current_name.setObjectName("currentName")
            current_layout.addWidget(self.current_name)
            self.current_stage = QLabel("The worker is idle.")
            self.current_stage.setObjectName("muted")
            self.current_stage.setWordWrap(True)
            self.current_stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            current_layout.addWidget(self.current_stage)
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
            self.progress.setFixedHeight(6)
            self.progress.hide()
            current_layout.addWidget(self.progress)
            self.preview_label = QLabel("")
            self.preview_label.setObjectName("previewLabel")
            self.preview_label.setWordWrap(True)
            self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            current_layout.addWidget(self.preview_label)
            current_row = QHBoxLayout()
            self.elapsed = QLabel("")
            self.elapsed.setObjectName("muted")
            current_row.addWidget(self.elapsed)
            current_row.addStretch()
            self.cancel_button = QPushButton("Cancel current task")
            self.cancel_button.setObjectName("dangerButton")
            self.cancel_button.clicked.connect(self.app.service.worker.cancel_active)
            self.cancel_button.setVisible(False)
            current_row.addWidget(self.cancel_button)
            current_layout.addLayout(current_row)
            body_layout.addWidget(current)

            recent, recent_layout = _card("Recent tasks")
            self.recent_list = QListWidget()
            self.recent_list.setMinimumHeight(220)
            self.recent_list.setUniformItemSizes(True)
            self.recent_list.itemDoubleClicked.connect(lambda _item: self._open_result())
            recent_layout.addWidget(self.recent_list)
            recent_buttons = QHBoxLayout()
            open_result = QPushButton("Open result")
            open_result.clicked.connect(self._open_result)
            recent_buttons.addWidget(open_result)
            retry = QPushButton("Retry")
            retry.clicked.connect(self._retry_job)
            recent_buttons.addWidget(retry)
            open_transcripts = QPushButton("Open transcripts folder")
            open_transcripts.clicked.connect(self._open_transcripts)
            recent_buttons.addWidget(open_transcripts)
            open_logs = QPushButton("Open logs folder")
            open_logs.clicked.connect(self._open_logs)
            recent_buttons.addWidget(open_logs)
            recent_buttons.addStretch()
            recent_layout.addLayout(recent_buttons)
            body_layout.addWidget(recent, 1)

            settings, settings_layout = _card("Advanced settings", collapsible=True)
            form = QFormLayout()
            self.model_box = QComboBox()
            self.model_box.addItems(list(config.SUPPORTED_MODELS))
            self.model_box.setCurrentText(self.app.service.settings.model)
            self.device_box = QComboBox()
            self.device_box.addItems(["auto", "cuda", "cpu"])
            self.device_box.setCurrentText(self.app.service.settings.device)
            form.addRow("Whisper model", self.model_box)
            form.addRow("Device", self.device_box)
            settings_layout.addLayout(form)
            token_row = QHBoxLayout()
            self.token_edit = QLineEdit()
            self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.token_edit.setPlaceholderText("HuggingFace token (required for speaker diarization)")
            self.token_edit.setText(self.app.service.token_store.get())
            token_row.addWidget(self.token_edit, 1)
            save_token = QPushButton("Save token")
            save_token.clicked.connect(self._save_token)
            token_row.addWidget(save_token)
            settings_layout.addLayout(token_row)
            self.token_status = QLabel()
            self.token_status.setObjectName("muted")
            settings_layout.addWidget(self.token_status)
            save_settings = QPushButton("Save model/device settings")
            save_settings.clicked.connect(self._save_settings)
            settings_layout.addWidget(save_settings)

            cache_row = QHBoxLayout()
            self.cache_size_label = QLabel()
            self.cache_size_label.setObjectName("muted")
            cache_row.addWidget(self.cache_size_label)
            cache_row.addStretch()
            self.clear_cache_btn = QPushButton("Clear Audio Cache")
            self.clear_cache_btn.setObjectName("clearCacheButton")
            self.clear_cache_btn.clicked.connect(self._clear_audio_cache)
            cache_row.addWidget(self.clear_cache_btn)
            settings_layout.addLayout(cache_row)


            logs, logs_layout = _card("Readable activity log")
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            self.log.setMaximumBlockCount(500)
            self.log.setMinimumHeight(230)
            scrollbar = self.log.verticalScrollBar()
            scrollbar.rangeChanged.connect(lambda _minimum, maximum: scrollbar.setValue(maximum))
            logs_layout.addWidget(self.log)
            body_layout.addWidget(logs, 1)
            body_layout.addWidget(watch)
            body_layout.addWidget(settings)
            body_layout.addItem(QSpacerItem(1, 4, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

            scroll.setWidget(body)
            root_layout.addWidget(scroll, 1)
            self.setCentralWidget(root)

        def _browse_file(self):
            extensions = " ".join(f"*{ext}" for ext in sorted(config.WATCH_EXTENSIONS))
            paths, _ = QFileDialog.getOpenFileNames(self, "Select video or audio", "", f"Video / Audio ({extensions})")
            if paths:
                self._select_files(paths)

        def _select_files(self, paths):
            self._selected_manual_files = list(dict.fromkeys(
                Path(path).resolve() for path in paths
                if Path(path).is_file() and Path(path).suffix.lower() in config.WATCH_EXTENSIONS
            ))
            names = [path.name for path in self._selected_manual_files]
            self.manual_file.setText(f"Selected: {names[0]}" if len(names) == 1 else f"Selected {len(names)} files")
            self.manual_file.setToolTip("\n".join(str(path) for path in self._selected_manual_files))

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event):
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self._select_files(paths)
            event.acceptProposedAction()

        def _transcribe_manual_file(self):
            if not self._selected_manual_files:
                QMessageBox.warning(self, "No file selected", "Please select or drop a file first.")
                return
            options = {
                "language": self.manual_lang.currentText(),
                "pipeline": self.manual_pipeline.currentText(),
                "max_speakers": self.manual_speakers.currentText(),
                "num_speakers": self.manual_exact_speakers.currentText(),
                "hotwords": self.manual_hotwords.toPlainText().strip(),
            }
            title = " ".join(self.manual_title.text().split())
            multiple = len(self._selected_manual_files) > 1
            queued = 0
            for path in self._selected_manual_files:
                job_title = f"{title} - {path.stem}" if title and multiple else title
                if self.app.service.add_file(path, {**options, "title": job_title}):
                    queued += 1
            skipped = len(self._selected_manual_files) - queued
            self.manual_file.setText(f"Queued {queued}; skipped {skipped} (already active or unavailable)")
            self._selected_manual_files = []
            self.manual_title.clear()

        def _open_result(self):
            item = self.recent_list.currentItem()
            row = self.app.service.store.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
            if not row or not row.get("output_path"):
                QMessageBox.information(self, "No result", "Select a completed task with a transcript.")
                return
            path = Path(row["output_path"])
            if not path.is_file():
                QMessageBox.information(self, "Result moved", "This transcript was moved, renamed, or deleted. Check your archive.")
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(self, "Cannot open result", "No application could open this Markdown file.")

        def _retry_job(self):
            item = self.recent_list.currentItem()
            if not item:
                return
            try:
                self.app.service.retry_job(item.data(Qt.ItemDataRole.UserRole))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "Cannot retry", str(exc))
            self.refresh()

        def _choose_watch_folder(self):
            path = QFileDialog.getExistingDirectory(self, "Select OBS recording folder", self.app.service.settings.watch_dir)
            if path:
                self.app.service.set_watch_dir(Path(path))
                self.refresh()

        def _toggle_watcher(self, enabled: bool):
            self.app.service.set_watcher_enabled(enabled)
            self.refresh()

        def _save_token(self):
            token = self.token_edit.text().strip()
            self.app.service.token_store.set(token)
            self.token_status.setText("Token saved. It will be passed to the next worker.") if token else self.token_status.setText("Token cleared; speaker diarization requires a token.")

        def _save_settings(self):
            self.app.service.settings.model = self.model_box.currentText()
            self.app.service.settings.device = self.device_box.currentText()
            self.app.service.settings.save()
            self.token_status.setText("Settings saved.")

        def _open_transcripts(self):
            path = Path(self.app.service.settings.transcript_dir)
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        def _open_logs(self):
            APP_DATA_DIR.joinpath("logs").mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DATA_DIR / "logs")))

        def _update_cache_display(self):
            size_str = self.app.service.get_cache_size()
            self.cache_size_label.setText(f"Cache usage: {size_str}")

        def _clear_audio_cache(self):
            reply = QMessageBox.question(
                self,
                "Clear Audio Cache",
                "Are you sure you want to clear temporary audio cache files?\n\n"
                "This will only remove completed/failed .wav files.\n"
                "JSON caches and transcripts will not be affected.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            result = self.app.service.clear_audio_cache()
            count = result.get("deleted_count", 0)
            size = result.get("reclaimed_size", "0 B")
            self.append_log(f"Audio cache cleared: {count} file(s) removed ({size} reclaimed)")
            self._update_cache_display()

        def closeEvent(self, event):
            event.ignore()
            self.hide()
            self.app.tray.showMessage("Still running in background", "The watcher continues in the system tray.", QSystemTrayIcon.MessageIcon.Information, 3000)

        def refresh(self, update_recent: bool = True):
            settings = self.app.service.settings
            self.watch_path.setText(settings.watch_dir)
            self.watch_toggle.blockSignals(True)
            self.watch_toggle.setChecked(settings.watcher_enabled)
            self.watch_toggle.blockSignals(False)
            active = self.app.service.worker.active
            if active and (self.app.service.store.get(active["job_id"]) or active).get("status") in {"completed", "completed_with_warning", "failed", "cancelled"}:
                active = None
            self.elapsed.setVisible(bool(active))
            if active:
                row = self.app.service.store.get(active["job_id"]) or active
                self.current_name.setText(Path(row["source_path"]).name)
                stage = row.get("stage") or "running"
                message = row.get("message") or stage
                progress = row.get("progress")
                self.progress.show()
                if stage == "transcribing" and isinstance(progress, (int, float)):
                    self.current_stage.setText(
                        f"2/4 · Transcription {progress:.0%} · {message}")
                    self.progress.setRange(0, 100)
                    self.progress.setValue(int(max(0, min(1, progress)) * 100))
                elif stage in {"loading_whisper", "loading_diarization", "diarizing", "converting"}:
                    step = "1/4" if stage == "converting" else "2/4" if stage == "loading_whisper" else "3/4"
                    self.current_stage.setText(f"{step} · {message}")
                    self.progress.setRange(0, 0)
                elif isinstance(progress, (int, float)):
                    self.current_stage.setText(f"{stage.title()} · {progress:.0%} · {message}")
                    self.progress.setRange(0, 100)
                    self.progress.setValue(int(max(0, min(1, progress)) * 100))
                else:
                    self.current_stage.setText(f"{stage.replace('_', ' ').title()} · {message}")
                    self.progress.setRange(0, 0)
                started = row.get("started_at") or row.get("updated_at")
                self.elapsed.setText(f"Started {datetime.fromisoformat(started).astimezone():%H:%M:%S}" if started else "")
                self.cancel_button.setVisible(True)
                preview = active.get("preview")
                if preview:
                    self.preview_label.setText(preview)
            else:
                self.current_name.setText("No active task")
                self.current_stage.setText("The worker is idle.")
                self.progress.hide()
                self.elapsed.setText("")
                self.cancel_button.setVisible(False)
                self.preview_label.setText("")
            self.preview_label.setVisible(bool(self.preview_label.text()))
            if update_recent:
                self._update_cache_display()
                self.refresh_recent_list()
            watcher = "Watching" if settings.watcher_enabled else "Paused"
            self.watch_detail.setText(f"{watcher} · new files only · one worker at a time")

        def refresh_recent_list(self):
            selected_job_id = None
            current_item = self.recent_list.currentItem()
            if current_item is not None:
                selected_job_id = current_item.data(Qt.ItemDataRole.UserRole)
            rows = self.app.service.store.recent(20)
            self.recent_list.clear()
            restore_item = None
            for row in rows:
                name = json.loads(row.get("options_json") or "{}").get("title") or Path(row["source_path"]).name
                status = row["status"]
                label = {"completed": "Done", "completed_with_warning": "Warning", "cancel_requested": "Cancelling"}.get(status, status.capitalize())
                stamp = datetime.fromisoformat(row["updated_at"]).astimezone().strftime("%m-%d %H:%M")
                item = QListWidgetItem(f"{stamp}  ·  {label}  ·  {name}")
                item.setData(Qt.ItemDataRole.UserRole, row["job_id"])
                item.setToolTip(f"{name}\n{row.get('error') or row.get('message', '')}")
                if status in {"failed", "completed_with_warning"}:
                    item.setForeground(QColor("#b91c1c" if status == "failed" else "#a16207"))
                self.recent_list.addItem(item)
                if selected_job_id is not None and row["job_id"] == selected_job_id:
                    restore_item = item
            if restore_item is not None:
                self.recent_list.setCurrentItem(restore_item)

        def append_log(self, message: str, event: str = "log"):
            timestamp = datetime.now().strftime("%H:%M:%S")
            lower = message.lower()
            if event in {"failed", "watch_error"} or any(word in lower for word in ("error", "traceback", "could not load", "cannot load", "fatal")):
                tag, color = "error", "#b91c1c"
            elif event in {"warning", "completed_with_warning"} or "warning" in lower or "[warn]" in lower:
                tag, color = "warn", "#a16207"
            elif event in {"stage", "progress", "heartbeat"} or "segments, up to" in lower:
                tag, color = "run", "#2563eb"
            else:
                tag, color = "info", "#475569"
            cursor = self.log.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            style = QTextCharFormat()
            style.setForeground(QColor(color))
            if not self.log.document().isEmpty():
                cursor.insertBlock()
            cursor.insertText(f"{timestamp} [{tag}] {message}", style)
            self.log.setTextCursor(cursor)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())


    class TrayApp(QObject):
        def __init__(self, qt_app: QApplication):
            super().__init__()
            self.qt_app = qt_app
            self.qt_app.setStyleSheet(
                """
                QWidget#window { background: #f5f7fb; color: #172033; }
                QFrame#header { background: #ffffff; border-bottom: 1px solid #e5e7eb; }
                QLabel#title { font-size: 20px; font-weight: 700; color: #111827; }
                QLabel#muted { color: #6b7280; }
                QLabel#statusPill { background: #eaf2ff; color: #2563eb; border-radius: 10px; padding: 6px 10px; font-weight: 600; }
                QLabel#statusPill[tone="error"] { background: #fee2e2; color: #b91c1c; }
                QLabel#statusPill[tone="warning"] { background: #fef3c7; color: #92400e; }
                QLabel#currentName { font-size: 15px; font-weight: 600; color: #111827; }
                QLabel#previewLabel { color: #4b5563; font-style: italic; min-height: 18px; margin-top: 2px; }
                QLabel#dropHint, QLabel#pathLabel { color: #4b5563; }
                QGroupBox { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; margin-top: 8px; padding-top: 12px; font-weight: 600; }
                QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; color: #374151; }
                QGroupBox#collapsibleSection { margin-top: 0; padding-top: 0; }
                QPushButton { background: #eef2f7; border: 0; border-radius: 7px; padding: 8px 12px; color: #1f2937; }
                QPushButton:hover { background: #e1e7ef; }
                QPushButton#sectionToggle { background: transparent; text-align: left; padding: 2px; font-weight: 600; color: #374151; }
                QPushButton#sectionToggle:hover { background: #eef2f7; }
                QPushButton#dangerButton { background: #fee2e2; color: #b91c1c; }
                QPushButton#accentButton { background: #2563eb; color: #ffffff; font-weight: 600; }
                QPushButton#accentButton:hover { background: #1d4ed8; }
                QPushButton#accentButton:disabled { background: #9ca3af; color: #e5e7eb; }
                QProgressBar { border: 0; border-radius: 3px; background: #e5e7eb; }
                QProgressBar::chunk { background: #3b82f6; border-radius: 3px; }
                QListWidget, QPlainTextEdit, QLineEdit, QComboBox { background: #fbfcfe; border: 1px solid #e5e7eb; border-radius: 7px; padding: 5px; }
                QListWidget::item { padding: 3px 2px; }
                QScrollArea { background: #f5f7fb; }
                """
            )
            self.bridge = Bridge()
            self.lock = QLockFile(str(APP_DATA_DIR / "app.lock"))
            self.lock.setStaleLockTime(30000)
            self.service = BackgroundService(on_event=self._from_service)
            self.tray = QSystemTrayIcon(_make_icon(), qt_app)
            self.tray.setToolTip("Simple Video Transcriber · starting")
            self.dashboard = Dashboard(self)
            self._build_menu()
            self.bridge.event_received.connect(self._handle_event)

        def acquire(self) -> bool:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            return self.lock.tryLock(100)

        def start(self):
            self.tray.show()
            self.service.start()
            self.tray.showMessage("Watcher started", f"Watching {self.service.settings.watch_dir}", QSystemTrayIcon.MessageIcon.Information, 3000)

        def _build_menu(self):
            menu = QMenu()
            self.open_action = QAction("Open dashboard", self)
            self.open_action.triggered.connect(self.show_dashboard)
            menu.addAction(self.open_action)
            menu.addAction("Open transcripts", self.dashboard._open_transcripts)
            menu.addAction("Open logs", self.dashboard._open_logs)
            menu.addSeparator()
            self.pause_action = QAction("Pause watcher", self)
            self.pause_action.triggered.connect(self._toggle_watcher)
            menu.addAction(self.pause_action)
            menu.addSeparator()
            quit_action = QAction("Exit", self)
            quit_action.triggered.connect(self.quit)
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)

        def _tray_activated(self, reason):
            if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
                self.show_dashboard()

        def _toggle_watcher(self):
            self.service.set_watcher_enabled(not self.service.settings.watcher_enabled)
            self._update_tooltip()

        def show_dashboard(self):
            try:
                self.dashboard.refresh()
            except Exception:
                pass
            if self.dashboard.isMinimized():
                self.dashboard.showNormal()
            else:
                self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()
            if sys.platform == "win32":
                try:
                    import ctypes
                    hwnd = int(self.dashboard.winId())
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

        def _from_service(self, event: str, payload: dict):
            self.bridge.event_received.emit(event, payload)

        def _handle_event(self, event: str, payload: dict):
            message = payload.get("message", event.replace("_", " "))
            if event == "service_started":
                if Path(self.service.settings.watch_dir).exists() or not self.service.settings.watcher_enabled:
                    self.dashboard.status_label.setText("● Ready")
                else:
                    self.tray.setIcon(_make_icon("#ef4444"))
                    self.dashboard.status_label.setText("● Watch folder unavailable")
            elif event == "watch_started":
                self.tray.setToolTip(f"Simple Video Transcriber · watching")
                self.dashboard.status_label.setText("● Watching")
            elif event in {"watch_error", "failed"}:
                self.tray.setIcon(_make_icon("#ef4444"))
                self.tray.setToolTip("Simple Video Transcriber · attention needed")
                self.dashboard.status_label.setText("● Attention needed")
                if event == "failed":
                    self.tray.showMessage("Transcription failed", message, QSystemTrayIcon.MessageIcon.Critical, 7000)
            elif event in {"detected", "ready", "queued", "started", "stage", "progress", "heartbeat"}:
                if event not in {"progress", "heartbeat"}:
                    self.tray.setIcon(_make_icon("#3b82f6"))
                self.dashboard.status_label.setText("● Processing")
                self._update_tooltip()
            elif event in {"completed", "completed_with_warning"}:
                self.tray.setIcon(_make_icon("#22c55e"))
                self._update_tooltip()
                self.dashboard.status_label.setText("● Ready with warning" if event == "completed_with_warning" else "● Ready")
                self.tray.showMessage("Transcription complete" if event == "completed" else "Completed with warning", message, QSystemTrayIcon.MessageIcon.Information, 5000)
            elif event == "watch_stopped":
                self.dashboard.status_label.setText("● Paused")
                self._update_tooltip()
            elif event == "cancelled":
                self.dashboard.status_label.setText("● Cancelled")
            elif event == "cancel_requested":
                self.dashboard.status_label.setText("● Cancelling")
            if event != "log":
                detail = payload.get("error") or message
                if event == "completed_with_warning" and not payload.get("error"):
                    row = self.service.store.get(payload.get("job_id", "")) or {}
                    detail = row.get("error") or message
                if event == "service_started" and not Path(self.service.settings.watch_dir).exists() and self.service.settings.watcher_enabled:
                    detail = f"Watch folder unavailable: {self.service.settings.watch_dir}"
                self.dashboard.status_label.setToolTip(detail)
                tone = "error" if event in {"failed", "watch_error"} else "warning" if event == "completed_with_warning" else "normal"
                pill = self.dashboard.status_label
                if pill.property("tone") != tone:
                    pill.setProperty("tone", tone)
                    pill.style().unpolish(pill)
                    pill.style().polish(pill)
            if event == "progress":
                preview = payload.get("preview")
                if preview:
                    self.dashboard.preview_label.setText(preview)
            elif event in {"completed", "completed_with_warning", "failed", "cancelled", "started", "queued"}:
                self.dashboard.preview_label.setText("")
            if event not in {"progress", "heartbeat"}:
                self.dashboard.append_log(message, event)
            if self.dashboard.isVisible():
                is_state_transition = event in {
                    "queued", "started", "completed", "completed_with_warning",
                    "failed", "cancelled", "cancel_requested", "service_started",
                }
                self.dashboard.refresh(update_recent=is_state_transition)

        def _update_tooltip(self):
            active = self.service.worker.active
            if active:
                self.tray.setToolTip(f"Simple Video Transcriber · {Path(active['source_path']).name}")
            elif self.service.settings.watcher_enabled:
                self.tray.setToolTip("Simple Video Transcriber · watching")
            else:
                self.tray.setToolTip("Simple Video Transcriber · paused")

        def quit(self):
            if self.service.worker.active:
                answer = QMessageBox.question(self.dashboard, "Task is running", "Stop the current task and exit?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.service.stop()
            self.tray.hide()
            self.lock.unlock()
            self.qt_app.quit()


def main() -> int:
    if not QT_AVAILABLE:
        print("PySide6 is required for the tray dashboard. Install with: pip install PySide6", flush=True)
        return 1
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hDesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hDesk:
                user32.SetThreadDesktop(hDesk)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = TrayApp(app)
    if not controller.acquire():
        QMessageBox.information(None, "Already running", "Simple Video Transcriber is already running in the system tray.")
        return 0
    controller.start()
    if "--tray-only" not in sys.argv and "--silent" not in sys.argv:
        controller.show_dashboard()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
