# SPDX-License-Identifier: AGPL-3.0-or-later
"""BM25 Okapi and tokenizer (design doc 0001, section 8.2). Hand-computed values."""

from __future__ import annotations

import math

import pytest

from noblivion import bm25


def test_tokenize_keeps_path_characters_and_drops_short_tokens():
    assert bm25.tokenize("Run the Tests: x, tools/run_all.sh -v") == [
        "run",
        "the",
        "tests",
        "tools/run_all.sh",
        "-v",
    ]


def test_tokenize_sub_adds_the_parts_of_a_compound_token():
    assert bm25.tokenize_sub("feedback_never_skip") == [
        "feedback_never_skip",
        "feedback",
        "never",
        "skip",
    ]
    assert bm25.tokenize_sub("plain") == ["plain"]


# Corpus: d0 = "alpha beta", d1 = "alpha gamma gamma", d2 = "delta".
# N = 3, dl = (2, 3, 1), avgdl = 2.
# idf(t) = ln(N - df + 0.5) - ln(df + 0.5)
#   alpha (df 2): ln(1.5) - ln(2.5) = ln(0.6) = -0.5108256  (negative)
#   beta, gamma, delta (df 1): ln(2.5) - ln(1.5) = +0.5108256
# mean idf before the floor = (-0.5108256 + 3 * 0.5108256) / 4 = 0.2554128
# alpha becomes epsilon * mean = 0.25 * 0.2554128 = 0.0638532
CORPUS = [["alpha", "beta"], ["alpha", "gamma", "gamma"], ["delta"]]
LN_5_3 = math.log(2.5 / 1.5)


def test_idf_values_and_the_epsilon_floor():
    index = bm25.BM25(CORPUS)
    assert index.idf["beta"] == pytest.approx(LN_5_3)
    assert index.idf["gamma"] == pytest.approx(0.5108256, abs=1e-7)
    assert index.idf["alpha"] == pytest.approx(0.25 * (2 * LN_5_3) / 4)
    assert index.idf["alpha"] == pytest.approx(0.0638532, abs=1e-7)


def test_scores_against_hand_computed_values():
    index = bm25.BM25(CORPUS)
    # gamma in d1: tf 2, dl 3. norm = 1.5 * (0.25 + 0.75 * 3 / 2) = 2.0625
    # 0.5108256 * 2 * 2.5 / (2 + 2.0625) = 0.5108256 * 1.2307692 = 0.6287084
    assert index.scores(["gamma"]) == pytest.approx([0.0, 0.6287084, 0.0], abs=1e-7)
    # alpha in d0: tf 1, dl 2. norm = 1.5. 0.0638532 * 2.5 / 2.5 = 0.0638532
    # alpha in d1: tf 1, dl 3. 0.0638532 * 2.5 / 3.0625 = 0.0521251
    assert index.scores(["alpha"]) == pytest.approx([0.0638532, 0.0521251, 0.0], abs=1e-7)


def test_a_repeated_query_token_counts_twice():
    index = bm25.BM25(CORPUS)
    once = index.scores(["delta"])[2]
    assert index.scores(["delta", "delta"])[2] == pytest.approx(2 * once)


def test_unknown_terms_and_empty_documents_score_zero():
    index = bm25.BM25([["alpha"], []])
    assert index.scores(["zeta"]) == [0.0, 0.0]
    assert index.scores(["alpha"])[1] == 0.0


def test_empty_corpus():
    index = bm25.BM25([])
    assert index.n == 0 and index.scores(["alpha"]) == []
    assert index.matches(["alpha"]) == []


def test_matches_says_which_documents_hold_a_query_token():
    # NOBLIVION-49. "deploy" is in 2 of 4 documents, so its idf is
    # log(2.5) - log(2.5) = 0 and every score is 0. The score cannot say
    # which documents match; the tokens can.
    index = bm25.BM25([["deploy", "window"], ["coffee"], ["deploy"], []])
    assert index.idf["deploy"] == 0.0
    assert index.scores(["how", "do", "deploy"]) == [0.0, 0.0, 0.0, 0.0]
    assert index.matches(["how", "do", "deploy"]) == [True, False, True, False]
    assert index.matches(["window", "coffee"]) == [True, True, False, False]
    assert index.matches(["zeta"]) == [False] * 4
    assert index.matches([]) == [False] * 4


def test_a_stop_word_is_not_a_match():
    # NOBLIVION-76. "the" is in nearly every note. As a match it let unrelated
    # notes with no vector into a hybrid answer, with no score.
    docs = ["deploy the service", "the coffee filter", "the garden hose", "how to brew tea"]
    index = bm25.BM25([bm25.tokenize(d) for d in docs])
    query = bm25.tokenize("How do I deploy the service?")
    assert index.matches(query) == [True, False, False, False]
    assert index.matches(bm25.tokenize("how do I do the")) == [False] * 4
    assert index.matches(["brew"]) == [False, False, False, True]
    # The scores do not change: a stop word adds its weight as before.
    assert index.scores(["the"])[1] > 0


def test_the_stop_words_are_tokens_of_the_tokenizer():
    assert bm25.STOP_WORDS
    for word in bm25.STOP_WORDS:
        assert bm25.tokenize(word) == [word]
