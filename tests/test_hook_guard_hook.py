# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PreToolUse guard hook (``hooks/guard_hook.py``).

What these tests hold:

1. A ``violates:`` HIT DENIES with the exact PreToolUse schema and the reason
   ``<rule>\\nApply: <apply>\\nMemory: <id>``; several hits list the others.
2. NO ALLOW AFTER A BUDGET (WI-3c): a retry of a denied command is denied
   again, every time. From the third deny of one memory in one agent of a
   session the reason starts with "Denied N times". Agents and sessions do not
   share the counts. The only way through is the override marker (9).
3. THE ROWS LEG: a ``triggers:`` hit with no violation allows and shows at
   most 3 rows, most specific first; one memory's row at most 3 times a session.
4. EDIT, WRITE, MULTIEDIT: ``glob:`` rows on the file path; never a deny; the
   content of a Write never reaches the log.
5. FAIL OPEN: a missing table, bad stdin, a broken regex entry or an exception
   print nothing and exit 0. A timeout allows the call. It prints one line
   for the user when the deny check was not done, else nothing (13).
6. THE LOG: one JSON line per deny, override and row set.
7. STATE PRUNING: a new session removes state files older than 7 days.
8. WRAPPERS: ``git -C``, ``env``, ``sudo``, ``timeout`` do not hide a guarded
   command, and a heredoc body does not deny (the WI-2 ``match``).
9. THE OVERRIDE MARKER: a top-level shell comment ``# guard-ok: <reason>``
   allows a hit at any count and logs ``override`` with the redacted reason.
   A marker in quotes, a heredoc body or an executed string does not count,
   and a marker with no reason does not count.
10. EVIDENCE BEFORE AN OVERRIDE (WI-3d): for a memory with ``complies`` and
   ``run_first`` the marker is refused until a PostToolUseFailure event
   ("Exit code <n>", not interrupted) showed a failed run of that command in
   the same agent of the session. The deny names the command to run first.
   A memory without ``complies`` keeps the plain override.
13. A CHANGED MEMORY FOLDER (NOBLIVION-50): a rule that a shell command
   deleted, edited, moved or added is read at the next call, for every kind
   of memory file. An ordinary command gets no deny and no message.

Every test points NOBLIVION_GUARD_TABLE, NOBLIVION_GUARD_STATE_DIR and
NOBLIVION_GUARD_LOG at tmp paths (13: no table override, the table is in the
tmp data dir). No test reads or writes a live file.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hookload import HOOKS, load_hook_with_config

HOOK = HOOKS / "guard_hook.py"

# An empty config at import: the built-in word lists only, never the user's
# own config file.
gh = load_hook_with_config("guard_hook", {}, "guard_hook_t_hook")


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited settings."""
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
    return {"home": home, "data": data}


FORCE = r"git\s+push\s+.*--force(?!-with-lease)"
OVERRIDE = (
    "Override only if you are sure this rule does not apply here: end the command with "
    "this shell comment: # guard-ok: <short reason>"
)
STASH = r"^git\s+stash\s+pop\s*$"


def entry(mid, rule=None, apply="", violates="", triggers=(), scope="tool"):
    return {
        "id": mid,
        "rule": f"rule of {mid}." if rule is None else rule,
        "apply": apply or f"apply of {mid}",
        "scope": scope,
        "triggers": list(triggers),
        "violates": violates,
    }


def ungraded(env):
    """The env with the weak-trigger grading off (WI-3g/WI-3h). The fixture
    triggers of the row-text tests are generic (``git push github main``,
    ``make deploy-all``, ``**/*.py``), so graded they give no row; these tests
    are about the row text and the bounds, not the grading."""
    return dict(env, NOBLIVION_GUARD_WEAK_SHARE="0")


BASE = [
    entry(
        "feedback_no_force",
        rule="Never force-push.",
        apply="Use a new branch instead.",
        violates=FORCE,
        triggers=["git push --force"],
    ),
    entry(
        "feedback_no_stash_pop",
        rule="Never pop the shared stash.",
        apply="git stash apply",
        violates=STASH,
        triggers=["git stash"],
    ),
    entry("feedback_push_notes", triggers=["git push", "git push github"]),
    entry("feedback_push_main", triggers=["git push github main"]),
    entry("feedback_push_generic", triggers=["git push", "git"]),  # broad only
    entry("feedback_pytest", triggers=["python -m pytest", "pytest"]),
    entry("feedback_python", triggers=["python"]),
    entry("feedback_py_edit", triggers=["glob:**/*.py"], scope="file"),
    entry(
        "feedback_merge_gate",
        triggers=["glob:tools/merge_gate.py"],
        scope="file",
        violates=r"merge_gate",
    ),
    entry("feedback_tool_only", triggers=["tool:Bash", "tool:Edit", "phrase:hello"]),
    entry("feedback_no_rule", rule="", triggers=["lsblk"]),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"version": 1, "entries": [dict(e) for e in BASE]}))
    e = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    # the default paths must never be used in a test
    monkeypatch.setattr(gh, "DEFAULT_LOG", tmp_path / "must-not-exist-log")
    monkeypatch.setattr(gh, "DEFAULT_STATE", tmp_path / "must-not-exist-state")
    yield e
    assert not (tmp_path / "must-not-exist-log").exists()
    assert not (tmp_path / "must-not-exist-state").exists()


def bash(cmd, sid="s1", agent=None):
    ev = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"}
    if agent:
        ev["agent_id"] = agent
    return ev


def call(env, event, raw=None, mod=None) -> Any:
    """In process: the parsed output, or None when the hook printed nothing.
    Typed Any: a test that expects output subscripts it directly, and a None
    there fails the test just as an assert would. ``mod`` is another loaded
    copy of the hook (default ``gh``)."""
    out = io.StringIO()
    rc = (mod or gh).main(
        stdin=io.StringIO(raw if raw is not None else json.dumps(event)), stdout=out, environ=env
    )
    assert rc == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def log(env):
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# 1. deny ----------------------------------------------------------------
def test_deny_subprocess_exact_schema(env):
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(bash("git push --force origin x")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert r.returncode == 0 and r.stderr == ""
    out = json.loads(r.stdout)
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Never force-push.\nApply: Use a new branch instead.\n"
            "Memory: feedback_no_force\n" + OVERRIDE,
        }
    }


def test_several_hits_deny_on_first_and_list_others(env):
    out = call(env, bash("git stash pop; git push --force origin x"))
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert reason.startswith("Never force-push.\nApply: ")
    assert "Also matches: feedback_no_stash_pop (Never pop the shared stash.)" in reason
    assert "additionalContext" not in out


def test_compliant_command_is_not_denied(env):
    out = call(env, bash("git push --force-with-lease origin x"))
    assert out is None or "permissionDecision" not in out


# 2. budget ----------------------------------------------------------------
def test_budget_spent_retry_is_still_denied_and_escalates(env):
    """Arm G trap t06 (RESULT-G.md): the model retried after 2 denies and the
    old budget let the 3rd attempt through (runs s2, s7). Now every retry is
    denied, and from the 3rd deny the reason says so first."""
    outs = [call(env, bash("git push --force o x")) for _ in range(5)]
    assert [o.get("permissionDecision") for o in outs] == ["deny"] * 5
    reasons = [o["permissionDecisionReason"] for o in outs]
    assert all(r.startswith("Never force-push.") for r in reasons[:2])
    for n, r in enumerate(reasons[2:], start=3):
        assert r.startswith(f"Denied {n} times in this session by this rule.")
        assert "Never force-push." in r and r.endswith(OVERRIDE)
    assert all("additionalContext" not in o for o in outs)
    assert [x["decision"] for x in log(env)] == ["deny"] * 5
    assert [x["counts"] for x in log(env)] == [{"feedback_no_force": n} for n in range(1, 6)]


def test_budget_is_per_session_and_per_memory(env):
    for _ in range(2):
        assert call(env, bash("git push --force o x", "a"))["permissionDecision"] == "deny"
    assert call(env, bash("git push --force o x", "b"))["permissionDecision"] == "deny"
    assert call(env, bash("git stash pop", "a"))["permissionDecision"] == "deny"


def test_budget_is_per_agent_within_a_session(env):
    """Review finding F2: a subagent sends the parent's session_id plus its own
    agent_id. Two denies in one agent must not spend another agent's budget."""
    for agent in ("agent-a", "agent-a"):
        assert call(env, bash("git push --force o x", "s1", agent))["permissionDecision"] == "deny"
    third = call(env, bash("git push --force o x", "s1", "agent-a"))
    assert third["permissionDecisionReason"].startswith("Denied 3 times")  # agent-a escalates
    for agent in ("agent-b", None):  # agent-b and the main agent do not
        out = call(env, bash("git push --force o x", "s1", agent))
        assert out["permissionDecisionReason"].startswith("Never force-push.")
    assert [r["agent_id"] for r in log(env) if r["decision"] == "deny"] == [
        "agent-a",
        "agent-a",
        "agent-a",
        "agent-b",
        "main",
    ]


def test_row_repeat_cap_is_per_agent(env):
    for _ in range(3):
        call(env, bash("python -m pytest -q", "s1", "agent-a"))
    assert call(env, bash("python -m pytest -q", "s1", "agent-a")) is None
    assert (
        "feedback_pytest"
        in call(env, bash("python -m pytest -q", "s1", "agent-b"))["additionalContext"]
    )


def test_budget_spent_memory_is_listed_when_another_denies(env):
    for _ in range(2):
        call(env, bash("git push --force o x"))
    out = call(env, bash("git push --force o x && git stash pop"))
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("Never pop the shared stash.")
    assert "feedback_no_force" in out["permissionDecisionReason"]


# 3. rows ------------------------------------------------------------------
def test_rows_on_trigger_without_violation(env):
    env = ungraded(env)
    out = call(env, bash("git push github main"))
    assert "permissionDecision" not in out
    ctx = out["additionalContext"]
    lines = ctx.splitlines()
    assert lines[0] == gh.ROWS_HEADER
    # most specific trigger first: "git push github main" > "git push github";
    # "git push" and "git" are broad (they fire on an everyday command): no row
    # WI-3f: each row is the id head, then Rule and Apply whole (no table
    # source here, so no body), never a "[memory <id>]" tag.
    assert lines[1] == "- Memory feedback_push_main (rule and fix in full):"
    assert lines[2:4] == [
        "  Rule: rule of feedback_push_main.",
        "  Apply: apply of feedback_push_main",
    ]
    assert lines[4] == "- Memory feedback_push_notes (rule and fix in full):"
    assert len(lines) == 7 and "[memory " not in ctx
    assert log(env)[-1]["decision"] == "rows"
    assert log(env)[-1]["ids"] == ["feedback_push_main", "feedback_push_notes"]


def test_broad_trigger_gives_no_row(env):
    assert gh.broad_trigger("git push") and gh.broad_trigger("tail")
    assert not gh.broad_trigger("git push github") and not gh.broad_trigger("python -m pytest")
    assert call(env, bash("git push origin x")) is None


