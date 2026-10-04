# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PostToolUse fields hook (``hooks/memory_fields_hook.py``).

The hook warns and never blocks: exit 0 on every input, silent unless a
feedback or project memory inside the memory folder has a format problem.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

HOOK = HOOKS / "memory_fields_hook.py"

mf = load_hook("memory_fields", "memory_fields_t_memory_fields_hook")

BARE = """---
name: feedback_x
description: a lesson
metadata:
  type: feedback
---

Body text.
"""
GOOD = {
    "rule": "Never run git stash in a shared checkout.",
    "apply": "Use a worktree.",
    "scope": "tool",
    "triggers": ["git stash"],
    "violates": r"\bgit\s+stash\b",
    "example_repeat": "git stash",
    "example_ok": "git worktree add ../wt -b x",
}


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited NOBLIVION_* var.
    The hook rebuilds the guard table: it must land in the tmp data dir."""
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
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(data / "guard-table.json"))
    return {"home": home, "data": data, "config": data / "config.json"}


def load_fields_hook():
    return load_hook("memory_fields_hook", "memory_fields_hook_t_memory_fields_hook")


def run_hook(stdin: str, folder: Path):
    env = dict(os.environ, NOBLIVION_MEMORY_DIR=str(folder))
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return r, time.time() - t0


def event(path, tool="Write", cwd="/"):
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {"file_path": str(path)},
            "cwd": cwd,
            "session_id": "s1",
        }
    )


@pytest.fixture
def mem(tmp_path):
    m = tmp_path / "mem"
    m.mkdir()
    return m


def test_warns_on_bare_feedback_memory(mem):
    p = mem / "feedback_x.md"
    p.write_text(BARE)
    r, secs = run_hook(event(p), mem)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    ctx = hso["additionalContext"]
    assert "rule is missing" in ctx and "scope is missing" in ctx
    assert "triggers: [" in ctx  # the one-line schema is carried
    assert secs < 2.0


def test_silent_on_clean_file(mem):
    p = mem / "feedback_x.md"
    p.write_text(mf.write_fields(BARE, GOOD))
    for tool in ("Write", "Edit", "MultiEdit"):
        r, _ = run_hook(event(p, tool), mem)
        assert r.returncode == 0 and r.stdout == ""


def test_a_feedback_write_rebuilds_the_guard_table_in_the_data_dir(mem, hook_env):
    p = mem / "feedback_x.md"
    p.write_text(mf.write_fields(BARE, GOOD))
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0 and r.stdout == ""
    assert (hook_env["data"] / "guard-table.json").is_file()
    assert not (hook_env["home"] / ".claude").exists()


def test_relative_path_resolved_against_cwd(mem):
    p = mem / "feedback_x.md"
    p.write_text(BARE)
    r, _ = run_hook(event("feedback_x.md", cwd=str(mem)), mem)
    assert "rule is missing" in r.stdout


def test_silent_for_non_memory_paths(mem):
    other = mem / "elsewhere"
    other.mkdir()
    (other / "feedback_x.md").write_text(BARE)
    (other / "project_x.md").write_text(BARE)
    (mem / "reference_x.md").write_text(BARE)
    (mem / "project_x.txt").write_text(BARE)
    (mem / "projectx.md").write_text(BARE)
    for path, tool in (
        (other / "feedback_x.md", "Write"),
        (other / "project_x.md", "Write"),
        (mem / "reference_x.md", "Write"),
        (mem / "project_x.txt", "Write"),
        (mem / "projectx.md", "Write"),
        (mem / "project_x.md", "Read"),
        (mem / "feedback_x.md", "Read"),
        (mem / "MEMORY.md", "Edit"),
    ):
        r, _ = run_hook(event(path, tool), mem)
        assert r.returncode == 0 and r.stdout == "", path


def test_fail_open_on_bad_input(mem):
    for stdin in (
        "not json",
        "",
        "[1, 2]",
        '{"tool_name": "Write", "tool_input": 5}',
        '{"tool_name": "Write", "tool_input": {"file_path": 7}}',
    ):
        r, _ = run_hook(stdin, mem)
        assert r.returncode == 0 and r.stdout == "", stdin


def test_fail_open_on_missing_file(mem):
    r, _ = run_hook(event(mem / "feedback_gone.md"), mem)
    assert r.returncode == 0 and r.stdout == ""


def test_guard_table_call_site_is_a_silent_noop_when_module_absent(monkeypatch):
    hook = load_fields_hook()
    monkeypatch.setattr(hook, "_load", lambda name: None)
    hook.rebuild_guard_table()  # must not raise


def test_guard_table_call_site_swallows_a_failing_rebuild(monkeypatch):
    hook = load_fields_hook()

    class Broken:
        @staticmethod
        def rebuild(folder):
            raise OSError("disk full")

    monkeypatch.setattr(hook, "_load", lambda name: Broken)
    hook.rebuild_guard_table()  # must not raise


def test_guard_table_call_site_calls_rebuild_when_present(mem, monkeypatch):
    hook = load_fields_hook()
    seen = []

    class Fake:
        @staticmethod
        def rebuild(folder):
            seen.append(folder)

    monkeypatch.setattr(hook, "_load", lambda name: Fake if name == "guard_table" else None)
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(mem))
    hook.rebuild_guard_table()
    assert seen == [[mem]]


def test_default_memory_folder_is_the_project_folder(hook_env, tmp_path):
    """NOBLIVION-31: the folder of the event's cwd, never the home folder's."""
    hook = load_fields_hook()
    home = hook_env["home"]
    project = tmp_path / "project"
    slug = "".join(c if c.isalnum() else "-" for c in str(project))
    assert hook.memory_dir(str(project)) == home / ".claude" / "projects" / slug / "memory"
    assert hook.memory_dir(None) is None


def test_warns_on_bad_complies(mem):
    p = mem / "feedback_x.md"
    p.write_text(mf.write_fields(BARE, dict(GOOD, complies=r"\bgit\s+stash\b")))
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0
    assert "complies matches its example_repeat" in r.stdout


def test_silent_on_clean_complies(mem):
    p = mem / "feedback_x.md"
    p.write_text(mf.write_fields(BARE, dict(GOOD, complies=r"\bgit\s+worktree\s+add\b")))
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0 and r.stdout == ""


# ── project memories: status: and project: ──────────────────────────────────
PROJECT_BARE = BARE.replace("feedback", "project")
ADD_LINE = 'add "status: open|closed|parked" and "project: <slug>" to the front matter'


def _ctx(r) -> str:
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_warns_on_a_project_memory_without_status_and_project(mem):
    p = mem / "project_x.md"
    p.write_text(PROJECT_BARE)
    for tool in ("Write", "Edit", "MultiEdit"):
        r, secs = run_hook(event(p, tool), mem)
        assert r.returncode == 0
        ctx = _ctx(r)
        assert ctx.startswith(
            "Memory file project_x.md does not meet the project memory format: "
            "status is missing; project is missing."
        )
        assert ADD_LINE in ctx
        assert "triggers: [" not in ctx  # no rule field is wrong: no rule schema
        assert secs < 2.0


@pytest.mark.parametrize(
    "lines",
    [
        "status: open\nproject: proj-12\nmetadata:\n  type: project",
        "metadata:\n  type: project\nstatus: closed\nproject: app-infra",
    ],
    ids=["top_before", "top_after"],
)
def test_silent_on_a_project_memory_with_both_fields(mem, lines):
    p = mem / "project_x.md"
    p.write_text(PROJECT_BARE.replace("metadata:\n  type: project", lines))
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize(
    "lines,problem",
    [
        ("status: done\nproject: global", "status 'done' is not one of open, closed, parked"),
        ("status: open\nproject: Proj_12", "project 'Proj_12' is not a lower-case slug"),
        ("status: open", "project is missing"),
        ("project: global", "status is missing"),
    ],
)
def test_warns_on_each_invalid_project_field(mem, lines, problem):
    p = mem / "project_x.md"
    p.write_text(PROJECT_BARE.replace("metadata:", lines + "\nmetadata:"))
    r, _ = run_hook(event(p), mem)
    ctx = _ctx(r)
    assert problem in ctx and ADD_LINE in ctx
    assert ctx.count(" is missing") + ctx.count(" is not ") == 1  # only that problem


def test_a_project_memory_with_a_bad_rule_field_gets_the_rule_schema(mem):
    p = mem / "project_x.md"
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:", "status: open\nproject: global\nscope: sometimes\nmetadata:"
        )
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert "scope 'sometimes' is not one of" in ctx and "triggers: [" in ctx
    assert ADD_LINE not in ctx


def test_a_feedback_memory_is_not_asked_for_status_or_project(mem):
    p = mem / "feedback_x.md"
    p.write_text(BARE)
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert ctx.startswith("Memory file feedback_x.md does not meet the rule format: ")
    assert "status" not in ctx.split("Fix the front matter")[0] and ADD_LINE not in ctx
    # a status that is present and wrong is reported, with the lines to write
    p.write_text(mf.write_fields(BARE, GOOD).replace("metadata:", "status: done\nmetadata:"))
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert "status 'done' is not one of open, closed, parked" in ctx and ADD_LINE in ctx


def test_a_write_of_every_memory_kind_rebuilds_the_guard_table(mem, monkeypatch, tmp_path):
    """NOBLIVION-50: the table reads ``violates`` from every memory file, so a
    write of any kind rebuilds it. An index or topic file is not in the table."""
    fh = load_fields_hook()
    calls = []
    monkeypatch.setattr(fh, "rebuild_guard_table", lambda cwd=None: calls.append(1))
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(mem))
    (mem / "project_x.md").write_text(PROJECT_BARE)
    (mem / "feedback_x.md").write_text(BARE)
    assert "status is missing" in fh.run(event(mem / "project_x.md"))
    assert calls == [1]
    assert "rule is missing" in fh.run(event(mem / "feedback_x.md"))
    assert calls == [1, 1]
    for name in ("user_x.md", "reference_x.md", "note.md"):
        (mem / name).write_text(BARE)
        assert fh.run(event(mem / name)) == ""  # no field check for these kinds
    assert calls == [1] * 5
    (mem / "sub").mkdir()
    outside = tmp_path / "feedback_elsewhere.md"
    for path in (mem / "MEMORY.md", mem / "topic_git.md", mem / "notes.txt", mem / "sub" / "a.md"):
        path.write_text(BARE)
        assert fh.run(event(path)) == ""
    outside.write_text(BARE)
    assert fh.run(event(outside)) == "" and fh.run(event(mem / "user_gone.md")) == ""
    assert calls == [1] * 5


def test_a_project_write_with_a_deny_rule_reaches_the_table(mem, hook_env):
    """End to end: the rule of a ``project_*`` file denies after the Write."""
    text = mf.write_fields(PROJECT_BARE, GOOD)
    (mem / "project_x.md").write_text(
        text.replace("metadata:", "status: open\nproject: global\nmetadata:")
    )
    r, _ = run_hook(event(mem / "project_x.md"), mem)
    assert r.returncode == 0 and r.stdout == ""
    table = json.loads((hook_env["data"] / "guard-table.json").read_text())
    assert [(e["id"], e["violates"]) for e in table["entries"]] == [("project_x", GOOD["violates"])]


NESTED_RULE = (
    'metadata:\n  rule: Do it.\n  apply: "So."\n  scope: tool\n  triggers:\n'
    "    - git push\n  type: project\nstatus: open\nproject: global"
)
MOVE_TAIL = (
    "out of the metadata: block to the top level of the front matter: no indent, "
    "directly above the closing ---."
)


def test_warns_when_rule_fields_sit_under_metadata_in_a_project_memory(mem):
    p = mem / "project_x.md"
    p.write_text(PROJECT_BARE.replace("metadata:\n  type: project", NESTED_RULE))
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert ctx.startswith(
        "Memory file project_x.md does not meet the project memory format: rule:, apply:, "
        "scope:, triggers: sit under metadata: and not at the top level. Fix the front matter: "
        'move the lines "rule:", "apply:", "scope:", "triggers:" (with the "- item" lines of '
        "triggers) " + MOVE_TAIL
    )
    assert "is missing" not in ctx and ADD_LINE not in ctx
    # a wrong nested value is reported beside the move
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:\n  type: project", NESTED_RULE.replace("scope: tool", "scope: sometimes")
        )
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert "scope 'sometimes' is not one of" in ctx and MOVE_TAIL in ctx
    # after the move (the library's own writer does it) the hook is silent
    p.write_text(
        mf.write_fields(PROJECT_BARE.replace("metadata:\n  type: project", NESTED_RULE), {})
    )
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0 and r.stdout == ""


def test_warns_when_status_and_project_sit_under_metadata(mem):
    p = mem / "project_x.md"
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:\n  type: project",
            "metadata:\n  type: project\n  status: parked\n  project: global",
        )
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert (
        "status:, project: sit under metadata: and not at the top level. Fix the front "
        'matter: move the lines "status:", "project:" ' + MOVE_TAIL
    ) in ctx
    assert "is missing" not in ctx and "- item" not in ctx
    # one nested line beside a top-level one: only that line is named
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:\n  type: project",
            "status: open\nmetadata:\n  type: project\n  project: global",
        )
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert (
        "project: sits under metadata: and not at the top level. Fix the front matter: "
        'move the line "project:" ' + MOVE_TAIL
    ) in ctx
    assert '"status:"' not in ctx


def test_warns_when_rule_fields_sit_under_metadata_in_a_feedback_memory(mem):
    p = mem / "feedback_x.md"
    p.write_text(
        BARE.replace("metadata:\n", 'metadata:\n  rule: Do it.\n  apply: "So."\n  scope: always\n')
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert ctx.startswith(
        "Memory file feedback_x.md does not meet the rule format: rule:, apply:, scope: sit "
        "under metadata: and not at the top level. Fix the front matter: move the lines "
        '"rule:", "apply:", "scope:" ' + MOVE_TAIL
    )
    assert "is missing" not in ctx


def test_a_missing_field_and_a_nested_field_are_both_named(mem):
    p = mem / "project_x.md"
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:\n  type: project", "metadata:\n  type: project\n  status: open"
        )
    )
    ctx = _ctx(run_hook(event(p), mem)[0])
    assert "project is missing; status: sits under metadata:" in ctx
    assert ADD_LINE in ctx and 'move the line "status:" ' + MOVE_TAIL in ctx


def test_an_indented_key_outside_metadata_is_no_problem(mem):
    p = mem / "project_x.md"
    p.write_text(
        PROJECT_BARE.replace(
            "metadata:\n  type: project",
            "status: open\nproject: global\nnotes:\n  rule: not a field\nmetadata:\n"
            "  type: project",
        )
    )
    r, _ = run_hook(event(p), mem)
    assert r.returncode == 0 and r.stdout == ""
