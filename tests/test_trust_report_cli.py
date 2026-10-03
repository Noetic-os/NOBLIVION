# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion trust report`` prints the trust report (design doc 0001, section 9.5).

The session start line says "Run noblivion trust report"; this is the command
it names. It reads the database directly, so the store need not run.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from noblivion import __main__ as cli
from noblivion import db, trust


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    for key in ("CLAUDE_PLUGIN_DATA", "NOBLIVION_CONFIG", "NOBLIVION_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(folder))
    db.open_db(folder / "noblivion.db", create=True).close()
    return folder


def test_report_prints_the_three_lists(data, capsys):
    assert cli.main(["trust", "report"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Memory trust report: generated ")
    for heading in ("Retire", "Promote", "Demote"):
        assert f"\n{heading} (" in out
    assert "You decide." in out


def test_report_json_is_the_store_answer(data, capsys):
    assert cli.main(["trust", "report", "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["persona"] == trust.PERSONA
    assert answer["retire"] == [] and answer["promote"] == [] and answer["demote"] == []


def test_render_report_lists_rows_and_cuts_at_the_limit():
    row = {
        "mv_id": 7,
        "root": "-proj",
        "path": "feedback_a.md",
        "trust": 0.81234,
        "trials": 9,
        "shown_sessions": 30,
        "use_sessions": 9,
        "reason": "used often",
    }
    answer = {"generated_at": "t", "trust_prior": 0.5, "retire": [], "demote": []}
    answer["promote"] = [row, dict(row, mv_id=8)]
    text = trust.render_report(answer, limit=1)
    assert "- -proj/feedback_a.md (id 7): trust 0.81, trials 9" in text
    assert "- ... 1 more (use --limit)" in text and "(id 8)" not in text


def test_a_locked_database_is_one_line_and_exit_5(data, capsys, monkeypatch):
    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(trust, "report", locked)
    assert cli.main(["trust", "report"]) == 5
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "database is locked" in err


def test_usage_names_the_report_command(capsys):
    assert cli.main(["--help"]) == 0
    assert "noblivion trust report" in capsys.readouterr().out