def test_flag_only_and_keyword_triggers_are_broad():
    # measured live 2026-09-29: `grep -v`, `grep -n`, `then` showed off-topic rows on most calls
    for t in (
        "grep -v",
        "grep -rn",
        "tail -1",
        "git -C",
        "journalctl -u",
        "then",
        "fi",
        "sed -n",
        "git status --short",
        "python",
        "kill",
        "mv",
        "git add -A",
    ):
        assert gh.broad_trigger(t), t
    for t in (
        "git worktree remove",
        "journalctl -u app-worker",
        "gh pr merge",
        "set -e",
        "pgrep -f pat",
    ):
        assert not gh.flags_only_trigger(t), t
    for t in ("git rev-parse --short", "git worktree remove", "docker run --rm --entrypoint"):
        assert not gh.broad_trigger(t), t


def test_rows_prefix_needs_a_word_boundary_and_skip_typed_triggers(env):
    assert call(env, bash("pythonx run")) is None
    assert call(env, bash("echo hello")) is None  # tool: and phrase: never fire
    assert call(env, bash("lsblk -f")) is None  # an entry without a rule is not shown


def test_rows_see_through_git_c_and_wrappers(env):
    env = ungraded(env)
    out = call(env, bash("sudo env A=1 git -C /repo push github main"))
    assert "- Memory feedback_push_notes (" in out["additionalContext"]


def test_row_repeat_cap(env):
    shown = []
    for _ in range(5):
        out = call(env, bash("python -m pytest -q"))
        shown.append(out["additionalContext"].count("- Memory feedback_pytest (") if out else 0)
    assert shown == [1, 1, 1, 0, 0]


def test_rows_capped_at_three_and_short(env, tmp_path):
    # rule and apply at their schema caps (160 and 400): three rows fit
    many = [
        entry(f"feedback_m{i}", rule="R" * 160, apply="A" * 400, triggers=["make deploy-all"])
        for i in range(6)
    ]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": many}))
    env = ungraded(env)
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    assert ctx.count("- Memory ") == 3 and "[memory " not in ctx
    assert len(ctx) <= gh.ROWS_MAX_CHARS


def test_rows_over_the_show_bound_drop_the_last_row(env, tmp_path):
    # fields far over the schema caps: the show bound holds, the first row stays
    many = [
        entry(
            f"feedback_m{i}",
            rule="rule word " * 80,
            apply="apply text " * 160,
            triggers=["make deploy-all"],
        )
        for i in range(6)
    ]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": many}))
    env = ungraded(env)
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    assert 1 <= ctx.count("- Memory ") < 3
    assert len(ctx) <= gh.ROWS_MAX_CHARS
    assert log(env)[-1]["ids"] == [f"feedback_m{i}" for i in range(ctx.count("- Memory "))]


def test_session_row_budget(env):
    env = ungraded(env)
    many = [
        entry(f"feedback_b{i}", rule="R" * 100, apply="A" * 100, triggers=[f"tool{i} run"])
        for i in range(40)
    ]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": many}))
    e = dict(env, NOBLIVION_GUARD_ROWS_SESSION_CHARS="1000")
    cmd = " ; ".join(f"tool{i} run" for i in range(40))
    sizes = []
    for _ in range(4):
        out = call(e, bash(cmd))
        sizes.append(out["additionalContext"].count("- Memory ") if out else 0)
    assert sizes[0] == 3 and sizes[-1] == 0
    assert [x["decision"] for x in log(e)] == ["rows"] * sum(1 for n in sizes if n)
    st = json.loads(gh._state_file(e, "s1").read_text())
    assert 0 < st["row_chars"] <= 1000
    # another session has its own budget
    assert call(e, bash(cmd, "other"))["additionalContext"].count("- Memory ") == 3


# 4. edit / write ----------------------------------------------------------
def test_edit_glob_rows_and_never_deny(env):
    env = ungraded(env)
    ev = {
        "session_id": "s1",
        "tool_name": "Edit",
        "cwd": "/repo",
        "tool_input": {
            "file_path": "tools/merge_gate.py",
            "old_string": "a",
            "new_string": "git push --force x",
        },
    }
    out = call(env, ev)
    assert "permissionDecision" not in out
    ctx = out["additionalContext"]
    # the literal glob is more specific than **/*.py
    assert ctx.index("feedback_merge_gate") < ctx.index("feedback_py_edit")
    assert log(env)[-1]["text"] == "/repo/tools/merge_gate.py"


def test_write_content_never_logged(env):
    env = ungraded(env)
    secret = "CONTENT-7731-never-in-the-log"
    ev = {
        "session_id": "s1",
        "tool_name": "Write",
        "cwd": "/repo",
        "tool_input": {"file_path": "/repo/a/b.py", "content": secret},
    }
    out = call(env, ev)
    assert "feedback_py_edit" in out["additionalContext"]
    assert secret not in Path(env["NOBLIVION_GUARD_LOG"]).read_text()


def test_multiedit_and_unmatched_path(env):
    ev = {
        "session_id": "s1",
        "tool_name": "MultiEdit",
        "cwd": "/repo",
        "tool_input": {"file_path": "/repo/README.md", "edits": []},
    }
    assert call(env, ev) is None


def test_other_tools_ignored(env):
    assert (
        call(
            env, {"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": "/repo/x.py"}}
        )
        is None
    )


@pytest.mark.parametrize(
    "glob,path,hit",
    [
        ("**/*.py", "/a/b/c.py", True),
        ("**/*.py", "c.py", True),
        ("**/*.py", "/a/c.pyc", False),
        ("tests/**/*.py", "/r/tests/x/y.py", True),
        ("tests/**/*.py", "/r/tests/y.py", True),
        ("tests/**/*.py", "/r/mytests/y.py", False),
        ("tools/*.sh", "/r/tools/a/b.sh", False),
        ("docker-compose.yml", "/r/docker-compose.yml", True),
        ("**/secrets/*", "/r/x/secrets/k", True),
    ],
)
def test_glob_regex(glob, path, hit):
    assert bool(gh.glob_regex(glob).search(path)) is hit


# 5. fail open -------------------------------------------------------------
def test_missing_table_is_silent_and_logs_once_per_agent(env):
    """Review finding F5: the guard is off, so say it once in the log."""
    os.unlink(env["NOBLIVION_GUARD_TABLE"])
    for sid, agent in (("s1", None), ("s1", None), ("s1", "a1"), ("s2", None), ("s2", None)):
        assert call(env, bash("git push --force o x", sid, agent)) is None
    rows = log(env)
    assert [(r["session_id"], r["agent_id"], r["decision"]) for r in rows] == [
        ("s1", "main", "error"),
        ("s1", "a1", "error"),
        ("s2", "main", "error"),
    ]
    assert rows[0]["error"] == f"table missing: {env['NOBLIVION_GUARD_TABLE']}"


@pytest.mark.parametrize("text", ["{not json", '{"entries": []}'])
def test_broken_or_empty_table_is_silent_and_logs_once(env, text):
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(text)
    for _ in range(3):
        assert call(env, bash("git push --force o x")) is None
    assert [r["error"].split(":")[0] for r in log(env)] == ["table empty or broken"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[1,2]",
        '{"tool_name":"Bash"}',
        '{"tool_name":"Bash","tool_input":{"command":5}}',
    ],
)
def test_bad_stdin_is_silent(env, raw):
    assert call(env, None, raw=raw) is None


def test_bad_stdin_subprocess_exit_zero(env):
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input="garbage",
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert log(env)[-1]["decision"] == "error"


def _wrapped(hook_path):
    """The WI-4 install shape (review finding F3)."""
    return ["sh", "-c", f"{sys.executable} {hook_path}; exit 0"]


