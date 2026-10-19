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

A Bash call counts for a shown note when the note has a row in the guard
table and the call is one of these, in this order:

1. ``violates`` matches the call: the rule is broken. Never a use.
2. ``complies`` matches the call: a run of the rule's own command. The regex
   runs on the masked view of the command (``guard_table.command_views``):
   heredoc bodies, quoted data and comments are masked, a string that the
   shell runs (``bash -c '...'``) is its own view. Text that is only data in
   the command never counts.
3. A plain command trigger fires on a segment of the command, the trigger is
   strong (``guard_hook.weak_trigger``), only this note holds it, it is not
   the name of a host or container (``names_a_place_only``), and the note's
   own ``rule`` or ``apply`` text tells the reader to run that command
   (``rule_runs_trigger``). A trigger says when a rule applies; it shows that
   the rule was followed only when it is the rule's own command.

Dropped after the hand-labelled sample of NOBLIVION-120 (precision 0.36 on 28
matches): ``glob:`` triggers and every file tool call (a path says that the
rule applies, not that it was followed; the text of a Write or Edit is data),
and any trigger that the rule only mentions. Each tier has a switch in code,
``TIER_COMPLIES`` and ``TIER_TRIGGER``, and stays off until a labelled sample
gives a Wilson 95 percent lower bound of 0.5 or more (docs/trust.md).

The ``apply`` field is prose and is not matched as a whole. ``phrase:`` and
``tool:`` triggers are not matched either: the first is about the prompt,
the second names no command.

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
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

SRC_BEHAVIOUR = "behaviour"
WHY_COMPLIES = "complies"
COMMAND_MAX_CHARS = 4_000  # a longer command is cut before the match
CALLS_MAX = 2_000  # tool calls looked at in one scan
USED_MAX = 500
# Tiers that may make a ``use`` (NOBLIVION-120). A tier stays off in code until
# a hand-labelled sample shows a Wilson 95 percent lower bound of precision of
# 0.5 or more (docs/trust.md, "Precision gate").
TIER_COMPLIES = True
TIER_TRIGGER = True
# Programs whose first plain argument is a host or a container, not a command:
# a trigger that names only that argument is the name of a place.
_PLACE_PROGRAMS = frozenset("ssh scp rsync sftp mosh".split())
_CONTAINER_SUBS = frozenset(
    "exec run logs restart start stop kill inspect attach cp top port rm".split()
)
_CONTAINER_PROGRAMS = frozenset("docker podman kubectl nerdctl".split())

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


def _masked_views(cmd: str, gt: Any) -> List[str]:
    """The text a ``complies`` regex may match: the masked shell segments of
    ``cmd`` (and their wrapper-free variants) and each masked view as a whole
    (for a regex that spans ``&&`` or ``|``). Heredoc bodies, quoted data and
    ``#`` comments are masked, so text that only appears as data (a file
    written with ``cat <<EOF``, a commit message, an ``echo``) never matches.
    A string that the shell runs (``bash -c '...'``) is its own view."""
    out: List[str] = []
    for masked, _raw, _dead in gt.command_views(cmd):
        if masked not in out:
            out.append(masked)
    for seg in gt.command_segments(cmd):
        for v in gt.variants(seg):
            if v not in out:
                out.append(v)
    return out


def complies_ids(cmd: str, table: Mapping[str, Any]) -> List[str]:
    """The ids of the entries whose ``complies`` regex matches ``cmd`` as a
    command, not as data. Same entries as ``guard_table.complies_match``
    (an entry with ``violates`` and ``complies``; a ``violates`` hit leaves it
    out) but the regex runs on the masked views only (``_masked_views``), never
    on the raw text. Fails open: a broken regex is skipped."""
    gt = _load("guard_table")
    views: Optional[List[str]] = None
    hit_ids: Optional[Set[str]] = None
    out: List[str] = []
    for e in table.get("entries") or []:
        if not isinstance(e, dict) or not e.get("violates") or not e.get("complies"):
            continue
        rx = gt._compile(str(e["complies"]))
        if rx is None:
            continue
        if views is None:
            views = [v for v in _masked_views(cmd, gt) if v.strip()]
        if not any(rx.search(v) for v in views):
            continue
        if hit_ids is None:
            hit_ids = {str(h["id"]) for h in gt.match(cmd, table)}
        if str(e.get("id", "")) not in hit_ids:
            out.append(str(e.get("id", "")))
    return out


