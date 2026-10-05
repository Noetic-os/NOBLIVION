#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Memory text for the model: the shared formatter and redaction of the
memory hooks. Work item WI-3f (from WI-14b).

Why
  A paid test of the reference implementation: a hook
  that showed a memory as a cut excerpt ending in ``[memory <id>]`` made the
  model open the memory file (find, Glob, Read: 3 calls) before it acted.
  WI-14b fixed the error recall hook. WI-3f moves that code here, so the
  guard rows (``guard_hook.py``), the prompt recall hook
  (``recall_hook.py``) and the error recall hook
  (``error_recall_hook.py``) show a memory the same way, from one
  copy of the code.

One memory (``render_hit``)
  ``- Memory <id> (...)`` first, never a bare tag at the end. Then
  ``Rule:`` and ``Apply:`` whole (the front-matter fields ``rule:`` and
  ``apply:``; a memory with no rule shows its description as the rule), and
  ``Text:`` the whole body when it is at most ``FULL_BODY_CHARS`` (1200)
  characters. A longer body shows ``Text on this <subject>:``, the body part
  on the query (``excerpt``), cut at a sentence end at ``TEXT_CHARS`` (1200),
  and the head says how many characters the file adds. ``compact`` shows
  Rule and Apply only.

Several memories (``render``)
  The header, then the hits. When they break ``max_chars``, each hit after the
  first gets a shorter body part (the overflow comes off it, at least
  ``MIN_TEXT_CHARS``), else it is compact. The caller drops a hit that still
  breaks the bound.

Redaction
  Memory text is redacted (``redact_memory``) before it is made inert
  (``inert``: the prompt recall hook's ``_neutral``, kept in ``corpus.py``, so no line break and no
  angle bracket of a memory reaches the model). ``redact_memory`` is the query
  set (``redact``) with four patterns made strict for prose (see the comments
  at the patterns).

Standard library only. The sibling modules (``corpus``,
``memory_fields``) are loaded from this file's folder only when a
caller does not pass its own function.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent

FULL_BODY_CHARS = 1200  # a body up to this length is shown whole
TEXT_CHARS = 1200  # else the body part on the query, cut at a sentence end
EXCERPT_LINES = 40  # lines of the body from the one on the query, before the cut
FIELD_CHARS = 600  # one rule or apply field (the schema caps them at 160 and 400)
RENDER_MAX_CHARS = 3200  # one show, all hits (the error recall bound)
MIN_TEXT_CHARS = 300  # a later hit's body part below this shows Rule and Apply only
APPLY_CHARS = 220  # the head of ``apply`` that ``excerpt`` must beat
FULL_NOTE = "This is the memory's own text, so act on it; no need to open the memory file."


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODS: Dict[str, Any] = {}


def _mod(name: str):
    if name not in _MODS:
        _MODS[name] = _load(name)
    return _MODS[name]


