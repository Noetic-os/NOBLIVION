#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label rows: memories whose subject labels a call or a prompt names.

Two legs, one rule set. Both read the label index of the guard table
(``label_index``, built by ``guard_table.build`` from every memory
file; ``memory_labels``). No daemon call, no network.

  * Tool time: the guard hook (``guard_hook.decide``) calls
    ``candidates`` and ``pick`` in its no-hit branch only. A call that a
    ``violates:`` rule denies never gets label rows, and a label row never
    denies: it is context text, nothing else.
  * Prompt time: the recall hook (``recall_hook.main``, event
    ``UserPromptSubmit``) calls ``prompt_leg`` after its own output. This leg
    is off unless ``NOBLIVION_RECALL_LABELS=1`` (the recall hook's rule: each
    leg behind its own variable), so a recall hook run without it prints
    what it printed before. Install: set it in the hook's environment.

The rules:
  * A label held by more than the index cutoff (8) memories never matches
    alone; such a memory needs a second shared label
    (``memory_labels.match``). Rank: the sum of the inverse
    document frequency of the shared labels.
  * Trigger rows come first. Label rows fill only the free places of
    ``MAX_ROWS`` (3) rows per call. At prompt time there are no trigger rows.
  * A memory already given in full in this agent of the session is skipped:
    the recall shown-set kinds ``apply``, ``fetched`` and ``trigger`` (the
    main agent only: a subagent never saw the main agent's prompt context),
    the guard state's full rows, and earlier label rows. The shown-set holds
    memory NAMES and the guard state holds file STEMS; the index maps both.
    An index menu row is not a delivery and does not count.
  * Own budget: label rows of one agent of a session stay under
    ``NOBLIVION_GUARD_LABEL_SESSION_CHARS`` (``SESSION_CHARS``, 4000). The trigger
    rows' budget (``ROWS_SESSION_CHARS``) is not charged.
  * One show of label rows stays under ``SHOW_CHARS`` and, with the trigger
    rows of the same call, under the guard's ``ROWS_MAX_CHARS``.
  * The matched labels of every row are logged: the guard log line of the
    call adds ``label_rows`` ``{memory: [labels]}``; a call with label rows
    only logs the decision ``labels``.
  * One switch per leg, so each leg can be measured alone: the tool-time leg
    is on wherever the guard table has a label index (table version 2) and
    ``NOBLIVION_GUARD_LABELS=0`` turns it off; the prompt leg is off unless
    ``NOBLIVION_RECALL_LABELS=1``. A missing or broken index, or any error,
    gives no label rows (fail open).

State: the guard hook's per-agent session state (``SessionState``) keeps
``labels`` ``{memory: shows}`` and ``label_chars``. The prompt leg opens the
state of agent ``main`` of the session through the guard hook's own class, so
one lock and one file format serve both legs. Standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_TOOLS = Path(__file__).resolve().parent
MAX_ROWS = 3  # rows per call, trigger rows included
SESSION_CHARS = 4000  # label row text per agent of a session
SESSION_CHARS_ENV = "NOBLIVION_GUARD_LABEL_SESSION_CHARS"
SWITCH_ENV = "NOBLIVION_GUARD_LABELS"  # the tool-time leg: on unless 0/off/false/no
PROMPT_ENV = "NOBLIVION_RECALL_LABELS"  # the prompt leg: off unless set
FILE_TOOLS_ENV = "NOBLIVION_GUARD_LABELS_FILE_TOOLS"  # the file-tool leg: off unless set
READ_TOOLS = ("Read", "Grep", "Glob")  # tools the file-tool leg adds to the guard hook
FILE_CONTENT_CHARS = 2000  # the Edit/Write/MultiEdit content the labeller reads
SHOW_CHARS = 2400  # one show of label rows
ROW_TEXT_CHARS = 600  # the body part of one label row
MIN_ROOM = 300  # less room than this: no label row
PROMPT_CHARS = 8000  # the prompt text the labeller reads
DEFAULT_CACHE_DIR = ""  # "": <data dir>/cache (hook_config)
SHOWN_SET_NAME = "shown_set.json"
SESSION_DIR_NAME = "by-session"
DELIVERED_KINDS = ("apply", "fetched", "trigger")
HEADER = (
    "Memory rows by subject (label rows): each memory names a host, ticket, service, file "
    "or tool of this {what}. This is the memory's own text; use a row only if it applies."
)
_SAFE_ID = re.compile(r"^[\w.-]{1,200}$")
_SID_RX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_MODS: Dict[str, Any] = {}


def _load(name: str):
    mod = _MODS.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return mod


def _lab():
    return _load("memory_labels")


def enabled(env: Mapping[str, str]) -> bool:
    """The tool-time leg: on unless ``NOBLIVION_GUARD_LABELS`` is off
    (``hook_config.switch``: 0, false, no, off or empty)."""
    return bool(_load("hook_config").switch(SWITCH_ENV, True, env))


def prompt_enabled(env: Mapping[str, str]) -> bool:
    """The prompt leg: on only when ``NOBLIVION_RECALL_LABELS`` is on
    (1, true, yes or on)."""
    return bool(_load("hook_config").switch(PROMPT_ENV, False, env))


def file_tools_enabled(env: Mapping[str, str]) -> bool:
    """The file-tool leg (Read, Grep and Glob inputs; Edit, Write and
    MultiEdit content): on only when ``NOBLIVION_GUARD_LABELS_FILE_TOOLS`` is
    on and the tool-time leg is on."""
    on = bool(_load("hook_config").switch(FILE_TOOLS_ENV, False, env))
    return on and enabled(env)


def _abs(raw: str, cwd: str) -> str:
    return os.path.normpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))


