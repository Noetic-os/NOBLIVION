# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranking (design doc 0001, section 8) and the RAM index reload (section 6.3)."""

from __future__ import annotations

import time

import pytest

from noblivion import bm25, db, embedding, ranking
from store_helpers import FakeEmbedder, add_memory, change_memory, soft_delete


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def row(memory_id: int, weight: float = 1.0) -> ranking.PoolRow:
    return ranking.PoolRow(
        memory_id, "claude_code", "r", f"m{memory_id}.md", "claude_code_md", "", weight, ()
    )


# -- rank_pool: fusion order -----------------------------------------------------


def test_fusion_order_hand_computed():
    # cosine order: 10, 20, 30.  BM25 order: 20, 30, 10.
    # fused 10 = 1/61 + 1/63 = 0.032266
    # fused 20 = 1/62 + 1/61 = 0.032522
    # fused 30 = 1/63 + 1/62 = 0.032002
    # So BM25 lifts 20 above 10, and 30 stays last.
    pool = [row(10), row(20), row(30)]
    result = ranking.rank_pool(pool, [0.0, 2.0, 1.0], {0: 0.9, 1: 0.5, 2: 0.1}, top_k=5)
    assert result.mode == "hybrid"
    assert [h.row.id for h in result.hits] == [20, 10, 30]
    assert [h.rank for h in result.hits] == [1, 2, 3]
    assert result.hits[0].fusion_score == round(1 / 62 + 1 / 61, 6)
    assert result.hits[1].fusion_score == round(1 / 61 + 1 / 63, 6)
    assert result.hits[2].fusion_score == round(1 / 63 + 1 / 62, 6)
    assert [h.score for h in result.hits] == [0.5, 0.9, 0.1]


def test_weight_multiplies_the_cosine():
    pool = [row(1), row(2, weight=2.0)]
    result = ranking.rank_pool(pool, [0.0, 0.0], {0: 0.6, 1: 0.4}, top_k=5)
    assert [h.row.id for h in result.hits] == [2, 1]
    assert [h.score for h in result.hits] == [0.8, 0.6]


def test_all_zero_bm25_list_is_skipped():
    pool = [row(1), row(2)]
    result = ranking.rank_pool(pool, [0.0, 0.0], {0: 0.3, 1: 0.7}, top_k=5)
    assert [h.row.id for h in result.hits] == [2, 1]
    assert [h.fusion_score for h in result.hits] == [round(1 / 61, 6), round(1 / 62, 6)]


def test_ties_break_on_the_memory_id():
    pool = [row(9), row(4)]
    result = ranking.rank_pool(pool, [1.0, 1.0], {0: 0.5, 1: 0.5}, top_k=5)
    assert [h.row.id for h in result.hits] == [4, 9]


def test_floor_keeps_every_row_in_the_cosine_list_and_top_k_cuts():
    # Section 8.3 step 4 never drops a row; a low cosine is still returned.
    pool = [row(1), row(2), row(3)]
    result = ranking.rank_pool(pool, [0.0, 0.0, 0.0], {0: -0.5, 1: 0.01, 2: 0.05}, top_k=5)
    assert [h.row.id for h in result.hits] == [3, 2, 1]
    assert len(ranking.rank_pool(pool, [0.0] * 3, {0: 0.1, 1: 0.2, 2: 0.3}, 2).hits) == 2


def test_a_row_without_a_vector_ranks_on_bm25_only_with_a_null_score():
    pool = [row(1), row(2)]
    result = ranking.rank_pool(pool, [0.0, 3.0], {0: 0.9}, top_k=5)
    by_id = {h.row.id: h for h in result.hits}
    assert by_id[2].score is None and by_id[2].fusion_score == round(1 / 61, 6)
    assert by_id[1].score == 0.9


def test_keyword_mode_drops_zero_rows_and_reports_null_score():
    pool = [row(1), row(2), row(3), row(4)]
    result = ranking.rank_pool(pool, [0.5, 0.0, 1.5, -0.1], None, top_k=5)
    assert result.mode == "keyword"
    assert [h.row.id for h in result.hits] == [3, 1, 4]
    result = ranking.rank_pool(pool, [0.5, 0.0, 1.5, -0.1], None, top_k=2)
    assert [h.score for h in result.hits] == [None, None]
    assert [h.fusion_score for h in result.hits] == [round(1 / 61, 6), round(1 / 62, 6)]


# -- RankIndex over the database ----------------------------------------------------


