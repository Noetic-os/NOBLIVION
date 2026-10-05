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
    # cosine order: 10, 20, 30.  BM25 order: 20, 30. Row 10 matches no query
    # term (BM25 0), so it is not in the BM25 list (NOBLIVION-49).
    # fused 10 = 1/61        = 0.016393
    # fused 20 = 1/62 + 1/61 = 0.032522
    # fused 30 = 1/63 + 1/62 = 0.032002
    # So the two rows that match a term are above 10.
    pool = [row(10), row(20), row(30)]
    result = ranking.rank_pool(pool, [0.0, 2.0, 1.0], {0: 0.9, 1: 0.5, 2: 0.1}, top_k=5)
    assert result.mode == "hybrid"
    assert [h.row.id for h in result.hits] == [20, 30, 10]
    assert [h.rank for h in result.hits] == [1, 2, 3]
    assert result.hits[0].fusion_score == round(1 / 62 + 1 / 61, 6)
    assert result.hits[1].fusion_score == round(1 / 63 + 1 / 62, 6)
    assert result.hits[2].fusion_score == round(1 / 61, 6)
    assert [h.score for h in result.hits] == [0.5, 0.1, 0.9]


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


def test_hybrid_bm25_list_holds_only_the_rows_that_match_a_query_term():
    # NOBLIVION-49. 200 rows, all with a vector, and only id 150 matches the
    # query. The ids 1-4 have the lowest cosines. A BM25 list over the whole
    # pool puts them at the BM25 ranks 2-5 (a tie of zeros breaks by id), and
    # that lifts them into the top 10.
    pool = [row(i) for i in range(1, 201)]
    scores = [5.0 if r.id == 150 else 0.0 for r in pool]
    cosines = {i: (0.001 * r.id if r.id <= 4 else 0.3 + 0.002 * r.id) for i, r in enumerate(pool)}
    result = ranking.rank_pool(pool, scores, cosines, top_k=10)
    assert [h.row.id for h in result.hits] == [150, 200, 199, 198, 197, 196, 195, 194, 193, 192]
    # A row that matches no term has the score of its cosine rank alone.
    assert result.hits[1].fusion_score == round(1 / 61, 6)


def test_hybrid_does_not_return_a_row_with_no_cosine_and_bm25_0():
    # NOBLIVION-49. 50 rows, the last 10 with a vector, as during a backfill.
    # Id 45 (with a vector) and id 7 (without) match the query.
    pool = [row(i) for i in range(1, 51)]
    scores = [3.0 if r.id == 45 else 2.0 if r.id == 7 else 0.0 for r in pool]
    cosines = {i: 0.3 + 0.01 * i for i in range(40, 50)}
    result = ranking.rank_pool(pool, scores, cosines, top_k=35)
    assert result.mode == "hybrid"
    assert [h for h in result.hits if h.score is None and h.bm25 == 0] == []
    assert sorted(h.row.id for h in result.hits) == [7, *range(41, 51)]
    by_id = {h.row.id: h for h in result.hits}
    # The matched row with no vector keeps its place: BM25 rank 2, null score.
    assert by_id[7].score is None and by_id[7].fusion_score == round(1 / 62, 6)


def test_hybrid_keeps_a_matched_row_with_a_negative_bm25():
    # A matched term can have a negative idf in a small pool (section 8.4), so
    # the test for the BM25 list is "not zero", as in keyword mode.
    pool = [row(1), row(2), row(3)]
    result = ranking.rank_pool(pool, [2.0, -0.1, 0.0], {0: 0.9}, top_k=5)
    assert [h.row.id for h in result.hits] == [1, 2]
    assert result.hits[1].score is None and result.hits[1].bm25 == -0.1


def test_hybrid_keeps_a_matched_row_with_a_bm25_of_0():
    # NOBLIVION-49. A query word in exactly half of the pool has an idf of 0,
    # so every BM25 score is 0. The rows 20 and 40 hold the word; 30 and 40
    # have no vector yet. Row 40 matches, so it is returned on the keyword
    # list alone. Row 30 has no vector and no match: it is in no list.
    pool = [row(10), row(20), row(30), row(40)]
    matched = [False, True, False, True]
    result = ranking.rank_pool(pool, [0.0] * 4, {0: 0.9, 1: 0.5}, top_k=5, matched=matched)
    assert result.mode == "hybrid"
    assert [h.row.id for h in result.hits] == [20, 10, 40]
    # fused 20 = 1/62 + 1/61, fused 10 = 1/61, fused 40 = 1/62
    assert [h.fusion_score for h in result.hits] == [
        round(1 / 62 + 1 / 61, 6),
        round(1 / 61, 6),
        round(1 / 62, 6),
    ]
    assert [h.score for h in result.hits] == [0.5, 0.9, None]
    assert [h.bm25 for h in result.hits] == [0.0, 0.0, 0.0]


