# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranking: BM25 + cosine, fused by reciprocal rank fusion (design doc 0001, section 8).

Interface for the REST service (E3c)::

    service = EmbeddingService(load_embedding_settings())   # noblivion.embedding
    service.start(conn)
    ranker = Ranker(service)
    result = ranker.search(conn, "query text", project="claude_code", top_k=35)
    result.mode            # "hybrid" or "keyword"
    for hit in result.hits:
        hit.rank, hit.row.id, hit.score, hit.fusion_score, hit.row.content

``RankIndex`` is the RAM index. It holds the live rows and the vectors of one
model, and reloads by the revision counters ``meta.content_rev`` and
``meta.vector_rev``, never by a clock (section 6.3). ``rank_pool`` is the pure
fusion step and needs no database.

numpy is optional. Without numpy, or without a usable model, every answer is
keyword-only (section 8.4).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from noblivion import bm25, db, inverted

try:  # numpy is in the optional extra noblivion[embed]
    import numpy as np
except ImportError:  # pragma: no cover - exercised by a monkeypatch test
    np = None  # type: ignore[assignment]

RRF_K = 60
SEM_FLOOR = 0.2
SCORE_DECIMALS = 4  # the weighted cosine, section 8.2
FUSION_DECIMALS = 6  # the fused score, compared and reported at 6 places
MODE_HYBRID = "hybrid"
MODE_KEYWORD = "keyword"
_CACHE_MAX = 64
_CHUNK_READ_BATCH = 500
# NOBLIVION-104. The text of the ``rule``, ``apply`` and ``triggers`` lines counts twice in the
# keyword score of every chunk of its file: the index shows them, so a word in them must find
# the file, and they are short and exact next to a body. The hook re-rank reads the same weight
# (``hooks/corpus.py`` ``INDEX_FIELDS_REPEAT``).
INDEX_FIELDS_REPEAT = 2
# A label of the file (a ticket key, a file name, a tool) that the query names adds this much to
# the keyword score of the file. At most ``LABEL_BOOST_MAX_LABELS`` labels count, so the whole
# boost is at most 0.8: it orders files whose scores are close, and never lifts a weak match
# over a strong one. A BM25 score is about 1 to 15 for a short query.
LABEL_BOOST = 0.4
LABEL_BOOST_MAX_LABELS = 2
# NOBLIVION-153. The keyword-only relevance gate (``keyword_gate``). A word is informative when
# its document frequency is at or under the cut where the words up to it hold half of all word
# uses (``BM25.df_cut``): its idf is above that of the median word of the running text. It is
# rare when it is at or under the cut of the rarest tenth of the word uses (about the top 10% of
# idf). The gate needs a pool of this many units: below it an idf says nothing.
GATE_MIN_UNITS = 20
GATE_INFORMATIVE_MASS = 0.5
GATE_RARE_MASS = 0.1
GATE_MIN_WORDS = 2
_MARKER_LINE_RE = re.compile(r"^\[(?:claude_code_md|transcript_mined): [^\]\n]*\][ \t]*$", re.M)


@dataclass(frozen=True)
class PoolRow:
    """One ranked unit. ``id`` and ``content`` are those of the file row. A
    chunk unit (NOBLIVION-103) holds the tokens of one chunk, and names it in
    ``chunk_id``, ``chunk_index``, ``heading`` and ``chunk_text``. A row that
    has no chunk is one unit with ``chunk_id`` None."""

    id: int
    project: str
    root: str
    path: str
    source_type: str
    content: str
    weight: float
    # The words of the unit. The store keeps them in the inverted index (``slot``) and leaves
    # this empty; only a caller that ranks a small pool of its own (``pick_chunk``) fills it.
    tokens: tuple[str, ...] = field(default=(), repr=False, compare=False)
    is_general: bool = False
    chunk_id: int | None = None
    chunk_index: int = 0
    heading: str = ""
    chunk_text: str = field(default="", repr=False, compare=False)
    labels: tuple[str, ...] = field(default=(), repr=False, compare=False)  # lower case
    # NOBLIVION-105: the index answer reads these two for a mined row (rule and apply lines).
    category: str = ""
    index_text: str = field(default="", repr=False, compare=False)
    slot: int = field(default=-1, repr=False, compare=False)  # the unit in the inverted index


