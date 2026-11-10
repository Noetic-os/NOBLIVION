# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job loop of the long run: handlers for the scheduler.

Core unit: the soak store of B1 (arm d), the live pass (arm a, files only), the
sandbox watch, the nightly replay (arms b and c) and the daily check. The handlers
only append counts to ``out/*.jsonl``; they never write outside the run folder.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import growth, live, profiles, realreplay, report, sandbox, soak, warm
from .buildrunner import Harness, Worker, index_wait_s, store_python
from .config import WINDOW
from .runner import (
    Run,
    bench_kept_store,
    determinism_hash,
    real_replay,
    replay_all,
    replay_rows,
    seed_soak_store,
)
from .scheduler import Job, Scheduler, in_window, job_note, jobs_for
from .util import write_private


class Pulse:
    """Heartbeat lines from a thread while the stores start: a restart that scans a
    20,000-note store takes minutes, and the watchdog must not count that as a hang."""

    def __init__(self, run: Run, stamp: Callable[[], dict[str, Any]], every_s: float) -> None:
        self.run, self.stamp, self.every_s = run, stamp, every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="pulse", daemon=True)

    def _beat(self) -> None:
        while not self._stop.is_set():
            row = self.stamp()
            self.run.append("heartbeat", {**row, "phase": row.get("phase", "setup")})
            self._stop.wait(self.every_s)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)


class Loop:
    def __init__(
        self,
        run: Run,
        soak_size: int,
        now: Callable[[], dt.datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        params=None,
        notify: Callable[[str], None] | None = None,
        replay_parallel: int = 1,  # one store starts at a time
        profile: profiles.Profile | None = None,
        jobs: tuple[Job, ...] | None = None,
    ) -> None:
        manifest = run.manifest
        self.profile = profile or profiles.profile_of(manifest)
        self.params = params or self.profile.params
        self.run, self.soak_size, self.sleep = run, soak_size, sleep
        self.notify = notify
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.sched = Scheduler(
            run.dir, self.now, params=self.params, jobs=jobs or jobs_for(self.params)
        )
        self.replay_parallel = replay_parallel
        self.final_health: dict[str, Any] | None = None
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

    def first_start(self) -> dt.datetime:
        """The start of day 1, written once. A start inside the heavy window would put
        two nights into one day (the rest of this window, and the next night, which
        the done markers would then skip): the start then moves to the end of the
        window, so every day of the run holds exactly one night."""
        now = self.now()
        if in_window(now):
            end = dt.time.fromisoformat(WINDOW.split("-")[1])
            return dt.datetime.combine(now.date(), end, now.tzinfo)
        return now

    def day(self) -> int:
        """The run day, in whole 24-hour periods from the start. The heavy window opens
        once per 24 hours, so every period holds exactly one night. Never below 1: a
        start moved to the end of the window lies ahead for up to an hour."""
        began = self.started()
        if began is None:
            return 1
        return max(1, int((self.now() - began).total_seconds() // 86400) + 1)

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
        seed_soak_store(run, root, self.soak_size)
        self.harness = Harness(
            "B1",
            run.build_path("B1"),
            root,
            store_python(live_data),
            live_data / "models",
            wait_s=index_wait_s(self.soak_size),
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
            self.sched.state["started"] = self.first_start().isoformat(timespec="seconds")
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
            try:
                self.final_health = self.harness.health()
            except Exception:  # noqa: BLE001 - the store may be gone already
                self.final_health = None
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
        assert self.worker and self.harness
        cwd = "/work/" + self.folders[0].parent.name.replace("-work-", "", 1)
        # The soak store is warm, but a slow call of the last 30 seconds may have set
        # the hook latch: the stamp is removed, and the row says how many calls ran
        # under it all the same (``valid`` is false then).
        warm.clear_latch(self.harness.data)
        call = warm.HookCalls(self.worker, cwd, "lb")
        sizes = [s for s in self.run.manifest["sizes"] if s >= 1000]
        builds = self.profile.builds
        budget = self.params.bench_budget_s
        each = budget / (1 + len(builds) * len(sizes))
        row = soak.latency_benchmark(call, self.texts, n=100, budget_s=each)
        self.run.append(
            "latency",
            {**self.stamp(), **row, **call.validity(), "size": self.soak_size, "build": "soak"},
        )
        for size in sizes:
            for build in builds:
                got = bench_kept_store(self.run, build, size, each)
                if got:
                    self.run.append("latency", {**self.stamp(), **got})

    def replay_nightly(self) -> None:
        """The scales of the night (``Profile.nightly_sizes``): the first run replays its
        small scales between the first and the last day and keeps the outcome hash of
        each, which must equal the hash of day 1; the large-project run grows each kept
        store by the day's notes and replays every scale against it. A failed arm does
        not skip the next arm or the hashes; its error is raised after them, so the job
        fails and the scheduler retries it after 30 minutes."""
        day = self.day()
        prof = self.profile
        sizes = prof.nightly_sizes(day, list(self.run.manifest["sizes"]))
        errors: list[Exception] = []
        if prof.growth_per_day:
            for size in sizes:
                try:
                    growth.grow(self.run, size, day, prof.growth_per_day, now=self.now())
                except Exception as exc:  # noqa: BLE001 - raised again below
                    errors.append(exc)
        try:
            replay_all(
                self.run,
                sizes,
                None,
                None,
                variants=prof.variants,
                builds=prof.builds,
                day=day,
                parallel=self.replay_parallel,
                python=store_python(Path(self.run.manifest["live"]["data_dir"])),
                guard_site=self.run.dir / "private" / "guard-site",
                stock_arms=prof.stock_arms,
                stock_dir=(
                    (lambda size, shard: growth.stock_dir(self.run, size, shard))
                    if prof.growth_per_day
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - raised again below
            errors.append(exc)
        heldout = self.run.manifest.get("heldout")
        if heldout:  # arm b: the real prompts, on the real corpus and its fillers
            levels = [lv for lv in realreplay.LEVELS if lv == 0 or lv in sizes]
            levels = levels if day in (1, self.params.run_days) else levels[:2]
            try:
                real_replay(
                    self.run,
                    Path(heldout),
                    levels,
                    day,
                    guard_site=self.run.dir / "private" / "guard-site",
                )
            except Exception as exc:  # noqa: BLE001 - raised again below
                errors.append(exc)
        rows = replay_rows(self.run, day)
        for size in sizes:
            if prof.determinism and size <= 200:
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
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise RuntimeError("; ".join(f"{type(e).__name__}: {e}" for e in errors))

    def daily_summary(self) -> None:
        """The daily check writes ``daily/<day>.json`` and, on a failure, ``ALERT``. It
        never writes ``stop`` or ``pause``: only the end time in ``state.json`` ends
        the run."""
        path = report.daily_summary(self.run.dir, self.now())
        res = report.status(self.run.dir, self.now(), notify=self.notify, params=self.params)
        self.run.append("status", {**self.stamp(), "ok": res["ok"], "failed": res["failed"]})
        _ = path

    # -- loop ----------------------------------------------------------------------

    def run_forever(self, max_seconds: float | None = None, tick_s: float = 5.0) -> None:
        """The loop. Nothing raises out of a tick: a failed job or a failed tick is logged
        to ``out/failures.jsonl`` and ``ALERT``, and the loop goes on until the end time
        from ``state.json`` (``over``), a ``stop`` file, or ``max_seconds``. A failed
        setup is logged and raised: the unit restarts after its ``RestartSec``."""
        end = time.monotonic() + max_seconds if max_seconds else None
        pulse = Pulse(self.run, self.stamp, self.params.heartbeat_s)
        try:
            pulse.start()
            try:
                self.setup()
            except Exception as exc:
                self.sched.record_failure("setup", job_note(exc))
                raise
            finally:
                pulse.stop()
            while not self.sched.stopped() and (end is None or time.monotonic() < end):
                try:
                    if self.over():
                        self.finish()
                        break
                    self.sched.run_due(self.handlers(), background=True)
                except Exception as exc:  # noqa: BLE001 - the loop must not end
                    self.sched.record_failure("tick", job_note(exc))
                self.sleep(tick_s)
        finally:
            self.teardown()

    def finish(self) -> None:
        """The last day is over: let a running night end, write the last daily summary and
        the checks, and leave a ``finished`` file. The unit then ends with status 0 and
        does not start again. The file is written even when the last summary fails, so
        the end is never missed. The heartbeat goes on while the last night ends, so the
        watchdog does not take the wait for a hang."""
        pulse = Pulse(
            self.run, lambda: {**self.stamp(), "phase": "finish"}, self.params.heartbeat_s
        )
        try:
            pulse.start()
            self.sched.wait()
            pulse.stop()
            self.heartbeat()
            self.daily_summary()
        except Exception as exc:  # noqa: BLE001 - the end still comes
            self.sched.record_failure("finish", job_note(exc))
        finally:
            pulse.stop()
        write_private(self.run.dir / "finished", self.now().isoformat(timespec="seconds") + "\n")


def command_notifier(command: str, timeout_s: float = 30.0) -> Callable[[str], None]:
    """``notify`` of the loop: runs ``command`` with the alert text as its last argument,
    without a shell. A failure is written to stderr (the journal of the unit) and the run
    goes on. The core unit has no network and a PATH of three tools: give an absolute
    path to a program that hands the text on without the network."""
    argv = shlex.split(command)
    if not argv or shutil.which(argv[0]) is None:
        raise SystemExit(f"notify command not found: {command!r}")

    def notify(text: str) -> None:
        try:
            subprocess.run([*argv, text], stdin=subprocess.DEVNULL, timeout=timeout_s, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"notify command failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    return notify


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
