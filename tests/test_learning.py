# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real isolated SQLite/filesystem checks for capture, leases and memory lifecycle."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from noblivion import db
from noblivion.learning import (
    BudgetExceeded,
    BudgetLimits,
    LearningError,
    LearningLedger,
    LeaseLost,
    RevisionConflict,
)


def batch(number=1, text="Use short progress updates.", *, role="user", session="session-a"):
    return {
        "batch_id": f"batch-{number}",
        "namespace": "test",
        "root": "sample-project",
        "source_client": "codex",
        "session_id": session,
        "records": [
            {
                "source_id": f"source-{number}",
                "record_id": f"record-{number}",
                "role": role,
                "content": text,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "observed_at": "2026-10-12T09:00:00Z",
                "metadata": {"authority": "direct_user", "direct_user": True},
            }
        ],
        "cursor": {"path_id": "transcript-a", "offset": number * 100},
    }


def evidence(payload):
    source = payload["records"][0]
    return {
        "source_id": source["source_id"],
        "source_hash": source["source_hash"],
        "start": 0,
        "end": len(source["content"]),
        "quote": source["content"],
    }


def decision(payload, **extra):
    return {
        "action": "add",
        "logical_key": "progress-style",
        "category": "preference",
        "title": "Progress format",
        "statement": payload["records"][0]["content"],
        "observed_at": "2026-10-12",
        "effective_at": None,
        "scope": "general",
        "evidence": [evidence(payload)],
        **extra,
    }


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "test.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def learn(ledger, payload=None, **extra):
    payload = payload or batch()
    ledger.capture_batch(payload)
    job = ledger.claim("worker-a")
    result = ledger.finish(job["id"], job["lease_token"], [decision(payload, **extra)])
    return result[0]


def reserve(ledger, job, **extra):
    args = {
        "limits": BudgetLimits(),
        "estimated_tokens": 100,
        "estimated_seconds": 1.0,
        "estimated_cost": 0.0,
        **extra,
    }
    return ledger.reserve(job["id"], job["lease_token"], **args)


def test_capture_receipt_is_atomic_idempotent_and_cursor_monotonic(ledger):
    payload = batch()
    first = ledger.capture_batch(payload)
    second = ledger.capture_batch(payload)
    assert first["accepted"] and second["duplicate"] and first["job_id"] == second["job_id"]
    assert ledger.conn.execute("SELECT count(*) FROM learning_sources").fetchone()[0] == 1
    changed = {**payload, "session_id": "different"}
    with pytest.raises(LearningError, match="identity collision"):
        ledger.capture_batch(changed)
    bad = batch(2)
    bad["records"][0]["source_hash"] = "incorrect"
    with pytest.raises(LearningError, match="hash mismatch"):
        ledger.capture_batch(bad)
    assert ledger.conn.execute("SELECT count(*) FROM learning_batches").fetchone()[0] == 1
    assert ledger.conn.execute("SELECT offset FROM learning_cursors").fetchone()[0] == 100
    earlier = batch(3)
    earlier["cursor"]["offset"] = 10
    ledger.capture_batch(earlier)
    assert ledger.conn.execute("SELECT offset FROM learning_cursors").fetchone()[0] == 100


def test_same_source_new_batch_does_not_charge_another_extraction(ledger):
    payload = batch()
    ledger.capture_batch(payload)
    replay = {**payload, "batch_id": "another-delivery-id"}
    assert ledger.capture_batch(replay)["job_id"] is None
    assert ledger.conn.execute("SELECT count(*) FROM learning_jobs").fetchone()[0] == 1


def test_source_identity_cannot_change_role_or_scope(ledger):
    payload = batch()
    ledger.capture_batch(payload)
    changed = json.loads(json.dumps(payload))
    changed["batch_id"] = "collision"
    changed["records"][0]["role"] = "tool"
    with pytest.raises(LearningError, match="source identity"):
        ledger.capture_batch(changed)
    assert ledger.conn.execute("SELECT role FROM learning_sources").fetchone()[0] == "user"