def seed(conn):
    ids = {
        "tests": add_memory(conn, "tests_use_venv", "Run pytest with the project venv python."),
        "deploy": add_memory(conn, "deploy_window", "Deploy only after the nightly backup."),
        "other_root": add_memory(conn, "venv_beta", "pytest venv rule for beta", root="-proj-b"),
        "mined": add_memory(
            conn, "mined_one", "pytest venv seen in a transcript", source_type=db.SOURCE_MINED
        ),
        "other_ns": add_memory(conn, "ns_row", "pytest venv elsewhere", project="other"),
    }
    for n, text in enumerate(["coffee filter brand", "garden hose length", "bike tyre size"]):
        add_memory(conn, f"filler_{n}", text)
    gone = add_memory(conn, "gone_row", "pytest venv deleted")
    soft_delete(conn, gone)
    ids["gone"] = gone
    return ids


def ranked_ids(result):
    return [h.row.id for h in result.hits]


def test_pool_filters(conn):
    ids = seed(conn)
    index = ranking.RankIndex()
    index.refresh(conn)
    everything = index.rank("pytest venv", project="claude_code", top_k=50)
    assert everything.mode == "keyword"
    assert set(ranked_ids(everything)) == {ids["tests"], ids["other_root"]}
    mined = index.rank("pytest venv", project="claude_code", top_k=50, include_mined=True)
    assert ids["mined"] in ranked_ids(mined)
    scoped = index.rank("pytest venv", project="claude_code", top_k=50, root="-proj-alpha")
    assert ranked_ids(scoped) == [ids["tests"]]
    shared = index.rank(
        "pytest venv", project="claude_code", top_k=50, root="-proj-alpha", shared_roots=["-proj-b"]
    )
    assert set(ranked_ids(shared)) == {ids["tests"], ids["other_root"]}


def test_pool_filter_runs_before_scoring(conn):
    # A row in another root must not change BM25 scores inside this root.
    add_memory(conn, "a", "alpha beta", root="r1")
    add_memory(conn, "b", "alpha gamma", root="r1")
    index = ranking.RankIndex()
    index.refresh(conn)
    before = index.rank("alpha", project="claude_code", top_k=5, root="r1")
    add_memory(conn, "c", "alpha alpha alpha", root="r2")
    add_memory(conn, "d", "alpha delta", root="r2")
    index.refresh(conn)
    after = index.rank("alpha", project="claude_code", top_k=5, root="r1")
    assert [(h.row.id, h.bm25) for h in before.hits] == [(h.row.id, h.bm25) for h in after.hits]


def test_empty_query_and_empty_pool(conn):
    index = ranking.RankIndex()
    index.refresh(conn)
    assert index.rank("pytest", project="claude_code", top_k=5).hits == []
    add_memory(conn, "a", "alpha")
    index.refresh(conn)
    assert index.rank("   ", project="claude_code", top_k=5).hits == []


def test_reload_on_revision_change_from_another_connection(conn, tmp_path):
    for n, text in enumerate(["coffee filter brand", "garden hose length", "bike tyre size"]):
        add_memory(conn, f"filler_{n}", text)
    index = ranking.RankIndex()
    index.refresh(conn)
    assert index.refresh(conn) is False
    other = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    try:
        new_id = add_memory(other, "fresh", "zebra crossing rule")
        # Set an older clock on purpose: the reload must not depend on time.
        other.execute(
            "UPDATE memories SET updated_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
            (new_id,),
        )
    finally:
        other.close()
    assert index.refresh(conn) is True
    assert ranked_ids(index.rank("zebra", project="claude_code", top_k=5)) == [new_id]
    change_memory(conn, new_id, "giraffe rule now")
    index.refresh(conn)
    assert index.rank("zebra", project="claude_code", top_k=5).hits == []
    assert ranked_ids(index.rank("giraffe", project="claude_code", top_k=5)) == [new_id]
    soft_delete(conn, new_id)
    index.refresh(conn)
    assert index.rank("giraffe", project="claude_code", top_k=5).hits == []


def test_a_vector_change_does_not_rebuild_bm25(conn):
    add_memory(conn, "a", "alpha beta")
    embedder = FakeEmbedder()
    index = ranking.RankIndex(embedder.model_id)
    index.refresh(conn)
    index.rank("alpha", project="claude_code", top_k=5)
    assert index.bm25_builds == 1
    embedding.backfill(conn, embedder)
    assert index.refresh(conn) is True
    result = index.rank("alpha", project="claude_code", top_k=5, query_vector=[1.0] * 16)
    assert result.mode == "hybrid"
    assert index.bm25_builds == 1
    add_memory(conn, "b", "beta gamma")
    index.refresh(conn)
    index.rank("alpha", project="claude_code", top_k=5)
    assert index.bm25_builds == 2


# -- Ranker: hybrid, keyword fallback, re-embed ----------------------------------------