# --------------------------------------------------------------------------
# redaction (moved from the error recall hook, WI-14 and WI-14b)
# --------------------------------------------------------------------------
_REDACT = "[REDACTED]"
_RX_SCHEME = (r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", 0)
_RX_KEY_NAME = (
    r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api[ \t_-]*key|auth|credential|private[_-]?key)[\w-]*)"
    r"(\s*[:=]\s*|\s+)(['\"]?)[^\s'\"&,;]{4,}\3",
    0,
)
# Memory text is prose: "token budget", "auth hook", "unauthenticated calls"
# are words, not secrets (the query forms hit 356 times in 705 memory bodies,
# 2026-09-30). In memory text a scheme value must hold a digit, a key name
# needs ":" or "=" before its value and the value is not a bare number
# (``MAX_TOKENS=16384``). Every other pattern is the same, except the ``pass``
# key and ``-p`` below.
_RX_SCHEME_MEMORY = (
    r"(?i)\b(bearer|basic|token)\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}",
    0,
)
_RX_KEY_NAME_MEMORY = (
    r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api[ \t_-]*key|auth|credential|private[_-]?key)[\w-]*)"
    r"(\s*[:=]\s*)(['\"]?)(?!\d+(?:[\s'\"&,;]|$))[^\s'\"&,;]{4,}\3",
    0,
)
# A key in quotes, the JSON form ``"password": "value"``: the closing quote of
# the key stops the two key name rules above. Only a key that ends in a secret
# word counts, so ``"max_tokens": 4096`` and ``"author": "..."`` stay. ``pass``
# counts alone or as the last part of a name (``"DB_PASS"``, not ``"bypass"``).
# A value in quotes runs to its closing quote, so a space in it does not end
# it. ``true``, ``false`` and ``null`` are not a secret. A number is a secret
# for a password key (``"password": 12345678``) and a count for a key that
# ends in ``token`` or ``pass`` (``"input_token": 12345678``). The same rule
# serves the query and the memory text.
_RX_QUOTED_KEY = (
    r"(?i)(['\"](?:[\w-]*(?:password|passwd|pwd|secret|credential|(?:api|private|access)[_-]?key)"
    r"|(?:[\w-]*token|(?:[\w.-]*[_.-])?pass)(?!['\"]\s*:\s*-?[\d.]+(?:[\s'\"&,;}\]]|$)))['\"])"
    r"(\s*:\s*)(?:(['\"])(?:(?!\3)[^\\\n]|\\.){4,}\3"
    r"|-?[\d.]{4,}(?=[\s'\"&,;}\]]|$)"
    r"|(?!\[REDACTED\])(?!(?:true|false|null)(?:[\s'\"&,;}\]]|$))[^\s'\"&,;]{4,})",
    0,
)
# Two values that the key name rules above miss, for a key that ends in a
# secret word: a value in quotes with a space in it (``password="two words"``),
# and a number after a password key (``password: 12345678``; the memory rule
# above keeps every number, because of ``MAX_TOKENS=16384``). A number after a
# key that ends in ``token`` stays. The same rule serves the query and the
# memory text.
_RX_QUOTED_VALUE = (
    r"(?i)((?:password|passwd|pwd|secret|credential|(?:api|private|access|secret)[_-]?key"
    r"|token(?=['\"]?\s*[:=]\s*['\"]))['\"]?)"
    r"(\s*[:=]\s*)(?:(['\"])(?:(?!\3)[^\\\n]|\\.){4,}\3|-?[\d.]{4,}(?=[\s'\"&,;}\]]|$))",
    0,
)
# pass / session / cookie keys only with ":" or "=" ("3 passed" stays).
_RX_PASS_KEY = (
    r"(?i)\b([\w-]*(?:pass|session|cookie)[\w-]*)(\s*[:=]\s*)(['\"]?)[^\s'\"&,;]{3,}\3",
    0,
)
# In memory text ``pass`` is a word of prose, of a compiler and of a test
# report too (``first_pass: complete``, ``PASS: test_name``, ``passed: 12``).
# There it is a key in front of ``=`` when it does not follow a letter
# (``DB_PASS=value``, not ``bypass=off``), and as the last part of a name in
# front of ``:`` when the value looks like a secret
# (``smtp-pass: hunter2hunter2``). ``passphrase``, ``passcode``, ``session``
# and ``cookie`` keys stay as in the query set; the key is read from that
# word on, at most 40 characters.
# A value that looks like a secret: one word of 8 or more characters that has
# a digit, a symbol inside it, or a capital letter after a small letter. A
# plain word stays, also with a capital first letter, in brackets or with a
# full stop or a comma after it. Only the first 64 characters are read, so a
# long word costs the same as a short one. The store redactor has the same
# text (``_SECRET_LIKE`` in ``src/noblivion/redaction.py``).
_SECRET_LIKE = (
    r"(?=[^\s'\"]{8})"
    r"(?:[^\s\d'\"]{0,63}\d"
    r"|[^\W_]{0,63}(?:_|[^\s\w'\"(){}\[\],;`])[^\W_]"
    r"|(?-i:[^\sa-z'\"]{0,63}[a-z][^\sA-Z'\"]{0,63}[A-Z]))"
)
_RX_PASS_KEY_MEMORY = (
    r"(?i)((?<![a-z])pass(?=\s*=)|(?<=[_.-])pass(?=\s*:\s*['\"]?" + _SECRET_LIKE + r")"
    r"|(?:passphrase|passcode|session|cookie)[\w-]{0,40})"
    r"(\s*[:=]\s*)(['\"]?)[^\s'\"&,;]{3,}\3",
    0,
)
_RX_DASH_P = (
    r"(?<!\S)(-p|--pass(?:word|wd)?)(\s+|=|(?=['\"]))(['\"]?)(?![\d:.]+(?:[\s'\"]|$))(?![/~.])"
    r"[^\s'\"]{4,}\3",
    0,
)
# ``docker compose -p <project>``, ``systemctl show -p Result`` and
# ``pytest -p no:cacheprovider`` are the 36 hits of the short form in memory
# text; there only a quote glued to ``-p`` (``mysql -p'...'``) or the long
# form counts.
_RX_DASH_P_MEMORY = (
    r"(?<!\S)(-p(?=['\"])|--pass(?:word|wd)?)(\s+|=|(?=['\"]))(['\"]?)(?![\d:.]+(?:[\s'\"]|$))"
    r"(?![/~.])[^\s'\"]{4,}\3",
    0,
)
# The shapes that the at-rest redactor of the store removes too. The hooks run
# without the store package, so this table is a copy of ``SHARED_SHAPES`` in
# ``src/noblivion/redaction.py``. A test fails when the two copies differ:
# change both.
_KEY_BEGIN = r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_END = r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_TEXT = r"[A-Za-z0-9+/=]{16,}"
_SHARED_SHAPES = (
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
_SECRET_PATTERNS = _SHARED_SHAPES + (
    _RX_SCHEME,
    (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}", 0),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}", 0),
    (r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{16,}", 0),
    (r"\bxox[abposr]-[A-Za-z0-9-]{10,}", 0),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", 0),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", 0),
    # URL user info up to the LAST @ before the host: a token with no colon
    # (https://<token>@github.com) and a password that holds an @.
    (r"(?<=://)[^/\s]+(?=@[^@/\s]*(?:[/\s:?#]|$))", 0),
    _RX_QUOTED_VALUE,
    _RX_KEY_NAME,
    _RX_QUOTED_KEY,
    _RX_PASS_KEY,
    # -p / --password <value>; not a path, a port or a host:port (mkdir -p,
    # ssh -p 2222, docker -p 8080:80), and -print stays.
    _RX_DASH_P,
    (r"\b[A-Fa-f0-9]{40,}\b", 0),
    (
        r"(?<![\w+/=-])(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[a-z])(?=[A-Za-z0-9+/_=-]*[A-Z])"
        r"[A-Za-z0-9+/_-]{32,}={0,2}(?![\w+/=-])",
        0,
    ),
)
_SECRET_RX = [re.compile(p, f) for p, f in _SECRET_PATTERNS]
_MEMORY_SWAP = {
    _RX_SCHEME: _RX_SCHEME_MEMORY,
    _RX_KEY_NAME: _RX_KEY_NAME_MEMORY,
    _RX_PASS_KEY: _RX_PASS_KEY_MEMORY,
    _RX_DASH_P: _RX_DASH_P_MEMORY,
}
_MEMORY_SECRET_RX = [re.compile(*_MEMORY_SWAP.get(pf, pf)) for pf in _SECRET_PATTERNS]


