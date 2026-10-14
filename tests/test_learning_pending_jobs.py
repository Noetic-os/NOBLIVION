# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pending extraction jobs are listed with a reason and an age, never dropped."""

import hashlib
import json
import time

import pytest

from noblivion import db, learning_cli
from noblivion.learning import LearningLedger

DAY = 86400


def batch(number, session):
    text = f"Use short progress updates {number}."
    return {
        "batch_id": f"batch-{number}",
        "namespace": "test",
        "root": "demo",
        "source_client": "claude",
        "session_id": session,
        "records": [
            {
                "source_id": f"source-{number}",
                "record_id": f"record-{number}",
                "role": "user",
                "content": text,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "observed_at": "2026-10-12T09:00:00Z",
                "metadata": {"authority": "direct_user", "direct_user": True},
            }
        ],
        "cursor": {"path_id": "transcript", "offset": number * 100},
    }


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "learning.db"
    conn = db.open_db(path, create=True)
    yield path, LearningLedger(conn)
    conn.close()


def age(ledger, days):
    ledger.conn.execute("UPDATE learning_jobs SET created_at=created_at-?", (days * DAY,))
    ledger.conn.execute("UPDATE learning_controls SET updated_at=updated_at-?", (days * DAY,))


def test_paused_sessions_are_listed_with_reason_and_age(database):
    _, ledger = database
    for number, session in enumerate(("one", "one", "two", "three"), start=1):
        ledger.capture_batch(batch(number, session))
    for session in ("one", "two"):
        ledger.set_controls("test", "demo", session, paused=True)
    age(ledger, 2)
    result = ledger.status("test", "demo")["pending"]
    assert result["total"] == 4 and result["stale"] == 0
    by_session = {}
    for job in result["jobs"]:
        by_session.setdefault(job["session_id"], []).append(job)
    assert {j["reason"] for j in by_session["one"] + by_session["two"]} == {"session paused"}
    assert by_session["three"][0]["reason"] == "waiting for a worker"
    assert all(1.9 < j["age_days"] < 2.1 for j in result["jobs"])
    assert not any(j["stale"] for j in result["jobs"])


def test_a_pause_older_than_seven_days_is_reported_as_stale(database):
    _, ledger = database
    ledger.capture_batch(batch(1, "old"))
    ledger.set_controls("test", "demo", "old", paused=True)
    age(ledger, 8)
    ledger.capture_batch(batch(2, "new"))
    ledger.set_controls("test", "demo", "new", paused=True)
    result = ledger.status()["pending"]
    assert result["stale"] == 1
    stale = [j for j in result["jobs"] if j["stale"]]
    assert [j["session_id"] for j in stale] == ["old"]
    # Reported, not dropped: the job is still pending and resumes when unpaused.
    assert ledger.status()["jobs"] == {"pending": 2}
    ledger.set_controls("test", "demo", "old", paused=False)
    assert ledger.claim("worker")["session_id"] == "old"


def test_scope_pause_provider_error_and_budget_reasons(database):
    _, ledger = database
    for number in (1, 2, 3):
        ledger.capture_batch(batch(number, f"s{number}"))
    ledger.set_controls("test", "demo", "s1", contribute=False)
    ledger.conn.execute(
        "UPDATE learning_jobs SET last_error='ProviderError',attempts=1 WHERE session_id='s2'"
    )
    ledger.conn.execute(
        "UPDATE learning_jobs SET last_error='BudgetExceeded',next_attempt=? WHERE session_id='s3'",
        (time.time() + 600,),
    )
    reasons = {j["session_id"]: j["reason"] for j in ledger.status()["pending"]["jobs"]}
    assert reasons == {
        "s1": "session contribution off",
        "s2": "provider error: ProviderError",
        "s3": "budget",
    }
    ledger.set_controls("test", "demo", paused=True)
    reasons = {j["session_id"]: j["reason"] for j in ledger.status()["pending"]["jobs"]}
    assert reasons["s2"] == reasons["s3"] == "scope paused"


def test_status_command_prints_the_pending_jobs(database, capsys):
    path, ledger = database
    ledger.capture_batch(batch(1, "paused-session"))
    ledger.set_controls("test", "demo", "paused-session", paused=True)
    ledger.conn.commit()
    assert learning_cli.main(["--database", str(path), "status", "--project", "test"]) == 0
    pending = json.loads(capsys.readouterr().out)["pending"]
    assert pending["total"] == 1
    assert pending["jobs"][0]["reason"] == "session paused"
