import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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

# Razorpay webhook signature secret (set when configuring the webhook URL in
# Razorpay's dashboard — see the setup walkthrough).
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")

# Subscription *creation* also lives here, not on the website — the website
# is deployed on Cloudflare Workers via OpenNext, which turned out to never
# bind dashboard-set "Variables and Secrets" into anything the app could
# actually read (confirmed empirically: every RAZORPAY_* var showed up as
# missing at runtime no matter how it was configured, while this exact same
# plain os.environ.get() pattern has worked reliably all along on Render).
# Moving the one piece of Razorpay logic that needs real secrets to this
# service sidesteps that platform issue entirely, at the cost of the
# browser calling this backend directly (see CORS setup below) instead of
# a same-origin Next.js route.
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

# India-only for now, by explicit choice — US/EU plan ids are deferred, so
# there's no real region-detection step here at all yet (this backend has
# no Cloudflare in front of it, so no CF-IPCountry-equivalent signal exists
# either way). Revisit when US/EU launch: likely by having the website's
# own /api/pricing (which *can* see CF-IPCountry) hand this endpoint a
# region, or by adding real IP geolocation here.
RAZORPAY_PLAN_IDS = {
    ("IN", "monthly"): os.environ.get("RAZORPAY_PLAN_PRO_IN_MONTHLY"),
    ("IN", "annual"): os.environ.get("RAZORPAY_PLAN_PRO_IN_ANNUAL"),
}
RAZORPAY_TOTAL_COUNT = {"monthly": 120, "annual": 10}  # see razorpay.ts's old note: no "forever" option exists

# Transactional email (welcome + "you're now Pro") — sent from here via
# Resend's HTTP API directly, since neither is a Supabase Auth email type
# (those only cover confirm-signup/reset-password/magic-link/etc., configured
# separately in the Supabase dashboard with Resend as custom SMTP).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Ritely <hello@ritelyapp.com>")

# Shared secret Supabase's Database Webhook (on auth.users INSERT) sends as
# a custom header — same verification pattern as APP_AUTH_TOKEN above, just
# a separate secret since this guards a different caller.
SUPABASE_WEBHOOK_SECRET = os.environ.get("SUPABASE_WEBHOOK_SECRET")


def _send_email(to_email: str, subject: str, html: str) -> None:
    if not RESEND_API_KEY or not to_email:
        return
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM_EMAIL, "to": [to_email], "subject": subject, "html": html},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        # Best-effort — a failed welcome/receipt email should never break
        # signup or subscription activation.
        print(f"[email] failed to send {subject!r} to {to_email!r}", file=sys.stderr)


# Same dark-card/indigo-button visual language as the Supabase auth email
# templates (D:\ritely\supabase\templates\*.html) — kept as a literal copy
# here since this service has no access to that repo's files at runtime.
# The `bgcolor` attribute alongside `background-color` is the "bulletproof
# button" pattern: some renderers strip inline background-color specifically
# off <a> tags, but leave it on <td>, so the color survives either way.
_EMAIL_LOGO_URL = "https://ritelyapp.com/logo-email.png"


