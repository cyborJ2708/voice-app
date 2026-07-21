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
import os
import sys
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from . import auth, config, focus_detect, inject
from .audio import AudioRecorder
from .backend_client import BackendClient, PolishWorker
from .hotkey import HotkeyManager
from .inject import ClipboardStage
from .overlay import PillOverlay
from .sanitize import sanitize_text
from .tray import HotkeyCaptureDialog, TrayIcon
from .welcome import WelcomeWindow

APP_NAME = "voice-polish"

# Tiered injection pipeline timings.
#
# PRE_PASTE_DELAY_MS: gap between staging the clipboard and sending Ctrl+V —
# a human pasting naturally has a beat here too; cheap insurance against a
# target app that hasn't processed a clipboard-changed notification yet.
# PROCESS_PRE_PASTE_OVERRIDES_MS: per-process longer delay, keyed by
# lowercased exe name (see focus_detect.get_focused_process_name()) — added
# for Electron apps (Notion confirmed to lag focus/DOM updates more than
# native apps during development).
# VERIFY_DELAY_MS: how long to wait after a keystroke before reading the
# focused element's value back to check it landed. Measured empirically
# against Notion (worst case observed): its post-paste re-render settles by
# ~250ms, so 550ms leaves real margin. Reused for both the clipboard tier
# and the typed tier's verification.
PRE_PASTE_DELAY_MS = 150
PROCESS_PRE_PASTE_OVERRIDES_MS = {
    "notion.exe": 350,
}
VERIFY_DELAY_MS = 550

_FAILURE_MESSAGES = {
    "auth": "authentication failed — check APP_AUTH_TOKEN",
    "network": "couldn't reach the backend",
    "empty": "no speech detected",
}

# Opt-in local debug log: which injection tier succeeded, per app process —
# never the injected text itself. Off unless this env var is set; written
# to %APPDATA%\voice-polish-desktop\injection_debug.log.
_DEBUG_LOG_ENV_VAR = "VOICE_POLISH_DEBUG_LOG"