def make_service(embedder, conn):
    settings = embedding.EmbeddingSettings(backend="fastembed", model=embedder.model)
    service = embedding.EmbeddingService(settings, factory=lambda *a: embedder)
    service.start(conn)
    return service


def test_hybrid_search_with_a_fake_model(conn):
    ids = seed(conn)
    embedder = FakeEmbedder()
    service = make_service(embedder, conn)
    service.backfill(conn)
    ranker = ranking.Ranker(service)
    result = ranker.search(conn, "pytest venv python", project="claude_code", top_k=3)
    assert result.mode == "hybrid"
    assert result.hits[0].row.id == ids["tests"]
    assert all(h.score is not None for h in result.hits)
    assert embedder.queries == ["pytest venv python"]


def test_keyword_only_while_no_model_is_available(conn):
    ids = seed(conn)
    settings = embedding.EmbeddingSettings(backend="none")
    service = embedding.EmbeddingService(settings)
    service.start(conn)
    assert service.state == "none" and service.model_id is None
    result = ranking.Ranker(service).search(conn, "deploy backup", project="claude_code", top_k=5)
    assert result.mode == "keyword"
    assert ranked_ids(result) == [ids["deploy"]]
    assert result.hits[0].score is None
    assert ranking.Ranker(None).search(conn, "deploy", project="claude_code", top_k=5).mode == (
        "keyword"
    )


def test_keyword_only_when_the_model_failed_to_load(conn):
    seed(conn)

    def broken(*_args):
        raise embedding.EmbeddingError("no model files")

    settings = embedding.EmbeddingSettings()
    service = embedding.EmbeddingService(settings, factory=broken)
    assert service.start(conn) == "failed"
    result = ranking.Ranker(service).search(conn, "deploy", project="claude_code", top_k=5)
    assert result.mode == "keyword"


def test_keyword_only_without_numpy(conn, monkeypatch):
    seed(conn)
    embedder = FakeEmbedder()
    service = make_service(embedder, conn)
    service.backfill(conn)
    monkeypatch.setattr(ranking, "np", None)
    result = ranking.Ranker(service).search(conn, "deploy", project="claude_code", top_k=5)
    assert result.mode == "keyword"
    assert embedder.queries == []


def test_keyword_only_when_the_query_embed_is_too_slow(conn):
    seed(conn)

    class Slow(FakeEmbedder):
        def embed_query(self, text):
            time.sleep(0.5)
            return super().embed_query(text)

    service = make_service(Slow(), conn)
    service.backfill(conn)
    ranker = ranking.Ranker(service, query_timeout_s=0.05)
    assert ranker.search(conn, "deploy", project="claude_code", top_k=5).mode == "keyword"
    service.close()


def test_reembed_uses_new_vectors_only_and_bm25_for_the_rest(conn):
    ids = seed(conn)
    old = FakeEmbedder("old-model")
    service = make_service(old, conn)
    service.backfill(conn)
    # The model changes: only one row has a vector of the new model so far.
    new = FakeEmbedder("new-model")
    service2 = make_service(new, conn)
    assert service2.state == "reembedding"
    embedding._write_vectors(
        conn, new.model_id, [(ids["deploy"], "x", new.embed_documents(["deploy backup"])[0])]
    )
    result = ranking.Ranker(service2).search(
        conn, "pytest venv deploy", project="claude_code", top_k=5
    )
    assert result.mode == "hybrid"
    by_id = {h.row.id: h for h in result.hits}
    assert by_id[ids["deploy"]].score is not None
    assert by_id[ids["tests"]].score is None  # BM25 only until it is re-embedded
    service2.backfill(conn)
    assert service2.state == "ready"
    result = ranking.Ranker(service2).search(conn, "pytest venv", project="claude_code", top_k=5)
    assert all(h.score is not None for h in result.hits)


def test_ranker_switches_the_index_model(conn):
    seed(conn)
    a = FakeEmbedder("model-a")
    service = make_service(a, conn)
    service.backfill(conn)
    ranker = ranking.Ranker(service)
    assert ranker.search(conn, "deploy", project="claude_code", top_k=5).mode == "hybrid"
    b = FakeEmbedder("model-b")
    service.embedder = b  # as a restart with a new model would
    service.state = "reembedding"
    # No vectors for model-b yet: the cosine leg is empty, so keyword mode.
    assert ranker.search(conn, "deploy", project="claude_code", top_k=5).mode == "keyword"
    assert ranker.index.model_id == b.model_id


def test_tokenizer_matches_the_store_leg():
    assert bm25.tokenize("tests_use_venv") == ["tests_use_venv"]
