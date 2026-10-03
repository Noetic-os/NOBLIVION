#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostToolUse hook (Write|Edit|MultiEdit, and Bash): index a written memory
in the local store at once. Work item WI-10 (lever 9).

Without this hook a memory file reaches the store only on the next scan of
the store's indexer (design doc 0001, section 5.6). With it, a write inside
the memory folder starts the indexer command (``noblivion index``) a few
seconds after the write.

Two modes, one file:

``(no argument)``  the hook. Stdin is the Claude Code hook JSON. When
    ``tool_input.file_path`` is a ``*.md`` inside the memory folder it writes a
    "pending" stamp, starts one detached worker and returns. It never waits
    for the indexer, prints nothing, and exits 0 always.

    The Bash leg. For tool ``Bash`` there is no file path, and the command
    text is not parsed (a command can write in many ways). The hook reads the
    newest change time of the memory folder (the folder itself, its ``*.md``
    files and its sub-folders; one ``scandir``) and compares it with the
    stamp ``memory_sync.bash_seen``. When the folder is newer it starts the
    same sync as above and records the new time, so the next Bash call with
    no change does nothing. The change time is the larger of mtime and ctime:
    a ``cp -p`` or ``mv`` that keeps an old mtime still counts, and a delete
    counts through the folder. The Write leg records the time too, so a Write
    is not synced a second time by the next Bash call. A scan of the store
    does not touch the stamp: a change that only the store carried costs ONE
    extra (idempotent) sync at the next Bash call, never one per call. Not seen: an
    edit of a file inside a sub-folder (only the top level is read).

``--run``  the worker. It takes a file lock, waits until no write happened for
    the debounce time, runs the indexer command once, and records the stamp it
    covered. A worker that finds its stamp already covered exits without a run,
    so N parallel writes make one run. A write DURING a run starts a worker
    that waits for the lock and runs once more.

The indexer command. The indexer has no per-file mode: it scans the whole
folder and is idempotent (a hash skip when nothing changed). The command is
read at run time:

1. ``NOBLIVION_MEMORY_SYNC_CMD`` when set (the tests, or an override), else
2. the config key ``sync.index_command`` (a shell command line), else
3. the local indexer entry point ``<data dir>/venv/bin/noblivion index``,
   when that file exists. The installer (E8) creates it.

With none of them, the worker runs nothing and logs ``status=no_indexer``.

It runs through ``/bin/sh -c`` with a small environment: ``PATH``, ``SHELL``,
``HOME``, ``LOGNAME``, ``USER``, and the ``NOBLIVION_*``,
``CLAUDE_PLUGIN_DATA``, ``CLAUDE_PLUGIN_ROOT`` and ``XDG_*`` variables, so the
indexer finds the same data dir and the label rules of these hooks
(``noblivion.labels``). When another scan holds the index lock the command exits non-zero,
so a failed run is tried again (bounded).

Log: one line per trigger and one per run in ``memory_sync.log`` under
``NOBLIVION_MEMORY_SYNC_STATE_DIR`` (default ``<data dir>/cache``). A line
holds a time, an event, a file NAME, a return code and a duration. It never
holds the command, a URL, a host name or any file content.

Off switch: ``NOBLIVION_MEMORY_SYNC_OFF=1`` (both legs).
``NOBLIVION_MEMORY_SYNC_BASH_OFF=1`` turns off the Bash leg alone. Standard
library only.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - POSIX only
    _fcntl = None  # type: ignore[assignment]