def _email_shell(heading: str, paragraph: str, button_label: str, button_url: str, footnote: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<meta name="x-apple-disable-message-reformatting">
<title>{heading}</title>
</head>
<body style="margin:0; padding:0; background-color:#0c0c0e;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#0c0c0e;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px; max-width:100%; background-color:#17171b; border:1px solid #2c2c33; border-radius:16px; overflow:hidden;">
          <tr>
            <td align="center" style="padding:32px 32px 0 32px;">
              <img src="{_EMAIL_LOGO_URL}" width="48" height="48" alt="Ritely" style="display:block; border-radius:12px;">
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:20px 32px 0 32px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
              <h1 style="margin:0; font-size:22px; line-height:1.3; font-weight:600; color:#ffffff;">{heading}</h1>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:12px 32px 0 32px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
              <p style="margin:0; font-size:14px; line-height:1.6; color:#b4b0c9;">{paragraph}</p>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:28px 32px 8px 32px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#7c6cff" style="background-color:#7c6cff; border-radius:999px;">
                    <a href="{button_url}" style="display:inline-block; padding:13px 32px; color:#ffffff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; font-size:14px; font-weight:600; text-decoration:none; border-radius:999px;">
                      {button_label}
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:20px 32px 32px 32px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
              <p style="margin:0; font-size:12px; line-height:1.6; color:#75758c;">{footnote}</p>
            </td>
          </tr>
        </table>
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px; max-width:100%;">
          <tr>
            <td align="center" style="padding:20px 32px 0 32px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
              <p style="margin:0; font-size:11px; line-height:1.6; color:#4d4d5c;">
                Ritely &middot; <a href="https://ritelyapp.com" style="color:#4d4d5c;">ritelyapp.com</a>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _welcome_email_html() -> str:
    return _email_shell(
        heading="Welcome to Ritely",
        paragraph=(
            "Speak any language and get polished, natural English typed anywhere on Windows "
            "&mdash; one hotkey, system-wide. Your free plan includes 4,000 words a week, no card required."
        ),
        button_label="Open your dashboard",
        button_url="https://ritelyapp.com/dashboard",
        footnote="Haven&rsquo;t installed the app yet? Grab it from your dashboard&rsquo;s download card.",
    )


def _pro_welcome_email_html() -> str:
    return _email_shell(
        heading="You're now on Pro",
        paragraph=(
            "Thanks for upgrading &mdash; your weekly word limit is gone. Dictate as much as you "
            "want, on every device you use Ritely."
        ),
        button_label="Open your dashboard",
        button_url="https://ritelyapp.com/dashboard",
        footnote="You can manage or cancel your subscription anytime from your dashboard.",
    )


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

# Only /api/razorpay/create-subscription is ever called directly by browser
# JS (see D:\ritely\src\app\pricing\PricingClient.tsx) — every other
# endpoint is called by either the desktop app (Python's `requests`, not
# subject to CORS) or the website's own server-side code (same-origin from
# the browser's perspective). Restricted to the real site origin, not "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ritelyapp.com"],
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)

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

    # Quota is only enforced for verified, logged-in users — there's no
    # identity to track it against otherwise (matches how dictionary
    # personalization already skips silently when user is None).
    usage = None
    if user is not None and SUPABASE_SERVICE_ROLE_KEY:
        system_prompt = _apply_dictionary_terms(system_prompt, user["id"])
        usage = _load_current_usage(user["id"])
        if usage["plan"] == "free" and usage["words_used_this_period"] >= FREE_WEEKLY_WORD_LIMIT:
            # Rejected before ever calling Gemini — the whole point is to
            # not pay for a request we're not going to allow.
            return JSONResponse(
                status_code=402,
                content={
                    "error": "quota_exceeded",
                    "reset_date": _next_period_start(usage).isoformat(),
                },
            )

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

        result = {"text": text}
        if usage is not None:
            # Allowed to run over the cap on this final request rather than
            # cutting it off mid-dictation — the limit check above is what
            # actually blocks the *next* one.
            word_count = len(text.split()) if text else 0
            new_total = _increment_words_used(user["id"], usage["words_used_this_period"], word_count)
            usage["words_used_this_period"] = new_total
            result.update(
                {
                    "plan": usage["plan"],
                    "words_used": new_total,
                    "words_remaining": _words_remaining(usage),
                    "reset_date": _next_period_start(usage).isoformat(),
                }
            )
        return JSONResponse(result)
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


def _supabase_rest_patch(path: str, params: dict, body: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            params=params,
            json=body,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return False
    return resp.status_code in (200, 204)


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


# --- subscription plans + free-tier quota -----------------------------------
#
# user_usage table: one row per user, service-role writes only (RLS blocks
# any client-side insert/update — see supabase_user_usage.sql). The free
# tier resets weekly, anchored to a fixed Monday 00:00 IST instant for every
# user regardless of their actual location — simpler and more predictable
# than a per-user timezone, and matches how the pricing page advertises the
# limit ("resets every Monday 00:00 IST").

FREE_WEEKLY_WORD_LIMIT = 4000
IST_OFFSET = timedelta(hours=5, minutes=30)  # fixed offset — India observes no DST


def _period_start_for(now_utc: datetime) -> datetime:
    """The most recent Monday 00:00 IST at or before now_utc, as a UTC instant."""
    shifted = now_utc + IST_OFFSET
    monday_midnight_shifted = shifted.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=shifted.weekday()
    )
    return monday_midnight_shifted - IST_OFFSET


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _next_period_start(usage_row: dict) -> datetime:
    return _parse_iso(usage_row["period_start"]) + timedelta(days=7)


