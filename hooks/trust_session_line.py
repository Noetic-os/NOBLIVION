#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: at most two short lines about memory upkeep.
Part H of the adaptive memory trust work, and the dedup status.

It reads two local files only. No daemon call, no memory folder scan; the work
is two small JSON reads (well under 5 ms):

- the trust report cache (contract C5; ``NOBLIVION_TRUST_REPORT_FILE``, default
  ``<data dir>/cache/trust-report.json``) ->
  ``Memory trust: N to retire, M to promote, D to demote. Run noblivion trust report``
- the dedup status file (contract C4; ``NOBLIVION_DEDUP_STATUS_FILE``, default
  ``<data dir>/cache/dedup-last-run.json``, written by every ``noblivion dedup``
  plan, apply and undo) -> for example
  ``Memory dedup: N merged on <date>, M proposals``

A missing or unreadable file gives no line. A line whose counts are all zero is
not printed either: it would ask for an action that has nothing to act on, and
SessionStart text is re-sent on every later call of the session. A dry run
and a plan say so and name the next command; each line names the date of the
run. A refused run (C4 "outcome": "refused") gives ``Memory dedup REFUSED on
<date>, nothing merged: <reason>``, except for a ``refused_code`` in
``QUIET_REFUSAL_CODES``. Source ``compact`` prints nothing (the session
already had the lines).
Exit 0 on every path.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, List, Optional

TRUST_FILE_ENV = "NOBLIVION_TRUST_REPORT_FILE"
TRUST_FILE_NAME = "trust-report.json"  # in <data dir>/cache
DEDUP_FILE_ENV = "NOBLIVION_DEDUP_STATUS_FILE"
DEDUP_FILE_NAME = "dedup-last-run.json"  # in <data dir>/cache
REASON_MAX_CHARS = 120
QUIET_REFUSAL_CODES = frozenset({"no_gate"})
MAX_FILE_BYTES = 4_000_000


def _cache_dir(environ: Mapping[str, str]) -> Path:
    """``<data dir>/cache`` from ``hook_config.py`` in this file's folder."""
    path = Path(__file__).resolve().parent / "hook_config.py"
    spec = importlib.util.spec_from_file_location("hook_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.cache_dir(environ)


def _read(path: str) -> Any:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def trust_line(doc: Any) -> str:
    if not isinstance(doc, dict):
        return ""
    raw = doc.get("counts")
    counts: dict = raw if isinstance(raw, dict) else {}
    n: List[int] = []
    for name in ("retire", "promote", "demote"):
        c = _count(counts.get(name))
        if c is None:
            rows = doc.get(name)
            c = len(rows) if isinstance(rows, list) else None
        if c is None:
            return ""
        n.append(c)
    if not any(n):
        return ""
    return (
        f"Memory trust: {n[0]} to retire, {n[1]} to promote, {n[2]} to demote. "
        "Run noblivion trust report"
    )


def _safe(value: Any) -> str:
    return "".join(ch for ch in str(value)[:64] if ch.isalnum() or ch in "-_ ()")


def dedup_line(doc: Any, now: Optional[_dt.datetime] = None) -> str:
    if not isinstance(doc, dict):
        return ""
    failed = doc.get("failed")  # C4 additive key: an apply that failed and is not undone
    if isinstance(failed, str) and failed:
        safe = _safe(failed)
        return (
            f"Memory dedup FAILED in run {safe}: run noblivion dedup undo {safe}. "
            "Apply is blocked until then"
        )
    when = _when(doc)
    if doc.get("outcome") == "refused":
        if doc.get("refused_code") in QUIET_REFUSAL_CODES:
            return ""
        raw_reason = doc.get("refused_reason")
        reason = " ".join(
            "".join(ch if ch.isprintable() else " " for ch in str(raw_reason or "")).split()
        )
        if len(reason) > REASON_MAX_CHARS:
            reason = reason[: REASON_MAX_CHARS - 3] + "..."
        return f"Memory dedup REFUSED {when}, nothing merged: {reason or 'no reason recorded'}"
    if doc.get("mode") == "dry-run":
        pairs = _count(doc.get("pairs"))
        if not pairs:
            return ""
        return (
            f"Memory dedup dry run {when}: {pairs} candidate pairs, nothing sent. "
            "Run noblivion dedup plan to judge them"
        )
    merged, proposals = _count(doc.get("merged")), _count(doc.get("proposals"))
    if doc.get("mode") == "plan":
        run = _safe(doc.get("run_id") or "")
        if not proposals or not run:
            return ""
        return (
            f"Memory dedup plan {run} {when}: {proposals} merge proposals. "
            f"Read the plan, then run noblivion dedup apply {run}"
        )
    if merged is None or proposals is None or not (merged or proposals):
        return ""
    return f"Memory dedup: {merged} merged {when}, {proposals} proposals"


def _when(doc: Mapping[str, Any]) -> str:
    """``on <date>`` of the run, or ``in the last run`` when the file has no
    readable ``finished_at``."""
    raw = doc.get("finished_at")
    if isinstance(raw, str):
        try:
            t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return "in the last run"
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return "on " + t.astimezone(_dt.timezone.utc).date().isoformat()
    return "in the last run"


def lines(environ: Mapping[str, str], now: Optional[_dt.datetime] = None) -> List[str]:
    trust = os.path.expanduser(environ.get(TRUST_FILE_ENV) or "") or str(
        _cache_dir(environ) / TRUST_FILE_NAME
    )
    dedup = os.path.expanduser(environ.get(DEDUP_FILE_ENV) or "") or str(
        _cache_dir(environ) / DEDUP_FILE_NAME
    )
    return [x for x in (trust_line(_read(trust)), dedup_line(_read(dedup), now)) if x]


def main(stdin=None, stdout=None, environ: Optional[Mapping[str, str]] = None) -> int:
    try:
        env = os.environ if environ is None else environ
        raw = (stdin or sys.stdin).read()
        try:
            event = json.loads(raw) if raw.strip() else {}
        except ValueError:
            event = {}
        if isinstance(event, dict) and event.get("source") == "compact":
            return 0
        out = lines(env)
        if out:
            w = stdout or sys.stdout
            w.write("\n".join(out) + "\n")
            w.flush()
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