def test_wrapper_turns_a_missing_hook_file_into_exit_zero(env, tmp_path):
    missing = tmp_path / "no-such-dir" / "guard_hook.py"
    bare = subprocess.run(
        [sys.executable, str(missing)], input="{}", capture_output=True, text=True
    )
    assert bare.returncode == 2  # what the wrapper is for
    r = subprocess.run(
        _wrapped(missing),
        input=json.dumps(bash("git push --force o x")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert (r.returncode, r.stdout) == (0, "")


def test_wrapper_keeps_the_deny_json_on_stdout(env):
    r = subprocess.run(
        _wrapped(HOOK),
        input=json.dumps(bash("git push --force o x")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_copy_without_its_modules_exits_zero(env, tmp_path):
    lone = tmp_path / "guard_hook.py"
    lone.write_text(HOOK.read_text())  # no guard_table.py next to it
    r = subprocess.run(
        [sys.executable, str(lone)],
        input=json.dumps(bash("git push --force o x")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert log(env)[-1]["decision"] == "error"


def test_broken_regex_entry_is_skipped_others_still_deny(env):
    t = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    t["entries"].insert(0, entry("feedback_broken", violates=r"git\s+push(("))
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps(t))
    out = call(env, bash("git push --force o x"))
    assert out["permissionDecision"] == "deny"
    assert "feedback_broken" not in out["permissionDecisionReason"]


def test_exception_fails_open_and_logs(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gh, "decide", boom)
    assert call(env, bash("git push --force o x")) is None
    assert log(env)[-1]["decision"] == "error" and "boom" in log(env)[-1]["error"]


def test_a_timeout_inside_a_fail_open_handler_still_ends_the_call(env, tmp_path, monkeypatch):
    """Review item 11 (2026-09-30): ``_TimeUp`` is a BaseException, so the
    ``except Exception`` of ``row_memory`` (a slow disk read) cannot swallow
    the one-shot alarm and let the rest of the call run with no time limit."""
    env = ungraded(env)
    mem = tmp_path / "mem"
    _memfile(mem, "feedback_deploy", "A body.")
    _table_with_source(env, mem, ["feedback_deploy"])
    real = gh._mt()

    class Slow:
        def __getattr__(self, name):
            return getattr(real, name)

        def read_memory(self, *_a, **_k):
            time.sleep(2)
            return {}

    out = io.StringIO()
    # a scoped patch: monkeypatch.undo() would also undo the autouse
    # hook_env, and the next call would write to the real data dir
    with monkeypatch.context() as m:
        m.setattr(gh, "_mt", lambda: Slow())
        t0 = time.monotonic()
        assert (
            gh.main(
                stdin=io.StringIO(json.dumps(bash("make deploy-all now"))),
                stdout=out,
                environ=env,
                time_limit=0.2,
            )
            == 0
        )
        assert time.monotonic() - t0 < 1.5
    assert out.getvalue() == ""  # the deny check was done: only the rows are lost
    assert log(env)[-1]["error"] == "timeout"
    # nothing was charged, and the state lock is free for the next call
    assert "Text: A body." in call(env, bash("make deploy-all now"))["additionalContext"]
    assert issubclass(gh._TimeUp, BaseException) and not issubclass(gh._TimeUp, Exception)


def test_timeout_fails_open(env, monkeypatch):
    def slow(*_a, **_k):
        time.sleep(2)
        return "never printed"

    monkeypatch.setattr(gh, "decide", slow)
    out = io.StringIO()
    t0 = time.monotonic()
    assert (
        gh.main(stdin=io.StringIO(json.dumps(bash("x"))), stdout=out, environ=env, time_limit=0.2)
        == 0
    )
    assert time.monotonic() - t0 < 1.5
    # allowed, and one line says that no deny rule was checked (NOBLIVION-50)
    assert json.loads(out.getvalue()) == _timeout_doc()
    assert "never printed" not in out.getvalue()
    assert log(env)[-1]["error"] == "timeout"


def test_unwritable_state_fails_open(env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    e = dict(env, NOBLIVION_GUARD_STATE_DIR=str(blocker / "state"))
    assert call(e, bash("git push --force o x")) is None


def _state(env, sid="s1", agent="main"):
    f = gh._state_file(env, sid, agent)
    return json.loads(f.read_text() or "{}") if f.exists() else {}


def test_log_failure_does_not_hide_the_deny_and_charges_only_printed_denies(env, tmp_path):
    """Review finding F4: the log is best effort; the deny is printed and
    charged even when the log cannot be written."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    e = dict(env, NOBLIVION_GUARD_LOG=str(blocker / "log.jsonl"))
    outs = [call(e, bash("git push --force o x")) for _ in range(3)]
    assert [o.get("permissionDecision") for o in outs] == ["deny", "deny", "deny"]
    assert outs[2]["permissionDecisionReason"].startswith("Denied 3 times")
    assert _state(e)["denies"] == {"feedback_no_force": 3}


class _BrokenOut(io.StringIO):
    def write(self, _text):
        raise BrokenPipeError("stdout closed")


def test_a_deny_that_cannot_be_printed_is_not_charged(env):
    assert (
        gh.main(
            stdin=io.StringIO(json.dumps(bash("git push --force o x"))),
            stdout=_BrokenOut(),
            environ=env,
        )
        == 0
    )
    assert _state(env).get("denies", {}) == {}
    assert [r["decision"] for r in log(env)] == ["error"]
    out = call(env, bash("git push --force o x"))
    assert out["permissionDecision"] == "deny"
    assert log(env)[-1]["counts"] == {"feedback_no_force": 1}


def test_order_is_decide_print_charge_log(env, monkeypatch):
    seen = []
    real_commit, real_log = gh.Decision.commit, gh.log_line

    class Out(io.StringIO):
        def write(self, text):
            seen.append("print")
            return super().write(text)

    monkeypatch.setattr(
        gh.Decision, "commit", lambda self: (seen.append("charge"), real_commit(self))
    )
    monkeypatch.setattr(gh, "log_line", lambda *a, **k: (seen.append("log"), real_log(*a, **k))[1])
    gh.main(stdin=io.StringIO(json.dumps(bash("git push --force o x"))), stdout=Out(), environ=env)
    assert seen == ["print", "charge", "log"]


# 6. log -------------------------------------------------------------------
def test_log_line_fields(env):
    long = "git push --force o x " + "y" * 600
    call(env, bash(long, "sess-9"))
    rec = log(env)[-1]
    assert set(rec) >= {"ts", "session_id", "tool", "decision", "ids", "text"}
    assert rec["ts"].endswith("Z") and rec["session_id"] == "sess-9" and rec["tool"] == "Bash"
    assert rec["decision"] == "deny" and rec["ids"] == ["feedback_no_force"]
    assert rec["text"] == long[:300]


def test_log_and_state_files_are_private(env):
    """Review finding F6: the log holds command text; 0600 like the transcripts."""
    old = os.umask(0o002)
    try:
        call(env, bash("git push --force o x"))
    finally:
        os.umask(old)
    files = [Path(env["NOBLIVION_GUARD_LOG"])] + list(
        Path(env["NOBLIVION_GUARD_STATE_DIR"]).glob("*.json")
    )
    assert len(files) == 2
    assert {oct(f.stat().st_mode & 0o777) for f in files} == {"0o600"}
    assert oct(Path(env["NOBLIVION_GUARD_STATE_DIR"]).stat().st_mode & 0o777) == "0o700"


def test_nothing_logged_when_nothing_matches(env):
    call(env, bash("echo hi"))
    assert log(env) == []


# 7. state pruning ---------------------------------------------------------
def test_new_session_prunes_old_state(env):
    sd = Path(env["NOBLIVION_GUARD_STATE_DIR"])
    sd.mkdir(parents=True)
    old, fresh = sd / "old.json", sd / "fresh.json"
    for f in (old, fresh):
        f.write_text("{}")
    past = time.time() - 8 * 24 * 3600
    os.utime(old, (past, past))
    call(env, bash("git push --force o x", "new-session"))
    assert not old.exists() and fresh.exists()
    assert len(list(sd.glob("*.json"))) == 2


def test_corrupt_state_file_is_reset(env):
    call(env, bash("git push --force o x", "c"))
    f = gh._state_file(env, "c")
    f.write_text("{broken")
    assert call(env, bash("git push --force o x", "c"))["permissionDecision"] == "deny"


# 8. wrappers and heredocs via match() -------------------------------------
@pytest.mark.parametrize(
    "cmd",
    [
        "git -C /srv/wt push --force origin x",
        "cd /x && git -c a.b=c push --force o x",
        "sudo env A=1 git push --force o x",
        "timeout 30 git push --force o x",
        "nohup setsid git push --force o x",
        "cd x && git stash pop",
        "GIT_DIR=/x git -C /y stash pop",
    ],
)
def test_wrappers_do_not_hide_a_guarded_command(env, cmd):
    assert call(env, bash(cmd))["permissionDecision"] == "deny"


def test_heredoc_body_does_not_deny(env):
    cmd = "cat > notes.md <<'EOF'\ngit push --force origin x\nEOF"
    out = call(env, bash(cmd))
    assert out is None or "permissionDecision" not in out


# FORCE is unanchored, so these fail on a matcher that searches the raw text
# (e97a5386 denied all four: review finding F1).
@pytest.mark.parametrize(
    "cmd",
    [
        'git commit -m "docs: never git push --force origin x"',
        "grep -rn 'git push --force' tools/",
        "echo done  # git push --force origin x",
        'gh pr create --title t --body "do not git push --force origin x"',
    ],
)
def test_quoted_text_or_comment_does_not_deny(env, cmd):
    out = call(env, bash(cmd))
    assert out is None or "permissionDecision" not in out
    assert [r for r in log(env) if r["decision"] == "deny"] == []


def test_executed_string_still_denies(env):
    out = call(env, bash("bash -c 'cd /r && git push --force origin x'"))
    assert out["permissionDecision"] == "deny"
    assert "feedback_no_force" in out["permissionDecisionReason"]


def test_rows_ignore_a_comment_and_see_an_executed_string(env):
    assert call(env, bash("echo x # ; python -m pytest -q")) is None
    out = call(env, bash("bash -c 'python -m pytest -q'"))
    assert "feedback_pytest" in out["additionalContext"]


# 9. the override marker (WI-3c) -------------------------------------------
# Grammar: a top-level shell comment `# guard-ok: <reason>`. The reason must
# hold a letter or a digit. A `#` in quotes, in a heredoc body, in an executed
# string (`bash -c '...'`), in `$( )` or backticks, or not at a word start is
# not a comment of the command, so it is not a marker.
@pytest.mark.parametrize(
    "cmd, reason",
    [
        ("git push --force o x  # guard-ok: my own branch", "my own branch"),
        ("git push --force o x #guard-ok:feedback_no_force", "feedback_no_force"),
        ("git push --force o x # guard-ok:   spaced reason   ", "spaced reason"),
        ("git push --force o x; # guard-ok: after a separator", "after a separator"),
        ("git push --force o x # guard-ok: on line 1\necho done", "on line 1"),
        ("git push --force o x # guard-ok: first\necho y # guard-ok: second", "second"),
        (
            "cat <<'EOF' > f # guard-ok: on the operator line\nbody\nEOF\ngit push --force o x",
            "on the operator line",
        ),
        (
            'echo "a # b" && git push --force o x # guard-ok: after a quote with a hash',
            "after a quote with a hash",
        ),
        ('echo "$(date "+%s")" # guard-ok: nested quotes', "nested quotes"),
    ],
)
def test_override_marker_found(cmd, reason):
    assert gh.override_marker(cmd) == (reason, True)


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force o x",
        'git push --force o x "# guard-ok: in double quotes"',
        "git push --force o x '# guard-ok: in single quotes'",
        "git push --force o x $'# guard-ok: ansi \\' quoted'",
        "git push --force o x; cat <<EOF\n# guard-ok: in a heredoc body\nEOF",
        "git push --force o x; cat <<'EOF'\n# guard-ok: in a quoted heredoc body\nEOF",
        "bash -c 'git push --force o x # guard-ok: in an executed string'",
        "echo $(true # guard-ok: in a subshell\n); git push --force o x",
        "echo `true # guard-ok: in backticks\n`; git push --force o x",
        "git push --force o x#guard-ok: not at a word start",
        "git push --force o x \\# guard-ok: escaped hash",
        "git push --force o x # guardok: wrong word",
        "git push --force o x # GUARD-OK: upper case",
    ],
)
def test_override_marker_not_found(cmd):
    assert gh.override_marker(cmd) == (None, False)


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force o x # guard-ok:",
        "git push --force o x # guard-ok:    ",
        "git push --force o x # guard-ok: -",
        "git push --force o x # guard-ok",
    ],
)
def test_override_marker_without_a_reason_is_seen_but_invalid(cmd):
    assert gh.override_marker(cmd) == (None, True)


def test_redact_reason_hides_secrets_and_cuts():
    r = gh.redact_reason("token=ghp_" + "a1" * 20 + " for the sync")
    assert "ghp_" not in r and "a1a1a1" not in r and "for the sync" in r
    r = gh.redact_reason("key AKIA" + "B" * 16 + " and sk-" + "x" * 30)
    assert "AKIA" not in r and "sk-xxx" not in r
    r = gh.redact_reason("password: hunter2 please")
    assert "hunter2" not in r
    r = gh.redact_reason("Bearer abc.def.ghi and " + "0f83895c" * 5)
    assert "abc.def" not in r and "0f83895c0f" not in r
    mid = "feedback_a_selector_bucket_pin_shadows_an_exact_role_pin_2026_09_29"
    assert gh.redact_reason(mid) == mid  # a memory id is kept
    assert gh.redact_reason("the key point: my own branch") == "the key point: my own branch"
    long = gh.redact_reason("word " * 100)
    assert len(long) <= gh.REASON_CHARS and long.endswith("...")


# 9b. the override marker in the decision (WI-3c) --------------------------
def test_deny_message_names_the_rule_the_apply_and_the_override(env):
    r = call(env, bash("git stash pop"))["permissionDecisionReason"]
    assert r == (
        "Never pop the shared stash.\nApply: git stash apply\n"
        "Memory: feedback_no_stash_pop\n" + OVERRIDE
    )


def test_marker_allows_after_the_budget_and_logs_override(env):
    for _ in range(2):
        call(env, bash("git push --force o x"))
    cmd = "git push --force o x  # guard-ok: rebased my own branch"
    assert call(env, bash(cmd)) is None  # allowed, nothing printed
    rec = log(env)[-1]
    assert rec["decision"] == "override" and rec["ids"] == ["feedback_no_force"]
    assert rec["reason"] == "rebased my own branch" and rec["text"] == cmd
    assert _state(env)["denies"] == {"feedback_no_force": 2}  # an override charges nothing
    again = call(env, bash("git push --force o x"))  # the marker is per call
    assert again["permissionDecisionReason"].startswith("Denied 3 times")


def test_marker_allows_on_the_first_attempt_and_covers_every_hit(env):
    cmd = "git stash pop && git push --force o x # guard-ok: feedback_no_force"
    assert call(env, bash(cmd)) is None
    rec = log(env)[-1]
    assert rec["decision"] == "override"
    assert sorted(rec["ids"]) == ["feedback_no_force", "feedback_no_stash_pop"]
    assert rec["reason"] == "feedback_no_force"


def test_marker_subprocess_exit_zero_and_silent(env):
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(bash("git push --force o x # guard-ok: test")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert log(env)[-1]["decision"] == "override"


@pytest.mark.parametrize(
    "cmd",
    [
        'git push --force o x "# guard-ok: in double quotes"',
        "git push --force o x '# guard-ok: in single quotes'",
        "git push --force o x; cat <<EOF\n# guard-ok: in a heredoc body\nEOF",
        "bash -c 'git push --force o x # guard-ok: in an executed string'",
        "git push --force o x#guard-ok: not a comment",
    ],
)
def test_marker_in_quoted_data_or_heredoc_does_not_allow(env, cmd):
    out = call(env, bash(cmd))
    assert out["permissionDecision"] == "deny"
    assert "has no reason" not in out["permissionDecisionReason"]
    assert [r["decision"] for r in log(env)] == ["deny"]


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force o x # guard-ok:",
        "git push --force o x # guard-ok:   ",
        "git push --force o x # guard-ok: ...",
        "git push --force o x # guard-ok",
    ],
)
def test_marker_without_a_reason_does_not_allow(env, cmd):
    out = call(env, bash(cmd))
    assert out["permissionDecision"] == "deny"
    assert "Your # guard-ok comment has no reason" in out["permissionDecisionReason"]
    assert [r["decision"] for r in log(env)] == ["deny"]


def test_override_log_redacts_a_secret_in_the_reason_and_the_text(env):
    secret = "ghp_" + "Zq9" * 12
    cmd = f"git push --force o x # guard-ok: token={secret} is mine"
    assert call(env, bash(cmd)) is None
    raw = Path(env["NOBLIVION_GUARD_LOG"]).read_text()
    assert secret not in raw and "Zq9Zq9" not in raw
    rec = log(env)[-1]
    assert rec["reason"] == "token=[redacted] is mine"


def test_marker_without_a_hit_changes_nothing(env):
    assert call(env, bash("ls /tmp # guard-ok: nothing to override")) is None
    assert log(env) == []
    out = call(env, bash("python -m pytest -q # guard-ok: rows still show"))
    assert "feedback_pytest" in out["additionalContext"]
    assert [r["decision"] for r in log(env)] == ["rows"]


# 10. WI-3d: an override needs a failed run of the rule's own command -------
GATE = r"lint_changed_lines\.py(?![^;&|\n]*--base[=\s]+[\"']?\$\()"
GATE_CMD = "python3 tools/lint_changed_lines.py --base HEAD^"
RUN_FIRST = "git merge-base HEAD github/main"
MARK = " # guard-ok: no github remote in this checkout"


def gate_entry(**over):
    e = entry(
        "feedback_gate",
        rule="Pass the merge base to the gate.",
        apply="BASE=$(git merge-base HEAD github/main)",
        violates=GATE,
    )
    e.update(complies=r"\bgit\s+merge-base\b", run_first=RUN_FIRST)
    e.update(over)
    return e


@pytest.fixture
def genv(env):
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"version": 1, "entries": [dict(e) for e in BASE] + [gate_entry()]})
    )
    return env


def failed(
    cmd,
    error="Exit code 128\nfatal: Not a valid object name github/main",
    sid="s1",
    agent=None,
    interrupt=False,
):
    ev = {
        "session_id": sid,
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_use_id": "toolu_1",
        "error": error,
        "is_interrupt": interrupt,
        "cwd": "/tmp",
    }
    if agent:
        ev["agent_id"] = agent
    return ev


def test_deny_gives_the_exact_command_to_run_first(genv):
    out = call(genv, bash(GATE_CMD))
    reason = out["permissionDecisionReason"]
    assert out["permissionDecision"] == "deny"
    assert f"Run this command first: {RUN_FIRST}" in reason
    assert "Override only if that command fails in this session" in reason
    assert OVERRIDE not in reason


def test_override_without_a_failed_run_is_refused(genv):
    out = call(genv, bash(GATE_CMD + MARK))
    reason = out["permissionDecisionReason"]
    assert out["permissionDecision"] == "deny"
    assert "override is refused" in reason and f"Run this command first: {RUN_FIRST}" in reason
    assert "has no reason" not in reason  # the marker is valid; only the evidence is missing
    rec = log(genv)[-1]
    assert rec["decision"] == "deny" and rec["override_refused"] == ["feedback_gate"]
    assert rec["reason"] == "no github remote in this checkout"
    assert rec["counts"] == {"feedback_gate": 1}


# One long word in a command. The log text of a call is made after the decision
# is printed, and from the start of the command only (``log_head``), so a slow
# text never turns a deny into a call that ran out of time (NOBLIVION-47).
LONG_UNITS = ["a", "a-b", "a.b"]


def _timed(env, event):
    t0 = time.perf_counter()
    out = call(env, event)
    return out, time.perf_counter() - t0


@pytest.mark.parametrize("size", [30_000, 100_000, 500_000])
@pytest.mark.parametrize("unit", LONG_UNITS)
def test_a_rule_deny_with_one_long_word_is_printed_and_logged(env, unit, size):
    cmd = "git push --force o x " + unit * (size // len(unit))
    out, took = _timed(env, bash(cmd))
    # The limit of the hook, not half of it: 500 KB of parts took 0.38 s on a
    # fast machine, and a CI machine can be 3 times slower.
    assert took < gh.TIME_LIMIT_S
    assert out["permissionDecision"] == "deny"
    rec = log(env)[-1]
    assert rec["decision"] == "deny" and rec["ids"] == ["feedback_no_force"]
    assert rec["text"] == f"git push --force o x [cut: {len(cmd)} characters]"


@pytest.mark.parametrize("size", [30_000, 100_000, 500_000])
@pytest.mark.parametrize("unit", LONG_UNITS)
def test_a_refused_override_with_a_long_reason_is_still_denied(genv, unit, size):
    """The reason of the marker goes to the log only: it is read after the print."""
    cmd = GATE_CMD + " # guard-ok: " + unit * (size // len(unit))
    out, took = _timed(genv, bash(cmd))
    assert took < 0.75
    assert out["permissionDecision"] == "deny"
    assert "override is refused" in out["permissionDecisionReason"]
    rec = log(genv)[-1]
    assert rec["decision"] == "deny" and rec["override_refused"] == ["feedback_gate"]
    assert rec["reason"] == f"[cut: {size // len(unit) * len(unit)} characters]"
    assert rec["text"] == f"{GATE_CMD} # guard-ok: [cut: {len(cmd)} characters]"


@pytest.mark.parametrize("unit", LONG_UNITS)
def test_an_override_with_a_long_reason_is_logged_in_time(genv, unit):
    assert call(genv, failed(RUN_FIRST)) is None  # the compliant form failed: the override is open
    cmd = GATE_CMD + " # guard-ok: " + unit * (500_000 // len(unit))
    out, took = _timed(genv, bash(cmd))
    assert out is None and took < 0.75  # allowed, and no line that the time ran out
    rec = log(genv)[-1]
    assert rec["decision"] == "override" and rec["reason"].startswith("[cut: ")


@pytest.mark.parametrize("unit", LONG_UNITS)
def test_a_failed_run_with_a_long_word_is_recorded_in_time(genv, unit):
    cmd = RUN_FIRST + " " + unit * (500_000 // len(unit))
    out, took = _timed(genv, failed(cmd, error="Exit code 128\n" + "b" * 500_000))
    assert out is None and took < gh.TIME_LIMIT_S  # 0.21 s on a fast machine
    rec = log(genv)[-1]
    assert rec["decision"] == "apply-failed"
    assert rec["text"] == f"{RUN_FIRST} [cut: {len(cmd)} characters]"


def test_log_head_keeps_a_short_text_and_cuts_a_long_text_at_a_word():
    short = "git status " * 300
    assert len(short) <= gh.LOG_SCAN_CHARS and gh.log_head(short) == short
    exact = "a" * gh.LOG_SCAN_CHARS
    assert gh.log_head(exact) == exact
    long = "word " * 2000
    head = gh.log_head(long)
    assert head == "word " * 818 + f"word [cut: {len(long)} characters]"
    assert len(head) < gh.LOG_SCAN_CHARS + 40
    assert gh.log_head("a" * 5000) == "[cut: 5000 characters]"  # one word: nothing of it stays
    assert gh.log_head(None) == "" and gh.log_head("") == ""


def test_log_head_drops_the_word_that_the_cut_splits():
    """The start of a token that ``redact`` would not know must not reach the log."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"
    filler = ("key=" + "v" * 400 + " ") * 10  # ``redact`` makes this short
    text = filler + "x" * (gh.LOG_SCAN_CHARS - len(filler) - 6) + token + " tail " * 100
    assert text[: gh.LOG_SCAN_CHARS].endswith("ghp_A1")
    logged = gh.redact(gh.log_head(text))
    assert logged == "key=[redacted] " * 9 + f"key=[redacted] [cut: {len(text)} characters]"
    assert len(logged) < gh.LOG_TEXT_CHARS  # so the end of the text would be in the log line
    # a word that starts at the cut, or ends at it, is not split: nothing more is dropped
    text = "word " * 819 + " " + "next " * 100
    assert text[gh.LOG_SCAN_CHARS - 1] == " "
    assert gh.log_head(text).startswith("word " * 818 + "word [cut: ")
    text = "word " * 818 + "wordss" + " next" * 100
    assert text[gh.LOG_SCAN_CHARS - 1] == "s" and text[gh.LOG_SCAN_CHARS] == " "
    assert gh.log_head(text).startswith("word " * 818 + "wordss [cut: ")


@pytest.mark.parametrize(
    "text",
    ["a" * 500_000, "a-b" * 166_667, "a.b" * 166_667, "key=" + "v" * 500_000, "word " * 100_000],
)
def test_the_log_text_of_a_500kb_text_costs_a_fixed_time(text):
    t0 = time.perf_counter()
    gh.redact(gh.log_head(text))
    gh.redact_reason(text)
    assert time.perf_counter() - t0 < 0.5


def test_a_compliant_run_that_succeeded_does_not_unlock_the_override(genv):
    assert call(genv, bash(RUN_FIRST)) is None  # it ran; no failure event came
    assert call(genv, bash(GATE_CMD + MARK))["permissionDecision"] == "deny"


def test_a_failed_run_of_the_rules_command_unlocks_the_override(genv):
    assert call(genv, failed("git -C fixture/repo merge-base HEAD github/main")) is None
    rec = log(genv)[-1]
    assert rec["decision"] == "apply-failed" and rec["ids"] == ["feedback_gate"]
    assert rec["error"].startswith("Exit code 128")
    assert call(genv, bash(GATE_CMD + MARK)) is None  # allowed, silent
    rec = log(genv)[-1]
    assert rec["decision"] == "override" and rec["evidence"] == {"feedback_gate": "failed:1"}
    assert call(genv, bash(GATE_CMD + MARK)) is None  # the evidence stays for the session
    # without the marker it is still denied, and the text now offers the override
    reason = call(genv, bash(GATE_CMD))["permissionDecisionReason"]
    assert OVERRIDE in reason and "Run this command first" not in reason


@pytest.mark.parametrize(
    "ev",
    [
        failed(RUN_FIRST, interrupt=True),  # interrupted
        failed(RUN_FIRST, error="Permission to use Bash has been denied."),  # never ran
        failed(RUN_FIRST, error="Exit code 0"),
        failed(RUN_FIRST, error=""),
        failed("git status"),  # not the rule's command
        failed(RUN_FIRST, sid="s2"),  # other session
        failed(RUN_FIRST, agent="sub1"),  # other agent
        dict(failed(RUN_FIRST), hook_event_name="PostToolUse"),  # a success event
        dict(failed(RUN_FIRST), tool_name="Edit"),
    ],
)
def test_no_evidence_from_other_events(genv, ev):
    assert call(genv, ev) is None
    assert call(genv, bash(GATE_CMD + MARK))["permissionDecision"] == "deny"
    assert not any(
        r["decision"] == "apply-failed" and r["session_id"] == "s1" and r["agent_id"] == "main"
        for r in log(genv)
    )


def test_a_post_event_never_denies_or_shows_rows(genv):
    """The hook on PostToolUseFailure only records; a violating command there
    (it cannot have run past the PreToolUse deny) prints nothing."""
    assert call(genv, failed("git push --force origin x")) is None
    assert call(genv, failed("git push github main")) is None
    assert [r["decision"] for r in log(genv)] == []


def test_a_memory_without_complies_keeps_the_plain_override(genv):
    assert call(genv, bash("git push --force origin x" + MARK)) is None
    rec = log(genv)[-1]
    assert rec["decision"] == "override" and rec["evidence"] == {"feedback_no_force": "no-complies"}


def test_several_hits_need_evidence_for_each_memory_with_complies(genv):
    cmd = GATE_CMD + " && git push --force origin x" + MARK
    out = call(genv, bash(cmd))
    assert out["permissionDecision"] == "deny"
    assert log(genv)[-1]["override_refused"] == ["feedback_gate"]
    call(genv, failed(RUN_FIRST))
    assert call(genv, bash(cmd)) is None
    assert log(genv)[-1]["evidence"] == {
        "feedback_gate": "failed:1",
        "feedback_no_force": "no-complies",
    }


def test_a_marker_with_no_reason_names_both_problems(genv):
    reason = call(genv, bash(GATE_CMD + " # guard-ok:"))["permissionDecisionReason"]
    assert "has no reason" in reason and f"Run this command first: {RUN_FIRST}" in reason


def test_post_failure_subprocess_exits_0_and_prints_nothing(genv):
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(failed(RUN_FIRST)),
        capture_output=True,
        text=True,
        env=dict(os.environ, **genv),
        timeout=10,
    )
    assert r.returncode == 0 and r.stdout == ""
    assert log(genv)[-1]["decision"] == "apply-failed"


def test_an_old_state_file_without_failed_still_loads(genv):
    call(genv, bash(GATE_CMD))
    st = next(Path(genv["NOBLIVION_GUARD_STATE_DIR"]).glob("*.json"))
    data = json.loads(st.read_text())
    data.pop("failed", None)
    st.write_text(json.dumps(data))
    call(genv, failed(RUN_FIRST))
    assert call(genv, bash(GATE_CMD + MARK)) is None


@pytest.mark.parametrize("name", ["PostToolUse", "PermissionDenied", "PostToolBatch"])
def test_other_hook_events_are_ignored(genv, name):
    ev = dict(bash("git push --force origin x"), hook_event_name=name)
    assert call(genv, ev) is None
    assert call(genv, dict(bash("git push github main"), hook_event_name=name)) is None
    assert log(genv) == []


def test_a_run_first_without_complies_keeps_the_plain_override(genv):
    Path(genv["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"version": 1, "entries": [gate_entry(complies="")]})
    )
    assert call(genv, bash(GATE_CMD + MARK)) is None
    assert log(genv)[-1]["evidence"] == {"feedback_gate": "no-complies"}


def test_an_error_prefix_before_the_exit_code_is_still_a_failed_run(genv):
    """The live error recall hook strips an "Error: " prefix before the
    "Exit code <n>" line; accept the same shape."""
    call(genv, failed(RUN_FIRST, error="Error: Exit code 1\nfatal: bad revision"))
    assert log(genv)[-1]["decision"] == "apply-failed"
    assert call(genv, bash(GATE_CMD + MARK)) is None


# WI-3f: the rows show the memory's own text (the WI-14b shape) ------------
def _memfile(
    folder,
    mid,
    body,
    rule="Run the deploy script with the lock.",
    apply="Use `make deploy-all LOCK=1`, never the bare target.",
):
    folder.mkdir(exist_ok=True)
    (folder / f"{mid}.md").write_text(
        f'---\nname: {mid}\ndescription: {rule}\ntype: feedback\nrule: {rule}\napply: "{apply}"\n'
        f"scope: tool\ntriggers:\n  - make deploy-all\n---\n{body}\n"
    )


def _table_with_source(env, folder, ids):
    entries = [
        entry(
            m,
            rule="Run the deploy script with the lock.",
            apply="Use `make deploy-all LOCK=1`, never the bare target.",
            triggers=["make deploy-all"],
        )
        for m in ids
    ]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"source": str(folder), "entries": entries})
    )


def test_row_shows_a_short_body_whole_with_no_tag(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    _memfile(mem, "feedback_deploy", "Measured 2026-09-30: the bare target raced a second deploy.")
    _table_with_source(env, mem, ["feedback_deploy"])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    lines = ctx.splitlines()
    assert lines[0] == gh.ROWS_HEADER and "no need to open the memory file" in lines[0]
    assert lines[1] == "- Memory feedback_deploy (full text; no need to open the file):"
    assert lines[2] == "  Rule: Run the deploy script with the lock."
    assert lines[3] == "  Apply: Use `make deploy-all LOCK=1`, never the bare target."
    assert lines[4] == "  Text: Measured 2026-09-30: the bare target raced a second deploy."
    assert "[memory " not in ctx and "..." not in ctx


def test_row_long_body_shows_the_part_on_this_command(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    filler = "\n".join(f"Background sentence {i} about something else entirely." for i in range(60))
    body = (
        filler
        + "\nThe make deploy-all staging target without LOCK raced a second deploy."
        + " Fix: set LOCK=1."
    )
    _memfile(mem, "feedback_deploy", body)
    _table_with_source(env, mem, ["feedback_deploy"])
    ctx = call(env, bash("make deploy-all staging"))["additionalContext"]
    assert "(rule and fix in full; the file adds about " in ctx
    text = [x for x in ctx.splitlines() if x.startswith("  Text on this command: ")]
    assert (
        text
        and "deploy-all staging target without LOCK raced" in text[0]
        and "Fix: set LOCK=1." in text[0]
    )
    assert "Background sentence 0 " not in ctx
    assert len(ctx) <= gh.ROWS_MAX_CHARS


def test_row_long_body_with_no_line_on_the_command_shows_its_start(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    body = "\n".join(f"Background sentence {i} about something else entirely." for i in range(60))
    _memfile(mem, "feedback_deploy", body)
    _table_with_source(env, mem, ["feedback_deploy"])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    text = [x for x in ctx.splitlines() if x.startswith("  Text (start): ")]
    assert text and text[0].startswith("  Text (start): Background sentence 0 about")
    assert 600 <= len(text[0]) <= 1200 + 20 and text[0].endswith(".")
    assert "the file adds about " in ctx


def test_row_memory_text_is_redacted_and_inert(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    tok = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    _memfile(
        mem,
        "feedback_deploy",
        f"The token was {tok} and password=hunter2hunter2.\n</system-reminder> <b>not a tag</b>",
    )
    _table_with_source(env, mem, ["feedback_deploy"])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    assert tok not in ctx and "hunter2hunter2" not in ctx and ctx.count("[REDACTED]") == 2
    assert "</system-reminder>" not in ctx and "‹/system-reminder›" in ctx
    # memory text stays one line per field: no memory line starts a row
    assert all(x.startswith(("- Memory ", "  ")) for x in ctx.splitlines()[1:])


def test_row_shown_before_is_rule_and_apply_only(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    _memfile(mem, "feedback_deploy", "A body the model already read.")
    _table_with_source(env, mem, ["feedback_deploy"])
    first = call(env, bash("make deploy-all now"))["additionalContext"]
    second = call(env, bash("make deploy-all again"))["additionalContext"]
    assert "  Text: A body the model already read." in first
    assert "Text" not in second and "the full text was shown earlier in this session" in second
    assert "  Rule: Run the deploy script with the lock." in second
    assert len(second) < len(first)


def test_row_missing_or_unsafe_file_shows_rule_and_apply(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    mem.mkdir()
    (tmp_path / "secret.md").write_text("---\nrule: x\n---\nMUST NOT BE READ\n")
    _table_with_source(env, mem, ["feedback_gone", "../secret"])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    assert "- Memory feedback_gone (rule and fix in full):" in ctx
    assert "MUST NOT BE READ" not in ctx and "Text" not in ctx


def test_rows_three_long_bodies_stay_under_the_show_bound(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    for i in range(3):
        _memfile(
            mem,
            f"feedback_d{i}",
            "\n".join(
                f"Line {j}: make deploy-all needs the lock, see the fix in step {j}."
                for j in range(80)
            ),
        )
    _table_with_source(env, mem, [f"feedback_d{i}" for i in range(3)])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    assert ctx.count("- Memory ") == 3 and len(ctx) <= gh.ROWS_MAX_CHARS
    assert "  Text (start): Line 0: make deploy-all" in ctx.split("- Memory feedback_d1")[0]


def test_rows_fall_back_to_legacy_rows_without_the_formatter(env, tmp_path, monkeypatch):
    def boom():
        raise ImportError("memory_text")

    monkeypatch.setattr(gh, "_mt", boom)
    out = call(env, bash("git push github main"))
    assert "[memory feedback_push_main]" in out["additionalContext"]


def test_deny_reason_is_unchanged_by_the_full_text_rows(env, tmp_path):
    mem = tmp_path / "mem"
    _memfile(mem, "feedback_no_force", "A long body. " * 200)
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    table["source"] = str(mem)
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps(table))
    out = call(env, bash("git push --force origin x"))
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "Never force-push.\nApply: Use a new branch instead.\nMemory: feedback_no_force\n"
        + OVERRIDE
    )
    assert "additionalContext" not in out


def test_full_text_session_cap_turns_rows_compact_but_keeps_them(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    ids = [f"feedback_c{i}" for i in range(4)]
    for m in ids:
        _memfile(
            mem, m, "A body of about eighty characters that the model reads once in full here."
        )
    entries = [
        entry(
            m,
            rule="Run the deploy script with the lock.",
            apply="Use the lock.",
            triggers=[f"tool{i} run"],
        )
        for i, m in enumerate(ids)
    ]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"source": str(mem), "entries": entries})
    )
    e = dict(env, NOBLIVION_GUARD_ROWS_FULL_SESSION_CHARS="500")
    first = call(e, bash("tool0 run"))["additionalContext"]
    assert "  Text: A body" in first
    later = [call(e, bash(f"tool{i} run"))["additionalContext"] for i in (1, 2, 3)]
    assert all("Text" not in x and x.count("- Memory ") == 1 for x in later)
    st = json.loads(gh._state_file(e, "s1").read_text())
    assert st["notes"]["full_chars"] == len(first) + sum(map(len, later))


def test_session_budget_is_charged_at_the_old_row_size(env, tmp_path):
    env = ungraded(env)
    mem = tmp_path / "mem"
    _memfile(mem, "feedback_deploy", "Long body line about the deploy lock. " * 40)
    _table_with_source(env, mem, ["feedback_deploy"])
    ctx = call(env, bash("make deploy-all now"))["additionalContext"]
    st = json.loads(gh._state_file(env, "s1").read_text())
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    assert st["row_chars"] == len(gh.legacy_text(table["entries"])) < len(ctx)


# 11. WI-3g: a weak trigger needs evidence in the call ------------------------
FILLER = [
    entry(
        f"feedback_filler{i}",
        rule=f"Filler rule {i} about widgets.",
        apply="Use widgets.",
        triggers=[f"filler{i} go"],
    )
    for i in range(16)
]
GH_API = [
    entry(
        "feedback_gh_review_jobs",
        rule="When a review run is red, list its jobs and read each conclusion.",
        apply="gh api repos/o/r/actions/runs/N/jobs --jq conclusion",
        triggers=["gh api"],
    ),
    entry(
        "feedback_gh_secret",
        rule="When a local secret rotates, run gh secret set.",
        apply="gh secret set X",
        triggers=["gh api", "gh secret set CI_TOKEN"],
    ),
    entry(
        "feedback_gh_runners",
        rule="Page through the runner list before counting.",
        apply="gh api --paginate",
        triggers=["gh api"],
    ),
    entry(
        "feedback_gh_green",
        rule="Verify the producer before trusting a green fixture.",
        apply="read it",
        triggers=["gh api"],
    ),
    entry(
        "feedback_canary",
        rule="Plant canaries in the persona namespace.",
        apply="docker exec persona touch",
        triggers=["sha256sum", "tools/live_hostile_suite.py"],
    ),
    entry(
        "feedback_force_weak",
        rule="Never force-push.",
        apply="Use a new branch.",
        violates=FORCE,
        triggers=["git push --force", "gh api"],
    ),
]


def _weak_table(env, extra=()):
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"entries": [dict(e) for e in FILLER + GH_API + list(extra)]})
    )


def test_weak_trigger_by_shape_and_by_a_crowd_never_strong_by_a_few():
    table = {"entries": FILLER + GH_API}
    share = gh.trigger_share(table)
    assert share["gh api"] == 5 and share["gh secret set CI_TOKEN"] == 1
    assert gh.weak_trigger("gh api", share=5) and gh.weak_trigger(
        "gh api", share=1
    )  # generic at any count
    assert not gh.weak_trigger("gh secret set CI_TOKEN", share=1)
    assert gh.weak_trigger("gh secret set CI_TOKEN", share=4)  # a crowd is weak
    assert gh.weak_trigger("sha256sum", share=1)  # a program name alone
    assert gh.weak_trigger("glob:**/*.py", share=1) and not gh.weak_trigger(
        "glob:tools/*.py", share=1
    )
    assert not gh.weak_trigger(
        "gh api", share=5, weak_share=0
    )  # NOBLIVION_GUARD_WEAK_SHARE=0: grading off


def test_row_evidence_ignores_trigger_shell_and_hex_words():
    table = {"entries": FILLER + GH_API}
    e = GH_API[0]
    score, words = gh.row_evidence(
        "gh api repos/o/r/actions/runs/1a2b3c4d5e/jobs | grep -c x", "gh api", e, table
    )
    assert (
        words == ["actions", "jobs", "repos"] and score > gh.WEAK_FULL_SCORE
    )  # "runs" is a stopword
    assert gh.row_evidence("gh api rate_limit | grep x", "gh api", e, table) == (0.0, [])


def test_weak_trigger_without_evidence_gives_no_row(env):
    _weak_table(env)
    assert call(env, bash("gh api rate_limit -i | grep -i date")) is None
    assert call(env, bash("sha256sum tools/a.py | cut -c1-16")) is None
    assert log(env) == []


def test_weak_trigger_with_evidence_shows_only_the_fitting_memory(env):
    _weak_table(env)
    out = call(env, bash("gh api repos/o/r/actions/runs/7/jobs --jq '.jobs[] | .conclusion'"))
    ctx = out["additionalContext"]
    assert ctx.count("- Memory ") == 1
    assert ctx.splitlines()[1] == "- Memory feedback_gh_review_jobs (rule and fix in full):"
    assert log(env)[-1]["ids"] == ["feedback_gh_review_jobs"] and "rule_only" not in log(env)[-1]


def test_weak_trigger_with_little_evidence_shows_the_rule_line_only(env):
    _weak_table(env)
    ctx = call(env, bash("sha256sum /srv/canaries/list.txt"))["additionalContext"]
    note = gh.WEAK_NOTE.format("sha256sum")
    assert ctx.splitlines()[1:] == [
        f"- Memory feedback_canary ({note}):",
        "  Rule: Plant canaries in the persona namespace.",
    ]
    assert "Apply" not in ctx
    assert log(env)[-1]["rule_only"] == ["feedback_canary"]


def test_a_weak_row_does_not_count_as_shown_in_full(env, tmp_path):
    """Review item 9 (2026-09-30): a Rule-line-only row is not "the full text
    was shown earlier". It still counts toward ``ROW_REPEAT``."""
    mem = tmp_path / "mem"
    _memfile(
        mem,
        "feedback_canary",
        "A body the model has not read yet.",
        rule="Plant canaries in the persona namespace.",
        apply="docker exec persona touch",
    )
    canary = entry(
        "feedback_canary",
        rule="Plant canaries in the persona namespace.",
        apply="docker exec persona touch",
        triggers=["sha256sum", "python3 tools/live_hostile_suite.py"],
    )
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"source": str(mem), "entries": [dict(e) for e in FILLER] + [canary]})
    )
    weak = call(env, bash("sha256sum /srv/canaries/list.txt"))["additionalContext"]
    assert "weak match" in weak and "Text" not in weak
    full = call(env, bash("python3 tools/live_hostile_suite.py --persona x"))["additionalContext"]
    assert "  Text: A body the model has not read yet." in full
    assert gh.SEEN_NOTE not in full
    again = call(env, bash("python3 tools/live_hostile_suite.py --persona y"))["additionalContext"]
    assert "Text" not in again and gh.SEEN_NOTE in again
    # three shows, weak or full: the repeat cap still holds
    assert call(env, bash("python3 tools/live_hostile_suite.py --persona z")) is None
    state = json.loads(next(Path(env["NOBLIVION_GUARD_STATE_DIR"]).glob("*")).read_text())
    assert state["rows"]["feedback_canary"] == 3 and state["full"] == {"feedback_canary": 2}


