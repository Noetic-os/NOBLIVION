# SPDX-License-Identifier: AGPL-3.0-or-later
"""The citation and correction trust signals (``hooks/trust_signals.py``,
NOBLIVION-38). Made-up data only.

What these tests hold:

1. THE SETTINGS. Both signals are off by default, each turns on by its env
   var or config key, and both are off when the trust events are off.
2. THE MATCH. A citation is a distinct name or an 8-word verbatim phrase of
   the rule, in a sentence that does not set the note aside. A correction
   needs a shown, cited note on its topic and no second candidate.
3. THE PRECISION on the labelled set equals the numbers in docs/trust.md.
4. THE FILES. The prompt hook writes each shown note once per session; the
   Stop flush reads the transcript once and appends the events to the spool.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

import trust_signal_cases as cases
from hookload import load_hook

sig = load_hook("trust_signals", "trust_signals_t_signals")
fl = load_hook("trust_flush", "trust_flush_t_signals")
te = sig.te()

SID = "signals-session-1"
DOCS = Path(__file__).resolve().parent.parent / "docs" / "trust.md"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
    }
    env.update(extra)
    return env


ON = {"NOBLIVION_TRUST_CITATION_USE": "1", "NOBLIVION_TRUST_CORRECTION_CONTRADICT": "1"}


# ── 1. the settings ─────────────────────────────────────────────────────────


def test_both_signals_are_off_by_default(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert sig.citation_on(env) is False
    assert sig.correction_on(env) is False
    assert sig.any_on(env) is False


def test_env_turns_each_signal_on(tmp_path: Path) -> None:
    assert sig.citation_on(_env(tmp_path, NOBLIVION_TRUST_CITATION_USE="on")) is True
    assert sig.correction_on(_env(tmp_path, NOBLIVION_TRUST_CORRECTION_CONTRADICT="yes")) is True


def test_config_key_turns_a_signal_on(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"trust": {"citation_use": True}}), encoding="utf-8")
    env = {"NOBLIVION_CONFIG": str(cfg)}
    assert sig.citation_on(env) is True
    assert sig.correction_on(env) is False


def test_trust_events_off_turns_the_signals_off(tmp_path: Path) -> None:
    env = _env(tmp_path, NOBLIVION_TRUST_EVENTS="0", **ON)
    assert sig.citation_on(env) is False
    assert sig.correction_on(env) is False


# ── 2. the match ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("feedback_no_git_stash", True),
        ("deploy", False),
        ("deployment", False),  # one part
        ("a_b", False),  # too short
        ("user-prefers-short", True),
    ],
)
def test_name_ok(name: str, ok: bool) -> None:
    assert sig.name_ok(name) is ok


def test_name_in_needs_a_whole_token() -> None:
    assert sig.name_in("feedback_no_git_stash", "see FEEDBACK_NO_GIT_STASH.md now")
    assert not sig.name_in("feedback_no_git_stash", "see feedback_no_git_stash_v2")
    assert not sig.name_in("feedback_no_git_stash", "see xfeedback_no_git_stash")


RULE = "Never use git stash; commit work in progress as a WIP commit instead."


def test_cites_a_long_verbatim_phrase() -> None:
    text = "So: never use git stash, commit work in progress as a WIP commit."
    assert sig.cites(text, "x", RULE)


def test_does_not_cite_a_short_phrase_or_paraphrase() -> None:
    assert not sig.cites("I keep work in progress safe.", "x", RULE)
    assert not sig.cites("I avoid stashing and commit instead.", "x", RULE)


@pytest.mark.parametrize(
    "text",
    [
        "feedback_no_git_stash does not apply here.",
        "feedback_no_git_stash is outdated.",
        "I could not open feedback_no_git_stash.",
        "The index showed feedback_no_git_stash.",
        "I override feedback_no_git_stash for this clone.",
    ],
)
def test_a_sentence_that_sets_the_note_aside_is_not_a_citation(text: str) -> None:
    assert not sig.cites(text, "feedback_no_git_stash", RULE)


def test_negation_in_another_sentence_does_not_hide_a_citation() -> None:
    text = "That old test is wrong. Per feedback_no_git_stash I made a WIP commit."
    assert sig.cites(text, "feedback_no_git_stash", RULE)


def test_about_by_name_or_by_overlap() -> None:
    assert sig.about("No, feedback_no_git_stash is old.", "feedback_no_git_stash", RULE)
    assert sig.about("No, use git stash here, not a WIP commit.", "feedback_no_git_stash", RULE)
    assert not sig.about("No, the colour is blue.", "feedback_no_git_stash", RULE)
    # three shared words in a long prompt are under the share
    long = "No, git stash commit " + " ".join(f"word{i}x" for i in range(20))
    assert not sig.about(long, "feedback_no_git_stash", RULE)


def test_is_correction_is_the_stop_checks_test() -> None:
    sc = sig._load("stop_checks")
    for prompt in ("No, that is wrong.", "Thanks, go on.", "wrong file"):
        assert sig.is_correction(prompt) == sc.is_correction(prompt)


def test_user_prompt_and_reply_text_skip_other_records() -> None:
    tool_result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
    }
    assert sig.user_prompt(tool_result) is None
    assert sig.user_prompt({"type": "user", "isMeta": True, "message": {"content": "hi"}}) is None
    assert sig.user_prompt({"type": "user", "message": {"content": "<command-name>"}}) is None
    assert sig.user_prompt({"type": "user", "message": {"content": "No, stop."}}) == "No, stop."
    only_tool = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
    }
    assert sig.reply_text(only_tool) is None


def test_scan_gives_one_use_per_note_across_calls() -> None:
    turns = [
        ("Save.", [101], "Per feedback_no_git_stash, a WIP commit."),
        ("Again.", [101], "feedback_no_git_stash again."),
    ]
    records, exposures = cases.build(turns)
    first, state = sig.scan(records[:2], cases.shown_map(), exposures, None, True, False)
    second, state = sig.scan(records[2:], cases.shown_map(), exposures, state, True, False)
    assert [(e["kind"], e["mv_id"], e["src"]) for e in first] == [("use", 101, "citation")]
    assert second == []
    assert state["used"] == [101]


def test_scan_finds_a_correction_whose_citation_came_in_an_earlier_call() -> None:
    turns = [
        ("Save.", [101], "Per feedback_no_git_stash I made a WIP commit."),
        ("No, a WIP commit on the shared branch is bad, use git stash here.", [], "OK."),
    ]
    records, exposures = cases.build(turns)
    _, state = sig.scan(records[:2], cases.shown_map(), exposures, None, False, True)
    events, state = sig.scan(records[2:], cases.shown_map(), exposures, state, False, True)
    assert [(e["kind"], e["mv_id"], e["src"]) for e in events] == [
        ("contradict", 101, "correction")
    ]
    assert state["contradicted"] == [101]


def test_scan_with_citation_off_still_tracks_citations_for_corrections() -> None:
    turns = [("Save.", [101], "Per feedback_no_git_stash I made a WIP commit.")]
    records, exposures = cases.build(turns)
    events, state = sig.scan(records, cases.shown_map(), exposures, None, False, True)
    assert events == []
    assert "101" in state["cited"]


def test_scan_ignores_bad_records_and_state() -> None:
    events, state = sig.scan(
        [{"type": "user"}, {"timestamp": "nope"}, {"type": "assistant", "timestamp": "x"}],
        {},
        {},
        {"prompts": 5, "cited": [1], "used": ["a"]},
    )
    assert events == []
    assert state["cited"] == {}


def test_events_are_valid_contract_events() -> None:
    ev = {"mv_id": 5, "kind": "use", "ts": "2026-09-01T10:00:00+00:00", "src": "citation"}
    assert te.contract_event(ev) == {
        "mv_id": 5,
        "kind": "use",
        "ts": "2026-09-01T10:00:00+00:00",
        "sources": ["citation"],
    }
    ev = dict(ev, kind="contradict", src="correction")
    assert te.contract_event(ev)["sources"] == ["correction"]


# ── 3. the precision ────────────────────────────────────────────────────────


def _docs_counts(signal: str) -> tuple[int, int]:
    text = DOCS.read_text(encoding="utf-8")
    m = re.search(rf"<!-- {signal}-precision: (\d+) of (\d+) -->", text)
    assert m, f"docs/trust.md has no {signal} precision marker"
    return int(m.group(1)), int(m.group(2))


@pytest.mark.parametrize("signal", ["citation", "correction"])
def test_precision_on_the_labelled_set_matches_the_docs(signal: str) -> None:
    row = cases.measure(sig)[signal]
    assert (row["tp"], row["tp"] + row["fp"]) == _docs_counts(signal)


def test_the_labelled_set_has_positives_and_near_misses() -> None:
    for signal in ("citation", "correction"):
        rows = [c for c in cases.CASES if c[1] == signal]
        assert sum(1 for c in rows if c[3]) >= 4
        assert sum(1 for c in rows if not c[3]) >= 6


def test_correction_finds_every_labelled_positive() -> None:
    assert cases.measure(sig)["correction"]["fn"] == 0


# ── 4. the files ────────────────────────────────────────────────────────────


def _shown_lines(cache: str) -> list[dict[str, Any]]:
    path = sig.shown_file(cache, SID)
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_record_shown_is_off_by_default(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cache = str(tmp_path / "cache")
    assert not sig.record_shown(cache, SID, "UserPromptSubmit", [(1, "a_b_c_d", "r")], env)
    assert not os.path.exists(sig.shown_file(cache, SID))


def test_record_shown_writes_each_note_once(tmp_path: Path) -> None:
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    cache = str(tmp_path / "cache")
    rows = [(1, "note_one_x", "Rule  one\nline"), (2, "note_two_x", None), (1, "dup", "x")]
    assert sig.record_shown(cache, SID, "UserPromptSubmit", rows, env)
    assert not sig.record_shown(cache, SID, "PreToolUse", [(2, "note_two_x", "")], env)
    assert sig.record_shown(cache, SID, "PreToolUse", [(3, "n" * 300, "r" * 900)], env)
    lines = _shown_lines(cache)
    assert [x["mv_id"] for x in lines] == [1, 2, 3]
    assert lines[0]["rule"] == "Rule one line"
    assert len(lines[2]["name"]) == sig.NAME_MAX and len(lines[2]["rule"]) == sig.RULE_MAX
    assert sig.load_shown(cache, SID)[1]["name"] == "note_one_x"


def test_record_shown_skips_other_events_and_bad_sessions(tmp_path: Path) -> None:
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    cache = str(tmp_path / "cache")
    assert not sig.record_shown(cache, SID, "SubagentStart:x", [(1, "a", "b")], env)
    assert not sig.record_shown(cache, "../bad", "UserPromptSubmit", [(1, "a", "b")], env)


def test_record_shown_stops_at_the_size_cap(tmp_path: Path, monkeypatch) -> None:
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(sig, "SHOWN_MAX_BYTES", 10)
    assert sig.record_shown(cache, SID, "UserPromptSubmit", [(1, "note_one_x", "r" * 50)], env)
    assert not sig.record_shown(cache, SID, "UserPromptSubmit", [(2, "note_two_x", "r")], env)


def test_recall_hook_records_the_shown_text(tmp_path: Path) -> None:
    rh = load_hook("recall_hook", "recall_hook_t_signals")
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    cache = str(tmp_path / "cache")
    lines = [
        rh.IndexLine(1, 11, "note_with_rule", "summary", 0.7, rule="The rule."),
        rh.IndexLine(2, 12, "note_no_rule", "Only a summary.", 0.6),
    ]
    rh._record_shown_events(cache, SID, "UserPromptSubmit", lines, env)
    got = sig.load_shown(cache, SID)
    assert got == {
        11: {"name": "note_with_rule", "rule": "The rule."},
        12: {"name": "note_no_rule", "rule": "Only a summary."},
    }
    spool = te.events_file(cache, SID)
    with open(spool, encoding="utf-8") as fh:
        assert sorted(json.loads(x)["mv_id"] for x in fh) == [11, 12]


def test_recall_hook_switch_names_match_the_module() -> None:
    rh = load_hook("recall_hook", "recall_hook_t_signals_names")
    assert rh.TRUST_SIGNAL_SWITCHES == (
        (sig.CITATION_ENV, sig.CITATION_KEY),
        (sig.CORRECTION_ENV, sig.CORRECTION_KEY),
    )
    assert sig.CITATION_DEFAULT is False and sig.CORRECTION_DEFAULT is False


def test_recall_hook_writes_no_shown_text_by_default(tmp_path: Path) -> None:
    rh = load_hook("recall_hook", "recall_hook_t_signals_off")
    env = _env(tmp_path)
    cache = str(tmp_path / "cache")
    lines = [rh.IndexLine(1, 11, "note_with_rule", "summary", 0.7, rule="The rule.")]
    rh._record_shown_events(cache, SID, "UserPromptSubmit", lines, env)
    assert not os.path.exists(sig.shown_file(cache, SID))
    assert os.path.exists(te.events_file(cache, SID))


def _session_files(tmp_path: Path, turns: list[tuple[Any, ...]]) -> tuple[str, str]:
    """Write the transcript, the spool's recall lines and the shown file."""
    cache = str(tmp_path / "cache")
    records, exposures = cases.build(turns)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    lines = [
        {"mv_id": mid, "kind": "recall", "ts": ts, "src": "index"}
        for mid, times in exposures.items()
        for ts in times
    ]
    assert te.append_events(cache, SID, lines, {"NOBLIVION_CONFIG": str(tmp_path / "n.json")})
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    shown = [(mid, name, rule) for mid, (name, rule) in cases.NOTES.items()]
    assert sig.record_shown(cache, SID, "UserPromptSubmit", shown, env)
    return cache, str(transcript)


