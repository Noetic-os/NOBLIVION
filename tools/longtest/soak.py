# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm d: a harness store under load for days, and the nightly latency benchmark.

Everything runs against a harness store (its own data folder and token), never the
live one. A step is small and returns plain numbers; the scheduler calls the steps
and appends their rows to ``out/soak.jsonl``.

* ``query_load``: recall requests through the store, in parallel for a burst.
* ``churn``: writes, edits and deletes of notes in a memory folder, then the
  write-to-searchable lag of each new note.
* ``drift``: notes on disk against the notes the store counts, read once the two
  have stopped moving (``settled_pair``).
* ``resources``: store memory (``/proc``), database and WAL size, ``quick_check``.
* ``latency_benchmark``: the prompt hook, ``n`` prompts, p50 and p95, for the table
  of the nightly benchmark.
* ``evaluate``: DR8 over the rows of the days so far.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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

    def health(self) -> tuple[bool, dict[str, Any]]:
        status, body, _ = self._call("/api/health", 10)
        return status == 200, body

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
    the write until its marker is searchable, and the lag of a deleted note the time
    from the delete until its marker is searchable no more. Both are waited out here,
    so a reading taken after a churn (``drift``) sees a store that has caught up and
    not one still working through the change."""
    rows: list[dict[str, Any]] = []
    # a nonce: a restart begins its serial at 1 again, and a marker that is in the store
    # already would be found at once and give a lag of zero
    token = f"zqx{serial:06d}{uuid.uuid4().hex[:8]}marker"
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
        gone = marker_of(victim.read_text(encoding="utf-8"))
        before = counted_memories(client)
        victim.unlink()
        sync(victim)
        rows.append(
            {
                "kind": "delete",
                "lag_s": (
                    None
                    if gone is None
                    else gone_lag(client, gone, project, timeout_s, before=before)
                ),
                "ok": True,
            }
        )
        rows[-1]["ok"] = rows[-1]["lag_s"] is not None
    if old:
        target = old[-1]
        target.write_text(note_text(token + "edit", serial) + "\nEdited.\n", encoding="utf-8")
        sync(target)
        rows.append({"kind": "edit", "ok": True})
    return rows


MARKER = re.compile(r"zqx\d{6}[0-9a-f]{8}marker")


def marker_of(text: str) -> str | None:
    """The marker of a churn note (``note_text``). An edited note carries the same
    marker with ``edit`` after it, so the pattern stops at ``marker``."""
    found = MARKER.search(text)
    return found.group(0) if found else None


def gone_lag(
    client: StoreClient,
    marker: str,
    project: str,
    timeout_s: float,
    poll_s: float = 0.25,
    before: int | None = None,
) -> float | None:
    """Seconds from now until the store has registered the delete, or ``None`` when it
    has not after ``timeout_s``. The store registers a delete a moment after the file
    goes, the same way it registers a write a moment after it lands. Registered means
    two things at once: ``marker`` is no longer searchable, and the store counts one
    note fewer than ``before``, the count read just before the file went. The search
    alone is not enough: the deleted note was edited after its write and carries the
    marker with a suffix, so only the vector path still finds it, and that path drops
    it from a short result at times. A reading that trusts the search then returns
    before the scan ran, and the drift reading after it sees the pending delete as a
    note the store keeps (NOBLIVION-213). With ``before`` unknown (the health call
    failed) the search is all there is."""
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        status, body, _ = client.search(marker, project, 3)
        if status == 200 and marker not in json.dumps(body):
            counted = None if before is None else counted_memories(client)
            if before is None or (counted is not None and counted <= before - 1):
                return round(time.monotonic() - started, 2)
        time.sleep(poll_s)
    return None


def counted_memories(client: StoreClient) -> int | None:
    """The notes the store counts, or ``None`` when the health call failed or carries
    no count. ``read_pair`` raises on the same answer; here the caller is a churn that
    must not raise (the kill test cuts the store off mid-churn by design)."""
    ok, health = client.health()
    if not ok or "memories" not in health:
        return None
    return int(health["memories"])


def disk_notes(folders: Sequence[Path]) -> int:
    return sum(
        1
        for f in folders
        for p in Path(f).glob("*.md")
        if not p.name.startswith(("MEMORY", "topic_"))
    )


class HealthCallFailed(Exception):
    """The health call itself failed: a bad status, a body with no ``memories``, or a
    body that counts no memories at all while the baseline counted some (a store that
    is still coming up answers 200 with ``memories: 0``). Not a drift reading: the
    scheduler logs this as a failed job, the way it logs any other failed handler,
    instead of ``soak_drift`` writing a row that folds the failure into the drift
    value."""


def read_pair(
    client: StoreClient, folders: Sequence[Path], baseline: dict[str, int]
) -> tuple[int, int, str]:
    """One reading: notes on disk, notes the store counts, and what the index is doing."""
    ok, health = client.health()
    if not ok or "memories" not in health:
        raise HealthCallFailed(f"health call failed: ok={ok} body={health!r}")
    counted = int(health["memories"])
    if counted == 0 and baseline.get("memories", 0) > 0:
        raise HealthCallFailed("the store counts no memories at all: it is not up yet")
    return disk_notes(folders), counted, str(health.get("index_state", "idle"))


def settled_pair(
    client: StoreClient,
    folders: Sequence[Path],
    baseline: dict[str, int],
    settle_s: float = 20.0,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int, bool]:
    """The notes on disk and the notes the store counts, read once the two have
    stopped moving: two readings in a row that agree, with the index idle. The store
    registers a write or a delete seconds after the file changes, so a reading taken
    inside that window is the lag of the index, not drift; under the churn of a
    compressed week that window is where most readings would land. Gives up after
    ``settle_s`` and says so, so a store that truly never catches up is reported and
    not hidden."""
    disk, counted, state = read_pair(client, folders, baseline)
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        sleep(poll_s)
        again = read_pair(client, folders, baseline)
        same = again[:2] == (disk, counted)
        disk, counted, state = again
        if same and state in ("idle", ""):
            return disk, counted, True
    return disk, counted, False


def drift(
    client: StoreClient,
    folders: Sequence[Path],
    baseline: dict[str, int],
    settle_s: float = 20.0,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Index drift: the change in notes on disk since the baseline, minus the change
    in notes the store counts. 0 when the store follows the disk. The pair is read
    settled (``settled_pair``), and the row says whether it settled. Raises
    ``HealthCallFailed`` when the health call itself failed: that is a separate
    failure mode, not evidence of drift."""
    disk, counted, settled = settled_pair(client, folders, baseline, settle_s, poll_s, sleep)
    value = (disk - baseline["disk"]) - (counted - baseline["memories"])
    return {
        "kind": "drift",
        "disk": disk,
        "memories": counted,
        "drift": value,
        "settled": settled,
    }


