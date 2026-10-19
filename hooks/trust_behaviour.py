#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavioural use: a later tool call follows a shown rule (NOBLIVION-120).

A ``use`` today is a guard deny, an MCP fetch or a reply that names the note
(``trust_signals``). A rule that Claude Code follows without a word counts as
nothing, so trust ranking never sees a positive signal for it. This module
adds one more source, ``behaviour``: a tool call after the prompt hook showed
the rule that fits the rule's own fields. Setting
``NOBLIVION_TRUST_BEHAVIOUR_USE`` (config key ``trust.behaviour_use``). Off
by default (docs/trust.md).

A tool call counts for a shown note when the note has a row in the guard
table and the call is one of these, in this order:

1. ``violates`` matches the call: the rule is broken. Never a use.
2. ``complies`` matches the call: a run of the rule's own command.
3. A plain command trigger fires on a Bash command, or a ``glob:`` trigger on
   the path of a Write, Edit, MultiEdit or NotebookEdit call. Only a STRONG
   trigger counts (``guard_hook.weak_trigger``): a generic trigger such as
   ``git status`` fires on most calls and says nothing about this one.

The ``apply`` field is prose, so it is not matched. ``complies`` is the
machine form of the apply command. ``phrase:`` and ``tool:`` triggers are not
matched either: the first is about the prompt, the second names no command.

A note gets at most one behaviour ``use`` per session, and only for a call
after the note was shown. The calls come from the main transcript; a
subagent's transcript is not read, as in ``trust_signals``.

It runs in the Stop flush worker (``trust_flush.scan_signals``), in the
background, with the same transcript records as the citation scan, so the
hooks stay fast and the replay tool counts the same way.

Standard library only; runs on Python 3.9. ``scan`` raises nothing on bad
input.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Sequence, Set, Tuple

SRC_BEHAVIOUR = "behaviour"
WHY_COMPLIES = "complies"
COMMAND_MAX_CHARS = 4_000  # a longer command is cut before the match
CALLS_MAX = 2_000  # tool calls looked at in one scan
USED_MAX = 500
FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_MODS: Dict[str, Any] = {}


def _load(name: str) -> Any:
    """A sibling module of this file, loaded once by path."""
    mod = _MODS.get(name)
    if mod is None:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), f"{name}.py")
        have = sys.modules.get(name)
        if have is not None and os.path.realpath(getattr(have, "__file__", "") or "") == path:
            _MODS[name] = have
            return have
        spec = importlib.util.spec_from_file_location(f"trust_behaviour_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, mod)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return mod


def _sig() -> Any:
    return _load("trust_signals")


def load_table(environ: Mapping[str, str], cwd: str = "") -> Dict[str, Any]:
    """The guard table of the session's project, as it is on disk. It is not
    rebuilt here. An absent or broken table reads as empty."""
    gt = _load("guard_table")
    folders = gt.source_dirs(cwd or None, environ)
    return gt.load_table(gt.table_path(folders[0] if folders else None, environ))


def _state(raw: Any) -> Dict[str, Any]:
    """The saved scan state, every field checked: it is a file on disk."""
    doc = raw if isinstance(raw, dict) else {}
    used = doc.get("used")
    ids = (
        [m for m in (_sig()._mid(x) for x in used) if m is not None]
        if isinstance(used, list)
        else []
    )
    return {"used": ids}


def tool_calls(rec: Mapping[str, Any]) -> List[Tuple[str, Mapping[str, Any]]]:
    """``[(tool name, input)]`` of the tool calls in a main-thread assistant
    record."""
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return []
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    out: List[Tuple[str, Mapping[str, Any]]] = []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name, inp = block.get("name"), block.get("input")
        if isinstance(name, str) and isinstance(inp, dict):
            out.append((name, inp))
    return out


def match_call(
    name: str, inp: Mapping[str, Any], table: Mapping[str, Any], share: Mapping[str, int]
) -> Dict[str, str]:
    """``{entry id: why}`` for the entries of ``table`` that this tool call
    follows. ``why`` is ``complies`` or the trigger that fired. A rule whose
    ``violates`` matches the call is never in the answer."""
    gh, gt = _load("guard_hook"), _load("guard_table")
    out: Dict[str, str] = {}
    if name == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return out
        cmd = cmd[:COMMAND_MAX_CHARS]
        broken = {str(h.get("id", "")) for h in gt.match(cmd, table)}
        for eid in gt.complies_match(cmd, table):
            out[eid] = WHY_COMPLIES
        cands = gh.command_rows(cmd, table)
    elif name in FILE_TOOLS:
        path = next(
            (
                inp[f]
                for f in ("file_path", "notebook_path")
                if isinstance(inp.get(f), str) and inp[f]
            ),
            "",
        )
        if not path:
            return out
        broken = set()
        cands = gh.path_rows(path, table)
    else:
        return out
    for _weight, trig, entry in cands:
        eid = str(entry.get("id", ""))
        if eid not in out and not gh.weak_trigger(trig, entry, share.get(trig, 0)):
            out[eid] = trig
    return {i: why for i, why in out.items() if i not in broken}


def scan(
    records: Iterable[Mapping[str, Any]],
    shown: Mapping[int, Mapping[str, str]],
    exposures: Mapping[int, Sequence[str]],
    table: Mapping[str, Any],
    state: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Transcript records, in order -> ``(events, state)``.

    ``shown`` maps a note id to its name; ``exposures`` maps it to the times
    it was shown; ``table`` is the guard table. A note gets one ``use`` per
    session. Each event holds ``why`` (``complies`` or the trigger), a local
    field for labelling: the store never sees it."""
    sig = _sig()
    st = _state(state)
    used: Set[int] = set(st["used"])
    entries = {
        sig.file_key(str(e.get("id", ""))): e
        for e in table.get("entries") or []
        if isinstance(e, dict) and (e.get("complies") or e.get("triggers"))
    }
    by_id: Dict[str, int] = {}
    for mid, note in shown.items():
        entry = entries.get(sig.file_key(note.get("name", "")))
        if entry is not None:
            by_id[str(entry.get("id", ""))] = mid
    events: List[Dict[str, Any]] = []
    live = [e for e in entries.values() if by_id.get(str(e.get("id", ""))) not in (None, *used)]
    if live:
        share = _load("guard_hook").trigger_share(table)
        calls = 0
        for rec in records:
            ts = sig.te().norm_ts(rec.get("timestamp"))
            if ts is None:
                continue
            for name, inp in tool_calls(rec):
                calls += 1
                for eid, why in match_call(name, inp, {"entries": live}, share).items():
                    mid = by_id[eid]
                    times = exposures.get(mid) or ()
                    if mid in used or not times or min(times) > ts:
                        continue  # used already, or not shown yet
                    used.add(mid)
                    events.append(
                        {
                            "mv_id": mid,
                            "kind": sig.te().KIND_USE,
                            "ts": ts,
                            "src": SRC_BEHAVIOUR,
                            "why": why[:200],
                        }
                    )
                live = [e for e in live if by_id[str(e.get("id", ""))] not in used]
            if calls >= CALLS_MAX or not live:
                break
    st["used"] = sorted(used)[-USED_MAX:]
    return events, st
