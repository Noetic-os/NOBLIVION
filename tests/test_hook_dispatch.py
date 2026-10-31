# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dispatcher (``hooks/dispatch.py``, NOBLIVION-162): one process per event, not one per module.

What these tests hold:

1. ``hooks.json`` starts one synchronous dispatcher command for UserPromptSubmit, SessionStart and
   PreCompact, and the table in ``dispatch.PLAN`` is the old list of commands.
2. The same event through the old commands (one process per module) and through the dispatcher
   gives the same exit code and the same stdout for each module, and the answer Claude Code
   receives is the one object the merge rules give.
3. A module that raises, exits, runs too long, or changes the environment, the working directory
   or ``sys.argv`` does not change what another module prints.
4. The deny paths (the guard and the credential guard) and the async work are not in the table, and
   the guard command of ``hooks.json`` still denies as one process.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import recall_helpers
from hookload import HOOKS, load_hook
from recall_helpers import INDEX_FLOOR_OFF, FakeStore, hook_env

dispatch = load_hook("dispatch", "hooktest_dispatch")

ROOT = HOOKS.parent
HOOKS_JSON = HOOKS / "hooks.json"
CWD = "/work/proj/demo"
PY = sys.executable


# -- hooks.json against the table ------------------------------------------------------


def _hooks() -> dict[str, list[dict[str, Any]]]:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]


def _commands(event: str) -> list[tuple[str | None, dict[str, Any]]]:
    return [(g.get("matcher"), h) for g in _hooks()[event] for h in g["hooks"]]


WARM_GUARD = '[ -e "${NOBLIVION_DATA_DIR:-${CLAUDE_PLUGIN_DATA}}/warm-prompt-hooks" ] || '


def _command_hooks(event: str) -> list[tuple[str | None, dict[str, Any]]]:
    return [(m, h) for m, h in _commands(event) if h.get("type", "command") == "command"]


def _script(command: str) -> str:
    return re.search(r"hooks/([a-z_]+\.py)", command).group(1)


@pytest.mark.parametrize("event", sorted(dispatch.PLAN))
def test_hooks_json_has_one_sync_dispatcher_command_per_planned_event(event):
    sync = [(m, h) for m, h in _command_hooks(event) if not h.get("async")]
    assert len(sync) == 1, sync
    matcher, handler = sync[0]
    assert matcher is None
    # NOBLIVION-162 step 2: the prompt command steps aside when the warm hook is on
    guard = WARM_GUARD if event == "UserPromptSubmit" else ""
    run = f'python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/dispatch.py" {event}'
    assert handler["command"] == guard + run
    assert handler["timeout"] == dispatch.timeout_of(event) <= 60


def test_the_table_lists_exactly_the_modules_the_old_hooks_json_started():
    """The commands of NOBLIVION-149's hooks.json for these three events, in its order."""
    old = {
        "SessionStart": [
            ("store_client", ()),
            ("guard_table", ("--rebuild",)),
            ("recall_hook", ("--reset-rows",)),
            ("continuity_hook", ()),
            ("trust_session_line", ()),
            ("learning_capture", ()),
            ("learning_summary", ()),
        ],
        "UserPromptSubmit": [
            ("stop_checks", ("--mark-correction",)),
            ("learning_context", ()),
            ("recall_hook", ()),
        ],
        "PreCompact": [("continuity_hook", ()), ("learning_capture", ())],
    }
    for event, expect in old.items():
        steps, _ = dispatch.PLAN[event]
        assert [(s.module, s.argv) for s in steps] == expect
    matchers = {s.module: s.matcher for s in dispatch.PLAN["SessionStart"][0]}
    assert matchers["recall_hook"] == "compact"
    assert matchers["continuity_hook"] == "startup|resume|clear|compact"
    assert matchers["trust_session_line"] == "startup|resume|clear"
    assert dispatch.PLAN["PreCompact"][0][0].matcher == "manual|auto"


def test_every_planned_module_and_entry_exists():
    for steps, _ in dispatch.PLAN.values():
        for s in steps:
            source = (HOOKS / f"{s.module}.py").read_text(encoding="utf-8")
            assert f"def {s.entry}(" in source, s
            for flag in s.argv:
                assert f'"{flag}"' in source, s


