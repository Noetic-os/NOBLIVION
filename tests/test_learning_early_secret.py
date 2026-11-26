# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-285: a secret in a later window fails the job before any model call.

The check before each call refused such a job only at that window, after the
earlier windows had used model time. The worker now checks the request of every
pending window before the first call. The check before each call stays.
"""

from __future__ import annotations

import json

import pytest

from noblivion import db
from noblivion.learning import LearningLedger
from noblivion.redaction import contains_secret
from test_learning_window_reject import (
    FILLER,
    FIRST,
    SCALE,
    SECOND,
    WINDOW_CHARS,
    WindowProvider,
    job,
    record,
    worker,
)

# The key is in the middle of a line, so the source passes every check at
# capture and in validate_sources. A window part that starts at the key puts it
# at a line start, and only the check of the provider request finds it.
HIDDEN = "pass: " + "Zq8hunter2" + "example"
PART = min(WINDOW_CHARS * SCALE, 262144) // 2


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "early.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def sliced_secret():
    """A source that passes as a whole, with a part that starts at the key."""
    lead = (SECOND + " The Go notes say" + " more" * PART)[: PART - 1] + " "
    text = f"{lead}{HIDDEN} for the job\n{FILLER}"
    assert not contains_secret(text) and contains_secret(text[PART:])
    return text


def test_a_secret_in_a_later_window_fails_the_job_with_no_model_call(ledger):
    text = sliced_secret()
    ledger.capture_batch(
        {
            "batch_id": "batch-early",
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": "early-session",
            "records": [
                record("s1", f"{FIRST}\n{FILLER}"),
                record("s2", text),
            ],
        }
    )
    provider = WindowProvider(json.dumps({"version": 1, "candidates": []}))

    result = worker(ledger, provider).run_once()

    assert result["status"] == "failed" and result["permanent"] is True
    assert result["error"] == "ValidationError"
    assert provider.extract_calls == []
    _, status, last_error, _ = job(ledger)
    assert status == "exhausted"
    assert last_error.startswith("ValidationError: unsafe")
    assert ledger.conn.execute("SELECT count(*) FROM learning_extraction_pages").fetchone()[0] == 0


def test_a_secret_in_a_window_that_makes_no_call_does_not_fail_the_job(ledger):
    # An assistant part holds no evidence, so its windows make no model call
    # and the early check skips them, as the check before each call does.
    second = record("s2", sliced_secret())
    second["role"] = "assistant"
    ledger.capture_batch(
        {
            "batch_id": "batch-quiet",
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": "quiet-session",
            "records": [record("s1", f"{FIRST}\n{FILLER}"), second],
        }
    )
    provider = WindowProvider(json.dumps({"version": 1, "candidates": []}))

    assert worker(ledger, provider).run_once()["status"] == "done"
    assert provider.extract_calls == [["s1"]]
