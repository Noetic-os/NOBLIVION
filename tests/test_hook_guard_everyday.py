# SPDX-License-Identifier: AGPL-3.0-or-later
"""Everyday triggers are graded by relevance, not dropped (NOBLIVION-129).

What these tests hold:

1. A note whose trigger is an everyday command (``pytest``, ``kill``,
   ``git worktree``) gives a row when it fits the call, and none when it does
   not. It fits when the note's rule names the command, or when the rule shares
   enough words with the context of the call (the command, its description and
   the last prompt of the session transcript).
2. ``python3 -m X``, ``python -m X`` and ``<path to python> -m X`` fire a
   trigger ``X``.
3. At most 3 rows come from everyday triggers in one call, and the per-session
   repeat limit holds.
4. The switch ``NOBLIVION_GUARD_EVERYDAY_TRIGGERS`` (on by default) off gives
   the rows before the change. The weak-trigger grading off (``WEAK_SHARE=0``)
   gives no everyday row.
5. The change makes rows only. A call with a deny is the same with the switch
   on and off.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook_with_config

gh = load_hook_with_config("guard_hook", {}, "guard_hook_t_everyday")

SWITCH = "NOBLIVION_GUARD_EVERYDAY_TRIGGERS"


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    import os

    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


def entry(mid, rule, apply="", triggers=(), violates=""):
    return {
        "id": mid,
        "rule": rule,
        "apply": apply or f"apply of {mid}",
        "scope": "tool",
        "triggers": list(triggers),
        "violates": violates,
    }


QUIET = entry(
    "feedback_pytest_quiet",
    "Run pytest quiet: pass -q so the output stays short.",
    "pytest -q",
    ["pytest"],
)
WORKTREE = entry(
    "feedback_check_fleet_prs",
    "Check the open fleet pull requests before you start ticket work.",
    "Run gh pr list for the repository first.",
    ["git worktree"],
)
NO_PKILL = entry(
    "feedback_no_pkill_f",
    "Never use pkill -f to stop a runner. Stop the runner by its process id.",
    "Find the runner process id, then signal that id only.",
    ["kill"],
)
FILLER = [
    entry("f_a", "Keep the changelog entry short.", "One line.", ["glob:CHANGELOG.md"]),
    entry("f_b", "Name every branch after its ticket.", "Branch from main.", ["git checkout -b"]),
    entry("f_c", "Rotate the staging password monthly.", "Use the vault.", ["vault write"]),
    entry("f_d", "Label every image with its commit.", "Pass a build argument.", ["docker build"]),
    entry(
        "f_e", "Review a migration before it runs.", "Read the generated sql.", ["alembic upgrade"]
    ),
    entry("f_f", "Pin the compiler version in CI.", "Edit the workflow file.", ["glob:.ci/*.yml"]),
    entry("f_g", "Back up the volume before a resize.", "Snapshot it.", ["lvextend"]),
    entry("f_h", "Prefer the mirror for large downloads.", "Set the index url.", ["pip download"]),
]
FORCE = r"git\s+push\s+.*--force(?!-with-lease)"
NO_FORCE = entry(
    "feedback_no_force",
    "Never force-push.",
    "Use a new branch instead.",
    ["git push --force"],
    FORCE,
)


def make_env(tmp_path, entries, **extra):
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"version": 1, "entries": entries}))
    env = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }
    env.update(extra)
    return env


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path, [QUIET, WORKTREE, NO_PKILL] + FILLER)


def transcript(tmp_path, *prompts):
    path = tmp_path / "t.jsonl"
    rows = []
    for text in prompts:
        rows.append({"type": "user", "message": {"role": "user", "content": text}})
        rows.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def bash(cmd, sid="s1", path="", desc=""):
    ti: dict[str, Any] = {"command": cmd}
    if desc:
        ti["description"] = desc
    ev: dict[str, Any] = {"session_id": sid, "tool_name": "Bash", "tool_input": ti, "cwd": "/tmp"}
    if path:
        ev["transcript_path"] = path
    return ev


def call(env, event) -> Any:
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def shown(out) -> list[str]:
    """The ids of the notes that a call shows."""
    if out is None:
        return []
    text = out.get("additionalContext", "")
    ids = [e["id"] for e in [QUIET, WORKTREE, NO_PKILL, NO_FORCE] if e["id"] in text]
    return ids


def log(env):
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# 1. the exact command --------------------------------------------------------
@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -v",
        "python3 -m pytest -v",
        "python -m pytest -v tests/x",
        "/opt/venv/bin/python -m pytest -v",
        "cd /repo && python3 -m pytest -v | tail -5",
    ],
)
def test_pytest_note_fires_on_each_form_of_the_command(env, cmd):
    out = call(env, bash(cmd))
    assert "permissionDecision" not in out
    assert shown(out) == [QUIET["id"]]
    assert log(env)[-1]["everyday"] == [QUIET["id"]]


def test_the_pytest_note_stays_quiet_on_another_command(env):
    assert call(env, bash("ls -la")) is None
    assert call(env, bash("python3 -m http.server")) is None


# 2. the context of the call -----------------------------------------------------
def test_worktree_note_fires_when_the_prompt_matches(env, tmp_path):
    path = transcript(
        tmp_path, "Start the ticket work, but check the open fleet pull requests first."
    )
    out = call(env, bash("git worktree list", path=path))
    assert shown(out) == [WORKTREE["id"]]


def test_worktree_note_stays_quiet_for_an_unrelated_prompt(env, tmp_path):
    path = transcript(tmp_path, "Fix the typo in the readme and rename the helper.")
    assert call(env, bash("git worktree list", path=path)) is None


def test_worktree_note_stays_quiet_without_context(env):
    assert call(env, bash("git worktree list")) is None


def test_the_newest_prompt_counts(env, tmp_path):
    path = transcript(
        tmp_path,
        "Check the open fleet pull requests before the ticket work.",
        "Fix the typo in the readme.",
    )
    assert call(env, bash("git worktree list", path=path)) is None


def test_the_call_description_is_context(env):
    desc = "List worktrees before ticket work, as the open fleet pull requests rule says"
    assert shown(call(env, bash("git worktree list", desc=desc))) == [WORKTREE["id"]]


def test_kill_fires_only_with_relevance(env, tmp_path):
    assert call(env, bash("kill 4711")) is None
    other = transcript(tmp_path, "Rename the helper and update the readme.")
    assert call(env, bash("kill 4711", path=other)) is None
    path = transcript(tmp_path, "The runner is stuck, stop that runner process.")
    assert shown(call(env, bash("kill 4711", path=path))) == [NO_PKILL["id"]]


def test_a_prompt_in_a_tool_result_or_a_system_note_is_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [
        {"message": {"role": "user", "content": "check the open fleet pull requests"}},
        {"message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
        {"message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
        {"message": {"role": "user", "content": [{"type": "text", "text": ""}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken")
    assert gh.last_prompt(str(path)) == "check the open fleet pull requests"
    assert gh.last_prompt(str(tmp_path / "absent.jsonl")) == ""
    assert gh.last_prompt(None) == "" and gh.last_prompt("") == ""


# 3. the cap and the repeat limit --------------------------------------------------
def test_at_most_three_rows_come_from_everyday_triggers(tmp_path):
    many = [
        entry(f"n_{i}", "Run pytest quiet in the project.", f"pytest -q {i}", ["pytest"])
        for i in range(6)
    ]
    env = make_env(tmp_path, many + FILLER)
    table = gh._gt().load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    cands = gh.everyday_rows("pytest -v", table)
    assert len(cands) == 6
    rows, _only = gh.grade_everyday(cands, "pytest -v", lambda: "pytest -v", table)
    assert gh.EVERYDAY_MAX == 3 and len(rows) == 3
    out = call(env, bash("pytest -v"))
    assert out["additionalContext"].count("Run pytest quiet") == 3
    assert len(log(env)[-1]["everyday"]) == 3


def test_a_row_is_not_repeated_on_every_call(env):
    results = [call(env, bash("pytest -v")) for _ in range(5)]
    assert [r is not None for r in results] == [True, True, True, False, False]


# 4. the switches ---------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["pytest -v", "python3 -m pytest -v"])
def test_switch_off_restores_the_old_behaviour(tmp_path, cmd):
    env = make_env(tmp_path, [QUIET, WORKTREE, NO_PKILL] + FILLER, **{SWITCH: "0"})
    assert call(env, bash(cmd)) is None


def test_switch_off_is_the_command_rows_of_before(tmp_path, env):
    table = gh._gt().load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    assert gh.command_rows("pytest -v", table) == []
    assert gh.command_rows("python3 -m pytest tests/slow", table) == []


def test_switch_is_on_by_default_and_reads_the_config_key(tmp_path):
    assert gh.everyday_on({}) is True
    assert gh.everyday_on({SWITCH: "0"}) is False
    assert gh.everyday_on({SWITCH: "1"}) is True
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"guard": {"everyday_triggers": False}}))
    assert gh.everyday_on({"NOBLIVION_CONFIG": str(cfg)}) is False
    assert gh.everyday_on({"NOBLIVION_CONFIG": str(cfg), SWITCH: "1"}) is True  # env wins


def test_grading_off_gives_no_everyday_row(tmp_path):
    env = make_env(tmp_path, [QUIET] + FILLER, NOBLIVION_GUARD_WEAK_SHARE="0")
    assert call(env, bash("pytest -v")) is None


def test_python_m_form_fires_a_specific_trigger_too(tmp_path):
    note = entry("slow_tests", "Skip the slow tests on a laptop.", "", ["pytest tests/slow"])
    env = make_env(tmp_path, [note] + FILLER)
    table = gh._gt().load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    cmd = "python3 -m pytest tests/slow -x"
    assert gh.command_rows(cmd, table) == []
    assert [e["id"] for _w, _t, e in gh.command_rows(cmd, table, True)] == ["slow_tests"]
    assert "slow_tests" in call(env, bash(cmd))["additionalContext"]
    assert call(dict(env, **{SWITCH: "0"}), bash(cmd, sid="s2")) is None


# 5. rows only -----------------------------------------------------------------------------
def test_a_deny_is_the_same_with_the_switch_on_and_off(tmp_path):
    entries = [NO_FORCE, QUIET, NO_PKILL] + FILLER
    on = make_env(tmp_path, entries)
    off = dict(on, **{SWITCH: "0", "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state2")})
    cmd = "git push --force origin x; pytest -v; kill 4711"
    a, b = call(on, bash(cmd)), call(off, bash(cmd))
    assert a == b and a["permissionDecision"] == "deny"
    assert "feedback_pytest_quiet" not in a["permissionDecisionReason"]


def test_everyday_triggers_never_deny(env, tmp_path):
    path = transcript(tmp_path, "stop that runner process, check the open fleet pull requests")
    for cmd in ("pytest -v", "git worktree list", "kill 4711", "python3 -m pytest -q"):
        out = call(env, bash(cmd, sid=cmd, path=path))
        assert out is None or "permissionDecision" not in out


def test_an_error_in_the_everyday_leg_keeps_the_old_rows(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(gh, "everyday_rows", boom)
    assert call(env, bash("pytest -v")) is None
    monkeypatch.undo()
    monkeypatch.setattr(gh, "grade_everyday", boom)
    assert call(env, bash("pytest -v", sid="s9")) is None


# 6. noise bounds ---------------------------------------------------------------------------
def test_a_text_plumbing_trigger_stays_dropped(tmp_path):
    note = entry("no_tail_f", "Never tail a log with -f: tail it with -n.", "", ["tail"])
    env = make_env(tmp_path, [note] + FILLER)
    table = gh._gt().load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    assert gh.everyday_rows("tail -f app.log", table) == []
    assert call(env, bash("tail -f app.log")) is None


def test_everyday_rows_fill_free_places_and_never_displace_another_row(tmp_path):
    strong = [
        entry(f"db_{i}", f"Read the schema before db change {i}.", "", ["docker exec app-db"])
        for i in range(3)
    ]
    env = make_env(tmp_path, strong + [QUIET] + FILLER)
    out = call(env, bash("docker exec app-db psql; pytest -v"))
    text = out["additionalContext"]
    assert all(f"db_{i}" in text for i in range(3)) and QUIET["id"] not in text
    # with a free place, the same note shows, as a Rule line only
    (tmp_path / "b").mkdir()
    env2 = make_env(tmp_path / "b", [strong[0], QUIET] + FILLER)
    text2 = call(env2, bash("docker exec app-db psql; pytest -v"))["additionalContext"]
    assert "db_0" in text2 and QUIET["id"] in text2
    assert "weak match on `pytest`" in text2 and "Apply:" not in text2.split(QUIET["id"])[1]


def test_everyday_rows_have_a_session_budget_of_their_own(env, monkeypatch):
    monkeypatch.setattr(gh, "EVERYDAY_SESSION_CHARS", 1)
    assert call(env, bash("pytest -v")) is None
    monkeypatch.setattr(gh, "EVERYDAY_SESSION_CHARS", 100000)
    assert shown(call(env, bash("pytest -v", sid="s2"))) == [QUIET["id"]]


def test_everyday_rows_rank_after_the_weak_rows_of_other_triggers(tmp_path):
    weak = [
        entry(f"z_lsblk_{i}", f"Run lsblk with -f only, case {i}.", "", ["lsblk"]) for i in range(3)
    ]
    env = make_env(tmp_path, weak + [QUIET] + FILLER)
    text = call(env, bash("lsblk; pytest -v"))["additionalContext"]
    assert all(f"z_lsblk_{i}" in text for i in range(3)) and QUIET["id"] not in text
