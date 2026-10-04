#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trust events for the claude_code persona: the mapping and the local append.
Part E of the adaptive memory trust work.

ONE mapping, used by every producer, so the live hooks and the transcript
replay can never count the same thing two ways:

| What happened                       | Producer                      | Key            | kind       |
|-------------------------------------|-------------------------------|----------------|------------|
| an index row was shown              | recall hook, at the emit      | mv_id (row id) | recall     |
| ``noblivion_recall`` fetched a memory | Stop flush, from the transcript | mv_id      | use        |
| a guard deny on rule R              | guard hook                    | path (R + .md) | use        |
| a guard override of rule R          | guard hook                    | path (R + .md) | contradict |
| guard rows (display only), labels, an error, a stop check pass | none |          |            |

``recall`` is exposure only: the store keeps it as NOT citation capable, so a
row that is shown and not used does not lose trust. ``use`` is the evidence
that a rule met real work or was opened. Guard ``rows`` only SHOW a rule next
to a command, so they are not a use (NOBLIVION-34).

A deny is a use only when the model does not override it. The hook cannot
know that at deny time: the override comes later, as a retry of the command
with the ``# guard-ok: <reason>`` marker. So the deny is recorded as ``use``
and the override as ``contradict``. In one session that nets below the prior:
the score counts a ``contradict`` session as a trial and takes 2 uses off for
it (``noblivion.trust.trust_score``).

The fetched id is read from the tool RESULT (``GROUNDED MEMORY <id>: ...``),
never from the tool input alone: with the rule index on, the MCP server reads a
small ``fetch_id`` as a RANK of the last index and answers with the real id.

The local append. One JSON line per event, appended to
``<cache>/by-session/<session>.trust-events.jsonl`` with one ``write`` call
(O_APPEND), under 1 ms. It runs unless ``NOBLIVION_TRUST_EVENTS`` (or the config
key ``trust.events``) is ``0``, ``off``, ``false`` or ``no``; the default is on
(design doc section 12.3). It never raises and never
prints. ``<cache>`` is ``NOBLIVION_RECALL_CACHE_DIR`` (default
``<data dir>/cache``), the recall hook's own folder.

A line holds the contract fields of POST /api/memory/feedback/batch (contract
C1: ``mv_id`` or ``path``, ``kind``, ``ts``) and a local ``src`` (and, for a
fetch, ``tool_use_id`` and the file ``name`` the result named). The flush sends
the contract fields only, and the ``src`` as the optional C1 field ``sources``
so the store can tell a guard deny from a fetch.

This module is imported by hooks, so it stays small and imports nothing heavy.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

EVENTS_ENV = "NOBLIVION_TRUST_EVENTS"
CACHE_ENV = "NOBLIVION_RECALL_CACHE_DIR"
DEFAULT_CACHE_DIR = ""  # "": <data dir>/cache (hook_config)
SESSION_DIR_NAME = "by-session"  # the recall hook's per-session folder
EVENTS_NAME = "trust-events.jsonl"

KIND_RECALL = "recall"
KIND_USE = "use"
KIND_CONTRADICT = "contradict"
KINDS = (KIND_RECALL, KIND_USE, KIND_CONTRADICT)

SRC_INDEX = "index"
SRC_FETCH = "fetch"
SRC_GUARD_ROWS = "guard_rows"  # written by older hooks only; never a use now
SRC_GUARD_DENY = "guard_deny"
SRC_GUARD_OVERRIDE = "guard_override"
SRC_HITS = "hits"  # the replay's pre-index hit rows (exposure)
# guard decision -> (kind, src). The decisions "rows" and "labels" are NOT
# here on purpose: they only show a rule, so they are not a use, in any mode.
GUARD_EVENTS = {
    "deny": (KIND_USE, SRC_GUARD_DENY),
    "override": (KIND_CONTRADICT, SRC_GUARD_OVERRIDE),
}

# One setting for the three trust modes. "b" and "c" act in trust_rank. "a"
# meant "guard rows are not a use"; that is now the default, so "a" changes
# nothing here. Unset or any other value = the default.
TRUST_MODE_ENV = "NOBLIVION_RECALL_TRUST_MODE"
TRUST_MODES = ("a", "b", "c")

# The recall hook's own events. The subagent hook runs the same index code with
# its own labels ("SubagentStart:<via>", "AgentRewrite"), its own cache folder
# and a compound session id; those exposures are not recorded in V2
# (REVIEW-4453 n4: an optional source, and shown is not a trial).
RECALL_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})

