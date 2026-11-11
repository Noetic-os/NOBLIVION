# SPDX-License-Identifier: AGPL-3.0-or-later
"""Warm-up of a harness store, and the hook latch.

A hook call that waits longer than its budget for the store writes ``store.hung`` in
the data dir. For the next 30 seconds the hooks send nothing to that store and answer
empty (``fail:store_hung``). A session meets that as one slow prompt. A replay sends
its whole shard inside those 30 seconds, so one slow call empties the arm: the rows
then measure the latch, not the build.

What makes a call slow is an embedding backlog, not a cold store: while chunks wait
for their vector the store embeds them and answers late (measured at 5,000 notes with
948 chunks to embed: 0 of 60 on the recall tool; with none: 60 of 60, and 52 of 60 on
the prompt). So a store is ready only when no chunk lacks a vector
(``Harness.start``), and then it is warmed: the recall tool and the prompt hook must
both answer inside the hook budget, and the stamp is removed.

Every worker answer says whether its call ran under the latch
(``worker.latch_in_force``) and what the hook gave as its fail reason. A row that ran
under the latch, or whose hook gave up on the store, is invalid: it is not a miss of
the build. A shard with such a row is not judged (``LatchedShard``).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

HOOK_BUDGET_S = 2.0  # the prompt hook's total time for the store (recall.timeout_s)
LATCH_FILE = "store.hung"
LATCHED = "store_hung"  # the hook's fail reason while the stamp is in force
GAVE_UP = "timeout"  # the hook's fail reason when the store took longer than the budget
INVALID_FAILS = (LATCHED, GAVE_UP)  # the row measured the wait for the store, not the build
WARM_BOUND_S = 60.0  # a store with no backlog that is not warm after this long is at fault
WARM_PAUSE_S = 1.0
WARM_OPS = ("pull", "prompt")  # the recall tool and the prompt hook: both are measured
# Not a prompt of the fixture: the warm-up must not prime a query that is measured.
WARM_QUERY = "warm-up call of the long-run harness: which note names the build order"


class WarmupError(RuntimeError):
    """No round answered inside the hook budget before the bound."""


class LatchedShard(RuntimeError):
    """Rows of this shard are invalid (the hook latch, or a hook that gave up on the
    store): the shard is run again, not judged."""


def clear_latch(data_dir: Path) -> bool:
    """Remove the ``store.hung`` stamp of a harness data dir. True when it was there."""
    try:
        (Path(data_dir) / LATCH_FILE).unlink()
        return True
    except FileNotFoundError:
        return False


def in_budget(answer: dict[str, Any], budget_s: float) -> bool:
    """Did this worker answer come from the store, inside the hook budget."""
    return bool(answer.get("ok")) and not answer.get("fail") and answer["ms"] <= budget_s * 1000


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
    """Rounds of one recall-tool call and one prompt, until both of a round answer from
    the store inside ``budget_s``, for at most ``bound_s`` seconds; then remove the
    latch stamp. The stamp is removed before every call as well, so each call reaches
    the store. Call it on a store with no embedding backlog (``Harness.start``).
    Returns the rounds, the times of the good round and the seconds the warm-up took."""
    began = clock()
    rounds = 0
    while True:
        rounds += 1
        got: dict[str, dict[str, Any]] = {}
        for op in WARM_OPS:
            clear_latch(data_dir)
            text = {"query": WARM_QUERY, "k": 5} if op == "pull" else {"prompt": WARM_QUERY}
            got[op] = worker.call(op, sid=f"warm-{op}-{rounds}", cwd=cwd, **text)
        if all(in_budget(r, budget_s) for r in got.values()):
            break
        if clock() - began >= bound_s:
            said = {
                op: f"{r.get('ms')} ms, fail {r.get('fail') or r.get('error')!r}"
                for op, r in got.items()
            }
            raise WarmupError(
                f"no round with both answers inside {budget_s:g} s in {rounds} rounds over "
                f"{bound_s:g} s: last round {said}"
            )
        sleep(pause_s)
    clear_latch(data_dir)
    return {
        "warm_tries": rounds,
        "warm_ms": round(float(got["pull"]["ms"]), 1),
        "warm_prompt_ms": round(float(got["prompt"]["ms"]), 1),
        "warm_s": round(clock() - began, 2),
    }


def invalid_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows that are not a result of the build: the call ran under the latch, or
    the hook gave up on the store (``INVALID_FAILS``). An empty answer of such a row
    is not a miss."""
    return [r for r in rows if r.get("latched") or r.get("fail") in INVALID_FAILS]


def fail_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
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
