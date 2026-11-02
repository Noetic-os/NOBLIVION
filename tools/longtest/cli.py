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

from . import config, labels, replay, report, sandbox
from .config import CLAIMS, PARAMS, SCALES
from .runner import (
    Run,
    archive_pilot_loop,
    bench_kept_store,
    changed_files,
    freeze,
    init_run,
    real_replay,
    replay_all,
    replay_rows,
    snapshot_tree,
    verify_frozen,
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
    if a.heldout:
        doc["heldout"] = str(Path(a.heldout).resolve())
        Run(Path(a.run_dir)).save_manifest(doc)
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
    before = live_memory_snapshot(live_cfg)
    from .buildrunner import store_python

    python = store_python(live_data)
    gate2 = zero_token_gate(run, python)
    log = run.dir / "private" / "external_connects.log"
    after_probe = sandbox.count_refused(log)
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
    after = live_memory_snapshot(live_cfg)
    # The live store writes its own files while the pilot runs; the pilot's own
    # claim is that the files it could touch (memory folders) did not change.
    moved = changed_files(before, after)
    gate3 = sanity_gate(rows)
    fired = channels_fired(rows)
    gate1 = freeze_gate(run)
    gate4 = claims_gate(rows, fired)
    gate6 = resources_gate(run, infos, a)
    gate7 = labels_gate(run, a)
    # the probe of this run is one try; anything after it came from a child
    gate2["external_connects_during_replay"] = sandbox.count_refused(log) - after_probe
    gate2["ok"] = bool(
        gate2["ok"]
        and gate2["public_connect_refused"]
        and not gate2["claude_processes"]
        and not gate2["external_connects_during_replay"]
    )
    gate5 = {"live_memory_files_changed": len(moved), "ok": not moved}
    loop_check = loop_gate(run, a) if a.loop_seconds else {"ok": False, "skipped": True}
    doc = {
        "gate1_freeze": gate1,
        "gate2_zero_tokens": gate2,
        "gate3_sanity": gate3,
        "gate4_channels_fired": fired,
        "gate4_claims": gate4,
        "gate5_isolation": gate5,
        "gate6_resources": gate6,
        "gate7_labels": gate7,
        "loop": loop_check,
        "tasks": infos,
        "summary": summary,
    }
    doc["gates"] = {
        "1": gate1["ok"],
        "2": gate2["ok"],
        "3": gate3["ok"],
        "4": gate4["ok"],
        "5": gate5["ok"],
        "6": gate6["ok"],
        "7": gate7["ok"],
        "loop": loop_check["ok"],
    }
    doc["all_gates"] = all(doc["gates"].values())
    write_private(run.dir / "pilot.json", json_text(doc))
    print(json.dumps(brief(doc), indent=1, sort_keys=True))
    return 0 if doc["all_gates"] else 1


def freeze_gate(run: Run) -> dict:
    """Gate 1: frozen manifest, hash of the pre-registration, no leakage, and the
    program that runs is the frozen copy."""
    doc = run.manifest
    problems = verify_frozen(run)
    leak = doc.get("leakage", {})
    return {
        "frozen": bool(doc.get("frozen")),
        "prereg_sha256": doc.get("prereg_sha256", ""),
        "answer_in_query": len(leak.get("answer_in_query", [])),
        "v2_overlap": len(leak.get("v2_overlap", [])),
        "problems": problems,
        "ok": bool(doc.get("frozen")) and not problems and bool(leak.get("ok")),
    }


def claims_gate(rows: Sequence[dict], fired: dict) -> dict:
    """Gate 4: each claim C1 to C24 has a recorded state in the pilot: fired in both
    builds, fired in B1 only, not fired (a product result, cause I or D, not tuned
    away), or carried by another arm or marked pending with its reason."""
    table: dict[str, dict] = {}
    for claim in CLAIMS:
        sel = [r for r in rows if claim in r.get("claims", []) and r["build"] in ("B0", "B1")]
        if not sel:
            note = replay.CLAIM_NOTES.get(claim, "no query in the pilot")
            table[claim] = {"state": "other arm or pending", "note": note}
            continue
        hit = {b: sum(1 for r in sel if r["build"] == b and r.get("hit")) for b in ("B0", "B1")}
        n = sum(1 for r in sel if r["build"] == "B1")
        state = "fired" if hit["B1"] else "not fired (product result)"
        table[claim] = {"state": state, "B0": hit["B0"], "B1": hit["B1"], "n": n}
    return {"claims": table, "ok": len(table) == len(CLAIMS)}


def resources_gate(run: Run, infos: Sequence[dict], a: argparse.Namespace) -> dict:
    """Gate 6: the index build time per build and size, the largest process of the
    pilot against the memory limit of the unit, free disk and the run folder size."""
    import resource
    import shutil

    index: dict[str, dict[str, float]] = {}
    for t in infos:
        index.setdefault(str(t["size"]), {})[t["build"]] = t["index_s"] + t["ready_s"]
    peak_gb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1e6
    bench = []
    for size in (s for s in parse_sizes(a.sizes) if s >= 1000):
        for build in ("B0", "B1"):
            got = bench_kept_store(run, build, size, a.bench_seconds)
            if got:
                run.append("latency", got)
                bench.append({k: got[k] for k in ("build", "size", "p50", "p95", "rss_mb")})
    free = shutil.disk_usage(run.dir).free / 1e9
    limit = float(config.PARAMS.memory_max.rstrip("G"))
    return {
        "index_ready_s": index,
        "peak_child_gb": round(peak_gb, 2),
        "memory_limit_gb": limit,
        "free_gb": round(free, 1),
        "run_folder_gb": round(report.run_dir_size_gb(run.dir), 3),
        "bench": bench,
        "ok": peak_gb < limit and free >= PARAMS.min_free_gb,
    }


def labels_gate(run: Run, a: argparse.Namespace) -> dict:
    """Gate 7: arm b on the dev prompts of the real set, and the new B1 pool pairs
    exported to ``private/`` for blind labels."""
    heldout = run.manifest.get("heldout")
    if not heldout:
        return {"ok": False, "reason": "no held-out set in the manifest"}
    levels = [lv for lv in (0, 1000) if lv == 0 or lv in parse_sizes(a.sizes)]
    done = real_replay(
        run,
        Path(heldout),
        levels,
        0,
        guard_site=run.dir / "private" / "guard-site",
        wait_s=a.wait,
    )
    done["ok"] = done["prompts"] > 0 and "pool_additions" in done
    return done


def loop_gate(run: Run, a: argparse.Namespace) -> dict:
    """The job loop for a few minutes on the soak store, then the daily check; the
    files of this loop are moved to ``pilot-loop/`` so that day 1 starts empty."""
    from .loop import Loop

    loop = Loop(run, a.soak_size)
    try:
        loop.run_forever(a.loop_seconds, tick_s=2.0)
    except SystemExit as exc:  # the start check refuses a dirty environment
        return {"ok": False, "error": str(exc)[:300]}
    result = run.dir / "pilot-loop-status.json"
    now = dt.datetime.now().astimezone()
    report.daily_summary(run.dir, now)
    res = report.status(run.dir, now, notify=None)
    write_private(result, json_text(res))
    rows = {p.stem: len(report.read_jsonl(p)) for p in sorted(run.out.glob("*.jsonl"))}
    soak_rows = report.read_jsonl(run.out / "soak.jsonl")
    kinds = sorted({r["kind"] for r in soak_rows})
    first = min((r for r in soak_rows if r["kind"] == "write"), key=lambda r: r["ts"], default={})
    archive_pilot_loop(run)
    return {
        "rows": rows,
        "soak_kinds": kinds,
        "lag_s": first.get("lag_s"),
        "status_failed": res["failed"],
        "status_notes": res["notes"],
        "ok": res["ok"],
    }


def live_memory_snapshot(config_dir: Path) -> dict:
    """File stats of what the program must not touch: the memory folders of every
    project, the settings file and the plugin lists. The session transcripts are left
    out, because live sessions write them all the time."""
    snap: dict = {}
    for folder in sorted((Path(config_dir) / "projects").glob("*/memory")):
        snap.update(snapshot_tree(folder))
    for name in (
        "settings.json",
        "plugins/installed_plugins.json",
        "plugins/known_marketplaces.json",
    ):
        snap.update(
            snapshot_tree(Path(config_dir) / name)
            if (Path(config_dir) / name).is_dir()
            else _stat_one(Path(config_dir) / name)
        )
    return snap


def _stat_one(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    return {str(path): (st.st_size, int(st.st_mtime_ns))}


def sanity_gate(rows: Sequence[dict]) -> dict:
    """Gate 3, at SMALL: V1 pull delivery of 0.95 or more for B0 and B1, S2 of 0.9 or
    more. SMALL is the smallest size in the rows; at larger sizes stock is meant to fail."""
    out: dict = {}
    sizes = [r["size"] for r in rows if r.get("size")]
    if sizes:
        rows = [r for r in rows if r.get("size") == min(sizes)]
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


def bench_call(worker, cwd: str):  # type: ignore[no-untyped-def]
    count = {"i": 0}

    def call(prompt: str) -> float:
        count["i"] += 1
        return worker.call("prompt", sid=f"bench-{count['i']}", cwd=cwd, prompt=prompt)["ms"]

    return call


def cmd_bench(a: argparse.Namespace) -> int:
    """Index build time, store memory and the prompt hook latency of each build at one
    size. The nightly benchmark of the plan, once; the stores are removed after."""
    import shutil

    from . import soak
    from .buildrunner import Harness, Worker, store_python
    from .fixtures import Fixture

    run = Run(Path(a.run_dir))
    fx = Fixture(Path(a.fixture)) if a.fixture else run.fixture()
    live_data = Path(run.manifest["live"]["data_dir"])
    shard = fx.shards(a.size)[0]
    texts = [fx.prompt_of(q) for q in fx.queries_for(a.size, ["V3", "V1"], shard)]
    first = min(fx.scale(a.size)["lessons"].values(), key=lambda r: r["shard"])
    cwd = "/work/" + first["session_project"]
    out = []
    for build in a.builds.split(","):
        root = run.dir / "stores" / "bench" / build.lower() / str(a.size)
        h = Harness(
            build,
            run.build_path(build),
            root,
            store_python(live_data),
            live_data / "models",
            wait_s=a.wait,
            settings=run.manifest.get("settings_env"),
        )
        h.guard_site = run.dir / "private" / "guard-site"
        h.prepare(fx, a.size, shard)
        try:
            health = h.start()
            pid = h.store.proc.pid if h.store.proc else None
            with Worker(h) as w:
                lat = soak.latency_benchmark(
                    bench_call(w, cwd), texts, n=a.prompts, budget_s=a.budget
                )
            res = soak.resources(h.data, pid)
            row = {
                "build": build,
                "size": a.size,
                "memories": health.get("memories"),
                "index_s": h.timing["index_s"],
                "ready_s": h.timing["ready_s"],
                "rss_mb": res["rss_mb"],
                "db_mb": res["db_mb"],
                "wal_mb": res["wal_mb"],
                "quick_check": res["quick_check"],
                "hook_ms": lat,
            }
        finally:
            h.stop()
            shutil.rmtree(root, ignore_errors=True)
        run.append("bench", row)
        out.append(row)
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from .loop import Loop

    run = Run(Path(a.run_dir))
    problems = verify_frozen(run)
    if problems:
        raise SystemExit("the run may not start: " + "; ".join(problems))
    pilot = run.dir / "pilot.json"
    if not pilot.exists() or not json.loads(pilot.read_text(encoding="utf-8")).get("all_gates"):
        raise SystemExit("the pilot has not passed: run `pilot` first")
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


def cmd_localnet(a: argparse.Namespace) -> int:
    from .loop import LocalNet

    LocalNet(Run(Path(a.run_dir))).run_forever(a.max_seconds)
    return 0


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
    frozen = run / "program" / "tools"
    units = sandbox.render_units(
        run,
        sys.executable,
        frozen if frozen.is_dir() else TOOLS,
        private_network=not a.no_private_network,
    )
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
    s.add_argument("--heldout", default="", help="the hand-labelled real set (arm b, labels)")
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
    s.add_argument("--loop-seconds", type=float, default=0.0, help="job loop check, 0 = skip")
    s.add_argument("--soak-size", type=int, default=20000)
    s.add_argument("--bench-seconds", type=float, default=30.0)
    s = add("bench", cmd_bench)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--size", type=int, required=True)
    s.add_argument("--fixture", default="", help="a fixture folder, default the run's own")
    s.add_argument("--builds", default="B0,B1")
    s.add_argument("--prompts", type=int, default=60)
    s.add_argument("--budget", type=float, default=1200.0)
    s.add_argument("--wait", type=float, default=3600.0)
    s = add("run", cmd_run)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--soak-size", type=int, default=20000)
    s.add_argument("--max-seconds", type=float, default=None)
    for name, fn in (("status", cmd_status), ("stop", cmd_stop), ("report", cmd_report)):
        add(name, fn).add_argument("--run-dir", required=True)
    s = add("units", cmd_units)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--no-private-network", action="store_true")
    s = add("localnet", cmd_localnet)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--max-seconds", type=float, default=None)
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
