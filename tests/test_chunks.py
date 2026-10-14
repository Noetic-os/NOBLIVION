# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chunk rows, the chunk ranking and the chunk fetch (NOBLIVION-103). Fictional data only."""

from __future__ import annotations

import hashlib

import pytest

from noblivion import chunking, db, embedding, ranking, redaction, rest
from store_helpers import FakeEmbedder, add_memory, change_memory, content

SECTIONS = {
    "Storage": "Snapshots of the object store stay for 45 days. " * 12,
    "Keys": "The signing key for tokens rotates every 90 days and two keys stay valid. " * 8,
    "Limits": "Every client may send 600 requests per minute before the gateway refuses. " * 8,
}


def long_body(sections: dict[str, str] | None = None) -> str:
    return "\n\n".join(
        f"## {name}\n{text.strip()}" for name, text in (sections or SECTIONS).items()
    )


def make_service(embedder, conn):
    settings = embedding.EmbeddingSettings(backend="fastembed", model=embedder.model)
    service = embedding.EmbeddingService(settings, factory=lambda *a: embedder)
    service.start(conn)
    return service


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def chunk_rows(conn, memory_id):
    return conn.execute(
        "SELECT id, chunk_index, heading, text, search_text, rev FROM memory_chunks "
        "WHERE parent_id = ? ORDER BY chunk_index",
        (memory_id,),
    ).fetchall()


# -- the schema and the migration -------------------------------------------------------


def make_old_store(path, memories):
    """A store at schema 4, as the release before the chunks left it: rows, no chunks."""
    conn = db.connect(path, create=True)
    for migration in db.load_migrations()[:4]:
        with db.write_tx(conn):
            for statement in db.split_statements(migration.sql):
                conn.execute(
                    statement, {"random_base": 1_000_000} if ":random_base" in statement else ()
                )
            seed = db._SEEDS.get(migration.version)
            if seed is not None:
                seed(conn)
            conn.execute(f"PRAGMA user_version = {migration.version:d}")
    assert db.user_version(conn) == 4
    with db.write_tx(conn):
        for name, content, state in memories:
            rev = db.bump_rev(conn)
            now = db.utc_now()
            conn.execute(
                "INSERT INTO memories (project, root, path, source_type, category, content, hash, "
                "labels, archived_at, deleted_at, rev, created_at, updated_at) "
                "VALUES ('claude_code', 'r', ?, 'claude_code_md', 'reference', ?, ?, '[]', ?, ?, "
                "?, ?, ?)",
                (
                    f"{name}.md",
                    content,
                    hashlib.sha256(content.encode()).hexdigest(),
                    now if state == "archived" else None,
                    now if state == "deleted" else None,
                    rev,
                    now,
                    now,
                ),
            )
    return conn


def stored(name, body):
    return f"# {name}\n\nA note about {name}\n\n[claude_code_md: {name}.md]\n\n{body}"


def test_migration_5_chunks_the_existing_rows_and_keeps_the_rows(tmp_path):
    path = tmp_path / "noblivion.db"
    old = make_old_store(
        path,
        [
            ("long_one", stored("long_one", long_body()), "live"),
            ("short_one", stored("short_one", "Run the checks."), "live"),
            ("old_one", stored("old_one", long_body()), "archived"),
            ("gone_one", stored("gone_one", long_body()), "deleted"),
        ],
    )
    before = {
        r[0]: tuple(r[1:])
        for r in old.execute("SELECT id, path, content, hash, rev FROM memories").fetchall()
    }
    rev_before = db.revisions(old)[0]
    old.close()

    conn = db.open_db(path)
    assert db.user_version(conn) == db.latest_version() >= 5
    assert list((tmp_path / "backups").glob("noblivion-v4-*.db"))  # a backup of the old schema
    rows = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM memories").fetchall()}
    # The rows are the same: id, path, content, hash. Only the revision moved.
    after = conn.execute("SELECT id, path, content, hash FROM memories").fetchall()
    assert {r[0]: tuple(r[1:]) for r in after} == {k: v[:3] for k, v in before.items()}
    # Every row has chunks, the live, the archived and the soft-deleted rows too.
    assert all(chunk_rows(conn, memory_id) for memory_id in rows.values())
    assert len(chunk_rows(conn, rows["long_one.md"])) == 4  # summary and 3 sections
    assert len(chunk_rows(conn, rows["short_one.md"])) == 1
    assert db.revisions(conn)[0] > rev_before  # a running ranking index reloads the rows
    assert db.get_meta(conn, db.CHUNKER_VERSION_KEY) == str(chunking.CHUNKER_VERSION)
    assert db.foreign_key_problems(conn) == []
    # The file-level row shape is unchanged: the marker line is in the row content.
    content = conn.execute("SELECT content FROM memories WHERE path = 'long_one.md'").fetchone()[0]
    assert "\n[claude_code_md: long_one.md]\n" in content
    conn.close()


