# SPDX-License-Identifier: AGPL-3.0-or-later
"""A window that ends on the output token cap splits and the job ends (NOBLIVION-245)."""

import hashlib
import json
from dataclasses import replace

import pytest

import noblivion.learning_worker as module
from noblivion import db
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_pages import LENGTH_REJECTED
from noblivion.learning_worker import (
    LENGTH_SPLIT_DEPTH,
    MAX_RETURNED_DEFERRALS,
    PIECE_STRIDE,
    LearningWorker,
    ProviderError,
    ProviderResponse,
    WorkerSettings,
)
from test_learning_context_repair import old_note, ready, semantic_length
from test_learning_review_fixes import ScriptedProvider, candidate, capture, worker

EMPTY = '{"version":1,"candidates":[]}'
LIMITS = replace(BudgetLimits(), max_calls=1000, max_seconds=100000, max_tokens=10**8)
# The progress keys of a job deferred on length by the release before this fix,
# dumped from that release with the same job (plain length, length on the retry).
# A cap that the deepest pieces of a window fit. Windows grow with the contract
# (NOBLIVION-241), and the split depth stays the same.
CAP = 3000 * module.WINDOW_SCALE[module.EXTRACTION_CONTRACT]
OLD_LENGTH_KEYS = {
    "call_configuration",
    "context_exclusions",
    "decisions",
    "execution_configuration",
    "execution_fingerprint",
    "existing_notes",
    "fingerprint",
    "next_window",
    "output_limit_hold",
    "provider_metrics",
}


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "length.db", create=True)
    yield LearningLedger(conn)
    conn.close()


class CappedProvider(ScriptedProvider):
    """Reply with ``length`` when the window holds more than ``cap`` characters."""

    def __init__(self, ledger, cap, first=None):
        super().__init__(ledger, [])
        self.cap, self.first, self.sizes, self.results = cap, first, [], []

    def complete(self, messages, **kwargs):
        window = json.loads(messages[-1]["content"])["sources"]
        size = sum(len(part["content"]) for part in window)
        self.sizes.append(size)
        if self.first is not None:
            reply, self.first = self.first, None
            self.results.append("format")
            return ProviderResponse(reply, 10)
        if size > self.cap:
            self.results.append("length")
            raise ProviderError("length", tokens=2048)
        self.results.append("success")
        return ProviderResponse(EMPTY, 10)


def job_row(ledger):
    row = ledger.conn.execute(
        "SELECT status,attempts,progress FROM learning_jobs ORDER BY rowid DESC"
    ).fetchone()
    return row["status"], row["attempts"], json.loads(row["progress"])


def receipts(ledger):
    return [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT unit_index,continuation,fingerprint,spans,candidates,status,reason "
            "FROM learning_extraction_pages ORDER BY unit_index,continuation"
        )
    ]


def test_length_on_large_windows_splits_until_they_fit_and_the_job_ends(ledger):
    capture(ledger, "new", "Routine context. " * 1100)
    provider = CappedProvider(ledger, cap=CAP)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert run.run_once()["status"] == "done"
    status, _, progress = job_row(ledger)
    assert status == "done"
    splits = sum(len(slots) for slots in progress["length_splits"].values())
    # One length call per split, then one success call per final piece.
    assert provider.results.count("length") == splits > 0
    assert provider.results.count("success") == len(receipts(ledger))
    assert all(
        size <= CAP
        for size, result in zip(provider.sizes, provider.results, strict=True)
        if result == "success"
    )
    assert all(r["status"] == "no_lesson" for r in receipts(ledger))
    calls = len(provider.results)
    ready(ledger)
    assert run.run_once()["status"] == "idle" and len(provider.results) == calls


