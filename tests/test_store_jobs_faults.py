# SPDX-License-Identifier: AGPL-3.0-or-later
"""Faults of the background jobs and the consent leftovers (NOBLIVION-63, NOBLIVION-77).

Fictional data only. No network: the remote backends are fakes on loopback.
"""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from noblivion import db, dedup, embedding, indexer, store
from store_helpers import FAKE_DIM, REMOTE_MODEL, FakeEmbedder, add_memory, fake_vector
from test_store import Running, fake_service, make_store, wait_for, write_memory


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def _count(run: Running, table: str) -> int:
    with closing(db.connect(run.store.settings.db_path)) as c:
        return int(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _messages(caplog, part: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if part in r.getMessage()]


# -- NOBLIVION-77 item 1: a revoke stops a running dedup plan ---------------------------


class _RevokingJudge:
    """Revokes the dedup consent during its first call, as a user would."""

    model = "fake/judge-model"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.calls = 0

    def judge(self, system: str, user: str) -> dedup.Verdict:
        self.calls += 1
        dedup.revoke_consent(self.conn)
        return dedup.parse_verdict(
            json.dumps({"verdict": "MERGE", "survivor": "A", "reason": "same lesson"})
        )


def test_a_revoke_during_a_plan_sends_no_further_pair(tmp_path, conn):
    folder = tmp_path / "memory"
    folder.mkdir()
    for n in range(6):
        (folder / f"note_{n}.md").write_text(f"lesson number {n}\n", encoding="utf-8")
    dedup.grant_consent(conn, _RevokingJudge.model)
    pairs = [
        dedup.Candidate("r", "feedback", f"note_{n}.md", f"note_{n + 1}.md", n, n + 1, 0.95)
        for n in range(5)
    ]
    judge = _RevokingJudge(conn)
    out = io.StringIO()
    plan = dedup.build_plan(
        pairs,
        {"r": folder},
        judge,
        dedup.load_prompt(),
        run_id="run-1",
        out=out,
        consent=lambda: dedup.has_consent(conn, judge.model),
    )
    assert judge.calls == 1
    assert len(plan["pairs"]) == 1  # the pair judged under the consent stays
    assert "consent" in plan["stopped"]
    assert "4 pairs not judged" in out.getvalue()


def test_a_dedup_consent_that_cannot_be_read_stops_the_plan(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    for name in ("a.md", "b.md"):
        (folder / name).write_text("same lesson\n", encoding="utf-8")

    def unreadable() -> bool:
        raise sqlite3.OperationalError("database is locked")

    class Judge:
        model = "fake/judge-model"
        calls = 0

        def judge(self, system, user):
            Judge.calls += 1
            raise AssertionError("a pair was sent without a known consent")

    plan = dedup.build_plan(
        [dedup.Candidate("r", "feedback", "a.md", "b.md", 1, 2, 0.95)],
        {"r": folder},
        Judge(),
        dedup.load_prompt(),
        run_id="run-2",
        out=io.StringIO(),
        consent=unreadable,
    )
    assert Judge.calls == 0 and plan["pairs"] == [] and plan["stopped"]


# -- NOBLIVION-77 items 2 and 4: the start of the embedding service -------------------


def _remote_settings() -> embedding.EmbeddingSettings:
    return embedding.EmbeddingSettings(backend="openrouter", model=REMOTE_MODEL)


def test_a_consent_read_error_at_start_does_not_wait_the_retry_hour(conn):
    """A start that failed on a consent read error starts again at the next
    tick when the read works, not one hour later."""
    now = [1000.0]
    fault = [True]
    embedding.grant_consent(conn, "openrouter", REMOTE_MODEL)

    def factory(settings, c, env):
        if fault[0]:
            raise sqlite3.OperationalError("database is locked")
        return FakeEmbedder(model=REMOTE_MODEL, backend="openrouter")

    service = embedding.EmbeddingService(_remote_settings(), factory=factory, clock=lambda: now[0])
    try:
        assert service.start(conn) == embedding.STATE_FAILED
        fault[0] = False
        now[0] += 1.0
        assert service.retry_due(conn) is True
        assert service.start(conn) in (embedding.STATE_READY, embedding.STATE_REEMBEDDING)
    finally:
        service.close()


def test_a_missing_consent_is_logged_once_not_once_per_retry_hour(conn, caplog):
    now = [1000.0]
    service = embedding.EmbeddingService(
        _remote_settings(), clock=lambda: now[0], env={"OPENROUTER_API_KEY": "test-key-not-real"}
    )
    try:
        with caplog.at_level("WARNING", logger="noblivion.store"):
            for _ in range(4):
                assert service.start(conn) == embedding.STATE_FAILED
                now[0] += embedding.RETRY_AFTER_S + 1
        assert len(_messages(caplog, "model load failed")) == 1
    finally:
        service.close()


# -- NOBLIVION-77 item 3 and NOBLIVION-63 item 1: the jobs loop goes on after a fault ---


def test_a_failed_backfill_runs_again_without_a_content_change(tmp_path, monkeypatch):
    """With ``index_interval_s=0`` a pass that raised is not done: it runs again."""
    monkeypatch.setattr(store, "JOB_RETRY_S", 0.0, raising=False)
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(folder, "lantern rule", "Light the lantern first.", "Lantern before dusk.")
    service = fake_service()
    real = service.backfill
    faults = [1]

    def flaky(c, **kwargs):
        if faults[0]:
            faults[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(c, **kwargs)

    service.backfill = flaky  # type: ignore[method-assign]
    run = Running(make_store(tmp_path, embedding_service=service))
    try:
        assert wait_for(lambda: faults[0] == 0)  # the first pass raised
        assert wait_for(lambda: _count(run, "vectors") == 1)  # and ran again
    finally:
        run.stop()


def test_one_fault_in_a_tick_does_not_end_the_jobs(tmp_path, monkeypatch, caplog):
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(folder, "lantern rule", "Light the lantern first.", "Lantern before dusk.")
    run = Running(make_store(tmp_path))
    jobs = next(t for t in run.store._threads if t.name == "noblivion-jobs")
    revisions = db.revisions
    fault = [False]

    def faulty(c):
        if fault[0] and threading.current_thread() is jobs:
            raise sqlite3.OperationalError("database is locked")
        return revisions(c)

    try:
        with caplog.at_level("INFO", logger="noblivion.store"):
            assert wait_for(lambda: _count(run, "vectors") == 1)
            monkeypatch.setattr(db, "revisions", faulty)
            fault[0] = True
            time.sleep(6 * store.JOB_TICK_S)
            assert jobs.is_alive(), "the background jobs ended"
            assert len(_messages(caplog, "tick failed")) == 1  # once per run of faults
            fault[0] = False
            write_memory(folder, "kettle rule", "Fill the kettle.", "Kettle before tea.")
            db_path = run.store.settings.db_path
            assert indexer.main(["--memory-dir", str(folder), "--db", str(db_path)]) == 0
            assert wait_for(lambda: _count(run, "vectors") == 2)
            assert jobs.is_alive()
            assert run.get("/health")[1]["index_state"] == store.INDEX_IDLE
    finally:
        run.stop()


def test_health_says_when_the_jobs_thread_ended(tmp_path, monkeypatch):
    st = make_store(tmp_path)

    def broken():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(st, "connection", broken)
    st._jobs()
    assert st.index_state == "stopped"


# -- NOBLIVION-63 item 2: a stalled remote stops the single-row fallback ---------------


class _StalledEmbedder(FakeEmbedder):
    def embed_documents(self, texts):
        self.calls.append(list(texts))
        raise embedding.EmbeddingError("POST failed: TimeoutError") from TimeoutError("timed out")


def test_a_stalled_backend_stops_the_single_row_fallback(conn):
    for n in range(10):
        add_memory(conn, f"note {n}", f"Lesson body number {n}.")
    embedder = _StalledEmbedder()
    result = embedding.backfill(conn, embedder, batch_size=5)
    # One batch call and one single-row call, then the pass stops.
    assert len(embedder.calls) == 2
    assert result.embedded == 0 and result.failed == 10


def test_a_read_timeout_of_the_remote_counts_as_a_timeout():
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)  # never accepts and never answers
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/api/embed"
        with pytest.raises(embedding.EmbeddingError) as caught:
            embedding._post_json(url, {"input": ["x"]}, {}, 0.2)
    assert embedding.timed_out(caught.value)


# -- NOBLIVION-63 item 3: the store answers while its jobs stop -------------------------


class _HeldEmbedder(FakeEmbedder):
    """Holds each document call until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()

    def embed_documents(self, texts):
        self.entered.set()
        self.gate.wait(20)
        return super().embed_documents(texts)


def test_the_store_answers_while_a_backfill_finishes_at_stop(tmp_path):
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(folder, "lantern rule", "Light the lantern first.", "Lantern before dusk.")
    embedder = _HeldEmbedder()
    run = Running(make_store(tmp_path, embedding_service=fake_service(embedder)))
    try:
        assert embedder.entered.wait(10)
        run.store.request_stop("test")
        time.sleep(0.5)  # the serve loop sees the stop
        status, body = run.get("/health")
        assert status == 200 and body["status"] == "ok"
    finally:
        embedder.gate.set()
        run.thread.join(15)
    assert run.exit_code == store.EXIT_OK


def test_a_stop_ends_the_backfill_between_batches(conn):
    for n in range(6):
        add_memory(conn, f"note {n}", f"Lesson body number {n}.")
    embedder = FakeEmbedder()

    def stop() -> bool:
        return bool(embedder.calls)  # True after the first batch

    result = embedding.backfill(conn, embedder, batch_size=2, stop=stop)
    assert len(embedder.calls) == 1
    assert result.embedded == 2 and not result.switched


# -- NOBLIVION-63 item 4: a new vector size under the same model name ------------------


class _SizedEmbedder(FakeEmbedder):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [fake_vector(t, self.dim) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return fake_vector(text, self.dim)


def _dims(conn: sqlite3.Connection) -> set[int]:
    return {
        int(r[0])
        for table in ("vectors", "chunk_vectors")
        for r in conn.execute(f"SELECT dim FROM {table}").fetchall()
    }


def _service(embedder: FakeEmbedder) -> embedding.EmbeddingService:
    settings = embedding.EmbeddingSettings(backend="fastembed", model=embedder.model)
    return embedding.EmbeddingService(settings, factory=lambda *a: embedder)


def test_a_new_vector_size_at_start_reembeds_every_row(conn):
    add_memory(conn, "lantern rule", "Lantern before dusk.")
    add_memory(conn, "kettle rule", "Kettle before tea.")
    old = _service(_SizedEmbedder(FAKE_DIM))
    old.start(conn)
    old.backfill(conn)
    old.close()
    assert _dims(conn) == {FAKE_DIM}

    new = _service(_SizedEmbedder(8))
    try:
        assert new.start(conn) == embedding.STATE_REEMBEDDING
        result = new.backfill(conn)
        assert result.embedded == 2
        assert _dims(conn) == {8}
        assert new.state == embedding.STATE_READY
        assert db.get_meta(conn, "embed_dim") == "8"
    finally:
        new.close()


def test_a_new_vector_size_seen_by_a_query_starts_a_reembed(conn):
    add_memory(conn, "lantern rule", "Lantern before dusk.")
    embedder = _SizedEmbedder(FAKE_DIM)
    service = _service(embedder)
    try:
        service.start(conn)
        service.backfill(conn)
        assert service.state == embedding.STATE_READY
        embedder.dim = 8  # the server now serves the model with another size
        assert service.embed_query("lantern", timeout_s=5.0, conn=conn) is not None
        assert service.state == embedding.STATE_REEMBEDDING
        service.backfill(conn)
        assert _dims(conn) == {8} and service.state == embedding.STATE_READY
    finally:
        service.close()


def test_the_store_keeps_the_vectors_of_a_finished_pass(tmp_path):
    # Guard for the new stop check: a store that is not stopping ends its pass.
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(folder, "lantern rule", "Light the lantern first.", "Lantern before dusk.")
    run = Running(make_store(tmp_path))
    try:
        assert wait_for(lambda: _count(run, "chunk_vectors") >= 1)
    finally:
        run.stop()
