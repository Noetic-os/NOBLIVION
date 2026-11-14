# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-247: an exhausted window no longer fails the whole job.

A saturated page gets a continuation. A continuation that drops all twelve of
its proposals makes no progress, so the window stops with an exhausted receipt.
Like a rejected window (NOBLIVION-244), the job goes on with its next window.
No dropped candidate is stored, the budget charges every call, and the hard
decision limit still ends the job.
"""

from __future__ import annotations

import hashlib
import json
from threading import Event

import pytest

from noblivion import db, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings

FILLER = "\n".join(f"Filler line {i:03d} without a lesson." for i in range(12))
FIRST = "Keep responses short."
SECOND = "Use tabs in Go files."
# The reason of every exhausted receipt in 0.1.28, before this fix.
OLD_REASON = "hard limit or repeated saturated response; coverage incomplete"
# One source per window, under every extraction contract (as in the NOBLIVION-244 tests).
SCALE = getattr(learning_worker, "WINDOW_SCALE", {}).get(learning_worker.EXTRACTION_CONTRACT, 1)
WINDOW_CHARS = -(-1024 // SCALE)
LIMITS = BudgetLimits(max_calls=64, max_tokens=5_000_000, max_seconds=10_000)


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "exhausted.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def record(ident, text):
    return {
        "source_id": ident,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "role": "user",
        "observed_at": "2026-01-02T12:00:00Z",
        "metadata": {},
    }


def capture_two_windows(ledger):
    ledger.capture_batch(
        {
            "batch_id": "batch-two",
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": "exhausted-session",
            "records": [
                record("s1", f"{FIRST}\n{FILLER}"),
                record("s2", f"{SECOND}\n{FILLER}"),
            ],
        }
    )


def candidate(ident, quote, key):
    return {
        "action": "add",
        "logical_key": key,
        "category": "preference",
        "scope": "project",
        "title": "Style",
        "statement": quote,
        "observed_at": "2026-01-02",
        "effective_at": None,
        "evidence": [{"source_id": ident, "quote": quote}],
    }


def absent(ident, count, start=0):
    """Candidates whose evidence quote is not in the source, so each is dropped."""
    return [
        candidate(ident, f"Text {n} that is not in the window.", f"absent-{n}")
        for n in range(start, start + count)
    ]


class Provider:
    """Window 0 (s1): page 0 keeps one of twelve, page 1 drops all twelve.

    Window 1 (s2) gives one valid lesson. ``stop``, when given, is set during
    the page 1 call.
    """

    def __init__(self, stop=None):
        self.stop = stop
        self.extract_calls = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "targets" in payload:
            relations = [
                {"target_id": t["target_id"], "relation": "distinct"} for t in payload["targets"]
            ]
            return ProviderResponse(json.dumps({"version": 1, "relations": relations}), 40)
        ids = sorted({part["id"] for part in payload["sources"]})
        assert len(ids) == 1, "the fixture needs one source per window"
        continuation = payload["continuation"]
        self.extract_calls.append((ids[0], continuation))
        if ids == ["s1"]:
            if continuation == 0:
                found = [candidate("s1", FIRST, "short-replies")] + absent("s1", 11)
            else:
                found = absent("s1", 12, start=100)
                if self.stop is not None:
                    self.stop.set()
        else:
            found = [candidate("s2", SECOND, "go-tabs")]
        return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 50)


def worker(ledger, provider, stop=None, **settings):
    result = LearningWorker(
        ledger,
        provider,
        WorkerSettings(window_chars=WINDOW_CHARS, overlap_chars=0, **settings),
        LIMITS,
    )
    result.stop = stop
    return result


def job(ledger):
    row = ledger.conn.execute(
        "SELECT id,status,attempts,last_error,progress FROM learning_jobs"
    ).fetchone()
    return row["id"], row["status"], row["last_error"], json.loads(row["progress"])


def pages(ledger):
    return [
        (row["unit_index"], row["continuation"], row["status"], json.loads(row["candidates"]))
        for row in ledger.conn.execute(
            "SELECT unit_index,continuation,status,candidates FROM learning_extraction_pages "
            "ORDER BY unit_index,continuation"
        )
    ]


def statements(ledger):
    return sorted(n["statement"] for n in ledger.current_notes("demo", "scope"))


def test_an_exhausted_window_ends_only_that_window_and_the_job_finishes(ledger):
    capture_two_windows(ledger)
    provider = Provider()
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job(ledger)
    assert provider.extract_calls == [("s1", 0), ("s1", 1), ("s2", 0)]
    found = pages(ledger)
    assert [(u, c, s) for u, c, s, _ in found] == [
        (0, 0, "saturated"),
        (0, 1, "exhausted"),
        (1, 0, "processed"),
    ]
    # The exhausted page keeps no candidate. No dropped candidate is stored.
    assert found[1][3] == []
    assert statements(ledger) == sorted([FIRST, SECOND])
    stored = json.dumps(
        [row[0] for row in ledger.conn.execute("SELECT detail FROM learning_decisions")]
    )
    assert "not in the window" not in stored
    _, status, last_error, progress = job(ledger)
    assert status == "done" and last_error == ""
    assert progress["exhausted_windows"] == [0]
    assert ledger.status()["exhausted_windows"] == 1
    # Every call, also the one of the exhausted page, has a settled budget charge.
    settled = ledger.conn.execute(
        "SELECT count(*) FROM learning_budget WHERE status='settled'"
    ).fetchone()[0]
    assert settled == len(provider.extract_calls) + 1


def test_the_hard_decision_limit_still_ends_the_job(ledger):
    capture_two_windows(ledger)
    provider = Provider()
    # Window 0 keeps one candidate; window 1 adds a second, past a limit of one.
    result = worker(ledger, provider, max_decisions=1).run_once()
    assert result["status"] == "failed" and result["permanent"]
    _, status, last_error, progress = job(ledger)
    assert status == "exhausted"
    assert last_error == "CoverageExhausted: extraction receipt is incomplete"
    assert provider.extract_calls == [("s1", 0), ("s1", 1), ("s2", 0)]
    assert [s for _, _, s, _ in pages(ledger)] == ["saturated", "exhausted", "exhausted"]
    assert statements(ledger) == []


def stopped_after_page_one(ledger):
    """Run until the page 1 receipt of window 0 is saved, then stop."""
    capture_two_windows(ledger)
    stop = Event()
    provider = Provider(stop=stop)
    assert worker(ledger, provider, stop).run_once()["status"] == "deferred"
    ident, _, _, progress = job(ledger)
    return ident, provider, progress


def as_0_1_28(ledger, ident, progress, *, status, keep_page_one):
    """Write the state that 0.1.28 left: window 0 not finished, no new keys."""
    progress = dict(progress)
    progress.pop("exhausted_windows", None)
    progress["next_window"] = 0
    progress["next_continuation"] = 1
    if keep_page_one:
        ledger.conn.execute(
            "UPDATE learning_extraction_pages SET reason=? WHERE status='exhausted'",
            (OLD_REASON,),
        )
    else:
        ledger.conn.execute("DELETE FROM learning_extraction_pages WHERE status='exhausted'")
    error = "CoverageExhausted: extraction receipt is incomplete" if keep_page_one else ""
    ledger.conn.execute(
        "UPDATE learning_jobs SET status=?,attempts=?,last_error=?,next_attempt=0,progress=? "
        "WHERE id=?",
        (status, 1 if keep_page_one else 0, error, json.dumps(progress), ident),
    )


def test_an_explicit_retry_of_a_job_exhausted_by_0_1_28_finishes(ledger):
    ident, provider, started = stopped_after_page_one(ledger)
    as_0_1_28(ledger, ident, started, status="exhausted", keep_page_one=True)
    ledger.retry(ident)
    provider = Provider()
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job(ledger)
    # The old exhausted receipt is read again; window 0 makes no new call.
    assert provider.extract_calls == [("s2", 0)]
    assert statements(ledger) == sorted([FIRST, SECOND])
    _, status, _, progress = job(ledger)
    assert status == "done" and progress["exhausted_windows"] == [0]
    for key in ("fingerprint", "execution_fingerprint", "execution_configuration"):
        assert progress[key] == started[key]


def test_a_pending_job_with_0_1_28_progress_resumes_and_finishes(ledger):
    ident, provider, started = stopped_after_page_one(ledger)
    as_0_1_28(ledger, ident, started, status="pending", keep_page_one=False)
    provider = Provider()
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job(ledger)
    assert provider.extract_calls == [("s1", 1), ("s2", 0)]
    _, status, _, progress = job(ledger)
    assert status == "done" and progress["exhausted_windows"] == [0]
    for key in ("fingerprint", "execution_fingerprint", "execution_configuration"):
        assert progress[key] == started[key]


def test_an_old_exhausted_receipt_with_candidates_still_fails_closed(ledger):
    """The legacy reason with kept candidates is a repeat or a hard limit."""
    ident, provider, started = stopped_after_page_one(ledger)
    as_0_1_28(ledger, ident, started, status="exhausted", keep_page_one=True)
    ledger.conn.execute(
        "UPDATE learning_extraction_pages SET candidates=(SELECT candidates FROM "
        "learning_extraction_pages WHERE continuation=0) WHERE status='exhausted'"
    )
    ledger.retry(ident)
    result = worker(ledger, Provider()).run_once()
    assert result["status"] == "failed" and result["permanent"]
    assert job(ledger)[2] == "CoverageExhausted: extraction receipt is incomplete"
