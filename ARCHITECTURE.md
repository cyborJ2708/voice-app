# Ritely (voice-polish) — Technical Architecture

This document describes how the system is structured end-to-end: the Windows desktop
client, the backend API, the AI model call, and how the two communicate. Written for
internal documentation purposes.

---

## 1. High-level data flow

```
[User speaks]
     │  (global hotkey toggles recording)
     ▼
[Desktop app: microphone capture, in-memory only]
     │  WAV bytes (16kHz, mono, 16-bit PCM), never written to disk
     ▼
[Desktop app: HTTPS POST multipart/form-data → backend]
     │  X-App-Token header (shared-secret auth)
     ▼
[FastAPI backend: /api/polish]
     │  audio bytes + system prompt → Gemini API
     ▼
[Google Gemini API: gemini-3.5-flash]
     │  returns polished English text
     ▼
[FastAPI backend: returns { "text": "..." } as JSON]
     │  HTTPS response
     ▼
[Desktop app: sanitize_text() — strips control chars/ANSI/BOM]
     │
     ▼
[Desktop app: tiered injection — clipboard paste → verify → simulated typing →
 verify → fallback "copy" card]
     │
     ▼
[Polished text lands in whatever app had keyboard focus]
```

Nothing in this pipeline writes audio or transcript text to disk at any point, on
either the client or the server.

---

## 2. Tech stack

| Layer | Technology |
|---|---|
| Backend language | Python 3 |
| Backend framework | FastAPI 0.139.2 |
| ASGI server | Uvicorn 0.51.0 (with `[standard]` extras — uvloop/httptools) |
| AI model SDK | `google-genai` 2.12.1 (official Google GenAI Python SDK) |
| Model | `gemini-3.5-flash` (Google Gemini, current stable/GA Flash line) |
| Config/secrets | `python-dotenv` 1.2.2 (`.env` file, never committed) |
| Multipart upload parsing | `python-multipart` 0.0.32 |
| Hosting | Render (free tier), deployed from a private GitHub repo |
| Desktop client language | Python 3 |
| Desktop UI framework | PySide6 (Qt for Python) |
| Audio capture | `sounddevice` (PortAudio bindings) + stdlib `wave`/`io` |
| Numeric processing | `numpy` (RMS level calculation for the waveform UI) |
| HTTP client | `requests` |
| Windows integration | `ctypes` (raw Win32 API calls) + `comtypes` (UI Automation) |
| Packaging | PyInstaller (single windowed .exe) + Inno Setup (installer) |

---

## 3. Backend structure (`D:\voiceapp\`)

### `main.py` — the entire API surface
A single-file FastAPI app. Routes:

- **`GET /`** — serves a static HTML test page (`static/index.html`), used for manual
  browser-based testing without the desktop client.
- **`GET /health`** — liveness check, no auth, no Gemini call. Used by Render's health
  monitor and for manually checking if the free-tier instance has cold-started yet.
- **`POST /api/polish`** *(auth-gated)* — the core endpoint. Accepts a multipart file
  upload (`audio`), sends it to Gemini, returns `{"text": "..."}`.