def _words_remaining(usage_row: dict) -> int | None:
    if usage_row["plan"] != "free":
        return None
    return max(0, FREE_WEEKLY_WORD_LIMIT - usage_row["words_used_this_period"])


def _load_current_usage(user_id: str) -> dict:
    """Gets (creating if absent) the user's usage row, resetting it first if
    a Monday-IST boundary has passed since period_start. Callers never need
    to reason about staleness themselves — the returned row is always
    current as of `now`."""
    rows = _supabase_rest_get(
        "user_usage",
        {
            "user_id": f"eq.{user_id}",
            "select": "plan,words_used_this_period,period_start,subscription_status,subscription_expires_at",
            "limit": "1",
        },
    )
    row = rows[0] if rows else None
    now_utc = datetime.now(timezone.utc)

    if row is None:
        created = _supabase_rest_post(
            "user_usage",
            {
                "user_id": user_id,
                "plan": "free",
                "words_used_this_period": 0,
                "period_start": _period_start_for(now_utc).isoformat(),
            },
        )
        return created or {
            "plan": "free",
            "words_used_this_period": 0,
            "period_start": now_utc.isoformat(),
            "subscription_status": None,
            "subscription_expires_at": None,
        }

    current_boundary = _period_start_for(now_utc)
    if _parse_iso(row["period_start"]) < current_boundary:
        _supabase_rest_patch(
            "user_usage",
            {"user_id": f"eq.{user_id}"},
            {"words_used_this_period": 0, "period_start": current_boundary.isoformat()},
        )
        row["words_used_this_period"] = 0
        row["period_start"] = current_boundary.isoformat()

    return row


def _increment_words_used(user_id: str, current_total: int, additional_words: int) -> int:
    new_total = current_total + additional_words
    _supabase_rest_patch("user_usage", {"user_id": f"eq.{user_id}"}, {"words_used_this_period": new_total})
    return new_total


@app.get("/api/me")
def me(user: dict = Depends(require_current_user)):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Account lookups aren't configured on this server yet.")

    user_id = user.get("id")
    email = user.get("email")

    today = date.today().isoformat()
    usage_daily = _supabase_rest_get(
        "usage_daily",
        {"user_id": f"eq.{user_id}", "usage_date": f"eq.{today}", "select": "dictation_count", "limit": "1"},
    )
    dictations_today = usage_daily[0]["dictation_count"] if usage_daily else 0

    quota = _load_current_usage(user_id)

    return {
        "id": user_id,
        "email": email,
        "plan": quota["plan"],
        "dictations_today": dictations_today,
        "words_used": quota["words_used_this_period"],
        "words_remaining": _words_remaining(quota),
        "reset_date": _next_period_start(quota).isoformat(),
        "subscription_status": quota.get("subscription_status"),
        "subscription_expires_at": quota.get("subscription_expires_at"),
    }


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


# --- insights / history (Stage 5) -------------------------------------------
#
# Metadata only, never transcript/text content — matches the app's "audio
# and text are never stored" invariant. The desktop app calls
# /api/dictation-events once per dictation attempt (after its own tiered
# injection pipeline resolves, since only the client knows which tier
# landed); this is also the only place usage_daily.dictation_count actually
# gets incremented (previously read-only — nothing ever wrote to it).

VALID_OUTCOMES = {"success", "empty", "error"}
VALID_INJECTION_TIERS = {"clipboard", "typed", "card", "no_target"}


MAX_REASONABLE_WORD_COUNT = 100_000  # sanity cap, not a real product limit


class DictationEventIn(BaseModel):
    outcome: str
    injection_tier: str | None = None
    word_count: int | None = None


