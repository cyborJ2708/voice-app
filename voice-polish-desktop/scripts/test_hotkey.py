"""Self-test: registers the real global hotkey and simulates pressing it.

This exercises the trickiest part of hotkey.py — RegisterHotKey +
QAbstractNativeEventFilter interop — without needing a human to physically
press Ctrl+Win+Space.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.hotkey import DEFAULT_HOTKEY, HotkeyManager
from voice_polish_desktop.winkeys import VK_CONTROL, VK_LWIN, send_combo


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    mgr = HotkeyManager(app)
    result = {"triggered": False}

    def on_trigger():
        result["triggered"] = True
        print("PASS: hotkey.triggered fired")
        app.quit()

    mgr.triggered.connect(on_trigger)
    mgr.registration_failed.connect(lambda msg: print(f"FAIL: {msg}"))

    ok = mgr.register(DEFAULT_HOTKEY)
    print(f"registered {DEFAULT_HOTKEY.label}: {ok}")
    if not ok:
        sys.exit(1)

    def simulate_press():
        print("simulating physical Ctrl+Win+Space...")
        send_combo([VK_CONTROL, VK_LWIN, DEFAULT_HOTKEY.vk])

    QTimer.singleShot(300, simulate_press)

    def timeout_check():
        if not result["triggered"]:
            print("FAIL: no hotkey event received within timeout")
            app.exit(1)

    QTimer.singleShot(2000, timeout_check)

    exit_code = app.exec()
    mgr.unregister()
    sys.exit(exit_code if not result["triggered"] else 0)


if __name__ == "__main__":
    main()
