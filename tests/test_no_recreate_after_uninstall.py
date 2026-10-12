# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-28: no process makes the data dir or the database again after an
uninstall deleted the data dir.

The reproducer: ``claude plugin uninstall`` deleted the data dir while the
store was still stopping. The store's stop ran ``PRAGMA wal_checkpoint`` on a
new connection, and ``db.connect`` made the folder (0700) and an empty
``noblivion.db`` (4096 bytes). Only ``install.sh`` may make the data dir, and a
new database needs the install stamp. Fictional data only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from noblivion import config, db, launcher, store
from noblivion.__main__ import main as cli_main
from store_helpers import mark_installed
from test_store import make_store, store_env, wait_for

ROOT = Path(__file__).resolve().parent.parent
UNINSTALL = ROOT / "scripts" / "uninstall.sh"


def _start_real_store(tmp_path: Path) -> tuple[Path, int, dict[str, str]]:
    env = store_env(tmp_path)  # marks the data dir as installed
    data = Path(env["NOBLIVION_DATA_DIR"])
    proc = subprocess.Popen(
        [sys.executable, "-m", "noblivion.store"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert wait_for(lambda: launcher.read_store_json(data) is not None, 20), "no store.json"
    return data, proc.pid, env


def _gone(pid: int) -> bool:
    try:
        finished, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return finished == pid


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def _delete_running_data_dir(path: Path, timeout: float = 5.0) -> None:
    """Delete a data dir that a live store may still write into. A file can
    appear or disappear during rmtree's walk, so retry errors while the root
    still exists. In Python before 3.13, a missing child raises FileNotFoundError
    even when the root remains. Stop retrying once deletion succeeds; the tests
    then check that nothing comes back."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if not path.exists():
                return
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_delete_retries_when_a_child_disappears_but_the_root_remains(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    child = data / "store.json"
    child.write_text("{}", encoding="utf-8")
    (data / "logs").mkdir()
    (data / "venv").mkdir()
    original_rmtree = shutil.rmtree
    calls = []

    def lose_child(path):
        calls.append(path)
        if len(calls) == 1:
            child.unlink()
            # Python <=3.12 propagates a child that disappears during the walk.
            # Inject that outcome explicitly so newer Python tests it as well.
            raise FileNotFoundError(2, "No such file or directory", str(child))
        original_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", lose_child)
    _delete_running_data_dir(data)
    assert calls == [data, data]
    assert not data.exists()


def test_delete_fails_at_the_deadline_if_the_root_remains(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    error = FileNotFoundError(2, "No such file or directory", str(data / "store.json"))

    def lose_child(path):
        raise error

    times = iter([0.0, 1.0])
    monkeypatch.setattr(shutil, "rmtree", lose_child)
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    with pytest.raises(FileNotFoundError) as caught:
        _delete_running_data_dir(data, timeout=0.5)
    assert caught.value is error
    assert data.is_dir()


# -- the reproducer -----------------------------------------------------------------


def test_a_store_stopping_after_the_data_dir_was_deleted_makes_nothing(tmp_path):
    """The NOBLIVION-28 reproducer: delete the data dir, then stop the store.
    Before the fix the stop made the folder and an empty noblivion.db."""
    data, pid, _ = _start_real_store(tmp_path)
    try:
        _delete_running_data_dir(data)
        os.kill(pid, signal.SIGTERM)
        assert wait_for(lambda: _gone(pid), 30), "the store did not stop"
        time.sleep(0.3)
        assert not data.exists(), sorted(p.name for p in data.iterdir())
    finally:
        _kill(pid)


def test_a_running_store_stops_when_its_data_dir_is_deleted(tmp_path):
    data, pid, _ = _start_real_store(tmp_path)
    try:
        _delete_running_data_dir(data)
        assert wait_for(lambda: _gone(pid), 20), "the store kept running without a data dir"
        assert not data.exists()
    finally:
        _kill(pid)


def test_uninstall_waits_for_the_store_to_exit(tmp_path):
    data, pid, env = _start_real_store(tmp_path)
    try:
        out = subprocess.run(
            ["bash", str(UNINSTALL), "--data-dir", str(data)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert out.returncode == 0, out.stderr
        assert "the store stopped" in out.stdout
        # The script returned: the store has exited, so a delete now is final.
        assert _gone(pid) or wait_for(lambda: _gone(pid), 1)
        shutil.rmtree(data)
        time.sleep(0.5)
        assert not data.exists()
    finally:
        _kill(pid)


# -- the rules behind the fix ------------------------------------------------------------


def test_connect_opens_an_existing_database_only(tmp_path):
    target = tmp_path / "gone" / "noblivion.db"
    with pytest.raises(db.DatabaseMissing):
        db.connect(target)
    with pytest.raises(db.DatabaseMissing):
        db.open_db(target, allow_migrate=True)
    assert not target.parent.exists()
    db.open_db(target, create=True).close()
    assert target.is_file()
    db.connect(target).close()


def test_the_store_needs_the_install_stamp_for_a_new_database(tmp_path):
    st = make_store(tmp_path)  # marks the data dir as installed
    config.install_stamp(st.data_dir).unlink()
    with pytest.raises(db.DatabaseMissing):
        st.open()
    assert not (st.data_dir / config.DB_FILE).exists()
    mark_installed(st.data_dir)
    st.open()
    try:
        assert (st.data_dir / config.DB_FILE).is_file()
    finally:
        st.close()


def test_the_store_never_makes_a_missing_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    assert store.main([]) == store.EXIT_NOT_INSTALLED
    assert not data.exists()


def test_the_launcher_never_makes_a_missing_data_dir(tmp_path, capsys):
    data = tmp_path / "data"
    env = {"NOBLIVION_DATA_DIR": str(data)}
    calls: list[float] = []
    state = launcher.ensure_running(env, spawn=lambda d, lock_wait_s=0.0: calls.append(1))
    assert state == launcher.STATE_NOT_INSTALLED
    assert calls == [] and not data.exists()


@pytest.fixture
def no_data_dir(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "data"
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.setenv("NOBLIVION_MINER", "1")  # off by default; ``mine`` must get to the database
    return data


@pytest.mark.parametrize(
    "argv",
    [["index"], ["mine"], ["trust", "report"], ["dedup", "pairs"], ["consent"]],
)
def test_cli_tools_never_make_the_data_dir(no_data_dir, argv, capsys):
    assert cli_main(argv) != 0
    assert not no_data_dir.exists()


def test_index_makes_a_new_database_only_after_install(no_data_dir, capsys):
    no_data_dir.mkdir()
    assert cli_main(["index"]) == 6
    assert "run install.sh" in capsys.readouterr().err
    assert not (no_data_dir / config.DB_FILE).exists()
    mark_installed(no_data_dir)
    assert cli_main(["index"]) == 0
    assert (no_data_dir / config.DB_FILE).is_file()


# -- the hooks of a session that still runs after the uninstall -----------------------------


def _events(tmp_path: Path) -> dict[str, dict]:
    memory = tmp_path / "home" / ".claude" / "projects" / "-work-demo" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    note = memory / "feedback_demo.md"
    note.write_text(
        "---\nname: demo\ndescription: run the demo tests first\ntype: feedback\n"
        'rule: Run the demo tests first.\napply: "pytest -q"\nscope: tool\n'
        "triggers: [pytest]\n---\nRun the demo tests before a push.\n",
        encoding="utf-8",
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "run the demo"}}) + "\n",
        encoding="utf-8",
    )
    base = {
        "session_id": "s-demo-1",
        "transcript_path": str(transcript),
        "cwd": "/work/demo",
        "permission_mode": "default",
    }
    bash = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_use_id": "tu1"}
    return {
        "SessionStart": {**base, "source": "startup"},
        "UserPromptSubmit": {**base, "prompt": "run the demo tests before the push"},
        "PreToolUse": {**base, **bash},
        "PostToolUse": {**base, **bash, "tool_response": {"stdout": "1 failed", "stderr": ""}},
        "PostToolUseFailure": {**base, **bash, "error": "Exit code 1\nModuleNotFoundError"},
        "PreCompact": {**base, "trigger": "manual", "custom_instructions": ""},
        "SubagentStart": {**base, "agent_id": "a1", "agent_type": "Explore"},
        "Stop": {**base, "stop_hook_active": False, "last_assistant_message": "Done."},
        "SessionEnd": {**base, "reason": "other"},
    }


def _hook_commands() -> list[tuple[str, str, str]]:
    doc = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    out = []
    for event, groups in doc["hooks"].items():
        for group in groups:
            first = group.get("matcher", "").split("|")[0]
            for hook in group["hooks"]:
                out.append((event, first, hook["command"]))
    return out


@pytest.mark.parametrize("event,matcher,command", _hook_commands())
def test_no_hook_makes_a_deleted_data_dir(tmp_path, event, matcher, command):
    """A session that still runs after ``claude plugin uninstall`` keeps its
    hooks (Claude Code keeps an orphaned plugin version for a while)."""
    data = tmp_path / "plugin-data"
    events = _events(tmp_path)
    payload = dict(events[event], hook_event_name=event)
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure") and matcher:
        payload["tool_name"] = matcher
        if matcher in ("Write", "Edit", "Read", "Grep"):
            payload["tool_input"] = {"file_path": str(tmp_path / "x.md"), "content": "x"}
        elif matcher in ("Agent", "Task"):
            payload["tool_input"] = {"prompt": "look", "subagent_type": "Explore"}
    if event == "SessionStart" and matcher:
        payload["source"] = matcher
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOBLIVION_", "CLAUDE_"))}
    env.update(
        HOME=str(tmp_path / "home"),
        CLAUDE_PLUGIN_ROOT=str(ROOT),
        CLAUDE_PLUGIN_DATA=str(data),
    )
    cmd = command.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
    proc = subprocess.run(
        ["bash", "-c", cmd],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    time.sleep(0.2)  # a detached child (the miner) would have run by now
    assert not data.exists(), (event, cmd, sorted(str(p) for p in data.rglob("*")))
    assert proc.returncode in (0, 2), proc.stderr[-400:]