def _log_injection_tier(tier: str, process_name: str | None) -> None:
    if not os.environ.get(_DEBUG_LOG_ENV_VAR):
        return
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} process={process_name or 'unknown'} tier={tier}\n"
        with (config.CONFIG_DIR / "injection_debug.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # logging must never crash the app


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
        self._login_thread: QThread | None = None
        self._login_worker: auth.LoginWorker | None = None

        self._wire_signals()
        self._tray.show()
        self._pill.enter_idle()  # always-visible resting capsule, per the design handoff

        # Optimistic: show whatever email was cached from the last session
        # immediately, without blocking startup on a network call. If the
        # stored token turns out to be expired, the next backend call (see
        # backend_client.py's Authorization header) triggers a silent
        # refresh; if that fails too, the user just sees the tray flip back
        # to "Log in…" next time they check it.
        cached_session = auth.load_session()
        if cached_session is not None:
            self._tray.set_logged_in_email(cached_session.email or "Ritely account")

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

        self._pill.cancelled.connect(self._on_capsule_cancelled)
        self._pill.confirmed.connect(self._on_capsule_confirmed)

        self._tray.pause_toggled.connect(self._on_pause_toggled)
        self._tray.change_hotkey_requested.connect(self._on_change_hotkey_requested)
        self._tray.login_requested.connect(self._on_login_requested)
        self._tray.logout_requested.connect(self._on_logout_requested)
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
            self._pill.enter_idle()
            self._state = AppState.IDLE
            return
        self._pill.enter_processing()
        self._state = AppState.PROCESSING
        self._start_polish_worker(wav_bytes)

    def _on_capsule_cancelled(self) -> None:
        """Mouse alternative to the hotkey: discard the in-progress recording
        without calling the backend at all."""
        if self._state != AppState.RECORDING:
            return
        self._audio.stop()  # bytes discarded, never sent anywhere
        self._pill.enter_idle()
        self._state = AppState.IDLE

    def _on_capsule_confirmed(self) -> None:
        """Mouse alternative to the hotkey: same effect as pressing the
        hotkey again while RECORDING."""
        if self._state != AppState.RECORDING:
            return
        self._stop_and_process()

    def _start_polish_worker(self, wav_bytes: bytes) -> None:
        self._thread = QThread(self)
        self._worker = PolishWorker(self._backend_client, wav_bytes)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        # Explicit QueuedConnection — confirmed by direct measurement that
        # AutoConnection was NOT resolving to queued here despite the
        # sender/receiver living on different threads, so
        # _on_polish_succeeded was actually executing on the worker thread.
        # That's a latent bug on its own, but it became consistently fatal
        # once this handler started chaining QTimer.singleShot calls (the
        # tiered injection pipeline): a timer created on the worker thread
        # never fires, because that thread's event loop is torn down (via
        # succeeded/failed -> self._thread.quit, connected below) before the
        # delay elapses.
        self._worker.succeeded.connect(self._on_polish_succeeded, Qt.QueuedConnection)
        self._worker.failed.connect(self._on_polish_failed, Qt.QueuedConnection)
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
            _log_injection_tier("no_target", focus_detect.get_focused_process_name())
            return

        # Tiered pipeline: clipboard+paste (tier 1) -> verify -> typed
        # (tier 2) -> verify -> fallback card (tier 3). Each step is
        # scheduled via QTimer rather than blocking, so the pill's
        # animation and the hotkey's native event filter stay responsive.
        process_name = focus_detect.get_focused_process_name()
        stage = inject.stage_clipboard(clean)
        delay = PROCESS_PRE_PASTE_OVERRIDES_MS.get(process_name, PRE_PASTE_DELAY_MS)
        QTimer.singleShot(delay, lambda: self._send_clipboard_tier(clean, stage, process_name))

    def _send_clipboard_tier(self, clean: str, stage: ClipboardStage, process_name: str | None) -> None:
        if not stage.send_paste_keystroke():
            # SendInput itself failed at the OS level — no point waiting to
            # verify a keystroke that was never actually sent.
            self._try_typed_tier(clean, stage, process_name)
            return
        QTimer.singleShot(VERIFY_DELAY_MS, lambda: self._verify_clipboard_tier(clean, stage, process_name))

    def _verify_clipboard_tier(self, clean: str, stage: ClipboardStage, process_name: str | None) -> None:
        if focus_detect.verify_text_present(clean):
            stage.restore_clipboard()
            self._pill.enter_success()
            _log_injection_tier("clipboard", process_name)
        else:
            self._try_typed_tier(clean, stage, process_name)

    def _try_typed_tier(self, clean: str, stage: ClipboardStage, process_name: str | None) -> None:
        inject.inject_text_typed(clean)
        QTimer.singleShot(VERIFY_DELAY_MS, lambda: self._verify_typed_tier(clean, stage, process_name))

    def _verify_typed_tier(self, clean: str, stage: ClipboardStage, process_name: str | None) -> None:
        if focus_detect.verify_text_present(clean):
            # Typing worked; clipboard was never actually needed for the
            # content to land, so just restore the user's original clipboard.
            stage.restore_clipboard()
            self._pill.enter_success()
            _log_injection_tier("typed", process_name)
        else:
            # Uncertain whether either tier landed — both attempts are left
            # in place (not undone), and the clipboard keeps `clean` as a
            # safety net rather than restoring the user's prior contents,
            # same contract as the no-target case, so the user is covered
            # either way ("Pasted — or copy here").
            stage.keep_polished_on_clipboard(clean)
            self._pill.enter_card(clean, subtle_label="Pasted — or copy here")
            _log_injection_tier("card", process_name)

    def _on_polish_failed(self, kind: str, message: str) -> None:
        self._state = AppState.IDLE
        self._pill.enter_error()
        detail = _FAILURE_MESSAGES.get(kind, message)
        self._tray.showMessage(APP_NAME, f"Error: {detail}", QSystemTrayIcon.Warning, 4000)

    def _on_audio_error(self, message: str) -> None:
        self._state = AppState.IDLE
        self._pill.enter_idle()
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

    # -- account (login/logout) ------------------------------------------------

    def _on_login_requested(self) -> None:
        if self._login_thread is not None:
            return  # a login attempt is already in flight
        self._login_thread = QThread(self)
        self._login_worker = auth.LoginWorker()
        self._login_worker.moveToThread(self._login_thread)
        self._login_thread.started.connect(self._login_worker.run)
        self._login_worker.succeeded.connect(self._on_login_succeeded, Qt.QueuedConnection)
        self._login_worker.failed.connect(self._on_login_failed, Qt.QueuedConnection)
        self._login_worker.succeeded.connect(self._login_thread.quit)
        self._login_worker.failed.connect(self._login_thread.quit)
        self._login_thread.finished.connect(self._on_login_thread_finished)
        self._login_thread.start()
        self._tray.showMessage(
            APP_NAME, "Opening your browser to log in…", QSystemTrayIcon.Information, 3000
        )

    def _on_login_thread_finished(self) -> None:
        if self._login_worker is not None:
            self._login_worker.deleteLater()
            self._login_worker = None
        if self._login_thread is not None:
            self._login_thread.deleteLater()
            self._login_thread = None

    def _on_login_succeeded(self, access_token: str, refresh_token: str, expires_at: float) -> None:
        email = auth.fetch_user_email(access_token) or ""
        auth.save_session(auth.AuthSession(access_token, refresh_token, expires_at, email))
        self._tray.set_logged_in_email(email or "Ritely account")
        self._tray.showMessage(
            APP_NAME,
            f"Logged in as {email}" if email else "Logged in",
            QSystemTrayIcon.Information,
            3000,
        )

    def _on_login_failed(self, message: str) -> None:
        self._tray.showMessage(APP_NAME, f"Login failed: {message}", QSystemTrayIcon.Warning, 5000)

    def _on_logout_requested(self) -> None:
        auth.clear_session()
        self._tray.set_logged_in_email(None)
        self._tray.showMessage(APP_NAME, "Logged out", QSystemTrayIcon.Information, 3000)

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
