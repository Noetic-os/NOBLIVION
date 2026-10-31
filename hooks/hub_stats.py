# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hub rows: notes that the prompt hook shows in more than half of the sessions
(NOBLIVION-121).

A note that fits many prompts (a general rule about timers, hooks or
notifications) is shown in most sessions. It fills a place on every prompt, so it
costs characters again and again and tells the model little that it does not
know. The hook counts, for each note, the sessions that were shown it on each
day. A note that the hook would show in more than ``HUB_SHARE`` of the sessions of
the last N days is a hub. The count is of the rows before the hub rule is
applied, and it is of the rows the hook would show (the first ``recall.prompt_rows``
rows), not of the whole candidate list (NOBLIVION-114): a count of the rows after the
rule would fall when the rule works, the note would stop being a hub, and its share would settle at the
threshold. The recall hook moves hub rows behind the other rows and keeps at most
a few of them (``recall_hook.demote_hubs``).

State: ``<cache>/hub-stats.json``, one object per UTC day::

    {"v": 1, "days": {"2026-10-19": {"s": 12, "n": {"note_name": 7}}}}

``s`` is the number of sessions that had at least one such row on that day,
``n`` the number of sessions in which the note was among the rows the hook
would show. A session counts once for
a note: ``<cache>/by-session/<session>.hub-seen.json`` lists the notes that the
session already counted (the recall hook prunes it with the other session files).

The counts are statistics, not a safety control. A write is an atomic replace
without a lock, so two sessions that write at the same moment can lose one
count. A broken or missing file reads as empty. Nothing here raises.

On with a 14-day window unless ``NOBLIVION_RECALL_HUB_DAYS`` (config key
``recall.hub_days``) is 0 (NOBLIVION-142). Standard library only; the hooks load this file by path.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Set, Tuple

DAYS_ENV = "NOBLIVION_RECALL_HUB_DAYS"
DAYS_KEY = "recall.hub_days"
DAYS_MAX = 30
DAYS_DEFAULT = 14  # equals config/config.default.json (NOBLIVION-142)
ROWS_ENV = "NOBLIVION_RECALL_HUB_ROWS"
ROWS_KEY = "recall.hub_rows"
ROWS_DEFAULT = 1
ROWS_MAX = 50

# "More than half of the sessions" (NOBLIVION-121).
HUB_SHARE = 0.5
# Fewer sessions than this in the window: no note is a hub. Two sessions that
# both show a note are not evidence that the note is a hub.
MIN_SESSIONS = 10

STATS_NAME = "hub-stats.json"
SEEN_SUFFIX = ".hub-seen.json"
SESSION_DIR_NAME = "by-session"
STATS_MAX_BYTES = 2_000_000  # a larger file is read as empty and is rewritten
NAME_MAX = 200
SESSION_MARK = "\x00session"  # in the seen list: this session is counted for the day
_SID_RX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _hc(env: Optional[Mapping[str, str]]) -> Any:
    import importlib.util
    import sys

    mod = sys.modules.get("hub_stats_hook_config")
    if mod is None:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hook_config.py")
        spec = importlib.util.spec_from_file_location("hub_stats_hook_config", path)
        if spec is None or spec.loader is None:
            raise ImportError("hook_config")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hub_stats_hook_config"] = mod
        spec.loader.exec_module(mod)
    return mod


def _int_setting(
    env: Mapping[str, str], name: str, key: str, default: int, low: int, high: int
) -> int:
    """The env var, else the config key, else ``default``; an unreadable value is
    the default (a hook never fails a prompt over a bad setting)."""
    raw: Any = (env.get(name) or "").strip() if name in env else None
    if raw is None or raw == "":
        try:
            raw = _hc(env).get(key, None, env)
        except Exception:  # noqa: BLE001 - a bad config reads as unset
            raw = None
    if isinstance(raw, bool) or raw is None or raw == "":
        return default
    try:
        return max(low, min(int(str(raw).strip()), high))
    except ValueError:
        return default


def window_days(env: Mapping[str, str]) -> int:
    """The hub window in days, 0 = off; 14 when the setting is absent."""
    return _int_setting(env, DAYS_ENV, DAYS_KEY, DAYS_DEFAULT, 0, DAYS_MAX)


def hub_rows(env: Mapping[str, str]) -> int:
    """How many hub rows one prompt may show, default 1."""
    return _int_setting(env, ROWS_ENV, ROWS_KEY, ROWS_DEFAULT, 0, ROWS_MAX)


