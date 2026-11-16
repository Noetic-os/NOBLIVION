# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract 2 semantic batches keep the NOBLIVION-250 and NOBLIVION-240 rules.

A peer commit mid-stage keeps the finished batch comparisons, and a batch
answer that parses but does not match the batch is asked again a bounded
number of times. Past the bound each task is asked alone, so one bad item
drops only its own candidate, and the job goes on (NOBLIVION-241).
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

    def __init__(self, extractions, bad_answers=0, bad_single=()):
        self.extractions = extractions
        self.bad_answers = bad_answers
        self.bad_single = set(bad_single)
        self.calls = []
        self.single = []
        self.prompts = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" in payload:
            text = json.dumps(payload["sources"])
            found = [c for marker, cs in self.extractions.items() if marker in text for c in cs]
            return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 100)
        if "candidate" in payload:
            # A contract 1 request: the fallback after a rejected batch.
            self.single.append(payload["candidate"])
            relations = [
                {"target_id": t["target_id"], "relation": "distinct"} for t in payload["targets"]
            ]
            if payload["candidate"] in self.bad_single:
                relations.pop()
            return ProviderResponse(json.dumps({"version": 1, "relations": relations}), 100)
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


def test_past_the_batch_bound_each_task_is_asked_alone(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=99)
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    # The first answer and MAX_SEMANTIC_REASKS re-asks, then one call per task.
    assert len(provider.calls) == 1 + learning_worker.MAX_SEMANTIC_REASKS
    assert provider.single == [C0, C1]
    state = progress(ledger, setup["job"])
    assert state["semantic"].get("rejected", []) == []
    assert len(ledger.current_notes("demo", "scope")) == 4


def test_one_item_that_stays_bad_drops_only_its_own_candidate(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=99, bad_single={C0})
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add"]
    keys = sorted(n["logical_key"] for n in ledger.current_notes("demo", "scope"))
    assert keys == ["green", "short", "tests"]
    # C0 gets its own bounded re-asks; C1 needs one call.
    assert provider.single == [C0] * (1 + learning_worker.MAX_SEMANTIC_REASKS) + [C1]
    state = progress(ledger, setup["job"])
    assert state["semantic"]["rejected"] == [0]
    assert len(ledger.current_notes("demo", "scope")) == 3
    report = ledger.status()
    assert report["rejected_candidates"]["semantic comparison not validated"] == 1


def test_a_batch_sends_only_the_tasks_whose_candidate_is_still_an_add(ledger, setup, monkeypatch):
    """Candidate 0 is blocked by its first task, so the next batch drops its other task."""
    real_targets = learning_worker.semantic_targets
    real_batches = learning_worker.semantic_batches

    def one_task_per_target(decisions, notes, **kwargs):
        tasks = real_targets(decisions, notes, **kwargs)
        return [{**task, "targets": [t]} for task in tasks for t in task["targets"]]

    def first_task_alone(tasks, **kwargs):
        if "max_items" in kwargs:  # the subset of one batch
            return real_batches(tasks, **kwargs)
        return real_batches(tasks[:1], **kwargs) + real_batches(tasks[1:], **kwargs)

    monkeypatch.setattr(learning_worker, "semantic_targets", one_task_per_target)
    monkeypatch.setattr(learning_worker, "semantic_batches", first_task_alone)
    provider = BatchProvider({"Prefer tabs": setup["candidates"]})
    original = provider.complete

    def first_is_equivalent(messages, **kwargs):
        response = original(messages, **kwargs)
        if len(provider.calls) == 1 and "candidates" in json.loads(messages[-1]["content"]):
            body = json.loads(response.content)
            body["results"][0]["relations"][0]["relation"] = "equivalent"
            return ProviderResponse(json.dumps(body), 100)
        return response

    monkeypatch.setattr(provider, "complete", first_is_equivalent)
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["conflict", "add"]
    assert len(provider.calls) == 2
    assert [c for c, _ in provider.calls[0]] == [C0]
    # The second batch held the other task of C0 and the three tasks of C1.
    # C0 is blocked already, so only the tasks of C1 are sent.
    assert [c for c, _ in provider.calls[1]] == [C1, C1, C1]
    assert set().union(*(t for _, t in provider.calls[1])) == {N1, N2, C0}


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
    assert result["status"] == "failed" and result["permanent"] is True
    assert result["error"] == "ValidationError"
    assert len(provider.calls) == 1


def test_a_peer_commit_after_a_batch_of_several_tasks_keeps_all_of_them(ledger, setup, monkeypatch):
    """next_task counts the tasks of the finished batches, not the batches."""
    real_targets = learning_worker.semantic_targets

    def one_task_per_target(decisions, notes, **kwargs):
        tasks = real_targets(decisions, notes, **kwargs)
        return [{**task, "targets": [t]} for task in tasks for t in task["targets"]]

    monkeypatch.setattr(learning_worker, "semantic_targets", one_task_per_target)
    monkeypatch.setattr(
        learning_worker,
        "semantic_batches",
        functools.partial(learning_worker.semantic_batches, max_items=2),
    )
    provider = BatchProvider({"Prefer tabs": setup["candidates"]})
    state = pause_after_first_batch(ledger, setup, provider)
    # The first batch held both tasks of C0 (one per note).
    assert [c for c, _ in provider.calls[0]] == [C0, C0]
    assert state["semantic"]["next_batch"] == 1 and state["semantic"]["next_task"] == 2
    a = capture(ledger, "a", N3, session="job-a")
    provider.extractions["commit messages"] = [candidate(a, "english")]
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=1e12 WHERE id=?", (setup["job"],))
    assert worker(ledger, provider, max_calls=50).run_once()["status"] == "done"
    provider.calls.clear()
    job_b = resume(ledger, provider, setup["job"])
    assert job_b["status"] == "done"
    pairs = [pair for call in provider.calls for pair in call]
    # C0 keeps both finished comparisons and needs only the new note.
    assert set().union(*(t for c, t in pairs if c == C0)) == {N3}
    assert set().union(*(t for c, t in pairs if c == C1)) == {N1, N2, N3, C0}
    assert progress(ledger, setup["job"])["semantic_reused"] == 2


def test_a_stop_during_the_fallback_does_not_ask_a_finished_task_again(ledger, setup):
    provider = BatchProvider({"Prefer tabs": setup["candidates"]}, bad_answers=99)
    # One extraction, the batch answer and its re-asks, then one task alone.
    calls = 1 + 1 + learning_worker.MAX_SEMANTIC_REASKS + 1
    first = worker(ledger, provider, max_calls=calls).run_once()
    assert first == {"status": "deferred", "job_id": setup["job"], "error": "BudgetExceeded"}
    assert progress(ledger, setup["job"])["semantic"]["fallback_done"] == [0]
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert provider.single == [C0, C1]
    assert len(provider.calls) == 1 + learning_worker.MAX_SEMANTIC_REASKS
