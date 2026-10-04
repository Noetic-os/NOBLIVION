# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion stop report``: count the stop check decisions of the last days.

The stop hook (``hooks/stop_checks.py``) writes one row per stop to
``<data dir>/stop-check-log.jsonl`` (``NOBLIVION_STOP_CHECK_LOG`` overrides
it; the rotated ``.1`` file is read too). In shadow mode, the default, a check
that fired and is on lists in ``would_block``; in enforce mode it lists in
``blocking``. A check that fired and is not on lists in ``shadow`` only. The
report counts each, per check, so a user can see what enforce mode would do
before turning it on. It reads the log only and changes nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from noblivion import config

LOG_NAME = "stop-check-log.jsonl"
CHECK_ORDER = ("commit", "tests", "notify", "deploy", "lesson")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def log_path(env: Mapping[str, str] | None = None) -> Path:
    """The decision log the stop hook writes."""
    env = os.environ if env is None else env
    raw = str(env.get("NOBLIVION_STOP_CHECK_LOG") or "").strip()
    return Path(raw).expanduser() if raw else config.data_dir(env) / LOG_NAME


def read_rows(path: Path) -> Iterator[dict]:
    """The rows of the log and its rotated ``.1`` file, oldest file first. A
    missing file or a broken line is skipped."""
    for p in (Path(str(path) + ".1"), path):
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row


def _when(row: Mapping[str, object]) -> datetime | None:
    try:
        return datetime.strptime(str(row.get("ts")), TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _names(row: Mapping[str, object], key: str) -> list[str]:
    value = row.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def build_report(
    rows: Iterable[Mapping[str, object]], days: int, now: datetime | None = None
) -> dict:
    """Counts per check over the stop rows of the last ``days`` days."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    checks: dict[str, dict[str, int]] = {}
    stops = 0
    modes: dict[str, int] = {}
    notify_skipped = 0

    def bump(name: str, key: str) -> None:
        row = checks.setdefault(name, {"would_block": 0, "blocked": 0, "logged_only": 0})
        row[key] += 1

    for row in rows:
        if row.get("event") != "stop":
            continue
        when = _when(row)
        if when is None or when < since:
            continue
        stops += 1
        mode = str(row.get("mode") or "enforce")  # rows before NOBLIVION-32 had no mode
        modes[mode] = modes.get(mode, 0) + 1
        would = _names(row, "would_block")
        for name in would:
            bump(name, "would_block")
        for name in _names(row, "blocking"):
            bump(name, "blocked")
        for name in _names(row, "shadow"):
            if name not in would:
                bump(name, "logged_only")
        notes = row.get("notes")
        if isinstance(notes, dict) and notes.get("notify_skipped"):
            notify_skipped += 1
    order = {n: i for i, n in enumerate(CHECK_ORDER)}
    return {
        "days": days,
        "since": since.strftime(TS_FORMAT),
        "stops": stops,
        "modes": modes,
        "checks": dict(sorted(checks.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))),
        "notify_skipped": notify_skipped,
    }


def render_report(answer: Mapping[str, Any], path: Path) -> str:
    """The report as text: one line per check."""
    modes: dict[str, int] = answer.get("modes") or {}
    mode_text = ", ".join(f"{k} {v}" for k, v in sorted(modes.items())) if modes else "none"
    lines = [
        f"Stop check report: the last {answer['days']} days, {answer['stops']} stops "
        f"(mode: {mode_text}).",
        f"Log: {path}",
        "",
    ]
    checks: dict[str, dict[str, int]] = answer.get("checks") or {}
    if not checks:
        lines.append("No check fired.")
    for name, c in checks.items():
        lines.append(
            f"- {name}: would block {c['would_block']}, blocked {c['blocked']}, "
            f"logged only (check not on) {c['logged_only']}"
        )
    if answer.get("notify_skipped"):
        lines.append(
            f"- notify skipped {answer['notify_skipped']} times: the session had no "
            "PushNotification tool"
        )
    lines.append("")
    lines.append(
        "Shadow mode is the default: no check blocks. Set NOBLIVION_STOP_CHECK_MODE=enforce "
        "(or the config key stop.mode) to let the checks block."
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """``noblivion stop report [--days N] [--json]``."""
    import argparse

    parser = argparse.ArgumentParser(prog="noblivion stop")
    parser.add_argument("action", choices=("report",))
    parser.add_argument("--days", type=int, default=7, help="the last N days (default 7)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    path = log_path()
    answer = build_report(read_rows(path), max(1, args.days))
    if args.json:
        print(json.dumps({**answer, "log": str(path)}, indent=1))
    else:
        print(render_report(answer, path))
    return 0
