"""The floating pill overlay: idle -> listening -> processing -> success/error,
plus a "card" state the pill smoothly expands into when injected text needs
a manual fallback (see app.py's use of focus_detect.py + inject.py).

Frameless, translucent, always-on-top, bottom-center of the screen.
Never steals keyboard focus (WA_ShowWithoutActivating) so the caret stays
in whatever field the user was typing into — this holds even for the card
state's Copy/dismiss buttons, which respond to mouse clicks without the
window ever being activated/receiving keyboard focus.

The card never logs or persists its text anywhere: it exists only as a
Python string held on this widget (self._card_text) and as the displayed
QTextEdit content, both cleared on dismissal or when a new recording starts
(see _leave_card).
"""
from __future__ import annotations

import ctypes
import enum
import math
import random
import time

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# --- visual constants -------------------------------------------------------

BG_COLOR = QColor(20, 20, 24, 232)
BORDER_COLOR = QColor(255, 255, 255, 18)
BAR_COLOR_TOP = QColor(146, 154, 255)      # periwinkle
BAR_COLOR_BOTTOM = QColor(190, 142, 255)   # violet
SUCCESS_COLOR = QColor(120, 235, 180)
ERROR_COLOR = QColor(235, 110, 110)        # muted rose
TEXT_COLOR = QColor(235, 235, 240, 235)

PILL_HEIGHT = 40
WIDTH_LISTENING = 192
WIDTH_PROCESSING = 104
WIDTH_SUCCESS = 104
WIDTH_ERROR = 104
BOTTOM_MARGIN = 64

NUM_BARS = 22
BAR_MIN_H = 4
BAR_MAX_H = 20

FRAME_MS = 16  # ~60fps
PAD = 16  # margin around the pill inside the widget, for shadow bleed

# Card (fallback) state
CARD_WIDTH = 320
CARD_HEIGHT = 200
CARD_RADIUS = 20

# -- native click-through toggle ---------------------------------------------
# Qt's WA_TransparentForMouseEvents attribute doesn't reliably propagate to
# the underlying WS_EX_TRANSPARENT extended window style once a translucent/
# layered native window already exists (confirmed during development: real
# OS-level clicks were silently swallowed even though Qt's own attribute
# state correctly read False — synthetic QTest clicks worked fine, proving
# it was specifically the native style bit, not the widget/layout logic).
# Setting the bit directly via ctypes is the reliable fix.
_user32 = ctypes.windll.user32
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020

# Explicit argtypes/restype — without them ctypes assumes 32-bit c_int for
# the HWND parameter, which silently truncates on 64-bit Windows.
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]


def _set_click_through(hwnd: int, click_through: bool) -> None:
    style = _user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if click_through:
        style |= _WS_EX_TRANSPARENT
    else:
        style &= ~_WS_EX_TRANSPARENT
    _user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style)


class PillState(enum.Enum):
    HIDDEN = "hidden"
    LISTENING = "listening"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    CARD = "card"


