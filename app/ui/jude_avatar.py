from __future__ import annotations

import math
from enum import Enum

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget


class AvatarState(str, Enum):
    """Visual states for the J.U.D.E. floating avatar."""

    IDLE = "idle"
    LISTENING = "listening"
    FAST_TIER1 = "fast_tier1"
    THINKING_TIER2 = "thinking_tier2"
    CONFIRM_WAIT = "confirm_wait"
    SPEAKING = "speaking"
    ERROR = "error"


_STATE_COLORS: dict[AvatarState, QColor] = {
    AvatarState.IDLE: QColor("#38BDF8"),
    AvatarState.LISTENING: QColor("#06B6D4"),
    AvatarState.FAST_TIER1: QColor("#10B981"),
    AvatarState.THINKING_TIER2: QColor("#8B5CF6"),
    AvatarState.CONFIRM_WAIT: QColor("#F59E0B"),
    AvatarState.SPEAKING: QColor("#0EA5E9"),
    AvatarState.ERROR: QColor("#EF4444"),
}


class JudeAvatarWidget(QWidget):
    """Pure QPainter J.U.D.E. glossy capsule robot head with blinking eyes and glow ring."""

    clicked = Signal()
    double_clicked = Signal()
    drag_moved = Signal(QPoint)
    context_menu_requested = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None, size: int = 108) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("J.U.D.E. — Double-click to open/collapse chat, click for caption, drag to move")

        self._state: AvatarState = AvatarState.IDLE
        self._phase: float = 0.0
        self._blink_elapsed_ms: int = 0
        self._next_blink_ms: int = 3600
        self._blink_active: bool = False
        self._blink_progress: float = 0.0  # 0=open, 1=closed
        self._drag_origin: QPoint | None = None
        self._drag_distance: int = 0
        self._ignore_release_click: bool = False

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    @property
    def state(self) -> AvatarState:
        return self._state

    def set_state(self, state: AvatarState) -> None:
        self._state = state
        self.update()

    def trigger_blink(self) -> None:
        """Start a smooth blink cycle immediately."""
        self._blink_active = True
        self._blink_progress = 0.0
        self.update()

    def set_blink_fraction(self, fraction: float) -> None:
        """Explicitly set blink fraction (0.0 open -> 1.0 closed) for deterministic testing."""
        self._blink_active = fraction > 0.0
        self._blink_progress = max(0.0, min(1.0, float(fraction)))
        self.update()

    def _on_tick(self) -> None:
        step = 0.09 if self._state == AvatarState.THINKING_TIER2 else 0.045
        self._phase = (self._phase + step) % (2.0 * math.pi * 100.0)

        if self._blink_active:
            self._blink_progress += 0.16
            if self._blink_progress >= 2.0:
                self._blink_active = False
                self._blink_progress = 0.0
                self._blink_elapsed_ms = 0
                self._next_blink_ms = 4200
        else:
            self._blink_elapsed_ms += 16
            if self._blink_elapsed_ms >= self._next_blink_ms:
                self._blink_active = True
                self._blink_progress = 0.0

        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_distance = 0
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.context_menu_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            cur = event.globalPosition().toPoint()
            delta = cur - self._drag_origin
            self._drag_distance += abs(delta.x()) + abs(delta.y())
            self._drag_origin = cur
            self.drag_moved.emit(delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = None
            self._ignore_release_click = True
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._ignore_release_click:
                self._ignore_release_click = False
                self._drag_origin = None
                event.accept()
                return
            if self._drag_origin is not None:
                was_click = self._drag_distance < 6
                self._drag_origin = None
                if was_click:
                    self.clicked.emit()
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        cx = w / 2.0
        bob_y = math.sin(self._phase) * 2.4
        cy = (h / 2.0) - 2.0 + bob_y

        ring_color = QColor(_STATE_COLORS.get(self._state, QColor("#38BDF8")))
        pulse = 0.5 + 0.5 * math.sin(self._phase * 1.4)

        # 1. Soft ground shadow below capsule
        shadow_scale = 1.0 - (bob_y / 18.0)
        shadow_w = w * 0.50 * shadow_scale
        shadow_h = h * 0.11 * shadow_scale
        shadow_grad = QRadialGradient(cx, h - 8.0, shadow_w * 0.6)
        shadow_grad.setColorAt(0.0, QColor(15, 23, 42, 85))
        shadow_grad.setColorAt(1.0, QColor(15, 23, 42, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_grad)
        painter.drawEllipse(QRectF(cx - shadow_w / 2.0, h - 12.0, shadow_w, shadow_h))

        # 2. Outer status glow aura
        glow_radius = min(w, h) * (0.46 + 0.03 * pulse)
        glow_grad = QRadialGradient(cx, cy, glow_radius)
        c_mid = QColor(ring_color)
        c_mid.setAlpha(int(120 + 65 * pulse))
        c_edge = QColor(ring_color)
        c_edge.setAlpha(0)
        glow_grad.setColorAt(0.55, c_mid)
        glow_grad.setColorAt(1.0, c_edge)
        painter.setBrush(glow_grad)
        painter.drawEllipse(QRectF(cx - glow_radius, cy - glow_radius, glow_radius * 2, glow_radius * 2))

        # 3. Crisp outer status ring (+ animated rotating sweep arc when thinking)
        head_w = w * 0.66
        head_h = h * 0.50
        head_rect = QRectF(cx - head_w / 2.0, cy - head_h / 2.0, head_w, head_h)
        ring_rect = head_rect.adjusted(-4.0, -4.0, 4.0, 4.0)

        ring_pen_color = QColor(ring_color)
        ring_pen_color.setAlpha(int(170 + 75 * pulse))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ring_pen_color, 2.4))
        painter.drawRoundedRect(ring_rect, ring_rect.height() / 2.0, ring_rect.height() / 2.0)

        if self._state in (AvatarState.THINKING_TIER2, AvatarState.SPEAKING):
            sweep_color = QColor("#FFFFFF") if self._state == AvatarState.THINKING_TIER2 else QColor(ring_color)
            sweep_color.setAlpha(220)
            painter.setPen(QPen(sweep_color, 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            start_deg = int((self._phase * 95.0) % 360) * 16
            painter.drawArc(ring_rect.adjusted(-2.5, -2.5, 2.5, 2.5), start_deg, 75 * 16)

        # 4. Glossy porcelain-white capsule head
        head_grad = QLinearGradient(head_rect.topLeft(), head_rect.bottomRight())
        head_grad.setColorAt(0.0, QColor("#FFFFFF"))
        head_grad.setColorAt(0.65, QColor("#F3F7FC"))
        head_grad.setColorAt(1.0, QColor("#DCE4F0"))

        painter.setBrush(head_grad)
        painter.setPen(QPen(QColor("#CBD5E1"), 1.3))
        painter.drawRoundedRect(head_rect, head_h / 2.0, head_h / 2.0)

        # Specular highlight along upper-left curve
        highlight_path = QPainterPath()
        hl_rect = head_rect.adjusted(head_w * 0.14, head_h * 0.10, -head_w * 0.34, -head_h * 0.52)
        highlight_path.addRoundedRect(hl_rect, hl_rect.height() / 2.0, hl_rect.height() / 2.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 160))
        painter.drawPath(highlight_path)

        # 5. Connected black eyes with blink + subtle gaze
        gaze_x = math.sin(self._phase * 0.7) * 2.2 if self._state == AvatarState.THINKING_TIER2 else 0.0
        eye_y = cy + 0.5
        eye_sep = head_w * 0.25
        left_x = cx - eye_sep + gaze_x
        right_x = cx + eye_sep + gaze_x

        if self._blink_progress <= 1.0:
            close_frac = self._blink_progress
        else:
            close_frac = 2.0 - self._blink_progress
        eye_rx = max(3.6, head_w * 0.072)
        eye_ry = max(1.0, eye_rx * (1.0 - 0.88 * close_frac))

        painter.setPen(QPen(QColor("#1E293B"), max(1.6, head_h * 0.036), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPoint(int(left_x), int(eye_y)), QPoint(int(right_x), int(eye_y)))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0F172A"))
        painter.drawEllipse(QRectF(left_x - eye_rx, eye_y - eye_ry, eye_rx * 2.0, eye_ry * 2.0))
        painter.drawEllipse(QRectF(right_x - eye_rx, eye_y - eye_ry, eye_rx * 2.0, eye_ry * 2.0))


def build_jude_tray_icon(size: int = 64) -> QIcon:
    """Paint a razor-sharp mini J.U.D.E. capsule icon in memory (0 MB external assets)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    pad = size * 0.08
    rect = QRectF(pad, size * 0.20, size - 2 * pad, size * 0.60)

    painter.setBrush(QColor("#0EA5E9"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), rect.height() / 2.0, rect.height() / 2.0)

    painter.setBrush(QColor("#FFFFFF"))
    painter.setPen(QPen(QColor("#CBD5E1"), 1.5))
    painter.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)

    cy = rect.center().y()
    lx = rect.center().x() - rect.width() * 0.24
    rx = rect.center().x() + rect.width() * 0.24
    r = size * 0.065

    painter.setPen(QPen(QColor("#0F172A"), max(2.0, size * 0.04)))
    painter.drawLine(QPoint(int(lx), int(cy)), QPoint(int(rx), int(cy)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0F172A"))
    painter.drawEllipse(QRectF(lx - r, cy - r, 2 * r, 2 * r))
    painter.drawEllipse(QRectF(rx - r, cy - r, 2 * r, 2 * r))
    painter.end()

    return QIcon(pixmap)
