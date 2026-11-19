# SPDX-License-Identifier: AGPL-3.0-or-later
"""A renamed field in the Claude Code hook input gives a sign (NOBLIVION-118).

Each hook entry point of ``hooks/hooks.json`` gets an input in which one field has a new name. The
hook still exits 0 and blocks nothing. The user gets one ``systemMessage`` for each session (an
async hook only logs, as Claude Code does not show its output), and ``input-drift.log`` gets one
line for each session. Complete input changes nothing.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hookload import HOOKS, load_hook
from recall_helpers import hook_env

dispatch = load_hook("dispatch", "hooktest_input_check_dispatch")

LOG_NAME = "input-drift.log"
NOTICE = "the Claude Code hook input changed"
SESSION = "s-drift-118"


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    return hook_env(
        tmp_path,
        HOME=str(home),
        XDG_DATA_HOME=str(tmp_path / "xdg"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
    )


def _event(tmp_path: Path, name: str, **fields: Any) -> dict[str, Any]:
    transcript = tmp_path / "t.jsonl"
    if not transcript.exists():
        transcript.write_text(json.dumps({"type": "user", "version": "9.8.7"}) + "\n")
    doc: dict[str, Any] = {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": name,
    }
    doc.update(fields)
    return doc


def _rename(doc: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    out = dict(doc)
    out[new] = out.pop(old)
    return out


BASH = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
WRITE = {"tool_name": "Write", "tool_input": {"file_path": "/x/notes.md", "content": "a"}}

# (id, script argv, event name, complete fields, field to rename, notify)
ENTRIES = [
    (
        "dispatch-SessionStart",
        ["dispatch.py", "SessionStart"],
        "SessionStart",
        {"source": "startup"},
        "source",
        True,
    ),
    (
        "dispatch-UserPromptSubmit",
        ["dispatch.py", "UserPromptSubmit"],
        "UserPromptSubmit",
        {"prompt": "hello"},
        "prompt",
        True,
    ),
    (
        "dispatch-PreCompact",
        ["dispatch.py", "PreCompact"],
        "PreCompact",
        {"trigger": "manual"},
        "trigger",
        True,
    ),
    ("guard_hook-PreToolUse", ["guard_hook.py"], "PreToolUse", BASH, "tool_name", True),
    (
        "guard_hook-PostToolUseFailure",
        ["guard_hook.py"],
        "PostToolUseFailure",
        BASH,
        "tool_input",
        True,
    ),
    (
        "mcp_session_hook",
        ["mcp_session_hook.py"],
        "PreToolUse",
        {"tool_name": "mcp__x__noblivion_recall", "tool_input": {}},
        "tool_name",
        True,
    ),
    (
        "recall_nudge",
        ["recall_nudge.py"],
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}},
        "tool_input",
        True,
    ),
    (
        "subagent_rules_hook-PreToolUse",
        ["subagent_rules_hook.py"],
        "PreToolUse",
        {"tool_name": "Agent", "tool_input": {"prompt": "x"}},
        "tool_input",
        True,
    ),
    (
        "subagent_rules_hook-SubagentStart",
        ["subagent_rules_hook.py"],
        "SubagentStart",
        {"agent_type": "Explore", "agent_id": "a1"},
        "agent_type",
        True,
    ),
    (
        "memory_fields_hook",
        ["memory_fields_hook.py"],
        "PostToolUse",
        dict(WRITE, tool_response={}),
        "tool_input",
        True,
    ),
    (
        "error_recall_hook-PostToolUse",
        ["error_recall_hook.py"],
        "PostToolUse",
        dict(BASH, tool_response={"stdout": ""}),
        "tool_response",
        True,
    ),
    (
        "error_recall_hook-PostToolUseFailure",
        ["error_recall_hook.py"],
        "PostToolUseFailure",
        dict(BASH, error="boom"),
        "tool_name",
        True,
    ),
    (
        "stop_checks",
        ["stop_checks.py"],
        "Stop",
        {"stop_hook_active": False},
        "stop_hook_active",
        True,
    ),
    (
        "learning_capture",
        ["learning_capture.py"],
        "SessionEnd",
        {"reason": "other"},
        "reason",
        True,
    ),
    (
        "memory_sync_hook",
        ["memory_sync_hook.py"],
        "PostToolUse",
        dict(WRITE, tool_response={}),
        "tool_name",
        False,
    ),
    (
        "trust_flush",
        ["trust_flush.py"],
        "Stop",
        {"stop_hook_active": False},
        "stop_hook_active",
        False,
    ),
    ("version_check", ["version_check.py"], "SessionStart", {"source": "startup"}, "source", False),
]


def _run(argv: list[str], doc: dict[str, Any], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / argv[0]), *argv[1:]],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data" / LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _blocks(stdout: str) -> bool:
    text = stdout.strip()
    try:
        doc = json.loads(text) if text else {}
    except ValueError:
        return False
    spec = doc.get("hookSpecificOutput") or {}
    return (
        doc.get("decision") == "block"
        or doc.get("continue") is False
        or spec.get("permissionDecision") == "deny"
    )


@pytest.mark.parametrize(
    ("argv", "name", "fields", "field", "notify"),
    [pytest.param(*e[1:], id=e[0]) for e in ENTRIES],
)
def test_a_renamed_field_gives_one_notice_and_one_log_line_per_session(
    tmp_path, argv, name, fields, field, notify
):
    env = _env(tmp_path)
    doc = _rename(_event(tmp_path, name, **fields), field, field + "_v2")
    first = _run(argv, doc, env)
    second = _run(argv, doc, env)
    for res in (first, second):
        assert res.returncode == 0, res.stderr
        assert not _blocks(res.stdout)
    if notify:
        message = json.loads(first.stdout)["systemMessage"]
        assert NOTICE in message and field in message and "9.8.7" in message
    else:
        assert NOTICE not in first.stdout
    assert NOTICE not in second.stdout
    lines = _log(tmp_path)
    assert len(lines) == 1
    assert lines[0]["missing"] == [field]
    assert lines[0]["event"] == name and lines[0]["claude_code"] == "9.8.7"
    assert field + "_v2" in lines[0]["fields"]


@pytest.mark.parametrize(
    ("argv", "name", "fields"),
    [pytest.param(*e[1:4], id=e[0]) for e in ENTRIES],
)
def test_complete_input_gives_no_notice_and_no_log(tmp_path, argv, name, fields):
    res = _run(argv, _event(tmp_path, name, **fields), _env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert NOTICE not in res.stdout
    assert _log(tmp_path) == []


def test_a_renamed_session_id_still_gives_one_notice(tmp_path):
    env = _env(tmp_path)
    doc = _rename(_event(tmp_path, "PreToolUse", **BASH), "session_id", "sessionId")
    outs = [_run(["guard_hook.py"], doc, env).stdout for _ in range(3)]
    assert sum(NOTICE in o for o in outs) == 1
    assert len(_log(tmp_path)) == 1


def test_the_warm_prompt_path_gives_the_notice(tmp_path):
    env = _env(tmp_path)
    args = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": SESSION,
        "cwd": str(tmp_path),
        "transcript_path": str(tmp_path / "t.jsonl"),
        "prompt": "",
    }  # "" : the field is gone
    code, stdout, _ = dispatch.run_forked(dispatch.WARM_EVENT, dispatch.warm_event(args), env)
    assert code == 0
    assert NOTICE in json.loads(stdout)["systemMessage"]
    assert _log(tmp_path)[0]["missing"] == ["prompt"]


# ── the helper: complete input passes stdout through byte for byte ──


@pytest.fixture
def ic():
    return load_hook("input_check", "hooktest_input_check")


def _call_run(ic, text: str, printed: str, monkeypatch) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    def fn() -> int:
        assert sys.stdin.read() == text  # the hook still reads its whole input
        sys.stdout.write(printed)
        return 0

    assert ic.run("demo", fn) == 0
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    return out.getvalue()


@pytest.mark.parametrize("printed", ["", "plain text", '{"b": 1,  "a": [ 2 ]}\n'])
def test_complete_input_keeps_the_output_byte_identical(ic, tmp_path, monkeypatch, printed):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_event(tmp_path, "PreToolUse", **BASH))
    assert _call_run(ic, text, printed, monkeypatch) == printed
    assert ic.apply(0, printed, text, "demo") is printed
    assert not (tmp_path / ic.LOG_NAME).exists()


def test_the_notice_joins_an_existing_system_message(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    out = json.loads(_call_run(ic, text, '{"systemMessage": "old", "x": 1}', monkeypatch))
    assert out["x"] == 1 and out["systemMessage"].startswith("old\nNOBLIVION:")


def test_a_non_zero_exit_keeps_the_notice_for_a_later_hook(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    assert ic.apply(2, "", text, "demo") == ""
    assert NOTICE in ic.apply(0, "", text, "demo")


def test_no_data_dir_gives_no_notice_and_makes_no_dir(ic, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(gone))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    assert ic.apply(0, "", text, "demo") == ""
    assert not gone.exists()


def test_only_input_that_names_its_event_is_checked(ic):
    assert ic.missing_fields("[1, 2]") is None
    assert ic.missing_fields("not json") is None
    assert ic.missing_fields("") is None
    assert ic.missing_fields('{"tool_name": "Bash", "tool_input": {}}') is None  # made by hand
    found = ic.missing_fields('{"hookEventName": "PreToolUse", "tool_name": "Bash"}')
    assert found is not None and "hook_event_name" in found[1]


def test_every_event_of_hooks_json_has_expected_fields(ic):
    events = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    assert set(events) <= set(ic.EXPECTED)


# ── the transcript shape ──


def _transcript_lines(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    (tmp_path / "t.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _msg(kind: str = "user", role: str = "user", content: Any = "hi") -> dict[str, Any]:
    return {"type": kind, "version": "9.8.7", "message": {"role": role, "content": content}}


def _noise(n: int) -> list[dict[str, Any]]:
    return [{"type": "system", "version": "9.8.7", "n": i} for i in range(n)]


def _drift_text(tmp_path: Path, event: str = "PreToolUse") -> str:
    return json.dumps(_event(tmp_path, event, **BASH))


def test_a_transcript_without_a_known_message_line_gives_one_notice_and_one_log_line(
    ic, tmp_path, monkeypatch
):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    # "message" renamed: type and version are still there, but no hook can read the line
    rows = [{"type": "user", "version": "9.8.7", "msg": {"role": "user", "content": "hi"}}]
    _transcript_lines(tmp_path, rows + _noise(ic.TRANSCRIPT_MIN_LINES))
    text = _drift_text(tmp_path)
    first = ic.apply(0, "", text, "demo")
    assert "the Claude Code session transcript changed shape" in json.loads(first)["systemMessage"]
    assert "9.8.7" in first and ic.TESTED_CLAUDE_CODE in first
    assert ic.apply(0, "", text, "demo") == ""  # one notice for each session
    lines = [json.loads(x) for x in (tmp_path / ic.LOG_NAME).read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["kind"] == "transcript"
    assert lines[0]["known"] == 0 and lines[0]["lines"] > ic.TRANSCRIPT_MIN_LINES
    assert "user" in lines[0]["types"] and lines[0]["claude_code"] == "9.8.7"


def test_a_renamed_role_inside_message_is_drift(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    rows = [_msg("user", role="human"), _msg("assistant", "assistant", [{"type": "chunk"}])]
    _transcript_lines(tmp_path, rows + _noise(ic.TRANSCRIPT_MIN_LINES))
    assert "changed shape" in ic.apply(0, "", _drift_text(tmp_path, "Stop"), "demo")


def test_a_known_message_line_or_a_short_transcript_gives_no_notice(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = _drift_text(tmp_path)
    _transcript_lines(tmp_path, _noise(ic.TRANSCRIPT_MIN_LINES - 1))  # too early for a verdict
    assert ic.apply(0, "", text, "demo") == ""
    assert ic.transcript_due(text)  # no marker yet: check again on the next call
    rows = [_msg("assistant", "assistant", [{"type": "tool_use"}])]
    _transcript_lines(tmp_path, _noise(ic.TRANSCRIPT_MIN_LINES) + rows)
    assert ic.apply(0, "", text, "demo") == ""
    assert not (tmp_path / ic.LOG_NAME).exists()
    assert not ic.transcript_due(text)  # healthy: the marker stops the check


def test_the_transcript_check_runs_only_on_pre_tool_post_tool_and_stop(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    _transcript_lines(tmp_path, _noise(2 * ic.TRANSCRIPT_MIN_LINES))
    start = json.dumps(_event(tmp_path, "SessionStart", source="startup"))
    assert ic.apply(0, "", start, "demo") == ""
    assert not ic.transcript_due(start)
    assert not (tmp_path / ic.LOG_NAME).exists()


def test_run_shows_the_transcript_notice_and_keeps_the_hook_output(ic, tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    _transcript_lines(tmp_path, _noise(2 * ic.TRANSCRIPT_MIN_LINES))
    out = json.loads(_call_run(ic, _drift_text(tmp_path), '{"x": 1}', monkeypatch))
    assert out["x"] == 1 and "changed shape" in out["systemMessage"]