@app.post("/api/dictation-events")
def log_dictation_event(payload: DictationEventIn, user: dict = Depends(require_current_user)):
    if payload.outcome not in VALID_OUTCOMES:
        raise HTTPException(status_code=400, detail="invalid outcome")
    if payload.injection_tier is not None and payload.injection_tier not in VALID_INJECTION_TIERS:
        raise HTTPException(status_code=400, detail="invalid injection_tier")
    if payload.word_count is not None and not (0 <= payload.word_count <= MAX_REASONABLE_WORD_COUNT):
        raise HTTPException(status_code=400, detail="invalid word_count")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Insights aren't configured on this server yet.")

    user_id = user["id"]
    row = _supabase_rest_post(
        "dictation_events",
        {
            "user_id": user_id,
            "outcome": payload.outcome,
            "injection_tier": payload.injection_tier,
            "word_count": payload.word_count,
        },
    )
    if row is None:
        raise HTTPException(status_code=502, detail="Could not log the event.")

    if payload.outcome == "success":
        _increment_usage_daily(user_id)

    return row


def _increment_usage_daily(user_id: str) -> None:
    today = date.today().isoformat()
    existing = _supabase_rest_get(
        "usage_daily",
        {"user_id": f"eq.{user_id}", "usage_date": f"eq.{today}", "select": "dictation_count", "limit": "1"},
    )
    if existing:
        _supabase_rest_patch(
            "usage_daily",
            {"user_id": f"eq.{user_id}", "usage_date": f"eq.{today}"},
            {"dictation_count": existing[0]["dictation_count"] + 1},
        )
    else:
        _supabase_rest_post("usage_daily", {"user_id": user_id, "usage_date": today, "dictation_count": 1})


@app.get("/api/history")
def get_history(user: dict = Depends(require_current_user), limit: int = 50):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Insights aren't configured on this server yet.")
    limit = max(1, min(limit, 200))
    rows = _supabase_rest_get(
        "dictation_events",
        {
            "user_id": f"eq.{user['id']}",
            "select": "id,created_at,outcome,injection_tier,word_count",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )
    return {"events": rows or []}


@app.get("/api/insights")
def get_insights(user: dict = Depends(require_current_user)):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Insights aren't configured on this server yet.")

    user_id = user["id"]
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = _supabase_rest_get(
        "dictation_events",
        {
            "user_id": f"eq.{user_id}",
            "select": "outcome,injection_tier,created_at,word_count",
            "created_at": f"gte.{since}",
            "limit": "5000",
        },
    ) or []

    total = len(rows)
    successes = [r for r in rows if r["outcome"] == "success"]
    tier_counts: dict[str, int] = {}
    for r in successes:
        tier = r.get("injection_tier") or "unknown"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    daily_counts: dict[str, int] = {}
    for r in rows:
        day = str(r["created_at"])[:10]
        daily_counts[day] = daily_counts.get(day, 0) + 1

    total_words = sum(r.get("word_count") or 0 for r in successes)

    return {
        "total_last_30_days": total,
        "successes_last_30_days": len(successes),
        "total_words_last_30_days": total_words,
        "tier_breakdown": tier_counts,
        "daily_counts": daily_counts,
    }


# --- Razorpay subscription checkout + webhook (Stage 6) ----------------------
#
# The desktop app never touches payment at all (it only reads plan/usage via
# /api/me and links out to the website to upgrade). The website's own
# checkout UI (pricing page) calls the endpoint below directly from browser
# JS — this is the ONE place the browser talks to this backend rather than
# the website's own server, because that's where the Razorpay secrets
# actually work reliably (see RAZORPAY_KEY_ID's comment above). Plan
# activation itself only ever happens via the signature-verified webhook
# further down — a client-side checkout "success" callback is never trusted
# for entitlement.


class CreateSubscriptionIn(BaseModel):
    interval: str  # "monthly" or "annual"


@app.post("/api/razorpay/create-subscription")
def create_razorpay_subscription(payload: CreateSubscriptionIn, user: dict = Depends(require_current_user)):
    if payload.interval not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="interval must be 'monthly' or 'annual'.")
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=501, detail="Payments aren't configured on this server yet.")

    # India-only for now (see RAZORPAY_PLAN_IDS's comment above).
    plan_id = RAZORPAY_PLAN_IDS.get(("IN", payload.interval))
    if not plan_id:
        raise HTTPException(status_code=501, detail="Pro isn't available in your region yet.")

    try:
        resp = requests.post(
            "https://api.razorpay.com/v1/subscriptions",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json={
                "plan_id": plan_id,
                "customer_notify": 1,
                "total_count": RAZORPAY_TOTAL_COUNT[payload.interval],
                # Correlates every future webhook event back to our own
                # user without a separate mapping table — Razorpay echoes
                # `notes` back verbatim on every subscription.* webhook
                # payload (see the webhook handler below).
                "notes": {"user_id": user["id"], "email": user.get("email", "")},
            },
            timeout=15,
        )
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Razorpay.")

    if not resp.ok:
        raise HTTPException(status_code=502, detail="Razorpay rejected the subscription request.")

    subscription_id = resp.json().get("id")
    if not subscription_id:
        raise HTTPException(status_code=502, detail="Razorpay response was missing a subscription id.")

    return {"subscription_id": subscription_id, "key_id": RAZORPAY_KEY_ID}


