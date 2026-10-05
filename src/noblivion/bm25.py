# SPDX-License-Identifier: AGPL-3.0-or-later
"""BM25 Okapi and the tokenizer of the keyword leg (design doc 0001, section 8.2).

Pure stdlib, so keyword-only ranking works without numpy.

The class reproduces BM25Okapi as the ``rank_bm25`` package implements it, as
the reference hooks did. Two details matter and are easy to get wrong:

- The inverse document frequency of a term found in more than half of the
  documents is negative. Every negative value is replaced by ``epsilon`` times
  the mean idf, where the mean is taken over the values before the
  replacement, negative values included.
- A query token that occurs twice in the query counts twice.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

K1 = 1.5
B = 0.75
EPSILON = 0.25

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9_.\-/]+")
_SUB_TOKEN_SPLIT_RE = re.compile(r"[_/.\-]+")

# Common English words that never make a keyword match (``BM25.matches``,
# NOBLIVION-76). Nearly every note holds "the", so as a match it let unrelated
# notes with no vector into a hybrid answer. Only articles, pronouns,
# auxiliary verbs, prepositions, conjunctions and question words, and the
# part of a contraction that the tokenizer keeps (``doesn`` of "doesn't"): a
# word that can name a task (``up``, ``down``, ``off``, ``out``) is not in
# the list. The scores do not use the list.
STOP_WORDS = frozenset(
    """
    about above after again am an and are as at be been before being below between
    both but by can could did do does doing don during each for from further had has
    have having he her here hers herself him himself his how if in into is it its
    itself just me more most my myself no nor not of on once only or other our ours
    ourselves own same she should so some such than that the their theirs them
    themselves then there these they this those through to too until very was we
    were what when where which while who whom why will with would you your yours
    yourself yourselves
    aren couldn didn doesn hadn hasn haven isn shouldn wasn weren won wouldn
    """.split()
)


def tokenize(text: str) -> list[str]:
    """The tokenizer of the reference store: lower case, split on every
    character that is not ``a-z 0-9 _ . - /``, keep tokens of 2 or more
    characters."""
    return [t for t in _TOKEN_SPLIT_RE.split(text.lower()) if len(t) >= 2]


def tokenize_sub(text: str) -> list[str]:
    """``tokenize``, plus the parts of a token that splits on ``_ / . -`` into
    two or more parts of two or more characters. The hook re-rank of the
    reference implementation used it; the store leg uses ``tokenize``."""
    out: list[str] = []
    for token in tokenize(text):
        out.append(token)
        parts = [p for p in _SUB_TOKEN_SPLIT_RE.split(token) if len(p) >= 2]
        if len(parts) > 1:
            out.extend(parts)
    return out


class BM25:
    """BM25 Okapi over one corpus of token lists.

    The document frequencies cover the whole corpus given here. The caller
    builds one instance per pool (section 8.1), so a row outside the pool
    cannot change the scores inside it.
    """

    __slots__ = ("idf", "tf", "dl", "avgdl", "n", "k1", "b")

    def __init__(
        self,
        corpus_tokens: Sequence[Sequence[str]],
        k1: float = K1,
        b: float = B,
        epsilon: float = EPSILON,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.n = len(corpus_tokens)
        self.tf = [Counter(doc) for doc in corpus_tokens]
        self.dl = [len(doc) for doc in corpus_tokens]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for counts in self.tf:
            df.update(counts.keys())
        idf: dict[str, float] = {}
        total = 0.0
        negative: list[str] = []
        for word, count in df.items():
            value = math.log(self.n - count + 0.5) - math.log(count + 0.5)
            idf[word] = value
            total += value
            if value < 0:
                negative.append(word)
        if idf:
            floor = epsilon * (total / len(idf))
            for word in negative:
                idf[word] = floor
        self.idf = idf

    def score(self, query_tokens: Sequence[str], index: int) -> float:
        tf = self.tf[index]
        if not tf:
            return 0.0
        norm = self.k1 * (1 - self.b + self.b * self.dl[index] / self.avgdl)
        total = 0.0
        for word in query_tokens:
            freq = tf.get(word, 0)
            if not freq:
                continue
            total += self.idf.get(word, 0.0) * freq * (self.k1 + 1) / (freq + norm)
        return total

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        """One score per document, in corpus order."""
        return [self.score(query_tokens, i) for i in range(self.n)]

    def matches(self, query_tokens: Sequence[str]) -> list[bool]:
        """One flag per document, in corpus order: True when the document
        holds a query token that is not in STOP_WORDS. The score cannot say
        this: a token in exactly half of the documents has an idf of 0, so it
        adds 0 to the score."""
        words = (set(query_tokens) - STOP_WORDS) & self.idf.keys()
        return [any(word in tf for word in words) for tf in self.tf]