def test_window_that_always_ends_on_length_makes_bounded_calls_and_is_rejected(ledger):
    capture(ledger, "new", "x" * 16000)
    provider = CappedProvider(ledger, cap=0)
    run = LearningWorker(ledger, provider, WorkerSettings(window_chars=40000), LIMITS)
    assert run.run_once()["status"] == "done"
    # A full binary tree of depth LENGTH_SPLIT_DEPTH: 15 calls, 8 floor pieces.
    assert provider.results == ["length"] * (2 ** (LENGTH_SPLIT_DEPTH + 1) - 1)
    rows = receipts(ledger)
    assert len(rows) == 2**LENGTH_SPLIT_DEPTH
    assert {(r["status"], r["reason"], r["candidates"]) for r in rows} == {
        ("rejected", LENGTH_REJECTED, "[]")
    }
    # The floor pieces cover every source character.
    covered = set()
    for row in rows:
        for span in json.loads(row["spans"]):
            covered.update(range(span["start"], span["end"]))
    assert covered == set(range(16000))
    _, _, progress = job_row(ledger)
    assert progress["rejected_windows"] == [0] and "output_limit_hold" not in progress
    assert ledger.status()["rejected_windows"] == 1
    assert not ledger.current_notes("demo", "scope")


def test_cut_off_reply_at_the_floor_keeps_no_candidate(ledger):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")

    def complete(messages, **kwargs):
        provider.calls.append("extract")
        raise ProviderError("length", tokens=2048)

    provider.complete = complete
    assert worker(ledger, provider).run_once()["status"] == "done"
    assert provider.calls == ["extract"]
    (row,) = receipts(ledger)
    assert (row["status"], row["reason"], row["candidates"]) == ("rejected", LENGTH_REJECTED, "[]")
    assert not ledger.current_notes("demo", "scope")


def test_explicit_retry_keeps_a_length_rejected_piece_and_does_not_call_it_again(ledger):
    capture(ledger, "new", "w" * 3000)
    sent = []
    state = {"stop": True}

    def complete(messages, **kwargs):
        window = json.loads(messages[-1]["content"])["sources"]
        sent.append(window[0]["offset"])
        if window[0]["offset"] == 0:
            # The whole window, then the left half at the floor.
            raise ProviderError("length", tokens=2048)
        if state.pop("stop", False):
            raise RuntimeError("provider down")
        return ProviderResponse(EMPTY, 10)

    provider = ScriptedProvider(ledger, [])
    provider.complete = complete
    run = LearningWorker(ledger, provider, WorkerSettings(max_attempts=1), LIMITS)
    assert run.run_once()["status"] == "failed"
    status, _, progress = job_row(ledger)
    assert status == "exhausted" and progress["length_splits"] == {"0": [0]}
    assert [(r["continuation"], r["reason"]) for r in receipts(ledger)] == [(64, LENGTH_REJECTED)]
    assert len(sent) == 3
    job_id = ledger.conn.execute("SELECT id FROM learning_jobs ORDER BY rowid DESC").fetchone()[0]
    ledger.retry(job_id)
    assert run.run_once()["status"] == "done"
    # Only the right half is sent again. The rejected left half keeps its receipt.
    assert len(sent) == 4 and sent[3] > 0
    assert [(r["continuation"], r["status"]) for r in receipts(ledger)] == [
        (64, "rejected"),
        (128, "no_lesson"),
    ]


@pytest.mark.parametrize("format_retry", [False, True])
def test_job_deferred_on_length_by_the_old_release_resumes_and_ends(
    ledger, monkeypatch, format_retry
):
    capture(ledger, "new", "y" * 5000)
    provider = CappedProvider(ledger, cap=3000, first="not JSON" if format_retry else None)

    # The old release let length leave the window. Make the split raise again.
    def old_release(piece, slot):
        raise ProviderError("length")

    monkeypatch.setattr(module, "splittable", old_release)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert run.run_once()["status"] == "deferred"
    status, _, progress = job_row(ledger)
    progress.pop("returned_deferrals")
    ledger.conn.execute("UPDATE learning_jobs SET attempts=0,progress=?", (json.dumps(progress),))
    assert set(progress) == OLD_LENGTH_KEYS | ({"format_repair"} if format_retry else set())
    if format_retry:
        assert progress["format_repair"] == {"calls": 1, "step": "extract:0:0"}
    frozen = (progress["fingerprint"], progress["execution_fingerprint"])
    old_calls = len(provider.results)
    assert provider.results[-1] == "length" and provider.sizes[-1] == 5000

    monkeypatch.undo()
    ready(ledger)
    upgraded = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert upgraded.run_once()["status"] == "done"
    # The held request is not sent again: only the two halves are new calls.
    assert provider.results[old_calls:] == ["success", "success"]
    assert all(size < 5000 for size in provider.sizes[old_calls:])
    _, _, progress = job_row(ledger)
    assert (progress["fingerprint"], progress["execution_fingerprint"]) == frozen
    assert {row["fingerprint"] for row in receipts(ledger)} == {frozen[0]}
    assert [row["continuation"] for row in receipts(ledger)] == [64, 128]