# The store's session-id rule (noblivion.trust.SESSION_ID_PATTERN), copied
# because the hooks do not import the package. A session the store refuses
# is not recorded, so its events are never sent (NOBLIVION-21). A test
# checks that the two strings match.
SESSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"  # used with fullmatch
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
_STEM_RE = re.compile(r"^[\w.-]{1,200}$")  # as the guard hook's _SAFE_ID
_FILE_RE = re.compile(r"^[\w.-]{1,200}\.md$")
# A source name, as the daemon checks it (the store's source-name check).
# \Z, not $: "$" matches before a trailing newline.
_SRC_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
SOURCES_MAX = 8  # the daemon rejects an event with more

RECALL_TOOL_SUFFIX = "noblivion_recall"
_FETCH_HEAD_RE = re.compile(r"^GROUNDED MEMORY (\d+): ")
_FETCH_SOURCE_RE = re.compile(r" \[([\w.-]{1,200}\.md)\]\s*$")
PENDING_MAX = 200


# ── switches and paths ──────────────────────────────────────────────────────


EVENTS_KEY = "trust.events"
_HOOK_CONFIG: List[Any] = []


def _hook_config() -> Any:
    """``hook_config.py`` from this file's folder, loaded once."""
    if not _HOOK_CONFIG:
        path = Path(__file__).resolve().parent / "hook_config.py"
        spec = importlib.util.spec_from_file_location("hook_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _HOOK_CONFIG.append(mod)
    return _HOOK_CONFIG[0]


def enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """On by default (design doc section 12.3: ``trust.events`` is ``1``).
    Off when ``NOBLIVION_TRUST_EVENTS``, else the config key ``trust.events``,
    is off (``hook_config.switch``: 0, false, no, off or empty). The events
    stay on this machine."""
    env = os.environ if environ is None else environ
    try:
        return bool(_hook_config().switch(EVENTS_ENV, True, env, EVENTS_KEY))
    except Exception:  # noqa: BLE001 - a broken install reads as the default
        return True


def _default_cache_dir(env: Mapping[str, str]) -> str:
    """``<data dir>/cache`` from ``hook_config.py`` in this file's folder."""
    return str(_hook_config().cache_dir(env))


def cache_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return os.path.expanduser(env.get(CACHE_ENV) or DEFAULT_CACHE_DIR or _default_cache_dir(env))


def valid_sid(session_id: Any) -> Optional[str]:
    if isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id):
        return session_id
    return None


def session_dir(cache: str) -> str:
    return os.path.join(cache, SESSION_DIR_NAME)


def events_file(cache: str, session_id: Any) -> Optional[str]:
    sid = valid_sid(session_id)
    if sid is None:
        return None
    return os.path.join(session_dir(cache), f"{sid}.{EVENTS_NAME}")


