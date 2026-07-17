"""Local config persistence in %APPDATA%\\voice-polish-desktop\\config.json.

This file is created at first run (not by the installer, which doesn't
know the user's token) with a placeholder empty `app_auth_token` — the
welcome window prompts for it when empty (see welcome.py) and persists it
here on save. This is a deliberate change from the original design (token
only ever in a process environment variable, never on disk): a real
installed app, launched from a Start Menu shortcut, has no environment
variable to read — the config file is the only place it can practically
live for a distributable build. As a one-time convenience for existing
dev/test setups, `_defaults()` seeds the token from `APP_AUTH_TOKEN` in the
environment *only when no config file exists yet* — once config.json
exists, its stored value is authoritative and the environment variable is
no longer consulted.

Hard invariant, enforced structurally rather than by convention: AppConfig
has no field for the backend's Gemini API key (that never leaves the
server) and no field for transcript/text content — save() only ever writes
the five fields below, there is no generic passthrough that could
accidentally leak a transcript to disk. The app-auth token itself IS
stored here by design (see above) — it is a shared app-level secret, not
the Gemini credential, and this file lives under the current Windows
user's own profile (%APPDATA%), not world-readable by other OS accounts.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .hotkey import DEFAULT_HOTKEY, HotkeySpec

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "voice-polish-desktop"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_BACKEND_BASE_URL = "http://localhost:8000"


@dataclass
class AppConfig:
    hotkey: HotkeySpec
    paused: bool
    first_run_complete: bool
    backend_base_url: str
    app_auth_token: str


def _defaults() -> AppConfig:
    return AppConfig(
        hotkey=DEFAULT_HOTKEY,
        paused=False,
        first_run_complete=False,
        backend_base_url=DEFAULT_BACKEND_BASE_URL,
        # One-time seed for continuity with the previous env-var-only setup;
        # only used when config.json doesn't exist yet — see module docstring.
        app_auth_token=os.environ.get("APP_AUTH_TOKEN", ""),
    )


def load() -> AppConfig:
    if not CONFIG_PATH.exists():
        return _defaults()

    defaults = _defaults()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults

    try:
        hotkey = HotkeySpec.from_json(raw["hotkey"])
    except (KeyError, TypeError, ValueError):
        hotkey = defaults.hotkey

    return AppConfig(
        hotkey=hotkey,
        paused=bool(raw.get("paused", defaults.paused)),
        first_run_complete=bool(raw.get("first_run_complete", defaults.first_run_complete)),
        backend_base_url=str(raw.get("backend_base_url", defaults.backend_base_url)),
        app_auth_token=str(raw.get("app_auth_token", "")),
    )


def save(config: AppConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "hotkey": config.hotkey.to_json(),
        "paused": config.paused,
        "first_run_complete": config.first_run_complete,
        "backend_base_url": config.backend_base_url,
        "app_auth_token": config.app_auth_token,
    }
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, CONFIG_PATH)
