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

from . import stats
from .config import PARAMS, SCALES, Params
from .util import json_text, read_jsonl, sha256_file, write_private

PRIMARY_CLAIMS = ("C1", "C3")


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


def status(
    run_dir: Path,
    now: dt.datetime,
    *,
    free_gb: Callable[[Path], float] | None = None,
    plugin_version: Callable[[], str] | None = None,
    notify: Callable[[str], None] | None = None,
    params: Params = PARAMS,
    plan_gb: float = 2.5,
) -> dict[str, Any]:
    """The checks of section 13. Returns ``{"ok", "failed": [...], "checks": {...}}``.
    A failure writes ``ALERT`` in the run folder and calls ``notify``."""
    run_dir = Path(run_dir)
    out = run_dir / "out"
    since = (now - dt.timedelta(days=1)).isoformat()
    beats = [r for r in read_jsonl(out / "heartbeat.jsonl") if r["ts"] >= since]
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
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
    free = (free_gb or (lambda p: shutil.disk_usage(p).free / 1e9))(run_dir)
    day = now.date().isoformat()
    failed_jobs = [
        n for n, j in state["jobs"].items() if j.get("failed", 0) and not j.get("ok", True)
    ]
    checks = {
        "heartbeats_24": len(beats) >= 24,
        "jobs_ran": not failed_jobs and bool(state["jobs"]),
        "no_external_connects": all(r.get("external_connects", 0) == 0 for r in sandbox),
        "no_claude_process": all(r.get("claude_processes", 0) == 0 for r in sandbox),
        "no_refused_open": all(
            r.get("refused_opens", 0) == 0 and r.get("refused_writes", 0) == 0 for r in sandbox
        ),
        "determinism_same": not mismatch,
        "free_disk": free >= params.min_free_gb,
        "run_folder_size": run_dir_size_gb(run_dir) <= plan_gb,
        "plugin_version_same": plugin_version is None
        or plugin_version() == manifest.get("plugin_version"),
        "summary_written": (run_dir / "daily" / f"{day}.json").exists(),
    }
    soak_rows = read_jsonl(out / "soak.jsonl")
    if soak_rows:
        from . import soak as soak_mod

        checks["soak_rules"] = bool(soak_mod.evaluate(soak_rows, params=params)["pass"])
    failed = sorted(k for k, v in checks.items() if not v)
    result = {"ok": not failed, "failed": failed, "checks": checks, "day": day}
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
