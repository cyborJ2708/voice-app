"""Detects whether an editable text control currently has keyboard focus,
and best-effort verifies that injected text actually landed — via Windows
UI Automation (UIA), not just the classic Win32 HWND/class-name approach.

Why UIA and not plain Win32: most modern apps (browsers, Electron apps,
UWP apps) render their text fields *inside* a single HWND via their own
compositor, so there's no separate child window to inspect at the Win32
level. UIA sees into those apps' accessibility trees instead, which is
exactly the coverage this feature needs. Confirmed during development
with real elements: a Qt QLineEdit reports itself as UIA control type
50032 (not the "expected" UIA_EditControlTypeId) yet is still correctly
identified as editable via pattern support — which is why detection here
is pattern-based (ValuePattern / TextEditPattern), not control-type-based;
relying on control type alone would have missed it.

Detection is deliberately conservative: any COM failure, unsupported
pattern, or ambiguous state resolves to "not editable" / "can't verify"
rather than guessing — the caller's designed response to that is always
the safe one (show the fallback card), never data loss.
"""
from __future__ import annotations

import ctypes

import comtypes
import comtypes.client

_uia = None
_mod = None
_initialized = False

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def initialize() -> None:
    """Call once, from the Qt main thread, before any other function here."""
    global _uia, _mod, _initialized
    if _initialized:
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # already initialized on this thread — fine
    _mod = comtypes.client.GetModule("UIAutomationCore.dll")
    _uia = comtypes.client.CreateObject(_mod.CUIAutomation, interface=_mod.IUIAutomation)
    _initialized = True


def _get_focused_element():
    if not _initialized:
        initialize()
    return _uia.GetFocusedElement()


def get_focused_process_name() -> str | None:
    """Best-effort: the executable name (e.g. "notion.exe") owning the
    currently-focused element. Used only for per-app delay tuning and
    opt-in debug logging (process name only — never window text/content).
    Returns None on any failure rather than raising, matching this
    module's conservative style.
    """
    try:
        element = _get_focused_element()
        if element is None:
            return None
        pid = element.CurrentProcessId
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buf_len = ctypes.c_uint32(260)
            buf = ctypes.create_unicode_buffer(260)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return None
            path = buf.value
            return path.rsplit("\\", 1)[-1].lower() if path else None
        finally:
            _kernel32.CloseHandle(handle)
    except (comtypes.COMError, OSError, AttributeError):
        return None


def has_editable_focus() -> bool:
    """True only if the currently-focused UI element is confidently editable."""
    try:
        element = _get_focused_element()
        if element is None or not element.CurrentIsEnabled:
            return False
        return _is_editable(element)
    except (comtypes.COMError, OSError, AttributeError):
        return False


def verify_text_present(expected: str) -> bool:
    """Best-effort: True only if `expected` is confidently found in the
    currently-focused element's text content. False covers both "confirmed
    absent" and "couldn't determine" — callers treat both the same way
    (show the fallback card), so no finer distinction is needed here.
    """
    if not expected:
        return False
    try:
        element = _get_focused_element()
        if element is None:
            return False
        value = _read_value(element)
        return value is not None and expected in value
    except (comtypes.COMError, OSError, AttributeError):
        return False


def _is_editable(element) -> bool:
    if _has_value_pattern_writable(element):
        return True
    if _supports_pattern(element, _mod.UIA_IsTextEditPatternAvailablePropertyId):
        return True
    # Legacy Win32 apps (confirmed with real Microsoft Word: its document
    # surface, UIA class '_WwG', exposes neither ValuePattern nor
    # TextEditPattern — only plain TextPattern, and even the legacy MSAA
    # role bridge reports it as a generic ROLE_SYSTEM_CLIENT rather than
    # ROLE_SYSTEM_TEXT, so neither of the checks above nor a role check
    # catch it). Narrowly widening to "Document control type + TextPattern"
    # catches Word without also matching things like terminal emulators
    # (a different control type) that also expose plain TextPattern —
    # unlike those, a true read-only UIA_DocumentControlTypeId (e.g. a PDF
    # viewer) just won't actually receive pasted text, which is exactly the
    # safe failure mode this module is designed around (verify_text_present
    # then correctly reports "uncertain" and the caller shows the fallback
    # card instead of silently losing anything).
    if (
        element.CurrentControlType == _mod.UIA_DocumentControlTypeId
        and _supports_pattern(element, _mod.UIA_IsTextPatternAvailablePropertyId)
    ):
        return True
    return False


def _has_value_pattern_writable(element) -> bool:
    if not _supports_pattern(element, _mod.UIA_IsValuePatternAvailablePropertyId):
        return False
    try:
        vp = element.GetCurrentPattern(_mod.UIA_ValuePatternId)
        vp = vp.QueryInterface(_mod.IUIAutomationValuePattern)
        return not bool(vp.CurrentIsReadOnly)
    except (comtypes.COMError, OSError, AttributeError):
        return False


def _supports_pattern(element, property_id) -> bool:
    try:
        return bool(element.GetCurrentPropertyValue(property_id))
    except (comtypes.COMError, OSError, AttributeError):
        return False


def _read_value(element) -> str | None:
    if _supports_pattern(element, _mod.UIA_IsValuePatternAvailablePropertyId):
        try:
            vp = element.GetCurrentPattern(_mod.UIA_ValuePatternId)
            vp = vp.QueryInterface(_mod.IUIAutomationValuePattern)
            return str(vp.CurrentValue)
        except (comtypes.COMError, OSError, AttributeError):
            pass

    if _supports_pattern(element, _mod.UIA_IsTextPatternAvailablePropertyId):
        try:
            tp = element.GetCurrentPattern(_mod.UIA_TextPatternId)
            tp = tp.QueryInterface(_mod.IUIAutomationTextPattern)
            doc_range = tp.DocumentRange
            return str(doc_range.GetText(-1))
        except (comtypes.COMError, OSError, AttributeError):
            pass

    return None
