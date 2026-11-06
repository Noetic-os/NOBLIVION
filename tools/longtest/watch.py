# SPDX-License-Identifier: AGPL-3.0-or-later
"""The watchdog of the run: ``nob-longtest-watch.timer`` runs this every 5 minutes.

It reads the run folder and asks the user manager about the two units. While no
``finished`` and no ``stop`` file exists it starts a unit that is inactive or
failed, and it restarts the core unit when ``out/heartbeat.jsonl`` is older than
15 minutes. One line per action goes to ``out/watchdog.jsonl``. It has no network
and writes nothing else.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .util import append_jsonl, private_dir

CORE = "nob-longtest.service"
UNITS = (CORE, "nob-longtest-localnet.service")
STALE_S = 900  # the core unit beats every 5 minutes; three missed beats mean it hangs
START_STATES = ("inactive", "failed")


def systemctl_path() -> str:
    return shutil.which("systemctl", path="/usr/bin:/bin") or "/usr/bin/systemctl"


def heartbeat_age_s(run_dir: Path, now: dt.datetime) -> float | None:
    """Seconds since the heartbeat file was last written; ``None`` without a file."""
    path = Path(run_dir) / "out" / "heartbeat.jsonl"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, now.timestamp() - mtime)


def idle(run_dir: Path) -> bool:
    run_dir = Path(run_dir)
    return (run_dir / "finished").exists() or (run_dir / "stop").exists()


def plan(
    states: Mapping[str, str],
    heartbeat_age: float | None,
    is_idle: bool,
    stale_s: float = STALE_S,
) -> list[tuple[str, str, str]]:
    """``(verb, unit, why)`` for each action. Nothing while the run is finished or
    stopped; nothing for a unit that is active or on its way (activating)."""
    if is_idle:
        return []
    actions = []
    for unit, state in states.items():
        if state in START_STATES:
            actions.append(("start", unit, state))
    if states.get(CORE) == "active" and heartbeat_age is not None and heartbeat_age > stale_s:
        actions.append(("restart", CORE, f"heartbeat {int(heartbeat_age)} s old"))
    return actions


def unit_state(systemctl: Sequence[str], unit: str) -> str:
    try:
        done = subprocess.run(
            [*systemctl, "is-active", unit], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip().split("\n")[0] or "unknown"


def run_once(
    run_dir: Path,
    systemctl: Sequence[str] | None = None,
    now: dt.datetime | None = None,
    stale_s: float = STALE_S,
    units: Sequence[str] = UNITS,
) -> list[dict[str, Any]]:
    run_dir = Path(run_dir)
    now = now or dt.datetime.now().astimezone()
    ctl = list(systemctl) if systemctl else [systemctl_path(), "--user"]
    states = {u: unit_state(ctl, u) for u in units}
    rows = []
    for verb, unit, why in plan(states, heartbeat_age_s(run_dir, now), idle(run_dir), stale_s):
        try:
            done = subprocess.run([*ctl, verb, unit], capture_output=True, text=True, timeout=120)
            rc, err = done.returncode, done.stderr.strip()[:200]
        except (OSError, subprocess.SubprocessError) as exc:  # the step logs, never fails
            rc, err = -1, f"{type(exc).__name__}: {exc}"[:200]
        row = {
            "ts": now.isoformat(timespec="seconds"),
            "action": verb,
            "unit": unit,
            "why": why,
            "rc": rc,
            "err": err,
        }
        private_dir(run_dir / "out")
        append_jsonl(run_dir / "out" / "watchdog.jsonl", row)
        rows.append(row)
    return rows
