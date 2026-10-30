# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay past prompts through the prompt hook and measure what it injects (NOBLIVION-121).

``replay_transcripts.py`` replays sessions to make trust data. This tool answers
a smaller question: how much text does the prompt hook add to each prompt, with
one set of settings or another? For each *arm* (a name and a list of
``NAME=VALUE`` settings) it runs every prompt of the chosen transcripts through
``hooks/recall_hook.py`` ``main`` in this process, with a fresh cache folder, the
replay clock (so the 14-day hub window sees the original dates) and a running
store. It does not write trust events and does not touch the live data dir.

Per arm the report holds:

* ``chars_per_prompt``: the mean number of characters the hook printed, label
  rows included, and the median and the 95th percentile;
* ``rows_per_prompt``: the mean number of index rows and label rows;
* ``hub_share``: the share of shown rows (once per session and note) that are
  hub notes, and ``top_notes``: the notes shown in the most sessions, each with
  the share of sessions. A hub note is one shown in more than half of the
  sessions of the *first* arm (the baseline).

Usage, with a store of a scratch data dir running over a copy of the memory
folder (see ``replay_transcripts.py`` for the set-up)::

    export NOBLIVION_DATA_DIR=/tmp/replay-data
    python tools/replay_prompt_rows.py --memory-dir DIR --since-days 7 \\
        --arm before --arm "after:NOBLIVION_RECALL_RULES_ONLY=1,NOBLIVION_RECALL_PROMPT_ROWS=5" \\
        "~/.claude/projects/<project>/*.jsonl"

The settings of the process (``NOBLIVION_CONFIG`` and the env) are the base of
every arm. The report holds counts and note names, never prompt text. With
``--sample N --sample-out PATH`` the tool also writes N shown rows of the last
arm with the prompt that caused them, to label by hand: that file holds prompt
text, so keep it private.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import io
import json
import os
import random
import re
import statistics
import sys
import tempfile
import time
import types
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
HOOKS = TOOLS.parent / "hooks"
LABEL_HEADER = "Memory rows by subject (label rows)"
ROW_RE = re.compile(r"^- \[id (\d+)\] .*\((?P<name>[^()]*)\)$")
ANY_ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
LABEL_RE = re.compile(r"^- Memory (?P<name>\S+)")
TOP = 8


