#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stop hook: send the session's trust events to the local store.
Part E of the adaptive memory trust work (design doc 0001, section 3.5).

A SEPARATE Stop command, not a step of the stop checks: those have a 1.8 s
self-limit and Stop fires at the end of every turn, so a 2 s POST inside them
would break it. This hook starts ONE detached worker and returns at once. It
prints nothing and exits 0 on every path. It does nothing when the trust
events are off (``trust_events.enabled``: ``NOBLIVION_TRUST_EVENTS`` or the
config key ``trust.events``; on by default).

The worker (``--child <session> <transcript> <cwd>``), under a per-session
lock:

1. reads the transcript (and its subagent transcripts) from where the last
   flush stopped, and appends one ``use`` event per ``noblivion_recall`` fetch
   that returned a memory (``trust_events.fetch_uses``);
2. reads the session's event file from the sent offset, merges repeats (one
   event per memory and kind, the earliest time), and POSTs them to
   ``POST /api/memory/feedback/batch`` (design doc section 4.5) in batches of
   at most 500 events and 256 KB, each with a 2 s total timeout. The body
   carries the session's ``root`` (section 5.1), so a ``path`` event resolves
   in the session's own memory folder;
3. moves the sent offset to the end of what the store accepted (HTTP 200);
   a failure keeps the offset, so the next Stop sends the lines again;
4. retries up to OTHER_SESSIONS_MAX other sessions that still have unsent
   lines (a session whose last Stop found the store down), refreshes the
   trust report cache once a day (``trust_report``), and removes event files
   that are sent and older than KEEP_DAYS.

A repeated Stop is safe: the store keeps one event per (session, memory,
kind), so a repeat is a duplicate, not a second count. One line per flush
goes to ``<cache>/trust-flush.log`` (counts and a reason only; no token, no
memory text).

The store is found and proved through ``store_client`` (``store.json``, the
health nonce and its HMAC proof); only a proven listener gets the token. The
flush never starts a store: a store that is down keeps the offset (section
3.5).

Standard library only. Hooks load their siblings by path.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.realpath(__file__))
ROUTE = "/api/memory/feedback/batch"
STATE_NAME = "trust-flush.json"
LOCK_NAME = "trust-flush.lock"
LOG_NAME = "trust-flush.log"
POST_TIMEOUT_S = 2.0
MAX_EVENTS = 500
MAX_BYTES = 256 * 1024
OTHER_SESSIONS_MAX = 5
KEEP_DAYS = 7.0
MAX_KEEP_DAYS = 30.0  # an unsent file older than this is dropped too
LOCK_WAIT_S = 10.0
REPORT_MAX_AGE_S = 20 * 3600.0
REPORT_RETRY_S = 3600.0
RESPONSE_MAX_BYTES = 65536

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


def te() -> Any:
    return _load("trust_events")


def sc() -> Any:
    return _load("store_client")


# ── state ───────────────────────────────────────────────────────────────────


def _state_path(cache: str, sid: str) -> str:
    return os.path.join(cache, "by-session", f"{sid}.{STATE_NAME}")