def _hook_config():
    """``hook_config.py`` from this file's folder (data dir, config file)."""
    path = Path(__file__).resolve().parent / "hook_config.py"
    spec = importlib.util.spec_from_file_location("hook_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CFG = _hook_config()
LIVE_MEMORY_DIR = str(_CFG.default_memory_dir())
DEFAULT_STATE_DIR = str(_CFG.cache_dir())
INDEX_COMMAND_KEY = "sync.index_command"
INDEXER_ENTRY = Path("venv") / "bin" / "noblivion"  # under the data dir
INDEXER_ARGS = ("index",)
TOOLS = ("Write", "Edit", "MultiEdit")
BASH_TOOL = "Bash"

MEMORY_DIR_ENV = "NOBLIVION_MEMORY_DIR"
STATE_DIR_ENV = "NOBLIVION_MEMORY_SYNC_STATE_DIR"
CMD_ENV = "NOBLIVION_MEMORY_SYNC_CMD"
OFF_ENV = "NOBLIVION_MEMORY_SYNC_OFF"
BASH_OFF_ENV = "NOBLIVION_MEMORY_SYNC_BASH_OFF"
DEBOUNCE_ENV = "NOBLIVION_MEMORY_SYNC_DEBOUNCE_S"
TIMEOUT_ENV = "NOBLIVION_MEMORY_SYNC_TIMEOUT_S"
RETRY_WAIT_ENV = "NOBLIVION_MEMORY_SYNC_RETRY_WAIT_S"
LOCK_WAIT_ENV = "NOBLIVION_MEMORY_SYNC_LOCK_WAIT_S"

DEFAULT_DEBOUNCE_S = 3.0  # no run until the folder was quiet this long
DEBOUNCE_MAX_FACTOR = 10  # a stream of writes cannot hold a run back for ever
DEFAULT_TIMEOUT_S = 120.0  # one indexer run; a first full load can take a minute
DEFAULT_RETRY_WAIT_S = 5.0
ATTEMPTS = 3  # another scan can hold the index lock
DEFAULT_LOCK_WAIT_S = 400.0  # longer than ATTEMPTS runs of another worker

PENDING_NAME = "memory_sync.pending"
DONE_NAME = "memory_sync.done"
BASH_SEEN_NAME = "memory_sync.bash_seen"  # newest folder change a trigger covered
LOCK_NAME = "memory_sync.worker.lock"
LOG_NAME = "memory_sync.log"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# ── settings ────────────────────────────────────────────────────────────────


def _env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def memory_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    return Path(_env(environ).get(MEMORY_DIR_ENV) or LIVE_MEMORY_DIR).expanduser()


def state_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    return Path(_env(environ).get(STATE_DIR_ENV) or DEFAULT_STATE_DIR).expanduser()


def is_off(environ: Optional[Mapping[str, str]] = None) -> bool:
    return bool(_CFG.switch(OFF_ENV, False, _env(environ)))


def is_bash_off(environ: Optional[Mapping[str, str]] = None) -> bool:
    return bool(_CFG.switch(BASH_OFF_ENV, False, _env(environ)))


# ── log ─────────────────────────────────────────────────────────────────────


def log_line(state: Path, event: str, **fields: Any) -> None:
    """Append one line. Never raises. ``fields`` are short, safe values only."""
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    parts = [ts, f"event={event}"]
    for key, value in fields.items():
        parts.append(f"{key}={_SAFE_NAME.sub('_', str(value))[:120]}")
    try:
        _make_dirs(state)
        with open(state / LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(" ".join(parts) + "\n")
    except OSError:
        pass


# ── the hook ────────────────────────────────────────────────────────────────


def target_path(
    event: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> Optional[Path]:
    """The memory file this event wrote, or None when the event is not a write
    of a ``*.md`` inside the memory folder."""
    if event.get("tool_name") not in TOOLS:
        return None
    ti = event.get("tool_input") or {}
    raw = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(str(event.get("cwd") or os.getcwd())) / p
    try:
        p = p.resolve()
        folder = memory_dir(environ).resolve()
    except OSError:
        return None
    if p.suffix != ".md" or folder not in p.parents:
        return None
    return p


def _read_stamp(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_stamp(path: Path, stamp: int) -> bool:
    try:
        _make_dirs(path.parent)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(str(int(stamp)), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def newest_change(folder: Path) -> tuple[int, str, int]:
    """``(newest change time in ns, name of the newest entry, *.md count)`` of
    the memory folder. Reads the folder itself, its ``*.md`` files and its
    sub-folders (top level only). ``(0, "", 0)`` when it cannot be read."""
    try:
        st = os.stat(folder)
        newest, name, count = max(st.st_mtime_ns, st.st_ctime_ns), folder.name, 0
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.name.endswith(".md"):
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        count += 1
                    elif not entry.is_dir(follow_symlinks=False):
                        continue
                    est = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                t = max(est.st_mtime_ns, est.st_ctime_ns)
                if t >= newest:  # on a tie name the file, not the folder
                    newest, name = t, entry.name
    except OSError:
        return 0, "", 0
    return newest, name, count


def _trigger(
    state: Path,
    name: str,
    leg: str,
    environ: Optional[Mapping[str, str]],
    spawn: Callable[[Optional[Mapping[str, str]]], bool],
    now_ns: Callable[[], int],
) -> str:
    """Write the pending stamp, start one worker, log one line."""
    extra = {"leg": leg} if leg else {}
    if not _write_stamp(state / PENDING_NAME, now_ns()):
        log_line(state, "trigger", file=name, status="state_dir_error", **extra)
        return "state_dir_error"
    status = "spawned" if spawn(environ) else "spawn_error"
    log_line(state, "trigger", file=name, status=status, **extra)
    return status


def run_bash_leg(
    environ: Optional[Mapping[str, str]] = None,
    spawn: Callable[[Optional[Mapping[str, str]]], bool] = None,  # type: ignore[assignment]
    now_ns: Callable[[], int] = time.time_ns,
) -> str:
    """A Bash call ended. Sync when the memory folder changed since the last
    trigger. Returns the logged status, or ``""`` when nothing changed."""
    if is_bash_off(environ):
        return ""
    newest, name, _count = newest_change(memory_dir(environ))
    if not newest:
        return ""
    state = state_dir(environ)
    if newest <= _read_stamp(state / BASH_SEEN_NAME):
        return ""
    status = _trigger(state, name, "bash", environ, spawn or spawn_worker, now_ns)
    # Record the change only when a worker runs for it: a failed start is
    # tried again at the next Bash call.
    if status == "spawned" and not _write_stamp(state / BASH_SEEN_NAME, newest):
        log_line(state, "trigger", file=name, status="seen_stamp_error", leg="bash")
    return status


def spawn_worker(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Start the detached worker. It is its own session, holds no pipe of the
    hook, and so the tool call never waits for it."""
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, os.path.abspath(__file__), "--run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=dict(_env(environ)),
            cwd="/",
        )
        return True
    except OSError:
        return False


def run_hook(
    stdin_text: str,
    environ: Optional[Mapping[str, str]] = None,
    spawn: Callable[[Optional[Mapping[str, str]]], bool] = spawn_worker,
    now_ns: Callable[[], int] = time.time_ns,
) -> str:
    """Handle one PostToolUse event. Returns the status that was logged, or
    ``""`` when the event is not a memory write and no Bash call that left
    the memory folder changed."""
    if is_off(environ):
        return ""
    event = json.loads(stdin_text or "{}")
    if not isinstance(event, dict):
        return ""
    if event.get("tool_name") == BASH_TOOL:
        return run_bash_leg(environ, spawn, now_ns)
    path = target_path(event, environ)
    if path is None:
        return ""
    state = state_dir(environ)
    status = _trigger(state, path.name, "", environ, spawn, now_ns)
    if status == "spawned" and not is_bash_off(environ):
        # This write is covered: the next Bash call must not sync it again.
        newest = newest_change(memory_dir(environ))[0]
        if newest > _read_stamp(state / BASH_SEEN_NAME):
            _write_stamp(state / BASH_SEEN_NAME, newest)
    return status


# ── the indexer command ─────────────────────────────────────────────────────


def indexer_entry(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``<data dir>/venv/bin/noblivion index`` as a shell command line, or
    None when the entry point is not installed."""
    path = _CFG.data_dir(_env(environ)) / INDEXER_ENTRY
    if not (path.is_file() and os.access(path, os.X_OK)):
        return None
    return " ".join([shlex.quote(str(path))] + [shlex.quote(a) for a in INDEXER_ARGS])


def mirror_command(
    environ: Optional[Mapping[str, str]] = None,
    indexer: Callable[[Optional[Mapping[str, str]]], Optional[str]] = indexer_entry,
) -> Optional[str]:
    """The command one run executes: the env override, else the config key
    ``sync.index_command``, else the local indexer entry point. None: none of
    them is set or installed."""
    env = _env(environ)
    override = (env.get(CMD_ENV) or "").strip()
    if override:
        return override
    configured = _CFG.get(INDEX_COMMAND_KEY, None, env)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return indexer(env)


_PASS_PREFIXES = ("NOBLIVION_", "XDG_")
_PASS_KEYS = ("HOME", "LOGNAME", "USER", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT")


def cron_like_env(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A small, cron-like environment, plus the variables that name the data
    dir and the config, so the indexer reads the same store as the hooks."""
    env = _env(environ)
    out = {"PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"}
    for key, value in env.items():
        if value and (key in _PASS_KEYS or key.startswith(_PASS_PREFIXES)):
            out[key] = value
    return out


def run_command(cmd: str, timeout_s: float, environ: Optional[Mapping[str, str]] = None) -> int:
    """Run the indexer command. Its output is dropped (the command redirects its
    own). Returns the return code, 124 on a timeout, 127 when it cannot start."""
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd],  # noqa: S603
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=cron_like_env(environ),
            cwd="/",
            start_new_session=True,
        )
    except OSError:
        return 127
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 15)  # the command's own process group
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        return 124


# ── the worker ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def worker_lock(state: Path, wait_s: float, sleep: Callable[[float], None] = time.sleep):
    """Yield True while this process holds the worker lock, False when it could
    not get it within ``wait_s``."""
    if _fcntl is None:  # pragma: no cover - POSIX only
        yield True
        return
    _make_dirs(state)
    fh = open(state / LOCK_NAME, "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + wait_s
        got = False
        while True:
            try:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                sleep(0.2)
        yield got
    finally:
        fh.close()  # closing the file drops the lock


def run_worker(
    environ: Optional[Mapping[str, str]] = None,
    runner: Callable[[str, float, Optional[Mapping[str, str]]], int] = run_command,
    indexer: Callable[[Optional[Mapping[str, str]]], Optional[str]] = indexer_entry,
    sleep: Callable[[float], None] = time.sleep,
    now_ns: Callable[[], int] = time.time_ns,
) -> str:
    """One worker. Returns the status that was logged."""
    env = _env(environ)
    state = state_dir(env)
    debounce = _float(env, DEBOUNCE_ENV, DEFAULT_DEBOUNCE_S)
    timeout_s = _float(env, TIMEOUT_ENV, DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S
    retry_wait = _float(env, RETRY_WAIT_ENV, DEFAULT_RETRY_WAIT_S)
    lock_wait = _float(env, LOCK_WAIT_ENV, DEFAULT_LOCK_WAIT_S)
    with worker_lock(state, lock_wait, sleep) as got:
        if not got:
            log_line(state, "run", status="lock_timeout")
            return "lock_timeout"
        # Debounce: wait until the newest write is `debounce` old, bounded.
        deadline_ns = now_ns() + int(debounce * DEBOUNCE_MAX_FACTOR * 1e9)
        while True:
            pending = _read_stamp(state / PENDING_NAME)
            age_s = (now_ns() - pending) / 1e9
            if age_s >= debounce or now_ns() >= deadline_ns:
                break
            sleep(min(max(debounce - age_s, 0.05), debounce or 0.05))
        if pending <= _read_stamp(state / DONE_NAME):
            log_line(state, "run", status="covered")
            return "covered"
        cmd = mirror_command(env, indexer)
        if not cmd:
            # No override, no config, and the indexer entry point is not
            # installed: nothing to run. The store's own scan picks the write up.
            log_line(state, "run", status="no_indexer")
            return "no_indexer"
        rc = 1
        t0 = time.monotonic()
        attempt = 0
        for attempt in range(1, ATTEMPTS + 1):
            rc = runner(cmd, timeout_s, env)
            if rc == 0:
                break
            if attempt < ATTEMPTS:
                sleep(retry_wait)
        ms = int((time.monotonic() - t0) * 1000)
        wait_ms = max(0, int((now_ns() - pending) / 1e6))
        if rc == 0:
            _write_stamp(state / DONE_NAME, pending)
        status = "ok" if rc == 0 else "failed"
        log_line(
            state,
            "run",
            status=status,
            rc=rc,
            attempts=attempt,
            run_ms=ms,
            write_to_done_ms=wait_ms,
        )
        return status


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args and args[0] == "--run":
            run_worker()
        else:
            run_hook(sys.stdin.read())
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass
    return 0


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _CFG.make_dirs(path)


if __name__ == "__main__":
    sys.exit(main())
