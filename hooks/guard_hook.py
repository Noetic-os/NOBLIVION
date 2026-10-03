#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PreToolUse guard hook: deny a Bash command that a memory rule forbids, and
show the matching rules next to an action. Work item WI-3.

Registered on ``PreToolUse`` with the matchers ``Bash``, ``Read|Grep``
(the credential guard below) and ``Edit|Write|MultiEdit``, and
on ``PostToolUseFailure`` with the matcher ``Bash`` (WI-3d, the evidence leg
below). Stdin is the Claude Code hook JSON (``session_id``,
``tool_name``, ``tool_input``, ``cwd``). The table is the WI-2 guard table
(``guard_table.table_path()``: env ``NOBLIVION_GUARD_TABLE``, default
``<data dir>/guard-table.json``). No daemon call, no network.

Bash
  * A ``violates:`` hit (``guard_table.match``; it strips the git
    global options and the env/sudo/nohup/setsid/timeout wrappers, ignores
    heredoc bodies, masks quoted data and ``#`` comments, and matches the
    string of ``bash -c '...'``, ``eval``, ``ssh host '...'`` as a command
    line of its own) prints ``permissionDecision: deny`` with the reason
    ``<rule>\\nApply: <apply>\\nMemory: <id>`` and one last line that gives the
    override syntax. With several hits the memory denied least often in this
    agent leads and the others are listed after it.
  * No budget (WI-3c). Every hit is denied, at any count. The old budget (2
    denies, then allow) let a stubborn retry through: arm G trap t06, runs s2
    and s7 (RESULT-G.md). The deny count per memory id per agent of a session
    (key: ``session_id`` plus ``agent_id``, ``main`` for the top-level agent;
    a subagent sends its parent's ``session_id``) only sets the text: from
    deny ``ESCALATE_AFTER + 1`` (3) the reason starts with "Denied N times in
    this session by this rule. A retry is denied every time ...".
  * The override marker is the one way through, and it is a deliberate act:
    a comment of the command line itself, ``# guard-ok: <reason>``, where the
    reason holds a letter or a digit (``override_marker``). A ``#`` in quotes,
    in a heredoc body, in an executed string, in ``$( )`` or backticks, or not
    at a word start is data, not a marker. A valid marker allows the call at
    any count, charges nothing, prints nothing and logs ``override`` with the
    reason and the command text, both redacted (``redact``). A marker with no
    reason does not allow; the deny names it. A false deny therefore costs one
    call plus one marked retry, and a loop needs the model to ignore the
    override line on every deny.
  * Evidence before an override (WI-3d). Arm G3C, trap t06: the model passed
    the deny with a marker and a FALSE reason ("no github remote") and never
    ran the rule's own command, which works in that checkout
    (RESULT-G3C.md). So a memory with a compliant form (table fields
    ``complies``, the memory's ``complies:`` regex, and ``run_first``, the
    command derived from its ``example_ok``) accepts the marker only after a
    run of that form FAILED in this agent of the session. The deny then says
    ``Run this command first: <run_first>`` and "Override only if that command
    fails in this session", in place of the plain override line. A marker
    without that evidence is refused: the call is denied (and charged) with
    the line "Your # guard-ok override is refused ...", and the ``deny`` log
    line adds ``override_refused`` (the ids) and ``reason``. With several hits
    every memory with a compliant form needs its own evidence. A memory with
    no compliant form keeps the plain override; the ``override`` log line
    adds ``evidence``: ``{id: "failed:<n>" | "no-complies"}``.
  * The evidence leg: on ``PostToolUseFailure`` (a Bash call that raised)
    the hook records a failed run when the error text starts with
    ``Exit code <n>``, optionally after ``Error: `` (n > 0: the command ran and exited non-zero; Claude
    Code's shell error text), the call was not interrupted, and the command
    is a run of a ``complies`` (``guard_table.complies_match``;
    a command that violates is never one). It prints nothing, never denies,
    and logs ``apply-failed`` with the ids and the first error line
    (redacted, max 120 characters). A permission denial or a hook deny is
    not evidence: the command never ran. A compliant run that succeeded
    leaves no evidence, so the override stays refused and the rule applies.
    Any other hook event (``PostToolUse`` ...) is ignored.
  * A ``triggers:`` hit with no violation (the rows-only leg): allow, and show
    at most ``MAX_ROWS`` (3) memories as ``additionalContext``, in the shape of
    ``memory_text.render_hit`` (WI-3f, from WI-14b): the id first,
    ``Rule:`` and ``Apply:`` whole, and the memory's own text (the body read
    from ``<table source>/<id>.md``: whole up to 1200 characters, else the part
    on this command, cut at a sentence end at 1200). No ``[memory <id>]`` tag,
    and the header says no file lookup is needed. Memory text is redacted
    (``redact_memory``) and made inert. A memory shown before in this agent of
    the session shows Rule and Apply only. One show stays under
    ``ROWS_MAX_CHARS`` (3600): a later row gets a shorter text, then Rule and
    Apply only; a row is dropped only when Rule and Apply alone break it (fields
    over the schema caps). The rows text of one agent of a session is full
    while under ``ROWS_FULL_SESSION_CHARS`` (12000; env
    ``NOBLIVION_GUARD_ROWS_FULL_SESSION_CHARS``), then Rule and Apply only. A plain trigger item fires when a shell segment of
    the command (the WI-2 split, also tried with its wrappers and git global
    options removed) equals it or starts with it plus a space. One memory's row
    is shown at most ``ROW_REPEAT`` (3) times per agent of a session, and all rows
    of one agent of a session together stay under ``ROWS_SESSION_CHARS`` (6000 characters, about
    1.5K tokens; env ``NOBLIVION_GUARD_ROWS_SESSION_CHARS``), counted at the size of the
    row before WI-3f (``legacy_text``), so the same rows show in the same calls. A trigger that fires on one of the everyday
    commands of ``memory_fields.BENIGN_COMMANDS`` (``tail``,
    ``git log``, ``sed -n`` ...) is not used for rows. Measured on the Bash
    commands of the 30 newest sessions (3288 calls): with neither bound, rows
    reached 59% of calls and about 71K characters per session; the two bounds
    bring it to about 5% of calls and under 6K characters per session.
  * WI-3g, weak triggers (``grade_rows``). A one-word command trigger
    (``sha256sum``, ``tar``), a command trigger with no specific argument
    (``gh api``, ``git push origin main``, ``git rev-parse --short``) unless the
    memory's rule line names it, and a ``glob:`` with no literal path part
    (``**/*.py``) are weak: they name the command, not the situation of the
    rule. A trigger of ``WEAK_SHARE`` (4) memories or more is weak too; a few
    holders never make a generic trigger strong (WI-3h). A weak row
    needs evidence: words of the call (the command, or the file path) that
    are also in the memory's rule or apply, less the trigger's words and
    ``EVIDENCE_STOP``, scored by their rarity in the table. No evidence: no
    row. Evidence under ``WEAK_FULL_SCORE`` (4.0): the Rule line only
    (``weak_row``; the log line adds ``rule_only``). Else the row as above.
    Strong rows come first. A deny never passes here. Env
    ``NOBLIVION_GUARD_WEAK_SHARE=0`` turns the grading off (the rows before WI-3g).
  * Label rows (``label_rows``). Where no
    ``violates:`` rule hits, the subject labels of the command or the path
    (host, ticket key, service, file name, tool) are matched against
    the table's ``label_index``. Label rows fill only the places that the
    trigger rows leave free (``MAX_ROWS``), skip a memory this agent of the
    session already got in full, and have their own session budget
    (``NOBLIVION_GUARD_LABEL_SESSION_CHARS``). A label row never denies. The log
    line adds ``label_rows`` (memory to matched labels); a call with label
    rows only logs ``labels``. ``NOBLIVION_GUARD_LABELS=0`` turns them off.
  * The file-tool leg of the label rows, off unless
    ``NOBLIVION_GUARD_LABELS_FILE_TOOLS=1``: Read, Grep and Glob calls get label
    rows from their path and pattern (label rows only: no trigger rows, never
    a deny), and Edit, Write and MultiEdit label a bounded slice of their
    content too (``label_rows.file_tool_query``). Switched off,
    a Read, Grep or Glob call returns before the table is read.

Credential guard (Bash, Read, Grep)
  * Before the table is read, ``credential_guard.decide`` denies
    a call that would put a git URL holding a credential (``user:secret@`` or
    a token as the user) into the model context: ``git remote -v|show|get-url``,
    ``git config --list|--get|--get-regexp`` of such a key, reads of a git
    config file (Bash ``cat``/``grep``/..., the Read tool, Grep in content
    mode). Only when the probe of that clone finds a credential URL, so a
    token-free clone is never denied. A scrub ``sed`` later in the pipeline
    and names-only ``git remote`` are allowed. No override marker; the reason
    gives the scrubbed forms and never the URL. The log line is
    ``credential-deny`` with the command (URL userinfo removed). Off with
    ``NOBLIVION_GUARD_CREDENTIAL=0``. Needs a PreToolUse entry with the matcher
    ``Read|Grep`` for the Read and Grep tools.

Edit, Write, MultiEdit
  * ``glob:`` triggers are matched against ``tool_input.file_path`` (made
    absolute with ``cwd``); a match shows rows as for Bash, same caps.
  * These tools are NEVER denied. Decision (2026-09-29): the ``violates:``
    field is defined as a regex over the raw Bash command
    (``memory_fields``), no memory in the live store has a
    ``violates:`` with scope ``file``, and a regex over a path or over new file
    text has no replay gate yet. A file deny needs its own field and its own
    replay first.
  * ``tool:`` and ``phrase:`` triggers are not used: a ``tool:Bash`` or
    ``tool:Edit`` trigger would fire on every call of the tool the hook is
    registered on, so it carries no signal; a phrase belongs to the prompt.

Every deny, every override, every recorded failed run and every shown row set goes to the
guard log (env ``NOBLIVION_GUARD_LOG``, default ``<data dir>/guard-log.jsonl``) as
one JSON line: ``ts`` (UTC), ``session_id``, ``agent_id``, ``tool``, ``decision``, ``ids``,
``text`` (the first 300 characters of the Bash command or of the file path;
the content of a Write is never logged). A deny line adds ``counts``, an
override line adds ``reason`` (at most 120 characters). State is one JSON file per agent of a
session under env ``NOBLIVION_GUARD_STATE_DIR`` (default
``<data dir>/guard-state/``); files older than 7 days are pruned when a new
session starts.

Install (review finding F3). Python itself exits 2 when it cannot open the
hook file, and Claude Code reads exit 2 from a PreToolUse hook as a block of
EVERY matched tool call. So register the plugin copy (not a worktree path),
next to ``guard_table.py`` and ``memory_fields.py``, inside a wrapper that
turns a launch failure into exit 0 and keeps stdout::

    sh -c 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard_hook.py"; exit 0'

The same wrapper is registered on ``PostToolUseFailure`` (matcher ``Bash``).
Without that registration no evidence is ever recorded, so the override of a
memory with a compliant form is always refused (the guard fails closed for
the override only; the deny itself is unchanged).

Fail open: any exception, a missing or broken table, bad stdin, or a run longer
than ``TIME_LIMIT_S`` exits 0 with no output (the error goes to the guard log
when it can). The log and the state files are created with mode 0600 (the
log holds command text), new folders with 0700. A missing, empty or broken table logs one ``error`` line per agent
of a session, not one per call. Standard library only.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_TOOLS = Path(__file__).resolve().parent


def _load_config_module():
    spec = importlib.util.spec_from_file_location("hook_config", _TOOLS / "hook_config.py")
    if spec is None or spec.loader is None:
        raise ImportError("hook_config")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _NoConfig:
    """Stand-in when ``hook_config.py`` cannot load (a lone copy of this
    file). The hook must still import, so ``main`` can fail open and log the
    error. Same data dir order as ``hook_config.data_dir``; no extra words."""

    @staticmethod
    def data_dir() -> Path:
        for name in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
            raw = (os.environ.get(name) or "").strip()
            if raw:
                return Path(os.path.expanduser(raw))
        xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
        base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "share"
        return base / "noblivion"

    @staticmethod
    def evidence_stop() -> frozenset:
        return frozenset()

    @staticmethod
    def generic_args() -> frozenset:
        return frozenset()


_CFG = None


def _cfg():
    global _CFG
    if _CFG is None:
        try:
            _CFG = _load_config_module()
        except Exception:  # noqa: BLE001 - a missing sibling must not stop the import
            _CFG = _NoConfig()
    return _CFG


DEFAULT_LOG = _cfg().data_dir() / "guard-log.jsonl"
DEFAULT_STATE = _cfg().data_dir() / "guard-state"
BASH_TOOLS = ("Bash",)
FILE_TOOLS = ("Edit", "Write", "MultiEdit")
READ_TOOLS = ("Read", "Grep", "Glob")  # label rows only, behind the file-tool switch
ESCALATE_AFTER = 2
ROW_REPEAT = 3
MAX_ROWS = 3
ROW_APPLY_CHARS = 220  # the legacy row (``legacy_row``) only
ROWS_MAX_CHARS = 3600  # one show of rows (WI-3f): one full row and two compact rows fit
ROWS_FULL_SESSION_CHARS = (
    12000  # row text per agent of a session before rows show Rule and Apply only
)
LOG_TEXT_CHARS = 300
STATE_MAX_AGE_S = 7 * 24 * 3600
TIME_LIMIT_S = 1.5
ROWS_SESSION_CHARS = 6000
ROWS_HEADER = (
    "Memory rules for this action (guard rows). This is the memory's own text, "
    "so act on it; no need to open the memory file."
)
SEEN_NOTE = "the full text was shown earlier in this session"


class _TimeUp(
    BaseException
):  # not Exception: no fail-open ``except Exception`` may swallow the alarm
    pass


def _load(name: str):
    path = _TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GT = None
_MF = None
_MT = None


def _mf():
    global _MF
    if _MF is None:
        _MF = _load("memory_fields")
    return _MF


def _gt():
    global _GT
    if _GT is None:
        _GT = _load("guard_table")
    return _GT


def _mt():
    global _MT
    if _MT is None:
        _MT = _load("memory_text")
    return _MT


def _label_candidates(
    query: str, subject: str, table: Mapping[str, Any], env: Mapping[str, str]
) -> List[Any]:
    """The label matches of the call (``label_rows``);
    ``[]`` when that module is missing or fails."""
    try:
        return _load("label_rows").candidates(
            query, subject, table, env, _gt().without_heredoc_bodies
        )
    except Exception:  # noqa: BLE001 - label rows fail open
        return []


def _file_tools_on(env: Mapping[str, str]) -> bool:
    """The file-tool leg switch of the label rows; False when the module is missing or fails.
    Unset (the default): False before the label module is loaded."""
    if not str(env.get("NOBLIVION_GUARD_LABELS_FILE_TOOLS") or "").strip():
        return False
    try:
        return bool(_load("label_rows").file_tools_enabled(env))
    except Exception:  # noqa: BLE001 - label rows fail open
        return False


def _file_query(tool: str, ti: Mapping[str, Any], cwd: str) -> Optional[Tuple[str, str]]:
    """``(label text, log text)`` of a file-tool call, or None (also on any error)."""
    try:
        return _load("label_rows").file_tool_query(tool, ti, cwd)
    except Exception:  # noqa: BLE001 - label rows fail open
        return None


def _label_pick(
    labs: List[Any],
    state: SessionState,
    env: Mapping[str, str],
    sid: object,
    agent: str,
    table: Mapping[str, Any],
    query: str,
    subject: str,
    taken: List[str],
    room: int,
) -> Any:
    """The label rows for the free places after the trigger rows, or None."""
    if not labs:
        return None
    try:
        return _load("label_rows").pick(
            labs, state, env, sid, agent, table, query, subject, taken, room
        )
    except Exception:  # noqa: BLE001 - label rows fail open
        return None


def _path_env(env: Mapping[str, str], name: str, default: Path) -> Path:
    return Path(env.get(name) or default).expanduser()


def log_path(env: Mapping[str, str]) -> Path:
    return _path_env(env, "NOBLIVION_GUARD_LOG", DEFAULT_LOG)


def state_dir(env: Mapping[str, str]) -> Path:
    return _path_env(env, "NOBLIVION_GUARD_STATE_DIR", DEFAULT_STATE)


def table_file(env: Mapping[str, str]) -> Path:
    raw = env.get("NOBLIVION_GUARD_TABLE")
    return Path(raw).expanduser() if raw else _gt().DEFAULT_TABLE


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------
def log_line(
    env: Mapping[str, str],
    session_id: object,
    tool: str,
    decision: str,
    ids: List[str],
    text: str,
    agent: str = "main",
    **extra: object,
) -> bool:
    """Append one JSON line to the guard log. Returns False when it could not."""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": str(session_id or ""),
        "agent_id": agent,
        "tool": tool,
        "decision": decision,
        "ids": list(ids),
        "text": (text or "")[:LOG_TEXT_CHARS],
    }
    rec.update(extra)
    try:  # best effort: a log failure never blocks a decision
        p = log_path(env)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# per-session state
# --------------------------------------------------------------------------
def agent_key(event: Mapping[str, object]) -> str:
    """The subagent of the call: ``agent_id``, or ``"main"`` for the top-level
    agent. A subagent sends its parent's ``session_id`` plus its own
    ``agent_id`` (review finding F2), so the session alone is not the key."""
    aid = event.get("agent_id") if isinstance(event, dict) else None
    return str(aid) if aid else "main"


def _state_file(env: Mapping[str, str], session_id: object, agent: str = "main") -> Path:
    raw = f"{session_id or 'no-session'}\0{agent or 'main'}"
    key = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return state_dir(env) / f"{key}.json"


def prune(folder: Path, now: Optional[float] = None, max_age: float = STATE_MAX_AGE_S) -> int:
    """Remove state files older than ``max_age`` seconds. Returns the count."""
    now = time.time() if now is None else now
    n = 0
    try:
        items = list(folder.glob("*.json"))
    except OSError:
        return 0
    for f in items:
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


class SessionState:
    """``{"denies": {id: n}, "rows": {id: n}, "full": {id: n}, "failed": {id: n}}``
    for one agent of one session (the top-level agent or one subagent), read
    and written under an exclusive lock so parallel tool calls do not lose
    counts. ``rows`` counts every show of a memory (the ``ROW_REPEAT`` cap);
    ``full`` counts the shows that were not a Rule-line-only weak row, so only
    those make a later row say ``SEEN_NOTE``."""

    def __init__(self, env: Mapping[str, str], session_id: object, agent: str = "main"):
        folder = state_dir(env)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = _state_file(env, session_id, agent)
        self.new = not self.path.exists()
        if self.new:
            prune(folder)
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        self._fh = os.fdopen(fd, "a+", encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        self._fh.seek(0)
        try:
            data = json.loads(self._fh.read() or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.denies: Dict[str, int] = dict(data.get("denies") or {})
        self.rows: Dict[str, int] = dict(data.get("rows") or {})
        self.full: Dict[str, int] = dict(data.get("full") or {})
        self.row_chars = int(data.get("row_chars") or 0)
        self.notes: Dict[str, int] = dict(data.get("notes") or {})
        self.failed: Dict[str, int] = dict(data.get("failed") or {})
        # Label rows shown per memory, and their own budget.
        self.labels: Dict[str, int] = dict(data.get("labels") or {})
        self.label_chars = int(data.get("label_chars") or 0)

    def close(self, save: bool = True) -> None:
        try:
            if save:
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(
                    json.dumps(
                        {
                            "denies": self.denies,
                            "rows": self.rows,
                            "full": self.full,
                            "row_chars": self.row_chars,
                            "notes": self.notes,
                            "failed": self.failed,
                            "labels": self.labels,
                            "label_chars": self.label_chars,
                        }
                    )
                )
                self._fh.flush()
        finally:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()


# --------------------------------------------------------------------------
# rows (triggers)
# --------------------------------------------------------------------------
_GLOB_RX: Dict[str, re.Pattern[str]] = {}


def glob_regex(glob: str) -> re.Pattern[str]:
    """A regex for a path glob: ``**/`` any folders (also none), ``**`` any
    text, ``*`` any text without ``/``, ``?`` one character that is not ``/``.
    It matches at the end of a path, starting at the path's start or after a
    ``/``, so ``tests/**/*.py`` matches ``/repo/tests/a/b.py``."""
    rx = _GLOB_RX.get(glob)
    if rx is None:
        out, i = [], 0
        while i < len(glob):
            if glob.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif glob.startswith("**", i):
                out.append(".*")
                i += 2
            elif glob[i] == "*":
                out.append("[^/]*")
                i += 1
            elif glob[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(glob[i]))
                i += 1
        rx = re.compile("(?:^|/)" + "".join(out) + r"\Z")
        _GLOB_RX[glob] = rx
    return rx


_BROAD: Dict[str, bool] = {}
_BENIGN_VARIANTS: List[str] = []


def broad_trigger(trigger: str) -> bool:
    """True when ``trigger`` fires on one of the everyday commands of
    ``memory_fields.BENIGN_COMMANDS``, is flags-only on an everyday
    name, or is one of those once its flags are removed (`git status --short`):
    such a trigger fires on most calls and tells the model nothing about this
    one. Memoized."""
    if trigger not in _BROAD:
        if not _BENIGN_VARIANTS:
            gt = _gt()
            for b in _mf().BENIGN_COMMANDS:
                for s in gt.segments(b):
                    _BENIGN_VARIANTS.extend(gt.variants(s))
        pre = trigger + " "
        core = " ".join(w for w in trigger.split() if not (w.startswith("-") or w.isdigit()))
        _BROAD[trigger] = (
            any(v == trigger or v.startswith(pre) for v in _BENIGN_VARIANTS)
            or flags_only_trigger(trigger)
            or (core != trigger and core != "" and broad_trigger(core))
        )
    return _BROAD[trigger]


# Shell keywords, and commands that read or print (or wrap another command). A
# trigger that is one of these, alone or with flags only (`grep -v`, `tail -n`,
# `git -C`, `then`), fires on most calls, like the everyday commands above.
SHELL_KEYWORDS = frozenset(
    "if then else elif fi for while until do done case esac in function".split()
)
EVERYDAY_NAMES = frozenset(
    (
        "ls cat head tail sed grep egrep rg find wc echo printf awk sort uniq cut tr xargs tee diff date "
        "sleep ps pgrep stat du df free uptime jq python python3 pytest git gh docker journalctl systemctl "
        "curl ssh env bash sh cd mkdir cp mv rm ln chmod test true timeout nohup setsid which readlink "
        "realpath basename dirname file less sudo kill"
    ).split()
)


def flags_only_trigger(trigger: str) -> bool:
    """True for a shell keyword, or an everyday command name followed only by
    flags and numbers (`grep -rn`, `tail -1`, `git -C`, `journalctl -u`)."""
    words = trigger.split()
    if not words:
        return False
    if len(words) == 1 and words[0] in SHELL_KEYWORDS:
        return True
    return words[0] in EVERYDAY_NAMES and all(w.startswith("-") or w.isdigit() for w in words[1:])


def _glob_weight(glob: str) -> int:
    """Literal characters of a glob: ``tools/x.sh`` is more specific than ``**/*.sh``."""
    return len(re.sub(r"[*?]", "", glob))


def command_rows(
    command: str, table: Mapping[str, Any]
) -> List[Tuple[int, str, Dict[str, object]]]:
    """``[(weight, trigger, entry)]`` for every entry with a plain trigger that
    a segment of ``command`` fires. One per entry, its longest trigger. The
    segments are those of ``guard_table.command_segments``: quoted
    data and comments are masked, executed strings are their own lines."""
    gt = _gt()
    variants: List[str] = []
    for s in gt.command_segments(command):  # quoted data and comments masked (F1)
        for v in gt.variants(s):
            if v not in variants:
                variants.append(v)
    if not variants:
        return []
    out = []
    for e in table.get("entries") or []:
        if not isinstance(e, dict):
            continue
        best = ""
        for t in e.get("triggers") or []:
            if not isinstance(t, str) or not t or t.startswith(("glob:", "tool:", "phrase:")):
                continue
            if len(t) <= len(best):
                continue
            pre = t + " "
            if any(v == t or v.startswith(pre) for v in variants) and not broad_trigger(t):
                best = t
        if best:
            out.append((len(best), best, e))
    return out


def path_rows(path: str, table: Mapping[str, Any]) -> List[Tuple[int, str, Dict[str, object]]]:
    """``[(weight, trigger, entry)]`` for every entry with a ``glob:`` trigger
    that matches ``path``. One per entry, its most specific glob."""
    out = []
    for e in table.get("entries") or []:
        if not isinstance(e, dict):
            continue
        best, weight = "", -1
        for t in e.get("triggers") or []:
            if not isinstance(t, str) or not t.startswith("glob:"):
                continue
            g = t[5:]
            if g and glob_regex(g).search(path) and _glob_weight(g) > weight:
                best, weight = t, _glob_weight(g)
        if best:
            out.append((weight, best, e))
    return out


# --------------------------------------------------------------------------
# weak triggers (WI-3g)
# --------------------------------------------------------------------------
# Measured 2026-09-30 (P/wi3g/RESULT-WI3G.md): 27% of the shown rows fit the
# call. The main cause is a generic trigger: ``ssh example-host``, ``gh api``,
# ``git rev-parse`` or ``docker exec`` is a trigger of 6 to 29 memories, so it
# fired on most calls of that command and showed the first three memories by
# name, whatever the call did. A one-word trigger (``sha256sum``, ``tar``)
# names a program that a memory only mentions. Such a match is WEAK: its row
# needs evidence, words of the call that are also in the memory's rule or
# apply (``row_evidence``). With evidence of ``WEAK_FULL_SCORE`` or more the
# row shows as before; with less, only its Rule line; with none, no row.
#
# WI-3h: a few holders never make a trigger strong. Until WI-3h a trigger of
# 4 or more memories was weak and one of 1 to 3 was strong, so a trigger
# removal (the WI-3g data fix) made the remaining holders of ``ssh example-host``
# or ``git rev-parse`` strong: they showed in full with no evidence, and 25 of
# 25 such new rows in the judged sample were false. Now a command trigger is
# strong only when it is specific (``specific_trigger``: an argument beyond
# the program and its subcommand that is not a flag, a number or a default
# name, ``GENERIC_ARGS``), or when the memory's rule line names
# its argument (``rule_names_trigger``: ``ssh example-host`` for "use
# example-host-1 instead of the bare name example-host"). A ``glob:`` trigger is strong only when a
# path part has no wildcard (``tools/*.py``, not ``**/*.py``). A trigger of
# ``WEAK_SHARE`` memories or more stays weak even when it is specific
# (``python3 tools/worktree_guard.py remove`` of 5 memories): the count can
# only make a trigger weak, never strong.
WEAK_SHARE = 4  # a trigger of this many memories or more is weak (0: no grading)
WEAK_FULL_SCORE = 4.0  # evidence for a weak row to show in full
WEAK_NOTE = "weak match on `{}`: the rule only; open the file if it applies to this call"
# Words that are evidence of nothing: shell words, common paths, and words
# that most rules use. Added to the STOPWORDS of the shared tokenizer. The
# word list is ``hook_config.EVIDENCE_STOP_DEFAULT`` plus the config key
# ``guard.evidence_stop_extra``.
EVIDENCE_STOP = frozenset(set(EVERYDAY_NAMES) | set(SHELL_KEYWORDS) | set(_cfg().evidence_stop()))
_HEX = re.compile(r"[0-9a-f]{6,}")
_SHARE: Dict[int, Tuple[object, Dict[str, int]]] = {}
_DF: Dict[int, Tuple[object, int, Dict[str, int]]] = {}


def trigger_share(table: Mapping[str, Any]) -> Dict[str, int]:
    """``{trigger: number of entries that hold it}`` for the plain and
    ``glob:`` triggers of ``table``. Memoized per table object."""
    got = _SHARE.get(id(table))
    if got is not None and got[0] is table:
        return got[1]
    share: Dict[str, int] = {}
    for e in table.get("entries") or []:
        if isinstance(e, dict):
            for t in set(x for x in e.get("triggers") or [] if isinstance(x, str)):
                share[t] = share.get(t, 0) + 1
    _SHARE[id(table)] = (table, share)
    return share


# WI-3h. Arguments that name a default host, remote or branch: they are in
# most calls of their command, so they do not make a trigger specific. The
# list is ``hook_config.GENERIC_ARGS_DEFAULT`` plus the config key
# ``guard.generic_args_extra`` (add your own host names there).
GENERIC_ARGS = _cfg().generic_args()
_WRAPPERS = frozenset("sudo env timeout nohup setsid time exec".split())
_INTERPRETERS = frozenset("python python3 bash sh node uv npx".split())
_NO_SUBCOMMAND = frozenset("ssh scp rsync".split())  # the second word is a host
_NESTED = {
    "git": frozenset("worktree stash remote submodule notes lfs".split()),
    "docker": frozenset(
        "image compose network volume container buildx system builder context".split()
    ),
}


def _unwrap(words: List[str]) -> List[str]:
    while words and words[0] in _WRAPPERS:
        words = words[1:]
    return words


def trigger_args(trigger: str) -> List[str]:
    """The words of a command trigger after its program and subcommand:
    ``git fetch github main`` -> ``[github, main]``, ``gh pr create`` -> ``[]``,
    ``ssh example-host`` -> ``[example-host]``. A wrapper (``sudo``) is skipped, and the
    script of an interpreter is an argument (``python3 tools/x.py``)."""
    w = _unwrap(trigger.split())
    if not w:
        return []
    if w[0] in _INTERPRETERS and len(w) > 1 and not w[1].startswith("-"):
        return w[1:]
    prog, rest = w[0], w[1:]
    if rest and prog not in _NO_SUBCOMMAND and not rest[0].startswith("-") and "/" not in rest[0]:
        sub, rest = rest[0], rest[1:]
        if (
            (prog == "gh" or sub in _NESTED.get(prog, ()))
            and rest
            and not rest[0].startswith("-")
            and "/" not in rest[0]
        ):
            rest = rest[1:]
    return rest


def specific_trigger(trigger: str) -> bool:
    """True when a command trigger has an argument that narrows the situation:
    a word after the program and subcommand that is not a flag, a number or
    a ``GENERIC_ARGS`` name (``docker exec app-db``, ``python3
    tools/x.py``; not ``git rev-parse --short``, ``git push origin main``). A flag alone
    does not count: in the judged samples ``git rev-parse --short`` and
    ``git merge-base --is-ancestor`` fit 0 of 13 calls."""
    return any(
        not a.startswith("-") and not a.isdigit() and a.lower() not in GENERIC_ARGS
        for a in trigger_args(trigger)
    )


def specific_glob(glob: str) -> bool:
    """True when a part of the glob's path has no wildcard (``tools/*.py``,
    ``**/Dockerfile``); ``**/*.py`` fires on every Python file."""
    return any(part and part != "**" and not re.search(r"[*?\[]", part) for part in glob.split("/"))


def _named_words(trigger: str) -> List[str]:
    w = _unwrap([x for x in trigger.split() if not x.startswith("-")])
    return w[1:] if len(w) > 1 else w


def rule_names_trigger(trigger: str, e: Mapping[str, object]) -> bool:
    """True when every word of a command trigger after its program (its
    subcommand and arguments; the program itself for a one-word trigger) is
    in the memory's rule line: the rule is about that command. In the judged
    samples 54 of 76 such rows fit the call (0.71), against 44 of 226 (0.19)
    for the other rows of a weak trigger."""
    if trigger.startswith(("glob:", "tool:", "phrase:")):
        return False
    words = set(_mt().tokens(" ".join(_named_words(trigger))))
    return bool(words) and words <= set(_mt().tokens(str(e.get("rule") or "")))


def weak_trigger(
    trigger: str,
    e: Optional[Mapping[str, object]] = None,
    share: int = 0,
    weak_share: int = WEAK_SHARE,
) -> bool:
    """True when a match on ``trigger`` needs evidence in the call. A trigger
    held by ``weak_share`` entries or more (``share``) is weak. Else a ``glob:``
    trigger is weak unless ``specific_glob``, and a command trigger is weak
    unless ``specific_trigger`` or its argument is named by the rule line of
    ``e`` (``rule_names_trigger``). A one-word trigger (a program alone) is
    always weak. A small ``share`` never makes a trigger strong (WI-3h).
    ``weak_share`` 0 turns the grading off (the rows before WI-3g)."""
    if weak_share <= 0:
        return False
    if share >= weak_share:
        return True
    if trigger.startswith("glob:"):
        return not specific_glob(trigger[5:])
    if len(trigger.split()) == 1:
        return True
    if specific_trigger(trigger):
        return False
    has_arg = any(not a.startswith("-") for a in trigger_args(trigger))
    return not (has_arg and e is not None and rule_names_trigger(trigger, e))


def _entry_words(e: Mapping[str, object]) -> set:
    return set(_mt().tokens(f"{e.get('rule') or ''} {e.get('apply') or ''}"))


def _doc_freq(table: Mapping[str, Any]) -> Tuple[int, Dict[str, int]]:
    """``(entries, {word: entries whose rule or apply holds it})``. Memoized."""
    got = _DF.get(id(table))
    if got is not None and got[0] is table:
        return got[1], got[2]
    df: Dict[str, int] = {}
    n = 0
    for e in table.get("entries") or []:
        if isinstance(e, dict):
            n += 1
            for w in _entry_words(e):
                df[w] = df.get(w, 0) + 1
    _DF[id(table)] = (table, n, df)
    return n, df


def row_evidence(
    query: str, trigger: str, e: Mapping[str, object], table: Mapping[str, Any]
) -> Tuple[float, List[str]]:
    """``(score, words)``: the words of ``query`` (the command, or the file
    path) that are also in the rule or the apply of ``e``, less the trigger's
    own words, ``EVIDENCE_STOP``, numbers and hex ids. The score is the sum of
    their rarity over the table, ``log(entries / (1 + entries with the word))``,
    so a word that few rules use weighs more."""
    mt = _mt()
    t = trigger[5:] if trigger.startswith("glob:") else trigger
    own = set(mt.tokens(t))
    q = {
        w
        for w in mt.tokens(query)
        if not w.isdigit() and not _HEX.fullmatch(w) and w not in EVIDENCE_STOP and w not in own
    }
    words = sorted(q & _entry_words(e))
    if not words:
        return 0.0, []
    n, df = _doc_freq(table)
    return round(sum(math.log(max(n, 1) / (1 + df.get(w, 0))) for w in words), 2), words


def named_evidence(trigger: str, e: Mapping[str, object], table: Mapping[str, Any]) -> float:
    """Evidence of a weak row whose rule line names its trigger: the rarity
    of the trigger, ``log(entries / (1 + entries with it))``, like an
    evidence word, kept under ``WEAK_FULL_SCORE`` (alone it gives the Rule
    line only). 0 when the rule does not name it."""
    if not rule_names_trigger(trigger, e):
        return 0.0
    n = sum(1 for x in table.get("entries") or [] if isinstance(x, dict))
    held = trigger_share(table).get(trigger, 0)
    return (
        round(min(math.log(max(n, 1) / (1 + held)), WEAK_FULL_SCORE - 0.01), 2)
        if n > held + 1
        else 0.0
    )


def grade_rows(
    cands: List[Tuple[int, str, Dict[str, object]]],
    query: str,
    table: Mapping[str, Any],
    weak_share: int = WEAK_SHARE,
    full_score: float = WEAK_FULL_SCORE,
) -> Tuple[List[Tuple[float, str, Dict[str, object]]], Dict[str, str]]:
    """``(cands, rule_only)``: a strong candidate keeps its weight; a weak one
    without evidence is dropped; a weak one with evidence comes after every
    strong one (weight ``-1``, then ``-2`` for a Rule-line-only row, and by
    score within each), and ``rule_only`` maps the id of a weak row under
    ``full_score`` to its trigger. ``pick_rows`` sorts by weight."""
    share = trigger_share(table)
    out: List[Tuple[float, str, Dict[str, object]]] = []
    rule_only: Dict[str, str] = {}
    for w, t, e in cands:
        if not weak_trigger(t, e, share.get(t, 0), weak_share):
            out.append((w, t, e))
            continue
        score, _words = row_evidence(query, t, e, table)
        score += named_evidence(t, e, table)
        if score <= 0:
            continue
        if score >= full_score:
            out.append((-1 + score / 1000.0, t, e))
        else:
            out.append((-2 + score / 1000.0, t, e))
            rule_only[str(e.get("id", ""))] = t
    return out, rule_only


def _short(text: object, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def legacy_row(e: Mapping[str, object]) -> str:
    """The row before WI-3f (a cut rule and apply, then a ``[memory <id>]``
    tag). Used only when the shared formatter cannot be loaded."""
    row = f"- {_short(e.get('rule', ''), 160)}"
    if e.get("apply"):
        row += f" Apply: {_short(e.get('apply', ''), ROW_APPLY_CHARS)}"
    return row + f" [memory {e.get('id', '')}]"


_SAFE_ID = re.compile(r"^[\w.-]{1,200}$")


def row_memory(e: Mapping[str, object], table: Mapping[str, Any]) -> Tuple[Dict[str, object], bool]:
    """``(memory, body_known)`` for a row: rule and apply from the table
    entry, the description and the body from ``<table source>/<id>.md``
    (the folder the table was built from, never a guess). An id that is not a
    plain file name, or a file that cannot be read, gives no body."""
    mem: Dict[str, Any] = {
        "id": str(e.get("id", "")),
        "rule": str(e.get("rule") or ""),
        "apply": str(e.get("apply") or ""),
        "description": "",
        "body": "",
    }
    src = str(table.get("source") or "")
    if not src or not _SAFE_ID.match(mem["id"]) or mem["id"].startswith("."):
        return mem, False
    try:
        m = _mt().read_memory(Path(src) / f"{mem['id']}.md", _mf)
    except Exception:  # noqa: BLE001 - no body, rule and apply still show
        return mem, False
    mem["description"], mem["body"] = m.get("description", ""), m.get("body", "")
    return mem, True


def legacy_text(rows: Sequence[Mapping[str, object]]) -> str:
    """The rows text before WI-3f. Its length is what the session budget
    ``ROWS_SESSION_CHARS`` is charged, so the full text shows the same rows
    in the same calls as before."""
    return "\n".join(["Memory rules for this action (guard rows):"] + [legacy_row(e) for e in rows])


def weak_row(mem: Mapping[str, object], trigger: str) -> str:
    """The row of a weak match with little evidence (WI-3g): the id head with
    ``WEAK_NOTE``, then the Rule line only."""
    mt = _mt()
    ident = re.sub(r"[^\w.-]", "", str(mem.get("id", "")))[:120]
    rule = mt.inert(mem.get("rule") or mem.get("description") or "")
    trig = mt.inert(trigger).replace("`", "")[:80]
    return (
        f"- Memory {ident} ({WEAK_NOTE.format(trig)}):\n"
        f"  Rule: {mt.cut_at_sentence(rule, mt.FIELD_CHARS)[0]}"
    )


def render_rows(
    rows: Sequence[Mapping[str, object]],
    table: Mapping[str, Any],
    query: str,
    subject: str,
    state: SessionState,
    max_chars: int = ROWS_MAX_CHARS,
    all_compact: bool = False,
    rule_only: Optional[Mapping[str, str]] = None,
) -> str:
    """The header and the rows (WI-3f): each row in the shared shape
    (``memory_text.render_hit``), under ``ROWS_MAX_CHARS``. A
    memory already shown in this agent of the session shows Rule and Apply
    only. A row in ``rule_only`` (WI-3g: id to its weak trigger) shows its
    Rule line only (``weak_row``). The caller drops a row that still breaks
    the bound. When the shared formatter cannot be loaded, the legacy rows."""
    try:
        mt = _mt()
    except Exception:  # noqa: BLE001
        return "\n".join([ROWS_HEADER] + [legacy_row(e) for e in rows])
    mems, known = [], {}
    for e in rows:
        m, k = row_memory(e, table)
        mems.append(m)
        known[m["id"]] = k

    weak = dict(rule_only or {})

    def one(
        mem: Mapping[str, object], q: str, compact: bool = False, text_chars: int = mt.TEXT_CHARS
    ) -> str:
        if str(mem["id"]) in weak:
            return weak_row(mem, weak[str(mem["id"])])
        seen = state.full.get(str(mem["id"]), 0) > 0
        return mt.render_hit(
            mem,
            q,
            compact or seen or all_compact,
            text_chars,
            subject=subject,
            compact_needs_apply=False,
            body_known=known[str(mem["id"])],
            head_fallback=True,
            compact_note=SEEN_NOTE if seen else None,
        )

    return mt.render(mems, query, header=ROWS_HEADER, max_chars=max_chars, render_one=one)


def pick_rows(
    cands: Sequence[Tuple[float, str, Dict[str, object]]], state: SessionState
) -> List[Dict[str, object]]:
    """At most ``MAX_ROWS`` entries, most specific first, each shown fewer than
    ``ROW_REPEAT`` times this session. Entries without a rule are not shown."""
    cands = sorted(cands, key=lambda c: (-c[0], str(c[2].get("id", ""))))
    out = []
    for _w, _t, e in cands:
        mid = str(e.get("id", ""))
        if not e.get("rule") or state.rows.get(mid, 0) >= ROW_REPEAT:
            continue
        out.append(e)
        if len(out) >= MAX_ROWS:
            break
    return out


# --------------------------------------------------------------------------
# the override marker (WI-3c)
# --------------------------------------------------------------------------
MARKER = "# guard-ok: <short reason>"
REASON_CHARS = 120
_MARKER_SEEN = re.compile(r"#[ \t]*guard-ok\b")
_MARKER_OK = re.compile(r"#[ \t]*guard-ok:[ \t]*(.*)")
_WORD_START = " \t\n;&|()"


def _top_comments(s: str) -> List[str]:
    """The comments of the command line ``s`` itself (heredoc bodies already
    removed): a ``#`` at a word start, outside quotes, outside ``$( )``,
    ``<( )``, ``>( )`` and backticks, up to the end of its line."""
    out: List[str] = []
    n = len(s)

    def skip_dq(i: int) -> int:  # i: after the opening quote
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
            elif c == '"':
                return i + 1
            elif s.startswith("$(", i):
                i = scan(i + 2, ")", False)
            elif c == "`":
                i = scan(i + 1, "`", False)
            else:
                i += 1
        return n

    def scan(i: int, stop: str, top: bool) -> int:
        depth = 0
        while i < n:
            c = s[i]
            if stop == ")" and c == ")" and depth == 0:
                return i + 1
            if stop == "`" and c == "`":
                return i + 1
            if c == "\\":
                i += 2
            elif s.startswith("$'", i):  # ANSI-C string: \' does not end it
                i += 2
                while i < n and s[i] != "'":
                    i += 2 if s[i] == "\\" else 1
                i += 1
            elif c == "'":
                j = s.find("'", i + 1)
                i = n if j < 0 else j + 1
            elif c == '"':
                i = skip_dq(i + 1)
            elif c in "$<>" and s.startswith("(", i + 1):
                i = scan(i + 2, ")", False)
            elif c == "`":
                i = scan(i + 1, "`", False)
            elif c == "#" and (i == 0 or s[i - 1] in _WORD_START):
                j = s.find("\n", i)
                j = n if j < 0 else j
                if top:
                    out.append(s[i:j])
                i = j
            else:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                i += 1
        return n

    scan(0, "", True)
    return out


def override_marker(command: str) -> Tuple[Optional[str], bool]:
    """``(reason, seen)`` for the override marker of ``command``.

    Grammar: a comment of the command line itself (see ``_top_comments``)
    that reads ``#``, optional blanks, ``guard-ok:``, optional blanks, then
    the reason up to the end of the line. The reason is valid when it holds a
    letter or a digit. With several markers the last one counts. ``seen`` is
    True when a top-level comment starts with ``# guard-ok`` at all, so a
    marker without a valid reason can be named in the deny message. A
    ``# guard-ok`` in quotes, a heredoc body, an executed string or ``$( )`` is
    data, not a marker: ``(None, False)``."""
    body = _gt().without_heredoc_bodies(command)
    if "guard-ok" not in body:
        return None, False
    reason, seen = None, False
    for c in _top_comments(body):
        if not _MARKER_SEEN.match(c):
            continue
        seen = True
        m = _MARKER_OK.match(c)
        r = m.group(1).strip() if m else ""
        if re.search(r"[A-Za-z0-9]", r):
            reason = r
    return reason, seen


# name=value / name: value for a secret-like name; "bearer <value>"; known
# token prefixes; and a run of 24+ letters AND digits (hex, most key bodies).
# A memory id (short words joined by "_") and a path are kept.
_SECRET_RX = [
    re.compile(r"(?i)\b(pass(?:word|wd)?|token|secret|api[_-]?key|key|auth|bearer)(\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(bearer)(\s+)\S+"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|glpat-|xox[abprs]-|sk-|AKIA)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])\b[A-Za-z0-9]{24,}"),
]


