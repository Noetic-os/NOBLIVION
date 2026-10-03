#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: at most two short lines about memory upkeep.
Part H of the adaptive memory trust work, and the dedup status.

It reads two local files only. No daemon call, no memory folder scan; the work
is two small JSON reads (well under 5 ms):

- the trust report cache (contract C5; ``NOBLIVION_TRUST_REPORT_FILE``, default
  ``<data dir>/cache/trust-report.json``) ->
  ``Memory trust: N to retire, M to promote, D to demote. Run noblivion trust``
- the dedup status file (contract C4; ``NOBLIVION_DEDUP_STATUS_FILE``, default
  ``<data dir>/cache/dedup-last-run.json``) ->
  ``Memory dedup: N merged last night, M proposals``

A missing or unreadable file gives no line. A line whose counts are all zero is
not printed either: it would ask for an action that has nothing to act on, and
SessionStart text is re-sent on every later call of the session. A dedup run in
dry-run mode says so; a run older than 36 hours names its date instead of "last
night". A run the gate refused (C4 "outcome": "refused")
gives ``Memory dedup REFUSED last night, nothing merged: <reason>``, except
when the reason is "no gate configured" (``refused_code`` "no_gate"): the gate
is empty by design until a prompt passes it, and a daily line for that state
would be noise. Any other refusal means the nightly is broken. Source
``compact`` prints nothing (the session already had the lines).
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
LAST_NIGHT_H = 36.0
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
        f"Memory trust: {n[0]} to retire, {n[1]} to promote, {n[2]} to demote. Run noblivion trust"
    )


def dedup_line(doc: Any, now: Optional[_dt.datetime] = None) -> str:
    if not isinstance(doc, dict):
        return ""
    failed = doc.get("failed")  # C4 additive key: an apply that failed and is not undone
    if isinstance(failed, str) and failed:
        safe = "".join(ch for ch in failed[:64] if ch.isalnum() or ch in "-_ ()")
        return (
            f"Memory dedup FAILED in run {safe}: run noblivion dedup undo {safe}. "
            "Nightly applies are blocked until then"
        )
    if doc.get("outcome") == "refused":
        if doc.get("refused_code") in QUIET_REFUSAL_CODES:
            return ""
        raw_reason = doc.get("refused_reason")
        reason = " ".join(
            "".join(ch if ch.isprintable() else " " for ch in str(raw_reason or "")).split()
        )
        if len(reason) > REASON_MAX_CHARS:
            reason = reason[: REASON_MAX_CHARS - 3] + "..."
        return f"Memory dedup REFUSED {_when(doc, now)}, nothing merged: {reason or 'no reason recorded'}"
    merged, proposals = _count(doc.get("merged")), _count(doc.get("proposals"))
    if merged is None or proposals is None or not (merged or proposals):
        return ""
    when = _when(doc, now)
    if doc.get("mode") == "dry-run":
        return f"Memory dedup dry run {when}: {merged} to merge, {proposals} proposals"
    return f"Memory dedup: {merged} merged {when}, {proposals} proposals"


def _when(doc: Mapping[str, Any], now: Optional[_dt.datetime]) -> str:
    when = "last night"
    raw = doc.get("finished_at")
    if isinstance(raw, str):
        try:
            t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            ref = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
            if (ref - t).total_seconds() > LAST_NIGHT_H * 3600.0:
                when = "on " + t.astimezone(_dt.timezone.utc).date().isoformat()
        except ValueError:
            when = "in the last run"
    return when


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
