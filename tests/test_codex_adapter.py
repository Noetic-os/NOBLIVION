# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex event translation and NOBLIVION hook boundaries. No model calls."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from noblivion import db, miner

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_noblivion_codex_adapter", ROOT / "codex/adapter.py")
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


def _installer():
    path = ROOT / "scripts/install_codex.py"
    spec = importlib.util.spec_from_file_location("_noblivion_codex_install_test", path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for name in tuple(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for folder in ("home", "data", "memory", "state", "project"):
        (tmp_path / folder).mkdir()
    (tmp_path / "data/config.json").write_text(
        json.dumps(
            {
                "memory_dirs": [str(tmp_path / "memory")],
                "namespace": "claude_code",
            }
        ),
        encoding="utf-8",
    )
    return {
        "data_dir": str(tmp_path / "data"),
        "memory_dir": str(tmp_path / "memory"),
        "state_dir": str(tmp_path / "state"),
        "namespace": "claude_code",
        "repo_root": str(ROOT),
        "workspace_prefixes": [str(tmp_path / "project")],
    }


def event(cfg: dict, name: str, **fields: object) -> dict:
    return {
        "session_id": "thr_codex_test_123",
        "cwd": cfg["workspace_prefixes"][0],
        "hook_event_name": name,
        **fields,
    }


def test_clients_can_point_at_one_store_and_memory_dir(cfg: dict) -> None:
    first = adapter.environment(cfg, event(cfg, "SessionStart", session_id="thr_first"))
    second = adapter.environment(cfg, event(cfg, "SessionStart", session_id="thr_second"))
    assert first["NOBLIVION_DATA_DIR"] == second["NOBLIVION_DATA_DIR"] == cfg["data_dir"]
    assert first["NOBLIVION_MEMORY_DIR"] == second["NOBLIVION_MEMORY_DIR"] == cfg["memory_dir"]
    assert first["NOBLIVION_RECALL_PROJECT"] == cfg["namespace"]
    assert second["NOBLIVION_RECALL_PROJECT"] == cfg["namespace"]
    assert first["NOBLIVION_RECALL_ROOT"] == Path(cfg["memory_dir"]).parent.name
    assert second["NOBLIVION_RECALL_ROOT"] == first["NOBLIVION_RECALL_ROOT"]
    assert first["NOBLIVION_RECALL_CACHE_DIR"] != second["NOBLIVION_RECALL_CACHE_DIR"]


def test_exec_command_becomes_bash_without_running_it(cfg: dict) -> None:
    translated = adapter.tool_events(
        event(cfg, "PreToolUse", tool_name="exec_command", tool_input={"cmd": "git status"})
    )
    assert len(translated) == 1
    assert translated[0]["tool_name"] == "Bash"
    assert translated[0]["tool_input"]["command"] == "git status"
    assert translated[0]["cwd"] == cfg["workspace_prefixes"][0]


def test_patch_tracks_every_changed_path(cfg: dict) -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: old.py\n"
        "*** Move to: new.py\n"
        "@@\n-old\n+new\n"
        "*** Delete File: gone.py\n"
        "*** Add File: added.py\n+x\n"
        "*** End Patch"
    )
    translated = adapter.tool_events(
        event(cfg, "PreToolUse", tool_name="apply_patch", tool_input={"command": patch})
    )
    assert [item["tool_name"] for item in translated] == ["Edit"] * 4
    assert [item["tool_input"]["file_path"] for item in translated] == [
        "old.py",
        "new.py",
        "gone.py",
        "added.py",
    ]


def test_unrelated_task_does_not_send_prompt_to_memory(cfg: dict, tmp_path: Path) -> None:
    other = tmp_path / "unrelated"
    other.mkdir()
    result = adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", cwd=str(other), prompt="A plain unrelated task"),
    )
    assert result == {}
    assert not list(Path(cfg["state_dir"]).rglob("codex-*.jsonl"))


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_fake_credential_read_is_denied_before_tool_runs(cfg: dict, tmp_path: Path) -> None:
    repo = Path(cfg["workspace_prefixes"][0])
    _git("init", "-q", cwd=repo)
    fake_secret = "s3cretTokenValue0123456789"  # gitleaks:allow - fictional fixture
    fake_url = f"https://bot:{fake_secret}@example.invalid/owner/repo.git"
    _git("remote", "add", "origin", fake_url, cwd=repo)

    for command in ("git remote -v", "cat .git/config"):
        blocked = adapter.handle_hook(
            cfg,
            event(
                cfg,
                "PreToolUse",
                tool_name="exec_command",
                tool_input={"cmd": command},
            ),
        )
        decision = blocked.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision == "deny", (command, blocked)
        assert fake_secret not in json.dumps(blocked)
        assert fake_url not in json.dumps(blocked)

    safe = adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PreToolUse",
            tool_name="exec_command",
            tool_input={"cmd": "git remote -v | sed 's#://[^@]*@#://#'"},
        ),
    )
    assert safe.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

    mcp_read = adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PreToolUse",
            tool_name="mcp__fs__read",
            tool_input={"path": ".git/config"},
        ),
    )
    assert mcp_read.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    assert fake_secret not in json.dumps(mcp_read)