class PillOverlay(QWidget):
    """Self-contained overlay widget. Call the enter_* methods to drive it."""

    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)

        self._state = PillState.HIDDEN
        self._current_width = WIDTH_LISTENING
        self._current_height = PILL_HEIGHT
        self._start_time = time.monotonic()
        self._card_text = ""

        # waveform state
        self._bar_phases = [random.uniform(0, math.tau) for _ in range(NUM_BARS)]
        self._bar_smoothed = [0.0] * NUM_BARS
        self._target_amplitude = 0.0
        self._amplitude_decay = 0.0

        # processing shimmer state
        self._shimmer_pos = 0.0

        # success-checkmark / error-X glyph progress (0..1), animated via Qt
        # property below — only one of SUCCESS/ERROR is ever active at once,
        # so both glyphs share this single animated property.
        self._glyph_progress = 0.0

        self._glyph_hold_timer = QTimer(self)
        self._glyph_hold_timer.setSingleShot(True)
        self._glyph_hold_timer.timeout.connect(self.fade_out)

        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._on_frame)
        self._frame_timer.start(FRAME_MS)

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._width_anim = QPropertyAnimation(self, b"pillWidth", self)
        self._width_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._height_anim = QPropertyAnimation(self, b"pillHeight", self)
        self._height_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._glyph_anim = QPropertyAnimation(self, b"glyphProgress", self)
        self._glyph_anim.setEasingCurve(QEasingCurve.OutBack)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 7)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.setGraphicsEffect(shadow)

        self._build_card_widgets()

        self.setWindowOpacity(0.0)
        self._resize_and_reposition()

    # -- card child widgets -----------------------------------------------------

    def _build_card_widgets(self) -> None:
        self._card_container = QWidget(self)
        self._card_container.setAttribute(Qt.WA_TranslucentBackground, True)
        self._card_container.hide()

        self._subtle_label = QLabel("")
        self._subtle_label.setStyleSheet(
            "color: rgba(235, 235, 240, 160); font-size: 11px; font-weight: 600;"
        )
        self._subtle_label.setWordWrap(True)

        self._dismiss_button = QPushButton("✕")
        self._dismiss_button.setFixedSize(22, 22)
        self._dismiss_button.setCursor(Qt.PointingHandCursor)
        self._dismiss_button.setStyleSheet(
            "QPushButton {"
            "  color: rgba(235, 235, 240, 180);"
            "  background-color: rgba(255, 255, 255, 18);"
            "  border: none; border-radius: 11px; font-size: 11px;"
            "}"
            "QPushButton:hover { background-color: rgba(255, 255, 255, 35); }"
        )
        self._dismiss_button.clicked.connect(self._on_dismiss_clicked)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addWidget(self._subtle_label, 1)
        top_row.addWidget(self._dismiss_button, 0, Qt.AlignTop)

        self._text_view = QTextEdit()
        self._text_view.setReadOnly(True)
        self._text_view.setFrameStyle(0)
        self._text_view.setStyleSheet(
            "QTextEdit {"
            "  background: transparent;"
            "  color: rgba(235, 235, 240, 235);"
            "  font-size: 13px;"
            "  border: none;"
            "}"
        )

        self._copy_button = QPushButton("Copy")
        self._copy_button.setCursor(Qt.PointingHandCursor)
        self._copy_button.setFixedHeight(30)
        self._copy_button.setStyleSheet(
            "QPushButton {"
            "  color: white; font-size: 12px; font-weight: 600;"
            "  border: none; border-radius: 15px; padding: 0 18px;"
            "  background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 rgb(146,154,255), stop:1 rgb(190,142,255));"
            "}"
            "QPushButton:hover { background-color: rgb(160, 168, 255); }"
        )
        self._copy_button.clicked.connect(self._on_copy_clicked)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self._copy_button)

        layout = QVBoxLayout(self._card_container)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)
        layout.addLayout(top_row)
        layout.addWidget(self._text_view, 1)
        layout.addLayout(bottom_row)

    # -- Qt properties driving animations ------------------------------------

    def _get_pill_width(self) -> float:
        return self._current_width

    def _set_pill_width(self, value: float) -> None:
        self._current_width = value
        self._resize_and_reposition()

    pillWidth = Property(float, _get_pill_width, _set_pill_width)

    def _get_pill_height(self) -> float:
        return self._current_height

    def _set_pill_height(self, value: float) -> None:
        self._current_height = value
        self._resize_and_reposition()

    pillHeight = Property(float, _get_pill_height, _set_pill_height)

    def _get_glyph_progress(self) -> float:
        return self._glyph_progress

    def _set_glyph_progress(self, value: float) -> None:
        self._glyph_progress = value
        self.update()

    glyphProgress = Property(float, _get_glyph_progress, _set_glyph_progress)

    # -- layout ---------------------------------------------------------------

    def _content_rect(self) -> QRectF:
        return QRectF(PAD, PAD, self.width() - PAD * 2, self._current_height)

    def _resize_and_reposition(self) -> None:
        width, height = self._current_width, self._current_height
        total_h = height + PAD * 2
        self.setFixedSize(int(width) + PAD * 2, int(total_h))
        screen = self.screen() or self.window().screen()
        geo = screen.availableGeometry() if screen else None
        if geo is not None:
            x = geo.center().x() - self.width() // 2
            y = geo.bottom() - int(total_h) - BOTTOM_MARGIN
            self.move(x, y)
        if self._card_container.isVisible():
            rect = self._content_rect()
            self._card_container.setGeometry(
                int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height())
            )
        self.update()

    # -- public state transitions ---------------------------------------------

    def enter_listening(self) -> None:
        self._leave_card()
        self._state = PillState.LISTENING
        self._show_animated()
        self._animate_size(WIDTH_LISTENING, PILL_HEIGHT)
        self._glyph_hold_timer.stop()

    def enter_processing(self) -> None:
        self._leave_card()
        self._state = PillState.PROCESSING
        self._shimmer_pos = 0.0
        self._animate_size(WIDTH_PROCESSING, PILL_HEIGHT)

    def enter_success(self, hold_ms: int = 900) -> None:
        self._leave_card()
        self._state = PillState.SUCCESS
        self._animate_size(WIDTH_SUCCESS, PILL_HEIGHT)
        self._start_glyph_anim(hold_ms)

    def enter_error(self, hold_ms: int = 900) -> None:
        """Brief error flash for backend/network failures — mirrors enter_success."""
        self._leave_card()
        self._state = PillState.ERROR
        self._show_animated()
        self._animate_size(WIDTH_ERROR, PILL_HEIGHT)
        self._start_glyph_anim(hold_ms)

    def enter_card(self, text: str, subtle_label: str = "") -> None:
        """Expand into an interactive card showing `text` in full, with Copy
        and dismiss actions. Stays open until dismissed or superseded by a
        new enter_* call (e.g. a new recording) — see _leave_card.
        """
        self._glyph_hold_timer.stop()
        self._state = PillState.CARD
        self._card_text = text
        self._text_view.setPlainText(text)
        self._subtle_label.setText(subtle_label)
        self._subtle_label.setVisible(bool(subtle_label))
        self._card_container.show()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        _set_click_through(int(self.winId()), click_through=False)
        self._show_animated()
        self._animate_size(CARD_WIDTH, CARD_HEIGHT)

    def _start_glyph_anim(self, hold_ms: int) -> None:
        self._glyph_anim.stop()
        self._glyph_anim.setStartValue(0.0)
        self._glyph_anim.setEndValue(1.0)
        self._glyph_anim.setDuration(320)
        self._glyph_anim.start()
        self._glyph_hold_timer.start(hold_ms)

    def _leave_card(self) -> None:
        """Tear down the card (if active) before any other state takes over."""
        if self._state != PillState.CARD and not self._card_container.isVisible():
            return
        self._card_container.hide()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        _set_click_through(int(self.winId()), click_through=True)
        self._card_text = ""
        self._text_view.clear()
        self._subtle_label.setText("")

    def _on_dismiss_clicked(self) -> None:
        self._leave_card()
        self.fade_out()

    def _on_copy_clicked(self) -> None:
        if self._card_text:
            QApplication.clipboard().setText(self._card_text)
        self._copy_button.setText("Copied!")
        QTimer.singleShot(1200, lambda: self._copy_button.setText("Copy"))

    def push_level(self, level: float) -> None:
        """Feed a 0..1 microphone amplitude sample (called from audio thread-safe slot)."""
        self._target_amplitude = max(0.0, min(1.0, level))

    def fade_out(self) -> None:
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.setDuration(220)
        self._opacity_anim.finished.connect(self._on_faded_out)
        self._opacity_anim.start()

    def _on_faded_out(self) -> None:
        try:
            self._opacity_anim.finished.disconnect(self._on_faded_out)
        except (RuntimeError, TypeError):
            pass
        if self.windowOpacity() <= 0.01:
            self._leave_card()
            self._state = PillState.HIDDEN
            self.hide()
            self.dismissed.emit()

    def _show_animated(self) -> None:
        if not self.isVisible():
            self.show()
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setDuration(180)
        self._opacity_anim.start()

    def _animate_size(self, target_width: float, target_height: float) -> None:
        self._width_anim.stop()
        self._width_anim.setStartValue(self._current_width)
        self._width_anim.setEndValue(target_width)
        self._width_anim.setDuration(240)
        self._width_anim.start()

        self._height_anim.stop()
        self._height_anim.setStartValue(self._current_height)
        self._height_anim.setEndValue(target_height)
        self._height_anim.setDuration(240)
        self._height_anim.start()

    # -- frame tick -------------------------------------------------------------

    def _on_frame(self) -> None:
        if self._state == PillState.LISTENING:
            self._target_amplitude *= 0.94  # decay if no fresher sample arrives
            self._tick_waveform()
            self.update()
        elif self._state == PillState.PROCESSING:
            self._shimmer_pos = (self._shimmer_pos + 0.02) % 1.6
            self.update()

    def _tick_waveform(self) -> None:
        t = time.monotonic() - self._start_time
        for i in range(NUM_BARS):
            wobble = 0.45 + 0.55 * abs(math.sin(t * 6.0 + self._bar_phases[i]))
            target = self._target_amplitude * wobble
            smoothed = self._bar_smoothed[i]
            attack = 0.5 if target > smoothed else 0.18
            self._bar_smoothed[i] = smoothed + (target - smoothed) * attack

    # -- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pill_rect = self._content_rect()
        is_card = self._state == PillState.CARD
        radius = CARD_RADIUS if is_card else self._current_height / 2
        path = QPainterPath()
        path.addRoundedRect(pill_rect, radius, radius)

        painter.fillPath(path, BG_COLOR)
        painter.setPen(QPen(BORDER_COLOR, 1))
        painter.drawPath(path)

        if is_card:
            return  # content is the real child widgets (_card_container), not custom-painted

        painter.save()
        painter.setClipPath(path)
        if self._state == PillState.LISTENING:
            self._paint_waveform(painter, pill_rect)
        elif self._state == PillState.PROCESSING:
            self._paint_shimmer(painter, pill_rect)
        elif self._state == PillState.SUCCESS:
            self._paint_success(painter, pill_rect)
        elif self._state == PillState.ERROR:
            self._paint_error(painter, pill_rect)
        painter.restore()

    def _paint_waveform(self, painter: QPainter, rect: QRectF) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, BAR_COLOR_TOP)
        gradient.setColorAt(1.0, BAR_COLOR_BOTTOM)
        pen = QPen(gradient, 2.6, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)

        usable_w = rect.width() - 20
        spacing = usable_w / (NUM_BARS - 1)
        cy = rect.center().y()
        for i in range(NUM_BARS):
            h = BAR_MIN_H + self._bar_smoothed[i] * (BAR_MAX_H - BAR_MIN_H)
            x = rect.left() + 10 + i * spacing
            painter.drawLine(QPointF(x, cy - h / 2), QPointF(x, cy + h / 2))

    def _paint_shimmer(self, painter: QPainter, rect: QRectF) -> None:
        dot_radius = 3.2
        gap = 13.0
        cy = rect.center().y()
        cx = rect.center().x()
        for i, dx in enumerate((-gap, 0.0, gap)):
            phase = self._shimmer_pos * math.tau + i * (math.tau / 3)
            scale = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(phase))
            color = QColor(BAR_COLOR_TOP)
            color.setAlphaF(0.45 + 0.55 * scale)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            r = dot_radius * (0.7 + 0.5 * scale)
            painter.drawEllipse(QPointF(cx + dx, cy), r, r)

    def _paint_success(self, painter: QPainter, rect: QRectF) -> None:
        progress = self._glyph_progress
        if progress <= 0:
            return
        cx, cy = rect.center().x(), rect.center().y()
        radius = 10.0
        color = QColor(SUCCESS_COLOR)
        color.setAlphaF(min(1.0, progress + 0.15))
        pen = QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)

        p1 = QPointF(cx - radius * 0.5, cy)
        p2 = QPointF(cx - radius * 0.12, cy + radius * 0.42)
        p3 = QPointF(cx + radius * 0.55, cy - radius * 0.4)

        clamped = min(1.0, progress * 1.4)
        if clamped <= 0.5:
            seg_t = clamped / 0.5
            mid = QPointF(
                p1.x() + (p2.x() - p1.x()) * seg_t,
                p1.y() + (p2.y() - p1.y()) * seg_t,
            )
            painter.drawLine(p1, mid)
        else:
            painter.drawLine(p1, p2)
            seg_t = (clamped - 0.5) / 0.5
            mid = QPointF(
                p2.x() + (p3.x() - p2.x()) * seg_t,
                p2.y() + (p3.y() - p2.y()) * seg_t,
            )
            painter.drawLine(p2, mid)

    def _paint_error(self, painter: QPainter, rect: QRectF) -> None:
        """Muted rose 'X' — same progressive-line technique as the checkmark."""
        progress = self._glyph_progress
        if progress <= 0:
            return
        cx, cy = rect.center().x(), rect.center().y()
        radius = 9.0
        color = QColor(ERROR_COLOR)
        color.setAlphaF(min(1.0, progress + 0.15))
        pen = QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)

        tl = QPointF(cx - radius, cy - radius)
        br = QPointF(cx + radius, cy + radius)
        tr = QPointF(cx + radius, cy - radius)
        bl = QPointF(cx - radius, cy + radius)

        clamped = min(1.0, progress * 1.4)
        if clamped <= 0.5:
            seg_t = clamped / 0.5
            mid = QPointF(
                tl.x() + (br.x() - tl.x()) * seg_t,
                tl.y() + (br.y() - tl.y()) * seg_t,
            )
            painter.drawLine(tl, mid)
        else:
            painter.drawLine(tl, br)
            seg_t = (clamped - 0.5) / 0.5
            mid = QPointF(
                tr.x() + (bl.x() - tr.x()) * seg_t,
                tr.y() + (bl.y() - tr.y()) * seg_t,
            )
            painter.drawLine(tr, mid)