def load_state(cache: str, sid: str) -> Dict[str, Any]:
    try:
        with open(_state_path(cache, sid), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    sent = doc.get("sent")
    transcripts = doc.get("transcripts")
    root = doc.get("root")
    state: Dict[str, Any] = {
        "sent": sent if isinstance(sent, int) and sent >= 0 else 0,
        "transcripts": transcripts if isinstance(transcripts, dict) else {},
    }
    if isinstance(root, str) and root:
        state["root"] = root
    return state


def save_state(cache: str, sid: str, state: Mapping[str, Any]) -> bool:
    path = _state_path(cache, sid)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def session_lock(cache: str, sid: str, wait_s: Optional[float] = None):
    """Yields True when this process holds the session's flush lock. The wait
    defaults to LOCK_WAIT_S, read at call time."""
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    path = os.path.join(cache, "by-session", f"{sid}.{LOCK_NAME}")
    fd = -1
    held = False
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
    except OSError:
        held = False
    try:
        yield held
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                if held:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def log(cache: str, sid: str, status: str) -> None:
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        os.makedirs(cache, mode=0o700, exist_ok=True)
        with open(os.path.join(cache, LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write(f"{ts} session={sid} {status}\n")
    except OSError:
        return


# ── the session root ────────────────────────────────────────────────────────


def session_root(environ: Mapping[str, str], cwd: str) -> Optional[str]:
    """The memory folder key of the session (design doc section 5.1), by the
    recall hook's own rule: ``NOBLIVION_RECALL_ROOT``, else the parent name
    of the session's memory folder, else the slug of ``cwd``. None when it
    cannot be known; the store then resolves a path only when it is unique."""
    try:
        rh = _load("recall_hook")
        payload = {"cwd": cwd} if cwd else {}
        root = rh.session_root(rh.session_env(environ, payload))
    except Exception:  # noqa: BLE001 - no root: the store's unique-path rule
        return None
    return root if isinstance(root, str) and root else None


# ── 1. fetches from the transcript ──────────────────────────────────────────


def scan_transcripts(
    cache: str, sid: str, transcript: str, state: Dict[str, Any], environ: Mapping[str, str]
) -> int:
    """Append the fetch ``use`` events found since the last flush. Returns the
    number appended, or -1 when they could not be appended. The offsets and the
    calls still waiting for a result are kept in ``state``, and move only once
    the events are on disk: a failed append is read again at the next Stop."""
    if not transcript or not os.path.isfile(transcript):
        return 0
    seen: Dict[str, Any] = state.setdefault("transcripts", {})
    moved: Dict[str, Any] = {}
    found: List[Dict[str, Any]] = []
    for path in te().iter_transcript_files(transcript):
        raw = seen.get(path)
        entry: Dict[str, Any] = raw if isinstance(raw, dict) else {}
        off = entry.get("offset")
        offset = off if isinstance(off, int) else 0
        got = entry.get("pending")
        pending: Dict[str, str] = got if isinstance(got, dict) else {}
        try:
            if os.path.getsize(path) < offset:  # rewritten: read it again
                offset, pending = 0, {}
            records, new_offset = te().read_records(path, offset)
        except OSError:
            continue
        events, pending = te().fetch_uses([r for _, r in records], pending)
        found.extend(events)
        moved[path] = {"offset": new_offset, "pending": pending}
    if found and not te().append_events(cache, sid, found, environ):
        return -1
    seen.update(moved)
    return len(found)


# ── 2. the batch ────────────────────────────────────────────────────────────


def read_unsent(cache: str, sid: str, sent: int) -> Tuple[List[Dict[str, Any]], int, int]:
    """``(events, end offset, bad lines)`` of the lines after ``sent``.
    Repeats are merged: one event per (memory, kind), the earliest time."""
    path = te().events_file(cache, sid)
    if path is None or not os.path.isfile(path):
        return [], sent, 0
    if os.path.getsize(path) < sent:  # replaced: send it all again
        sent = 0
    records, end = te().read_records(path, sent)
    events, bad = te().merge_events(rec for _, rec in records)
    return events, end, bad


def _head(sid: str, root: Optional[str]) -> Dict[str, Any]:
    head: Dict[str, Any] = {"session_id": sid}
    if root:
        head["root"] = root
    return head


def batches(
    sid: str,
    events: Sequence[Mapping[str, Any]],
    max_events: int = MAX_EVENTS,
    max_bytes: int = MAX_BYTES,
    root: Optional[str] = None,
) -> List[bytes]:
    """The request bodies: each at most ``max_events`` events and ``max_bytes``."""
    out: List[bytes] = []
    head = len(json.dumps(dict(_head(sid, root), events=[])).encode("utf-8"))
    cur: List[Mapping[str, Any]] = []
    size = head
    for ev in events:
        n = len(json.dumps(ev, separators=(",", ":")).encode("utf-8")) + 1
        if cur and (len(cur) >= max_events or size + n > max_bytes):
            out.append(_body(sid, cur, root))
            cur, size = [], head
        cur.append(ev)
        size += n
    if cur:
        out.append(_body(sid, cur, root))
    return out


def _body(sid: str, events: Sequence[Mapping[str, Any]], root: Optional[str]) -> bytes:
    doc = dict(_head(sid, root), events=list(events))
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


class PostError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_timeout(exc: BaseException) -> bool:
    return "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()


def http_json(
    method: str, url: str, token: str, body: Optional[bytes], timeout_s: float = POST_TIMEOUT_S
) -> Any:
    """Send one request to the loopback store and decode the JSON answer
    within ``timeout_s`` TOTAL. The request runs in a thread; past the
    deadline the socket is shut down. Raises PostError with a short reason;
    never follows a redirect. ``token`` is sent only when not empty."""
    import http.client
    import urllib.parse

    box: Dict[str, Any] = {}
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise PostError("not_loopback")  # store_client only names 127.0.0.1
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    def worker() -> None:
        conn = None
        resp = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=timeout_s)
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if token:
                headers["Authorization"] = f"Bearer {token}"
            conn.connect()
            box["sock"] = conn.sock
            if box.get("aborted"):
                return
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(RESPONSE_MAX_BYTES + 1)
            if resp.status != 200:
                box["error"] = PostError(f"http_{resp.status}")
            elif len(raw) > RESPONSE_MAX_BYTES:
                box["error"] = PostError("too_large")
            else:
                try:
                    box["value"] = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    box["error"] = PostError("bad_json")
        except OSError as exc:
            name = type(exc).__name__
            box["error"] = PostError("timeout" if _is_timeout(exc) else f"unreachable:{name}")
        except Exception as exc:  # noqa: BLE001 - every failure is a reason string
            box["error"] = PostError(f"error:{type(exc).__name__}")
        finally:
            for closer in (resp, conn):
                if closer is not None:
                    with contextlib.suppress(OSError):
                        closer.close()

    t = threading.Thread(target=worker, name="trust-flush-http", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        box["aborted"] = True
        sock = box.get("sock")
        if isinstance(sock, socket.socket):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        raise PostError("timeout")
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise PostError("no_response")
    return box["value"]


def connect_store(environ: Mapping[str, str], timeout_s: float = POST_TIMEOUT_S) -> Tuple[str, str]:
    """``(base_url, token)`` of a proven store (``store_client.connect``).
    Raises PostError with the store_client reason."""
    client = sc()
    probe_s = min(timeout_s, max(client.PROBE_TIMEOUT_S, timeout_s / 2.0))
    try:
        return client.connect(
            environ, lambda url, budget: http_json("GET", url, "", None, budget), probe_s
        )
    except client.StoreUnavailable as exc:
        raise PostError(exc.reason) from None


Poster = Callable[[str, str, bytes, float], Any]
COUNT_KEYS = ("inserted", "duplicate", "unknown", "rejected")
# A store answer that the same body gets again on every try (NOBLIVION-21):
# the batch is dropped and logged, not sent again at every Stop.
PERMANENT_REASONS = frozenset({"http_400", "http_413", "http_422"})


def _post(url: str, token: str, body: bytes, timeout_s: float) -> Any:
    return http_json("POST", url, token, body, timeout_s)


def _counts(answer: Any) -> Dict[str, int]:
    if not isinstance(answer, dict):
        raise PostError("bad_answer")
    out: Dict[str, int] = {}
    for k in COUNT_KEYS:
        v = answer.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int):
            raise PostError("bad_answer")
        out[k] = v
    return out


def flush_events(
    cache: str,
    sid: str,
    state: Dict[str, Any],
    environ: Mapping[str, str],
    post: Optional[Poster] = None,
    connect: Optional[Callable[[Mapping[str, str]], Tuple[str, str]]] = None,
) -> str:
    """Send the unsent lines. Returns the status for the log line. The sent
    offset moves only when every batch was accepted or refused for good
    (``PERMANENT_REASONS``); a refused batch is dropped and counted in the
    status."""
    events, end, bad = read_unsent(cache, sid, int(state.get("sent") or 0))
    if not events:
        if end != state.get("sent"):
            state["sent"] = end
        return f"ok:nothing bad={bad}"
    try:
        base, token = (connect or connect_store)(environ)
    except PostError as exc:
        return f"fail:{exc.reason} events={len(events)}"
    except Exception as exc:  # noqa: BLE001 - a fake or a client bug: fail open
        return f"fail:error:{type(exc).__name__} events={len(events)}"
    url = base + ROUTE
    send = post or _post
    total = dict.fromkeys(COUNT_KEYS, 0)
    root = state.get("root") if isinstance(state.get("root"), str) else None
    dropped: Dict[str, int] = {}
    for body in batches(sid, events, root=root):
        try:
            counts = _counts(send(url, token, body, POST_TIMEOUT_S))
        except PostError as exc:
            if exc.reason in PERMANENT_REASONS:
                n = len(json.loads(body)["events"])
                dropped[exc.reason] = dropped.get(exc.reason, 0) + n
                continue
            if not exc.reason.startswith("http_"):
                with contextlib.suppress(Exception):
                    sc().forget(environ)  # no answer: prove again next time
            return f"fail:{exc.reason} events={len(events)}"
        except Exception as exc:  # noqa: BLE001 - a fake or a transport bug: fail open
            return f"fail:error:{type(exc).__name__} events={len(events)}"
        for k in COUNT_KEYS:
            total[k] += counts[k]
    state["sent"] = end
    status = (
        "ok events={} inserted={inserted} duplicate={duplicate} unknown={unknown} "
        "rejected={rejected} bad={bad}"
    ).format(len(events), bad=bad, **total)
    for reason in sorted(dropped):
        status += f" dropped:{reason}={dropped[reason]}"
    return status


# ── 4. other sessions, the report, pruning ──────────────────────────────────


def _event_sessions(cache: str) -> List[Tuple[float, str, int]]:
    """(mtime, session, size) of every event file, newest first."""
    folder = os.path.join(cache, "by-session")
    suffix = "." + te().EVENTS_NAME
    out: List[Tuple[float, str, int]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(suffix):
            continue
        sid = name[: -len(suffix)]
        if te().valid_sid(sid) is None:
            continue
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        out.append((st.st_mtime, sid, st.st_size))
    out.sort(reverse=True)
    return out


def flush_others(
    cache: str,
    own: str,
    environ: Mapping[str, str],
    post: Optional[Poster] = None,
    now: Optional[float] = None,
    connect: Optional[Callable[[Mapping[str, str]], Tuple[str, str]]] = None,
) -> int:
    """Retry other sessions with unsent lines; stop at the first failure (the
    store is down, so the next one would fail too). Returns the count tried."""
    cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400.0
    tried = 0
    for mtime, sid, size in _event_sessions(cache):
        if tried >= OTHER_SESSIONS_MAX or mtime < cutoff:
            break
        if sid == own or load_state(cache, sid)["sent"] >= size:
            continue
        with session_lock(cache, sid, wait_s=0.0) as held:
            if not held:
                continue
            state = load_state(cache, sid)  # read again under the lock
            if state["sent"] >= size:
                continue
            tried += 1
            status = flush_events(cache, sid, state, environ, post, connect)
            save_state(cache, sid, state)
            log(cache, sid, "retry " + status)
            if status.startswith("fail:"):
                break
    return tried


def prune(cache: str, own: str, now: Optional[float] = None) -> int:
    """Remove the files of every other session (events, state, lock) whose
    newest file is older than KEEP_DAYS and whose events were all sent, and of
    any session older than MAX_KEEP_DAYS. Returns the count removed."""
    t = time.time() if now is None else now
    folder = os.path.join(cache, "by-session")
    suffixes = ("." + te().EVENTS_NAME, "." + STATE_NAME, "." + LOCK_NAME)
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    groups: Dict[str, List[Tuple[str, float]]] = {}
    for name in names:
        sfx = next((x for x in suffixes if name.endswith(x)), None)
        sid = name[: -len(sfx)] if sfx else ""
        if not sid or sid == own or te().valid_sid(sid) is None:
            continue
        try:
            mtime = os.stat(os.path.join(folder, name)).st_mtime
        except OSError:
            continue
        groups.setdefault(sid, []).append((name, mtime))
    removed = 0
    for sid, files in groups.items():
        age_days = (t - max(m for _, m in files)) / 86400.0
        if age_days < KEEP_DAYS:
            continue
        if age_days < MAX_KEEP_DAYS:
            size = 0
            with contextlib.suppress(OSError):
                size = os.path.getsize(os.path.join(folder, f"{sid}.{te().EVENTS_NAME}"))
            if load_state(cache, sid)["sent"] < size:
                continue
        for name, _ in files:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, name))
                removed += 1
    return removed


def refresh_report(environ: Mapping[str, str], now: Optional[float] = None) -> str:
    """Refresh the trust report cache when it is older than a day."""
    try:
        report = _load("trust_report")
        path = report.cache_file(environ)
        t = time.time() if now is None else now
        with contextlib.suppress(OSError):
            if t - os.path.getmtime(path) < REPORT_MAX_AGE_S:
                return "fresh"
        # At most one attempt an hour: a store that is down must not cost a
        # request at every Stop.
        stamp = path + ".attempt"
        with contextlib.suppress(OSError):
            if t - os.path.getmtime(stamp) < REPORT_RETRY_S:
                return "skip:recent_attempt"
        os.makedirs(os.path.dirname(stamp), mode=0o700, exist_ok=True)
        with open(stamp, "w", encoding="utf-8"):
            pass
        report.refresh(environ)
        return "refreshed"
    except Exception as exc:  # noqa: BLE001 - the report is optional here
        return f"fail:{type(exc).__name__}"


# ── the worker and the hook ─────────────────────────────────────────────────


def run_child(
    sid: str,
    transcript: str,
    environ: Mapping[str, str],
    post: Optional[Poster] = None,
    refresh: Optional[Callable[[Mapping[str, str]], str]] = None,
    cwd: str = "",
    connect: Optional[Callable[[Mapping[str, str]], Tuple[str, str]]] = None,
) -> str:
    """The detached worker's whole job. Returns the status it logged."""
    cache = te().cache_dir(environ)
    if te().valid_sid(sid) is None:
        return "skip:bad_session"
    with session_lock(cache, sid) as held:
        if not held:
            log(cache, sid, "skip:locked")
            return "skip:locked"
        state = load_state(cache, sid)
        root = session_root(environ, cwd)
        if root:
            state["root"] = root
        fetched = scan_transcripts(cache, sid, transcript, state, environ)
        status = flush_events(cache, sid, state, environ, post, connect)
        save_state(cache, sid, state)
    status = f"{status} fetch_uses={fetched}" if fetched >= 0 else f"{status} fetch_append_failed"
    if status.startswith("ok"):
        others = flush_others(cache, sid, environ, post, connect=connect)
        status += f" others={others}"
        status += " report=" + (refresh or refresh_report)(environ)
    prune(cache, sid)
    log(cache, sid, status)
    return status


def spawn_child(sid: str, transcript: str, environ: Mapping[str, str], cwd: str = "") -> bool:
    """Start the detached worker: its own session, no pipe of the hook, so
    Claude Code does not wait for it."""
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, os.path.abspath(__file__), "--child", sid, transcript, cwd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=dict(environ),
            cwd="/",
        )
        return True
    except OSError:
        return False


