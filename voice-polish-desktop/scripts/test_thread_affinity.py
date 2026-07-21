"""Regression test for a real bug found during development: connecting
worker.succeeded to a QObject's bound method must correctly marshal
execution to the main thread so that QTimer.singleShot scheduled inside
the slot fires — otherwise a timer created on a worker thread whose event
loop is about to be torn down (via succeeded -> thread.quit) never fires,
and the tiered injection pipeline silently stalls forever mid-flight.

Uses a FAKE worker (sleeps briefly, emits succeeded with fixed text) so
this is fast and fully deterministic — no mic, no backend, no cold-start
delays, no silence variability, unlike test_app.py's real end-to-end run.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLineEdit

from voice_polish_desktop import config
from voice_polish_desktop.app import AppController
from voice_polish_desktop.hotkey import MOD_CONTROL, MOD_SHIFT, HotkeySpec

TEST_HOTKEY = HotkeySpec(modifiers=MOD_CONTROL | MOD_SHIFT, vk=0x20)
FIXED_TEXT = "This is fixed deterministic test text for thread affinity diagnosis."

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        failures += 1


class FakeWorker(QObject):
    succeeded = Signal(str)
    failed = Signal(str, str)

    def run(self):
        time.sleep(0.3)  # simulate network latency briefly
        self.succeeded.emit(FIXED_TEXT)


def main() -> int:
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    config.save(config.AppConfig(
        hotkey=TEST_HOTKEY, paused=False, first_run_complete=True,
        backend_base_url=config.DEFAULT_BACKEND_BASE_URL, app_auth_token="",
    ))

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    field = QLineEdit()
    field.setWindowTitle("thread affinity test target")
    field.resize(420, 40)
    field.show()
    field.raise_()
    field.activateWindow()
    field.setFocus()

    controller = AppController(app)
    controller._hotkey_mgr.unregister()  # not needed for this test

    def run_fake_worker():
        thread = QThread()
        worker = FakeWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(controller._on_polish_succeeded, Qt.QueuedConnection)
        worker.succeeded.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        controller._test_thread = thread  # keep alive
        controller._test_worker = worker
        thread.start()

    def check_result():
        check("text landed via the real tiered injection pipeline", field.text() == FIXED_TEXT)
        print(f"field text: {field.text()!r}")
        app.quit()

    QTimer.singleShot(300, run_fake_worker)
    QTimer.singleShot(3000, check_result)  # generous window, no cold-start variable here
    app.exec()

    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
