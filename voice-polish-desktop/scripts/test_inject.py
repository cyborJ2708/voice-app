"""Real end-to-end test of inject.py: actual OS clipboard + actual SendInput.

Creates a genuinely OS-focused QLineEdit, injects text into it, and verifies
both that the text landed AND that the clipboard was restored afterward.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from voice_polish_desktop import inject

ORIGINAL_CLIPBOARD = "this was already on the clipboard before injection"
INJECTED_TEXT = "Hello from voice-polish-desktop!"


def main() -> None:
    app = QApplication(sys.argv)

    field = QLineEdit()
    field.setWindowTitle("inject.py test target")
    field.resize(420, 40)

    clipboard = app.clipboard()
    clipboard.setText(ORIGINAL_CLIPBOARD)

    result = {"mode": None, "field_text": None, "clipboard_after": None}

    def do_inject():
        result["mode"] = inject.inject(INJECTED_TEXT)
        QTimer.singleShot(500, check_result)  # let the scheduled clipboard restore fire

    def check_result():
        result["field_text"] = field.text()
        result["clipboard_after"] = clipboard.text()
        app.quit()

    field.show()
    field.raise_()
    field.activateWindow()
    field.setFocus()
    QTimer.singleShot(400, do_inject)  # give the window manager time to actually focus it

    app.exec()

    ok_text = result["field_text"] == INJECTED_TEXT
    ok_clip = result["clipboard_after"] == ORIGINAL_CLIPBOARD
    print(f"mode used: {result['mode']}")
    print(f"{'PASS' if ok_text else 'FAIL'}: injected text landed in the field "
          f"(got {result['field_text']!r})")
    print(f"{'PASS' if ok_clip else 'FAIL'}: clipboard restored to original "
          f"(got {result['clipboard_after']!r})")

    sys.exit(0 if (ok_text and ok_clip) else 1)


if __name__ == "__main__":
    main()
