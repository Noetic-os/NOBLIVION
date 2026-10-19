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
    # a rule whose rewrite does not remove the hit (a bad pair of regex and rewrite)
    entry("feedback_bad_pair", r"^uv\s+run\s+pytest\b", rewrite="add-flag -q after uv run pytest"),
    # a rewrite that lacks the compliant form of the memory
    entry(
        "feedback_no_complies",
        r"^git\s+status\b(?!.*-sb)",
        rewrite="add-flag -sb after git status",
        complies=r"^git\s+status\s+--porcelain",
    ),
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
def test_with_every_switch_off_a_rewrite_field_changes_nothing(env):
    out = call(env, bash("pytest tests"))
    assert out["permissionDecision"] == "deny"
    assert "updatedInput" not in out
    assert decisions(env) == ["deny"]


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
        ("cd x && pytest tests", "not_applicable"),
        ("cd x && git log", "not_applicable"),
        ("uv run pytest", "still_violates"),
        ("git status", "no_compliant_form"),
    ],
)
def test_a_doubtful_rewrite_keeps_the_deny_and_says_why(env, command, why):
    e = on(env, "NOBLIVION_GUARD_REWRITE")
    out = call(e, bash(command))
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out
    rows = lines(e)
    assert [r["decision"] for r in rows] == ["deny", "rewrite-skipped"]
    assert rows[1]["why"] == why


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


def test_a_rewrite_comes_before_an_ask_and_an_ask_covers_a_failed_rewrite(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK")
    out = call(e, bash("python -m pytest tests"))
    assert out["updatedInput"]["command"] == "python -m pytest -q tests"
    out = call(e, bash("cd x && python -m pytest tests"))  # not at the start: no rewrite
    assert out["permissionDecision"] == "ask" and "updatedInput" not in out
    assert decisions(e) == ["rewrote", "asked", "rewrite-skipped"]


# 5. shadow -------------------------------------------------------------------
def test_shadow_logs_what_would_happen_and_denies(env):
    e = on(env, "NOBLIVION_GUARD_ACT_SHADOW")
    for cmd in ("pytest tests", "git remote prune origin"):
        out = call(e, bash(cmd))
        assert out["permissionDecision"] == "deny" and "updatedInput" not in out
    assert decisions(e) == ["deny", "would-rewrite", "deny", "would-ask"]
    assert lines(e)[1]["kind"] == "flag"


def test_a_live_switch_wins_over_the_shadow_for_its_own_kind(env):
    e = on(env, "NOBLIVION_GUARD_ACT_SHADOW", "NOBLIVION_GUARD_REWRITE")
    assert call(e, bash("pytest tests"))["permissionDecision"] == "ask"
    assert call(e, bash("git remote prune origin"))["permissionDecision"] == "deny"
    assert decisions(e) == ["rewrote", "deny", "would-ask"]


def test_the_log_holds_ids_and_the_command_head_and_no_rule_text(env):
    e = on(env, "NOBLIVION_GUARD_REWRITE", "NOBLIVION_GUARD_ASK", "NOBLIVION_GUARD_ACT_SHADOW")
    for cmd in ("pytest tests", "git remote prune origin", "cd x && pytest tests"):
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