def test_claim_includes_prior_tool_evidence_and_flattens_authority(ledger):
    first = batch(1, "Prior test failed.", role="tool")
    ledger.capture_batch(first)
    job = ledger.claim("worker")
    ledger.finish(job["id"], job["lease_token"], [])
    ledger.capture_batch(batch(2, "Tests now pass.", role="tool"))
    next_job = ledger.claim("worker")
    assert [source["id"] for source in next_job["sources"]] == ["source-1", "source-2"]
    assert next_job["sources"][0]["metadata"]["authority"] == "direct_user"


def test_no_cross_session_context(ledger):
    ledger.capture_batch(batch())
    job = ledger.claim("worker")
    ledger.finish(job["id"], job["lease_token"], [])
    ledger.capture_batch(batch(2, session="session-b"))
    assert [s["id"] for s in ledger.claim("worker")["sources"]] == ["source-2"]


def test_secrets_rejected_before_source_cursor_or_receipt(ledger):
    with pytest.raises(LearningError, match="unsafe"):
        ledger.capture_batch(batch(text="My password is synthetic-fixture-only."))
    for table in ("learning_sources", "learning_batches", "learning_jobs", "learning_cursors"):
        assert ledger.conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_source_and_job_survive_reopen(ledger, tmp_path):
    receipt = ledger.capture_batch(batch())
    conn = db.open_db(tmp_path / "test.db")
    try:
        job = LearningLedger(conn).claim("worker-after-restart")
        assert job["id"] == receipt["job_id"]
        assert job["sources"][0]["content"] == "Use short progress updates."
    finally:
        conn.close()


def test_two_workers_cannot_claim_same_job(ledger, tmp_path):
    ledger.capture_batch(batch())

    def claim_worker(name):
        conn = db.open_db(tmp_path / "test.db")
        try:
            return LearningLedger(conn).claim(name)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(claim_worker, ("worker-a", "worker-b")))
    assert sum(job is not None for job in jobs) == 1


def test_expired_lease_fences_late_writes_and_reclaims(ledger):
    payload = batch()
    ledger.capture_batch(payload)
    old = ledger.claim("old-worker")
    ledger.conn.execute("UPDATE learning_jobs SET lease_until=0")
    new = ledger.claim("new-worker")
    assert new["attempts"] == 2 and new["lease_token"] != old["lease_token"]
    with pytest.raises(LeaseLost):
        ledger.finish(old["id"], old["lease_token"], [decision(payload)])
    ledger.finish(new["id"], new["lease_token"], [decision(payload)])
    assert len(ledger.current_notes("test", "sample-project")) == 1


def test_bounded_retry_exhaustion_and_explicit_recovery(ledger):
    ledger.capture_batch(batch())
    for attempt in (1, 2):
        job = ledger.claim("worker", max_attempts=2)
        assert job["attempts"] == attempt
        ledger.fail(
            job["id"], job["lease_token"], "temporary failure", retry_delay=0, max_attempts=2
        )
    assert ledger.claim("worker", max_attempts=2) is None
    assert ledger.status()["jobs"] == {"exhausted": 1}
    ledger.retry(job["id"])
    assert ledger.claim("worker", max_attempts=2)["attempts"] == 1


