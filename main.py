import json
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from google import genai
from google.genai import errors, types

load_dotenv()

# --- config ---------------------------------------------------------------

# Pinned to the current stable (GA, non-preview) Flash model. Note:
# gemini-2.5-flash and gemini-2.0-flash are no longer usable on this key's
# free-tier quota (Google has phased out free-tier grants for older Flash
# generations) — gemini-3.5-flash is the current stable line with quota.
MODEL_NAME = "gemini-3.5-flash"

# Statuses the frontend will retry on (see static/index.html).
RETRYABLE_STATUS_CODES = (429, 503)

BASE_DIR = Path(__file__).parent
PROMPT_PATH = BASE_DIR / "prompt.txt"
FEEDBACK_PATH = BASE_DIR / "feedback.jsonl"

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in."
    )

# Optional shared secret checked on every client request. When unset, the
# server stays open (useful for local `static/index.html` dev usage); set it
# to require callers (e.g. voice-polish-desktop) to send a matching
# `X-App-Token` header. This is an app-level gate, not a Gemini credential —
# the Gemini key never leaves the server.
APP_AUTH_TOKEN = os.environ.get("APP_AUTH_TOKEN")


def verify_app_token(x_app_token: str | None = Header(default=None, alias="X-App-Token")) -> None:
    if not APP_AUTH_TOKEN:
        return
    if not x_app_token or not secrets.compare_digest(x_app_token, APP_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid app token.")


# Per-user identity (distinct from APP_AUTH_TOKEN above, which just gates
# "is this a legitimate copy of the app" — this identifies *who*). Verified
# by calling Supabase's own /auth/v1/user with the caller's token rather
# than checking the JWT signature locally: this works regardless of
# whether the Supabase project signs with HS256 or the newer per-project
# ES256/RS256 keys, at the cost of one extra network round-trip — an
# acceptable trade given /api/polish already makes a Gemini call.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def _verify_supabase_token(authorization: str | None) -> dict | None:
    if not authorization or not authorization.startswith("Bearer ") or not SUPABASE_URL:
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY or ""},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


def require_current_user(authorization: str | None = Header(default=None)) -> dict:
    user = _verify_supabase_token(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid login token.")
    return user


client = genai.Client(api_key=API_KEY)

app = FastAPI()

EXTENSION_MIME_MAP = {
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


def resolve_mime_type(content_type: str | None, filename: str | None) -> str:
    if content_type:
        base = content_type.split(";")[0].strip()
        if base and base != "application/octet-stream":
            return base
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXTENSION_MIME_MAP:
            return EXTENSION_MIME_MAP[ext]
    return "audio/webm"


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    # Deliberately no auth, no Gemini call — a lightweight liveness check
    # for Render's health monitoring and manual "is it awake yet" pings.
    return {"status": "ok"}


@app.post("/api/polish", dependencies=[Depends(verify_app_token)])
async def polish(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload.")

    mime_type = resolve_mime_type(audio.content_type, audio.filename)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # A single Gemini attempt per request. Retries across 429/503 live in the
    # frontend (see static/index.html) so the UI can show "Retrying..."
    # between attempts instead of blocking silently on the backend.
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                thinking_config=types.ThinkingConfig(thinking_budget=768),
            ),
        )
        text = (response.text or "").strip()
        return JSONResponse({"text": text})
    except errors.APIError as exc:
        status_code = exc.code if exc.code in RETRYABLE_STATUS_CODES else 502
        raise HTTPException(status_code=status_code, detail=exc.message or str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini request failed: {exc}")


class FeedbackIn(BaseModel):
    text: str
    rating: str  # "up" or "down"


@app.post("/api/feedback", dependencies=[Depends(verify_app_token)])
def feedback(payload: FeedbackIn):
    if payload.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'.")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_text": payload.text,
        "rating": payload.rating,
    }
    with FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"status": "ok"}


def _supabase_rest_get(path: str, params: dict) -> list | None:
    """GET against Supabase's PostgREST API using the service-role key
    (bypasses RLS — safe here because we already independently verified the
    caller's identity via require_current_user, and every query below
    filters by that verified user_id, never a caller-supplied one)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params=params,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


@app.get("/api/me")
def me(user: dict = Depends(require_current_user)):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Account lookups aren't configured on this server yet.")

    user_id = user.get("id")
    email = user.get("email")

    subs = _supabase_rest_get(
        "subscriptions", {"user_id": f"eq.{user_id}", "select": "plan,status", "limit": "1"}
    )
    plan = subs[0]["plan"] if subs else "free"

    today = date.today().isoformat()
    usage = _supabase_rest_get(
        "usage_daily",
        {"user_id": f"eq.{user_id}", "usage_date": f"eq.{today}", "select": "dictation_count", "limit": "1"},
    )
    dictations_today = usage[0]["dictation_count"] if usage else 0

    return {"id": user_id, "email": email, "plan": plan, "dictations_today": dictations_today}
