#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionEnd hook: start the transcript miner (design doc 0001, section 11.4).

When a session ends, its transcript is complete. This hook starts
``noblivion mine`` detached and returns at once. It never waits for the
miner, prints nothing and exits 0 always.

Rules:

- Off when ``NOBLIVION_MINER`` is ``0``/``false``/``off``/``no``, or when the
  config key ``miner.enabled`` is false.
- At most one start per ``MIN_INTERVAL_S`` (10 minutes): the stamp file
  ``mine.stamp`` in ``<data dir>/cache`` holds the time of the last start.
- The command: ``NOBLIVION_MINE_CMD`` when set (a shell command line, for
  tests or an override), else ``<data dir>/venv/bin/noblivion mine`` when that
  file exists (the installer creates it). With neither, nothing runs.
- The miner itself takes ``mine.lock``, so two starts never mine at once.
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Mapping, Optional

MIN_INTERVAL_S = 600.0
STAMP_NAME = "mine.stamp"
CMD_ENV = "NOBLIVION_MINE_CMD"
ENABLED_ENV = "NOBLIVION_MINER"
ENABLED_KEY = "miner.enabled"
MINER_ENTRY = Path("venv") / "bin" / "noblivion"  # under the data dir
OFF_WORDS = ("0", "false", "off", "no")


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


def is_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    value = _CFG.setting(ENABLED_ENV, ENABLED_KEY, True, _env(environ))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in OFF_WORDS


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
        stamp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
) -> str:
    """Handle one SessionEnd event. Returns a status word (for the tests)."""
    if not is_enabled(environ):
        return "off"
    argv = command(environ)
    if argv is None:
        return "no_miner"
    stamp = _CFG.cache_dir(_env(environ)) / STAMP_NAME
    moment = now()
    if not _due(stamp, moment):
        return "throttled"
    if not _write_stamp(stamp, moment):
        return "no_stamp"
    return "started" if starter(argv, environ) else "spawn_failed"


def main() -> int:
    try:
        sys.stdin.read()  # the hook JSON; nothing in it is needed
    except (OSError, ValueError):
        pass
    try:
        run_hook()
    except Exception:  # noqa: BLE001 - a hook never fails the session end
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
