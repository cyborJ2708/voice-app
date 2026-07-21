"""Drives the real tiered injection pipeline against real, already-running
apps: Word, Notion, Slack, Edge/Gmail, Notepad. For each, force-foregrounds
the app's window (no manual clicking needed), fires a fake-worker-emitted
fixed test string through the REAL AppController pipeline, and reports
which tier succeeded (via the opt-in debug log) plus what actually
happened (has_editable_focus, final pill state, clipboard content).

Uses a FakeWorker (like test_thread_affinity.py) rather than the real mic
+ backend, since this test is about the injection pipeline's behavior
across different target apps, not about mic/backend variability (already
covered by test_app.py).
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from voice_polish_desktop import config, focus_detect
from voice_polish_desktop.app import AppController
from voice_polish_desktop.hotkey import MOD_CONTROL, MOD_SHIFT, HotkeySpec

os.environ["VOICE_POLISH_DEBUG_LOG"] = "1"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
TEST_HOTKEY = HotkeySpec(modifiers=MOD_CONTROL | MOD_SHIFT, vk=0x20)

TARGETS = [
    ("Word", None, "WINWORD.EXE"),
    ("Notion", None, "Notion.exe"),
    ("Slack", None, "slack.exe"),
    ("Edge/Gmail", "gmail", "msedge.exe"),
    ("Notepad", None, "Notepad.exe"),
]


def _process_name_for_hwnd(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf_len = ctypes.c_uint32(260)
        buf = ctypes.create_unicode_buffer(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def find_window(process_name: str, title_contains: str | None = None):
    result = {}

    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        proc = _process_name_for_hwnd(hwnd)
        if proc.lower() != process_name.lower():
            return True
        if title_contains and title_contains.lower() not in title.lower():
            return True
        result["hwnd"] = hwnd
        result["title"] = title
        return False

    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(CB(callback), 0)
    return result.get("hwnd"), result.get("title")


SW_RESTORE = 9


def force_foreground(hwnd) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg_hwnd = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(current_thread, fg_thread, True)
    user32.AttachThreadInput(current_thread, target_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(current_thread, fg_thread, False)
    user32.AttachThreadInput(current_thread, target_thread, False)


class FakeWorker(QObject):
    succeeded = Signal(str)
    failed = Signal(str, str)

    def __init__(self, text):
        super().__init__()
        self._text = text

    def run(self):
        time.sleep(0.2)
        self.succeeded.emit(self._text)


def main() -> int:
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    config.save(config.AppConfig(
        hotkey=TEST_HOTKEY, paused=False, first_run_complete=True,
        backend_base_url=config.DEFAULT_BACKEND_BASE_URL, app_auth_token="",
    ))

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    controller._hotkey_mgr.unregister()

    log_path = config.CONFIG_DIR / "injection_debug.log"
    if log_path.exists():
        log_path.unlink()

    results = []
    queue = list(TARGETS)

    def run_next():
        if not queue:
            app.quit()
            return
        label, title_filter, process_name = queue.pop(0)
        hwnd, title = find_window(process_name, title_filter)
        if not hwnd:
            results.append((label, "NOT FOUND", None, None))
            QTimer.singleShot(100, run_next)
            return

        force_foreground(hwnd)

        def after_foreground():
            editable = focus_detect.has_editable_focus()
            test_text = f"Voice Polish test injection for {label} — please ignore/delete."
            thread = QThread()
            worker = FakeWorker(test_text)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.succeeded.connect(controller._on_polish_succeeded, Qt.QueuedConnection)
            worker.succeeded.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            controller._test_thread = thread
            controller._test_worker = worker
            thread.start()

            def check_outcome():
                pill_state = str(controller._pill._state)
                clipboard_now = app.clipboard().text()
                landed = test_text in clipboard_now or controller._pill._card_text == test_text
                results.append((label, "editable" if editable else "NOT editable", pill_state, title))
                QTimer.singleShot(300, run_next)

            QTimer.singleShot(1600, check_outcome)

        QTimer.singleShot(500, after_foreground)

    QTimer.singleShot(300, run_next)
    app.exec()

    print()
    print("=== Results ===")
    for label, editable, pill_state, title in results:
        print(f"{label}: focus_detect={editable}, final_pill_state={pill_state}, window_title={title!r}")

    print()
    print("=== Tier log (from injection_debug.log) ===")
    if log_path.exists():
        print(log_path.read_text(encoding="utf-8"))
    else:
        print("(no log entries written)")

    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    if log_path.exists():
        log_path.unlink()

    return 0


if __name__ == "__main__":
    sys.exit(main())
