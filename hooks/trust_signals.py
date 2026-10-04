#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two more trust signals from the session transcript (NOBLIVION-38).

1. CITATION -> ``use``. The reply names a note that the prompt hook showed
   earlier in the session, by its file name. Setting
   ``NOBLIVION_TRUST_CITATION_USE`` (config key ``trust.citation_use``).
   Off by default: on real sessions its precision was 7 of 40 at first,
   then 15 of 29 after the save and list-echo skips (NOBLIVION-43). The
   stricter rule of NOBLIVION-45 (name only, no note-keeping turn, no note
   the prompt quotes, at most 2 notes per reply) is measured in
   docs/trust.md.
2. CORRECTION -> ``contradict``. The user corrects Claude Code, and the
   correction is about a note that was shown in the last prompts and that
   the reply cited. Setting ``NOBLIVION_TRUST_CORRECTION_CONTRADICT``
   (config key ``trust.correction_contradict``). Off by default: the words
   of a correction do not say whether the note led Claude Code wrong or
   Claude Code ignored a right note. A wrong ``contradict`` takes 2 uses off
   a good note.

Both signals need the trust events (``trust_events.enabled``).

Where the data comes from:

- The prompt hook appends the name and the rule text of each note it shows
  to ``<cache>/by-session/<session>.trust-shown.jsonl``, once per note and
  session (``record_shown``). Only when a signal is on.
- The time of each showing is the ``recall`` line in the event spool.
- The Stop flush worker (``trust_flush``) reads the main transcript from
  where it stopped and calls ``scan``. It runs in the background, so the
  hooks stay fast. A subagent transcript is not read: its prompt is a task,
  not a user correction.

The rules are strict on purpose. A false ``use`` raises a note; a false
``contradict`` lowers a good note. See docs/trust.md for the measured
precision on a labelled set of made-up examples.

Standard library only; runs on Python 3.9. Never raises to the caller of
``record_shown``; ``scan`` raises nothing on bad input.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CITATION_ENV = "NOBLIVION_TRUST_CITATION_USE"
CITATION_KEY = "trust.citation_use"
CITATION_DEFAULT = False
CORRECTION_ENV = "NOBLIVION_TRUST_CORRECTION_CONTRADICT"
CORRECTION_KEY = "trust.correction_contradict"
CORRECTION_DEFAULT = False

SHOWN_NAME = "trust-shown.jsonl"
SHOWN_MAX_BYTES = 1_000_000  # no more notes are added to a larger file
SPOOL_READ_MAX = 4_000_000  # the tail of the event spool that is read
NAME_MAX = 200
RULE_MAX = 400
REPLY_MAX_CHARS = 20_000
PROMPT_MAX_CHARS = 4_000

SRC_CITATION = "citation"
SRC_CORRECTION = "correction"

# Citation.
PHRASE_WORDS = 8  # a verbatim phrase of the rule: this many words in a row
PHRASE_MIN_CONTENT = 4  # of which at least this many are content words
NAME_MIN_CHARS = 10  # a shorter name is too common to count as a citation
NAME_MIN_PARTS = 2  # "feedback_no_stash" has 3 parts; "deploy" has 1

# Correction.
CORRECTION_WINDOW = 2  # the note was shown in the last 2 prompts
PROMPT_SLACK_S = 5  # a showing this close to the correction is its own recall
OVERLAP_MIN = 3  # content words shared by the correction and the note
OVERLAP_SHARE = 0.25  # ... and at least this share of the correction's words
CITED_MAX = 500

_WORD_RE = re.compile(r"[a-z0-9]+")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_NAME_PART_RE = re.compile(r"[_.\-]+")

# Words that carry no topic. Kept short: a longer list would also drop words
# that do carry one.
STOPWORDS = frozenset(
    """
    a about after again all also always an and any are as at be because been before
    being but by can could did do does doing done don dont each every for from get
    got had has have here how if in into is it its just like make made more most must
    my never no not now of off on once one only or other our out over please same
    should so some such than that the their them then there these they this those
    through to too under until up use used uses using very was way we were what when
    where which while who why will with without would yes yet you your
    rule rules memory memories note notes feedback project topic
    """.split()
)

