# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job loop of the 7-day run: handlers for the scheduler.

Core unit: the soak store of B1 (arm d), the live pass (arm a, files only), the
sandbox watch, the nightly replay (arms b and c) and the daily check. The handlers
only append counts to ``out/*.jsonl``; they never write outside the run folder.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import live, realreplay, report, sandbox, soak
from .buildrunner import Harness, Worker, store_python
from .config import PARAMS
from .runner import (
    Run,
    bench_kept_store,
    determinism_hash,
    real_replay,
    replay_all,
    replay_rows,
)
from .scheduler import Job, Scheduler
from .util import write_private


class Loop:
    def __init__(
        self,
        run: Run,
        soak_size: int,
        now: Callable[[], dt.datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        params=PARAMS,
        notify: Callable[[str], None] | None = None,
        replay_parallel: int = 2,
    ) -> None:
        self.run, self.soak_size, self.sleep, self.params = run, soak_size, sleep, params
        self.notify = notify
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.sched = Scheduler(run.dir, self.now, params=params)
        self.replay_parallel = replay_parallel
        manifest = run.manifest
        self.live = live.Live(
            Path(manifest["live"]["config_dir"]),
            Path(manifest["live"]["data_dir"]),
            self.sched.state.setdefault("offsets", {}),
            manifest["run_id"],
        )
        self.guard = sandbox.OpenGuard(
            live.live_paths_guard(
                Path(manifest["live"]["config_dir"]), Path(manifest["live"]["data_dir"])
            ),
            allow_write=[run.dir],
        )
        self.sockets = sandbox.SocketGuard()
        self.passes: list[dict[str, Any]] = []
        self.harness: Harness | None = None
        self.client: soak.StoreClient | None = None
        self.worker: Worker | None = None
        self.base: dict[str, int] = {}
        self.serial = 0

    # -- setup ---------------------------------------------------------------------

    def started(self) -> dt.datetime | None:
        """When day 1 began: the moment the soak store was ready, kept in the state
        file so that a restart resumes the same clock. Not the time of ``init``."""
        text = self.sched.state.get("started")
        return dt.datetime.fromisoformat(text) if text else None

    def day(self) -> int:
        """The run day, in whole 24-hour periods from the start. The heavy window opens
        once per 24 hours, so every period holds exactly one night."""
        began = self.started()
        if began is None:
            return 1
        return int((self.now() - began).total_seconds() // 86400) + 1

    def over(self) -> bool:
        began = self.started()
        end = began + dt.timedelta(days=self.params.run_days) if began else None
        return end is not None and self.now() >= end

    def setup(self) -> None:
        run, fx = self.run, self.run.fixture()
        check = sandbox.start_check(os.environ, sandbox_program())
        if not check["ok"]:
            raise SystemExit(f"start check failed: {check}")
        self.guard.install()
        self.sockets.install()
        self.log = run.dir / "private" / "external_connects.log"
        self.log_base = sandbox.count_refused(self.log)  # tries before this run began
        live_data = Path(run.manifest["live"]["data_dir"])
        shard = fx.shards(self.soak_size)[0]
        root = run.dir / "stores" / "soak" / f"shard-{shard:02d}"
        self.harness = Harness(
            "B1",
            run.build_path("B1"),
            root,
            store_python(live_data),
            live_data / "models",
            settings=run.manifest.get("settings_env"),
        )
        self.harness.prepare(fx, self.soak_size, shard)
        self.harness.start()
        self.client = soak.StoreClient(self.harness.data)
        self.folders = sorted((self.harness.config_dir / "projects").glob("*/memory"))
        self.base = soak.baseline(self.client, self.folders)
        self.worker = Worker(self.harness)
        self.texts = [fx.prompt_of(q) for q in fx.queries_for(self.soak_size, ["V3"])][:60]
        if self.started() is None:
            self.sched.state["started"] = self.now().isoformat(timespec="seconds")
            self.sched.save()
        self.run.append(
            "sandbox",
            {
                **self.stamp(),
                "kernel_net_isolated": sandbox.net_isolated(),
                "external_connects": 0,
                "claude_processes": 0,
                "refused_opens": 0,
                "refused_writes": 0,
            },
        )

    def teardown(self) -> None:
        if self.worker:
            self.worker.close()
        if self.harness:
            self.harness.stop()
        self.guard.uninstall()
        self.sockets.uninstall()

    # -- handlers ------------------------------------------------------------------

    def handlers(self) -> dict[str, Callable[[], Any]]:
        return {
            "heartbeat": self.heartbeat,
            "live_pass": self.live_pass,
            "soak_load": self.soak_load,
            "soak_churn": self.soak_churn,
            "soak_drift": self.soak_drift,
            "soak_resources": self.soak_resources,
            "sandbox_watch": self.sandbox_watch,
            "replay_nightly": self.replay_nightly,
            "latency_bench": self.latency_bench,
            "daily_summary": self.daily_summary,
        }

    def stamp(self) -> dict[str, Any]:
        return {
            "ts": self.now().isoformat(timespec="seconds"),
            "day": self.now().date().isoformat(),
        }

    def heartbeat(self) -> None:
        self.run.append("heartbeat", self.stamp())

    def live_pass(self) -> None:
        self.passes.append(self.live.pass_once())
        self.run.append("live", {**self.stamp(), **live.daily(self.passes[-1:])})
        del self.passes[:-1]

    def soak_load(self) -> None:
        assert self.client
        burst = self.now().minute < 15  # one burst an hour, at the top of the hour
        parallel = self.params.soak_burst if burst else 1
        count = self.params.soak_burst if burst else max(1, self.params.soak_load_factor)
        rows = soak.query_load(
            self.client,
            [self.texts[(self.serial + i) % len(self.texts)] for i in range(count)],
            parallel,
        )
        self.serial += count
        for r in rows:
            self.run.append("soak", {**self.stamp(), **r, "burst": burst})

    def soak_churn(self) -> None:
        assert self.client and self.worker
        folder = self.folders[0]

        def sync(path: Path) -> None:
            self.worker.call(
                "sync",
                sid=f"sync-{self.serial}",
                cwd="/work/" + folder.parent.name.replace("-work-", "", 1),
                file_path=str(path),
            )

        self.serial += 1
        for r in soak.churn(folder, self.client, self.serial, sync):
            self.run.append("soak", {**self.stamp(), **r})

    def soak_drift(self) -> None:
        assert self.client
        self.run.append(
            "soak", {**self.stamp(), **soak.drift(self.client, self.folders, self.base)}
        )

    def soak_resources(self) -> None:
        assert self.harness and self.harness.store
        pid = self.harness.store.proc.pid if self.harness.store.proc else None
        self.run.append("soak", {**self.stamp(), **soak.resources(self.harness.data, pid)})

    def sandbox_watch(self) -> None:
        self.run.append(
            "sandbox",
            {
                **self.stamp(),
                "external_connects": self.sockets.refused
                + sandbox.count_refused(self.log)
                - self.log_base,
                "claude_processes": sandbox.claude_processes(os.getpid()),
                "refused_opens": self.guard.refused_reads,
                "refused_writes": self.guard.refused_writes,
            },
        )

    def latency_bench(self) -> None:
        """The nightly benchmark, 20 minutes at most: the prompt hook of B0 and of B1 at
        every size from 1,000 notes, on the stores that the replay keeps; and the soak
        store as it is, so that its row carries the churn."""
        assert self.worker
        cwd = "/work/" + self.folders[0].parent.name.replace("-work-", "", 1)
        counter = {"n": 0}

        def call(prompt: str) -> float:
            counter["n"] += 1
            return self.worker.call("prompt", sid=f"lb-{counter['n']}", cwd=cwd, prompt=prompt)[
                "ms"
            ]

        sizes = [s for s in self.run.manifest["sizes"] if s >= 1000]
        budget = self.params.bench_budget_s
        each = budget / (1 + 2 * len(sizes))
        row = soak.latency_benchmark(call, self.texts, n=100, budget_s=each)
        self.run.append("latency", {**self.stamp(), **row, "size": self.soak_size, "build": "soak"})
        for size in sizes:
            for build in ("B0", "B1"):
                got = bench_kept_store(self.run, build, size, each)
                if got:
                    self.run.append("latency", {**self.stamp(), **got})

    def replay_nightly(self) -> None:
        """Day 1 and day 7: every scale. Days 2 to 6: SMALL and MEDIUM-1 again, and the
        outcome hash of each, which must equal the hash of day 1."""
        day = self.day()
        sizes = [s for s in self.run.manifest["sizes"]]
        if 1 < day < 7:
            sizes = [s for s in sizes if s <= 200]
        replay_all(
            self.run,
            sizes,
            None,
            None,
            day=day,
            parallel=self.replay_parallel,
            python=store_python(Path(self.run.manifest["live"]["data_dir"])),
            guard_site=self.run.dir / "private" / "guard-site",
        )
        heldout = self.run.manifest.get("heldout")
        if heldout:  # arm b: the real prompts, on the real corpus and its fillers
            levels = [lv for lv in realreplay.LEVELS if lv == 0 or lv in sizes]
            levels = levels if day in (1, 7) else levels[:2]
            real_replay(
                self.run,
                Path(heldout),
                levels,
                day,
                guard_site=self.run.dir / "private" / "guard-site",
            )
        rows = replay_rows(self.run, day)
        for size in sizes:
            if size <= 200:
                self.run.append(
                    "determinism",
                    {
                        "ts": self.stamp()["ts"],
                        "day": day,
                        "scale": size,
                        "part": "all",
                        "hash": determinism_hash(rows, size),
                    },
                )

    def daily_summary(self) -> None:
        path = report.daily_summary(self.run.dir, self.now())
        res = report.status(self.run.dir, self.now(), notify=self.notify)
        self.run.append("status", {**self.stamp(), "ok": res["ok"], "failed": res["failed"]})
        _ = path

    # -- loop ----------------------------------------------------------------------

    def run_forever(self, max_seconds: float | None = None, tick_s: float = 5.0) -> None:
        end = time.monotonic() + max_seconds if max_seconds else None
        try:
            self.setup()
            while not self.sched.stopped() and (end is None or time.monotonic() < end):
                if self.over():
                    self.finish()
                    break
                self.sched.run_due(self.handlers(), background=True)
                self.sleep(tick_s)
        finally:
            self.teardown()

    def finish(self) -> None:
        """Day 7 is over: let a running night end, write the last daily summary and
        the checks, and leave a ``finished`` file. The unit then ends with status 0 and
        does not start again."""
        self.sched.wait()
        self.heartbeat()
        self.daily_summary()
        write_private(self.run.dir / "finished", self.now().isoformat(timespec="seconds") + "\n")


