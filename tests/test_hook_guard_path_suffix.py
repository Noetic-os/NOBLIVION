# SPDX-License-Identifier: AGPL-3.0-or-later
"""A command trigger fires when the command runs with a directory path (NOBLIVION-135).

A note with the trigger ``tools/worktree_guard.py remove`` or ``docker compose
down`` must show its rule when the command word, or the script that an
interpreter runs, carries a directory prefix: ``/usr/bin/docker compose down``,
``python3 /srv/proj-a/tools/worktree_guard.py remove x``. Rows only: no new deny.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pytest

from hookload import load_hook_with_config

gh = load_hook_with_config("guard_hook", {}, "guard_hook_t_path_suffix")


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


def entry(mid, rule, triggers, violates=""):
    return {
        "id": mid,
        "rule": rule,
        "apply": f"apply of {mid}",
        "scope": "tool",
        "triggers": list(triggers),
        "violates": violates,
    }


WORKTREE = entry(
    "worktree_removal",
    "Remove a worktree with the guard script, not with rm.",
    ["tools/worktree_guard.py remove"],
)
COMPOSE = entry(
    "compose_down_volumes",
    "Compose down with the volume flag deletes the data volumes.",
    ["docker compose down"],
)
GREP_NOTE = entry("quiet_note", "Keep the output short.", ["glob:CHANGELOG.md"])


@pytest.fixture
def env(tmp_path):
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"version": 1, "entries": [WORKTREE, COMPOSE, GREP_NOTE]}))
    return {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",  # this file tests which trigger fires, not the grading
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }


def call(env, cmd) -> Any:
    ev = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"}
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(ev)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def shown(out) -> list[str]:
    text = (out or {}).get("additionalContext", "")
    return [e["id"] for e in (WORKTREE, COMPOSE) if e["id"] in text]


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 tools/worktree_guard.py remove x",  # control: the plain form
        "python3 /srv/proj-a/tools/worktree_guard.py remove x",
        "python3 ./tools/worktree_guard.py remove x",
        "/usr/bin/python3 /srv/proj-a/tools/worktree_guard.py remove x",
        "cd /srv/proj-a && python3 /srv/proj-a/tools/worktree_guard.py remove x",
        "/srv/proj-a/tools/worktree_guard.py remove x",
    ],
)
def test_script_trigger_fires_with_a_directory_prefix(env, cmd):
    out = call(env, cmd)
    assert out is not None and "permissionDecision" not in out
    assert shown(out) == [WORKTREE["id"]]


@pytest.mark.parametrize(
    "cmd",
    [
        "docker compose down",
        "/usr/bin/docker compose down -v",
        "/usr/local/bin/docker compose down",
    ],
)
def test_program_trigger_fires_with_a_directory_prefix(env, cmd):
    out = call(env, cmd)
    assert out is not None and "permissionDecision" not in out
    assert shown(out) == [COMPOSE["id"]]


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 /srv/proj-a/tools/other_guard.py remove x",  # another script
        "python3 /srv/proj-a/tools/worktree_guard.py list",  # another sub command
        "cat /srv/proj-a/tools/worktree_guard.py remove",  # a path argument of cat
        "ls /usr/bin/docker compose down",  # a path argument of ls
        "echo hi",
    ],
)
def test_the_suffix_match_stays_quiet_elsewhere(env, cmd):
    assert call(env, cmd) is None


def test_the_suffix_match_adds_a_row_and_no_deny(tmp_path):
    violates = entry(
        "deny_note", "Never run the guard script.", ["tools/x.py run"], r"tools/x\.py\s+run"
    )
    table = tmp_path / "t2.json"
    table.write_text(json.dumps({"version": 1, "entries": [violates]}))
    env = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state2"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log2.jsonl"),
    }
    plain = call(env, "python3 tools/x.py run")
    assert plain is not None
    deny_plain = plain.get("permissionDecision")
    env["NOBLIVION_GUARD_STATE_DIR"] = str(tmp_path / "state3")
    pathed = call(env, "python3 /srv/p/tools/x.py run")
    assert pathed is not None
    assert pathed.get("permissionDecision") == deny_plain
