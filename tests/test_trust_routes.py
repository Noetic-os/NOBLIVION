# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trust routes over a live store (design doc 0001, sections 4.5 and 4.6).

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import sqlite3

import pytest

from noblivion import db, trust
from test_store import running  # noqa: F401 - the fixture

FEEDBACK = "/api/memory/feedback/batch"
REPORT = "/api/memory/trust/report"


def _ids(run) -> dict[str, int]:
    with run.store.connection() as conn:
        rows = conn.execute("SELECT path, id FROM memories").fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def test_feedback_batch_stores_and_is_idempotent(running):  # noqa: F811
    ids = _ids(running)
    body = {
        "session_id": "6f1c2a8e-0b7d-4c55-9a51-3e2f0d9c7b10",
        "root": "-work-proj-demo",
        "project": "ignored",
        "events": [
            {"kind": "recall", "mv_id": ids["feedback_tests_use_venv.md"]},
            {"kind": "use", "path": "feedback_tests_use_venv.md", "sources": ["guard_deny"]},
            {
                "kind": "contradict",
                "path": "feedback_tests_use_venv.md",
                "sources": ["guard_override"],
            },
            {"kind": "use", "mv_id": 2_000_000_001},
            {"kind": "maybe", "mv_id": 1},
        ],
    }
    status, answer, _ = running.request("POST", FEEDBACK, body=body)
    assert (status, answer) == (200, {"inserted": 3, "duplicate": 0, "unknown": 1, "rejected": 1})
    status, answer, _ = running.request("POST", FEEDBACK, body=body)
    assert (status, answer) == (200, {"inserted": 0, "duplicate": 3, "unknown": 1, "rejected": 1})
    with running.store.connection() as conn:
        row = conn.execute(
            "SELECT trials, use_pos, contradiction_count, trust_score FROM feedback "
            "WHERE memory_id = ?",
            (ids["feedback_tests_use_venv.md"],),
        ).fetchone()
    assert tuple(row[:3]) == (1, 1.0, 1)
    assert row[3] < 0.5  # a deny and its override: below the prior


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"[1, 2]", 400, "body must be a JSON object"),
        (b"{not json", 400, "body is not valid JSON"),
        ({"session_id": "bad id", "events": []}, 400, None),
        ({"session_id": "s1", "events": "x"}, 400, "events must be a list"),
        ({"session_id": "s1", "events": [{"kind": "use", "mv_id": 1}] * 501}, 413, None),
    ],
)
def test_feedback_batch_refusals(running, body, status, detail):  # noqa: F811
    got, answer, _ = running.request("POST", FEEDBACK, body=body)
    assert got == status and set(answer) == {"detail"}
    if detail is not None:
        assert answer["detail"] == detail


def test_feedback_batch_store_failure_is_503_and_writes_nothing(running, monkeypatch):  # noqa: F811
    ids = _ids(running)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(trust, "recompute", broken)
    body = {"session_id": "s1", "events": [{"kind": "use", "mv_id": min(ids.values())}]}
    status, answer, _ = running.request("POST", FEEDBACK, body=body)
    assert (status, answer) == (503, {"detail": "memory feedback store failed"})
    with running.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM feedback_events").fetchone()[0] == 0


def test_trust_report_route(running):  # noqa: F811
    status, answer = running.get(REPORT)
    assert status == 200
    assert answer["persona"] == "claude_code" and answer["truncated"] is False
    assert {k: answer[k] for k in ("retire", "promote", "demote")} == {
        "retire": [],
        "promote": [],
        "demote": [],
    }
    assert running.get(REPORT + "?persona=claude_code")[0] == 200
    assert running.get(REPORT + "?persona=other") == (
        400,
        {"detail": "the trust report serves persona claude_code only"},
    )
    assert running.request("POST", REPORT, body={})[0] == 405


def test_trust_report_store_failure_is_503(running, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(trust, "report", broken)
    assert running.get(REPORT) == (503, {"detail": "trust report unavailable"})


def test_the_store_runs_a_maintenance_pass_at_start(running):  # noqa: F811
    with running.store.connection() as conn:
        assert db.get_meta(conn, "last_maintenance_at") is not None