# A sentence that cites a note to set it aside is not a use.
NEGATION_RE = re.compile(
    r"\b(?:does\s*n[o']?t\s+apply|do\s*n[o']?t\s+apply|not\s+appl\w*|not\s+relevant|"
    r"irrelevant|ignor\w*|outdated|obsolete|stale|wrong|incorrect|not\s+follow\w*|"
    r"overrid\w*|override|guard-ok|disregard\w*|set\s+aside|"
    # it could not read the note
    r"could\s*n[o']?t|cannot|can't|unable|missing|not\s+found|"
    # it talks about the index, not the task
    r"index|showed|listed|"
    # it says it did not follow the note (NOBLIVION-45)
    r"unread|missed|overlooked|violat\w*|walked\s+(?:straight\s+)?into)\b",
    re.I,
)

# Not a use either (NOBLIVION-43): a reply names a note because it saves or
# edits it. A sentence with one of these words adds no note NAME (a verbatim
# phrase of the rule still counts), and a note that a tool call of the same
# turn writes is not cited in that turn.
SAVE_RE = re.compile(
    r"\b(?:saved|recorded|updated|wrote|written|rewrote|rewritten|added|created|"
    r"stored|edited|appended|"
    # more ways to say it (NOBLIVION-45)
    r"corrected|amended|revised|renamed|deleted|removed|append|appending|saving|"
    r"recording|updating|editing)\b",
    re.I,
)
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_REDIRECT_RE = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*['\"]?([^\s'\";|&<>()]+)")
WRITTEN_MAX = 200
# A Bash command that runs one of these can write any file it names
# (NOBLIVION-45: a note fixed by a Python heredoc or ``sed -i``).
_WRITER_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:python[0-9.]*|perl\s+-\S*i|sed\s+(?:-\S+\s+)*-i|cp|mv|rm|"
    r"install|truncate|tee)\b"
)
_MD_FILE_RE = re.compile(r"[\w.-]+\.md\b", re.I)
# Claude Code keeps its notes as ``.md`` files in a folder of this name.
NOTE_DIR = "memory"
NOTE_WRITE = "/" + NOTE_DIR  # in written_files: the turn writes a note

# A list echo is not a use (NOBLIVION-43): the user asked to see the notes,
# or one reply names many shown notes at once. A reply that applies notes to
# a task names a few of them. On the real-session sets the replies labelled
# true named at most 2 shown notes, the list echoes 24 and 38.
ECHO_PROMPT_RE = re.compile(
    r"\b(?:list|show|print|display|repeat|recite|quote|dump|enumerate)\b"
    r"[^.?!\n]{0,60}?\b(?:rules?|memor(?:y|ies)|notes?)\b",
    re.I,
)
ECHO_MIN_NOTES = 3  # NOBLIVION-45: 5 before; the true replies named at most 2

# A correction that holds the user TO a note says the note is right.
ENFORCE_RE = re.compile(
    r"\b(?:ignored|ignoring|forgot|forgotten|forget|did\s*n[o']?t\s+(?:follow|read|listen)|"
    r"did\s+not\s+(?:follow|read|listen)|not\s+follow\w*|broke|break(?:ing)?|violat\w*|"
    r"i\s+(?:have\s+|'ve\s+)?(?:already\s+)?(?:told|asked)\s+you|how\s+many\s+times|again|"
    r"as\s+(?:the|your|my)\s+(?:rule|memory|note)\s+says|remember|follow\s+(?:the|your|my))\b",
    re.I,
)

_MODS: Dict[str, Any] = {}


