"""Design tokens for the capsule — ported exactly from the design handoff at
D:\\ritely\\design_handoff_audio_capsule\\ (README.md + Audio Capsule.dc.html).
That document is the source of truth: colors, shadows, dimensions, and the
canvas waveform math are all copied from it, not reinterpreted. Where CSS has
no direct Qt equivalent (multi-layer inset box-shadow, backdrop-filter), see
overlay.py for how each is approximated/implemented.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFontDatabase

# -- accent (from the handoff's --accent / --accent-ring) --------------------

ACCENT = QColor("#8b8bff")
ACCENT_RING = QColor(139, 139, 255, 115)  # accent @ 0.45

# -- capsule shell gradient (background: linear-gradient(180deg, ...)) -------
# Fully opaque (alpha 255) — explicit user direction: the capsule interior
# must read as solid black, not see-through. The handoff's own 0.72/0.80
# alpha values were for a true backdrop-blur surface (blurring what's behind
# it); since real blur-behind doesn't work for this window (see git history/
# overlay.py notes), a translucent-but-not-blurred fill just looked like a
# washed-out gray box instead — solid color is both what was asked for and
# the more correct look given no real blur is happening underneath.
SHELL_TOP = QColor(38, 38, 44, 255)
SHELL_BOTTOM = QColor(16, 16, 19, 255)

# Approximating the handoff's stacked inset box-shadows (Qt has no direct
# multi-layer inset-shadow primitive — each is painted as its own pass in
# overlay.py's _paint_shell):
RIM_TOP_HIGHLIGHT = QColor(255, 255, 255, 41)  # inset 0 1.2px 0 rgba(255,255,255,0.16)
RIM_HAIRLINE = QColor(255, 255, 255, 18)       # inset 0 0 0 1px rgba(255,255,255,0.07)
FLOOR_SHADE = QColor(0, 0, 0, 71)              # inset 0 -10px 24px rgba(0,0,0,0.28)
DROP_SHADOW = QColor(0, 0, 0, 158)             # 0 24px 70px -12px rgba(0,0,0,0.62)

# -- cancel button (30x30, left, listening only) ------------------------------

CANCEL_FILL = QColor(255, 255, 255, 23)        # rgba(255,255,255,0.09)
CANCEL_FILL_HOVER = QColor(255, 255, 255, 43)  # rgba(255,255,255,0.17)
CANCEL_ICON = QColor(255, 255, 255, 199)       # rgba(255,255,255,0.78)

# -- confirm button (34x34, right, listening only) ----------------------------

CONFIRM_FILL = QColor(255, 255, 255, 255)      # #fff
CONFIRM_SHADOW = QColor(0, 0, 0, 89)           # rgba(0,0,0,0.35)
CONFIRM_ICON = QColor("#18181b")

# -- text ----------------------------------------------------------------------

FG = QColor(255, 255, 255, 209)        # rgba(255,255,255,0.82) — state labels
FG_DIM_50 = QColor(255, 255, 255, 128)
FG_DIM_40 = QColor(255, 255, 255, 102)
FG_DIM_38 = QColor(255, 255, 255, 97)
FG_DIM_30 = QColor(255, 255, 255, 77)

# States our app has that the handoff doesn't define (backend processing
# feedback) — not covered by the design doc, kept from the prior iteration.
SUCCESS = QColor("#3ddc91")
ERROR = QColor("#ff6b6b")

# -- shape/dimension tokens (all exact, from the handoff) ---------------------

CAPSULE_RADIUS = 999  # effectively fully rounded at any height we use

# The handoff specifies 60px height / 176x42 canvas / 30-34px buttons — sized
# for a hero design-reference mockup. Scaled down for the actual shipped
# overlay (explicit user direction: keep the exact look, shrink the size)
# while keeping every proportion, gradient, and animation identical. Pushed
# smaller again per follow-up feedback, and IDLE now uses its own, much
# narrower canvas width than LISTENING (also explicit feedback: idle should
# read as clearly, distinctly smaller, not just a hair narrower) — height
# stays identical between the two on purpose (see overlay.py's
# SMALL_STATES_WIDTH / _outer_content_size notes: the native window's height
# must stay constant across idle/listening/processing/success/error to avoid
# the DWM resize-ghosting bug, so only widths can differ between them).
_SCALE = 0.47
CAPSULE_HEIGHT = round(60 * _SCALE)      # 28
PAD_LEFT = round(18 * _SCALE)            # 8
PAD_RIGHT = round(22 * _SCALE)           # 10
GAP = round(16 * _SCALE)                 # 8

CANCEL_SIZE = round(30 * _SCALE)         # 14
CONFIRM_SIZE = round(34 * _SCALE)        # 16
CANVAS_WIDTH = round(176 * _SCALE)       # 83   — listening's canvas
IDLE_CANVAS_WIDTH = round(83 * 0.55)     # 46   — much narrower, idle only
CANVAS_HEIGHT = round(42 * _SCALE)       # 20
TIMER_MIN_WIDTH = 22  # not strictly scaled — needs room to fit "0:00" without clipping

CARD_RADIUS = 20  # our own fallback-card state, not part of the handoff

# -- font ----------------------------------------------------------------------

_FONT_FILE = Path(__file__).parent / "assets" / "fonts" / "PlusJakartaSans-Variable.ttf"
_FONT_FAMILY = "Plus Jakarta Sans"
MONO_FAMILY = "Consolas"  # handoff calls for ui-monospace/SF Mono/Menlo; Consolas is
                          # the closest universally-available Windows equivalent
_font_loaded = False


def ensure_font_loaded() -> str:
    """Registers the bundled Plus Jakarta Sans variable font with Qt exactly
    once. Returns the font family name to use — falls back to Segoe UI if the
    bundled file is missing (e.g. a stripped-down dev checkout) or fails to
    load, so the app never crashes over a missing font asset.
    """
    global _font_loaded
    if _font_loaded:
        return _FONT_FAMILY
    _font_loaded = True
    if not _FONT_FILE.exists():
        return "Segoe UI"
    font_id = QFontDatabase.addApplicationFont(str(_FONT_FILE))
    if font_id == -1:
        return "Segoe UI"
    families = QFontDatabase.applicationFontFamilies(font_id)
    return families[0] if families else "Segoe UI"
