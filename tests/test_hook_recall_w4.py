# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three optional index changes of the recall hook: a longer query, row
hygiene and the local re-rank.

The default index asks the store for 35 candidates with the first 300
characters of the prompt and renders what comes back. Three changes, each
behind its own variable:

  P2  NOBLIVION_RECALL_INDEX_QUERY_CHARS  more of the prompt reaches the query
  P3  NOBLIVION_RECALL_INDEX_HYGIENE      overfetch, then drop index and closed rows
  P1h NOBLIVION_RECALL_INDEX_RERANK       re-rank the candidates locally

What these tests hold:

1. All three are off unless asked for. With none set the hook asks the store
   exactly what the default index asks and renders exactly its rows.
2. The local work never breaks a prompt and never hides a fallback: every
   failure falls back to the store's own list and names the fallback in the
   log line.
3. The hook's corpus reader agrees with the store indexer's reader on the
   shapes a memory folder has, and builds the same stored text.
4. The lexical leg is BM25Okapi, including the negative inverse document
   frequency floor (epsilon times the mean taken before the replacement).
5. A rendered list has no holes: after a drop or a re-rank the ranks are 1..n.

Drives the real hook against a fake store that records every request, so the
top_k the hook asks for and the query it sends are part of the assertion.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook
from noblivion import bm25 as store_bm25
from noblivion import indexer
from recall_helpers import INDEX_FLOOR_OFF, FakeStore, hook_env

hook = load_hook("recall_hook", "hooktest_recall_w4")


# -- a fake store that records what the hook asked for --------------------------------


class _Daemon:
    def __init__(self, data_dir: Path):
        self.store = FakeStore(data_dir)
        self.index_rows: list[dict[str, Any]] = []
        self.store.route("/api/memories/index", self._index)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return [
            {"path": p, "qs": {k: v[0] for k, v in q.items()}}
            for p, q, _h in self.store.requests
            if p != "/health"
        ]

    def _index(self, path: str, query: dict) -> Any:
        top_k = int((query.get("top_k") or ["0"])[0])
        rows = [dict(r) for r in self.index_rows[:top_k]]
        return {"namespace": (query.get("project") or [None])[0], "reason": None, "results": rows}


@pytest.fixture
def daemon(tmp_path):
    state = _Daemon(tmp_path / "data")
    try:
        yield state
    finally:
        state.store.stop()


@pytest.fixture
def env(daemon, tmp_path) -> dict[str, str]:
    return hook_env(
        tmp_path, NOBLIVION_RECALL_TIMEOUT_S="5.0", NOBLIVION_RECALL_INDEX="1", **INDEX_FLOOR_OFF
    )


def _row(rank: int, mid: int, title: str, summary: str, score: float) -> dict[str, Any]:
    return {
        "rank": rank,
        "id": mid,
        "title": title,
        "summary": summary,
        "score": score,
        "fusion_score": 1.0 / (60 + rank),
        "source": f"{title}.md",
    }


def _serve(env: dict[str, str], prompt: str) -> str:
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id="w4-test", prompt=prompt)
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue()


def _ranks_and_ids(text: str) -> list[tuple]:
    return [
        (int(m["rank"]), int(m["id"]))
        for m in re.finditer(r"^- (?P<rank>\d+)\. .* \(id (?P<id>\d+), score ", text, re.M)
    ]


def _log_status(env: dict[str, str]) -> str:
    """The status field of the hook's last log line. The line is plain text:
    ``<ts> event=.. session=.. hits=N chars=N ms=N <status>``."""
    log = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log"
    return log.read_text().strip().splitlines()[-1].rsplit(" ", 1)[1]


