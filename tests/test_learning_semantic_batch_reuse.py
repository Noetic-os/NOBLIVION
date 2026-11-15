"""Contract 2 semantic batches keep the NOBLIVION-250 and NOBLIVION-240 rules.

A peer commit mid-stage keeps the finished batch comparisons, and a batch
answer that parses but does not match the batch is asked again a bounded
number of times. Past the bound the batch's candidates are not stored, and the
job goes on (NOBLIVION-241).
"""

from __future__ import annotations

import functools
import json

import pytest

from noblivion import db, learning_worker
from noblivion.learning import LearningLedger
from noblivion.learning_worker import ProviderResponse
from test_learning_semantic_reuse import (
    candidate,
    capture,
    progress,
    resume,
    seed,
    worker,
)

TWO = "Prefer tabs over spaces. Run the tests before each commit."
N1, N2 = "Keep responses short.", "Use green terminal text."
C0, C1 = "Prefer tabs over spaces.", "Run the tests before each commit."
N3 = "Write commit messages in English."


class BatchProvider:
    """Extraction by source marker; each batch call logs, per item, the target statements."""

    def __init__(self, extractions, bad_answers=0):
        self.extractions = extractions
        self.bad_answers = bad_answers
        self.calls = []
        self.prompts = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" in payload:
            text = json.dumps(payload["sources"])
            found = [c for marker, cs in self.extractions.items() if marker in text for c in cs]
            return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 100)
        assert "candidates" in payload, "contract 2 sends semantic batches"
        statements = {t["target_id"]: t["statement"] for t in payload["targets"]}
        self.calls.append(
            [
                (item["candidate"], {statements[a] for a in item["compare"]})
                for item in payload["candidates"]
            ]
        )
        self.prompts.append(messages[0]["content"])
        results = [
            {
                "item_id": item["item_id"],
                "relations": [{"target_id": a, "relation": "distinct"} for a in item["compare"]],
            }
            for item in payload["candidates"]
        ]
        if self.bad_answers:
            self.bad_answers -= 1
            results[0]["relations"].pop()  # parses, but one pair has no relation
        return ProviderResponse(json.dumps({"version": 2, "results": results}), 100)


@pytest.fixture(autouse=True)
def contract_two(monkeypatch):
    monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", 2)


@pytest.fixture
def one_task_per_batch(monkeypatch):
    """Each batch holds one task, so a budget stop can fall between batches."""
    monkeypatch.setattr(
        learning_worker,
        "semantic_batches",
        functools.partial(learning_worker.semantic_batches, max_items=1),
    )


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "batch.db", create=True)
    yield LearningLedger(conn)
    conn.close()


@pytest.fixture
def setup(ledger):
    """Two notes, then job B with two add candidates in the same root."""
    n1 = seed(ledger, "s1", N1, "short")["note_id"]
    n2 = seed(ledger, "s2", N2, "green")["note_id"]
    b = capture(ledger, "b", TWO, session="job-b")
    cands = [candidate(b, "tabs", C0), candidate(b, "tests", C1)]
    job_b = ledger.conn.execute("SELECT id FROM learning_jobs WHERE session_id='job-b'").fetchone()[
        0
    ]
    return {"n1": n1, "n2": n2, "candidates": cands, "job": job_b}


def pause_after_first_batch(ledger, setup, provider):
    first = worker(ledger, provider, max_calls=2).run_once()
    assert first == {"status": "deferred", "job_id": setup["job"], "error": "BudgetExceeded"}
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    return progress(ledger, setup["job"])


def test_a_peer_commit_mid_stage_keeps_the_finished_batch(ledger, setup, one_task_per_batch):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]})
    state = pause_after_first_batch(ledger, setup, provider)
    assert provider.calls == [[(C0, {N1, N2})]]
    assert state["semantic"]["next_batch"] == 1 and state["semantic"]["next_task"] == 1
    # Job A in the same root commits a new note while job B waits mid-stage.
    a = capture(ledger, "a", N3, session="job-a")
    provider.extractions["commit messages"] = [candidate(a, "english")]
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=1e12 WHERE id=?", (setup["job"],))
    job_a = worker(ledger, provider, max_calls=50).run_once()
    assert job_a["status"] == "done" and job_a["results"][0]["action"] == "add"
    provider.calls.clear()
    job_b = resume(ledger, provider, setup["job"])
    assert job_b["status"] == "done"
    assert [r["action"] for r in job_b["results"]] == ["add", "add"]
    # Candidate 0 needs only the new note. Candidate 1 had no comparison yet.
    assert provider.calls == [[(C0, {N3})], [(C1, {N1, N2, N3, C0})]]
    state = progress(ledger, setup["job"])
    assert state["semantic_restarts"] == 1 and state["semantic_reused"] == 2
    assert len(ledger.current_notes("demo", "scope")) == 5


def test_one_bad_batch_answer_is_asked_again_and_the_job_finishes(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=1)
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert len(provider.calls) == 2
    assert "was rejected" not in provider.prompts[0]
    assert "(try 2)" in provider.prompts[1]
    assert progress(ledger, setup["job"])["semantic"].get("rejected", []) == []
    assert ledger.status()["semantic_reasks"] == 1


def test_a_budget_stop_after_a_bad_batch_answer_resumes_with_the_re_ask(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=1)
    pause_after_first_batch(ledger, setup, provider)
    state = progress(ledger, setup["job"])
    assert state["semantic"]["batch_reasks"]["0"]["failures"] == 1
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert len(provider.calls) == 2 and "(try 2)" in provider.prompts[1]


def test_at_the_bound_the_batch_candidates_are_not_stored_and_the_job_ends(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=99)
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert result["results"] == []
    # The first answer and MAX_SEMANTIC_REASKS re-asks, then no more calls.
    assert len(provider.calls) == 1 + learning_worker.MAX_SEMANTIC_REASKS
    state = progress(ledger, setup["job"])
    assert sorted(state["semantic"]["rejected"]) == [0, 1]
    assert len(ledger.current_notes("demo", "scope")) == 2
    report = ledger.status()
    assert report["rejected_candidates"]["semantic comparison not validated"] == 2


def test_an_unsafe_batch_answer_still_fails_the_job(ledger, setup, monkeypatch):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]})
    original = provider.complete

    def unsafe(messages, **kwargs):
        response = original(messages, **kwargs)
        if "candidates" in json.loads(messages[-1]["content"]):
            return ProviderResponse("x" * 70000, 100)
        return response

    monkeypatch.setattr(provider, "complete", unsafe)
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] in {"failed", "deferred"}
    assert len(provider.calls) == 1