def main(
    stdin=None,
    stdout=None,
    environ: Optional[Mapping[str, str]] = None,
    spawn: Optional[Callable[..., bool]] = None,
) -> int:
    """The Stop hook. Prints nothing, returns 0 on every path."""
    del stdout  # this hook never writes to it
    env = dict(os.environ if environ is None else environ)
    try:
        try:
            mod = te()
        except Exception as exc:  # noqa: BLE001 - an install without the module: say so
            log(_fallback_cache(env), "-", f"skip:no_module:{type(exc).__name__}")
            return 0
        if not mod.enabled(env):
            return 0
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        if not isinstance(payload, dict):
            return 0
        sid = payload.get("session_id")
        if not isinstance(sid, str) or mod.valid_sid(sid) is None:
            return 0
        transcript = payload.get("transcript_path")
        transcript = transcript if isinstance(transcript, str) else ""
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else ""
        (spawn or spawn_child)(sid, transcript, env, cwd)
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass
    return 0


def _fallback_cache(env: Mapping[str, str]) -> str:
    """``NOBLIVION_RECALL_CACHE_DIR``, else ``<data dir>/cache`` by the
    section 12.2 rule, without any sibling module."""
    explicit = (env.get("NOBLIVION_RECALL_CACHE_DIR") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    data = (env.get("NOBLIVION_DATA_DIR") or env.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if not data:
        xdg = (env.get("XDG_DATA_HOME") or "").strip() or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
        data = os.path.join(xdg, "noblivion")
    return os.path.join(os.path.expanduser(data), "cache")


def _cli(argv: Sequence[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--child":
        try:
            run_child(
                argv[1],
                argv[2] if len(argv) > 2 else "",
                dict(os.environ),
                cwd=argv[3] if len(argv) > 3 else "",
            )
        except Exception:  # noqa: BLE001, S110 - a detached worker has no one to tell
            pass
        return 0
    return main()


if __name__ == "__main__":
    try:
        code = _cli(sys.argv[1:])
    except BaseException:  # noqa: BLE001 - a hook fails open
        code = 0
    os._exit(code)
