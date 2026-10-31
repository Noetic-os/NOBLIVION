# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quarantine of a deny rule that the model overrides most of the time (NOBLIVION-155).

A deny costs one turn and its text. When the model passes the override marker
(``# guard-ok: <reason>``) on most denies of one rule, the rule is wrong for the
work it meets: it names another repository, or its own check fails. Then it
should stop denying and only show as a row.

The rule, read from the guard log (``guard-log.jsonl`` and ``.1``) over the last
``WINDOW_DAYS`` days, per rule id:

  * ``denies``: ``deny`` lines that name the rule;
  * ``overrides``: ``override`` lines that name the rule;
  * the rule is quarantined when ``overrides >= MIN_OVERRIDES`` and
    ``overrides / max(denies, overrides) >= MIN_RATE``.

A quarantined rule shows as a row where it would have denied (the guard hook),
and the session-start line names it with its counts (``line``). It leaves the
quarantine by itself: the counts cover the last ``WINDOW_DAYS`` days only, so a
rule that is fixed, or that is not overridden any more, denies again.

The credential guard is never quarantined: it logs ``credential-deny`` and
``credential-override``, never ``deny`` and ``override``, and ``exempt`` also
refuses every id that starts with ``credential``. Only the rules of the memory
table go through this module.

No network, no model. Standard library only. Off with
``NOBLIVION_GUARD_QUARANTINE=0`` (config key ``guard.quarantine``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Optional, Tuple

WINDOW_DAYS = 14
MIN_OVERRIDES = 3
MIN_RATE = 0.5
MAX_LOG_BYTES = 16_000_000  # of each log file; the log rotates at 5 MB
NAMED = 3  # rules the session-start line names
QUARANTINE_ENV, QUARANTINE_KEY = "NOBLIVION_GUARD_QUARANTINE", "guard.quarantine"
_DENY, _OVERRIDE = '"decision": "deny"', '"decision": "override"'

_CFG = None


def _cfg():
    """``hook_config.py`` from this file's folder, loaded once."""
    global _CFG
    if _CFG is None:
        path = Path(__file__).resolve().parent / "hook_config.py"
        spec = importlib.util.spec_from_file_location("hook_config_quarantine", path)
        if spec is None or spec.loader is None:
            raise ImportError("hook_config")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CFG = mod
    return _CFG


def enabled(env: Mapping[str, str]) -> bool:
    """The switch, on by default. A bad config never turns it off."""
    try:
        return bool(_cfg().switch(QUARANTINE_ENV, True, env, QUARANTINE_KEY))
    except Exception:  # noqa: BLE001
        return True


def log_path(env: Mapping[str, str]) -> Path:
    """The guard log: ``NOBLIVION_GUARD_LOG``, else ``<data dir>/guard-log.jsonl``."""
    raw = env.get("NOBLIVION_GUARD_LOG")
    if raw:
        return Path(os.path.expanduser(raw))
    return _cfg().data_dir(env) / "guard-log.jsonl"


def exempt(rule_id: str) -> bool:
    """True for an id that is never quarantined: the credential guard."""
    return str(rule_id).lower().startswith("credential")


def _lines(path: Path) -> List[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > MAX_LOG_BYTES:
                fh.seek(size - MAX_LOG_BYTES)
                fh.readline()  # a cut line
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def tally(
    path: Path, now: Optional[float] = None, days: int = WINDOW_DAYS
) -> Dict[str, Tuple[int, int]]:
    """``{rule id: (denies, overrides)}`` over the last ``days`` days of the log
    at ``path`` and ``path.1``. A line that does not parse is skipped."""
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime((time.time() if now is None else now) - days * 86400)
    )
    out: Dict[str, List[int]] = {}
    for p in (Path(str(path) + ".1"), path):
        for line in _lines(p):
            if _DENY not in line and _OVERRIDE not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or str(rec.get("ts") or "") < cutoff:
                continue
            kind = rec.get("decision")
            if kind not in ("deny", "override") or not isinstance(rec.get("ids"), list):
                continue
            for rid in rec["ids"]:
                row = out.setdefault(str(rid), [0, 0])
                row[0 if kind == "deny" else 1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def is_quarantined(denies: int, overrides: int) -> bool:
    """The rule: ``MIN_OVERRIDES`` or more overrides, and an override rate of
    ``MIN_RATE`` or more (overrides over the larger of denies and overrides)."""
    total = max(denies, overrides)
    return overrides >= MIN_OVERRIDES and total > 0 and overrides / total >= MIN_RATE


def quarantined(
    path: Path, now: Optional[float] = None, days: int = WINDOW_DAYS
) -> Dict[str, Tuple[int, int]]:
    """``{rule id: (overrides, total)}`` of the quarantined rules. ``total`` is
    the larger of denies and overrides: "overridden 32 of 53"."""
    out: Dict[str, Tuple[int, int]] = {}
    for rid, (denies, overrides) in tally(path, now, days).items():
        if not exempt(rid) and is_quarantined(denies, overrides):
            out[rid] = (overrides, max(denies, overrides))
    return out


def line(env: Mapping[str, str], now: Optional[float] = None) -> str:
    """The session-start line that names the quarantined rules, or ``""``."""
    if not enabled(env):
        return ""
    got = quarantined(log_path(env), now)
    if not got:
        return ""
    ranked = sorted(got.items(), key=lambda kv: (-kv[1][0], kv[0]))
    named = [f"{_safe(rid)} (overridden {o} of {t})" for rid, (o, t) in ranked[:NAMED]]
    more = len(ranked) - NAMED
    if more > 0:
        named.append(f"and {more} more")
    return (
        f"Guard: {len(ranked)} deny rule(s) shown as a row only, overridden too often in "
        f"{WINDOW_DAYS} days: " + "; ".join(named) + ". Fix or remove them"
    )


def _safe(rid: str) -> str:
    return "".join(ch for ch in str(rid)[:80] if ch.isalnum() or ch in "-_.")
