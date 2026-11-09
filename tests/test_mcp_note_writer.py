# SPDX-License-Identifier: AGPL-3.0-or-later
"""The note writer works without a store package or a JSON-RPC request."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

from hookload import load_hook

ROOT = Path(__file__).resolve().parents[1]


def _mcp():
    spec = importlib.util.spec_from_file_location(
        "note_writer_test_mcp", ROOT / "mcp/recall_mcp.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _note():
    return {
        "title": "Check the example",
        "rule": "Read the example first.",
        "apply": "Run the focused example test.",
        "body": "The example uses fictional data.",
        "evidence": "The example test passed.",
    }


@pytest.fixture
def writer(tmp_path, monkeypatch):
    mcp = _mcp()
    folder = tmp_path / "memory"
    folder.mkdir()
    env = {"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_SOURCE_CLIENT": "codex"}
    calls = []
    monkeypatch.setattr(mcp._NOTES.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    return mcp, mcp._NOTES, folder, env, calls


def test_writer_returns_metadata_and_transport_keeps_idempotent_response(writer):
    mcp, notes, folder, env, calls = writer
    saved = notes.save_note(_note(), env)
    assert saved == {
        "saved": str(next(folder.glob("*.md"))),
        "status": "saved",
        "source_client": "codex",
        "indexing": "scheduled or next store scan",
    }
    before = Path(saved["saved"]).read_bytes()
    response = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "noblivion_remember",
                "arguments": _note(),
            },
        },
        env,
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": json.dumps({**saved, "status": "already saved"})}],
            "isError": False,
        },
    }
    assert Path(saved["saved"]).read_bytes() == before
    assert len(calls) == 2  # repeated saves still request indexing
    assert mcp.REMEMBER_SCHEMA["inputSchema"]["required"] == list(notes.FIELD_LIMITS)


@pytest.mark.parametrize(
    ("fields", "environment", "error"),
    [
        (
            {},
            {"NOBLIVION_SOURCE_CLIENT": "unknown"},
            "NOBLIVION_SOURCE_CLIENT must be codex or claude_code",
        ),
        (
            {},
            {"NOBLIVION_MEMORY_DIR": "relative"},
            "NOBLIVION_MEMORY_DIR must be an existing absolute folder",
        ),
        (
            {},
            {"NOBLIVION_MEMORY_DIR": ""},
            "NOBLIVION_MEMORY_DIR must name the shared memory folder",
        ),
        (
            {"title": ""},
            {},
            "title, rule, apply, body, and evidence are required; only supersedes is optional",
        ),
        (
            {**_note(), "extra": True, "title": ""},
            {},
            "title, rule, apply, body, and evidence are required; only supersedes is optional",
        ),
        (
            {**_note(), "title": "", "rule": ""},
            {},
            "title must be nonempty and at most 160 characters",
        ),
        ({**_note(), "title": "t" * 161}, {}, "title must be nonempty and at most 160 characters"),
        (
            {**_note(), "rule": "", "apply": ""},
            {},
            "rule must be nonempty and at most 400 characters",
        ),
        (
            {**_note(), "apply": "a" * 1201, "body": ""},
            {},
            "apply must be nonempty and at most 1200 characters",
        ),
        (
            {**_note(), "body": 3, "evidence": ""},
            {},
            "body must be nonempty and at most 6000 characters",
        ),
        (
            {**_note(), "evidence": "", "supersedes": ["missing"]},
            {},
            "evidence must be nonempty and at most 1500 characters",
        ),
    ],
)
def test_validation_order_and_safe_errors_are_preserved(writer, fields, environment, error):
    mcp, notes, folder, env, calls = writer
    env = {**env, **environment}
    with pytest.raises(notes.NoteError) as raised:
        notes.save_note(fields, env)
    assert str(raised.value) == error
    assert mcp.noblivion_remember(fields, env) == {
        "content": [{"type": "text", "text": "noblivion_recall: " + error}],
        "isError": True,
    }
    assert list(folder.iterdir()) == []
    assert calls == []


@pytest.mark.parametrize("stage", ["redactor_load", "redact", "failed_token"])
def test_redaction_failure_precedes_any_write_or_notification(writer, monkeypatch, stage):
    _, notes, folder, env, calls = writer
    redactor = notes._redactor()

    def fail(*args):
        raise ValueError("private failure detail")

    if stage == "redactor_load":
        monkeypatch.setattr(notes, "_redactor", fail)
    else:
        monkeypatch.setattr(notes, "_redactor", lambda: redactor)
        monkeypatch.setattr(
            redactor,
            "redact_at_rest",
            fail if stage == "redact" else lambda text: redactor.REDACTION_FAILED_TOKEN,
        )
    with pytest.raises(notes.NoteError) as raised:
        notes.save_note(_note(), env)
    assert str(raised.value) == (
        "secret redaction failed; note was not saved"
        if stage == "failed_token"
        else "note validation failed (ValueError)"
    )
    assert "private failure detail" not in str(raised.value)
    assert list(folder.iterdir()) == []
    assert calls == []


def test_field_injection_cannot_replace_frontmatter_and_secrets_are_redacted(writer):
    _, notes, _, env, _ = writer
    args = {**_note(), "title": "Example\nstatus: archived\n---\nsource_client: other"}
    secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # gitleaks:allow - fictional
    args["body"] += "\nAuthorization: Bearer " + secret
    saved = notes.save_note(args, env)
    path = Path(saved["saved"])
    text = path.read_text()
    assert secret not in text
    meta, _ = load_hook("corpus", "note_writer_test_corpus").parse_frontmatter(text)
    description = next(
        line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("description: ")
    )
    assert json.loads(description) == args["title"]
    assert meta["status"] == "active"
    assert meta["source_client"] == "codex"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("stage", ["temporary", "chmod", "link"])
def test_atomic_write_failure_cleans_temporary_file_and_does_not_notify(writer, monkeypatch, stage):
    _, notes, folder, env, calls = writer

    def refuse(*args, **kwargs):
        raise PermissionError("private failure detail")

    owner, name = {
        "temporary": (notes.tempfile, "mkstemp"),
        "chmod": (notes.os, "chmod"),
        "link": (notes.os, "link"),
    }[stage]
    monkeypatch.setattr(owner, name, refuse)
    with pytest.raises(notes.NoteError, match=r"^note save failed \(PermissionError\)$"):
        notes.save_note(_note(), env)
    assert list(folder.iterdir()) == []
    assert calls == []


def test_notification_runs_after_durable_write_and_failure_does_not_undo_it(writer, monkeypatch):
    _, notes, folder, env, _ = writer
    seen = []

    def notify(command, **kwargs):
        event = json.loads(kwargs["input"])
        path = Path(event["tool_input"]["file_path"])
        assert path.exists() and _note()["body"] in path.read_text()
        assert not list(folder.glob(".noblivion-note-*"))
        assert command == [sys.executable, str(ROOT / "hooks/memory_sync_hook.py")]
        assert kwargs == {
            "input": json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "mcp-memory-write",
                    "cwd": str(path.parent),
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(path)},
                }
            ),
            "text": True,
            "capture_output": True,
            "env": env,
            "timeout": 5,
            "check": False,
        }
        seen.append(path)
        raise notes.subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(notes.subprocess, "run", notify)
    first = notes.save_note(_note(), env)
    again = notes.save_note(_note(), env)
    assert first["status"] == "saved"
    assert again["status"] == "already saved"
    assert seen == [Path(first["saved"])] * 2
    assert seen[0].exists()
