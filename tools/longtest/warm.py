# SPDX-License-Identifier: AGPL-3.0-or-later
"""Warm-up of a harness store, and the hook latch.

A hook call that waits longer than its budget for the store writes ``store.hung`` in
the data dir. For the next 30 seconds the hooks send nothing to that store and answer
empty (``fail:store_hung``). A session meets that as one slow prompt. A replay sends
its whole shard inside those 30 seconds, so one slow first call on a cold store
empties the arm: the rows then measure the latch, not the build.

So a store is warmed before it is measured: one call must answer inside the hook
budget, and the stamp is removed. Every worker answer says whether its call ran under
the latch (``worker.latch_in_force``) and what the hook gave as its fail reason. A
shard with a latched row is not judged (``LatchedShard``).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOOK_BUDGET_S = 2.0  # the prompt hook's total time for the store (recall.timeout_s)
LATCH_FILE = "store.hung"
LATCHED = "store_hung"  # the hook's fail reason while the stamp is in force
WARM_BOUND_S = 300.0  # a store that gives no answer inside the budget for this long is not warm
WARM_PAUSE_S = 1.0
# Not a prompt of the fixture: the warm-up must not prime a query that is measured.
WARM_QUERY = "warm-up call of the long-run harness: which note names the build order"


class WarmupError(RuntimeError):
    """No call answered inside the hook budget before the bound."""


class LatchedShard(RuntimeError):
    """Rows of this shard ran under the hook latch: the shard is run again, not judged."""


def clear_latch(data_dir: Path) -> bool:
    """Remove the ``store.hung`` stamp of a harness data dir. True when it was there."""
    try:
        (Path(data_dir) / LATCH_FILE).unlink()
        return True
    except FileNotFoundError:
        return False


def warm_up(
    worker: Any,
    data_dir: Path,
    cwd: str,
    budget_s: float = HOOK_BUDGET_S,
    bound_s: float = WARM_BOUND_S,
    pause_s: float = WARM_PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Call the recall tool until one call answers from the store inside ``budget_s``,
    for at most ``bound_s`` seconds; then remove the latch stamp. The stamp is removed
    before every try as well, so each try reaches the store. Returns the tries, the
    time of the good call and the seconds the warm-up took."""
    began = clock()
    tries, last = 0, {}
    while True:
        clear_latch(data_dir)
        tries += 1
        last = worker.call("pull", sid=f"warm-{tries}", cwd=cwd, query=WARM_QUERY, k=5)
        if last.get("ok") and not last.get("fail") and last["ms"] <= budget_s * 1000:
            break
        if clock() - began >= bound_s:
            raise WarmupError(
                f"no answer inside {budget_s:g} s in {tries} tries over {bound_s:g} s: "
                f"last call {last.get('ms')} ms, fail {last.get('fail') or last.get('error')!r}"
            )
        sleep(pause_s)
    clear_latch(data_dir)
    return {
        "warm_tries": tries,
        "warm_ms": round(float(last["ms"]), 1),
        "warm_s": round(clock() - began, 2),
    }


def latched_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("latched") or r.get("fail") == LATCHED]


def fail_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Fail reason -> rows, over the rows that carry one."""
    return dict(sorted(Counter(r["fail"] for r in rows if r.get("fail")).items()))


class HookCalls:
    """The prompt hook as a benchmark call: returns milliseconds and keeps what the hook
    said, so a latency row can show that it measured the store and not the latch."""

    def __init__(self, worker: Any, cwd: str, tag: str) -> None:
        self.worker, self.cwd, self.tag = worker, cwd, tag
        self.n = self.latched = 0
        self.fails: Counter[str] = Counter()

    def __call__(self, prompt: str) -> float:
        self.n += 1
        r = self.worker.call("prompt", sid=f"{self.tag}-{self.n}", cwd=self.cwd, prompt=prompt)
        if r.get("latched") or r.get("fail") == LATCHED:
            self.latched += 1
        if r.get("fail"):
            self.fails[r["fail"]] += 1
        return r["ms"]

    def validity(self) -> dict[str, Any]:
        """The fields a latency row carries: ``valid`` is false when a call was latched."""
        return {
            "latched": self.latched,
            "fails": dict(sorted(self.fails.items())),
            "valid": self.latched == 0,
        }