def test_the_deny_paths_and_the_async_work_stay_their_own_commands():
    planned = {s.module for steps, _ in dispatch.PLAN.values() for s in steps}
    assert planned.isdisjoint(
        {"guard_hook", "credential_guard", "memory_sync_hook", "trust_flush", "version_check"}
    )
    for _matcher, handler in _commands("PreToolUse"):
        if _script(handler["command"]) == "guard_hook.py":
            assert handler["command"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard_hook.py"'
            assert not handler.get("async")
    for event in ("SessionStart", "Stop", "PostToolUse"):
        for _m, handler in _commands(event):
            if _script(handler["command"]) in ("version_check.py", "trust_flush.py"):
                assert handler.get("async") is True
            if _script(handler["command"]) == "memory_sync_hook.py":
                assert handler.get("async") is True


def test_processes_started_per_event():
    """The count NOBLIVION-162 reports: synchronous commands, then async ones."""

    def count(event: str, source: str | None = None) -> tuple[int, int]:
        sync = asyn = 0
        for matcher, handler in _command_hooks(event):
            if source is not None and matcher and not re.fullmatch(matcher, source):
                continue
            if handler.get("async"):
                asyn += 1
            else:
                sync += 1
        return sync, asyn

    assert count("UserPromptSubmit") == (1, 0)
    assert count("SessionStart", "startup") == (1, 1)
    assert count("SessionStart", "compact") == (1, 0)
    assert count("PreCompact", "auto") == (1, 0)
    assert count("Stop") == (1, 1)


# -- the same inputs through both paths ------------------------------------------------


def _old(module: str, argv: tuple[str, ...], event: dict[str, Any], env: dict[str, str]):
    p = subprocess.run(
        [PY, str(HOOKS / f"{module}.py"), *argv],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return p.returncode, p.stdout


def _new(name: str, event: dict[str, Any], env: dict[str, str]):
    p = subprocess.run(
        [PY, str(HOOKS / "dispatch.py"), "--raw", name],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _expected_parts(name: str, event: dict[str, Any], env: dict[str, str]):
    """What the old commands print for the steps whose matcher accepts the event."""
    steps, field = dispatch.PLAN[name]
    chosen = dispatch._selected(steps, field, event)
    return [(s.module, *_old(s.module, s.argv, event, env)) for s in chosen]


def _world(tmp_path: Path, name: str, store: FakeStore, memory: Path, **extra: str):
    """One env: its own data dir (the fake store's token in it), cache and stop-check state."""
    base = tmp_path / name
    recall_helpers.write_store_files(base / "data", store.port, store.token)
    (base / "home").mkdir(parents=True, exist_ok=True)
    return hook_env(
        base,
        HOME=str(base / "home"),
        NOBLIVION_RECALL_TIMEOUT_S="2.0",
        NOBLIVION_MEMORY_DIR=str(memory),
        NOBLIVION_STOP_CHECK_STATE=str(base / "stop"),
        NOBLIVION_TRUST_REPORT_FILE=str(base / "trust.json"),
        NOBLIVION_GUARD_TABLE=str(base / "table.json"),
        CLAUDE_PROJECT_DIR=CWD,
        **INDEX_FLOOR_OFF,
        **extra,
    )


@pytest.fixture
def store(tmp_path):
    s = FakeStore(tmp_path / "store-data")

    def search(path, query):
        project = (query.get("project") or [None])[0]
        text = "# feedback_x\n\n[claude_code_md: feedback_x.md]\n\nthe hit shape"
        return {"namespace": project, "results": [text]}

    s.route("/api/memories/search", search)
    yield s
    s.stop()


@pytest.fixture
def memory(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    # a deny rule that the table build drops: guard_table --rebuild prints one JSON line for it
    (mem / "feedback_no_force.md").write_text(
        "---\nname: feedback_no_force\ndescription: a lesson\ntype: feedback\n"
        'rule: Do the safe thing.\napply: "Use the safe command."\nscope: tool\n'
        "triggers: [git push --force origin main]\n"
        "violates: 'git\\s+push\\s+.*--force|.*'\n"
        "repeat: git push --force origin main\nok: ls\n---\n\nA lesson.\n",
        encoding="utf-8",
    )
    return mem


def _keep_reminder(env: dict[str, str], session: str) -> None:
    import time

    state = Path(env["NOBLIVION_STOP_CHECK_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    (state / f"{session}.reminder.json").write_text(
        json.dumps({"ts": time.time(), "lines": ["- a rule: left open"]}), encoding="utf-8"
    )


def _prompt(text: str = "merge the pull request", session: str = "sess-1") -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt": text,
        "cwd": CWD,
        "transcript_path": CWD + "/t.jsonl",
    }


def _flat(text: str) -> str:
    """The recall header names the milliseconds of the store call: not part of the comparison."""
    return re.sub(r", \d+ ms\)", ", N ms)", text)


def _same_parts(raw: dict[str, Any], expected: list[tuple[str, int, str]]):
    got = [(p["module"], p["code"], _flat(p["stdout"])) for p in raw["parts"]]
    assert got == [(m, c, _flat(o)) for m, c, o in expected]


def test_user_prompt_submit_gives_the_same_parts_and_one_merged_answer(tmp_path, store, memory):
    event = _prompt()
    old_env = _world(tmp_path, "old", store, memory)
    new_env = _world(tmp_path, "new", store, memory)
    _keep_reminder(old_env, "sess-1")
    _keep_reminder(new_env, "sess-1")
    expected = _expected_parts("UserPromptSubmit", event, old_env)
    raw = _new("UserPromptSubmit", event, new_env)
    _same_parts(raw, expected)
    printing = [(m, out) for m, code, out in expected if out.strip()]
    # two modules print: the kept stop-check reminder (JSON) and the recall rows (plain text,
    # which Claude Code reads as context)
    assert [m for m, _ in printing] == ["stop_checks", "recall_hook"]
    reminder = json.loads(printing[0][1])["hookSpecificOutput"]["additionalContext"]
    rows = printing[1][1].strip()
    assert "left open" in reminder and "the hit shape" in rows
    merged = json.loads(_flat(raw["stdout"]))
    assert merged == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _flat(reminder + "\n" + rows),
        }
    }


def test_user_prompt_submit_with_one_printing_module_passes_its_bytes_through(
    tmp_path, store, memory
):
    event = _prompt()
    old_env = _world(tmp_path, "old", store, memory)
    new_env = _world(tmp_path, "new", store, memory)
    expected = _expected_parts("UserPromptSubmit", event, old_env)
    raw = _new("UserPromptSubmit", event, new_env)
    _same_parts(raw, expected)
    (only,) = [out for _, _, out in expected if out.strip()]
    assert "the hit shape" in only
    assert _flat(raw["stdout"]) == _flat(only)


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_session_start_gives_the_same_parts_for_each_source(tmp_path, store, memory, source):
    event = {"hook_event_name": "SessionStart", "session_id": "s2", "source": source, "cwd": CWD}
    old_env = _world(tmp_path, "old", store, memory)
    new_env = _world(tmp_path, "new", store, memory)
    for env in (old_env, new_env):
        Path(env["NOBLIVION_TRUST_REPORT_FILE"]).write_text(
            json.dumps({"counts": {"retire": 2, "promote": 0, "demote": 1}}), encoding="utf-8"
        )
    expected = _expected_parts("SessionStart", event, old_env)
    raw = _new("SessionStart", event, new_env)
    _same_parts(raw, expected)
    names = [m for m, _, _ in expected]
    assert ("recall_hook" in names) == (source == "compact")
    assert ("trust_session_line" in names) == (source != "compact")
    printing = [json.loads(out) for _, _, out in expected if out.strip()]
    assert printing, "guard_table prints the line for the dropped rule"
    assert json.loads(raw["stdout"]) == dispatch._merge_docs(printing, "SessionStart")
    merged = json.loads(raw["stdout"])
    assert "skipped" in merged["systemMessage"]
    if source != "compact":
        assert "Memory trust: 2 to retire" in merged["systemMessage"]
        assert merged["systemMessage"].count("\n") == 1


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_pre_compact_gives_the_same_parts(tmp_path, store, memory, trigger):
    event = {"hook_event_name": "PreCompact", "session_id": "s3", "trigger": trigger, "cwd": CWD}
    old_env = _world(tmp_path, "old", store, memory)
    new_env = _world(tmp_path, "new", store, memory)
    expected = _expected_parts("PreCompact", event, old_env)
    _same_parts(_new("PreCompact", event, new_env), expected)
    assert [m for m, _, _ in expected] == ["continuity_hook", "learning_capture"]


def test_an_event_without_the_matcher_field_runs_the_steps_without_a_matcher(
    tmp_path, store, memory
):
    event = {"hook_event_name": "PreCompact", "session_id": "s3", "cwd": CWD}
    raw = _new("PreCompact", event, _world(tmp_path, "new", store, memory))
    assert [p["module"] for p in raw["parts"]] == ["learning_capture"]


# -- a fault stays in its module ---------------------------------------------------------


@pytest.fixture
def fake_hooks(tmp_path, monkeypatch):
    """The dispatcher over a folder of small test modules, run in this process."""
    folder = tmp_path / "fake-hooks"
    folder.mkdir()
    monkeypatch.setattr(dispatch, "HOOKS_DIR", folder)
    monkeypatch.setattr(dispatch, "_LOADED", {})
    monkeypatch.setattr(dispatch, "RESERVE_S", 0.2)

    def make(name: str, body: str) -> None:
        (folder / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")

    def plan(*steps):
        monkeypatch.setitem(dispatch.PLAN, "UserPromptSubmit", (tuple(steps), ""))

    make.plan = plan  # type: ignore[attr-defined]
    return make


S = dispatch.Step


def _run(text: str = '{"prompt": "x"}'):
    parts = dispatch.run_event("UserPromptSubmit", text)
    return parts, dispatch.combine(parts, "UserPromptSubmit")


PRINTER = """
    import json, os, sys
    def main():
        event = json.loads(sys.stdin.read())
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
            "additionalContext": "seen:" + str(event.get("prompt")) + ":"
            + str(os.environ.get("LEAK")) + ":" + str(len(sys.argv)) + ":" + os.getcwd()}}))
        return 0
"""


def _context(part) -> str:
    return json.loads(part.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "bad",
    [
        "def main():\n    raise RuntimeError('boom')\n",
        "def main():\n    import sys\n    sys.exit(1)\n",
        'def main():\n    print(\'{"systemMessage": "from a failed module"}\')\n    return 3\n',
        "def main():\n    raise KeyboardInterrupt\n",
        "raise ImportError('does not load')\n",
    ],
)
def test_a_failed_module_does_not_change_another_modules_output(fake_hooks, bad):
    fake_hooks("first", PRINTER)
    fake_hooks("bad", bad)
    fake_hooks("last", PRINTER)
    fake_hooks.plan(S(None, "first", (), 5), S(None, "bad", (), 5), S(None, "last", (), 5))
    alone = dispatch._run_step(S(None, "last", (), 5), '{"prompt": "x"}', 5)
    parts, (code, out, _err) = _run()
    first, failed, last = parts
    assert first.stdout == last.stdout == alone.stdout
    assert (first.code, last.code) == (0, 0) and failed.code != 0
    assert code == 0
    assert "from a failed module" not in out
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == (
        _context(alone) + "\n" + _context(alone)
    )


def test_a_module_that_changes_the_process_does_not_change_the_next_module(fake_hooks, tmp_path):
    fake_hooks("first", PRINTER)
    fake_hooks(
        "dirty",
        f"""
        import os, sys
        def main():
            os.environ["LEAK"] = "yes"
            os.chdir({str(tmp_path)!r})
            sys.argv.append("extra")
            sys.stdout.write("half a line")
            return 0
        """,
    )
    fake_hooks("last", PRINTER)
    fake_hooks.plan(S(None, "first", (), 5), S(None, "dirty", (), 5), S(None, "last", (), 5))
    before = (dict(os.environ), os.getcwd(), list(sys.argv))
    parts, (code, out, _err) = _run()
    assert (dict(os.environ), os.getcwd(), list(sys.argv)) == before
    first, dirty, last = parts
    assert first.stdout == last.stdout
    assert "seen:x:None:1:" in _context(last)
    assert dirty.stdout == "half a line"


def test_a_module_that_runs_too_long_is_cut_and_the_next_one_still_runs(fake_hooks):
    fake_hooks("slow", "import time\ndef main():\n    time.sleep(30)\n    return 0\n")
    fake_hooks(
        "stubborn",
        """
        import time
        def main():
            try:
                time.sleep(30)
            except BaseException:
                pass
            print('{"systemMessage": "stubborn answered"}')
            return 0
        """,
    )
    fake_hooks("last", PRINTER)
    fake_hooks.plan(S(None, "slow", (), 1), S(None, "stubborn", (), 1), S(None, "last", (), 5))
    parts, (code, out, err) = _run()
    assert parts[0].code == 1 and "ran out of time" in parts[0].stderr
    assert parts[2].code == 0 and "seen:x" in parts[2].stdout
    assert code == 0 and "seen:x" in out
    assert sum(p.ms for p in parts[:2]) < 4000


def test_the_steps_share_the_limit_of_the_dispatcher_command(fake_hooks, monkeypatch):
    fake_hooks("a", "import time\ndef main():\n    time.sleep(30)\n    return 0\n")
    fake_hooks("b", PRINTER)
    fake_hooks.plan(S(None, "a", (), 30), S(None, "b", (), 30))
    monkeypatch.setattr(dispatch, "timeout_of", lambda event: 1)
    parts, (code, out, _err) = _run()
    assert parts[0].code == 1 and "ran out of time" in parts[0].stderr
    assert parts[0].ms < 1500
    assert code == 0  # the second step got what was left, or none: either way the call returns


def test_exit_code_two_blocks_and_nothing_else_prints(fake_hooks):
    fake_hooks("first", PRINTER)
    fake_hooks(
        "blocker", "import sys\ndef main():\n    sys.stderr.write('stop here\\n')\n    return 2\n"
    )
    fake_hooks.plan(S(None, "first", (), 5), S(None, "blocker", (), 5))
    _parts, (code, out, err) = _run()
    assert (code, out, err) == (2, "", "stop here\n")


# -- the merge -------------------------------------------------------------------------


def _combine(*stdouts: str, event: str = "SessionStart"):
    parts = [dispatch.Part("m", (), 0, s, "", 0) for s in stdouts]
    return dispatch.combine(parts, event)


def test_one_answer_passes_through_byte_for_byte():
    raw = '{"systemMessage":   "keep  my spacing"}\n'
    assert _combine("", raw, "  \n") == (0, raw, "")
    assert _combine("", "") == (0, "", "")


def test_system_messages_and_contexts_join_in_table_order():
    a = json.dumps(
        {
            "systemMessage": "one",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx1"},
        }
    )
    b = json.dumps({"systemMessage": "two"})
    c = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx3"}}
    )
    code, out, _ = _combine(a, b, c)
    assert code == 0
    assert json.loads(out) == {
        "systemMessage": "one\ntwo",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx1\nctx3"},
    }


def test_plain_text_counts_as_context():
    doc = json.dumps({"systemMessage": "m"})
    _, out, _ = _combine("plain words\n", doc, event="UserPromptSubmit")
    assert json.loads(out) == {
        "systemMessage": "m",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "plain words",
        },
    }


def test_block_and_stop_win_over_other_answers():
    block = json.dumps({"decision": "block", "reason": "r1"})
    other = json.dumps(
        {"decision": "approve", "reason": "r2", "continue": False, "stopReason": "s"}
    )
    _, out, _ = _combine(other, block, json.dumps({"continue": True}))
    assert json.loads(out) == {
        "decision": "block",
        "reason": "r2\nr1",
        "continue": False,
        "stopReason": "s",
    }


def test_a_part_that_failed_keeps_no_stdout():
    ok = json.dumps({"systemMessage": "ok"})
    parts = [
        dispatch.Part("a", (), 0, ok, "", 0),
        dispatch.Part("b", (), 1, json.dumps({"systemMessage": "no"}), "trace\n", 0),
    ]
    assert dispatch.combine(parts, "SessionStart") == (0, ok, "trace\n")


def test_main_rejects_an_unknown_event(capsys):
    assert dispatch.main(["Nope"]) == 1
    assert dispatch.main([]) == 1
    assert "usage" in capsys.readouterr().err


# -- the deny path is the same command -------------------------------------------------


def _hook_command(event: str, matcher: str, script: str) -> str:
    for m, handler in _commands(event):
        if m == matcher and _script(handler["command"]) == script:
            return handler["command"]
    raise AssertionError((event, matcher, script))


def test_the_guard_command_of_hooks_json_denies_a_credential_read(tmp_path):
    """The deny path as Claude Code starts it: the command line of hooks.json, in a shell."""
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    secret = "s3cretTokenValue0123456789"  # gitleaks:allow - a fake test value
    for args in (
        ["init", "-q"],
        ["remote", "add", "origin", f"https://bot-user:{secret}@example.invalid/owner/repo.git"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    env = hook_env(
        tmp_path,
        HOME=str(home),
        CLAUDE_PLUGIN_ROOT=str(ROOT),
        NOBLIVION_MEMORY_DIR=str(tmp_path / "memory"),
        NOBLIVION_GUARD_TABLE=str(tmp_path / "table.json"),
        NOBLIVION_GUARD_LOG=str(tmp_path / "guard-log.jsonl"),
        CLAUDE_PROJECT_DIR=str(repo),
        GIT_CONFIG_GLOBAL=str(tmp_path / "no-global-gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
    )
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "g1",
        "tool_name": "Bash",
        "tool_input": {"command": "git remote -v"},
        "cwd": str(repo),
    }
    command = _hook_command("PreToolUse", "Bash", "guard_hook.py")
    p = subprocess.run(
        command,
        shell=True,  # noqa: S602 - the command line of hooks.json, as Claude Code runs it
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("Credential guard:")
    assert secret not in p.stdout and secret not in p.stderr
