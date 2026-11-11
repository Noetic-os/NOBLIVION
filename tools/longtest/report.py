# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision rules, the daily health check, the final report and the export.

``verdicts`` applies DR1 to DR8 to the summaries and returns WIN, TIE or LOSE per
scale. ``status`` is the 06:30 check of the plan (exit 0 only when every item holds).
``export`` writes the sanitised files: counts, hashes and ids, no text.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import profiles, stats, warm
from .config import PARAMS, SCALES, Params
from .scheduler import window_start
from .util import json_text, read_jsonl, sha256_file, write_private

PRIMARY_CLAIMS = ("C1", "C3")
TREND_CLAIM = "C1"  # DR6 of the large project: delivery on C1, night by night


def _size_class(size: int) -> str:
    return "large" if size >= 5000 else "small_medium"


def char_ratio(rows: Sequence[Mapping[str, Any]], resamples: int = 2000) -> dict[str, Any]:
    """DR4: the paired ratio B1 / B0 of characters on the same prompt queries, per
    size, with the cluster bootstrap interval (cluster = lesson)."""
    pairs: dict[tuple, dict[str, Mapping[str, Any]]] = {}
    for r in rows:
        if r["channel"] == "prompt" and r["build"] in ("B0", "B1"):
            pairs.setdefault((r["size"], r["qid"]), {})[r["build"]] = r
    by_size: dict[int, dict[str, list[tuple[float, float]]]] = {}
    for (size, _), got in pairs.items():
        if "B0" in got and "B1" in got:
            by_size.setdefault(size, {}).setdefault(got["B1"]["lesson"], []).append(
                (float(got["B1"]["chars"]), float(got["B0"]["chars"]))
            )
    out = {}
    for size, clusters in sorted(by_size.items()):

        def ratio(p: list[tuple[float, float]]) -> float:
            den = sum(b for _, b in p)
            return sum(a for a, _ in p) / den if den else 1.0

        out[str(size)] = stats.cluster_bootstrap(clusters, resamples, stat=ratio)
    return out


def verdicts(
    summary: Mapping[str, Any],
    labels: Mapping[str, Any] | None,
    soak: Mapping[str, Any] | None,
    latency_p95_20k: float | None,
    live: Mapping[str, Any] | None = None,
    params: Params = PARAMS,
) -> dict[str, Any]:
    """The pre-registered rules over the replay summary. Anything the data does not
    yet hold is ``None`` (reported as pending), never guessed."""
    vs2, vb0 = summary["b1_vs_s2"], summary["b1_vs_b0"]
    adj = stats.holm({k: v["p"] for k, v in vs2.items()})
    rates = summary["rates"]
    claims: dict[str, Any] = {}
    for key, v in sorted(vs2.items()):
        size, claim = key.split("|")
        s2 = rates.get(f"{size}|{claim}|S2")
        stock_fails = bool(s2 and s2["high"] < params.dr2_upper)  # DR2
        dr1 = v["low"] > params.dr1_lower and adj[key] < params.alpha
        claims[key] = {
            "b1_minus_s2": v["diff"],
            "low": v["low"],
            "high": v["high"],
            "p_holm": round(adj[key], 4),
            "stock_fails": stock_fails,
            "dr1": bool(dr1 and stock_fails),
            "no_test": not stock_fails,
            "lose": v["high"] < 0,
        }
    for key, v in sorted(vb0.items()):
        claims.setdefault(key, {})["dr3"] = v["low"] > params.dr3_lower
    fd = summary["false_delivery_v5"]
    scales: dict[str, Any] = {}
    for name, size in SCALES.items():
        limit = params.dr5_large if _size_class(size) == "large" else params.dr5_small_medium
        v5 = fd.get(f"{size}|V5|B1")
        dr5 = None if v5 is None else v5["high"] <= limit
        dr6 = None
        if labels is not None:
            floor = params.dr6_large if _size_class(size) == "large" else params.dr6_small_medium
            dr6 = labels.get("precision_b1", 0.0) >= floor and labels.get("b1_minus_b0", 0.0) >= 0
        primary = [claims.get(f"{size}|{c}") for c in PRIMARY_CLAIMS if f"{size}|{c}" in claims]
        win_core = any(p and p.get("dr1") for p in primary)
        lose = any(p and p.get("lose") for p in primary) or dr5 is False
        if not primary:
            word = "NO DATA"
        elif lose:
            word = "LOSE"
        elif win_core and dr5 and dr6:
            word = "WIN"
        elif win_core and (dr5 is None or dr6 is None):
            word = "PENDING"
        else:
            word = "TIE"
        scales[name] = {"size": size, "dr5": dr5, "dr6": dr6, "verdict": word}
    cost = char_ratio_pass(summary.get("char_ratio"), live, params)
    dr7 = None if latency_p95_20k is None else latency_p95_20k <= params.dr7_p95_ms
    return {
        "claims": claims,
        "scales": scales,
        "dr4": cost,
        "dr7": dr7,
        "dr8": None if soak is None else bool(soak["pass"]),
    }


