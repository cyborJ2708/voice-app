"""System tray icon + 3-item menu (Pause/Resume, Change Hotkey, Quit), plus
the hotkey-capture dialog used both for the manual "Change Hotkey" action
and for recovering from a failed hotkey registration at startup.

TrayIcon itself holds no business logic — it just emits signals.
AppController (app.py) owns what Pause/Change Hotkey/Quit actually do.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

from .hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyManager,
    HotkeySpec,
    qt_key_to_vk,
)

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"


class TrayIcon(QSystemTrayIcon):
    pause_toggled = Signal(bool)         # new paused state
    change_hotkey_requested = Signal()
    quit_requested = Signal()

    def __init__(self, paused: bool = False, parent: QObject | None = None) -> None:
        super().__init__(QIcon(str(ICON_PATH)), parent)
        self.setToolTip("voice-polish-desktop")

        self._paused = paused
        self._menu = QMenu()
        self._pause_action = QAction(self._pause_label(), self._menu)
        self._pause_action.triggered.connect(self._on_pause_clicked)

        change_hotkey_action = QAction("Change Hotkey…", self._menu)
        change_hotkey_action.triggered.connect(self.change_hotkey_requested.emit)

        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self.quit_requested.emit)

        self._menu.addAction(self._pause_action)
        self._menu.addAction(change_hotkey_action)
        self._menu.addSeparator()
        self._menu.addAction(quit_action)
        self._menu.aboutToShow.connect(self._refresh_pause_label)
        self.setContextMenu(self._menu)

    def _pause_label(self) -> str:
        return "Resume" if self._paused else "Pause"

    def _refresh_pause_label(self) -> None:
        self._pause_action.setText(self._pause_label())

    def _on_pause_clicked(self) -> None:
        self.set_paused(not self._paused)
        self.pause_toggled.emit(self._paused)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause_action.setText(self._pause_label())


class HotkeyCaptureDialog(QDialog):
    """Captures the next modifier+key combo pressed while it has focus."""

    def __init__(self, hotkey_manager: HotkeyManager, current: HotkeySpec, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change Hotkey")
        self.setFixedSize(360, 160)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self._hotkey_manager = hotkey_manager
        self._previous_spec = current
        self._candidate: HotkeySpec | None = None
        self._held_modifiers = 0

        self._prompt = QLabel("Press a new key combination…")
        self._prompt.setAlignment(Qt.AlignCenter)
        self._prompt.setStyleSheet("font-size: 15px; font-weight: 600; padding: 8px;")

        self._error_label = QLabel("")
        self._error_label.setAlignment(Qt.AlignCenter)
        self._error_label.setStyleSheet("color: #dc2626; font-size: 12px;")
        self._error_label.setWordWrap(True)

        self._save_button = QPushButton("Save")
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._on_save)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons = QDialogButtonBox()
        buttons.addButton(self._save_button, QDialogButtonBox.AcceptRole)
        buttons.addButton(cancel_button, QDialogButtonBox.RejectRole)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(self._prompt)
        layout.addWidget(self._error_label)
        layout.addStretch()
        layout.addWidget(buttons)

        self.setFocusPolicy(Qt.StrongFocus)

    # -- capture --------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        mods = self._qt_modifiers_to_bitmask(event.modifiers())
        vk = qt_key_to_vk(event.key())

        if vk is None:
            # bare modifier press (or an untranslatable key) — show progress
            self._held_modifiers = mods
            self._prompt.setText(self._format_label(mods, None) + "…")
            return

        if mods == 0:
            self._prompt.setText("Add a modifier (Ctrl/Alt/Shift/Win) before the key")
            return

        self._candidate = HotkeySpec(modifiers=mods, vk=vk)
        self._prompt.setText(self._candidate.label)
        self._error_label.setText("")
        self._save_button.setEnabled(True)

    @staticmethod
    def _qt_modifiers_to_bitmask(qt_mods) -> int:
        bitmask = 0
        if qt_mods & Qt.ControlModifier:
            bitmask |= MOD_CONTROL
        if qt_mods & Qt.MetaModifier:  # the Windows key, on Windows
            bitmask |= MOD_WIN
        if qt_mods & Qt.AltModifier:
            bitmask |= MOD_ALT
        if qt_mods & Qt.ShiftModifier:
            bitmask |= MOD_SHIFT
        return bitmask

    @staticmethod
    def _format_label(mods: int, vk: int | None) -> str:
        spec = HotkeySpec(modifiers=mods, vk=vk or 0x20)
        label = spec.label
        return label.rsplit("+", 1)[0] if vk is None and mods else label

    # -- save/cancel ------------------------------------------------------------

    def _on_save(self) -> None:
        if self._candidate is None:
            return
        ok = self._hotkey_manager.register(self._candidate)
        if ok:
            self.accept()
            return

        self._error_label.setText(
            f"Couldn't use {self._candidate.label} — it's already in use. Try another combo."
        )
        self._save_button.setEnabled(False)
        self._candidate = None
        # Don't leave the app with no hotkey while the dialog is still open.
        self._hotkey_manager.register(self._previous_spec)

    def result_spec(self) -> HotkeySpec | None:
        """The newly-registered spec, if the dialog was accepted."""
        return self._candidate if self.result() == QDialog.Accepted else None