def _load(path: Path, alias: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def prompt_event(rt: types.ModuleType, rec: Mapping[str, Any], signals: Any) -> tuple | None:
    """``(text, kind)`` of a record Claude Code runs the prompt hook for, or None.
    ``typed`` is what ``prompt_text`` accepts: the prompts a person wrote. The hook
    also runs for a ``notification`` (a task notification sent as a user message)
    and for a ``queued`` prompt (one delivered as an attachment while the model
    was busy), so a cost count needs them too."""
    text = rt.prompt_text(rec, signals)
    if text is not None:
        return text, "typed"
    if rec.get("type") == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") == "queued_command":
            text = signals._text_blocks(att.get("prompt"))
            return (text, "queued") if isinstance(text, str) and text.strip() else None
        return None
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    text = signals._text_blocks((rec.get("message") or {}).get("content"))
    if isinstance(text, str) and text.lstrip().startswith("<task-notification"):
        return text, "notification"
    return None


def parse_arm(text: str) -> tuple[str, dict[str, str]]:
    """``name`` or ``name:VAR=VALUE,VAR=VALUE`` -> ``(name, settings)``."""
    name, _, rest = text.partition(":")
    settings: dict[str, str] = {}
    for item in [x for x in rest.split(",") if x.strip()]:
        key, eq, value = item.partition("=")
        if not eq or not key.strip():
            raise SystemExit(f"bad setting {item!r} in arm {text!r}: use NAME=VALUE")
        settings[key.strip()] = value.strip()
    if not name.strip():
        raise SystemExit(f"arm {text!r} has no name")
    return name.strip(), settings


def shown_names(output: str) -> list[str]:
    """The note names on the rows of one hook output: index rows and label rows.
    A row whose name holds parentheses still counts, under its id."""
    names: list[str] = []
    for line in output.splitlines():
        m = ROW_RE.match(line) or LABEL_RE.match(line)
        if m:
            names.append(m.group("name"))
        elif ANY_ROW_RE.match(line):
            names.append("id-" + ANY_ROW_RE.match(line).group("id"))
    return names


def percentile(values: Sequence[int], share: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def summarize(
    per_prompt: Sequence[Mapping[str, Any]], hubs: set[str] | None = None
) -> dict[str, Any]:
    """The report of one arm from its prompts: ``{"session", "chars", "names"}``."""
    chars = [int(p["chars"]) for p in per_prompt]
    rows = [len(p["names"]) for p in per_prompt]
    sessions: dict[str, set[str]] = defaultdict(set)
    for p in per_prompt:
        sessions[p["session"]].update(p["names"])
    with_rows = [s for s, names in sessions.items() if names]
    seen: dict[str, int] = defaultdict(int)
    for s in with_rows:
        for name in sessions[s]:
            seen[name] += 1
    total = sum(seen.values())
    n = max(1, len(sessions))
    out: dict[str, Any] = {
        "prompts": len(per_prompt),
        "sessions": len(sessions),
        "prompts_with_rows": sum(1 for r in rows if r),
        "chars_per_prompt": round(statistics.fmean(chars), 1) if chars else 0.0,
        "chars_median": int(statistics.median(chars)) if chars else 0,
        "chars_p95": percentile(chars, 0.95),
        "chars_max": max(chars, default=0),
        "label_chars_per_prompt": round(
            statistics.fmean(int(p.get("label_chars", 0)) for p in per_prompt), 1
        )
        if per_prompt
        else 0.0,
        "rows_per_prompt": round(statistics.fmean(rows), 2) if rows else 0.0,
        "distinct_notes": len(seen),
        "top_notes": [
            {"name": name, "sessions": c, "share": round(c / n, 3)}
            for name, c in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
        ],
    }
    if hubs is not None:
        out["hub_share"] = (
            round(sum(c for k, c in seen.items() if k in hubs) / total, 3) if total else 0.0
        )
        out["hub_notes_max_session_share"] = round(
            max((seen.get(h, 0) for h in hubs), default=0) / n, 3
        )
    return out


def hub_notes(per_prompt: Sequence[Mapping[str, Any]], share: float = 0.5) -> set[str]:
    """Notes shown in more than ``share`` of the sessions."""
    sessions: dict[str, set[str]] = defaultdict(set)
    for p in per_prompt:
        sessions[p["session"]].update(p["names"])
    count: dict[str, int] = defaultdict(int)
    for names in sessions.values():
        for name in names:
            count[name] += 1
    n = max(1, len(sessions))
    return {k for k, c in count.items() if c / n > share}


def arm_env(
    base_env: Mapping[str, str], work: Path, arm_dir: Path, memory_dir: str
) -> dict[str, str]:
    """The env of a replay run: its own cache and guard files, never the live ones."""
    arm_dir.mkdir(parents=True, exist_ok=True)
    env = dict(base_env)
    env.update(
        {
            "NOBLIVION_RECALL_CACHE_DIR": str(arm_dir / "cache"),
            "NOBLIVION_GUARD_TABLE": str(work / "guard-table.json"),
            "NOBLIVION_GUARD_STATE_DIR": str(arm_dir / "guard-state"),
            "NOBLIVION_GUARD_LOG": str(arm_dir / "guard-log.jsonl"),
            "NOBLIVION_MEMORY_DIR": memory_dir,
        }
    )
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def filtered_hook(
    rt: types.ModuleType, hooks: Path, alias: str
) -> tuple[types.ModuleType, dict[str, Any]]:
    """The prompt hook and ``{"fn": exists}``: the hook gets the store's answer
    without the notes for which ``fn(file name)`` is false."""
    hook = _load(hooks / "recall_hook.py", alias)
    exists: dict[str, Any] = {"fn": lambda n: True}
    store_get = hook.store_get
    hook.store_get = lambda build, environ, timeout_s: rt.filter_payload(
        store_get(build, environ, timeout_s), exists["fn"]
    )
    return hook, exists


def run_arm(
    rt: types.ModuleType,
    paths: Sequence[str],
    base_env: Mapping[str, str],
    settings: Mapping[str, str],
    work: Path,
    name: str,
    memory_dir: str,
    births: Mapping[str, _dt.datetime] | None,
    every_event: bool = False,
) -> list[dict[str, Any]]:
    """Every prompt of ``paths`` through a fresh copy of the prompt hook. With
    ``every_event`` also the notifications and queued prompts (``prompt_event``);
    one text is one event, because a notification can be recorded twice."""
    env = arm_env(base_env, work, work / name, memory_dir)
    env.update(settings)
    clock = rt.Clock()
    hook, exists = filtered_hook(rt, HOOKS, f"replay_rows_hook_{name}")
    stats = hook._sibling_module("hub_stats")
    stats._dt = clock.module
    signals = hook._sibling_module("trust_signals")
    out: list[dict[str, Any]] = []
    for path in paths:
        sid = Path(path).stem
        seen: set[str] = set()
        for rec in rt.read_jsonl(path):
            hit = prompt_event(rt, rec, signals)
            if hit is None or (hit[1] != "typed" and not every_event):
                continue
            text, kind = hit
            moment = rt._iso(str(rec.get("timestamp") or ""))
            if moment is None or (kind != "typed" and text[:200] in seen):
                continue
            seen.add(text[:200])
            clock.now = moment
            if births is not None:
                exists["fn"] = lambda n, m=moment: n not in births or births[n] <= m
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}
            buf = io.StringIO()
            started = time.perf_counter()
            hook.main(io.StringIO(json.dumps(payload)), buf, dict(env))
            spent = time.perf_counter() - started
            shown = buf.getvalue().rstrip("\n")
            out.append(
                {
                    "session": sid,
                    "moment": moment,
                    "chars": len(shown),
                    "label_chars": len(shown) - shown.find(LABEL_HEADER)
                    if LABEL_HEADER in shown
                    else 0,
                    "names": shown_names(shown),
                    "ms": round(spent * 1000, 1),
                    "kind": kind,
                    "prompt": text,
                }
            )
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    rt = _load(TOOLS / "replay_transcripts.py", "replay_rows_transcripts")
    if args.runs_in:
        return report_of(rt, load_runs(args.runs_in), args)
    paths = rt.main_transcripts(args.transcripts)
    if args.since_days:
        limit = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=args.since_days)
        paths = [p for p in paths if (rt._iso(rt.first_ts(p)) or limit) >= limit]
    memory_dir = os.path.abspath(os.path.expanduser(args.memory_dir))
    births = None
    if not args.no_births:
        names = {p.name for p in Path(memory_dir).glob("*.md")}
        births = rt.note_births(rt.all_transcripts(paths), names)
    gt = _load(HOOKS / "guard_table.py", "replay_rows_guard_table")
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="replay-rows-"))
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["NOBLIVION_GUARD_TABLE"] = str(work / "guard-table.json")
    folders = gt.source_dirs(None, dict(env, NOBLIVION_MEMORY_DIR=memory_dir))
    gt.rebuild(folders, gt.table_path(folders[0], env))
    arms = [parse_arm(a) for a in args.arm] or [("before", {})]
    report_from = None
    if args.report_days:
        newest = max((rt._iso(rt.first_ts(p)) for p in paths), default=None)
        if newest is not None:
            report_from = newest - _dt.timedelta(days=args.report_days)
    runs: dict[str, dict[str, Any]] = {}
    for name, settings in arms:
        prompts = run_arm(rt, paths, os.environ, settings, work, name, memory_dir, births)
        runs[name] = {"settings": settings, "prompts": prompts}
    if args.runs_out:
        save_runs(args.runs_out, runs)
    args.report_from = report_from
    args.transcript_count = len(paths)
    return report_of(rt, runs, args)