def _write(
    folder: Path, base: str, name: str, description: str, body: str, kind_line: str = ""
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    meta = ["---", f"name: {name}", f"description: {description}"]
    if kind_line:
        meta.append("metadata:")
        meta.append(f"  type: {kind_line}")
    meta.append("---")
    (folder / base).write_text("\n".join(meta) + "\n\n" + body + "\n")


# -- 1. off unless asked for ----------------------------------------------------------


def test_with_no_variable_set_the_hook_asks_what_the_default_index_asks(daemon, env):
    """The default request and the default rows, unchanged. The index floor is
    off in this env (``INDEX_FLOOR_OFF``); with it the hook asks for more rows."""
    daemon.index_rows = [
        _row(i, 100 + i, f"feedback_row_{i}", "a summary", 0.9 - i / 100) for i in range(1, 6)
    ]
    prompt = "x" * 5000
    text = _serve(env, prompt)
    assert len(daemon.requests) == 1
    qs = daemon.requests[0]["qs"]
    assert int(qs["top_k"]) == hook.INDEX_K_DEFAULT, "the default k, not a candidate depth"
    assert len(qs["q"]) == hook.QUERY_MAX_CHARS, "the default 300-character query"
    # The store's own order and the store's own rank numbers, untouched.
    assert _ranks_and_ids(text) == [(1, 101), (2, 102), (3, 103), (4, 104), (5, 105)]


def test_p2_sends_more_of_the_prompt_and_only_when_asked(daemon, env):
    daemon.index_rows = [_row(1, 101, "feedback_row_1", "a summary", 0.9)]
    _serve(env, "y" * 5000)
    assert len(daemon.requests[-1]["qs"]["q"]) == 300
    _serve(dict(env, NOBLIVION_RECALL_INDEX_QUERY_CHARS="2000"), "y" * 5000)
    assert len(daemon.requests[-1]["qs"]["q"]) == 2000


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2000", 2000),
        ("999999", hook.INDEX_QUERY_CHARS_MAX),  # bounded here, not at the store
        ("0", 1),
        ("not-a-number", hook.QUERY_MAX_CHARS),  # never fail a prompt over a setting
        ("", hook.QUERY_MAX_CHARS),
    ],
)
def test_p2_is_bounded_and_falls_back_to_the_default(value, expected):
    assert hook.index_query_chars({hook.INDEX_QUERY_CHARS_ENV: value}) == expected


# -- 2. P3, row hygiene ---------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path) -> Path:
    """A small corpus with one of every row P3 drops and one it must keep."""
    folder = tmp_path / "memory"
    _write(folder, "MEMORY.md", "MEMORY", "the index of every memory", "- a pointer")
    _write(
        folder,
        "topic_ci_gates.md",
        "topic_ci_gates",
        "pointers to the CI gate memories",
        "- another pointer",
    )
    _write(
        folder,
        "feedback_done_thing.md",
        "feedback_done_thing",
        "FIXED 2026-09-01: the gate no longer fires",
        "it was fixed",
    )
    _write(
        folder,
        "feedback_live_thing.md",
        "feedback_live_thing",
        "Run the migration before the deploy, or the pod restarts",
        "the deploy reads the schema at start",
    )
    _write(
        folder,
        "feedback_never_git_checkout.md",
        "feedback_never_git_checkout",
        "git checkout -- discards uncommitted work with no prompt",
        "use an inverse edit instead",
    )
    return folder


