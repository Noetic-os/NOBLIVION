# SPDX-License-Identifier: AGPL-3.0-or-later
"""An optional load probe: learning waits while other clients queue (NOBLIVION-260).

The probe reads one gauge from a Prometheus-style metrics page of the shared
local model server, for example ``num_requests_waiting``. It is a policy: the
supervisor reads it again every pass, and a started job never freezes it. With
no ``load_probe`` block nothing here runs and no request is made.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_GAUGE = "num_requests_waiting"
# Read at most this much of a metrics page, and cache an answer this long, so
# several worker loops never turn into a stream of requests.
MAX_PAGE_BYTES = 2 * 1024 * 1024
CACHE_SECONDS = 1.0
PROBE_KEYS = {"url", "gauge", "max_waiting", "timeout_seconds", "max_backoff_seconds"}
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_options(settings: dict) -> dict | None:
    """The validated ``load_probe`` block, or None when none is configured.

    The page must be on loopback, like the provider: no proxy, no redirect, no
    credentials. A malformed block raises ValueError and learning stays off.
    """
    block = settings.get("load_probe")
    if block is None:
        return None
    if not isinstance(block, dict) or set(block) - PROBE_KEYS:
        raise ValueError("invalid learning load probe")
    url = block.get("url")
    if not isinstance(url, str):
        raise ValueError("invalid learning load probe")
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("the learning load probe must be an http loopback URL")
    gauge = block.get("gauge", DEFAULT_GAUGE)
    if not isinstance(gauge, str) or not _NAME.match(gauge):
        raise ValueError("invalid learning load probe gauge")
    numbers = {}
    for name, default, low, high in (
        ("max_waiting", 0, 0, 1_000_000),
        ("timeout_seconds", 2.0, 0.05, 30.0),
        ("max_backoff_seconds", 30.0, 1.0, 3600.0),
    ):
        value = block.get(name, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError("invalid learning load probe limits")
        numbers[name] = value
    return {"url": url, "gauge": gauge, **numbers}


def parse_gauge(text: str, gauge: str) -> float | None:
    """The sum of every sample of ``gauge`` on a metrics page; None when absent."""
    total, found = 0.0, False
    for line in text.splitlines():
        if not line.startswith(gauge) or line.startswith("#"):
            continue
        rest = line[len(gauge) :]
        if rest[:1] == "{":
            end = rest.rfind("}")
            if end < 0:
                continue
            rest = rest[end + 1 :]
        elif rest[:1] not in (" ", "\t"):
            continue  # a longer name that starts with the gauge name
        fields = rest.split()
        if not fields:
            continue
        try:
            value = float(fields[0])
        except ValueError:
            continue
        if not math.isfinite(value) or value < 0:
            continue
        total += value
        found = True
    return total if found else None


class LoadProbe:
    """Reads the waiting gauge; shared by all worker loops of one store."""

    def __init__(self, options: dict):
        self.options = dict(options)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._read_at = float("-inf")
        self._value: float | None = None
        self._unreadable = False
        self._backoff = 0.0
        self._hold_until = 0.0

    def same(self, options: dict) -> bool:
        return options == self.options

    def waiting(self) -> float | None:
        """Requests that wait now, or None when the page cannot be read."""
        with self._lock:
            now = time.monotonic()
            if now - self._read_at >= CACHE_SECONDS:
                self._value = self._fetch()
                self._read_at = time.monotonic()
                unreadable = self._value is None
                if unreadable != self._unreadable:
                    self._unreadable = unreadable
                    if unreadable:
                        log.warning("learning load probe unreadable; learning goes on without it")
                    else:
                        log.info("learning load probe readable again")
            return self._value

    def _fetch(self) -> float | None:
        timeout = self.options["timeout_seconds"]
        deadline = time.monotonic() + timeout
        try:
            with self.opener.open(self.options["url"], timeout=timeout) as response:
                if response.status != 200:
                    return None
                chunks, size = [], 0
                while time.monotonic() < deadline:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        return None
                    chunks.append(chunk)
                else:
                    return None
        except Exception:  # noqa: BLE001 - an unreadable probe never stops learning
            return None
        return parse_gauge(b"".join(chunks).decode("utf-8", "replace"), self.options["gauge"])

    def hold(self) -> float:
        """Seconds learning waits now: 0 when it may call, else a bounded back-off.

        The wait doubles from one second up to ``max_backoff_seconds`` each time
        a hold ends and the queue is still there, and starts again from one second
        once the queue is empty. Loops that ask during a hold share it. An
        unreadable page counts as empty (learning goes on, as with no probe).
        """
        value = self.waiting()
        now = time.monotonic()
        with self._lock:
            if value is None or value <= self.options["max_waiting"]:
                self._backoff, self._hold_until = 0.0, 0.0
                return 0.0
            if now >= self._hold_until:
                self._backoff = min(
                    float(self.options["max_backoff_seconds"]), max(1.0, self._backoff * 2)
                )
                self._hold_until = now + self._backoff
            return self._hold_until - now
