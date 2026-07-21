"""The floating capsule overlay — a pixel-accurate recreation of the design
handoff at D:\\ritely\\design_handoff_audio_capsule\\ (README.md +
Audio Capsule.dc.html). That document is the source of truth for the glass
shell, button/canvas/timer dimensions, and the waveform's canvas math; see
theme.py for the exact token values copied from it.

Two states from the handoff: IDLE (a compact resting pill, flat waveform,
always visible — "waiting for the hotkey") and LISTENING (cancel/waveform/
timer/confirm). PROCESSING / SUCCESS / ERROR / CARD are this app's own
additions for backend-call feedback and the injection-fallback safety net —
not covered by the handoff, styled to match its shell for consistency.

Frameless, always-on-top, bottom-center of the screen, permanently visible
(idle when not recording) rather than appearing/disappearing per recording —
matches the handoff's "always-ready listener" framing. Never steals keyboard
focus (WA_ShowWithoutActivating) so the caret stays in whatever field the
user was typing into.

The card never logs or persists its text anywhere: it exists only as a
Python string held on this widget (self._card_text) and as the displayed
QTextEdit content, both cleared on dismissal or when a new recording starts.
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
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
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

from . import theme

# --- sizing (all from theme.py, which mirrors the handoff exactly) ----------

CAPSULE_HEIGHT = theme.CAPSULE_HEIGHT
PAD_LEFT = theme.PAD_LEFT
PAD_RIGHT = theme.PAD_RIGHT
GAP = theme.GAP
CANCEL_SIZE = theme.CANCEL_SIZE
CONFIRM_SIZE = theme.CONFIRM_SIZE
CANVAS_W = theme.CANVAS_WIDTH          # listening's canvas width
IDLE_CANVAS_W = theme.IDLE_CANVAS_WIDTH  # idle's own, much narrower canvas
CANVAS_H = theme.CANVAS_HEIGHT
TIMER_MIN_W = theme.TIMER_MIN_WIDTH

# Widths derived from the handoff's flex layout (content-driven, no explicit
# width in the source — computed here from padding + children + gaps). IDLE
# uses its own narrower canvas so it reads as clearly smaller than LISTENING,
# not just a hair narrower.
WIDTH_IDLE = PAD_LEFT + IDLE_CANVAS_W + PAD_RIGHT
WIDTH_LISTENING = (
    PAD_LEFT + CANCEL_SIZE + GAP + CANVAS_W + GAP + TIMER_MIN_W + GAP + CONFIRM_SIZE + PAD_RIGHT
)

# Our own additional states, not in the handoff — kept narrow, same height.
WIDTH_PROCESSING = 50
WIDTH_SUCCESS = 50
WIDTH_ERROR = 50

# The native window's width stays constant across all of IDLE/LISTENING/
# PROCESSING/SUCCESS/ERROR — see the "fixed outer footprint" note on
# _resize_and_reposition for why. It only changes size for CARD, which is a
# comparatively rare, deliberate transition.
SMALL_STATES_WIDTH = WIDTH_LISTENING  # the widest of the small states

BOTTOM_MARGIN = 28  # sits low near the screen edge, small gap above it

FRAME_MS = 16  # ~60fps

PAD = 32  # widget margin around the capsule, for the drop shadow's bleed
          # (handoff shadow: 0 24px 70px rgba(...) — a large soft floor shadow,
          # scaled down along with the rest of the capsule)

# Card (fallback) state — our own addition, not part of the handoff.
CARD_WIDTH = 300
CARD_HEIGHT = 180
CARD_RADIUS = theme.CARD_RADIUS

# Continuous idle "float" (handoff's cap-float: translateY 0 -> -5 -> 0, 6s
# ease-in-out infinite) — runs whenever the capsule is visible, in every state.
FLOAT_AMPLITUDE = 5.0
FLOAT_PERIOD_S = 6.0

# One-time entrance slide (not part of the handoff, but needed since the app
# still animates the capsule in on first launch rather than it just
# appearing instantly).
SLIDE_DISTANCE = CAPSULE_HEIGHT + BOTTOM_MARGIN + 2 * PAD + 30
SLIDE_DURATION_MS = 250

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


def _rgba(color: QColor) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


# -- exact vector icons (from the handoff's inline SVG paths, scaled) --------
# Reference viewBoxes are 12x12 (cancel) / 14x14 (confirm) at the handoff's
# full-size 30/34px buttons — coordinates below scale proportionally with
# our smaller button sizes so the icon-to-button ratio stays exact.

def _make_x_icon(box: int) -> QIcon:
    """Cancel icon: two strokes, from the handoff's 12x12 viewBox path
    (`M2.6 2.6 L9.4 9.4 M9.4 2.6 L2.6 9.4`), scaled to `box`."""
    s = box / 12.0
    pm = QPixmap(box, box)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(theme.CANCEL_ICON, max(1.0, 1.6 * s), Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(2.6 * s, 2.6 * s), QPointF(9.4 * s, 9.4 * s))
    p.drawLine(QPointF(9.4 * s, 2.6 * s), QPointF(2.6 * s, 9.4 * s))
    p.end()
    return QIcon(pm)


def _make_check_icon(box: int) -> QIcon:
    """Confirm icon: polyline from the handoff's 14x14 viewBox path
    (`M3 7.3 L5.8 10 L11 4.1`), scaled to `box`."""
    s = box / 14.0
    pm = QPixmap(box, box)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(theme.CONFIRM_ICON, max(1.0, 1.9 * s), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    path = QPainterPath()
    path.moveTo(3 * s, 7.3 * s)
    path.lineTo(5.8 * s, 10 * s)
    path.lineTo(11 * s, 4.1 * s)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


class _CancelButton(QPushButton):
    """30x30 circle, exact colors/hover from the handoff. No scale effect —
    only the confirm button gets that treatment, per spec."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(CANCEL_SIZE, CANCEL_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        icon_box = max(8, round(12 * CANCEL_SIZE / 30))
        self.setIcon(_make_x_icon(icon_box))
        self.setIconSize(QSize(icon_box, icon_box))
        self.setToolTip("Cancel")
        self.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {_rgba(theme.CANCEL_FILL)};"
            f"  border: none; border-radius: {CANCEL_SIZE // 2}px;"
            f"}}"
            f"QPushButton:hover {{ background-color: {_rgba(theme.CANCEL_FILL_HOVER)}; }}"
        )


