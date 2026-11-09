# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service backfill reuses only a complete, correctly scoped revision checkpoint."""

import random
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest

from noblivion import chunking, db, embedding
from store_helpers import FakeEmbedder, add_memory, change_memory


@pytest.fixture
def conn(tmp_path):
    conn = db.open_db(tmp_path / "memory.db", create=True)
    yield conn
    conn.close()


@pytest.fixture
def service(conn):
    service = embedding.EmbeddingService(
        embedding.EmbeddingSettings(backend="fake", model="fake-model"),
        factory=lambda *args: FakeEmbedder(),
    )
    service.start(conn)
    yield service
    service.close()


def stored_vectors(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in ("vectors", "chunk_vectors")
    }


def assert_current_vectors(conn):
    for content, digest in conn.execute(
        "SELECT m.content, v.content_hash FROM memories m JOIN vectors v ON v.memory_id = m.id"
    ):
        assert digest == embedding.text_hash(embedding.embed_text(content))
    for text, digest in conn.execute(
        "SELECT c.search_text, v.content_hash FROM memory_chunks c "
        "JOIN chunk_vectors v ON v.chunk_id = c.id"
    ):
        assert digest == embedding.text_hash(text.strip()[: embedding.MAX_EMBED_CHARS])


def test_unchanged_service_pass_reads_and_hashes_no_text(conn, service):
    for i in range(100):
        add_memory(conn, f"note{i}", f"orchard pruning calendar {i}")
    assert service.backfill(conn).embedded == 100
    before = stored_vectors(conn)
    service.embedder.calls.clear()
    reads = []

    def observe(cursor, row):
        reads.extend(name for name, *_ in cursor.description if name in {"content", "search_text"})
        return sqlite3.Row(cursor, row)

    conn.row_factory = observe
    try:
        with patch.object(embedding, "text_hash", wraps=embedding.text_hash) as digest:
            actual = service.backfill(conn)
            assert digest.call_count == 0
        assert reads == []
        # The conservative one-shot path is the same workload and result.
        with patch.object(embedding, "text_hash", wraps=embedding.text_hash) as digest:
            expected = embedding.backfill(conn, service.embedder)
            assert digest.call_count == 200
        assert len(reads) == 200
    finally:
        conn.row_factory = sqlite3.Row
    assert actual == expected == embedding.BackfillResult(0, 0, 0, False)
    assert service.embedder.calls == []
    assert stored_vectors(conn) == before


def test_changed_section_preserves_other_chunks_and_namespaces(conn, service):
    body = "## Orchard\n" + "prune trees in spring. " * 40 + "\n## Pond\n" + "feed fish. " * 70
    changed = add_memory(conn, "garden", body, project="alpha")
    untouched = add_memory(conn, "garden", body, project="beta")
    service.backfill(conn)
    before = stored_vectors(conn)
    change_memory(conn, changed, body.replace("feed fish.", "count fish."))
    service.embedder.calls.clear()
    result = service.backfill(conn)
    assert (result.embedded, result.chunks_embedded) == (1, 1)
    after = stored_vectors(conn)
    assert [row for row in before["vectors"] if row[0] == untouched] == [
        row for row in after["vectors"] if row[0] == untouched
    ]
    assert (
        sum(a != b for a, b in zip(before["chunk_vectors"], after["chunk_vectors"], strict=True))
        == 1
    )
    assert_current_vectors(conn)
    assert embedding.backfill(conn, service.embedder) == embedding.BackfillResult(0, 0, 0, False)


@pytest.mark.parametrize("table", ["vectors", "chunk_vectors"])
def test_missing_vectors_are_repaired_without_a_content_revision(conn, service, table):
    add_memory(conn, "orchard", "prune the trees")
    service.backfill(conn)
    revision = db.revisions(conn)[0]
    with db.write_tx(conn):
        conn.execute(f"DELETE FROM {table}")
    result = service.backfill(conn)
    assert db.revisions(conn)[0] == revision
    assert (result.embedded, result.chunks_embedded) == ((1, 0) if table == "vectors" else (0, 1))
    assert_current_vectors(conn)


