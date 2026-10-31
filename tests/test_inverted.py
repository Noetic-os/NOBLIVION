# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared inverted index (NOBLIVION-157): same scores as ``bm25.BM25`` per pool, update by
changed rows, and a bounded memory.

The reference is ``bm25.BM25`` over the token lists of the pool alone. The sum for the floor
(the mean idf of the pool) adds its terms in another order, so a score may differ in the
last bit; the tests compare to 1e-9 and compare the order and the flags exactly.
"""

from __future__ import annotations

import random

import pytest

from noblivion import bm25, db, inverted, ranking
from store_helpers import add_memory, change_memory, soft_delete

WORDS = [f"w{n}" for n in range(40)] + ["the", "and", "of", "until", "up", "lease-rotate"]


def make_docs(seed: int, count: int) -> list[list[str]]:
    rng = random.Random(seed)
    docs = []
    for _ in range(count):
        size = rng.randint(0, 30)
        # a skewed draw: the first words are common, so some idf values are negative
        docs.append([WORDS[min(int(rng.expovariate(0.12)), len(WORDS) - 1)] for _ in range(size)])
    return docs


QUERIES = [
    ["w0"],
    ["w1", "w1", "w7"],
    ["the", "w3"],
    ["until"],
    ["nothing", "here"],
    ["w39", "w38", "w0", "and", "up"],
    [],
]


def assert_same(view: inverted.PoolView, docs: list[list[str]]) -> None:
    ref = bm25.BM25(docs)
    assert view.n == ref.n
    for query in QUERIES:
        got = view.scores(query)
        want = ref.scores(query)
        assert got == pytest.approx(want, rel=1e-9, abs=1e-9)
        assert view.matches(query) == ref.matches(query)
        assert view.real_terms(query) == ref.real_terms(query)
    for word in WORDS:
        assert view.df[word] == ref.df[word]
    for mass in (0.1, 0.5, 0.9):
        assert view.df_cut(mass) == ref.df_cut(mass)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_pool_scores_like_bm25_over_the_pool_alone(seed):
    docs = make_docs(seed, 60)
    index = inverted.InvertedIndex()
    slots = [index.add(d) for d in docs]
    rng = random.Random(seed)
    member = sorted(rng.sample(range(len(docs)), 25))
    view = inverted.PoolView(index, [slots[i] for i in member])
    assert_same(view, [docs[i] for i in member])
    # the whole index as one pool, too
    assert_same(inverted.PoolView(index, slots), docs)


@pytest.mark.parametrize("seed", [4, 5])
def test_a_synced_pool_equals_a_pool_made_new(seed):
    docs = make_docs(seed, 80)
    index = inverted.InvertedIndex()
    slots = [index.add(d) for d in docs]
    rng = random.Random(seed)
    first = sorted(rng.sample(range(len(docs)), 30))
    view = inverted.PoolView(index, [slots[i] for i in first])
    view.df_cut(0.5)  # the collection frequencies exist, so the sync must keep them
    # three steps: units leave, units enter, units are replaced
    members = set(first)
    for _ in range(3):
        for i in rng.sample(sorted(members), 8):
            members.discard(i)
            index.remove(slots[i])
        for _ in range(6):
            docs.append(make_docs(rng.randint(0, 9999), 1)[0])
            slots.append(index.add(docs[-1]))
            members.add(len(docs) - 1)
        order = sorted(members)
        view.sync([slots[i] for i in order])
        assert_same(view, [docs[i] for i in order])
        fresh = inverted.PoolView(index, [slots[i] for i in order])
        assert view.df_by_id == fresh.df_by_id
        assert view.hist == fresh.hist
    assert view.syncs == 3 * (8 + 6)


def test_a_sync_without_a_change_does_nothing():
    index = inverted.InvertedIndex()
    slots = [index.add(["a", "b"]), index.add(["b", "c"])]
    view = inverted.PoolView(index, slots)
    view.sync(list(slots))
    assert view.syncs == 0


def test_a_unit_outside_the_pool_cannot_change_a_score_inside_it():
    index = inverted.InvertedIndex()
    inside = [index.add(["alpha", "beta"]), index.add(["beta", "gamma"]), index.add(["delta"])]
    view = inverted.PoolView(index, inside)
    before = view.scores(["beta", "alpha"])
    for _ in range(20):
        index.add(["alpha", "beta", "beta"])
    assert view.scores(["beta", "alpha"]) == before


def test_a_removed_unit_does_not_stay_in_the_postings_of_a_pool():
    index = inverted.InvertedIndex()
    a = index.add(["quokka", "x"])
    b = index.add(["quokka", "y"])
    rest = [index.add([f"other{n}"]) for n in range(6)]
    view = inverted.PoolView(index, [a, b, *rest])
    index.remove(a)
    view.sync([b, *rest])
    assert view.df["quokka"] == 1
    assert view.scores(["quokka"])[0] > 0 and view.scores(["quokka"])[1] == 0.0
    assert index.dead == 1 and index.live == 7


# -- the ranker -------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def reference_for(conn, ranker: ranking.RankIndex, key: tuple):
    """The pool units, their view, and the token lists of the units read from the database
    (the chunk text and the marker line of the file: these notes have no index fields)."""
    units, view, _ = ranker._pool(ranker._snap, key)
    texts = {
        int(r["id"]): r["search_text"]
        for r in conn.execute("SELECT id, search_text FROM memory_chunks")
    }
    docs = []
    for u in units:
        marker = ranking._MARKER_LINE_RE.search(u.content)
        path_tokens = bm25.tokenize(marker.group(0)) if marker else []
        text = texts[u.chunk_id] if u.chunk_id is not None else u.content
        docs.append(bm25.tokenize(text) + (path_tokens if u.chunk_id is not None else []))
    return units, view, docs


def test_the_ranker_pool_scores_like_the_reference_after_every_change(conn):
    ids = [
        add_memory(conn, f"n{n}", " ".join(WORDS[(n * 7 + k) % len(WORDS)] for k in range(30)))
        for n in range(12)
    ]
    ranker = ranking.RankIndex()
    ranker.refresh(conn)
    key = ranker.pool_key("claude_code", None, (), False)

    def check():
        units, view, docs = reference_for(conn, ranker, key)
        assert len(units) == view.n == len(docs)
        assert_same(view, docs)

    check()
    builds = ranker.bm25_builds
    assert ranker.pool_syncs == 0
    change_memory(conn, ids[0], "w1 w1 w2 fresh text")
    soft_delete(conn, ids[1])
    add_memory(conn, "late", "w3 w4 until")
    ranker.refresh(conn)
    check()
    assert ranker.bm25_builds == builds  # no pool was built again
    assert ranker.pool_syncs == 1  # the pool followed its changed units
    # one changed file gave an exact set of units to the counts
    view = ranker._pool(ranker._snap, key)[1]
    fresh = inverted.PoolView(ranker._snap.index, view.slots)
    assert view.df_by_id == fresh.df_by_id and view.hist == fresh.hist


def test_a_changed_row_leaves_the_postings_of_other_rows_alone(conn):
    a = add_memory(conn, "a", "alpha beta")
    add_memory(conn, "b", "beta gamma")
    ranker = ranking.RankIndex()
    ranker.refresh(conn)
    index = ranker._snap.index
    before = {w: (list(index.post_slot[t]), list(index.post_tf[t])) for w, t in index.vocab.items()}
    change_memory(conn, a, "alpha delta")
    ranker.refresh(conn)
    assert ranker._snap.index is index  # patched in place, not rebuilt
    for word in ("gamma",):
        t = index.vocab[word]
        assert (list(index.post_slot[t]), list(index.post_tf[t])) == before[word]
    assert index.dead >= 1


def test_a_rebuild_follows_many_dead_slots(conn, monkeypatch):
    monkeypatch.setattr(ranking, "_COMPACT_MIN_DEAD", 3)
    a = add_memory(conn, "a", "alpha")
    add_memory(conn, "b", "beta")
    ranker = ranking.RankIndex()
    ranker.refresh(conn)
    first = ranker._snap.index
    for n in range(4):
        change_memory(conn, a, f"alpha v{n}")
        ranker.refresh(conn)
    assert ranker._snap.index is not first
    assert ranker._snap.index.dead < 3
    assert [h.row.id for h in ranker.rank("alpha", project="claude_code", top_k=5).hits] == [a]
