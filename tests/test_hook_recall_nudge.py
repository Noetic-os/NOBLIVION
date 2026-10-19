# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall nudge (``hooks/recall_nudge.py``, NOBLIVION-123).

What these tests hold:

1. Off by default: with no switch the hook prints nothing and writes no file.
2. Which calls match comes from the configuration. The defaults match ticket-create and
   ticket-comment tools and the PR merge commands; a configured list replaces them; a bad
   expression is skipped. A mention in quotes, a comment or a here-document does not match.
3. The window rule: the nudge fires when the last 10 tool calls hold no recall call and no
   earlier nudge. The 10th call back counts, the 11th does not.
4. The line is under 120 characters, whatever the input holds.
5. The log holds counts and no text of the call; shadow mode logs and prints nothing.
6. The recall call is logged with the moment of the last nudge while that nudge is in the window.

Fictional names only.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

nudge = load_hook("recall_nudge", "hooktest_recall_nudge")

RECALL = "mcp__plugin_noblivion_noblivion__noblivion_recall"
TICKET = "mcp__tickets__createJiraIssue"
COMMENT = "mcp__tickets__addCommentToJiraIssue"
SID = "sess-1"


# ── helpers ─────────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    return {
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_RECALL_CACHE_DIR": str(data / "cache"),
        "NOBLIVION_CONFIG": str(cfg),
        "NOBLIVION_RECALL_NUDGE": "1",
    }


def write_config(env, doc):
    Path(env["NOBLIVION_CONFIG"]).write_text(json.dumps(doc), encoding="utf-8")


def tool_line(ident, name, sidechain=False):
    rec = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": ident, "name": name, "input": {}}]},
    }
    return json.dumps(rec)


def transcript(tmp_path, names, extra=()):
    """A transcript file with one tool call per name, oldest first: ids c1, c2, ..."""
    lines = [tool_line(f"c{i + 1}", n) for i, n in enumerate(names)]
    lines.extend(extra)
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def payload(path, name=TICKET, tool_input=None, **extra):
    doc = {
        "hook_event_name": "PreToolUse",
        "session_id": SID,
        "transcript_path": path,
        "tool_name": name,
        "tool_input": tool_input if tool_input is not None else {"summary": "Fix the login page"},
        "tool_use_id": "cur",
    }
    doc.update(extra)
    return doc


def log_lines(env):
    path = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "nudge.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ── 1. off by default ───────────────────────────────────────────────────────


def test_off_by_default_prints_nothing_and_writes_nothing(tmp_path, env):
    del env["NOBLIVION_RECALL_NUDGE"]
    out = io.StringIO()
    p = payload(transcript(tmp_path, ["Read"] * 3))
    assert nudge.main(io.StringIO(json.dumps(p)), out, env) == 0
    assert out.getvalue() == ""
    assert not (Path(env["NOBLIVION_RECALL_CACHE_DIR"])).exists()


@pytest.mark.parametrize(
    "setting, expected",
    [
        ({}, "off"),
        ({"NOBLIVION_RECALL_NUDGE": "1"}, "on"),
        ({"NOBLIVION_RECALL_NUDGE": "0"}, "off"),
        ({"NOBLIVION_RECALL_NUDGE_SHADOW": "1"}, "shadow"),
        ({"NOBLIVION_RECALL_NUDGE": "1", "NOBLIVION_RECALL_NUDGE_SHADOW": "1"}, "on"),
        ({"NOBLIVION_RECALL_NUDGE": "banana"}, "off"),
    ],
)
def test_mode_from_env(env, setting, expected):
    del env["NOBLIVION_RECALL_NUDGE"]
    env.update(setting)
    assert nudge.mode(env) == expected


def test_mode_from_config_key(env):
    del env["NOBLIVION_RECALL_NUDGE"]
    write_config(env, {"recall": {"nudge": True}})
    assert nudge.mode(env) == "on"
    write_config(env, {"recall": {"nudge_shadow": True}})
    assert nudge.mode(env) == "shadow"
    # the env var wins over the config key
    env["NOBLIVION_RECALL_NUDGE"] = "0"
    write_config(env, {"recall": {"nudge": True}})
    assert nudge.mode(env) == "off"