def redact(text: str) -> str:
    """``text`` with secret-like values replaced by ``[redacted]``."""
    text = str(text or "")
    for rx in _SECRET_RX[:2]:
        text = rx.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    for rx in _SECRET_RX[2:]:
        text = rx.sub("[redacted]", text)
    return text


def redact_reason(text: str) -> str:
    """The override reason for the log: blanks folded, secret-like values
    replaced by ``[redacted]``, cut to ``REASON_CHARS``."""
    return _short(redact(" ".join(str(text or "").split())), REASON_CHARS)


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------
def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name) or default)
    except ValueError:
        return default


OVERRIDE_LINE = (
    "Override only if you are sure this rule does not apply here: end the command "
    f"with this shell comment: {MARKER}"
)
BAD_MARKER_LINE = "Your # guard-ok comment has no reason: write the reason after the colon."
REFUSED_LINE = (
    "Your # guard-ok override is refused: no run of this rule's own command has "
    "failed in this session."
)
RUN_FIRST_LINE = "Run this command first: {}"
FAILS_FIRST_LINE = (
    "Override only if that command fails in this session: then end the command "
    f"with this shell comment: {MARKER}"
)
# A Bash call that ran and exited non-zero: Claude Code's error text for a
# shell error is "Exit code <n>" + stderr + stdout (the CLI's ShellError).
APPLY_FAILED = re.compile(r"^\s*(?:Error:\s*)?Exit code [1-9][0-9]*\b")
ERROR_CHARS = 120


