# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tools/replay_transcripts.py (NOBLIVION-39), on a made-up transcript."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from noblivion import db
from store_helpers import add_memory

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SID = "0b6c2f0e-1111-4c4c-8888-000000000001"
NAME = "feedback_run_the_widget_checks"
RULE = "Run the widget checks before every deploy of the widget service."


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "replay_transcripts", TOOLS / "replay_transcripts.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["replay_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load_tool()


@pytest.fixture()
def clean_modules():
    """The replay loads the hooks under their own names: drop them after."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def _user(ts: str, text, **extra) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def _assistant(ts: str, text: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _transcript() -> list[dict]:
    return [
        _user("2026-01-05T10:00:00.000Z", "please deploy the widget service"),
        _assistant(
            "2026-01-05T10:00:20.000Z",
            f"Per {NAME}, I run the widget checks first. They pass.",
        ),
        _user(
            "2026-01-05T10:00:30.000Z",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        ),
        _user("2026-01-05T10:01:00.000Z", "[Request interrupted by user]"),
        _user("2026-01-05T10:02:00.000Z", "summary of the session", isCompactSummary=True),
        _user("2026-01-05T10:03:00.000Z", "<command-name>/clear</command-name>"),
        _user("2026-01-05T10:04:00.000Z", "now deploy it again"),
    ]


def test_prompt_text_keeps_only_typed_prompts(clean_modules) -> None:
    plugin = rt.Plugin(rt.Clock())
    texts = [rt.prompt_text(r, plugin.signals) for r in _transcript()]
    assert [t for t in texts if t is not None] == [
        "please deploy the widget service",
        "now deploy it again",
    ]


def test_main_transcripts_skips_subagents_and_sorts_by_time(tmp_path: Path) -> None:
    late = _write(tmp_path / "b.jsonl", [_user("2026-01-06T00:00:00Z", "x")])
    early = _write(tmp_path / "a2.jsonl", [_user("2026-01-04T00:00:00Z", "x")])
    _write(tmp_path / "a2" / "subagents" / "agent-1.jsonl", [_user("2026-01-01T00:00:00Z", "x")])
    _write(tmp_path / "empty.jsonl", [{"type": "summary"}])
    found = rt.main_transcripts([str(tmp_path / "**" / "*.jsonl"), str(tmp_path / "*.jsonl")])
    assert found == [str(early), str(late)]


def test_note_births_count_tool_inputs_only(tmp_path: Path) -> None:
    rec_result = _user(
        "2026-01-01T00:00:00Z",
        [{"type": "tool_result", "tool_use_id": "t0", "content": f"{NAME}.md"}],
    )
    rec_call = {
        "type": "assistant",
        "timestamp": "2026-01-03T00:00:00Z",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": f"/m/{NAME}.md"}}
            ]
        },
    }
    later = dict(rec_call, timestamp="2026-01-04T00:00:00Z")
    path = _write(tmp_path / "s.jsonl", [rec_result, later, rec_call])
    births = rt.note_births([str(path)], {f"{NAME}.md", "other_note.md"})
    assert births == {f"{NAME}.md": _dt.datetime(2026, 1, 3, tzinfo=_dt.timezone.utc)}


def test_filter_payload_drops_notes_that_do_not_exist_yet() -> None:
    payload = {
        "results": ["# a\n\n[claude_code_md: a_note.md]\n", "# b\n\n[claude_code_md: b_note.md]\n"],
        "scores": [0.9, 0.8],
        "namespace": "claude_code",
    }
    out = rt.filter_payload(payload, lambda name: name != "a_note.md")
    assert out["results"] == [payload["results"][1]]
    assert out["scores"] == [0.8]
    assert out["namespace"] == "claude_code"
    assert rt.filter_payload(payload, lambda name: True) is payload


def test_replay_session_writes_events_with_the_original_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_modules
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("NOBLIVION_RECALL_CACHE_DIR", raising=False)
    conn = db.open_db(data / "noblivion.db", create=True)
    mid = add_memory(conn, NAME, RULE)
    path = _write(tmp_path / "proj" / f"{SID}.jsonl", _transcript())

    clock = rt.Clock()
    plugin = rt.Plugin(clock)
    env = rt.replay_env({"NOBLIVION_DATA_DIR": str(data)}, str(tmp_path / "memory"), False)
    cache = plugin.te.cache_dir(env)
    calls: list[str] = []

    def fake_prompt(sid: str, text: str, environ) -> None:
        # What the prompt hook writes when it shows the note.
        calls.append(text)
        plugin.signals.record_shown(cache, sid, "UserPromptSubmit", [(mid, NAME, RULE)], environ)
        plugin.te.record_index(cache, sid, "UserPromptSubmit", [mid], environ)

    monkeypatch.setattr(plugin, "prompt", fake_prompt)
    got = rt.replay_session(str(path), plugin, clock, env, conn)

    assert calls == ["please deploy the widget service", "now deploy it again"]
    assert got["prompts"] == 2
    assert got["shown"] == 1
    assert got["events"]["store:inserted"] == 2
    rows = conn.execute(
        "SELECT kind, ts FROM feedback_events WHERE session_id = ? ORDER BY kind", (SID,)
    ).fetchall()
    conn.close()
    assert [(r[0], r[1][:19]) for r in rows] == [
        ("recall", "2026-01-05T10:00:00"),
        ("use", "2026-01-05T10:00:20"),
    ]
    assert len(got["trace"]) == 1
    line = got["trace"][0]
    assert (line["kind"], line["src"], line["name"]) == ("use", "citation", NAME)
    assert NAME in line["text"]
