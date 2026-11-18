# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session batch learning with a watermark (NOBLIVION-267). No real model is called."""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from noblivion import db, learning_batch, learning_ranges, learning_runtime
from noblivion.learning import BudgetLimits, LearningLedger, StepAside
from noblivion.learning_worker import LearningWorker, WorkerSettings
from test_learning_night_window import BEFORE, INSIDE, manual_pool
from test_learning_parallel import FakeStore, GatedProvider, reserve_one, rows, write_settings

PROJECT, ROOT = "demo", "example"
# About 5,100 characters: a capture job with earlier context needs several windows.
FILLER = " Routine context." * 300
OPEN = '{"next_window":0,"decisions":[]}'


def payload(number, session="s1", text=None, client="codex"):
    text = text or f"Use short progress updates, rule {number} of session {session}."
    return {
        "batch_id": f"batch-{session}-{client}-{number}",
        "namespace": PROJECT,
        "root": ROOT,
        "source_client": client,
        "session_id": session,
        "records": [
            {
                "source_id": f"source-{session}-{client}-{number}",
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
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "batch.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def jobs(ledger):
    """Every extract job with its range, oldest first."""
    return [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT j.id,j.status,j.last_error,j.attempts,r.session_id,r.first_seq,r.last_seq,"
            "EXISTS (SELECT 1 FROM learning_batch_items i WHERE i.job_id=j.id) item "
            "FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id "
            "ORDER BY j.created_at,j.rowid"
        )
    ]


def items(ledger):
    return [job for job in jobs(ledger) if job["item"]]


def marks(ledger):
    return {
        row["session_id"]: row["last_seq"]
        for row in ledger.conn.execute("SELECT * FROM learning_session_watermarks")
    }


# --- the setting ---------------------------------------------------------------


def test_the_setting_is_off_by_default_and_strictly_validated(tmp_path):
    assert learning_batch.batch_options({}) is False
    assert learning_batch.batch_options({"batch": {}}) is False
    assert learning_batch.batch_options({"batch": {"enabled": False}}) is False
    assert learning_batch.batch_options({"batch": {"enabled": True}}) is True
    for bad in ({"enabled": "yes"}, {"enabled": 1}, {"enabled": True, "size": 3}, [], "on"):
        with pytest.raises(ValueError):
            learning_batch.batch_options({"batch": bad})
    write_settings(tmp_path, 1)
    value = json.loads((tmp_path / "learning.json").read_text())
    assert learning_runtime.load_settings(tmp_path)["enabled"] is True
    assert learning_runtime.settings_report(tmp_path)["batch"] == {"enabled": False}
    value["batch"] = {"enabled": "true"}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_runtime.load_settings(tmp_path)["enabled"] is False
    value["batch"] = {"enabled": True}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_runtime.load_settings(tmp_path)["enabled"] is True
    assert learning_runtime.settings_report(tmp_path)["batch"] == {"enabled": True}


# --- off: exactly as before ----------------------------------------------------


def test_off_the_claim_plans_nothing_and_takes_the_capture_job(ledger, monkeypatch):
    for name in ("plan", "superseded_ids", "cancel_covered"):
        monkeypatch.setattr(learning_batch, name, lambda *a, **k: pytest.fail("batch code ran"))
    for number in (1, 2, 3):
        ledger.capture_batch(payload(number))
    job = ledger.claim("w")
    assert [s["seq"] for s in job["sources"]] == [1, 2, 3]
    ledger.finish(job["id"], job["lease_token"], [])
    assert items(ledger) == [] and marks(ledger) == {}
    assert [j["status"] for j in jobs(ledger)] == ["done"]


def run_until_idle(store, settings, limit=50):
    results = []
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        for _ in range(limit):
            result = learning_runtime.run_once(ledger, settings, worker_id="w")
            if result["status"] == "idle":
                return results
            results.append(result)
    raise AssertionError("the worker did not go idle")


def scripted_store(tmp_path, monkeypatch, provider, batch_block):
    """Two sessions; one job of s1 is started (checkpoint and a call) before more captures."""
    tmp_path.mkdir()
    store = FakeStore(tmp_path)
    write_settings(tmp_path, 1)
    value = json.loads((tmp_path / "learning.json").read_text())
    if batch_block is not None:
        value["batch"] = batch_block
    (tmp_path / "learning.json").write_text(json.dumps(value))

    def options(settings):
        return (
            provider,
            WorkerSettings(**settings.get("worker", {})),
            BudgetLimits(**settings.get("budget", {})),
        )

    monkeypatch.setattr(learning_runtime, "worker_options", options)
    settings = learning_runtime.load_settings(tmp_path)
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        for number in (1, 2):
            for session in ("s1", "s2"):
                ledger.capture_batch(payload(number, session, long_text(number, session)))
    # One full run with batch off makes the first s1 and s2 jobs done.
    run_until_idle(store, dict(settings, batch={"enabled": False}))
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        for number in (3, 4, 5):
            ledger.capture_batch(payload(number, "s1", long_text(number, "s1")))
        ledger.capture_batch(payload(3, "s2", long_text(3, "s2")))
    return store, settings, len(provider.starts)


def long_text(number, session):
    return f"Use short progress updates, rule {number} of session {session}." + FILLER


def summary(store):
    return {
        "jobs": sorted(
            (
                r["session_id"],
                r["status"],
                r["last_error"],
                r["attempts"],
                r["first_seq"],
                r["last_seq"],
            )
            for r in rows(
                store,
                "SELECT j.status,j.last_error,j.attempts,r.session_id,r.first_seq,r.last_seq "
                "FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id",
            )
        ),
        "budget_rows": len(rows(store, "SELECT 1 FROM learning_budget")),
        "pages": len(rows(store, "SELECT 1 FROM learning_extraction_pages")),
        "items": rows(store, "SELECT * FROM learning_batch_items"),
        "watermarks": rows(store, "SELECT * FROM learning_session_watermarks"),
    }


def test_off_with_no_block_and_with_enabled_false_runs_the_same(tmp_path, monkeypatch):
    seen = {}
    for name, block in (("absent", None), ("false", {"enabled": False})):
        provider = GatedProvider(open_gate=True)
        store, settings, _ = scripted_store(tmp_path / name, monkeypatch, provider, block)
        run_until_idle(store, settings)
        seen[name] = (summary(store), len(provider.starts))
    assert seen["absent"] == seen["false"]
    result, _ = seen["absent"]
    assert result["items"] == [] and result["watermarks"] == []
    # Per-capture jobs only: the new jobs read their earlier context again.
    assert ("s1", "done", "", 1, 1, 5) in result["jobs"]
    assert all(status == "done" for _, status, *_ in result["jobs"])


def test_on_one_item_per_session_reads_only_new_text(tmp_path, monkeypatch):
    off = GatedProvider(open_gate=True)
    store, settings, off_before = scripted_store(tmp_path / "off", monkeypatch, off, None)
    run_until_idle(store, settings)
    on = GatedProvider(open_gate=True)
    store, settings, on_before = scripted_store(tmp_path / "on", monkeypatch, on, {"enabled": True})
    assert on_before == off_before
    run_until_idle(store, settings)
    result = summary(store)
    jobs_by_status = {(j[0], j[1], j[2], j[4], j[5]) for j in result["jobs"]}
    # Items start after the text done jobs read, with no earlier context.
    assert ("s1", "done", "", 3, 5) in jobs_by_status
    assert ("s2", "done", "", 3, 3) in jobs_by_status
    # The capture jobs that the items cover are cancelled with the reason batch.
    assert ("s1", "cancelled", "batch", 1, 5) in jobs_by_status
    assert ("s2", "cancelled", "batch", 1, 3) in jobs_by_status
    assert {(w["session_id"], w["last_seq"]) for w in result["watermarks"]} == {
        ("s1", 5),
        ("s2", 3),
    }
    assert len(result["items"]) == 2
    # The capture jobs read 5 and 3 sources again; the items read 3 and 1.
    assert len(on.starts) - on_before < len(off.starts) - off_before


# --- planning, watermark and cancel --------------------------------------------


def test_an_item_takes_the_text_after_the_watermark_and_no_context(ledger):
    for number in (1, 2, 3):
        ledger.capture_batch(payload(number))
    ledger.capture_batch(payload(1, "s2"))
    item = ledger.claim("w", batch=True)
    assert item["id"] in {i["id"] for i in items(ledger)}
    assert len(items(ledger)) == 2  # one per session
    ledger.finish(item["id"], item["lease_token"], [])
    other = ledger.claim("w", batch=True)
    ledger.finish(other["id"], other["lease_token"], [])
    assert marks(ledger) == {"s1": 3, "s2": 1}
    assert [(j["status"], j["last_error"]) for j in jobs(ledger) if not j["item"]] == [
        ("cancelled", "batch"),
        ("cancelled", "batch"),
    ]
    # A new capture makes a capture job with earlier context ...
    new = ledger.capture_batch(payload(4))["job_id"]
    assert [s["seq"] for s in ledger._job_sources(new)] == [1, 2, 3, 4]
    # ... but the batch item reads only the new text.
    item = ledger.claim("w", batch=True)
    assert [s["seq"] for s in item["sources"]] == [4]
    ledger.finish(item["id"], item["lease_token"], [])
    assert marks(ledger)["s1"] == 4
    assert ledger.conn.execute(
        "SELECT status,last_error FROM learning_jobs WHERE id=?", (new,)
    ).fetchone()[:] == ("cancelled", "batch")
    assert ledger.claim("w", batch=True) is None


def test_a_capture_during_the_item_run_waits_for_the_next_item(ledger):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    ledger.capture_batch(payload(1, "s2"))
    item = ledger.claim("w", batch=True)
    planned = {j["session_id"]: j["id"] for j in items(ledger)}
    assert len(planned) == 2 and item["id"] in planned.values()
    # The open capture jobs take the new sources; the items keep their ranges,
    # the leased one and the one no worker has claimed yet.
    ledger.capture_batch(payload(3))
    ledger.capture_batch(payload(2, "s2"))
    assert [s["seq"] for s in ledger._job_sources(planned["s1"])] == [1, 2]
    assert [s["seq"] for s in ledger._job_sources(planned["s2"])] == [1]
    ledger.finish(item["id"], item["lease_token"], [])
    capture_jobs = {j["session_id"]: j for j in jobs(ledger) if not j["item"]}
    session = "s1" if item["id"] == planned["s1"] else "s2"
    newest = {"s1": 3, "s2": 2}[session]
    assert (capture_jobs[session]["status"], capture_jobs[session]["last_seq"]) == (
        "pending",
        newest,
    )
    while (item := ledger.claim("w", batch=True)) is not None:
        ledger.finish(item["id"], item["lease_token"], [])
    assert sorted((j["session_id"], j["first_seq"], j["last_seq"]) for j in items(ledger)) == [
        ("s1", 1, 2),
        ("s1", 3, 3),
        ("s2", 1, 1),
        ("s2", 2, 2),
    ]
    assert {j["status"] for j in jobs(ledger) if not j["item"]} == {"cancelled"}


def test_a_started_job_keeps_its_work_and_no_item_reads_its_text(ledger):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    started = ledger.claim("w")
    ledger.checkpoint(started["id"], started["lease_token"], {"next_window": 1, "decisions": []})
    ledger.defer(started["id"], started["lease_token"], retry_delay=0, error="StepAside")
    later = ledger.capture_batch(payload(3))["job_id"]
    assert [s["seq"] for s in ledger._job_sources(later)] == [1, 2, 3]
    # The started job is older, so it is claimed first; it is never superseded.
    first = ledger.claim("w", batch=True)
    assert first["id"] == started["id"] and first["progress"]["next_window"] == 1
    item = ledger.claim("w2", batch=True)
    assert [s["seq"] for s in item["sources"]] == [3]
    # The started job goes back to pending with its work before the item commits.
    ledger.defer(first["id"], first["lease_token"], retry_delay=0, error="StepAside")
    ledger.finish(item["id"], item["lease_token"], [])
    state = {j["id"]: (j["status"], j["last_error"]) for j in jobs(ledger)}
    assert state[started["id"]] == ("pending", "StepAside")
    assert state[later] == ("cancelled", "batch")
    resumed = ledger.claim("w", batch=True)
    assert resumed["id"] == started["id"] and resumed["progress"]["next_window"] == 1
    ledger.finish(resumed["id"], resumed["lease_token"], [])


def test_the_job_size_limit_splits_a_session_into_items(ledger, monkeypatch):
    monkeypatch.setattr(learning_ranges, "JOB_CHARS", 100)
    monkeypatch.setattr(learning_ranges, "CONTEXT_BYTES", 100)
    for number in (1, 2, 3):
        ledger.capture_batch(payload(number, text=chr(96 + number) * 60))
    seen = []
    while True:
        item = ledger.claim("w", batch=True)
        if item is None:
            break
        seen.append([s["seq"] for s in item["sources"]])
        ledger.finish(item["id"], item["lease_token"], [])
    assert seen == [[1], [2], [3]]
    assert marks(ledger) == {"s1": 3}
    assert {j["status"] for j in jobs(ledger) if not j["item"]} == {"cancelled"}


def test_each_client_of_a_session_has_its_own_watermark(ledger):
    ledger.capture_batch(payload(1, client="codex"))
    ledger.capture_batch(payload(1, client="claude"))
    while (item := ledger.claim("w", batch=True)) is not None:
        ledger.finish(item["id"], item["lease_token"], [])
    clients = {
        row[0]: row[1]
        for row in ledger.conn.execute(
            "SELECT source_client,last_seq FROM learning_session_watermarks"
        )
    }
    assert clients == {"codex": 1, "claude": 1}


def test_the_bulk_cancel_is_idempotent(ledger):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    ledger.capture_batch(payload(1, "s2"))
    key = (PROJECT, ROOT, "s1", "codex")
    with db.write_tx(ledger.conn):
        assert learning_batch.cancel_covered(ledger.conn, key, 1, time.time()) == 0
        assert learning_batch.cancel_covered(ledger.conn, key, 2, time.time()) == 1
        assert learning_batch.cancel_covered(ledger.conn, key, 2, time.time()) == 0
    by_session = {j["session_id"]: j["status"] for j in jobs(ledger)}
    assert by_session == {"s1": "cancelled", "s2": "pending"}
    # A commit run again on a done item changes nothing more.
    item = ledger.claim("w", batch=True)
    ledger.finish(item["id"], item["lease_token"], [])
    before = marks(ledger)
    with db.write_tx(ledger.conn):
        assert learning_batch.commit(ledger.conn, item["id"], time.time()) == 0
    assert marks(ledger) == before
    # The watermark never goes down.
    ledger.conn.execute("UPDATE learning_session_watermarks SET last_seq=9")
    with db.write_tx(ledger.conn):
        learning_batch.commit(ledger.conn, item["id"], time.time())
    assert marks(ledger) == {"s2": 9}


def test_the_bulk_cancel_rolls_back_with_a_failed_commit(ledger, monkeypatch):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    item = ledger.claim("w", batch=True)
    original = learning_batch.cancel_covered

    def fail_after(*args):
        assert original(*args) == 1
        raise RuntimeError("disk full")

    monkeypatch.setattr(learning_batch, "cancel_covered", fail_after)
    with pytest.raises(RuntimeError):
        ledger.finish(item["id"], item["lease_token"], [])
    assert marks(ledger) == {}
    state = {j["item"]: j["status"] for j in jobs(ledger)}
    assert state == {1: "leased", 0: "pending"}
    monkeypatch.setattr(learning_batch, "cancel_covered", original)
    ledger.finish(item["id"], item["lease_token"], [])
    assert marks(ledger) == {"s1": 2}
    assert {j["status"] for j in jobs(ledger) if not j["item"]} == {"cancelled"}


def test_an_exhausted_item_hands_the_session_back_and_is_not_planned_again(ledger):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    item = ledger.claim("w", batch=True, max_attempts=1)
    ledger.fail(item["id"], item["lease_token"], "Provider:invalid", max_attempts=1)
    assert (
        ledger.conn.execute(
            "SELECT status FROM learning_jobs WHERE id=?", (item["id"],)
        ).fetchone()[0]
        == "exhausted"
    )
    # The capture job runs as before; no new item repeats the failed range.
    job = ledger.claim("w", batch=True)
    assert job["id"] != item["id"] and not learning_batch.is_item(ledger.conn, job["id"])
    assert len(items(ledger)) == 1
    ledger.finish(job["id"], job["lease_token"], [])
    # An explicit retry runs the item again; its commit raises the watermark.
    ledger.retry(item["id"])
    again = ledger.claim("w", batch=True)
    assert again["id"] == item["id"]
    ledger.finish(again["id"], again["lease_token"], [])
    assert marks(ledger) == {"s1": 2}


def test_a_new_source_after_prune_still_numbers_above_the_watermark(ledger):
    ledger.capture_batch(payload(1))
    item = ledger.claim("w", batch=True)
    ledger.finish(item["id"], item["lease_token"], [])
    # Prune drops the old sources and ranges; the watermark row stays.
    ledger.conn.execute("DELETE FROM learning_job_ranges")
    ledger.conn.execute("DELETE FROM learning_sources")
    ledger.capture_batch(payload(2))
    assert ledger.conn.execute("SELECT seq FROM learning_sources").fetchone()[0] == 2
    item = ledger.claim("w", batch=True)
    assert [s["seq"] for s in item["sources"]] == [2]


def test_open_jobs_with_only_handled_text_are_cancelled_without_an_item(ledger):
    for number in (1, 2):
        ledger.capture_batch(payload(number))
    job = [j for j in jobs(ledger)][0]
    with db.write_tx(ledger.conn):
        ledger.conn.execute(
            "INSERT INTO learning_session_watermarks VALUES (?,?,?,?,?,?,?)",
            (PROJECT, ROOT, "s1", "codex", 2, "earlier", time.time()),
        )
    assert ledger.claim("w", batch=True) is None
    assert items(ledger) == []
    row = ledger.conn.execute(
        "SELECT status,last_error FROM learning_jobs WHERE id=?", (job["id"],)
    ).fetchone()
    assert tuple(row) == ("cancelled", "batch")


def test_legacy_and_derivation_jobs_are_not_touched(ledger):
    ledger.conn.execute(
        "INSERT INTO learning_sources (id,project,root,session_id,source_client,role,content,"
        "sha256,observed_at,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("legacy", PROJECT, ROOT, "s1", "codex", "user", "Old text.", "x", "2026", "{}", 1.0),
    )
    ledger.conn.execute(
        "INSERT INTO learning_batches VALUES ('b-l','h',?,?,'s1',0,1)", (PROJECT, ROOT)
    )
    ledger.conn.execute(
        "INSERT INTO learning_jobs (id,batch_id,project,root,session_id,created_at,updated_at) "
        "VALUES ('legacy-job','b-l',?,?,'s1',1,1)",
        (PROJECT, ROOT),
    )
    ledger.conn.execute("INSERT INTO learning_job_sources VALUES ('legacy-job','legacy',0)")
    job = ledger.claim("w", batch=True)
    assert job["id"] == "legacy-job" and items(ledger) == []


# --- the night window and the strict probe -------------------------------------


def test_no_item_is_planned_or_claimed_outside_the_window(tmp_path):
    store = FakeStore(tmp_path)
    with store.connection() as conn:
        LearningLedger(conn).capture_batch(payload(1))
    pool = manual_pool(store, clock=BEFORE)
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        assert ledger.claim("w", batch=True) is None
    assert rows(store, "SELECT * FROM learning_batch_items") == []
    pool.clock = lambda: INSIDE
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        item = ledger.claim("w", batch=True)
    assert item is not None and len(rows(store, "SELECT * FROM learning_batch_items")) == 1


def test_an_item_steps_aside_for_the_strict_probe(tmp_path):
    store = FakeStore(tmp_path)
    with store.connection() as conn:
        LearningLedger(conn).capture_batch(payload(1))
    pool = manual_pool(store, probe=SimpleNamespace(hold=lambda own: 5.0, strict=True))
    assert pool.step_aside() == 5.0  # the worker loop claims nothing
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        item = ledger.claim("w", batch=True)
        with pytest.raises(StepAside) as aside:
            reserve_one(ledger, item, pool.budget)
    assert aside.value.reason == "queue"
    assert rows(store, "SELECT 1 FROM learning_budget") == []


def test_the_worker_passes_the_batch_flag_only_when_on():
    calls = []

    class Ledger:
        def claim(self, *args, **kwargs):
            calls.append(kwargs)

    limits = BudgetLimits()
    LearningWorker(Ledger(), GatedProvider(), WorkerSettings(), limits).run_once()
    LearningWorker(Ledger(), GatedProvider(), WorkerSettings(), limits, batch=True).run_once()
    assert calls == [
        {"lease_seconds": 120, "max_attempts": 4},
        {"lease_seconds": 120, "max_attempts": 4, "batch": True},
    ]
    assert "batch" not in WorkerSettings.__dataclass_fields__


# --- migration and report ------------------------------------------------------


def test_the_migration_keeps_every_existing_row(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    every = db.load_migrations()
    monkeypatch.setattr(db, "load_migrations", lambda: every[:7])
    conn = db.open_db(path, create=True)
    ledger = LearningLedger.__new__(LearningLedger)
    ledger.conn = conn
    assert db.user_version(conn) == 7
    for number in (1, 2):
        conn.execute(
            "INSERT INTO learning_batches VALUES (?,?,?,?,?,0,1)",
            (f"b{number}", "h", PROJECT, ROOT, "s1"),
        )
        conn.execute(
            "INSERT INTO learning_jobs (id,batch_id,project,root,session_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,1,1)",
            (f"j{number}", f"b{number}", PROJECT, ROOT, "s1"),
        )
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM learning_jobs ORDER BY id")]
    conn.close()
    monkeypatch.setattr(db, "load_migrations", lambda: every)
    conn = db.open_db(path)
    try:
        assert db.user_version(conn) == len(every) >= 8
        assert [tuple(r) for r in conn.execute("SELECT * FROM learning_jobs ORDER BY id")] == before
        assert conn.execute("SELECT count(*) FROM learning_session_watermarks").fetchone()[0] == 0
    finally:
        conn.close()
    assert list((tmp_path / "backups").glob("noblivion-v7-*.db"))


def test_the_batch_report_counts_items_cancels_and_model_seconds(ledger):
    from noblivion.learning_batch_report import batch_report

    for number in (1, 2):
        ledger.capture_batch(payload(number))
    item = ledger.claim("w", batch=True)
    progress = {
        "next_window": 1,
        "decisions": [],
        "provider_metrics": {"calls": 3, "seconds": 90.5},
    }
    ledger.checkpoint(item["id"], item["lease_token"], progress)
    ledger.finish(item["id"], item["lease_token"], [])
    report = batch_report(ledger.conn)["batch"]
    assert report == {
        "items": {"done": 1},
        "cancelled_jobs": 1,
        "sessions_with_watermark": 1,
        "done_items": 1,
        "model_calls": 3,
        "model_seconds": 90.5,
        "model_seconds_per_done_item": 90.5,
    }
    assert batch_report(ledger.conn, project="other")["batch"]["items"] == {}