def test_budget_counts_failed_calls_across_restarts_and_never_refunds(ledger, tmp_path):
    ledger.capture_batch(batch())
    job = ledger.claim("worker")
    reservation = reserve(ledger, job, limits=replace(BudgetLimits(), max_calls=1))
    ledger.fail(job["id"], job["lease_token"], "uncertain network result", retry_delay=0)
    conn = db.open_db(tmp_path / "test.db")
    reopened = LearningLedger(conn)
    try:
        next_job = reopened.claim("restart")
        with pytest.raises(BudgetExceeded):
            reserve(reopened, next_job, limits=replace(BudgetLimits(), max_calls=1))
        reopened.settle(reservation["id"], actual_tokens=1, actual_seconds=0.1, actual_cost=0)
        row = conn.execute("SELECT * FROM learning_budget").fetchone()
        assert row["tokens"] == 100 and row["seconds"] == 1.0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"estimated_cost": None},
        {"estimated_cost": float("nan")},
        {"estimated_cost": 0.001},
        {"estimated_tokens": -1},
        {"estimated_seconds": float("inf")},
        {"limits": replace(BudgetLimits(), max_tokens=99)},
        {"limits": replace(BudgetLimits(), max_seconds=0.5)},
    ],
)
def test_budget_fails_closed(ledger, settings):
    ledger.capture_batch(batch())
    job = ledger.claim("worker")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, job, **settings)
    assert ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == 0


def test_persistent_concurrency_and_measured_overrun(ledger):
    ledger.capture_batch(batch())
    ledger.capture_batch(batch(2, session="session-b"))
    first, second = ledger.claim("one"), ledger.claim("two")
    reservation = reserve(ledger, first)
    with pytest.raises(BudgetExceeded):
        reserve(ledger, second)
    ledger.settle(reservation["id"], actual_tokens=125, actual_seconds=2)
    reserve(ledger, second)
    assert ledger.status()["budget"]["tokens"] == 225


def test_checkpoint_survives_budget_defer_without_exhaustion(ledger):
    ledger.capture_batch(batch())
    job = ledger.claim("worker")
    progress = {"next_window": 1, "decisions": [], "fingerprint": "safe-window-hash"}
    ledger.checkpoint(job["id"], job["lease_token"], progress)
    ledger.defer(job["id"], job["lease_token"], retry_delay=0)
    resumed = ledger.claim("restart", max_attempts=1)
    assert resumed["progress"] == progress and resumed["attempts"] == 1


def test_title_change_keeps_stable_identity_and_revision_history(ledger):
    first = learn(ledger, scope="project")
    changed = ledger.edit(first["note_id"], 1, title="A new title")
    assert changed["note_id"] == first["note_id"] and changed["revision"] == 2
    inspected = ledger.inspect(first["note_id"])
    assert inspected["logical_key"] == "progress-style"
    assert [item["revision"] for item in inspected["history"]] == [1, 2]
    with pytest.raises(RevisionConflict):
        ledger.edit(first["note_id"], 1, title="Stale title")


def test_repeat_is_unchanged_and_distinct_fact_is_conflict(ledger):
    first = learn(ledger)
    again = learn(ledger, batch(2), title="Different title")
    assert again["action"] == "unchanged" and again["note_id"] == first["note_id"]
    conflict = learn(ledger, batch(3, "Use long progress updates."))
    assert conflict["action"] == "conflict"
    assert ledger.inspect(first["note_id"])["statement"] == "Use short progress updates."


def test_correction_requires_current_revision_and_immediately_invalidates_index(ledger):
    first = learn(ledger)
    note = ledger.inspect(first["note_id"])
    _index_note(ledger, note)
    correction = batch(2, "Use detailed progress updates.")
    conflict = learn(
        ledger, correction, action="update", note_id=note["note_id"], expected_revision=0
    )
    assert conflict["action"] == "conflict"
    correction = batch(3, "Use detailed progress updates.")
    changed = learn(
        ledger, correction, action="update", note_id=note["note_id"], expected_revision=1
    )
    assert changed["revision"] == 2 and changed["note_id"] == note["note_id"]
    assert ledger.conn.execute("SELECT archived_at FROM memories").fetchone()[0]
    assert "short progress" not in ledger.summary("test", "sample-project")


