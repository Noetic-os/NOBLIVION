#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionEnd hook: start the transcript miner (design doc 0001, section 11.4).

When a session ends, its transcript is complete. This hook starts
``noblivion mine`` detached and returns at once. It never waits for the
miner, prints nothing and exits 0 always.

Rules:

- Off by default (NOBLIVION-53). On when ``NOBLIVION_MINER`` is
  ``1``/``true``/``on``/``yes``, or when the config key ``miner.enabled`` is
  true. The env var wins.
- A learning client (``<data dir>/learning-client.json``) does not turn the
  miner off by being there (NOBLIVION-106). The hook reads it and applies the
  learner's opt-out rule, ``allowed`` of ``codex/capture.py``: the miner stays
  off when ``learning.contribute`` is not true, when ``learning.paused`` is
  set, when the ending session opted out, or when the file is unreadable.
  ``noblivion mine`` applies the same rule to each transcript.
- One line per decision goes to ``<data dir>/cache/mine-hook.log``:
  ``on``, ``off-by-env``, ``off-by-opt-out`` or ``off-by-bad-config``.
- At most one start per ``MIN_INTERVAL_S`` (10 minutes): the stamp file
  ``mine.stamp`` in ``<data dir>/cache`` holds the time of the last start.
  ``NOBLIVION_MINER_STAMP_DIR`` can keep a client-specific stamp elsewhere.
- The command: ``NOBLIVION_MINE_CMD`` when set (a shell command line, for
  tests or an override), else ``<data dir>/venv/bin/noblivion mine`` when that
  file exists (the installer creates it). With neither, nothing runs.
- The miner itself takes ``mine.lock``, so two starts never mine at once.
"""

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple

MIN_INTERVAL_S = 600.0
STAMP_NAME = "mine.stamp"
CMD_ENV = "NOBLIVION_MINE_CMD"
ENABLED_ENV = "NOBLIVION_MINER"
STAMP_DIR_ENV = "NOBLIVION_MINER_STAMP_DIR"
ENABLED_KEY = "miner.enabled"
MINER_ENTRY = Path("venv") / "bin" / "noblivion"  # under the data dir
LEARNING_CLIENT = "learning-client.json"
LOG_NAME = "mine-hook.log"
LOG_MAX_BYTES = 1_000_000  # then the log moves to .1
# The decisions, as logged.
ON = "on"
OFF_ENV = "off-by-env"
OFF_OPT_OUT = "off-by-opt-out"
OFF_BAD_CONFIG = "off-by-bad-config"


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


def _env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _capture_allowed():
    """``allowed`` of ``codex/capture.py``: the one opt-out rule of the
    learner. Raises when the file is missing or does not load."""
    path = Path(__file__).resolve().parent.parent / "codex" / "capture.py"
    spec = importlib.util.spec_from_file_location("_mine_hook_capture", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return mod.allowed


def decide(
    environ: Optional[Mapping[str, str]] = None, session_id: str = ""
) -> Tuple[bool, str, str]:
    """``(enabled, decision, detail)``. The switch comes first. Then, when a
    learning client config exists, its opt-outs apply: the global ones, and
    the ones of ``session_id`` when it is given. A config that cannot be read
    is off, never permissive."""
    env = _env(environ)
    if not _CFG.switch(ENABLED_ENV, False, env, ENABLED_KEY):
        return False, OFF_ENV, ""
    path = _CFG.data_dir(env) / LEARNING_CLIENT
    if not path.exists():
        return True, ON, ""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
        allowed = _capture_allowed()
    except Exception as exc:  # noqa: BLE001 - unreadable config: off
        return False, OFF_BAD_CONFIG, type(exc).__name__
    try:
        if not allowed(cfg, ""):
            return False, OFF_OPT_OUT, "learning"
        if session_id and not allowed(cfg, session_id):
            return False, OFF_OPT_OUT, "session"
    except Exception as exc:  # noqa: BLE001 - a malformed section is an opt-out
        return False, OFF_BAD_CONFIG, type(exc).__name__
    return True, ON, ""


def is_enabled(environ: Optional[Mapping[str, str]] = None, session_id: str = "") -> bool:
    return decide(environ, session_id)[0]


def log_decision(
    decision: str, detail: str = "", environ: Optional[Mapping[str, str]] = None
) -> None:
    """Append one line to ``mine-hook.log`` so a user can see why nothing was
    mined. The line holds no session id and no path. Never raises."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    line = f"{stamp} decision={decision}" + (f" detail={detail}" if detail else "") + "\n"
    try:
        folder = _CFG.cache_dir(_env(environ))
        _make_dirs(folder)
        path = folder / LOG_NAME
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def command(environ: Optional[Mapping[str, str]] = None) -> Optional[List[str]]:
    env = _env(environ)
    raw = str(env.get(CMD_ENV) or "").strip()
    if raw:
        return ["/bin/sh", "-c", raw]
    entry = _CFG.data_dir(env) / MINER_ENTRY
    if entry.is_file() and os.access(entry, os.X_OK):
        return [str(entry), "mine"]
    return None


def _due(stamp: Path, now: float) -> bool:
    try:
        last = float(stamp.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True
    return now - last >= MIN_INTERVAL_S or now < last


def _write_stamp(stamp: Path, now: float) -> bool:
    try:
        _make_dirs(stamp.parent)
        tmp = stamp.with_name(stamp.name + ".tmp")
        tmp.write_text(f"{now:.3f}\n", encoding="utf-8")
        os.replace(tmp, stamp)
        return True
    except OSError:
        return False


def spawn(argv: List[str], environ: Optional[Mapping[str, str]] = None) -> bool:
    """Start the miner in its own session, with no pipe of the hook."""
    try:
        subprocess.Popen(  # noqa: S603
            argv,
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
    environ: Optional[Mapping[str, str]] = None,
    starter: Callable[[List[str], Optional[Mapping[str, str]]], bool] = spawn,
    now: Callable[[], float] = time.time,
    session_id: str = "",
) -> str:
    """Handle one SessionEnd event. Returns a status word (for the tests)."""
    enabled, decision, detail = decide(environ, session_id)
    log_decision(decision, detail, environ)
    if not enabled:
        return "off"
    argv = command(environ)
    if argv is None:
        return "no_miner"
    env = _env(environ)
    override = str(env.get(STAMP_DIR_ENV) or "").strip()
    stamp_dir = Path(override).expanduser() if override else _CFG.cache_dir(env)
    stamp = stamp_dir / STAMP_NAME
    moment = now()
    if not _due(stamp, moment):
        return "throttled"
    if not _write_stamp(stamp, moment):
        return "no_stamp"
    return "started" if starter(argv, environ) else "spawn_failed"


def main() -> int:
    session_id = ""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        value = event.get("session_id") if isinstance(event, dict) else None
        session_id = value if isinstance(value, str) else ""
    except (OSError, ValueError):
        pass
    try:
        run_hook(session_id=session_id)
    except Exception:  # noqa: BLE001 - a hook never fails the session end
        pass
    return 0


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _CFG.make_dirs(path)


if __name__ == "__main__":
    sys.exit(main())