def sandbox_program() -> list[Path]:
    from .util import TOOLS

    return sorted((TOOLS / "longtest").glob("*.py"))


class LocalNet:
    """The localhost unit: the token-free ``/health`` probe of the live store, every
    15 minutes. It reads ``store.json`` for the port and nothing else of the live
    folder. It ends when the core unit has finished or a ``stop`` file exists. Arm e
    (the local model) is not started here: it needs decision D9 and its own switch."""

    JOBS = (Job("health_probe", 900),)

    def __init__(
        self,
        run: Run,
        now: Callable[[], dt.datetime] | None = None,
        free_gb: Callable[[Path], float] | None = None,
    ) -> None:
        self.run = run
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.sched = Scheduler(
            run.dir, self.now, free_gb=free_gb, jobs=self.JOBS, state_file="state-localnet.json"
        )
        self.data = Path(run.manifest["live"]["data_dir"])

    def probe(self) -> None:
        body = live.probe_health(self.data)
        self.run.append(
            "health",
            {
                "ts": self.now().isoformat(timespec="seconds"),
                "day": self.now().date().isoformat(),
                "up": body is not None,
                "ok": bool(body and body.get("ok", True)),
            },
        )

    def run_forever(self, max_seconds: float | None = None, tick_s: float = 30.0) -> None:
        end = time.monotonic() + max_seconds if max_seconds else None
        while not self.sched.stopped() and not (self.run.dir / "finished").exists():
            if end is not None and time.monotonic() >= end:
                break
            self.sched.run_due({"health_probe": self.probe})
            time.sleep(tick_s)
