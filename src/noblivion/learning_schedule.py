# SPDX-License-Identifier: AGPL-3.0-or-later
"""An optional time window for learning model calls (NOBLIVION-266).

The ``schedule`` block of learning.json names one daily window in local time,
for example ``{"window": "22:00-06:00"}``. Outside it, the store's worker loops
claim no job and send no model call. A window may cross midnight. With no
``schedule`` block learning may call at any time, as before.
"""

from __future__ import annotations

import re
from datetime import datetime

SCHEDULE_KEYS = {"window"}
# Outside the window a job waits at most this long before it looks again, so a
# changed learning.json is seen within the hour.
MAX_WINDOW_WAIT = 3600.0
_WINDOW = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])\Z")
DAY = 86400


def local_now() -> datetime:
    """The local wall-clock time. Tests replace it."""
    return datetime.now()


def schedule_options(settings: dict) -> dict | None:
    """The validated ``schedule`` block, or None when none is configured.

    A malformed block raises ValueError and learning stays off, like the other
    blocks. A window that starts where it ends is malformed: it names no time.
    """
    block = settings.get("schedule")
    if block is None:
        return None
    if not isinstance(block, dict) or set(block) - SCHEDULE_KEYS or "window" not in block:
        raise ValueError("invalid learning schedule")
    window = block["window"]
    match = _WINDOW.match(window) if isinstance(window, str) else None
    if match is None:
        raise ValueError("the learning schedule window must be HH:MM-HH:MM")
    hour, minute, end_hour, end_minute = (int(part) for part in match.groups())
    start, end = (hour * 60 + minute) * 60, (end_hour * 60 + end_minute) * 60
    if start == end:
        raise ValueError("the learning schedule window is empty")
    return {"window": window, "start": start, "end": end}


def window_open(options: dict | None, now: datetime) -> bool:
    """True when ``now`` is inside the window; always True with no window."""
    if options is None:
        return True
    second = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    start, end = options["start"], options["end"]
    if start < end:
        return start <= second < end
    return second >= start or second < end  # the window crosses midnight


def window_wait(options: dict | None, now: datetime) -> float:
    """Seconds learning waits now: 0 inside the window, else up to the next start.

    The wait is at most ``MAX_WINDOW_WAIT``, so a job that went back looks
    again within the hour, and at least one second.
    """
    if window_open(options, now):
        return 0.0
    second = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return max(1.0, min(MAX_WINDOW_WAIT, (options["start"] - second) % DAY))


def schedule_report(options: dict | None, now: datetime) -> dict:
    """The schedule part of the learning status."""
    if options is None:
        return {"window": None, "open": True}
    report = {"window": options["window"], "open": window_open(options, now)}
    if not report["open"]:
        second = now.hour * 3600 + now.minute * 60 + now.second
        report["opens_in_seconds"] = int((options["start"] - second) % DAY)
    return report