def deny_reason(
    first: Mapping[str, object],
    others: Sequence[Mapping[str, object]],
    count: int = 1,
    bad_marker: bool = False,
    run_first: Optional[List[str]] = None,
    refused: bool = False,
) -> str:
    """``count`` is this deny's number for ``first``'s memory in this agent of
    the session. From ``ESCALATE_AFTER + 1`` the reason starts with it.
    ``run_first`` holds the commands of the hit memories whose override still
    needs a failed run (WI-3d); then those commands replace the plain override
    line. ``refused``: the command carried a valid marker that was refused."""
    lines = []
    if count > ESCALATE_AFTER:
        lines.append(
            f"Denied {count} times in this session by this rule. A retry is denied "
            "every time: change the command, or use the override below."
        )
    lines.append(
        f"{first.get('rule', '')}\nApply: {first.get('apply', '')}\nMemory: {first.get('id', '')}"
    )
    if others:
        lines.append(
            "Also matches: "
            + "; ".join(f"{o.get('id', '')} ({_short(o.get('rule', ''), 120)})" for o in others)
        )
    if bad_marker:
        lines.append(BAD_MARKER_LINE)
    if refused:
        lines.append(REFUSED_LINE)
    if run_first:
        lines += [RUN_FIRST_LINE.format(c) for c in run_first]
        lines.append(FAILS_FIRST_LINE)
    else:
        lines.append(OVERRIDE_LINE)
    return "\n".join(lines)


