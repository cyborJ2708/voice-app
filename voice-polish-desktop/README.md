# voice-polish-desktop

Windows system-tray app: press a global hotkey to record your mic, send the
audio to your FastAPI/Gemini backend, and paste the polished text wherever
your cursor is.

## Requirements

- Windows, Python 3.12+
- The backend from `D:\voiceapp\main.py` running and reachable

The backend URL and app-auth token (if the backend has `APP_AUTH_TOKEN` set
— see `D:\voiceapp\.env.example`) are entered once on the first-run welcome
screen and saved to `%APPDATA%\voice-polish-desktop\config.json`. This is a
deliberate change from an earlier env-var-only design: a real installed app
launched from a Start Menu shortcut has no environment variable to read, so
the config file is the only place these can practically live for a
distributable build. As a dev convenience, setting `APP_AUTH_TOKEN` in the
environment before the very first run still seeds the config automatically
(see `config.py`'s docstring) — but it's no longer required.

## Run from source

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

First run shows a one-time welcome screen explaining the hotkey (default
`Ctrl+Win+Space`), with fields for the backend URL / app-auth token and a
practice box — press the hotkey while it's focused to try the real
pipeline. After that, only the tray icon and the overlay pill appear.

## Injection fallback (no target / unverified paste)

After the backend responds, the app checks whether an editable field
currently has keyboard focus — via UI Automation (`focus_detect.py`,
`comtypes`), not just a Win32 HWND/class-name check, since most modern apps
(browsers, Electron apps) render their fields inside one HWND with no
separate child window to inspect at that level.

- **No editable target detected** → the polished text is placed on the
  clipboard (the *prior* clipboard contents are **not** restored — the
  polished text is what's left there on purpose) and the pill expands into
  an interactive card showing the full text with **Copy** and dismiss (✕)
  buttons.
- **Editable target found** → the app pastes as usual, then reads the
  field's value back via UI Automation to confirm the text actually landed.
  Confirmed → clipboard is restored to whatever it held before, as normal.
  Unconfirmed → same card as above (subtitled "Pasted — or copy here"),
  and the clipboard again keeps the polished text rather than restoring,
  so you're covered either way.
- The card stays open until dismissed or until a new recording starts
  (whichever comes first), and never writes its text anywhere but the
  screen and the clipboard — cleared from memory the moment it closes.

This verification is inherently best-effort: there's no way to prove a
target "silently accepted but visually ignored" a paste without reading its
own UI state back, which is exactly what the UI Automation check does — but
some custom-rendered controls (e.g. certain canvas-based editors) don't
expose a readable value at all, and will always land in "unconfirmed" as a
result. That's the safe failure mode by design: you get the card instead of
losing the text.

**Note for this dev machine specifically:** `Ctrl+Win+Space` is already
registered by another running app here (confirmed via a real Win32 error
1409 during development). If registration fails at startup, the app shows a
tray notification and opens the "Change Hotkey" picker automatically so you
can choose a free combination — it doesn't crash or leave the hotkey
non-functional.

## Packaging: exe + installer

Two stages: PyInstaller builds the single-file `.exe`, then Inno Setup
wraps it into a distributable installer.

### Stage 1 — PyInstaller (`voice-polish-desktop.spec`)

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python scripts\generate_icon.py   # only needed if assets/icon.ico is missing
.venv\Scripts\pyinstaller voice-polish-desktop.spec --noconfirm
```

Output: `dist\voice-polish-desktop.exe` — single file, no console window,
custom icon, **~40MB**. Handled pitfalls, each actually verified rather
than assumed:

- **Tray icon asset**: `assets/icon.ico` is read at runtime via a plain
  file path (`Path(__file__).parent / "assets" / "icon.ico"` in `tray.py`),
  not a Python import — PyInstaller's static analysis can't discover it on
  its own. Without an explicit `datas` entry the exe still runs but shows a
  **blank tray icon** (`QIcon` just silently fails to load a nonexistent
  path — no crash, no error). Confirmed this was actually happening, fixed
  via an explicit `datas` entry in the spec, and re-confirmed fixed with a
  frozen-build console test (`QIcon(...).isNull() == False`, all 4 sizes
  present).
- **sounddevice's PortAudio DLL**: bundled automatically by PyInstaller's
  built-in hook; reinforced explicitly via `collect_data_files("sounddevice")`
  as cheap insurance.
- **comtypes' runtime code generation** (`focus_detect.py`'s UI Automation
  calls): comtypes generates a Python module at runtime the first time it's
  needed, which is a known PyInstaller risk area — confirmed working with a
  standalone frozen console build that actually ran `focus_detect.initialize()`
  and `has_editable_focus()`, not just inferred safe.
- **Size trimming**: PyInstaller's PySide6 hook bundles several Qt DLLs
  (Qml, Quick, Pdf, Svg, VirtualKeyboard, OpenGL, Network — several MB
  each) regardless of what the app actually imports, since Python-level
  `excludes` only stops *import* discovery, not the hook's own binary
  collection. Confirmed via `pefile` that this app's actual dependency
  chain (`Qt6Widgets.dll` → `Qt6Gui.dll` → `Qt6Core.dll`) has **zero**
  binary import-table dependency on any of them, then filtered them out of
  `a.binaries` directly in the spec — cut the exe from 52MB → 40MB. Re-ran
  the full app (tray icon, overlay rendering, real hotkey/mic/backend/paste
  loop) against the trimmed build afterward to confirm nothing broke.
- **Overlay rendering**: confirmed the frameless/translucent pill actually
  renders and resizes correctly from the frozen build by finding its live
  window by title+aspect-ratio (`EnumWindows`) during a real recording
  pass, not by eyeballing a screenshot (which, during development, turned
  out to be unreliable — an unrelated on-screen UI element with a similar
  pill shape was initially mistaken for the app's own overlay).
- **No secrets bundled**: see "Security verification" below —
  raw-bytes `grep` on the exe is **not sufficient** (PyInstaller's PYZ
  archive is zlib-compressed, so even a string that's definitely present in
  source, like the literal env-var name `APP_AUTH_TOKEN`, doesn't show up
  in a naive grep). Verified instead by extracting and decompiling this
  app's own modules' actual bytecode.

### Stage 2 — Inno Setup (`installer\voice-polish-desktop.iss`)

```
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\voice-polish-desktop.iss
```

Output: `installer_output\voice-polish-desktop-setup.exe` — **~42MB**
(Inno Setup's LZMA compression on top of an already-compressed PyInstaller
payload yields only marginal further reduction — expected, not a bug).

What it does, each confirmed with a real install/uninstall cycle on this
machine (not just read from the script):

- Installs to `Program Files\voice-polish-desktop` (admin elevation
  required — confirmed via a real UAC prompt).
- Start Menu shortcuts for the app and its uninstaller (confirmed present
  after install).
- Optional **"Start with Windows"** checkbox, unchecked by default →
  writes `HKCU\...\Run\voice-polish-desktop` pointing at the installed exe
  only if checked (confirmed both the key's exact value and that skipping
  the checkbox leaves it absent).
- App name/version/publisher metadata shows correctly in Windows' Apps
  list (confirmed: "voice-polish-desktop 1.0.0" / "Jaydev Ahire").
- Uninstaller removes the Program Files install, Start Menu shortcuts,
  the registry Run key (if present), **and**
  `%APPDATA%\voice-polish-desktop` (config.json, including the app-auth
  token) — confirmed all four are actually gone after a real uninstall run,
  not just present in the `.iss` source.

One caveat worth knowing: Inno Setup itself warns
`PrivilegesRequired=admin` combined with per-user areas (`HKCU`,
`{userappdata}`) can behave unexpectedly if the installer is run "as a
different user" than the one actually logged into the desktop — a rare
scenario for a normal single-user machine, but worth knowing if this app
is ever deployed via an enterprise imaging/admin-push workflow where the
installing account and the eventual end-user account differ.

### Rebuild command sequence (for future updates)

1. Make your code changes.
2. Bump `#define MyAppVersion` in `installer\voice-polish-desktop.iss` (and
   the version reference in this README if you keep one).
3. Rebuild the exe:
   ```
   .venv\Scripts\pyinstaller voice-polish-desktop.spec --noconfirm
   ```
4. Verify no secrets leaked into the new build:
   ```
   .venv\Scripts\python scripts\verify_no_secrets.py
   ```
5. Rebuild the installer:
   ```
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\voice-polish-desktop.iss
   ```
6. Smoke-test the new `installer_output\voice-polish-desktop-setup.exe`
   using the checklist below before shipping it.

Keep `AppId` in the `.iss` file **unchanged** across releases — changing it
makes Windows treat the next install as a separate, parallel app instead of
an in-place upgrade.

### Clean-machine test checklist

Run on a machine that has never had this app or its dependencies installed
(a fresh VM is ideal):

- [ ] Copy just `voice-polish-desktop-setup.exe` over — nothing else.
- [ ] Run it as a non-admin user → confirm a UAC prompt appears and
      installing without approving it is not possible.
- [ ] Install once **without** checking "Start with Windows" → confirm no
      `HKCU\...\Run` entry gets created.
- [ ] Confirm `Program Files\voice-polish-desktop\` contains the exe and
      an uninstaller, and a Start Menu folder/shortcuts exist.
- [ ] Launch the app → first-run welcome window appears → enter your
      backend URL and app-auth token (if your backend requires one) →
      Save & Continue.
- [ ] Confirm the tray icon is visible (not blank) — check the hidden
      icons overflow area if it's not immediately visible.
- [ ] Focus a text field, press the configured hotkey, speak, press it
      again → confirm the pill animates through listening → processing →
      success and the polished text lands.
- [ ] Focus somewhere non-editable (e.g. the desktop) and repeat → confirm
      the pill expands into the fallback card with Copy/dismiss buttons
      instead of losing the text.
- [ ] Test Pause, Change Hotkey (including picking an already-taken combo,
      to confirm the inline error), and Quit from the tray menu.
- [ ] If "Start with Windows" was enabled on a separate install, reboot
      and confirm the app actually auto-starts.
- [ ] Uninstall via Settings → Apps (or the Start Menu shortcut) → confirm
      `Program Files\voice-polish-desktop`, the Start Menu folder,
      `%APPDATA%\voice-polish-desktop` (including the token), and any
      `HKCU\...\Run` entry are all gone afterward.
- [ ] Confirm the backend server itself was never touched by any of the
      above — this app only ever talks to it over HTTP(S), it doesn't
      install or manage it.

## Security notes

- No API keys of any kind live in this app. The Gemini key stays
  server-side, full stop — it's never present in this codebase, this
  app's config, or the packaged exe. The only credential here is the
  app-auth token, entered once on first run and stored in
  `%APPDATA%\voice-polish-desktop\config.json` (see "Requirements" above
  for why this moved off an environment variable) — never logged, never
  sent anywhere except as the `X-App-Token` header on backend requests.
- All backend requests use `requests` with `verify=True` explicit and a
  URL-scheme check: `https://` is required for every host except
  `http://localhost` / `http://127.0.0.1` (a local-dev exception matching
  how the backend runs today — tighten `backend_client._validate_base_url`
  once a real HTTPS deployment exists). Unchanged by the packaging work in
  this session — still enforced in the built exe exactly as in source.
- Audio is captured and held in memory only (`sounddevice` -> `wave` +
  `io.BytesIO`) — no path is ever opened for audio data.
- No transcripts or recognized text are logged locally, anywhere, by this
  app.
- No third-party analytics or telemetry.
- Text returned from the backend is sanitized (`sanitize.py`) before
  injection, and injection itself only ever happens via clipboard paste
  (Ctrl+V) or literal Unicode character typing (`KEYEVENTF_UNICODE`) — never
  through anything that resolves against a shortcut/VK table, so injected
  content structurally cannot trigger an unintended shortcut.
- Global hotkey capture uses the Win32 `RegisterHotKey` API (only reports
  the one registered combination), not a system-wide low-level keyboard
  hook — deliberately, to keep the app's visibility into keystrokes as
  narrow as possible. See `hotkey.py`'s docstring.

### Security verification (packaged build)

Claims below are each backed by an actual check performed against the real
built artifacts, not just source-code reasoning:

- **No secrets in the exe**: raw-bytes `grep` on the compiled exe is
  unreliable — confirmed empirically that it can't even find the literal
  env-var name `APP_AUTH_TOKEN`, despite that string being unambiguously
  present in `config.py`'s source, because PyInstaller's PYZ archive is
  zlib-compressed as a whole. The real check
  (`scripts\verify_no_secrets.py`) extracts and unmarshals this app's own
  12 modules' actual compiled bytecode from the frozen exe's archive and
  inspects every string constant for the real `APP_AUTH_TOKEN` value and
  the real Gemini API key value — both absent. The extraction method was
  itself sanity-checked by confirming it *does* find an expected non-secret
  string (the `APP_AUTH_TOKEN` env-var name itself, and `config.py`'s
  docstring), ruling out a false-clean result from broken tooling. Also
  confirmed no `.env` file exists anywhere under this app's own source
  tree (the backend's separate `.env`, at `D:\voiceapp\.env`, lives
  outside this project entirely and was never a candidate for bundling).
- **HTTPS-only**: `backend_client._validate_base_url` (unchanged by this
  packaging work) rejects any `http://` URL except the documented
  localhost/127.0.0.1 dev exception, in both source and the built exe.
- **Config file isn't world-readable**: checked actual NTFS permissions
  (`icacls`) on a real `config.json` under `%APPDATA%` — only `SYSTEM`,
  `Administrators`, and the owning user account have access; no `Everyone`
  or `Users` grant, so other accounts on the same machine can't read it.
  Its only sensitive field is the app-auth token (a shared app-level
  secret, not the Gemini credential) plus the backend URL — no transcripts,
  no Gemini key, ever.

## Dependencies and CVE review

Checked with `pip-audit -r requirements.txt` (no known vulnerabilities found
at time of writing) plus a manual pass:

| Package | Version | Notes |
|---|---|---|
| `PySide6` | 6.8.2 | No known CVEs in scope. The app does not use `QtWebEngine` (Chromium-derived, where most Qt CVE history concentrates) anywhere. |
| `sounddevice` | 0.5.1 | No known CVEs. Thin ctypes binding over PortAudio; the main supply-chain surface is the bundled PortAudio binary, accounted for at packaging time. |
| `numpy` | 2.2.4 | No known CVEs affecting this version. |
| `requests` | >=2.32.4 | Pinned above 2.32.3 specifically to pick up the fix for CVE-2024-47081 (`.netrc` credential leak via crafted URL netloc parsing). Low practical exploitability here since this app never uses `.netrc`-based auth, but the fix is free and the API is unchanged. |
| `comtypes` | 1.4.16 | No known CVEs. Thin, mature (15+ years) ctypes/COM binding, used only for UI Automation focus/verification calls in `focus_detect.py` — confirmed working inside the frozen `.exe` (its runtime code-generation step is a known PyInstaller risk area, tested directly rather than assumed). |

`pip-audit` is a point-in-time check — re-run it before each release, not
just once.

`pyinstaller` (in `requirements-dev.txt`) is build-time only and never
ships inside the packaged `.exe`.

## What's intentionally not built

- No settings/preferences window beyond the tray's Pause / Change Hotkey /
  Quit and the one-time welcome screen.
- No local feedback-file logging (the backend's existing `/api/feedback`
  endpoint is unrelated and untouched) — there's no UI surface to opt into
  it from, and the simplest reading of "no transcripts logged locally" is
  to just not log them.
- No UI-Automation-based readback for the *typing* fallback path
  specifically — `inject.py`'s `inject()`/`inject_text_typed()` convenience
  functions still only fall back from paste to typing on an OS-level
  `SendInput` failure. The richer flow (UIA target detection + paste
  verification + on-screen card fallback) lives in `focus_detect.py` +
  `inject.attempt_paste()`, wired into `app.py`'s main recording path — see
  "Injection fallback" above.
