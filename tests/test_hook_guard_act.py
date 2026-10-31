# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules act at the tool call: rewrite and ask (``hooks/guard_hook.py``, NOBLIVION-122).

What these tests hold:

1. Switches off (the default): a ``rewrite`` and an ``on_violation`` field in the
   table change nothing and write no log line. The deny is as before.
2. ``NOBLIVION_GUARD_REWRITE`` on: one hit with a valid rewrite becomes
   ``updatedInput`` with ``ask`` (default) or ``allow``, a one-line reason that
   names only the memory id, and a log line ``rewrote``. Other tool input keys
   stay. Nothing is charged, so a retry is not escalated.
3. A rewrite is never made when it is doubtful: more than one hit, a command
   that is not exactly the known shape, a new command that still hits a rule or
   misses ``complies``, or a ``# guard-ok`` marker. The deny stands and the log
   says why (``rewrite-skipped``).
4. ``NOBLIVION_GUARD_ASK`` on: a memory marked ``on_violation: ask`` makes the
   guard ask with the rule as the reason (log ``asked``). Mixed with a deny rule,
   the deny stands.
5. ``NOBLIVION_GUARD_ACT_SHADOW``: only the log lines ``would-rewrite`` and
   ``would-ask``; the output is the deny.
6. The table build carries the two fields, and drops a bad one alone.
7. A rewrite and an ask are trust events (use, sources ``rewrite`` and
   ``guard_ask``); a shadow line or a skip is not.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook_with_config

gh = load_hook_with_config("guard_hook", {}, "guard_hook_act_t")
gt = load_hook_with_config("guard_table", {}, "guard_table_act_t")
mf = load_hook_with_config("memory_fields", {}, "memory_fields_act_t")
te = load_hook_with_config("trust_events", {}, "trust_events_act_t")

PYTEST_V = r"^pytest\b(?!.*\s-q\b)"
LOG_V = r"^git\s+log\b(?!.*(?:--oneline|--pretty|--format|--stat|--graph|\s-p\b|\s-s\b))"
ASK_V = r"^git\s+remote\s+prune\b"
FORCE = r"git\s+push\s+.*--force(?!-with-lease)"


def entry(mid, violates, rewrite="", on_violation="", complies="", apply=None):
    e: dict[str, Any] = {
        "id": mid,
        "rule": f"rule of {mid}.",
        "apply": apply or f"apply of {mid}",
        "scope": "tool",
        "triggers": [],
        "violates": violates,
        "complies": complies,
        "run_first": "",
    }
    if rewrite:
        e["rewrite"] = rewrite
    if on_violation:
        e["on_violation"] = on_violation
    return e


TABLE = [
    entry(
        "feedback_pytest_quiet",
        PYTEST_V,
        rewrite="add-flag -q after pytest",
        complies=r"^pytest\s+-q\b",
    ),
    entry("feedback_log_cap", LOG_V, rewrite="add-flag --oneline after git log"),
    entry("feedback_ask_prune", ASK_V, on_violation="ask"),
    entry("feedback_no_force", FORCE),
    # a rewrite that lacks the compliant form of the memory
    entry(
        "feedback_no_complies",
        r"^git\s+status\b(?!.*-sb)",
        rewrite="add-flag -sb after git status",
        complies=r"^git\s+status\s+--porcelain",
    ),
]

# Two more notes on the pytest program. NOBLIVION-155: ``uv run pytest`` and
# ``python -m pytest`` are read as ``pytest``, so these cannot sit in the table
# with the pytest note above (two hits: no rewrite). They get a table of their own.
EXTRA = [
    # a rule whose rewrite does not remove the hit (a bad pair of regex and rewrite)
    entry("feedback_bad_pair", r"^uv\s+run\s+pytest\b", rewrite="add-flag -q after uv run pytest"),
    # a rewrite and an ask together: the rewrite is tried first
    entry(
        "feedback_both",
        r"^python\s+-m\s+pytest\b(?!.*\s-q\b)",
        rewrite="add-flag -q after python -m pytest",
        on_violation="ask",
    ),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import os

    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


@pytest.fixture
def env(tmp_path):
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"version": 1, "entries": [dict(e) for e in TABLE]}))
    return {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        "NOBLIVION_CONFIG": str(tmp_path / "no-config.json"),
        "NOBLIVION_TRUST_EVENTS": "0",
    }


