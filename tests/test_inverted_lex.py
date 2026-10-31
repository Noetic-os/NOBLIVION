# SPDX-License-Identifier: AGPL-3.0-or-later
"""The store computes the BM25 values of the hook re-rank (NOBLIVION-157).

The hook used to build ``BM25`` over the sub-token words of the local memory files on every
prompt. The store now answers the same two values for each row (``lex``). These tests make
the store's values equal to the hook's own computation over the same folder, and make the
hook's re-rank give the same order with the store's values as with its own index.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import db, indexer, ranking

hook = load_hook("recall_hook", "hooktest_inverted_lex")
corpus_mod = load_hook("corpus", "hooktest_inverted_lex_corpus")

WORDS = [f"term{n}" for n in range(30)] + ["the", "and", "feedback_never_checkout", "git", "lease"]
QUERIES = [
    "git checkout lease",
    "term1 term2 term2 the",
    "feedback_never_checkout term7",
    "nothing like it",
    "the and of",
    "term29 term28 term0 git",
]


def write_notes(folder: Path, count: int = 24) -> None:
    folder.mkdir(parents=True)
    rng = random.Random(7)
    for n in range(count):
        body = " ".join(WORDS[min(int(rng.expovariate(0.15)), len(WORDS) - 1)] for _ in range(60))
        rule = f"rule: {WORDS[n % len(WORDS)]} {WORDS[(n * 3) % len(WORDS)]}"
        triggers = "triggers:\n  - phrase: " + WORDS[(n * 5) % len(WORDS)]
        (folder / f"feedback_note{n}.md").write_text(
            f"---\nname: note {n}\ndescription: about {WORDS[n % 7]}\ntype: feedback\n"
            f"{rule}\n{triggers}\n---\n{body}\n",
            encoding="utf-8",
        )
    (folder / "MEMORY.md").write_text("- [note 0](feedback_note0.md) term3\n", encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    folder = tmp_path / "projects" / "-work-demo" / "memory"
    write_notes(folder)
    conn = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    indexer.scan(conn, [folder], project="claude_code", labeller=None)
    yield conn, folder
    conn.close()


def local_values(folder: Path, query: str):
    """What the hook computed on its own: ``{rel_path: (every word, informative words)}``."""
    files = corpus_mod.load_memory_corpus(str(folder), None)
    index = hook.BM25([md.tokens for md in files])
    words = hook.tokens_sub(query)
    informative = [w for w in words if w not in index.common]
    every = index.scores(words)
    shared = index.scores(informative) if informative else {}
    return {md.rel_path: (every[i], shared.get(i, 0.0)) for i, md in enumerate(files)}


def test_the_store_values_equal_the_hook_values(world):
    conn, folder = world
    ranker = ranking.Ranker(None)
    compared = 0
    for query in QUERIES:
        want = local_values(folder, query)
        result = ranker.search(
            conn, query, project="claude_code", top_k=100, root="-work-demo", lex=True
        )
        # a keyword answer drops the rows that match nothing: compare every row that came back
        compared += len(result.hits)
        for hit in result.hits:
            assert hit.lex is not None
            got = hit.lex
            ref = want[hit.row.path]
            assert got == pytest.approx(ref, rel=1e-9, abs=1e-9), (query, hit.row.path)
    assert compared >= 60
    # the MEMORY.md index file counts in the pool, as it counts in the hook corpus
    assert "MEMORY.md" in want


def test_no_lex_without_the_request(world):
    conn, _ = world
    result = ranking.Ranker(None).search(
        conn, "term1", project="claude_code", top_k=5, root="-work-demo"
    )
    assert result.hits and all(h.lex is None for h in result.hits)


def test_the_pool_follows_a_change_like_a_pool_made_new(world):
    conn, folder = world
    ranker = ranking.Ranker(None)
    ranker.search(conn, "term1", project="claude_code", top_k=5, root="-work-demo", lex=True)
    (folder / "feedback_note3.md").write_text(
        "---\nname: note 3\ndescription: changed\ntype: feedback\nrule: lease term1\n---\n"
        "term1 term1 lease checkout\n",
        encoding="utf-8",
    )
    (folder / "feedback_note4.md").unlink()
    indexer.scan(conn, [folder], project="claude_code", labeller=None, delete_grace_days=0)
    for query in QUERIES:
        want = local_values(folder, query)
        result = ranker.search(
            conn, query, project="claude_code", top_k=100, root="-work-demo", lex=True
        )
        for hit in result.hits:
            assert hit.lex == pytest.approx(want[hit.row.path], rel=1e-9, abs=1e-9)


def index_lines(conn, query: str, lex: bool):
    """The rows of the index route, parsed by the hook."""
    from noblivion import rest

    result = ranking.Ranker(None).search(
        conn, query, project="claude_code", top_k=40, root="-work-demo", lex=lex
    )
    answer = rest.index_answer(result, "claude_code", query=query, mode="keyword", model=None)
    payload = json.loads(json.dumps(answer))
    # the cosine is not part of this test: give every row one, in the store order
    for n, item in enumerate(payload["results"]):
        item["score"] = round(0.9 - n * 0.01, 4)
    payload["mode"] = "hybrid"
    return hook.parse_index(payload)


def test_the_hook_rerank_is_the_same_with_the_store_values(world):
    conn, folder = world
    files = corpus_mod.load_memory_corpus(str(folder), None)
    by_name = hook.corpus_by_name(files)
    index = hook.BM25([md.tokens for md in files])
    for query in QUERIES:
        own = index_lines(conn, query, lex=False)
        stored = index_lines(conn, query, lex=True)
        if not own:
            continue
        assert all(ln.store_lex is not None for ln in stored)
        a, joined_a = hook.rerank_index(own, query, files, by_name, index)
        b, joined_b = hook.rerank_index(stored, query, files, by_name, None)
        assert [ln.mid for ln in a] == [ln.mid for ln in b]
        assert joined_a == joined_b
        for x, y in zip(a, b):  # noqa: B905 - same length by the assert above
            assert x.lexical == pytest.approx(y.lexical, rel=1e-9, abs=1e-9)
            assert x.fused == pytest.approx(y.fused, rel=1e-9, abs=1e-9)


def test_the_corpus_cache_keeps_no_tokens_and_reads_them_when_asked(tmp_path):
    folder = tmp_path / "memory"
    write_notes(folder, 5)
    cache = tmp_path / "corpus_cache.json"
    eager = corpus_mod.load_memory_corpus(str(folder), None)
    first = corpus_mod.load_memory_corpus(str(folder), str(cache))
    again = corpus_mod.load_memory_corpus(str(folder), str(cache))
    rows = json.loads(cache.read_text(encoding="utf-8"))["files"]
    assert all(row[8] == "" for row in rows.values() if len(row) == 12)  # the tokens column
    for a, b, c in zip(eager, first, again):  # noqa: B905
        assert a.tokens == b.tokens == c.tokens
        assert c._tokens is not None  # read on first use, from the file
    fresh = corpus_mod.load_memory_corpus(str(folder), str(cache))
    assert all(md._tokens is None for md in fresh)  # a cache hit builds none
