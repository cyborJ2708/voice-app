"""AppController — ties every module together into the tray-app state machine.

State machine: IDLE -> RECORDING -> PROCESSING -> IDLE. No separate
success/error AppState exists — those are transient pill displays that
return the controller straight to IDLE.

The hotkey is a toggle, not push-to-talk: Win32 RegisterHotKey has no
key-up event, and a low-level hook to get one was already deliberately
rejected in hotkey.py for security reasons (see its docstring) — so toggle
is a structural consequence of that choice, not a stylistic one.
"""
from __future__ import annotations

import enum
import sys

from PySide6.QtCore import QObject, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from . import config, focus_detect
from .audio import AudioRecorder
from .backend_client import BackendClient, PolishWorker
from .hotkey import HotkeyManager
from .inject import PasteAttempt, attempt_paste
from .overlay import PillOverlay
from .sanitize import sanitize_text
from .tray import HotkeyCaptureDialog, TrayIcon
from .welcome import WelcomeWindow

APP_NAME = "voice-polish"

# Time to let a paste actually land in the target app before we read its
# UI Automation value back to verify it — see _finalize_injection.
VERIFY_DELAY_MS = 400

_FAILURE_MESSAGES = {
    "auth": "authentication failed — check APP_AUTH_TOKEN",
    "network": "couldn't reach the backend",
    "empty": "no speech detected",
}


class AppState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