def file_tool_query(tool: str, tool_input: Any, cwd: str) -> Optional[Tuple[str, str]]:
    """``(label text, log text)`` of a file-tool call, or None.

    Read: the path. Grep: the path, the ``glob`` filter and the pattern.
    Glob: the path and the pattern. Edit, Write and MultiEdit: the path, then
    the first ``FILE_CONTENT_CHARS`` characters of the content (Edit: new then
    old string; MultiEdit: every edit in order; Write: the content). The log
    text is the path (Grep and Glob: path and pattern), never the content."""
    if not isinstance(tool_input, Mapping):
        return None
    ti = tool_input

    def text(key: str) -> str:
        v = ti.get(key)
        return v if isinstance(v, str) else ""

    if tool == "Read" or tool in ("Edit", "Write", "MultiEdit"):
        if not text("file_path"):
            return None
        path = _abs(text("file_path"), cwd)
        if tool == "Read":
            return path, path
        if tool == "Write":
            body = text("content")
        elif tool == "Edit":
            body = "\n".join(x for x in (text("new_string"), text("old_string")) if x)
        else:
            got = ti.get("edits")
            edits: List[Any] = got if isinstance(got, list) else []
            body = "\n".join(
                str(e.get(k))
                for e in edits
                if isinstance(e, Mapping)
                for k in ("new_string", "old_string")
                if isinstance(e.get(k), str)
            )
        body = body[:FILE_CONTENT_CHARS]
        return (path + ("\n" + body if body else "")), path
    if tool in ("Grep", "Glob"):
        parts = [_abs(text("path"), cwd)] if text("path") else []
        parts += [x for x in (text("glob"), text("pattern")) if x]
        if not parts:
            return None
        q = "\n".join(parts)[:FILE_CONTENT_CHARS]
        return q, " ".join(parts)[:400]
    return None


def session_chars(env: Mapping[str, str]) -> int:
    try:
        return max(0, int(env.get(SESSION_CHARS_ENV) or SESSION_CHARS))
    except ValueError:
        return SESSION_CHARS


def index_of(table: Any) -> Optional[Mapping[str, Any]]:
    """The label index of a guard table, or None (old table, broken index)."""
    if not isinstance(table, Mapping):
        return None
    idx = table.get("label_index")
    try:
        return idx if _lab().index_ok(idx) else None
    except Exception:  # noqa: BLE001 - no labeller, no label rows
        return None


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def query_labels(
    query: str, subject: str, strip: Optional[Callable[[str], str]] = None
) -> Dict[str, str]:
    """The labels of the call or the prompt. A Bash command is labelled
    without its heredoc bodies (data, not the command): ``strip`` is
    ``guard_table.without_heredoc_bodies`` (the guard hook passes
    its own copy; else the module is loaded here)."""
    lab = _lab()
    if subject == "command":
        if strip is None:
            try:
                strip = _load("guard_table").without_heredoc_bodies
            except Exception:  # noqa: BLE001 - the whole command is labelled
                strip = None
        return lab.labels_of_command(query, strip)
    return lab.labels(query[:PROMPT_CHARS] if subject == "prompt" else query)


def candidates(
    query: str,
    subject: str,
    table: Any,
    env: Mapping[str, str],
    strip: Optional[Callable[[str], str]] = None,
) -> List[Any]:
    """The ranked label matches of ``query`` (``memory_labels.Match``),
    or ``[]``: the leg of ``subject`` switched off, no index, nothing
    specific, or any error."""
    on = {"prompt": prompt_enabled, "read": file_tools_enabled}.get(subject, enabled)(env)
    if not on or not isinstance(query, str) or not query.strip():
        return []
    try:
        idx = index_of(table)
        if idx is None:
            return []
        return _lab().match(query_labels(query, subject, strip), idx)
    except Exception:  # noqa: BLE001 - fail open
        return []