def _spool(cache: str) -> list[dict[str, Any]]:
    with open(te.events_file(cache, SID), encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


TURNS = [
    ("Save.", [101], "Per feedback_no_git_stash I made a WIP commit."),
    ("No, a WIP commit on the shared branch is bad, use git stash here.", [], "OK."),
]


def test_flush_appends_citation_uses_once(tmp_path: Path) -> None:
    cache, transcript = _session_files(tmp_path, TURNS)
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    state: dict[str, Any] = {}
    assert fl.scan_signals(cache, SID, transcript, state, env) == "use:1,contradict:0"
    assert fl.scan_signals(cache, SID, transcript, state, env) == ""
    added = [e for e in _spool(cache) if e["kind"] != "recall"]
    assert [(e["kind"], e["mv_id"], e["src"]) for e in added] == [("use", 101, "citation")]
    assert state["signals"]["offset"] == os.path.getsize(transcript)


def test_flush_appends_a_contradict_when_that_signal_is_on(tmp_path: Path) -> None:
    cache, transcript = _session_files(tmp_path, TURNS)
    env = _env(tmp_path, **ON)
    state: dict[str, Any] = {}
    assert fl.scan_signals(cache, SID, transcript, state, env) == "use:1,contradict:1"
    kinds = sorted(e["kind"] for e in _spool(cache) if e["kind"] != "recall")
    assert kinds == ["contradict", "use"]


def test_flush_does_nothing_when_the_signals_are_off(tmp_path: Path) -> None:
    cache, transcript = _session_files(tmp_path, TURNS)
    state: dict[str, Any] = {}
    assert fl.scan_signals(cache, SID, transcript, state, _env(tmp_path)) == ""
    assert "signals" not in state
    assert all(e["kind"] == "recall" for e in _spool(cache))


def test_flush_state_keeps_the_signal_state(tmp_path: Path) -> None:
    cache = str(tmp_path / "cache")
    state = {"sent": 0, "transcripts": {}, "signals": {"offset": 7, "used": [1]}}
    assert fl.save_state(cache, SID, state)
    assert fl.load_state(cache, SID)["signals"] == {"offset": 7, "used": [1]}


def test_flush_reads_a_rewritten_transcript_again(tmp_path: Path) -> None:
    cache, transcript = _session_files(tmp_path, TURNS[:1])
    env = _env(tmp_path, NOBLIVION_TRUST_CITATION_USE="1")
    state: dict[str, Any] = {"signals": {"offset": 10**9, "used": [101]}}
    assert fl.scan_signals(cache, SID, transcript, state, env) == "use:1,contradict:0"


def test_prune_removes_the_shown_file(tmp_path: Path) -> None:
    cache, _ = _session_files(tmp_path, TURNS[:1])
    old = 10 * 86400.0
    folder = os.path.join(cache, "by-session")
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        os.utime(path, (os.path.getmtime(path) - 40 * 86400.0,) * 2)
    assert fl.prune(cache, "other-session", now=None) >= 2
    assert not os.path.exists(sig.shown_file(cache, SID))
    del old
