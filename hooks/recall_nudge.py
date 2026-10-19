#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PreToolUse: one line of context before a tool call that a past session may have settled (NOBLIVION-123).

The ``noblivion_recall`` tool is called about once in 5,000 tool calls. The prompt hook shows
rows before the model has judged anything, so the model never decides to ask. This hook marks
two moments where a lookup pays: filing or commenting on a ticket, and merging. Before such a
call it adds one line of ``additionalContext``::

    Recall first: call noblivion_recall with query "<topic>", then act.

The line is under 120 characters. It is added only when the main session made no recall call and
got no nudge in its last 10 tool calls, so a burst of ticket comments gets one line, not ten.

Off by default. Two settings, each an environment variable and a config key:

``NOBLIVION_RECALL_NUDGE`` (``recall.nudge``)
    Switch. On: the hook prints the line.
``NOBLIVION_RECALL_NUDGE_SHADOW`` (``recall.nudge_shadow``)
    Switch. On, with the first off: the hook logs what it would do and prints nothing.

Which calls match comes from the configuration, not from this file's author:

``recall.nudge_tools``
    A list of regular expressions, matched with a case-blind search against the tool name of an
    MCP tool. The default list matches the "create a ticket" and "comment on a ticket" tools of
    the common ticket servers (for example ``createJiraIssue``, ``create_issue``,
    ``addCommentToJiraIssue``, ``add_issue_comment``).
``recall.nudge_commands``
    A list of regular expressions matched against each simple command of a Bash call, at its
    start (``^`` is the start of the command, after variable assignments and wrappers such as
    ``env``). Text inside quotes, comments and here-documents is masked first, so ``grep "gh pr
    merge"`` does not match. The default list holds ``gh pr merge`` and ``glab mr merge``.
    A list that is set replaces the default list. A bad expression is skipped.