def baseline(
    client: StoreClient,
    folders: Sequence[Path],
    settle_s: float = 20.0,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """The pair every later drift reading is measured against. It is read settled: a
    baseline taken while the store is still following the disk is off by the notes in
    flight, and every later reading carries that error for the whole run. Raises
    ``HealthCallFailed`` when it does not settle, rather than starting a run on a
    number that is already wrong."""
    disk, counted, settled = settled_pair(client, folders, {}, settle_s, poll_s, sleep)
    if not settled:
        raise HealthCallFailed(
            f"the store had not caught up with the disk after {settle_s} s: "
            f"disk={disk} memories={counted}"
        )
    return {"disk": disk, "memories": counted}


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


def queries_needed(upper: float) -> int:
    """The fewest error-free requests whose Wilson upper bound is under ``upper``."""
    n = 1
    while stats.wilson(0, n)[1] >= upper:
        n += 1
    return n


def evaluate(
    rows: Sequence[dict[str, Any]], days: Sequence[str] | None = None, params: Params = PARAMS
) -> dict[str, Any]:
    """DR8 over soak rows. Rows hold ``day``, ``kind`` and the values above. Per-day
    p95 against each budget, the error rate (Wilson upper bound), drift at every
    settled check, lag p95, Theil-Sen slopes of latency and memory, and quick_check."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    days = sorted(by_day) if days is None else list(days)
    queries = [r for r in rows if r["kind"] == "query"]
    errors = sum(1 for r in queries if not r["ok"])
    upper = stats.wilson(errors, len(queries))[1] if queries else 1.0
    # A short run cannot show a 0.5% error bound: with no error it is underpowered, not
    # failed. The rule counts again as soon as the request count could prove it.
    enough = len(queries) >= queries_needed(params.error_rate_upper)
    # The budget is for steady load. The hourly burst of 20 parallel requests queues in
    # the store (one search takes about 0.35 s at 20,000 notes, so the last of 20 waits
    # about 7 s); its tail is reported as burst_p95_ms and its errors count in full.
    steady = lambda d: [  # noqa: E731
        r["ms"] for r in by_day.get(d, []) if r["kind"] == "query" and not r.get("burst")
    ]
    burst = lambda d: [  # noqa: E731
        r["ms"] for r in by_day.get(d, []) if r["kind"] == "query" and r.get("burst")
    ]
    daily_p95 = {d: percentile(steady(d), 0.95) for d in days}
    burst_p95 = {d: round(percentile(burst(d), 0.95), 1) for d in days if burst(d)}
    lags = [r["lag_s"] for r in rows if r["kind"] == "write" and r.get("lag_s") is not None]
    failed_writes = sum(1 for r in rows if r["kind"] == "write" and not r["ok"])
    # The error rate of one day, over query and write rows only: a drift, edit or
    # delete row carries no real pass/fail of its own and must not move this rate.
    graded = lambda d: [  # noqa: E731
        r for r in by_day.get(d, []) if r["kind"] in ("query", "write")
    ]
    error_rate_by_day = {}
    for d in days:
        rs = graded(d)
        errs = sum(1 for r in rs if not r["ok"])
        error_rate_by_day[d] = {
            "n": len(rs),
            "errors": errs,
            "rate": round(errs / len(rs), 5) if rs else None,
        }
    # A drift row that did not settle holds a reading of a store still working through
    # a change: its value is the lag of the index, so it is counted as unsettled and
    # not as drift. A row from before this rule has no ``settled`` key and counts as
    # settled, the way it was judged when it was written.
    drift_rows = [r for r in rows if r["kind"] == "drift"]
    unsettled = sum(1 for r in drift_rows if not r.get("settled", True))
    drifts = [r["drift"] for r in drift_rows if r.get("settled", True)]
    res = [r for r in rows if r["kind"] == "resources"]
    xs = list(range(len(daily_p95)))
    latency_trend = stats.theil_sen(xs, [daily_p95[d] for d in days]) if len(days) >= 3 else None
    mem_x = [i for i, _ in enumerate(res)]
    mem_trend = stats.theil_sen(mem_x, [r["rss_mb"] for r in res]) if len(res) >= 3 else None
    span_days = max(1, len(days))
    growth = (res[-1]["rss_mb"] - res[0]["rss_mb"]) / span_days if len(res) >= 2 else 0.0
    checks = {
        "budget_recall": all(v <= params.budget_recall_ms for v in daily_p95.values()),
        "error_rate": upper < params.error_rate_upper or (not enough and errors == 0),
        "drift_zero": all(d == 0 for d in drifts),
        "drift_settled": unsettled == 0,
        "lag_p95": percentile(lags, 0.95) <= params.lag_p95_s if lags else True,
        "writes_searchable": failed_writes == 0,
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
        "error_rate_underpowered": not enough,
        "lag_samples": len(lags),
        "daily_p95_ms": {d: round(v, 1) for d, v in daily_p95.items()},
        "burst_p95_ms": burst_p95,
        "lag_p95_s": round(percentile(lags, 0.95), 2) if lags else None,
        "failed_writes": failed_writes,
        "drift_max": max((abs(d) for d in drifts), default=0),
        "drift_unsettled": unsettled,
        "memory_growth_mb_per_day": round(growth, 2),
        "error_rate_by_day": error_rate_by_day,
    }
