# SPDX-License-Identifier: AGPL-3.0-or-later
"""At-rest secret redaction (design doc 0001, section 5.7). Stdlib ``re`` only.

The indexer, the transcript miner and every other writer call ``redact_at_rest``
before text is stored, embedded or sent anywhere.

Covered shapes:

- a private key block (``-----BEGIN ... PRIVATE KEY-----``, also the PGP
  form ``PRIVATE KEY BLOCK``);
- HTTP auth headers (``Authorization``, ``Proxy-Authorization``), bare ``Basic``
  credentials, ``Bearer`` tokens and JWTs;
- API key shapes: ``sk-`` keys, GitHub token prefixes, Slack ``xox`` tokens,
  chat bot tokens, cloud access key ids, and the token prefixes ``glpat-``,
  ``AIza``, ``sk_live_``, ``rk_live_``, ``hf_`` and ``npm_``;
- the text of an XML ``<password>`` element;
- CLI ``key=value``, ``--token=``, ``--password=`` and ``-p VALUE`` forms;
- secret field names in ``key: value`` and ``key=value`` form, also with the
  key in quotes (JSON: ``"password": "value"``). A key that ends in ``pass``
  or ``pwd`` counts only in front of ``=`` and in the JSON form;
- the password in a connection URL (``scheme://user:PASSWORD@host``);
- the data block of a cluster ``kind: Secret`` manifest;
- email addresses.

Long hex runs are kept on purpose: git hashes and digests in memory files are
load-bearing and are not credentials.

Contract:

- Idempotent: ``redact_at_rest(redact_at_rest(x)) == redact_at_rest(x)``. A
  replacement can create a word boundary that lets a later pattern match, so
  the function runs up to ``MAX_PASSES`` passes until the text is stable.
- Fail closed: if the text does not settle, or a pattern raises, the result is
  ``REDACTION_FAILED_TOKEN``, never the raw text.
- ``REDACTOR_VERSION`` changes when a rule changes. The indexer stores it in
  ``meta`` for each namespace and writes every row again when it differs.
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTOR_VERSION = "2"

REDACTION_TOKEN = "***REDACTED***"
FIELD_TOKEN = "[REDACTED]"
EMAIL_TOKEN = "[REDACTED_EMAIL]"
REDACTION_FAILED_TOKEN = "***REDACTION_FAILED_TEXT_WITHHELD***"

MAX_PASSES = 5

# A value that is already a redaction marker is never matched again. Without
# this guard the key=value rule and the field rule would rewrite each other's
# marker forever and the text would never settle.
_NOT_A_MARKER = r"(?!\*\*\*REDACTED|\[REDACTED)"

_Replacement = Callable[["re.Match[str]"], str]

# -- Shapes that the hook redactor removes too: ``(pattern, flags)`` rows. The
# hooks run on the user's python3 without this package, so
# ``hooks/memory_text.py`` holds a copy of this table (``_SHARED_SHAPES``). A
# test fails when the two copies differ: change both.
_KEY_BEGIN = r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_END = r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_TEXT = r"[A-Za-z0-9+/=]{16,}"
SHARED_SHAPES: tuple[tuple[str, int], ...] = (
    # A private key block, also the PGP form (``PRIVATE KEY BLOCK``). Prose
    # that only names the BEGIN line stays: a block is masked when
    # - it has an END line, and its BEGIN line ends the line or key text
    #   follows it. The block holds no second BEGIN line, so a text of many
    #   BEGIN lines is read once;
    # - or it has no END line (a cut-off paste). Then the BEGIN line, up to 4
    #   header lines (``Name: value``) and the runs of key text after them are
    #   masked. The text after the key stays. A last run of fewer than 16
    #   characters stays too.
    (
        _KEY_BEGIN
        + r"(?:(?=[ \t]*(?:[\r\n]|\\[rn]|"
        + _KEY_TEXT
        + r"))(?:(?!-----BEGIN ).)*?"
        + _KEY_END
        + r"|(?:\r?\n[A-Za-z-]{1,20}: [^\r\n]{0,80}){0,4}(?:(?:\s|\\[rn])*"
        + _KEY_TEXT
        + r")+)",
        re.S,
    ),
    # Token prefixes: a code host, a cloud API, a payment API (two key types),
    # a model hub, a package registry.
    (r"\bglpat-[A-Za-z0-9_-]{16,}", 0),
    (r"\bAIza[A-Za-z0-9_-]{30,}", 0),
    (r"\b[sr]k_live_[A-Za-z0-9]{16,}", 0),
    (r"\bhf_[A-Za-z0-9]{30,}", 0),
    (r"\bnpm_[A-Za-z0-9]{30,}", 0),
    # The text of an XML ``<password>`` element. The tags stay. A placeholder
    # (``...``, ``${name}``) is not a password.
    (r"(?i)(?<=<password>)(?![.$])[^<]{4,}(?=</password>)", 0),
)

# -- Credential shapes. Order matters: the key block first, so no later rule
# masks a part of it, and the more specific header form before the bare one,
# so a Bearer token inside an Authorization header is not masked twice.
_SHAPE_PATTERNS: list[tuple[re.Pattern[str], _Replacement]] = [
    *((re.compile(shape, flags), lambda m: REDACTION_TOKEN) for shape, flags in SHARED_SHAPES),
    (
        re.compile(r"Proxy-Authorization:\s*([A-Za-z]+)\s+[^\s'\"\r\n]+", re.IGNORECASE),
        lambda m: f"Proxy-Authorization: {m.group(1)} {REDACTION_TOKEN}",
    ),
    (
        re.compile(
            r"(?<!Proxy-)Authorization:\s*([A-Za-z]+)\s+" + _NOT_A_MARKER + r"[^\s'\"\r\n]+",
            re.IGNORECASE,
        ),
        lambda m: f"Authorization: {m.group(1)} {REDACTION_TOKEN}",
    ),
    # Bare HTTP Basic credential (base64 of user:pass). At least 8 characters,
    # so the English word "basic" before a short word is not masked.
    (
        re.compile(r"\bBasic\s+[A-Za-z0-9+/]{8,}={0,2}", re.IGNORECASE),
        lambda m: f"Basic {REDACTION_TOKEN}",
    ),
    (
        re.compile(r"Bearer\s+" + _NOT_A_MARKER + r"[^\s'\"\r\n]{8,}", re.IGNORECASE),
        lambda m: f"Bearer {REDACTION_TOKEN}",
    ),
    # JWT: three base64url parts, the first two start with a JSON object.
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_.\-]+"),
        lambda m: REDACTION_TOKEN,
    ),
    # sk- keys, including the hyphenated project form.
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), lambda m: REDACTION_TOKEN),
    # GitHub tokens: fine-grained and classic prefixes.
    (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"), lambda m: REDACTION_TOKEN),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), lambda m: REDACTION_TOKEN),
    # Slack tokens.
    (re.compile(r"xox[bpars]-[A-Za-z0-9_-]{10,}"), lambda m: REDACTION_TOKEN),
    # Chat bot token: bot<digits>:<secret>. The prefix stays for readability.
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{30,}"), lambda m: f"bot{REDACTION_TOKEN}"),
    # Cloud access key ids (long-lived and temporary).
    (re.compile(r"A(?:KIA|SIA)[A-Z0-9]{16}"), lambda m: REDACTION_TOKEN),
]

# -- Broad CLI and key=value forms. At rest a false positive costs little and
# a missed credential costs a lot, so these run too.
_KEYVALUE_PATTERNS: list[tuple[re.Pattern[str], _Replacement]] = [
    # A value in quotes runs to its closing quote on the same line, so a space
    # in it does not end it (`password="two words"`).
    (
        re.compile(
            r"(token|secret|password|passwd|api_key|apikey|authorization|credential)"
            r"\s*=\s*" + _NOT_A_MARKER + r"(?:(['\"])(?:(?!\2)[^\\\n]|\\.)*\2|\S+)",
            re.IGNORECASE,
        ),
        lambda m: f"{m.group(1)}={REDACTION_TOKEN}",
    ),
    (re.compile(r"--token=" + _NOT_A_MARKER + r"\S+"), lambda m: f"--token={REDACTION_TOKEN}"),
    (
        re.compile(r"--password=" + _NOT_A_MARKER + r"\S+"),
        lambda m: f"--password={REDACTION_TOKEN}",
    ),
    (
        re.compile(r"(^|\s)-p\s+" + _NOT_A_MARKER + r"\S+"),
        lambda m: f"{m.group(1)}-p {REDACTION_TOKEN}",
    ),
]

# -- Secret field names in `key: value` or `key=value` form (config files,
# command output). The value must have at least 6 characters. The key may end
# with a quote (JSON: `"password": "value"`). A value in quotes is masked up
# to its closing quote on the same line, so a space in it does not end it and
# the rest of a compact JSON line stays. Any other value is the run up to the
# next whitespace.
# After `:` a number, `true`, `false` or `null` is not a secret, so
# `"input_token": 12345678,` stays with its comma.
# `pass` and `pwd` are words of prose, of a compiler and of a test report
# (`first_pass: complete`, `PASS: test_name`, `PWD=/tmp`). They are a key only
# in front of `=` (`DB_PASS=value`, `pass=value`, `MYSQL_PWD=value`) and in
# the JSON form with both sides in quotes (`"db_pass": "value"`). `pass` must
# not follow a letter ("bypass") and `pwd` must be the last part of a name.
_NOT_A_BARE_SCALAR = r"(?!\s*(?:-?[\d.]+|true|false|null)(?:[\s,;}\]]|$))"
_FIELD_KEY = (
    r"(?:password|passwd|secret|token|api[_-]?key|credential|private[_-]?key|access[_-]?key"
    r"|secret[_-]?key|client[_-]?secret|jwt|\.dockerconfigjson|tls\.crt|tls\.key|ca\.crt)"
    r"['\"]?\s*(?:=|:" + _NOT_A_BARE_SCALAR + r")\s*"
    r"|(?:(?<![a-z])pass|(?<=[_.\-])pwd)['\"]?\s*=\s*"
    r"|(?:(?<=['\"_.\-])pass|(?<=[_.\-])pwd)['\"]\s*:\s*(?=['\"])"
)
_SECRET_FIELD_RE = re.compile(
    r"(?i)(" + _FIELD_KEY + r")"
    r"(?:(['\"])" + _NOT_A_MARKER + r"(?:(?!\2)[^\\\n]|\\.){6,}\2"
    r"|(?!['\"]?(?:\*\*\*REDACTED|\[REDACTED))[^\s]{6,})",
)
_BEARER_FIELD_RE = re.compile(
    r"(?i)((?:authorization|bearer)\s*[=:\s]\s*(?:bearer\s+)?)"
    + _NOT_A_MARKER
    + r"([A-Za-z0-9\-_=.]{20,})",
)

# -- Cluster Secret manifest data: an indented `key: <base64>` line. Applied only
# when the text holds `kind: Secret`.
_K8S_SECRET_DATA_RE = re.compile(r"^(\s+[\w.\-]+:\s+)([A-Za-z0-9+/]{16,}={0,2})$", re.MULTILINE)
_K8S_KIND_RE = re.compile(r"kind:\s*secret\b", re.IGNORECASE)

# -- Connection URL password. The span runs to the next whitespace. The match
# is anchored right after each "://", so a "user:pass@" shape inside a URL path
# (a timestamp, for example) is not a credential. The user part is strict: it
# cannot hold ":", "@", "/", "?", "#". The password part may hold "/", "?", "#"
# because real base64 passwords do. The scheme is optional, so a cut-off
# "://user:pass@host" is still masked.
_URL_SPAN_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]*)?://[^\s]*")
_CONN_STR_AUTH_RE = re.compile(r"([^:@/?#\s]+):([^@\s]+)@")

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _redact_conn_str_urls(text: str) -> str:
    """Mask ``scheme://user:PASSWORD@host`` credentials.

    Walks every "://" in a span, not only the first, so a credential in a
    nested URL (a redirect parameter) or in the second URL of a comma-joined
    list is masked too. A loop, not recursion, so a long list cannot overflow
    the stack.
    """

    def _sub(m: re.Match[str]) -> str:
        span = m.group(0)
        pieces: list[str] = []
        pos = 0
        search_from = 0
        while True:
            idx = span.find("://", search_from)
            if idx == -1:
                pieces.append(span[pos:])
                break
            authority = idx + 3
            cred = _CONN_STR_AUTH_RE.match(span, authority)
            if not cred or cred.group(2) == FIELD_TOKEN:
                search_from = authority
                continue
            pieces.append(span[pos:authority])
            pieces.append(f"{cred.group(1)}:{FIELD_TOKEN}@")
            pos = cred.end()
            search_from = pos
        return "".join(pieces)

    return _URL_SPAN_RE.sub(_sub, text)


def _mask_field(m: re.Match[str]) -> str:
    quote = m.group(2) or ""
    return f"{m.group(1)}{quote}{FIELD_TOKEN}{quote}"


def _redact_once(text: str) -> str:
    out = text
    for pattern, repl in _SHAPE_PATTERNS:
        out = pattern.sub(repl, out)
    for pattern, repl in _KEYVALUE_PATTERNS:
        out = pattern.sub(repl, out)
    out = _SECRET_FIELD_RE.sub(_mask_field, out)
    out = _BEARER_FIELD_RE.sub(lambda m: m.group(1) + FIELD_TOKEN, out)
    out = _redact_conn_str_urls(out)
    if _K8S_KIND_RE.search(out):
        out = _K8S_SECRET_DATA_RE.sub(lambda m: m.group(1) + FIELD_TOKEN, out)
    return _EMAIL_RE.sub(EMAIL_TOKEN, out)


def redact_at_rest(text: str) -> str:
    """Return ``text`` with every covered secret shape masked.

    Returns ``REDACTION_FAILED_TOKEN`` when the text does not settle within
    ``MAX_PASSES`` passes or when a pattern raises. Never returns the raw text
    on failure.
    """
    try:
        out = _redact_once(text)
        for _ in range(MAX_PASSES - 1):
            nxt = _redact_once(out)
            if nxt == out:
                return out
            out = nxt
        return out if _redact_once(out) == out else REDACTION_FAILED_TOKEN
    except Exception:  # noqa: BLE001 - fail closed, never the raw text
        return REDACTION_FAILED_TOKEN


def would_change(text: str) -> bool:
    """True when the redactor would change ``text`` (used to drop labels)."""
    return redact_at_rest(text) != text
