"""Full integration test of AppController, driven through the REAL global
hotkey (actual SendInput key presses, actual mic, actual backend call).

Pre-seeds config with Ctrl+Shift+Space (confirmed free on this machine —
the shipped default Ctrl+Win+Space is known to conflict with another app
here) so this test doesn't get stuck on the blocking conflict-resolution
dialog. Requires the backend running locally with APP_AUTH_TOKEN set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from voice_polish_desktop import config
from voice_polish_desktop.app import AppController, AppState
from voice_polish_desktop.hotkey import MOD_CONTROL, MOD_SHIFT, HotkeySpec
from voice_polish_desktop.winkeys import VK_CONTROL, send_combo

VK_SHIFT = 0x10
VK_SPACE = 0x20
TEST_HOTKEY = HotkeySpec(modifiers=MOD_CONTROL | MOD_SHIFT, vk=VK_SPACE)

failures = 0
uncaught_exceptions: list[BaseException] = []


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


def _record_uncaught(exc_type, exc_value, exc_tb) -> None:
    # Qt/PySide6 silently swallows exceptions raised inside slots/timer
    # callbacks by default. Without this hook, a crash inside e.g.
    # enter_success() could go unnoticed here: the real paste already
    # happens earlier in attempt_paste(), so "text landed in the field"
    # would still pass even if the pill's success animation crashed right
    # after. Confirmed this gap for real during development.
    import traceback
    uncaught_exceptions.append(exc_value)
    traceback.print_exception(exc_type, exc_value, exc_tb)


def press_test_hotkey() -> None:
    send_combo([VK_CONTROL, VK_SHIFT, VK_SPACE])


def main() -> int:
    sys.excepthook = _record_uncaught
    # Clean slate, and avoid the shipped-default hotkey conflict on this machine.
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    seed = config.AppConfig(
        hotkey=TEST_HOTKEY, paused=False, first_run_complete=True,
        backend_base_url=config.DEFAULT_BACKEND_BASE_URL,
        app_auth_token=os.environ.get("APP_AUTH_TOKEN", ""),
    )
    config.save(seed)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    target = QLineEdit()
    target.setWindowTitle("AppController integration test target")
    target.resize(420, 40)
    target.show()
    target.raise_()
    target.activateWindow()
    target.setFocus()

    controller = AppController(app)

    check("hotkey registered on startup", controller._hotkey_mgr.current is not None)
    check("state starts IDLE", controller._state == AppState.IDLE)

    def step_press_to_record():
        print("pressing test hotkey (start)...")
        press_test_hotkey()

    def step_check_recording():
        check("state is RECORDING after first press", controller._state == AppState.RECORDING)
        check("audio recorder is recording", controller._audio.is_recording())
        print("speak now (recording ~1.5s)...")

    def step_press_to_stop():
        print("pressing test hotkey (stop)...")
        press_test_hotkey()

    def step_check_processing():
        check("state is PROCESSING after second press", controller._state == AppState.PROCESSING)

    def step_check_final(elapsed_polls=[0]):
        if controller._state == AppState.PROCESSING and elapsed_polls[0] < 40:
            elapsed_polls[0] += 1
            QTimer.singleShot(500, step_check_final)
            return
        check("state returned to IDLE after backend round-trip", controller._state == AppState.IDLE)
        check("polished text landed in the focused field", len(target.text()) > 0)
        print(f"field text: {target.text()!r}")
        step_test_pause()

    def step_test_pause():
        controller._tray._on_pause_clicked()  # toggles paused True, emits pause_toggled
        check("config.paused is True after toggling pause", controller._config.paused is True)
        press_test_hotkey()
        QTimer.singleShot(400, step_check_paused_ignored)

    def step_check_paused_ignored():
        check("hotkey press ignored while paused (state stays IDLE)", controller._state == AppState.IDLE)
        check("audio not recording while paused", not controller._audio.is_recording())
        controller._tray._on_pause_clicked()  # resume
        step_test_quit()

    def step_test_quit():
        registered_spec = controller._hotkey_mgr.current
        controller._on_quit_requested()
        # after quit, the hotkey should be unregistered — re-registering it
        # from a fresh manager should now succeed.
        import ctypes
        user32 = ctypes.windll.user32
        reregister_ok = bool(user32.RegisterHotKey(None, 12345, registered_spec.modifiers, registered_spec.vk))
        check("hotkey unregistered on quit (re-registration elsewhere succeeds)", reregister_ok)
        if reregister_ok:
            user32.UnregisterHotKey(None, 12345)

    QTimer.singleShot(400, step_press_to_record)
    QTimer.singleShot(700, step_check_recording)
    QTimer.singleShot(2200, step_press_to_stop)
    QTimer.singleShot(2500, step_check_processing)
    QTimer.singleShot(2700, step_check_final)

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