class _ConfirmButton(QPushButton):
    """34x34 circle, white fill, exact drop shadow, and a real hover
    scale(1.07) — implemented as an animated geometry change since Qt
    stylesheets don't support CSS transforms."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(CONFIRM_SIZE, CONFIRM_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        icon_box = max(9, round(14 * CONFIRM_SIZE / 34))
        self.setIcon(_make_check_icon(icon_box))
        self.setIconSize(QSize(icon_box, icon_box))
        self.setToolTip("Confirm")
        self.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {theme.CONFIRM_FILL.name()};"
            f"  border: none; border-radius: {CONFIRM_SIZE // 2}px;"
            f"}}"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setOffset(0, 2)
        shadow.setColor(theme.CONFIRM_SHADOW)
        self.setGraphicsEffect(shadow)

        self._scale_anim = QPropertyAnimation(self, b"geometry", self)
        self._scale_anim.setDuration(150)
        self._scale_anim.setEasingCurve(QEasingCurve.OutCubic)

    def anchor_top_left(self, x: int, y: int) -> None:
        """Records where this button rests at 1.0x scale — used as the
        stable reference point for the hover-scale animation so repeated
        hovers don't drift."""
        self._base_rect = QRect(x, y, CONFIRM_SIZE, CONFIRM_SIZE)
        if not self.geometry().size().isEmpty() and self._scale_anim.state() != QPropertyAnimation.Running:
            self.setGeometry(self._base_rect)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._animate_to(1.07)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._animate_to(1.0)
        super().leaveEvent(event)

    def _animate_to(self, factor: float) -> None:
        base = getattr(self, "_base_rect", None)
        if base is None:
            return
        new_size = CONFIRM_SIZE * factor
        new_rect = QRect(0, 0, int(new_size), int(new_size))
        new_rect.moveCenter(base.center())
        self._scale_anim.stop()
        self._scale_anim.setStartValue(self.geometry())
        self._scale_anim.setEndValue(new_rect)
        self._scale_anim.start()


class PillState(enum.Enum):
    HIDDEN = "hidden"       # not shown at all (app paused / quitting)
    IDLE = "idle"           # resting, waiting for the hotkey — always visible otherwise
    LISTENING = "listening"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    CARD = "card"


