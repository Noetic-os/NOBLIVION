# SPDX-License-Identifier: AGPL-3.0-or-later
"""Commands of the long-run test.

    python -m longtest.cli init   --run-dir DIR --repo REPO --b0 COMMIT --b1 COMMIT
    python -m longtest.cli freeze --run-dir DIR
    python -m longtest.cli pilot  --run-dir DIR [--sizes 30,200] [--shards 2] [--limit 6]
    python -m longtest.cli run    --run-dir DIR [--soak-size 200] [--max-seconds N]
    python -m longtest.cli status --run-dir DIR
    python -m longtest.cli stop   --run-dir DIR
    python -m longtest.cli labels baseline --heldout DIR
    python -m longtest.cli report --run-dir DIR
    python -m longtest.cli export --run-dir DIR --out DIR
    python -m longtest.cli units  --run-dir DIR

``tools`` must be on ``PYTHONPATH``. ``units`` prints the text of the systemd user
units and writes them under the run folder; it installs nothing. ``run`` starts
the 7-day loop and is for the operator after decision D8; ``pilot`` runs minutes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import labels, replay, report, sandbox
from .config import PARAMS, SCALES
from .runner import (
    Run,
    changed_files,
    freeze,
    init_run,
    replay_all,
    replay_rows,
    snapshot_tree,
    zero_token_gate,
)
from .util import TOOLS, json_text, write_private


def parse_sizes(text: str) -> list[int]:
    sizes = [int(p) for p in text.split(",") if p]
    bad = [s for s in sizes if s not in SCALES.values()]
    if bad:
        raise SystemExit(f"unknown sizes {bad}; the sizes are {sorted(SCALES.values())}")
    return sizes


def cmd_init(a: argparse.Namespace) -> int:
    doc = init_run(
        Path(a.run_dir),
        Path(a.repo),
        a.b0,
        a.b1,
        parse_sizes(a.sizes),
        Path(a.live_config),
        Path(a.live_data),
    )
    print(json.dumps({k: doc[k] for k in ("run_id", "builds", "sizes")}, indent=1))
    return 0


def cmd_freeze(a: argparse.Namespace) -> int:
    doc = freeze(Run(Path(a.run_dir)))
    print(
        json.dumps(
            {
                "frozen": doc["frozen"],
                "prereg_sha256": doc["prereg_sha256"],
                "leakage": doc["leakage"],
            },
            indent=1,
        )
    )
    return 0 if doc["frozen"] else 1


def cmd_pilot(a: argparse.Namespace) -> int:
    """Minutes, not days: gates 2, 3, 4 and 5 of the plan on a scratch store."""
    run = Run(Path(a.run_dir))
    manifest = run.manifest
    live_cfg, live_data = Path(manifest["live"]["config_dir"]), Path(manifest["live"]["data_dir"])
    before = snapshot_tree(live_cfg / "projects"), snapshot_tree(live_data)
    from .buildrunner import store_python

    python = store_python(live_data)
    gate2 = zero_token_gate(run, python)
    site = run.dir / "private" / "guard-site"
    sizes = parse_sizes(a.sizes)
    infos = (
        []
        if a.reuse
        else replay_all(
            run,
            sizes,
            a.shards,
            a.limit,
            parallel=a.parallel,
            day=0,
            guard_site=site,
            python=python,
            wait_s=a.wait,
        )
    )
    rows = [r for r in replay_rows(run, 0)]
    summary = replay.summarize(rows)
    summary["char_ratio"] = report.char_ratio(rows)
    after = snapshot_tree(live_cfg / "projects"), snapshot_tree(live_data)
    # The live store writes its own files while the pilot runs; the pilot's own
    # claim is that the files it could touch (memory folders) did not change.
    moved = changed_files(before[0], after[0])
    gate3 = sanity_gate(rows)
    fired = channels_fired(rows)
    doc = {
        "gate2_zero_tokens": {
            **gate2,
            "external_connects_in_children": sandbox.count_refused(
                run.dir / "private" / "external_connects.log"
            ),
        },
        "gate3_sanity": gate3,
        "gate4_channels_fired": fired,
        "gate5_isolation": {"live_memory_files_changed": len(moved)},
        "tasks": infos,
        "summary": summary,
    }
    write_private(run.dir / "pilot.json", json_text(doc))
    print(json.dumps(brief(doc), indent=1, sort_keys=True))
    return 0


def sanity_gate(rows: Sequence[dict]) -> dict:
    """Gate 3: V1 pull delivery of 0.95 or more for B0 and B1, S2 of 0.9 or more."""
    out = {}
    for build in ("B0", "B1", "S2"):
        sel = [
            r
            for r in rows
            if r["variant"] == "V1"
            and r["build"] == build
            and (r["channel"] == "pull" or build == "S2")
        ]
        hits = sum(1 for r in sel if r["hit"])
        out[build] = {"n": len(sel), "delivery": round(hits / len(sel), 3) if sel else None}
    out["ok"] = all(
        out[b]["delivery"] is not None
        and out[b]["delivery"]
        >= (PARAMS.sanity_s2_delivery if b == "S2" else PARAMS.sanity_pull_delivery)
        for b in ("B0", "B1", "S2")
    )
    return out


def channels_fired(rows: Sequence[dict]) -> dict:
    """Gate 4: per variant and build, did the channel deliver at least once on its own
    trigger. A channel that never fires is a product result (cause I or D)."""
    cells: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["build"] not in ("B0", "B1") or r.get("expect") in replay.NEGATIVE:
            continue
        if r["variant"] == "BENIGN":
            continue
        cell = cells.setdefault(f"{r['variant']}:{r['channel']}", {"B0": 0, "B1": 0, "n": 0})
        cell[r["build"]] += 1 if r.get("hit") else 0
        cell["n"] += 1 if r["build"] == "B1" else 0
    return cells


def brief(doc: dict) -> dict:
    s = doc["summary"]
    return {
        "gate2": {
            k: doc["gate2_zero_tokens"][k]
            for k in ("ok", "public_connect_refused", "refused_logged", "claude_processes")
        },
        "gate3": doc["gate3_sanity"],
        "gate5": doc["gate5_isolation"],
        "tasks": [
            (
                t["build"],
                t["size"],
                t["shard"],
                t["memories"],
                t["index_s"],
                t["ready_s"],
                t["seconds"],
            )
            for t in doc["tasks"]
        ],
        "false_delivery": s["false_delivery_v5"],
        "false_deny": s["false_deny"],
        "cost": s["cost"],
        "mrr_v2_pull": s["mrr_v2_pull"],
        "rates": s["rates"],
    }


def cmd_run(a: argparse.Namespace) -> int:
    from .loop import Loop

    run = Run(Path(a.run_dir))
    if not run.manifest.get("frozen"):
        raise SystemExit("the run is not frozen: run `freeze` first")
    loop = Loop(run, a.soak_size)
    loop.run_forever(a.max_seconds)
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from .runner import installed_version

    run = Run(Path(a.run_dir))
    live_data = Path(run.manifest["live"]["data_dir"])
    res = report.status(
        run.dir, dt.datetime.now().astimezone(), plugin_version=lambda: installed_version(live_data)
    )
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


def cmd_stop(a: argparse.Namespace) -> int:
    write_private(Path(a.run_dir) / "stop", "stop\n")
    return 0


def cmd_labels(a: argparse.Namespace) -> int:
    if a.action == "baseline":
        print(json.dumps(labels.dev_baseline(Path(a.heldout)), indent=1, sort_keys=True))
        return 0
    raise SystemExit(f"unknown labels action {a.action}")


def cmd_report(a: argparse.Namespace) -> int:
    run = Run(Path(a.run_dir))
    rows = replay_rows(run)
    summary = replay.summarize(rows)
    summary["char_ratio"] = report.char_ratio(rows)
    doc = report.verdicts(summary, None, None, None)
    write_private(run.dir / "final_report.json", json_text({"summary": summary, "verdicts": doc}))
    print(json.dumps(doc["scales"], indent=1))
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    print("\n".join(report.export(Path(a.run_dir), Path(a.out))))
    return 0


def cmd_units(a: argparse.Namespace) -> int:
    run = Path(a.run_dir)
    units = sandbox.render_units(run, sys.executable, TOOLS)
    target = run / "units"
    target.mkdir(mode=0o700, exist_ok=True)
    for name, text in units.items():
        write_private(target / name, text)
        print(f"# {name}\n{text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="longtest", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn, **kw):  # type: ignore[no-untyped-def]
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(func=fn)
        return sp

    s = add("init", cmd_init)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--b0", required=True)
    s.add_argument("--b1", required=True)
    s.add_argument("--sizes", default="30,200,1000,5000,20000")
    s.add_argument("--live-config", default=str(Path.home() / ".claude"))
    s.add_argument(
        "--live-data",
        default=str(Path.home() / ".claude" / "plugins" / "data" / "noblivion-noblivion"),
    )
    s = add("freeze", cmd_freeze)
    s.add_argument("--run-dir", required=True)
    s = add("pilot", cmd_pilot)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--sizes", default="30,200")
    s.add_argument("--shards", type=int, default=2)
    s.add_argument("--limit", type=int, default=6)
    s.add_argument("--parallel", type=int, default=2)
    s.add_argument("--wait", type=float, default=900.0)
    s.add_argument("--reuse", action="store_true", help="analyse the rows of day 0 as they are")
    s = add("run", cmd_run)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--soak-size", type=int, default=20000)
    s.add_argument("--max-seconds", type=float, default=None)
    for name, fn in (
        ("status", cmd_status),
        ("stop", cmd_stop),
        ("report", cmd_report),
        ("units", cmd_units),
    ):
        add(name, fn).add_argument("--run-dir", required=True)
    s = add("export", cmd_export)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--out", required=True)
    s = add("labels", cmd_labels)
    s.add_argument("action", choices=["baseline"])
    s.add_argument("--heldout", required=True)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