def test_a_new_store_has_empty_chunk_tables_and_no_work_at_migration(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    assert conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0] == 0
    assert db.ensure_chunks(conn) == 0
    assert db.get_meta(conn, db.CHUNKER_VERSION_KEY) == str(chunking.CHUNKER_VERSION)


# -- keeping the chunks in step with the row ----------------------------------------------


def test_insert_and_rewrite_keep_the_chunks_in_step(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    first = chunk_rows(conn, memory_id)
    assert [r["heading"] for r in first] == ["", "Storage", "Keys", "Limits"]
    # Change one section: the other chunks keep their row id and their rev.
    edited = dict(SECTIONS, Keys="The signing key now rotates every 30 days. " * 10)
    with db.write_tx(conn):
        rev = db.bump_rev(conn)
        db.rewrite_memory(
            conn,
            memory_id,
            root="-proj-alpha",
            path="gateway.md",
            category="feedback",
            content=content("gateway", long_body(edited)),
            content_hash="1" * 64,
            labels="[]",
            rev=rev,
            now=db.utc_now(),
        )
    second = chunk_rows(conn, memory_id)
    assert [r["id"] for r in second] == [r["id"] for r in first]
    changed = [r["heading"] for a, r in zip(first, second, strict=True) if a["rev"] != r["rev"]]
    assert changed == ["Keys"]
    assert "30 days" in second[2]["text"] and second[3]["rev"] == first[3]["rev"]
    # A shorter text drops the chunks past its end.
    with db.write_tx(conn):
        db.rewrite_memory(
            conn,
            memory_id,
            root="-proj-alpha",
            path="gateway.md",
            category="feedback",
            content=content("gateway", "Short now."),
            content_hash="2" * 64,
            labels="[]",
            rev=db.bump_rev(conn),
            now=db.utc_now(),
        )
    assert len(chunk_rows(conn, memory_id)) == 1


def test_a_chunk_with_the_same_text_keeps_its_vector(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    vectors = conn.execute("SELECT chunk_id, blob FROM chunk_vectors").fetchall()
    assert len(vectors) == 4
    embedder.calls.clear()
    edited = dict(SECTIONS, Keys="The signing key now rotates every 30 days. " * 10)
    change_memory(conn, memory_id, long_body(edited))
    result = embedding.backfill(conn, embedder)
    assert result.chunks_embedded == 1  # the edited section only
    kept = {r[0]: r[1] for r in conn.execute("SELECT chunk_id, blob FROM chunk_vectors")}
    assert sum(1 for cid, blob in vectors if kept[cid] == blob) == 3


def test_the_purge_removes_the_chunks_and_their_vectors(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    embedding.backfill(conn, FakeEmbedder())
    assert conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == 4
    with db.write_tx(conn):
        db.soft_delete(conn, memory_id, rev=db.bump_rev(conn), now="2000-01-01T00:00:00.000000Z")
    assert db.purge_deleted(conn, 1) == 1
    assert conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == 0


def test_ensure_chunks_fills_a_row_that_has_none_and_stamps_it(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    with db.write_tx(conn):
        conn.execute("DELETE FROM memory_chunks")
    rev_before = db.revisions(conn)[0]
    assert db.ensure_chunks(conn) == 1
    assert len(chunk_rows(conn, memory_id)) == 4
    assert db.revisions(conn)[0] == rev_before + 1
    assert conn.execute("SELECT rev FROM memories WHERE id = ?", (memory_id,)).fetchone()[0] == (
        rev_before + 1
    )
    assert db.ensure_chunks(conn) == 0  # nothing left to do


def test_a_new_chunker_version_cuts_every_row_again(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    with db.write_tx(conn):
        db.set_meta(conn, db.CHUNKER_VERSION_KEY, "0")
        conn.execute("UPDATE memory_chunks SET text = 'stale', search_text = 'stale'")
    assert db.ensure_chunks(conn) == 1
    assert all(r["text"] != "stale" for r in chunk_rows(conn, memory_id))
    assert db.get_meta(conn, db.CHUNKER_VERSION_KEY) == str(chunking.CHUNKER_VERSION)


# -- ranking: the later section of a long file ----------------------------------------------


def long_file_pool(conn):
    """One long file with three topics, and short notes with the same words in them."""
    ids = {"long": add_memory(conn, "gateway", long_body())}
    for n, text in enumerate(
        ["snapshots of the cache", "token cost of the model", "a limit on the pager", "other"]
    ):
        ids[f"n{n}"] = add_memory(conn, f"note_{n}", text)
    return ids


def test_a_word_in_a_later_section_finds_the_file_and_names_the_chunk(conn):
    ids = long_file_pool(conn)
    index = ranking.RankIndex()
    index.refresh(conn)
    result = index.rank("signing key rotates", project="claude_code", top_k=5)
    top = result.hits[0]
    assert top.row.id == ids["long"]
    assert (top.row.chunk_index, top.row.heading) == (2, "Keys")
    assert "rotates every 90 days" in top.row.chunk_text
    assert top.rank == 1 and len({h.row.id for h in result.hits}) == len(result.hits)
    # The file row stays the file: its content holds the marker line.
    assert "[claude_code_md: gateway.md]" in top.row.content
    assert result.pool_size == 5  # files, not chunks


def test_one_hit_per_file_with_the_best_cosine_of_its_chunks(conn):
    ids = long_file_pool(conn)
    embedder = FakeEmbedder()
    service = make_service(embedder, conn)
    service.backfill(conn)
    result = ranking.Ranker(service).search(
        conn, "signing key rotates every 90 days", project="claude_code", top_k=5
    )
    hits = {h.row.id: h for h in result.hits}
    assert result.mode == "hybrid" and len(hits) == len(result.hits)
    top = hits[ids["long"]]
    assert top.row.heading == "Keys"
    # The score is the best cosine of any chunk of the file.
    query = embedding.normalize(embedder.embed_query("signing key rotates every 90 days"))
    best = 0.0
    for (blob,) in conn.execute(
        "SELECT v.blob FROM chunk_vectors v JOIN memory_chunks c ON c.id = v.chunk_id "
        "WHERE c.parent_id = ?",
        (ids["long"],),
    ):
        vector = embedding.blob_to_vector(blob)
        best = max(best, sum(a * b for a, b in zip(query, vector, strict=True)))
    assert top.score == pytest.approx(round(best, 4), abs=1e-4)


def test_a_row_with_no_chunk_rows_is_ranked_as_one_unit(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    for n in range(4):
        add_memory(conn, f"filler_{n}", f"filler text number {n}")
    with db.write_tx(conn):
        conn.execute("DELETE FROM memory_chunks WHERE parent_id = ?", (memory_id,))
        conn.execute("UPDATE memories SET rev = ? WHERE id = ?", (db.bump_rev(conn), memory_id))
    index = ranking.RankIndex()
    index.refresh(conn)
    hit = index.rank("signing key rotates", project="claude_code", top_k=3).hits[0]
    assert hit.row.id == memory_id and hit.row.chunk_id is None


def test_a_reload_follows_a_changed_file_and_a_new_chunk_vector(conn):
    ids = long_file_pool(conn)
    index = ranking.RankIndex()
    index.refresh(conn)
    assert index.rank("nightly backup window", project="claude_code", top_k=3).hits == []
    section = {"Backup": "The nightly backup window is two hours long. " * 6}
    change_memory(conn, ids["long"], long_body(dict(SECTIONS, **section)))
    index.refresh(conn)
    hit = index.rank("nightly backup window", project="claude_code", top_k=3).hits[0]
    assert (hit.row.id, hit.row.heading) == (ids["long"], "Backup")
    # A removed section leaves the pool.
    change_memory(conn, ids["long"], long_body(SECTIONS))
    index.refresh(conn)
    assert index.rank("nightly backup window", project="claude_code", top_k=3).hits == []


def test_a_vector_of_a_removed_chunk_is_dropped_from_the_index(conn):
    ids = long_file_pool(conn)
    embedder = FakeEmbedder()
    service = make_service(embedder, conn)
    service.backfill(conn)
    ranker = ranking.Ranker(service)
    ranker.search(conn, "signing key", project="claude_code", top_k=3)
    change_memory(conn, ids["long"], "Short now.")
    ranker.search(conn, "signing key", project="claude_code", top_k=3)
    snap = ranker.index._snap
    live = {c.id for chunks in snap.chunks.values() for c in chunks}
    assert set(snap.vectors) <= live


def test_pick_chunk_uses_the_keywords_and_the_cosine():
    texts = [(1, "alpha beta gamma"), (2, "delta epsilon"), (3, "zeta eta theta")]
    assert ranking.pick_chunk(texts, "epsilon") == 1
    assert ranking.pick_chunk(texts, "nothing like it") is None
    assert ranking.pick_chunk([], "epsilon") is None
    # With a vector and no keyword match, the cosine picks the chunk.
    vectors = {
        1: embedding.vector_to_blob([1.0, 0.0]),
        2: embedding.vector_to_blob([0.0, 1.0]),
        3: embedding.vector_to_blob([-1.0, 0.0]),
    }
    assert ranking.pick_chunk(texts, "unmatched words", vectors, [0.1, 0.9]) == 1


# -- the REST shapes --------------------------------------------------------------------------


def fetch(conn, memory_id, **kwargs):
    return rest.fetch_answer(conn, str(memory_id), "claude_code", **kwargs)


def test_the_index_row_names_the_chunk_and_keeps_its_old_fields(conn):
    ids = long_file_pool(conn)
    index = ranking.RankIndex()
    index.refresh(conn)
    result = index.rank("signing key rotates", project="claude_code", top_k=3)
    answer = rest.index_answer(result, "claude_code", query="x", mode="keyword")
    row = answer["results"][0]
    assert (row["id"], row["chunk"], row["heading"]) == (ids["long"], 2, "Keys")
    assert row["source"] == "gateway.md" and row["title"] == "gateway"
    assert row["source_type"] == db.SOURCE_MD


def test_a_short_memory_is_fetched_whole_as_before(conn):
    memory_id = add_memory(conn, "short_one", "Run the checks first.")
    answer = fetch(conn, memory_id)
    assert set(answer) == {"namespace", "id", "title", "source", "text", "reason"}
    assert "[claude_code_md: short_one.md]" in answer["text"] and "Run the checks" in answer["text"]


def test_a_long_memory_comes_back_as_its_summary_and_the_list_of_sections(conn):
    body = long_body({name: text * 4 for name, text in SECTIONS.items()})
    memory_id = add_memory(conn, "gateway", body)
    assert len(content("gateway", body)) > rest.FETCH_WHOLE_CHARS
    answer = fetch(conn, memory_id)
    assert answer["reason"] is None and answer["title"] == "gateway"
    assert answer["text"].startswith("# gateway\n\nSections of this memory")
    assert "Storage" in answer["text"] and "Keys" in answer["text"] and "chunk=" in answer["text"]
    assert "rotates every 90 days" not in answer["text"]  # the sections are not in the summary
    assert [c["heading"] for c in answer["chunks"]][:3] == ["", "Storage", "Storage"]
    assert all({"chunk", "heading", "chars"} == set(c) for c in answer["chunks"])


def test_a_chunk_number_returns_that_chunk_and_its_heading(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    answer = fetch(conn, memory_id, chunk="2")
    assert answer["reason"] is None
    assert (answer["chunk"], answer["heading"]) == (2, "Keys")
    assert answer["text"].startswith("## Keys\n") and "rotates every 90 days" in answer["text"]
    assert "[claude_code_md" not in answer["text"] and "# gateway" not in answer["text"]
    assert answer["source"] == "gateway.md" and answer["title"] == "gateway"
    assert fetch(conn, memory_id, chunk="0")["text"].startswith("# gateway")


@pytest.mark.parametrize(
    ("chunk", "reason"),
    [("x", rest.BAD_CHUNK_REASON), ("-1", rest.BAD_CHUNK_REASON), ("99", rest.NO_CHUNK_REASON)],
)
def test_a_bad_chunk_number_is_refused_with_a_reason(conn, chunk, reason):
    memory_id = add_memory(conn, "gateway", long_body())
    answer = fetch(conn, memory_id, chunk=chunk)
    assert answer["reason"] == reason and answer["text"] == ""


def test_a_query_returns_the_section_that_matches(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    answer = fetch(conn, memory_id, query="how many requests per minute")
    assert (answer["chunk"], answer["heading"]) == (3, "Limits")
    assert "600 requests per minute" in answer["text"]
    # No word matches: the answer is the same as for no query, and names no chunk.
    miss = fetch(conn, memory_id, query="zebra giraffe")
    assert "chunk" not in miss and miss["reason"] is None


def test_a_query_uses_the_chunk_vectors_when_it_has_a_vector(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    # No keyword of the query is in the file; the vector of the Keys section is the closest.
    target = conn.execute(
        "SELECT search_text FROM memory_chunks WHERE parent_id = ? AND heading = 'Keys'",
        (memory_id,),
    ).fetchone()[0]
    answer = fetch(
        conn,
        memory_id,
        query="zebra giraffe",
        query_vector=embedder.embed_query(target),
        model_id=embedder.model_id,
    )
    assert answer["heading"] == "Keys"


def test_a_fetched_chunk_is_redacted_like_the_whole_text(conn):
    secret = "sk-ant-" + "a1b2c3d4e5f6" * 4
    body = long_body(dict(SECTIONS, Keys=f"The deploy token is {secret} and it rotates. " * 8))
    memory_id = add_memory(conn, "gateway", body)
    answer = fetch(conn, memory_id, chunk="2")
    assert secret not in answer["text"]
    assert redaction.redact_at_rest(answer["text"]) == answer["text"]


def test_a_search_entry_of_a_large_file_shows_the_header_and_the_best_chunk(conn):
    body = long_body({name: text * 4 for name, text in SECTIONS.items()})
    ids = {"long": add_memory(conn, "gateway", body)}
    for n in range(4):
        add_memory(conn, f"note_{n}", f"filler note {n}")
    index = ranking.RankIndex()
    index.refresh(conn)
    result = index.rank("signing key rotates", project="claude_code", top_k=3)
    answer = rest.search_answer(result, "claude_code", "m")
    text = answer["results"][0]
    assert text.startswith("# gateway\n\n[claude_code_md: gateway.md]")
    assert "rotates every 90 days" in text and "## Storage" not in text
    assert len(text) < len(content("gateway", body)) / 2
    assert result.hits[0].row.id == ids["long"]


def test_a_search_entry_of_a_small_file_is_the_whole_row(conn):
    memory_id = add_memory(conn, "gateway", long_body())
    for n in range(4):
        add_memory(conn, f"note_{n}", f"filler note {n}")
    index = ranking.RankIndex()
    index.refresh(conn)
    result = index.rank("signing key rotates", project="claude_code", top_k=3)
    text = rest.search_answer(result, "claude_code", "m")["results"][0]
    row = conn.execute("SELECT content FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
    assert text == row
