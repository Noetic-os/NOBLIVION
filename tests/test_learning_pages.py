# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real SQLite dense coverage, atomic admission and checkpoint recovery."""

import hashlib
import json

import pytest

from noblivion import db
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_pages import coverage
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings


class DenseProvider:
    def __init__(self, *, repeat=False):
        self.calls = []
        self.repeat = repeat

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        value = json.loads(messages[-1]["content"])
        self.calls.append(value)
        if "sources" not in value:
            return ProviderResponse(
                json.dumps(
                    {
                        "version": 1,
                        "relations": [
                            {"target_id": t["target_id"], "relation": "distinct"}
                            for t in value["targets"]
                        ],
                    }
                ),
                50,
            )
        returned = set() if self.repeat else set(value.get("already_returned", []))
        candidates = []
        for source in value["sources"]:
            for statement in source["content"].splitlines():
                if statement in returned or not statement.startswith("Set fixture_"):
                    continue
                key = statement.split()[1]
                candidates.append(
                    dict(
                        action="add",
                        logical_key=key,
                        category="fact",
                        title=key,
                        statement=statement,
                        scope="project",
                        observed_at="2026-10-12",
                        effective_at=None,
                        evidence=[{"source_id": source["id"], "quote": statement}],
                    )
                )
        return ProviderResponse(json.dumps({"version": 1, "candidates": candidates[:12]}), 50)


def seed(conn, count=40):
    ledger = LearningLedger(conn)
    text = "\n".join(f"Set fixture_{i:02d} to {i + 700}." for i in range(count))
    receipt = ledger.capture_batch(
        dict(
            batch_id="dense",
            namespace="fixture",
            root="root",
            session_id="learn",
            source_client="codex",
            records=[
                dict(
                    source_id="dense-source",
                    record_id="dense-record",
                    role="user",
                    content=text,
                    source_hash=hashlib.sha256(text.encode()).hexdigest(),
                    observed_at="2026-10-12T09:00:00Z",
                    metadata={"direct_user": True},
                )
            ],
        )
    )
    return ledger, receipt["job_id"]


def run(ledger, provider, **limits):
    return LearningWorker(
        ledger,
        provider,
        WorkerSettings(**limits),
        BudgetLimits(max_calls=256, max_tokens=5000000, max_seconds=10000),
    ).run_once()


def test_more_than_32_distinct_lessons_and_late_evidence_are_admitted_atomically(tmp_path):
    conn = db.open_db(tmp_path / "coverage.db", create=True)
    ledger, job_id = seed(conn)
    provider = DenseProvider()
    result = run(ledger, provider)
    assert result["status"] == "done", result
    notes = ledger.current_notes("fixture", "root")
    assert len(notes) == 40
    assert "Set fixture_39 to 739." in {note["statement"] for note in notes}
    pages = coverage(conn, job_id)
    assert [page["status"] for page in pages] == ["saturated"] * 3 + ["processed"]
    assert (
        conn.execute("SELECT status FROM learning_jobs WHERE id=?", (job_id,)).fetchone()[0]
        == "done"
    )
    assert conn.execute("SELECT count(*) FROM learning_evidence").fetchone()[0] == 40
    assert run(ledger, provider)["status"] == "idle"
    conn.close()


def test_crash_after_page_receipt_before_cursor_reuses_receipt_after_reopen(tmp_path):
    path = tmp_path / "restart.db"
    conn = db.open_db(path, create=True)
    ledger, job_id = seed(conn, 15)
    provider = DenseProvider()
    checkpoint = ledger.checkpoint
    failed = False

    def crash(job, lease, progress):
        nonlocal failed
        if progress.get("next_continuation") == 1 and not failed:
            failed = True
            raise OSError("injected storage interruption")
        return checkpoint(job, lease, progress)

    ledger.checkpoint = crash
    assert run(ledger, provider)["status"] == "failed"
    assert len(provider.calls) == 1
    assert len(ledger.current_notes("fixture", "root")) == 0
    conn.execute("UPDATE learning_jobs SET next_attempt=0")
    conn.close()
    conn = db.open_db(path)
    ledger = LearningLedger(conn)
    assert run(ledger, provider)["status"] == "done"
    assert len(ledger.current_notes("fixture", "root")) == 15
    assert len([call for call in provider.calls if "sources" in call]) == 2
    assert len(coverage(conn, job_id)) == 2
    conn.close()


