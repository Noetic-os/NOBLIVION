# SPDX-License-Identifier: AGPL-3.0-or-later
"""The zero-token and read-only guards of the long-run test."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import sandbox

PROGRAM = sorted((Path(sandbox.__file__).parent).glob("*.py"))


def test_environment_names_are_refused_by_name_only():
    env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "x",
        "FOO_TOKEN": "y",
        "https_proxy": "z",
        "OPENAI_BASE_URL": "u",
        "HOME": "/h",
        "SAFE": "1",
    }
    assert sandbox.check_environment(env) == [
        "ANTHROPIC_API_KEY",
        "FOO_TOKEN",
        "OPENAI_BASE_URL",
        "https_proxy",
    ]
    assert sandbox.check_environment({"PATH": "/usr/bin", "LANG": "C"}) == []


def test_clean_environment_keeps_four_names():
    env = sandbox.clean_environment(
        {
            "PATH": "p",
            "HOME": "h",
            "LANG": "l",
            "PYTHONPATH": "x",
            "ANTHROPIC_API_KEY": "k",
            "OTHER": "o",
        },
        EXTRA="1",
    )
    assert set(env) == {"PATH", "HOME", "LANG", "PYTHONPATH", "EXTRA"}


def test_path_with_a_claude_binary_is_found(tmp_path):
    (tmp_path / "claude").write_text("#!/bin/sh\n")
    assert sandbox.path_has_claude(f"{tmp_path}/none:{tmp_path}") == [str(tmp_path)]
    assert sandbox.path_has_claude(f"{tmp_path}/none") == []


def test_unit_path_folder_holds_no_claude(tmp_path):
    """The system PATH may hold a claude binary, so the unit gets its own folder."""
    folder = sandbox.make_bin(tmp_path / "bin")
    assert (folder / "git").is_symlink()
    assert sandbox.path_has_claude(str(folder)) == []


def test_program_never_starts_claude():
    """The scan of the plan: no spawn call in the program holds the word claude."""
    assert sandbox.scan_for_claude(PROGRAM) == []


def test_scan_finds_a_spawn_of_claude(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\nsubprocess.run(['claude', '-p', 'hi'])\n"
        "import os\nos.system('/usr/local/bin/claude --version')\n"
    )
    ok = tmp_path / "ok.py"
    ok.write_text("import subprocess\nsubprocess.run(['git', 'status'])\nx = 'claude'\n")
    assert len(sandbox.scan_for_claude([bad])) == 2
    assert sandbox.scan_for_claude([ok]) == []


def test_socket_guard_refuses_public_and_counts():
    with sandbox.SocketGuard() as guard:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("192.0.2.1", 80), timeout=1)
        with pytest.raises(ConnectionRefusedError):
            socket.getaddrinfo("example.com", 80)
    assert guard.refused == 2 and guard.targets[0] == "192.0.2.1"
    # uninstalled: the original behaviour is back (name lookup not refused by the guard)
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_socket_guard_allows_loopback():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with sandbox.SocketGuard() as guard:
        client = socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=2)
        client.close()
    server.close()
    assert guard.refused == 0 and guard.allowed >= 1


def test_open_guard_refuses_credentials_and_live_writes(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "token").write_text("secret")
    (live / "notes.txt").write_text("fine")
    work = tmp_path / "work"
    work.mkdir()
    with sandbox.OpenGuard([live], allow_write=[work]) as guard:
        with pytest.raises(PermissionError):
            open(live / "token")
        with pytest.raises(PermissionError):
            open(live / "new.txt", "w")
        assert open(live / "notes.txt").read() == "fine"  # a read of a live file is allowed
        (work / "out.txt").write_text("ok")
        (work / "token").write_text("harness token")  # a harness store's own token
        assert (work / "token").read_text() == "harness token"
    assert guard.refused_reads == 1 and guard.refused_writes == 1
    assert guard.events == ["credential:token", "write:new.txt"]
    assert not (live / "new.txt").exists()


def test_guard_site_refuses_in_a_child_interpreter(tmp_path):
    log = tmp_path / "refused.log"
    site = sandbox.write_guard_site(tmp_path / "site", log)
    code = (
        "import socket\ntry:\n socket.create_connection(('192.0.2.1', 80), timeout=1)\n"
        "except ConnectionRefusedError:\n print('refused')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(site)},
        timeout=30,
    )
    assert done.stdout.strip() == "refused"
    assert sandbox.count_refused(log) == 1


def test_claude_process_count_reads_proc(tmp_path):
    def proc(pid, ppid, comm):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm} x) S {ppid} 0 0\n")
        (d / "comm").write_text(comm + "\n")

    proc(10, 1, "python3")
    proc(11, 10, "claude")
    proc(12, 11, "sh")
    proc(20, 1, "claude")  # not below pid 10
    assert sandbox.descendants(10, tmp_path) == [11, 12]
    assert sandbox.claude_processes(10, tmp_path) == 1


def test_start_check_reports_every_finding(tmp_path):
    clean = {"PATH": str(sandbox.make_bin(tmp_path / "bin")), "HOME": "/h"}
    assert sandbox.start_check(clean, PROGRAM)["ok"]
    dirty = {"PATH": "/usr/bin", "ANTHROPIC_AUTH_TOKEN": "x"}
    res = sandbox.start_check(dirty, PROGRAM)
    assert not res["ok"] and res["env_names"] == ["ANTHROPIC_AUTH_TOKEN"]


def test_units_carry_the_limits_and_install_nothing(tmp_path):
    units = sandbox.render_units(tmp_path, "/usr/bin/python3", Path("/opt/tools"))
    core, local = units["nob-longtest.service"], units["nob-longtest-localnet.service"]
    for line in (
        "PrivateNetwork=yes",
        "Nice=19",
        "CPUWeight=10",
        "CPUQuota=400%",
        "IOSchedulingClass=idle",
        "MemoryMax=12G",
        "TasksMax=64",
        "UMask=0077",
        "RuntimeMaxSec=7d 2h",
        "UnsetEnvironment=ANTHROPIC_API_KEY",
    ):
        assert line in core, line
    assert "IPAddressDeny=any" in local and "IPAddressAllow=127.0.0.0/8" in local
    assert "PrivateNetwork" not in local
    assert f"PYTHONPATH={tmp_path}/private/guard-site:/opt/tools" in core
    plain = sandbox.render_units(
        tmp_path, "/usr/bin/python3", Path("/opt/tools"), private_network=False
    )
    assert "PrivateNetwork" not in plain["nob-longtest.service"]
    assert "ExecStart=/usr/bin/python3 -m longtest.cli run" in plain["nob-longtest.service"]
    assert f"PATH={tmp_path}/bin" in core
    assert not list(tmp_path.iterdir())


def test_process_table_falls_back_to_ps_without_proc(tmp_path):
    """macOS has no /proc: the same answers come from ps."""
    import os

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        missing = tmp_path / "no-proc"
        assert child.pid in sandbox.descendants(os.getpid(), missing)
        assert sandbox.claude_processes(os.getpid(), missing) == 0
    finally:
        child.kill()
        child.wait()
