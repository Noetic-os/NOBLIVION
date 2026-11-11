# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan 4 Part C, claim W3 (health): the compressed soak mix (C2), cold start (C3)
and the kill test (C4).

C2 runs the registered 7-day operation mix at a chosen speed-up, on one store alone,
and feeds the rows it collects to ``soak.evaluate`` -- the same judging code and the
same thresholds as the real-time soak (``config.PARAMS``), so a compressed day is
judged exactly as a real day of the 7-day run would be. The mix itself (which
operation fires when) is new: ``soak.py`` gives the operations, this module gives
their schedule and interleaving.

C3 and C4 are short and scripted, not time-compressed: they call the store's start
and search once, directly, and summarize the trials.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import soak
from .config import PARAMS, Params
from .util import summarize_ms

# The registered 7-day operation mix (PLAN4 Part C2): about 12,300 queries, of which
# about 250 bursts of 20; about 660 writes, 660 edits and close to 660 deletes. One
# churn call (``soak.churn``) writes one note and edits one every time, but deletes
# one only once more than 3 notes are kept (``soak.py``:126) -- so 660 churns give
# 660 writes and 660 edits, and all but the first few deletes.
MIX: dict[str, int] = {
    "queries": 12_300,
    "burst_events": 250,
    "burst_size": 20,
    "churns": 660,
}
WEEK_S = 7 * 86400.0
CHECK_EVERY_S = 3600.0  # the real-time interval of soak_resources and soak_drift
DAYS = 7