@pytest.mark.parametrize("settings", [{"max_continuations": 1}, {"max_pages": 1}])
def test_saturation_bounds_never_acknowledge_missing_late_evidence(tmp_path, settings):
    conn = db.open_db(tmp_path / "exhausted.db", create=True)
    ledger, job_id = seed(conn, 15)
    result = run(ledger, DenseProvider(), **settings)
    assert result["status"] == "failed" and result["permanent"]
    assert not ledger.current_notes("fixture", "root")
    job = conn.execute("SELECT status FROM learning_jobs WHERE id=?", (job_id,)).fetchone()
    assert job["status"] == "exhausted"
    assert coverage(conn, job_id)[0]["status"] in {"saturated", "exhausted"}
    conn.close()


def test_repeated_saturated_page_is_visible_exhaustion(tmp_path):
    conn = db.open_db(tmp_path / "repeat.db", create=True)
    ledger, job_id = seed(conn, 15)
    result = run(ledger, DenseProvider(repeat=True))
    assert result["status"] == "failed" and result["error"] == "CoverageExhausted"
    assert [row["status"] for row in coverage(conn, job_id)] == ["saturated", "exhausted"]
    assert not ledger.current_notes("fixture", "root")
    conn.close()


def test_forget_erases_all_page_quoted_payloads_before_return(tmp_path):
    conn = db.open_db(tmp_path / "forget.db", create=True)
    ledger, job_id = seed(conn, 15)
    assert run(ledger, DenseProvider())["status"] == "done"
    ledger.forget(ledger.current_notes("fixture", "root")[0]["note_id"])
    assert not coverage(conn, job_id)
    assert "Set fixture_14" not in json.dumps(
        [dict(r) for r in conn.execute("SELECT * FROM learning_jobs")]
    )
    conn.close()


def test_first_request_failure_cannot_change_frozen_configuration(tmp_path):
    conn = db.open_db(tmp_path / "freeze.db", create=True)
    ledger, job_id = seed(conn, 15)
    provider = DenseProvider()
    original = provider.complete
    provider.complete = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("outage"))
    assert run(ledger, provider)["status"] == "failed"
    conn.execute("UPDATE learning_jobs SET next_attempt=0")
    provider.complete = original
    result = run(ledger, provider, max_continuations=10)
    assert result["status"] == "failed" and result["permanent"]
    assert provider.calls == []
    assert not coverage(conn, job_id)
    conn.close()


def test_explicit_retry_repairs_rejected_page_without_replaying_accepted_pages(tmp_path):
    conn = db.open_db(tmp_path / "retry.db", create=True)
    ledger, job_id = seed(conn, 15)
    provider = DenseProvider()
    original = provider.complete
    rejected = False

    def reject_second(messages, **kwargs):
        nonlocal rejected
        payload = json.loads(messages[-1]["content"])
        if payload.get("continuation") == 1 and not rejected:
            rejected = True
            return ProviderResponse('{"version":1,"candidates":[{}]}', 50)
        return original(messages, **kwargs)

    provider.complete = reject_second
    assert run(ledger, provider)["status"] == "failed"
    assert [r["status"] for r in coverage(conn, job_id)] == ["saturated", "rejected"]
    ledger.retry(job_id)
    assert run(ledger, provider)["status"] == "done"
    assert len(ledger.current_notes("fixture", "root")) == 15
    assert len([call for call in provider.calls if "sources" in call]) == 2
    conn.close()


