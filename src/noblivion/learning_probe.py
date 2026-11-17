# SPDX-License-Identifier: AGPL-3.0-or-later
"""An optional load probe: learning waits while other clients queue (NOBLIVION-260).

The probe reads one gauge from a Prometheus-style metrics page of the shared
local model server, for example ``num_requests_waiting``. It is a policy: the
supervisor reads it again every pass, and a started job never freezes it. With
no ``load_probe`` block nothing here runs and no request is made.

With ``"strict": true`` (NOBLIVION-266) the probe also reads the running gauge,
holds while any request of another client runs, and holds while the page
cannot be read.
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
DEFAULT_RUNNING_GAUGE = "num_requests_running"
# Read at most this much of a metrics page, and cache an answer this long, so
# several worker loops never turn into a stream of requests.
MAX_PAGE_BYTES = 2 * 1024 * 1024
CACHE_SECONDS = 1.0
PROBE_KEYS = {
    "url",
    "gauge",
    "max_waiting",
    "timeout_seconds",
    "max_backoff_seconds",
    "strict",
    "running_gauge",
}
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
    running_gauge = block.get("running_gauge", DEFAULT_RUNNING_GAUGE)
    for name in (gauge, running_gauge):
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError("invalid learning load probe gauge")
    strict = block.get("strict", False)
    if type(strict) is not bool:
        raise ValueError("invalid learning load probe strict flag")
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
    return {
        "url": url,
        "gauge": gauge,
        **numbers,
        "strict": strict,
        "running_gauge": running_gauge,
    }


_SAMPLE_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def parse_gauge(text: str, gauge: str) -> float | None:
    """The sum of every sample of ``gauge`` on a metrics page; None when absent.

    A sample matches by its exact name or by ``<namespace>:<gauge>``, the form a
    server with a metrics prefix uses (``vllm:num_requests_waiting``). Label sets
    add up.
    """
    total, found = 0.0, False
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        named = _SAMPLE_NAME.match(line)
        if named is None:
            continue
        name = named.group(0)
        if name != gauge and not name.endswith(":" + gauge):
            continue
        rest = line[named.end() :]
        if rest[:1] == "{":
            end = rest.rfind("}")
            if end < 0:
                continue
            rest = rest[end + 1 :]
        elif rest[:1] not in (" ", "\t"):
            continue
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
    """Reads the waiting gauge; shared by all worker loops of one store.

    One read runs at a time, in its own thread, and a caller waits for it at
    most ``timeout_seconds`` in total: a server that sends its page a byte at a
    time cannot hold a worker loop (and its job lease) for longer. No lock is
    held while the page is read. A read that is still running after its
    deadline counts as unreadable, and no second read starts until it ends.
    """

    def __init__(self, options: dict):
        self.options = dict(options)
        self.options.setdefault("strict", False)
        self.options.setdefault("running_gauge", DEFAULT_RUNNING_GAUGE)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._read_at = float("-inf")
        self._values: dict | None = None
        self._unreadable = False
        self._reader: threading.Thread | None = None
        self._result: dict = {}
        self._backoff = 0.0
        self._hold_until = 0.0

    @property
    def strict(self) -> bool:
        return self.options["strict"] is True

    def same(self, options: dict) -> bool:
        return options == self.options

    def waiting(self) -> float | None:
        """Requests that wait now, or None when the page cannot be read."""
        values = self.read()
        return None if values is None else values["waiting"]

    def read(self) -> dict | None:
        """``{"waiting", "running"}`` now, or None when the page cannot be read.

        ``running`` is read only in strict mode; else it is None.
        """
        with self._lock:
            if time.monotonic() - self._read_at < CACHE_SECONDS:
                return self._values
            reader, result = self._reader, self._result
            if reader is None or not reader.is_alive():
                result = {"value": None, "abandoned": False, "recorded": False}
                reader = threading.Thread(
                    target=self._read, args=(result,), name="noblivion-load-probe", daemon=True
                )
                self._reader, self._result = reader, result
                # Start under the lock: a caller in the gap would see a thread
                # that is not alive yet and start a second read.
                reader.start()
            elif result["abandoned"]:
                return self._values  # a read that ran past its deadline still ends
        # Every loop that asks while a read runs waits for that read, so none
        # takes an unknown page for an empty queue; the wait is bounded.
        reader.join(self.options["timeout_seconds"])
        with self._lock:
            if not result["recorded"]:
                result["recorded"] = True
                result["abandoned"] = reader.is_alive()
                self._record(None if result["abandoned"] else result["value"])
            return self._values

    def _read(self, result: dict) -> None:
        result["value"] = self._fetch()

    def _record(self, values: dict | None) -> None:
        self._values = values
        self._read_at = time.monotonic()
        unreadable = values is None
        if unreadable != self._unreadable:
            self._unreadable = unreadable
            if unreadable and self.strict:
                log.warning("learning load probe unreadable; learning waits until it reads")
            elif unreadable:
                log.warning("learning load probe unreadable; learning goes on without it")
            else:
                log.info("learning load probe readable again")

    def _fetch(self) -> dict | None:
        timeout = self.options["timeout_seconds"]
        deadline = time.monotonic() + timeout
        try:
            with self.opener.open(self.options["url"], timeout=timeout) as response:
                if response.status != 200:
                    return None
                chunks, size = [], 0
                while time.monotonic() < deadline:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        return None
                    chunks.append(chunk)
                else:
                    return None
        except Exception:  # noqa: BLE001 - an unreadable page is a state, not an error
            return None
        return self.parse(b"".join(chunks).decode("utf-8", "replace"))

    def parse(self, text: str) -> dict | None:
        """Both gauges of one page; None when a gauge that counts is missing."""
        waiting = parse_gauge(text, self.options["gauge"])
        if waiting is None:
            return None
        running = None
        if self.strict:
            running = parse_gauge(text, self.options["running_gauge"])
            if running is None:
                return None
        return {"waiting": waiting, "running": running}

    def hold(self, own_running: int = 0) -> float:
        """Seconds learning waits now: 0 when it may call, else a bounded back-off.

        The wait doubles from one second up to ``max_backoff_seconds`` each time
        a hold ends and the server is still busy, and starts again from one
        second once it is free. Loops that ask during a hold share it.

        Busy means more than ``max_waiting`` requests wait. In strict mode it
        also means that more requests run than ``own_running``, the calls that
        learning itself has in flight, or that the page cannot be read. Without
        strict mode an unreadable page counts as empty (learning goes on, as with
        no probe).
        """
        values = self.read()
        now = time.monotonic()
        with self._lock:
            if values is None:
                busy = self.strict
            else:
                busy = values["waiting"] > self.options["max_waiting"]
                if self.strict and values["running"] - max(0, own_running) > 0:
                    busy = True
            if not busy:
                self._backoff, self._hold_until = 0.0, 0.0
                return 0.0
            if now >= self._hold_until:
                self._backoff = min(
                    float(self.options["max_backoff_seconds"]), max(1.0, self._backoff * 2)
                )
                self._hold_until = now + self._backoff
            return self._hold_until - now