def save_runs(path: str, runs: Mapping[str, Mapping[str, Any]]) -> None:
    """The raw runs, without prompt text, for a later report."""
    doc = {
        name: {
            "settings": run["settings"],
            "prompts": [
                {
                    **{k: v for k, v in p.items() if k not in ("prompt", "moment")},
                    "moment": p["moment"].isoformat(),
                }
                for p in run["prompts"]
            ],
        }
        for name, run in runs.items()
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def load_runs(paths: Sequence[str]) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for name, run in doc.items():
            for p in run["prompts"]:
                p["moment"] = _dt.datetime.fromisoformat(p["moment"])
            runs[name] = run
    return runs


def report_of(
    rt: types.ModuleType, runs: Mapping[str, Mapping[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    """The report of the runs. The first arm is the baseline."""
    names = list(runs)
    report_from = getattr(args, "report_from", None)
    if report_from is None and args.report_days:
        newest = max((p["moment"] for r in runs.values() for p in r["prompts"]), default=None)
        if newest is not None:
            report_from = newest - _dt.timedelta(days=args.report_days)
    prompts = {
        n: [p for p in runs[n]["prompts"] if report_from is None or p["moment"] >= report_from]
        for n in names
    }
    hubs = hub_notes(prompts[names[0]])
    report: dict[str, Any] = {
        "transcripts": getattr(args, "transcript_count", None),
        "baseline_arm": names[0],
        "baseline_hub_notes": sorted(hubs),
        "arms": {
            n: {"settings": runs[n]["settings"], **summarize(prompts[n], hubs)} for n in names
        },
    }
    if args.sample and args.sample_out:
        rows = [(p["prompt"], n) for p in prompts[names[-1]] for n in p["names"] if "prompt" in p]
        random.Random(121).shuffle(rows)
        with open(args.sample_out, "w", encoding="utf-8") as fh:
            for prompt, note in rows[: args.sample]:
                fh.write(json.dumps({"note": note, "prompt": prompt[:1500]}) + "\n")
        report["sample_rows"] = min(args.sample, len(rows))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("transcripts", nargs="*", help="main transcript files or glob patterns")
    parser.add_argument("--memory-dir", help="the memory folder the store indexed")
    parser.add_argument("--arm", action="append", default=[], help="NAME or NAME:VAR=VAL,VAR=VAL")
    parser.add_argument("--since-days", type=float, default=0.0)
    parser.add_argument(
        "--report-days",
        type=float,
        default=0.0,
        help="report only the prompts of the last N days before the newest session starts; "
        "the earlier prompts still run (the hub window warms up on them)",
    )
    parser.add_argument("--runs-out", help="save the raw runs here (no prompt text)")
    parser.add_argument(
        "--runs-in", action="append", default=[], help="report saved runs instead of replaying"
    )
    parser.add_argument("--no-births", action="store_true", help="show notes before they exist")
    parser.add_argument("--work-dir", help="where the arms keep their cache folders")
    parser.add_argument("--sample", type=int, default=0, help="rows of the last arm to label")
    parser.add_argument("--sample-out", help="file for the sample (holds prompt text)")
    args = parser.parse_args(argv)
    if not args.runs_in and not args.transcripts:
        parser.error("give transcripts or --runs-in")
    if not args.runs_in and not args.memory_dir:
        parser.error("--memory-dir is required to replay")
    print(json.dumps(run(args), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
