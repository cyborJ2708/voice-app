"""First-run welcome window — one screen, shown once, never reopened.

The practice text box gets the real end-to-end pipeline "for free": the
global hotkey, recording, backend call, and injection are all focus-target-
agnostic (inject.py pastes wherever the OS focus currently is), so this
window doesn't need any special wiring beyond being a normal editable field
the user can click into before pressing the hotkey.

Also collects the backend URL and app-auth token here — this is the one
and only place either is ever entered, since a distributable installed app
has no environment variable to read them from (see config.py's docstring).
Not a "settings page": it's the same one-time first-run screen, just
carrying the connection fields alongside the hotkey explanation rather than
adding a second window.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#4f46e5"

_FIELD_STYLE = (
    "QLineEdit {"
    "  border: 1px solid rgba(128, 128, 128, 0.35);"
    "  border-radius: 8px;"
    "  padding: 8px 10px;"
    "  font-size: 13px;"
    "}"
)


class WelcomeWindow(QWidget):
    finished = Signal(str, str)  # (backend_url, app_auth_token)

    def __init__(
        self,
        hotkey_label: str,
        backend_url: str = "",
        app_auth_token: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("voice-polish")
        self.setFixedSize(480, 520)
        self.setAttribute(Qt.WA_DeleteOnClose)

        headline = QLabel("Say it, polish it, paste it.")
        headline.setStyleSheet("font-size: 22px; font-weight: 700;")
        headline.setWordWrap(True)

        body = QLabel(
            f"Press <b>{hotkey_label}</b> anywhere to start recording. Press it again "
            "to stop — your speech is sent to your backend, polished, and pasted "
            "right where your cursor is."
        )
        body.setWordWrap(True)
        body.setStyleSheet("font-size: 14px; opacity: 0.85; line-height: 1.5;")

        connection_label = QLabel("Connection")
        connection_label.setStyleSheet("font-size: 12px; font-weight: 600; opacity: 0.7;")

        url_label = QLabel("Backend URL")
        url_label.setStyleSheet("font-size: 11px; opacity: 0.6;")
        self._url_field = QLineEdit(backend_url)
        self._url_field.setPlaceholderText("https://your-backend.example.com")
        self._url_field.setStyleSheet(_FIELD_STYLE)

        token_label = QLabel("App auth token (only if your backend requires one)")
        token_label.setStyleSheet("font-size: 11px; opacity: 0.6;")
        self._token_field = QLineEdit(app_auth_token)
        self._token_field.setEchoMode(QLineEdit.Password)
        self._token_field.setPlaceholderText("leave blank if not required")
        self._token_field.setStyleSheet(_FIELD_STYLE)

        practice_label = QLabel("Try it here:")
        practice_label.setStyleSheet("font-size: 12px; font-weight: 600; opacity: 0.7;")

        self._practice_box = QTextEdit()
        self._practice_box.setPlaceholderText(
            f"Click here, then press {hotkey_label} and say something…"
        )
        self._practice_box.setStyleSheet(
            "QTextEdit {"
            "  border: 1px solid rgba(128, 128, 128, 0.35);"
            "  border-radius: 10px;"
            "  padding: 12px;"
            "  font-size: 14px;"
            "}"
        )
        self._practice_box.setMinimumHeight(100)

        got_it = QPushButton("Save && Continue")
        got_it.setCursor(Qt.PointingHandCursor)
        got_it.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {ACCENT};"
            f"  color: white;"
            f"  border: none;"
            f"  border-radius: 10px;"
            f"  padding: 10px 22px;"
            f"  font-size: 14px;"
            f"  font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{ background-color: #4338ca; }}"
        )
        got_it.clicked.connect(self._on_got_it)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 28)
        layout.setSpacing(10)
        layout.addWidget(headline)
        layout.addWidget(body)
        layout.addSpacing(6)
        layout.addWidget(connection_label)
        layout.addWidget(url_label)
        layout.addWidget(self._url_field)
        layout.addWidget(token_label)
        layout.addWidget(self._token_field)
        layout.addSpacing(6)
        layout.addWidget(practice_label)
        layout.addWidget(self._practice_box)
        layout.addStretch()
        layout.addWidget(got_it, alignment=Qt.AlignRight)

    def _on_got_it(self) -> None:
        self.finished.emit(self._url_field.text().strip(), self._token_field.text().strip())
        self.close()