def test_hybrid_ranks_a_matched_row_with_a_bm25_of_0_under_the_scored_rows():
    # The keyword list is in BM25 order: a positive score, then the matched
    # rows with 0 (ties by id), then a matched row with a negative score.
    pool = [row(1), row(2), row(3), row(4), row(5)]
    matched = [True, True, True, False, True]
    result = ranking.rank_pool(pool, [0.0, 2.0, -0.1, 0.0, 0.0], {3: 0.9}, 5, matched)
    by_id = {h.row.id: h.fusion_score for h in result.hits}
    assert by_id == {
        4: round(1 / 61, 6),  # the cosine list only
        2: round(1 / 61, 6),
        1: round(1 / 62, 6),
        5: round(1 / 63, 6),
        3: round(1 / 64, 6),
    }


def test_no_matched_row_means_no_keyword_list():
    pool = [row(1), row(2)]
    result = ranking.rank_pool(pool, [0.0, 0.0], {0: 0.3, 1: 0.7}, 5, [False, False])
    assert [h.fusion_score for h in result.hits] == [round(1 / 61, 6), round(1 / 62, 6)]
    assert ranking.rank_pool(pool, [0.0, 0.0], None, 5, [False, False]).hits == []


def test_keyword_mode_keeps_a_matched_row_with_a_bm25_of_0():
    # Section 8.4 drops the rows that match no query term. A matched row whose
    # only term is in exactly half of the pool has a score of 0 too.
    pool = [row(1), row(2), row(3), row(4)]
    matched = [True, False, True, True]
    result = ranking.rank_pool(pool, [0.0, 0.0, 1.5, 0.0], None, top_k=5, matched=matched)
    assert result.mode == "keyword"
    assert [h.row.id for h in result.hits] == [3, 1, 4]
    assert [h.score for h in result.hits] == [None, None, None]


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


def half_pool(conn):
    """4 notes; "deploy" is in exactly 2 of them, so its idf is 0."""
    return {
        "deploy_a": add_memory(conn, "release_steps", "deploy the service after the backup"),
        "coffee": add_memory(conn, "coffee_filter", "paper filter brand"),
        "deploy_b": add_memory(conn, "rollback_steps", "deploy the old build again"),
        "garden": add_memory(conn, "garden_hose", "hose length and nozzle"),
    }


def test_a_word_in_half_of_the_pool_still_matches_in_hybrid_mode(conn):
    # NOBLIVION-49, the review reproducer: 4 notes, "deploy" in 2, and the
    # query "how do I deploy". Every BM25 score is 0. During a backfill only
    # 2 notes have a vector; the matched note without one must come back.
    ids = half_pool(conn)
    embedder = FakeEmbedder()
    service = make_service(embedder, conn)
    for key in ("deploy_a", "coffee"):
        content = conn.execute("SELECT content FROM memories WHERE id = ?", (ids[key],))
        text = embedding.embed_text(content.fetchone()[0])
        vector = embedder.embed_documents([text])[0]
        embedding._write_vectors(
            conn, embedder.model_id, [(ids[key], embedding.text_hash(text), vector)]
        )
    result = ranking.Ranker(service).search(
        conn, "how do I deploy", project="claude_code", top_k=10
    )
    assert result.mode == "hybrid"
    assert [h.bm25 for h in result.hits] == [0.0] * len(result.hits)
    by_id = {h.row.id: h for h in result.hits}
    assert set(by_id) == {ids["deploy_a"], ids["coffee"], ids["deploy_b"]}
    assert by_id[ids["deploy_b"]].score is None  # matched, no vector yet
    assert ids["garden"] not in by_id  # no vector and no match
    # The matched note with a vector is in both lists, so it is first.
    assert result.hits[0].row.id == ids["deploy_a"]


def test_a_word_in_half_of_the_pool_still_matches_in_keyword_mode(conn):
    ids = half_pool(conn)
    index = ranking.RankIndex()
    index.refresh(conn)
    result = index.rank("how do I deploy", project="claude_code", top_k=10)
    assert result.mode == "keyword"
    assert ranked_ids(result) == [ids["deploy_a"], ids["deploy_b"]]
    assert index.rank("how do I brew", project="claude_code", top_k=10).hits == []


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
    content = conn.execute("SELECT content FROM memories WHERE id = ?", (ids["deploy"],))
    digest = embedding.text_hash(embedding.embed_text(content.fetchone()[0]))
    embedding._write_vectors(
        conn, new.model_id, [(ids["deploy"], digest, new.embed_documents(["deploy backup"])[0])]
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
