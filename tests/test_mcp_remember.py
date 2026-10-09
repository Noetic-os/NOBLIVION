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


@pytest.fixture
def claude_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """A Claude Code session with no NOBLIVION_MEMORY_DIR (NOBLIVION-94)."""
    config = tmp_path / "config"
    project = tmp_path / "project"
    config.mkdir(mode=0o700)
    project.mkdir()
    env = {
        "CLAUDE_CONFIG_DIR": str(config),
        "CLAUDE_PROJECT_DIR": str(project),
        "NOBLIVION_MEMORY_SYNC_OFF": "1",
        "HOME": str(tmp_path),
    }
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    return env, project, config / "projects" / slug / "memory"


def test_claude_remember_without_memory_dir_uses_the_session_folder(
    claude_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, expected = claude_env
    mcp = _mcp()
    result = mcp.noblivion_remember(_lesson(), env)
    assert result["isError"] is False, result

    files = list(expected.glob("reference_claude_code_*.md"))
    assert len(files) == 1
    assert stat.S_IMODE(expected.stat().st_mode) == 0o700
    hook_config = importlib.util.spec_from_file_location(
        "_hook_config_94", ROOT / "hooks" / "hook_config.py"
    )
    assert hook_config is not None and hook_config.loader is not None
    hc = importlib.util.module_from_spec(hook_config)
    hook_config.loader.exec_module(hc)
    assert hc.resolve_memory_dir(None, env) == expected


def test_claude_invalid_save_without_memory_dir_creates_no_folder(
    claude_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, expected = claude_env
    args = _lesson()
    del args["evidence"]
    result = _mcp().noblivion_remember(args, env)
    assert result["isError"] is True
    assert not expected.exists()


def test_codex_remember_without_memory_dir_still_fails(
    claude_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, _, expected = claude_env
    env["NOBLIVION_SOURCE_CLIENT"] = "codex"
    result = _mcp().noblivion_remember(_lesson(), env)
    assert result["isError"] is True
    assert "NOBLIVION_MEMORY_DIR" in result["content"][0]["text"]
    assert not expected.exists()