@app.post("/api/razorpay/cancel-subscription")
def cancel_razorpay_subscription(user: dict = Depends(require_current_user)):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=501, detail="Payments aren't configured on this server yet.")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=501, detail="Subscriptions aren't configured on this server yet.")

    rows = _supabase_rest_get(
        "user_usage",
        {"user_id": f"eq.{user['id']}", "select": "razorpay_subscription_id,plan", "limit": "1"},
    )
    row = rows[0] if rows else None
    subscription_id = row.get("razorpay_subscription_id") if row else None
    if not row or row.get("plan") != "pro" or not subscription_id:
        raise HTTPException(status_code=400, detail="No active Pro subscription to cancel.")

    try:
        resp = requests.post(
            f"https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            # Finishes out the period already paid for rather than cutting
            # access immediately — Pro features only actually go away once
            # Razorpay's subscription.cancelled webhook fires at the real
            # cycle end (same "webhook is the only source of truth for
            # entitlement" rule as everywhere else here; this endpoint only
            # ever sets a "cancel is pending" status, never plan=free itself).
            json={"cancel_at_cycle_end": 1},
            timeout=15,
        )
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach Razorpay.")

    if not resp.ok:
        raise HTTPException(status_code=502, detail="Razorpay couldn't cancel the subscription.")

    _supabase_rest_patch(
        "user_usage", {"user_id": f"eq.{user['id']}"}, {"subscription_status": "cancel_at_cycle_end"}
    )

    return {"status": "ok"}


class SupabaseUserWebhookIn(BaseModel):
    type: str
    record: dict | None = None


