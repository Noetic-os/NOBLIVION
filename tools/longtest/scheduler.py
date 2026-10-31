# SPDX-License-Identifier: AGPL-3.0-or-later
"""The internal schedule of the run, with a state file that survives a restart.

A job has an interval. A heavy job (index builds, the nightly replay, the day-7
re-run) runs only in the window 01:00 to 06:00 and waits while the load average is
over the limit. Every job waits while free disk is under the floor or while a
``stop`` file exists. The state file holds the last run of each job and the offsets
of the live files, so a restart resumes and does not read a file twice.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PARAMS, Params
from .util import json_text, write_private


@dataclass(frozen=True)
class Job:
    name: str
    every_s: int
    heavy: bool = False
    daily_at: str = ""  # "06:30": once a day after this local time, instead of an interval


JOBS = (
    Job("heartbeat", 3600),
    Job("live_pass", 900),
    Job("soak_load", 600),
    Job("soak_churn", 900),
    Job("soak_drift", 3600),
    Job("soak_resources", 3600),
    Job("sandbox_watch", 60),
    Job("replay_nightly", 86400, heavy=True),
    Job("latency_bench", 86400, heavy=True),
    Job("daily_summary", 86400, daily_at="06:30"),
)


def in_window(now: dt.datetime, window: str = "01:00-06:00") -> bool:
    start, end = (dt.time.fromisoformat(p) for p in window.split("-"))
    return start <= now.time() < end


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"jobs": {}, "offsets": {}, "pause": ""}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    write_private(Path(path), json_text(state))


class Scheduler:
    def __init__(
        self,
        run_dir: Path,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
        loadavg: Callable[[], float] = lambda: os.getloadavg()[0],
        free_gb: Callable[[Path], float] | None = None,
        params: Params = PARAMS,
        jobs: tuple[Job, ...] = JOBS,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.now, self.loadavg = now, loadavg
        self.free_gb = free_gb or (lambda p: shutil.disk_usage(p).free / 1e9)
        self.params, self.jobs = params, jobs
        self.state_path = self.run_dir / "state.json"
        self.state = load_state(self.state_path)

    def stopped(self) -> bool:
        return (self.run_dir / "stop").exists()

    def pause_reason(self, job: Job) -> str:
        """Why nothing runs now, or why this job waits. Empty when it can run."""
        if self.stopped():
            return "stop"
        if self.free_gb(self.run_dir) < self.params.min_free_gb:
            return "disk"
        if job.heavy:
            if not in_window(self.now()):
                return "window"
            if self.loadavg() > self.params.load_pause:
                return "load"
        return ""

    def _is_due(self, job: Job, now: dt.datetime) -> bool:
        last = self.state["jobs"].get(job.name, {}).get("last")
        last_t = dt.datetime.fromisoformat(last) if last else None
        if job.daily_at:
            at = dt.datetime.combine(now.date(), dt.time.fromisoformat(job.daily_at), now.tzinfo)
            return now >= at and (last_t is None or last_t < at)
        return last_t is None or (now - last_t).total_seconds() >= job.every_s

    def due(self) -> list[Job]:
        now = self.now()
        return [j for j in self.jobs if self._is_due(j, now) and not self.pause_reason(j)]

    def waiting(self) -> dict[str, str]:
        now = self.now()
        return {
            j.name: r for j in self.jobs if self._is_due(j, now) and (r := self.pause_reason(j))
        }

    def mark(self, job: Job, ok: bool, note: str = "") -> None:
        entry = self.state["jobs"].setdefault(job.name, {"runs": 0, "failed": 0})
        entry.update(last=self.now().isoformat(timespec="seconds"), ok=ok, note=note)
        entry["runs"] += 1
        entry["failed"] += 0 if ok else 1
        save_state(self.state_path, self.state)

    def run_due(self, handlers: Mapping[str, Callable[[], Any]]) -> list[tuple[str, bool]]:
        """Run every due job once. A handler that raises marks the job failed; the
        run goes on, and the daily check reports it."""
        done = []
        for job in self.due():
            try:
                handlers[job.name]()
                ok, note = True, ""
            except Exception as exc:  # noqa: BLE001 - one job must not stop the run
                ok, note = False, type(exc).__name__
            self.mark(job, ok, note)
            done.append((job.name, ok))
        return done