def test_stale_vectors_retry_after_a_partial_failure_without_a_new_revision(conn, service):
    first = add_memory(conn, "first", "old text")
    second = add_memory(conn, "second", "old text")
    service.backfill(conn)
    change_memory(conn, first, "fresh fruit")
    change_memory(conn, second, "poison fruit")
    service.embedder.fail_on = "poison"
    result = service.backfill(conn, batch_size=1)
    assert (result.embedded, result.failed, result.chunks_embedded, result.chunks_failed) == (
        1,
        1,
        1,
        1,
    )
    revision = db.revisions(conn)[0]
    service.embedder.fail_on = ""
    service.embedder.calls.clear()
    result = service.backfill(conn)
    assert db.revisions(conn)[0] == revision
    assert (result.embedded, result.chunks_embedded) == (1, 1)
    assert len(service.embedder.calls) == 1
    assert "poison" in service.embedder.calls[0][0]
    assert_current_vectors(conn)


def test_an_edit_during_a_pass_remains_eligible(conn, service, monkeypatch):
    memory_id = add_memory(conn, "orchard", "old text")
    service.backfill(conn)
    change_memory(conn, memory_id, "middle text")
    send = service.embedder.embed_documents

    def edit_while_embedding(texts):
        change_memory(conn, memory_id, "newest text")
        return send(texts)

    with monkeypatch.context() as edit:
        edit.setattr(service.embedder, "embed_documents", edit_while_embedding)
        assert service.backfill(conn).embedded == 0
    result = service.backfill(conn)
    assert result.embedded == 1
    assert_current_vectors(conn)


def test_an_interrupted_pass_keeps_successful_batches_and_retries_stale_rows(
    conn, service, monkeypatch
):
    ids = [add_memory(conn, f"note{i}", "old fruit") for i in range(3)]
    service.backfill(conn)
    for memory_id in ids:
        change_memory(conn, memory_id, "new fruit")
    send = service.embedder.embed_documents
    calls = 0

    def interrupt_second_batch(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return send(texts)

    with monkeypatch.context() as interrupt:
        interrupt.setattr(service.embedder, "embed_documents", interrupt_second_batch)
        with pytest.raises(KeyboardInterrupt):
            service.backfill(conn, batch_size=1)
    result = service.backfill(conn, batch_size=1)
    assert (result.embedded, result.chunks_embedded) == (2, 3)
    assert_current_vectors(conn)


def test_checkpoint_does_not_cross_connections_or_models(conn, service, tmp_path):
    for i in range(3):
        add_memory(conn, f"note{i}", "orchard")
    service.backfill(conn)
    other = db.open_db(tmp_path / "other.db", create=True)
    try:
        memory_id = add_memory(other, "different", "old text")
        embedding.backfill(other, FakeEmbedder())
        change_memory(other, memory_id, "new text")
        assert service.backfill(other).embedded == 1
        service.embedder.model = "new-model"
        assert service.backfill(other).embedded == 1
        assert {row[1] for row in stored_vectors(other)["vectors"]} == {"fake:new-model"}
        assert_current_vectors(other)
    finally:
        other.close()


def test_mined_policy_change_retries_existing_stale_vectors(conn, service):
    memory_id = add_memory(conn, "mined", "old text", source_type=db.SOURCE_MINED)
    service.backfill(conn)
    change_memory(conn, memory_id, "new text")
    service.embedder.remote = True
    embedding.grant_consent(conn, service.embedder.backend, service.embedder.model)
    service.settings = replace(service.settings, remote_include_mined=False)
    assert service.backfill(conn).skipped_mined == 1
    assert service.backfill(conn).skipped_mined == 1
    service.settings = replace(service.settings, remote_include_mined=True)
    assert service.backfill(conn).embedded == 1
    assert_current_vectors(conn)


def test_chunker_change_and_missing_chunks_are_repaired(conn, service, monkeypatch):
    add_memory(conn, "orchard", "pruning schedule. " * 40)
    service.backfill(conn)
    old_count = conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0]
    monkeypatch.setattr(chunking, "CHUNKER_VERSION", chunking.CHUNKER_VERSION + 1)
    split = chunking.split_content
    monkeypatch.setattr(
        chunking, "split_content", lambda content: split(content, cap=200, minimum=50)
    )
    assert service.backfill(conn).chunks_embedded > old_count
    assert_current_vectors(conn)
    with db.write_tx(conn):
        conn.execute("DELETE FROM memory_chunks")
    assert service.backfill(conn).chunks_embedded > 0
    assert_current_vectors(conn)


