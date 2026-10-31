# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the guard rewrite on past Bash commands (NOBLIVION-122).

The rewrite changes what a command does, so it is measured before a switch is
turned on. This tool reads every Bash tool call of the given transcripts and
runs each command through the same code as the guard hook (``guard_hook.plan_act``
with the rewrite switch on), against a table of candidate rules. It reports:

* ``commands`` and ``unique``: Bash calls read, and the number of different commands;
* per rule: ``hits`` (the ``violates`` regex fired), ``rewrites`` (the hook would
  rewrite), and the reasons for the others (``skipped``);
* ``not_idempotent``: rewrites to which the rewrite applies again (must be 0);
* ``precision``, when ``--labels`` is given (see below).

The candidate rules are the built-in ones below (generic, no memory text), or the
entries with a ``rewrite`` field of a real guard table (``--table PATH``). The report
holds counts only: no command, no rule text, and a rule is named by its id.

Hand check. ``--sample N --sample-out PATH`` writes N distinct rewrites (the command
before and after, one JSON object per line) to a private file. It holds command
text, so keep it private. A person reads it and writes one label per line to
a second file: ``T`` (a true match: the command is a real run of the program, the
rule applies and the rewrite is safe and wanted), ``F`` (a false match), or
``?`` (cannot tell; counts as false). ``--labels PATH`` then prints the counts,
the precision and the Wilson 95 percent interval.

Usage::

    python tools/replay_guard_rewrite.py "~/.claude/projects/*/*.jsonl" \\
        --sample 40 --sample-out /private/sample.jsonl
    python tools/replay_guard_rewrite.py "~/.claude/projects/*/*.jsonl" \\
        --sample-from /private/sample.jsonl --labels /private/labels.txt
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import random
import sys
import types
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent.parent / "hooks"

# Candidate rules: a regex over one shell segment, a rewrite, a compliant form.
_QUIET_OFF = r"(?!.*(?:\s-[A-Za-z]*[qvh]|--quiet|--verbose|--help|--version|--collect-only))"
BUILTIN_RULES: list[dict[str, str]] = [
    {
        "id": f"demo_{name}_quiet",
        "violates": rf"^{regex}\b{_QUIET_OFF}",
        "rewrite": f"add-flag -q after {prefix}",
        "complies": rf"^{regex}\s+-q\b",
    }
    # One rule for pytest: the deny and the rewrite read ``python3 -m pytest``,
    # ``.venv/bin/python -m pytest``, ``uv run pytest`` and ``.venv/bin/pytest`` as
    # ``pytest`` (NOBLIVION-155), so a rule per form would hit twice and rewrite nothing.
    for name, regex, prefix in (("pytest", "pytest", "pytest"),)
] + [
    {
        "id": "demo_log_oneline",
        "violates": (
            r"^git\s+log\b(?!.*(?:--oneline|--pretty|--format|--stat|--graph|\s-p\b|\s-s\b))"
        ),
        "rewrite": "add-flag --oneline after git log",
        "complies": r"^git\s+log\s+--oneline\b",
    },
    {
        "id": "demo_status_short",
        "violates": (
            r"^git\s+status\b(?!.*(?:--short|\s-[A-Za-z]*[sb]|--porcelain|--branch|--long))"
        ),
        "rewrite": "add-flag -sb after git status",
        "complies": r"^git\s+status\s+-sb\b",
    },
]


def _load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"replay_rw_{name}", HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def commands(paths: Sequence[str]) -> Iterator[str]:
    """Every Bash command of the transcripts (one JSON object per line)."""
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"Bash"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    ti = block.get("input")
                    cmd = ti.get("command") if isinstance(ti, dict) else None
                    if block.get("name") == "Bash" and isinstance(cmd, str) and cmd.strip():
                        yield cmd[:20000]


def table_from(args: argparse.Namespace, gt: Any) -> dict[str, Any]:
    if args.table:
        table = gt.load_table(Path(args.table))
        table["entries"] = [e for e in table.get("entries", []) if e.get("rewrite")]
        return table
    entries = [
        {"rule": "", "apply": "", "scope": "tool", "triggers": [], "run_first": "", **r}
        for r in BUILTIN_RULES
    ]
    return {"version": 1, "entries": entries}