@app.post("/api/webhooks/supabase-user-created")
async def supabase_user_created_webhook(
    payload: SupabaseUserWebhookIn,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    # Configured as a Supabase Database Webhook on auth.users, INSERT only
    # (Database → Webhooks in the dashboard) — fires at signup, before email
    # confirmation, same moment Supabase's own "Confirm signup" email goes
    # out. Verified via a custom header (set on the webhook's HTTP config in
    # Supabase) rather than Razorpay-style HMAC, since Supabase's Database
    # Webhooks don't sign their payloads.
    if (
        not SUPABASE_WEBHOOK_SECRET
        or not x_webhook_secret
        or not secrets.compare_digest(x_webhook_secret, SUPABASE_WEBHOOK_SECRET)
    ):
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    if payload.type != "INSERT" or not payload.record:
        return {"status": "ignored"}

    email = payload.record.get("email")
    if email:
        _send_email(email, "Welcome to Ritely", _welcome_email_html())

    return {"status": "ok"}


_RAZORPAY_ACTIVE_EVENTS = {"subscription.activated", "subscription.charged"}
_RAZORPAY_INACTIVE_EVENTS = {"subscription.cancelled", "subscription.halted"}


def _verify_razorpay_signature(body: bytes, signature: str) -> bool:
    if not RAZORPAY_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _upsert_user_usage_subscription(
    user_id: str,
    plan: str,
    subscription_status: str,
    subscription_expires_at: str | None,
    razorpay_subscription_id: str,
) -> None:
    fields = {
        "plan": plan,
        "subscription_status": subscription_status,
        "subscription_expires_at": subscription_expires_at,
        "razorpay_subscription_id": razorpay_subscription_id,
    }
    existing = _supabase_rest_get("user_usage", {"user_id": f"eq.{user_id}", "select": "user_id", "limit": "1"})
    if existing:
        _supabase_rest_patch("user_usage", {"user_id": f"eq.{user_id}"}, fields)
        return
    # Webhook can arrive for a user who's never made a dictation request yet
    # (no row exists) — create it rather than silently dropping the event.
    now_utc = datetime.now(timezone.utc)
    _supabase_rest_post(
        "user_usage",
        {
            "user_id": user_id,
            "words_used_this_period": 0,
            "period_start": _period_start_for(now_utc).isoformat(),
            **fields,
        },
    )


@app.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    # TEMPORARY diagnostic logging — visible in Render's Logs tab, never
    # prints the signature/secret itself, just enough to see which branch a
    # real delivery took. Remove once the webhook is confirmed working.
    print(f"[razorpay webhook] received, body_len={len(body)}, has_signature={bool(signature)}", file=sys.stderr)

    if not _verify_razorpay_signature(body, signature):
        print("[razorpay webhook] REJECTED: signature did not verify", file=sys.stderr)
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        payload = json.loads(body)
    except ValueError:
        print("[razorpay webhook] REJECTED: body was not valid JSON", file=sys.stderr)
        raise HTTPException(status_code=400, detail="invalid JSON")

    event = payload.get("event")
    entity = (payload.get("payload") or {}).get("subscription", {}).get("entity", {})
    # Set at subscription-creation time (see the create-subscription endpoint
    # above's `notes`) and echoed back verbatim on every webhook for that
    # subscription — the only correlation between a Razorpay object and our
    # own user_id.
    user_id = (entity.get("notes") or {}).get("user_id")
    subscription_id = entity.get("id")
    print(
        f"[razorpay webhook] event={event!r} subscription_id={subscription_id!r} "
        f"user_id={user_id!r} notes={entity.get('notes')!r}",
        file=sys.stderr,
    )

    if not user_id or not subscription_id or event not in (_RAZORPAY_ACTIVE_EVENTS | _RAZORPAY_INACTIVE_EVENTS):
        print("[razorpay webhook] IGNORED: missing user_id/subscription_id or unhandled event type", file=sys.stderr)
        return {"status": "ignored"}

    if not SUPABASE_SERVICE_ROLE_KEY:
        print("[razorpay webhook] REJECTED: SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        raise HTTPException(status_code=501, detail="Subscriptions aren't configured on this server yet.")

    if event in _RAZORPAY_ACTIVE_EVENTS:
        current_end = entity.get("current_end")
        expires_at = (
            datetime.fromtimestamp(current_end, tz=timezone.utc).isoformat() if current_end else None
        )
        _upsert_user_usage_subscription(user_id, "pro", "active", expires_at, subscription_id)
        print(f"[razorpay webhook] APPLIED: user_id={user_id!r} -> plan=pro, expires_at={expires_at!r}", file=sys.stderr)
        # Only on first activation, not every recurring "subscription.charged"
        # renewal — this is a one-time "you're now Pro" announcement, not a
        # monthly billing receipt.
        if event == "subscription.activated":
            email = (entity.get("notes") or {}).get("email")
            if email:
                _send_email(email, "You're now Ritely Pro!", _pro_welcome_email_html())
    else:
        status = event.split(".", 1)[1]  # "cancelled" or "halted"
        _upsert_user_usage_subscription(user_id, "free", status, None, subscription_id)
        print(f"[razorpay webhook] APPLIED: user_id={user_id!r} -> plan=free, status={status!r}", file=sys.stderr)

    return {"status": "ok"}
