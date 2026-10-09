# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex save tool and Claude recall share the NOBLIVION memory store."""

from __future__ import annotations

import importlib.util
import re
import stat
import sys
from pathlib import Path

import pytest

from noblivion import db, indexer

ROOT = Path(__file__).resolve().parents[1]


def _mcp():
    path = ROOT / "mcp" / "recall_mcp.py"
    name = f"_codex_remember_mcp_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def memory_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"
    data_dir.mkdir(mode=0o700)
    memory_dir.mkdir(mode=0o700)
    env = {
        "NOBLIVION_DATA_DIR": str(data_dir),
        "NOBLIVION_MEMORY_DIR": str(memory_dir),
        "NOBLIVION_RECALL_MEMORY_DIR": str(memory_dir),
        "NOBLIVION_RECALL_PROJECT": "claude_code",
        "NOBLIVION_SOURCE_CLIENT": "codex",
        "NOBLIVION_MEMORY_SYNC_OFF": "1",
        "HOME": str(tmp_path),
    }
    return env, data_dir, memory_dir


def _lesson() -> dict[str, str]:
    return {
        "title": "Check the test command before release",
        "rule": "Run the focused test before release.",
        "apply": "Use pytest on the changed test file.",
        "body": "This lesson applies to the shared test project.",
        "evidence": "Fixture result: focused test passed.",
    }


def test_tools_list_keeps_recall_and_adds_remember() -> None:
    mcp = _mcp()
    answer = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
    names = [tool["name"] for tool in answer["result"]["tools"]]
    assert names.count("noblivion_recall") == 1
    assert names.count("noblivion_remember") == 1


def test_remember_writes_one_private_redacted_note(
    memory_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, memory_dir = memory_env
    mcp = _mcp()
    args = _lesson()
    fake_secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # gitleaks:allow - fictional
    args["body"] += f"\nAuthorization: Bearer {fake_secret}"
    result = mcp.noblivion_remember(args, env)
    assert result["isError"] is False, result

    files = list(memory_dir.glob("*.md"))
    assert len(files) == 1
    saved = files[0]
    text = saved.read_text(encoding="utf-8")
    assert "source_client:" in text and "codex" in text
    assert "Evidence:" in text and args["evidence"] in text
    assert args["rule"] in text
    assert fake_secret not in text
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_invalid_save_through_mcp_makes_no_file(
    memory_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, memory_dir = memory_env
    mcp = _mcp()
    args = _lesson()
    del args["evidence"]
    answer = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "noblivion_remember", "arguments": args},
        },
        env,
    )
    assert answer is not None
    assert "error" in answer or answer["result"]["isError"] is True
    assert list(memory_dir.iterdir()) == []


def test_codex_note_enters_the_same_sqlite_namespace_as_claude(
    memory_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, data_dir, memory_dir = memory_env
    mcp = _mcp()
    assert mcp.noblivion_remember(_lesson(), env)["isError"] is False
    saved = next(memory_dir.glob("*.md"))
    assert _lesson()["rule"] in saved.read_text(encoding="utf-8")

    shared_db = data_dir / "noblivion.db"
    conn = db.open_db(shared_db, create=True)
    try:
        scan = indexer.scan(conn, [memory_dir], project=env["NOBLIVION_RECALL_PROJECT"])
        assert scan.changed == 1
        rows = db.md_rows(conn, env["NOBLIVION_RECALL_PROJECT"])
        assert len(rows) == 1
        content = conn.execute(
            "SELECT content FROM memories WHERE id = ?", (rows[0].id,)
        ).fetchone()[0]
        assert rows[0].path.startswith("reference_codex_")
        assert _lesson()["title"] in content
        assert _lesson()["evidence"] in content
        assert rows[0].id > 0
    finally:
        conn.close()
    assert list(data_dir.glob("*.db")) == [shared_db]


def test_remember_call_uses_the_same_mcp_endpoint(
    memory_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, memory_dir = memory_env
    mcp = _mcp()
    reply = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "noblivion_remember", "arguments": _lesson()},
        },
        env,
    )
    assert reply["id"] == 3
    assert reply["result"]["isError"] is False
    assert len(list(memory_dir.glob("*.md"))) == 1


