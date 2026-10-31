# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm d: a harness store under load for days, and the nightly latency benchmark.

Everything runs against a harness store (its own data folder and token), never the
live one. A step is small and returns plain numbers; the scheduler calls the steps
and appends their rows to ``out/soak.jsonl``.

* ``query_load``: recall requests through the store, in parallel for a burst.
* ``churn``: writes, edits and deletes of notes in a memory folder, then the
  write-to-searchable lag of each new note.
* ``drift``: notes on disk against the notes the store counts.
* ``resources``: store memory (``/proc``), database and WAL size, ``quick_check``.
* ``latency_benchmark``: the prompt hook, ``n`` prompts, p50 and p95, for the table
  of the nightly benchmark.
* ``evaluate``: DR8 over the rows of the days so far.
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import stats
from .config import PARAMS, Params
from .util import percentile, summarize_ms


class StoreClient:
    """A harness store over HTTP, with its own token. Loopback only."""

    def __init__(self, data_dir: Path) -> None:
        self.data = Path(data_dir)

    def _call(self, route: str, timeout: float = 30.0) -> tuple[int, dict[str, Any], float]:
        info = json.loads((self.data / "store.json").read_text(encoding="utf-8"))
        token = (self.data / "token").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{route}", headers={"Authorization": "Bearer " + token}
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as answer:  # noqa: S310
                body = json.loads(answer.read())
                return answer.status, body, (time.perf_counter() - started) * 1000
        except urllib.error.HTTPError as exc:
            return exc.code, {}, (time.perf_counter() - started) * 1000
        except (OSError, ValueError):
            return 0, {}, (time.perf_counter() - started) * 1000

    def health(self) -> dict[str, Any]:
        return self._call("/api/health", 10)[1]

    def search(
        self, text: str, project: str = "claude_code", top_k: int = 5
    ) -> tuple[int, dict[str, Any], float]:
        query = urllib.parse.urlencode({"q": text, "project": project, "top_k": top_k})
        return self._call("/api/memories/index?" + query)


def query_load(
    client: StoreClient, texts: Sequence[str], parallel: int = 1, project: str = "claude_code"
) -> list[dict[str, Any]]:
    """One request per text; ``parallel`` at once. A row per request: status and ms."""

    def one(text: str) -> dict[str, Any]:
        status, _, ms = client.search(text, project)
        return {"kind": "query", "status": status, "ms": round(ms, 1), "ok": status == 200}

    if parallel <= 1:
        return [one(t) for t in texts]
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        return list(pool.map(one, texts))


def note_text(token: str, serial: int) -> str:
    return (
        f"---\nname: Soak note {serial}\ndescription: churn note {serial} {token}\ntype: project\n"
        f"---\n\nThe churn note {serial} holds the marker {token} and nothing else of value.\n"
    )


def churn(
    folder: Path,
    client: StoreClient,
    serial: int,
    sync: Callable[[Path], None],
    project: str = "claude_code",
    timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
    """Write one new note, edit one, delete one. ``sync`` is the memory-sync step
    of the build (the hook after a write). The lag of a new note is the time from
    the write until its marker is searchable."""
    rows: list[dict[str, Any]] = []
    token = f"zqx{serial:06d}marker"
    path = Path(folder) / f"project_soak_{serial:06d}.md"
    started = time.monotonic()
    path.write_text(note_text(token, serial), encoding="utf-8")
    sync(path)
    lag = None
    while time.monotonic() - started < timeout_s:
        status, body, _ = client.search(token, project, 3)
        if status == 200 and token in json.dumps(body):
            lag = time.monotonic() - started
            break
        time.sleep(0.25)
    rows.append(
        {
            "kind": "write",
            "lag_s": round(lag, 2) if lag is not None else None,
            "ok": lag is not None,
        }
    )
    old = sorted(Path(folder).glob("project_soak_*.md"))
    if len(old) > 3:
        victim = old[0]
        victim.unlink()
        sync(victim)
        rows.append({"kind": "delete", "ok": True})
    if old:
        target = old[-1]
        target.write_text(note_text(token + "edit", serial) + "\nEdited.\n", encoding="utf-8")
        sync(target)
        rows.append({"kind": "edit", "ok": True})
    return rows


def disk_notes(folders: Sequence[Path]) -> int:
    return sum(
        1
        for f in folders
        for p in Path(f).glob("*.md")
        if not p.name.startswith(("MEMORY", "topic_"))
    )


def drift(client: StoreClient, folders: Sequence[Path], baseline: dict[str, int]) -> dict[str, Any]:
    """Index drift: the change in notes on disk since the baseline, minus the change
    in notes the store counts. 0 when the store follows the disk."""
    health = client.health()
    disk = disk_notes(folders)
    counted = int(health.get("memories", 0))
    value = (disk - baseline["disk"]) - (counted - baseline["memories"])
    return {"kind": "drift", "disk": disk, "memories": counted, "drift": value}


def baseline(client: StoreClient, folders: Sequence[Path]) -> dict[str, int]:
    return {"disk": disk_notes(folders), "memories": int(client.health().get("memories", 0))}


def rss_mb(pid: int, proc: Path = Path("/proc")) -> float:
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError):
        pass
    return 0.0


