# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PostToolUse hook that syncs a written memory at once.

The hook never blocks the tool call, acts only on a ``*.md`` inside the memory
folder, and N parallel writes make one indexer run. Every test uses tmp folders
and a fake indexer command; none reaches a host.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

HOOK = HOOKS / "memory_sync_hook.py"

ms = load_hook("memory_sync_hook", "memory_sync_hook_t_memory_sync")


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """No inherited ``NOBLIVION_*`` variable; home and data dir under ``tmp_path``."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    return {"home": home, "data": data}


@pytest.fixture()
def dirs(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    state = tmp_path / "state"
    return mem, state


def _env(mem: Path, state: Path, **extra: str) -> dict:
    env = {
        "NOBLIVION_MEMORY_DIR": str(mem),
        "NOBLIVION_MEMORY_SYNC_STATE_DIR": str(state),
        "NOBLIVION_DATA_DIR": str(state.parent / "data"),
        "HOME": str(state.parent / "home"),
        "PATH": "/usr/bin:/bin",
        "NOBLIVION_MEMORY_SYNC_CMD": "true",
    }
    env.update(extra)
    return env


def _event(path: Path, tool: str = "Write") -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": str(path)},
            "cwd": str(path.parent),
            "session_id": "s1",
        }
    )


def _log(state: Path) -> str:
    p = state / ms.LOG_NAME
    return p.read_text() if p.exists() else ""


def _wait_for(pred, timeout_s: float = 15.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ── which events trigger ────────────────────────────────────────────────────


def test_a_memory_write_triggers_one_spawn(dirs):
    mem, state = dirs
    spawned = []
    status = ms.run_hook(
        _event(mem / "feedback_x.md"),
        _env(mem, state),
        spawn=lambda env: spawned.append(1) or True,
    )
    assert status == "spawned" and spawned == [1]
    assert int((state / ms.PENDING_NAME).read_text()) > 0
    line = _log(state).strip()
    assert "event=trigger" in line and "file=feedback_x.md" in line and "status=spawned" in line
    assert len(line.splitlines()) == 1


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit"])
def test_edit_tools_trigger(dirs, tool):
    mem, state = dirs
    status = ms.run_hook(_event(mem / "MEMORY.md", tool), _env(mem, state), spawn=lambda env: True)
    assert status == "spawned"


def test_a_relative_path_resolves_against_the_event_cwd(dirs):
    mem, state = dirs
    ev = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": "topic_a.md"}, "cwd": str(mem)}
    )
    assert ms.run_hook(ev, _env(mem, state), spawn=lambda env: True) == "spawned"


@pytest.mark.parametrize("case", ["outside", "sibling_prefix", "not_md", "no_path", "off"])
def test_other_events_do_nothing(dirs, tmp_path, case):
    mem, state = dirs
    env = _env(mem, state)
    ev = _event(mem / "feedback_x.md")
    if case == "outside":
        ev = _event(tmp_path / "elsewhere" / "feedback_x.md")
    elif case == "sibling_prefix":  # "memory-old" starts with "memory"
        ev = _event(tmp_path / "memory-old" / "feedback_x.md")
    elif case == "not_md":
        ev = _event(mem / "notes.json")
    elif case == "no_path":
        ev = json.dumps({"tool_name": "Write", "tool_input": {}})
    elif case == "off":
        env["NOBLIVION_MEMORY_SYNC_OFF"] = "1"
    spawned = []
    assert ms.run_hook(ev, env, spawn=lambda e: spawned.append(1) or True) == ""
    assert spawned == [] and not state.exists()


def test_a_symlink_out_of_the_folder_does_not_trigger(dirs, tmp_path):
    mem, state = dirs
    real = tmp_path / "real.md"
    real.write_text("x")
    (mem / "link.md").symlink_to(real)
    assert ms.run_hook(_event(mem / "link.md"), _env(mem, state), spawn=lambda e: True) == ""


def test_hook_process_exits_0_and_is_silent_on_bad_input(dirs):
    mem, state = dirs
    for stdin in ("", "not json", "[1, 2]", '{"tool_name": "Write"}'):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            text=True,
            capture_output=True,
            env=_env(mem, state),
            timeout=20,
        )
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


# ── the indexer command ─────────────────────────────────────────────────────


def _config(data: Path, doc: dict) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps(doc))


def _install_indexer(data: Path) -> Path:
    entry = data / "venv" / "bin" / "noblivion"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/bin/sh\nexit 0\n")
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


def test_env_command_wins_over_the_config_and_the_indexer(tmp_path):
    data = tmp_path / "data"
    _config(data, {"sync": {"index_command": "configured-index --all"}})
    env = {"NOBLIVION_DATA_DIR": str(data), "NOBLIVION_MEMORY_SYNC_CMD": "echo hi"}
    assert ms.mirror_command(env, indexer=lambda e: "from-indexer") == "echo hi"
    env.pop("NOBLIVION_MEMORY_SYNC_CMD")
    assert ms.mirror_command(env, indexer=lambda e: "from-indexer") == "configured-index --all"
    _config(data, {})
    assert ms.mirror_command(env, indexer=lambda e: "from-indexer") == "from-indexer"
    assert ms.mirror_command(env, indexer=lambda e: None) is None


def test_a_blank_env_command_or_config_key_falls_through(tmp_path):
    data = tmp_path / "data"
    _config(data, {"sync": {"index_command": "   "}})
    env = {"NOBLIVION_DATA_DIR": str(data), "NOBLIVION_MEMORY_SYNC_CMD": "  "}
    assert ms.mirror_command(env, indexer=lambda e: "from-indexer") == "from-indexer"


def test_the_indexer_entry_point_is_used_only_when_installed(tmp_path):
    data = tmp_path / "data"
    env = {"NOBLIVION_DATA_DIR": str(data)}
    assert ms.indexer_entry(env) is None
    entry = _install_indexer(data)
    assert ms.indexer_entry(env) == f"{entry} index"
    assert ms.mirror_command(env) == f"{entry} index"
    entry.chmod(0o644)  # present but not executable
    assert ms.indexer_entry(env) is None


def test_the_command_gets_the_small_cron_environment():
    env = ms.cron_like_env(
        {
            "HOME": "/h",
            "USER": "u",
            "SECRET_TOKEN": "zzz",
            "PATH": "/odd",
            "NOBLIVION_DATA_DIR": "/d",
            "XDG_DATA_HOME": "/x",
            "CLAUDE_PLUGIN_DATA": "/p",
            "NOBLIVION_EMPTY": "",
        }
    )
    assert env == {
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/sh",
        "HOME": "/h",
        "USER": "u",
        "NOBLIVION_DATA_DIR": "/d",
        "XDG_DATA_HOME": "/x",
        "CLAUDE_PLUGIN_DATA": "/p",
    }


def test_the_default_state_dir_is_the_data_dir_cache(hook_env):
    mod = load_hook("memory_sync_hook", "memory_sync_hook_t_memory_sync_defaults")
    assert mod.state_dir({}) == hook_env["data"] / "cache"
    home = hook_env["home"]
    project = hook_env["home"].parent / "project"
    slug = str(project).replace("/", "-").replace("_", "-").replace(".", "-")
    env = {"HOME": str(home)}
    assert mod.memory_dir(env, str(project)) == home / ".claude" / "projects" / slug / "memory"
    assert mod.memory_dir(env) is None  # NOBLIVION-31: no home-folder default


# ── the worker ──────────────────────────────────────────────────────────────


def _worker(mem, state, runner, indexer=lambda e: None, **extra):
    env = _env(
        mem,
        state,
        NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="0",
        NOBLIVION_MEMORY_SYNC_RETRY_WAIT_S="0",
        **extra,
    )
    return ms.run_worker(env, runner=runner, indexer=indexer, sleep=lambda s: None)


def test_worker_runs_once_and_a_second_worker_is_covered(dirs):
    mem, state = dirs
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    calls = []

    def runner(cmd, t, env):
        calls.append(cmd)
        return 0

    assert _worker(mem, state, runner) == "ok"
    assert _worker(mem, state, runner) == "covered"
    assert calls == ["true"]
    log = _log(state)
    assert "status=ok" in log and "rc=0" in log and "status=covered" in log


def test_a_write_after_a_run_runs_again(dirs):
    mem, state = dirs
    calls = []

    def runner(cmd, t, env):
        calls.append(1)
        return 0

    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    _worker(mem, state, runner)
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**6)
    assert _worker(mem, state, runner) == "ok"
    assert len(calls) == 2


def test_a_failed_run_is_retried_and_not_marked_done(dirs):
    mem, state = dirs
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    rcs = iter([1, 1, 0])
    calls = []
    assert _worker(mem, state, lambda c, t, e: calls.append(1) or next(rcs)) == "ok"
    assert len(calls) == 3 and "attempts=3" in _log(state)

    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**6)
    calls.clear()
    assert _worker(mem, state, lambda c, t, e: calls.append(1) or 1) == "failed"
    assert len(calls) == ms.ATTEMPTS
    # Not marked done, so the next trigger runs the indexer again.
    assert _worker(mem, state, lambda c, t, e: calls.append(1) or 0) == "ok"


def test_no_indexer_is_logged_and_nothing_runs(dirs):
    mem, state = dirs
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    calls = []
    status = _worker(mem, state, lambda c, t, e: calls.append(1) or 0, NOBLIVION_MEMORY_SYNC_CMD="")
    assert status == "no_indexer" and calls == []
    assert "status=no_indexer" in _log(state)


def test_the_worker_runs_the_configured_command(dirs):
    mem, state = dirs
    _config(state.parent / "data", {"sync": {"index_command": "configured-index"}})
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    calls = []
    status = _worker(mem, state, lambda c, t, e: calls.append(c) or 0, NOBLIVION_MEMORY_SYNC_CMD="")
    assert status == "ok" and calls == ["configured-index"]


def test_the_log_never_holds_the_command(dirs):
    mem, state = dirs
    ms._write_stamp(state / ms.PENDING_NAME, time.time_ns() - 10**9)
    secret_cmd = "true # http://192.0.2.7:11435 host-name-xyz"
    _worker(mem, state, lambda c, t, e: 0, NOBLIVION_MEMORY_SYNC_CMD=secret_cmd)
    log = _log(state)
    assert "192.0.2.7" not in log and "host-name-xyz" not in log and "http" not in log


def test_run_command_times_out_and_returns_124(dirs):
    mem, state = dirs
    t0 = time.monotonic()
    assert ms.run_command("sleep 30", 0.3, _env(mem, state)) == 124
    assert time.monotonic() - t0 < 10
    assert ms.run_command("exit 7", 5, _env(mem, state)) == 7


# ── end to end: real hook, real detached worker, fake indexer ───────────────


def _fake_indexer(tmp_path: Path, seconds: float = 0.0) -> str:
    counter = tmp_path / "runs.txt"
    return f"sleep {seconds}; echo run >> {counter}"


def _runs(tmp_path: Path) -> int:
    p = tmp_path / "runs.txt"
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_end_to_end_hook_returns_fast_and_the_worker_runs(dirs, tmp_path):
    mem, state = dirs
    env = _env(
        mem,
        state,
        NOBLIVION_MEMORY_SYNC_CMD=_fake_indexer(tmp_path, 2),
        NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="0.3",
    )
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=_event(mem / "feedback_x.md"),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )
    took = time.monotonic() - t0
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    # the indexer sleeps 2 s
    assert took < 1.5, f"the hook waited for the indexer: {took:.2f} s"
    assert _wait_for(lambda: "status=ok" in _log(state))
    assert _runs(tmp_path) == 1


def test_end_to_end_the_installed_indexer_runs_with_the_data_dir(dirs, tmp_path):
    mem, state = dirs
    data = state.parent / "data"
    entry = _install_indexer(data)
    seen = tmp_path / "seen.txt"
    entry.write_text(f'#!/bin/sh\necho "$1 $NOBLIVION_DATA_DIR" >> {seen}\n')
    env = _env(mem, state, NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="0.1")
    env.pop("NOBLIVION_MEMORY_SYNC_CMD")
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=_event(mem / "feedback_x.md"),
        text=True,
        env=env,
        timeout=20,
        check=True,
    )
    assert _wait_for(lambda: "status=ok" in _log(state))
    assert seen.read_text().splitlines() == [f"index {data}"]


def test_end_to_end_parallel_writes_make_one_run(dirs, tmp_path):
    mem, state = dirs
    env = _env(
        mem,
        state,
        NOBLIVION_MEMORY_SYNC_CMD=_fake_indexer(tmp_path, 0.2),
        NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="1.0",
    )
    procs = [
        subprocess.Popen(
            [sys.executable, str(HOOK)],
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(6)
    ]
    for i, p in enumerate(procs):
        p.communicate(_event(mem / f"feedback_{i}.md"), timeout=20)
        assert p.returncode == 0
    assert _wait_for(lambda: _log(state).count("event=run") == 6, 30)
    log = _log(state)
    assert log.count("event=trigger") == 6
    assert log.count("status=ok") == 1 and log.count("status=covered") == 5
    assert _runs(tmp_path) == 1
    assert int((state / ms.DONE_NAME).read_text()) == int((state / ms.PENDING_NAME).read_text())


def test_end_to_end_a_write_during_a_run_runs_once_more(dirs, tmp_path):
    mem, state = dirs
    env = _env(
        mem,
        state,
        NOBLIVION_MEMORY_SYNC_CMD=_fake_indexer(tmp_path, 1.5),
        NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="0.1",
    )
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=_event(mem / "a.md"),
        text=True,
        env=env,
        timeout=20,
        check=True,
    )
    assert _wait_for(lambda: (state / ms.LOCK_NAME).exists())
    time.sleep(0.8)  # the first indexer run is under way
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=_event(mem / "b.md"),
        text=True,
        env=env,
        timeout=20,
        check=True,
    )
    assert _wait_for(lambda: _log(state).count("status=ok") == 2, 30)
    assert _runs(tmp_path) == 2


# ── the Bash leg ────────────────────────────────────────────────────────────
# A Bash call has no file path. The hook compares the newest change time of
# the memory folder with its own stamp. No test here starts a worker: `spawn`
# is a counter.


def _bash(command: str = "ls") -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": "/",
            "session_id": "s1",
        }
    )


class _Spawn:
    def __init__(self, ok: bool = True):
        self.calls, self.ok = 0, ok

    def __call__(self, env):
        self.calls += 1
        return self.ok


def _settled(mem: Path, state: Path, spawn: _Spawn) -> dict:
    """A folder with one memory, already seen by the Bash leg."""
    (mem / "feedback_a.md").write_text("a")
    env = _env(mem, state)
    assert ms.run_hook(_bash(), env, spawn=spawn) == "spawned"
    spawn.calls = 0
    (state / ms.LOG_NAME).unlink()
    return env


def _bump(path: Path) -> None:
    """Give ``path`` a change time later than every stamp, without a sleep."""
    ns = time.time_ns() + 5_000_000_000
    os.utime(path, ns=(ns, ns))


def test_bash_with_no_change_does_not_sync(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    pending = (state / ms.PENDING_NAME).read_text()
    for _ in range(3):
        assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 0 and _log(state) == ""
    assert (state / ms.PENDING_NAME).read_text() == pending


def test_bash_after_a_touched_file_syncs_once(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    _bump(mem / "feedback_a.md")
    assert ms.run_hook(_bash("python3 write_it.py"), env, spawn=spawn) == "spawned"
    assert spawn.calls == 1
    line = _log(state).strip()
    assert len(line.splitlines()) == 1
    assert "event=trigger" in line and "file=feedback_a.md" in line and "leg=bash" in line
    assert "write_it" not in line  # the command is never logged


def test_two_bash_calls_after_one_change_sync_once(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    _bump(mem / "feedback_a.md")
    assert ms.run_hook(_bash(), env, spawn=spawn) == "spawned"
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 1 and _log(state).count("event=trigger") == 1


@pytest.mark.parametrize("change", ["new_file", "delete", "old_mtime_copy"])
def test_bash_sees_a_new_a_deleted_and_an_old_dated_file(dirs, change):
    mem, state = dirs
    spawn = _Spawn()
    (mem / "feedback_b.md").write_text("b")
    env = _settled(mem, state, spawn)
    time.sleep(0.02)  # past the file system's time step
    if change == "new_file":
        (mem / "feedback_c.md").write_text("c")
    elif change == "delete":
        (mem / "feedback_b.md").unlink()
    else:  # `cp -p`: the mtime is years old
        (mem / "feedback_b.md").write_text("b2")
        os.utime(mem / "feedback_b.md", ns=(10**18, 10**18))
    assert ms.run_hook(_bash(), env, spawn=spawn) == "spawned"
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 1


@pytest.mark.parametrize("switch", ["NOBLIVION_MEMORY_SYNC_BASH_OFF", "NOBLIVION_MEMORY_SYNC_OFF"])
def test_bash_leg_switch_off(dirs, switch):
    mem, state = dirs
    (mem / "feedback_a.md").write_text("a")
    spawn = _Spawn()
    env = _env(mem, state, **{switch: "1"})
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 0 and not state.exists()


def test_bash_switch_off_leaves_the_write_leg_on(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _env(mem, state, NOBLIVION_MEMORY_SYNC_BASH_OFF="1")
    assert ms.run_hook(_event(mem / "feedback_x.md"), env, spawn=spawn) == "spawned"
    assert spawn.calls == 1 and not (state / ms.BASH_SEEN_NAME).exists()


def test_bash_change_outside_the_memory_folder_does_not_sync(dirs, tmp_path):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    time.sleep(0.02)
    other = tmp_path / "memory-old"
    other.mkdir()
    (other / "feedback_x.md").write_text("x")  # a sibling folder
    (tmp_path / "notes.md").write_text("x")  # the parent folder
    assert ms.run_hook(_bash(f"echo x > {other}/feedback_x.md"), env, spawn=spawn) == ""
    assert spawn.calls == 0 and _log(state) == ""


def test_bash_with_a_missing_memory_folder_does_nothing(dirs, tmp_path):
    mem, state = dirs
    spawn = _Spawn()
    env = _env(tmp_path / "absent", state)
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 0 and not state.exists()


def test_first_bash_call_without_a_stamp_syncs_once(dirs):
    mem, state = dirs
    (mem / "feedback_a.md").write_text("a")
    spawn = _Spawn()
    env = _env(mem, state)
    assert ms.run_hook(_bash(), env, spawn=spawn) == "spawned"
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 1


def test_a_write_tool_sync_is_not_repeated_by_the_next_bash_call(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    time.sleep(0.02)
    (mem / "feedback_w.md").write_text("w")  # what the Write tool did
    assert ms.run_hook(_event(mem / "feedback_w.md"), env, spawn=spawn) == "spawned"
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 1


def test_a_failed_worker_start_is_tried_again_at_the_next_bash_call(dirs):
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    _bump(mem / "feedback_a.md")
    assert ms.run_hook(_bash(), env, spawn=_Spawn(ok=False)) == "spawn_error"
    assert ms.run_hook(_bash(), env, spawn=spawn) == "spawned"
    assert ms.run_hook(_bash(), env, spawn=spawn) == ""
    assert spawn.calls == 1


def test_an_outside_index_run_costs_one_extra_sync_not_one_per_call(dirs):
    """Another indexer run carries a change and does not touch the hook's
    stamp. The next Bash call syncs once more; the calls after it do not."""
    mem, state = dirs
    spawn = _Spawn()
    env = _settled(mem, state, spawn)
    _bump(mem / "feedback_a.md")  # changed outside; another run indexed it
    results = [ms.run_hook(_bash(), env, spawn=spawn) for _ in range(5)]
    assert results == ["spawned", "", "", "", ""] and spawn.calls == 1


def test_end_to_end_bash_leg_runs_the_indexer_once(dirs, tmp_path):
    mem, state = dirs
    (mem / "feedback_a.md").write_text("a")
    env = _env(
        mem,
        state,
        NOBLIVION_MEMORY_SYNC_CMD=_fake_indexer(tmp_path),
        NOBLIVION_MEMORY_SYNC_DEBOUNCE_S="0.2",
    )
    for _ in range(3):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=_bash(),
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _wait_for(lambda: "event=run status=ok" in _log(state))
    time.sleep(0.5)
    assert _runs(tmp_path) == 1
    assert _log(state).count("event=trigger") == 1
