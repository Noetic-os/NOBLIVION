# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count how often the recall nudge would fire on past sessions (NOBLIVION-123).

The nudge (``hooks/recall_nudge.py``) adds one line before a ticket tool or a merge command when
the session made no recall call in its last 10 tool calls. This tool reads the tool calls of past
Claude Code transcripts and applies the hook's own code to them: the same patterns (from the
config of this process, ``NOBLIVION_CONFIG``, or ``--config PATH``), the same command lexer and
the same window rule (``window_verdict``). Nothing is written and no hook runs. The report holds
counts only: no command, no topic, no session id.

Report fields:

* ``sessions``: transcripts read (subagent transcripts are left out, as the hook leaves out
  subagent calls); ``sessions_with_recall``: sessions with at least one recall call;
  ``recall_calls`` and ``fetch_calls``: recall calls, and those that read one memory by id;
* per moment (``tool`` for an MCP ticket tool, ``command`` for a merge command): ``matches``
  (calls that match), ``fire`` (the nudge would be printed), ``recent_recall`` (the session
  recalled within its last 10 calls: no line), ``cooldown`` (a line was printed for an earlier
  call within the last 10 calls), and ``followed`` (a recall call came within the next 10 tool
  calls after a line: the share of lines that a recall call followed anyway, with no live
  effect measured);
* ``sessions_with_match`` and ``sessions_with_fire``;
* ``nudge_chars`` and ``nudge_tokens_est``: the characters of all lines, and that number over 4.

This is a replay of the past, in which the model never saw a nudge. It gives the number of lines
a live week would print, and a base rate. It does not show whether a line makes the model call
the tool: that needs a live week with the switch on (see docs/recall-reflex.md).

Usage::

    python tools/replay_recall_nudge.py "~/.claude/projects/*/*.jsonl" --since-days 30 --json
    python tools/replay_recall_nudge.py "~/.claude/projects/*/*.jsonl" --config my-config.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import importlib.util
import json
import os
import sys
from collections import Counter, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
WINDOW = 10


def _load_nudge() -> Any:
    spec = importlib.util.spec_from_file_location("recall_nudge", HOOKS / "recall_nudge.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["recall_nudge"] = mod
    spec.loader.exec_module(mod)
    return mod


def tool_calls(path: str) -> Iterator[tuple[str, str, dict[str, Any], str, str]]:
    """``(id, name, input, timestamp, git branch)`` of each main-thread tool call, in file order."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"tool_use"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (
                not isinstance(rec, dict)
                or rec.get("isSidechain")
                or rec.get("type") != "assistant"
            ):
                continue
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else ""
            branch = rec.get("gitBranch") if isinstance(rec.get("gitBranch"), str) else ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tin = block.get("input")
                    yield (
                        str(block.get("id") or ""),
                        str(block.get("name") or ""),
                        tin if isinstance(tin, dict) else {},
                        ts,
                        branch,
                    )


def replay_session(nudge: Any, path: str, cutoff: str, env: dict[str, str]) -> dict[str, Any]:
    """The counts of one transcript. A call before ``cutoff`` feeds the window, not the counts."""
    prior: deque[tuple[str, str]] = deque(maxlen=WINDOW)  # newest first
    last_nudged = ""
    pending: list[list[Any]] = []  # [moment, calls left]
    counts: Counter[str] = Counter()
    chars = 0
    for ident, name, tin, ts, branch in tool_calls(path):
        counted = not cutoff or ts >= cutoff
        for entry in pending:
            entry[1] -= 1
        is_recall = name.endswith(nudge.RECALL_SUFFIX)
        if is_recall:
            if counted:
                counts["recall_calls"] += 1
                counts["fetch_calls"] += 1 if tin.get("fetch_id") else 0
            for entry in pending:
                if entry[1] >= 0 and not entry[2]:
                    entry[2] = True
                    counts[entry[0] + ".followed"] += 1
        else:
            hit = nudge.classify({"tool_name": name, "tool_input": tin}, env)
            if hit is not None:
                moment, variant, match, tool_input = hit
                verdict = nudge.window_verdict(list(prior), last_nudged)
                if verdict == "fire":
                    last_nudged = ident  # an earlier call keeps the state, counted or not
                if counted:
                    counts[moment + ".matches"] += 1
                    counts[moment + "." + verdict] += 1
                    counts["session_match"] = 1
                    if verdict == "fire":
                        topic = nudge.topic_for(moment, tool_input, variant, match, branch)
                        chars += len(nudge.nudge_line(topic))
                        counts["fires"] += 1
                        counts["session_fire"] = 1
                        pending.append([moment, WINDOW, False])
        pending = [e for e in pending if e[1] > 0]
        prior.appendleft((ident, name))
    counts["chars"] = chars
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pattern", nargs="+", help="glob(s) of transcript files")
    ap.add_argument("--since-days", type=float, default=0.0, help="count only calls this recent")
    ap.add_argument("--config", help="a NOBLIVION config file (default: the process settings)")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)
    if args.config:
        os.environ["NOBLIVION_CONFIG"] = os.path.expanduser(args.config)
    nudge = _load_nudge()
    env = dict(os.environ)
    cutoff = ""
    if args.since_days > 0:
        cutoff = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=args.since_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
    files = sorted(
        {
            f
            for pat in args.pattern
            for f in glob.glob(os.path.expanduser(pat), recursive=True)
            if "/subagents/" not in f
        }
    )
    total: Counter[str] = Counter()
    sessions = with_recall = with_match = with_fire = 0
    for path in files:
        c = replay_session(nudge, path, cutoff, env)
        sessions += 1
        with_recall += 1 if c.get("recall_calls") else 0
        with_match += c.pop("session_match", 0)
        with_fire += c.pop("session_fire", 0)
        total.update(c)
    report: dict[str, Any] = {
        "sessions": sessions,
        "sessions_with_recall": with_recall,
        "recall_calls": total["recall_calls"],
        "fetch_calls": total["fetch_calls"],
        "sessions_with_match": with_match,
        "sessions_with_fire": with_fire,
        "fires": total["fires"],
        "nudge_chars": total["chars"],
        "nudge_tokens_est": round(total["chars"] / 4),
        "moments": {
            m: {
                k: total[m + "." + k]
                for k in ("matches", "fire", "recent_recall", "cooldown", "followed")
            }
            for m in ("tool", "command")
        },
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
