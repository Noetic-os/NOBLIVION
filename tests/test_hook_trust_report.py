# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trust report tool (``hooks/trust_report.py``).

Fictional data only. What these tests hold:

1. It reads the section 4.6 answer (persona claude_code, three lists of rows
   with ``root``), refuses a wrong persona or a missing list, drops a
   malformed row, and writes the cache file (the report, ``fetched_at`` and
   the counts) atomically as mode 0600.
2. A store that is down writes nothing. ``--cache-only`` makes no store call.
3. End to end: the report of a live store, found through ``store.json`` and
   proved with the HMAC nonce, reaches the SessionStart line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook

rep = load_hook("trust_report", "trust_report_t_report")
sl = load_hook("trust_session_line", "trust_session_line_t_report")


def row(mid: int, path: str, **kw: Any) -> dict[str, Any]:
    d = {
        "mv_id": mid,
        "root": "-work-proj-demo",
        "path": path,
        "trust": 0.5,
        "trials": 0,
        "shown_sessions": 25,
        "use_sessions": 0,
        "reason": "shown in 25 sessions, used in none; first indexed 41 days ago",
    }
    d.update(kw)
    return d


REPORT = {
    "persona": "claude_code",
    "generated_at": "2026-10-02T03:00:00Z",
    "trust_prior": 0.5,
    "retire": [row(1, "feedback_a.md"), row(2, "feedback_b.md")],
    "promote": [row(3, "feedback_c.md", trust=0.8, trials=9, use_sessions=9)],
    "demote": [],
    "truncated": False,
}


def test_validate_accepts_the_answer_and_drops_a_malformed_row():
    doc = dict(
        REPORT,
        retire=REPORT["retire"] + [{"mv_id": "x"}, "nope", {"mv_id": True, "path": "p.md"}],
    )
    out = rep.validate(doc)
    assert [r["mv_id"] for r in out["retire"]] == [1, 2] and len(out["promote"]) == 1
    assert out["retire"][0]["root"] == "-work-proj-demo" and out["truncated"] is False


@pytest.mark.parametrize(
    "doc",
    [
        dict(REPORT, persona="other"),
        {k: v for k, v in REPORT.items() if k != "demote"},
        dict(REPORT, retire="x"),
        ["not", "an", "object"],
    ],
)
def test_validate_refuses_a_wrong_persona_or_a_missing_list(doc):
    with pytest.raises(ValueError):
        rep.validate(doc)


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "NOBLIVION_TRUST_REPORT_FILE": str(tmp_path / "cache" / "trust-report.json"),
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
        "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
        "NOBLIVION_STORE_AUTOSTART": "0",
    }


def test_the_default_cache_file_is_the_one_the_session_line_reads(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    assert rep.cache_file(env) == str(tmp_path / "data" / "cache" / "trust-report.json")


def test_refresh_writes_the_cache_with_counts(env, monkeypatch):
    seen = {}

    def fake_store_get(build, environ, timeout):
        seen.update(url=build("http://127.0.0.1:9"), timeout=timeout)
        return REPORT

    monkeypatch.setattr(rep.rh(), "store_get", fake_store_get)
    rep.refresh(env)
    assert seen == {
        "url": "http://127.0.0.1:9/api/memory/trust/report?persona=claude_code",
        "timeout": rep.TIMEOUT_S,
    }
    path = Path(env["NOBLIVION_TRUST_REPORT_FILE"])
    doc = json.loads(path.read_text())
    assert doc["counts"] == {"retire": 2, "promote": 1, "demote": 0}
    assert doc["format"] == rep.FORMAT and doc["fetched_at"] and doc["persona"] == "claude_code"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_a_store_that_is_down_writes_nothing(env, capsys):
    with pytest.raises(rep.rh().RecallError):
        rep.refresh(env)
    assert not Path(env["NOBLIVION_TRUST_REPORT_FILE"]).exists()
    assert rep.main([], env) == 1
    assert "did not answer (store_down)" in capsys.readouterr().err


def test_cache_only_prints_without_a_store_call(env, monkeypatch, capsys):
    rep.write_cache(rep.validate(REPORT), env)
    monkeypatch.setattr(rep.rh(), "store_get", lambda *a: pytest.fail("must not be called"))
    assert rep.main(["--cache-only"], env) == 0
    out = capsys.readouterr().out
    assert "Retire candidates" in out and "Demote candidates" in out
    assert "-work-proj-demo/feedback_a.md (id 1)" in out
    assert rep.main(["--cache-only", "--json"], env) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["mv_id"] for r in doc["retire"]] == [1, 2] and doc["fetched_at"]


def test_no_cache_and_no_store_is_a_clear_failure(env, capsys):
    assert rep.main(["--cache-only"], env) == 1
    assert "cannot read it" in capsys.readouterr().err


def test_end_to_end_a_live_store_report_reaches_the_session_line(tmp_path, env):
    from noblivion import db, store, trust
    from store_helpers import add_memory
    from test_store import Running, make_store, wait_for

    run = Running(make_store(tmp_path))
    try:
        assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
        with run.store.connection() as conn:
            memory_id = add_memory(conn, "feedback_never_used", "A rule no one uses.")
            with db.write_tx(conn):
                conn.execute("UPDATE memories SET created_at = '2026-01-01T00:00:00.000000Z'")
            for i in range(20):
                body = {"session_id": f"s{i}", "events": [{"kind": "recall", "mv_id": memory_id}]}
                trust.store_batch(conn, trust.parse_batch(body))
        env = dict(env, NOBLIVION_DATA_DIR=str(run.store.data_dir))
        assert rep.main([], env) == 0
        doc = json.loads(Path(env["NOBLIVION_TRUST_REPORT_FILE"]).read_text())
        assert [(r["mv_id"], r["root"]) for r in doc["retire"]] == [(memory_id, "-proj-alpha")]
        line = sl.trust_line(doc)
        assert line.startswith("Memory trust: 1 to retire, 0 to promote, 0 to demote.")
    finally:
        run.stop()


def test_render_names_contradictions_and_says_trust_is_usage_evidence():
    """NOBLIVION-34. An older store sends no ``contradict_sessions``: the row
    then reads as before."""
    doc = rep.validate(
        dict(REPORT, retire=[row(1, "feedback_a.md", contradict_sessions=3), row(2, "b.md")])
    )
    text = rep.render(doc)
    assert "feedback_a.md (id 1): " in text and "used in 0, contradicted in 3." in text
    assert text.count("contradicted in") == 1  # b.md has no field
    assert "usage evidence" in text and "does not say whether a note is correct" in text