def test_consent_revocation_interrupts_a_warm_pass_and_new_consent_retries(
    conn, service, monkeypatch
):
    ids = [add_memory(conn, f"note{i}", "old fruit") for i in range(3)]
    remote = service.embedder
    remote.remote = True
    embedding.grant_consent(conn, remote.backend, remote.model)
    service.backfill(conn)
    for memory_id in ids:
        change_memory(conn, memory_id, "new fruit")
    remote.calls.clear()
    send = remote.embed_documents

    def revoke_after_one_batch(texts):
        result = send(texts)
        embedding.revoke_consent(conn)
        return result

    with monkeypatch.context() as revoke:
        revoke.setattr(remote, "embed_documents", revoke_after_one_batch)
        assert service.backfill(conn, batch_size=1) is None
    assert service.state == embedding.STATE_FAILED
    assert len(remote.calls) == 1
    assert service.backfill(conn) is None
    assert len(remote.calls) == 1
    embedding.grant_consent(conn, remote.backend, remote.model)
    monkeypatch.setattr(service, "_factory", lambda *args: remote)
    service.start(conn)
    result = service.backfill(conn, batch_size=1)
    assert (result.embedded, result.chunks_embedded) == (2, 3)
    assert_current_vectors(conn)


def test_incremental_results_match_full_scans_through_changes_and_retries(conn, service, tmp_path):
    """The one-shot path checks every hash and acts as the equivalence oracle."""
    ids = [
        add_memory(
            conn,
            f"note{i}",
            "prune trees. " * (150 if i % 2 else 5),
            source_type=db.SOURCE_MINED if i == 0 else db.SOURCE_MD,
        )
        for i in range(6)
    ]
    baseline = db.open_db(tmp_path / "baseline.db", create=True)
    conn.backup(baseline)
    reference = FakeEmbedder()
    rng = random.Random(17)
    include_mined = True
    try:
        for step in range(40):
            operation = rng.choice(
                ("edit", "missing", "archive", "model", "policy", "failure", "idle")
            )
            memory_id = rng.choice(ids)
            if operation == "edit":
                for database in (conn, baseline):
                    change_memory(database, memory_id, f"prune fruit {step}. " * 150)
            elif operation == "missing":
                for database in (conn, baseline):
                    with db.write_tx(database):
                        database.execute("DELETE FROM vectors WHERE memory_id = ?", (memory_id,))
                        database.execute(
                            "DELETE FROM chunk_vectors WHERE chunk_id IN "
                            "(SELECT id FROM memory_chunks WHERE parent_id = ?)",
                            (memory_id,),
                        )
            elif operation == "archive":
                for database in (conn, baseline):
                    with db.write_tx(database):
                        database.execute(
                            "UPDATE memories SET archived_at = CASE WHEN archived_at IS NULL "
                            "THEN '2000-01-01T00:00:00.000000Z' ELSE NULL END, "
                            "rev = ? WHERE id = ?",
                            (db.bump_rev(database), memory_id),
                        )
            elif operation == "model":
                service.embedder.model = reference.model = f"model-{step % 2}"
            elif operation == "policy":
                include_mined = not include_mined
            elif operation == "failure":
                service.embedder.fail_on = reference.fail_on = "" if reference.fail_on else "fruit"
            actual = service.backfill(conn, include_mined=include_mined, batch_size=2)
            expected = embedding.backfill(
                baseline, reference, include_mined=include_mined, batch_size=2
            )
            assert actual == expected, (step, operation)
            assert stored_vectors(conn) == stored_vectors(baseline), (step, operation)
        service.embedder.fail_on = reference.fail_on = ""
        assert service.backfill(conn) == embedding.backfill(baseline, reference)
        assert stored_vectors(conn) == stored_vectors(baseline)
    finally:
        baseline.close()


def test_an_empty_batch_does_not_mark_stale_vectors_complete(conn, service):
    memory_id = add_memory(conn, "orchard", "old text")
    service.backfill(conn)
    change_memory(conn, memory_id, "new text")
    assert service.backfill(conn, batch_size=0).embedded == 0
    assert service.backfill(conn).embedded == 1
    assert_current_vectors(conn)
