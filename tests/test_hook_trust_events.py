# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trust events: the mapping, the local append, the guard hook call site and
the transcript reader.

What these tests hold:

1. THE MAPPING. An index row -> ``recall`` keyed by its id. Guard ``rows`` or a
   ``deny`` on rule R -> ``use`` keyed by ``R.md``; an override, an error and
   an apply-failed line -> nothing. A ``noblivion_recall`` fetch -> ``use``
   keyed by the id the RESULT names, so a fetch by rank counts the memory it
   read; an error result, a search answer, a fetch-looking text from another
   tool and a call without a result -> nothing.
2. ON BY DEFAULT (design doc section 12.3). With NOBLIVION_TRUST_EVENTS (or
   the config key ``trust.events``) ``0``, ``off``, ``false`` or ``no`` no file
   is written: not by the module and not by the guard hook.
3. THE APPEND. One JSON line per event in one write, under 1 ms; an unwritable
   folder fails open. The default folder is ``<data dir>/cache``.
4. THE GUARD HOOK. ``rows`` and ``deny`` append ``use`` events; an override
   appends none; the printed decision is unchanged.
5. TRANSCRIPTS. ``read_records`` stops before a partial last line; a call and
   its result split over two reads pair through ``pending``; workflow agent
   transcripts are read, a workflow journal is not.