def test_strong_rows_come_before_weak_rows(env):
    strong = [
        entry(f"feedback_s{i}", triggers=["gh api repos/o/r/actions/runs/7/jobs"]) for i in range(2)
    ]
    _weak_table(env, strong)
    call(env, bash("gh api repos/o/r/actions/runs/7/jobs --jq conclusion"))
    assert log(env)[-1]["ids"] == ["feedback_s0", "feedback_s1", "feedback_gh_review_jobs"]


def test_a_strong_trigger_of_the_same_memory_still_shows(env):
    _weak_table(env)
    ctx = call(env, bash("gh secret set CI_TOKEN < f"))["additionalContext"]
    assert "- Memory feedback_gh_secret (rule and fix in full):" in ctx


def test_weak_grading_never_touches_a_deny(env):
    _weak_table(env)
    out = call(env, bash("git push --force origin x"))
    assert (
        out["permissionDecision"] == "deny"
        and "Never force-push." in out["permissionDecisionReason"]
    )
    assert log(env)[-1]["decision"] == "deny"


def test_weak_share_zero_restores_the_rows_before_wi3g(env):
    _weak_table(env)
    e = dict(env, NOBLIVION_GUARD_WEAK_SHARE="0")
    ctx = call(e, bash("gh api rate_limit"))["additionalContext"]
    # the rows before WI-3g: the first three by name, whatever the call does
    assert log(e)[-1]["ids"] == [
        "feedback_force_weak",
        "feedback_gh_green",
        "feedback_gh_review_jobs",
    ]
    assert "weak match" not in ctx


