#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find, prove and start the local store from a stdlib hook.

Design doc 0001, sections 3.2 and 3.3. The hooks do not import the
``noblivion`` package (it lives in the store venv), so this module copies the
client side of ``noblivion.launcher``:

1. ``store.json`` in the data dir names the port. No file: no store. The
   hooks never fall back to the configured port.
2. The listener proof: ``GET /health?nonce=<32 hex>`` with no token. The
   answer must hold ``proof = hex(HMAC-SHA256(token, "noblivion-health:" +
   nonce))`` for the token in the ``token`` file, compared in constant time
   (``proof_ok``). Only a proven listener gets the token, as
   ``Authorization: Bearer <token>``.
3. A store that is down is started by ``<venv>/bin/noblivion ensure-running``
   (``request_start``): a detached child, never waited for. The launcher
   checks the proof and the version, and runs the atomic start check
   (``flock`` on ``spawn.stamp``, design doc section 3.2) itself. The hook
   only reads the stamp's age; it never touches or locks it.

The HTTP request itself belongs to the caller (``recall_hook.http_get_json``),
so this module opens no socket.

Run as a script, it is the ``SessionStart`` hook: it starts the launcher and
exits 0 at once, whatever happens. Under the plugin (``CLAUDE_PLUGIN_ROOT``
is set) it also prints one line when the store venv is missing or was built
for another plugin version (``install_notice``, design doc section 13.2).

Settings (design doc section 12.3):

  NOBLIVION_BIN / ``store.bin``   the ``noblivion`` entry point; default
                                  ``<data dir>/venv/bin/noblivion``
  NOBLIVION_STORE_AUTOSTART       ``0``, ``off``, ``false`` or ``no``: never
                                  start the store from a hook

Standard library only. Hooks load this file by path, as a sibling.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

STORE_JSON_FILE = "store.json"
TOKEN_FILE = "token"
SPAWN_STAMP_FILE = "spawn.stamp"
PROOF_PREFIX = "noblivion-health:"
PROBE_TIMEOUT_S = 0.3
PROOF_TTL_S = 30.0  # a long-lived caller (the MCP server) proves again after this
SPAWN_STAMP_MAX_AGE_S = 30.0
BIN_ENV = "NOBLIVION_BIN"
BIN_KEY = "store.bin"
AUTOSTART_ENV = "NOBLIVION_STORE_AUTOSTART"
VENV_BIN = Path("venv") / "bin" / "noblivion"  # under the data dir
INSTALL_STAMP = Path("venv") / "noblivion-install.json"  # written by install.sh
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
TOKEN_RE = re.compile(r"[0-9a-f]{64}")
LOOPBACK = "127.0.0.1"

# Why a store call did not happen. Each one is a log reason.
DOWN = "store_down"  # no store.json, or no answer to the proof request
NO_TOKEN = "no_token"  # store.json but no valid token file
FOREIGN = "foreign_listener"  # an answer without the right proof

_PROVEN: Dict[Tuple[str, int, str], float] = {}