def _out(context: str = "", deny: str = "") -> str:
    hso: Dict[str, str] = {"hookEventName": "PreToolUse"}
    if deny:
        hso["permissionDecision"] = "deny"
        hso["permissionDecisionReason"] = deny
    if context:
        hso["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": hso}, ensure_ascii=False)


class Decision:
    """What one call decided. ``out`` is the text to print. The session state
    stays locked until ``commit`` (charge the counts, save, unlock) or
    ``abort`` (unlock, charge nothing), so parallel calls cannot overspend."""

    def __init__(
        self,
        out: str = "",
        state: Optional[SessionState] = None,
        charge=None,
        log: Optional[Tuple[str, List[str]]] = None,
        extra: Optional[Dict[str, object]] = None,
        log_text: Optional[str] = None,
    ):
        self.out, self.state, self._charge, self.log = out, state, charge, log
        self.extra: Dict[str, object] = dict(extra or {})
        self.log_text = log_text  # None: the command or the file path

    def commit(self) -> None:
        if self.state is None:
            return
        st, self.state = self.state, None
        try:
            if self._charge is not None:
                self.extra = self._charge(st) or {}
        except BaseException:
            st.close(save=False)
            raise
        st.close(save=True)

    def abort(self) -> None:
        if self.state is not None:
            st, self.state = self.state, None
            st.close(save=False)