# ── 2. matching ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "mcp__tickets__createJiraIssue",
        "mcp__tickets__addCommentToJiraIssue",
        "mcp__tracker__create_issue",
        "mcp__tracker__create_ticket",
        "mcp__tracker__add_issue_comment",
        "mcp__tracker__add_comment",
        "mcp__claude_ai_Tracker__createIssue",
    ],
)
def test_default_tools_match(env, name):
    assert nudge.match_tool(name, nudge.tool_patterns(env))


@pytest.mark.parametrize(
    "name",
    [
        "mcp__tickets__getJiraIssue",
        "mcp__tickets__editJiraIssue",
        "mcp__tickets__transitionJiraIssue",
        "mcp__tickets__searchJiraIssuesUsingJql",
        "mcp__tickets__createIssueLink",
        "mcp__tickets__addWorklogToJiraIssue",
        "mcp__docs__createConfluencePage",
        "mcp__plugin_noblivion_noblivion__noblivion_remember",
        "createJiraIssue",  # not an MCP tool name
        "Bash",
    ],
)
def test_default_tools_do_not_match(env, name):
    assert not nudge.match_tool(name, nudge.tool_patterns(env))


def test_configured_tool_list_replaces_the_default(env):
    write_config(env, {"recall": {"nudge_tools": ["^mcp__shop__open_case$", "(unclosed"]}})
    pats = nudge.tool_patterns(env)
    assert len(pats) == 1  # the bad expression is skipped
    assert nudge.match_tool("mcp__shop__open_case", pats)
    assert not nudge.match_tool(TICKET, pats)


def test_configured_list_with_only_bad_items_matches_nothing(env):
    write_config(env, {"recall": {"nudge_tools": ["(bad"]}})
    assert nudge.tool_patterns(env) == []


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 12 --squash --delete-branch",
        "cd /work/repo && gh pr merge 12",
        "GH_TOKEN=x gh pr merge 12",
        "env GH_TOKEN=x gh pr merge 12",
        "git push && gh pr merge 12 --squash",
        "glab mr merge 7",
        "gh pr merge",
    ],
)
def test_default_commands_match(env, command):
    assert nudge.match_command(command, nudge.command_patterns(env)) is not None


@pytest.mark.parametrize(
    "command",
    [
        'echo "gh pr merge 12"',
        "grep -n 'gh pr merge' docs/guide.md",
        "gh pr view 12",
        "gh pr checks 12 --watch",
        "git merge main",
        "mygh pr merge 12",
        "# gh pr merge 12",
        "cat <<'EOF'\ngh pr merge 12\nEOF",
        "git commit -m 'never run gh pr merge by hand'",
        "",
    ],
)
def test_default_commands_do_not_match(env, command):
    assert nudge.match_command(command, nudge.command_patterns(env)) is None


def test_configured_command_pattern_for_a_merge_wrapper(env):
    write_config(env, {"recall": {"nudge_commands": [r"^tools/merge-pr\b"]}})
    pats = nudge.command_patterns(env)
    hit = nudge.match_command("tools/merge-pr 12 -- --squash", pats)
    assert hit is not None
    assert nudge.match_command("gh pr merge 12", pats) is None


def test_topic_comes_from_the_call_and_is_clean(env):
    hit = nudge.match_command("gh pr merge 1234 --squash", nudge.command_patterns(env))
    assert nudge.topic_for("command", {}, hit[1], hit[2]) == "1234"
    hit = nudge.match_command("gh pr merge --squash 99", nudge.command_patterns(env))
    assert nudge.topic_for("command", {}, hit[1], hit[2]) == "99"
    hit = nudge.match_command("gh pr merge", nudge.command_patterns(env))
    assert nudge.topic_for("command", {}, hit[1], hit[2]) == "this merge"
    assert nudge.topic_for("tool", {"issueIdOrKey": "ABC-12"}) == "ABC-12"
    assert nudge.topic_for("tool", {"summary": 'Fix "login"; rm -rf $HOME\n`x`'}) == (
        "Fix login rm -rf HOME x"
    )
    assert nudge.topic_for("tool", {}) == "this ticket"


