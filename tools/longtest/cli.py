# SPDX-License-Identifier: AGPL-3.0-or-later
"""Commands of the long-run test.

    python -m longtest.cli init   --run-dir DIR --repo REPO --b0 COMMIT --b1 COMMIT
    python -m longtest.cli freeze --run-dir DIR
    python -m longtest.cli pilot  --run-dir DIR [--sizes 30,200] [--shards 2] [--limit 6]
    python -m longtest.cli run    --run-dir DIR [--soak-size 200] [--max-seconds N]
                                  [--notify-cmd CMD]
    python -m longtest.cli status --run-dir DIR
    python -m longtest.cli stop   --run-dir DIR
    python -m longtest.cli labels baseline --heldout DIR
    python -m longtest.cli report --run-dir DIR
    python -m longtest.cli export --run-dir DIR --out DIR
    python -m longtest.cli units  --run-dir DIR [--notify-cmd CMD]
    python -m longtest.cli seed-stores --run-dir DIR --from DIR
    python -m longtest.cli watch  --run-dir DIR
    python -m longtest.cli steps  --run-dir DIR [--steps N] [--pilot] [--keep-going]
    python -m longtest.cli health-c2 --run-dir DIR --out-dir DIR --size 20000 [--speed 20]
    python -m longtest.cli health-c3 --run-dir DIR --out-dir DIR [--sizes 5000,20000]
                                      [--starts 10]
    python -m longtest.cli health-c4 --run-dir DIR --out-dir DIR --size 20000

``tools`` must be on ``PYTHONPATH``. ``units`` prints the text of the systemd user
units and writes them under the run folder; it installs nothing. ``run`` starts
the loop of the run's profile (``init --profile``); ``pilot`` runs minutes, or two
hours for the large-project profile. ``watch`` is the watchdog step of the timer.
``steps`` is the time-compressed run (``init --profile large-project-steps``): the
growth steps back to back, with no nightly window and no wait; it goes on where a
killed run stopped, and ``steps --pilot`` is its short pilot. ``health-c2``,
``health-c3`` and ``health-c4`` are Plan 4 Part C (claim W3, health): the compressed
7-day operation mix, the cold-start trials and the kill test, each against its own
copy of the run's kept B1 store, apart from the one growth and replay use (seed the
run's kept B1 store first with ``seed-stores``; the health copy is then made on
first use).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import config, growth, labels, profiles, replay, report, sandbox, warm, watch
from .buildrunner import index_wait_s
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
    seed_stores_from,
    series_hash,
    snapshot_tree,
    stock_expected,
    verify_frozen,
    zero_token_gate,
)
from .scheduler import light_jobs
from .util import TOOLS, append_jsonl, json_text, read_jsonl, write_private


def parse_sizes(text: str) -> list[int]:
    sizes = [int(p) for p in text.split(",") if p]
    bad = [s for s in sizes if s not in SCALES.values()]
    if bad:
        raise SystemExit(f"unknown sizes {bad}; the sizes are {sorted(SCALES.values())}")
    return sizes


def cmd_init(a: argparse.Namespace) -> int:
    prof = profiles.get(a.profile)
    sizes = parse_sizes(a.sizes) if a.sizes else list(prof.sizes)
    doc = init_run(
        Path(a.run_dir),
        Path(a.repo),
        a.b0,
        a.b1,
        sizes,
        Path(a.live_config),
        Path(a.live_data),
        profile=prof.name,
    )
    if a.heldout:
        doc["heldout"] = str(Path(a.heldout).resolve())
        Run(Path(a.run_dir)).save_manifest(doc)
    print(json.dumps({k: doc[k] for k in ("run_id", "profile", "builds", "sizes")}, indent=1))
    return 0


def cmd_seed_stores(a: argparse.Namespace) -> int:
    """Copy the kept B1 stores of another run of the same build and fixture, so the
    pilot scans them instead of embedding for hours. The source is only read."""
    run = Run(Path(a.run_dir))
    sizes = parse_sizes(a.sizes) if a.sizes else list(run.manifest["sizes"])
    print(json.dumps(seed_stores_from(run, Path(a.source), sizes), indent=1, sort_keys=True))
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
    if profiles.profile_of(manifest).name in profiles.LARGE_FAMILY:
        return pilot_large(run, a)
    a.sizes = a.sizes or "30,200"
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


def pilot_large(run: Run, a: argparse.Namespace) -> int:
    """The pilot of the large-project profile (Plan 3, item 7): two hours at 20,000
    notes under the unit's own limits. Gates: the frozen program; zero tokens and zero
    external connects; every store ready inside its cap; one replay shard done per
    scale; B1 pull delivery on V1; one soak hour with a heartbeat every 5 minutes, a
    write that became searchable, and the embedding runtime ready at the end; the live
    memory folders untouched. A failed gate blocks the start.

    The time-compressed profile has no soak store and no unit: its pilot is short. It
    keeps the gates of the replay (freeze, zero tokens, stores ready, one warmed shard
    per scale, the sanity floor of each build, the claims, the live folders) and shows
    per scale what the validity gate of the report would say for each arm. Its sanity
    gate is the recall tool alone (``pilot_sanity``); the prompt channel is shown."""
    manifest = run.manifest
    prof = profiles.profile_of(manifest)
    live_cfg, live_data = Path(manifest["live"]["config_dir"]), Path(manifest["live"]["data_dir"])
    before = live_memory_snapshot(live_cfg)
    from .buildrunner import store_python

    python = store_python(live_data)
    gate2 = zero_token_gate(run, python)
    log = run.dir / "private" / "external_connects.log"
    after_probe = sandbox.count_refused(log)
    site = run.dir / "private" / "guard-site"
    sizes = parse_sizes(a.sizes) if a.sizes else list(manifest["sizes"])
    infos: list[dict] = []
    replay_error = ""
    if not a.reuse:
        try:
            infos = replay_all(
                run,
                sizes,
                1,
                a.limit,
                variants=prof.variants,
                builds=prof.builds,
                parallel=a.parallel,
                day=0,
                guard_site=site,
                python=python,
                wait_s=a.wait,
                stock_arms=prof.stock_arms,
                stock_dir=lambda size, shard: growth.stock_dir(run, size, shard),
            )
        except Exception as exc:  # noqa: BLE001 - a failed task is a failed gate, not a crash
            replay_error = f"{type(exc).__name__}: {exc}"[:500]
    rows = [r for r in replay_rows(run, 0)]
    summary = replay.summarize(rows)
    after = live_memory_snapshot(live_cfg)
    moved = changed_files(before, after)
    stores = stores_ready_gate(run, infos, sizes, a.wait, replay_error)
    gate3 = sanity_gate(rows, builds=prof.builds)
    fired = channels_fired(rows)
    gate4 = claims_gate(rows, fired, claims=prof.claims)
    gate1 = freeze_gate(run)
    gate2["external_connects_during_replay"] = sandbox.count_refused(log) - after_probe
    gate2["ok"] = bool(
        gate2["ok"]
        and gate2["public_connect_refused"]
        and not gate2["claude_processes"]
        and not gate2["external_connects_during_replay"]
    )
    gate5 = {"live_memory_files_changed": len(moved), "ok": not moved}
    arms = (*prof.builds, *prof.stock_arms)
    expected = stock_expected(run.fixture(), sizes, 1, a.limit, prof.variants)
    floors = report.validity([dict(r, day=1) for r in rows], arms, prof.params, sizes, expected)
    report_gate = {
        size: {"ok": g["ok"], "arms": g["steps"].get("1", {})} for size, g in floors.items()
    }
    if prof.steps:
        gate3["ok"] = pilot_sanity(report_gate, prof.builds)
        doc = {
            "profile": prof.name,
            "mode": "compressed",
            "gate1_freeze": gate1,
            "gate2_zero_tokens": gate2,
            "gate3_sanity": gate3,
            "sanity_by_scale": report_gate,
            "prompt_by_scale": prompt_by_scale(rows, sizes, prof.builds),
            "report_gate_would_pass": all(g["ok"] for g in report_gate.values()),
            "gate4_channels_fired": fired,
            "gate4_claims": gate4,
            "gate5_isolation": gate5,
            "stores_ready": stores,
            "tasks": infos,
            "summary": summary,
        }
        doc["gates"] = {
            "freeze": gate1["ok"],
            "zero_tokens": gate2["ok"],
            "stores_ready": stores["ok"],
            "replay_shard_done": stores["shards_done"],
            "sanity": gate3["ok"],
            "claims": gate4["ok"],
            "isolation": gate5["ok"],
            "no_external_connects": not gate2["external_connects_during_replay"],
        }
        doc["all_gates"] = all(doc["gates"].values())
        write_private(run.dir / "pilot.json", json_text(doc))
        shown = (
            "gates",
            "all_gates",
            "sanity_by_scale",
            "prompt_by_scale",
            "report_gate_would_pass",
        )
        print(json.dumps({**{k: doc[k] for k in shown}, "stores": stores}, indent=1))
        return 0 if doc["all_gates"] else 1
    loop_check = loop_gate(run, a, prof) if a.loop_seconds else {"ok": False, "skipped": True}
    limits = unit_limits()
    doc = {
        "profile": prof.name,
        "sanity_by_scale": report_gate,
        "gate1_freeze": gate1,
        "gate2_zero_tokens": gate2,
        "gate3_sanity": gate3,
        "gate4_channels_fired": fired,
        "gate4_claims": gate4,
        "gate5_isolation": gate5,
        "stores_ready": stores,
        "unit_limits": limits,
        "loop": loop_check,
        "tasks": infos,
        "summary": summary,
    }
    doc["gates"] = {
        "freeze": gate1["ok"],
        "zero_tokens": gate2["ok"],
        "under_unit_limits": limits["ok"],
        "stores_ready": stores["ok"],
        "replay_shard_done": stores["shards_done"],
        "sanity": gate3["ok"],
        "claims": gate4["ok"],
        "isolation": gate5["ok"],
        "soak_hour": loop_check["ok"],
        "heartbeat_5min": bool(loop_check.get("heartbeat", {}).get("ok")),
        "no_external_connects": bool(loop_check.get("external_connects") == 0)
        and not gate2["external_connects_during_replay"],
        "embedding_ready": bool(loop_check.get("embedding_ready")),
    }
    doc["all_gates"] = all(doc["gates"].values())
    write_private(run.dir / "pilot.json", json_text(doc))
    print(
        json.dumps(
            {"gates": doc["gates"], "stores": stores, "limits": limits, "loop": loop_check},
            indent=1,
        )
    )
    return 0 if doc["all_gates"] else 1


def pilot_sanity(report_gate: dict, builds: Sequence[str]) -> bool:
    """The sanity gate of the short pilot: at every scale every build reached its floor
    on the recall tool (V1 on the pull channel, 0.95 or more) and has no invalid row.
    The prompt channel is not gated: the prompt hook holds a row back by its own
    rules, so a healthy store gives 52 of 60 there, under the floor of the recall tool.
    Stock is not gated either: stock that fails at 5,000 notes is the hypothesis."""
    return all(g["arms"].get(b, {}).get("ok") for g in report_gate.values() for b in builds)


def prompt_by_scale(rows: Sequence[dict], sizes: Sequence[int], builds: Sequence[str]) -> dict:
    """Per scale and build: the delivery on the prompt channel of the sanity set.
    Shown in the pilot, never a gate (``pilot_sanity``)."""
    return {
        str(size): {
            b: report.prompt_delivery([r for r in rows if r.get("size") == size], b) for b in builds
        }
        for size in sizes
    }


def stores_ready_gate(
    run: Run, infos: Sequence[dict], sizes: Sequence[int], wait_s: float, error: str = ""
) -> dict:
    """Every B1 store of the pilot was ready (index plus start, the wait for the
    embedding backlog in it) inside its cap, and the replay shard of every scale has
    its done marker for day 0. A pilot that is run again skips a task that is done; its
    timings then come from the day-0 marker."""
    fx = run.fixture()
    done = run.done(0)
    markers = [
        r
        for r in read_jsonl(run.out / "replay-done.jsonl")
        if r.get("day") == 0 and r.get("build") == "B1" and "ready_s" in r
    ]
    table: dict[str, dict] = {}
    for size in sizes:
        cap = index_wait_s(size, wait_s)
        got = [t for t in infos if t["build"] == "B1" and t["size"] == size]
        got = got or [m for m in markers if m["size"] == size]
        took = max((t["index_s"] + t["ready_s"] for t in got), default=None)
        shard = fx.shards(size)[0]
        table[str(size)] = {
            "cap_s": cap,
            "ready_s": took,
            "memories": max((t.get("memories") or 0 for t in got), default=None),
            "backlog_chunks": max((t.get("backlog_chunks") or 0 for t in got), default=None),
            "drain_s": max((t.get("drain_s") or 0.0 for t in got), default=None),
            "ok": took is not None and took <= cap,
            "shard_done": ("B1", size, shard) in done,
        }
    return {
        "sizes": table,
        "error": error,
        "ok": not error and all(v["ok"] for v in table.values()),
        "shards_done": all(v["shard_done"] for v in table.values()),
    }


def unit_limits(proc: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict:
    """The task and memory limits of the cgroup this process runs in: the pilot must
    run under the unit's own limits (a transient unit or the unit itself), not under
    the limits of a shell. ``pids.max`` of a shell scope is ``max``."""
    out: dict = {"cgroup": "", "pids_max": None, "memory_max": None}
    try:
        line = (proc / "self" / "cgroup").read_text(encoding="utf-8").strip().splitlines()[-1]
        path = line.split(":", 2)[-1]
        out["cgroup"] = path
        base = cgroup_root / path.lstrip("/")
        out["pids_max"] = (base / "pids.max").read_text(encoding="utf-8").strip()
        out["memory_max"] = (base / "memory.max").read_text(encoding="utf-8").strip()
    except (OSError, IndexError):
        pass
    out["ok"] = bool(out["pids_max"] and out["pids_max"] != "max")
    return out


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


def claims_gate(rows: Sequence[dict], fired: dict, claims: Sequence[str] = CLAIMS) -> dict:
    """Gate 4: each claim in scope has a recorded state in the pilot: fired in both
    builds, fired in B1 only, not fired (a product result, cause I or D, not tuned
    away), or carried by another arm or marked pending with its reason."""
    table: dict[str, dict] = {}
    for claim in claims:
        sel = [r for r in rows if claim in r.get("claims", []) and r["build"] in ("B0", "B1")]
        if not sel:
            note = replay.CLAIM_NOTES.get(claim, "no query in the pilot")
            table[claim] = {"state": "other arm or pending", "note": note}
            continue
        hit = {b: sum(1 for r in sel if r["build"] == b and r.get("hit")) for b in ("B0", "B1")}
        n = sum(1 for r in sel if r["build"] == "B1")
        state = "fired" if hit["B1"] else "not fired (product result)"
        table[claim] = {"state": state, "B0": hit["B0"], "B1": hit["B1"], "n": n}
    return {"claims": table, "ok": len(table) == len(claims)}


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


def loop_gate(run: Run, a: argparse.Namespace, prof: profiles.Profile | None = None) -> dict:
    """The job loop for a few minutes (an hour in the large-project pilot) on the soak
    store with the light jobs only, then the daily check; the files of this loop are
    moved to ``pilot-loop/`` so that day 1 starts empty. For the large-project profile
    the gate also wants a heartbeat every 5 minutes with no gap over 10, a write that
    became searchable, zero external connects, and the embedding runtime ready."""
    from .loop import Loop

    prof = prof or profiles.profile_of(run.manifest)
    jobs = light_jobs(prof.params) if prof.watchdog else None
    loop = Loop(run, a.soak_size, profile=prof, jobs=jobs)
    began = dt.datetime.now().astimezone()
    try:
        loop.run_forever(a.loop_seconds, tick_s=2.0)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - a dirty environment or a dead store
        archive_pilot_loop(run)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    result = run.dir / "pilot-loop-status.json"
    now = dt.datetime.now().astimezone()
    report.daily_summary(run.dir, now)
    res = report.status(run.dir, now, notify=None, params=prof.params)
    write_private(result, json_text(res))
    rows = {p.stem: len(read_jsonl(p)) for p in sorted(run.out.glob("*.jsonl"))}
    soak_rows = read_jsonl(run.out / "soak.jsonl")
    kinds = sorted({r["kind"] for r in soak_rows})
    first = min((r for r in soak_rows if r["kind"] == "write"), key=lambda r: r["ts"], default={})
    beats = [dt.datetime.fromisoformat(r["ts"]) for r in read_jsonl(run.out / "heartbeat.jsonl")]
    cadence = report.heartbeat_cadence(beats, prof.params.heartbeat_s, since=began)
    sandbox_rows = read_jsonl(run.out / "sandbox.jsonl")
    connects = sum(int(r.get("external_connects", 0)) for r in sandbox_rows)
    health = loop.final_health or {}
    embedding = health.get("embedding") or {}
    embedding_ready = embedding.get("state") == "ready" and not embedding.get("missing_vectors")
    archive_pilot_loop(run)
    out = {
        "rows": rows,
        "soak_kinds": kinds,
        "lag_s": first.get("lag_s"),
        "writes": sum(1 for r in soak_rows if r["kind"] == "write"),
        "writes_searchable": sum(1 for r in soak_rows if r["kind"] == "write" and r.get("ok")),
        "heartbeat": cadence,
        "external_connects": connects,
        "embedding_ready": bool(embedding_ready),
        "embedding": {k: embedding.get(k) for k in ("state", "missing_vectors", "model")},
        "memories": health.get("memories"),
        "status_failed": res["failed"],
        "status_notes": res["notes"],
        "ok": res["ok"],
    }
    if prof.watchdog:
        wanted = {"query", "write", "drift", "resources"}
        out["ok"] = bool(
            res["ok"]
            and cadence["ok"]
            and wanted <= set(kinds)
            and out["writes"] > 0
            and out["writes_searchable"] == out["writes"]
            and connects == 0
            and embedding_ready
        )
    return out


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


def sanity_gate(rows: Sequence[dict], builds: Sequence[str] = ("B0", "B1")) -> dict:
    """Gate 3, at SMALL: V1 pull delivery of 0.95 or more for B0 and B1, S2 of 0.9 or
    more. SMALL is the smallest size in the rows; at larger sizes stock is meant to fail.
    A profile that has no small scale (``builds`` is B1 alone) gates its builds only
    and reports S2: stock that fails at 5,000 notes is the hypothesis, not a fault."""
    out: dict = {}
    sizes = [r["size"] for r in rows if r.get("size")]
    if sizes:
        rows = [r for r in rows if r.get("size") == min(sizes)]
    gated = list(builds) + (["S2"] if "B0" in builds else [])
    for build in (*builds, "S2"):
        out[build] = report.sanity_delivery(rows, build)
    out["ok"] = all(
        out[b]["delivery"] is not None and out[b]["delivery"] >= report.sanity_floor(b)
        for b in gated
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
                warmed = warm.warm_up(w, h.data, cwd)
                call = warm.HookCalls(w, cwd, "bench")
                lat = soak.latency_benchmark(call, texts, n=a.prompts, budget_s=a.budget)
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
                **warmed,
                **call.validity(),
            }
        finally:
            h.stop()
            shutil.rmtree(root, ignore_errors=True)
        run.append("bench", row)
        out.append(row)
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from .loop import Loop, command_notifier

    run = Run(Path(a.run_dir))
    problems = verify_frozen(run)
    if problems:
        raise SystemExit("the run may not start: " + "; ".join(problems))
    pilot = run.dir / "pilot.json"
    if not pilot.exists() or not json.loads(pilot.read_text(encoding="utf-8")).get("all_gates"):
        raise SystemExit("the pilot has not passed: run `pilot` first")
    if profiles.profile_of(run.manifest).steps:
        raise SystemExit("this profile runs steps, not nights: use `steps`")
    notify = command_notifier(a.notify_cmd) if a.notify_cmd else None
    loop = Loop(run, a.soak_size, notify=notify)
    loop.run_forever(a.max_seconds)
    return 0


def cmd_steps(a: argparse.Namespace) -> int:
    """The time-compressed run. ``--pilot`` is its short pilot (day 0: one shard per
    scale, a few queries per variant, no growth). Without it: the steps that are not
    done yet, one line per step. Exit 0 when every step is done and every build fired."""
    from . import compressed

    run = Run(Path(a.run_dir))
    prof = profiles.profile_of(run.manifest)
    if not prof.steps:
        raise SystemExit(f"profile {prof.name} runs nights, not steps: use `run`")
    if a.pilot:
        return pilot_large(run, a)
    problems = verify_frozen(run)
    if problems:
        raise SystemExit("the run may not start: " + "; ".join(problems))
    pilot = run.dir / "pilot.json"
    if not pilot.exists() or not json.loads(pilot.read_text(encoding="utf-8")).get("all_gates"):
        raise SystemExit("the pilot has not passed: run `steps --pilot` first")

    def show(row: dict) -> None:
        keys = ("step", "ts", "seconds", "attempts", "ok", "builds_fired")
        cells = {
            size: {arm: c["delivery"] for arm, c in g["arms"].items()}
            for size, g in row["gate"].items()
        }
        print(json.dumps({**{k: row[k] for k in keys}, "sanity": cells}), flush=True)

    res = compressed.run_steps(
        run,
        a.steps or None,
        retries=a.retries,
        keep_going=a.keep_going,
        wait_s=a.wait,
        on_step=show,
    )
    print(json.dumps(res, indent=1))
    return 0 if res["complete"] and not res["stopped"] else 1


def health_harness(run: Run, size: int, fresh: bool = False) -> tuple[Any, list[Path], Any, int]:
    """A ``Harness`` of a health copy of B1's kept store, apart from the one growth
    and replay write into every day (``runner.seed_health_store``): C2 and C4 churn
    notes of their own and must not race that store, or grow it past the size C2 and
    C3 are judged at. Shared by the three health commands; each opens its own worker
    and closes it when done. ``fresh`` throws the copy away first: a check that
    measures the store against the notes on disk must not start from what the last
    check left behind (NOBLIVION-211)."""
    from .buildrunner import Harness, index_wait_s, store_python
    from .runner import seed_health_store

    fx = run.fixture()
    shard = fx.shards(size)[0]
    root = seed_health_store(run, size, fresh=fresh)
    live_data = Path(run.manifest["live"]["data_dir"])
    harness = Harness(
        "B1",
        run.build_path("B1"),
        root,
        store_python(live_data),
        live_data / "models",
        wait_s=index_wait_s(size),
        settings=run.manifest.get("settings_env"),
    )
    folders = harness.prepare(fx, size, shard)
    return harness, folders, fx, shard


def health_sync(worker: Any, folder: Path, tag: str, serial: dict) -> Callable[[Path], None]:
    def sync(path: Path) -> None:
        serial["n"] += 1
        worker.call(
            "sync",
            sid=f"{tag}-{serial['n']}",
            cwd="/work/" + folder.parent.name.replace("-work-", "", 1),
            file_path=str(path),
        )

    return sync


def cmd_health_c2(a: argparse.Namespace) -> int:
    """Plan 4 C2: the registered 7-day operation mix, compressed to ``--speed`` times
    real speed, on a health copy of the kept B1 store (``health_harness``).
    Resumable: a restart reads the events already run from ``c2-progress.jsonl``
    (written as each event finishes, not only once the whole run returns, so a kill
    loses at most the one event in flight) and goes on from there; the schedule
    clock is offset to the last event's own ``fire_s`` so the remaining events keep
    their registered spacing instead of waiting from zero again. No measured work
    starts inside the quiet window (00:45-03:00 CET): one wait before the store even
    starts, then one more before and during every event's own wait, each one pushing
    the schedule clock forward by the seconds slept so a pause does not bunch the
    events that follow it back to back."""
    import time as pytime

    from . import health, soak
    from .buildrunner import Worker
    from .compressed import wait_out_quiet

    def now() -> dt.datetime:
        return dt.datetime.now().astimezone()

    run = Run(Path(a.run_dir))
    out_dir = Path(a.out_dir)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    wait_out_quiet(run, now, pytime.sleep, "health c2 start")
    # A first start takes a store nothing else has touched; a restart must keep the one
    # the events already run churned into, or the rows of this run no longer fit it.
    done = [r["i"] for r in read_jsonl(out_dir / "c2-progress.jsonl")]
    harness, folders, fx, _shard = health_harness(run, a.size, fresh=not done)
    harness.start()
    client = soak.StoreClient(harness.data)
    worker = Worker(harness)
    base = soak.baseline(client, folders)
    texts = [fx.prompt_of(q) for q in fx.queries_for(a.size, ["V3"])][:60]
    if not texts:
        raise SystemExit(f"no V3 queries for size {a.size}: nothing to send")
    serial = {"n": 0}
    sync = health_sync(worker, folders[0], "health-c2", serial)
    counter = {"q": 0}

    def next_text() -> str:
        counter["q"] += 1
        return texts[counter["q"] % len(texts)]

    def op_query(_event):
        return soak.query_load(client, [next_text()], 1)

    def op_burst(_event):
        return soak.query_load(client, [next_text()] * 20, 20)

    def op_churn(_event):
        return soak.churn(folders[0], client, serial["n"] + 1, sync)

    def op_resources(_event):
        pid = harness.store.proc.pid if harness.store and harness.store.proc else None
        return [soak.resources(harness.data, pid)]

    def op_drift(_event):
        return [soak.drift(client, folders, base)]

    ops = {
        "query": op_query,
        "burst": op_burst,
        "churn": op_churn,
        "resources": op_resources,
        "drift": op_drift,
    }
    rows_path = out_dir / "c2-rows.jsonl"
    progress_path = out_dir / "c2-progress.jsonl"
    events, _compressed_s = health.compressed_schedule(speed=a.speed)
    # A fresh process's clock starts at 0: offset it to the last event already run,
    # so a resume keeps the registered spacing of the events still ahead instead of
    # waiting from zero up to their own (far larger) fire_s again.
    clock = {"began": pytime.monotonic() - (events[max(done)]["fire_s"] if done else 0.0)}

    def elapsed() -> float:
        return pytime.monotonic() - clock["began"]

    def pause() -> None:
        clock["began"] += wait_out_quiet(run, now, pytime.sleep, "health c2")

    try:
        health.run_compressed(
            events,
            ops,
            lambda row: append_jsonl(rows_path, row),
            elapsed,
            # Looked up on the module, not bound once, so the wait and ``elapsed``
            # always read the same clock.
            sleep=pytime.sleep,
            pause=pause,
            done=done,
            on_done=lambda i: append_jsonl(progress_path, {"i": i}),
        )
    finally:
        worker.close()
        harness.stop()
    rows = read_jsonl(rows_path)
    doc = health.c2_report(rows, speed=a.speed)
    write_private(out_dir / "c2.json", json_text(doc))
    print(json.dumps({"checks": doc["evaluate"]["checks"], "failed_rules": doc["failed_rules"]}))
    return 0 if doc["evaluate"]["pass"] else 1


def cmd_health_c3(a: argparse.Namespace) -> int:
    """Plan 4 C3: ``--starts`` cold starts at each of ``--sizes``, on a health copy of
    the kept B1 store (stopped and started again every trial, never re-seeded): the
    time to become ready, the time to the first answer, whether it was inside the 2 s
    budget, and whether it carried any result row. A real cold start reads the
    vectors already in the database (the health copy has them already, the same as a
    production restart), so every start here is ``index=False`` -- the opposite of
    C4's own restart after a kill, which must re-index. No measured work starts
    inside the quiet window: one wait before each size's first start, then one more
    before every later trial."""
    import time as pytime

    from . import health, soak
    from .compressed import wait_out_quiet

    def now() -> dt.datetime:
        return dt.datetime.now().astimezone()

    run = Run(Path(a.run_dir))
    out_dir = Path(a.out_dir)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    sizes = parse_sizes(a.sizes) if a.sizes else [5000, 20000]
    doc: dict[str, Any] = {"starts": a.starts, "sizes": {}}
    for size in sizes:
        harness, folders, fx, _shard = health_harness(run, size)
        texts = [fx.prompt_of(q) for q in fx.queries_for(size, ["V3"])]
        if not texts:
            raise SystemExit(f"no V3 queries for size {size}: nothing to search")
        text = texts[0]
        trials = []
        try:
            for _ in range(a.starts):
                wait_out_quiet(run, now, pytime.sleep, f"health c3 {size}")
                harness.stop()

                def start(harness=harness) -> float:
                    t0 = pytime.monotonic()
                    harness.start(index=False)
                    return pytime.monotonic() - t0

                client = soak.StoreClient(harness.data)

                def search(client=client, text=text) -> tuple[int, dict[str, Any], float]:
                    return client.search(text)

                trials.append(health.cold_start_trial(start, search))
        finally:
            harness.stop()
        doc["sizes"][str(size)] = health.cold_start_summary(trials)
    write_private(out_dir / "c3.json", json_text(doc))
    print(json.dumps(doc["sizes"], indent=1))
    return 0 if all(s["pass"] for s in doc["sizes"].values()) else 1


def cmd_health_c4(a: argparse.Namespace) -> int:
    """Plan 4 C4: ``kill -9`` the store mid-burst on a health copy of the kept B1
    store (``health_harness``), restart it, then ``quick_check`` and drift. A churn
    that the kill cuts off answers ``ok: False``; that is the point of the test, not
    graded here (DR8 is not run on this row set). The restart re-indexes
    (``index=True``, the opposite of C3's own restart): a write cut off mid-sync is
    still on disk but not yet in the database, and a real restart's file scan is what
    would pick it up -- without it ``soak.drift`` could read a false, nondeterministic
    nonzero drift for a note the crash orphaned, not the product. No measured work
    starts inside the quiet window."""
    import os
    import signal
    import threading
    import time as pytime

    from . import health, soak
    from .buildrunner import Worker
    from .compressed import wait_out_quiet

    def now() -> dt.datetime:
        return dt.datetime.now().astimezone()

    run = Run(Path(a.run_dir))
    out_dir = Path(a.out_dir)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    wait_out_quiet(run, now, pytime.sleep, "health c4 start")
    harness, folders, _fx, _shard = health_harness(run, a.size)
    harness.start()
    worker = Worker(harness)
    try:
        client = soak.StoreClient(harness.data)
        base = soak.baseline(client, folders)
        serial = {"n": 0}
        sync = health_sync(worker, folders[0], "health-c4", serial)
        burst_errors: list[str] = []

        def burst() -> None:
            for _ in range(a.burst):
                try:
                    soak.churn(folders[0], client, serial["n"] + 1, sync, timeout_s=a.churn_timeout)
                except Exception as exc:  # noqa: BLE001 - the store dies mid-burst by design
                    burst_errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=burst)
        thread.start()
        pytime.sleep(a.kill_after)
        pid = harness.store.proc.pid if harness.store and harness.store.proc else None
        if pid:
            os.kill(pid, signal.SIGKILL)
        join_timeout = max(30.0, a.churn_timeout * a.burst + 10)
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            raise RuntimeError(f"the burst thread was still running after {join_timeout}s")
        worker.close()
        harness.store = None  # the process is gone already; a fresh Store starts clean
        harness.start(index=True)  # a write cut off mid-sync needs the scan to find it
        client = soak.StoreClient(harness.data)
        pid2 = harness.store.proc.pid if harness.store and harness.store.proc else None
        resources_row = soak.resources(harness.data, pid2)
        drift_row = soak.drift(client, folders, base)
        doc = {
            "killed": bool(pid),
            "burst_errors": len(burst_errors),
            "reindexed_on_restart": True,
            **health.kill_test_evaluate(resources_row, drift_row),
        }
    finally:
        worker.close()
        harness.stop()
    write_private(out_dir / "c4.json", json_text(doc))
    print(json.dumps(doc, indent=1))
    return 0 if doc["pass"] else 1


def cmd_status(a: argparse.Namespace) -> int:
    from .runner import installed_version

    run = Run(Path(a.run_dir))
    live_data = Path(run.manifest["live"]["data_dir"])
    res = report.status(
        run.dir,
        dt.datetime.now().astimezone(),
        plugin_version=lambda: installed_version(live_data),
        params=profiles.profile_of(run.manifest).params,
    )
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


def cmd_watch(a: argparse.Namespace) -> int:
    """One step of the watchdog: start a unit that is inactive or failed, restart the
    core unit when its heartbeat is stale, log each action. Exit 0 always: the timer
    must not go into a failed state."""
    ctl = [a.systemctl, "--user"] if a.systemctl else None
    rows = watch.run_once(Path(a.run_dir), systemctl=ctl, stale_s=a.stale_seconds)
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0


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
    from . import soak

    run = Run(Path(a.run_dir))
    prof = profiles.profile_of(run.manifest)
    rows = [r for r in replay_rows(run) if r.get("day")]  # the pilot (day 0) is not the run
    large = prof.name in profiles.LARGE_FAMILY
    incomplete: list[str] = []
    if prof.steps:
        # a steps run is judged on its registered steps only: the rows of a step that
        # has no row in steps.jsonl did not finish, and a run with fewer steps is not judged
        done = {int(r["step"]) for r in read_jsonl(run.out / "steps.jsonl")}
        rows = [r for r in rows if int(r["day"]) in done]
        missing = [s for s in range(1, prof.steps + 1) if s not in done]
        if missing:
            incomplete.append(
                f"{prof.steps - len(missing)} of {prof.steps} steps done; "
                f"missing steps {', '.join(map(str, missing))}"
            )
    if large and rows:
        # DR1 and DR3 are judged on the last night (the grown store); the nights before
        # feed the trend of DR6. A query replays every night, so the rows must not mix.
        last = max(int(r["day"]) for r in rows)
        judged = [r for r in rows if int(r["day"]) == last]
    else:
        judged = rows
    summary = replay.summarize(judged)
    summary["char_ratio"] = report.char_ratio(judged)
    if large:
        soak_rows = read_jsonl(run.out / "soak.jsonl")
        evaluation = soak.evaluate(soak_rows, params=prof.params) if soak_rows else None
        by_night = report.delivery_by_night(rows)
        trend = report.delivery_trend(by_night)
        # an arm must prove that it fired on every step that a rule reads, or the scale
        # is INVALID: the pilot alone does not show that the nights were measured
        sizes = list(run.manifest["sizes"])
        gate = report.validity(
            rows,
            arms=(*prof.builds, *prof.stock_arms),
            params=prof.params,
            sizes=sizes,
            # a night and a step replay every shard and every query
            expected=stock_expected(run.fixture(), sizes, None, None, prof.variants),
        )
        doc = report.verdicts_large(
            summary,
            evaluation,
            trend,
            params=prof.params,
            sizes=list(run.manifest["sizes"]),
            validity=gate,
            health_rule=not prof.steps,
            incomplete=incomplete,
        )
        doc["steps"] = read_jsonl(run.out / "steps.jsonl")
        doc["outcome_hash"] = {str(size): series_hash(rows, size) for size in sizes}
        doc["latency"] = report.latency_validity(read_jsonl(run.out / "latency.jsonl"))
        doc["judged_night"] = max((int(r["day"]) for r in rows), default=None)
        doc["delivery_by_night"] = {str(k): v for k, v in by_night.items()}
        doc["soak"] = evaluation
        doc["growth"] = read_jsonl(run.out / "growth.jsonl")
        doc["failures"] = len(read_jsonl(run.out / "failures.jsonl"))
        doc["watchdog_actions"] = len(read_jsonl(run.out / "watchdog.jsonl"))
    else:
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
    prof = profiles.profile_of(Run(run).manifest) if (run / "run.json").is_file() else None
    units = sandbox.render_units(
        run,
        sys.executable,
        frozen if frozen.is_dir() else TOOLS,
        params=prof.params if prof else PARAMS,
        private_network=not a.no_private_network,
        notify_cmd=a.notify_cmd,
        settings=prof.units if prof else None,
        watchdog=bool(prof and prof.watchdog),
        systemctl=watch.systemctl_path(),
    )
    target = run / "units"
    target.mkdir(mode=0o700, exist_ok=True)
    for name, text in units.items():
        write_private(target / name, text)
        print(f"# {name}\n{text}")
    return 0


NOTIFY_HELP = (
    "a command that hears of an alert: the text is its last argument, it runs without a "
    "shell, and a failure is logged and ignored"
)


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
    s.add_argument("--b0", default="", help="the older build; the large-project profile has none")
    s.add_argument("--b1", required=True)
    s.add_argument(
        "--profile",
        default=profiles.DEFAULT,
        choices=sorted(profiles.PROFILES),
        help="what the run tests; it fixes the sizes, the builds, the claims and the rules",
    )
    s.add_argument("--sizes", default="", help="default: the sizes of the profile")
    s.add_argument("--heldout", default="", help="the hand-labelled real set (arm b, labels)")
    s.add_argument("--live-config", default=str(Path.home() / ".claude"))
    s.add_argument(
        "--live-data",
        default=str(Path.home() / ".claude" / "plugins" / "data" / "noblivion-noblivion"),
    )
    s = add("freeze", cmd_freeze)
    s.add_argument("--run-dir", required=True)
    s = add("seed-stores", cmd_seed_stores)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--from", dest="source", required=True, help="a run folder; read only")
    s.add_argument("--sizes", default="", help="default: the sizes of the run")
    s = add("watch", cmd_watch)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--systemctl", default="", help="absolute path; default: found on PATH")
    s.add_argument("--stale-seconds", type=float, default=watch.STALE_S)
    s = add("pilot", cmd_pilot)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--sizes", default="", help="default: 30,200; the large profile: its sizes")
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
    s.add_argument("--notify-cmd", default="", help=NOTIFY_HELP)
    for name, fn in (("status", cmd_status), ("stop", cmd_stop), ("report", cmd_report)):
        add(name, fn).add_argument("--run-dir", required=True)
    s = add("units", cmd_units)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--no-private-network", action="store_true")
    s.add_argument("--notify-cmd", default="", help=NOTIFY_HELP)
    s = add("steps", cmd_steps)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--steps", type=int, default=0, help="default: the steps of the profile")
    s.add_argument("--retries", type=int, default=2, help="runs again of a failed step")
    s.add_argument("--keep-going", action="store_true", help="go on when a build did not fire")
    s.add_argument("--wait", type=float, default=900.0)
    s.add_argument("--pilot", action="store_true", help="the short pilot (day 0), not the run")
    s.add_argument("--sizes", default="", help="pilot: default the sizes of the run")
    s.add_argument("--limit", type=int, default=10, help="pilot: queries per variant")
    s.add_argument("--parallel", type=int, default=1)
    s.add_argument("--reuse", action="store_true", help="pilot: analyse the rows of day 0")
    s = add("health-c2", cmd_health_c2)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--out-dir", required=True, help="where c2.json and its raw rows go")
    s.add_argument("--size", type=int, default=20000)
    s.add_argument("--speed", type=float, default=20.0, help="the week runs this many times over")
    s = add("health-c3", cmd_health_c3)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--out-dir", required=True, help="where c3.json goes")
    s.add_argument("--sizes", default="", help="default: 5000,20000")
    s.add_argument("--starts", type=int, default=10)
    s = add("health-c4", cmd_health_c4)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--out-dir", required=True, help="where c4.json goes")
    s.add_argument("--size", type=int, default=20000)
    s.add_argument("--burst", type=int, default=5, help="churns of the write burst")
    s.add_argument("--kill-after", type=float, default=0.3, help="seconds before the kill -9")
    s.add_argument("--churn-timeout", type=float, default=3.0, help="per-churn lag wait")
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
