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
from typing import Any, Dict, List, Optional

import pytest

from hookload import HOOKS, load_hook
from recall_helpers import hook_env

ic = load_hook("input_check", "hooktest_input_check")
dispatch = load_hook("dispatch", "hooktest_input_check_dispatch")

NOTICE = "the Claude Code hook input changed"
SESSION = "s-drift-118"


def _env(tmp_path: Path) -> Dict[str, str]:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    return hook_env(
        tmp_path,
        HOME=str(home),
        XDG_DATA_HOME=str(tmp_path / "xdg"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
    )


def _event(tmp_path: Path, name: str, **fields: Any) -> Dict[str, Any]:
    transcript = tmp_path / "t.jsonl"
    if not transcript.exists():
        transcript.write_text(json.dumps({"type": "user", "version": "9.8.7"}) + "\n")
    doc: Dict[str, Any] = {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": name,
    }
    doc.update(fields)
    return doc


def _rename(doc: Dict[str, Any], old: str, new: str) -> Dict[str, Any]:
    out = dict(doc)
    out[new] = out.pop(old)
    return out


BASH = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
WRITE = {"tool_name": "Write", "tool_input": {"file_path": "/x/notes.md", "content": "a"}}

# (id, script argv, event name, complete fields, field to rename, notify)
ENTRIES = [
    ("dispatch-SessionStart", ["dispatch.py", "SessionStart"], "SessionStart", {"source": "startup"}, "source", True),
    ("dispatch-UserPromptSubmit", ["dispatch.py", "UserPromptSubmit"], "UserPromptSubmit", {"prompt": "hello"}, "prompt", True),
    ("dispatch-PreCompact", ["dispatch.py", "PreCompact"], "PreCompact", {"trigger": "manual"}, "trigger", True),
    ("guard_hook-PreToolUse", ["guard_hook.py"], "PreToolUse", BASH, "tool_name", True),
    ("guard_hook-PostToolUseFailure", ["guard_hook.py"], "PostToolUseFailure", BASH, "tool_input", True),
    ("mcp_session_hook", ["mcp_session_hook.py"], "PreToolUse", {"tool_name": "mcp__x__noblivion_recall", "tool_input": {}}, "tool_name", True),
    ("recall_nudge", ["recall_nudge.py"], "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}}, "tool_input", True),
    ("subagent_rules_hook-PreToolUse", ["subagent_rules_hook.py"], "PreToolUse", {"tool_name": "Agent", "tool_input": {"prompt": "x"}}, "tool_input", True),
    ("subagent_rules_hook-SubagentStart", ["subagent_rules_hook.py"], "SubagentStart", {"agent_type": "Explore", "agent_id": "a1"}, "agent_type", True),
    ("memory_fields_hook", ["memory_fields_hook.py"], "PostToolUse", dict(WRITE, tool_response={}), "tool_input", True),
    ("error_recall_hook-PostToolUse", ["error_recall_hook.py"], "PostToolUse", dict(BASH, tool_response={"stdout": ""}), "tool_response", True),
    ("error_recall_hook-PostToolUseFailure", ["error_recall_hook.py"], "PostToolUseFailure", dict(BASH, error="boom"), "tool_name", True),
    ("stop_checks", ["stop_checks.py"], "Stop", {"stop_hook_active": False}, "stop_hook_active", True),
    ("learning_capture", ["learning_capture.py"], "SessionEnd", {"reason": "other"}, "reason", True),
    ("memory_sync_hook", ["memory_sync_hook.py"], "PostToolUse", dict(WRITE, tool_response={}), "tool_name", False),
    ("trust_flush", ["trust_flush.py"], "Stop", {"stop_hook_active": False}, "stop_hook_active", False),
    ("version_check", ["version_check.py"], "SessionStart", {"source": "startup"}, "source", False),
]


def _run(argv: List[str], doc: Dict[str, Any], env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / argv[0]), *argv[1:]],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _log(tmp_path: Path) -> List[Dict[str, Any]]:
    path = tmp_path / "data" / ic.LOG_NAME
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
    args = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "cwd": str(tmp_path),
            "transcript_path": str(tmp_path / "t.jsonl"), "prompt": ""}  # "" : the field is gone
    code, stdout, _ = dispatch.run_forked(dispatch.WARM_EVENT, dispatch.warm_event(args), env)
    assert code == 0
    assert NOTICE in json.loads(stdout)["systemMessage"]
    assert _log(tmp_path)[0]["missing"] == ["prompt"]


# ── the helper: complete input passes stdout through byte for byte ──


def _call_run(text: str, printed: str, monkeypatch) -> str:
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
def test_complete_input_keeps_the_output_byte_identical(tmp_path, monkeypatch, printed):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_event(tmp_path, "PreToolUse", **BASH))
    assert _call_run(text, printed, monkeypatch) == printed
    assert ic.apply(0, printed, text, "demo") is printed
    assert not (tmp_path / ic.LOG_NAME).exists()


def test_the_notice_joins_an_existing_system_message(tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    out = json.loads(_call_run(text, '{"systemMessage": "old", "x": 1}', monkeypatch))
    assert out["x"] == 1 and out["systemMessage"].startswith("old\nNOBLIVION:")


def test_a_non_zero_exit_keeps_the_notice_for_a_later_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    assert ic.apply(2, "", text, "demo") == ""
    assert NOTICE in ic.apply(0, "", text, "demo")


def test_no_data_dir_gives_no_notice_and_makes_no_dir(tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(gone))
    text = json.dumps(_rename(_event(tmp_path, "PreToolUse", **BASH), "tool_name", "tool"))
    assert ic.apply(0, "", text, "demo") == ""
    assert not gone.exists()


def test_input_that_is_not_an_object_counts_as_drift():
    assert ic.missing_fields("[1, 2]") == ("?", ["<json object>"], [])
    assert ic.missing_fields("") is None


def test_every_event_of_hooks_json_has_expected_fields():
    events = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    assert set(events) <= set(ic.EXPECTED)