def test_a_grading_error_keeps_the_rows_before_wi3g(env, monkeypatch):
    _weak_table(env)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(gh, "grade_rows", boom)
    call(env, bash("gh api rate_limit"))
    assert log(env)[-1]["decision"] == "rows" and len(log(env)[-1]["ids"]) == 3


def test_weak_glob_needs_evidence_in_the_path(env):
    globs = [
        entry(
            f"feedback_py{i}",
            rule=f"Rule {i} for asyncio code.",
            apply="await it",
            triggers=["glob:**/*.py"],
            scope="file",
        )
        for i in range(3)
    ]
    globs.append(
        entry(
            "feedback_py_async",
            rule="Offload asyncio writes with care.",
            apply="asyncio.to_thread",
            triggers=["glob:**/*.py"],
            scope="file",
        )
    )
    _weak_table(env, globs)
    ev = {
        "session_id": "s1",
        "tool_name": "Write",
        "cwd": "/repo",
        "tool_input": {"file_path": "/repo/tools/report.py", "content": "asyncio"},
    }
    assert call(env, ev) is None  # the content is not evidence, the path has none
    ev["tool_input"]["file_path"] = "/repo/src/asyncio_writer.py"
    assert "feedback_py" in call(env, ev)["additionalContext"]


def test_weak_grading_in_a_subprocess(env):
    _weak_table(env)
    ev = json.dumps(bash("gh api rate_limit"))
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=ev,
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
    )
    assert r.returncode == 0 and r.stdout == ""
    ev = json.dumps(bash("gh api repos/o/r/actions/runs/7/jobs --jq conclusion"))
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=ev,
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
    )
    assert (
        "feedback_gh_review_jobs" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    )