def run(args: argparse.Namespace) -> dict[str, Any]:
    gh, gt = _load("guard_hook"), _load("guard_table")
    rw = _load("guard_rewrite")
    table = table_from(args, gt)
    env = {
        "NOBLIVION_GUARD_REWRITE": "1",
        "NOBLIVION_CONFIG": os.devnull,
        "NOBLIVION_DATA_DIR": args.scratch,
    }
    paths = sorted(
        {p for pat in args.patterns for p in glob.glob(os.path.expanduser(pat), recursive=True)}
    )
    seen: dict[str, tuple[list[str], str, str]] = {}  # command -> (hit ids, outcome, new)
    total = 0
    per_rule: dict[str, Counter] = {}
    for cmd in commands(paths):
        total += 1
        if cmd not in seen:
            seen[cmd] = judge(gh, gt, table, env, cmd)
        ids, outcome, _new = seen[cmd]
        for i in ids:
            per_rule.setdefault(i, Counter())["hits"] += 1
            per_rule[i][outcome] += 1
    # Counts are per call (a repeated command counts each time); the sample is per command.
    rewrites = {c: v for c, v in seen.items() if v[1] == "rewrite"}
    not_idem = 0
    for ids, _o, new in rewrites.values():
        spec = next((e.get("rewrite") for e in table["entries"] if e["id"] == ids[0]), "")
        if rw.apply(new, spec) is not None:
            not_idem += 1
    forms: dict[str, Counter] = {}
    for cmd, (ids, outcome, _new) in seen.items():
        if ids:
            f = forms.setdefault(launcher_form(gt, cmd), Counter())
            f["unique_hits"] += 1
            if outcome == "rewrite":
                f["unique_rewrites"] += 1
    report: dict[str, Any] = {
        "transcripts": len(paths),
        "commands": total,
        "unique": len(seen),
        "hit_commands": sum(1 for v in seen.values() if v[0]),
        "rewrite_commands": len(rewrites),
        "not_idempotent": not_idem,
        "rules": {k: dict(sorted(v.items())) for k, v in sorted(per_rule.items())},
        "forms": {k: dict(sorted(v.items())) for k, v in sorted(forms.items())},
    }
    if args.sample and args.sample_out:
        # "rewrite": the rewrites. "hit": every command the violates regex fired on
        # (the stage before the rewrite), with the outcome of the rewrite step.
        pool = rewrites if args.sample_of == "rewrite" else {c: v for c, v in seen.items() if v[0]}
        rng = random.Random(args.seed)
        pick = rng.sample(sorted(pool), min(args.sample, len(pool)))
        with open(args.sample_out, "w", encoding="utf-8") as out:
            os.chmod(args.sample_out, 0o600)
            for cmd in pick:
                ids, outcome, new = pool[cmd]
                row = {"id": ids[0], "outcome": outcome, "before": cmd, "after": new}
                out.write(json.dumps(row) + "\n")
        report["sample_written"] = len(pick)
    if args.labels:
        report["labels"] = precision(Path(args.labels))
    return report


def launcher_form(gt: Any, cmd: str) -> str:
    """How the first simple command that the interpreter form changes launches its program:
    ``python -m``, ``uv run``, ``venv program``, or ``plain``. Counts only."""
    for seg in gt.command_segments(cmd):
        for v in gt.variants(seg):
            words = v.split()
            if gt.launcher_skip(words):
                return "uv run" if words[0].rsplit("/", 1)[-1] == "uv" else "python -m"
            if gt.interpreter_form(v):
                return "venv program"
    return "plain"


def judge(gh: Any, gt: Any, table: dict[str, Any], env: dict[str, str], cmd: str):
    """``(hit ids, outcome, new command)``. Outcome: ``no-hit``, ``rewrite`` or the
    reason it was skipped."""
    hits = gt.match(cmd, table)
    if not hits:
        return [], "no-hit", ""
    ids = [str(h["id"]) for h in hits]
    plan = gh.plan_act(cmd, {"command": cmd}, hits, table, env)
    if plan.final is not None:
        new = json.loads(plan.final.out)["hookSpecificOutput"]["updatedInput"]["command"]
        return ids, "rewrite", new
    why = next(
        (x[2].get("why", "skipped") for x in plan.also if x[0] == "rewrite-skipped"), "skipped"
    )
    return ids, why, ""


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def precision(path: Path) -> dict[str, Any]:
    labels = [x.strip().upper() for x in path.read_text().splitlines() if x.strip()]
    true = sum(1 for x in labels if x == "T")
    low, high = wilson(true, len(labels))
    return {
        "n": len(labels),
        "true": true,
        "false": len(labels) - true,
        "precision": round(true / len(labels), 3) if labels else 0.0,
        "wilson95": [round(low, 3), round(high, 3)],
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("patterns", nargs="*", help="transcript globs (JSON lines)")
    ap.add_argument("--table", help="a guard table file: use its entries that carry a rewrite")
    ap.add_argument(
        "--sample", type=int, default=0, help="write N distinct rewrites to --sample-out"
    )
    ap.add_argument("--sample-out", help="private file for the sample (holds command text)")
    ap.add_argument("--sample-of", choices=("rewrite", "hit"), default="rewrite")
    ap.add_argument("--seed", type=int, default=122)
    ap.add_argument("--labels", help="file with one label (T, F or ?) per sample line")
    ap.add_argument(
        "--scratch", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "noblivion-replay-rw")
    )
    args = ap.parse_args(argv)
    if not args.patterns and not args.labels:
        ap.error("give transcript globs or --labels")
    if not args.patterns:
        print(json.dumps({"labels": precision(Path(args.labels))}, indent=2))
        return 0
    os.makedirs(args.scratch, exist_ok=True)
    print(json.dumps(run(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
