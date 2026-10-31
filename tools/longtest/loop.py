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

from . import live, report, sandbox, soak
from .buildrunner import Harness, Worker, store_python
from .config import PARAMS
from .runner import Run, determinism_hash, replay_all, replay_rows
from .scheduler import Scheduler


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
        self.start = dt.datetime.fromisoformat(manifest["created"])
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

    def day(self) -> int:
        return (self.now().date() - self.start.astimezone(self.now().tzinfo).date()).days + 1

    def setup(self) -> None:
        run, fx = self.run, self.run.fixture()
        check = sandbox.start_check(os.environ, sandbox_program())
        if not check["ok"]:
            raise SystemExit(f"start check failed: {check}")
        self.guard.install()
        self.sockets.install()
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
                "external_connects": self.sockets.refused,
                "claude_processes": sandbox.claude_processes(os.getpid()),
                "refused_opens": self.guard.refused_reads,
                "refused_writes": self.guard.refused_writes,
            },
        )

    def latency_bench(self) -> None:
        assert self.worker
        cwd = "/work/" + self.folders[0].parent.name.replace("-work-", "", 1)
        counter = {"n": 0}

        def call(prompt: str) -> float:
            counter["n"] += 1
            return self.worker.call("prompt", sid=f"lb-{counter['n']}", cwd=cwd, prompt=prompt)[
                "ms"
            ]

        row = soak.latency_benchmark(call, self.texts, n=100, budget_s=1200)
        self.run.append("latency", {**self.stamp(), **row, "size": self.soak_size, "build": "B1"})

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
                self.sched.run_due(self.handlers())
                self.sleep(tick_s)
        finally:
            self.teardown()


def sandbox_program() -> list[Path]:
    from .util import TOOLS

    return sorted((TOOLS / "longtest").glob("*.py"))