def day_of(fire_s: float, compressed_s: float, days: int = DAYS) -> int:
    """The virtual day (1 to ``days``) that ``fire_s`` falls in, over a compressed
    week of ``compressed_s`` seconds. Never above ``days``: rounding on the last
    event of a kind can place it a hair past the week's own length."""
    day_s = compressed_s / days
    return min(days, int(fire_s // day_s) + 1)


def compressed_schedule(
    mix: Mapping[str, int] = MIX,
    speed: float = 20.0,
    week_s: float = WEEK_S,
    check_every_s: float = CHECK_EVERY_S,
    days: int = DAYS,
) -> tuple[list[dict[str, Any]], float]:
    """The events of one compressed week, in order: ``fire_s`` (seconds after the run
    begins) and ``day`` (the virtual day for judging). Each kind is spread evenly
    across the week on its own clock, then every kind is merged and sorted by
    ``fire_s`` -- the mix interleaves the way a real week does, not kind by kind.
    Returns the events and the compressed week's length in seconds."""
    compressed_s = week_s / speed
    steady = mix["queries"] - mix["burst_events"] * mix["burst_size"]
    if steady < 0:
        raise ValueError("burst queries outnumber the registered total of queries")
    checks = max(1, int(week_s / check_every_s))
    counts = {
        "query": steady,
        "burst": mix["burst_events"],
        "churn": mix["churns"],
        "resources": checks,
        "drift": checks,
    }
    events: list[dict[str, Any]] = []
    for kind, n in counts.items():
        for i in range(n):
            fire_s = compressed_s * (i + 0.5) / n
            day = day_of(fire_s, compressed_s, days)
            events.append({"fire_s": fire_s, "kind": kind, "day": day})
    events.sort(key=lambda e: e["fire_s"])
    return events, compressed_s


def run_compressed(
    events: Sequence[Mapping[str, Any]],
    ops: Mapping[str, Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]],
    sink: Callable[[dict[str, Any]], None],
    elapsed: Callable[[], float],
    sleep: Callable[[float], None] = time.sleep,
    pause: Callable[[], None] = lambda: None,
    poll_s: float = 1.0,
    done: Sequence[int] = (),
    on_done: Callable[[int], None] | None = None,
) -> list[int]:
    """Runs ``events`` (``compressed_schedule``'s output) in order against
    ``elapsed()`` (seconds since the run began). Before each wait and while it lasts,
    ``pause()`` runs first: a caller that must not do measured work right now (the
    quiet window, PLAN4 rule 4) sleeps there instead, outside the event's own wait.
    ``done`` holds indices already run, so a restart after a kill goes on where it
    stopped rather than from the top; ``on_done(i)`` fires right after event ``i``
    finishes, not only once this call returns, so a caller that persists it (a
    progress file) loses at most the one event in flight to a kill, not the whole
    run. A failed op (an exception, including ``soak.HealthCallFailed``) does not end
    the run: the row it would have given becomes one ``ok: False`` error row instead,
    tagged with its ``kind``. Each row that an op returns is tagged with the virtual
    ``day`` (``"vday-<n>"``, so ``soak.evaluate`` groups it), the schedule ``slip_s``
    (how far ``elapsed()`` ran past the event's own ``fire_s`` -- a sign the 20x pace
    is not holding) and, for a burst, ``burst: True`` -- the same tag ``soak.py``'s
    own caller adds (``loop.py``). Returns the indices this call ran, in the order it
    ran them."""
    ran: list[int] = []
    skip = set(done)
    for i, event in enumerate(events):
        if i in skip:
            continue
        pause()
        while elapsed() < event["fire_s"]:
            sleep(min(poll_s, event["fire_s"] - elapsed()))
            pause()
        try:
            produced = ops[event["kind"]](event)
        except Exception as exc:  # noqa: BLE001 - a failed op must not end the run
            produced = [
                {"kind": event["kind"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ]
        slip_s = round(elapsed() - event["fire_s"], 1)
        for row in produced:
            tagged = {**row, "day": f"vday-{event['day']}", "slip_s": slip_s}
            if event["kind"] == "burst":
                tagged["burst"] = True
            sink(tagged)
        ran.append(i)
        if on_done:
            on_done(i)
    return ran


def c2_report(
    rows: Sequence[Mapping[str, Any]],
    mix: Mapping[str, int] = MIX,
    speed: float = 20.0,
    params: Params = PARAMS,
    days: int = DAYS,
) -> dict[str, Any]:
    """The C2 result: ``soak.evaluate`` over the collected rows, judged against the
    registered thresholds (``config.PARAMS``), plus the mix and speed that produced
    them. The write-to-searchable lag rule is a known product fault (NOBLIVION-192,
    measured 28 to 32 s in run-c against the 10 s rule); when it fails here too, the
    failure is named with its ticket rather than left as a bare ``False``.

    Two items PLAN4 Part C names that ``soak.evaluate`` does not judge, reported
    alongside it, not gated (PLAN4 names no numeric rule for either): ``memory_mb_
    per_1000_ops``, the RSS delta between the first and last resource reading over
    the query, write, edit and delete rows counted in between; and ``pace``, the
    schedule ``slip_s`` (``run_compressed``'s own tag) across every row -- how far
    behind the compressed clock the run fell, the measure of whether the registered
    ``speed`` actually held under the known NOBLIVION-192 lag (ticket cli.cmd_health_c2
    quantifies the risk: at 28 to 32 s a churn, 660 churns can use most of the
    compressed week's own length)."""
    day_labels = [f"vday-{n}" for n in range(1, days + 1)]
    judged = soak.evaluate(rows, days=day_labels, params=params)
    failed_rules = [name for name, ok in judged["checks"].items() if not ok]
    known = {
        "lag_p95": (
            "product fault, ticket NOBLIVION-192: write-to-searchable lag measured "
            "28 to 32 s in run-c against the 10 s rule (store.py around line 444-449); "
            "not fixed by this harness"
        ),
    }
    res = [r for r in rows if r["kind"] == "resources"]
    op_rows = sum(1 for r in rows if r["kind"] in ("query", "write", "edit", "delete"))
    memory_mb_per_1000_ops = (
        round((res[-1]["rss_mb"] - res[0]["rss_mb"]) / (op_rows / 1000), 3)
        if len(res) >= 2 and op_rows
        else None
    )
    slips = [r["slip_s"] for r in rows if r.get("slip_s") is not None]
    return {
        "mix": dict(mix),
        "speed": speed,
        "days": days,
        "evaluate": judged,
        "failed_rules": {name: known.get(name, "no known cause on file") for name in failed_rules},
        "memory_mb_per_1000_ops": memory_mb_per_1000_ops,
        "pace": {"slip_s": summarize_ms(slips) if slips else None},
    }


def cold_start_trial(
    start: Callable[[], float],
    search: Callable[[], tuple[int, Mapping[str, Any], float]],
    budget_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One cold start (PLAN4 C3): ``start()`` starts the store from a cold process and
    returns the seconds it took to become ready (``Harness.start``'s own timing); the
    first search follows at once. ``within_budget`` is the rule under test: the first
    answer inside 2 s (``warm.HOOK_BUDGET_S``). ``got_rows`` is false when the first
    answer carries no result row: an empty answer inside the budget is not a miss of
    the budget, it is a separate thing to report (the share of first prompts with no
    rows, PLAN4 C3)."""
    t0 = clock()
    ready_s = start()
    status, body, ms = search()
    results = body.get("results") or []
    return {
        "ready_s": round(ready_s, 2),
        "first_answer_ms": round(ms, 1),
        "first_answer_s": round(clock() - t0, 2),
        "within_budget": status == 200 and ms <= budget_s * 1000,
        "got_rows": status == 200 and bool(results),
    }


def cold_start_summary(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Across the starts of one scale: the share inside the 2 s budget, the share
    whose first prompt got no rows, and the spread of the time to the first answer.
    ``pass`` needs every trial inside the budget -- PLAN4 names no tolerance for a
    slow first answer, only reports the no-rows share next to it."""
    n = len(trials)
    within = sum(1 for t in trials if t["within_budget"])
    no_rows = sum(1 for t in trials if not t["got_rows"])
    return {
        "n": n,
        "within_budget": within,
        "within_budget_share": round(within / n, 3) if n else None,
        "no_rows": no_rows,
        "no_rows_share": round(no_rows / n, 3) if n else None,
        "pass": n > 0 and within == n,
        "first_answer_s": summarize_ms([t["first_answer_s"] for t in trials]) if trials else None,
    }


def kill_test_evaluate(
    resources_row: Mapping[str, Any], drift_row: Mapping[str, Any]
) -> dict[str, Any]:
    """PLAN4 C4: after ``kill -9`` during a write burst and a restart, ``quick_check``
    and drift must still read clean. Takes the rows ``soak.resources`` and
    ``soak.drift`` give once the store is back up."""
    quick_check_ok = resources_row["quick_check"] == "ok"
    drift_zero = drift_row["drift"] == 0
    return {
        "quick_check": resources_row["quick_check"],
        "quick_check_ok": quick_check_ok,
        "drift": drift_row["drift"],
        "drift_zero": drift_zero,
        "pass": quick_check_ok and drift_zero,
    }
