r"""Real end-to-end test of the loopback login flow against the live,
deployed ritelyapp.com + Supabase project. Opens your actual default
browser — log in (or it'll already be logged in if your browser session
from earlier testing is still active) and this script reports what came
back.

Usage:
    ..\.venv\Scripts\python.exe test_login_flow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QCoreApplication

from voice_polish_desktop import auth

app = QCoreApplication(sys.argv)
worker = auth.LoginWorker()


def on_succeeded(access_token: str, refresh_token: str, expires_at: float) -> None:
    print("SUCCEEDED")
    print("access_token (first 20 chars):", access_token[:20], "...")
    print("refresh_token (first 20 chars):", refresh_token[:20], "...")
    print("expires_at:", expires_at)
    email = auth.fetch_user_email(access_token)
    print("email:", email)
    if email:
        session = auth.AuthSession(access_token, refresh_token, expires_at, email)
        auth.save_session(session)
        print("Session saved to Credential Manager.")
        loaded = auth.load_session()
        print("Reloaded from Credential Manager:", loaded)
    app.quit()


def on_failed(message: str) -> None:
    print("FAILED:", message)
    app.quit()


worker.succeeded.connect(on_succeeded)
worker.failed.connect(on_failed)

print("Opening browser to ritelyapp.com/auth/desktop ... waiting for callback (up to 5 min)")
worker.run()  # blocking is fine here — this script has nothing else to do; signals
              # fire synchronously (direct connection, same thread) before this returns
sys.exit(0)
