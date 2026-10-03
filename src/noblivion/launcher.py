# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find, prove and start the store (design doc 0001, sections 3.2 and 3.3).

This module imports only the stdlib and ``noblivion.config``, so
``noblivion ensure-running`` returns fast. The hooks are stdlib scripts that
do not import the package (section 5.2); E4 copies the same steps:

1. Read ``store.json`` for the port. No file: no store.
2. Prove the listener: ``GET /health?nonce=<32 hex>`` with no token; the
   answer must hold ``proof = hex(HMAC-SHA256(token, "noblivion-health:" +
   nonce))``. Compare in constant time. No proof: the store is down.
3. A store with another version gets ``SIGTERM``; a new one is started.
4. ``spawn.stamp`` younger than 30 s: another caller starts the store.
5. Else touch the stamp and start ``python -m noblivion.store`` detached.
   Steps 4 and 5 check and touch the stamp under ``flock`` on the stamp, so
   callers at the same moment start one store, not one each.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from noblivion import __version__, config

PROOF_PREFIX = "noblivion-health:"
PROBE_TIMEOUT_S = 0.3
SPAWN_STAMP_MAX_AGE_S = 30.0
RESTART_LOCK_WAIT_S = 10.0
TOKEN_RE = re.compile(r"[0-9a-f]{64}")
NONCE_RE = re.compile(r"[0-9a-fA-F]{32,128}")

STATE_RUNNING = "running"  # a proven store with this version answers
STATE_STARTING = "starting"  # another caller started one less than 30 s ago
STATE_STARTED = "started"  # this call started one


def health_proof(token: str, nonce: str) -> str:
    """The listener proof of section 3.3."""
    message = (PROOF_PREFIX + nonce).encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def read_token(data_dir: Path) -> str | None:
    """The token from the ``token`` file, or None when it is missing or malformed."""
    try:
        text = (data_dir / config.TOKEN_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if TOKEN_RE.fullmatch(text) else None


def read_store_json(data_dir: Path) -> dict | None:
    """``{pid, port, version, started_at}`` or None when missing or malformed."""
    try:
        info = json.loads((data_dir / config.STORE_JSON_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    port, pid = info.get("port"), info.get("pid")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    return info


def probe(port: int, token: str, timeout: float = PROBE_TIMEOUT_S) -> dict | None:
    """The short health answer when the listener proves it holds ``token``, else None.

    Sends no token and no text; a foreign listener learns only a nonce.
    """
    nonce = secrets.token_hex(16)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", f"/health?nonce={nonce}")
        response = conn.getresponse()
        raw = response.read(65536)
        if response.status != 200:
            return None
        answer = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if not isinstance(answer, dict):
        return None
    proof = answer.get("proof")
    if not isinstance(proof, str):
        return None
    expected = health_proof(token, nonce).encode("ascii")
    if not hmac.compare_digest(proof.encode("ascii", "replace"), expected):
        return None
    return answer


def spawn_store(data_dir: Path, *, lock_wait_s: float = 0.0) -> int:
    """Start ``python -m noblivion.store`` detached. Returns its pid. Does not wait."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = data_dir / config.STORE_LOG_FILE
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "noblivion.store"]
    if lock_wait_s > 0:
        argv += ["--lock-wait", str(lock_wait_s)]
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            cwd=str(data_dir),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(fd)
    child.returncode = 0  # detached: never waited for, no ResourceWarning
    return child.pid


def ensure_running(
    env: Mapping[str, str] | None = None, *, clock=time.time, spawn=spawn_store
) -> str:
    """Steps 1-5 of section 3.2. Returns ``running``, ``starting`` or ``started``."""
    data_dir = config.data_dir(env)
    lock_wait = 0.0
    info = read_store_json(data_dir)
    token = read_token(data_dir)
    if info is not None and token is not None:
        answer = probe(info["port"], token)
        if answer is not None:
            if answer.get("version") == __version__:
                return STATE_RUNNING
            # A proven store of another version may run an old redactor: replace it.
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except OSError:
                pass
            lock_wait = RESTART_LOCK_WAIT_S
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not claim_spawn(data_dir / config.SPAWN_STAMP_FILE, clock, force=bool(lock_wait)):
        return STATE_STARTING
    spawn(data_dir, lock_wait_s=lock_wait)
    return STATE_STARTED


def claim_spawn(stamp: Path, clock=time.time, *, force: bool = False) -> bool:
    """True when this caller starts the store (steps 4 and 5 of section 3.2).

    The check of the stamp age and the touch run under ``flock`` on the
    stamp, so of several callers at one moment exactly one wins. A caller
    that makes the stamp file wins; a caller that finds it checks its age.
    ``force`` (a store of another version was stopped) skips the age check.
    """
    for _ in range(3):
        try:
            fd = os.open(stamp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                fd = os.open(stamp, os.O_RDWR)
            except FileNotFoundError:
                continue  # removed between the two opens: try again
            created = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not created and not force:
                if clock() - os.fstat(fd).st_mtime < SPAWN_STAMP_MAX_AGE_S:
                    return False
            os.utime(fd)
            return True
        finally:
            os.close(fd)  # closing the fd releases the flock
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="noblivion ensure-running",
        description="Start the store in the background unless a proven store runs.",
    )
    parser.add_argument("--json", action="store_true", help="print the state as JSON")
    args = parser.parse_args(argv)
    state = ensure_running()
    print(json.dumps({"state": state}) if args.json else f"noblivion: store {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
