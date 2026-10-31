# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm a: following live files by offset, and counts only."""

from __future__ import annotations

import json

from longtest import live


def test_read_new_waits_for_a_complete_line_and_resumes(tmp_path):
    path, state = tmp_path / "log", {}
    path.write_text("one\ntwo\npart")
    assert live.read_new(path, state) == ["one", "two"]
    assert live.read_new(path, state) == []
    path.write_text("one\ntwo\npartial done\nthree\n")
    assert live.read_new(path, state) == ["partial done", "three"]


def test_read_new_notices_a_rotation(tmp_path):
    path, state = tmp_path / "log", {}
    path.write_text("a\nb\nc\n")
    assert len(live.read_new(path, state)) == 3
    path.unlink()
    path.write_text("x\n")
    assert live.read_new(path, state) == ["x"]


def test_missing_file_is_empty(tmp_path):
    assert live.read_new(tmp_path / "none", {}) == []


def test_recall_line_parse():
    line = (
        "2026-10-31T04:21:05+00:00 event=UserPromptSubmit session=abc hits=4 chars=2081 "
        "ms=549 ok:joined194of194:floor162of194"
    )
    got = live.parse_recall_line(line)
    assert got["hits"] == 4 and got["chars"] == 2081 and got["ms"] == 549 and not got["fail"]
    assert got["state"] == "ok"
    assert live.parse_recall_line("garbage") is None
    assert live.parse_recall_line(line.replace("ok:", "ms=1 fail:"))["fail"]


def test_guard_line_drops_the_text():
    doc = {
        "ts": "t",
        "session_id": "s",
        "agent_id": "a1",
        "tool": "Bash",
        "decision": "deny",
        "ids": ["n1"],
        "text": "rm -rf /secret/path",
    }
    got = live.parse_guard_line(json.dumps(doc), "salt")
    assert "text" not in got and "rm -rf" not in json.dumps(got) and got["ids"] == 1
    assert got["who"] != "a1" and len(got["who"]) == 12


def test_transcript_counts_and_resent_tokens():
    lines = [
        json.dumps(
            {"type": "user", "message": {"content": "hello"}, "timestamp": "2026-01-01T10:00:00Z"}
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T10:00:05Z",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 90,
                        "cache_creation_input_tokens": 0,
                    },
                    "content": [{"type": "tool_use"}, {"type": "text"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T10:00:09Z",
                "message": {"usage": {"input_tokens": 5}, "content": []},
            }
        ),
        "not json",
    ]
    got = live.transcript_counts(lines)
    assert got["calls"] == 2 and got["input_tokens"] == 105 and got["tool_calls"] == 1
    assert got["prompts"] == 1
    # one injection of 400 characters before the second call: 100 tokens x 1 later call
    assert live.resent_tokens([("2026-01-01T10:00:06Z", 400)], got["call_times"]) == 100


def test_pass_once_over_a_fake_install(tmp_path):
    cfg, data = tmp_path / "cfg", tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    (cfg / "projects" / "-work-a").mkdir(parents=True)
    (data / "cache" / "recall.log").write_text(
        "2026-01-01T10:00:06+00:00 event=UserPromptSubmit session=s1 hits=3 chars=400 ms=100 ok\n"
    )
    (data / "guard-log.jsonl").write_text(json.dumps({"decision": "deny", "text": "secret"}) + "\n")
    (cfg / "projects" / "-work-a" / "s1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T10:00:09Z",
                "message": {"usage": {"input_tokens": 1000}, "content": []},
            }
        )
        + "\n"
    )
    state: dict = {}
    first = live.Live(cfg, data, state, "salt").pass_once()
    again = live.Live(cfg, data, state, "salt").pass_once()
    assert len(first["recall"]) == 1 and again["recall"] == [] and again["sessions"] == {}
    day = live.daily([first])
    assert day["prompt_hooks"] == 1 and day["guard_events"] == {"deny": 1}
    assert day["resent_tokens_estimate"] == 100 and day["input_tokens"] == 1000
    assert "secret" not in json.dumps(day) and "secret" not in json.dumps(first)
    assert state and all("offset" in v for v in state.values())