def char_ratio_pass(
    ratio: Mapping[str, Any] | None, live: Mapping[str, Any] | None, params: Params = PARAMS
) -> dict[str, Any]:
    chars = None if not ratio else all(v["high"] <= params.dr4_char_ratio for v in ratio.values())
    resent = None
    if live and live.get("resent_ratio") is not None:
        resent = live["resent_ratio"] <= params.dr4_resent_ratio
    return {
        "chars": chars,
        "resent": resent,
        "pass": None if chars is None or resent is None else (chars and resent),
    }


# -- the daily check ---------------------------------------------------------------


def run_dir_size_gb(run_dir: Path) -> float:
    return sum(p.stat().st_size for p in Path(run_dir).rglob("*") if p.is_file()) / 1e9


def heartbeats_needed(
    beats: Sequence[dt.datetime], now: dt.datetime, params: Params = PARAMS
) -> int:
    """How many hourly lines the last 24 hours must hold: 24 once the run is a day old,
    and one more than the whole hours since its first line before that. A run with no
    line at all needs one. The window never shrinks for a run that went quiet: the
    first line stays the first line."""
    if not beats:
        return 1
    begin = max(min(beats), now - dt.timedelta(days=1))
    hours = int((now - begin).total_seconds() // 3600)
    return max(1, min(params.heartbeats_per_day, 1 + hours))


def night_day(started: str | None, now: dt.datetime) -> int:
    """The run day of the last night, counted as ``Loop.day`` counts: the day that holds
    the start of the latest heavy window. 0 before the first night of the run."""
    if not started:
        return 0
    began = dt.datetime.fromisoformat(started)
    return max(0, int((window_start(now) - began).total_seconds() // 86400) + 1)


def heartbeat_cadence(
    beats: Sequence[dt.datetime], every_s: float, since: dt.datetime | None = None
) -> dict[str, Any]:
    """The largest gap between heartbeat lines (from ``since``), against twice the
    interval: a loop that beats every 5 minutes never leaves a gap of 10."""
    pts = sorted(t for t in beats if since is None or t >= since)
    gaps = [(b - a).total_seconds() for a, b in zip(pts, pts[1:], strict=False)]
    worst = max(gaps, default=0.0)
    return {
        "n": len(pts),
        "max_gap_s": round(worst, 1),
        "limit_s": 2 * every_s,
        "ok": len(pts) >= 2 and worst <= 2 * every_s,
    }


def sanity_delivery(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    """Delivery of one arm on the sanity set: the V1 rows, on the pull channel for a
    build (the recall tool with the lesson's own words) and as they are for stock."""
    sel = [
        r
        for r in rows
        if r.get("variant") == "V1"
        and r.get("build") == arm
        and (r.get("channel") == "pull" or arm.startswith("S"))
    ]
    hits = sum(1 for r in sel if r.get("hit"))
    return {"n": len(sel), "delivery": round(hits / len(sel), 3) if sel else None}


def prompt_delivery(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    """Delivery of one build on the prompt channel of the sanity set. Reported, never
    gated: the prompt hook holds a row back by its own rules, so a healthy store does
    not reach the floor of the recall tool (52 of 60 measured)."""
    sel = [
        r
        for r in rows
        if r.get("variant") == "V1" and r.get("build") == arm and r.get("channel") == "prompt"
    ]
    hits = sum(1 for r in sel if r.get("hit"))
    return {"n": len(sel), "delivery": round(hits / len(sel), 3) if sel else None}


def sanity_floor(arm: str, params: Params = PARAMS) -> float:
    return params.sanity_s2_delivery if arm.startswith("S") else params.sanity_pull_delivery


def validity(
    rows: Sequence[Mapping[str, Any]],
    arms: Sequence[str] = ("B1", "S2"),
    params: Params = PARAMS,
    sizes: Sequence[int] | None = None,
) -> dict[str, Any]:
    """The validity gate of the report: an arm must prove that it fired before its
    rows count. Per size and judged step (a night, or a step of the compressed run;
    day 0 is the pilot and is left out) and per arm: the sanity delivery against its
    registered floor, and no invalid row (``warm.invalid_rows``: the hook latch, or a
    hook that gave up on the store; such a row is not a miss of the build). An arm
    with no sanity row has proved nothing. Returns ``size -> {"ok", "causes", "steps":
    {step: {arm: {"n", "delivery", "floor", "invalid", "ok"}}}}``; a size with no step
    at all is not ok (no data is not a proof)."""
    by: dict[int, dict[int, list[Mapping[str, Any]]]] = {}
    for r in rows:
        if r.get("day") and r.get("size"):
            by.setdefault(int(r["size"]), {}).setdefault(int(r["day"]), []).append(r)
    out: dict[str, Any] = {}
    for size in sorted(by) if sizes is None else sizes:
        steps: dict[str, Any] = {}
        causes: list[str] = []
        for step, step_rows in sorted(by.get(size, {}).items()):
            cell: dict[str, Any] = {}
            for arm in arms:
                got = sanity_delivery(step_rows, arm)
                floor = sanity_floor(arm, params)
                fired = got["delivery"] is not None and got["delivery"] >= floor
                bad = warm.invalid_rows(r for r in step_rows if r.get("build") == arm)
                got.update(floor=floor, invalid=len(bad), ok=fired and not bad)
                if not fired:
                    seen = "no sanity rows" if not got["n"] else f"{got['delivery']} of {got['n']}"
                    causes.append(
                        f"step {step}: {arm} sanity delivery {seen}, under the floor {floor}"
                    )
                if bad:
                    causes.append(
                        f"step {step}: {arm} has {len(bad)} invalid rows, fail reasons "
                        f"{warm.fail_counts(bad)} (the hook latch, or a hook that gave "
                        "up on the store)"
                    )
                cell[arm] = got
            steps[str(step)] = cell
        if not steps:
            causes.append("no judged step")
        out[str(size)] = {"ok": not causes, "causes": causes, "steps": steps}
    return out


def latency_validity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Latency rows by what they measured: ``invalid`` rows held calls that ran under
    the hook latch (30 seconds of empty answers), ``unchecked`` rows come from a
    program that did not count them. Only ``valid`` rows are a latency of the build."""
    out = {"rows": len(rows), "valid": 0, "invalid": 0, "unchecked": 0, "invalid_rows": []}
    for r in rows:
        if "valid" not in r:
            out["unchecked"] += 1
        elif r["valid"]:
            out["valid"] += 1
        else:
            out["invalid"] += 1
            out["invalid_rows"].append(
                {k: r.get(k) for k in ("ts", "build", "size", "latched", "p50", "p95")}
            )
    return out


def delivery_by_night(
    rows: Sequence[Mapping[str, Any]], claim: str = TREND_CLAIM, build: str = "B1"
) -> dict[int, dict[int, float]]:
    """``size -> day -> delivery rate`` of ``build`` on ``claim``, from replay rows. Day
    0 (the pilot) is left out."""
    cells: dict[int, dict[int, list[bool]]] = {}
    for r in rows:
        if r.get("build") != build or claim not in (r.get("claims") or []) or not r.get("day"):
            continue
        if r.get("hit") is None:
            continue
        cells.setdefault(int(r["size"]), {}).setdefault(int(r["day"]), []).append(bool(r["hit"]))
    return {
        size: {day: round(sum(v) / len(v), 4) for day, v in sorted(days.items()) if v}
        for size, days in sorted(cells.items())
    }


def delivery_trend(
    by_night: Mapping[int, Mapping[int, float]], min_nights: int = 3
) -> dict[str, Any]:
    """DR6 per size: the Theil-Sen slope of delivery over the nights. ``ok`` is ``None``
    with fewer than ``min_nights`` nights, true when the interval holds 0 or lies above."""
    out: dict[str, Any] = {}
    for size, days in by_night.items():
        xs = [float(d) for d in sorted(days)]
        ys = [days[int(x)] for x in xs]
        if len(xs) < min_nights:
            out[str(size)] = {"nights": len(xs), "ok": None}
            continue
        t = stats.theil_sen(xs, ys)
        out[str(size)] = {
            "nights": len(xs),
            "slope": t["slope"],
            "low": t["low"],
            "high": t["high"],
            "ok": bool(t["holds_zero"] or t["low"] > 0),
        }
    return out


def verdicts_large(
    summary: Mapping[str, Any],
    soak: Mapping[str, Any] | None,
    trend: Mapping[str, Any] | None,
    params: Params = PARAMS,
    sizes: Sequence[int] = profiles.LARGE_SIZES,
    validity: Mapping[str, Any] | None = None,
    health_rule: bool = True,
) -> dict[str, Any]:
    """The rules of the large-project profile (DR1, DR3, DR5, DR6) over the replay
    summary, the soak evaluation and the night trend. What the data does not hold is
    ``None`` (pending), never guessed. ``validity`` is the gate of ``validity()``: a
    scale whose gate failed gets the verdict INVALID with the causes, and DR1, DR3 and
    DR6 are not judged there (``None``), because an arm that did not fire shows
    neither a win nor a loss. ``health_rule`` is false for a run with no soak store
    (the time-compressed run): DR5 is then not in scope and the verdict does not wait
    for it."""
    vs2 = summary["b1_vs_s2"]
    adj = stats.holm({k: v["p"] for k, v in vs2.items()})
    claims: dict[str, Any] = {}
    void = {size for size, gate in (validity or {}).items() if not gate["ok"]}
    for key, v in sorted(vs2.items()):
        judged = key.split("|")[0] not in void
        claims[key] = {
            "b1_minus_s2": v["diff"],
            "low": v["low"],
            "high": v["high"],
            "p_holm": round(adj[key], 4),
            "judged": judged,
            "dr1": bool(v["low"] > params.dr1_lower and adj[key] < params.alpha)
            if judged
            else None,
            "lose": v["high"] < 0 if judged else None,
        }
    fd = summary.get("false_delivery_v5", {})
    health = None
    if soak is not None:
        checks = soak["checks"]
        health = all(checks[k] for k in ("budget_recall", "lag_p95", "drift_zero", "error_rate"))
    scales: dict[str, Any] = {}
    names = {v: k for k, v in SCALES.items()}
    for size in sizes:
        # C2 rows count a correct silence on V5 as a hit in both arms, so the paired
        # difference B1 minus S2 on C2 is the difference in silence; not worse means
        # its lower bound stays above the DR3 floor.
        silence = vs2.get(f"{size}|C2")
        dr3 = None if silence is None else silence["low"] > params.dr3_lower
        dr6 = None if not trend or str(size) not in trend else trend[str(size)]["ok"]
        primary = [claims[f"{size}|{c}"] for c in PRIMARY_CLAIMS if f"{size}|{c}" in claims]
        invalid = str(size) in void
        if invalid:
            dr3 = dr6 = None  # not judged
        win_core = any(p["dr1"] for p in primary)
        lose = any(p["lose"] for p in primary) or dr3 is False
        if invalid:
            word = "INVALID"
        elif not primary:
            word = "NO DATA"
        elif lose:
            word = "LOSE"
        elif win_core and dr3 and dr6 and (health or not health_rule):
            word = "WIN"
        elif win_core and (dr3 is None or dr6 is None or (health is None and health_rule)):
            word = "PENDING"
        else:
            word = "TIE"
        scales[names.get(size, str(size))] = {
            "size": size,
            "dr1_c1": claims.get(f"{size}|C1", {}).get("dr1"),
            "dr1_c3": claims.get(f"{size}|C3", {}).get("dr1"),
            "dr3": dr3,
            "false_delivery_b1": fd.get(f"{size}|V5|B1"),
            "false_delivery_s2": fd.get(f"{size}|V5|S2"),
            "dr5": health,
            "dr6": dr6,
            "verdict": word,
        }
        if invalid:
            scales[names.get(size, str(size))]["invalid_because"] = validity[str(size)]["causes"]
    return {
        "claims": claims,
        "scales": scales,
        "dr5": health,
        "trend": trend,
        "validity": validity,
        "dr5_in_scope": health_rule,
    }


def status(
    run_dir: Path,
    now: dt.datetime,
    *,
    free_gb: Callable[[Path], float] | None = None,
    plugin_version: Callable[[], str] | None = None,
    notify: Callable[[str], None] | None = None,
    params: Params = PARAMS,
    plan_gb: float | None = None,
) -> dict[str, Any]:
    """The checks of section 13. Returns ``{"ok", "failed": [...], "checks": {...}}``.
    A failure writes ``ALERT`` in the run folder and calls ``notify``. The check writes
    nothing else: never ``stop``, never ``pause``. The determinism check is for a
    profile whose small scales replay every night; a profile whose stores grow has
    nothing to compare and the check is not applied."""
    run_dir = Path(run_dir)
    out = run_dir / "out"
    since = now - dt.timedelta(days=1)
    all_beats = [dt.datetime.fromisoformat(r["ts"]) for r in read_jsonl(out / "heartbeat.jsonl")]
    beats = [t for t in all_beats if t >= since]
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    profile = profiles.profile_of(manifest)
    if params is PARAMS:
        params = profile.params
    needed = heartbeats_needed(all_beats, now, params)
    state = (
        json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        if (run_dir / "state.json").exists()
        else {"jobs": {}}
    )
    sandbox = read_jsonl(out / "sandbox.jsonl")
    det = read_jsonl(out / "determinism.jsonl")
    first = {(r["scale"], r["part"]): r["hash"] for r in det if r["day"] == 1}
    mismatch = [
        r
        for r in det
        if r["day"] > 1 and first.get((r["scale"], r["part"]), r["hash"]) != r["hash"]
    ]
    # No row to compare is no proof: from the second night on, day 1 and a later day
    # must both have a row.
    compared = bool(first) and any(r["day"] > 1 for r in det)
    unmeasured = night_day(state.get("started"), now) >= 2 and not compared
    if not profile.determinism:
        mismatch, unmeasured = [], False
    free = (free_gb or (lambda p: shutil.disk_usage(p).free / 1e9))(run_dir)
    day = now.date().isoformat()
    failed_jobs = [
        n for n, j in state["jobs"].items() if j.get("failed", 0) and not j.get("ok", True)
    ]
    checks = {
        "heartbeats_24": len(beats) >= needed,
        "jobs_ran": not failed_jobs and bool(state["jobs"]),
        "no_external_connects": all(r.get("external_connects", 0) == 0 for r in sandbox),
        "no_claude_process": all(r.get("claude_processes", 0) == 0 for r in sandbox),
        "no_refused_open": all(
            r.get("refused_opens", 0) == 0 and r.get("refused_writes", 0) == 0 for r in sandbox
        ),
        "determinism_same": not mismatch and not unmeasured,
        "free_disk": free >= params.min_free_gb,
        "run_folder_size": run_dir_size_gb(run_dir) <= (plan_gb or params.run_folder_gb),
        "plugin_version_same": plugin_version is None
        or plugin_version() == manifest.get("plugin_version"),
        "summary_written": (run_dir / "daily" / f"{day}.json").exists(),
    }
    soak_rows = read_jsonl(out / "soak.jsonl")
    notes: dict[str, Any] = {}
    if soak_rows:
        from . import soak as soak_mod

        dr8 = soak_mod.evaluate(soak_rows, params=params)
        # The write-to-searchable lag is a measured property of the build (the debounce
        # and the indexer run of the memory-sync hook). A miss of the 10 s rule goes into
        # the verdict, as DR8 says; it is not a fault of the run, so it does not raise ALERT
        # every day. A write that never became searchable does.
        checks["soak_rules"] = all(v for k, v in dr8["checks"].items() if k != "lag_p95")
        notes = {
            "dr8_lag_p95_s": dr8["lag_p95_s"],
            "dr8_lag_rule_holds": dr8["checks"]["lag_p95"],
            "error_rate_underpowered": dr8["error_rate_underpowered"],
        }
    failed = sorted(k for k, v in checks.items() if not v)
    result = {"ok": not failed, "failed": failed, "checks": checks, "day": day, "notes": notes}
    if failed:
        write_private(run_dir / "ALERT", "failed: " + ", ".join(failed) + "\n")
        if notify:
            notify("long-run test: " + ", ".join(failed))
    return result


def daily_summary(run_dir: Path, now: dt.datetime) -> Path:
    """The daily JSON: counters grown, from the out files. Counts only."""
    run_dir = Path(run_dir)
    out = run_dir / "out"
    sandbox = read_jsonl(out / "sandbox.jsonl")
    doc = {
        "day": now.date().isoformat(),
        "external_connects": sum(r.get("external_connects", 0) for r in sandbox),
        "claude_processes": sum(r.get("claude_processes", 0) for r in sandbox),
        "rows": {p.stem: len(read_jsonl(p)) for p in sorted(out.glob("*.jsonl"))},
    }
    path = run_dir / "daily" / f"{doc['day']}.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_private(path, json_text(doc))
    return path


def export(run_dir: Path, dest: Path) -> list[str]:
    """The sanitised export: the manifest, the daily summaries, the final report,
    and the hash of every file. No row of ``out`` and nothing of ``private``."""
    run_dir, dest = Path(run_dir), Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    names = []
    for src in [
        run_dir / "run.json",
        run_dir / "final_report.json",
        run_dir / "REPORT.md",
        *sorted((run_dir / "daily").glob("*.json")),
    ]:
        if src.is_file():
            target = dest / src.name
            shutil.copyfile(src, target)
            target.chmod(0o600)
            names.append(src.name)
    sums = "".join(f"{sha256_file(dest / n)}  {n}\n" for n in names)
    write_private(dest / "files.sha256", sums)
    return names