def test_supersede_retains_history_and_removes_old_note_from_current(ledger):
    first = learn(ledger)
    changed = learn(
        ledger,
        batch(2, "Use detailed progress updates."),
        action="supersede",
        note_id=first["note_id"],
        expected_revision=1,
    )
    assert changed["note_id"] != first["note_id"]
    assert ledger.inspect(first["note_id"])["status"] == "superseded"
    assert len(ledger.current_notes("test", "sample-project")) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": []},
        {"statement": "An unsupported inference."},
        {"category": "untyped"},
        {"category": "fact", "scope": "general"},
        {"category": "proven_fix", "scope": "project"},
    ],
)
def test_invalid_candidate_cannot_complete_job_or_write_note(ledger, change):
    payload = batch()
    ledger.capture_batch(payload)
    job = ledger.claim("worker")
    with pytest.raises(LearningError):
        ledger.finish(job["id"], job["lease_token"], [decision(payload, **change)])
    assert ledger.status()["notes"] == {} and ledger.status()["jobs"] == {"leased": 1}


def test_assistant_claim_is_not_user_authority(ledger):
    payload = batch(role="assistant")
    ledger.capture_batch(payload)
    job = ledger.claim("worker")
    with pytest.raises(LearningError, match="direct user"):
        ledger.finish(job["id"], job["lease_token"], [decision(payload)])


def test_tampered_and_cross_scope_evidence_rejected(ledger):
    payload = batch()
    ledger.capture_batch(payload)
    job = ledger.claim("worker")
    altered = evidence(payload)
    altered["source_hash"] = "wrong"
    with pytest.raises(LearningError, match="hash mismatch"):
        ledger.finish(job["id"], job["lease_token"], [decision(payload, evidence=[altered])])
    ledger.conn.execute("UPDATE learning_sources SET root='another-project'")
    with pytest.raises(LearningError, match="scope"):
        ledger.finish(job["id"], job["lease_token"], [decision(payload)])


def _index_note(ledger, note):
    with db.write_tx(ledger.conn):
        return db.insert_memory(
            ledger.conn,
            project=note["project"],
            root=note["root"],
            path=note["path"],
            source_type=db.SOURCE_MD,
            category="user",
            content=note["statement"],
            content_hash=note["file_hash"],
            labels="[]",
            rev=db.bump_rev(ledger.conn),
            now=db.utc_now(),
        )


def test_projection_survives_index_failure_and_reports_readiness_truthfully(ledger, tmp_path):
    note_id = learn(ledger)["note_id"]
    folder = tmp_path / "memory"
    assert ledger.project_pending("test", "sample-project", folder)["written"] == 1
    assert ledger.acknowledge_index("test", "sample-project") == 0
    assert ledger.inspect(note_id)["projection"]["status"] == "written"
    assert ledger.project_pending("test", "sample-project", folder)["written"] == 0
    note = ledger.inspect(note_id)
    assert hashlib.sha256((folder / note["path"]).read_bytes()).hexdigest() == note["file_hash"]
    _index_note(ledger, note)
    assert ledger.acknowledge_index("test", "sample-project") == 1
    assert ledger.inspect(note_id)["projection"]["status"] == "indexed"


