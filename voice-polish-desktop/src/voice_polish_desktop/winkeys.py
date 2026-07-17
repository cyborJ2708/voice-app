"""Thin ctypes wrapper around Win32 SendInput.

Used for two things:
  - simulating the paste shortcut (Ctrl+V) to inject polished text
  - a "simulated typing" fallback that types literal Unicode characters
    when clipboard paste isn't accepted by the focused control

Kept as stdlib ctypes rather than pulling in `pyautogui` (which drags in
pygetwindow/pymsgbox/pytweening and does far more than we need) or the
`keyboard` package (system-wide hook, see hotkey.py's docstring).
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_LWIN = 0x5B
VK_V = 0x56

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    # Must mirror the *real* Win32 INPUT union (mi/ki/hi), not just the
    # keyboard variant we use — SendInput validates cbSize against the true
    # struct size, so a union missing members silently rejects every event.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def _send(inputs: list[INPUT]) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.set_last_error(0)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise OSError(f"SendInput only accepted {sent}/{n} events (error {err}: {ctypes.FormatError(err)})")


def _key_input(vk: int, key_up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=ki))


def _unicode_input(char: str, key_up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    ki = KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=ki))


def send_combo(vks: list[int], hold_ms: float = 12) -> None:
    """Press all vks down in order, hold briefly, release in reverse order."""
    _send([_key_input(vk, key_up=False) for vk in vks])
    time.sleep(hold_ms / 1000)
    _send([_key_input(vk, key_up=True) for vk in reversed(vks)])


def send_ctrl_v() -> None:
    send_combo([VK_CONTROL, VK_V])


def type_unicode_text(text: str, per_char_delay_ms: float = 2) -> None:
    """Type literal characters via KEYEVENTF_UNICODE — no shortcuts can fire.

    Each character is sent as a raw Unicode key event, not looked up against
    any keyboard layout / VK table, so there's no path from character content
    to a modifier combo or shortcut being triggered.
    """
    for ch in text:
        if ch == "\n":
            _send([_key_input(0x0D, key_up=False)])
            _send([_key_input(0x0D, key_up=True)])
        else:
            _send([_unicode_input(ch, key_up=False)])
            _send([_unicode_input(ch, key_up=True)])
        if per_char_delay_ms:
            time.sleep(per_char_delay_ms / 1000)
