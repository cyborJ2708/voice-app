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


def optional_current_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Like require_current_user, but never raises — /api/polish works fine
    logged out (dictionary personalization is just skipped), unlike /api/me
    or the dictionary CRUD endpoints, which are meaningless without a user."""
    return _verify_supabase_token(authorization)


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
async def polish(audio: UploadFile = File(...), user: dict | None = Depends(optional_current_user)):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload.")

    mime_type = resolve_mime_type(audio.content_type, audio.filename)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    if user is not None:
        system_prompt = _apply_dictionary_terms(system_prompt, user["id"])

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


def _supabase_rest_post(path: str, body: dict) -> dict | None:
    """POST a single row via PostgREST, returning the inserted row (with its
    generated id) — same service-role-bypasses-RLS reasoning as
    _supabase_rest_get, and same caveat: callers must only ever pass an
    already-verified user_id, never one taken from client input."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=body,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code not in (200, 201):
        return None
    rows = resp.json()
    return rows[0] if rows else None


def _supabase_rest_delete(path: str, params: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=representation",
            },
            params=params,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return False
    if resp.status_code not in (200, 204):
        return False
    if resp.status_code == 204:
        return True
    return bool(resp.json())


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


# --- dictionary (Stage 4) --------------------------------------------------
#
# Custom per-user terms merged into the Gemini system prompt at polish time
# (see _apply_dictionary_terms below and its call in /api/polish), so
# proper nouns / jargon / names get transcribed the way the user actually
# spells them rather than the nearest common word. dictionary_terms table:
# see supabase_dictionary_terms.sql.

MAX_DICTIONARY_TERM_LENGTH = 200


class DictionaryTermIn(BaseModel):
    term: str


@app.get("/api/dictionary")
def list_dictionary(user: dict = Depends(require_current_user)):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Dictionary isn't configured on this server yet.")
    rows = _supabase_rest_get(
        "dictionary_terms", {"user_id": f"eq.{user['id']}", "select": "id,term", "order": "term.asc"}
    )
    return {"terms": rows or []}


@app.post("/api/dictionary")
def add_dictionary_term(payload: DictionaryTermIn, user: dict = Depends(require_current_user)):
    term = payload.term.strip()
    if not term:
        raise HTTPException(status_code=400, detail="term must not be empty.")
    if len(term) > MAX_DICTIONARY_TERM_LENGTH:
        raise HTTPException(status_code=400, detail="term is too long.")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Dictionary isn't configured on this server yet.")

    row = _supabase_rest_post("dictionary_terms", {"user_id": user["id"], "term": term})
    if row is None:
        raise HTTPException(status_code=502, detail="Could not save the term.")
    return row


@app.delete("/api/dictionary/{term_id}")
def delete_dictionary_term(term_id: str, user: dict = Depends(require_current_user)):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Dictionary isn't configured on this server yet.")
    # Filtering by both id AND the verified user_id (never a caller-supplied
    # one) means a user can never delete another user's term even by
    # guessing/enumerating ids.
    ok = _supabase_rest_delete("dictionary_terms", {"id": f"eq.{term_id}", "user_id": f"eq.{user['id']}"})
    if not ok:
        raise HTTPException(status_code=404, detail="Term not found.")
    return {"status": "ok"}


def _apply_dictionary_terms(system_prompt: str, user_id: str) -> str:
    terms = _supabase_rest_get(
        "dictionary_terms",
        {"user_id": f"eq.{user_id}", "select": "term", "order": "term.asc", "limit": "300"},
    )
    if not terms:
        return system_prompt
    term_list = "\n".join(f"- {t['term']}" for t in terms)
    return (
        f"{system_prompt}\n\n"
        "Custom Dictionary — the user has specifically registered these words, "
        "names, and terms. When you hear something that sounds like one of "
        "these, spell and capitalize it exactly as given below, even if it "
        "sounds like a different common word:\n"
        f"{term_list}"
    )