@dataclass(frozen=True)
class Hit:
    rank: int  # 1-based
    row: PoolRow
    score: float | None  # weighted cosine, 4 places; None when the row has no cosine
    fusion_score: float  # 6 places
    bm25: float


@dataclass(frozen=True)
class RankResult:
    mode: str
    hits: list[Hit]
    pool_size: int


def _q(value: float, places: int) -> float:
    return round(float(value), places)


def rank_pool(
    pool: Sequence[PoolRow],
    bm25_scores: Sequence[float],
    cosines: dict[int, float] | None,
    top_k: int,
    matched: Sequence[bool] | None = None,
) -> RankResult:
    """The fusion step of section 8.3 and the keyword mode of section 8.4.

    ``pool`` and ``bm25_scores`` are aligned. ``cosines`` maps a pool position
    to the raw cosine of that row (rows without a vector are absent). ``None``
    or an empty map means keyword mode.

    ``matched`` is aligned with ``pool`` too: True for a row that holds a
    query token (``BM25.matches``). Without it a row matches when its BM25
    score is not 0. That test misses a row whose only query token is in
    exactly half of the pool: the token has an idf of 0 (NOBLIVION-49).
    """
    n = len(pool)
    top_k = max(0, int(top_k))
    bm = [_q(s, FUSION_DECIMALS) for s in bm25_scores]
    if matched is None:
        # In a pool of one or two rows a matched term can get a negative idf
        # (BM25 Okapi), so the test is "not zero", not "above zero".
        matched = [s != 0 for s in bm]
    # The rows that match a query term, in BM25 order. Section 8.4 drops the
    # others: "rows with a BM25 score of 0".
    by_bm25 = sorted((i for i in range(n) if matched[i]), key=lambda i: (-bm[i], pool[i].id))
    if not cosines:
        hits = [
            Hit(rank, pool[i], None, _q(1.0 / (RRF_K + rank), FUSION_DECIMALS), bm[i])
            for rank, i in enumerate(by_bm25[:top_k], start=1)
        ]
        return RankResult(MODE_KEYWORD, hits, n)

    sem = {i: _q(c * pool[i].weight, SCORE_DECIMALS) for i, c in cosines.items()}
    lists = [sorted(sem, key=lambda i: (-sem[i], pool[i].id))]
    if by_bm25 and bm[by_bm25[0]] >= 0:
        # Only the rows that match a query term, as in keyword mode above. A
        # row that matches none gets no fused score from this list, so a row
        # with no cosine and no match is in no list and is not returned.
        lists.append(by_bm25)
    elif by_bm25:
        # The best matched score is negative: every matched term has a
        # negative idf, as in a pool of two rows that both hold it. The row
        # with more uses of the term then has the lower score, so the BM25
        # order is not used and the cosine order decides. A matched row with
        # no cosine is in no other list: it keeps a keyword rank.
        lists.append([i for i in by_bm25 if i not in sem])
    fused: dict[int, float] = {}
    for ranked in lists:
        for rank, i in enumerate(ranked, start=1):
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
    fused = {i: _q(s, FUSION_DECIMALS) for i, s in fused.items()}
    order = sorted(fused, key=lambda i: (-fused[i], pool[i].id))

    floor = _q(1.0 / (RRF_K + n), FUSION_DECIMALS) if n else 0.0
    chosen: list[tuple[int, float]] = []
    for i in order:
        if len(chosen) >= top_k:
            break
        # Kept for parity with the reference: a row in the cosine list always
        # has a fused score of at least the floor, so this never drops a row.
        if sem.get(i, -math.inf) < SEM_FLOOR and fused[i] < floor:
            continue
        chosen.append((i, fused[i]))
    if not chosen and top_k and lists[0]:
        best = lists[0][0]
        chosen.append((best, sem[best]))
    hits = [Hit(rank, pool[i], sem.get(i), f, bm[i]) for rank, (i, f) in enumerate(chosen, start=1)]
    return RankResult(MODE_HYBRID, hits, n)