CRED_TOOLS = ("Bash", "Read", "Grep")
_CRED_BASH_HINT = re.compile(
    r"git|config|credential|\brg\b|\bfind\b|\bxargs\b"
    r"|\bgrep\b[^|;&\n]*\s(?:-[A-Za-z]*[rR]|--recursive|--dereference-recursive|-d\s*recurse)"
)


def _credential_check(
    tool: str, ti: Mapping[str, Any], cwd: str, env: Mapping[str, str]
) -> Optional[Decision]:
    """Deny a Bash, Read or Grep call that would put a git URL
    holding a credential into the model context (credential_guard).
    Runs before the table is read, so it works with no table. A cheap text
    test comes first, so most calls never load the module. Off with
    NOBLIVION_GUARD_CREDENTIAL=0. Any error allows; the log text has the URL
    userinfo removed."""
    if tool not in CRED_TOOLS:
        return None
    if tool == "Bash":
        cmd = ti.get("command")
        if not isinstance(cmd, str) or not _CRED_BASH_HINT.search(cmd):
            return None
    elif tool == "Read":
        fp = str(ti.get("file_path") or "")
        if os.path.basename(fp) not in (
            "config",
            "config.worktree",
            ".git-credentials",
            "credentials",
            ".gitconfig",
            "gitconfig",
        ) and not os.path.islink(os.path.join(cwd, fp)):
            return None  # a link is checked by its target
    elif str(ti.get("output_mode") or "files_with_matches") != "content":
        return None
    try:
        cg = _load("credential_guard")
        if not cg.guard_on(env):
            return None
        strip = _gt().without_heredoc_bodies if tool == "Bash" else None
        got = cg.decide(tool, ti, cwd, strip=strip)
    except Exception:  # noqa: BLE001 - the guard fails open
        return None
    if got is None:
        return None
    text = ti.get("command") if tool == "Bash" else (ti.get("file_path") or ti.get("path") or "")
    return Decision(
        _out(deny=got[0]),
        None,
        None,
        ("credential-deny", ["credential-url"]),
        extra={"what": got[1]},
        log_text=cg.scrub(redact(str(text or ""))),
    )