class StoreUnavailable(Exception):
    """The store is down or unproven. ``reason`` is one of the constants above."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_CFG: Any = None


def _hook_config():
    """``hook_config.py`` from this file's folder, loaded once."""
    global _CFG
    if _CFG is None:
        path = Path(__file__).resolve().parent / "hook_config.py"
        spec = importlib.util.spec_from_file_location("hook_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CFG = mod
    return _CFG


def data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return _hook_config().data_dir(env)


def health_proof(token: str, nonce: str) -> str:
    """The listener proof of section 3.3."""
    message = (PROOF_PREFIX + nonce).encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def proof_ok(answer: Any, token: str, nonce: str) -> bool:
    """True when ``answer`` (the decoded health JSON) proves ``token``."""
    if not isinstance(answer, dict):
        return False
    proof = answer.get("proof")
    if not isinstance(proof, str):
        return False
    expected = health_proof(token, nonce).encode("ascii")
    return hmac.compare_digest(proof.encode("ascii", "replace"), expected)


def read_token(folder: Path) -> Optional[str]:
    """The token from the ``token`` file, or None when missing or malformed."""
    try:
        text = (folder / TOKEN_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if TOKEN_RE.fullmatch(text) else None


def read_store_json(folder: Path) -> Optional[Dict[str, Any]]:
    """``{pid, port, version, started_at}``, or None when missing or malformed."""
    try:
        info = json.loads((folder / STORE_JSON_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    port = info.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    return info


def locate(env: Optional[Mapping[str, str]] = None) -> Tuple[int, str]:
    """``(port, token)`` from the data dir, with no network call. Raises
    StoreUnavailable(DOWN) without ``store.json`` and (NO_TOKEN) without a
    valid token."""
    folder = data_dir(env)
    info = read_store_json(folder)
    if info is None:
        raise StoreUnavailable(DOWN)
    token = read_token(folder)
    if token is None:
        raise StoreUnavailable(NO_TOKEN)
    return int(info["port"]), token


def base_url(port: int) -> str:
    return "http://%s:%d" % (LOOPBACK, port)


def health_url(port: int, nonce: str) -> str:
    return "%s/health?nonce=%s" % (base_url(port), nonce)


def connect(
    env: Optional[Mapping[str, str]],
    get_json: Callable[[str, float], Any],
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[str, str]:
    """``(base_url, token)`` of a proven store.

    ``get_json(url, timeout_s)`` sends a GET with NO token and returns the
    decoded JSON, or raises. A proof is kept for PROOF_TTL_S per (data dir,
    port, token), so one process proves once. Raises StoreUnavailable.
    """
    port, token = locate(env)
    key = (str(data_dir(env)), port, token)
    seen = _PROVEN.get(key)
    if seen is not None and clock() - seen < PROOF_TTL_S:
        return base_url(port), token
    nonce = new_nonce()
    try:
        answer = get_json(health_url(port, nonce), timeout_s)
    except Exception:  # noqa: BLE001 - no answer is "down", whatever the cause
        raise StoreUnavailable(DOWN) from None
    if not proof_ok(answer, token, nonce):
        raise StoreUnavailable(FOREIGN)
    _PROVEN[key] = clock()
    return base_url(port), token


def forget(env: Optional[Mapping[str, str]] = None) -> None:
    """Drop the kept proofs of this data dir (after a failed request)."""
    folder = str(data_dir(env))
    for key in [k for k in _PROVEN if k[0] == folder]:
        _PROVEN.pop(key, None)


def _off(value: Any) -> bool:
    return str(value or "").strip().lower() in ("0", "off", "false", "no")


def launcher_bin(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The ``noblivion`` entry point, or None when it is not an executable file."""
    cfg = _hook_config()
    raw = cfg.setting(BIN_ENV, BIN_KEY, None, env)
    path = Path(os.path.expanduser(str(raw))) if raw else data_dir(env) / VENV_BIN
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def request_start(
    env: Optional[Mapping[str, str]] = None,
    *,
    check_stamp: bool = True,
    clock: Callable[[], float] = time.time,
    popen: Callable[..., Any] = subprocess.Popen,
) -> str:
    """Ask the launcher to start the store. Never waits and never raises.

    Returns ``off`` (autostart off), ``starting`` (``spawn.stamp`` is younger
    than 30 s, so a start is under way), ``no_launcher`` (no entry point),
    ``spawned`` or ``error``.
    """
    try:
        e = os.environ if env is None else env
        if _off(e.get(AUTOSTART_ENV)):
            return "off"
        if check_stamp:
            stamp = data_dir(env) / SPAWN_STAMP_FILE
            try:
                if clock() - stamp.stat().st_mtime < SPAWN_STAMP_MAX_AGE_S:
                    return "starting"
            except OSError:
                pass
        exe = launcher_bin(env)
        if exe is None:
            return "no_launcher"
        child = popen(
            [exe, "ensure-running"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=dict(e),
        )
        child.returncode = 0  # detached: never waited for, no ResourceWarning
        return "spawned"
    except Exception:  # noqa: BLE001 - a hook never fails on a start
        return "error"


def plugin_version(root: Optional[Path] = None) -> Optional[str]:
    """The ``version`` of ``.claude-plugin/plugin.json`` in the plugin root
    (the parent of this file's folder), or None."""
    base = PLUGIN_ROOT if root is None else root
    try:
        with (base / ".claude-plugin" / "plugin.json").open(encoding="utf-8") as fh:
            version = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def installed_version(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The plugin version ``install.sh`` built the venv for, or None."""
    try:
        with (data_dir(env) / INSTALL_STAMP).open(encoding="utf-8") as fh:
            version = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def install_notice(
    started: str, env: Optional[Mapping[str, str]] = None, root: Optional[Path] = None
) -> Optional[str]:
    """One line for the session when the store venv is missing (``started``
    is ``no_launcher``) or was built for another plugin version. None when
    all is well or the store is not started from hooks."""
    base = PLUGIN_ROOT if root is None else root
    e = os.environ if env is None else env
    plugin_data = (e.get(PLUGIN_DATA_ENV) or "").strip()
    script = 'bash "%s"' % (base / "scripts" / "install.sh")
    if plugin_data:  # a terminal has no CLAUDE_PLUGIN_DATA: name the folder
        script = '%s="%s" %s' % (PLUGIN_DATA_ENV, plugin_data, script)
    if started == "no_launcher":
        return (
            "NOBLIVION: the memory store is not installed, so memory recall is off. "
            "Tell the user to run: %s" % script
        )
    if started == "off":
        return None
    want, have = plugin_version(base), installed_version(env)
    if want and have and want != have:
        return (
            "NOBLIVION: the memory store was built for plugin version %s, the plugin is %s. "
            "Tell the user to run: %s" % (have, want, script)
        )
    return None


def main(stdin=None, environ: Optional[Mapping[str, str]] = None) -> int:
    """The SessionStart hook: start the launcher, exit 0. It prints nothing,
    except the ``install_notice`` line when it runs under the plugin."""
    try:
        (stdin or sys.stdin).read()
    except Exception:  # noqa: BLE001
        pass
    started = request_start(environ, check_stamp=False)
    try:
        env = os.environ if environ is None else environ
        if (env.get(PLUGIN_ROOT_ENV) or "").strip():
            notice = install_notice(started, env)
            if notice:
                print(notice)
    except Exception:  # noqa: BLE001 - a hook never fails on a notice
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