@pytest.fixture
def env_extra(env, tmp_path):
    table = tmp_path / "table-extra.json"
    table.write_text(json.dumps({"version": 1, "entries": [dict(e) for e in EXTRA]}))
    return dict(env, NOBLIVION_GUARD_TABLE=str(table))


def on(env, *names):
    return dict(env, **{n: "1" for n in names})


def bash(cmd, sid="s1", **more):
    ti = {"command": cmd, **more}
    return {"session_id": sid, "tool_name": "Bash", "tool_input": ti, "cwd": "/tmp"}


def call(env, event):
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def lines(env):
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def decisions(env):
    return [d["decision"] for d in lines(env)]


# 1. switches off -----------------------------------------------------------
def test_with_every_switch_off_a_rewrite_note_does_not_deny(env):
    # NOBLIVION-122c: a rewrite note is not a deny note. With every switch off
    # the plain command runs unchanged and nothing is logged.
    assert call(env, bash("pytest tests")) is None
    assert decisions(env) == []


def test_with_every_switch_off_an_ask_field_still_denies(env):
    assert call(env, bash("git remote prune origin"))["permissionDecision"] == "deny"
    assert decisions(env) == ["deny"]


# 2. rewrite ----------------------------------------------------------------
def test_a_rewrite_asks_by_default_with_the_new_command(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    out = call(e, bash("pytest tests", description="run", timeout=5000))
    assert out["permissionDecision"] == "ask"
    assert out["updatedInput"] == {
        "command": "pytest -q tests",
        "description": "run",
        "timeout": 5000,
    }
    assert (
        out["permissionDecisionReason"]
        == "NOBLIVION rewrote this command (memory feedback_pytest_quiet)"
    )
    assert "pytest -q tests" in out["additionalContext"]
    assert "apply of feedback_pytest_quiet" in out["additionalContext"]
    rows = lines(e)
    assert [(r["decision"], r["ids"]) for r in rows] == [("rewrote", ["feedback_pytest_quiet"])]
    assert rows[0]["kind"] == "flag" and rows[0]["perm"] == "ask"


def test_the_allow_setting_makes_the_hook_approve_the_new_command(env):
    e = dict(on(env, "NOBLIVION_GUARD_REWRITE"), NOBLIVION_GUARD_REWRITE_DECISION="allow")
    out = call(e, bash("git log"))
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["command"] == "git log --oneline"
    assert lines(e)[0]["perm"] == "allow"


@pytest.mark.parametrize("value", ["", "maybe", "ALLOW "])
def test_a_bad_decision_setting_is_ask_and_allow_is_case_blind(env, value):
    e = dict(on(env, "NOBLIVION_GUARD_REWRITE"), NOBLIVION_GUARD_REWRITE_DECISION=value)
    want = "allow" if value.strip().lower() == "allow" else "ask"
    assert call(e, bash("git log"))["permissionDecision"] == want


def test_a_rewrite_charges_nothing_and_the_new_command_is_not_hit(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    for _ in range(4):
        assert call(e, bash("pytest tests"))["permissionDecision"] == "ask"
    assert call(e, bash("pytest -q tests")) is None  # idempotent: nothing to do
    assert call(e, bash("git log --oneline")) is None
    assert set(decisions(e)) == {"rewrote"}


def test_the_config_key_turns_the_rewrite_on(env, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"guard": {"rewrite": True}}))
    e = dict(env, NOBLIVION_CONFIG=str(cfg))
    assert call(e, bash("git log"))["updatedInput"]["command"] == "git log --oneline"