def _table_problem(env: Mapping[str, str], sid: object, agent: str, error: str) -> Decision:
    """No table, so no guard: allow, and log one ``error`` line per agent of a
    session (review finding F5), so a missing table leaves a trace."""
    state = SessionState(env, sid, agent)
    if state.notes.get("table_error"):
        state.close(save=False)
        return Decision()

    def note(st: SessionState) -> Dict[str, object]:
        st.notes["table_error"] = 1
        return {"error": error[:300]}

    return Decision("", state, note, ("error", []))


def decide(event: Mapping[str, object], env: Mapping[str, str]) -> Decision:
    """The decision for one PreToolUse event. Nothing is charged or logged
    here: ``main`` prints ``out`` first, then commits, then logs."""
    if not isinstance(event, dict):
        return Decision()
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input")
    if tool not in BASH_TOOLS + FILE_TOOLS + READ_TOOLS or not isinstance(ti, dict):
        return Decision()
    kind = str(event.get("hook_event_name") or "PreToolUse")
    if kind == "PostToolUseFailure":
        return _record_failure(event, env)
    if kind != "PreToolUse":
        return Decision()
    cwd = str(event.get("cwd") or os.getcwd())
    cred = _credential_check(tool, ti, cwd, env)
    if cred is not None:
        return cred
    file_q: Optional[Tuple[str, str]] = None
    if tool not in BASH_TOOLS and _file_tools_on(env):
        file_q = _file_query(tool, ti, cwd)
    if tool in READ_TOOLS and file_q is None:
        return Decision()  # the file-tool leg is off: nothing is read
    sid = event.get("session_id")
    agent = agent_key(event)
    tpath = table_file(env)
    table = _gt().load_table(tpath) if tpath.is_file() else {}
    if not table.get("entries"):
        why = "table empty or broken" if tpath.is_file() else "table missing"
        return _table_problem(env, sid, agent, f"{why}: {tpath}")

    if tool in BASH_TOOLS:
        command = ti.get("command")
        if not isinstance(command, str) or not command.strip():
            return Decision()
        hits = _gt().match(command, table)
        marker, marker_seen = override_marker(command) if hits else (None, False)
        cands = command_rows(command, table)
        query, subject = command, "command"
        label_q, label_s = query, subject
    elif tool in READ_TOOLS and file_q is not None:
        hits, cands = [], []  # label rows only (the file-tool leg)
        query, subject = file_q[1], "read"
        label_q, label_s = file_q[0], subject
    else:
        raw = ti.get("file_path")
        if not isinstance(raw, str) or not raw:
            return Decision()
        p = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
        hits = []  # file edits are never denied (see the module text)
        cands = path_rows(os.path.normpath(p), table)
        query, subject = os.path.normpath(p), "file"
        label_q, label_s = (file_q[0] if file_q is not None else query), subject
    # Label rows only where no rule denies (a label never denies).
    labs = (
        _label_candidates(label_q, label_s, table, env)
        if not hits and table.get("label_index")
        else []
    )
    if not hits and not cands and not labs:
        return Decision()
    rule_only: Dict[str, str] = {}
    if not hits:
        # WI-3g: a weak trigger needs evidence in the call (``grade_rows``).
        # Denies never pass here. An error keeps the rows before WI-3g.
        try:
            cands, rule_only = grade_rows(
                cands, query, table, _int_env(env, "NOBLIVION_GUARD_WEAK_SHARE", WEAK_SHARE)
            )
        except Exception:  # noqa: BLE001
            rule_only = {}
        if not cands and not labs:
            return Decision()

    state = SessionState(env, sid, agent)
    try:
        if hits:
            # Every hit is denied, at any count (WI-3c: the old budget let a
            # stubborn retry through). The rule denied least often leads, so a
            # new rule is read first; the others are listed after it.
            ids = [str(h["id"]) for h in hits]
            # WI-3d: a memory with a compliant form (complies + run_first)
            # accepts the override only after a run of that form failed in
            # this agent of the session (recorded on PostToolUseFailure).
            need = [
                h
                for h in hits
                if h.get("complies") and h.get("run_first") and not state.failed.get(str(h["id"]))
            ]
            if marker is not None and not need:
                evidence = {
                    i: (f"failed:{state.failed[i]}" if state.failed.get(i) else "no-complies")
                    for i in ids
                }
                state.close(save=False)  # an override charges nothing
                return Decision(
                    log=("override", ids),
                    extra={"reason": redact_reason(marker), "evidence": evidence},
                    log_text=redact(command),
                )
            shown = sorted(hits, key=lambda h: state.denies.get(str(h["id"]), 0))
            count = state.denies.get(str(shown[0]["id"]), 0) + 1
            run_first = list(dict.fromkeys(str(h["run_first"]) for h in need))
            refused = marker is not None
            extra: Dict[str, object] = {}
            if refused:
                extra = {
                    "override_refused": [str(h["id"]) for h in need],
                    "reason": redact_reason(marker or ""),
                }

            def charge_deny(st: SessionState) -> Dict[str, object]:
                for i in ids:
                    st.denies[i] = st.denies.get(i, 0) + 1
                return dict({"counts": {i: st.denies.get(i, 0) for i in ids}}, **extra)

            bad_marker = marker_seen and marker is None
            return Decision(
                _out(deny=deny_reason(shown[0], shown[1:], count, bad_marker, run_first, refused)),
                state,
                charge_deny,
                ("deny", ids),
            )
        rows = pick_rows(cands, state)
        budget = _int_env(env, "NOBLIVION_GUARD_ROWS_SESSION_CHARS", ROWS_SESSION_CHARS)
        # Which rows show, and in which calls: the old rule, charged at the old
        # row size, so the full text changes what a row says, not which rows.
        while rows and state.row_chars + len(legacy_text(rows)) > budget:
            rows = rows[:-1]
        if not rows:
            lab = _label_pick(
                labs, state, env, sid, agent, table, query, subject, [], ROWS_MAX_CHARS
            )
            if lab is None:
                state.close(save=False)
                return Decision()
            return Decision(
                _out(context=lab.text),
                state,
                lab.charge,
                ("labels", lab.ids),
                log_text=redact(query) if subject == "read" else None,
            )
        # What a row says (WI-3f): the full text while this agent of the
        # session has shown under ROWS_FULL_SESSION_CHARS of row text, then
        # Rule and Apply only. One show stays under ROWS_MAX_CHARS.
        full_left = _int_env(
            env, "NOBLIVION_GUARD_ROWS_FULL_SESSION_CHARS", ROWS_FULL_SESSION_CHARS
        ) - int(state.notes.get("full_chars", 0))
        bound = max(0, min(ROWS_MAX_CHARS, full_left))
        text_out = render_rows(rows, table, query, subject, state, bound, rule_only=rule_only)
        if len(text_out) > bound:
            text_out = render_rows(
                rows,
                table,
                query,
                subject,
                state,
                ROWS_MAX_CHARS,
                all_compact=True,
                rule_only=rule_only,
            )
        while len(rows) > 1 and len(text_out) > ROWS_MAX_CHARS:  # only fields over the schema caps
            rows = rows[:-1]
            text_out = render_rows(
                rows,
                table,
                query,
                subject,
                state,
                ROWS_MAX_CHARS,
                all_compact=True,
                rule_only=rule_only,
            )
        shown_weak = [str(e.get("id", "")) for e in rows if str(e.get("id", "")) in rule_only]
        legacy_chars = len(legacy_text(rows))
        # Label rows fill the free places, after the trigger rows.
        lab = _label_pick(
            labs,
            state,
            env,
            sid,
            agent,
            table,
            query,
            subject,
            [str(e.get("id", "")) for e in rows],
            ROWS_MAX_CHARS - len(text_out) - 1,
        )

        def charge_rows(st: SessionState) -> Optional[Dict[str, object]]:
            st.row_chars += legacy_chars
            st.notes["full_chars"] = int(st.notes.get("full_chars", 0)) + len(text_out)
            for e in rows:
                mid = str(e.get("id", ""))
                st.rows[mid] = st.rows.get(mid, 0) + 1
                if mid not in shown_weak:  # a weak row showed the Rule line only
                    st.full[mid] = st.full.get(mid, 0) + 1
            extra: Dict[str, object] = {"rule_only": shown_weak} if shown_weak else {}
            if lab is not None:
                extra.update(lab.charge(st))
            return extra or None

        context = text_out + ("\n" + lab.text if lab is not None else "")
        return Decision(
            _out(context=context),
            state,
            charge_rows,
            ("rows", [str(e.get("id", "")) for e in rows]),
        )
    except BaseException:
        state.close(save=False)
        raise


