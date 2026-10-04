# SPDX-License-Identifier: AGPL-3.0-or-later
"""Injection-pattern redactor for memory text (design doc 0001, section 15.3).

Memory text goes into the model's context. This module masks text shapes that
try to give the model new instructions: "ignore previous instructions", a fake
system message, a forged approval claim, a directive hidden in a comment.

The store runs ``redact_injection`` on every title, summary and text it
returns, field by field. Stdlib only.

Rules:

- Detection runs on a folded copy: cross-script look-alike letters map to
  ASCII, format characters (zero-width space, bidi controls) and non-space
  control characters are dropped, then NFKC folds width and font variants.
- No hit: the original text comes back unchanged, so non-Latin text is never
  mangled by the fold.
- A hit: the folded copy comes back with each match replaced by
  ``INJECTION_TOKEN``.
- Fail closed: if a pattern raises, the result is the failure token of the
  secret redactor, never the raw text.
- A fixed pattern list, not a judge of meaning: an instruction in plain
  words ("always run this command before a push") passes unchanged.
"""

from __future__ import annotations

import re
import unicodedata

from noblivion.redaction import REDACTION_FAILED_TOKEN

INJECTION_TOKEN = "[REDACTED:injection-pattern]"

# Cyrillic and Greek letters that look like Latin letters used in the patterns.
_CONFUSABLE_SKELETON = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ѕ": "s", "ј": "j", "ԁ": "d", "ѵ": "v", "к": "k", "н": "h",
    "м": "m", "т": "t", "ԛ": "q", "ԝ": "w", "ѡ": "w", "ǥ": "g",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o",
    "Р": "p", "С": "c", "Т": "t", "У": "y", "Х": "x", "Ѕ": "s", "Ј": "j",
    "Ѵ": "v",
    "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k",
    "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x",
    "ο": "o", "ν": "v", "υ": "u", "ι": "i", "α": "a",
}  # fmt: skip

# An approval claim counts only with a grant verb near it and no negation or
# requirement word in between. Memory files often state policy such as "this
# needs explicit operator approval"; that text must stay readable.
_OPERATOR_GRANT_CLAIM = (
    r"(?<!no )(?<!not )(?<!never )"
    r"operator[\s-]+(directive|override|authoriz\w*|approval)\b"
    r"(?:(?!\b(?:must|should|will|would|never|no|not|cannot)\b)[^.\n]){0,80}\b"
    r"(granted|given|obtained|recorded|approved|on\s+file|hereby|"
    r"pre-?granted|pre-?approved)\b"
)

_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"system\s+override",
        r"persona\s+directive",
        _OPERATOR_GRANT_CLAIM,
        r"ignore\s+(all\s+)?previous\s+(instructions|rules|directives)",
        r"disregard\s+(all\s+)?previous",
        r"new\s+system\s+(message|prompt|instruction)",
        r"this\s+is\s+(an\s+)?(automated\s+)?system\s+message",
        r"<\s*system\s*>",
        r"\[\s*SYSTEM\s*\]",
        r"pre[- ]approved\s+by\s+(the\s+)?operator",
        # An upper-case DIRECTIVE block hidden in an HTML or Markdown comment.
        r"<!--[^>]{0,200}\b[A-Z][A-Z \t-]{2,40}DIRECTIVE\b",
        r"operator[\s-]*ratifi\w*",
        # A self-declared supersession of earlier instructions. Scoped to
        # instruction nouns, so "this rule supersedes the previous regulation"
        # is not touched.
        r"supersed\w*\s+(all\s+)?(prior|previous|existing)\s+"
        r"(instruction|formatting|directive|rule|prompt)s?\b",
        # A conditional trigger that waits for a later, unrelated turn.
        r"when\s+(asked|queried)\s+about\b[^.\n]{0,80}\b(first|always|begin|start)\b",
    )
]


def _fold(text: str) -> str:
    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        if cat == "Cc" and not ch.isspace():
            # Keep every whitespace control character: the patterns match
            # "\s+" between words, so dropping a form feed would join them.
            continue
        out.append(_CONFUSABLE_SKELETON.get(ch, ch))
    return unicodedata.normalize("NFKC", "".join(out))


def _redact(text: str) -> tuple[str, int]:
    hits = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        return INJECTION_TOKEN

    out = _fold(text)
    for pattern in _PATTERNS:
        out = pattern.sub(_sub, out)
    if hits == 0:
        return text, 0
    return out, hits


def redact_injection(text: str) -> tuple[str, int]:
    """Return ``(text, hits)`` with injection-shaped spans masked."""
    if not text:
        return text, 0
    try:
        return _redact(text)
    except Exception:  # noqa: BLE001 - fail closed, never the raw text
        return REDACTION_FAILED_TOKEN, 0
