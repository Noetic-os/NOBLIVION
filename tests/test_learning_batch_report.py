# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only batch report: sessions, re-read overlap and started jobs (NOBLIVION-265)."""

from __future__ import annotations

import hashlib
import json

import pytest

from noblivion import db, learning_cli
from noblivion.learning import LearningLedger
from noblivion.learning_batch_report import batch_report

PROJECT, SCOPE = "test", "demo"


def batch(number, session, text):
    return {
        "batch_id": f"batch-{session}-{number}",
        "namespace": PROJECT,
        "root": SCOPE,
        "source_client": "claude",
        "session_id": session,
        "records": [
            {
                "source_id": f"source-{session}-{number}",
                "record_id": f"record-{session}-{number}",
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
def store(tmp_path):
    path = tmp_path / "report.db"
    conn = db.open_db(path, create=True)
    ledger = LearningLedger(conn)
    # Session s1: job A reads seq 1-2 and finishes. Seq 3 then makes job B, which takes
    # seq 1-2 again as context: 10 + 20 characters of overlap.
    job_a = ledger.capture_batch(batch(1, "s1", "a" * 10))["job_id"]
    assert ledger.capture_batch(batch(2, "s1", "b" * 20))["job_id"] == job_a
    conn.execute("UPDATE learning_jobs SET status='done' WHERE id=?", (job_a,))
    job_b = ledger.capture_batch(batch(3, "s1", "c" * 40))["job_id"]
    assert job_b != job_a
    conn.execute(
        "UPDATE learning_jobs SET progress=? WHERE id=?",
        (json.dumps({"next_window": 1, "decisions": []}), job_b),
    )
    # Session s2: a charged job without stored work. Session s3: an unstarted job.
    job_c = ledger.capture_batch(batch(1, "s2", "d" * 7))["job_id"]
    conn.execute("UPDATE learning_jobs SET attempts=1 WHERE id=?", (job_c,))
    job_d = ledger.capture_batch(batch(1, "s3", "e" * 5))["job_id"]
    conn.close()
    return path, {"a": job_a, "b": job_b, "c": job_c, "d": job_d}


def open_ro(path):
    import sqlite3

    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_report_counts_overlap_sessions_and_started_jobs(store):
    path, _ = store
    conn = open_ro(path)
    try:
        report = batch_report(conn)
    finally:
        conn.close()
    assert report["extract_jobs"]["done"] == {
        "jobs": 1,
        "chars": 30,
        "overlap_chars": 0,
        "overlap_share": 0.0,
    }
    pending = report["pending"]
    assert pending["jobs"] == 3 and pending["sessions"] == 3
    assert pending["chars"] == 70 + 7 + 5
    assert pending["overlap_chars"] == 30
    assert pending["overlap_share"] == round(30 / 82, 4)
    assert pending["distinct_chars"] == 82
    # Seq 1-2 of s1 were read by the done job A, so only 40 + 7 + 5 are new.
    assert pending["distinct_chars_not_done"] == 52
    assert pending["started"] == {"holds_work": 1, "charged_no_work": 1, "unstarted": 1}


def test_cancelled_job_does_not_count_as_covered(store):
    path, jobs = store
    conn = db.open_db(path)
    conn.execute("UPDATE learning_jobs SET status='cancelled' WHERE id=?", (jobs["a"],))
    conn.close()
    conn = open_ro(path)
    try:
        pending = batch_report(conn)["pending"]
    finally:
        conn.close()
    assert pending["overlap_chars"] == 0
    assert pending["distinct_chars_not_done"] == 82


def test_scope_filter(store):
    path, _ = store
    conn = open_ro(path)
    try:
        assert batch_report(conn, project="other")["pending"]["jobs"] == 0
        assert batch_report(conn, project=PROJECT, root="other")["pending"]["jobs"] == 0
        assert batch_report(conn, project=PROJECT, root=SCOPE)["pending"]["jobs"] == 3
    finally:
        conn.close()


def report_after(path, *statements):
    conn = db.open_db(path)
    for sql, args in statements:
        conn.execute(sql, args)
    conn.close()
    conn = open_ro(path)
    try:
        return batch_report(conn)
    finally:
        conn.close()


def test_a_source_that_is_not_eligible_is_not_counted(store):
    path, _ = store
    report = report_after(
        path, ("UPDATE learning_sources SET status='excluded' WHERE id=?", ("source-s1-3",))
    )
    assert report["pending"]["chars"] == 30 + 7 + 5
    assert report["pending"]["distinct_chars_not_done"] == 12


def test_a_budget_charge_alone_counts_as_charged(store):
    path, jobs = store
    report = report_after(
        path,
        (
            "INSERT INTO learning_budget VALUES ('b1',?,'t',0,0,0,'settled',0,0)",
            (jobs["d"],),
        ),
    )
    assert report["pending"]["started"] == {"holds_work": 1, "charged_no_work": 2, "unstarted": 0}


def test_an_extraction_page_alone_counts_as_stored_work(store):
    path, jobs = store
    report = report_after(
        path,
        (
            "INSERT INTO learning_extraction_pages VALUES (?,0,0,'f','[]','[]','processed','',0)",
            (jobs["d"],),
        ),
    )
    assert report["pending"]["started"] == {"holds_work": 2, "charged_no_work": 1, "unstarted": 0}


def test_partial_work_keys_count_as_stored_work(store):
    path, jobs = store
    progress = json.dumps({"next_window": 0, "decisions": [], "semantic": {"x": 1}})
    report = report_after(
        path, ("UPDATE learning_jobs SET progress=? WHERE id=?", (progress, jobs["d"]))
    )
    assert report["pending"]["started"]["holds_work"] == 2


def test_a_job_without_a_range_is_counted_by_status(store):
    path, jobs = store
    report = report_after(path, ("DELETE FROM learning_job_ranges WHERE job_id=?", (jobs["a"],)))
    assert report["unranged_extract_jobs"] == {"done": 1}
    assert "done" not in report["extract_jobs"]


def test_cli_reads_a_store_it_cannot_write(store, capsys):
    path, _ = store
    path.chmod(0o444)
    try:
        assert learning_cli.main(["--database", str(path), "batch-report"]) == 0
    finally:
        path.chmod(0o600)
    assert json.loads(capsys.readouterr().out)["pending"]["jobs"] == 3


def test_cli_reads_without_writing(store, capsys):
    path, _ = store
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert learning_cli.main(["--database", str(path), "batch-report"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"]["jobs"] == 3
    # A WAL reader may add the shared-memory index file; no existing file changes.
    assert {name: (path.parent / name).read_bytes() for name in before} == before
    wal = path.parent / (path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_cli_missing_database_is_not_created(tmp_path, capsys):
    path = tmp_path / "missing.db"
    assert learning_cli.main(["--database", str(path), "batch-report"]) == 1
    assert not path.exists()
    assert "OSError" in capsys.readouterr().err


def test_cli_connection_refuses_writes(store, monkeypatch, capsys):
    path, _ = store

    def writes(conn, project, root):
        conn.execute("CREATE TABLE probe_write (a)")
        return {}

    monkeypatch.setattr("noblivion.learning_batch_report.batch_report", writes)
    assert learning_cli.main(["--database", str(path), "batch-report"]) == 1
    assert "OperationalError" in capsys.readouterr().err
    conn = open_ro(path)
    try:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='probe_write'").fetchone()
    finally:
        conn.close()
