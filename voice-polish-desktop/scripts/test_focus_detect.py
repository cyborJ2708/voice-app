"""Real UIA tests: editable/non-editable detection + verify_text_present,
against genuinely-focused Qt widgets (positive: QLineEdit, negative: QLabel).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from voice_polish_desktop import focus_detect

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def main() -> int:
    app = QApplication(sys.argv)
    focus_detect.initialize()

    field = QLineEdit()
    field.setWindowTitle("focus_detect test — editable")
    field.resize(320, 40)

    label = QLabel("not editable")
    label.setWindowTitle("focus_detect test — non-editable")
    label.setFocusPolicy(Qt.StrongFocus)
    label.resize(320, 40)

    def step_field():
        field.show()
        field.raise_()
        field.activateWindow()
        field.setFocus()
        QTimer.singleShot(400, step_check_field)

    def step_check_field():
        check("QLineEdit (empty) detected as editable", focus_detect.has_editable_focus())
        check("verify_text_present is False before any text set", not focus_detect.verify_text_present("hello"))
        field.setText("hello uia verification")
        check("verify_text_present True after setText matches", focus_detect.verify_text_present("hello uia verification"))
        check("verify_text_present False for text not present", not focus_detect.verify_text_present("something else entirely"))
        field.hide()
        QTimer.singleShot(200, step_label)

    def step_label():
        label.show()
        label.raise_()
        label.activateWindow()
        label.setFocus()
        QTimer.singleShot(400, step_check_label)

    def step_check_label():
        check("QLabel (focusable, non-editable) NOT detected as editable", not focus_detect.has_editable_focus())
        label.hide()
        app.quit()

    QTimer.singleShot(200, step_field)
    app.exec()

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
