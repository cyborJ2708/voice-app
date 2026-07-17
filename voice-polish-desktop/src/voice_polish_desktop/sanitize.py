"""Defensive cleanup of backend-returned text before it is ever injected.

Not the primary safeguard against unintended keystrokes/shortcuts — see
inject.py's module docstring: injection only ever goes through Ctrl+V paste
or KEYEVENTF_UNICODE typing, neither of which resolves through a VK/shortcut
table, so there is no code path today where returned text content could
itself trigger a shortcut. This module is a backstop against a malformed or
adversarial backend response, not the sole line of defense.
"""
from __future__ import annotations

import re

MAX_LENGTH = 20_000  # generous — several minutes of dictation; guards a runaway response

# CSI-style ANSI escape sequences (ESC [ ... letter), e.g. "\x1b[31m".
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# C0/C1 control chars except \t (\x09) and \n (\x0a).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")

_QUOTE_PAIRS = [
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),  # “ ”
    ("‘", "’"),  # ‘ ’
    ("`", "`"),
]


def sanitize_text(raw: str) -> str:
    """Clean backend-returned text before clipboard/typing injection.

    Strips a leading BOM, ANSI escape sequences, control characters (keeping
    tabs and newlines — multi-line dictation is legitimate), one layer of
    wrapping quotes, surrounding whitespace, and hard-truncates oversized
    input. Does NOT strip newlines/markdown — those are content-quality
    concerns, not security ones.
    """
    text = raw.lstrip(chr(0xFEFF))
    text = _ANSI_RE.sub("", text)
    text = text.replace("\x1b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _strip_wrapping_quotes(text)
    text = text.strip()
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH]
    return text


def _strip_wrapping_quotes(text: str) -> str:
    stripped = text.strip()
    for open_q, close_q in _QUOTE_PAIRS:
        if (
            len(stripped) >= 2
            and stripped.startswith(open_q)
            and stripped.endswith(close_q)
        ):
            inner = stripped[len(open_q) : -len(close_q)]
            if inner:
                return inner
    return text
