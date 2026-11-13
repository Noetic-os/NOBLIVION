"""NOBLIVION-56: a compaction resets the guard's show state of the session.

After a compaction the context no longer holds the guard rows and full texts
shown before it, so the guard must treat them as not yet shown: the show
counts and the budgets they used reset. The deny counts, the failed run-first
commands and the one-time table error note stay. A SessionStart of another
source (startup, resume, clear) leaves the state as it is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook
from recall_helpers import hook_env

gh = load_hook("guard_hook", "guard_hook_t_compact_reset")

SID = "sess-56"
BEFORE = {
    "denies": {"mem-2": 2},
    "rows": {"mem-1": 3},
    "full": {"mem-1": 1},
    "row_chars": 5900,
    "notes": {"table_error": 1, "everyday_chars": 1400},
    "failed": {"mem-3": 1},
    "labels": {"mem-4": 1},
    "label_chars": 800,
}


def _env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    return hook_env(
        tmp_path,
        HOME=str(tmp_path / "home"),
        NOBLIVION_GUARD_STATE_DIR=str(tmp_path / "guard-state"),
        NOBLIVION_GUARD_TABLE=str(tmp_path / "table.json"),
        NOBLIVION_MEMORY_DIR=str(tmp_path / "memory"),
        NOBLIVION_STOP_CHECK_STATE=str(tmp_path / "stop"),
        NOBLIVION_TRUST_REPORT_FILE=str(tmp_path / "trust.json"),
    )


def _seed(env: dict[str, str], agent: str = "main") -> Path:
    state = gh.SessionState(env, SID, agent)
    for key, value in BEFORE.items():
        setattr(state, key, json.loads(json.dumps(value)))
    state.close()
    return gh._state_file(env, SID, agent)


def _session_start(env: dict[str, str], source: str, **extra: str) -> None:
    event = {"hook_event_name": "SessionStart", "session_id": SID, "source": source, **extra}
    p = subprocess.run(
        [sys.executable, str(HOOKS / "dispatch.py"), "SessionStart"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_compact_resets_the_show_state_and_keeps_the_deny_record(tmp_path):
    env = _env(tmp_path)
    path = _seed(env)
    _session_start(env, "compact")
    after = _read(path)
    assert after["rows"] == {} and after["full"] == {} and after["labels"] == {}
    assert after["row_chars"] == 0 and after["label_chars"] == 0
    assert after["notes"] == {"table_error": 1}
    assert after["denies"] == BEFORE["denies"]
    assert after["failed"] == BEFORE["failed"]


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_other_sources_keep_the_state(tmp_path, source):
    env = _env(tmp_path)
    path = _seed(env)
    _session_start(env, source)
    assert _read(path) == BEFORE


def test_compact_resets_only_the_agent_of_the_event(tmp_path):
    env = _env(tmp_path)
    main_path = _seed(env)
    sub_path = _seed(env, "agent-7")
    _session_start(env, "compact", agent_id="agent-7")
    assert _read(main_path) == BEFORE
    assert _read(sub_path)["rows"] == {}


def test_compact_without_a_state_file_makes_none(tmp_path):
    env = _env(tmp_path)
    assert gh.reset_shown(env, SID) is False
    _session_start(env, "compact")
    assert not gh._state_file(env, SID).exists()
