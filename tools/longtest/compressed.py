# SPDX-License-Identifier: AGPL-3.0-or-later
"""The time-compressed run: the growth steps of the large-project run, back to back.

The nightly run waits 24 hours between two growth points and replays inside a heavy
window. Nothing in the replay needs that time: a step grows each kept store by the
profile's notes, starts and warms one store at a time, replays every variant for the
build and the stock arm, and checks that each arm fired on the sanity set. A step is
written as a night (``day`` is the step number), so the report judges the steps as it
judges nights: DR1 and DR3 on the last step, DR6 over all of them.

The run resumes after a kill. ``out/growth.jsonl`` keeps a step from growing twice,
``out/replay-done.jsonl`` keeps a done shard from replaying twice, and
``out/steps.jsonl`` holds one row per finished step.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import growth, live, profiles, report, sandbox
from .buildrunner import store_python
from .runner import Run, replay_all, replay_rows, stock_expected
from .scheduler import job_note
from .util import append_jsonl, read_jsonl

RETRIES = 2  # a step whose shard was latched, or whose store was not warm, runs again


def steps_done(run: Run) -> dict[int, dict[str, Any]]:
    return {int(r["step"]): r for r in read_jsonl(run.out / "steps.jsonl")}


def step_gate(
    run: Run,
    prof: profiles.Profile,
    step: int,
    sizes: list[int],
    shards: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """The validity gate of one step: ``size -> {"ok", "causes", "arms"}``."""
    arms = (*prof.builds, *prof.stock_arms)
    expected = stock_expected(run.fixture(), sizes, shards, limit, prof.variants)
    gate = report.validity(
        replay_rows(run, step), arms=arms, params=prof.params, sizes=sizes, expected=expected
    )
    return {
        size: {"ok": g["ok"], "causes": g["causes"], "arms": g["steps"].get(str(step), {})}
        for size, g in gate.items()
    }


def builds_fired(gate: dict[str, Any], prof: profiles.Profile) -> bool:
    """Did every build reach its floor at every scale of the step. The stock arm is a
    file simulation: a run again cannot change it, so a failed mechanical gate does not
    stop the run; the report still gives INVALID for it."""
    return all(g["arms"].get(b, {}).get("ok") for g in gate.values() for b in prof.builds)


def run_step(
    run: Run,
    prof: profiles.Profile,
    step: int,
    sizes: list[int],
    now: Callable[[], dt.datetime],
    retries: int = RETRIES,
    shards: int | None = None,
    limit: int | None = None,
    wait_s: float = 900.0,
) -> dict[str, Any]:
    """One step: grow, replay (start, warm, all variants; one store at a time), gate.
    A replay that fails (a latched shard, a store that is not warm in time) is logged
    to ``out/failures.jsonl`` and run again at once, ``retries`` times: the tasks that
    are done are not replayed. Returns the row written to ``out/steps.jsonl``."""
    began = time.monotonic()
    grown = [growth.grow(run, size, step, prof.growth_per_day, now=now()) for size in sizes]
    attempt = 0
    while True:
        attempt += 1
        try:
            replay_all(
                run,
                sizes,
                shards,
                limit,
                variants=prof.variants,
                builds=prof.builds,
                parallel=1,
                day=step,
                wait_s=wait_s,
                python=store_python(Path(run.manifest["live"]["data_dir"])),
                guard_site=run.dir / "private" / "guard-site",
                stock_arms=prof.stock_arms,
                stock_dir=lambda size, shard: growth.stock_dir(run, size, shard),
            )
            break
        except Exception as exc:  # noqa: BLE001 - logged, then again or raised
            append_jsonl(
                run.out / "failures.jsonl",
                {
                    "ts": now().isoformat(timespec="seconds"),
                    "job": f"step-{step}",
                    "error": job_note(exc),
                    "attempt": attempt,
                },
            )
            if attempt > retries:
                raise
    gate = step_gate(run, prof, step, sizes, shards, limit)
    row = {
        "step": step,
        "ts": now().isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - began, 1),
        "attempts": attempt,
        "added": {str(g["size"]): g.get("added", 0) for g in grown},
        "gate": gate,
        "ok": all(g["ok"] for g in gate.values()),
        "builds_fired": builds_fired(gate, prof),
    }
    run.append("steps", row)
    return row


def run_steps(
    run: Run,
    steps: int | None = None,
    now: Callable[[], dt.datetime] | None = None,
    retries: int = RETRIES,
    keep_going: bool = False,
    shards: int | None = None,
    limit: int | None = None,
    wait_s: float = 900.0,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Steps 1 to ``steps`` (default: the steps of the profile), each once. A step that
    is in ``out/steps.jsonl`` is skipped, so a run that was killed goes on where it
    stopped. The run stops when a build did not fire on a step (``keep_going`` goes on)
    and when a ``stop`` file is in the run folder. The zero-token guards of the nightly
    loop are on for the whole run."""
    from .loop import sandbox_program

    prof = profiles.profile_of(run.manifest)
    if not prof.steps:
        raise SystemExit(f"profile {prof.name} runs nights, not steps: use `run`")
    now = now or (lambda: dt.datetime.now().astimezone())
    steps = steps or prof.steps
    sizes = list(run.manifest["sizes"])
    check = sandbox.start_check(os.environ, sandbox_program())
    if not check["ok"]:
        raise SystemExit(f"start check failed: {check}")
    cfg, data = (Path(run.manifest["live"][k]) for k in ("config_dir", "data_dir"))
    guard = sandbox.OpenGuard(live.live_paths_guard(cfg, data), allow_write=[run.dir])
    sockets = sandbox.SocketGuard()
    log = run.dir / "private" / "external_connects.log"
    base = sandbox.count_refused(log)
    guard.install()
    sockets.install()
    ran: list[int] = []
    stopped = ""
    try:
        for step in range(1, steps + 1):
            if step in steps_done(run):
                continue
            if (run.dir / "stop").exists():
                stopped = "stop file"
                break
            row = run_step(run, prof, step, sizes, now, retries, shards, limit, wait_s)
            ran.append(step)
            if on_step:
                on_step(row)
            if not row["builds_fired"] and not keep_going:
                stopped = f"step {step}: a build did not fire on the sanity set"
                break
    finally:
        guard.uninstall()
        sockets.uninstall()
        run.append(
            "sandbox",
            {
                "ts": now().isoformat(timespec="seconds"),
                "day": now().date().isoformat(),
                "kernel_net_isolated": sandbox.net_isolated(),
                "external_connects": sockets.refused + sandbox.count_refused(log) - base,
                "claude_processes": sandbox.claude_processes(os.getpid()),
                "refused_opens": guard.refused_reads,
                "refused_writes": guard.refused_writes,
            },
        )
    done = steps_done(run)
    return {
        "steps": steps,
        "ran": ran,
        "done": sorted(done),
        "complete": all(s in done for s in range(1, steps + 1)),
        "stopped": stopped,
        "valid_steps": sorted(s for s, r in done.items() if r["ok"]),
        "invalid": {str(s): r["gate"] for s, r in sorted(done.items()) if not r["ok"]},
    }
