"""Insert polished text at the current keyboard focus.

Tier 1: clipboard paste (Ctrl+V) — preserves the target app's normal paste
handling (undo stack, formatting rules, etc.) and works everywhere paste
works. The user's prior clipboard contents are snapshotted first and
restored afterward (once success is confirmed — see ClipboardStage).

Tier 2 (only reached if tier 1 can't be verified, or SendInput itself
fails): simulated typing via winkeys.type_unicode_text, which types literal
Unicode characters (KEYEVENTF_UNICODE) rather than resolving through any
VK/shortcut table — so typed content can never trigger a shortcut. Slower,
but works in apps that consume/ignore synthetic Ctrl+V without visibly
failing (confirmed necessary for some apps during development).

Tier 3: app.py falls back to the on-screen card if neither tier can be
verified — see ClipboardStage.keep_polished_on_clipboard().

app.py's AppController drives the full tiered pipeline (with the pre-paste
delay and per-tier verification) via stage_clipboard()/ClipboardStage; the
simpler attempt_paste()/inject() functions below are single-shot
equivalents kept for callers that don't need the full pipeline.

Text passed here must already be sanitized by the caller (sanitize.py) —
this module does not sanitize, keeping the one security-relevant transform
in one obvious place rather than duplicated/hidden here.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import QApplication

from . import winkeys

DEFAULT_RESTORE_DELAY_MS = 250


class InjectError(Exception):
    """Raised when the clipboard-paste path fails at the OS level."""


def _clipboard() -> QClipboard:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("inject.py requires a running QApplication")
    return app.clipboard()


def _snapshot_clipboard(clipboard: QClipboard) -> QMimeData:
    """Deep-copy every format out of the current clipboard contents.

    Copying the data out (rather than holding a reference to
    clipboard.mimeData()) matters because Qt doesn't guarantee that object
    stays valid/queryable once the clipboard owner changes.
    """
    src = clipboard.mimeData()
    snapshot = QMimeData()
    for fmt in src.formats():
        snapshot.setData(fmt, src.data(fmt))
    return snapshot


def _restore_clipboard(clipboard: QClipboard, snapshot: QMimeData) -> None:
    clipboard.setMimeData(snapshot)


def inject_text(text: str, restore_delay_ms: int = DEFAULT_RESTORE_DELAY_MS) -> None:
    """Paste `text` at the current focus via the clipboard, then restore it.

    Raises InjectError if the OS-level paste keystroke couldn't be sent at
    all (SendInput rejected the synthetic event) — in that case the
    clipboard is restored immediately since nothing was pasted.
    """
    clipboard = _clipboard()
    snapshot = _snapshot_clipboard(clipboard)
    clipboard.setText(text)

    try:
        winkeys.send_ctrl_v()
    except OSError as exc:
        _restore_clipboard(clipboard, snapshot)
        raise InjectError(f"paste keystroke failed: {exc}") from exc

    # The target app needs a moment to actually read the clipboard on paste;
    # scheduling the restore (rather than a blocking sleep) keeps the Qt
    # main thread — and the pill's animation — responsive in the meantime.
    QTimer.singleShot(restore_delay_ms, lambda: _restore_clipboard(clipboard, snapshot))


def inject_text_typed(text: str, per_char_delay_ms: float = 2) -> None:
    """Fallback: types `text` as literal Unicode characters, no clipboard involved."""
    winkeys.type_unicode_text(text, per_char_delay_ms=per_char_delay_ms)


def inject(text: str) -> str:
    """Insert `text` at the current focus, preferring paste, falling back to typing.

    Returns "clipboard" or "typed" depending on which path succeeded.

    This is the simple, no-verification convenience path: the fallback to
    typing only fires when SendInput itself raises (a real OS-level
    failure). It does not know whether the target actually accepted the
    paste. For the richer flow — checking whether a valid editable target
    exists at all, and best-effort verifying the text actually landed via
    UI Automation — see attempt_paste() below plus focus_detect.py; app.py
    uses that combination for the main recording flow, and falls back to
    an on-screen card when detection/verification comes back negative or
    uncertain, so text is never silently lost even where this simpler
    function's fallback trigger wouldn't have caught it.
    """
    try:
        inject_text(text)
        return "clipboard"
    except InjectError:
        inject_text_typed(text)
        return "typed"


@dataclass
class PasteAttempt:
    """Result of attempt_paste() — the clipboard is left in an intermediate
    state (holding `text`) until the caller decides what to do with it.
    """

    mode: str  # "clipboard" or "typed"
    _clipboard: QClipboard
    _snapshot: QMimeData | None  # None when mode == "typed" — nothing was touched

    def restore_clipboard(self) -> None:
        """Restore the clipboard to what it held before this attempt."""
        if self._snapshot is not None:
            _restore_clipboard(self._clipboard, self._snapshot)

    def keep_polished_on_clipboard(self, text: str) -> None:
        """Leave `text` on the clipboard as a safety net (no restore)."""
        self._clipboard.setText(text)


def attempt_paste(text: str) -> PasteAttempt:
    """Attempt injection without deciding the clipboard's final state.

    Unlike inject(), this doesn't auto-restore the clipboard — the caller
    (app.py) decides afterward, based on UI Automation verification,
    whether to restore the user's prior clipboard contents (confirmed
    success) or leave `text` on the clipboard as a safety net (no valid
    target, or the paste couldn't be confirmed) via
    PasteAttempt.keep_polished_on_clipboard().

    This is the simple, single-step version (no pre-paste delay, no typed
    fallback tier) — kept for callers that don't need the full tiered
    pipeline. See ClipboardStage/stage_clipboard below for that.
    """
    clipboard = _clipboard()
    snapshot = _snapshot_clipboard(clipboard)
    clipboard.setText(text)

    try:
        winkeys.send_ctrl_v()
        return PasteAttempt(mode="clipboard", _clipboard=clipboard, _snapshot=snapshot)
    except OSError:
        _restore_clipboard(clipboard, snapshot)  # nothing was pasted, restore immediately
        inject_text_typed(text)
        return PasteAttempt(mode="typed", _clipboard=clipboard, _snapshot=None)


@dataclass
class ClipboardStage:
    """Clipboard has been set to the target text; no keystroke sent yet.

    Split from a single synchronous call (unlike attempt_paste/PasteAttempt)
    specifically so the caller can insert a real delay — driven by a Qt
    QTimer, not a blocking sleep — between staging the clipboard and
    sending Ctrl+V. Some apps (observed: this was never proven to be the
    root cause for Word/Notion specifically, but is cheap insurance and
    matches how a human pasting would naturally have a beat between
    Ctrl+C-ing content into place and pressing Ctrl+V) may not have fully
    processed a clipboard-content-changed notification the instant it
    changes.
    """

    _clipboard: QClipboard
    _snapshot: QMimeData

    def send_paste_keystroke(self) -> bool:
        """Send Ctrl+V. Returns False on an OS-level SendInput failure
        (caller should treat that as "clipboard tier unavailable, skip
        straight to the typed tier") rather than raising.
        """
        try:
            winkeys.send_ctrl_v()
            return True
        except OSError:
            return False

    def restore_clipboard(self) -> None:
        _restore_clipboard(self._clipboard, self._snapshot)

    def keep_polished_on_clipboard(self, text: str) -> None:
        self._clipboard.setText(text)


def stage_clipboard(text: str) -> ClipboardStage:
    """First half of the tiered pipeline: snapshot + set the clipboard,
    but don't send Ctrl+V yet — see ClipboardStage.send_paste_keystroke().
    """
    clipboard = _clipboard()
    snapshot = _snapshot_clipboard(clipboard)
    clipboard.setText(text)
    return ClipboardStage(_clipboard=clipboard, _snapshot=snapshot)
