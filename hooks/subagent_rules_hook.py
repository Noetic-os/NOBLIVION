#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-9: serve the ranked memory rules to a subagent.

A subagent starts with no memory index: the UserPromptSubmit recall hook does
not fire for it. This hook gives the subagent the ranked rules for its task
text. It imports ``recall_hook.py`` from its own folder and uses
that hook's index code unchanged (the same store call, relevance floor,
no-rule drop, hygiene, re-rank, rule rows and APPLY lines).

What Claude Code 2.1.261 gives (read from the installed binary, 2026-09-30):

- ``SubagentStart`` input: the common fields plus ``agent_id`` and
  ``agent_type``. It does NOT carry the task prompt. Its output
  ``hookSpecificOutput.additionalContext`` is added to the SUBAGENT's first
  messages, not to the parent.
- ``PreToolUse`` for the ``Agent`` tool (older name ``Task``) carries
  ``tool_input.prompt``, ``tool_input.subagent_type`` and ``tool_use_id``. Its
  ``additionalContext`` goes to the PARENT. ``updatedInput`` can rewrite the
  prompt, but the harness applies it only together with
  ``permissionDecision`` ``allow`` or ``ask``.

So the hook has two legs, both wired to this one script:

1. ``PreToolUse`` (matcher ``Agent|Task``): save the task text in
   ``<cache>/subagent/pending/<session>/<tool_use_id>.json``. Print nothing.
2. ``SubagentStart``: find the task text, ask the store, print the rules as
   ``additionalContext``. The task text is found in this order, and the log
   line names the way (``SubagentStart:<via>``):
   ``input``  the event carries the text itself (INPUT_TEXT_KEYS; 2.1.261
              does not);
   ``meta``   ``<transcript folder>/<session>/subagents/agent-<id>.meta.json``
              names the ``toolUseId``, which names the saved file exactly;
   ``transcript``  the first user line of ``agent-<id>.jsonl``;
   ``fifo``   the oldest saved text of this session with the same agent type;
   ``fifo_any``  the oldest saved text of this session.

``NOBLIVION_SUBAGENT_RULES_MODE=rewrite`` is the other path, off by default: the
PreToolUse leg appends the rules to ``tool_input.prompt`` with ``updatedInput``
and ``permissionDecision: allow``, and the SubagentStart leg does nothing. It
needs no matching, but it approves the Agent call from the hook (deny and ask
rules in the settings still win) and it changes the prompt the parent wrote.

State: everything is under ``<cache>/subagent`` (``<cache>`` is
``NOBLIVION_RECALL_CACHE_DIR``, default ``<data dir>/cache``), and the
session key of a call is ``<session id>.<agent id>``. The parent session's
shown-set and last index are in ``<cache>`` and are never read or written.

Fail open: any error prints nothing and exits 0. The whole call is cut at
``BUDGET_S`` (4.5 s; the hook entry has ``timeout: 5``). A store that is down,
fails the listener proof or timed out in the last 30 s (``store.hung``, the
recall hook's back-off) gives no rules (design doc section 3.5). One log
line per call in ``<cache>/subagent/recall.log``, in the recall log's format.
It holds no task text and no memory text.

The memory folder and the root are the session's (``recall_hook.session_env``,
from the event's ``cwd``), else ``NOBLIVION_MEMORY_DIR``. There is no
home-folder default (NOBLIVION-31).

Environment:
  NOBLIVION_SUBAGENT_RULES_OFF          any value: do nothing
  NOBLIVION_SUBAGENT_RULES_K            rows, default 8
  NOBLIVION_SUBAGENT_RULES_MAX_CHARS    characters of context, default 2500
  NOBLIVION_SUBAGENT_RULES_QUERY_CHARS  characters of task text sent, default 2000
  NOBLIVION_SUBAGENT_RULES_MODE         ``context`` (default) or ``rewrite``
  NOBLIVION_RECALL_*                    as the recall hook. Unset index options
                                      take the defaults in INDEX_DEFAULTS. The
                                      floor NOBLIVION_RECALL_INDEX_MIN_SCORE,
                                      unset, is DEFAULT_INDEX_MIN_SCORE (0.68)
                                      for an answer of the measured model and
                                      no floor for another model.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_recall_hook():
    name = "recall_hook"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, name + ".py"))
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rh = _load_recall_hook()

