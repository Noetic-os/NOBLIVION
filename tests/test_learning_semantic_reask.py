# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-240: one bad semantic answer no longer fails the whole job.

A semantic answer that parses but does not match its task is asked again a
bounded number of times. At the bound the one affected candidate is not
stored, and the job goes on. Two windows that disagree on one logical key drop
the decisions of that key, and the job goes on. No candidate whose comparison
was not validated is stored, every call is charged, and no candidate text goes
into an error or a count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from noblivion import db, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import (
    LearningWorker,
    ProviderResponse,
    WorkerSettings,
)
from test_learning_review_fixes import candidate, capture, refs

OLD = "Keep responses short."
FIRST = "Use tabs in Go files."
SECOND = "Run the linter before each commit."
FILLER = "\n".join(f"Filler line {i:03d} without a lesson." for i in range(12))
# One source per window, under every extraction contract (as in the NOBLIVION-244 tests).
SCALE = getattr(learning_worker, "WINDOW_SCALE", {}).get(learning_worker.EXTRACTION_CONTRACT, 1)
WINDOW_CHARS = -(-1024 // SCALE)
# Read from the module, so this file also loads on a release without the fix.
MAX_SEMANTIC_REASKS = getattr(learning_worker, "MAX_SEMANTIC_REASKS", 2)
LIMITS = replace(BudgetLimits(), max_calls=1000, max_seconds=100000, max_tokens=10**8)


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "reask.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def seed_note(ledger):
    """One managed note, so every new add gets a semantic comparison."""
    record = capture(ledger, "old", OLD)
    job = ledger.claim("seed")
    item = candidate(record, key="short-replies")
    item["evidence"] = refs(record)
    ledger.finish(job["id"], job["lease_token"], [item])


def two_lessons(ledger):
    record = capture(ledger, "new", f"{FIRST} {SECOND}")
    first = candidate(record, key="go-tabs", statement=FIRST)
    first["evidence"] = [{"source_id": "new", "quote": FIRST}]
    second = candidate(record, key="lint-first", statement=SECOND)
    second["evidence"] = [{"source_id": "new", "quote": SECOND}]
    return [first, second]


def good(task):
    return {
        "version": 1,
        "relations": [
            {"target_id": t["target_id"], "relation": "distinct"} for t in task["targets"]
        ],
    }


def bad(task, failure):
    answer = good(task)
    if failure == "invalid semantic target":
        answer["relations"][0]["target_id"] = "invented-target"
    elif failure == "incomplete semantic comparison":
        answer["relations"] = answer["relations"][:-1]
    elif failure == "invalid semantic relation":
        answer["relations"][0]["relation"] = "approved"
    return answer


class SemanticProvider:
    """Extraction returns ``candidates``. ``answer(task, prompt)`` gives the semantic reply."""

    def __init__(self, candidates, answer):
        self.candidates, self.answer = candidates, answer
        self.semantic = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" in payload:
            if payload.get("continuation"):
                return ProviderResponse('{"version":1,"candidates":[]}', 10)
            return ProviderResponse(json.dumps({"version": 1, "candidates": self.candidates}), 10)
        self.semantic.append((payload["candidate"], messages[0]["content"]))
        return ProviderResponse(json.dumps(self.answer(payload, messages[0]["content"])), 10)


def worker(ledger, provider, limits=LIMITS):
    return LearningWorker(ledger, provider, WorkerSettings(), limits)


def job_row(ledger):
    row = ledger.conn.execute(
        "SELECT id,status,last_error,progress FROM learning_jobs ORDER BY rowid DESC"
    ).fetchone()
    return row["id"], row["status"], row["last_error"], json.loads(row["progress"])


def statements(ledger):
    return sorted(n["statement"] for n in ledger.current_notes("demo", "scope"))


def charged(ledger):
    return ledger.conn.execute(
        "SELECT count(*) FROM learning_budget WHERE status='settled'"
    ).fetchone()[0]


@pytest.mark.parametrize(
    "failure",
    ["invalid semantic target", "incomplete semantic comparison", "invalid semantic relation"],
)
def test_one_bad_semantic_answer_is_asked_again_and_the_job_finishes(ledger, failure):
    seed_note(ledger)
    lessons = two_lessons(ledger)
    state = {"bad": 1}

    def answer(task, prompt):
        if task["candidate"] == FIRST and state["bad"]:
            state["bad"] -= 1
            return bad(task, failure)
        return good(task)

    provider = SemanticProvider(lessons, answer)
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job_row(ledger)
    assert statements(ledger) == sorted([OLD, FIRST, SECOND])
    asked = [prompt for statement, prompt in provider.semantic if statement == FIRST]
    # The re-ask is a varied request: the same request at temperature 0 repeats.
    assert len(asked) == 2 and asked[0] != asked[1] and failure in asked[1]
    _, _, _, progress = job_row(ledger)
    assert progress["semantic"]["reasks"] == {"0": {"failures": 1, "error": failure}}
    assert progress["semantic"].get("rejected", []) == []
    # Every call, also the re-ask, has a settled budget charge.
    assert charged(ledger) == 1 + len(provider.semantic)
    assert ledger.status()["semantic_reasks"] == 1


@pytest.mark.parametrize("failure", ["invalid semantic target", "incomplete semantic comparison"])
def test_at_the_bound_only_the_affected_candidate_is_not_stored(ledger, failure):
    seed_note(ledger)
    lessons = two_lessons(ledger)

    def answer(task, prompt):
        return bad(task, failure) if task["candidate"] == FIRST else good(task)

    provider = SemanticProvider(lessons, answer)
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job_row(ledger)
    # The candidate whose comparison was never validated is not stored in any form.
    assert statements(ledger) == sorted([OLD, SECOND])
    stored = ledger.conn.execute("SELECT detail FROM learning_decisions").fetchall()
    assert not any(FIRST in row[0] for row in stored)
    asked = [s for s, _ in provider.semantic if s == FIRST]
    assert len(asked) == 1 + MAX_SEMANTIC_REASKS
    _, status, last_error, progress = job_row(ledger)
    assert status == "done" and last_error == ""
    assert progress["semantic"]["rejected"] == [0]
    reasks = progress["semantic"]["reasks"]
    assert reasks == {"0": {"failures": 1 + MAX_SEMANTIC_REASKS, "error": failure}}
    assert FIRST not in json.dumps(reasks)
    assert charged(ledger) == 1 + len(provider.semantic)
    status_view = ledger.status()
    assert status_view["rejected_candidates"] == {"semantic comparison not validated": 1}
    assert status_view["semantic_reasks"] == 1 + MAX_SEMANTIC_REASKS
    # A finished job makes no more calls.
    calls = len(provider.semantic)
    assert worker(ledger, provider).run_once()["status"] == "idle"
    assert len(provider.semantic) == calls


def test_an_unsafe_semantic_answer_still_fails_the_job(ledger):
    """Only an answer that parses but misses its task is asked again."""
    seed_note(ledger)
    lessons = two_lessons(ledger)
    secret = "ghp_" + "Ab3" * 12

    def answer(task, prompt):
        reply = good(task)
        reply["relations"][0]["relation"] = secret
        return reply

    provider = SemanticProvider(lessons, answer)
    result = worker(ledger, provider).run_once()
    assert result["status"] == "failed" and result["permanent"]
    assert job_row(ledger)[2] == "ValidationError: unsafe semantic response"
    assert statements(ledger) == [OLD]


def record_of(ident, text):
    return {
        "source_id": ident,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "role": "user",
        "observed_at": "2026-01-02T12:00:00Z",
        "metadata": {},
    }


def test_windows_that_disagree_on_one_key_drop_only_that_key(ledger):
    """Two windows give one logical key two statements. Both are dropped."""
    seed_note(ledger)
    spaces = "Use spaces in Go files."
    ledger.capture_batch(
        {
            "batch_id": "batch-pair",
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": "reask-session",
            "records": [
                record_of("s1", f"{FIRST} {SECOND}\n{FILLER}"),
                record_of("s2", f"{spaces}\n{FILLER}"),
            ],
        }
    )

    def lesson(ident, key, text):
        item = candidate({"source_id": ident, "content": text}, key=key, statement=text)
        item["evidence"] = [{"source_id": ident, "quote": text}]
        return item

    by_window = {
        "s1": [lesson("s1", "go-indent", FIRST), lesson("s1", "lint-first", SECOND)],
        "s2": [lesson("s2", "go-indent", spaces)],
    }

    class PerWindow(SemanticProvider):
        def complete(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            if "sources" in payload:
                ids = {part["id"] for part in payload["sources"]}
                assert len(ids) == 1, "the fixture needs one source per window"
                self.candidates = by_window[ids.pop()]
            return super().complete(messages, **kwargs)

    provider = PerWindow([], lambda task, prompt: good(task))
    settings = WorkerSettings(window_chars=WINDOW_CHARS, overlap_chars=0)
    result = LearningWorker(ledger, provider, settings, LIMITS).run_once()
    assert result["status"] == "done", job_row(ledger)
    assert statements(ledger) == sorted([OLD, SECOND])
    _, _, _, progress = job_row(ledger)
    assert progress["inconsistent_decisions"] == 1
    assert ledger.status()["rejected_candidates"] == {"inconsistent window decisions": 1}
    assert "go-indent" not in json.dumps(ledger.status())


# The keys of the semantic stage in 0.1.28, before this fix.
OLD_STAGE_KEYS = {"notes", "snapshot", "next_task", "decisions"}


def paused_in_the_semantic_stage(ledger):
    """Extraction done, the semantic call waits on the budget, as a 0.1.28 job."""
    seed_note(ledger)
    lessons = two_lessons(ledger)
    provider = SemanticProvider(lessons, lambda task, prompt: good(task))
    paused = worker(ledger, provider, replace(LIMITS, max_calls=1)).run_once()
    assert paused["status"] == "deferred" and provider.semantic == []
    ident, status, _, progress = job_row(ledger)
    assert status == "pending"
    progress["semantic"] = {k: progress["semantic"][k] for k in OLD_STAGE_KEYS}
    ledger.conn.execute(
        "UPDATE learning_jobs SET progress=?,next_attempt=0 WHERE id=?",
        (json.dumps(progress), ident),
    )
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    return ident, lessons, progress


def bad_once(failure):
    state = {"bad": 1}

    def answer(task, prompt):
        if task["candidate"] == FIRST and state["bad"]:
            state["bad"] -= 1
            return bad(task, failure)
        return good(task)

    return answer


def test_a_pending_job_with_0_1_28_progress_resumes_and_is_asked_again(ledger):
    ident, lessons, started = paused_in_the_semantic_stage(ledger)
    provider = SemanticProvider(lessons, bad_once("invalid semantic target"))
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job_row(ledger)
    assert statements(ledger) == sorted([OLD, FIRST, SECOND])
    _, _, _, progress = job_row(ledger)
    for key in ("fingerprint", "execution_fingerprint", "execution_configuration"):
        assert progress[key] == started[key]
    assert progress["semantic"]["reasks"] == {
        "0": {"failures": 1, "error": "invalid semantic target"}
    }


@pytest.mark.parametrize("format_retry_used", [False, True])
def test_an_explicit_retry_of_a_job_exhausted_on_a_semantic_answer_finishes(
    ledger, format_retry_used
):
    ident, lessons, started = paused_in_the_semantic_stage(ledger)
    progress = dict(started)
    if format_retry_used:
        # 0.1.28 had used the one format retry of this step before it failed.
        progress["format_repair"] = {"step": "semantic:0", "calls": 1}
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='exhausted',attempts=1,"
        "last_error='ValidationError: invalid semantic target',progress=? WHERE id=?",
        (json.dumps(progress), ident),
    )
    ledger.retry(ident)
    # Temperature 0: the first answer after the retry repeats the old failure.
    provider = SemanticProvider(lessons, bad_once("invalid semantic target"))
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job_row(ledger)
    assert statements(ledger) == sorted([OLD, FIRST, SECOND])
    _, status, _, progress = job_row(ledger)
    assert status == "done"
    for key in ("fingerprint", "execution_fingerprint", "execution_configuration"):
        assert progress[key] == started[key]


def test_a_budget_stop_between_a_bad_answer_and_its_re_ask_resumes_with_the_re_ask(ledger):
    seed_note(ledger)
    lessons = two_lessons(ledger)
    failure = "invalid semantic target"

    def answer(task, prompt):
        return bad(task, failure) if task["candidate"] == FIRST else good(task)

    provider = SemanticProvider(lessons, answer)
    # One extraction call and one semantic call fit; the re-ask waits on the budget.
    paused = worker(ledger, provider, replace(LIMITS, max_calls=2)).run_once()
    assert paused["status"] == "deferred", job_row(ledger)
    _, _, _, progress = job_row(ledger)
    assert progress["semantic"]["reasks"] == {"0": {"failures": 1, "error": failure}}
    assert len(provider.semantic) == 1
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done", job_row(ledger)
    asked = [prompt for statement, prompt in provider.semantic if statement == FIRST]
    # The resumed call is the re-ask, and the bound holds across both runs.
    assert failure in asked[1]
    assert len(asked) == 1 + MAX_SEMANTIC_REASKS
    assert statements(ledger) == sorted([OLD, SECOND])