def names_a_place_only(trigger: str) -> bool:
    """True when a command trigger names only a host or a container: ``ssh
    example-host``, ``docker exec app-db``. The name of a place is in most
    calls that go there, whatever the rule is about, so it does not show that
    the rule was followed. A trigger with a command after the place (``ssh
    host systemctl``, ``docker exec app-db psql``) is not a place only."""
    words = [w for w in trigger.split() if not w.startswith("-") and "=" not in w]
    while words and words[0] in ("sudo", "env", "timeout", "nohup", "setsid", "time", "exec"):
        words = words[1:]
    if not words:
        return True
    prog, rest = words[0], words[1:]
    if prog in _PLACE_PROGRAMS:
        return len(rest) <= 1
    if prog in _CONTAINER_PROGRAMS:
        if rest and rest[0] in ("container", "compose"):
            rest = rest[1:]
        if rest and rest[0] in _CONTAINER_SUBS:
            return len(rest) <= 2  # sub + container, no command in it
    return False


_SPAN = re.compile(r"`([^`\n]+)`")
# The words right before a command in a rule that tells the reader to run it.
_IMPERATIVE = re.compile(r"\b(run|use|call|execute|invoke)\s+(?:the\s+)?$", re.I)
_NEGATED = re.compile(r"\b(never|not|don't|avoid|without|instead of|rather than)\b", re.I)
_OPEN_WORD = ("-", "<", "[", "$", "{", "(", "...", "\u2026")  # a flag or a placeholder


def _command_core(span: str) -> str:
    """The literal words of a command in a rule, up to the first flag or
    placeholder: ``python3 x.py check <id> --live`` -> ``python3 x.py check``."""
    core: List[str] = []
    for w in span.split():
        if w.startswith(_OPEN_WORD):
            break
        core.append(w)
    return " ".join(core)


def rule_runs_trigger(trigger: str, entry: Mapping[str, Any]) -> bool:
    """True when the note's own ``rule`` or ``apply`` text tells the reader to
    run the trigger command: a backticked command whose literal words (up to
    the first flag or placeholder) are the whole trigger, right after ``run``, ``use``, ``call``, ``execute`` or
    ``invoke``. A trigger says when a rule applies; only a trigger that is the
    rule's own command also says the rule was followed. A command that the rule
    only mentions (the failing command in ``When `x` prints ...``, the command
    after ``instead of`` or ``never``, a host, a container, a file glob) is the
    situation, not the rule being followed: in the hand-labelled sample of
    NOBLIVION-120 such triggers fitted the call in 10 of 28 cases."""
    want = " ".join(trigger.split())
    if not want or want.startswith(("glob:", "tool:", "phrase:")):
        return False
    for field in ("rule", "apply"):
        text = entry.get(field)
        if not isinstance(text, str):
            continue
        for m in _SPAN.finditer(text):
            span = " ".join(m.group(1).split())
            if _command_core(span) != want:
                continue  # the rule's command is longer or shorter than the trigger
            before = text[max(0, m.start() - 30) : m.start()]
            if _IMPERATIVE.search(before[-20:]) and not _NEGATED.search(before):
                return True
    return False


def behaviour_trigger(trigger: str, entry: Mapping[str, Any], share: int) -> bool:
    """True when a fired plain command trigger can show that the rule was
    followed: it is strong (``guard_hook.weak_trigger``), held by this note
    alone (a trigger that two notes share cannot tell which one the call
    followed), it is not the name of a host or container only, and the note's
    own text tells the reader to run that command (``rule_runs_trigger``)."""
    gh = _load("guard_hook")
    if share > 1 or names_a_place_only(trigger):
        return False
    if gh.weak_trigger(trigger, entry, share):
        return False
    return rule_runs_trigger(trigger, entry)


def match_call(
    name: str,
    inp: Mapping[str, Any],
    table: Mapping[str, Any],
    share: Mapping[str, int],
    complies_on: Optional[bool] = None,
    trigger_on: Optional[bool] = None,
) -> Dict[str, str]:
    """``{entry id: why}`` for the entries of ``table`` that this tool call
    follows. ``why`` is ``complies`` or the trigger that fired. A rule whose
    ``violates`` matches the call is never in the answer. ``complies_on`` and
    ``trigger_on`` default to ``TIER_COMPLIES`` and ``TIER_TRIGGER``."""
    use_complies = TIER_COMPLIES if complies_on is None else complies_on
    use_trigger = TIER_TRIGGER if trigger_on is None else trigger_on
    gh, gt = _load("guard_hook"), _load("guard_table")
    out: Dict[str, str] = {}
    if name == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return out
        cmd = cmd[:COMMAND_MAX_CHARS]
        broken = {str(h.get("id", "")) for h in gt.match(cmd, table)}
        if use_complies:
            for eid in complies_ids(cmd, table):
                out[eid] = WHY_COMPLIES
        cands = gh.command_rows(cmd, table) if use_trigger else []
    else:
        return out
    for _weight, trig, entry in cands:
        eid = str(entry.get("id", ""))
        if eid not in out and behaviour_trigger(trig, entry, share.get(trig, 0)):
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