def parse_labels(raw: object) -> tuple[str, ...]:
    """The ``labels`` column (a JSON list) as lower-case labels. Bad JSON gives no label."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else []
    except ValueError:
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(sorted({v.strip().lower() for v in values if isinstance(v, str) and v.strip()}))


def label_boost(labels: Sequence[str], query_tokens: Iterable[str]) -> float:
    """The bounded keyword boost of a file for the tokens of the query and of the tool target
    (NOBLIVION-104). A label matches a token that is the label, or a path that ends in
    ``/<label>``: the label ``ranking.py`` matches ``src/pkg/ranking.py``."""
    if not labels:
        return 0.0
    tokens = set(query_tokens)
    paths = [t for t in tokens if "/" in t]
    found = sum(
        1 for label in labels if label in tokens or any(p.endswith("/" + label) for p in paths)
    )
    return min(found, LABEL_BOOST_MAX_LABELS) * LABEL_BOOST


def keyword_gate(
    ranked: RankResult,
    index: bm25.BM25 | inverted.PoolView,
    tokens: Sequence[str],
    named: Iterable[str],
) -> RankResult:
    """The relevance gate of keyword-only mode (NOBLIVION-153). Without a cosine no floor can
    judge a row, so any prompt with a word in common got its rows. The gate keeps the rows
    only when the top row holds at least ``GATE_MIN_WORDS`` informative words of the query, or
    when every term of the query that the pool knows is rare and in the top row (a short
    prompt with one rare word), or when a label of the top row is named by the query; else it
    returns no row.

    A stop word is not a term for the gate unless the query has no other term. ``tokens`` are
    the tokens of the query and ``named`` those of the query and the target (the label match
    reads both). A pool under ``GATE_MIN_UNITS`` units passes: its idf is not a measure.
    """
    if ranked.mode != MODE_KEYWORD or not ranked.hits or index.n < GATE_MIN_UNITS:
        return ranked
    top = ranked.hits[0].row
    if top.labels and label_boost(top.labels, named) > 0:
        return ranked
    terms = index.real_terms(tokens)
    terms = {w for w in terms if w not in bm25.STOP_WORDS} or terms
    if isinstance(index, inverted.PoolView):
        shared = {w for w in terms if index.unit_has(top.slot, w)}
    else:
        shared = terms & set(top.tokens)
    # A word in more than half of the units is never informative, whatever the cut.
    cap = index.n // 2
    informative = min(index.df_cut(GATE_INFORMATIVE_MASS), cap)
    rare = min(index.df_cut(GATE_RARE_MASS), cap)
    if sum(1 for w in shared if index.df[w] <= informative) >= GATE_MIN_WORDS:
        return ranked
    if terms and all(w in shared and index.df[w] <= rare for w in terms):
        return ranked
    return RankResult(ranked.mode, [], ranked.pool_size)


def collapse_to_files(
    ranked: RankResult,
    units: Sequence[PoolRow],
    cosines: dict[int, float] | None,
    top_k: int,
    n_files: int,
) -> RankResult:
    """One hit per file from a ranking of chunk units.

    The best unit of a file in the fused order gives the file its place and
    names its chunk. The ``score`` is the best weighted cosine of any chunk of
    the file, so a hook floor on the cosine does not drop a file whose best
    keyword chunk has a lower cosine than another chunk. A file with no
    cosine in any chunk has ``score`` None.
    """
    best_cosine: dict[int, float] = {}
    for i, c in (cosines or {}).items():
        weighted = _q(c * units[i].weight, SCORE_DECIMALS)
        file_id = units[i].id
        if weighted > best_cosine.get(file_id, -math.inf):
            best_cosine[file_id] = weighted
    hits: list[Hit] = []
    seen: set[int] = set()
    for hit in ranked.hits:
        if hit.row.id in seen:
            continue
        seen.add(hit.row.id)
        score = best_cosine.get(hit.row.id)
        hits.append(Hit(len(hits) + 1, hit.row, score, hit.fusion_score, hit.bm25))
        if len(hits) >= top_k:
            break
    return RankResult(ranked.mode, hits, n_files)


def pick_chunk(
    chunks: Sequence[tuple[int, str]],
    query: str,
    vectors: dict[int, bytes] | None = None,
    query_vector: Sequence[float] | None = None,
) -> int | None:
    """The position in ``chunks`` of the chunk that best answers ``query``, or None.

    ``chunks`` holds ``(chunk id, search text)`` of one file. It is the ranking of
    ``RankIndex.rank`` on a pool of one file: BM25 and, when ``query_vector`` and
    the chunk ``vectors`` (chunk id -> float32 blob) are given, the cosine,
    fused by ``rank_pool``. None when no chunk holds a query word and there is no cosine.
    """
    tokens = bm25.tokenize(query)
    if not chunks or not tokens:
        return None
    units = [
        PoolRow(cid, "", "", "", "", "", 1.0, tuple(bm25.tokenize(text))) for cid, text in chunks
    ]
    index = bm25.BM25([u.tokens for u in units])
    cosines: dict[int, float] = {}
    if np is not None and vectors and query_vector:
        dim = len(query_vector)
        q = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if math.isfinite(norm) and norm != 0.0:
            for position, unit in enumerate(units):
                blob = vectors.get(unit.id)
                if blob is not None and len(blob) == dim * 4:
                    cosines[position] = float(np.frombuffer(blob, dtype="<f4") @ (q / norm))
    ranked = rank_pool(units, index.scores(tokens), cosines, 1, index.matches(tokens))
    if not ranked.hits:
        return None
    best = ranked.hits[0].row.id
    return next(i for i, u in enumerate(units) if u.id == best)


# -- RAM index ------------------------------------------------------------------


@dataclass
class _Pool:
    """A cached pool: its units in id order, the view over the shared index, the file count."""

    units: list
    view: inverted.PoolView
    n_files: int
    stamp: int  # the content revision of the snapshot that the units were taken from


@dataclass
class _Snapshot:
    content_rev: int = -1
    vector_rev: int = -1
    rows: dict[int, PoolRow] = field(default_factory=dict)
    units: dict[int, tuple[PoolRow, ...]] = field(default_factory=dict)  # memory_id -> units
    index: inverted.InvertedIndex | None = None
    vectors: dict[int, bytes] = field(default_factory=dict)  # chunk id -> float32 blob
    dims: dict[int, int] = field(default_factory=dict)
    pools: dict = field(default_factory=dict)  # pool key -> _Pool
    matrices: dict = field(default_factory=dict)  # (pool key, dim) -> (positions, matrix)


# The index is rebuilt from the database, not patched, when this share of its slots is dead and
# at least this many are (a changed row leaves its old slots behind).
_COMPACT_SHARE = 0.3
_COMPACT_MIN_DEAD = 5000


class RankIndex:
    """Live rows, the inverted index of their units and the vector matrix of one model.

    ``refresh(conn)`` reads both counters. When one is higher than the loaded
    value, it reads the changed rows (``rev > loaded``) in one read
    transaction and applies them: a changed row takes its old units out of the
    shared inverted index and adds new ones, and the cached pools follow by the units that
    entered or left them (NOBLIVION-157). A lock lets one reload run at a time; a
    search waits for the short apply step of a reload that runs and then ranks on the new
    state. The first load, and a rebuild after many dead units, builds a new index
    beside the old one and swaps it in.
    """

    def __init__(self, model_id: str | None = None) -> None:
        self._model_id = model_id
        self._snap = _Snapshot()
        self._lock = threading.Lock()  # one reload at a time
        self._ilock = threading.RLock()  # the index and its pools: a reload applies, a search reads
        self.bm25_builds = 0  # pools made from scratch; for tests and /health
        self.pool_syncs = 0  # pools brought up to date by their changed units
        self.reloads = 0

    @property
    def model_id(self) -> str | None:
        return self._model_id

    @property
    def revisions(self) -> tuple[int, int]:
        snap = self._snap
        return snap.content_rev, snap.vector_rev

    def set_model(self, model_id: str | None) -> None:
        """Use the vectors of another model. The next refresh loads them all."""
        with self._lock:
            if model_id == self._model_id:
                return
            self._model_id = model_id
            with self._ilock:
                old = self._snap
                self._snap = _Snapshot(
                    content_rev=old.content_rev,
                    vector_rev=-1,
                    rows=old.rows,
                    units=old.units,
                    index=old.index,
                    pools=old.pools,
                )

    def refresh(self, conn: sqlite3.Connection) -> bool:
        """Reload when a counter moved. Returns True when a reload ran."""
        content_rev, vector_rev = db.revisions(conn)
        snap = self._snap
        if content_rev <= snap.content_rev and vector_rev <= snap.vector_rev:
            return False
        with self._lock:
            snap = self._snap
            if content_rev <= snap.content_rev and vector_rev <= snap.vector_rev:
                return False  # another thread reloaded while we waited
            self._snap = self._reload(conn, snap)
            self.reloads += 1
            return True

    def _reload(self, conn: sqlite3.Connection, old: _Snapshot) -> _Snapshot:
        model = self._model_id
        index = old.index
        # A new index beside the old one: the first load, and a rebuild when too many slots are
        # dead. It reads every live row, so it asks for the changes since the start.
        fresh = index is None or (
            index.dead >= _COMPACT_MIN_DEAD and index.dead > _COMPACT_SHARE * len(index)
        )
        since = -1 if fresh else old.content_rev
        changed_vectors: list = []
        with db.read_tx(conn):
            content_rev, vector_rev = db.revisions(conn)
            new = _Snapshot(content_rev=content_rev, vector_rev=vector_rev)
            if content_rev > old.content_rev:
                changed_rows = conn.execute(
                    "SELECT m.id, m.project, m.root, m.path, m.source_type, m.category, "
                    "m.content, m.weight, m.labels, m.index_text, m.archived_at, m.deleted_at, "
                    "EXISTS (SELECT 1 FROM learning_notes n "
                    "WHERE n.project=m.project AND n.root=m.root AND n.path=m.path "
                    "AND n.file_hash=m.hash AND n.status='active' AND n.scope='general' "
                    "AND n.category IN ('preference','workflow')) is_general "
                    "FROM memories m WHERE m.rev > ?",
                    (since,),
                ).fetchall()
                # A physical forget has no changed row to reload. Remove old
                # cached rows by checking live identities in this same snapshot.
                live_ids = {
                    int(row[0])
                    for row in conn.execute(
                        "SELECT id FROM memories WHERE archived_at IS NULL AND deleted_at IS NULL"
                    )
                }
                self._apply_changes(conn, old, new, changed_rows, live_ids, fresh)
            else:
                new.rows, new.units, new.index = old.rows, old.units, old.index
                new.pools = old.pools  # the index stays: no content change
            if model is not None and vector_rev > old.vector_rev:
                changed_vectors = conn.execute(
                    "SELECT chunk_id, dim, blob FROM chunk_vectors WHERE model = ? AND rev > ?",
                    (model, old.vector_rev),
                ).fetchall()
        with self._ilock:
            self._finish(new, old, changed_vectors, content_rev)
            self._snap = new
        return new

    def _apply_changes(
        self,
        conn: sqlite3.Connection,
        old: _Snapshot,
        new: _Snapshot,
        changed_rows: list,
        live_ids: set[int],
        fresh: bool,
    ) -> None:
        """Take the changed rows into the index and into ``new``. The rows are cut into words
        and added one batch at a time, so the words of the whole store are never in memory
        together. A fresh index is built beside the old one, with no lock; a patch of the
        old index holds the lock, and its work is the changed rows only."""
        changed_ids = {int(r["id"]) for r in changed_rows}
        if fresh:
            idx = inverted.InvertedIndex()
            rows: dict[int, PoolRow] = {}
            units: dict[int, tuple[PoolRow, ...]] = {}
            new.pools = {}
            self._add_rows(conn, idx, changed_rows, live_ids, rows, units)
        else:
            idx = old.index  # type: ignore[assignment]
            with self._ilock:
                units = {mid: u for mid, u in old.units.items() if mid in live_ids}
                for mid, gone in old.units.items():
                    if mid in changed_ids or mid not in live_ids:
                        for u in gone:
                            idx.remove(u.slot)
                        units.pop(mid, None)
                rows = {
                    mid: row
                    for mid, row in old.rows.items()
                    if mid in live_ids and mid not in changed_ids
                }
                self._add_rows(conn, idx, changed_rows, live_ids, rows, units)
                new.pools = old.pools
        new.rows, new.units, new.index = rows, units, idx

    @staticmethod
    def _add_rows(
        conn: sqlite3.Connection,
        idx: inverted.InvertedIndex,
        changed_rows: list,
        live_ids: set[int],
        rows: dict[int, PoolRow],
        units: dict[int, tuple[PoolRow, ...]],
    ) -> None:
        live = [r for r in changed_rows if int(r["id"]) in live_ids]
        for start in range(0, len(live), _CHUNK_READ_BATCH):
            part = live[start : start + _CHUNK_READ_BATCH]
            # All chunks of a changed row: the chunks of one row are read together.
            chunks: dict[int, list] = {}
            marks = ",".join("?" * len(part))
            for c in conn.execute(
                "SELECT parent_id, id, chunk_index, heading, text, search_text "
                f"FROM memory_chunks WHERE parent_id IN ({marks}) "
                "ORDER BY parent_id, chunk_index",
                [int(r["id"]) for r in part],
            ):
                chunks.setdefault(int(c["parent_id"]), []).append(c)
            for r in part:
                mid = int(r["id"])
                field_tokens = bm25.tokenize(r["index_text"]) * INDEX_FIELDS_REPEAT
                row = PoolRow(
                    id=mid,
                    project=r["project"],
                    root=r["root"],
                    path=r["path"],
                    source_type=r["source_type"],
                    content=r["content"],
                    weight=float(r["weight"]),
                    is_general=bool(r["is_general"]),
                    labels=parse_labels(r["labels"]),
                    category=r["category"],
                    index_text=r["index_text"],
                )
                rows[mid] = row
                found = chunks.get(mid)
                if found:
                    # The marker line (the file path) is part of the keyword text of every chunk
                    # of its file, as it was part of the keyword text of the file row.
                    marker = _MARKER_LINE_RE.search(row.content)
                    path_tokens = bm25.tokenize(marker.group(0)) if marker else []
                    units[mid] = tuple(
                        replace(
                            row,
                            chunk_id=int(c["id"]),
                            chunk_index=int(c["chunk_index"]),
                            heading=c["heading"],
                            chunk_text=c["text"],
                            slot=idx.add(
                                bm25.tokenize(c["search_text"]) + path_tokens + field_tokens
                            ),
                        )
                        for c in found
                    )
                else:
                    # A row that changed and has no chunk row (a writer that skipped them) is
                    # one unit of its own.
                    units[mid] = (
                        replace(row, slot=idx.add(bm25.tokenize(row.content) + field_tokens)),
                    )

    def _finish(
        self, new: _Snapshot, old: _Snapshot, changed_vectors: list, content_rev: int
    ) -> None:
        if changed_vectors:
            new.vectors = dict(old.vectors)
            new.dims = dict(old.dims)
            for r in changed_vectors:
                new.vectors[int(r["chunk_id"])] = bytes(r["blob"])
                new.dims[int(r["chunk_id"])] = int(r["dim"])
        else:
            new.vectors, new.dims = old.vectors, old.dims
            if content_rev == old.content_rev:
                new.matrices = old.matrices
        if content_rev > old.content_rev:
            live_chunks = {
                u.chunk_id for found in new.units.values() for u in found if u.chunk_id is not None
            }
            new.vectors = {k: v for k, v in new.vectors.items() if k in live_chunks}
            new.dims = {k: v for k, v in new.dims.items() if k in live_chunks}

    # -- pools ----------------------------------------------------------------

    @staticmethod
    def pool_key(
        project: str, root: str | None, shared_roots: Iterable[str], include_mined: bool
    ) -> tuple:
        if root is None:
            return (project, None, None, bool(include_mined))
        return (project, root, tuple(sorted(set(shared_roots) - {root})), bool(include_mined))

    def _pool(self, snap: _Snapshot, key: tuple) -> tuple[list[PoolRow], inverted.PoolView, int]:
        cached = snap.pools.get(key)
        if cached is not None and snap.index is not None and cached.view.index is not snap.index:
            cached = None  # a view made before the first load
        if cached is not None and cached.stamp == snap.content_rev:
            return cached.units, cached.view, cached.n_files
        project, root, shared, include_mined = key
        # The imported rows (``db.IMPORT_ROOT``) belong to no project
        # folder, so every session searches them, like a shared root.
        roots = None if root is None else {root, *shared, db.IMPORT_ROOT}
        rows = [
            r
            for r in snap.rows.values()
            if r.project == project
            and (roots is None or r.root in roots or r.is_general)
            and (include_mined or r.source_type != db.SOURCE_MINED)
        ]
        if root is not None and shared:
            # NOBLIVION-31: a memory file of the session's root wins over a
            # file with the same name in a shared root (the global folder).
            own = {r.path for r in rows if r.root == root and r.source_type == db.SOURCE_MD}
            rows = [
                r
                for r in rows
                if r.root == root or r.source_type != db.SOURCE_MD or r.path not in own
            ]
        rows.sort(key=lambda r: r.id)
        # One unit per chunk (BM25 and cosine are per chunk), or one unit for a
        # row that has no chunk. The units of a row stay together, in chunk order.
        units = [u for r in rows for u in snap.units[r.id]]
        slots = [u.slot for u in units]
        if cached is not None:
            # A content change: only the units that entered or left the pool touch its counts.
            cached.view.sync(slots)
            self.pool_syncs += 1
            view = cached.view
        else:
            view = inverted.PoolView(snap.index or inverted.InvertedIndex(), slots)
            self.bm25_builds += 1
            if len(snap.pools) >= _CACHE_MAX:
                snap.pools.clear()
        snap.pools[key] = _Pool(units, view, len(rows), snap.content_rev)
        return units, view, len(rows)

    def _cosines(
        self, snap: _Snapshot, key: tuple, rows: Sequence[PoolRow], query_vector: Sequence[float]
    ) -> dict[int, float]:
        if np is None or not snap.vectors:
            return {}
        dim = len(query_vector)
        mkey = (key, dim)
        cached = snap.matrices.get(mkey)
        if cached is None:
            positions = [
                i
                for i, r in enumerate(rows)
                if r.chunk_id in snap.vectors and snap.dims.get(r.chunk_id) == dim
            ]
            if positions:
                blob = b"".join(snap.vectors[rows[i].chunk_id] for i in positions)
                matrix = np.frombuffer(blob, dtype="<f4").reshape(len(positions), dim)
            else:
                matrix = np.zeros((0, dim), dtype="<f4")
            cached = (positions, matrix)
            if len(snap.matrices) >= _CACHE_MAX:
                snap.matrices.clear()
            snap.matrices[mkey] = cached
        positions, matrix = cached
        if not positions:
            return {}
        q = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if not math.isfinite(norm) or norm == 0.0:
            return {}
        sims = matrix @ (q / norm)
        return {i: float(s) for i, s in zip(positions, sims.tolist(), strict=True)}

    def rank(
        self,
        query: str,
        *,
        project: str,
        top_k: int,
        query_vector: Sequence[float] | None = None,
        root: str | None = None,
        shared_roots: Iterable[str] = (),
        include_mined: bool = False,
        target: str = "",
        gate: bool = False,
    ) -> RankResult:
        """Rank the pool of section 8.1 for ``query``. No database access.

        Hybrid when ``query_vector`` is given and at least one pool row has a
        vector of the index model with the same dim; keyword-only otherwise.
        ``target`` is the text of the tool target (a path or a command): it takes part in the
        label boost only, never in the keyword score (NOBLIVION-104). ``gate`` applies the keyword
        gate (``keyword_gate``) to a keyword-only answer; the prompt index asks for it.
        """
        with self._ilock:
            return self._rank(
                query,
                project=project,
                top_k=top_k,
                query_vector=query_vector,
                root=root,
                shared_roots=shared_roots,
                include_mined=include_mined,
                target=target,
                gate=gate,
            )

    def _rank(
        self,
        query: str,
        *,
        project: str,
        top_k: int,
        query_vector: Sequence[float] | None,
        root: str | None,
        shared_roots: Iterable[str],
        include_mined: bool,
        target: str,
        gate: bool,
    ) -> RankResult:
        snap = self._snap
        key = self.pool_key(project, root, shared_roots, include_mined)
        units, index, n_files = self._pool(snap, key)
        if not units or not query.strip():
            mode = MODE_HYBRID if query_vector is not None and units else MODE_KEYWORD
            return RankResult(mode, [], n_files)
        tokens = bm25.tokenize(query)
        scores = index.scores(tokens)
        matched = index.matches(tokens)
        named = {*tokens, *bm25.tokenize(target)}
        if any(u.labels for u in units):
            # NOBLIVION-104: a label that the query or the target names adds a bounded boost to
            # the score of a file. It does not make a file a keyword match: a file that holds no
            # query word stays out, as before.
            per_file: dict[int, float] = {}
            boosts = [
                per_file.setdefault(u.id, label_boost(u.labels, named)) if u.labels else 0.0
                for u in units
            ]
            if any(boosts):
                scores = [s + b for s, b in zip(scores, boosts, strict=True)]
        cosines = self._cosines(snap, key, units, query_vector) if query_vector else {}
        # Rank every unit, then keep the best unit of each file (NOBLIVION-103).
        ranked = rank_pool(units, scores, cosines, len(units), matched)
        collapsed = collapse_to_files(ranked, units, cosines, top_k, n_files)
        return keyword_gate(collapsed, index, tokens, named) if gate else collapsed


class Ranker:
    """The search entry point for the REST service: refresh, embed, rank.

    ``embedding`` is an ``EmbeddingService`` or None (keyword-only).
    """

    def __init__(self, embedding=None, *, query_timeout_s: float = 1.0) -> None:
        self.embedding = embedding
        self.query_timeout_s = query_timeout_s
        self.index = RankIndex(embedding.model_id if embedding is not None else None)

    def query_vector(self, conn: sqlite3.Connection, query: str) -> list[float] | None:
        """The embedding of ``query`` with the query time limit, or None (no model,
        no numpy, a slow or failed embed): the caller then ranks by keyword only."""
        if self.embedding is None or np is None or self.embedding.model_id is None:
            return None
        if not query.strip():
            return None
        return self.embedding.embed_query(query, self.query_timeout_s, conn)

    def search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        project: str,
        top_k: int,
        root: str | None = None,
        shared_roots: Iterable[str] = (),
        include_mined: bool = False,
        target: str = "",
        gate: bool = False,
    ) -> RankResult:
        model_id = self.embedding.model_id if self.embedding is not None else None
        if np is None:
            model_id = None
        self.index.set_model(model_id)
        self.index.refresh(conn)
        vector = None
        if model_id is not None and query.strip():
            vector = self.embedding.embed_query(query, self.query_timeout_s, conn)
        return self.index.rank(
            query,
            project=project,
            top_k=top_k,
            query_vector=vector,
            root=root,
            shared_roots=shared_roots,
            include_mined=include_mined,
            target=target,
            gate=gate,
        )