def test_p3_asks_for_the_candidate_depth_and_drops_the_rows_it_says(daemon, env, corpus):
    daemon.index_rows = [
        _row(1, 201, "MEMORY", "the index of every memory", 0.70),
        _row(2, 202, "topic_ci_gates", "pointers to the CI gate memories", 0.68),
        _row(3, 203, "feedback_done_thing", "FIXED 2026-09-01: the gate no longer fires", 0.66),
        _row(4, 204, "feedback_live_thing", "Run the migration before the deploy", 0.64),
    ]
    text = _serve(
        dict(env, NOBLIVION_RECALL_INDEX_HYGIENE="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus)),
        "a prompt about deploys",
    )
    assert int(daemon.requests[-1]["qs"]["top_k"]) == hook.INDEX_CANDIDATE_TOP_K
    # The index file, the topic file and the closed note are gone; the live note
    # is rank 1 and not rank 4, because a list with holes is a list with holes.
    assert _ranks_and_ids(text) == [(1, 204)]


def test_p3_keeps_a_row_it_cannot_classify(daemon, env, corpus):
    """A row whose title names no local file is a real candidate. Dropping what
    cannot be classified would quietly shorten the list whenever the pool and
    the corpus disagree."""
    daemon.index_rows = [
        _row(1, 301, "a_mined_row_no_file_has", "from a transcript, not a file", 0.70),
        _row(2, 302, "feedback_live_thing", "Run the migration before the deploy", 0.64),
    ]
    text = _serve(
        dict(env, NOBLIVION_RECALL_INDEX_HYGIENE="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus)),
        "a prompt",
    )
    assert [mid for _, mid in _ranks_and_ids(text)] == [301, 302]


def test_a_closed_marker_is_read_from_the_body_when_there_is_no_description(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "feedback_bare.md").write_text("RESOLVED: nothing to do here\n\nmore text\n")
    md = hook.load_memory_corpus(str(folder))[0]
    assert md.description == "" and md.dropped is True


# -- 3. P1h, the local re-rank --------------------------------------------------------


def test_p1h_promotes_the_row_only_a_sub_token_split_can_match(daemon, env, corpus):
    """The whole of change P1 in one case.

    ``feedback_never_git_checkout`` is ONE token to the store's tokenizer, so
    a prompt that says "git checkout" scores zero against the only memory
    about it. Splitting the token on ``_`` makes the match possible.

    The arithmetic, by the documented rule (reciprocal rank fusion, k=60, one
    weight each, ties by id ascending). Cosine order is by the row's score, so
    the target is 4th of 4. Only the target carries the query's tokens, so it
    is 1st in the lexical list and the other three follow by id: 401, 402, 403.
      target 404: 1/(60+4) + 1/(60+1) = 0.015625 + 0.016393 = 0.032018
      row 401:    1/(60+1) + 1/(60+2) = 0.016393 + 0.016129 = 0.032522
    So the target does NOT reach rank 1 from last place: that is what rank
    fusion is. It moves from 4 to 2, and the assertion says 2.
    """
    daemon.index_rows = [
        _row(1, 401, "feedback_live_thing", "Run the migration before the deploy", 0.70),
        _row(2, 402, "topic_ci_gates", "pointers to the CI gate memories", 0.68),
        _row(3, 403, "feedback_done_thing", "FIXED 2026-09-01: the gate no longer fires", 0.66),
        _row(
            4,
            404,
            "feedback_never_git_checkout",
            "git checkout -- discards uncommitted work",
            0.64,
        ),
    ]
    text = _serve(
        dict(env, NOBLIVION_RECALL_INDEX_RERANK="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus)),
        "I ran git checkout by mistake, can I get the file back",
    )
    assert [mid for _, mid in _ranks_and_ids(text)] == [401, 404, 402, 403]


def test_p1h_with_p3_reaches_rank_1_because_hygiene_removed_the_rows_above(daemon, env, corpus):
    """P3 first, then P1h: the two changes are measured together for a reason."""
    daemon.index_rows = [
        _row(1, 401, "MEMORY", "the index of every memory", 0.70),
        _row(2, 402, "topic_ci_gates", "pointers to the CI gate memories", 0.68),
        _row(3, 403, "feedback_done_thing", "FIXED 2026-09-01: the gate no longer fires", 0.66),
        _row(
            4,
            404,
            "feedback_never_git_checkout",
            "git checkout -- discards uncommitted work",
            0.64,
        ),
    ]
    text = _serve(
        dict(
            env,
            NOBLIVION_RECALL_INDEX_HYGIENE="1",
            NOBLIVION_RECALL_INDEX_RERANK="1",
            NOBLIVION_RECALL_MEMORY_DIR=str(corpus),
        ),
        "I ran git checkout by mistake, can I get the file back",
    )
    assert [mid for _, mid in _ranks_and_ids(text)] == [404]


def test_p1h_is_off_on_a_keyword_answer_and_the_log_says_so(daemon, env, corpus):
    """Design doc section 8.4: a keyword-mode answer carries no cosine, so the
    hook keeps the store's order instead of fusing with a missing leg."""
    rows = [
        _row(1, 401, "feedback_live_thing", "Run the migration before the deploy", 0.70),
        _row(2, 404, "feedback_never_git_checkout", "git checkout -- discards work", 0.64),
    ]
    daemon.store.route(
        "/api/memories/index",
        lambda _p, q: {
            "namespace": q["project"][0],
            "reason": None,
            "mode": "keyword",
            "results": rows,
        },
    )
    e = dict(env, NOBLIVION_RECALL_INDEX_RERANK="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus))
    text = _serve(e, "I ran git checkout by mistake, can I get the file back")
    assert [mid for _, mid in _ranks_and_ids(text)] == [401, 404]
    assert _log_status(e) == "ok:rerank_off:keyword"


def test_the_cosine_leg_is_the_score_and_not_the_order_the_daemon_sent(daemon, env, corpus):
    """The store's returned order is its OWN fusion of two legs. P1h fuses
    with its COSINE leg, which is the score field, so a row the store ranked 1
    on fusion but scored lowest on cosine goes last in the cosine list."""
    rows = [
        _row(1, 501, "feedback_live_thing", "a", 0.10),
        _row(2, 502, "feedback_done_thing", "b", 0.90),
    ]
    lines = [
        hook.IndexLine(
            rank=r["rank"], mid=r["id"], title=r["title"], summary=r["summary"], score=r["score"]
        )
        for r in rows
    ]
    folder_corpus = hook.load_memory_corpus(str(corpus))
    by_name = hook.corpus_by_name(folder_corpus)
    bm25 = hook.BM25([md.tokens for md in folder_corpus])
    ranked, joined = hook.rerank_index(
        lines, "nothing lexical matches here", folder_corpus, by_name, bm25
    )
    assert joined == 2
    assert [ln.mid for ln in ranked] == [502, 501], "ordered by cosine, not by arrival"


def test_a_row_with_bm25_0_keeps_its_cosine_place(tmp_path):
    """NOBLIVION-76. Only a row whose file holds a query word is ranked in the
    BM25 list. A list that also ranked the rows with BM25 0, by their id, gave
    a row with a low id a better fused score than a row with a better cosine."""
    folder = tmp_path / "memory"
    for name, body in (
        ("feedback_a", "alpha note"),
        ("feedback_b", "bravo note"),
        ("feedback_c", "charlie note"),
        ("feedback_d", "the kiwi note"),
    ):
        _write(folder, f"{name}.md", name, f"{name} rule", body)
    cosines = {1: 0.5, 2: 0.6, 3: 0.7, 4: 0.4}
    lines = [
        hook.IndexLine(
            rank=mid, mid=mid, title=f"feedback_{'abcd'[mid - 1]}", summary="s", score=cos
        )
        for mid, cos in cosines.items()
    ]
    folder_corpus = hook.load_memory_corpus(str(folder))
    bm25 = hook.BM25([md.tokens for md in folder_corpus])
    ranked, joined = hook.rerank_index(
        lines, "kiwi", folder_corpus, hook.corpus_by_name(folder_corpus), bm25
    )
    assert joined == 4
    # The kiwi row is in both lists; the others keep their cosine order.
    assert [ln.mid for ln in ranked] == [4, 3, 2, 1]


def test_a_row_with_no_score_goes_last(corpus):
    lines = [
        hook.IndexLine(rank=1, mid=601, title="feedback_live_thing", summary="a", score=None),
        hook.IndexLine(rank=2, mid=602, title="feedback_done_thing", summary="b", score=-0.9),
    ]
    folder_corpus = hook.load_memory_corpus(str(corpus))
    bm25 = hook.BM25([md.tokens for md in folder_corpus])
    ranked, _ = hook.rerank_index(
        lines, "no lexical match", folder_corpus, hook.corpus_by_name(folder_corpus), bm25
    )
    assert [ln.mid for ln in ranked] == [602, 601]


# -- 4. fail open, and say which fallback ran -----------------------------------------


def test_no_memory_folder_means_the_daemon_s_own_list_and_the_log_says_so(daemon, env):
    daemon.index_rows = [
        _row(1, 701, "feedback_live_thing", "a", 0.9),
        _row(2, 702, "MEMORY", "the index", 0.8),
    ]
    e = dict(env, NOBLIVION_RECALL_INDEX_HYGIENE="1", NOBLIVION_RECALL_INDEX_RERANK="1")
    text = _serve(e, "a prompt")
    assert [mid for _, mid in _ranks_and_ids(text)] == [701, 702], "nothing dropped"
    assert _log_status(e) == "ok:local_off:no_memory_dir"


def test_a_memory_folder_that_is_not_a_folder_is_the_same_fallback(daemon, env, tmp_path):
    daemon.index_rows = [_row(1, 801, "feedback_live_thing", "a", 0.9)]
    missing = tmp_path / "there-is-no-folder-here"
    e = dict(env, NOBLIVION_RECALL_INDEX_RERANK="1", NOBLIVION_RECALL_MEMORY_DIR=str(missing))
    _serve(e, "a prompt")
    assert _log_status(e) == "ok:local_off:no_memory_dir"


def test_the_log_line_carries_the_join_count(daemon, env, corpus):
    """A join that is failing looks exactly like a ranking that is working, so
    the count is in the log. 1 of 2 rows found its file here."""
    daemon.index_rows = [
        _row(1, 901, "feedback_live_thing", "a", 0.9),
        _row(2, 902, "not_a_file_in_the_corpus", "b", 0.8),
    ]
    e = dict(env, NOBLIVION_RECALL_INDEX_RERANK="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus))
    _serve(e, "a prompt")
    assert _log_status(e) == "ok:joined1of2"


def test_an_unreadable_memory_file_does_not_break_the_prompt(daemon, env, corpus):
    daemon.index_rows = [_row(1, 1001, "feedback_live_thing", "a", 0.9)]
    (corpus / "broken.md").mkdir()  # a directory named like a memory file
    e = dict(env, NOBLIVION_RECALL_INDEX_RERANK="1", NOBLIVION_RECALL_MEMORY_DIR=str(corpus))
    text = _serve(e, "a prompt")
    assert [mid for _, mid in _ranks_and_ids(text)] == [1001]
    assert _log_status(e).startswith("ok:")


# -- 5. the hook's reader agrees with the store indexer's -----------------------------


def _indexer_files(folder: Path) -> dict[str, indexer.ParsedFile]:
    found = indexer.read_folder(folder.parent.name, folder)
    return {
        entry.path: indexer.parse_file(entry.path, entry.abspath.read_text(encoding="utf-8"))
        for entry in found.files
    }


def test_the_hooks_frontmatter_reader_agrees_with_the_indexers(tmp_path):
    """The shapes a memory folder has, and the shapes that break a parser."""
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "feedback_colon.md").write_text(
        "---\nname: feedback_colon\n"
        "description: 🔴 a value with ': ' inside it and an emoji\n"
        "metadata:\n  type: feedback\n---\n\nbody text\n"
    )
    (folder / "project_nested.md").write_text(
        '---\nname: project_nested\ndescription: "quoted value"\n'
        "metadata:\n  type: project\n  extra: kept\n---\n\nbody\n"
    )
    (folder / "unknown_prefix.md").write_text(
        "---\nname: unknown_prefix\ndescription: falls back on the frontmatter type\n"
        "metadata:\n  type: user\n---\n\nbody\n"
    )
    (folder / "no_frontmatter.md").write_text("just a body, no dashes\n")
    (folder / "unterminated.md").write_text("---\nname: never_closed\ndescription: x\n")
    (folder / "MEMORY.md").write_text("---\nname: MEMORY\ndescription: the index\n---\n\n- x\n")
    (folder / "empty.md").write_text("   \n")
    ours = {md.rel_path: md for md in hook.load_memory_corpus(str(folder))}
    theirs = _indexer_files(folder)
    assert set(ours) == set(theirs), "the same files, and the empty one skipped"
    assert "empty.md" not in ours
    for rel, want in theirs.items():
        got = ours[rel]
        assert (got.name, got.kind, got.description, got.body) == (
            want.name,
            want.category,
            want.description,
            want.body,
        ), rel


def test_build_content_matches_the_indexers(tmp_path):
    """P1h must tokenize the text the store indexed, or the two legs would be
    ranking different documents."""
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "feedback_x.md").write_text(
        "---\nname: feedback_x\ndescription: a description\n---\n\nthe body\n"
    )
    theirs = _indexer_files(folder)["feedback_x.md"]
    ours = hook.load_memory_corpus(str(folder))[0]
    assert hook.build_memory_content(
        ours.name, ours.description, ours.rel_path, ours.body
    ) == indexer.build_content("feedback_x.md", theirs)


# -- 6. the lexical leg is BM25Okapi --------------------------------------------------

_BM25_CORPUS = [
    "the deploy reads the schema at start".split(),
    "the migration runs before the deploy".split(),
    "the gate no longer fires".split(),
    "git checkout discards uncommitted work".split(),
]
_BM25_QUERY = "the deploy git".split()


def test_bm25_equals_rank_bm25_including_the_negative_idf_floor():
    """rank_bm25's BM25Okapi, the reference class. The term ``the`` is in
    every document, so its inverse document frequency is negative and the
    epsilon floor applies. Skipped when rank_bm25 is not installed; the next
    two tests hold the same rule without it."""
    rank_bm25 = pytest.importorskip("rank_bm25")
    theirs = rank_bm25.BM25Okapi(
        _BM25_CORPUS, k1=hook.BM25_K1, b=hook.BM25_B, epsilon=hook.BM25_EPSILON
    )
    ours = hook.BM25(_BM25_CORPUS)
    want = list(theirs.get_scores(_BM25_QUERY))
    got = ours.scores(_BM25_QUERY)
    assert len(got) == len(want)
    for i, value in enumerate(want):
        assert math.isclose(got[i], value, rel_tol=1e-12, abs_tol=1e-12), i
    assert any(theirs.idf[w] < 0 for w in ("the",)) is False, (
        "rank_bm25 has already replaced it; the floor is what we reproduce"
    )


def test_bm25_equals_the_stores_lexical_leg():
    """The store's keyword leg (``noblivion.bm25``) and the hook's re-rank
    must score one corpus the same, or the two legs rank different things."""
    theirs = store_bm25.BM25(_BM25_CORPUS)
    ours = hook.BM25(_BM25_CORPUS)
    want = theirs.scores(_BM25_QUERY)
    got = ours.scores(_BM25_QUERY)
    assert len(got) == len(want)
    for i, value in enumerate(want):
        assert math.isclose(got[i], value, rel_tol=1e-12, abs_tol=1e-12), i
    assert (hook.BM25_K1, hook.BM25_B, hook.BM25_EPSILON) == (
        store_bm25.K1,
        store_bm25.B,
        store_bm25.EPSILON,
    )


def test_the_negative_idf_floor_uses_the_mean_before_the_replacement():
    n = len(_BM25_CORPUS)
    df: dict[str, int] = {}
    for doc in _BM25_CORPUS:
        for word in set(doc):
            df[word] = df.get(word, 0) + 1
    raw = {w: math.log(n - c + 0.5) - math.log(c + 0.5) for w, c in df.items()}
    assert raw["the"] < 0
    floor = hook.BM25_EPSILON * (sum(raw.values()) / len(raw))
    ours = hook.BM25(_BM25_CORPUS)
    assert math.isclose(ours.idf["the"], floor, rel_tol=1e-12)
    assert math.isclose(ours.idf["git"], raw["git"], rel_tol=1e-12)


def test_bm25_scores_only_the_documents_asked_for():
    """The answer is keyed by document index, and only the documents asked
    for are in it. P1h scores only the store's candidates, which keeps it
    inside its latency budget."""
    corpus = [["alpha", "beta"], ["beta", "gamma"], ["gamma", "delta"]]
    ours = hook.BM25(corpus)
    every = ours.scores(["beta"])
    assert set(every) == {0, 1, 2}
    some = ours.scores(["beta"], [0, 2])
    assert set(some) == {0, 2}
    assert some[0] == every[0] and some[2] == every[2], "the same score, fewer rows"
    assert every[2] == 0.0, "document 2 does not carry the term"


def test_the_sub_token_split_needs_two_parts_of_two_characters():
    assert hook.tokens_sub("feedback_never_git_checkout") == [
        "feedback_never_git_checkout",
        "feedback",
        "never",
        "git",
        "checkout",
    ]
    # A token that splits into fewer than two parts of two characters is left
    # whole. The store's tokenizer keeps "-", ".", "_" and "/" inside a token,
    # so each of these is ONE token before the split is even tried.
    assert hook.tokens_sub("plain") == ["plain"]
    assert hook.tokens_sub("a-b") == ["a-b"], "both parts are one character"
    assert hook.tokens_sub("ab-c") == ["ab-c"], "one part is one character"
    assert hook.tokens_sub("ab-cd") == ["ab-cd", "ab", "cd"]
    # And a token below the two-character floor never enters the list at all.
    assert hook.tokens_sub("a b") == []


# -- 7. a rendered list has no holes --------------------------------------------------


def test_renumber_gives_1_to_n():
    lines = [
        hook.IndexLine(rank=r, mid=r * 10, title="t", summary="s", score=0.5) for r in (3, 7, 9)
    ]
    assert [ln.rank for ln in hook.renumber(lines)] == [1, 2, 3]
    assert [ln.mid for ln in hook.renumber(lines)] == [30, 70, 90]


def test_the_render_cap_still_cuts_from_the_bottom_after_a_rerank(daemon, env, corpus):
    daemon.index_rows = [
        _row(i, 1100 + i, "feedback_live_thing", "s" * 150, 0.9 - i / 100) for i in range(1, 40)
    ]
    text = _serve(
        dict(
            env,
            NOBLIVION_RECALL_INDEX_HYGIENE="1",
            NOBLIVION_RECALL_INDEX_RERANK="1",
            NOBLIVION_RECALL_MEMORY_DIR=str(corpus),
        ),
        "a prompt",
    )
    ranks = [r for r, _ in _ranks_and_ids(text)]
    assert ranks == list(range(1, len(ranks) + 1)), "a prefix of the ranking, no holes"
    assert len(text) <= hook.INDEX_OUTPUT_MAX_CHARS