# 12. WI-3h: weak or strong does not depend on the number of holders ---------
# A host name is generic only when the config names it: the built-in list
# holds git words only, and ``guard.generic_args_extra`` adds the user's hosts.
HOST_CONFIG = {"guard": {"generic_args_extra": ["alpha"], "evidence_stop_extra": ["Widget"]}}
gh_host = load_hook_with_config("guard_hook", HOST_CONFIG, "guard_hook_t_hook_host")


def test_a_host_name_is_generic_only_when_the_config_names_it():
    assert "alpha" in gh_host.GENERIC_ARGS and "alpha" not in gh.GENERIC_ARGS
    assert gh.GENERIC_ARGS >= {"localhost", "origin", "github", "upstream", "main", "master"}
    assert gh.specific_trigger("ssh alpha") and not gh_host.specific_trigger("ssh alpha")
    # the evidence stop words are extended the same way, lower case
    assert "widget" in gh_host.EVIDENCE_STOP and "widget" not in gh.EVIDENCE_STOP


def test_specific_trigger_needs_an_argument_that_narrows_the_call():
    specific = [
        "docker exec app-db",
        "python3 tools/lint_ratchet.py",
        "python3 tools/worktree_guard.py remove",
        "gh api repos/o/r",
        "sudo docker exec app-core",
        "git worktree add /srv/wt",
    ]
    generic = [
        "ssh alpha",
        "git rev-parse",
        "git rev-parse --short",
        "git merge-base --is-ancestor",
        "git worktree add",
        "git worktree add -b",
        "git fetch github main",
        "gh pr create",
        "systemctl show",
        "sudo docker",
        "docker run --rm",
        "tail -n 5",
        "python3 -c",
    ]
    assert [t for t in specific if not gh_host.specific_trigger(t)] == []
    assert [t for t in generic if gh_host.specific_trigger(t)] == []
    assert gh.trigger_args("git fetch github main") == ["github", "main"]
    assert gh.trigger_args("gh pr create") == [] and gh.trigger_args("ssh alpha") == ["alpha"]
    assert gh.specific_glob("tools/*.py") and gh.specific_glob("**/Dockerfile")
    assert not gh.specific_glob("**/*.py")


