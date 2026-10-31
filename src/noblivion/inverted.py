# SPDX-License-Identifier: AGPL-3.0-or-later
"""One inverted index for every pool of the store (NOBLIVION-157).

``InvertedIndex`` holds the term postings of every ranked unit (a chunk, or a row that has no
chunk) once: a term id for each distinct word, and for each term two parallel arrays of unit
slots and term frequencies. A unit needs no token list after it is added, so the index costs
about 8 bytes for each distinct word of a unit, where a token list held one string object
for each word use. A changed row removes its own units (the slot is marked dead) and adds new
ones; no other posting moves.

``PoolView`` is the statistics of one pool (section 8.1) over that index: its document
frequencies, its length sum and the idf of every word, kept by the units that enter or leave
the pool. It answers the same questions as ``bm25.BM25`` (``scores``, ``matches``,
``real_terms``, ``df``, ``df_cut``) and gives the same scores: a pool is a filter over the
postings at query time, and the idf, the mean idf for the floor and the average length are
those of the pool alone, so a unit outside the pool cannot change a score inside it.
``bm25.BM25`` stays as the reference that the tests compare with.

Not thread safe. The caller holds one lock for the index and its views.
"""

from __future__ import annotations

import math
from array import array
from collections import Counter
from collections.abc import Iterable, Sequence

# `zip(strict=)` needs Python 3.10. The store also runs on 3.9 in the eval suite lane of CI, and the
# pairs here are two arrays of one length, made together in `InvertedIndex.add`, so no strict check.
from noblivion import bm25


class InvertedIndex:
    """Postings of all units. A slot is never reused; ``dead`` counts removed slots."""

    __slots__ = ("vocab", "words", "post_slot", "post_tf", "dl", "alive", "fterm", "ftf", "dead")

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.words: list[str] = []
        self.post_slot: list[array] = []  # term id -> slots, ascending
        self.post_tf: list[array] = []  # term id -> term frequency, aligned with post_slot
        self.dl = array("I")  # slot -> length in tokens
        self.alive = bytearray()
        self.fterm: list[array | None] = []  # slot -> distinct term ids
        self.ftf: list[array | None] = []  # slot -> term frequencies, aligned with fterm
        self.dead = 0

    def __len__(self) -> int:
        return len(self.dl)

    @property
    def live(self) -> int:
        return len(self.dl) - self.dead

    def add(self, tokens: Sequence[str]) -> int:
        """Add one unit. Returns its slot."""
        slot = len(self.dl)
        counts = Counter(tokens)
        vocab = self.vocab
        post_slot = self.post_slot
        post_tf = self.post_tf
        ids: list[int] = []
        for word, freq in counts.items():
            tid = vocab.get(word)
            if tid is None:
                tid = len(self.words)
                vocab[word] = tid
                self.words.append(word)
                post_slot.append(array("I"))
                post_tf.append(array("I"))
            post_slot[tid].append(slot)
            post_tf[tid].append(freq)
            ids.append(tid)
        terms = array("I", ids)
        freqs = array("I", counts.values())
        self.dl.append(len(tokens))
        self.alive.append(1)
        self.fterm.append(terms)
        self.ftf.append(freqs)
        return slot

    def remove(self, slot: int) -> None:
        """Mark a unit dead. Its postings stay until the index is rebuilt; a pool view never
        holds a dead slot, so a score never reads one. Its term list stays too, because a
        view that held the slot needs it to take the slot out of its counts."""
        if self.alive[slot]:
            self.alive[slot] = 0
            self.dead += 1

    def holds(self, slot: int, word: str) -> bool:
        tid = self.vocab.get(word)
        return tid is not None and slot < len(self.fterm) and tid in (self.fterm[slot] or ())

    def term_freq(self, slot: int, word: str) -> int:
        tid = self.vocab.get(word)
        terms = self.fterm[slot] if slot < len(self.fterm) else None
        if tid is None or not terms:
            return 0
        for t, f in zip(terms, self.ftf[slot] or ()):  # noqa: B905 - 3.9 lane
            if t == tid:
                return int(f)
        return 0