def test_installer_is_idempotent_and_keeps_foreign_hooks(cfg: dict, tmp_path: Path) -> None:
    installer = _installer()
    executable = Path(cfg["data_dir"]) / "venv/bin/noblivion"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    foreign = {
        "description": "other hooks",
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "python3 /tmp/foreign_hook.py"}]}
            ]
        },
    }
    (codex_home / "hooks.json").write_text(json.dumps(foreign), encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    args = argparse.Namespace(
        data_dir=cfg["data_dir"],
        memory_dir=cfg["memory_dir"],
        workspace=cfg["workspace_prefixes"],
        namespace=cfg["namespace"],
        codex_home=str(codex_home),
    )

    installer.install(args)
    installer.install(args)

    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["description"] == "other hooks"
    for name in installer.EVENTS:
        owned = [
            group
            for group in hooks["hooks"][name]
            if any("/codex/adapter.py" in hook.get("command", "") for hook in group["hooks"])
        ]
        assert len(owned) == 1, name
    assert len(hooks["hooks"]["SessionStart"]) == 2
    assert hooks["hooks"]["SessionStart"][0] == foreign["hooks"]["SessionStart"][0]
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert toml.count("[mcp_servers.noblivion]") == 1
    assert 'model = "gpt-6-sol"' in toml
    assert 'NOBLIVION_SOURCE_CLIENT = "codex"' in toml
    installed_cfg = json.loads((codex_home / "noblivion/config.json").read_text(encoding="utf-8"))
    assert installed_cfg["recall_root"] == Path(cfg["memory_dir"]).parent.name
    assert f'NOBLIVION_RECALL_ROOT = "{installed_cfg["recall_root"]}"' in toml


def test_only_worktrees_of_selected_git_repo_are_active(
    cfg: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = Path(cfg["workspace_prefixes"][0])
    _git("init", "-q", cwd=selected)
    (selected / "README.md").write_text("fixture\n", encoding="utf-8")
    _git("add", "README.md", cwd=selected)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "initial",
        cwd=selected,
    )
    sibling = tmp_path / "sibling-worktree"
    _git("worktree", "add", "-q", "-b", "sibling", str(sibling), cwd=selected)
    unrelated = tmp_path / "unrelated-repo"
    unrelated.mkdir()
    _git("init", "-q", cwd=unrelated)

    installer = _installer()
    executable = Path(cfg["data_dir"]) / "venv/bin/noblivion"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    installed = installer.install(
        argparse.Namespace(
            data_dir=cfg["data_dir"],
            memory_dir=cfg["memory_dir"],
            workspace=[str(selected)],
            namespace=cfg["namespace"],
            codex_home=str(tmp_path / "codex-home"),
        )
    )
    cfg = json.loads(Path(installed["config"]).read_text(encoding="utf-8"))
    assert cfg["workspace_git_dirs"] == [adapter._git_common(str(selected))]
    assert adapter._git_common(str(sibling)) == cfg["workspace_git_dirs"][0]
    assert adapter._git_common(str(unrelated)) != cfg["workspace_git_dirs"][0]
    monkeypatch.setattr(adapter, "_invoke", lambda *args: {})
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", cwd=str(sibling), prompt="Worktree task"),
    )
    journals = list(Path(cfg["state_dir"]).rglob("codex-*.jsonl"))
    assert len(journals) == 1
    assert "Worktree task" in journals[0].read_text(encoding="utf-8")

    assert (
        adapter.handle_hook(
            cfg,
            event(cfg, "UserPromptSubmit", cwd=str(unrelated), prompt="Unrelated task"),
        )
        == {}
    )
    assert "Unrelated task" not in journals[0].read_text(encoding="utf-8")