def _holders(n, trigger, rule="Check the deploy revision on the host.", apply="read the label"):
    return [entry(f"feedback_h{i}", rule=rule, apply=apply, triggers=[trigger]) for i in range(n)]


def test_a_generic_trigger_stays_weak_when_its_holders_are_removed(env):
    # the earlier design limit: 6 holders of `ssh <host>` were weak, 1 to 3 were strong
    for n in (6, 3, 1):
        Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
            json.dumps({"entries": FILLER + _holders(n, "ssh alpha")})
        )
        e = dict(env, NOBLIVION_GUARD_STATE_DIR=env["NOBLIVION_GUARD_STATE_DIR"] + str(n))
        assert call(e, bash("ssh alpha 'docker ps'"), mod=gh_host) is None, n


def test_a_specific_trigger_is_strong_unless_a_crowd_holds_it(env):
    for n, rows in ((3, 3), (6, 0)):
        Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
            json.dumps({"entries": FILLER + _holders(n, "python3 tools/safe_merge_gate.py")})
        )
        out = call(
            dict(env, NOBLIVION_GUARD_STATE_DIR=env["NOBLIVION_GUARD_STATE_DIR"] + str(n)),
            bash("python3 tools/safe_merge_gate.py 2251"),
        )
        assert (out["additionalContext"].count("- Memory ") if out else 0) == rows, n


def test_a_rule_that_names_the_trigger_argument_makes_it_strong(env):
    tailnet = entry(
        "feedback_tailnet",
        rule="When connecting to the alpha host, use alpha-1 instead of the bare name alpha.",
        apply="ssh alpha-1",
        triggers=["ssh alpha"],
    )
    other = entry(
        "feedback_deploy_rev",
        rule="Check the deploy revision on the host.",
        apply="read the label",
        triggers=["ssh alpha"],
    )
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(
        json.dumps({"entries": FILLER + [tailnet, other]})
    )
    assert gh_host.rule_names_trigger("ssh alpha", tailnet)
    assert not gh_host.rule_names_trigger("ssh alpha", other)
    ctx = call(env, bash("ssh alpha 'docker ps'"), mod=gh_host)["additionalContext"]
    assert (
        "- Memory feedback_tailnet (rule and fix in full" in ctx
        and "feedback_deploy_rev" not in ctx
    )


def test_a_rule_that_names_a_weak_trigger_is_evidence_for_the_rule_line(env):
    named = entry(
        "feedback_exec_user",
        rule="When you run docker exec in app-devops, do not force -u appuser.",
        apply="docker exec app-devops sh",
        triggers=["docker exec"],
    )
    silent = entry(
        "feedback_exec_other",
        rule="Plant canaries in the persona namespace.",
        apply="touch it",
        triggers=["docker exec"],
    )
    table = {"entries": FILLER + [named, silent]}
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps(table))
    assert 0 < gh.named_evidence("docker exec", named, table) < gh.WEAK_FULL_SCORE
    assert gh.named_evidence("docker exec", silent, table) == 0.0
    ctx = call(env, bash("docker exec obs-db true"))["additionalContext"]
    assert ctx.splitlines()[1:] == [
        f"- Memory feedback_exec_user ({gh.WEAK_NOTE.format('docker exec')}):",
        "  Rule: When you run docker exec in app-devops, do not force -u appuser.",
    ]


def test_wi3h_grading_never_touches_a_deny(env):
    deny = entry(
        "feedback_no_force_h",
        rule="Never force-push.",
        apply="Use a new branch.",
        violates=FORCE,
        triggers=["git push --force"],
    )
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": FILLER + [deny]}))
    assert gh.weak_trigger("git push --force", deny)
    out = call(env, bash("git push --force origin x"))
    assert (
        out["permissionDecision"] == "deny"
        and "Never force-push." in out["permissionDecisionReason"]
    )


# review item 10 (2026-09-30): the log text of every line is redacted ----------
def test_the_log_text_of_a_deny_and_of_a_rows_line_is_redacted(env):
    tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    out = call(env, bash(f"GH_TOKEN={tok} git push --force origin x"))
    assert out["permissionDecision"] == "deny"
    call(ungraded(env), bash(f"git push github main --token {tok}", sid="s2"))
    lines = log(env)
    assert [x["decision"] for x in lines] == ["deny", "rows"]
    for x in lines:
        assert tok not in json.dumps(x) and "[redacted]" in x["text"]
    assert lines[0]["text"].endswith("git push --force origin x")


def test_the_log_text_of_a_file_path_is_kept(env):
    assert (
        gh._log_text({"tool_input": {"file_path": "/r/a1b2c3d4e5f6a7b8c9d0e1f2a3b4/x.py"}})
        == "/r/a1b2c3d4e5f6a7b8c9d0e1f2a3b4/x.py"
    )


# 13. NOBLIVION-50: a changed memory folder, and a timeout ---------------------
@pytest.fixture
def folder_env(tmp_path, monkeypatch, hook_env):
    """No table override: the hook reads the table of the memory folder
    ``mem`` under the tmp data dir, and builds it itself."""
    mem = tmp_path / "mem"
    mem.mkdir()
    e = {
        "HOME": str(hook_env["home"]),
        "NOBLIVION_DATA_DIR": str(hook_env["data"]),
        "NOBLIVION_MEMORY_DIR": str(mem),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    return e, mem


def _rulefile(folder, mid, word="pop"):
    """A memory file with a deny rule on ``git stash <word>``."""
    (folder / f"{mid}.md").write_text(
        f"---\nname: {mid}\ndescription: a lesson\ntype: feedback\n"
        f'rule: Never run git stash {word}.\napply: "Use git stash list."\nscope: tool\n'
        f'triggers: [git stash]\nviolates: "^git\\\\s+stash\\\\s+{word}\\\\s*$"\n'
        f'example_repeat: "git stash {word}"\nexample_ok: "git stash list"\n---\n\nBody.\n'
    )


def _denied(env, cmd):
    out = call(env, bash(cmd))
    return bool(out) and out.get("permissionDecision") == "deny"


def test_a_rule_deleted_with_rm_stops_denying_at_the_next_call(folder_env):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    _rulefile(mem, "feedback_no_stash_drop", "drop")
    assert _denied(env, "git stash pop") and _denied(env, "git stash drop")
    (mem / "feedback_no_stash_pop.md").unlink()
    assert not _denied(env, "git stash pop")
    assert _denied(env, "git stash drop")


@pytest.mark.parametrize("name", ["feedback_no_clear", "project_no_clear", "reference_no_clear"])
def test_a_rule_added_by_a_shell_command_denies_at_the_next_call(folder_env, name):
    """As ``git checkout`` or ``cp`` brings a file: no Write event, any kind."""
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    assert not _denied(env, "git stash clear")
    _rulefile(mem, name, "clear")
    assert _denied(env, "git stash clear")


def test_a_rule_edited_or_moved_by_a_shell_command_is_read_at_the_next_call(folder_env):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    assert _denied(env, "git stash pop") and not _denied(env, "git stash drop")
    _rulefile(mem, "feedback_no_stash_pop", "drop")  # ``sed -i``: the same file, a new regex
    assert _denied(env, "git stash drop") and not _denied(env, "git stash pop")
    (mem / "feedback_no_stash_pop.md").rename(mem / "feedback_no_stash_pop.md.off")  # ``mv``
    assert not _denied(env, "git stash drop")


def test_the_table_is_rebuilt_once_per_change_of_the_folder(folder_env, monkeypatch):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    gtm = gh._gt()
    real, calls = gtm.rebuild, []
    monkeypatch.setattr(gtm, "rebuild", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert _denied(env, "git stash pop") and len(calls) == 1  # no table yet
    assert _denied(env, "git stash pop") and not _denied(env, "ls") and len(calls) == 1
    (mem / "MEMORY.md").write_text("- an index line\n")  # not a memory file
    assert _denied(env, "git stash pop") and len(calls) == 1
    _rulefile(mem, "user_no_stash_drop", "drop")
    assert _denied(env, "git stash drop") and len(calls) == 2
    assert _denied(env, "git stash drop") and _denied(env, "git stash pop") and len(calls) == 2


def test_only_the_guard_legs_rebuild_a_stale_table(folder_env):
    """``table_file`` without ``fresh`` (the prompt leg of the label rows)
    builds a missing table, as before, and leaves a stale one."""
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    path = gh.table_file(env, "/tmp")
    built = path.read_text()
    _rulefile(mem, "feedback_no_stash_drop", "drop")
    assert gh.table_file(env, "/tmp") == path and path.read_text() == built
    assert gh.table_file(env, "/tmp", fresh=True) == path
    assert "feedback_no_stash_drop" in path.read_text()


def test_a_table_override_is_never_rebuilt_by_the_hook(env, tmp_path, monkeypatch):
    """``NOBLIVION_GUARD_TABLE`` pins one file: the hook reads it as it is."""
    mem = tmp_path / "mem"
    mem.mkdir()
    _rulefile(mem, "feedback_no_stash_clear", "clear")
    e = dict(env, NOBLIVION_MEMORY_DIR=str(mem))
    before = Path(env["NOBLIVION_GUARD_TABLE"]).read_text()
    assert not _denied(e, "git stash clear") and _denied(e, "git stash pop")
    assert Path(env["NOBLIVION_GUARD_TABLE"]).read_text() == before


ORDINARY = [
    "ls -la",
    "git status",
    "git push origin main",
    "git stash list",
    "cat README.md | head -5",
    "python3 -m pytest -q tests && echo done",
]


@pytest.mark.parametrize("notes", ["none", "plain"])
def test_an_ordinary_command_in_a_folder_with_no_rules_gets_no_deny_and_no_message(
    folder_env, notes
):
    env, mem = folder_env
    if notes == "plain":
        (mem / "user_profile.md").write_text("---\nname: profile\ntype: user\n---\n\nA note.\n")
        (mem / "MEMORY.md").write_text("- [profile](user_profile.md)\n")
    for cmd in ORDINARY:
        out = io.StringIO()
        assert gh.main(stdin=io.StringIO(json.dumps(bash(cmd))), stdout=out, environ=env) == 0
        assert out.getvalue() == ""
    (mem / "user_other.md").write_text("---\nname: other\ntype: user\n---\n\nA note.\n")
    for cmd in ORDINARY:  # after a change of the folder too
        out = io.StringIO()
        assert gh.main(stdin=io.StringIO(json.dumps(bash(cmd))), stdout=out, environ=env) == 0
        assert out.getvalue() == ""


def test_an_ordinary_command_next_to_a_rule_gets_no_deny_and_no_message(folder_env):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(bash("ls -la && git status")),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    (mem / "feedback_no_stash_pop.md").unlink()
    for cmd in ORDINARY + ["git stash pop"]:
        assert call(env, bash(cmd)) is None


def _timed_out(env, event, time_limit=0.2):
    out = io.StringIO()
    t0 = time.monotonic()
    rc = gh.main(
        stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env, time_limit=time_limit
    )
    assert rc == 0 and time.monotonic() - t0 < 1.5
    return out.getvalue()


def _timeout_doc():
    """What a timeout prints: one line for the user and for the model, no deny."""
    return {
        "systemMessage": gh.TIMEOUT_LINE,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": gh.TIMEOUT_LINE,
        },
    }


def test_a_deny_check_over_the_time_limit_tells_the_user(env):
    """The ticket's reproducer through ``main``: 1000 rules and a 90 KB
    command that ends in a force-push. The call is allowed (fail open), and
    one line says that the deny rules were not checked."""
    rules = [entry(f"feedback_r{i}", violates=rf"\btool{i}\s+drop\s+all\b") for i in range(1000)]
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": rules + BASE}))
    words = " ".join(f"word{i}" for i in range(10000))
    text = _timed_out(env, bash(f"echo {words} && git push --force origin main"))
    assert text.count("\n") == 1 and json.loads(text) == _timeout_doc()
    assert gh.TIMEOUT_LINE.startswith("NOBLIVION: ") and "not checked" in gh.TIMEOUT_LINE
    assert log(env)[-1]["error"] == "timeout"
    # with the time it needs, the same call is denied
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": BASE}))
    assert _denied(env, f"echo {words} && git push --force origin main")


def _slow_decide(monkeypatch):
    def slow(*_a, **_k):
        time.sleep(2)
        return gh.Decision()

    monkeypatch.setattr(gh, "decide", slow)


@pytest.mark.parametrize("tool", ["Read", "Grep"])
def test_a_timeout_of_the_credential_guard_tools_tells_the_user(env, monkeypatch, tool):
    _slow_decide(monkeypatch)
    ev = {"session_id": "s1", "tool_name": tool, "tool_input": {"file_path": "/r/x"}, "cwd": "/"}
    assert json.loads(_timed_out(env, ev)) == _timeout_doc()


def test_a_timeout_where_nothing_can_be_denied_prints_nothing(env, monkeypatch):
    """Edit, Write and MultiEdit are never denied, and the evidence leg
    (PostToolUseFailure) never denies: a timeout there loses no deny."""
    _slow_decide(monkeypatch)
    edit = {
        "session_id": "s1",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/r/x.py", "old_string": "a", "new_string": "b"},
        "cwd": "/",
    }
    failed = dict(bash("git stash list"), hook_event_name="PostToolUseFailure", error="Exit code 1")
    assert _timed_out(env, edit) == ""
    assert _timed_out(env, failed) == ""


def _sleeper(*_a, **_k):
    time.sleep(2)


def test_a_timeout_after_a_deny_check_that_found_nothing_prints_nothing(env, monkeypatch):
    """The line is for a deny that was not checked. When the check is done and
    found nothing, a later timeout (the rows, the state lock) loses rows only."""
    read = {
        "session_id": "s1",
        "tool_name": "Read",
        "tool_input": {"file_path": "/r/x"},
        "cwd": "/",
    }
    with monkeypatch.context() as m:
        m.setattr(gh, "command_rows", _sleeper)  # after ``match``
        assert _timed_out(env, bash("git push origin main")) == ""
        assert log(env)[-1]["error"] == "timeout"
    with monkeypatch.context() as m:
        m.setattr(gh, "_file_tools_on", _sleeper)  # after the credential guard
        assert _timed_out(env, read) == ""
    with monkeypatch.context() as m:
        m.setattr(gh, "SessionState", _sleeper)
        # a table with no rule: nothing to deny, so no line
        Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": []}))
        for cmd in ORDINARY:
            assert _timed_out(env, bash(cmd)) == ""
        # a hit that the hook could not print: the line
        Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"entries": BASE}))
        assert json.loads(_timed_out(env, bash("git push --force origin x"))) == _timeout_doc()
    with monkeypatch.context() as m:
        m.setattr(gh, "_credential_check", _sleeper)
        assert json.loads(_timed_out(env, read)) == _timeout_doc()


