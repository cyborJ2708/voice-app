"""Browser-based login (loopback OAuth-style handoff) against the Ritely
website's Supabase-backed auth, plus persistent session storage.

Flow:
  1. start_login() picks a free localhost port, starts a one-shot HTTP
     server on it, and opens the user's default browser to
     {WEBSITE_URL}/auth/desktop?port=...&state=...
  2. The website (already logged in, or after the user logs in there)
     redirects the browser to http://127.0.0.1:{port}/callback with the
     Supabase access/refresh tokens as query params, echoing back `state`.
  3. The local server verifies `state` matches (defense against another
     local process racing to claim the port — see RFC 8252 §7.3), captures
     the tokens, shows a "you can close this tab" page, and shuts down.

No API keys/secrets here: SUPABASE_ANON_KEY below is the same public,
client-safe key already embedded in the website's own JS bundle — it
identifies the Supabase project, it does not grant any access by itself
(Row Level Security on every table is what actually protects data).

Tokens are never written to a config file or anywhere in plaintext — only
to Windows Credential Manager via the `keyring` package.
"""
from __future__ import annotations

import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import keyring
import keyring.errors
import requests
from PySide6.QtCore import QObject, Signal

WEBSITE_URL = "https://ritelyapp.com"
SUPABASE_URL = "https://yyyafkjcerdfflnwyvvv.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl5"
    "eWFma2pjZXJkZmZsbnd5dnZ2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ0NzA4OTgsImV4"
    "cCI6MjEwMDA0Njg5OH0.I6jONasKvvhZX24Hfoo0uErjImCkDu4OHf8XlFEOQmY"
)

_SERVICE = "ritely-desktop"

_LOGIN_TIMEOUT_S = 300  # give up if the browser flow isn't completed in 5 min
_REFRESH_SKEW_S = 60    # refresh a bit before actual expiry, not exactly at it


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str
    expires_at: float  # unix epoch seconds
    email: str


# -- persistent storage (Windows Credential Manager via keyring) -------------
#
# Windows Credential Manager caps a single credential's blob at ~2560 bytes
# (CRED_MAX_CREDENTIAL_BLOB_SIZE), stored as UTF-16 internally — in practice
# that's roughly 1200-1300 ASCII characters before CredWrite starts failing
# with "the stub received bad data" (confirmed empirically: 1200 chars
# succeeded, 1800 failed). A real Supabase access token (a signed JWT with
# the user's claims) routinely exceeds that. Rather than fall back to a
# plaintext file — which the spec explicitly rules out — long values are
# split across multiple credential entries and reassembled on read, so
# everything genuinely stays inside Credential Manager.
_CHUNK_SIZE = 900  # comfortably under the ~1200-char practical limit


def _set_long_value(key: str, value: str) -> None:
    chunks = [value[i : i + _CHUNK_SIZE] for i in range(0, len(value), _CHUNK_SIZE)] or [""]
    keyring.set_password(_SERVICE, f"{key}__count", str(len(chunks)))
    for i, chunk in enumerate(chunks):
        keyring.set_password(_SERVICE, f"{key}__{i}", chunk)


def _get_long_value(key: str) -> str | None:
    count_raw = keyring.get_password(_SERVICE, f"{key}__count")
    if count_raw is None:
        return None
    try:
        count = int(count_raw)
    except ValueError:
        return None
    parts = []
    for i in range(count):
        part = keyring.get_password(_SERVICE, f"{key}__{i}")
        if part is None:
            return None  # a chunk went missing — treat the whole value as absent
        parts.append(part)
    return "".join(parts)


def _delete_long_value(key: str) -> None:
    count_raw = keyring.get_password(_SERVICE, f"{key}__count")
    try:
        count = int(count_raw) if count_raw is not None else 0
    except ValueError:
        count = 0
    for i in range(count):
        try:
            keyring.delete_password(_SERVICE, f"{key}__{i}")
        except keyring.errors.PasswordDeleteError:
            pass
    try:
        keyring.delete_password(_SERVICE, f"{key}__count")
    except keyring.errors.PasswordDeleteError:
        pass


def save_session(session: AuthSession) -> None:
    _set_long_value("access_token", session.access_token)
    _set_long_value("refresh_token", session.refresh_token)
    keyring.set_password(_SERVICE, "expires_at", str(session.expires_at))
    keyring.set_password(_SERVICE, "email", session.email)