def test_projection_write_failure_retries_after_restart(ledger, tmp_path, monkeypatch):
    note_id = learn(ledger)["note_id"]
    import noblivion.learning as module

    real_replace = module.os.replace

    def unavailable(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(module.os, "replace", unavailable)
    result = ledger.project_pending("test", "sample-project", tmp_path / "memory")
    assert result["failed"] == 1 and ledger.inspect(note_id)["projection"]["status"] == "pending"
    monkeypatch.setattr(module.os, "replace", real_replace)
    conn = db.open_db(tmp_path / "test.db")
    try:
        assert (
            LearningLedger(conn).project_pending("test", "sample-project", tmp_path / "memory")[
                "written"
            ]
            == 1
        )
    finally:
        conn.close()


def test_exclusion_revokes_source_dependent_notes_and_stale_worker(ledger, tmp_path):
    first = learn(ledger)
    ledger.project_pending("test", "sample-project", tmp_path / "memory")
    ledger.capture_batch(batch(2))
    job = ledger.claim("in-flight")
    result = ledger.exclude_source("source-1")
    assert result["excluded_notes"] == [first["note_id"]]
    assert ledger.summary("test", "sample-project") == ""
    with pytest.raises(LeaseLost):
        ledger.finish(job["id"], job["lease_token"], [])
    assert ledger.project_pending("test", "sample-project", tmp_path / "memory")["removed"] == 1
    assert ledger.capture_batch({**batch(), "batch_id": "replay-excluded"})["job_id"] is None


def test_forget_erases_history_evidence_source_decisions_and_recall(ledger, tmp_path):
    first = learn(ledger)
    note = ledger.inspect(first["note_id"])
    _index_note(ledger, note)
    ledger.project_pending("test", "sample-project", tmp_path / "memory")
    result = ledger.forget(note["note_id"])
    assert result["forgotten_notes"] == [note["note_id"]]
    forgotten = ledger.inspect(note["note_id"])
    assert forgotten["status"] == "forgotten" and not forgotten["statement"]
    assert not forgotten["history"] and not forgotten["evidence"]
    for table in ("learning_revisions", "learning_evidence", "learning_decisions", "memories"):
        assert ledger.conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert ledger.conn.execute("SELECT content FROM learning_sources").fetchone()[0] == ""
    assert ledger.capture_batch({**batch(), "batch_id": "forgotten-replay"})["job_id"] is None
    ledger.project_pending("test", "sample-project", tmp_path / "memory")
    assert not list((tmp_path / "memory").glob("*.md"))
    assert ledger.acknowledge_index("test", "sample-project") == 1


def test_use_and_contribution_are_independent_and_root_opt_out_wins(ledger):
    ledger.set_controls("test", "sample-project", use=False)
    first = learn(ledger)
    assert ledger.summary("test", "sample-project") == ""
    assert ledger.current_notes("test", "sample-project")[0]["note_id"] == first["note_id"]
    ledger.set_controls("test", "sample-project", "session-a", use=True)
    assert not ledger.effective_controls("test", "sample-project", "session-a")["use"]
    ledger.set_controls("test", "sample-project", use=True, contribute=False)
    receipt = ledger.capture_batch(batch(2))
    assert receipt["skipped"] and receipt["job_id"] is None
    assert ledger.summary("test", "sample-project")
    assert ledger.conn.execute("SELECT count(*) FROM learning_sources").fetchone()[0] == 1


def test_pause_fences_inflight_worker_and_resume_recovers(ledger):
    payload = batch()
    ledger.capture_batch(payload)
    job = ledger.claim("worker")
    ledger.set_controls("test", "sample-project", "session-a", paused=True)
    with pytest.raises(LeaseLost):
        ledger.finish(job["id"], job["lease_token"], [decision(payload)])
    assert ledger.claim("worker") is None
    ledger.set_controls("test", "sample-project", "session-a", paused=False)
    resumed = ledger.claim("worker")
    ledger.finish(resumed["id"], resumed["lease_token"], [decision(payload)])


def test_summary_is_bounded_deterministic_and_derived(ledger):
    first = learn(ledger)
    summary = ledger.summary("test", "sample-project")
    assert summary == ledger.summary("test", "sample-project")
    assert first["note_id"] in summary
    assert ledger.summary("test", "sample-project", limit_chars=5) == ""
    ledger.exclude(first["note_id"])
    assert ledger.summary("test", "sample-project") == ""


def test_legacy_context_is_read_only(ledger):
    with db.write_tx(ledger.conn):
        memory_id = db.insert_memory(
            ledger.conn,
            project="test",
            root="sample-project",
            path="manual.md",
            source_type=db.SOURCE_MD,
            category="user",
            content="Keep concise updates.",
            content_hash="a" * 64,
            labels="[]",
            rev=db.bump_rev(ledger.conn),
            now=db.utc_now(),
        )
    legacy = ledger.reconciliation_context("test", "sample-project")[0]
    assert legacy["read_only"] and legacy["note_id"] == f"legacy:{memory_id}"
    payload = batch()
    ledger.capture_batch(payload)
    job = ledger.claim("worker")
    result = ledger.finish(
        job["id"], job["lease_token"], [{"action": "unchanged", "note_id": legacy["note_id"]}]
    )
    assert result[0]["action"] == "unchanged"
    assert ledger.current_notes("test", "sample-project") == []


def test_cli_reads_existing_store_and_changes_controls(ledger, tmp_path, capsys):
    from noblivion.learning_cli import main

    args = ["--database", str(tmp_path / "test.db")]
    assert (
        main([*args, "controls", "--project", "test", "--root", "sample-project", "--use", "off"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["use"] is False
    assert main([*args, "status", "--project", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == {}
    assert main(["--database", str(tmp_path / "missing.db"), "status"]) == 1
    assert not (tmp_path / "missing.db").exists()


def test_version_one_database_migrates_with_backup(tmp_path):
    path = tmp_path / "existing.db"
    conn = db.connect(path, create=True)
    first = db.load_migrations()[0]
    with db.write_tx(conn):
        for statement in db.split_statements(first.sql):
            conn.execute(statement, {"random_base": 1234567} if ":random_base" in statement else ())
        conn.execute("PRAGMA user_version=1")
    conn.close()
    migrated = db.open_db(path)
    try:
        assert db.user_version(migrated) == len(db.load_migrations())
        assert migrated.execute("SELECT count(*) FROM learning_jobs").fetchone()[0] == 0
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    finally:
        migrated.close()


def test_two_connections_cannot_overwrite_one_revision(ledger, tmp_path):
    first = learn(ledger, scope="project")

    def change_title(title):
        conn = db.open_db(tmp_path / "test.db")
        try:
            try:
                return LearningLedger(conn).edit(first["note_id"], 1, title=title)["revision"]
            except RevisionConflict:
                return "conflict"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change_title, ("First title", "Second title")))
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert ledger.inspect(first["note_id"])["revision"] == 2


def test_logical_key_change_cannot_duplicate_identical_memory(ledger):
    first = learn(ledger)
    again = learn(ledger, batch(2), logical_key="a-different-model-key", title="Another title")
    assert again["action"] == "unchanged" and again["note_id"] == first["note_id"]
    assert len(ledger.current_notes("test", "sample-project")) == 1


def test_exclusion_cancels_queued_duplicate_source_and_blocks_new_identity(ledger):
    first = learn(ledger)
    ledger.capture_batch(batch(2))
    ledger.exclude(first["note_id"])
    assert ledger.claim("worker") is None
    assert ledger.capture_batch(batch(3))["job_id"] is None
    assert (
        ledger.conn.execute(
            "SELECT count(*) FROM learning_sources WHERE status='eligible'"
        ).fetchone()[0]
        == 0
    )


def test_legacy_relevance_searches_after_first_five_hundred_rows(ledger):
    with db.write_tx(ledger.conn):
        for number in range(502):
            db.insert_memory(
                ledger.conn,
                project="test",
                root="sample-project",
                path=f"manual-{number}.md",
                source_type=db.SOURCE_MD,
                category="reference",
                content="Unrelated information."
                if number < 501
                else "Silver orchard release steps.",
                content_hash=str(number),
                labels="[]",
                rev=db.bump_rev(ledger.conn),
                now=db.utc_now(),
            )
    results = ledger.reconciliation_context(
        "test", "sample-project", query="silver orchard", limit=3
    )
    assert len(results) == 1 and results[0]["title"] == "manual-501.md"