def test_a_timeout_after_the_deny_was_printed_adds_no_message(env, monkeypatch):
    def slow(_self):
        time.sleep(2)

    monkeypatch.setattr(gh.Decision, "commit", slow)
    text = _timed_out(env, bash("git push --force origin x"))
    assert text.count("\n") == 1
    assert json.loads(text)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "systemMessage" not in json.loads(text)


def test_a_rebuild_of_a_changed_folder_has_its_own_time_limit(folder_env, monkeypatch):
    """The rebuild times each regex in a child process, so it can take longer
    than the limit of the deny check. It must not turn the check off."""
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    gtm = gh._gt()
    real = gtm.rebuild

    def slow(*a, **k):
        time.sleep(0.6)
        return real(*a, **k)

    monkeypatch.setattr(gtm, "rebuild", slow)
    out = io.StringIO()
    ev = json.dumps(bash("git stash pop"))
    assert gh.main(stdin=io.StringIO(ev), stdout=out, environ=env, time_limit=0.4) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
    # the limit of the call is back after a rebuild
    _rulefile(mem, "feedback_no_stash_drop", "drop")
    monkeypatch.setattr(gtm, "rebuild", real)
    monkeypatch.setattr(gtm, "match", lambda *_a, **_k: time.sleep(2) or [])
    assert json.loads(_timed_out(env, bash("git stash drop"), time_limit=0.4)) == _timeout_doc()


def _main_doc(env, cmd, time_limit=0.4):
    """One call through ``main`` with a short time limit: the whole output document, or None."""
    text = _timed_out(env, bash(cmd), time_limit=time_limit).strip()
    return json.loads(text) if text else None


def _slow_rebuild(monkeypatch, calls, seconds=5.0):
    """Every rebuild of the hook takes ``seconds``; the limit of a rebuild is 0.2 seconds."""
    gtm = gh._gt()
    real = gtm.rebuild

    def slow(*a, **k):
        calls.append(1)
        time.sleep(seconds)
        return real(*a, **k)

    monkeypatch.setattr(gtm, "rebuild", slow)
    monkeypatch.setattr(gh, "REBUILD_LIMIT_S", 0.2)
    return real


def test_a_rebuild_over_its_time_limit_keeps_the_old_table_in_force(folder_env, monkeypatch):
    """The reviewer's case with a slow rebuild in place of 650 rules: a memory file changes, the
    rebuild runs out of time. The rules of the last build must still deny, in this call and in
    the next ones, and the user reads one line for this change of the folder, not one per call."""
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    assert _denied(env, "git stash pop")  # builds the table
    calls: list = []
    real = _slow_rebuild(monkeypatch, calls)
    _rulefile(mem, "feedback_no_stash_drop", "drop")  # ``sed -i``, ``cp``: no Write event

    doc = _main_doc(env, "git stash pop")
    assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert doc["systemMessage"] == gh.REBUILD_LATE_LINE and len(calls) == 1
    assert (
        gh.REBUILD_LATE_LINE.startswith("NOBLIVION: ") and "stay in force" in doc["systemMessage"]
    )
    # the next calls: the old rules, no second rebuild, no second line, no wait
    for _ in range(3):
        doc = _main_doc(env, "git stash pop")
        assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "systemMessage" not in doc
    assert _main_doc(env, "ls") is None
    assert _main_doc(env, "git stash drop") is None  # the rule of the new file is not in force yet
    assert len(calls) == 1
    lines = [x for x in log(env) if x["decision"] == "error"]
    assert len(lines) == 1 and "rebuild" in lines[0]["error"]

    # the folder changes again: one more try, and one more line when that try is late too
    _rulefile(mem, "feedback_no_stash_clear", "clear")
    doc = _main_doc(env, "git stash pop")
    assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert doc["systemMessage"] == gh.REBUILD_LATE_LINE and len(calls) == 2
    assert "systemMessage" not in _main_doc(env, "git stash pop") and len(calls) == 2

    # a rebuild that ends in time puts every rule in force and ends the late state
    monkeypatch.setattr(gh._gt(), "rebuild", real)
    _rulefile(mem, "feedback_no_stash_apply", "apply")
    for word in ("pop", "drop", "clear", "apply"):
        doc = _main_doc(env, f"git stash {word}")
        assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "systemMessage" not in doc
    assert not list(gh.table_file(env, "/tmp").parent.glob("*" + gh.LATE_SUFFIX))


def test_a_call_that_times_out_after_a_late_rebuild_prints_both_lines(folder_env, monkeypatch):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    assert _denied(env, "git stash pop")
    _slow_rebuild(monkeypatch, [])
    monkeypatch.setattr(gh._gt(), "match", lambda *_a, **_k: time.sleep(2) or [])
    _rulefile(mem, "feedback_no_stash_drop", "drop")
    doc = _main_doc(env, "git stash pop")
    assert doc["systemMessage"] == gh.TIMEOUT_LINE + " " + gh.REBUILD_LATE_LINE
    assert doc["hookSpecificOutput"] == _timeout_doc()["hookSpecificOutput"]
    assert _main_doc(env, "git stash pop") == _timeout_doc()  # the rebuild line came once


def test_a_late_rebuild_with_no_table_tells_the_user_once(folder_env, monkeypatch):
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    calls: list = []
    real = _slow_rebuild(monkeypatch, calls)
    doc = _main_doc(env, "git stash pop")
    assert doc["systemMessage"] == gh.REBUILD_NONE_LINE and "no deny rule" in doc["systemMessage"]
    assert "permissionDecision" not in doc["hookSpecificOutput"]
    assert _main_doc(env, "git stash pop") is None and _main_doc(env, "ls") is None
    assert len(calls) == 1
    # the session start builds the table (no limit there): the rule is in force again
    real([mem], gh.table_file(env, "/tmp"))
    assert _denied(env, "git stash pop") and len(calls) == 1


def test_a_late_rebuild_on_a_failure_event_is_tried_again_and_told_later(folder_env, monkeypatch):
    """A PostToolUseFailure event prints no line for the user, so it does not use up the one
    line of this change: the next PreToolUse call prints it."""
    env, mem = folder_env
    _rulefile(mem, "feedback_no_stash_pop")
    assert _denied(env, "git stash pop")
    calls: list = []
    _slow_rebuild(monkeypatch, calls)
    _rulefile(mem, "feedback_no_stash_drop", "drop")
    failed = dict(bash("git stash list"), hook_event_name="PostToolUseFailure", error="Exit code 1")
    assert _timed_out(env, failed, time_limit=0.4).strip() == "" and len(calls) == 1
    doc = _main_doc(env, "git stash pop")
    assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert doc["systemMessage"] == gh.REBUILD_LATE_LINE and len(calls) == 2


def test_a_rebuild_after_one_edit_times_only_the_changed_rule(folder_env, monkeypatch):
    """Through the hook: the regex probe of a rule runs once, not at each rebuild."""
    env, mem = folder_env
    for word in ("pop", "drop", "clear", "apply"):
        _rulefile(mem, f"feedback_no_stash_{word}", word)
    mfm = gh._gt()._mf()
    real, probes = mfm.subprocess.run, []
    monkeypatch.setattr(mfm.subprocess, "run", lambda *a, **k: probes.append(1) or real(*a, **k))
    assert _denied(env, "git stash pop") and len(probes) == 4  # no table yet: every rule
    target = mem / "feedback_no_stash_drop.md"
    target.write_text(target.read_text().replace("Body.", "Body, edited."))
    assert _denied(env, "git stash drop") and len(probes) == 4  # the text changed, no regex
    _rulefile(mem, "feedback_no_stash_drop", "show")
    assert _denied(env, "git stash show") and not _denied(env, "git stash drop")
    assert len(probes) == 5  # one regex changed
