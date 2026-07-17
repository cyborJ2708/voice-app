"""Real test of inject.attempt_paste(): both the restore-clipboard and
keep-polished-on-clipboard outcomes, against a genuinely focused field.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from voice_polish_desktop import inject

ORIGINAL_CLIPBOARD = "original clipboard contents before attempt_paste"
TEXT_A = "first attempt — will be restored"
TEXT_B = "second attempt — will be kept on clipboard"

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def main() -> int:
    app = QApplication(sys.argv)
    field = QLineEdit()
    field.setWindowTitle("attempt_paste test target")
    field.resize(420, 40)
    clipboard = app.clipboard()

    def step1():
        clipboard.setText(ORIGINAL_CLIPBOARD)
        field.show()
        field.raise_()
        field.activateWindow()
        field.setFocus()
        QTimer.singleShot(400, step2_restore_path)

    _pending = {}

    def step2_restore_path():
        attempt = inject.attempt_paste(TEXT_A)
        check("attempt_paste mode is clipboard", attempt.mode == "clipboard")
        _pending["attempt"] = attempt
        # SendInput is async — give the Qt event loop a turn to actually
        # deliver/process the paste before checking the field's contents.
        QTimer.singleShot(400, step2b_check_and_restore)

    def step2b_check_and_restore():
        check("field received TEXT_A", field.text() == TEXT_A)
        _pending["attempt"].restore_clipboard()
        QTimer.singleShot(200, step3_check_restored)

    def step3_check_restored():
        check("clipboard restored to original after restore_clipboard()", clipboard.text() == ORIGINAL_CLIPBOARD)
        field.clear()
        QTimer.singleShot(100, step4_keep_path)

    def step4_keep_path():
        attempt = inject.attempt_paste(TEXT_B)
        _pending["attempt"] = attempt
        QTimer.singleShot(400, step4b_check_and_keep)

    def step4b_check_and_keep():
        check("second field received TEXT_B", field.text() == TEXT_B)
        _pending["attempt"].keep_polished_on_clipboard(TEXT_B)
        QTimer.singleShot(200, step5_check_kept)

    def step5_check_kept():
        check("clipboard holds TEXT_B (not restored) after keep_polished_on_clipboard()", clipboard.text() == TEXT_B)
        app.quit()

    QTimer.singleShot(200, step1)
    app.exec()

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