class PillOverlay(QWidget):
    """Self-contained overlay widget. Call the enter_* methods to drive it."""

    dismissed = Signal()
    cancelled = Signal()   # user clicked the ✕ button during LISTENING
    confirmed = Signal()   # user clicked the ✓ button during LISTENING

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
        self._current_width = WIDTH_IDLE
        self._current_height = CAPSULE_HEIGHT
        self._start_time = time.monotonic()
        self._last_frame_ts = self._start_time
        self._listen_start_time = 0.0
        self._card_text = ""
        self._slide_offset = SLIDE_DISTANCE  # start off-screen (below), for the one-time entrance
        self._float_offset = 0.0

        # waveform energy model — see _tick_energy(); mirrors the handoff's
        # canvas math exactly, but driven by real mic amplitude (push_level)
        # rather than the handoff's simulated random "voice" envelope (the
        # handoff's own README calls for exactly this substitution).
        self._energy = 0.06
        self._target_amplitude = 0.0

        # processing shimmer state (our own addition)
        self._shimmer_pos = 0.0

        # success-checkmark / error-X glyph progress (0..1), animated via Qt
        # property below — only one of SUCCESS/ERROR is ever active at once,
        # so both glyphs share this single animated property.
        self._glyph_progress = 0.0

        self._glyph_hold_timer = QTimer(self)
        self._glyph_hold_timer.setSingleShot(True)
        self._glyph_hold_timer.timeout.connect(self.enter_idle)

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
        self._slide_anim = QPropertyAnimation(self, b"slideOffset", self)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(theme.DROP_SHADOW)
        self.setGraphicsEffect(shadow)

        self._build_action_buttons()
        self._build_timer_label()
        self._build_card_widgets()

        self.setWindowOpacity(0.0)
        self._resize_and_reposition()

    # -- listening-state cancel/confirm buttons + timer --------------------------

    def _build_action_buttons(self) -> None:
        self._cancel_button = _CancelButton(self)
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._cancel_button.hide()

        self._confirm_button = _ConfirmButton(self)
        self._confirm_button.clicked.connect(self._on_confirm_clicked)
        self._confirm_button.hide()

    def _build_timer_label(self) -> None:
        font_family = theme.MONO_FAMILY
        self._timer_label = QLabel("0:00", self)
        self._timer_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._timer_label.setFixedWidth(TIMER_MIN_W)
        self._timer_label.setStyleSheet(
            f"color: {theme.ACCENT.name()}; font-family: '{font_family}';"
            f"font-size: 9px; letter-spacing: 0.3px; background: transparent;"
        )
        self._timer_label.hide()

    def _on_cancel_clicked(self) -> None:
        self.cancelled.emit()

    def _on_confirm_clicked(self) -> None:
        self.confirmed.emit()

    def _position_children(self, rect: QRectF) -> None:
        """Positions cancel/canvas-area/timer/confirm per the handoff's exact
        flex layout (left padding, gap, ..., right padding) — only relevant
        while LISTENING; IDLE has no interactive children to position."""
        x = rect.left() + PAD_LEFT
        y_cancel = int(rect.center().y() - CANCEL_SIZE / 2)
        self._cancel_button.move(int(x), y_cancel)
        x += CANCEL_SIZE + GAP

        # canvas area (waveform) — not a real widget, just reserved space;
        # _canvas_rect() below recomputes this same span for painting.
        x += CANVAS_W + GAP

        y_timer = int(rect.center().y() - self._timer_label.height() / 2)
        self._timer_label.move(int(x), y_timer)
        x += TIMER_MIN_W + GAP

        y_confirm = int(rect.center().y() - CONFIRM_SIZE / 2)
        self._confirm_button.anchor_top_left(int(x), y_confirm)

    def _canvas_rect(self, rect: QRectF) -> QRectF:
        """The waveform's drawing area — positioned right after the cancel
        button (LISTENING) or right after the left padding alone (IDLE).
        IDLE uses its own narrower canvas width (see IDLE_CANVAS_W) so it
        reads as distinctly smaller, not the same waveform just sitting in a
        smaller pill."""
        x = rect.left() + PAD_LEFT
        width = IDLE_CANVAS_W
        if self._state == PillState.LISTENING:
            x += CANCEL_SIZE + GAP
            width = CANVAS_W
        y = rect.center().y() - CANVAS_H / 2
        return QRectF(x, y, width, CANVAS_H)

    # -- card child widgets (our own addition, not part of the handoff) ---------

    def _build_card_widgets(self) -> None:
        font_family = theme.ensure_font_loaded()

        self._card_container = QWidget(self)
        self._card_container.setAttribute(Qt.WA_TranslucentBackground, True)
        self._card_container.hide()

        self._subtle_label = QLabel("")
        self._subtle_label.setStyleSheet(
            f"color: {_rgba(theme.FG_DIM_38)}; font-family: '{font_family}';"
            f"font-size: 11px; font-weight: 600;"
        )
        self._subtle_label.setWordWrap(True)

        self._dismiss_button = _CancelButton()
        self._dismiss_button.clicked.connect(self._on_dismiss_clicked)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addWidget(self._subtle_label, 1)
        top_row.addWidget(self._dismiss_button, 0, Qt.AlignTop)

        self._text_view = QTextEdit()
        self._text_view.setReadOnly(True)
        self._text_view.setFrameStyle(0)
        self._text_view.setStyleSheet(
            f"QTextEdit {{"
            f"  background: transparent;"
            f"  color: {_rgba(theme.FG)};"
            f"  font-family: '{font_family}';"
            f"  font-size: 13px;"
            f"  border: none;"
            f"}}"
        )

        self._copy_button = QPushButton("Copy")
        self._copy_button.setCursor(Qt.PointingHandCursor)
        self._copy_button.setFixedHeight(28)
        self._copy_button.setStyleSheet(
            f"QPushButton {{"
            f"  color: {theme.CONFIRM_ICON.name()}; font-family: '{font_family}';"
            f"  font-size: 12px; font-weight: 600;"
            f"  border: none; border-radius: 14px; padding: 0 16px;"
            f"  background-color: {theme.CONFIRM_FILL.name()};"
            f"}}"
            f"QPushButton:hover {{ background-color: {_rgba(theme.FG_DIM_50)}; }}"
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

    def _get_slide_offset(self) -> float:
        return self._slide_offset

    def _set_slide_offset(self, value: float) -> None:
        self._slide_offset = value
        self._resize_and_reposition()

    slideOffset = Property(float, _get_slide_offset, _set_slide_offset)

    # -- layout ---------------------------------------------------------------

    def _outer_content_size(self) -> tuple[float, float]:
        """The native window's content-area footprint. Constant across all
        of IDLE/LISTENING/PROCESSING/SUCCESS/ERROR (sized for the widest of
        them, LISTENING) so those frequent transitions never resize the
        actual HWND — only repaint within it. Resizing a translucent layered
        window on every animation frame (~60/sec) was leaving stale
        DWM-composited pixels behind on Windows (confirmed: the "processing
        dots stuck on screen" bug went away once transitions between these
        states stopped triggering setFixedSize). CARD is large enough that
        it still gets a real resize — it's a rare, deliberate transition,
        not a per-cycle one, so it doesn't hit this problem in practice.
        """
        if self._state == PillState.CARD:
            return CARD_WIDTH, CARD_HEIGHT
        return SMALL_STATES_WIDTH, CAPSULE_HEIGHT

    def _content_rect(self) -> QRectF:
        outer_w, _outer_h = self._outer_content_size()
        # Pill is horizontally centered within the (possibly wider) fixed
        # outer footprint, so it still visually grows/shrinks around a
        # constant screen-center point even though the window itself doesn't
        # resize for these transitions.
        x = PAD + (outer_w - self._current_width) / 2
        return QRectF(x, PAD, self._current_width, self._current_height)

    def _resize_and_reposition(self) -> None:
        outer_w, outer_h = self._outer_content_size()
        total_w = outer_w + PAD * 2
        total_h = outer_h + PAD * 2
        self.setFixedSize(int(total_w), int(total_h))
        screen = self.screen() or self.window().screen()
        geo = screen.availableGeometry() if screen else None
        if geo is not None:
            x = geo.center().x() - self.width() // 2
            y = (
                geo.bottom()
                - int(total_h)
                - BOTTOM_MARGIN
                + int(self._slide_offset)
                + int(self._float_offset)
            )
            self.move(x, y)
        rect = self._content_rect()
        if self._card_container.isVisible():
            self._card_container.setGeometry(
                int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height())
            )
        if self._state == PillState.LISTENING:
            self._position_children(rect)
        self.update()

    # -- public state transitions ---------------------------------------------

    def enter_idle(self) -> None:
        """Resting state — always the state to return to when nothing else
        is happening (not a "hidden" state; see HIDDEN for actually hiding
        the capsule, e.g. on pause/quit)."""
        self._leave_card()
        was_hidden = self._state == PillState.HIDDEN
        self._state = PillState.IDLE
        self._hide_action_buttons()
        self._glyph_hold_timer.stop()
        self._show_animated(is_first_show=was_hidden)
        self._animate_size(WIDTH_IDLE, CAPSULE_HEIGHT)

    def enter_listening(self) -> None:
        self._leave_card()
        was_hidden = self._state == PillState.HIDDEN
        self._state = PillState.LISTENING
        self._listen_start_time = time.monotonic()
        self._timer_label.setText("0:00")
        self._show_animated(is_first_show=was_hidden)
        self._animate_size(WIDTH_LISTENING, CAPSULE_HEIGHT)
        self._glyph_hold_timer.stop()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        _set_click_through(int(self.winId()), click_through=False)
        self._cancel_button.show()
        self._confirm_button.show()
        self._timer_label.show()
        self._position_children(self._content_rect())

    def enter_processing(self) -> None:
        self._leave_card()
        self._state = PillState.PROCESSING
        self._shimmer_pos = 0.0
        self._animate_size(WIDTH_PROCESSING, CAPSULE_HEIGHT)
        self._hide_action_buttons()

    def enter_success(self, hold_ms: int = 900) -> None:
        self._leave_card()
        self._state = PillState.SUCCESS
        self._animate_size(WIDTH_SUCCESS, CAPSULE_HEIGHT)
        self._start_glyph_anim(hold_ms)
        self._hide_action_buttons()

    def enter_error(self, hold_ms: int = 900) -> None:
        """Brief error flash for backend/network failures — mirrors enter_success."""
        self._leave_card()
        was_hidden = self._state == PillState.HIDDEN
        self._state = PillState.ERROR
        self._show_animated(is_first_show=was_hidden)
        self._animate_size(WIDTH_ERROR, CAPSULE_HEIGHT)
        self._start_glyph_anim(hold_ms)
        self._hide_action_buttons()

    def enter_card(self, text: str, subtle_label: str = "") -> None:
        """Expand into an interactive card showing `text` in full, with Copy
        and dismiss actions. Stays open until dismissed or superseded by a
        new enter_* call (e.g. a new recording) — see _leave_card.
        """
        self._glyph_hold_timer.stop()
        self._hide_action_buttons()
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

    def hide_capsule(self) -> None:
        """Actually hides the capsule entirely (app paused or quitting) —
        distinct from enter_idle(), which keeps it visible at rest."""
        self._hide_action_buttons()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if self.winId():
            _set_click_through(int(self.winId()), click_through=True)
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.setDuration(SLIDE_DURATION_MS)
        self._opacity_anim.finished.connect(self._on_faded_out)
        self._opacity_anim.start()

    def _hide_action_buttons(self) -> None:
        self._cancel_button.hide()
        self._confirm_button.hide()
        self._timer_label.hide()

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
        self.enter_idle()

    def _on_copy_clicked(self) -> None:
        if self._card_text:
            QApplication.clipboard().setText(self._card_text)
        self._copy_button.setText("Copied!")
        QTimer.singleShot(1200, lambda: self._copy_button.setText("Copy"))

    def push_level(self, level: float) -> None:
        """Feed a 0..1 microphone amplitude sample (called from audio thread-safe slot)."""
        self._target_amplitude = max(0.0, min(1.0, level))

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

    def _show_animated(self, is_first_show: bool = False) -> None:
        if not self.isVisible():
            self.show()
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setDuration(180)
        self._opacity_anim.start()

        # One-time slide-up from below the screen edge — only on the very
        # first appearance (app launch); afterward the capsule stays visible
        # permanently (idle <-> listening <-> ... transitions in place).
        if is_first_show and self._slide_offset > 0:
            self._slide_anim.stop()
            self._slide_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._slide_anim.setStartValue(self._slide_offset)
            self._slide_anim.setEndValue(0.0)
            self._slide_anim.setDuration(SLIDE_DURATION_MS)
            self._slide_anim.start()

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
        now = time.monotonic()
        dt = min(now - self._last_frame_ts, 0.05)
        self._last_frame_ts = now

        if self._state != PillState.HIDDEN:
            self._tick_float(now)

        if self._state == PillState.LISTENING:
            self._tick_energy(dt)
            self._tick_timer(now)
            self.update()
        elif self._state == PillState.IDLE:
            self._tick_energy(dt)
            self.update()
        elif self._state == PillState.PROCESSING:
            self._shimmer_pos = (self._shimmer_pos + 0.02) % 1.6
            self.update()

    def _tick_float(self, now: float) -> None:
        # handoff: cap-float — translateY(0 -> -5px -> 0), 6s ease-in-out
        # infinite. A raised cosine is a smooth, continuous stand-in for
        # that 0%/50%/100% keyframe easing.
        t = now - self._start_time
        phase = (t % FLOAT_PERIOD_S) / FLOAT_PERIOD_S
        new_offset = -FLOAT_AMPLITUDE * (0.5 - 0.5 * math.cos(phase * 2 * math.pi))
        if new_offset != self._float_offset:
            self._float_offset = new_offset
            self._resize_and_reposition()

    def _tick_energy(self, dt: float) -> None:
        # handoff's exact energy model: target = 0.62 * voice * intensity
        # while listening, 0 otherwise; eased at rate dt*8. "voice" here is
        # real mic amplitude (self._target_amplitude via push_level), not
        # the handoff's simulated random envelope — per the handoff's own
        # instruction to substitute real mic input in production.
        intensity = 1.0
        target = 0.62 * self._target_amplitude * intensity if self._state == PillState.LISTENING else 0.0
        self._energy += (target - self._energy) * min(dt * 8, 1.0)

    def _tick_timer(self, now: float) -> None:
        elapsed = int(now - self._listen_start_time)
        self._timer_label.setText(f"{elapsed // 60}:{elapsed % 60:02d}")

    # -- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pill_rect = self._content_rect()
        is_card = self._state == PillState.CARD
        radius = CARD_RADIUS if is_card else pill_rect.height() / 2
        path = QPainterPath()
        path.addRoundedRect(pill_rect, radius, radius)

        self._paint_shell(painter, path, pill_rect)

        if is_card:
            return  # content is the real child widgets (_card_container), not custom-painted

        painter.save()
        painter.setClipPath(path)
        if self._state in (PillState.IDLE, PillState.LISTENING):
            self._paint_waveform(painter, self._canvas_rect(pill_rect))
        elif self._state == PillState.PROCESSING:
            self._paint_shimmer(painter, pill_rect)
        elif self._state == PillState.SUCCESS:
            self._paint_success(painter, pill_rect)
        elif self._state == PillState.ERROR:
            self._paint_error(painter, pill_rect)
        painter.restore()

    def _paint_shell(self, painter: QPainter, path: QPainterPath, rect: QRectF) -> None:
        """The glass capsule shell — approximating the handoff's stacked
        inset box-shadows as separate painted passes (Qt has no direct
        multi-layer inset-shadow primitive):
          1. vertical gradient fill (the shell's own background)
          2. floor shade: soft dark gradient at the bottom (inset bottom shade)
          3. rim hairline: full-perimeter 1px border (inset hairline rim)
          4. top highlight: a bright short arc along the top edge (inset top highlight)
        The big soft floating drop-shadow is a QGraphicsDropShadowEffect set
        on the whole widget in __init__, not painted here. If Acrylic
        blur-behind engaged successfully, this fill composites on top of
        real blurred desktop content instead of just flat color.
        """
        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        fill.setColorAt(0.0, theme.SHELL_TOP)
        fill.setColorAt(1.0, theme.SHELL_BOTTOM)
        painter.fillPath(path, fill)

        painter.save()
        painter.setClipPath(path)
        floor_rect = QRectF(rect.left(), rect.bottom() - rect.height() * 0.4, rect.width(), rect.height() * 0.4)
        floor_gradient = QLinearGradient(floor_rect.topLeft(), floor_rect.bottomLeft())
        floor_gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        floor_gradient.setColorAt(1.0, theme.FLOOR_SHADE)
        painter.fillRect(floor_rect, floor_gradient)
        painter.restore()

        painter.setPen(QPen(theme.RIM_HAIRLINE, 1))
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)
        painter.setPen(QPen(theme.RIM_TOP_HIGHLIGHT, 1.2))
        painter.drawLine(
            QPointF(rect.left() + rect.height() / 2, rect.top() + 0.6),
            QPointF(rect.right() - rect.height() / 2, rect.top() + 0.6),
        )
        painter.restore()

    def _paint_waveform(self, painter: QPainter, rect: QRectF) -> None:
        """Exact port of the handoff's canvas draw() for the 'line' visualizer."""
        cy = rect.center().y()
        w = rect.width()
        t = time.monotonic() - self._start_time

        if self._energy < 0.04:
            self._paint_flat_line(painter, rect, cy)
            return

        amp = rect.height() * 0.34 * min(self._energy / 0.55, 1.0) + rect.height() * 0.03

        # soft glow pass approximating the handoff's canvas shadowBlur,
        # applied to the primary stroke only
        glow_alpha = 0.5 * min(self._energy / 0.4, 1.0)
        if glow_alpha > 0:
            self._draw_wave_stroke(
                painter, rect, cy, t, amp, speed=2.6, freq=7, phase=0,
                alpha=0.95, width=6.0, extra_glow_alpha=glow_alpha,
            )
        self._draw_wave_stroke(painter, rect, cy, t, amp, speed=2.6, freq=7, phase=0, alpha=0.95, width=2.0)
        self._draw_wave_stroke(
            painter, rect, cy, t, amp * 0.6, speed=3.4, freq=11, phase=1.7, alpha=0.3, width=1.4
        )

    def _draw_wave_stroke(
        self, painter: QPainter, rect: QRectF, cy: float, t: float, amp: float,
        speed: float, freq: float, phase: float, alpha: float, width: float,
        extra_glow_alpha: float | None = None,
    ) -> None:
        w = rect.width()
        steps = 60
        path = QPainterPath()
        for i in range(steps + 1):
            x = rect.left() + (i / steps) * w
            win = math.sin(math.pi * (i / steps))
            xf = (i / steps) * w  # matches handoff's "x" (0..W), not absolute screen x
            y = (
                cy
                + math.sin(xf / w * freq + t * speed + phase) * amp * win
                + math.sin(xf / w * freq * 2.3 + t * speed * 1.4) * amp * 0.4 * win
            )
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        use_alpha = extra_glow_alpha if extra_glow_alpha is not None else alpha
        accent_soft = QColor(theme.ACCENT)
        accent_soft.setAlphaF(max(0.0, min(1.0, use_alpha * 0.15)))
        accent_mid = QColor(theme.ACCENT)
        accent_mid.setAlphaF(max(0.0, min(1.0, use_alpha)))
        white_end = QColor(255, 255, 255)
        white_end.setAlphaF(max(0.0, min(1.0, use_alpha * 0.85)))
        gradient.setColorAt(0.0, accent_soft)
        gradient.setColorAt(0.5, accent_mid)
        gradient.setColorAt(1.0, white_end)

        painter.setPen(QPen(gradient, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(path)

    def _paint_flat_line(self, painter: QPainter, rect: QRectF, cy: float) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        accent_soft = QColor(theme.ACCENT)
        accent_soft.setAlphaF(0.15)
        accent_mid = QColor(theme.ACCENT)
        accent_mid.setAlphaF(0.6)
        white_end = QColor(255, 255, 255)
        white_end.setAlphaF(0.5)
        gradient.setColorAt(0.0, accent_soft)
        gradient.setColorAt(0.5, accent_mid)
        gradient.setColorAt(1.0, white_end)
        painter.setPen(QPen(gradient, 1.8, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(rect.left(), cy), QPointF(rect.right(), cy))

    def _paint_shimmer(self, painter: QPainter, rect: QRectF) -> None:
        dot_radius = 2.6
        gap = 11.0
        cy = rect.center().y()
        cx = rect.center().x()
        for i, dx in enumerate((-gap, 0.0, gap)):
            phase = self._shimmer_pos * math.tau + i * (math.tau / 3)
            scale = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(phase))
            color = QColor(theme.ACCENT)
            color.setAlphaF(min(1.0, 0.35 + 0.5 * scale))
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            r = dot_radius * (0.7 + 0.5 * scale)
            painter.drawEllipse(QPointF(cx + dx, cy), r, r)

    def _paint_success(self, painter: QPainter, rect: QRectF) -> None:
        progress = self._glyph_progress
        if progress <= 0:
            return
        cx, cy = rect.center().x(), rect.center().y()
        radius = 9.0
        color = QColor(theme.SUCCESS)
        color.setAlphaF(min(1.0, progress + 0.15))
        pen = QPen(color, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
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
        radius = 8.5
        color = QColor(theme.ERROR)
        color.setAlphaF(min(1.0, progress + 0.15))
        pen = QPen(color, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
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