def now_iso(now: Optional[_dt.datetime] = None) -> str:
    """UTC, ISO-8601, seconds: ``2026-10-01T21:47:18+00:00``."""
    t = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def norm_ts(value: Any) -> Optional[str]:
    """A transcript or log time (``...Z`` or with an offset) as ``now_iso``
    writes it, or None when it is not a time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return now_iso(t)


# ── the mapping ─────────────────────────────────────────────────────────────


def _mid(value: Any) -> Optional[int]:
    """A memory id: a whole number >= 1, from an int or its text. A bool, a
    float or anything else is refused (int(True) is 1, int(1.5) is 1)."""
    if isinstance(value, bool):
        return None
    try:
        mid = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return mid if mid >= 1 else None


def index_events(mids: Iterable[Any], ts: str, src: str = SRC_INDEX) -> List[Dict[str, Any]]:
    """Index rows shown -> one ``recall`` event per memory id (exposure only)."""
    out: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    for value in mids:
        mid = _mid(value)
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        out.append({"mv_id": mid, "kind": KIND_RECALL, "ts": ts, "src": src})
    return out


def hit_events(stems: Iterable[Any], ts: str) -> List[Dict[str, Any]]:
    """The replay's pre-index hit rows (2026-09-22..09-30), already joined to
    their file stems -> one ``recall`` event per file, keyed by path."""
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for stem in stems:
        path = path_of_stem(stem)
        if path is None or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "kind": KIND_RECALL, "ts": ts, "src": SRC_HITS})
    return out


def path_of_stem(stem: Any) -> Optional[str]:
    """A guard-table id (the memory file stem) -> ``<stem>.md``, or None for an
    id that is not a plain file stem."""
    if not isinstance(stem, str) or not _STEM_RE.match(stem) or stem.startswith("."):
        return None
    return f"{stem}.md"


def trust_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """``a``, ``b`` or ``c`` from NOBLIVION_RECALL_TRUST_MODE; ``""`` otherwise.
    trust_rank.trust_mode reads it the same way."""
    env = os.environ if environ is None else environ
    raw = (env.get(TRUST_MODE_ENV) or "").strip().lower()
    return raw if raw in TRUST_MODES else ""


def guard_events(
    decision: str, ids: Iterable[Any], ts: str, environ: Optional[Mapping[str, str]] = None
) -> List[Dict[str, Any]]:
    """A guard decision on rule R -> one event per R, keyed by path: a deny
    -> ``use``, an override -> ``contradict``. Every other decision (rows,
    labels, error, apply-failed) -> none: guard rows only show a rule, so
    they are not a use (NOBLIVION-34). ``environ`` is kept for callers; no
    mode changes the mapping."""
    kind_src = GUARD_EVENTS.get(decision)
    if kind_src is None:
        return []
    kind, src = kind_src
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for stem in ids:
        path = path_of_stem(stem)
        if path is None or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "kind": kind, "ts": ts, "src": src})
    return out


def fetch_event(result_text: str, ts: str, tool_use_id: str = "") -> Optional[Dict[str, Any]]:
    """A ``noblivion_recall`` fetch RESULT -> one ``use`` event, or None.

    Only a result that opens with ``GROUNDED MEMORY <id>: `` is a fetch that
    returned a memory: an error result, a search answer (``GROUNDED MEMORY
    (... persona ...``) and anything else give no event. The id is the one
    the server answered with, so a fetch by rank counts the memory it read.
    """
    first = (result_text or "").lstrip().split("\n", 1)[0]
    m = _FETCH_HEAD_RE.match(first)
    if m is None:
        return None
    mid = _mid(m.group(1))
    if mid is None:
        return None
    ev: Dict[str, Any] = {"mv_id": mid, "kind": KIND_USE, "ts": ts, "src": SRC_FETCH}
    if tool_use_id:
        ev["tool_use_id"] = tool_use_id
    src = _FETCH_SOURCE_RE.search(first)
    if src and _FILE_RE.match(src.group(1)):
        ev["name"] = src.group(1)
    return ev


def contract_event(ev: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The C1 shape of a local line: ``{"mv_id"|"path", "kind", "ts"}`` plus
    ``"sources": [src]`` when the line has a valid ``src``, or None for a line
    that is not a valid event. ``mv_id`` wins when a line has both. A bad
    ``src`` is dropped, not the event: the server rejects an event whole for a
    bad source name. A ``use`` line from guard rows (an older hook wrote it
    before NOBLIVION-34) is not a valid event: a shown rule is not a use."""
    kind = ev.get("kind")
    ts = norm_ts(ev.get("ts"))
    if kind not in KINDS or ts is None:
        return None
    if kind == KIND_USE and ev.get("src") == SRC_GUARD_ROWS:
        return None
    out: Dict[str, Any]
    mid = _mid(ev.get("mv_id")) if ev.get("mv_id") is not None else None
    path = ev.get("path")
    if mid is not None:
        out = {"mv_id": mid, "kind": kind, "ts": ts}
    elif isinstance(path, str) and _FILE_RE.match(path) and not path.startswith("."):
        out = {"path": path, "kind": kind, "ts": ts}
    else:
        return None
    src = ev.get("src")
    if isinstance(src, str) and _SRC_RE.match(src):
        out["sources"] = [src]
    return out


def merge_events(events: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Local lines -> C1 events, one per (memory, kind) with the earliest time
    and the sorted union of the sources of its lines, oldest first. Returns
    ``(events, bad)``, ``bad`` = lines that are not events. The server keeps
    one row per (session, memory, kind), so a repeat would only come back as a
    duplicate."""
    merged: Dict[Tuple[str, Any, str], Dict[str, Any]] = {}
    sources: Dict[Tuple[str, Any, str], Set[str]] = {}
    bad = 0
    for raw in events:
        ev = contract_event(raw)
        if ev is None:
            bad += 1
            continue
        key = (
            ("mv_id", ev["mv_id"], ev["kind"])
            if "mv_id" in ev
            else ("path", ev["path"], ev["kind"])
        )
        sources.setdefault(key, set()).update(ev.pop("sources", ()))
        old = merged.get(key)
        if old is None or ev["ts"] < old["ts"]:
            merged[key] = ev
    for key, ev in merged.items():
        if sources[key]:
            ev["sources"] = sorted(sources[key])[:SOURCES_MAX]
    out = sorted(
        merged.values(), key=lambda e: (e["ts"], e["kind"], str(e.get("mv_id") or e.get("path")))
    )
    return out, bad


# ── the local append ────────────────────────────────────────────────────────


def append_events(
    cache: str,
    session_id: Any,
    events: Sequence[Mapping[str, Any]],
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Append ``events`` to the session's file, one JSON line each, in ONE write.
    Off when ``enabled`` is False. Returns True when the lines were written.
    Never raises: a hook must not fail because a cache file could not be written.
    """
    if not events or not enabled(environ):
        return False
    path = events_file(cache, session_id)
    if path is None:
        return False
    try:
        data = "".join(
            json.dumps(dict(ev), separators=(",", ":"), sort_keys=True) + "\n" for ev in events
        ).encode("utf-8")
        _make_dirs(os.path.dirname(path))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return True
    except (OSError, TypeError, ValueError):
        return False


def record_index(
    cache: str,
    session_id: Any,
    event: str,
    mids: Iterable[Any],
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """The recall hook's call: the ids of the rows it just showed. Only the
    recall hook's own events count (see RECALL_EVENTS)."""
    if event not in RECALL_EVENTS or not enabled(environ):
        return False
    return append_events(cache, session_id, index_events(mids, now_iso()), environ)


def record_guard(
    environ: Mapping[str, str], session_id: Any, decision: str, ids: Iterable[Any]
) -> bool:
    """The guard hook's call: a ``deny`` or ``override`` decision on these
    memory ids (file stems); any other decision writes nothing. The file goes
    to the recall cache folder, where the Stop flush reads it."""
    if not enabled(environ):
        return False
    return append_events(
        cache_dir(environ), session_id, guard_events(decision, ids, now_iso(), environ), environ
    )


# ── transcripts ─────────────────────────────────────────────────────────────


def read_records(path: str, offset: int = 0) -> Tuple[List[Tuple[int, Dict[str, Any]]], int]:
    """The complete JSON lines of ``path`` from byte ``offset``:
    ``([(line_start, record), ...], new_offset)``. A last line with no newline
    is not read (Claude Code may still be writing it), so ``new_offset`` stops
    before it. A line that is not a JSON object is skipped."""
    out: List[Tuple[int, Dict[str, Any]]] = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    pos = 0
    end = data.rfind(b"\n") + 1
    for raw in data[:end].split(b"\n")[:-1] if end else []:
        start = offset + pos
        pos += len(raw) + 1
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append((start, rec))
    return out, offset + end


def _blocks(rec: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def is_recall_tool(name: Any) -> bool:
    return isinstance(name, str) and name.startswith("mcp__") and name.endswith(RECALL_TOOL_SUFFIX)


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def fetch_uses(
    records: Iterable[Mapping[str, Any]],
    pending: Optional[Mapping[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """``noblivion_recall`` fetch calls in transcript records -> ``use`` events.

    A call is paired with its result by ``tool_use_id``. ``pending`` holds the
    calls seen earlier without a result (id -> the call's time), for a reader
    that reads a transcript in pieces (the Stop flush). Returns the events and
    the calls still without a result. A search call (``query``) is not a fetch
    and is never pending.
    """
    calls: Dict[str, str] = dict(pending or {})
    events: List[Dict[str, Any]] = []
    for rec in records:
        ts = norm_ts(rec.get("timestamp")) or now_iso()
        for block in _blocks(rec):
            kind = block.get("type")
            if kind == "tool_use" and is_recall_tool(block.get("name")):
                ti = block.get("input")
                tid = block.get("id")
                if isinstance(ti, dict) and ti.get("fetch_id") is not None and isinstance(tid, str):
                    calls[tid] = ts
            elif kind == "tool_result":
                tid = block.get("tool_use_id")
                if not isinstance(tid, str) or tid not in calls:
                    continue
                call_ts = calls.pop(tid)
                if block.get("is_error"):
                    continue
                ev = fetch_event(_result_text(block), call_ts, tid)
                if ev is not None:
                    events.append(ev)
    if len(calls) > PENDING_MAX:
        calls = dict(sorted(calls.items(), key=lambda kv: kv[1])[-PENDING_MAX:])
    return events, calls


def is_agent_transcript(name: str) -> bool:
    """A subagent transcript file name (``agent-<id>.jsonl``); a workflow's
    ``journal.jsonl`` is not one."""
    return name.startswith("agent-") and name.endswith(".jsonl")


def iter_transcript_files(main_path: str) -> Iterator[str]:
    """The main transcript and every subagent transcript of its session:
    ``<dir>/<session>/subagents/agent-*.jsonl`` and, for workflow agents,
    ``<dir>/<session>/subagents/workflows/<wf>/agent-*.jsonl``."""
    yield main_path
    folder = os.path.join(os.path.splitext(main_path)[0], "subagents")
    found: List[str] = []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        found.extend(os.path.join(root, n) for n in names if is_agent_transcript(n))
    yield from sorted(found)


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _hook_config().make_dirs(path)