# ── 4. the line ─────────────────────────────────────────────────────────────


def test_line_is_under_120_characters_for_any_topic():
    worst = nudge.topic_for("tool", {"summary": "w" * 5000})
    line = nudge.nudge_line(worst)
    assert len(worst) <= nudge.TOPIC_MAX
    assert len(line) <= nudge.LINE_MAX == 120
    assert line.startswith("Recall first: call noblivion_recall")
    assert len(nudge.nudge_line("x")) < 70


# ── 3. the window ───────────────────────────────────────────────────────────


def test_window_verdict_boundary():
    # newest first: the recall call is the 10th call back, then the 11th
    tenth = [(f"i{i}", "Read") for i in range(9)] + [("r", RECALL)]
    eleventh = [(f"i{i}", "Read") for i in range(10)] + [("r", RECALL)]
    assert nudge.window_verdict(tenth) == "recent_recall"
    assert nudge.window_verdict(eleventh) == "fire"
    assert nudge.window_verdict([]) == "fire"
    # also a plain-named recall tool of another host counts
    assert nudge.window_verdict([("a", "noblivion_recall")]) == "recent_recall"


def test_window_verdict_cooldown_uses_the_last_nudged_call():
    window = [(f"i{i}", "Read") for i in range(9)] + [("n", TICKET)]
    assert nudge.window_verdict(window, "n") == "cooldown"
    assert nudge.window_verdict(window, "gone") == "fire"
    assert nudge.window_verdict(window, "") == "fire"
    # a nudged call that fell out of the 10 calls does not count
    assert nudge.window_verdict(window + [("x", "Read")], "x") == "fire"


def test_recent_calls_reads_the_newest_ten_and_skips_the_current_call(tmp_path):
    names = ["Read"] * 15
    path = transcript(tmp_path, names)
    calls = nudge.recent_calls(path, "")
    assert [i for i, _n in calls] == [f"c{k}" for k in range(15, 5, -1)]
    calls = nudge.recent_calls(path, "c15")
    assert calls[0][0] == "c14" and len(calls) == 10


def test_recent_calls_ignores_sidechain_and_other_records(tmp_path):
    lines = [
        tool_line("s1", RECALL, sidechain=True),
        json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_use", "id": "u", "name": RECALL}]},
            }
        ),
        "not json but has tool_use in it",
        tool_line("c1", "Read"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert nudge.recent_calls(str(path)) == [("c1", "Read")]


def test_recent_calls_reads_parallel_calls_of_one_message(tmp_path):
    rec = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                {"type": "text", "text": "x"},
                {"type": "tool_use", "id": "b", "name": "Grep", "input": {}},
            ]
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    assert nudge.recent_calls(str(path)) == [("b", "Grep"), ("a", "Read")]


def test_recent_calls_widens_the_tail_and_gives_up_when_the_window_is_unknown(
    tmp_path, monkeypatch
):
    # ten calls early in the file, then one very long record: the first tail misses them
    big = json.dumps({"type": "user", "message": {"content": "x" * 3000}})
    lines = [tool_line(f"c{i}", "Read") for i in range(10)] + [big] + [tool_line("n", "Grep")]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(nudge, "TAIL_STEPS", (1000, 100_000))
    assert len(nudge.recent_calls(str(path))) == 10  # the second step reaches them
    monkeypatch.setattr(nudge, "TAIL_STEPS", (1000,))
    assert nudge.recent_calls(str(path)) is None  # unknown: stay silent
    assert nudge.recent_calls(str(tmp_path / "missing.jsonl")) is None


def test_recent_calls_of_a_short_file_is_complete(tmp_path):
    path = transcript(tmp_path, ["Read", "Grep"])
    assert nudge.recent_calls(path) == [("c2", "Grep"), ("c1", "Read")]
    assert nudge.recent_calls(transcript(tmp_path, [])) == []


# ── decide: the nudge itself ────────────────────────────────────────────────


def test_fires_before_a_ticket_tool_when_nothing_was_recalled(tmp_path, env):
    out = nudge.decide(payload(transcript(tmp_path, ["Read"] * 3)), env)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso  # context only: never allows or denies
    assert "updatedInput" not in hso
    assert hso["additionalContext"] == (
        'Recall first: call noblivion_recall with query "Fix the login page", then act.'
    )
    assert len(hso["additionalContext"]) <= 120


