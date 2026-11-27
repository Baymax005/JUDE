from __future__ import annotations

import html
import os
from typing import Callable

import win32gui
import win32process
from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.engine import EngineResponse, JudeEngine
from app.ui.confirm_dialog import ConfirmationDialog
from app.ui.jude_avatar import AvatarState, JudeAvatarWidget, build_jude_tray_icon
from app.ui.memory_dialog import MemoryManagerDialog
from app.voice.listener import VoiceListener
from app.voice.tts import VoiceSpeaker


class _TaskWorker(QObject):
    """Runs JudeEngine tasks off the GUI thread so J.U.D.E. animations remain 60 FPS."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], EngineResponse]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.finished.emit(self._fn())
        except Exception as exc:
            self.failed.emit(str(exc))


class _SpeechBubbleFrame(QFrame):
    """Compact caption bubble above J.U.D.E. that toggles full chat on click."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class JudeFloatingWindow(QWidget):
    """Frameless always-on-top floating J.U.D.E. widget + expandable HUD."""

    def __init__(
        self,
        engine: JudeEngine | None = None,
        speaker: VoiceSpeaker | None = None,
        listener: VoiceListener | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine or JudeEngine()
        self.speaker = speaker or VoiceSpeaker()
        self.listener = listener or VoiceListener(
            echo_guard_fn=lambda: self.speaker.is_speaking_or_cooling
        )
        self._hud_open = False
        self._last_external_hwnd: int = 0
        self._active_thread: QThread | None = None
        self._active_worker: _TaskWorker | None = None
        self._history_html: list[str] = []
        self._last_bubble_text: str = ""

        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.setInterval(8500)
        self._bubble_timer.timeout.connect(self._hide_bubble)

        self.setWindowTitle("J.U.D.E.")
        self.setWindowIcon(build_jude_tray_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._build_ui()
        self._setup_tray()
        self._snap_bottom_right()

        self.listener.speech_started.connect(self._on_speech_started)
        self.listener.utterance_ready.connect(self._on_voice_utterance)
        self.listener.status_changed.connect(self._append_system_message)

        # Track last external active window so "Explain Screen" sees the app behind J.U.D.E.
        self._fg_timer = QTimer(self)
        self._fg_timer.setInterval(400)
        self._fg_timer.timeout.connect(self._poll_foreground_window)
        self._fg_timer.start()

        self._append_system_message(
            f"Ready ({self.engine.router_status}). Double-click J.U.D.E. to open chat or collapse."
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Expandable HUD Card (hidden in compact floating mode)
        self.hud_card = QFrame()
        self.hud_card.setObjectName("hudCard")
        self.hud_card.setFixedWidth(460)
        self.hud_card.setStyleSheet(
            """
            QFrame#hudCard {
                background-color: rgba(15, 23, 42, 242);
                border: 1px solid #334155;
                border-radius: 16px;
            }
            QLabel#brandLbl {
                color: #F8FAFC;
                font-size: 15px;
                font-weight: 700;
                letter-spacing: 0.8px;
            }
            QLabel#tierBadge {
                background-color: #1E293B;
                color: #38BDF8;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 3px 9px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton.iconBtn {
                background-color: #1E293B;
                color: #E2E8F0;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 4px 9px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton.iconBtn:hover {
                background-color: #334155;
            }
            QPushButton#closeBtn {
                background-color: #1E293B;
                color: #FCA5A5;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 4px 9px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#closeBtn:hover {
                background-color: #DC2626;
                color: #FFFFFF;
                border-color: #EF4444;
            }
            QPushButton.chipBtn {
                background-color: #1E293B;
                color: #CBD5E1;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton.chipBtn:hover {
                background-color: #0F172A;
                border-color: #38BDF8;
                color: #F8FAFC;
            }
            QTextBrowser {
                background-color: rgba(30, 41, 59, 175);
                color: #F1F5F9;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 8px;
                font-family: "Segoe UI", sans-serif;
                font-size: 12.5px;
            }
            QLineEdit {
                background-color: #1E293B;
                color: #F8FAFC;
                border: 1px solid #475569;
                border-radius: 10px;
                padding: 8px 10px;
                font-size: 12.5px;
            }
            QLineEdit:focus {
                border-color: #38BDF8;
            }
            QPushButton#sendBtn {
                background-color: #0EA5E9;
                color: #082F49;
                border: none;
                border-radius: 10px;
                padding: 8px 14px;
                font-weight: 700;
            }
            QPushButton#sendBtn:hover {
                background-color: #38BDF8;
            }
            """
        )

        hud_layout = QVBoxLayout(self.hud_card)
        hud_layout.setContentsMargins(12, 12, 12, 12)
        hud_layout.setSpacing(8)

        header_row_1 = QHBoxLayout()
        header_row_1.setSpacing(6)
        brand_lbl = QLabel("J.U.D.E.")
        brand_lbl.setObjectName("brandLbl")

        self.tier_badge = QLabel("Ready")
        self.tier_badge.setObjectName("tierBadge")
        self.tier_badge.setMaximumWidth(250)

        collapse_btn = QPushButton("—")
        collapse_btn.setProperty("class", "iconBtn")
        collapse_btn.setToolTip("Collapse to floating J.U.D.E. widget")
        collapse_btn.clicked.connect(self.toggle_hud)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setToolTip("Close & Quit J.U.D.E.")
        self.close_btn.clicked.connect(self._quit_app)

        header_row_1.addWidget(brand_lbl)
        header_row_1.addWidget(self.tier_badge, 0, Qt.AlignmentFlag.AlignLeft)
        header_row_1.addStretch(1)
        header_row_1.addWidget(collapse_btn)
        header_row_1.addWidget(self.close_btn)
        hud_layout.addLayout(header_row_1)

        header_row_2 = QHBoxLayout()
        header_row_2.setSpacing(6)

        self.memory_btn = QPushButton("Memory")
        self.memory_btn.setProperty("class", "iconBtn")
        self.memory_btn.setToolTip("View or delete saved memories and learned preferences")
        self.memory_btn.clicked.connect(self.open_memory_manager)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setProperty("class", "iconBtn")
        self.clear_btn.setToolTip("Clear chat history")
        self.clear_btn.clicked.connect(self.clear_chat)

        self.hud_mic_btn = QPushButton()
        self.hud_mic_btn.setProperty("class", "iconBtn")
        self.hud_mic_btn.clicked.connect(self.toggle_mic)

        self.hud_mute_btn = QPushButton()
        self.hud_mute_btn.setProperty("class", "iconBtn")
        self.hud_mute_btn.clicked.connect(self.toggle_mute)

        header_row_2.addWidget(self.memory_btn)
        header_row_2.addWidget(self.clear_btn)
        header_row_2.addStretch(1)
        header_row_2.addWidget(self.hud_mic_btn)
        header_row_2.addWidget(self.hud_mute_btn)
        hud_layout.addLayout(header_row_2)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(True)
        self.chat_view.setMinimumHeight(210)
        self.chat_view.setMaximumHeight(280)
        hud_layout.addWidget(self.chat_view)

        chips_row = QHBoxLayout()
        for label, prompt in (
            ("System Status", "Give me a quick system status report"),
            ("Explain Screen", "Explain what is currently on my screen"),
            ("Polish Word", "Rewrite the selected paragraph in Word to sound formal"),
        ):
            chip = QPushButton(label)
            chip.setProperty("class", "chipBtn")
            chip.clicked.connect(lambda _=False, p=prompt: self.submit_query(p))
            chips_row.addWidget(chip)
        hud_layout.addLayout(chips_row)

        input_row = QHBoxLayout()
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Ask J.U.D.E. anything...")
        self.input_box.returnPressed.connect(self._on_send_clicked)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._on_send_clicked)

        input_row.addWidget(self.input_box, 1)
        input_row.addWidget(self.send_btn)
        hud_layout.addLayout(input_row)

        self.hud_card.setVisible(False)
        root.addWidget(self.hud_card, 0, Qt.AlignmentFlag.AlignRight)

        # Compact Floating J.U.D.E. Dock (always visible)
        dock_col = QVBoxLayout()
        dock_col.setSpacing(4)
        dock_col.setContentsMargins(0, 0, 0, 0)

        self.speech_bubble = _SpeechBubbleFrame(self)
        self.speech_bubble.setObjectName("speechBubble")
        self.speech_bubble.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speech_bubble.setToolTip("Click to open full chat")
        self.speech_bubble.setMaximumWidth(275)
        self.speech_bubble.setStyleSheet(
            """
            QFrame#speechBubble {
                background-color: rgba(15, 23, 42, 238);
                border: 1px solid #38BDF8;
                border-radius: 12px;
            }
            """
        )
        bubble_layout = QVBoxLayout(self.speech_bubble)
        bubble_layout.setContentsMargins(10, 6, 10, 6)
        self.bubble_label = QLabel("")
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setStyleSheet(
            "color: #F8FAFC; font-size: 12px; font-weight: 500; background: transparent; border: none;"
        )
        bubble_layout.addWidget(self.bubble_label)
        self.speech_bubble.clicked.connect(self.toggle_hud)
        self.speech_bubble.setVisible(False)
        dock_col.addWidget(self.speech_bubble, 0, Qt.AlignmentFlag.AlignRight)

        self.avatar = JudeAvatarWidget(self, size=108)
        self.avatar.clicked.connect(self._on_avatar_single_click)
        self.avatar.double_clicked.connect(self.toggle_hud)
        self.avatar.drag_moved.connect(self._on_avatar_dragged)
        self.avatar.context_menu_requested.connect(self._show_avatar_menu)
        dock_col.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignRight)

        dock_pills = QHBoxLayout()
        dock_pills.setSpacing(4)
        dock_pills.setContentsMargins(0, 0, 0, 0)

        pill_style = """
            QPushButton {
                background-color: rgba(15, 23, 42, 225);
                color: #E2E8F0;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 2px 8px;
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton:hover {
                border-color: #38BDF8;
            }
        """

        self.mini_mic_btn = QPushButton()
        self.mini_mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mini_mic_btn.setStyleSheet(pill_style)
        self.mini_mic_btn.clicked.connect(self.toggle_mic)

        self.mini_mute_btn = QPushButton()
        self.mini_mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mini_mute_btn.setStyleSheet(pill_style)
        self.mini_mute_btn.clicked.connect(self.toggle_mute)

        dock_pills.addWidget(self.mini_mic_btn)
        dock_pills.addWidget(self.mini_mute_btn)
        dock_col.addLayout(dock_pills)

        root.addLayout(dock_col)
        self._refresh_mute_labels()
        self._refresh_mic_labels()
        self.adjustSize()

    def _setup_tray(self) -> None:
        self.tray_icon: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(build_jude_tray_icon(), self)
        tray.setToolTip("J.U.D.E. Desktop Assistant")

        menu = self._build_context_menu()
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.toggle_hud()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        tray.show()
        self.tray_icon = tray

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        toggle_act = QAction("Show / Hide J.U.D.E. Panel", self)
        toggle_act.triggered.connect(self.toggle_hud)
        memory_act = QAction("Manage Memories...", self)
        memory_act.triggered.connect(self.open_memory_manager)
        mic_act = QAction("Toggle Microphone", self)
        mic_act.triggered.connect(self.toggle_mic)
        mute_act = QAction("Toggle Voice Mute", self)
        mute_act.triggered.connect(self.toggle_mute)
        reset_act = QAction("Reset Widget Position", self)
        reset_act.triggered.connect(self._snap_bottom_right)
        quit_act = QAction("Quit J.U.D.E.", self)
        quit_act.triggered.connect(self._quit_app)

        menu.addAction(toggle_act)
        menu.addAction(memory_act)
        menu.addAction(mic_act)
        menu.addAction(mute_act)
        menu.addAction(reset_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        return menu

    def _show_avatar_menu(self, global_pos: QPoint) -> None:
        menu = self._build_context_menu()
        menu.exec(global_pos)

    def open_memory_manager(self) -> None:
        dlg = MemoryManagerDialog(self.engine.memory_db_path, self)
        dlg.exec()

    def clear_chat(self) -> None:
        self._history_html.clear()
        self.chat_view.clear()
        self.engine.ollama.clear_history()
        self.tier_badge.setText("Ready")
        self._append_system_message("Chat cleared.")

    def _snap_bottom_right(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        x = avail.right() - self.width() - 20
        y = avail.bottom() - self.height() - 18
        self.move(max(avail.left(), x), max(avail.top(), y))

    def _on_avatar_dragged(self, delta: QPoint) -> None:
        self.move(self.pos() + delta)

    def _poll_foreground_window(self) -> None:
        fg = win32gui.GetForegroundWindow()
        if not fg or not win32gui.IsWindowVisible(fg):
            return
        _, pid = win32process.GetWindowThreadProcessId(fg)
        if pid != os.getpid() and win32gui.GetWindowText(fg).strip():
            self._last_external_hwnd = fg

    def _refresh_mute_labels(self) -> None:
        muted = self.speaker.muted
        label = "Muted" if muted else "Voice On"
        self.mini_mute_btn.setText(label)
        self.hud_mute_btn.setText(label)

    def _refresh_mic_labels(self) -> None:
        on = self.listener.enabled
        label = "Mic On" if on else "Mic Off"
        self.mini_mic_btn.setText(label)
        self.hud_mic_btn.setText(label)

    def toggle_mic(self) -> None:
        self.listener.set_enabled(not self.listener.enabled)
        self._refresh_mic_labels()

    def toggle_mute(self) -> None:
        self.speaker.set_muted(not self.speaker.muted)
        self._refresh_mute_labels()

    def _adjust_size_preserving_bottom_right(self) -> None:
        old_bottom_right = self.geometry().bottomRight()
        self.adjustSize()
        if old_bottom_right.x() > 0 and old_bottom_right.y() > 0:
            new_x = old_bottom_right.x() - self.width() + 1
            new_y = old_bottom_right.y() - self.height() + 1
            screen = QGuiApplication.primaryScreen()
            if screen:
                avail = screen.availableGeometry()
                new_x = max(avail.left(), min(new_x, avail.right() - self.width()))
                new_y = max(avail.top(), min(new_y, avail.bottom() - self.height()))
            self.move(new_x, new_y)

    def _show_bubble(self, text: str, auto_hide_ms: int = 8500) -> None:
        clean = " ".join((text or "").split())
        if not clean:
            return
        if len(clean) > 240:
            clean = clean[:237].rstrip() + "..."
        self._last_bubble_text = clean
        if self._hud_open:
            return
        self.bubble_label.setText(clean)
        self.speech_bubble.setVisible(True)
        self._adjust_size_preserving_bottom_right()
        if auto_hide_ms > 0:
            self._bubble_timer.start(auto_hide_ms)
        else:
            self._bubble_timer.stop()

    def _hide_bubble(self) -> None:
        self._bubble_timer.stop()
        if self.speech_bubble.isVisible():
            self.speech_bubble.setVisible(False)
            self._adjust_size_preserving_bottom_right()

    def _on_avatar_single_click(self) -> None:
        if self._hud_open:
            return
        if self.speech_bubble.isVisible():
            self._hide_bubble()
        elif self._last_bubble_text:
            self._show_bubble(self._last_bubble_text, auto_hide_ms=8500)

    def _on_speech_started(self) -> None:
        if self._active_thread is None:
            self.avatar.set_state(AvatarState.LISTENING)
            self.tier_badge.setText("Listening...")
            if not self._hud_open:
                self._show_bubble("Listening...", auto_hide_ms=0)

    def _on_voice_utterance(self, text: str) -> None:
        if self._active_thread is not None:
            return
        self.submit_query(text, from_voice=True)

    def toggle_hud(self) -> None:
        self._hud_open = not self._hud_open
        if self._hud_open:
            self._bubble_timer.stop()
            self.speech_bubble.setVisible(False)
        self.hud_card.setVisible(self._hud_open)
        self._adjust_size_preserving_bottom_right()
        if self._hud_open:
            self.activateWindow()
            self.input_box.setFocus()

    def _append_message(self, speaker_label: str, text: str, color: str) -> None:
        safe_html = html.escape(text).replace("\n", "<br>")
        self._history_html.append(
            f"<div style='margin-bottom:8px;'>"
            f"<span style='color:{color}; font-weight:700;'>{html.escape(speaker_label)}:</span> "
            f"<span>{safe_html}</span></div>"
        )
        self.chat_view.setHtml("".join(self._history_html))
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _append_system_message(self, text: str) -> None:
        self._append_message("System", text, "#94A3B8")

    def _set_busy(self, busy: bool) -> None:
        self.input_box.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)

    def _on_send_clicked(self) -> None:
        text = self.input_box.text().strip()
        if not text:
            return
        self.input_box.clear()
        self.submit_query(text)

    def submit_query(self, text: str, from_voice: bool = False) -> None:
        if self._active_thread is not None:
            return
        if not from_voice and not self._hud_open:
            self.toggle_hud()
        self.listener.set_paused(True)
        self._append_message("You (Voice)" if from_voice else "You", text, "#38BDF8")
        self.tier_badge.setText("Routing...")
        self.avatar.set_state(AvatarState.THINKING_TIER2)
        self._set_busy(True)
        if not self._hud_open:
            self._show_bubble("Thinking...", auto_hide_ms=0)

        hwnd = self._last_external_hwnd
        self._run_async(lambda: self.engine.process(text, target_hwnd=hwnd))

    def _run_async(self, fn: Callable[[], EngineResponse]) -> None:
        thread = QThread(self)
        worker = _TaskWorker(fn)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_engine_response)
        worker.failed.connect(self._on_engine_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._clear_worker_refs)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._active_thread = thread
        self._active_worker = worker
        thread.start()

    def _clear_worker_refs(self) -> None:
        self._active_thread = None
        self._active_worker = None
        self._set_busy(False)

    def _on_engine_error(self, err: str) -> None:
        self.listener.set_paused(False)
        self.avatar.set_state(AvatarState.ERROR)
        self.tier_badge.setText("Error")
        self._append_message("J.U.D.E.", f"Error: {err}", "#F87171")
        if not self._hud_open:
            self._show_bubble(f"Error: {err}", auto_hide_ms=8500)
        QTimer.singleShot(2500, lambda: self.avatar.set_state(AvatarState.IDLE))

    def _on_engine_response(self, resp: EngineResponse) -> None:
        self.tier_badge.setText(resp.badge_text)
        self._append_message("J.U.D.E.", resp.reply, "#10B981" if resp.tier_used == "tier1" else "#A78BFA")

        if resp.pending is not None:
            self.avatar.set_state(AvatarState.CONFIRM_WAIT)
            dialog = ConfirmationDialog(resp.pending, self)
            accepted = dialog.exec() == ConfirmationDialog.DialogCode.Accepted
            if accepted:
                edited = dialog.edited_after_text()
                self.avatar.set_state(AvatarState.THINKING_TIER2)
                self._set_busy(True)
                self._run_async(lambda pid=resp.pending.id, ed=edited: self.engine.confirm_pending(pid, ed))
                return

            escalate = dialog.escalate_requested
            self.avatar.set_state(AvatarState.THINKING_TIER2 if escalate else AvatarState.IDLE)
            self._set_busy(True)
            self._run_async(lambda pid=resp.pending.id, esc=escalate: self.engine.cancel_pending(pid, esc))
            return

        if resp.tier_used == "tier1":
            self.avatar.set_state(AvatarState.FAST_TIER1)
        else:
            self.avatar.set_state(AvatarState.SPEAKING if not self.speaker.muted else AvatarState.IDLE)

        if resp.spoken_summary:
            self.speaker.speak(resp.spoken_summary)

        if not self._hud_open:
            bubble_msg = (resp.spoken_summary or resp.reply).strip()
            if bubble_msg:
                self._show_bubble(bubble_msg, auto_hide_ms=8500)

        self.listener.set_paused(False)
        QTimer.singleShot(2200, lambda: self.avatar.set_state(AvatarState.IDLE))

    def _quit_app(self) -> None:
        self.listener.stop()
        self.speaker.stop()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        QApplication.quit()