def test_interrupted_split_resumes_without_repeating_finished_pieces(ledger):
    capture(ledger, "new", "z" * 5000)
    provider = CappedProvider(ledger, cap=3000)
    first = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    original = provider.complete

    def stop_after_left_half(messages, **kwargs):
        if provider.results.count("success") == 1:
            raise RuntimeError("worker host stopped")
        return original(messages, **kwargs)

    provider.complete = stop_after_left_half
    assert first.run_once()["status"] == "failed"
    assert provider.results == ["length", "success"]
    _, _, before = job_row(ledger)
    provider.complete = original
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    second = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert second.run_once()["status"] == "done"
    assert provider.results == ["length", "success", "success"]
    _, _, after = job_row(ledger)
    assert after["execution_fingerprint"] == before["execution_fingerprint"]
    assert after["length_splits"] == {"0": [0]}


def dependency_job(ledger):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    note = old_note()
    note["statement"] = "The cobalt job reads GH_TOKEN=***REDACTED*** first."
    ledger.reconciliation_context = lambda *a, **k: [old_note(), note]
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    return provider, "ContextDependencyDeferred"


def length_job(ledger):
    def semantic(*args, **kwargs):
        raise ProviderError("length")

    provider, _ = semantic_length(ledger, semantic)
    return provider, "Provider:length"


@pytest.mark.parametrize("make", [dependency_job, length_job])
def test_deferrals_that_give_the_attempt_back_are_capped(ledger, make):
    provider, error = make(ledger)
    settings = WorkerSettings()
    run = LearningWorker(ledger, provider, settings, LIMITS)
    results = []
    for _ in range(MAX_RETURNED_DEFERRALS + settings.max_attempts + 2):
        ready(ledger)
        results.append(run.run_once())
    statuses = [r["status"] for r in results]
    assert (
        statuses
        == ["deferred"] * MAX_RETURNED_DEFERRALS + ["failed"] * settings.max_attempts + ["idle"] * 2
    )
    assert all(r["error"] == error for r in results if r["status"] != "idle")
    status, attempts, progress = job_row(ledger)
    assert status == "exhausted" and attempts == settings.max_attempts
    assert progress["returned_deferrals"] == MAX_RETURNED_DEFERRALS + settings.max_attempts


def test_explicit_retry_renews_the_deferral_count(ledger):
    provider, _ = dependency_job(ledger)
    run = LearningWorker(ledger, provider, WorkerSettings(max_attempts=1), LIMITS)
    for _ in range(MAX_RETURNED_DEFERRALS + 1):
        ready(ledger)
        run.run_once()
    job_id = ledger.conn.execute("SELECT id FROM learning_jobs ORDER BY rowid DESC").fetchone()[0]
    assert job_row(ledger)[0] == "exhausted"
    ledger.retry(job_id)
    assert "returned_deferrals" not in job_row(ledger)[2]
    assert run.run_once()["status"] == "deferred"


FACT = "The indigo service uses port 8443."


def fact_candidate(record, key="indigo-port", quote=FACT):
    return candidate(
        record,
        key=key,
        category="fact",
        statement=FACT,
        evidence=[{"source_id": record["source_id"], "quote": quote}],
    )