def load_session() -> AuthSession | None:
    access_token = _get_long_value("access_token")
    refresh_token = _get_long_value("refresh_token")
    if not access_token or not refresh_token:
        return None
    try:
        expires_at = float(keyring.get_password(_SERVICE, "expires_at") or "0")
    except ValueError:
        expires_at = 0.0
    email = keyring.get_password(_SERVICE, "email") or ""
    return AuthSession(access_token, refresh_token, expires_at, email)


def clear_session() -> None:
    _delete_long_value("access_token")
    _delete_long_value("refresh_token")
    for key in ("expires_at", "email"):
        try:
            keyring.delete_password(_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent — logging out when not logged in is a no-op


# -- Supabase calls -----------------------------------------------------------


def fetch_user_email(access_token: str) -> str | None:
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {access_token}", "apikey": SUPABASE_ANON_KEY},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("email")


def _refresh(refresh_token: str) -> AuthSession | None:
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None

    data = resp.json()
    access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token")
    if not access_token or not new_refresh_token:
        return None

    expires_at = float(data.get("expires_at") or (time.time() + data.get("expires_in", 3600)))
    email = fetch_user_email(access_token) or ""
    session = AuthSession(access_token, new_refresh_token, expires_at, email)
    save_session(session)
    return session


def get_valid_access_token() -> str | None:
    """Returns a currently-usable access token, silently refreshing if the
    stored one is expired/near-expiry. Returns None if there's no session,
    or if refresh fails (refresh token itself expired/revoked) — callers
    should treat None as "needs to log in again", not retry blindly.
    """
    session = load_session()
    if session is None:
        return None
    if time.time() < session.expires_at - _REFRESH_SKEW_S:
        return session.access_token
    refreshed = _refresh(session.refresh_token)
    if refreshed is None:
        clear_session()
        return None
    return refreshed.access_token


# -- loopback login flow -------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib override)
        parsed = urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        if not state or state != self.server.expected_state:  # type: ignore[attr-defined]
            self.server.result = ("error", "Login request didn't match — please try again.")  # type: ignore[attr-defined]
            self._respond("Something went wrong. Please try logging in again from Ritely.")
            return

        access_token = params.get("access_token", [""])[0]
        refresh_token = params.get("refresh_token", [""])[0]
        expires_at_raw = params.get("expires_at", [""])[0]
        if not access_token or not refresh_token:
            self.server.result = ("error", "Login didn't complete — missing tokens.")  # type: ignore[attr-defined]
            self._respond("Login didn't complete. Please try again.")
            return

        try:
            expires_at = float(expires_at_raw)
        except ValueError:
            expires_at = time.time() + 3600

        self.server.result = ("ok", access_token, refresh_token, expires_at)  # type: ignore[attr-defined]
        self._respond("You're connected! You can close this tab and return to Ritely.")

    def _respond(self, message: str) -> None:
        body = (
            "<!doctype html><html><body style=\"font-family:-apple-system,'Segoe UI',"
            "sans-serif;display:flex;align-items:center;justify-content:center;"
            "height:100vh;margin:0;background:#0b0b0d;color:#fff;\">"
            f"<p>{message}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # no request logging — nothing useful and nothing sensitive to log either way


class _CallbackServer(HTTPServer):
    def __init__(self, expected_state: str) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.expected_state = expected_state
        self.result: tuple | None = None


class LoginWorker(QObject):
    """Runs the loopback server off the Qt main thread — handle_request()
    blocks until the browser hits the callback (or times out), so this must
    not run on the UI thread. See app.py for the moveToThread wiring."""

    succeeded = Signal(str, str, float)  # access_token, refresh_token, expires_at
    failed = Signal(str)

    def run(self) -> None:
        state = secrets.token_urlsafe(24)
        try:
            server = _CallbackServer(state)
        except OSError as exc:
            self.failed.emit(f"Couldn't start local login listener: {exc}")
            return

        port = server.server_address[1]
        webbrowser.open(f"{WEBSITE_URL}/auth/desktop?port={port}&state={state}")

        server.timeout = _LOGIN_TIMEOUT_S
        server.handle_request()
        server.server_close()

        if server.result is None:
            self.failed.emit("Login timed out. Please try again.")
            return
        if server.result[0] == "error":
            self.failed.emit(server.result[1])
            return

        _, access_token, refresh_token, expires_at = server.result
        self.succeeded.emit(access_token, refresh_token, expires_at)