def redact(text: str, patterns: Optional[List[re.Pattern[str]]] = None) -> str:
    """``text`` with secrets replaced by ``[REDACTED]``. A ``key=value`` pair
    keeps its key name so the query still says what failed. ``patterns``
    defaults to the query set (``_SECRET_RX``)."""
    out = text or ""
    for rx in _SECRET_RX if patterns is None else patterns:
        if rx.groups >= 3:
            out = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACT}", out)
        elif rx.groups == 1:
            out = rx.sub(lambda m: f"{m.group(1)} {_REDACT}", out)
        else:
            out = rx.sub(_REDACT, out)
    return out


def _cut(text: str, n: int) -> str:
    """``text`` cut to at most ``n`` characters. A word the cut splits is
    dropped whole, so no cut leaves the head of a secret behind."""
    if len(text) <= n:
        return text
    head = text[:n]
    if not text[n].isspace():
        kept = head.rsplit(None, 1)
        head = kept[0] if len(kept) > 1 else head
    return head.rstrip()


# --------------------------------------------------------------------------
# query words
# --------------------------------------------------------------------------
_TOKEN_RX = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    (
        "a an the and or of to in on at by for from with is are was were be been it its this that "
        "not no do does did as if then so but into when what which who how all any can cannot use "
        "you your we our i me my he she they them his her their there here has have had will would "
        "should could may might must shall than too very just only also more most other some such "
        "run runs ran line file files error errors"
    ).split()
)


def tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RX.findall((text or "").lower()) if t not in STOPWORDS and len(t) > 1]