class _Df:
    """``view.df[word]``: the document frequency in the pool, 0 for an unknown word."""

    __slots__ = ("_view",)

    def __init__(self, view: PoolView) -> None:
        self._view = view

    def __getitem__(self, word: str) -> int:
        tid = self._view.index.vocab.get(word)
        return self._view.df_by_id.get(tid, 0) if tid is not None else 0


class PoolView:
    """BM25 Okapi statistics of one pool over a shared ``InvertedIndex``."""

    def __init__(
        self,
        index: InvertedIndex,
        slots: Sequence[int],
        k1: float = bm25.K1,
        b: float = bm25.B,
        epsilon: float = bm25.EPSILON,
    ) -> None:
        self.index = index
        self.k1 = k1
        self.b = b
        self.epsilon = epsilon
        self.slots: list[int] = []
        self.pos: dict[int, int] = {}
        self.sumdl = 0
        self.df_by_id: Counter[int] = Counter()
        self.hist: Counter[int] = Counter()  # document frequency -> how many words have it
        self._cf: Counter[int] | None = None  # collection frequency, made on demand
        self.df = _Df(self)
        self._norm: list[float] | None = None
        self._floor: float | None = None
        self._cuts: dict[float, int] = {}
        self.syncs = 0  # for tests: units that entered or left since the view was made
        self._load(slots)

    @property
    def n(self) -> int:
        return len(self.slots)

    # -- keeping the counts -------------------------------------------------------

    def _load(self, slots: Sequence[int]) -> None:
        idx = self.index
        for slot in slots:
            self.df_by_id.update(idx.fterm[slot])  # type: ignore[arg-type]
            self.sumdl += idx.dl[slot]
        self.hist = Counter(self.df_by_id.values())
        self._set_slots(slots)

    def _set_slots(self, slots: Sequence[int]) -> None:
        self.slots = list(slots)
        self.pos = {slot: i for i, slot in enumerate(self.slots)}
        self._norm = None
        self._floor = None
        self._cuts = {}

    def sync(self, slots: Sequence[int]) -> None:
        """Make the pool hold exactly ``slots`` (in that order). Only the units that
        entered or left the pool touch the counts."""
        if len(slots) == len(self.slots) and list(slots) == self.slots:
            return
        new = set(slots)
        idx = self.index
        for slot in [s for s in self.pos if s not in new]:
            self._leave(idx, slot)
        for slot in slots:
            if slot not in self.pos:
                self._enter(idx, slot)
        self._set_slots(slots)

    def _enter(self, idx: InvertedIndex, slot: int) -> None:
        self.syncs += 1
        self.sumdl += idx.dl[slot]
        cf = self._cf
        for tid, freq in zip(idx.fterm[slot], idx.ftf[slot]):  # type: ignore[arg-type]  # noqa: B905 - 3.9 lane
            d = self.df_by_id.get(tid, 0)
            if d:
                self._hist_move(d, -1)
            self.df_by_id[tid] = d + 1
            self.hist[d + 1] += 1
            if cf is not None:
                cf[tid] += freq

    def _leave(self, idx: InvertedIndex, slot: int) -> None:
        self.syncs += 1
        self.sumdl -= idx.dl[slot]
        cf = self._cf
        for tid, freq in zip(idx.fterm[slot], idx.ftf[slot]):  # type: ignore[arg-type]  # noqa: B905 - 3.9 lane
            d = self.df_by_id[tid]
            self._hist_move(d, -1)
            if d == 1:
                del self.df_by_id[tid]
            else:
                self.df_by_id[tid] = d - 1
                self.hist[d - 1] += 1
            if cf is not None:
                cf[tid] -= freq
                if not cf[tid]:
                    del cf[tid]

    def _hist_move(self, d: int, step: int) -> None:
        self.hist[d] += step
        if not self.hist[d]:
            del self.hist[d]

    # -- statistics --------------------------------------------------------------------

    def _floor_value(self) -> float:
        """``epsilon`` times the mean idf over every word of the pool, before the
        replacement of the negative values (as ``bm25.BM25`` does it)."""
        if self._floor is None:
            n = self.n
            total = 0.0
            count = 0
            for d, c in self.hist.items():
                total += c * (math.log(n - d + 0.5) - math.log(d + 0.5))
                count += c
            self._floor = self.epsilon * (total / count) if count else 0.0
        return self._floor

    def idf_of(self, word: str) -> float:
        """The idf of ``word`` in this pool, 0.0 for a word the pool does not hold."""
        tid = self.index.vocab.get(word)
        d = self.df_by_id.get(tid, 0) if tid is not None else 0
        return self._idf(d) if d else 0.0

    def _idf(self, d: int) -> float:
        value = math.log(self.n - d + 0.5) - math.log(d + 0.5)
        return value if value >= 0 else self._floor_value()

    def _norms(self) -> list[float]:
        if self._norm is None:
            idx = self.index
            avgdl = (self.sumdl / self.n) if self.n else 0.0
            self._norm = [
                self.k1 * (1 - self.b + self.b * idx.dl[slot] / avgdl) if avgdl else 0.0
                for slot in self.slots
            ]
        return self._norm

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        """One score per unit of the pool, in pool order. A query word that occurs twice
        counts twice."""
        n = self.n
        out = [0.0] * n
        if not n:
            return out
        idx = self.index
        norm = self._norms()
        pos = self.pos
        k1 = self.k1
        for word in query_tokens:
            tid = idx.vocab.get(word)
            if tid is None:
                continue
            d = self.df_by_id.get(tid, 0)
            if not d:
                continue
            idf = self._idf(d)
            for slot, freq in zip(idx.post_slot[tid], idx.post_tf[tid]):  # noqa: B905 - 3.9 lane
                i = pos.get(slot)
                if i is not None:
                    out[i] += idf * freq * (k1 + 1) / (freq + norm[i])
        return out

    def real_terms(self, query_tokens: Iterable[str]) -> set[str]:
        """As ``bm25.BM25.real_terms``, over this pool."""
        n = self.n
        out = set()
        for word in set(query_tokens):
            d = self.df[word]
            if d and (word not in bm25.STOP_WORDS or d * bm25.STOP_WORD_RARE_SHARE_DIV <= n):
                out.add(word)
        return out

    def is_common(self, word: str) -> bool:
        """True when the pool holds ``word`` and its idf is negative before the replacement:
        the word is in more than half of the units (``bm25.BM25`` calls these ``negative``)."""
        d = self.df[word]
        return bool(d) and (math.log(self.n - d + 0.5) - math.log(d + 0.5)) < 0

    def matches(self, query_tokens: Sequence[str]) -> list[bool]:
        """One flag per unit: True when it holds a real term of the query."""
        flags = [False] * self.n
        idx = self.index
        pos = self.pos
        for word in self.real_terms(query_tokens):
            for slot in idx.post_slot[idx.vocab[word]]:
                i = pos.get(slot)
                if i is not None:
                    flags[i] = True
        return flags

    def unit_has(self, slot: int, word: str) -> bool:
        return self.index.holds(slot, word)

    def df_cut(self, mass: float) -> int:
        """As ``bm25.BM25.df_cut``: the document frequency at which the words up to it
        hold ``mass`` of all word uses in the pool."""
        cached = self._cuts.get(mass)
        if cached is not None:
            return cached
        if self._cf is None:
            idx = self.index
            cf: Counter[int] = Counter()
            for slot in self.slots:
                for tid, freq in zip(idx.fterm[slot], idx.ftf[slot]):  # type: ignore[arg-type]  # noqa: B905 - 3.9 lane
                    cf[tid] += freq
            self._cf = cf
        uses: Counter[int] = Counter()
        for tid, freq in self._cf.items():
            uses[self.df_by_id[tid]] += freq
        total = sum(uses.values())
        seen = 0
        cut = 0
        for frequency in sorted(uses):
            seen += uses[frequency]
            cut = frequency
            if seen >= mass * total:
                break
        self._cuts[mass] = cut
        return cut
