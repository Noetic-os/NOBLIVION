# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-241: a contract 1 job with no partial work freezes again under contract 2.

A job with any partial work keeps its frozen contract, and a job whose freeze
no longer matches the configuration still fails visibly (NOBLIVION-236).
"""

from __future__ import annotations

import json

import pytest

from noblivion import db, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import ProviderError, ProviderResponse, WorkerSettings
from test_learning_fewer_calls import Oracle, many_candidates, progress, run
from test_learning_review_fixes import capture

LONG = "Routine context. " * 1100  # three contract 1 windows, one of contract 2


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def reopen(ledger):
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")


def freeze_under_contract_1(ledger, monkeypatch, provider, **limits):
    """Run the job once as a release with contract 1 would, then upgrade."""
    monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", 1)
    first = run(ledger, provider, **limits)
    frozen = progress(ledger)
    assert frozen["execution_configuration"]["contract"] == 1
    monkeypatch.undo()
    assert learning_worker.EXTRACTION_CONTRACT == 2
    reopen(ledger)
    return first, frozen


def pages(ledger):
    return ledger.conn.execute("SELECT count(*) FROM learning_extraction_pages").fetchone()[0]


def test_job_frozen_with_no_work_freezes_again_under_contract_2(ledger, monkeypatch):
    adds = many_candidates(ledger, 11, prefix=LONG)
    provider = Oracle(adds)
    # No call fits the budget: the job froze, but no window is done.
    first, frozen = freeze_under_contract_1(ledger, monkeypatch, provider, max_calls=0)
    assert first["error"] == "BudgetExceeded" and provider.calls == []
    assert frozen["next_window"] == 0 and pages(ledger) == 0
    result = run(ledger, provider)
    assert result["status"] == "done"
    after = progress(ledger)
    assert after["refrozen_from"] == 1
    assert after["execution_configuration"]["contract"] == 2
    assert after["call_configuration"]["contract"] == 2
    assert after["execution_fingerprint"] != frozen["execution_fingerprint"]
    # Contract 2 windows (three times larger) and batched semantic calls.
    assert provider.offsets == [0] and "semantic" not in provider.calls
    assert provider.calls.count("batch") == 2


def test_job_with_a_failed_call_and_no_page_freezes_again(ledger, monkeypatch):
    capture(ledger, "long", LONG)

    class FailOnce(Oracle):
        def complete(self, messages, **kwargs):
            if not self.calls:
                self.calls.append("failed")
                raise ProviderError("transport")
            return super().complete(messages, **kwargs)

    provider = FailOnce()
    first, frozen = freeze_under_contract_1(ledger, monkeypatch, provider)
    assert first["status"] == "failed" and first["permanent"] is False
    assert "call_configuration" in frozen and pages(ledger) == 0
    assert run(ledger, provider)["status"] == "done"
    assert progress(ledger)["execution_configuration"]["contract"] == 2
    assert provider.offsets == [0]


def test_job_with_a_completed_window_keeps_contract_1(ledger, monkeypatch):
    capture(ledger, "long", LONG)
    provider = Oracle()
    first, frozen = freeze_under_contract_1(ledger, monkeypatch, provider, max_calls=1)
    assert first["error"] == "BudgetExceeded" and pages(ledger) == 1
    assert run(ledger, provider)["status"] == "done"
    after = progress(ledger)
    assert "refrozen_from" not in after
    assert after["execution_fingerprint"] == frozen["execution_fingerprint"]
    assert after["execution_configuration"] == frozen["execution_configuration"]
    assert provider.offsets[:2] == [0, 4000]


def test_page_receipt_alone_keeps_contract_1(ledger, monkeypatch):
    """A page counts as work even when next_window still says 0."""
    capture(ledger, "long", LONG)
    provider = Oracle()
    freeze_under_contract_1(ledger, monkeypatch, provider, max_calls=1)
    state = progress(ledger)
    state["next_window"] = 0
    ledger.conn.execute("UPDATE learning_jobs SET progress=?", (json.dumps(state),))
    run(ledger, provider)
    after = progress(ledger)
    assert "refrozen_from" not in after
    assert after["execution_configuration"]["contract"] == 1


@pytest.mark.parametrize(
    "marker",
    [
        {"next_continuation": 1},
        {"decisions": [{"action": "add"}]},
        {"format_repair": {"step": "window:0:0", "calls": 0}},
        {"semantic": {"next_batch": 0}},
        {"dependency_deferred": True},
        {"candidate_mismatches": []},
    ],
)
def test_any_partial_work_marker_keeps_contract_1(ledger, monkeypatch, marker):
    capture(ledger, "long", LONG)
    freeze_under_contract_1(ledger, monkeypatch, Oracle(), max_calls=0)
    state = {**progress(ledger), **marker}
    worker = learning_worker.LearningWorker(ledger, Oracle(), WorkerSettings(), BudgetLimits())
    (job_id,) = ledger.conn.execute("SELECT id FROM learning_jobs").fetchone()
    sources = ledger._job_sources(job_id)
    assert worker._may_refreeze({"id": job_id}, state, sources) is False
    assert worker._may_refreeze({"id": job_id}, progress(ledger), sources) is True


def test_output_limit_hold_keeps_contract_1(ledger, monkeypatch):
    capture(ledger, "long", LONG)

    class Length(Oracle):
        def complete(self, messages, **kwargs):
            self.calls.append("length")
            raise ProviderError("length", tokens=10)

    provider = Length()
    first, frozen = freeze_under_contract_1(ledger, monkeypatch, provider)
    assert first["error"] == "Provider:length" and frozen["output_limit_hold"]
    result = run(ledger, provider)
    # The hold stays under contract 1 and repeats no call.
    assert result["error"] == "Provider:length" and provider.calls == ["length"]
    after = progress(ledger)
    assert "refrozen_from" not in after
    assert after["execution_configuration"] == frozen["execution_configuration"]


def test_changed_configuration_with_no_work_still_fails_visibly(ledger, monkeypatch):
    capture(ledger, "long", LONG)
    freeze_under_contract_1(ledger, monkeypatch, Oracle(), max_calls=0)
    budget = BudgetLimits(max_calls=1000, max_seconds=100000, max_tokens=10**8)
    worker = learning_worker.LearningWorker(
        ledger, Oracle(), WorkerSettings(window_chars=5000), budget
    )
    result = worker.run_once()
    assert result["status"] == "failed" and result["permanent"] is True
    (error,) = ledger.conn.execute("SELECT last_error FROM learning_jobs").fetchone()
    assert error == "ValidationError: window configuration changed during a job"


def test_unknown_contract_with_no_work_still_fails_visibly(ledger, monkeypatch):
    capture(ledger, "long", LONG)
    freeze_under_contract_1(ledger, monkeypatch, Oracle(), max_calls=0)
    state = progress(ledger)
    state["execution_configuration"]["contract"] = 9
    ledger.conn.execute("UPDATE learning_jobs SET progress=?", (json.dumps(state),))
    result = run(ledger, Oracle())
    assert result["status"] == "failed" and result["permanent"] is True


def test_new_job_is_not_marked_as_refrozen(ledger):
    capture(ledger, "short", "Use short sentences in every project.")
    provider = Oracle()
    provider.complete = lambda messages, **kwargs: ProviderResponse(
        '{"version":1,"candidates":[]}', 50
    )
    assert run(ledger, provider)["status"] == "done"
    assert "refrozen_from" not in progress(ledger)