# --------------------------------------------------------------------------
# what this agent of the session already has in full
# --------------------------------------------------------------------------
def cache_dir(env: Mapping[str, str]) -> str:
    raw = env.get("NOBLIVION_RECALL_CACHE_DIR") or DEFAULT_CACHE_DIR
    return os.path.expanduser(raw) if raw else str(_load("hook_config").cache_dir(env))


def shown_set_path(cache: str, session_id: str) -> str:
    """``recall_hook.session_read_file`` for the shown-set and a
    valid session id: the session's own file, or the single file while the
    session has none."""
    own = os.path.join(cache, SESSION_DIR_NAME, f"{session_id}.{SHOWN_SET_NAME}")
    return own if os.path.exists(own) else os.path.join(cache, SHOWN_SET_NAME)


def delivered_names(env: Mapping[str, str], session_id: Any) -> Set[str]:
    """The memory names of the recall shown-set kinds ``apply``, ``fetched``
    and ``trigger`` for this session (``recall_hook.load_shown_set``
    rule: a file of another session reads as empty). No valid session id:
    nothing (the recall hook would read the newest file of ANY session).
    Never raises."""
    if not (isinstance(session_id, str) and _SID_RX.match(session_id)):
        return set()
    try:
        with open(shown_set_path(cache_dir(env), session_id), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(doc, dict):
        return set()
    stored = doc.get("session")
    if isinstance(stored, str) and _SID_RX.match(stored) and stored != session_id:
        return set()
    out: Set[str] = set()
    for kind in DELIVERED_KINDS:
        items = doc.get(kind)
        if isinstance(items, list):
            out.update(x for x in items if isinstance(x, str) and x)
    return out


def delivered(state: Any, env: Mapping[str, str], session_id: Any, agent: str) -> Set[str]:
    """Stems and names of the memories this agent of the session already got
    in full: guard full rows, earlier label rows, and (main agent only) the
    recall shown-set kinds apply, fetched and trigger."""
    out: Set[str] = set()
    for attr in ("full", "labels"):
        got = getattr(state, attr, None)
        if isinstance(got, dict):
            out.update(str(k) for k, v in got.items() if v)
    if agent == "main":
        out.update(delivered_names(env, session_id))
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _memory(m: Any, table: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """``(memory, body_known)``: rule, apply, description and body from
    ``<table source>/<id>.md``; the index's short text when it cannot be read."""
    mem: Dict[str, Any] = {"id": m.id, "rule": "", "apply": "", "description": m.text, "body": ""}
    src = str(table.get("source") or "")
    if not src or not _SAFE_ID.match(m.id) or m.id.startswith("."):
        return mem, False
    try:
        mt = _load("memory_text")
        got = mt.read_memory(Path(src) / f"{m.id}.md", lambda: _load("memory_fields"))
    except Exception:  # noqa: BLE001 - the short text still shows
        return mem, False
    mem.update({k: got.get(k, "") for k in ("rule", "apply", "description", "body")})
    if not mem["description"]:
        mem["description"] = m.text
    return mem, True


def render_row(
    m: Any, table: Mapping[str, Any], query: str, subject: str, compact: bool = False
) -> str:
    """One label row in the shared shape (``memory_text.render_hit``)
    plus the matched labels. A memory without a rule shows its description as
    ``Summary``, not as a rule."""
    mt = _load("memory_text")
    mem, known = _memory(m, table)
    subject = "file" if subject == "read" else subject
    text = mt.render_hit(
        mem,
        query,
        compact,
        ROW_TEXT_CHARS,
        subject=subject,
        compact_needs_apply=False,
        body_known=known,
        head_fallback=True,
        compact_note="label match; open the file if it applies" if compact else None,
    )
    if not mem.get("rule"):
        text = text.replace("\n  Rule: ", "\n  Summary: ", 1)
    labels = ", ".join(mt.inert(x).replace("`", "") for x in m.labels)
    return f"{text}\n  Labels: {labels}"


class Pick:
    """Label rows chosen for one show: ``text`` to add, ``rows`` (matches),
    ``charge(state)`` to call when the show is committed."""

    def __init__(self, rows: List[Any], text: str):
        self.rows = rows
        self.text = text

    @property
    def ids(self) -> List[str]:
        return [m.id for m in self.rows]

    def log_extra(self) -> Dict[str, Any]:
        return {"label_rows": {m.id: list(m.labels) for m in self.rows}}

    def charge(self, state: Any) -> Dict[str, Any]:
        shown = getattr(state, "labels", None)
        if not isinstance(shown, dict):
            shown = {}
            state.labels = shown
        for m in self.rows:
            shown[m.id] = int(shown.get(m.id, 0)) + 1
        state.label_chars = int(getattr(state, "label_chars", 0) or 0) + len(self.text)
        return self.log_extra()


def pick(
    matches: Sequence[Any],
    state: Any,
    env: Mapping[str, str],
    session_id: Any,
    agent: str,
    table: Mapping[str, Any],
    query: str,
    subject: str,
    taken: Sequence[str] = (),
    room: int = SHOW_CHARS,
) -> Optional[Pick]:
    """The label rows of one show, or None. ``taken``: the memories of this
    call's trigger rows (they fill places first, and are not repeated).
    ``room``: the characters this show may still add."""
    try:
        free = MAX_ROWS - len(taken)
        room = min(SHOW_CHARS, int(room))
        left = session_chars(env) - int(getattr(state, "label_chars", 0) or 0)
        if not matches or free <= 0 or min(room, left) < MIN_ROOM:
            return None
        have = delivered(state, env, session_id, agent) | {str(t) for t in taken}
        what = {
            "command": "call",
            "file": "file edit",
            "prompt": "prompt",
            "read": "file read or search",
        }.get(subject, "call")
        header = HEADER.format(what=what)
        rows: List[Any] = []
        parts: List[str] = [header]
        bound = min(room, left)
        for m in matches:
            if len(rows) >= free:
                break
            if m.id in have or m.name in have:
                continue
            for compact in (False, True):
                row = render_row(m, table, query, subject, compact)
                if len("\n".join(parts + [row])) <= bound:
                    parts.append(row)
                    rows.append(m)
                    break
            # A row that fits neither full nor compact is left out; a later,
            # shorter row may still fit the room.
        if not rows:
            return None
        return Pick(rows, "\n".join(parts))
    except Exception:  # noqa: BLE001 - fail open: no label rows
        return None


# --------------------------------------------------------------------------
# the prompt leg (UserPromptSubmit, called by the recall hook)
# --------------------------------------------------------------------------
def _guard_log(
    env: Mapping[str, str], session_id: Any, ids: List[str], extra: Dict[str, Any]
) -> None:
    """One ``labels`` line in the guard log, the guard hook's shape. No prompt text."""
    try:
        gh = _load("guard_hook")
        gh.log_line(env, session_id, "UserPromptSubmit", "labels", ids, "", "main", **extra)
    except Exception:  # noqa: BLE001, S110 - a log line is best effort; the rows are out
        pass


def prompt_leg(stdin_text: str, stdout: Any, env: Mapping[str, str]) -> int:
    """Add label rows for a ``UserPromptSubmit`` event, after the recall
    hook's own output. Returns the characters written. Never raises."""
    t0 = time.perf_counter()
    try:
        if _load("hook_config").switch(
            "NOBLIVION_RECALL_DISABLE", False, env
        ) or not prompt_enabled(env):
            return 0
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
            return 0
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            prompt = payload.get("user_input")
        if not isinstance(prompt, str) or not prompt.strip():
            return 0
        gh = _load("guard_hook")
        cwd = payload.get("cwd")
        tpath = gh.table_file(env, cwd if isinstance(cwd, str) else None)
        if not tpath.is_file():
            return 0
        table = _load("guard_table").load_table(tpath)
        matches = candidates(prompt, "prompt", table, env)
        if not matches:
            return 0
        sid = payload.get("session_id")
        state = gh.SessionState(env, sid, "main")
        saved = False
        try:
            got = pick(matches, state, env, sid, "main", table, prompt, "prompt")
            if got is None:
                return 0
            stdout.write(got.text + "\n")
            stdout.flush()
            extra = got.charge(state)
            state.close(save=True)
            saved = True
        finally:
            if not saved:
                state.close(save=False)
        extra["ms"] = int((time.perf_counter() - t0) * 1000)
        _guard_log(env, sid, got.ids, extra)
        return len(got.text)
    except BaseException:  # noqa: BLE001 - a hook fails open
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``--match TEXT``: print the label matches of TEXT against the guard
    table of the working dir's project (``NOBLIVION_GUARD_TABLE`` wins) as JSON."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--match" in args and args.index("--match") + 1 < len(args):
        env = dict(os.environ)
        gt = _load("guard_table")
        table = gt.load_table(gt.table_path())
        text = args[args.index("--match") + 1]
        print(
            json.dumps(
                [m.as_dict() for m in candidates(text, "command", table, env)[:10]], indent=1
            )
        )
        return 0
    print((__doc__ or "").split("\n\n")[0], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
