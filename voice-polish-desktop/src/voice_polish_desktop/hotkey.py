"""Global hotkey via the Win32 RegisterHotKey API.

Deliberately implemented with stdlib `ctypes` only, rather than a
third-party global-hotkey package: most of those (e.g. `keyboard`) work by
installing a low-level system-wide keyboard hook, which is a much larger
attack surface (it sees *every* keystroke system-wide, not just the
registered combo) and routinely gets flagged by antivirus/EDR as
keylogger-like behavior. RegisterHotKey only ever tells us about the one
combination we asked for.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

HOTKEY_ID = 1  # the app only ever registers one global hotkey at a time

MOD_LABELS = {
    MOD_CONTROL: "Ctrl",
    MOD_WIN: "Win",
    MOD_ALT: "Alt",
    MOD_SHIFT: "Shift",
}
MOD_ORDER = (MOD_CONTROL, MOD_WIN, MOD_ALT, MOD_SHIFT)

# Qt::Key values that don't line up numerically with their Win32 VK code.
# (Letters, digits, and Space *do* line up — see qt_key_to_vk below.)
_SPECIAL_VK = {
    Qt.Key_Tab: 0x09,
    Qt.Key_Backspace: 0x08,
    Qt.Key_Return: 0x0D,
    Qt.Key_Enter: 0x0D,
    Qt.Key_Escape: 0x1B,
    Qt.Key_Insert: 0x2D,
    Qt.Key_Delete: 0x2E,
    Qt.Key_Home: 0x24,
    Qt.Key_End: 0x23,
    Qt.Key_PageUp: 0x21,
    Qt.Key_PageDown: 0x22,
    Qt.Key_Left: 0x25,
    Qt.Key_Up: 0x26,
    Qt.Key_Right: 0x27,
    Qt.Key_Down: 0x28,
    **{getattr(Qt, f"Key_F{i}"): 0x6F + i for i in range(1, 13)},
}


def qt_key_to_vk(key: int) -> Optional[int]:
    """Best-effort Qt::Key -> Win32 virtual-key translation for hotkey capture."""
    if key in _SPECIAL_VK:
        return _SPECIAL_VK[key]
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9 or key == Qt.Key_Space:
        return key
    return None


@dataclass(frozen=True)
class HotkeySpec:
    modifiers: int  # bitmask of MOD_*
    vk: int

    @property
    def label(self) -> str:
        parts = [MOD_LABELS[m] for m in MOD_ORDER if self.modifiers & m]
        parts.append(_vk_label(self.vk))
        return "+".join(parts)

    def to_json(self) -> dict:
        return {"modifiers": self.modifiers, "vk": self.vk}

    @staticmethod
    def from_json(data: dict) -> "HotkeySpec":
        return HotkeySpec(modifiers=int(data["modifiers"]), vk=int(data["vk"]))


def _vk_label(vk: int) -> str:
    for key, mapped in _SPECIAL_VK.items():
        if mapped == vk:
            return {
                0x09: "Tab", 0x08: "Backspace", 0x0D: "Enter", 0x1B: "Esc",
                0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End",
                0x21: "PageUp", 0x22: "PageDown", 0x25: "Left", 0x26: "Up",
                0x27: "Right", 0x28: "Down",
            }.get(mapped, f"F{mapped - 0x6F}" if 0x70 <= mapped <= 0x7B else str(mapped))
    if vk == 0x20:
        return "Space"
    return chr(vk)


DEFAULT_HOTKEY = HotkeySpec(modifiers=MOD_CONTROL | MOD_WIN, vk=0x20)  # Ctrl+Win+Space


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey: Callable[[int], None]) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, eventType, message):  # noqa: N802 (Qt override)
        if eventType == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self._on_hotkey(msg.wParam)
        return False, 0


class HotkeyManager(QObject):
    """Owns the single global hotkey registration and emits `triggered` on press.

    RegisterHotKey delivers WM_HOTKEY as a *thread* message (hWnd=None), so it
    rides along on Qt's own event loop via a native event filter instead of
    needing a separate message-pump thread.
    """

    triggered = Signal()
    registration_failed = Signal(str)

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self._filter = _NativeFilter(self._on_hotkey)
        app.installNativeEventFilter(self._filter)
        self._registered = False
        self.current: Optional[HotkeySpec] = None

    def register(self, spec: HotkeySpec) -> bool:
        self.unregister()
        ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, spec.modifiers | MOD_NOREPEAT, spec.vk))
        if ok:
            self._registered = True
            self.current = spec
        else:
            self.registration_failed.emit(
                f"Could not register {spec.label} — it may already be in use by "
                "another app or Windows itself."
            )
        return ok

    def unregister(self) -> None:
        if self._registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = False

    def _on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == HOTKEY_ID:
            self.triggered.emit()
