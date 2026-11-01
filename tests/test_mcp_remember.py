# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex save tool and Claude recall share the NOBLIVION memory store."""

from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture
def sync_calls(monkeypatch):
    calls = []
    mcp = _mcp()
    monkeypatch.setattr(mcp.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    return mcp, calls


def test_supersedes_schema_and_real_tool_call(memory_env, sync_calls):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    listing = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, env)
    schema = next(
        tool["inputSchema"]
        for tool in listing["result"]["tools"]
        if tool["name"] == "noblivion_remember"
    )
    assert schema["required"] == ["title", "rule", "apply", "body", "evidence"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["supersedes"]["minItems"] == 1
    assert schema["properties"]["supersedes"]["maxItems"] == indexer.SUPERSEDES_MAX
    names = ["old.md.md", "feedback_z.md", "_older-2.md"]
    for name in names:
        (folder / name).write_text("A previous lesson.\n")
    reply = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "noblivion_remember",
                "arguments": {
                    **_lesson(),
                    "supersedes": ["old.md.md", "feedback_z", "_older-2.md"],
                },
            },
        },
        env,
    )
    assert reply["result"]["isError"] is False
    saved = Path(json.loads(reply["result"]["content"][0]["text"])["saved"])
    text = saved.read_text()
    assert indexer.parse_file(saved.name, text).supersedes == ("_older-2", "feedback_z", "old.md")
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert len(calls) == 1
    assert all((folder / name).read_text() == "A previous lesson.\n" for name in names)


def test_omitting_supersedes_keeps_existing_note_bytes(memory_env, sync_calls):
    env, _, folder = memory_env
    mcp, _ = sync_calls
    result = mcp.noblivion_remember(_lesson(), env)
    assert not result["isError"]
    saved = next(folder.glob("*.md"))
    ident = saved.stem.removeprefix("reference_codex_")
    expected = (
        '---\nname: "reference-codex-' + ident + '"\n'
        'description: "Check the test command before release"\n'
        'type: "reference"\nsource_client: "codex"\nstatus: "active"\n'
        'rule: "Run the focused test before release."\n'
        'apply: "Use pytest on the changed test file."\n'
        'scope: "always"\n---\n\n# Check the test command before release\n\n'
        "This lesson applies to the shared test project.\n\n"
        "Evidence: Fixture result: focused test passed.\n"
    )
    assert saved.read_bytes() == expected.encode()


def test_supersedes_accepts_eight_targets_and_maximum_filename_length(memory_env, sync_calls):
    env, _, folder = memory_env
    mcp, _ = sync_calls
    stems = ["a" * 252, *[f"old_{i}" for i in range(7)]]
    for stem in stems:
        (folder / (stem + ".md")).write_text("Existing source.\n")
    result = mcp.noblivion_remember({**_lesson(), "supersedes": stems}, env)
    assert not result["isError"]
    saved = Path(json.loads(result["content"][0]["text"])["saved"])
    assert indexer.parse_file(saved.name, saved.read_text()).supersedes == tuple(sorted(stems))


@pytest.mark.parametrize(
    "targets",
    [
        None,
        "old",
        {},
        [],
        [True],
        [1],
        [None],
        [""],
        ["old"] * 9,
        ["old", "old.md"],
        ["old", "old"],
        ["missing"],
        ["../old"],
        ["folder/old"],
        ["folder\\old"],
        ["/old"],
        [" old"],
        ["old "],
        ["old\n"],
        ["old,other"],
        ["[old]"],
        [".hidden"],
        ["éclair"],
        ["a" * 253],
        ["OLD"],
    ],
)
def test_invalid_supersedes_never_writes_or_schedules(memory_env, sync_calls, targets):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    old = folder / "old.md"
    old.write_bytes(b"Existing source.\n")
    result = mcp.noblivion_remember({**_lesson(), "supersedes": targets}, env)
    assert result["isError"] is True
    assert list(folder.iterdir()) == [old]
    assert old.read_bytes() == b"Existing source.\n"
    assert calls == []


@pytest.mark.parametrize("kind", ["directory", "symlink_inside", "symlink_outside", "other_scope"])
def test_supersedes_requires_a_regular_file_in_this_folder(memory_env, sync_calls, kind):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    outside = folder.parent / "old.md"
    outside.write_text("Outside source.\n")
    target = folder / "old.md"
    if kind == "directory":
        target.mkdir()
    elif kind.startswith("symlink"):
        original = outside
        if kind == "symlink_inside":
            original = folder / "real.md"
            original.write_text("Inside source.\n")
        target.symlink_to(original)
    before = {path.name for path in folder.iterdir()}
    result = mcp.noblivion_remember({**_lesson(), "supersedes": ["old"]}, env)
    assert result["isError"] is True
    assert {path.name for path in folder.iterdir()} == before
    assert outside.read_text() == "Outside source.\n"
    assert calls == []


def test_missing_supersedes_target_does_not_create_claude_folder(claude_env, sync_calls):
    env, _, expected = claude_env
    mcp, calls = sync_calls
    result = mcp.noblivion_remember({**_lesson(), "supersedes": ["old"]}, env)
    assert result["isError"] is True
    assert not expected.exists()
    assert calls == []


@pytest.mark.parametrize("extra", [{"unknown": "old"}, {"supersedes": ["old"], "other": True}])
def test_remember_still_rejects_unknown_fields(memory_env, sync_calls, extra):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    assert mcp.noblivion_remember({**_lesson(), **extra}, env)["isError"] is True
    assert list(folder.iterdir()) == []
    assert calls == []


