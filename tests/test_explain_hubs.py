# SPDX-License-Identifier: AGPL-3.0-or-later
"""`noblivion explain --hubs` lists hub notes as candidates for the index (NOBLIVION-114).

Fictional data only. The command reads the hub counts of the prompt hook and writes nothing.
"""

from __future__ import annotations

import datetime as dt
import json

from noblivion import config, explain


def _write_hub_stats(folder, sessions, shown):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cache = folder / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "hub-stats.json").write_text(
        json.dumps({"v": 1, "days": {today: {"s": sessions, "n": shown}}})
    )
    return cache


def test_explain_lists_hub_notes_as_candidates_and_writes_nothing(tmp_path, capsys):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 20, {"note_a": 15, "note_b": 11, "note_c": 10, "note_d": 2})
    env = {"NOBLIVION_DATA_DIR": str(folder), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    result = explain.hub_notes(folder=folder, env=env)
    assert [c["note"] for c in result["candidates"]] == ["note_a", "note_b"]  # note_c: exactly half
    assert result["candidates"][0] == {"note": "note_a", "sessions": 15, "share": 0.75}
    assert result["sessions"] == 20 and "never edits" in result["action"]
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_explain_hubs_reports_too_few_sessions_and_a_window_that_is_off(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 3, {"note_a": 3})
    env = {"NOBLIVION_DATA_DIR": str(folder), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
    few = explain.hub_notes(folder=folder, env=env)
    assert few["candidates"] == [] and "Fewer than" in few["reason"]
    off = explain.hub_notes(folder=folder, env=dict(env, NOBLIVION_RECALL_HUB_DAYS="0"))
    assert off["candidates"] == [] and "off" in off["reason"]


def test_the_hubs_option_needs_no_prompt_and_a_missing_prompt_is_still_refused(
    tmp_path, capsys, monkeypatch
):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 20, {"note_a": 15})
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(folder))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(cache))
    assert explain.main(["--hubs", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["note"] for c in doc["candidates"]] == ["note_a"]
    assert explain.main(["--hubs"]) == 0
    assert "note_a: shown in 15 sessions (0.75)" in capsys.readouterr().out
    assert explain.main([]) == 2
    assert "prompt is missing" in capsys.readouterr().out
    assert config.DB_FILE not in {p.name for p in folder.iterdir()}