def test_fires_before_a_merge_command(tmp_path, env):
    p = payload(
        transcript(tmp_path, ["Read"]),
        name="Bash",
        tool_input={"command": "cd /w && gh pr merge 41 --squash"},
    )
    out = nudge.decide(p, env)
    assert '"41"' in out["hookSpecificOutput"]["additionalContext"]


def test_a_recall_call_in_the_last_10_calls_silences_it(tmp_path, env):
    path = transcript(tmp_path, [RECALL] + ["Read"] * 9)
    assert nudge.decide(payload(path), env) is None
    assert log_lines(env)[-1].endswith("result=skip_recent_recall")


def test_a_recall_call_11_calls_back_does_not_silence_it(tmp_path, env):
    path = transcript(tmp_path, [RECALL] + ["Read"] * 10)
    assert nudge.decide(payload(path), env) is not None


def test_the_current_call_in_the_transcript_is_not_counted(tmp_path, env):
    # the current call is already in the file as the newest; it must not take a place in the window
    names = [RECALL] + ["Read"] * 9 + [TICKET]
    path = transcript(tmp_path, names)  # the recall is the 10th call before the current one
    assert nudge.decide(payload(path, tool_use_id="c11"), env) is None
    names = [RECALL] + ["Read"] * 10 + [TICKET]
    path = transcript(tmp_path, names)  # the 11th
    assert nudge.decide(payload(path, tool_use_id="c12"), env) is not None


def test_a_burst_gets_one_line_and_the_next_after_ten_calls(tmp_path, env):
    first = nudge.decide(payload(transcript(tmp_path, ["Read"]), tool_use_id="t1"), env)
    assert first is not None
    # the nudged call t1 is in the window of the next matching call
    p = transcript(tmp_path, ["Read", TICKET, "Read", "Read"])  # c2 is the nudged call id?
    state = json.loads((Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "nudge-state.json").read_text())
    assert state[SID]["id"] == "t1" and state[SID]["moment"] == "tool"
    lines = [tool_line("t1", TICKET)] + [tool_line(f"r{i}", "Read") for i in range(8)]
    Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert nudge.decide(payload(p, name=COMMENT, tool_use_id="t2"), env) is None
    assert log_lines(env)[-1].endswith("result=skip_cooldown")
    lines = [tool_line("t1", TICKET)] + [tool_line(f"r{i}", "Read") for i in range(10)]
    Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert nudge.decide(payload(p, name=COMMENT, tool_use_id="t3"), env) is not None


def test_state_is_kept_per_session(tmp_path, env):
    path = transcript(tmp_path, ["Read"])
    assert nudge.decide(payload(path, tool_use_id="a"), env) is not None
    assert nudge.decide(payload(path, tool_use_id="b", session_id="sess-2"), env) is not None


def test_state_keeps_only_the_newest_sessions(tmp_path, env):
    cache = Path(env["NOBLIVION_RECALL_CACHE_DIR"])
    for i in range(nudge.STATE_SESSIONS + 5):
        nudge.save_state(cache, env, f"s{i}", {"id": "x", "moment": "tool"})
    state = nudge.load_state(cache)
    assert len(state) == nudge.STATE_SESSIONS
    assert "s0" not in state and f"s{nudge.STATE_SESSIONS + 4}" in state


@pytest.mark.parametrize(
    "mutate",
    [
        {"agent_id": "agent-1"},  # a subagent: its transcript is not the parent's
        {"session_id": "bad id/../x"},
        {"session_id": None},
        {"tool_input": "not a dict"},
        {"tool_name": "Read"},
        {"tool_name": "mcp__tickets__getJiraIssue"},
        {"tool_name": RECALL},
        {"tool_name": "mcp__plugin_noblivion_noblivion__noblivion_remember"},
    ],
)
def test_calls_that_get_no_line(tmp_path, env, mutate):
    p = payload(transcript(tmp_path, ["Read"]))
    p.update(mutate)
    assert nudge.decide(p, env) is None