class AppController(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self._state = AppState.IDLE
        self._config = config.load()

        focus_detect.initialize()

        self._pill = PillOverlay()
        self._audio = AudioRecorder(self)
        self._backend_client = self._build_backend_client()

        self._tray = TrayIcon(paused=self._config.paused)
        self._hotkey_mgr = HotkeyManager(app)

        self._thread: QThread | None = None
        self._worker: PolishWorker | None = None
        self._welcome: WelcomeWindow | None = None

        self._wire_signals()
        self._tray.show()

        # If this fails, registration_failed fires synchronously (same-thread
        # direct connection) and _on_hotkey_registration_failed below opens a
        # blocking conflict-resolution dialog before __init__ continues —
        # tray.show() above ensures the tray icon is already visible by then.
        self._hotkey_mgr.register(self._config.hotkey)

        if not self._config.first_run_complete:
            self._show_welcome()

    # -- setup ------------------------------------------------------------------

    def _build_backend_client(self) -> BackendClient:
        try:
            return BackendClient(self._config.backend_base_url, self._config.app_auth_token)
        except ValueError:
            self._config.backend_base_url = config.DEFAULT_BACKEND_BASE_URL
            return BackendClient(self._config.backend_base_url, self._config.app_auth_token)

    def _wire_signals(self) -> None:
        self._hotkey_mgr.triggered.connect(self._on_hotkey_triggered)
        self._hotkey_mgr.registration_failed.connect(self._on_hotkey_registration_failed)

        self._audio.level.connect(self._pill.push_level)
        self._audio.error.connect(self._on_audio_error)

        self._tray.pause_toggled.connect(self._on_pause_toggled)
        self._tray.change_hotkey_requested.connect(self._on_change_hotkey_requested)
        self._tray.quit_requested.connect(self._on_quit_requested)

    def _show_welcome(self) -> None:
        label = self._hotkey_mgr.current.label if self._hotkey_mgr.current else self._config.hotkey.label
        self._welcome = WelcomeWindow(label, self._config.backend_base_url, self._config.app_auth_token)
        self._welcome.finished.connect(self._on_welcome_finished)
        self._welcome.show()

    # -- hotkey / recording state machine ----------------------------------------

    def _on_hotkey_triggered(self) -> None:
        if self._config.paused:
            return
        if self._state == AppState.IDLE:
            self._audio.start()
            self._pill.enter_listening()
            self._state = AppState.RECORDING
        elif self._state == AppState.RECORDING:
            self._stop_and_process()
        # PROCESSING: ignore — a request is already in flight.

    def _stop_and_process(self) -> None:
        wav_bytes = self._audio.stop()
        if not wav_bytes:
            self._pill.fade_out()
            self._state = AppState.IDLE
            return
        self._pill.enter_processing()
        self._state = AppState.PROCESSING
        self._start_polish_worker(wav_bytes)

    def _start_polish_worker(self, wav_bytes: bytes) -> None:
        self._thread = QThread(self)
        self._worker = PolishWorker(self._backend_client, wav_bytes)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._on_polish_succeeded)
        self._worker.failed.connect(self._on_polish_failed)
        self._worker.succeeded.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_polish_succeeded(self, text: str) -> None:
        self._state = AppState.IDLE
        clean = sanitize_text(text)
        if not clean:
            self._on_polish_failed("empty", "no speech detected")
            return

        if not focus_detect.has_editable_focus():
            # No valid injection target — never lose the text: put it on
            # the clipboard as a safety net (deliberately NOT restoring
            # whatever was on the clipboard before) and show it in the card.
            self._app.clipboard().setText(clean)
            self._pill.enter_card(clean, subtle_label="No text field detected — copied to clipboard")
            return

        attempt = attempt_paste(clean)
        QTimer.singleShot(VERIFY_DELAY_MS, lambda: self._finalize_injection(clean, attempt))

    def _finalize_injection(self, clean: str, attempt: PasteAttempt) -> None:
        if focus_detect.verify_text_present(clean):
            attempt.restore_clipboard()
            self._pill.enter_success()
        else:
            # Uncertain whether the paste landed — the attempt is left in
            # place (not undone) and we don't restore the user's prior
            # clipboard: `clean` stays on the clipboard as a safety net,
            # same contract as the no-target case, so the user is covered
            # either way ("Pasted — or copy here").
            attempt.keep_polished_on_clipboard(clean)
            self._pill.enter_card(clean, subtle_label="Pasted — or copy here")

    def _on_polish_failed(self, kind: str, message: str) -> None:
        self._state = AppState.IDLE
        self._pill.enter_error()
        detail = _FAILURE_MESSAGES.get(kind, message)
        self._tray.showMessage(APP_NAME, f"Error: {detail}", QSystemTrayIcon.Warning, 4000)

    def _on_audio_error(self, message: str) -> None:
        self._state = AppState.IDLE
        self._pill.fade_out()
        self._tray.showMessage(APP_NAME, f"Error: {message}", QSystemTrayIcon.Warning, 4000)

    # -- tray actions -------------------------------------------------------------

    def _on_pause_toggled(self, paused: bool) -> None:
        self._config.paused = paused
        config.save(self._config)

    def _on_change_hotkey_requested(self) -> None:
        dialog = HotkeyCaptureDialog(self._hotkey_mgr, current=self._config.hotkey)
        if dialog.exec() == QDialog.Accepted:
            spec = dialog.result_spec()
            if spec is not None:
                self._config.hotkey = spec
                config.save(self._config)

    def _on_hotkey_registration_failed(self, message: str) -> None:
        self._tray.showMessage(APP_NAME, message, QSystemTrayIcon.Warning, 5000)
        self._on_change_hotkey_requested()

    def _on_welcome_finished(self, backend_url: str, app_auth_token: str) -> None:
        self._config.first_run_complete = True
        if backend_url:
            self._config.backend_base_url = backend_url
        self._config.app_auth_token = app_auth_token
        config.save(self._config)
        # Rebuilt with whatever was just entered — the client built at
        # startup used only the previously-saved (possibly default/empty)
        # values.
        self._backend_client = self._build_backend_client()

    def _on_quit_requested(self) -> None:
        self._hotkey_mgr.unregister()
        if self._audio.is_recording():
            self._audio.stop()
        self._app.quit()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # no persistent main window — closing welcome must not quit
    controller = AppController(app)  # noqa: F841 — kept alive for the duration of app.exec() below
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