def _parse_day(text: str) -> _dt.date:
    """``YYYY-MM-DD`` as a date. Only ``_dt.datetime`` and ``_dt.timedelta`` are
    used from ``_dt``, because the replay tool replaces ``_dt`` with a clock that
    has only those (``tools/replay_prompt_rows.py``)."""
    return _dt.datetime.fromisoformat(text).date()


def _today(now: Optional[_dt.datetime] = None) -> _dt.date:
    t = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).date()


def stats_path(cache: str) -> str:
    return os.path.join(cache, STATS_NAME)


def seen_path(cache: str, session_id: Any) -> Optional[str]:
    if not isinstance(session_id, str) or not _SID_RX.match(session_id):
        return None
    return os.path.join(cache, SESSION_DIR_NAME, f"{session_id}{SEEN_SUFFIX}")


def _read_json(path: str) -> Any:
    try:
        if os.path.getsize(path) > STATS_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, doc: Any) -> bool:
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_days(cache: str) -> Dict[str, Dict[str, Any]]:
    """``{day: {"s": sessions, "n": {name: sessions}}}``, only well-formed days."""
    doc = _read_json(stats_path(cache))
    days = doc.get("days") if isinstance(doc, dict) else None
    out: Dict[str, Dict[str, Any]] = {}
    for day, rec in days.items() if isinstance(days, dict) else ():
        if not (isinstance(day, str) and isinstance(rec, dict)):
            continue
        s, n = rec.get("s"), rec.get("n")
        if isinstance(s, int) and not isinstance(s, bool) and isinstance(n, dict):
            out[day] = {
                "s": max(0, s),
                "n": {
                    k: v
                    for k, v in n.items()
                    if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                },
            }
    return out


def window(
    days: Mapping[str, Mapping[str, Any]], span: int, now: Optional[_dt.datetime] = None
) -> Tuple[int, Dict[str, int]]:
    """``(sessions, {name: sessions shown})`` over the last ``span`` UTC days,
    today included."""
    today = _today(now)
    first = today - _dt.timedelta(days=max(1, span) - 1)
    sessions = 0
    counts: Dict[str, int] = {}
    for day, rec in days.items():
        try:
            d = _parse_day(day)
        except ValueError:
            continue
        if not first <= d <= today:
            continue
        sessions += int(rec.get("s") or 0)
        for name, n in (rec.get("n") or {}).items():
            counts[name] = counts.get(name, 0) + int(n)
    return sessions, counts


def hubs(cache: str, span: int, now: Optional[_dt.datetime] = None) -> Tuple[Set[str], int]:
    """``(names of the hub notes, sessions in the window)``. Fewer than
    ``MIN_SESSIONS`` sessions: no hub. Never raises."""
    try:
        sessions, counts = window(load_days(cache), span, now)
    except Exception:  # noqa: BLE001 - no statistics, no hub
        return set(), 0
    if sessions < MIN_SESSIONS:
        return set(), sessions
    return {n for n, c in counts.items() if c / sessions > HUB_SHARE}, sessions


def record(
    cache: str,
    session_id: Any,
    names: Iterable[str],
    span: int,
    now: Optional[_dt.datetime] = None,
) -> bool:
    """Count the notes that the session was just shown, once per session and
    note. True when the counts reached the disk or there was nothing to count.
    Needs a valid session id (without one the session cannot be told from
    another). Never raises."""
    try:
        sid_file = seen_path(cache, session_id)
        if sid_file is None:
            return False
        today = _today(now)
        day = today.isoformat()
        raw = _read_json(sid_file)
        seen: List[str] = [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []
        counted = set(seen)
        fresh = sorted({n[:NAME_MAX] for n in names if isinstance(n, str) and n} - counted)
        first = SESSION_MARK not in counted
        if not fresh and not first:
            return True
        days = load_days(cache)
        rec = days.setdefault(day, {"s": 0, "n": {}})
        if first:
            rec["s"] += 1
        for name in fresh:
            rec["n"][name] = int(rec["n"].get(name, 0)) + 1
        keep_from = today - _dt.timedelta(days=max(span, 1) + 1)
        for old in list(days):
            try:
                if _parse_day(old) < keep_from:
                    del days[old]
            except ValueError:
                del days[old]
        ok = _write_json(stats_path(cache), {"v": 1, "days": days})
        if ok:
            _write_json(sid_file, sorted(counted | set(fresh) | {SESSION_MARK}))
        return ok
    except Exception:  # noqa: BLE001 - statistics are optional
        return False
