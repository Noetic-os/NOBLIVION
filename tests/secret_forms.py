# SPDX-License-Identifier: AGPL-3.0-or-later
"""The secret forms that both redactors must remove (NOBLIVION-46).

The at-rest redactor of the store and the redactor of the hooks
(``hooks/memory_text.py``) are two pieces of code. ``tests/test_redaction.py``
feeds every form below to the first, ``tests/test_hook_memory_text.py`` feeds
it to the second. A form added here is checked on both sides, so the two
cannot drift apart again without a failing test.

Every secret is fictional and built at run time from parts, so no
secret-shaped literal sits in the repository for a leak scanner to find.
The token bodies are lower-case letters only: no rule for a long mixed-case
string removes them, only the rule for the prefix.

Valid for Python 3.9: the hook tests import this file.
"""

from __future__ import annotations

ALNUM = "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ1cF0xR"
LOWER = "abcdefghijklmnopqrstuvwxyz"
B64 = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"


def fake(prefix: str, n: int, alphabet: str = ALNUM) -> str:
    return prefix + (alphabet * (n // len(alphabet) + 1))[:n]


KEY_BODY = fake("", 64, B64)
KEY_TAIL = fake("", 22, B64[::-1]) + "=="


def key_block(kind: str, end: bool = True) -> str:
    """A private key block of ``kind`` ("RSA", "" for the plain form), in the
    shape a key tool writes: full lines, then a short padded line. With
    ``end`` False the END line is cut off."""
    name = (kind + " " if kind else "") + "PRIVATE " + "KEY"
    lines = [f"-----BEGIN {name}-----", KEY_BODY, KEY_BODY, KEY_TAIL]
    if end:
        lines.append(f"-----END {name}-----")
    return "\n".join(lines)


PASSWORD = "hunter2" + "hunter2"
API_KEY = fake("", 24)
GLPAT = fake("gl" + "pat-", 20, LOWER)
GOOGLE_KEY = fake("AI" + "za", 35, LOWER)
PAYMENT_KEY = fake("sk" + "_live_", 24, LOWER)
HF_TOKEN = fake("h" + "f_", 34, LOWER)
NPM_TOKEN = fake("np" + "m_", 36, LOWER)

# (name, text, the secret that must be gone)
FORMS: list[tuple[str, str, str]] = [
    ("openssh key block", f"The key file:\n{key_block('OPENSSH')}\nKeep it safe.", KEY_BODY),
    ("rsa key block", f"The key file:\n{key_block('RSA')}\nKeep it safe.", KEY_BODY),
    ("ec key block", f"The key file:\n{key_block('EC')}\nKeep it safe.", KEY_BODY),
    ("plain key block", f"The key file:\n{key_block('')}\nKeep it safe.", KEY_BODY),
    ("key block with no END line", f"The key file:\n{key_block('RSA', end=False)}", KEY_BODY),
    ("json password", '{"password": "' + PASSWORD + '"}', PASSWORD),
    ("json api_key", '{"api_key": "' + API_KEY + '"}', API_KEY),
    ("compact json", '{"user":"alice","password":"' + PASSWORD + '","port":5432}', PASSWORD),
    ("pass suffix", f"DB_PASS={PASSWORD}", PASSWORD),
    ("glpat- prefix", f"the token {GLPAT} was used", GLPAT),
    ("AIza prefix", f"the key {GOOGLE_KEY} was used", GOOGLE_KEY),
    ("sk_live_ prefix", f"the key {PAYMENT_KEY} was used", PAYMENT_KEY),
    ("hf_ prefix", f"the token {HF_TOKEN} was used", HF_TOKEN),
    ("npm_ prefix", f"the token {NPM_TOKEN} was used", NPM_TOKEN),
]

# Text that no redactor may change.
ORDINARY: list[str] = [
    "Ask the admin to reset the password.",
    'The "password" field must not be empty.',
    "See https://example.com/docs/setup?page=2 for the steps.",
    "commit 2656d01f3a9c is the fix",
    "The second pass over the list is faster.",
    '{"passed": 2730, "failed": 0}',
    "npm_config_registry and hf_hub_download are plain names.",
    "AIza and sk_live_ are prefixes of two key types.",
    "The file starts with a BEGIN line and stops with an END line.",
]

# (name, a hostile text of 100 KB or more): a new or changed rule must stay
# linear on each one.
_BEGIN = "-----BEGIN PRIVATE " + "KEY-----"
SIZE = 100_000
HOSTILE: list[tuple[str, str]] = [
    ("BEGIN lines, no END line", _BEGIN * (SIZE // len(_BEGIN) + 1)),
    ("one BEGIN line, a long body", _BEGIN + "A" * SIZE),
    ("a BEGIN line with a long name", "-----BEGIN " + "A " * (SIZE // 2)),
    ("END lines that never close", _BEGIN + ("-----END " + "A" * 20) * (SIZE // 29 + 1)),
    ("dashes", "-" * SIZE),
    ("letters", "a" * SIZE),
    ("a key name and spaces", "password" + " " * SIZE),
    ("key names", "password " * (SIZE // 9 + 1)),
    ("pass words", "_pass pass " * (SIZE // 11 + 1)),
    ("quoted keys", 'password":"' * (SIZE // 11 + 1)),
    ("single-quoted keys", "password':'" * (SIZE // 11 + 1)),
    ("a quote that never closes", 'password: "' + "a" * SIZE),
    ("escapes that never close", 'password: "' + '\\"' * (SIZE // 2)),
    ("AIza prefixes", "AIza" * (SIZE // 4)),
    ("glpat- prefixes", "glpat-" * (SIZE // 6 + 1)),
    ("sk_live_ prefixes", "sk_live_" * (SIZE // 8)),
    ("one long hf_ token", "hf_" + "a" * SIZE),
    ("npm_ prefixes", "npm_" * (SIZE // 4)),
    ("a quote and a long name", '"' + "a-" * (SIZE // 2)),
    ("quotes and names", '"secret-a' * (SIZE // 9 + 1)),
]
