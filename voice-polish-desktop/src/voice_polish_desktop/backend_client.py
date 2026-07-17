"""HTTPS client for the backend's /api/polish endpoint.

Security notes:
  - `verify=True` is passed explicitly on every request (it's the requests
    default, but the security spec calls for cert verification, so it's
    stated rather than implied).
  - The app-auth token is passed in at construction (from AppConfig —
    see config.py) and sent as `X-App-Token`. This module itself never
    reads or writes config.json/environment variables directly — it's a
    pure transport layer, so there's exactly one place (config.py) that
    decides where the token comes from.
  - The base URL's scheme is validated: https:// is required for every host
    except http://localhost and http://127.0.0.1, which are allowed as a
    local-dev exception (matches how the backend is run today). See
    _validate_base_url — kept as a single isolated function so tightening
    this policy later (e.g. dropping the localhost exception once a real
    deployment exists) is a one-line change.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QObject, Signal

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
MAX_ATTEMPTS = 3
RETRY_DELAY_S = 2.0
RETRYABLE_STATUS = (429, 503)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


class BackendError(Exception):
    """Base for all backend_client errors."""


class NetworkError(BackendError):
    """Connection refused, DNS failure, timeout, TLS error, etc."""


class AuthError(BackendError):
    """401 — missing/invalid X-App-Token."""


class ServerError(BackendError):
    """Any other non-2xx response, after retries are exhausted where applicable."""


class EmptyResultError(BackendError):
    """200 OK but the response contained no usable text."""


def _validate_base_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return url.rstrip("/")
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return url.rstrip("/")
    raise ValueError(
        f"Refusing insecure backend URL {url!r}: https:// is required "
        f"(http:// is only allowed for localhost/127.0.0.1)."
    )


class BackendClient:
    def __init__(self, base_url: str, app_auth_token: str = "") -> None:
        self._base_url = _validate_base_url(base_url)
        self._app_auth_token = app_auth_token

    def polish(self, wav_bytes: bytes) -> str:
        """Blocking. Runs the retry loop in-process. Raises BackendError subtypes."""
        url = f"{self._base_url}/api/polish"
        headers = {}
        if self._app_auth_token:
            headers["X-App-Token"] = self._app_auth_token

        last_detail = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            files = {"audio": ("recording.wav", wav_bytes, "audio/wav")}
            try:
                resp = requests.post(
                    url,
                    files=files,
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                    verify=True,
                )
            except requests.exceptions.RequestException as exc:
                raise NetworkError(str(exc)) from exc

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ServerError(f"invalid JSON response: {exc}") from exc
                text = (data.get("text") or "").strip()
                if not text:
                    raise EmptyResultError("backend returned no text")
                return text

            if resp.status_code == 401:
                raise AuthError("missing or invalid app auth token")

            detail = _extract_detail(resp)
            last_detail = detail or f"HTTP {resp.status_code}"

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_S)
                continue

            raise ServerError(f"{resp.status_code}: {last_detail}")

        raise ServerError(last_detail or "request failed after retries")


def _extract_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        detail = data.get("detail")
        return str(detail) if detail else ""
    except ValueError:
        return ""


class PolishWorker(QObject):
    """Runs BackendClient.polish() off the Qt main thread.

    Standard Qt moveToThread pattern (not QThread subclassing) — see app.py
    for the wiring. Keeps the pill's animation timer and the hotkey's native
    event filter responsive during the (potentially several-second) HTTP call.
    """

    succeeded = Signal(str)       # polished text
    failed = Signal(str, str)     # (kind, message) — kind: network/auth/server/empty

    def __init__(self, client: BackendClient, wav_bytes: bytes) -> None:
        super().__init__()
        self._client = client
        self._wav_bytes = wav_bytes

    def run(self) -> None:
        try:
            text = self._client.polish(self._wav_bytes)
            self.succeeded.emit(text)
        except AuthError as exc:
            self.failed.emit("auth", str(exc))
        except NetworkError as exc:
            self.failed.emit("network", str(exc))
        except EmptyResultError as exc:
            self.failed.emit("empty", str(exc))
        except ServerError as exc:
            self.failed.emit("server", str(exc))