How the hook knows the last 10 tool calls: it reads the tail of the session transcript
(``transcript_path`` of the hook input) and takes the 10 newest ``tool_use`` blocks before this
one. If the tail does not show 10 calls and does not reach the start of the file, the hook stays
silent. A call from a subagent gets no line (the transcript of the hook input is the parent's).

Log: ``<cache>/nudge.log``, one line per matching call and one per recall call, no query text.
``recall.log`` is not touched, because tools read it as one line per prompt. State:
``<cache>/nudge-state.json``, the id of the last nudged call per session (50 sessions).

Fail open: any error prints nothing and exits 0. Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Pattern, Sequence, Tuple

_HERE = Path(__file__).resolve().parent

SWITCH_ENV, SWITCH_KEY = "NOBLIVION_RECALL_NUDGE", "recall.nudge"
SHADOW_ENV, SHADOW_KEY = "NOBLIVION_RECALL_NUDGE_SHADOW", "recall.nudge_shadow"
TOOLS_KEY, COMMANDS_KEY = "recall.nudge_tools", "recall.nudge_commands"

# Case-blind. A ticket server names the tools in camel case or with underscores.
DEFAULT_TOOLS: Tuple[str, ...] = (
    r"^mcp__.+__create_?(jira_?)?(issue|ticket)$",
    r"^mcp__.+__add_?(issue_?)?comment(_?to_?(jira_?)?issue)?$",
)
DEFAULT_COMMANDS: Tuple[str, ...] = (r"^gh\s+pr\s+merge\b", r"^glab\s+mr\s+merge\b")

WINDOW = 10  # tool calls
RECALL_SUFFIX = "noblivion_recall"
REMEMBER_SUFFIX = "noblivion_remember"
LINE = 'Recall first: call noblivion_recall with query "{topic}", then act.'
TOPIC_MAX = 40
LINE_MAX = 120
PATTERN_MAX_CHARS, PATTERN_MAX_COUNT = 200, 20
COMMAND_MAX_CHARS = 20_000
TAIL_STEPS = (262_144, 2_097_152, 16_777_216)  # bytes of transcript read, widening
STATE_NAME, LOG_NAME = "nudge-state.json", "nudge.log"
STATE_SESSIONS = 50
LOG_MAX_BYTES = 5_000_000

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TOPIC_BAD = re.compile(r"[^A-Za-z0-9 _./#:-]")
_TOPIC_KEYS = ("issueIdOrKey", "issue_key", "issue", "key", "summary", "title")

# ── settings ────────────────────────────────────────────────────────────────

_CFG: Any = None


def _cfg() -> Any:
    """``hook_config`` from this folder, or None when it cannot load (a lone copy)."""
    global _CFG
    if _CFG is None:
        try:
            spec = importlib.util.spec_from_file_location("hook_config", _HERE / "hook_config.py")
            if spec is None or spec.loader is None:
                raise ImportError("hook_config")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _CFG = mod
        except Exception:  # noqa: BLE001
            _CFG = False
    return _CFG or None


def _switch(name: str, key: str, env: Mapping[str, str]) -> bool:
    cfg = _cfg()
    if cfg is None:
        return False
    try:
        return bool(cfg.switch(name, False, env, key))
    except Exception:  # noqa: BLE001 - a bad config never turns the nudge on
        return False


def nudge_on(env: Mapping[str, str]) -> bool:
    return _switch(SWITCH_ENV, SWITCH_KEY, env)


def shadow_on(env: Mapping[str, str]) -> bool:
    return _switch(SHADOW_ENV, SHADOW_KEY, env)


def mode(env: Mapping[str, str]) -> str:
    """``on``, ``shadow`` or ``off``. A config that cannot load gives ``off``."""
    if nudge_on(env):
        return "on"
    return "shadow" if shadow_on(env) else "off"


def _patterns(
    key: str, default: Sequence[str], flags: int, env: Mapping[str, str]
) -> List[Pattern[str]]:
    items: Sequence[str] = default
    cfg = _cfg()
    if cfg is not None:
        try:
            configured = cfg.string_list(key, env)
        except Exception:  # noqa: BLE001
            configured = ()
        if configured:
            items = configured
    out: List[Pattern[str]] = []
    for text in list(items)[:PATTERN_MAX_COUNT]:
        if not isinstance(text, str) or len(text) > PATTERN_MAX_CHARS:
            continue
        try:
            out.append(re.compile(text, flags))
        except re.error:
            continue
    return out


def tool_patterns(env: Mapping[str, str]) -> List[Pattern[str]]:
    return _patterns(TOOLS_KEY, DEFAULT_TOOLS, re.IGNORECASE, env)


def command_patterns(env: Mapping[str, str]) -> List[Pattern[str]]:
    return _patterns(COMMANDS_KEY, DEFAULT_COMMANDS, 0, env)


# ── matching ────────────────────────────────────────────────────────────────

_GT: Any = None


def _guard_table() -> Any:
    global _GT
    if _GT is None:
        spec = importlib.util.spec_from_file_location("guard_table", _HERE / "guard_table.py")
        if spec is None or spec.loader is None:
            raise ImportError("guard_table")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GT = mod
    return _GT


def match_tool(name: str, patterns: Sequence[Pattern[str]]) -> bool:
    return any(p.search(name) for p in patterns)


def _prefilter(pattern: Pattern[str]) -> Optional[Pattern[str]]:
    """A superset of ``pattern`` that matches anywhere in a command line: the ``^`` becomes
    "start or a separator". It keeps the guard table unloaded for most Bash calls."""
    text = pattern.pattern
    if text.startswith("^"):
        text = r"(?:^|[\s;&|(`])" + text[1:]
    try:
        return re.compile(text, pattern.flags)
    except re.error:
        return None


def match_command(
    command: str, patterns: Sequence[Pattern[str]]
) -> Optional[Tuple[Pattern[str], str, re.Match[str]]]:
    """``(pattern, simple command, match)`` for the first live simple command of ``command``
    that a pattern matches at its start, else None. Quoted data, comments and here-document
    bodies are masked by the guard table's lexer first."""
    if not command or len(command) > COMMAND_MAX_CHARS or not patterns:
        return None
    for p in patterns:
        pre = _prefilter(p)
        if pre is not None and pre.search(command):
            break
    else:
        return None
    gt = _guard_table()
    for seg in gt.command_segments(command):
        for variant in gt.variants(seg):
            for p in patterns:
                m = p.search(variant)
                if m is not None:
                    return p, variant, m
    return None


def branch_for(cwd: Any) -> str:
    """The branch that HEAD names in the git folder of ``cwd`` (or a parent), read from the files
    and not with ``git``: no process, no lock. ``""`` for a detached HEAD or no repository."""
    try:
        here = Path(str(cwd)).resolve()
        for folder in (here, *here.parents)[:12]:
            git = folder / ".git"
            if git.is_file():  # a linked worktree: ".git" holds "gitdir: <path>"
                text = git.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return ""
                git = (folder / text[7:].strip()).resolve()
            if git.is_dir():
                head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
                prefix = "ref: refs/heads/"
                return head[len(prefix) :] if head.startswith(prefix) else ""
    except (OSError, ValueError, RuntimeError):
        pass
    return ""


def _clean_topic(raw: str) -> str:
    return " ".join(_TOPIC_BAD.sub(" ", raw).split())[:TOPIC_MAX].strip()


def topic_for(
    kind: str, tool_input: Any, variant: str = "", match: Any = None, branch: str = ""
) -> str:
    """A short, safe search topic for the line: the ticket key or summary of a ticket call; for a
    command the branch of the working folder when the argument is only a PR number, else the
    first argument after the matched command (a branch or a URL)."""
    raw = ""
    if kind == "tool" and isinstance(tool_input, dict):
        for key in _TOPIC_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                raw = value
                break
    elif kind == "command" and match is not None:
        arg = next((w for w in variant[match.end() :].split() if not w.startswith("-")), "")
        raw = branch if (branch and (not arg or arg.isdigit())) else arg
    clean = _clean_topic(raw)
    return clean or ("this ticket" if kind == "tool" else "this merge")


def nudge_line(topic: str) -> str:
    return LINE.format(topic=topic)[:LINE_MAX]


# ── the last 10 tool calls ──────────────────────────────────────────────────


def recent_calls(
    path: str, skip_id: str = "", limit: int = WINDOW
) -> Optional[List[Tuple[str, str]]]:
    """The ``limit`` newest ``(tool_use id, name)`` pairs of the main thread in the transcript at
    ``path``, newest first, without the call ``skip_id``. None when the tail does not show
    ``limit`` calls and does not reach the start of the file (the window is unknown)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            for step in TAIL_STEPS:
                start = max(0, size - step)
                fh.seek(start)
                data = fh.read()
                lines = data.split(b"\n")
                if start > 0:
                    lines = lines[1:]  # the first line is cut
                calls: List[Tuple[str, str]] = []
                for line in reversed(lines):
                    if b'"tool_use"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or rec.get("isSidechain"):
                        continue
                    msg = rec.get("message")
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if rec.get("type") != "assistant" or not isinstance(content, list):
                        continue
                    for block in reversed(content):
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        ident, name = block.get("id"), block.get("name")
                        if not isinstance(name, str) or ident == skip_id:
                            continue
                        calls.append((ident if isinstance(ident, str) else "", name))
                        if len(calls) >= limit:
                            return calls
                if start == 0:
                    return calls
    except OSError:
        return None
    return None


def window_verdict(prior: Sequence[Tuple[str, str]], last_nudged_id: str = "") -> str:
    """``fire``, ``recent_recall`` or ``cooldown`` for a matching call, from the newest-first
    ``prior`` calls (at most 10 are read). The one rule, shared with the replay tool."""
    window = list(prior)[:WINDOW]
    if any(name.endswith(RECALL_SUFFIX) for _id, name in window):
        return "recent_recall"
    if last_nudged_id and any(ident == last_nudged_id for ident, _name in window):
        return "cooldown"
    return "fire"


# ── state and log ───────────────────────────────────────────────────────────


def cache_dir(env: Mapping[str, str]) -> Path:
    explicit = (env.get("NOBLIVION_RECALL_CACHE_DIR") or "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    cfg = _cfg()
    if cfg is None:
        raise OSError("no hook_config")
    return Path(cfg.cache_dir(env))


def _make_dirs(path: Path, env: Mapping[str, str]) -> None:
    cfg = _cfg()
    if cfg is not None:
        cfg.make_dirs(path, env)  # never makes a data dir that an uninstall deleted
    else:
        raise OSError("no hook_config")


def load_state(cache: Path) -> Dict[str, Dict[str, str]]:
    try:
        doc = json.loads((cache / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_state(cache: Path, env: Mapping[str, str], session: str, entry: Dict[str, str]) -> None:
    """Record ``entry`` for ``session``, keep the newest ``STATE_SESSIONS`` sessions. A lost
    update (two sessions at once) costs one extra line, so there is no lock."""
    try:
        doc = load_state(cache)
        doc.pop(session, None)
        doc[session] = entry  # dict order: newest last
        while len(doc) > STATE_SESSIONS:
            doc.pop(next(iter(doc)))
        _make_dirs(cache, env)
        tmp = cache / (STATE_NAME + ".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, cache / STATE_NAME)
    except OSError:
        pass


def log_event(cache: Path, env: Mapping[str, str], session: str, **fields: Any) -> None:
    """One line in ``nudge.log``. Carries no topic, no query and no command text."""
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    body = " ".join("%s=%s" % (k, v) for k, v in fields.items())
    try:
        _make_dirs(cache, env)
        path = cache / LOG_NAME
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, str(path) + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s session=%s %s\n" % (ts, session, body))
    except OSError:
        pass


# ── the hook ────────────────────────────────────────────────────────────────


def _session(payload: Mapping[str, Any]) -> str:
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) and _SESSION_ID_RE.match(sid) else ""


def classify(
    payload: Mapping[str, Any], env: Mapping[str, str]
) -> Optional[Tuple[str, str, Any, Any]]:
    """``(moment, variant, match, tool_input)`` when the call is one to nudge before, else None.
    ``moment`` is ``tool`` (an MCP tool) or ``command`` (a Bash command)."""
    name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        return None
    if name.endswith(RECALL_SUFFIX) or name.endswith(REMEMBER_SUFFIX):
        return None
    if name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        hit = match_command(command, command_patterns(env))
        return ("command", hit[1], hit[2], tool_input) if hit else None
    if name.startswith("mcp__") and match_tool(name, tool_patterns(env)):
        return ("tool", "", None, tool_input)
    return None


def decide(payload: Any, env: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """The hook output for ``payload``, or None. Logs every matching call. Never raises."""
    m = mode(env)
    if m == "off" or not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    session = _session(payload)
    if not session:
        return None
    hit = classify(payload, env)
    if hit is None:
        return None
    moment, variant, match, tool_input = hit
    cache = cache_dir(env)
    call_id = payload.get("tool_use_id")
    call_id = call_id if isinstance(call_id, str) else ""
    path = payload.get("transcript_path")
    prior = recent_calls(path, call_id) if isinstance(path, str) and path else None
    if prior is None:
        log_event(cache, env, session, event="nudge", moment=moment, result="skip_no_window")
        return None
    last = (load_state(cache).get(session) or {}).get("id", "")
    verdict = window_verdict(prior, last if isinstance(last, str) else "")
    if verdict != "fire":
        log_event(cache, env, session, event="nudge", moment=moment, result="skip_" + verdict)
        return None
    branch = branch_for(payload.get("cwd")) if moment == "command" and payload.get("cwd") else ""
    line = nudge_line(topic_for(moment, tool_input, variant, match, branch))
    save_state(cache, env, session, {"id": call_id, "moment": moment})
    log_event(
        cache,
        env,
        session,
        event="nudge",
        moment=moment,
        result="fired" if m == "on" else "would_fire",
        chars=len(line),
    )
    if m != "on":
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": line}}


def note_recall(payload: Any, env: Mapping[str, str]) -> None:
    """Log one recall call (called by ``mcp_session_hook``), with the moment of the last nudge
    when that nudged call is still in the last 10 tool calls. Does nothing when both switches are
    off. Never raises."""
    try:
        if mode(env) == "off" or not isinstance(payload, dict) or payload.get("agent_id"):
            return
        session = _session(payload)
        if not session:
            return
        cache = cache_dir(env)
        tool_input = payload.get("tool_input")
        fetch = 1 if isinstance(tool_input, dict) and tool_input.get("fetch_id") else 0
        entry = load_state(cache).get(session) or {}
        after = "-"
        path = payload.get("transcript_path")
        call_id = payload.get("tool_use_id")
        if entry.get("id") and isinstance(path, str) and path:
            prior = recent_calls(path, call_id if isinstance(call_id, str) else "")
            if prior and any(ident == entry["id"] for ident, _n in prior):
                after = str(entry.get("moment") or "-")
        log_event(cache, env, session, event="recall_call", fetch=fetch, after=after)
    except Exception:  # noqa: BLE001 - logging never blocks a recall call
        pass


def main(stdin=None, stdout=None, environ: Optional[Mapping[str, str]] = None) -> int:
    """Always returns 0: a failure here must never block a tool call."""
    env = os.environ if environ is None else environ
    try:
        if mode(env) == "off":
            return 0
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        out = decide(payload, env)
        if out is not None:
            (stdout or sys.stdout).write(json.dumps(out) + "\n")
    except Exception:  # noqa: BLE001 - fail open: print nothing, exit 0
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
