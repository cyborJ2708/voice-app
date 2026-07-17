"""Drives the already-running packaged .exe through the real global hotkey
to confirm sounddevice/PortAudio actually works when bundled (the specific
packaging risk this test exists to catch), using a focused QLineEdit as the
injection target, exactly like test_app.py does for the dev-venv version.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from voice_polish_desktop.winkeys import VK_CONTROL, send_combo

VK_SHIFT = 0x10
VK_SPACE = 0x20


def press_test_hotkey() -> None:
    send_combo([VK_CONTROL, VK_SHIFT, VK_SPACE])


def main() -> None:
    app = QApplication(sys.argv)
    target = QLineEdit()
    target.setWindowTitle("packaged exe test target")
    target.resize(420, 40)
    target.show()
    target.raise_()
    target.activateWindow()
    target.setFocus()

    def step1():
        print("pressing hotkey (start recording in the packaged exe)...")
        press_test_hotkey()

    def step2():
        print("speak now (~1.5s)...")

    def step3():
        print("pressing hotkey (stop)...")
        press_test_hotkey()

    def step4():
        print(f"field text after {POLL[0]*500}ms: {target.text()!r}")
        if not target.text() and POLL[0] < 20:
            POLL[0] += 1
            QTimer.singleShot(500, step4)
            return
        result = target.text()
        print()
        if result:
            print(f"PASS: packaged exe produced text via real mic+backend: {result!r}")
        else:
            print("FAIL: no text landed within timeout")
        app.quit()

    POLL = [0]
    QTimer.singleShot(500, step1)
    QTimer.singleShot(700, step2)
    QTimer.singleShot(2200, step3)
    QTimer.singleShot(2500, step4)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