def test_codex_journal_uses_shared_miner_record_shape(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "_invoke", lambda *args: {})
    cfg["recall_root"] = "shared-project-root"
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", prompt="Run the fixture build command."),
    )
    adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PostToolUse",
            tool_name="exec_command",
            tool_use_id="call_fixture",
            tool_input={"cmd": "make missing-target"},
            tool_response={
                "exit_code": 2,
                "output": "make: *** No rule to make target 'missing-target'.",
            },
        ),
    )
    adapter.handle_hook(
        cfg,
        event(cfg, "Stop", last_assistant_message="The build target failed."),
    )
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", prompt="No, use the generated build target."),
    )
    adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PostToolUse",
            session_id="thr_second_codex_session",
            tool_name="exec_command",
            tool_use_id="call_second_fixture",
            tool_input={"cmd": "make other-missing-target"},
            tool_response={
                "exit_code": 2,
                "output": "make: *** No rule to make target 'other-missing-target'.",
            },
        ),
    )

    folder = Path(cfg["state_dir"]) / "transcripts" / cfg["recall_root"]
    journals = sorted(folder.glob("codex-*.jsonl"))
    assert len(journals) == 2
    journal = journals[0]
    stats = miner.MineStats()
    with journal.open("rb") as source:
        scan = miner.mine_stream(
            source,
            session_id="codex_fixture",
            offset=0,
            miner=miner.Miner(stats),
            stats=stats,
        )
    assert scan.candidates
    assert stats.bad_lines == 0

    conn = db.open_db(Path(cfg["data_dir"]) / "noblivion.db", create=True)
    try:
        mined = miner.run(
            conn,
            miner.list_transcripts(str(folder / "codex-*.jsonl")),
            project=cfg["namespace"],
            max_run_s=None,
        )
        rows = conn.execute(
            "SELECT root, path, category FROM memories WHERE source_type = ? ORDER BY path",
            (db.SOURCE_MINED,),
        ).fetchall()
    finally:
        conn.close()
    assert mined.inserted == 3
    assert {row["root"] for row in rows} == {cfg["recall_root"]}
    assert {row["category"] for row in rows} == {
        miner.KIND_TOOL_ERROR,
        miner.KIND_CORRECTION,
    }
    assert len({row["path"].split("#", 1)[0] for row in rows}) == 2
    assert all(row["path"].startswith("codex-") for row in rows)


def test_mcp_fs_read_maps_to_shared_file_guard(cfg: dict) -> None:
    translated = adapter.tool_events(
        event(
            cfg,
            "PreToolUse",
            tool_name="mcp__fs__read",
            tool_input={"path": ".git/config"},
        )
    )
    assert len(translated) == 1
    assert translated[0]["tool_name"] == "Read"
    assert translated[0]["tool_input"]["file_path"] == ".git/config"


def test_codex_session_end_mines_codex_journals_with_separate_stamp(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg["recall_root"] = "shared-override-root"
    calls: list[tuple[str, dict, dict]] = []

    def invoke(_cfg: dict, env: dict, script: str, payload: dict, *args: str) -> dict:
        calls.append((script, env, payload))
        return {}

    monkeypatch.setattr(adapter, "_invoke", invoke)
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", prompt="A fixture prompt."),
    )
    adapter.handle_hook(cfg, event(cfg, "SessionEnd", reason="other"))
    script, env, payload = calls[-1]
    assert script == "mine_session_end"
    assert env["NOBLIVION_MINER_STAMP_DIR"] == cfg["state_dir"]
    assert shlex.split(env["NOBLIVION_MINE_CMD"]) == [
        str(Path(cfg["data_dir"]) / "venv/bin/noblivion"),
        "mine",
        "--transcripts",
        str(Path(cfg["state_dir"]) / "transcripts" / cfg["recall_root"] / "codex-*.jsonl"),
        "--lock-timeout",
        "30",
    ]
    assert Path(payload["transcript_path"]).is_file()
    assert Path(payload["transcript_path"]).parent.name == cfg["recall_root"]


