"""Test tray.py: tray icon creation, and HotkeyCaptureDialog key handling
(driven programmatically via QTest.keyClick, no manual interaction needed).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ctypes

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from voice_polish_desktop.hotkey import HotkeySpec, MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, HotkeyManager
from voice_polish_desktop.tray import HotkeyCaptureDialog, TrayIcon

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def main() -> int:
    app = QApplication(sys.argv)

    check("system tray is available", QSystemTrayIcon.isSystemTrayAvailable())

    tray = TrayIcon(paused=False)
    check("tray icon not null", not tray.icon().isNull())
    check("initial menu label is Pause", tray._pause_label() == "Pause")
    tray.set_paused(True)
    check("after set_paused(True), label is Resume", tray._pause_label() == "Resume")

    events = {"pause": None, "quit": False, "change": False}
    tray.pause_toggled.connect(lambda v: events.__setitem__("pause", v))
    tray.quit_requested.connect(lambda: events.__setitem__("quit", True))
    tray.change_hotkey_requested.connect(lambda: events.__setitem__("change", True))
    tray.set_paused(False)
    tray._on_pause_clicked()
    check("pause_toggled signal fired with True", events["pause"] is True)

    mgr = HotkeyManager(app)
    # Grab a combo via a *separate* raw registration (different hotkey ID,
    # bypassing HotkeyManager, which only ever tracks one ID) to create a
    # genuine OS-level conflict independent of the manager under test.
    taken_mods, taken_vk = MOD_CONTROL | MOD_SHIFT, 0x31  # Ctrl+Shift+1
    user32 = ctypes.windll.user32
    OTHER_HOTKEY_ID = 999
    ok = bool(user32.RegisterHotKey(None, OTHER_HOTKEY_ID, taken_mods, taken_vk))
    check("setup: pre-registered Ctrl+Shift+1 (separate ID) to create a real conflict", ok)

    current = HotkeySpec(modifiers=MOD_CONTROL | MOD_ALT, vk=0x32)  # Ctrl+Alt+2 (not registered, just "current" for the test)
    dialog = HotkeyCaptureDialog(mgr, current=current)

    # Simulate pressing Ctrl+Shift+1 (the conflicting combo) inside the dialog.
    QTest.keyClick(dialog, Qt.Key_1, Qt.ControlModifier | Qt.ShiftModifier)
    check("candidate captured for Ctrl+Shift+1", dialog._candidate is not None and dialog._candidate.vk == 0x31)
    check("save button enabled after valid candidate", dialog._save_button.isEnabled())

    dialog._on_save()
    check("save with a taken combo shows an inline error", dialog._error_label.text() != "")
    check("save with a taken combo does not accept the dialog", dialog.result() != QDialog.Accepted)
    check("save button disabled again after failed save", not dialog._save_button.isEnabled())

    # Now simulate a combo that IS free.
    QTest.keyClick(dialog, Qt.Key_9, Qt.ControlModifier | Qt.ShiftModifier)
    check("new candidate captured for Ctrl+Shift+9", dialog._candidate is not None and dialog._candidate.vk == 0x39)
    dialog._on_save()
    check("save with a free combo accepts the dialog", dialog.result() == QDialog.Accepted)
    check("result_spec() returns the saved spec", dialog.result_spec() is not None and dialog.result_spec().vk == 0x39)

    mgr.unregister()
    user32.UnregisterHotKey(None, OTHER_HOTKEY_ID)

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