OFF_ENV = "NOBLIVION_SUBAGENT_RULES_OFF"
K_ENV = "NOBLIVION_SUBAGENT_RULES_K"
CAP_ENV = "NOBLIVION_SUBAGENT_RULES_MAX_CHARS"
QUERY_CHARS_ENV = "NOBLIVION_SUBAGENT_RULES_QUERY_CHARS"
MODE_ENV = "NOBLIVION_SUBAGENT_RULES_MODE"
K_DEFAULT = 8
K_MAX = 30
CAP_DEFAULT = 2500
CAP_MIN = 300
QUERY_CHARS_DEFAULT = 2000
BUDGET_S = 4.5
SUBDIR = "subagent"
PENDING = "pending"
PENDING_MAX_AGE_S = 86400.0
# A subagent starts at once after its Agent call. A saved text older than this
# belongs to a call that never started a subagent (denied, or failed), so the
# order-based match must not hand it to a later subagent. The exact match by
# ``toolUseId`` has no age limit.
PENDING_FIFO_MAX_AGE_S = 300.0
META_WAIT_S = 0.25
AGENT_TOOLS = ("Agent", "Task")
DEFAULT_AGENT_TYPE = "general-purpose"
HEADER = "Memory rules for this subagent task, ranked from the local memory store."
# 2.1.261 sends none of these with SubagentStart. Two readings of the public
# hooks page on 2026-09-30 named a task text field, each with a different name,
# so a newer Claude Code may send one. A text in the event is used first.
INPUT_TEXT_KEYS = ("subagent_prompt", "task_description", "prompt")

# The relevance floor of the subagent index: a cosine of the recall hook's
# THRESHOLD_MODEL, and the default only for an answer that names that model.
# Another model gets no default floor, as the prompt index does
# (recall_hook.INDEX_MIN_SCORE_ENV). NOBLIVION_RECALL_INDEX_MIN_SCORE that is
# set is the floor for every model.
# Measured by tools/eval_thresholds.py on this hook's path (NOBLIVION-76). The
# old floor 0.52 let rows through for 16 of the 20 queries that no memory
# answers (precision 0.112, recall 1.000); 0.68 has the best F1 (precision
# 0.857, recall 0.750, 1 of 20). The eval queries are prompts: a task text is
# longer, and its cosines were not measured.
DEFAULT_INDEX_MIN_SCORE = 0.68
# The index options of the reference prompt hook setup. Used only for a
# variable the hook command does not set.
INDEX_DEFAULTS: Tuple[Tuple[str, str], ...] = (
    (rh.INDEX_DROP_NO_RULE_ENV, "1"),
    (rh.INDEX_ROW_DEDUPE_ENV, "1"),
    (rh.INDEX_APPLY_ENV, "1"),
    (rh.INDEX_HYGIENE_ENV, "1"),
    (rh.INDEX_RERANK_ENV, "1"),
    (rh.INDEX_RULE_ROWS_ENV, "1"),
    (rh.SHOWN_SET_ENV, "1"),
)


class _Budget(BaseException):
    """The call ran past BUDGET_S."""


def _int_env(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(env.get(name) or default).strip())
    except (TypeError, ValueError):
        return default
    return default if value < low else min(value, high)


def is_off(env: Mapping[str, str]) -> bool:
    """``NOBLIVION_SUBAGENT_RULES_OFF`` or ``NOBLIVION_RECALL_DISABLE`` is on."""
    return rh.switch_on(env, OFF_ENV) or rh.recall_disabled(env)


def sub_cache(env: Mapping[str, str]) -> str:
    """The hook's own state folder, inside the recall cache folder."""
    return os.path.join(rh.cache_dir(env), SUBDIR)


def index_environ(env: Mapping[str, str]) -> Dict[str, str]:
    """The environment the recall hook's index code runs with: the caller's,
    the live defaults for what it left unset, and this hook's own K, cap and
    state folder."""
    out = dict(env)
    for name, value in INDEX_DEFAULTS:
        out.setdefault(name, value)
    out[rh.INDEX_ENV] = "1"
    out[rh.INDEX_K_ENV] = str(_int_env(env, K_ENV, K_DEFAULT, 1, K_MAX))
    cap = _int_env(env, CAP_ENV, CAP_DEFAULT, CAP_MIN, 20000)
    out[rh.INDEX_CAP_ENV] = str(cap - len(HEADER) - 1)
    out["NOBLIVION_RECALL_CACHE_DIR"] = sub_cache(env)
    if not out.get(rh.MEMORY_DIR_ENV):
        folder = os.path.expanduser(env.get("NOBLIVION_MEMORY_DIR") or "")
        if folder and os.path.isdir(folder):
            out[rh.MEMORY_DIR_ENV] = folder
    return out


def _sid(*parts: Any) -> Optional[str]:
    """``a.b`` when every part is a valid id and the whole is one too."""
    if not all(rh._valid_sid(p) for p in parts):
        return None
    return rh._valid_sid(".".join(parts))


# ── the saved task texts ────────────────────────────────────────────────────


def pending_dir(env: Mapping[str, str], session: str) -> str:
    return os.path.join(sub_cache(env), PENDING, session)


