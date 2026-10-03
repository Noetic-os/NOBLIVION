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
