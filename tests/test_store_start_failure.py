# SPDX-License-Identifier: AGPL-3.0-or-later
"""A store that cannot start is reported, not silent (NOBLIVION-25).

The reproducer of the ticket: another program holds the store port. The store
logs the reason, writes ``store.error``; ``noblivion ensure-running`` waits,
prints ``failed`` with the reason and exits 1; the SessionStart hook prints
one line for the user.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import config, launcher, store
from test_store import store_env

sc = load_hook("store_client", "store_client_start_failure")


def _held_port():
    """A listening socket on 127.0.0.1; the caller closes it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def _exe() -> str:
    exe = shutil.which("noblivion", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("no noblivion entry point next to the test python")
    return exe


def test_default_port_is_any_free_port():
    """Port 8894 was taken on a test host (NOBLIVION-25). The hooks read the
    real port from store.json, so the default is 0: the OS picks a free port."""
    assert config.DEFAULT_PORT == 0
    assert config.load_store_settings({"NOBLIVION_CONFIG": "/nonexistent"}).port == 0


def test_bind_error_text_names_the_reason():
    exc = OSError(98, "Address already in use")
    exc.errno = __import__("errno").EADDRINUSE
    text = store.bind_error_text(8894, exc)
    assert text.startswith("bind failed on port 8894: Address already in use")
    assert '"port"' in text and "config.json" in text


def test_ensure_running_reports_a_taken_port(tmp_path):
    with _held_port() as held:
        port = held.getsockname()[1]
        env = dict(store_env(tmp_path), NOBLIVION_PORT=str(port))
        proc = subprocess.run(
            [sys.executable, "-m", "noblivion", "ensure-running"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    data = Path(env["NOBLIVION_DATA_DIR"])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert f"noblivion: store failed: bind failed on port {port}: " in proc.stderr
    assert "in use" in proc.stderr
    log = (data / "logs" / "store.log").read_text(encoding="utf-8")
    assert f"bind failed on port {port}: " in log and "OSError" not in log
    error = data / "store.error"
    assert stat.S_IMODE(error.stat().st_mode) == 0o600
    assert json.loads(error.read_text())["reason"].startswith(f"bind failed on port {port}")
    assert not (data / "store.json").exists()


def test_ensure_running_json_reports_the_reason(tmp_path):
    with _held_port() as held:
        env = dict(store_env(tmp_path), NOBLIVION_PORT=str(held.getsockname()[1]))
        proc = subprocess.run(
            [sys.executable, "-m", "noblivion", "ensure-running", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 1
    answer = json.loads(proc.stdout)
    assert answer["state"] == "failed" and "bind failed" in answer["reason"]


def test_a_good_start_removes_an_old_store_error(tmp_path):
    env = store_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    launcher.write_start_error(data, "old failure")
    proc = subprocess.run(
        [sys.executable, "-m", "noblivion", "ensure-running", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout) == {"state": "started"}
        assert not (data / "store.error").exists()
    finally:
        info = launcher.read_store_json(data)
        if info is not None:
            os.kill(info["pid"], 15)


def test_wait_until_up_fails_on_a_child_exit_without_store_error(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "data").mkdir(exist_ok=True)
    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    state, reason = launcher.wait_until_up(env, since=0.0, wait_s=20, pid=child.pid)
    assert state == "failed" and "exited with code 3" in reason
    assert launcher.read_start_error(tmp_path / "data") == reason


def test_wait_until_up_times_out_with_a_reason(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "data").mkdir(exist_ok=True)
    ticks = iter(range(100))
    state, reason = launcher.wait_until_up(
        env, since=0.0, wait_s=3, clock=lambda: next(ticks), sleep=lambda _s: None
    )
    assert state == "failed" and reason.startswith("no answer from the store after 3 s")


class _Child:
    def __init__(self, codes):
        self.codes = list(codes)
        self.returncode = None

    def poll(self):
        return self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]


def test_session_start_hook_prints_one_line_when_the_store_cannot_start(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    exe = tmp_path / "noblivion"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o700)
    env = {"NOBLIVION_DATA_DIR": str(data), "NOBLIVION_BIN": str(exe)}
    launcher.write_start_error(data, "bind failed on port 8894: Address already in use")
    assert sc.main(stdin=io.StringIO("{}"), environ=env) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    doc = json.loads(out[0])
    line = doc["systemMessage"]
    assert line.startswith("NOBLIVION: the memory store could not start")
    assert "bind failed on port 8894: Address already in use" in line
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert line in doc["hookSpecificOutput"]["additionalContext"]


def test_session_start_hook_ignores_an_old_store_error(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    launcher.write_start_error(data, "old")
    os.utime(data / "store.error", (1, 1))
    assert sc.read_start_error(data, since=1000.0) is None
    reason = sc.wait_for_launcher(
        _Child([None, 1]), {"NOBLIVION_DATA_DIR": str(data)}, since=1000.0, sleep=lambda _s: None
    )
    assert reason == "the launcher exited with code 1"


def test_session_start_hook_is_silent_on_a_good_start_and_on_a_slow_one(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path)}
    assert sc.wait_for_launcher(_Child([0]), env, since=0.0) is None
    slow = _Child([None])
    ticks = iter(range(100))
    assert (
        sc.wait_for_launcher(
            slow, env, since=0.0, wait_s=2, clock=lambda: next(ticks), sleep=lambda _s: None
        )
        is None
    )
    assert slow.returncode == 0  # left running, detached


def test_session_start_hook_reports_a_real_taken_port(tmp_path, capsys):
    """End to end: the real launcher, a held port, the hook's one line."""
    with _held_port() as held:
        port = held.getsockname()[1]
        env = dict(store_env(tmp_path), NOBLIVION_PORT=str(port), NOBLIVION_BIN=_exe())
        env["NOBLIVION_STORE_AUTOSTART"] = "1"
        assert sc.main(stdin=io.StringIO("{}"), environ=env, wait_s=30) == 0
    doc = json.loads(capsys.readouterr().out)
    assert f"bind failed on port {port}" in doc["systemMessage"]