def test_no_transcript_means_no_line_and_a_log_line(tmp_path, env):
    p = payload(str(tmp_path / "missing.jsonl"))
    assert nudge.decide(p, env) is None
    assert log_lines(env)[-1].endswith("moment=tool result=skip_no_window")
    p = payload("")
    assert nudge.decide(p, env) is None


def test_a_non_matching_bash_command_logs_nothing(tmp_path, env):
    p = payload(transcript(tmp_path, ["Read"]), name="Bash", tool_input={"command": "ls -la"})
    assert nudge.decide(p, env) is None
    assert log_lines(env) == []


# ── 5. log and shadow ───────────────────────────────────────────────────────


def test_log_holds_counts_and_no_call_text(tmp_path, env):
    p = payload(
        transcript(tmp_path, ["Read"]),
        tool_input={"summary": "SECRET-TOPIC-TEXT", "issueIdOrKey": "KEY-77"},
    )
    nudge.decide(p, env)
    (line,) = log_lines(env)
    assert "SECRET-TOPIC-TEXT" not in line and "KEY-77" not in line
    assert f"session={SID} event=nudge moment=tool result=fired chars=" in line
    assert line.split()[0].startswith("20")


def test_shadow_logs_what_it_would_do_and_prints_nothing(tmp_path, env):
    del env["NOBLIVION_RECALL_NUDGE"]
    env["NOBLIVION_RECALL_NUDGE_SHADOW"] = "1"
    out = io.StringIO()
    p = payload(transcript(tmp_path, ["Read"]))
    assert nudge.main(io.StringIO(json.dumps(p)), out, env) == 0
    assert out.getvalue() == ""
    assert "result=would_fire" in log_lines(env)[-1]
    # the shadow run keeps the cooldown, so its counts match a live run
    state = nudge.load_state(Path(env["NOBLIVION_RECALL_CACHE_DIR"]))
    assert state[SID]["id"] == "cur"


def test_recall_log_is_not_touched(tmp_path, env):
    nudge.decide(payload(transcript(tmp_path, ["Read"])), env)
    assert not (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log").exists()


def test_log_rotates_at_the_size_limit(tmp_path, env, monkeypatch):
    monkeypatch.setattr(nudge, "LOG_MAX_BYTES", 50)
    cache = Path(env["NOBLIVION_RECALL_CACHE_DIR"])
    for _ in range(3):
        nudge.log_event(cache, env, SID, event="nudge", moment="tool", result="fired")
    assert (cache / "nudge.log.1").exists() and (cache / "nudge.log").exists()


def test_a_deleted_data_dir_is_not_made_again(tmp_path, env):
    import shutil

    shutil.rmtree(env["NOBLIVION_DATA_DIR"])
    out = io.StringIO()
    p = payload(transcript(tmp_path, ["Read"]))
    assert nudge.main(io.StringIO(json.dumps(p)), out, env) == 0
    assert not Path(env["NOBLIVION_DATA_DIR"]).exists()


# ── 6. the recall call ──────────────────────────────────────────────────────


def test_recall_call_is_logged_with_the_moment_of_the_nudge(tmp_path, env):
    nudge.decide(payload(transcript(tmp_path, ["Read"]), tool_use_id="t1"), env)
    lines = [tool_line("t1", TICKET), tool_line("x1", "Read"), tool_line("cur2", RECALL)]
    path = tmp_path / "t2.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    call = payload(str(path), name=RECALL, tool_input={"query": "q"}, tool_use_id="cur2")
    nudge.note_recall(call, env)
    assert log_lines(env)[-1].endswith("event=recall_call fetch=0 after=tool")
    # a fetch by id, and a recall far from the nudge
    lines = [tool_line("t1", TICKET)] + [tool_line(f"x{i}", "Read") for i in range(10)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    call = payload(str(path), name=RECALL, tool_input={"fetch_id": 5}, tool_use_id="cur3")
    nudge.note_recall(call, env)
    assert log_lines(env)[-1].endswith("event=recall_call fetch=1 after=-")


def test_recall_call_is_not_logged_when_both_switches_are_off(tmp_path, env):
    del env["NOBLIVION_RECALL_NUDGE"]
    nudge.note_recall(payload(transcript(tmp_path, ["Read"]), name=RECALL), env)
    assert log_lines(env) == []


def test_session_hook_logs_the_recall_call(tmp_path, env, monkeypatch):
    session_hook = load_hook("mcp_session_hook", "hooktest_nudge_session_hook")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    call = payload(
        transcript(tmp_path, ["Read"]), name=RECALL, tool_input={"query": "q"}, tool_use_id="z"
    )
    out = io.StringIO()
    assert session_hook.main(io.StringIO(json.dumps(call)), out) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["updatedInput"]["session_key"] == SID
    assert "event=recall_call" in log_lines(env)[-1]


# ── fail open, and the script as the plugin runs it ─────────────────────────


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": 5}'])
def test_main_fails_open(env, raw):
    out = io.StringIO()
    assert nudge.main(io.StringIO(raw), out, env) == 0
    assert out.getvalue() == ""


def test_script_prints_one_json_object(tmp_path, env):
    p = payload(transcript(tmp_path, ["Read"]))
    run_env = {**os.environ, **env}
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "recall_nudge.py")],
        input=json.dumps(p),
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
    )
    assert proc.returncode == 0 and proc.stderr == ""
    doc = json.loads(proc.stdout)
    assert doc["hookSpecificOutput"]["additionalContext"].startswith("Recall first:")
    off = {k: v for k, v in run_env.items() if k != "NOBLIVION_RECALL_NUDGE"}
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "recall_nudge.py")],
        input=json.dumps(p),
        capture_output=True,
        text=True,
        env=off,
        timeout=30,
    )
    assert proc.returncode == 0 and proc.stdout == ""