def redact_memory(text: str) -> str:
    """Memory text with secrets redacted (``_MEMORY_SECRET_RX``)."""
    return redact(text, _MEMORY_SECRET_RX)


# --------------------------------------------------------------------------
# reading a memory file
# --------------------------------------------------------------------------
_DESC_RX = re.compile(r"^description:\s*(.*)$", re.M)


def read_memory(path: Path, fields: Optional[Callable[[], Any]] = None) -> Dict[str, str]:
    """``{id, rule, apply, description, body}`` of one memory file.
    ``fields`` returns the ``memory_fields`` module (default: the
    sibling file); an error in it leaves the whole text as the body."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rule = apply = ""
    try:
        f = (fields or (lambda: _mod("memory_fields")))()
        fr = f.read_fields(text)
        rule = str(fr.get("rule") or "")
        apply = str(fr.get("apply") or "")
        body = f.body_of(text)
    except Exception:  # noqa: BLE001 - a broken file is still text
        body = text
    m = _DESC_RX.search(text.split("\n---", 2)[0] if text.startswith("---") else "")
    desc = m.group(1).strip().strip('"') if m else ""
    return {"id": Path(path).stem, "rule": rule, "apply": apply, "description": desc, "body": body}


# --------------------------------------------------------------------------
# the text of one memory (moved from the error recall hook, WI-14b)
# --------------------------------------------------------------------------
def excerpt(mem: Mapping[str, object], query: str, max_lines: int = 6) -> str:
    """The part of the memory that speaks about THIS query: the line of the
    body (then of ``apply``) that holds the most query terms (at least
    two, and more than the head of ``apply`` holds), with the text after it,
    so the fix that follows a quoted error is shown. Else the head of
    ``apply``. A memory can list several traps; the head names only the
    first. ``max_lines`` lines from the best one on (``render_hit`` takes 40
    and cuts by characters, so the fix below the quoted error is kept)."""
    apply = str(mem.get("apply") or "")
    q = set(tokens(query))
    if not q:
        return apply
    # The body first: ``apply`` is capped at 400 characters and can stop in
    # the middle of the paragraph the body holds in full.
    keep = lambda x: x.strip() and not x.lstrip().startswith("#")  # noqa: E731
    body_lines = [x for x in str(mem.get("body") or "").splitlines() if keep(x)]
    lines = body_lines + [x for x in apply.splitlines() if keep(x)]
    head_hits = len(q & set(tokens(apply[:APPLY_CHARS])))
    best, best_i = head_hits, -1
    for i, line in enumerate(lines):
        n = len(q & set(tokens(line)))
        if n > best and n >= 2:
            best, best_i = n, i
    if best_i < 0:
        return apply
    # A best line in the body runs to the body's end at most: the apply text
    # after it is shown in its own field.
    end = (
        min(best_i + max_lines, len(body_lines)) if best_i < len(body_lines) else best_i + max_lines
    )
    return " ".join(lines[best_i:end])


def _neutral_of_recall_hook(text: str) -> str:
    return _mod("corpus")._neutral(text)


def inert(text: object, neutral: Optional[Callable[[str], str]] = None) -> str:
    """Memory text as one inert line: secrets redacted on the whole text
    first, then ``neutral`` (default: the prompt recall hook's ``_neutral``:
    no line breaks, no angle brackets)."""
    text = redact_memory(str(text or ""))
    try:
        return (neutral or _neutral_of_recall_hook)(text)
    except Exception:  # noqa: BLE001
        return " ".join(text.split()).replace("<", "‹").replace(">", "›")


_SENTENCE_END = re.compile(r"[.!?:;][`'\")\]]*(?=\s)")


def cut_at_sentence(text: str, n: int) -> Tuple[str, int]:
    """``(head, chars left out)``: ``text`` whole when it fits in ``n``, else
    cut after the last sentence end in the second half of ``n``, else at a
    word (``_cut``). The count is what the file holds beyond the head."""
    if len(text) <= n:
        return text, 0
    ends = [m.end() for m in _SENTENCE_END.finditer(text, 0, n) if m.end() >= n // 2]
    head = text[: ends[-1]] if ends else _cut(text, n)
    head = head.rstrip()
    return head, len(text) - len(head)


def render_hit(
    mem: Mapping[str, object],
    query: str = "",
    compact: bool = False,
    text_chars: int = TEXT_CHARS,
    *,
    subject: str = "error",
    full_body_chars: int = FULL_BODY_CHARS,
    field_chars: int = FIELD_CHARS,
    excerpt_lines: int = EXCERPT_LINES,
    compact_needs_apply: bool = True,
    body_known: bool = True,
    compact_note: Optional[str] = None,
    head_fallback: bool = False,
    neutral: Optional[Callable[[str], str]] = None,
) -> str:
    """One hit: the id first (for the log and the reader), then the memory's
    own text, never a bare tag at the end. ``Rule`` and ``Apply`` come whole
    from the front matter (``rule:``, ``apply:``). The body is shown whole
    when it is short (``full_body_chars``); else the part of it on this
    ``subject`` (``excerpt`` on ``query``), cut at a sentence end at
    ``text_chars``, with the count of characters the file holds beyond it.
    ``render`` lowers ``text_chars`` for a later hit. ``compact`` shows Rule
    and Apply only; with ``compact_needs_apply`` (the error recall shape) a
    memory with no apply is never compact. ``body_known`` False: the caller
    could not read the body, so the head says only that rule and fix are
    whole. ``compact_note`` replaces the character count in a compact head
    (the guard: the full text was shown earlier in the session).
    ``head_fallback``: when no body line beats the head of ``apply`` (so
    ``excerpt`` returns ``apply``), show the start of the body as
    ``Text (start):`` instead of no text (the guard rows and the prompt
    recall; the error recall shape keeps it off)."""
    ident = re.sub(r"[^\w.-]", "", str(mem.get("id", "")))[:120]
    rule = inert(mem.get("rule") or mem.get("description") or "", neutral)
    apply = inert(mem.get("apply") or "", neutral)
    body = inert(mem.get("body") or "", neutral)
    fields = []
    if rule:
        fields.append("  Rule: " + cut_at_sentence(rule, field_chars)[0])
    if apply and apply != rule:
        fields.append("  Apply: " + cut_at_sentence(apply, field_chars)[0])
    what = "rule and fix in full" if apply else "summary in full"
    if not body_known:
        head = f"- Memory {ident} ({what}):"
    elif compact and (apply or not compact_needs_apply):
        note = compact_note or f"the file adds about {len(body)} characters of background"
        head = f"- Memory {ident} ({what}; {note}):"
    elif len(body) <= min(full_body_chars, text_chars):
        if body:
            fields.append("  Text: " + body)
        head = f"- Memory {ident} (full text; no need to open the file):"
    else:
        raw = excerpt(mem, query, excerpt_lines)
        label = f"Text on this {subject}"
        if head_fallback and raw == str(mem.get("apply") or ""):
            raw, label = str(mem.get("body") or ""), "Text (start)"
        ex = inert(raw, neutral)
        shown, _ = cut_at_sentence(ex, text_chars)
        if shown and shown != apply:
            fields.append(f"  {label}: " + shown)
        left = max(0, len(body) - len(shown if shown != apply else ""))
        what = "rule and fix in full" if apply else f"summary and the text on this {subject}"
        head = f"- Memory {ident} ({what}; the file adds about {left} characters of background):"
    return "\n".join([head] + fields)


def render(
    hits: List[Mapping[str, object]],
    query: str = "",
    *,
    header: str,
    max_chars: int = RENDER_MAX_CHARS,
    text_chars: int = TEXT_CHARS,
    min_text_chars: int = MIN_TEXT_CHARS,
    render_one: Optional[Callable[..., str]] = None,
) -> str:
    """``header`` and the hits. When the full hits break ``max_chars``, each
    hit after the first gets a shorter body part on the query (the overflow
    comes off it; a memory can list several traps and the part on THIS query
    is the one to keep), and below ``min_text_chars`` it is compact (Rule and
    Apply only). The caller drops a hit that still breaks the bound.
    ``render_one(mem, query, compact=..., text_chars=...)`` renders one hit
    (default ``render_hit``)."""
    one = render_one or render_hit
    parts = [header] + [one(m, query) for m in hits]
    for i in range(len(parts) - 1, 1, -1):
        room = text_chars
        while len("\n".join(parts)) > max_chars:
            room -= len("\n".join(parts)) - max_chars
            if room < min_text_chars:
                parts[i] = one(hits[i - 1], query, compact=True)
                break
            parts[i] = one(hits[i - 1], query, text_chars=room)
    return "\n".join(parts)
