# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the stdlib hooks in ``hooks/``.

Each module imports, each hook entry point exits 0 on an empty event, and the
settings that replaced hard-coded names come from the config file. The full
hook test suites are ported separately.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
MODULES = sorted(p.stem for p in HOOKS.glob("*.py"))
ENTRY_POINTS = [
    ("guard_hook.py", []),
    ("stop_checks.py", []),
    ("stop_checks.py", ["--mark-correction"]),
    ("continuity_hook.py", []),
    ("memory_fields_hook.py", []),
    ("memory_sync_hook.py", []),
    ("trust_session_line.py", []),
    ("guard_table.py", ["--rebuild"]),
    ("recall_hook.py", []),
    ("recall_hook.py", ["--reset-rows"]),
    ("error_recall_hook.py", []),
    ("subagent_rules_hook.py", []),
    ("store_client.py", []),
]


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """A data dir, a home folder and a config file, all under ``tmp_path``."""
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
    return {"home": home, "data": data, "config": data / "config.json"}


def load(name: str):
    """Load ``hooks/<name>.py`` by path, as the hooks load their siblings."""
    spec = importlib.util.spec_from_file_location(f"smoke_{name}", HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # A dataclass resolves its annotations through sys.modules.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def test_every_moved_module_is_present() -> None:
    expected = {
        "continuity_hook",
        "corpus",
        "credential_guard",
        "error_recall_hook",
        "guard_hook",
        "guard_table",
        "hook_config",
        "label_rows",
        "memory_fields",
        "memory_fields_hook",
        "memory_labels",
        "memory_sync_hook",
        "memory_text",
        "recall_hook",
        "stop_checks",
        "store_client",
        "subagent_rules_hook",
        "trust_events",
        "trust_rank",
        "trust_session_line",
    }
    expected.add("mine_session_end")  # SessionEnd trigger of the transcript miner (E10)
    assert set(MODULES) == expected


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str, hook_env) -> None:
    assert load(name).__doc__


@pytest.mark.parametrize(("script", "args"), ENTRY_POINTS)
def test_hook_exits_zero_on_empty_event(script: str, args: list, hook_env) -> None:
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / script), *args],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert proc.stdout == ""


def test_data_dir_order(hook_env, monkeypatch) -> None:
    cfg = load("hook_config")
    assert cfg.data_dir() == hook_env["data"]
    monkeypatch.delenv("NOBLIVION_DATA_DIR")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_env["home"] / "plugin"))
    assert cfg.data_dir() == hook_env["home"] / "plugin"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("XDG_DATA_HOME", str(hook_env["home"] / "xdg"))
    assert cfg.data_dir() == hook_env["home"] / "xdg" / "noblivion"
    assert cfg.cache_dir() == hook_env["home"] / "xdg" / "noblivion" / "cache"


def test_default_memory_dir_is_built_from_home(hook_env) -> None:
    cfg = load("hook_config")
    home = hook_env["home"]
    slug = cfg.project_slug(home)
    assert "/" not in slug and slug.startswith("-")
    assert cfg.default_memory_dir() == home / ".claude" / "projects" / slug / "memory"


def test_broken_config_reads_as_empty(hook_env) -> None:
    hook_env["config"].write_text("{not json", encoding="utf-8")
    cfg = load("hook_config")
    assert cfg.load_config() == {}
    assert cfg.generic_args() == cfg.GENERIC_ARGS_DEFAULT


def test_guard_word_lists_are_extended_by_config(hook_env) -> None:
    guard = load("guard_hook")
    assert guard.GENERIC_ARGS == load("hook_config").GENERIC_ARGS_DEFAULT
    assert "example-host" not in guard.GENERIC_ARGS
    hook_env["config"].write_text(
        json.dumps(
            {
                "guard": {
                    "generic_args_extra": ["Example-Host"],
                    "evidence_stop_extra": ["widget"],
                }
            }
        ),
        encoding="utf-8",
    )
    guard = load("guard_hook")
    assert "example-host" in guard.GENERIC_ARGS
    assert {"origin", "main"} <= guard.GENERIC_ARGS
    assert "widget" in guard.EVIDENCE_STOP