# ── the branch as the merge topic ───────────────────────────────────────────


def _repo(tmp_path, head):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    sub = tmp_path / "repo" / "src" / "deep"
    sub.mkdir(parents=True)
    return sub


def test_branch_for_reads_head_without_git(tmp_path):
    sub = _repo(tmp_path, "ref: refs/heads/feature/login-fix")
    assert nudge.branch_for(str(sub)) == "feature/login-fix"
    assert nudge.branch_for(str(tmp_path)) == ""  # no repository


def test_branch_for_a_detached_head_and_bad_input(tmp_path):
    sub = _repo(tmp_path, "0123456789abcdef0123456789abcdef01234567")
    assert nudge.branch_for(str(sub)) == ""
    assert nudge.branch_for(None) in ("", nudge.branch_for("None"))
    assert nudge.branch_for("/no/such/folder") == ""


def test_branch_for_a_linked_worktree(tmp_path):
    main = tmp_path / "main" / ".git"
    (main / "worktrees" / "wt1").mkdir(parents=True)
    (main / "worktrees" / "wt1" / "HEAD").write_text("ref: refs/heads/topic-9\n", encoding="utf-8")
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/worktrees/wt1\n", encoding="utf-8")
    assert nudge.branch_for(str(wt)) == "topic-9"


def test_merge_topic_uses_the_branch_when_the_argument_is_only_a_number(env):
    pats = nudge.command_patterns(env)
    num = nudge.match_command("gh pr merge 41 --squash", pats)
    assert nudge.topic_for("command", {}, num[1], num[2], "superpower-123") == "superpower-123"
    bare = nudge.match_command("gh pr merge --squash", pats)
    assert nudge.topic_for("command", {}, bare[1], bare[2], "superpower-123") == "superpower-123"
    named = nudge.match_command("gh pr merge other-branch", pats)
    assert nudge.topic_for("command", {}, named[1], named[2], "superpower-123") == "other-branch"
    assert nudge.topic_for("command", {}, num[1], num[2], "") == "41"


def test_decide_puts_the_branch_of_cwd_in_the_merge_line(tmp_path, env):
    sub = _repo(tmp_path, "ref: refs/heads/superpower-123")
    p = payload(
        transcript(tmp_path, ["Read"]),
        name="Bash",
        tool_input={"command": "gh pr merge 41 --squash"},
        cwd=str(sub),
    )
    line = nudge.decide(p, env)["hookSpecificOutput"]["additionalContext"]
    assert 'query "superpower-123"' in line and len(line) <= 120
