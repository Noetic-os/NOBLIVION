# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay a guard log with the quarantine rule; count the denies it keeps and drops (NOBLIVION-155).

The rule (``hooks/guard_quarantine.py``): a deny rule with 3 or more overrides and
an override rate of 50 percent or more in 14 days shows as a row only. This tool
walks the ``deny`` and ``override`` lines of a guard log in time order and
asks, at each deny, whether the rule would have been quarantined by the lines
before it. It counts only what the rule itself would have seen: after a deny is
dropped, the override that followed it would not have happened either.

A deny is *overridden* when an ``override`` line of the same rule, session and agent
follows it within ``--pair-minutes``. That is the pointless deny: the model decided
the rule did not apply. Every other deny is a *true* deny. The report is:

* ``denies``, ``overridden``, ``true``: the log as it is;
* ``kept`` / ``dropped``, each split in overridden and true: the replay;
* ``overridden_dropped_share`` and ``true_kept_share``: the two numbers that decide;
* ``by_rule_over_the_whole_log``: the rules that the log shows as overridden most of the
  time (3 or more overrides, rate of 50 percent or more over the whole log) against all
  other rules, each with its denies and the denies the replay dropped. The aim: drop the
  denies of the first kind, keep every deny of the second;
* ``rules``: per rule (named ``rule_1`` ... by rank, never by id) its denies, overrides,
  dropped denies and the day it was first quarantined.

The report holds counts only: no command text, no rule id.

Usage::

    python tools/replay_guard_quarantine.py ~/.claude/plugins/data/<data dir>/guard-log.jsonl
"""

from __future__ import annotations

import argparse
import calendar
import importlib.util
import json
import sys
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def _quarantine() -> Any:
    spec = importlib.util.spec_from_file_location(
        "replay_guard_quarantine_mod", HOOKS / "guard_quarantine.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("guard_quarantine")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _epoch(ts: str) -> float:
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))


def read(paths: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in paths:
        for path in (Path(p + ".1"), Path(p)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("decision") in ("deny", "override") and isinstance(
                        rec.get("ids"), list
                    ):
                        rec["_t"] = _epoch(str(rec["ts"]))
                        rows.append(rec)
                except (ValueError, KeyError, TypeError):
                    continue
    rows.sort(key=lambda r: r["_t"])
    return rows


def replay(
    rows: list[dict[str, Any]],
    days: int | None = None,
    min_overrides: int | None = None,
    rate: float | None = None,
    pair_minutes: float = 30.0,
) -> dict[str, Any]:
    q = _quarantine()
    days = q.WINDOW_DAYS if days is None else days
    need = q.MIN_OVERRIDES if min_overrides is None else min_overrides
    share = q.MIN_RATE if rate is None else rate
    window = days * 86400.0
    gap = pair_minutes * 60.0

    # 1. pair each deny with the first later override of the same rule, session and agent
    overridden: set[tuple[int, str]] = set()  # (row index, rule id) of an overridden deny
    open_denies: dict[tuple[str, str, str], deque[int]] = defaultdict(deque)
    pair: dict[tuple[int, str], int] = {}  # (override row, rule) -> deny row
    for i, r in enumerate(rows):
        for rid in map(str, r["ids"]):
            if q.exempt(rid):
                continue
            key = (str(r.get("session_id")), str(r.get("agent_id")), rid)
            if r["decision"] == "deny":
                open_denies[key].append(i)
            else:
                dq = open_denies[key]
                while dq and r["_t"] - rows[dq[0]]["_t"] > gap:
                    dq.popleft()
                if dq:
                    d = dq.popleft()
                    overridden.add((d, rid))
                    pair[(i, rid)] = d

    # 2. replay: a rule sees only the events that the replay itself let through
    seen: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
    dropped_rows: set[tuple[int, str]] = set()
    out: dict[str, int] = defaultdict(int)
    per: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
    first_q: dict[str, str] = {}
    for i, r in enumerate(rows):
        t = r["_t"]
        for rid in map(str, r["ids"]):
            if q.exempt(rid):
                continue
            ev = seen[rid]
            while ev and t - ev[0][0] > window:
                ev.popleft()
            if r["decision"] == "override":
                d = pair.get((i, rid))
                if d is not None and (d, rid) in dropped_rows:
                    continue  # its deny never happened, so the override never happened
                ev.append((t, "override"))
                per[rid]["overrides"] += 1
                continue
            denies = sum(1 for _t, k in ev if k == "deny")
            overrides = sum(1 for _t, k in ev if k == "override")
            kind = "overridden" if (i, rid) in overridden else "true"
            out["denies"] += 1
            out[kind] += 1
            per[rid]["denies"] += 1
            if overrides >= need and overrides / max(denies, overrides, 1) >= share:
                dropped_rows.add((i, rid))
                out["dropped"] += 1
                out[f"dropped_{kind}"] += 1
                per[rid]["dropped"] += 1
                first_q.setdefault(rid, str(r["ts"])[:10])
            else:
                ev.append((t, "deny"))
                out["kept"] += 1
                out[f"kept_{kind}"] += 1
    ranked = sorted(per, key=lambda k: (-per[k]["denies"], k))
    # By hindsight, on the whole log (the way the ticket counts them): a rule that
    # the model overrides most of the time, and every other rule. The replay should
    # drop the denies of the first kind and keep every deny of the second.
    real: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        for rid in map(str, r["ids"]):
            real[rid][0 if r["decision"] == "deny" else 1] += 1
    mostly = {
        k
        for k, (d, o) in real.items()
        if not q.exempt(k) and o >= need and o / max(d, o, 1) >= share
    }
    cls = {"overridden_rules": 0, "other_rules": 0}
    for k in per:
        kind = "overridden_rules" if k in mostly else "other_rules"
        cls[f"{kind}_denies"] = cls.get(f"{kind}_denies", 0) + per[k]["denies"]
        cls[f"{kind}_dropped"] = cls.get(f"{kind}_dropped", 0) + per[k]["dropped"]
        cls[kind] += 1
    report: dict[str, Any] = {
        "denies": out["denies"],
        "overridden": out["overridden"],
        "true": out["true"],
        "kept": out["kept"],
        "kept_overridden": out["kept_overridden"],
        "kept_true": out["kept_true"],
        "dropped": out["dropped"],
        "dropped_overridden": out["dropped_overridden"],
        "dropped_true": out["dropped_true"],
        "overridden_dropped_share": round(out["dropped_overridden"] / out["overridden"], 3)
        if out["overridden"]
        else None,
        "true_kept_share": round(out["kept_true"] / out["true"], 3) if out["true"] else None,
        "by_rule_over_the_whole_log": cls,
        "rules": {
            f"rule_{n}": {
                "denies": per[k]["denies"],
                "overrides": per[k]["overrides"],
                "dropped": per[k]["dropped"],
                "first_quarantined": first_q.get(k, ""),
            }
            for n, k in enumerate(ranked, 1)
        },
    }
    if rows:
        report["days_in_log"] = round((rows[-1]["_t"] - rows[0]["_t"]) / 86400.0, 1)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+", help="guard-log.jsonl (the .1 file is read too)")
    ap.add_argument("--days", type=int)
    ap.add_argument("--min-overrides", type=int)
    ap.add_argument("--rate", type=float)
    ap.add_argument("--pair-minutes", type=float, default=30.0)
    args = ap.parse_args(argv)
    rows = read(args.logs)
    report = replay(rows, args.days, args.min_overrides, args.rate, args.pair_minutes)
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