- **`POST /api/feedback`** *(auth-gated)* — accepts `{"text": ..., "rating": "up"|"down"}`
  and appends it to a local `feedback.jsonl` file (thumbs up/down on the polished
  result, no audio, opt-in from the desktop app's practice box).

### `prompt.txt`
The full system prompt sent to Gemini as `system_instruction` on every request (see
below, §5). Kept in its own file rather than inlined in code so it can be iterated on
without touching `main.py`.

### `.env` (gitignored, never committed)
Holds the two real secrets:
- `GEMINI_API_KEY` — the actual Google GenAI API key. This **never** leaves the server;
  the desktop client never sees it.
- `APP_AUTH_TOKEN` — a shared secret the desktop client must send back on every
  request (see §6). Optional: if unset, the server accepts unauthenticated requests
  (used only for local dev via the browser test page).

### `requirements.txt`
Pinned dependency versions (see table above) — deliberately minimal, well-known
packages, reviewed for known CVEs before pinning.

### `static/index.html`
A plain HTML/JS test harness — lets you record from a browser mic and hit `/api/polish`
directly, useful for testing the backend in isolation from the desktop app.

---

## 4. How audio gets from the mic to the request

1. **Capture** (`voice_polish_desktop/audio.py`): `sounddevice.InputStream` opens the
   default microphone at 16kHz / mono / 16-bit PCM (falling back to the device's native
   sample rate if 16kHz isn't supported). Audio arrives in a callback on PortAudio's own
   thread — each chunk is appended to an in-memory Python list (`self._chunks`), **never
   written to a file**. The same callback computes a rolling RMS amplitude and emits it
   as a Qt signal, which drives the waveform animation in the floating pill UI.
2. **Stop & encode**: when recording stops, the raw PCM chunks are joined and wrapped
   into a complete WAV container entirely in memory, using the stdlib `wave` module
   writing into an `io.BytesIO()` buffer — at no point does this touch disk.
3. **Upload** (`voice_polish_desktop/backend_client.py`): the WAV bytes are POSTed as
   `multipart/form-data` to `{backend_url}/api/polish`, field name `audio`, filename
   `recording.wav`, content-type `audio/wav`.

---

## 5. The model call (backend side)

Inside `POST /api/polish` (`main.py`):

```python
response = client.models.generate_content(
    model="gemini-3.5-flash",
    contents=[types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)],
    config=types.GenerateContentConfig(
        system_instruction=system_prompt,       # from prompt.txt
        thinking_config=types.ThinkingConfig(thinking_budget=768),
    ),
)
```

- **The audio file itself is the entire user-content payload** — there's no separate
  transcription step; Gemini receives the raw audio and is instructed (via the system
  prompt) to both understand it and rewrite it in one pass.
- **`thinking_budget=768`** caps the model's internal reasoning tokens — small enough to
  keep latency low, large enough for the model to work through code-switched, informally
  spoken input before producing final text.
- **System prompt** (`prompt.txt`, reproduced in full):

  > You will receive a voice recording from a speaker who may mix Marathi, Hindi, and
  > English in the same sentence, with filler words (umm, matlab, you know), false
  > starts, and self-corrections.
  > Your job is NOT word-for-word translation. Your job is to understand what the
  > speaker is trying to communicate, and write it the way a fluent, professional
  > English speaker would naturally express that same intent.
  > Before writing, understand not just the register (casual vs formal) but the
  > speaker's emotional tone — grateful, angry, apologetic, sad, excited, urgent.
  > Preserve that emotion in the output at its natural intensity. Never neutralize
  > genuine emotion into generic politeness.
  >
  > Rules:
  > - Preserve intent and information exactly — never add points the speaker didn't
  >   make, never drop details they did (names, dates, numbers, tasks).
  > - Match the tone register: if they're speaking casually to a friend, output casual
  >   English; if formally to a boss or professor, output polished formal English. Do
  >   not make casual messages stiff.
  > - If the speaker corrects themselves ("Monday... no wait, Tuesday"), keep only
  >   their final intent.
  > - Remove fillers, false starts, and repetition.
  > - Replace Indian English idioms with natural equivalents (e.g., "do the needful" →
  >   "please take care of this", "revert back" → "reply", "prepone" → "move up").
  > - Keep it the length the speaker intended — do not expand a two-line message into
  >   a paragraph.
  > - Output ONLY the final text. No explanations, no options, no labels like "Polished
  >   version:", and no quotation marks around the output.

- **Result extraction**: `response.text` is stripped and returned as `{"text": "..."}`.
  If Gemini returns an error, the backend maps it: 429/503 pass through as-is (so the
  client knows to retry), anything else becomes a generic 502.

---

## 6. Authentication between client and backend

There's no user login on the desktop app — instead, a single shared secret gates the
API:

- `APP_AUTH_TOKEN` is set as an environment variable on the Render server.
- The desktop app stores the same value in its local config (`%APPDATA%\voice-polish-desktop\config.json`,
  entered once during first-run setup) and sends it as an `X-App-Token` header on every
  request.
- The backend's `verify_app_token` dependency does a constant-time comparison
  (`secrets.compare_digest`) and returns `401` if it's missing or wrong.
- This is deliberately **not** a per-user credential — it's an app-level gate to keep
  the endpoint from being hit by arbitrary internet traffic, not a multi-tenant auth
  system. (The separate Ritely *website*, by contrast, uses real per-user Supabase Auth
  — that's a different product surface with accounts/subscriptions, documented
  separately.)

---

## 7. Latency management

Several deliberate decisions keep perceived latency low and failures well-behaved:

- **`thinking_budget=768`** (backend) — bounds how much internal reasoning Gemini does
  per request, trading a small amount of output quality ceiling for consistently fast
  responses on short dictation-length audio clips.
- **Client-side timeouts** (`backend_client.py`): `connect_timeout=10s`,
  `read_timeout=75s`. The read timeout was deliberately widened from an initial 30s
  after real load testing showed Render's free tier queuing concurrent requests under
  load, with genuine (non-failed) responses taking up to ~45-60s — 30s was misreporting
  slow-but-healthy requests as network failures.
- **Cold starts**: Render's free tier spins the container down after ~15 minutes idle;
  the next request pays a ~30-60s cold-start penalty. This is why the read timeout has
  real headroom above typical warm-request latency (a few seconds) rather than being
  tuned tightly to it.
- **Retry policy**: on `429` (rate limited) or `503` (overloaded), the client retries up
  to 3 attempts with a 2-second delay between them, before surfacing a failure. `401`
  (auth) and network-level errors (DNS, connection refused, TLS) fail immediately with
  no retry, since retrying those can't help.
- **Non-blocking UI**: the entire backend call runs on a separate `QThread` (via the Qt
  `moveToThread` pattern, not thread subclassing), so the floating pill's waveform
  animation and the global-hotkey listener stay responsive for the whole duration of
  the network call — the user sees a live "processing" animation rather than a frozen
  UI.

---

## 8. How the "paste back" works (client side)

Once polished text comes back, it goes through a **tiered injection pipeline**
(`voice_polish_desktop/inject.py` + `app.py`), because different Windows apps handle
synthetic paste/keystrokes differently:

1. **Sanitize first** (`sanitize.py`): strips a leading BOM, ANSI escape sequences, C0/C1
   control characters (keeping tabs/newlines — legitimate for multi-line dictation),
   one layer of wrapping quotation marks, and hard-truncates at 20,000 characters. This
   is a defensive backstop against a malformed/adversarial model response, not the
   primary safeguard — see next point.
2. **Tier 1 — clipboard + Ctrl+V**: the user's current clipboard contents are
   snapshotted (every MIME format, not just text), the polished text is placed on the
   clipboard, and a synthetic Ctrl+V is sent via the Win32 `SendInput` API after a short
   configurable delay (150ms, or 350ms for apps like Notion that were found to lag
   focus/DOM updates). After a further verification delay (550ms), the app reads back
   the focused UI element's text via **UI Automation** (`focus_detect.py`, using
   `comtypes`) to confirm the paste actually landed.
3. **Tier 2 — simulated Unicode typing**: if tier 1 can't be verified (or `SendInput`
   itself failed at the OS level), the app falls back to typing the text character-by-
   character using `KEYEVENTF_UNICODE` — this types literal Unicode code points rather
   than resolving through any virtual-key/shortcut table, so typed content can never
   trigger an unintended keyboard shortcut. Verified the same way via UI Automation.
