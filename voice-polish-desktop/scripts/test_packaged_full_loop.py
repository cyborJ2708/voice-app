"""Stage 1 verification: drives the already-running rebuilt packaged .exe
through the real global hotkey (real mic, real backend call using the
NEW config-file-based token — no env var set for this process either),
captures a screenshot of the overlay pill mid-flow for visual confirmation,
and confirms text lands via real paste.
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
    field = QLineEdit()
    field.setWindowTitle("packaged full-loop test target")
    field.resize(420, 40)
    field.show()
    field.raise_()
    field.activateWindow()
    field.setFocus()

    screen = app.primaryScreen()

    def step1():
        print("pressing hotkey (start recording in the packaged exe)...")
        press_test_hotkey()

    def step2_screenshot():
        pix = screen.grabWindow(0)
        out = Path(__file__).resolve().parent / "overlay_frozen_listening.png"
        pix.save(str(out))
        print(f"saved listening-state screenshot to {out}")
        print("speak now...")

    def step3():
        print("pressing hotkey (stop)...")
        press_test_hotkey()

    def step3b_screenshot():
        pix = screen.grabWindow(0)
        out = Path(__file__).resolve().parent / "overlay_frozen_processing.png"
        pix.save(str(out))
        print(f"saved processing-state screenshot to {out}")

    def step4(poll=[0]):
        text = field.text()
        if not text and poll[0] < 20:
            poll[0] += 1
            QTimer.singleShot(500, step4)
            return
        print()
        if text:
            print(f"PASS: packaged exe (config-token, no env var) produced real text: {text!r}")
        else:
            print("FAIL: no text landed within timeout")
        app.quit()

    QTimer.singleShot(500, step1)
    QTimer.singleShot(1800, step2_screenshot)
    QTimer.singleShot(2600, step3)
    QTimer.singleShot(2900, step3b_screenshot)
    QTimer.singleShot(3100, step4)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
