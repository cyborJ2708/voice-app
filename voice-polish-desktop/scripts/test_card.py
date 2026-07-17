"""Visual + interaction test of the card fallback state.

Takes a screenshot for visual review, then drives the Copy and dismiss(X)
buttons via QTest.mouseClick — Qt's own synthetic event injection.

Note: real OS-level SendInput mouse clicks were extensively tested against
this window during development and could not be made to register (even
against a completely plain, default QWidget+QPushButton with no special
flags at all — ruling out anything specific to this card's window flags).
Native WM_LBUTTONDOWN delivery to the window was confirmed via a native
event filter, and SendInput mouse clicks were confirmed to work against
real OS shell UI (the taskbar clock flyout) in this same environment, so
mouse-click injection isn't blocked outright here — but it does not
translate into a registered Qt button click for windows owned by this
process, for reasons not fully root-caused (timing/event-pump adjustments,
DPI-scaling overrides, and window-flag bisection were all tried without
success). QTest.mouseClick — Qt's own internal synthetic event path —
reliably triggers the same handlers, confirming the button logic itself is
correct; only the SendInput-specific delivery path in this dev environment
is unverified. Real end-user mouse hardware is expected to work normally
(this is a standard, widely-used Qt window pattern), but this is flagged
explicitly as the one thing not independently confirmed end-to-end.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.overlay import PillOverlay

SAMPLE_TEXT = (
    "This is the fallback card. It shows up when no editable text field was "
    "detected, or when we couldn't confirm the paste landed — so the polished "
    "text never just disappears."
)

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def main() -> int:
    app = QApplication(sys.argv)
    pill = PillOverlay()

    events = {"dismissed": False}
    pill.dismissed.connect(lambda: events.__setitem__("dismissed", True))

    def show_card():
        pill.enter_card(SAMPLE_TEXT, subtle_label="Pasted — or copy here")
        QTimer.singleShot(500, take_screenshot)

    def take_screenshot():
        pix = pill.grab()
        out = Path(__file__).resolve().parent / "card_preview.png"
        pix.save(str(out))
        print(f"saved screenshot to {out}")
        QTimer.singleShot(200, click_copy)

    def click_copy():
        QTest.mouseClick(pill._copy_button, Qt.LeftButton)
        QTimer.singleShot(200, check_copy)

    def check_copy():
        check("Copy button click registered ('Copied!' shown)", pill._copy_button.text() == "Copied!")
        check("clipboard holds the card text after copy", app.clipboard().text() == SAMPLE_TEXT)
        QTimer.singleShot(200, click_dismiss)

    def click_dismiss():
        QTest.mouseClick(pill._dismiss_button, Qt.LeftButton)
        QTimer.singleShot(500, check_dismiss)

    def check_dismiss():
        check("dismissed signal fired after X click", events["dismissed"])
        check("card text cleared from memory after dismiss", pill._card_text == "")
        check("text_view cleared after dismiss", pill._text_view.toPlainText() == "")
        app.quit()

    QTimer.singleShot(300, show_card)
    app.exec()

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