# NOBLIVION-94: the plugin's .mcp.json sets no NOBLIVION_MEMORY_DIR, so with
# no override the tool must use the project memory folder the hooks use.


@pytest.fixture
def project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], Path, Path]:
    """An env with no NOBLIVION_MEMORY_DIR, a project folder, and the folder
    Claude Code keeps for that project (not created here)."""
    project = tmp_path / "work" / "project"
    # A .git folder makes the project its own repository root, so no folder
    # above tmp_path can change the project name.
    (project / ".git").mkdir(parents=True)
    config = tmp_path / "claude-config"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    folder = config / "projects" / slug / "memory"
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    env = {
        "NOBLIVION_DATA_DIR": str(data_dir),
        "CLAUDE_CONFIG_DIR": str(config),
        "NOBLIVION_MEMORY_SYNC_OFF": "1",
        "HOME": str(tmp_path),
    }
    no_policy = tmp_path / "no-managed-settings.json"
    cfg = _mcp()._load_hook()._sibling_module("hook_config")
    monkeypatch.setattr(cfg, "managed_settings_path", lambda: no_policy)
    return env, project, folder


@pytest.mark.parametrize("via", ["CLAUDE_PROJECT_DIR", "working dir"])
def test_remember_without_override_saves_in_the_project_memory_folder(
    project_env: tuple[dict[str, str], Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    env, project, folder = project_env
    folder.mkdir(parents=True)
    if via == "CLAUDE_PROJECT_DIR":
        env["CLAUDE_PROJECT_DIR"] = str(project)
        monkeypatch.chdir(tmp_path)  # the env var wins over the working dir
    else:
        monkeypatch.chdir(project)
    result = _mcp().noblivion_remember(_lesson(), env)
    assert result["isError"] is False, result
    saved = list(folder.glob("reference_claude_code_*.md"))
    assert len(saved) == 1
    assert _lesson()["rule"] in saved[0].read_text(encoding="utf-8")


def test_override_wins_over_the_project_memory_folder(
    project_env: tuple[dict[str, str], Path, Path], tmp_path: Path
) -> None:
    env, project, folder = project_env
    folder.mkdir(parents=True)
    override = tmp_path / "override"
    override.mkdir()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["NOBLIVION_MEMORY_DIR"] = str(override)
    assert _mcp().noblivion_remember(_lesson(), env)["isError"] is False
    assert len(list(override.glob("*.md"))) == 1
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("value", ["relative/memory", "absent"])
def test_bad_override_is_refused(
    project_env: tuple[dict[str, str], Path, Path], tmp_path: Path, value: str
) -> None:
    env, project, folder = project_env
    folder.mkdir(parents=True)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["NOBLIVION_MEMORY_DIR"] = value if value != "absent" else str(tmp_path / "absent")
    result = _mcp().noblivion_remember(_lesson(), env)
    assert result["isError"] is True
    assert (
        "NOBLIVION_MEMORY_DIR must be an existing absolute folder" in result["content"][0]["text"]
    )
    assert list(folder.iterdir()) == []


def test_missing_project_memory_folder_is_named_and_not_created(
    project_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, project, folder = project_env
    env["CLAUDE_PROJECT_DIR"] = str(project)
    result = _mcp().noblivion_remember(_lesson(), env)
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert str(folder) in text and "does not exist" in text
    assert "NOBLIVION_MEMORY_DIR" in text
    assert not folder.exists() and not folder.parent.exists()


def test_sync_hook_gets_the_folder_of_the_note(
    project_env: tuple[dict[str, str], Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project, folder = project_env
    folder.mkdir(parents=True)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    mcp = _mcp()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(mcp.subprocess, "run", lambda *a, **kw: calls.append(kw["env"]))
    assert mcp.noblivion_remember(_lesson(), env)["isError"] is False
    assert len(calls) == 1
    assert calls[0]["NOBLIVION_MEMORY_DIR"] == str(folder)
    assert "NOBLIVION_MEMORY_DIR" not in env