def test_derivation_dispatch_uses_same_reservation_path_and_no_extraction(tmp_path):
    conn = db.open_db(tmp_path / "derivation.db", create=True)
    ledger, _ = seed(conn, 1)
    provider = DenseProvider()
    assert run(ledger, provider, enable_consolidation=True)["status"] == "done"
    assert (
        conn.execute("SELECT count(*) FROM learning_jobs WHERE kind='consolidate'").fetchone()[0]
        == 1
    )
    initial = len(provider.calls)

    def proposals(messages, **kwargs):
        value = json.loads(messages[-1]["content"])
        assert "notes" in value and "sources" not in value
        assert (
            conn.execute("SELECT count(*) FROM learning_budget WHERE status='reserved'").fetchone()[
                0
            ]
            == 1
        )
        provider.calls.append(value)
        return ProviderResponse('{"claims":[]}', 50)

    provider.complete = proposals
    assert run(ledger, provider, enable_consolidation=True)["status"] == "done"
    assert len(provider.calls) == initial + 1
    assert conn.execute("SELECT count(*) FROM learning_extraction_pages").fetchone()[0] == 1
    conn.close()


def test_shorter_duplicate_continuation_cannot_fake_complete_coverage(tmp_path):
    conn = db.open_db(tmp_path / "short-duplicate.db", create=True)
    ledger, job_id = seed(conn, 15)
    provider = DenseProvider(repeat=True)
    original = provider.complete

    def shorter_repeat(messages, **kwargs):
        response = original(messages, **kwargs)
        if json.loads(messages[-1]["content"]).get("continuation") == 1:
            value = json.loads(response.content)
            value["candidates"] = value["candidates"][:11]
            return ProviderResponse(json.dumps(value), 50)
        return response

    provider.complete = shorter_repeat
    result = run(ledger, provider)
    assert result["status"] == "failed" and result["error"] == "CoverageExhausted"
    assert [r["status"] for r in coverage(conn, job_id)] == ["saturated", "exhausted"]
    assert not ledger.current_notes("fixture", "root")
    conn.close()


def test_derivation_enqueue_storage_failure_rolls_back_extract_admission(tmp_path, monkeypatch):
    conn = db.open_db(tmp_path / "enqueue-fault.db", create=True)
    ledger, job_id = seed(conn, 1)

    def fail_queue(*args, **kwargs):
        raise OSError("injected queue storage failure")

    monkeypatch.setattr(ledger, "_enqueue_derivation", fail_queue)
    result = run(ledger, DenseProvider(), enable_consolidation=True)
    assert result["status"] == "failed"
    assert not ledger.current_notes("fixture", "root")
    assert (
        conn.execute("SELECT status FROM learning_jobs WHERE id=?", (job_id,)).fetchone()[0]
        == "pending"
    )
    assert (
        conn.execute("SELECT count(*) FROM learning_jobs WHERE kind='consolidate'").fetchone()[0]
        == 0
    )
    conn.close()


@pytest.mark.parametrize("kind", ["extract", "consolidate"])
@pytest.mark.parametrize("drift", ["endpoint", "class"])
def test_restart_provider_identity_drift_makes_no_new_request(tmp_path, kind, drift):
    path = tmp_path / "provider-drift.db"
    conn = db.open_db(path, create=True)
    ledger, _ = seed(conn, 1)
    provider = DenseProvider()
    provider.endpoint = "http://127.0.0.1:41001/v1/chat/completions"
    if kind == "consolidate":
        assert run(ledger, provider, enable_consolidation=True)["status"] == "done"
    provider.complete = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("outage"))
    assert run(ledger, provider, enable_consolidation=(kind == "consolidate"))["status"] == "failed"
    before = conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0]
    conn.execute("UPDATE learning_jobs SET next_attempt=0")
    conn.close()
    conn = db.open_db(path)
    ledger = LearningLedger(conn)
    if drift == "endpoint":
        provider.endpoint = "http://127.0.0.1:41002/v1/chat/completions"
    else:

        class ChangedProvider(DenseProvider):
            pass

        provider = ChangedProvider()
        provider.endpoint = "http://127.0.0.1:41001/v1/chat/completions"
    provider.complete = lambda *args, **kwargs: pytest.fail("changed provider must never be called")
    result = run(ledger, provider, enable_consolidation=(kind == "consolidate"))
    assert result["status"] == "failed" and result["permanent"]
    assert conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == before
    conn.close()