def _load(name: str) -> Any:
    """A sibling module of this file, loaded once by path."""
    mod = _MODS.get(name)
    if mod is None:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), f"{name}.py")
        # The prompt hook has loaded its siblings under their own names: reuse
        # them, so this module adds no load time on the prompt path.
        have = sys.modules.get(name)
        if have is not None and os.path.realpath(getattr(have, "__file__", "") or "") == path:
            _MODS[name] = have
            return have
        spec = importlib.util.spec_from_file_location(f"trust_signals_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        # A dataclass looks its module up in sys.modules while it is built.
        sys.modules.setdefault(spec.name, mod)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return mod


def te() -> Any:
    return _load("trust_events")


# ── switches ────────────────────────────────────────────────────────────────


def _switch(env_name: str, default: bool, key: str, environ: Optional[Mapping[str, str]]) -> bool:
    env = os.environ if environ is None else environ
    try:
        if not te().enabled(env):
            return False
        return bool(_load("hook_config").switch(env_name, default, env, key))
    except Exception:  # noqa: BLE001 - a broken install: the signal is off
        return False


def citation_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    """A citation in a reply is a ``use``. Off by default."""
    return _switch(CITATION_ENV, CITATION_DEFAULT, CITATION_KEY, environ)


def correction_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    """A correction of a cited note is a ``contradict``. Off by default."""
    return _switch(CORRECTION_ENV, CORRECTION_DEFAULT, CORRECTION_KEY, environ)


def any_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    return citation_on(environ) or correction_on(environ)


# ── the shown notes (written by the prompt hook) ───────────────────────────


def shown_file(cache: str, session_id: Any) -> Optional[str]:
    sid = te().valid_sid(session_id)
    if sid is None:
        return None
    return os.path.join(te().session_dir(cache), f"{sid}.{SHOWN_NAME}")


def _mid(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        mid = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return mid if mid >= 1 else None


def load_shown(cache: str, session_id: Any) -> Dict[int, Dict[str, str]]:
    """``{memory id: {"name", "rule"}}`` of the notes shown in the session."""
    path = shown_file(cache, session_id)
    out: Dict[int, Dict[str, str]] = {}
    if path is None:
        return out
    try:
        with open(path, "rb") as fh:
            data = fh.read(SHOWN_MAX_BYTES + 65536)
    except OSError:
        return out
    for raw in data.split(b"\n"):
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        mid = _mid(rec.get("mv_id"))
        name = rec.get("name")
        rule = rec.get("rule")
        if mid is None or mid in out:
            continue
        out[mid] = {
            "name": name[:NAME_MAX] if isinstance(name, str) else "",
            "rule": rule[:RULE_MAX] if isinstance(rule, str) else "",
        }
    return out


def record_shown(
    cache: str,
    session_id: Any,
    event: str,
    rows: Iterable[Tuple[Any, Any, Any]],
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """The prompt hook's call, after the emit: ``rows`` of ``(id, name,
    rule)``. Appends the notes this session has not had yet, in one write.
    Only for the prompt hook's own events and only when a signal is on.
    Returns True when lines were written. Never raises."""
    try:
        if event not in te().RECALL_EVENTS or not any_on(environ):
            return False
        path = shown_file(cache, session_id)
        if path is None:
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > SHOWN_MAX_BYTES:
            return False
        have = set(load_shown(cache, session_id)) if size else set()
        lines: List[str] = []
        for mid_raw, name, rule in rows:
            mid = _mid(mid_raw)
            if mid is None or mid in have:
                continue
            have.add(mid)
            doc = {
                "mv_id": mid,
                "name": name[:NAME_MAX] if isinstance(name, str) else "",
                "rule": " ".join(rule.split())[:RULE_MAX] if isinstance(rule, str) else "",
            }
            lines.append(json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n")
        if not lines:
            return False
        te()._make_dirs(os.path.dirname(path))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, "".join(lines).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - a hook never fails on a cache file
        return False


def load_exposures(cache: str, session_id: Any) -> Dict[int, List[str]]:
    """``{memory id: [times shown, sorted]}`` from the ``recall`` lines of
    the session's event spool (the tail of at most SPOOL_READ_MAX bytes)."""
    out: Dict[int, List[str]] = {}
    path = te().events_file(cache, session_id)
    if path is None:
        return out
    try:
        start = max(0, os.path.getsize(path) - SPOOL_READ_MAX)
        records, _ = te().read_records(path, start)
    except OSError:
        return out
    for _, rec in records:
        if rec.get("kind") != te().KIND_RECALL:
            continue
        mid = _mid(rec.get("mv_id"))
        ts = te().norm_ts(rec.get("ts"))
        if mid is not None and ts is not None:
            out.setdefault(mid, []).append(ts)
    for times in out.values():
        times.sort()
    return out


# ── text matching ───────────────────────────────────────────────────────────


def words(text: str) -> List[str]:
    return _WORD_RE.findall((text or "").lower())


def content_words(text: str) -> Set[str]:
    return {w for w in words(text) if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()}


def name_ok(name: str) -> bool:
    """A name distinct enough that its presence in a text is a citation."""
    if not isinstance(name, str) or len(name) < NAME_MIN_CHARS:
        return False
    parts = [p for p in _NAME_PART_RE.split(name) if p]
    return len(parts) >= NAME_MIN_PARTS


def name_in(name: str, text: str) -> bool:
    """``name`` (or ``name.md``) as a whole token of ``text``, any case."""
    if not name_ok(name):
        return False
    stem = name[:-3] if name.lower().endswith(".md") else name
    rx = re.compile(r"(?<![\w-])" + re.escape(stem.lower()) + r"(?![\w-])")
    return rx.search((text or "").lower()) is not None


def phrases(rule: str) -> Set[Tuple[str, ...]]:
    """The PHRASE_WORDS-word runs of ``rule`` with enough content words."""
    ws = words(rule)
    out: Set[Tuple[str, ...]] = set()
    for i in range(len(ws) - PHRASE_WORDS + 1):
        run = tuple(ws[i : i + PHRASE_WORDS])
        if sum(1 for w in run if len(w) >= 3 and w not in STOPWORDS) >= PHRASE_MIN_CONTENT:
            out.add(run)
    return out


def sentences(text: str) -> List[str]:
    return [s for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]


_TOKEN_RES = (re.compile(r"[\w-]+"), re.compile(r"[\w.-]+"))


def _stem(name: str) -> str:
    low = name.lower()
    return low[:-3] if low.endswith(".md") else low


class _Note:
    """A shown note, prepared once per scan: its name and its phrases."""

    __slots__ = ("stem", "runs")

    def __init__(self, name: str, rule: str):
        self.stem = _stem(name) if name_ok(name) else ""
        self.runs = phrases(rule)


class _Reply:
    """A reply, prepared once per scan: the name-like tokens and the word
    runs of its sentences. A sentence that sets a note aside adds nothing,
    so a match anywhere in these sets is a citation."""

    __slots__ = ("tokens", "grams")

    def __init__(self, text: str):
        self.tokens: Set[str] = set()
        self.grams: Set[Tuple[str, ...]] = set()
        for sent in sentences((text or "")[:REPLY_MAX_CHARS]):
            if NEGATION_RE.search(sent):
                continue
            low = sent.lower()
            for rx in _TOKEN_RES if not SAVE_RE.search(sent) else ():
                for tok in rx.findall(low):
                    tok = tok.strip(".")
                    self.tokens.add(tok)
                    if tok.endswith(".md"):
                        self.tokens.add(tok[:-3])
            ws = words(sent)
            self.grams.update(
                tuple(ws[i : i + PHRASE_WORDS]) for i in range(len(ws) - PHRASE_WORDS + 1)
            )

    @property
    def empty(self) -> bool:
        return not self.tokens and not self.grams

    def names(self, note: _Note) -> bool:
        return bool(note.stem) and note.stem in self.tokens

    def cites(self, note: _Note) -> bool:
        if self.names(note):
            return True
        return bool(note.runs) and not note.runs.isdisjoint(self.grams)


def cites(text: str, name: str, rule: str) -> bool:
    """True when a sentence of ``text`` names the note or repeats a long
    phrase of its rule, and that sentence does not set the note aside. The
    correction signal needs this; a ``use`` needs the name (``names``)."""
    return _Reply(text).cites(_Note(name, rule))


def names(text: str, name: str) -> bool:
    """True when a sentence of ``text`` names the note by its file name,
    and that sentence neither sets the note aside nor saves it. Only this
    counts as a ``use`` (NOBLIVION-45): a phrase of the rule in the reply
    also matched a fact that the reply and the note both state."""
    return _Reply(text).names(_Note(name, ""))


class _Prompt:
    """A prompt, prepared once: its lower-case text and its word runs."""

    __slots__ = ("low", "grams")

    def __init__(self, text: str):
        self.low = (text or "").lower()
        ws = words(text)
        self.grams = {tuple(ws[i : i + PHRASE_WORDS]) for i in range(len(ws) - PHRASE_WORDS + 1)}

    def quotes(self, note: _Note) -> bool:
        if note.stem and re.search(r"(?<![\w-])" + re.escape(note.stem) + r"(?![\w-])", self.low):
            return True
        return bool(note.runs) and not note.runs.isdisjoint(self.grams)


def quotes(prompt: str, name: str, rule: str) -> bool:
    """The prompt names the note or repeats a long phrase of its rule: the
    note is the task, so a reply that names it restates the task
    (NOBLIVION-45)."""
    return _Prompt(prompt).quotes(_Note(name, rule))


def about(prompt: str, name: str, rule: str) -> bool:
    """The correction is about this note: it names it, or it shares at least
    OVERLAP_MIN content words with the note's rule and name, and they are at
    least OVERLAP_SHARE of the correction's content words."""
    if name_in(name, prompt):
        return True
    asked = content_words(prompt)
    if not asked:
        return False
    note = content_words(rule) | content_words(_NAME_PART_RE.sub(" ", name or ""))
    shared = asked & note
    return len(shared) >= OVERLAP_MIN and len(shared) / len(asked) >= OVERLAP_SHARE


def is_correction(prompt: str) -> Optional[str]:
    """The stop checks' own correction test (``stop_checks.is_correction``)."""
    try:
        return _load("stop_checks").is_correction(prompt)
    except Exception:  # noqa: BLE001 - no test: no correction
        return None


# ── transcript records ──────────────────────────────────────────────────────


def _text_blocks(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None  # a tool result, not a prompt
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts) if parts else None


def user_prompt(rec: Mapping[str, Any]) -> Optional[str]:
    """The text the user typed, or None for any other record."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("role", "user") != "user":
        return None
    text = _text_blocks(msg.get("content"))
    if text is None or not text.strip() or text.lstrip().startswith("<"):
        return None
    return text[:PROMPT_MAX_CHARS]


def reply_text(rec: Mapping[str, Any]) -> Optional[str]:
    """The prose of an assistant record (text blocks only), or None."""
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return "\n".join(parts)[:REPLY_MAX_CHARS] if parts else None


def file_key(path: str) -> str:
    """A note name or a file path -> its file name without ``.md``, lower
    case, ``-`` as ``_``: the key that says the two are the same note."""
    base = _stem(os.path.basename(str(path or "").rstrip("/\\")))
    return base.replace("-", "_")


def _note_path(path: str) -> bool:
    """A ``.md`` file in a folder named NOTE_DIR: a note of Claude Code."""
    parts = re.split(r"[/\\]+", str(path or ""))
    return len(parts) >= 2 and parts[-2] == NOTE_DIR and parts[-1].lower().endswith(".md")


_NOTE_PATH_RE = re.compile(r"(?:^|[/\\])" + NOTE_DIR + r"[/\\][\w.-]+\.md\b")


def written_files(rec: Mapping[str, Any]) -> Set[str]:
    """The ``file_key`` of each file that a tool call of this assistant
    record writes: Write, Edit, MultiEdit, NotebookEdit, a shell redirect
    (``>``, ``>>``, ``tee``) in a Bash command, or a ``.md`` file that a
    Bash command names when it runs a writer (``python``, ``sed -i``,
    ``cp``, ``mv``, ``rm`` ...). When one of these files is a note
    (``_note_path``), the set also holds NOTE_WRITE."""
    out: Set[str] = set()
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return out
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        name = block.get("name")
        paths: List[str] = []
        if name in WRITE_TOOLS:
            paths = [inp[f] for f in ("file_path", "notebook_path") if isinstance(inp.get(f), str)]
        elif name == "Bash" and isinstance(inp.get("command"), str):
            command = inp["command"]
            paths = _REDIRECT_RE.findall(command)
            if _WRITER_RE.search(command):
                paths += _MD_FILE_RE.findall(command)
                if _NOTE_PATH_RE.search(command):
                    out.add(NOTE_WRITE)
        for path in paths:
            if path:
                out.add(file_key(path))
                if _note_path(path):
                    out.add(NOTE_WRITE)
    out.discard("")
    return out


def _parse(ts: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _before(ts: str, limit: str, slack_s: int = 0) -> bool:
    """``ts`` is at least ``slack_s`` seconds before ``limit``."""
    a, b = _parse(ts), _parse(limit)
    if a is None or b is None:
        return False
    return (b - a).total_seconds() >= max(slack_s, 0) and a < b


def _shift(ts: str, seconds: int) -> str:
    """``ts`` moved by ``seconds``, in the same format; ``ts`` when it is not a time.
    The prompt hook may write its ``recall`` line a moment before the
    transcript stamps the prompt."""
    t = _parse(ts)
    if t is None:
        return ts
    return te().now_iso(t + _dt.timedelta(seconds=seconds))


# ── the scan ────────────────────────────────────────────────────────────────


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _state(raw: Any) -> Dict[str, Any]:
    """The saved scan state, every field checked: it is a file on disk."""
    doc = raw if isinstance(raw, dict) else {}
    prompts = [p for p in _list(doc.get("prompts")) if isinstance(p, str)]
    cited_raw = doc.get("cited") if isinstance(doc.get("cited"), dict) else {}
    cited = {str(k): v for k, v in cited_raw.items() if _mid(k) and isinstance(v, str)}
    used = [m for m in (_mid(x) for x in _list(doc.get("used"))) if m is not None]
    hit = [m for m in (_mid(x) for x in _list(doc.get("contradicted"))) if m is not None]
    turn_raw = doc.get("turn") if isinstance(doc.get("turn"), dict) else {}
    turn = {
        "echo": turn_raw.get("echo") is True,
        "written": {w for w in _list(turn_raw.get("written")) if isinstance(w, str)},
        "task": {m for m in (_mid(x) for x in _list(turn_raw.get("task"))) if m is not None},
    }
    return {"prompts": prompts, "cited": cited, "used": used, "contradicted": hit, "turn": turn}


def scan(
    records: Iterable[Mapping[str, Any]],
    shown: Mapping[int, Mapping[str, str]],
    exposures: Mapping[int, Sequence[str]],
    state: Any = None,
    citation: bool = True,
    correction: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Transcript records, in order -> ``(events, state)``.

    ``shown`` maps a note id to its name and rule; ``exposures`` maps it to
    the times it was shown. ``state`` carries what earlier calls saw (the
    last prompt times, the notes cited and when). A note gets at most one
    ``use`` and one ``contradict`` per session from these signals.

    The citations of a turn (a prompt and the records up to the next one)
    count when the turn ends, or at the end of ``records``: a citation of a
    note that a tool call of the same turn writes does not count, and in a
    turn whose prompt asks to see the notes (``ECHO_PROMPT_RE``) none does.
    A ``use`` also needs the note's name (a phrase only is a citation for
    the correction signal), a turn that writes no note (``NOTE_WRITE`` or
    a shown note's file), and a prompt that does not quote the note.
    A turn split over two calls keeps what its first part wrote and the
    notes its prompt quotes in ``state``; a citation counted in the first
    part stays counted.
    """
    st = _state(state)
    used: Set[int] = set(st["used"])
    contradicted: Set[int] = set(st["contradicted"])
    events: List[Dict[str, Any]] = []
    notes: Dict[int, _Note] = {}
    turn: Dict[str, Any] = st["turn"]
    pending: List[Tuple[int, str, bool]] = []

    keys = {file_key(n.get("name", "")) for n in shown.values()} - {""}

    def note(mid: int) -> _Note:
        if mid not in notes:
            notes[mid] = _Note(shown[mid].get("name", ""), shown[mid].get("rule", ""))
        return notes[mid]

    def settle() -> None:
        written = turn["written"]
        keeping = NOTE_WRITE in written or not keys.isdisjoint(written)
        for mid, at, named in pending:
            if file_key(shown[mid].get("name", "")) in written:
                continue  # the turn saves or edits this note
            st["cited"][str(mid)] = at
            if not named or keeping or mid in turn["task"]:
                continue  # a phrase only, a note-keeping turn, or the task itself
            if citation and mid not in used:
                used.add(mid)
                events.append({"mv_id": mid, "kind": te().KIND_USE, "ts": at, "src": SRC_CITATION})
        pending.clear()

    for rec in records:
        ts = te().norm_ts(rec.get("timestamp"))
        if ts is None:
            continue
        prompt = user_prompt(rec)
        if prompt is not None:
            settle()
            asked = _Prompt(prompt)
            turn = {
                "echo": bool(ECHO_PROMPT_RE.search(prompt)),
                "written": set(),
                "task": {mid for mid in shown if asked.quotes(note(mid))},
            }
            if correction:
                mid = _correction_target(prompt, ts, shown, exposures, st)
                if mid is not None and mid not in contradicted:
                    contradicted.add(mid)
                    events.append(
                        {
                            "mv_id": mid,
                            "kind": te().KIND_CONTRADICT,
                            "ts": ts,
                            "src": SRC_CORRECTION,
                        }
                    )
            st["prompts"] = (st["prompts"] + [ts])[-CORRECTION_WINDOW:]
            continue
        if len(turn["written"]) < WRITTEN_MAX:
            turn["written"] |= written_files(rec)
        reply = reply_text(rec)
        if reply is None or turn["echo"]:
            continue
        prepared = _Reply(reply)
        if prepared.empty:
            continue
        found: List[int] = []
        for mid in shown:
            times = exposures.get(mid) or ()
            if not times or min(times) > ts:
                continue  # not shown yet
            if prepared.cites(note(mid)):
                found.append(mid)
        if len(found) < ECHO_MIN_NOTES:  # more is a list of the notes
            pending.extend((mid, ts, prepared.names(note(mid))) for mid in found)
    settle()
    st["turn"] = {
        "echo": turn["echo"],
        "written": sorted(turn["written"])[:WRITTEN_MAX],
        "task": sorted(turn["task"])[:CITED_MAX],
    }
    if len(st["cited"]) > CITED_MAX:
        keep = sorted(st["cited"].items(), key=lambda kv: kv[1])[-CITED_MAX:]
        st["cited"] = dict(keep)
    st["used"] = sorted(used)
    st["contradicted"] = sorted(contradicted)
    return events, st


def _correction_target(
    prompt: str,
    ts: str,
    shown: Mapping[int, Mapping[str, str]],
    exposures: Mapping[int, Sequence[str]],
    st: Mapping[str, Any],
) -> Optional[int]:
    """The ONE note this correction contradicts, or None. All must hold:
    the prompt is a correction; it does not hold Claude Code to a note
    (ENFORCE_RE); the note was shown in the last CORRECTION_WINDOW prompts
    and not just now; a reply cited it after it was shown; the correction is
    about it (``about``). Two or more such notes: None."""
    if not is_correction(prompt) or ENFORCE_RE.search(prompt):
        return None
    prompts: List[str] = st["prompts"]
    window = (
        _shift(prompts[-CORRECTION_WINDOW], -PROMPT_SLACK_S)
        if len(prompts) >= CORRECTION_WINDOW
        else ""
    )
    found: List[int] = []
    for key, cited_ts in st["cited"].items():
        mid = _mid(key)
        if mid is None or mid not in shown:
            continue
        times = [
            t
            for t in exposures.get(mid) or ()
            if t >= window and _before(t, ts, PROMPT_SLACK_S) and t <= cited_ts
        ]
        if not times or cited_ts >= ts:
            continue
        note = shown[mid]
        if about(prompt, note.get("name", ""), note.get("rule", "")):
            found.append(mid)
    return found[0] if len(found) == 1 else None
