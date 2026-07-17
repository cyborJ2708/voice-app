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

import comtypes
import comtypes.client

_uia = None
_mod = None
_initialized = False


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
    return _supports_pattern(element, _mod.UIA_IsTextEditPatternAvailablePropertyId)


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
