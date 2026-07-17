"""End-to-end test of the injection fallback flow added to app.py:
no-target -> card, verified -> success, uncertain -> card ("Pasted — or copy here").

Drives AppController._on_polish_succeeded directly with controlled text
(the mic/backend round-trip is already covered exhaustively by
test_app.py/test_backend_client.py — this test targets the NEW branching
logic specifically), but exercises REAL focus_detect (real UIA calls
against real focused widgets) and REAL inject.attempt_paste (real
clipboard + real SendInput paste) for the no-target and verified-success
branches. Only the "uncertain verification" branch is forced via a
monkeypatch of focus_detect.verify_text_present, since reliably reproducing
a genuine UIA verification failure on demand isn't practical — the branch
logic itself (not UIA's ability to detect uncertainty, already covered by
test_focus_detect.py) is what's under test there.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from voice_polish_desktop import config, focus_detect
from voice_polish_desktop.app import AppController
from voice_polish_desktop.hotkey import MOD_CONTROL, MOD_SHIFT, HotkeySpec
from voice_polish_desktop.overlay import PillState

TEST_HOTKEY = HotkeySpec(modifiers=MOD_CONTROL | MOD_SHIFT, vk=0x20)  # Ctrl+Shift+Space, confirmed free

failures = 0
uncaught_exceptions: list[BaseException] = []


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def _record_uncaught(exc_type, exc_value, exc_tb) -> None:
    # Qt/PySide6 silently swallows exceptions raised inside slots/timer
    # callbacks by default — without this hook, a real bug (e.g. a typo'd
    # method name inside enter_success()) can crash a callback partway
    # through while shallow state-only assertions still pass, masking it
    # entirely. This was caught exactly this way during development.
    import traceback
    uncaught_exceptions.append(exc_value)
    traceback.print_exception(exc_type, exc_value, exc_tb)


def main() -> int:
    sys.excepthook = _record_uncaught
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    config.save(config.AppConfig(
        hotkey=TEST_HOTKEY, paused=False, first_run_complete=True,
        backend_base_url=config.DEFAULT_BACKEND_BASE_URL, app_auth_token="",
    ))

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    clipboard = app.clipboard()

    field = QLineEdit()
    field.setWindowTitle("fallback flow test — editable target")
    field.resize(320, 40)

    label = QLabel("not editable")
    label.setWindowTitle("fallback flow test — non-editable target")
    label.setFocusPolicy(Qt.StrongFocus)
    label.resize(320, 40)

    controller = AppController(app)

    # -- Branch A: no editable target -> card, clipboard = polished text ----

    ORIGINAL_CLIPBOARD_A = "clipboard before branch A"
    TEXT_A = "polished text for the no-target branch"

    def branch_a_start():
        clipboard.setText(ORIGINAL_CLIPBOARD_A)
        label.show()
        label.raise_()
        label.activateWindow()
        label.setFocus()
        QTimer.singleShot(400, branch_a_run)

    def branch_a_run():
        check("(setup) real UIA confirms QLabel is NOT editable", not focus_detect.has_editable_focus())
        controller._on_polish_succeeded(TEXT_A)
        QTimer.singleShot(200, branch_a_check)

    def branch_a_check():
        check("branch A: pill entered CARD state", controller._pill._state == PillState.CARD)
        check("branch A: card text is TEXT_A", controller._pill._card_text == TEXT_A)
        check("branch A: clipboard holds TEXT_A (not the old contents)", clipboard.text() == TEXT_A)
        check("branch A: subtle label mentions no field detected", "No text field" in controller._pill._subtle_label.text())
        controller._pill._on_dismiss_clicked()
        label.hide()
        QTimer.singleShot(300, branch_b_start)

    # -- Branch B: editable target, verified -> success, clipboard restored --

    ORIGINAL_CLIPBOARD_B = "clipboard before branch B"
    TEXT_B = "polished text for the verified branch"

    def branch_b_start():
        clipboard.setText(ORIGINAL_CLIPBOARD_B)
        field.show()
        field.raise_()
        field.activateWindow()
        field.setFocus()
        QTimer.singleShot(400, branch_b_run)

    def branch_b_run():
        check("(setup) real UIA confirms QLineEdit IS editable", focus_detect.has_editable_focus())
        controller._on_polish_succeeded(TEXT_B)
        # allow attempt_paste's SendInput + the VERIFY_DELAY_MS readback to complete
        QTimer.singleShot(900, branch_b_check)

    def branch_b_check():
        check("branch B: field received TEXT_B via real paste", field.text() == TEXT_B)
        check("branch B: pill entered SUCCESS state", controller._pill._state == PillState.SUCCESS)
        check("branch B: clipboard restored to original (not TEXT_B)", clipboard.text() == ORIGINAL_CLIPBOARD_B)
        field.clear()
        QTimer.singleShot(300, branch_c_start)

    # -- Branch C: editable target, but verification forced uncertain --------

    ORIGINAL_CLIPBOARD_C = "clipboard before branch C"
    TEXT_C = "polished text for the uncertain branch"

    def branch_c_start():
        clipboard.setText(ORIGINAL_CLIPBOARD_C)
        field.show()
        field.raise_()
        field.activateWindow()
        field.setFocus()
        QTimer.singleShot(400, branch_c_run)

    _real_verify = focus_detect.verify_text_present

    def branch_c_run():
        focus_detect.verify_text_present = lambda expected: False  # force "uncertain"
        controller._on_polish_succeeded(TEXT_C)
        QTimer.singleShot(900, branch_c_check)

    def branch_c_check():
        focus_detect.verify_text_present = _real_verify
        check("branch C: pill entered CARD state (uncertain verification)", controller._pill._state == PillState.CARD)
        check("branch C: card text is TEXT_C", controller._pill._card_text == TEXT_C)
        check("branch C: clipboard holds TEXT_C (not restored)", clipboard.text() == TEXT_C)
        check("branch C: subtle label is the 'pasted or copy' variant", "Pasted" in controller._pill._subtle_label.text())
        controller._pill._on_dismiss_clicked()
        controller._hotkey_mgr.unregister()
        app.quit()

    QTimer.singleShot(300, branch_a_start)
    app.exec()

    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()

    if uncaught_exceptions:
        check(f"no uncaught exceptions during the run (saw {len(uncaught_exceptions)})", False)

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