def _record_failure(event: Mapping[str, object], env: Mapping[str, str]) -> Decision:
    """PostToolUseFailure (WI-3d): a Bash call that ran and exited non-zero,
    and whose command is a run of a memory's compliant form (``complies``),
    is the evidence that unlocks that memory's override for this agent of the
    session. Prints nothing, never denies. An interrupt, a permission denial
    or any error text that is not "Exit code <n>" is not evidence."""
    if str(event.get("tool_name") or "") not in BASH_TOOLS or event.get("is_interrupt"):
        return Decision()
    ti = event.get("tool_input")
    command = ti.get("command") if isinstance(ti, dict) else None
    error = event.get("error")
    if not isinstance(command, str) or not isinstance(error, str) or not APPLY_FAILED.match(error):
        return Decision()
    tpath = table_file(env)
    table = _gt().load_table(tpath) if tpath.is_file() else {}
    if not table.get("entries"):
        return Decision()  # no table: no guard, nothing to record
    ids = _gt().complies_match(command, table)
    if not ids:
        return Decision()
    state = SessionState(env, event.get("session_id"), agent_key(event))

    def charge_failed(st: SessionState) -> Dict[str, object]:
        for i in ids:
            st.failed[i] = st.failed.get(i, 0) + 1
        return {
            "error": _short(
                redact(error.strip().splitlines()[0] if error.strip() else ""), ERROR_CHARS
            )
        }

    return Decision("", state, charge_failed, ("apply-failed", ids), log_text=redact(command))