def test_labels_have_no_host_map_and_take_prefixes_from_config(hook_env) -> None:
    text = "deploy app-db for PROJ-12 on example-host with gitleaks"
    labels = load("memory_labels")
    assert not hasattr(labels, "HOST_ALIASES")
    assert labels.labels(text) == {"gitleaks": "tool"}
    hook_env["config"].write_text(
        json.dumps({"labels": {"ticket_prefixes": ["proj"], "service_prefixes": ["app"]}}),
        encoding="utf-8",
    )
    labels = load("memory_labels")
    assert labels.labels(text) == {"app-db": "service", "PROJ-12": "key", "gitleaks": "tool"}


def test_memory_sync_without_indexer_is_a_logged_no_op(hook_env) -> None:
    sync = load("memory_sync_hook")
    state = hook_env["data"] / "state"
    env = {
        "NOBLIVION_DATA_DIR": str(hook_env["data"]),
        "NOBLIVION_MEMORY_SYNC_STATE_DIR": str(state),
        "NOBLIVION_MEMORY_SYNC_DEBOUNCE_S": "0",
    }
    (state).mkdir()
    (state / sync.PENDING_NAME).write_text("5", encoding="utf-8")
    ran: list = []
    status = sync.run_worker(env, runner=lambda *a: ran.append(a) or 0, now_ns=lambda: 10)
    assert status == "no_indexer"
    assert ran == []
    assert "status=no_indexer" in (state / sync.LOG_NAME).read_text(encoding="utf-8")


def test_memory_sync_runs_the_installed_indexer(hook_env) -> None:
    sync = load("memory_sync_hook")
    entry = hook_env["data"] / "venv" / "bin" / "noblivion"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)
    env = {"NOBLIVION_DATA_DIR": str(hook_env["data"])}
    cmd = sync.mirror_command(env)
    assert cmd is not None and cmd.endswith("noblivion index")
    passed = sync.cron_like_env({**env, "SECRET_TOKEN": "x", "HOME": "/nonexistent"})
    assert passed["NOBLIVION_DATA_DIR"] == str(hook_env["data"])
    assert "SECRET_TOKEN" not in passed


def test_continuity_briefing_works_when_the_store_is_down(hook_env) -> None:
    memory = hook_env["home"] / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# index\n", encoding="utf-8")
    (memory / "feedback_check_the_lock.md").write_text(
        "---\nname: check the lock\ndescription: check the lock file before a restart\n"
        "rule: Check the lock file before you restart the worker.\n---\nbody\n",
        encoding="utf-8",
    )
    cont = load("continuity_hook")
    env = {
        "NOBLIVION_CONTINUITY": "1",
        "NOBLIVION_RECALL_MEMORY_DIR": str(memory),
        "NOBLIVION_RECALL_CACHE_DIR": str(hook_env["data"] / "cache"),
    }
    assert not (hook_env["data"] / "store.json").exists()
    text, rows, status = cont.briefing({"cwd": "/srv/proj-demo"}, env)
    assert status == "recency_only"
    assert rows == 1
    assert "Check the lock file" in text


def test_stop_deploy_check_is_off_without_deploy_hosts(hook_env) -> None:
    stop = load("stop_checks")
    assert stop.DEPLOY_HOSTS == ()
    turn = stop.Turn(
        reply="I merged the pull request.",
        calls=[(1, "Bash", {"command": "gh pr merge 7 --squash"})],
    )
    assert stop.check_deploy(turn, stop.Ctx(live=False)) is None
    hook_env["config"].write_text(
        json.dumps({"stop": {"deploy_hosts": ["example-host"]}}), encoding="utf-8"
    )
    stop = load("stop_checks")
    turn = stop.Turn(
        reply="I merged the pull request.",
        calls=[(1, "Bash", {"command": "gh pr merge 7 --squash"})],
    )
    reason = stop.check_deploy(turn, stop.Ctx(live=False))
    assert reason is not None and "example-host" in reason