"""

from __future__ import annotations

import io
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook

te = load_hook("trust_events", "trust_events_t_trust_events")
gh = load_hook("guard_hook", "guard_hook_t_trust_events")

SID = "trust-e-session"
TS = "2026-10-01T10:00:00+00:00"
ON = {"NOBLIVION_TRUST_EVENTS": "1"}
RECALL_TOOL = "mcp__noblivion__noblivion_recall"


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


def _lines(cache: Path, sid: str = SID) -> list[dict[str, Any]]:
    path = cache / "by-session" / f"{sid}.trust-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


# 1. the mapping ---------------------------------------------------------
def test_index_rows_map_to_recall_by_id_once_each():
    evs = te.index_events([19001, "19002", 19001, 0, -3, True, 1.5, "x"], TS)
    assert evs == [
        {"mv_id": 19001, "kind": "recall", "ts": TS, "src": "index"},
        {"mv_id": 19002, "kind": "recall", "ts": TS, "src": "index"},
    ]


@pytest.mark.parametrize("decision,src", [("rows", "guard_rows"), ("deny", "guard_deny")])
def test_guard_rows_and_deny_map_to_use_by_path(decision, src):
    evs = te.guard_events(decision, ["feedback_a", "feedback_a", "../etc", ".hidden", ""], TS)
    assert evs == [{"path": "feedback_a.md", "kind": "use", "ts": TS, "src": src}]


@pytest.mark.parametrize("decision", ["override", "error", "apply-failed", "followed", "error_hit"])
def test_other_guard_decisions_and_dropped_kinds_map_to_nothing(decision):
    assert te.guard_events(decision, ["feedback_a"], TS) == []


def test_contract_event_keeps_the_c1_fields_only():
    line = {
        "mv_id": 5,
        "path": "x.md",
        "kind": "use",
        "ts": "2026-10-01T10:00:00.500Z",
        "src": "fetch",
        "tool_use_id": "t",
        "name": "x.md",
    }
    assert te.contract_event(line) == {"mv_id": 5, "kind": "use", "ts": TS, "sources": ["fetch"]}
    assert te.contract_event({"path": "x.md", "kind": "recall", "ts": TS}) == {
        "path": "x.md",
        "kind": "recall",
        "ts": TS,
    }
    for bad in (
        {"mv_id": 5, "kind": "followed", "ts": TS},
        {"mv_id": 5, "kind": "use"},
        {"path": "a/b.md", "kind": "use", "ts": TS},
        {"kind": "use", "ts": TS},
    ):
        assert te.contract_event(bad) is None


def test_merge_events_keeps_one_per_memory_and_kind_with_the_earliest_time():
    later = "2026-10-01T11:00:00+00:00"
    evs, bad = te.merge_events(
        [
            {"mv_id": 1, "kind": "recall", "ts": later},
            {"mv_id": 1, "kind": "recall", "ts": TS},
            {"mv_id": 1, "kind": "use", "ts": later},
            {"path": "a.md", "kind": "use", "ts": TS},
            {"kind": "nonsense"},
        ]
    )
    assert bad == 1
    assert evs == [
        {"mv_id": 1, "kind": "recall", "ts": TS},
        {"path": "a.md", "kind": "use", "ts": TS},
        {"mv_id": 1, "kind": "use", "ts": later},
    ]


def test_contract_event_carries_a_valid_src_as_sources_and_drops_a_bad_one():
    """A bad src must not cost the event: the server rejects an event whole
    for a bad source name.

    MUTANT: never copy src, or copy it unchecked.
    """
    for src in te.GUARD_USE_DECISIONS.values():
        got = te.contract_event({"path": "x.md", "kind": "use", "ts": TS, "src": src})
        assert got["sources"] == [src]
    for bad in ("Fetch", "guard-rows", "a,b", "", "fetch\n", "x" * 33, 7, None, ["fetch"]):
        assert te.contract_event({"mv_id": 5, "kind": "use", "ts": TS, "src": bad}) == {
            "mv_id": 5,
            "kind": "use",
            "ts": TS,
        }


def test_merge_events_unions_the_sources_of_one_memory_and_kind():
    """MUTANT: keep the sources of the earliest line only, or leave them unsorted."""
    later = "2026-10-01T11:00:00+00:00"
    evs, bad = te.merge_events(
        [
            {"path": "a.md", "kind": "use", "ts": later, "src": "guard_rows"},
            {"path": "a.md", "kind": "use", "ts": TS, "src": "guard_deny"},
            {"path": "a.md", "kind": "use", "ts": TS, "src": "guard_deny"},
            {"mv_id": 1, "kind": "use", "ts": later, "src": "fetch"},
            {"mv_id": 1, "kind": "use", "ts": TS},
            {"mv_id": 2, "kind": "recall", "ts": TS},
        ]
    )
    assert bad == 0
    assert evs == [
        {"mv_id": 2, "kind": "recall", "ts": TS},
        {"mv_id": 1, "kind": "use", "ts": TS, "sources": ["fetch"]},
        {"path": "a.md", "kind": "use", "ts": TS, "sources": ["guard_deny", "guard_rows"]},
    ]


def test_merge_events_never_sends_more_sources_than_the_server_takes():
    """MUTANT: drop the SOURCES_MAX cap (the server would reject the event)."""
    evs, _ = te.merge_events(
        [{"mv_id": 1, "kind": "use", "ts": TS, "src": f"s{i:02d}"} for i in range(12)]
    )
    assert evs[0]["sources"] == [f"s{i:02d}" for i in range(te.SOURCES_MAX)]
    assert te.SOURCES_MAX == 8


def _call(tid: str, name: str = RECALL_TOOL, **inp: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": "2026-10-01T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
        },
    }


def _result(tid: str, text: str, is_error: bool = False, as_list: bool = True) -> dict[str, Any]:
    content: Any = [{"type": "text", "text": text}] if as_list else text
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": "2026-10-01T10:00:01.000Z",
        "message": {"role": "user", "content": [block]},
    }


FETCH_BY_RANK = (
    "GROUNDED MEMORY 18046: voice-terse-opening [feedback_voice_terse_opening.md]\n"
    "fetch_id 3 read as rank 3 = id 18046\nRULE: be short\nbody"
)


def test_fetch_counts_the_id_the_result_names_not_the_rank_typed():
    evs, pending = te.fetch_uses([_call("t1", fetch_id=3), _result("t1", FETCH_BY_RANK)])
    assert pending == {}
    assert evs == [
        {
            "mv_id": 18046,
            "kind": "use",
            "ts": TS,
            "src": "fetch",
            "tool_use_id": "t1",
            "name": "feedback_voice_terse_opening.md",
        }
    ]


def test_the_recall_tool_is_matched_by_its_noblivion_name():
    """The MCP server prefix may vary; the tool name suffix is ``noblivion_recall``."""
    assert te.RECALL_TOOL_SUFFIX == "noblivion_recall"
    assert te.is_recall_tool(RECALL_TOOL)
    assert te.is_recall_tool("mcp__plugin_noblivion_memory__noblivion_recall")
    for other in ("noblivion_recall", "mcp__x__recall", "mcp__x__noblivion_recall_x", None, 3):
        assert not te.is_recall_tool(other)
    evs, _ = te.fetch_uses(
        [_call("t8", name="mcp__x__other_recall", fetch_id=3), _result("t8", FETCH_BY_RANK)]
    )
    assert evs == []


def test_fetch_errors_searches_and_other_tools_give_nothing():
    bash = {
        "type": "assistant",
        "timestamp": "2026-10-01T10:00:00Z",
        "message": {
            "content": [
                {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "cat x"}}
            ]
        },
    }
    records = [
        _call("t2", fetch_id=5),
        _result("t2", "noblivion_recall: memory 5 not returned (gone)", True),
        _call("t3", query="deploy"),
        _result("t3", "GROUNDED MEMORY (persona claude_code, 1 hits)"),
        _call("t4", fetch_id=6),
        _result("t4", "recall daemon unavailable", as_list=False),
        bash,
        _result("b1", "GROUNDED MEMORY 777: forged [x.md]"),
    ]
    evs, pending = te.fetch_uses(records)
    assert evs == [] and pending == {}


def test_an_error_result_is_never_a_use_even_with_a_fetch_head():
    # Claude Code marks a refused or failed call is_error; its text is not the
    # tool's answer, whatever it starts with.
    evs, _ = te.fetch_uses([_call("t9", fetch_id=7), _result("t9", FETCH_BY_RANK, is_error=True)])
    assert evs == []


def test_a_call_and_its_result_pair_across_two_reads():
    evs, pending = te.fetch_uses([_call("t5", fetch_id=18046)])
    assert evs == [] and list(pending) == ["t5"]
    evs, pending = te.fetch_uses([_result("t5", FETCH_BY_RANK)], pending)
    assert [e["mv_id"] for e in evs] == [18046] and pending == {}


# 2. + 3. the append -----------------------------------------------------
@pytest.mark.parametrize("value", ["0", "off", "false", "no", " OFF "])
def test_append_is_off_when_the_variable_says_off(tmp_path, value):
    env = {"NOBLIVION_TRUST_EVENTS": value, "NOBLIVION_CONFIG": str(tmp_path / "none.json")}
    assert te.enabled(env) is False
    assert te.append_events(str(tmp_path), SID, te.index_events([1], TS), env) is False
    guard_env = dict(env, NOBLIVION_RECALL_CACHE_DIR=str(tmp_path))
    assert te.record_guard(guard_env, SID, "deny", ["a"]) is False
    assert not (tmp_path / "by-session").exists()


@pytest.mark.parametrize("value", [None, "", "1", "true", "yes"])
def test_events_are_on_by_default_and_for_any_other_value(tmp_path, value):
    env = {"NOBLIVION_CONFIG": str(tmp_path / "none.json")}
    if value is not None:
        env["NOBLIVION_TRUST_EVENTS"] = value
    assert te.enabled(env) is True
    assert te.append_events(str(tmp_path), SID, te.index_events([1], TS), env) is True


@pytest.mark.parametrize(("config", "on"), [(0, False), (False, False), ("off", False), (1, True)])
def test_the_config_key_switches_the_events(tmp_path, config, on):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"trust": {"events": config}}), encoding="utf-8")
    assert te.enabled({"NOBLIVION_CONFIG": str(path)}) is on
    # The env var wins over the config file.
    assert te.enabled({"NOBLIVION_CONFIG": str(path), "NOBLIVION_TRUST_EVENTS": "1"}) is True


def test_events_are_on_by_default_in_the_process_environment(tmp_path, monkeypatch):
    """Design doc section 12.3: ``trust.events`` defaults to ``1``."""
    monkeypatch.delenv("NOBLIVION_TRUST_EVENTS", raising=False)
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "none.json"))
    assert te.enabled() is True


def test_append_writes_one_json_line_per_event(tmp_path):
    assert te.append_events(str(tmp_path), SID, te.index_events([1, 2], TS), ON)
    assert te.append_events(str(tmp_path), SID, te.guard_events("deny", ["r"], TS), ON)
    assert _lines(tmp_path) == [
        {"kind": "recall", "mv_id": 1, "src": "index", "ts": TS},
        {"kind": "recall", "mv_id": 2, "src": "index", "ts": TS},
        {"kind": "use", "path": "r.md", "src": "guard_deny", "ts": TS},
    ]
    mode = (tmp_path / "by-session" / f"{SID}.trust-events.jsonl").stat().st_mode & 0o777
    assert mode == 0o600


def test_append_refuses_a_bad_session_id_and_fails_open_on_an_unwritable_folder(tmp_path):
    assert te.append_events(str(tmp_path), "../x", te.index_events([1], TS), ON) is False
    assert te.append_events(str(tmp_path), None, te.index_events([1], TS), ON) is False
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert te.append_events(str(blocker), SID, te.index_events([1], TS), ON) is False


def test_append_takes_under_a_millisecond(tmp_path):
    evs = te.index_events(range(1, 31), TS)  # one prompt's 30 rows
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        te.append_events(str(tmp_path), SID, evs, ON)
        times.append(time.perf_counter() - t0)
    # The budget is 1 ms; a loaded CI runner gets five times that.
    assert statistics.median(times) < 0.005, statistics.median(times)


def test_record_index_counts_only_the_recall_hook_events(tmp_path):
    for label in ("SubagentStart:meta", "AgentRewrite", "Stop"):
        assert te.record_index(str(tmp_path), SID, label, [1], ON) is False
    assert te.record_index(str(tmp_path), SID, "UserPromptSubmit", [7, 7, 8], ON)
    assert [e["mv_id"] for e in _lines(tmp_path)] == [7, 8]


def test_the_default_cache_folder_is_in_the_data_dir(hook_env):
    """Without NOBLIVION_RECALL_CACHE_DIR the events go to ``<data dir>/cache``."""
    assert te.cache_dir() == str(hook_env["data"] / "cache")
    assert te.cache_dir({"NOBLIVION_DATA_DIR": str(hook_env["data"] / "x")}) == str(
        hook_env["data"] / "x" / "cache"
    )
    env = dict(ON, NOBLIVION_DATA_DIR=str(hook_env["data"]))
    assert te.record_guard(env, SID, "deny", ["feedback_a"])
    got = _lines(hook_env["data"] / "cache")
    assert [(e["path"], e["src"]) for e in got] == [("feedback_a.md", "guard_deny")]


# 4. the guard hook ------------------------------------------------------
@pytest.fixture
def guard_env(tmp_path, hook_env) -> dict[str, str]:
    table = tmp_path / "table.json"
    entries = [
        {
            "id": "feedback_no_force",
            "rule": "Never force-push.",
            "apply": "Use a new branch.",
            "scope": "tool",
            "triggers": ["git push --force"],
            "violates": r"git\s+push\s+.*--force(?!-with-lease)",
        },
        {
            "id": "feedback_pytest",
            "rule": "Run pytest with -q.",
            "apply": "pytest -q",
            "scope": "tool",
            "triggers": ["python -m pytest", "pytest"],
            "violates": "",
        },
    ]
    table.write_text(json.dumps({"version": 1, "entries": entries}))
    return {
        "HOME": str(hook_env["home"]),
        "NOBLIVION_DATA_DIR": str(hook_env["data"]),
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "guard.jsonl"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",
    }


def _guard(env: dict[str, str], command: str, cwd: str) -> str:
    event = {
        "session_id": SID,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
    }
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    return out.getvalue()


def test_guard_deny_and_rows_append_use_events_by_path(tmp_path, guard_env):
    env = dict(guard_env, **ON)
    deny = _guard(env, "git push --force origin x", str(tmp_path))
    rows = _guard(env, "python -m pytest tests/test_x.py", str(tmp_path))
    assert '"permissionDecision": "deny"' in deny and "additionalContext" in rows
    got = _lines(tmp_path / "cache")
    assert [(e["path"], e["src"], e["kind"]) for e in got] == [
        ("feedback_no_force.md", "guard_deny", "use"),
        ("feedback_pytest.md", "guard_rows", "use"),
    ]


def test_guard_override_appends_nothing(tmp_path, guard_env):
    _guard(
        dict(guard_env, **ON),
        "git push --force origin x  # guard-ok: a rebased branch of mine",
        str(tmp_path),
    )
    log = (tmp_path / "guard.jsonl").read_text().splitlines()
    assert [json.loads(x)["decision"] for x in log] == ["override"]
    assert _lines(tmp_path / "cache") == []


def test_guard_with_events_off_prints_the_same_and_writes_nothing(tmp_path, guard_env):
    off_env = dict(guard_env, NOBLIVION_TRUST_EVENTS="0")
    off = _guard(off_env, "git push --force origin x", str(tmp_path))
    on_env = dict(guard_env, NOBLIVION_GUARD_STATE_DIR=str(tmp_path / "state2"), **ON)
    on = _guard(on_env, "git push --force origin x", str(tmp_path))
    assert off == on
    assert len(_lines(tmp_path / "cache")) == 1  # only the run with events on


# 5. transcripts ---------------------------------------------------------
def test_read_records_stops_before_a_partial_last_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"a": 1}\nnot json\n{"b": 2}\n{"c": ')
    recs, end = te.read_records(str(path))
    assert [r for _, r in recs] == [{"a": 1}, {"b": 2}]
    assert end == len(b'{"a": 1}\nnot json\n{"b": 2}\n')
    with open(path, "ab") as fh:
        fh.write(b"3}\n")
    recs, end2 = te.read_records(str(path), end)
    assert [r for _, r in recs] == [{"c": 3}] and end2 == path.stat().st_size


def test_iter_transcript_files_finds_subagents_and_workflow_agents(tmp_path):
    main = tmp_path / "s1.jsonl"
    main.write_text("")
    sub = tmp_path / "s1" / "subagents"
    (sub / "workflows" / "wf_1").mkdir(parents=True)
    for p in (
        sub / "agent-a.jsonl",
        sub / "workflows" / "wf_1" / "agent-b.jsonl",
        sub / "workflows" / "wf_1" / "journal.jsonl",
        sub / "agent-c.meta.json",
    ):
        p.write_text("")
    got = [os.path.relpath(p, tmp_path) for p in te.iter_transcript_files(str(main))]
    assert got == [
        "s1.jsonl",
        "s1/subagents/agent-a.jsonl",
        "s1/subagents/workflows/wf_1/agent-b.jsonl",
    ]


# review findings ---------------------------------------------------------
def test_the_guard_alarm_still_reaches_the_guard_through_the_trust_call(
    tmp_path, guard_env, monkeypatch
):
    """The guard's own time limit (a BaseException) is not swallowed by the
    trust append; the decision was already printed."""
    real = gh._load

    class Slow:
        @staticmethod
        def enabled(*a, **k):
            return True

        @staticmethod
        def record_guard(*a, **k):
            raise gh._TimeUp()

    monkeypatch.setattr(gh, "_load", lambda name: Slow if name == "trust_events" else real(name))
    out = _guard(dict(guard_env, **ON), "git push --force origin x", str(tmp_path))
    assert '"permissionDecision": "deny"' in out
    lines = [json.loads(x) for x in (tmp_path / "guard.jsonl").read_text().splitlines()]
    assert [x["decision"] for x in lines] == ["deny", "error"] and lines[1]["error"] == "timeout"
