# SPDX-License-Identifier: AGPL-3.0-or-later
"""The behavioural use signal (``hooks/trust_behaviour.py``, NOBLIVION-120).
Made-up data only.

What these tests hold:

1. THE SETTING. Off by default, on by its env var or config key, and off when
   the trust events are off.
2. THE MATCH. A Bash call after the showing counts when it runs the rule's
   ``complies`` command, or fires a strong trigger that the rule's own text
   tells the reader to run. Text that is only data in the command (a heredoc
   body, a quoted argument, a comment) never counts. A call that matches
   ``violates`` never counts. A weak, broad, shared or place-only trigger, a
   trigger that the rule only mentions, and any file tool call never count
   (NOBLIVION-120: precision gate).
3. THE ONCE-PER-SESSION RULE, and the rule that a call before the showing
   does not count.
4. THE FILES. The Stop flush appends the event with its own source, keeps the
   state between flushes, and merges it with a citation of the same note.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hookload import load_hook

sig = load_hook("trust_signals", "trust_signals_t_behaviour")
bh = load_hook("trust_behaviour", "trust_behaviour_t_behaviour")
fl = load_hook("trust_flush", "trust_flush_t_behaviour")
te = sig.te()

SID = "behaviour-session-1"
T_SHOWN = "2026-09-01T10:00:01Z"
T_CALL = "2026-09-01T10:01:00Z"
T_EARLY = "2026-09-01T09:59:00Z"

PUBLISH = {
    "id": "feedback_publish_with_dry_run",
    "rule": "Run the publish command with --dry-run first.",
    "apply": "make publish-widgets --dry-run",
    "scope": "tool",
    "triggers": ["make publish-widgets"],
    "violates": r"^make\s+publish-widgets\s+--force",
    "complies": r"make\s+publish-widgets\s+--dry-run",
    "run_first": "make publish-widgets --dry-run",
}
LINT = {
    "id": "feedback_run_the_widget_linter",
    "rule": "Run `python3 tools/widget_lint.py` after an edit.",
    "apply": "python3 tools/widget_lint.py",
    "scope": "tool",
    "triggers": ["python3 tools/widget_lint.py"],
    "violates": "",
    "complies": "",
    "run_first": "",
}
BROAD = {
    "id": "feedback_check_git_state",
    "rule": "Check the git state before work.",
    "apply": "git status",
    "scope": "tool",
    "triggers": ["git status"],
    "violates": "",
    "complies": "",
    "run_first": "",
}
GLOB = {
    "id": "feedback_schema_edits_need_a_migration",
    "rule": "A change to the schema file needs a migration.",
    "apply": "add a migration",
    "scope": "file",
    "triggers": ["glob:db/schema.sql"],
    "violates": "",
    "complies": "",
    "run_first": "",
}
ENTRIES = [PUBLISH, LINT, BROAD, GLOB]
MIDS = {PUBLISH["id"]: 1, LINT["id"]: 2, BROAD["id"]: 3, GLOB["id"]: 4}
SHOWN = {mid: {"name": f"{eid}.md", "rule": ""} for eid, mid in MIDS.items()}
SEEN = {mid: [T_SHOWN] for mid in MIDS.values()}


def _call(ts: str, name: str, **inp: Any) -> dict[str, Any]:
    block = {"type": "tool_use", "id": "t1", "name": name, "input": inp}
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [block]},
    }


def _bash(ts: str, command: str) -> dict[str, Any]:
    return _call(ts, "Bash", command=command)


def _run(records: list[dict[str, Any]], **kw: Any) -> list[tuple[int, str]]:
    table = kw.pop("table", {"entries": ENTRIES})
    events, _ = bh.scan(records, kw.pop("shown", SHOWN), kw.pop("exposures", SEEN), table)
    return [(e["mv_id"], e["why"]) for e in events]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
        "NOBLIVION_GUARD_TABLE": str(tmp_path / "table.json"),
    }
    env.update(extra)
    return env


# ── 1. the setting ──────────────────────────────────────────────────────────


def test_the_signal_is_off_by_default(tmp_path: Path) -> None:
    assert sig.BEHAVIOUR_DEFAULT is False
    assert sig.behaviour_on(_env(tmp_path)) is False
    assert sig.any_on(_env(tmp_path)) is False


def test_env_and_config_key_turn_it_on(tmp_path: Path) -> None:
    assert sig.behaviour_on(_env(tmp_path, NOBLIVION_TRUST_BEHAVIOUR_USE="1")) is True
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"trust": {"behaviour_use": True}}), encoding="utf-8")
    assert sig.behaviour_on({"NOBLIVION_CONFIG": str(cfg)}) is True
    assert sig.citation_on({"NOBLIVION_CONFIG": str(cfg)}) is False


def test_it_is_off_when_the_trust_events_are_off(tmp_path: Path) -> None:
    env = _env(tmp_path, NOBLIVION_TRUST_BEHAVIOUR_USE="1", NOBLIVION_TRUST_EVENTS="0")
    assert sig.behaviour_on(env) is False


def test_the_recall_hook_records_shown_notes_for_it() -> None:
    rh = load_hook("recall_hook", "recall_hook_t_behaviour_names")
    assert (sig.BEHAVIOUR_ENV, sig.BEHAVIOUR_KEY) in rh.TRUST_SIGNAL_SWITCHES


# ── 2. the match ────────────────────────────────────────────────────────────


def test_a_complies_run_is_a_use() -> None:
    got = _run([_bash(T_CALL, "make publish-widgets --dry-run")])
    assert got == [(1, "complies")]


def test_a_strong_trigger_is_a_use() -> None:
    got = _run([_bash(T_CALL, "cd /srv/app && python3 tools/widget_lint.py --all")])
    assert got == [(2, "python3 tools/widget_lint.py")]


def test_a_violates_match_is_never_a_use() -> None:
    # The command fires the trigger of PUBLISH and breaks its rule.
    assert _run([_bash(T_CALL, "make publish-widgets --force")]) == []


def test_a_violates_match_blocks_a_complies_run_in_the_same_command() -> None:
    cmd = "make publish-widgets --dry-run && make publish-widgets --force"
    assert _run([_bash(T_CALL, cmd)]) == []


def test_a_trigger_with_a_specific_argument_counts() -> None:
    specific = dict(
        PUBLISH,
        rule="Use `make publish widgets-eu` for the EU stock.",
        triggers=["make publish widgets-eu"],
        complies="",
    )
    got = _run([_bash(T_CALL, "make publish widgets-eu --now")], table={"entries": [specific]})
    assert got == [(1, "make publish widgets-eu")]


def test_a_weak_trigger_that_the_rule_does_not_name_is_not_a_use() -> None:
    unnamed = dict(PUBLISH, complies="")
    got = _run([_bash(T_CALL, "make publish-widgets staging")], table={"entries": [unnamed]})
    assert got == []


def test_a_broad_trigger_is_not_a_use() -> None:
    assert _run([_bash(T_CALL, "git status --short")]) == []


def test_a_one_word_trigger_is_weak_and_is_not_a_use() -> None:
    weak = dict(LINT, id="feedback_use_the_linter", triggers=["widgetlint"])
    shown = {7: {"name": "feedback_use_the_linter.md", "rule": ""}}
    got = _run(
        [_bash(T_CALL, "widgetlint src")],
        table={"entries": [weak]},
        shown=shown,
        exposures={7: [T_SHOWN]},
    )
    assert got == []


def test_a_glob_trigger_on_a_file_edit_is_not_a_use() -> None:
    # A path says that the rule applies, not that it was followed (NOBLIVION-120).
    assert _run([_call(T_CALL, "Edit", file_path="/work/app/db/schema.sql")]) == []
    assert _run([_call(T_CALL, "Write", file_path="/work/app/db/schema.sql")]) == []


def test_a_file_write_with_the_command_in_its_text_is_not_a_use() -> None:
    body = "run make publish-widgets --dry-run and python3 tools/widget_lint.py"
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        rec = _call(T_CALL, tool, file_path="/work/notes.md", content=body, new_string=body)
        assert _run([rec]) == []


def test_a_subagent_call_is_not_read() -> None:
    rec = _bash(T_CALL, "make publish-widgets --dry-run")
    rec["isSidechain"] = True
    assert _run([rec]) == []


def test_a_note_without_a_table_row_is_not_matched() -> None:
    shown = {9: {"name": "project_unknown_note.md", "rule": ""}}
    got = _run(
        [_bash(T_CALL, "make publish-widgets --dry-run")],
        shown=shown,
        exposures={9: [T_SHOWN]},
    )
    assert got == []


def test_an_empty_or_bad_call_is_skipped() -> None:
    assert _run([_bash(T_CALL, ""), _call(T_CALL, "Bash"), {"type": "assistant"}]) == []


# ── 2b. the precision gate (NOBLIVION-120) ──────────────────────────────────


def _one(entry: dict[str, Any], command: str) -> list[tuple[int, str]]:
    shown = {1: {"name": entry["id"] + ".md", "rule": ""}}
    return _run(
        [_bash(T_CALL, command)],
        table={"entries": [entry]},
        shown=shown,
        exposures={1: [T_SHOWN]},
    )


def test_complies_text_in_a_heredoc_body_is_not_a_use() -> None:
    cmd = "cat >> notes.md <<'EOF'\nrun make publish-widgets --dry-run next\nEOF"
    assert _run([_bash(T_CALL, cmd)]) == []
    cmd = "cat > notes.md <<EOF\nmake publish-widgets --dry-run\nEOF\nls"
    assert _run([_bash(T_CALL, cmd)]) == []


def test_complies_text_in_a_quoted_argument_or_comment_is_not_a_use() -> None:
    for cmd in (
        'echo "make publish-widgets --dry-run"',
        "git commit -m 'document make publish-widgets --dry-run'",
        "ls  # make publish-widgets --dry-run",
        'grep -rn "make publish-widgets --dry-run" docs',
    ):
        assert _run([_bash(T_CALL, cmd)]) == [], cmd


def test_a_complies_run_after_a_heredoc_or_in_a_chain_still_counts() -> None:
    cmd = "cat > a.txt <<EOF\nx\nEOF\nmake publish-widgets --dry-run"
    assert _run([_bash(T_CALL, cmd)]) == [(1, "complies")]
    assert _run([_bash(T_CALL, "cd /srv/app && make publish-widgets --dry-run | tail")]) == [
        (1, "complies")
    ]
    assert _run([_bash(T_CALL, "bash -c 'make publish-widgets --dry-run'")]) == [(1, "complies")]


def test_trigger_text_in_a_heredoc_body_or_quote_is_not_a_use() -> None:
    cmd = "cat > todo.md <<EOF\npython3 tools/widget_lint.py\nEOF"
    assert _run([_bash(T_CALL, cmd)]) == []
    assert _run([_bash(T_CALL, 'echo "python3 tools/widget_lint.py"')]) == []


def test_a_trigger_that_the_rule_only_mentions_is_not_a_use() -> None:
    cases = {
        "when": "When `python3 tools/widget_lint.py` fails, read the log first.",
        "instead": "Use `make lint` instead of `python3 tools/widget_lint.py`.",
        "never": "Never call `python3 tools/widget_lint.py` on a release branch.",
        "plain": "Lint the widgets after an edit.",
    }
    for why, rule in cases.items():
        entry = dict(LINT, rule=rule, apply="lint the widgets")
        assert _one(entry, "python3 tools/widget_lint.py --all") == [], why


def test_a_trigger_that_the_rule_tells_to_run_is_a_use() -> None:
    entry = dict(
        LINT, rule="Lint.", apply="Run `python3 tools/widget_lint.py --all` after an edit."
    )
    assert _one(entry, "python3 tools/widget_lint.py --all") == [
        (1, "python3 tools/widget_lint.py")
    ]
    # a different subcommand than the one the rule names is not the rule's command
    sub = dict(
        LINT,
        rule="Run `python3 tools/widget_lint.py fix`.",
        triggers=["python3 tools/widget_lint.py"],
    )
    assert _one(sub, "python3 tools/widget_lint.py check") == []


def test_a_trigger_two_notes_share_is_not_a_use() -> None:
    other = dict(LINT, id="feedback_second_widget_note")
    shown = {
        2: {"name": LINT["id"] + ".md", "rule": ""},
        5: {"name": other["id"] + ".md", "rule": ""},
    }
    got = _run(
        [_bash(T_CALL, "python3 tools/widget_lint.py")],
        table={"entries": [LINT, other]},
        shown=shown,
        exposures={2: [T_SHOWN], 5: [T_SHOWN]},
    )
    assert got == []


def test_a_trigger_that_names_only_a_host_or_container_is_a_place_only() -> None:
    for trig in (
        "ssh build-host",
        "ssh -o BatchMode=yes build-host",
        "docker exec app-db",
        "docker exec -i app-db",
        "docker container logs app-db",
        "kubectl exec app-pod",
    ):
        assert bh.names_a_place_only(trig), trig
    for trig in (
        "ssh build-host systemctl",
        "docker exec app-db psql",
        "docker compose up",
        "python3 tools/x.py",
        "make publish-widgets",
    ):
        assert not bh.names_a_place_only(trig), trig


def test_a_place_only_trigger_is_not_a_use_even_when_the_rule_runs_it() -> None:
    entry = dict(LINT, rule="Run `ssh build-host` and read the log.", triggers=["ssh build-host"])
    assert _one(entry, "ssh build-host uptime") == []


def test_only_the_complies_tier_can_be_switched_on_alone(monkeypatch: Any) -> None:
    monkeypatch.setattr(bh, "TIER_TRIGGER", False)
    assert _run([_bash(T_CALL, "python3 tools/widget_lint.py")]) == []
    assert _run([_bash(T_CALL, "make publish-widgets --dry-run")]) == [(1, "complies")]
    monkeypatch.setattr(bh, "TIER_TRIGGER", True)
    monkeypatch.setattr(bh, "TIER_COMPLIES", False)
    assert _run([_bash(T_CALL, "make publish-widgets --dry-run")]) == []
    assert _run([_bash(T_CALL, "python3 tools/widget_lint.py")]) == [
        (2, "python3 tools/widget_lint.py")
    ]


def test_the_shipped_tiers_are_the_ones_the_gate_passed() -> None:
    assert bh.TIER_COMPLIES is True
    assert bh.TIER_TRIGGER is True


# ── 3. once per session, and only after the showing ─────────────────────────


def test_a_note_is_used_once_per_session() -> None:
    calls = [_bash(T_CALL, "make publish-widgets --dry-run") for _ in range(3)]
    assert _run(calls) == [(1, "complies")]


def test_one_call_can_follow_two_rules() -> None:
    cmd = "python3 tools/widget_lint.py && make publish-widgets --dry-run"
    assert sorted(_run([_bash(T_CALL, cmd)])) == [
        (1, "complies"),
        (2, "python3 tools/widget_lint.py"),
    ]


def test_a_call_before_the_showing_is_not_a_use() -> None:
    early = _bash(T_EARLY, "make publish-widgets --dry-run")
    assert _run([early]) == []
    late = _bash(T_CALL, "make publish-widgets --dry-run")
    assert _run([early, late]) == [(1, "complies")]


def test_a_note_that_was_never_shown_is_not_a_use() -> None:
    assert _run([_bash(T_CALL, "make publish-widgets --dry-run")], exposures={}) == []


def test_the_state_carries_the_used_notes_to_the_next_scan() -> None:
    table = {"entries": ENTRIES}
    rec = [_bash(T_CALL, "make publish-widgets --dry-run")]
    events, state = bh.scan(rec, SHOWN, SEEN, table)
    assert [e["mv_id"] for e in events] == [1]
    again, state2 = bh.scan(rec, SHOWN, SEEN, table, state)
    assert again == [] and state2["used"] == [1]


def test_a_bad_saved_state_is_read_as_empty() -> None:
    assert bh._state({"used": "x"}) == {"used": []}
    assert bh._state({"used": [0, True, "3", 4]}) == {"used": [3, 4]}
    assert bh._state(None) == {"used": []}


def test_the_event_has_its_own_source() -> None:
    events, _ = bh.scan(
        [_bash(T_CALL, "make publish-widgets --dry-run")], SHOWN, SEEN, {"entries": ENTRIES}
    )
    assert events == [
        {
            "mv_id": 1,
            "kind": "use",
            "ts": "2026-09-01T10:01:00+00:00",
            "src": "behaviour",
            "why": "complies",
        }
    ]


# ── 4. the files ────────────────────────────────────────────────────────────


def _session(tmp_path: Path, records: list[dict[str, Any]], **extra: str) -> tuple[str, str, Any]:
    cache = str(tmp_path / "cache")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    (tmp_path / "table.json").write_text(json.dumps({"entries": ENTRIES}), encoding="utf-8")
    env = _env(tmp_path, NOBLIVION_TRUST_BEHAVIOUR_USE="1", **extra)
    lines = [
        {"mv_id": mid, "kind": "recall", "ts": T_SHOWN, "src": "index"} for mid in MIDS.values()
    ]
    assert te.append_events(cache, SID, lines, env)
    rows = [(mid, f"{eid}.md", "A rule.") for eid, mid in MIDS.items()]
    assert sig.record_shown(cache, SID, "UserPromptSubmit", rows, env)
    return cache, str(transcript), env


def _spool(cache: str) -> list[dict[str, Any]]:
    with open(te.events_file(cache, SID), encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def test_the_flush_appends_one_behaviour_use_and_keeps_the_state(tmp_path: Path) -> None:
    cache, transcript, env = _session(tmp_path, [_bash(T_CALL, "make publish-widgets --dry-run")])
    state: dict[str, Any] = {}
    assert fl.scan_signals(cache, SID, transcript, state, env) == "use:1,contradict:0"
    assert fl.scan_signals(cache, SID, transcript, state, env) == ""
    added = [e for e in _spool(cache) if e["kind"] != "recall"]
    assert [(e["kind"], e["mv_id"], e["src"]) for e in added] == [("use", 1, "behaviour")]
    assert state["signals"]["behaviour"] == {"used": [1]}


def test_the_flush_does_nothing_when_the_setting_is_off(tmp_path: Path) -> None:
    cache, transcript, env = _session(tmp_path, [_bash(T_CALL, "make publish-widgets --dry-run")])
    env["NOBLIVION_TRUST_BEHAVIOUR_USE"] = "0"
    state: dict[str, Any] = {}
    assert fl.scan_signals(cache, SID, transcript, state, env) == ""
    assert "signals" not in state
    assert all(e["kind"] == "recall" for e in _spool(cache))


def test_the_flush_does_not_read_a_later_call_twice(tmp_path: Path) -> None:
    cache, transcript, env = _session(tmp_path, [_bash(T_CALL, "make publish-widgets --dry-run")])
    state: dict[str, Any] = {}
    fl.scan_signals(cache, SID, transcript, state, env)
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_bash("2026-09-01T10:02:00Z", "make publish-widgets --dry-run")) + "\n")
        fh.write(json.dumps(_bash("2026-09-01T10:03:00Z", "python3 tools/widget_lint.py")) + "\n")
    assert fl.scan_signals(cache, SID, transcript, state, env) == "use:1,contradict:0"
    uses = [(e["mv_id"], e["src"]) for e in _spool(cache) if e["kind"] == "use"]
    assert uses == [(1, "behaviour"), (2, "behaviour")]


def test_the_flush_uses_the_cwd_to_find_the_table(tmp_path: Path) -> None:
    cache, transcript, env = _session(tmp_path, [_bash(T_CALL, "make publish-widgets --dry-run")])
    del env["NOBLIVION_GUARD_TABLE"]
    env["NOBLIVION_MEMORY_DIR"] = str(tmp_path / "memory")
    env["NOBLIVION_DATA_DIR"] = str(tmp_path / "data")
    (tmp_path / "memory").mkdir()
    gt = load_hook("guard_table", "guard_table_t_behaviour")
    out = gt.table_path(tmp_path / "memory", env)
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"entries": ENTRIES}), encoding="utf-8")
    assert fl.scan_signals(cache, SID, transcript, {}, env, str(tmp_path)) == "use:1,contradict:0"


def test_a_citation_and_a_behaviour_use_merge_with_both_sources(tmp_path: Path) -> None:
    cache, transcript, env = _session(tmp_path, [_bash(T_CALL, "make publish-widgets --dry-run")])
    assert fl.scan_signals(cache, SID, transcript, {}, env) == "use:1,contradict:0"
    te.append_events(
        cache, SID, [{"mv_id": 1, "kind": "use", "ts": T_CALL, "src": "citation"}], env
    )
    events, bad = te.merge_events(
        rec for _, rec in te.read_records(te.events_file(cache, SID), 0)[0]
    )
    assert bad == 0
    use = [e for e in events if e["kind"] == "use" and e["mv_id"] == 1]
    assert use[0]["sources"] == ["behaviour", "citation"]
    assert "why" not in use[0]


def test_the_table_loader_reads_an_absent_table_as_empty(tmp_path: Path) -> None:
    env = {"NOBLIVION_GUARD_TABLE": str(tmp_path / "missing.json")}
    assert bh.load_table(env)["entries"] == []
