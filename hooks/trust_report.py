#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The memory trust report: which memories to retire, promote or demote.
Part H of the adaptive memory trust work (design doc 0001, section 4.6).

It reads ``GET /api/memory/trust/report?persona=claude_code`` from the local
store (the store computes the three lists on read) and writes the answer to
the local cache file ``<data dir>/cache/trust-report.json``
(``NOBLIVION_TRUST_REPORT_FILE`` names another file). The SessionStart line
(``trust_session_line.py``) reads only that file. The Stop flush
(``trust_flush.py``) refreshes it once a day; this tool refreshes it by hand.
Nothing here changes a memory: retiring a file (moving it to the memory
folder's ``.archive/``) and editing ``MEMORY.md`` stay agent actions.

The lists:

- retire: shown in >= 20 sessions and never used, not an index file, not in
  the MEMORY.md of its folder, first indexed >= 30 days ago;
- promote: trust >= 0.7, >= 5 trials, used in >= 20% of sessions over >= 14
  days, not in MEMORY.md (a candidate for the always-on index);
- demote: a MEMORY.md line whose file was not used in 30 days.

"Used" means a fetch by ``noblivion_recall`` or a guard row or deny on the
rule. No event measures compliance or harm averted.

The store is found, proved and (when down) started through the recall hook's
``store_get`` (``store_client``: ``store.json``, the HMAC listener proof, the
token). Each row names its ``root``, the memory folder key, because a file
name alone is not unique across folders.

Usage::

    python3 hooks/trust_report.py              # refresh, then print
    python3 hooks/trust_report.py --cache-only # print the cache, no store call
    python3 hooks/trust_report.py --json       # the cache document as JSON

Standard library only. Hooks load their siblings by path.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.realpath(__file__))
PERSONA = "claude_code"
ROUTE = "/api/memory/trust/report"
CACHE_FILE_ENV = "NOBLIVION_TRUST_REPORT_FILE"
CACHE_FILE_NAME = "trust-report.json"  # in <data dir>/cache
LISTS = ("retire", "promote", "demote")
TIMEOUT_S = 10.0  # design doc section 3.5
FORMAT = "noblivion-trust-report-cache/1"
ROW_INT = ("trials", "shown_sessions", "use_sessions")

_MODS: Dict[str, Any] = {}


def _load(name: str) -> Any:
    mod = _MODS.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, f"{name}.py"))
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return mod


def rh() -> Any:
    """The recall hook module (``store_get``, ``RecallError``), loaded once."""
    return _load("recall_hook")