def test_supersedes_is_idempotent_but_target_changes_and_self_target_are_rejected(
    memory_env, sync_calls
):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    for name in ("a.md", "b.md"):
        (folder / name).write_text("Previous source.\n")
    first = mcp.noblivion_remember({**_lesson(), "supersedes": ["b", "a.md"]}, env)
    assert not first["isError"]
    saved = Path(json.loads(first["content"][0]["text"])["saved"])
    before = saved.read_bytes()
    repeated = mcp.noblivion_remember({**_lesson(), "supersedes": ["a", "b.md"]}, env)
    assert json.loads(repeated["content"][0]["text"])["status"] == "already saved"
    calls.clear()
    for targets in (["a"], [saved.name]):
        result = mcp.noblivion_remember({**_lesson(), "supersedes": targets}, env)
        assert result["isError"] is True
        assert saved.read_bytes() == before
    assert calls == []


def test_supersedes_keeps_concurrent_writer_and_cleans_temp_file(
    memory_env, sync_calls, monkeypatch
):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    (folder / "old.md").write_text("Previous source.\n")
    link = mcp.os.link

    def concurrent_writer(source, destination):
        destination.write_bytes(b"Concurrent source.\n")
        link(source, destination)

    monkeypatch.setattr(mcp.os, "link", concurrent_writer)
    result = mcp.noblivion_remember({**_lesson(), "supersedes": ["old"]}, env)
    assert result["isError"] is True
    assert "concurrently" in result["content"][0]["text"]
    assert next(folder.glob("reference_*.md")).read_bytes() == b"Concurrent source.\n"
    assert not list(folder.glob(".noblivion-note-*"))
    assert calls == []


def test_secret_shaped_supersedes_identifier_is_not_saved_or_echoed(memory_env, sync_calls):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # gitleaks:allow - fictional
    old = folder / (secret + ".md")
    old.write_text("Existing source.\n")
    result = mcp.noblivion_remember({**_lesson(), "supersedes": [secret]}, env)
    assert result["isError"] is True
    assert secret not in json.dumps(result)
    assert list(folder.iterdir()) == [old]
    assert calls == []


@pytest.mark.parametrize("failure", ["exception", "failed_token", "changed"])
def test_supersedes_redaction_fails_closed(memory_env, sync_calls, monkeypatch, failure):
    env, _, folder = memory_env
    mcp, calls = sync_calls
    (folder / "old.md").write_text("Existing source.\n")
    redactor = mcp._redactor()
    original = redactor.redact_at_rest

    def redact(text):
        if text != "old":
            return original(text)
        if failure == "exception":
            raise ValueError("private redactor detail")
        return redactor.REDACTION_FAILED_TOKEN if failure == "failed_token" else "different"

    monkeypatch.setattr(redactor, "redact_at_rest", redact)
    monkeypatch.setattr(mcp, "_redactor", lambda: redactor)
    result = mcp.noblivion_remember({**_lesson(), "supersedes": ["old"]}, env)
    assert result["isError"] is True
    assert "private redactor detail" not in json.dumps(result)
    assert [p.name for p in folder.iterdir()] == ["old.md"]
    assert calls == []


@pytest.mark.parametrize("field", ["title", "rule", "apply", "body", "evidence"])
def test_supersedes_keeps_all_existing_field_redaction(memory_env, sync_calls, field):
    env, _, folder = memory_env
    mcp, _ = sync_calls
    (folder / "old.md").write_text("Existing source.\n")
    secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # gitleaks:allow - fictional
    args = {**_lesson(), "supersedes": ["old"]}
    args[field] += "\nAuthorization: Bearer " + secret
    result = mcp.noblivion_remember(args, env)
    assert not result["isError"]
    assert secret not in next(folder.glob("reference_*.md")).read_text()


@pytest.mark.parametrize("with_link", [False, True])
def test_saved_correction_uses_existing_indexer_scope_and_live_rows(
    memory_env, sync_calls, with_link
):
    env, data, folder = memory_env
    mcp, _ = sync_calls
    other = folder.parent / "other-project" / "memory"
    other.mkdir(parents=True)
    old = folder / "feedback_old.md"
    old_text = "---\nname: old\ndescription: old limit\n---\nThe limit was 100 units.\n"
    old.write_text(old_text)
    (other / old.name).write_text(old_text)
    conn = db.open_db(data / "noblivion.db", create=True)
    try:
        indexer.scan(conn, [folder, other], project="fixture")
        indexer.scan(conn, [folder], project="another")
        args = _lesson()
        if with_link:
            args["supersedes"] = [old.name]
        result = mcp.noblivion_remember(args, env)
        assert not result["isError"]
        saved = Path(json.loads(result["content"][0]["text"])["saved"])
        # Saving only schedules indexing. The existing row is still live now.
        assert (
            conn.execute("SELECT count(*) FROM memories WHERE archived_at IS NOT NULL").fetchone()[
                0
            ]
            == 0
        )
        scan = indexer.scan(conn, [folder, other], project="fixture")
        assert scan.superseded == int(with_link)
        rows = {
            (r["project"], r["root"], r["path"])
            for r in conn.execute(
                "SELECT project,root,path FROM memories "
                "WHERE archived_at IS NULL AND deleted_at IS NULL"
            )
        }
        assert ("fixture", folder.parent.name, saved.name) in rows
        assert (("fixture", folder.parent.name, old.name) in rows) is not with_link
        assert ("fixture", other.parent.name, old.name) in rows
        assert ("another", folder.parent.name, old.name) in rows
        assert old.read_text() == old_text
        assert (other / old.name).read_text() == old_text
    finally:
        conn.close()