def save_pending(
    env: Mapping[str, str], session: str, tool_use_id: str, agent_type: str, prompt: str
) -> bool:
    folder = pending_dir(env, session)
    path = os.path.join(folder, tool_use_id + ".json")
    doc = {"ts": time.time(), "agent_type": agent_type, "prompt": prompt}
    try:
        _make_dirs(folder)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _read_pending(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("prompt"), str):
        return None
    return doc


def list_pending(env: Mapping[str, str], session: str) -> List[Tuple[float, str, Dict[str, Any]]]:
    """``(ts, path, doc)`` of every saved text of the session, oldest first."""
    folder = pending_dir(env, session)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(folder, name)
        doc = _read_pending(path)
        if doc is None:
            continue
        try:
            ts = float(doc.get("ts"))  # pyright: ignore[reportArgumentType] - None or a bad value raises, caught below
        except (TypeError, ValueError):
            ts = 0.0
        out.append((ts, path, doc))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def _claim(path: str) -> bool:
    """Take a saved text for one subagent. False when another call took it."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def prune_pending(env: Mapping[str, str], now: Optional[float] = None) -> int:
    """Remove saved texts older than PENDING_MAX_AGE_S (a subagent that never
    started), and session folders left empty. Never raises."""
    root = os.path.join(sub_cache(env), PENDING)
    cutoff = (time.time() if now is None else now) - PENDING_MAX_AGE_S
    removed = 0
    try:
        sessions = os.listdir(root)
    except OSError:
        return 0
    for session in sessions:
        folder = os.path.join(root, session)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        left = len(names)
        for name in names:
            path = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if os.stat(path).st_mtime < cutoff:
                    os.remove(path)
                    removed += 1
                    left -= 1
        if left <= 0:
            with contextlib.suppress(OSError):
                os.rmdir(folder)
    return removed


# ── find the task text of a starting subagent ──────────────────────────────


def _subagent_file(
    payload: Mapping[str, Any], session: str, agent_id: str, suffix: str
) -> Optional[str]:
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    return os.path.join(
        os.path.dirname(transcript), session, "subagents", f"agent-{agent_id}{suffix}"
    )


def _meta_tool_use_id(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    tid = doc.get("toolUseId") if isinstance(doc, dict) else None
    return tid if rh._valid_sid(tid) else None


def _transcript_prompt(path: Optional[str]) -> Optional[str]:
    """The text of the first user line of a subagent transcript."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(5):
                line = fh.readline(2_000_000)
                if not line:
                    break
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(doc, dict) or doc.get("type") != "user":
                    continue
                content = (doc.get("message") or {}).get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    texts = [
                        c["text"]
                        for c in content
                        if isinstance(c, dict) and isinstance(c.get("text"), str)
                    ]
                    return "\n".join(texts) or None
    except OSError:
        return None
    return None


def find_task_text(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    session: str,
    agent_id: str,
    wait_s: float = META_WAIT_S,
) -> Tuple[Optional[str], str]:
    """``(text, via)``. ``(None, reason)`` when no way found the text."""
    for key in INPUT_TEXT_KEYS:
        direct = payload.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct, "input"
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        agent_type = DEFAULT_AGENT_TYPE
    meta = _subagent_file(payload, session, agent_id, ".meta.json")
    fresh_from = time.time() - PENDING_FIFO_MAX_AGE_S
    pending = [item for item in list_pending(env, session) if item[0] >= fresh_from]
    same = [item for item in pending if item[2].get("agent_type") == agent_type]
    # Two saved texts of one type: only the meta file says which is ours. It is
    # written at about the time this event fires, so wait a moment for it.
    deadline = time.monotonic() + (wait_s if len(same) > 1 else 0.0)
    while True:
        tid = _meta_tool_use_id(meta)
        if tid or time.monotonic() >= deadline:
            break
        time.sleep(0.025)
    if tid:
        path = os.path.join(pending_dir(env, session), tid + ".json")
        doc = _read_pending(path)
        if doc is not None and _claim(path):
            return doc["prompt"], "meta"
    text = _transcript_prompt(_subagent_file(payload, session, agent_id, ".jsonl"))
    if text and text.strip():
        return text, "transcript"
    for items, via in ((same, "fifo"), (pending, "fifo_any")):
        for _ts, path, doc in items:
            if _claim(path):
                return doc["prompt"], via
    return None, "no_task_text"


# ── the two legs ────────────────────────────────────────────────────────────


def _rules_for(text: str, label: str, sid: Optional[str], t0: float, env: Mapping[str, str]) -> str:
    """The context block for ``text``, or "". The recall hook's index code
    writes the call's log line."""
    ienv = index_environ(env)
    query = " ".join(text.split())[: _int_env(env, QUERY_CHARS_ENV, QUERY_CHARS_DEFAULT, 50, 8000)]
    cache = rh.cache_dir(ienv)
    if not query:
        rh.log_line(cache, label, sid, 0, 0, 0, "skip:empty_query")
        return ""
    buf = io.StringIO()
    rh._serve_index(query, label, sid, cache, t0, buf, ienv, DEFAULT_INDEX_MIN_SCORE)
    body = buf.getvalue().strip("\n")
    return f"{HEADER}\n{body}" if body else ""