def test_a_piece_after_a_split_stores_its_candidate(ledger):
    record = capture(ledger, "new", "Routine filler text. " * 230 + FACT)
    provider = CappedProvider(ledger, cap=3000)
    scripted = ScriptedProvider(ledger, [], "distinct")
    pieces = []

    def complete(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" not in payload:
            return scripted.complete(messages, **kwargs)
        window = payload["sources"]
        if sum(len(part["content"]) for part in window) > 3000:
            raise ProviderError("length", tokens=2048)
        pieces.append(window[0]["offset"])
        found = any(FACT in part["content"] for part in window)
        body = {"version": 1, "candidates": [fact_candidate(record)] if found else []}
        return ProviderResponse(json.dumps(body), 10)

    provider.complete = complete
    result = LearningWorker(ledger, provider, WorkerSettings(), LIMITS).run_once()
    assert result["status"] == "done" and len(pieces) == 2 and pieces[1] > 0
    assert [n["statement"] for n in ledger.current_notes("demo", "scope")] == [FACT]


def test_job_deferred_on_length_after_a_saturated_page_resumes_and_keeps_it(ledger, monkeypatch):
    record = capture(ledger, "new", FACT + " Routine filler text." * 240)
    seen = []
    # Twelve proposals saturate the page. Eleven quote text that is not in the
    # source, so they are dropped, and one candidate is kept.
    page = [fact_candidate(record)] + [
        fact_candidate(record, key=f"missing-{index}", quote=f"Missing quote {index}.")
        for index in range(11)
    ]
    scripted = ScriptedProvider(ledger, [], "distinct")

    def complete(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" not in payload:
            return scripted.complete(messages, **kwargs)
        size = sum(len(part["content"]) for part in payload["sources"])
        seen.append((size, payload["continuation"], payload["already_returned"]))
        if size > 3000 and payload["continuation"] == 0:
            return ProviderResponse(json.dumps({"version": 1, "candidates": page}), 10)
        if size > 3000:
            raise ProviderError("length", tokens=2048)
        return ProviderResponse(EMPTY, 10)

    provider = ScriptedProvider(ledger, [])
    provider.complete = complete

    def old_release(piece, slot):
        raise ProviderError("length")

    monkeypatch.setattr(module, "splittable", old_release)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert run.run_once()["status"] == "deferred"
    _, _, progress = job_row(ledger)
    assert progress["next_continuation"] == 1 and "output_limit_hold" in progress
    assert [r["status"] for r in receipts(ledger)] == ["saturated"]
    old_calls = len(seen)

    monkeypatch.undo()
    ready(ledger)
    upgraded = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert upgraded.run_once()["status"] == "done"
    # The held page is not sent again. Both halves start at continuation 0 and
    # get the kept candidate of the saturated page as already returned.
    assert [(c, r) for _, c, r in seen[old_calls:]] == [(0, [FACT]), (0, [FACT])]
    assert all(size < 3000 for size, _, _ in seen[old_calls:])
    assert [r["continuation"] for r in receipts(ledger)] == [0, 64, 128]
    assert [n["statement"] for n in ledger.current_notes("demo", "scope")] == [FACT]


def capture_turn(ledger, user_text, assistant_text):
    """One batch with a user part and an assistant part, so one window of two parts."""
    records = [
        {
            "source_id": ident,
            "source_hash": hashlib.sha256(text.encode()).hexdigest(),
            "content": text,
            "role": role,
            "observed_at": observed,
            "metadata": {},
        }
        for ident, text, role, observed in (
            ("asks", user_text, "user", "2026-01-02T12:00:00Z"),
            ("narrates", assistant_text, "assistant", "2026-01-02T12:00:01Z"),
        )
    ]
    ledger.capture_batch(
        {
            "batch_id": "batch-turn",
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": "review-session",
            "records": records,
        }
    )


def test_split_piece_without_eligible_evidence_gets_a_receipt_and_no_call(ledger):
    # NOBLIVION-241: from contract 2 on the no-call receipt is per piece, so the
    # assistant half of a split window gets no model call.
    capture_turn(ledger, "User asks about routine context. " * 60, "Assistant narration. " * 80)
    provider = CappedProvider(ledger, cap=2500)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert run.run_once()["status"] == "done"
    _, _, progress = job_row(ledger)
    assert progress["execution_configuration"]["contract"] == 3
    assert progress["length_splits"] == {"0": [0]}
    # The whole window ends on length, then only the user piece is called.
    assert provider.results == ["length", "success"]
    rows = {r["continuation"]: (r["status"], r["reason"]) for r in receipts(ledger)}
    assert sorted(rows) == [PIECE_STRIDE, 2 * PIECE_STRIDE]
    assert rows[2 * PIECE_STRIDE] == (
        "no_lesson",
        "no tool or direct user text in the window; no model call",
    )
    assert rows[PIECE_STRIDE] == ("no_lesson", "provider reported no eligible lesson")