def cache_file(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    explicit = os.path.expanduser(env.get(CACHE_FILE_ENV) or "")
    if explicit:
        return explicit
    return str(Path(_load("hook_config").cache_dir(env)) / CACHE_FILE_NAME)


def report_url(base: str) -> str:
    return base + ROUTE + "?" + urllib.parse.urlencode({"persona": PERSONA})


def _row(raw: Any) -> Optional[Dict[str, Any]]:
    """One list row in the section 4.6 shape, or None for a row that is not one."""
    if not isinstance(raw, dict):
        return None
    mid = raw.get("mv_id")
    path = raw.get("path")
    if isinstance(mid, bool) or not isinstance(mid, int) or not isinstance(path, str):
        return None
    trust = raw.get("trust")
    root = raw.get("root")
    row: Dict[str, Any] = {
        "mv_id": mid,
        "root": root if isinstance(root, str) else None,
        "path": path,
        "trust": float(trust)
        if isinstance(trust, (int, float)) and not isinstance(trust, bool)
        else None,
        "reason": str(raw.get("reason") or ""),
    }
    for k in ROW_INT:
        v = raw.get(k)
        row[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
    return row


def validate(doc: Any) -> Dict[str, Any]:
    """The section 4.6 answer, checked: the persona and the three lists.
    Raises ValueError on a wrong shape; drops a row that is not a row."""
    if not isinstance(doc, dict):
        raise ValueError("report is not an object")
    if doc.get("persona") != PERSONA:
        raise ValueError(f"report persona is {doc.get('persona')!r}, not {PERSONA}")
    out: Dict[str, Any] = {
        "persona": PERSONA,
        "generated_at": str(doc.get("generated_at") or ""),
        "trust_prior": doc.get("trust_prior"),
        "truncated": doc.get("truncated") is True,
    }
    for name in LISTS:
        rows = doc.get(name)
        if not isinstance(rows, list):
            raise ValueError(f"report has no {name} list")
        out[name] = [r for r in (_row(x) for x in rows) if r is not None]
    return out


def fetch(environ: Mapping[str, str], timeout_s: float = TIMEOUT_S) -> Dict[str, Any]:
    """GET the report from the proven store. Raises the recall hook's
    RecallError on a transport failure or a store that is down."""
    return validate(rh().store_get(report_url, environ, timeout_s))


def write_cache(
    report: Mapping[str, Any], environ: Mapping[str, str], now: Optional[_dt.datetime] = None
) -> str:
    """Write the cache file atomically: the report, the time it was read and
    the counts."""
    t = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    doc = dict(report)
    doc["format"] = FORMAT
    doc["fetched_at"] = t.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")
    doc["counts"] = {name: len(report.get(name) or []) for name in LISTS}
    path = cache_file(environ)
    _make_dirs(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)
    return path


def refresh(environ: Mapping[str, str]) -> Dict[str, Any]:
    report = fetch(environ)
    write_cache(report, environ)
    return report


def read_cache(environ: Mapping[str, str]) -> Dict[str, Any]:
    with open(cache_file(environ), encoding="utf-8") as fh:
        doc = json.load(fh)
    report = validate(doc)
    report["fetched_at"] = doc.get("fetched_at") if isinstance(doc, dict) else None
    return report


HEADINGS = {
    "retire": "Retire candidates (shown in 20 or more sessions, never used)",
    "promote": "Promote candidates (high trust, used broadly; not in MEMORY.md)",
    "demote": "Demote candidates (a MEMORY.md line whose file was not used in 30 days)",
}


def _fmt(value: Any, spec: str = "") -> str:
    return "-" if value is None else format(value, spec)


def render(report: Mapping[str, Any], limit: int = 50) -> str:
    lines = [
        f"Memory trust report for persona {PERSONA}: generated "
        f"{report.get('generated_at') or '-'}, read {report.get('fetched_at') or '-'}, "
        f"prior {_fmt(report.get('trust_prior'))}."
    ]
    for name in LISTS:
        rows: Sequence[Mapping[str, Any]] = report.get(name) or []
        lines.append("")
        lines.append(f"{HEADINGS[name]}: {len(rows)}")
        for r in rows[:limit]:
            where = f"{r['root']}/{r['path']}" if r.get("root") else r["path"]
            lines.append(
                f"- {where} (id {r['mv_id']}): trust {_fmt(r.get('trust'), '.2f')}, "
                f"trials {_fmt(r.get('trials'))}, shown in {_fmt(r.get('shown_sessions'))} "
                f"sessions, used in {_fmt(r.get('use_sessions'))}. {r.get('reason') or ''}".rstrip()
            )
        if len(rows) > limit:
            lines.append(f"- ... {len(rows) - limit} more (use --limit)")
    if report.get("truncated"):
        lines.append("")
        lines.append("The store cut a list at 500 rows.")
    lines.append("")
    lines.append(
        "To retire a file: move it to the memory folder's .archive/ and remove its index lines. "
        "To promote or demote: edit MEMORY.md. These are agent actions; this tool changes nothing."
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cache-only", action="store_true", help="print the cache file; no store call")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--limit", type=int, default=50, help="rows per list (default 50)")
    args = ap.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    try:
        if args.cache_only:
            report = read_cache(env)
        else:
            report = refresh(env)
            report["fetched_at"] = read_cache(env).get("fetched_at")
    except (OSError, ValueError) as exc:
        print(f"trust report: cannot read it ({exc})", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - RecallError and transport failures
        reason = getattr(exc, "reason", type(exc).__name__)
        print(
            f"trust report: the memory store did not answer ({reason}); "
            f"--cache-only prints the last copy",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render(report, max(1, args.limit)))
    return 0


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _load("hook_config").make_dirs(path)


if __name__ == "__main__":
    sys.exit(main())