# 3. never when doubtful ------------------------------------------------------
@pytest.mark.parametrize(
    "command,why",
    [
        ("pytest tests; git log", "multi_hit"),
        ("pytest tests > out.txt", "not_applicable"),
        ("cd x && git log > out.txt", "not_applicable"),
        ("cd x && pytest tests | grep FAIL", "not_applicable"),
        ("(cd x && pytest tests)", "not_applicable"),
        ("bash -c 'pytest tests'", "not_applicable"),
        ("git status", "no_compliant_form"),
    ],
)
def test_a_doubtful_rewrite_falls_back_to_the_on_violation_decision(env, command, why):
    # rewrite on, ask off: the call runs unchanged and the log says why
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash(command)) is None
    rows = lines(e)
    assert [r["decision"] for r in rows] == ["rewrite-skipped"]
    assert rows[0]["why"] == why
    # rewrite on, ask on: the default of a rewrite note is to ask, never to deny
    e = on(env, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK")
    out = call(e, bash(command))
    assert out["permissionDecision"] == "ask" and "updatedInput" not in out
    assert decisions(e) == ["rewrite-skipped", "asked", "rewrite-skipped"]


def test_an_override_marker_is_an_override_and_never_a_rewrite(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash("pytest tests  # guard-ok: slow suite on purpose")) is None
    assert decisions(e) == ["override"]


def test_a_marker_without_a_reason_keeps_the_deny(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    out = call(e, bash("pytest tests  # guard-ok:"))
    assert out["permissionDecision"] == "deny"
    assert decisions(e) == ["deny"]


def test_a_rule_without_a_rewrite_still_denies_with_the_switch_on(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash("git push --force origin x"))["permissionDecision"] == "deny"
    assert decisions(e) == ["deny"]


def test_an_error_in_the_rewrite_keeps_the_deny(env, monkeypatch):
    e = on(env, "NOBLIVION_GUARD_REWRITE")

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(gh, "_rw", lambda: type("M", (), {"apply": staticmethod(boom)}))
    assert call(e, bash("pytest tests"))["permissionDecision"] == "deny"
    rows = lines(e)
    assert [r["decision"] for r in rows] == ["deny", "act-error"]
    assert rows[1]["error"] == "RuntimeError"


# 4. ask ----------------------------------------------------------------------
def test_ask_puts_the_rule_in_the_reason(env):
    e = on(env, "NOBLIVION_GUARD_ASK")
    out = call(e, bash("git remote prune origin"))
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"] == (
        "rule of feedback_ask_prune.\nApply: apply of feedback_ask_prune\n"
        "Memory: feedback_ask_prune"
    )
    assert "updatedInput" not in out
    assert [(r["decision"], r["ids"]) for r in lines(e)] == [("asked", ["feedback_ask_prune"])]


def test_an_ask_mixed_with_a_deny_rule_is_a_deny(env):
    e = on(env, "NOBLIVION_GUARD_ASK")
    out = call(e, bash("git remote prune origin; git push --force origin x"))
    assert out["permissionDecision"] == "deny"
    rows = lines(e)
    assert [r["decision"] for r in rows] == ["deny", "ask-skipped"]
    assert rows[1]["why"] == "mixed_with_deny"


def test_a_rewrite_that_leaves_the_hit_is_skipped(env_extra):
    e = on(env_extra, "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash("uv run pytest")) is None
    rows = lines(e)
    assert [r["decision"] for r in rows] == ["rewrite-skipped"]
    assert rows[0]["why"] == "still_violates"
    e = on(env_extra, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK")
    out = call(e, bash("uv run pytest"))
    assert out["permissionDecision"] == "ask" and "updatedInput" not in out


def test_a_rewrite_comes_before_an_ask_and_an_ask_covers_a_failed_rewrite(env_extra):
    e = on(env_extra, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK")
    out = call(e, bash("python -m pytest tests"))
    assert out["updatedInput"]["command"] == "python -m pytest -q tests"
    out = call(e, bash("python -m pytest tests > log.txt"))  # a redirect: no rewrite
    assert out["permissionDecision"] == "ask" and "updatedInput" not in out
    out = call(e, bash("cd x && python -m pytest tests"))  # NOBLIVION-126: in a list
    assert out["updatedInput"]["command"] == "cd x && python -m pytest -q tests"
    assert decisions(e) == ["rewrote", "asked", "rewrite-skipped", "rewrote"]


# 5. shadow -------------------------------------------------------------------
def test_shadow_logs_what_would_happen(env):
    e = on(env, "NOBLIVION_GUARD_ACT_SHADOW")
    # a rewrite note: no deny, the command runs unchanged, the log says what would happen
    assert call(e, bash("pytest tests")) is None
    # a note with no rewrite keeps its deny
    out = call(e, bash("git remote prune origin"))
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out
    assert decisions(e) == ["would-rewrite", "deny", "would-ask"]
    assert lines(e)[0]["kind"] == "flag"


def test_a_live_switch_wins_over_the_shadow_for_its_own_kind(env):
    e = on(env, "NOBLIVION_GUARD_ACT_SHADOW", "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash("pytest tests"))["permissionDecision"] == "ask"
    assert call(e, bash("git remote prune origin"))["permissionDecision"] == "deny"
    assert decisions(e) == ["rewrote", "deny", "would-ask"]


def test_the_log_holds_ids_and_the_command_head_and_no_rule_text(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK", "NOBLIVION_GUARD_ACT_SHADOW")
    for cmd in ("pytest tests", "git remote prune origin", "pytest tests > out.txt"):
        call(e, bash(cmd))
    blob = Path(e["NOBLIVION_GUARD_LOG"]).read_text()
    assert "rule of" not in blob and "apply of" not in blob


# 6. the table build ------------------------------------------------------------
def _memory(folder: Path, name: str, **fields) -> None:
    text = "---\nname: x\ndescription: d\nmetadata:\n  type: feedback\n---\nbody\n"
    base = {
        "rule": "Cap the output.",
        "apply": "git log --oneline",
        "scope": "tool",
        "triggers": ["git log"],
        "violates": LOG_V,
        "example_repeat": "git log",
        "example_ok": "git log --oneline",
    }
    base.update(fields)
    (folder / name).write_text(mf.write_fields(text, base))


def test_the_build_carries_the_fields_and_the_hit_returns_them(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    _memory(folder, "feedback_a.md", rewrite="add-flag --oneline after git log", on_violation="ask")
    _memory(
        folder,
        "feedback_b.md",
        violates=ASK_V,
        example_repeat="git remote prune o",
        example_ok="git remote -v",
    )
    table = gt.build(folder)
    by = {e["id"]: e for e in table["entries"]}
    assert by["feedback_a"]["rewrite"] == "add-flag --oneline after git log"
    assert by["feedback_a"]["on_violation"] == "ask"
    assert "rewrite" not in by["feedback_b"] and "on_violation" not in by["feedback_b"]
    hit = gt.match("git log", table)[0]
    assert (hit["rewrite"], hit["on_violation"]) == ("add-flag --oneline after git log", "ask")


def test_a_bad_rewrite_is_dropped_alone_and_the_deny_stays(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    _memory(folder, "feedback_a.md", rewrite="add-flag --oneline after git log", on_violation="ask")
    path = folder / "feedback_a.md"
    text = path.read_text()
    text = text.replace(
        "add-flag --oneline after git log", "add-flag --oneline after git log; rm -rf x"
    )
    text = text.replace("on_violation: ask", "on_violation: sometimes")
    path.write_text(text)
    table = gt.build(folder)
    (e,) = table["entries"]
    assert e["violates"] == LOG_V and "rewrite" not in e and "on_violation" not in e
    kinds = {k for r in table["skipped"] for k in r if k in ("rewrite", "on_violation")}
    assert kinds == {"rewrite", "on_violation"}
    assert "rewrite or on_violation fields are skipped" in gt.skipped_line(table)
    assert gt.match("git log", table)  # the deny still hits


def test_the_memory_check_reports_the_new_fields(tmp_path):
    f = {
        "rule": "r",
        "apply": "a",
        "scope": "tool",
        "triggers": ["git log"],
        "violates": LOG_V,
        "example_repeat": "git log",
        "example_ok": "git log --oneline",
        "rewrite": "add-flag --oneline after git log",
        "on_violation": "ask",
    }
    assert mf.check_fields(f) == []
    assert any("on_violation" in p for p in mf.check_fields(dict(f, on_violation="maybe")))
    assert any(
        "only with violates" in p
        for p in mf.check_fields(dict(f, violates="", example_repeat="", example_ok=""))
    )
    assert any("rewrite" in p for p in mf.check_fields(dict(f, rewrite="head -n 3")))


def test_the_fields_survive_a_write_and_a_read(tmp_path):
    text = "---\nname: x\ndescription: d\nmetadata:\n  type: feedback\n---\nbody\n"
    f = {
        "rule": "r",
        "apply": "a",
        "scope": "tool",
        "triggers": ["git log"],
        "violates": LOG_V,
        "example_repeat": "git log",
        "example_ok": "git log --oneline",
        "rewrite": "add-flag --oneline after git log",
        "on_violation": "ask",
    }
    new = mf.write_fields(text, f)
    got = mf.read_fields(new)
    assert (got["rewrite"], got["on_violation"]) == ("add-flag --oneline after git log", "ask")
    assert mf.write_fields(new, f) == new  # idempotent
    stripped = mf.strip_schema(new)
    assert "rewrite" not in stripped and "on_violation" not in stripped


# 7. trust events ---------------------------------------------------------------
def test_a_rewrite_and_an_ask_are_uses_and_a_shadow_line_is_not():
    ev = te.guard_events("rewrote", ["feedback_a"], "2026-10-19T00:00:00Z")
    assert [(e["kind"], e["src"]) for e in ev] == [("use", "rewrite")]
    ev = te.guard_events("asked", ["feedback_a"], "2026-10-19T00:00:00Z")
    assert [(e["kind"], e["src"]) for e in ev] == [("use", "guard_ask")]
    for d in ("would-rewrite", "would-ask", "rewrite-skipped", "ask-skipped", "act-error"):
        assert te.guard_events(d, ["feedback_a"], "2026-10-19T00:00:00Z") == []


def test_the_hook_records_the_trust_event_of_a_rewrite(env, tmp_path):
    e = dict(on(env, "NOBLIVION_GUARD_REWRITE"), NOBLIVION_TRUST_EVENTS="1")
    e["NOBLIVION_RECALL_CACHE_DIR"] = str(tmp_path / "cache")
    call(e, bash("pytest tests"))
    spooled = [
        json.loads(x)
        for p in (tmp_path / "cache").rglob("*.jsonl")
        for x in p.read_text().splitlines()
    ]
    assert any(s.get("src") == "rewrite" and s.get("kind") == "use" for s in spooled), spooled


# 8. a rewrite note is not a deny note (NOBLIVION-122) ----------------------------
# A note with a valid ``rewrite`` never adds a deny: with the rewrite and ask
# switches off, or only shadow on, it logs and lets the command run unchanged.
# A note without ``rewrite`` denies exactly as before (the golden file).
RW, ASK, SH = "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK", "NOBLIVION_GUARD_ACT_SHADOW"
COMBOS = [(r, a, s) for r in (0, 1) for a in (0, 1) for s in (0, 1)]


def _names(r, a, s):
    return [n for n, v in ((RW, r), (ASK, a), (SH, s)) if v]


# (switches rewrite/ask/shadow) -> (decision of the hook, log lines in order)
PLAIN = {  # "pytest tests": the rewrite can be made
    (0, 0, 0): (None, []),
    (0, 0, 1): (None, ["would-rewrite"]),
    (0, 1, 0): ("ask", ["asked"]),
    (0, 1, 1): ("ask", ["asked", "would-rewrite"]),
    (1, 0, 0): ("ask", ["rewrote"]),
    (1, 0, 1): ("ask", ["rewrote"]),
    (1, 1, 0): ("ask", ["rewrote"]),
    (1, 1, 1): ("ask", ["rewrote"]),
}
COMPOUND = {  # "pytest tests > out.txt": the rewrite cannot be made
    (0, 0, 0): (None, []),
    (0, 0, 1): (None, ["rewrite-skipped", "would-ask"]),
    (0, 1, 0): ("ask", ["asked"]),
    (0, 1, 1): ("ask", ["asked", "rewrite-skipped"]),
    (1, 0, 0): (None, ["rewrite-skipped"]),
    (1, 0, 1): (None, ["rewrite-skipped", "would-ask"]),
    (1, 1, 0): ("ask", ["asked", "rewrite-skipped"]),
    (1, 1, 1): ("ask", ["asked", "rewrite-skipped"]),
}


# NOBLIVION-126: a simple command of a list or a pipeline, after wrappers, acts as the
# plain command in every state of the three switches (shadow and ask included)
NEW_SHAPES = {
    "cd x && pytest tests": "cd x && pytest -q tests",
    "make build; pytest tests": "make build; pytest -q tests",
    "pytest tests | tail -5": "pytest -q tests | tail -5",
    "cd ~/proj && env CI=1 timeout 600 pytest tests/unit 2>&1 | tail -20": (
        "cd ~/proj && env CI=1 timeout 600 pytest -q tests/unit 2>&1 | tail -20"
    ),
}
REWRITTEN = {"pytest tests": "pytest -q tests", **NEW_SHAPES}


@pytest.mark.parametrize("combo", COMBOS)
@pytest.mark.parametrize(
    "command,want",
    [("pytest tests", PLAIN), ("pytest tests > out.txt", COMPOUND)]
    + [(c, PLAIN) for c in NEW_SHAPES],
)
def test_a_rewrite_note_never_denies_in_any_state_of_the_three_switches(env, combo, command, want):
    e = on(env, *_names(*combo))
    out = call(e, bash(command))
    decision, log = want[combo]
    assert (out or {}).get("permissionDecision") == decision
    assert decisions(e) == log
    assert "deny" not in decisions(e)
    if combo[0] and command in REWRITTEN:  # only a live rewrite changes the command
        assert out["updatedInput"]["command"] == REWRITTEN[command]
    else:
        assert "updatedInput" not in (out or {})


ANY_PYTEST = [
    entry(
        "feedback_pytest_any",
        r"\bpytest\b(?!.*\s-q\b)",
        rewrite="add-flag -q after pytest",
        complies=r"\bpytest\s+-q\b",
    )
]


@pytest.mark.parametrize(
    "command,new",
    [
        ("python3 -m pytest tests", "python3 -m pytest -q tests"),
        (".venv/bin/python -m pytest tests", ".venv/bin/python -m pytest -q tests"),
        (
            "cd ~/proj && .venv/bin/python -m pytest tests/unit | tail -5",
            "cd ~/proj && .venv/bin/python -m pytest -q tests/unit | tail -5",
        ),
    ],
)
def test_python_dash_m_of_the_program_is_rewritten_as_the_program(env, command, new):
    # NOBLIVION-126: the measured misses, end to end through the hook
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"version": 1, "entries": ANY_PYTEST}))
    assert call(on(env, RW), bash(command))["updatedInput"]["command"] == new
    assert call(on(env, SH), bash(command)) is None  # shadow: runs unchanged
    out = call(on(env, ASK), bash(command))  # ask only: the rule is the reason
    assert out["permissionDecision"] == "ask" and "updatedInput" not in out
    assert decisions(env) == ["rewrote", "would-rewrite", "asked"]
    assert call(on(env, RW), bash(new)) is None  # idempotent


def test_the_command_that_already_has_the_flag_is_left_alone(env):
    e = on(env, RW)
    assert call(e, bash("pytest -q tests")) is None
    assert decisions(e) == []


def test_a_rewrite_note_with_on_violation_deny_keeps_the_deny(env):
    # the note says deny itself: it is a deny note (a doctor warning names the clash)
    table = [
        entry(
            "feedback_both_say",
            PYTEST_V,
            rewrite="add-flag -q after pytest",
            on_violation="deny",
        )
    ]
    path = Path(env["NOBLIVION_GUARD_TABLE"])
    path.write_text(json.dumps({"version": 1, "entries": table}))
    assert call(env, bash("pytest tests"))["permissionDecision"] == "deny"
    e = on(env, SH)
    assert call(e, bash("pytest tests"))["permissionDecision"] == "deny"
    out = call(on(env, RW), bash("pytest tests"))  # the live rewrite still acts
    assert out["updatedInput"]["command"] == "pytest -q tests"


@pytest.mark.parametrize("combo", COMBOS)
def test_a_rewrite_note_mixed_with_a_deny_note_keeps_the_deny(env, combo):
    e = on(env, *_names(*combo))
    out = call(e, bash("pytest tests; git push --force origin x"))
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out


def test_an_error_keeps_the_deny_with_every_switch_on(env, monkeypatch):
    e = on(env, RW, ASK, SH)

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(gh, "_rw", lambda: type("M", (), {"apply": staticmethod(boom)}))
    assert call(e, bash("pytest tests"))["permissionDecision"] == "deny"


def test_the_words_inside_a_heredoc_or_a_quoted_echo_are_not_touched(env):
    e = on(env, RW)
    for cmd in (
        "cat <<'EOF'\npytest tests\nEOF",
        "echo 'pytest tests'",
        'echo "run pytest tests"',
    ):
        assert call(e, bash(cmd)) is None
    assert decisions(e) == []


GOLDEN_TABLE = [
    entry("feedback_plain", r"^make\s+clean\b"),
    entry("feedback_ask_only", ASK_V, on_violation="ask"),
    entry("feedback_explicit_deny", r"^terraform\s+apply\b", on_violation="deny"),
    entry("feedback_force", FORCE),
    entry("feedback_needs_run", r"^deploy\b(?!\s+--check)", complies=r"^deploy\s+--check"),
]
GOLDEN_TABLE[-1]["run_first"] = "deploy --check"
GOLDEN_COMMANDS = [
    "make clean",
    "git remote prune origin",
    "terraform apply",
    "git push --force origin x",
    "deploy now",
    "deploy now  # guard-ok: checked by hand",
    "make clean; terraform apply",
    "git remote prune origin; make clean",
    "make clean",
    "git status",
]
GOLDEN_FILE = Path(__file__).with_name("golden_guard_act.json")


def golden_results(root: Path) -> dict:
    """For every state of the three switches: what each command of
    ``GOLDEN_COMMANDS`` gave (hook output, hashed, and the log lines). The table holds
    no rewrite note. ``golden_guard_act.json`` was recorded with the hook of
    commit fab09b5, before the change of this section."""
    import hashlib

    out: dict = {}
    for combo in COMBOS:
        d = root / ("c" + "".join(map(str, combo)))
        d.mkdir()
        table = d / "table.json"
        table.write_text(json.dumps({"version": 1, "entries": [dict(e) for e in GOLDEN_TABLE]}))
        env = {
            "NOBLIVION_GUARD_TABLE": str(table),
            "NOBLIVION_GUARD_STATE_DIR": str(d / "state"),
            "NOBLIVION_GUARD_LOG": str(d / "log.jsonl"),
            "NOBLIVION_CONFIG": str(d / "no-config.json"),
            "NOBLIVION_TRUST_EVENTS": "0",
        }
        env = on(env, *_names(*combo))
        rows = []
        for cmd in GOLDEN_COMMANDS:
            before = len(lines(env))
            buf = io.StringIO()
            gh.main(stdin=io.StringIO(json.dumps(bash(cmd))), stdout=buf, environ=env)
            text = buf.getvalue().strip()
            rows.append(
                [
                    cmd,
                    hashlib.sha1(text.encode()).hexdigest()[:12],
                    [x["decision"] for x in lines(env)[before:]],
                ]
            )
        out["".join(map(str, combo))] = rows
    return out


def test_every_note_without_a_rewrite_denies_as_before(tmp_path):
    golden = json.loads(GOLDEN_FILE.read_text())
    got = golden_results(tmp_path)
    assert got == golden
    # the golden file is not empty and holds denies and asks
    flat = [r for rows in golden.values() for r in rows]
    assert len(flat) == 8 * len(GOLDEN_COMMANDS)
    assert any("deny" in r[2] for r in flat) and any("asked" in r[2] for r in flat)


# 9. the doctor warning: rewrite together with a deny-only field -----------------
def test_rewrite_with_on_violation_deny_is_a_warning_and_changes_no_guard(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    _memory(
        folder, "feedback_a.md", rewrite="add-flag --oneline after git log", on_violation="deny"
    )
    _memory(folder, "feedback_b.md", rewrite="add-flag --oneline after git log")
    f = mf.read_fields((folder / "feedback_a.md").read_text())
    assert mf.check_fields(f) == []  # a warning, not a problem
    assert mf.rewrite_warnings(f) == [mf.REWRITE_DENY_WARNING]
    assert mf.rewrite_warnings(mf.read_fields((folder / "feedback_b.md").read_text())) == []
    table = gt.build(folder)
    assert [w["id"] for w in table["warnings"]] == ["feedback_a"]
    by = {e["id"]: e for e in table["entries"]}
    assert by["feedback_a"]["violates"] == LOG_V and by["feedback_a"]["on_violation"] == "deny"
    assert "feedback_a" in gt.skipped_line(table) and "deny" in gt.skipped_line(table)
    (tmp_path / "mem2").mkdir()
    _memory(tmp_path / "mem2", "feedback_c.md", rewrite="add-flag --oneline after git log")
    assert "warnings" not in gt.build(tmp_path / "mem2")


def test_the_write_hook_names_the_clash(tmp_path, monkeypatch):
    hook = load_hook_with_config("memory_fields_hook", {}, "memory_fields_hook_act_t")
    folder = tmp_path / "memory"
    folder.mkdir()
    _memory(
        folder, "feedback_a.md", rewrite="add-flag --oneline after git log", on_violation="deny"
    )
    event = {"tool_name": "Write", "tool_input": {"file_path": str(folder / "feedback_a.md")}}
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(folder))
    out = hook.run(json.dumps(event))
    assert "rewrite and on_violation: deny disagree" in out