4. **Tier 3 — fallback card**: if neither tier can be confirmed, the app shows a small
   on-screen card with the polished text and a "Copy" button, and leaves the text on
   the clipboard as a safety net — the user is never left without the result, even in
   apps that silently swallow both paste and simulated typing.
5. **Clipboard restoration**: if tier 1 or 2 is confirmed successful, the user's
   original clipboard contents are restored automatically. If nothing could be
   confirmed, the polished text is deliberately *left* on the clipboard instead
   (better to leave something useful than restore silently and lose the result).

An opt-in local debug log (off by default, enabled via an environment variable) records
*which tier succeeded for which target process* — e.g. `process=notion.exe tier=typed`
— for diagnosing app-specific injection issues. It never logs the injected text itself.

---

## 9. File-by-file reference

### Backend (`D:\voiceapp\`)
| File | Purpose |
|---|---|
| `main.py` | FastAPI app: routes, auth gate, Gemini call |
| `prompt.txt` | System prompt sent to Gemini |
| `.env` / `.env.example` | Secrets (real / template) |
| `requirements.txt` | Pinned Python dependencies |
| `static/index.html` | Browser-based manual test harness |
| `feedback.jsonl` | Local append-only log of thumbs up/down feedback (gitignored) |

### Desktop client (`D:\voiceapp\voice-polish-desktop\src\voice_polish_desktop\`)
| File | Purpose |
|---|---|
| `app.py` | `AppController` — the state machine tying every module together |
| `audio.py` | In-memory mic capture → WAV bytes |
| `backend_client.py` | HTTPS client for `/api/polish`, retries, timeouts, error types |
| `sanitize.py` | Strips control chars/ANSI/BOM from model output before injection |
| `inject.py` | Clipboard snapshot/restore, paste, and typed-injection primitives |
| `winkeys.py` | Low-level `SendInput`/`KEYEVENTF_UNICODE` Win32 wrapper |
| `hotkey.py` | Global hotkey registration (`RegisterHotKey`) + Qt native event filter |
| `focus_detect.py` | UI Automation: is the focus editable? did the text actually land? |
| `overlay.py` | The floating pill UI (idle/listening/processing/success/error/card states) |
| `tray.py` | System tray icon + menu (Pause, Change Hotkey, Quit) |
| `welcome.py` | First-run welcome window (hotkey label, backend URL/token entry, practice box) |
| `config.py` | Reads/writes `%APPDATA%\voice-polish-desktop\config.json` |

---

## 10. Security summary

- Gemini API key lives only on the server; the desktop app never has it.
- All client↔server traffic is HTTPS with certificate verification explicitly enabled
  (`verify=True`), except an explicit `http://localhost`/`127.0.0.1` exception for local
  development.
- Shared-secret `X-App-Token` gates the API from arbitrary internet traffic.
- Audio is never written to disk on either side, at any point.
- Model output is sanitized before injection, and injection itself uses only paste and
  literal-Unicode typing — neither path can resolve into a keyboard shortcut.
- No third-party analytics or telemetry anywhere in the pipeline.