def resources(data_dir: Path, pid: int | None) -> dict[str, Any]:
    db = Path(data_dir) / "noblivion.db"
    wal = Path(str(db) + "-wal")
    check = "unknown"
    try:  # read-only, short: a long read transaction would block the WAL checkpoint
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            check = str(con.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as exc:
        check = "error:" + type(exc).__name__
    return {
        "kind": "resources",
        "rss_mb": rss_mb(pid) if pid else 0.0,
        "db_mb": round(db.stat().st_size / 1e6, 2) if db.exists() else 0.0,
        "wal_mb": round(wal.stat().st_size / 1e6, 2) if wal.exists() else 0.0,
        "quick_check": check,
    }


def latency_benchmark(
    call: Callable[[str], float], prompts: Sequence[str], n: int = 100, budget_s: float = 1200.0
) -> dict[str, Any]:
    """``call(prompt)`` returns milliseconds. Runs up to ``n`` prompts or ``budget_s``
    seconds (the nightly benchmark is capped at 20 minutes)."""
    ms: list[float] = []
    end = time.monotonic() + budget_s
    for i in range(n):
        if time.monotonic() > end:
            break
        ms.append(call(prompts[i % len(prompts)]))
    return {"kind": "latency", **summarize_ms(ms)}


def evaluate(
    rows: Sequence[dict[str, Any]], days: Sequence[str] | None = None, params: Params = PARAMS
) -> dict[str, Any]:
    """DR8 over soak rows. Rows hold ``day``, ``kind`` and the values above. Per-day
    p95 against each budget, the error rate (Wilson upper bound), drift at every
    check, lag p95, Theil-Sen slopes of latency and memory, and quick_check."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    days = sorted(by_day) if days is None else list(days)
    queries = [r for r in rows if r["kind"] == "query"]
    errors = sum(1 for r in queries if not r["ok"])
    upper = stats.wilson(errors, len(queries))[1] if queries else 1.0
    daily_p95 = {
        d: percentile([r["ms"] for r in by_day.get(d, []) if r["kind"] == "query"], 0.95)
        for d in days
    }
    lags = [r["lag_s"] for r in rows if r["kind"] == "write" and r.get("lag_s") is not None]
    failed_writes = sum(1 for r in rows if r["kind"] == "write" and not r["ok"])
    drifts = [r["drift"] for r in rows if r["kind"] == "drift"]
    res = [r for r in rows if r["kind"] == "resources"]
    xs = list(range(len(daily_p95)))
    latency_trend = stats.theil_sen(xs, [daily_p95[d] for d in days]) if len(days) >= 3 else None
    mem_x = [i for i, _ in enumerate(res)]
    mem_trend = stats.theil_sen(mem_x, [r["rss_mb"] for r in res]) if len(res) >= 3 else None
    span_days = max(1, len(days))
    growth = (res[-1]["rss_mb"] - res[0]["rss_mb"]) / span_days if len(res) >= 2 else 0.0
    checks = {
        "budget_recall": all(v <= params.budget_recall_ms for v in daily_p95.values()),
        "error_rate": upper < params.error_rate_upper,
        "drift_zero": all(d == 0 for d in drifts),
        "lag_p95": (percentile(lags, 0.95) <= params.lag_p95_s and failed_writes == 0)
        if (lags or failed_writes)
        else True,
        "quick_check": all(r["quick_check"] == "ok" for r in res) if res else True,
        "memory_trend": (
            mem_trend is None
            or bool(mem_trend["holds_zero"])
            or growth < params.memory_growth_mb_per_day
        ),
        "latency_trend": latency_trend is None or bool(latency_trend["holds_zero"]),
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "queries": len(queries),
        "errors": errors,
        "error_rate_upper": round(upper, 5),
        "daily_p95_ms": {d: round(v, 1) for d, v in daily_p95.items()},
        "lag_p95_s": round(percentile(lags, 0.95), 2) if lags else None,
        "failed_writes": failed_writes,
        "drift_max": max((abs(d) for d in drifts), default=0),
        "memory_growth_mb_per_day": round(growth, 2),
    }