def test_codex_miner_stamp_does_not_throttle_claude(cfg: dict, tmp_path: Path) -> None:
    path = ROOT / "hooks/mine_session_end.py"
    spec = importlib.util.spec_from_file_location("_noblivion_miner_stamp_test", path)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    started: list[list[str]] = []

    def starter(argv: list[str], _env: dict) -> bool:
        started.append(argv)
        return True

    env = {
        "NOBLIVION_DATA_DIR": cfg["data_dir"],
        "NOBLIVION_MINE_CMD": "true",
    }
    codex_env = {
        **env,
        "NOBLIVION_MINER_STAMP_DIR": str(tmp_path / "codex-stamps"),
    }
    assert hook.run_hook(env, starter, now=lambda: 1000.0) == "started"
    assert hook.run_hook(codex_env, starter, now=lambda: 1000.0) == "started"
    assert hook.run_hook(env, starter, now=lambda: 1300.0) == "throttled"
    assert hook.run_hook(codex_env, starter, now=lambda: 1300.0) == "throttled"
    assert len(started) == 2
    assert (Path(cfg["data_dir"]) / "cache/mine.stamp").is_file()
    assert (tmp_path / "codex-stamps/mine.stamp").is_file()


def test_spawn_agent_saves_its_task_for_subagent_start(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict]] = []

    def invoke(_cfg: dict, _env: dict, script: str, payload: dict, *args: str) -> dict:
        calls.append((script, payload))
        return {}

    monkeypatch.setattr(adapter, "_invoke", invoke)
    adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PreToolUse",
            tool_name="spawn_agent",
            tool_use_id="call_subagent",
            tool_input={"message": "Review the parser tests.", "task_name": "review"},
        ),
    )
    scripts = [name for name, _ in calls]
    assert "subagent_rules_hook" in scripts
    assert "recall_hook" in scripts
    rule_event = next(payload for name, payload in calls if name == "subagent_rules_hook")
    assert rule_event["tool_name"] == "Agent"
    assert rule_event["tool_input"]["prompt"] == "Review the parser tests."
    assert rule_event["tool_use_id"] == "call_subagent"
    recall_event = next(payload for name, payload in calls if name == "recall_hook")
    assert recall_event["prompt"] == "Review the parser tests."


def test_failed_codex_shell_call_yields_error_memory(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Path(cfg["memory_dir"]) / "feedback_rev_parse.md"
    memory.write_text(
        "---\n"
        "name: feedback_rev_parse\n"
        'description: "Use one ref for git rev-parse."\n'
        "metadata:\n  type: feedback\n"
        "rule: When git rev-parse fails with Needed a single revision, pass one ref.\n"
        'apply: "Run git rev-parse once per ref."\n'
        "scope: tool\n"
        "triggers: [git rev-parse]\n"
        "---\n\n"
        "`git rev-parse --short a b` fails with `fatal: Needed a single revision`.\n",
        encoding="utf-8",
    )
    original = adapter._invoke
    seen: list[dict] = []

    def invoke(config: dict, env: dict, script: str, payload: dict, *args: str) -> dict:
        if script == "error_recall_hook":
            seen.append(payload)
            return original(config, env, script, payload, *args)
        return {}

    monkeypatch.setattr(adapter, "_invoke", invoke)
    result = adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PostToolUse",
            tool_name="exec_command",
            tool_use_id="failed_command",
            tool_input={"cmd": "git rev-parse --short a b"},
            tool_response={
                "exit_code": 128,
                "output": "fatal: Needed a single revision",
            },
        ),
    )
    assert len(seen) == 1
    assert seen[0]["hook_event_name"] == "PostToolUseFailure"
    assert seen[0]["error"].startswith("Exit code 128\n")
    assert "feedback_rev_parse" in (
        result.get("hookSpecificOutput", {}).get("additionalContext") or ""
    )


def test_recall_root_uses_installed_value_not_inherited_environment(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg["recall_root"] = "shared-recall-root"
    monkeypatch.setenv("NOBLIVION_RECALL_ROOT", "wrong-client-root")
    env = adapter.environment(cfg, event(cfg, "SessionStart"))
    assert env["NOBLIVION_RECALL_ROOT"] == "shared-recall-root"


def test_an_inherited_claude_project_dir_is_dropped(
    cfg: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # NOBLIVION-37: Codex never sets CLAUDE_PROJECT_DIR. A value inherited
    # from an outer Claude Code session must not pick the memory folder.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    env = adapter.environment(cfg, event(cfg, "SessionStart"))
    assert "CLAUDE_PROJECT_DIR" not in env
    assert env["NOBLIVION_MEMORY_DIR"] == cfg["memory_dir"]
