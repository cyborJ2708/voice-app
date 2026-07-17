"""Smoke test for sanitize.sanitize_text — literal before/after assertions."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_polish_desktop.sanitize import MAX_LENGTH, sanitize_text

CASES = [
    ("leading BOM stripped", chr(0xFEFF) + "hello", "hello"),
    ("ANSI escape stripped", "\x1b[31mred\x1b[0m text", "red text"),
    ("control chars stripped, tab/newline kept", "a\x00b\x07\tc\nd", "ab\tc\nd"),
    ('wrapping double quotes stripped', '"hello world"', "hello world"),
    ("wrapping single quotes stripped", "'hello world'", "hello world"),
    ("mismatched quotes left alone", '"hello world\'', '"hello world\''),
    ("interior quotes untouched", 'she said "hi" to me', 'she said "hi" to me'),
    ("CRLF normalized to LF", "line1\r\nline2\rline3", "line1\nline2\nline3"),
    (
        "legitimate multi-line dictation preserved",
        "Buy milk\nCall mom\nFinish report",
        "Buy milk\nCall mom\nFinish report",
    ),
    ("surrounding whitespace stripped, interior kept", "  hi   there  ", "hi   there"),
    ("empty string stays empty", "", ""),
]


def main() -> int:
    failures = 0
    for name, raw, expected in CASES:
        actual = sanitize_text(raw)
        ok = actual == expected
        status = "PASS" if ok else "FAIL"
        print(f"{status}: {name}")
        if not ok:
            failures += 1
            print(f"  expected={expected!r}")
            print(f"  actual  ={actual!r}")

    oversized = "x" * (MAX_LENGTH + 500)
    truncated = sanitize_text(oversized)
    ok = len(truncated) == MAX_LENGTH
    print(f"{'PASS' if ok else 'FAIL'}: oversized input truncated to MAX_LENGTH")
    if not ok:
        failures += 1
        print(f"  expected length={MAX_LENGTH}, actual length={len(truncated)}")

    print()
    if failures:
        print(f"{failures} case(s) FAILED")
        return 1
    print(f"All {len(CASES) + 1} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