def mode(env: Mapping[str, str]) -> str:
    return "rewrite" if (env.get(MODE_ENV) or "").strip().lower() == "rewrite" else "context"


def run(stdin_text: str, stdout, env: Dict[str, str]) -> None:
    t0 = time.monotonic()
    cache = sub_cache(env)
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        rh.log_line(cache, "-", None, 0, 0, 0, "fail:bad_stdin")
        return
    event = str(payload.get("hook_event_name") or "-")
    session = rh._valid_sid(payload.get("session_id"))
    env = rh.session_env(env, payload)
    if is_off(env):
        rh.log_line(cache, event, session, 0, 0, 0, "skip:disabled")
        return
    if event == "PreToolUse":
        if payload.get("tool_name") not in AGENT_TOOLS:
            rh.log_line(cache, event, session, 0, 0, 0, "skip:tool")
            return
        raw_ti = payload.get("tool_input")
        ti: Dict[str, Any] = raw_ti if isinstance(raw_ti, dict) else {}
        prompt = ti.get("prompt")
        tid = rh._valid_sid(payload.get("tool_use_id"))
        if not isinstance(prompt, str) or not prompt.strip():
            rh.log_line(cache, event, session, 0, 0, 0, "skip:empty_query")
            return
        if not session or not tid:
            rh.log_line(cache, event, session, 0, 0, 0, "skip:no_session")
            return
        if mode(env) == "rewrite":
            block = _rules_for(prompt, "AgentRewrite", _sid(session, tid), t0, env)
            if block:
                doc = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "memory rules added to the subagent prompt",
                        "updatedInput": dict(ti, prompt=f"{prompt}\n\n{block}"),
                    }
                }
                stdout.write(json.dumps(doc) + "\n")
                stdout.flush()
            return
        agent_type = ti.get("subagent_type")
        if not isinstance(agent_type, str) or not agent_type:
            agent_type = DEFAULT_AGENT_TYPE
        cut = _int_env(env, QUERY_CHARS_ENV, QUERY_CHARS_DEFAULT, 50, 8000)
        ok = save_pending(env, session, tid, agent_type, prompt[: cut * 2])
        with contextlib.suppress(Exception):
            prune_pending(env)
        rh.log_line(
            cache,
            "AgentSave",
            session,
            0,
            0,
            int((time.monotonic() - t0) * 1000),
            "ok" if ok else "fail:unwritable",
        )
        return
    if event != "SubagentStart":
        rh.log_line(cache, event, session, 0, 0, 0, "skip:event")
        return
    if mode(env) == "rewrite":
        rh.log_line(cache, event, session, 0, 0, 0, "skip:mode_rewrite")
        return
    agent_id = rh._valid_sid(payload.get("agent_id"))
    if not session or not agent_id:
        rh.log_line(cache, event, session, 0, 0, 0, "skip:no_session")
        return
    sid = _sid(session, agent_id)
    text, via = find_task_text(payload, env, session, agent_id)
    if text is None:
        rh.log_line(cache, event, sid, 0, 0, int((time.monotonic() - t0) * 1000), f"skip:{via}")
        return
    block = _rules_for(text, f"SubagentStart:{via}", sid, t0, env)
    if block:
        doc = {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": block}}
        stdout.write(json.dumps(doc) + "\n")
        stdout.flush()


def _on_alarm(_signum, _frame):
    raise _Budget()


def main(
    stdin=None, stdout=None, environ: Optional[Dict[str, str]] = None, budget_s: float = BUDGET_S
) -> int:
    """Always returns 0. The output is one write at the end, so a call that is
    cut or fails has printed nothing."""
    env: Dict[str, str] = {}
    armed = False
    out = io.StringIO()
    try:
        env = dict(os.environ if environ is None else environ)
        if budget_s > 0 and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, budget_s)
            armed = True
        run((stdin or sys.stdin).read(), out, env)
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            armed = False
        text = out.getvalue()
        if text:
            target = stdout or sys.stdout
            target.write(text)
            target.flush()
    except BaseException as exc:  # noqa: BLE001 - fail open: no output, exit 0
        with contextlib.suppress(BaseException):
            if armed:
                signal.setitimer(signal.ITIMER_REAL, 0)
            status = (
                "fail:budget" if isinstance(exc, _Budget) else f"fail:internal:{type(exc).__name__}"
            )
            rh.log_line(sub_cache(env), "-", None, 0, 0, 0, status)
        return 0
    return 0


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _load_recall_hook()._sibling_module("hook_config").make_dirs(path)


if __name__ == "__main__":
    sys.exit(main())
