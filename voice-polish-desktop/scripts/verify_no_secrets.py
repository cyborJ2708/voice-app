"""Extracts our own modules' compiled bytecode from the frozen exe's PYZ
archive and inspects every string constant for secret values — a real
verification, not just a raw-bytes grep (which can't see into the
compressed archive at all, as directly confirmed: grepping the exe for the
known env-var NAME "APP_AUTH_TOKEN" found zero matches even though it's
unambiguously present in config.py's source, purely because the archive
is compressed).

Secret values to check for are read from the environment, never hardcoded
here — this script itself lives in source control, so hardcoding a real
token/API key in it would defeat the entire point of a "no secrets in the
repo" check.
"""
import dis
import marshal
import os
import sys

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

EXE_PATH = "dist/voice-polish-desktop.exe"

SECRET_VALUES = [
    v for v in (os.environ.get("APP_AUTH_TOKEN"), os.environ.get("GEMINI_API_KEY")) if v
]
if not SECRET_VALUES:
    print(
        "WARNING: neither APP_AUTH_TOKEN nor GEMINI_API_KEY is set in the "
        "environment — nothing to check for. Set at least one before running "
        "this script for a meaningful result."
    )

OUR_MODULES = [
    "voice_polish_desktop.config",
    "voice_polish_desktop.backend_client",
    "voice_polish_desktop.app",
    "voice_polish_desktop.hotkey",
    "voice_polish_desktop.tray",
    "voice_polish_desktop.welcome",
    "voice_polish_desktop.audio",
    "voice_polish_desktop.inject",
    "voice_polish_desktop.focus_detect",
    "voice_polish_desktop.winkeys",
    "voice_polish_desktop.sanitize",
    "voice_polish_desktop.overlay",
]


def all_code_constants(code, seen=None):
    """Recursively walk a code object's co_consts for every string constant."""
    if seen is None:
        seen = set()
    for const in code.co_consts:
        if isinstance(const, str):
            seen.add(const)
        elif hasattr(const, "co_consts"):
            all_code_constants(const, seen)
    return seen


def main() -> int:
    carchive = CArchiveReader(EXE_PATH)
    toc = carchive.toc

    pyz_entry = None
    for name, entry in toc.items():
        if "PYZ" in name or name.startswith("PYZ-"):
            pyz_entry = name
            break
    if pyz_entry is None:
        # In recent PyInstaller, the PYZ is just one of the CArchive members
        # named like 'PYZ-00.pyz' — find it by typecode 'z' if name match failed.
        for name, entry in toc.items():
            if entry[-1] == "z":
                pyz_entry = name
                break

    print(f"PYZ archive entry: {pyz_entry}")
    pyz_data = carchive.extract(pyz_entry)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as tmp:
        tmp.write(pyz_data)
        tmp_path = tmp.name

    pyz = ZlibArchiveReader(tmp_path)

    findings = []
    checked = 0
    for module_name in OUR_MODULES:
        if module_name not in pyz.toc:
            print(f"  ! {module_name} not found in PYZ toc (checking anyway not possible)")
            continue
        checked += 1
        code = pyz.extract(module_name)
        strings = all_code_constants(code)
        for secret in SECRET_VALUES:
            if any(secret in s for s in strings):
                findings.append((module_name, secret))

    print(f"Checked {checked}/{len(OUR_MODULES)} of our own modules' decompiled bytecode constants.")
    if findings:
        print("FAIL: secret value(s) found embedded in bytecode:")
        for module_name, secret in findings:
            print(f"  {module_name}: contains {secret[:12]}...")
        return 1

    print("PASS: no secret values found in any of our modules' decompiled string constants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