def _log_text(event: object) -> str:
    """The command with secret-like values redacted (``redact``), or the
    absolute file path, for the log line."""
    if not isinstance(event, dict) or not isinstance(event.get("tool_input"), dict):
        return ""
    ti = event["tool_input"]
    if isinstance(ti.get("command"), str):
        return redact(ti["command"])
    raw = ti.get("file_path")
    if not isinstance(raw, str) or not raw:
        return ""
    p = raw if os.path.isabs(raw) else os.path.join(str(event.get("cwd") or os.getcwd()), raw)
    return os.path.normpath(p)


def _trust_use(env: Mapping[str, str], sid: object, decision: str, ids: List[str]) -> None:
    """Trust events: guard rows or a deny on rule R are a ``use`` of R
    (trust_events). Only with NOBLIVION_TRUST_EVENTS=1; prints
    nothing. An ordinary error is dropped; the time limit still applies."""
    if (env.get("NOBLIVION_TRUST_EVENTS") or "").strip() != "1" or decision not in ("rows", "deny"):
        return
    try:
        _load("trust_events").record_guard(env, sid, decision, ids)
    except Exception:  # noqa: BLE001, S110 - trust events fail open; never the decision
        pass


def _alarm(_signum, _frame):
    raise _TimeUp()


def _arm(seconds: float) -> bool:
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return True
    except (ValueError, OSError, AttributeError):  # not the main thread
        return False


def _disarm() -> None:
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except BaseException:  # noqa: BLE001, S110 - a hook fails open; an alarm that fires here is spent
        pass


def main(
    stdin=None,
    stdout=None,
    environ: Optional[Mapping[str, str]] = None,
    time_limit: float = TIME_LIMIT_S,
) -> int:
    """Read one event and act in this order: decide, print, charge the budget,
    log (review finding F4). The log is best effort: a log failure never hides
    a printed decision. A failure before the print charges nothing. Exit 0
    always: any error or a run over ``time_limit`` seconds prints nothing
    (the tool call goes on)."""
    env = dict(os.environ if environ is None else environ)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    sid, tool, agent = "", "", "main"
    d: Optional[Decision] = None
    armed = False
    try:
        armed = _arm(time_limit)
        event = json.loads(stdin.read())
        if isinstance(event, dict):
            sid, tool = event.get("session_id") or "", str(event.get("tool_name") or "")
            agent = agent_key(event)
        d = decide(event, env)
        if armed:
            _disarm()
            armed = False
        if d.out:
            stdout.write(d.out + "\n")
            stdout.flush()
        armed = _arm(time_limit)  # charge and log get their own time limit
        d.commit()
        if d.log:
            text = d.log_text if d.log_text is not None else _log_text(event)
            log_line(env, sid, tool, d.log[0], d.log[1], text, agent, **d.extra)
            _trust_use(env, sid, d.log[0], d.log[1])
    except BaseException as exc:  # noqa: BLE001 - a guard fails open
        if armed:
            _disarm()
            armed = False
        if d is not None:
            try:
                d.abort()
            except Exception:  # noqa: BLE001, S110 - a hook fails open
                pass
        log_line(
            env,
            sid,
            tool,
            "error",
            [],
            "",
            agent,
            error=("timeout" if isinstance(exc, _TimeUp) else f"{type(exc).__name__}: {exc}")[:300],
        )
    finally:
        if armed:
            _disarm()
    return 0


if __name__ == "__main__":
    # Exit 0 on every path (review finding F3): Claude Code reads exit 2 from a
    # PreToolUse hook as a block. os._exit skips interpreter shutdown, where a
    # late flush error could still set a non-zero code.
    try:
        main()
    except BaseException:  # noqa: BLE001, S110 - a hook fails open
        pass
    try:
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook fails open
        pass
    os._exit(0)
