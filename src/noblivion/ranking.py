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

import math
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from noblivion import bm25, db

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


@dataclass(frozen=True)
class PoolRow:
    id: int
    project: str
    root: str
    path: str
    source_type: str
    content: str
    weight: float
    tokens: tuple[str, ...] = field(repr=False, compare=False)


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
) -> RankResult:
    """The fusion step of section 8.3 and the keyword mode of section 8.4.

    ``pool`` and ``bm25_scores`` are aligned. ``cosines`` maps a pool position
    to the raw cosine of that row (rows without a vector are absent). ``None``
    or an empty map means keyword mode.
    """
    n = len(pool)
    top_k = max(0, int(top_k))
    bm = [_q(s, FUSION_DECIMALS) for s in bm25_scores]
    if not cosines:
        # "Drop rows with a BM25 score of 0" (section 8.4): a row that matches
        # no query term. In a pool of one or two rows a matched term can get a
        # negative idf (BM25 Okapi), so the test is "not zero", not "above zero".
        order = sorted((i for i in range(n) if bm[i] != 0), key=lambda i: (-bm[i], pool[i].id))
        hits = [
            Hit(rank, pool[i], None, _q(1.0 / (RRF_K + rank), FUSION_DECIMALS), bm[i])
            for rank, i in enumerate(order[:top_k], start=1)
        ]
        return RankResult(MODE_KEYWORD, hits, n)

    sem = {i: _q(c * pool[i].weight, SCORE_DECIMALS) for i, c in cosines.items()}
    lists = [sorted(sem, key=lambda i: (-sem[i], pool[i].id))]
    if n and max(bm) > 0:
        lists.append(sorted(range(n), key=lambda i: (-bm[i], pool[i].id)))
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


# -- RAM index ------------------------------------------------------------------


@dataclass
class _Snapshot:
    content_rev: int = -1
    vector_rev: int = -1
    rows: dict[int, PoolRow] = field(default_factory=dict)
    vectors: dict[int, bytes] = field(default_factory=dict)  # memory_id -> float32 blob
    dims: dict[int, int] = field(default_factory=dict)
    pools: dict = field(default_factory=dict)  # pool key -> (rows, BM25)
    matrices: dict = field(default_factory=dict)  # (pool key, dim) -> (positions, matrix)


class RankIndex:
    """Live rows, their BM25 indexes and the vector matrix of one model.

    ``refresh(conn)`` reads both counters. When one is higher than the loaded
    value, it reads the changed rows (``rev > loaded``) in one read
    transaction, applies them to a copy and swaps the copy in. A lock lets one
    reload run at a time; a search during a reload uses the old snapshot. A
    ``vector_rev`` change keeps the BM25 indexes; only a ``content_rev``
    change drops them.
    """

    def __init__(self, model_id: str | None = None) -> None:
        self._model_id = model_id
        self._snap = _Snapshot()
        self._lock = threading.Lock()
        self.bm25_builds = 0  # for tests and /health
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
            old = self._snap
            self._snap = _Snapshot(
                content_rev=old.content_rev, vector_rev=-1, rows=old.rows, pools=old.pools
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
        with db.read_tx(conn):
            content_rev, vector_rev = db.revisions(conn)
            changed_rows = []
            if content_rev > old.content_rev:
                changed_rows = conn.execute(
                    "SELECT id, project, root, path, source_type, content, weight, "
                    "archived_at, deleted_at FROM memories WHERE rev > ?",
                    (old.content_rev,),
                ).fetchall()
            changed_vectors = []
            if model is not None and vector_rev > old.vector_rev:
                changed_vectors = conn.execute(
                    "SELECT memory_id, dim, blob FROM vectors WHERE model = ? AND rev > ?",
                    (model, old.vector_rev),
                ).fetchall()
        new = _Snapshot(content_rev=content_rev, vector_rev=vector_rev)
        if content_rev > old.content_rev:
            rows = dict(old.rows)
            for r in changed_rows:
                if r["archived_at"] is None and r["deleted_at"] is None:
                    rows[int(r["id"])] = PoolRow(
                        id=int(r["id"]),
                        project=r["project"],
                        root=r["root"],
                        path=r["path"],
                        source_type=r["source_type"],
                        content=r["content"],
                        weight=float(r["weight"]),
                        tokens=tuple(bm25.tokenize(r["content"])),
                    )
                else:
                    rows.pop(int(r["id"]), None)
            new.rows = rows
        else:
            new.rows = old.rows
            new.pools = old.pools  # BM25 stays: no content change
        if changed_vectors:
            new.vectors = dict(old.vectors)
            new.dims = dict(old.dims)
            for r in changed_vectors:
                new.vectors[int(r["memory_id"])] = bytes(r["blob"])
                new.dims[int(r["memory_id"])] = int(r["dim"])
        else:
            new.vectors, new.dims = old.vectors, old.dims
            if content_rev == old.content_rev:
                new.matrices = old.matrices
        return new

    # -- pools ----------------------------------------------------------------

    @staticmethod
    def pool_key(
        project: str, root: str | None, shared_roots: Iterable[str], include_mined: bool
    ) -> tuple:
        roots = None if root is None else tuple(sorted({root, *shared_roots}))
        return (project, roots, bool(include_mined))

    def _pool(self, snap: _Snapshot, key: tuple) -> tuple[list[PoolRow], bm25.BM25]:
        cached = snap.pools.get(key)
        if cached is not None:
            return cached
        project, roots, include_mined = key
        rows = sorted(
            (
                r
                for r in snap.rows.values()
                if r.project == project
                and (roots is None or r.root in roots)
                and (include_mined or r.source_type != db.SOURCE_MINED)
            ),
            key=lambda r: r.id,
        )
        built = (rows, bm25.BM25([r.tokens for r in rows]))
        self.bm25_builds += 1
        if len(snap.pools) >= _CACHE_MAX:
            snap.pools.clear()
        snap.pools[key] = built
        return built

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
                i for i, r in enumerate(rows) if r.id in snap.vectors and snap.dims.get(r.id) == dim
            ]
            if positions:
                blob = b"".join(snap.vectors[rows[i].id] for i in positions)
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
    ) -> RankResult:
        """Rank the pool of section 8.1 for ``query``. No database access.

        Hybrid when ``query_vector`` is given and at least one pool row has a
        vector of the index model with the same dim; keyword-only otherwise.
        """
        snap = self._snap
        key = self.pool_key(project, root, shared_roots, include_mined)
        rows, index = self._pool(snap, key)
        if not rows or not query.strip():
            mode = MODE_HYBRID if query_vector is not None and rows else MODE_KEYWORD
            return RankResult(mode, [], len(rows))
        scores = index.scores(bm25.tokenize(query))
        cosines = self._cosines(snap, key, rows, query_vector) if query_vector else {}
        return rank_pool(rows, scores, cosines, top_k)


class Ranker:
    """The search entry point for the REST service: refresh, embed, rank.

    ``embedding`` is an ``EmbeddingService`` or None (keyword-only).
    """

    def __init__(self, embedding=None, *, query_timeout_s: float = 1.0) -> None:
        self.embedding = embedding
        self.query_timeout_s = query_timeout_s
        self.index = RankIndex(embedding.model_id if embedding is not None else None)

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
    ) -> RankResult:
        model_id = self.embedding.model_id if self.embedding is not None else None
        if np is None:
            model_id = None
        self.index.set_model(model_id)
        self.index.refresh(conn)
        vector = None
        if model_id is not None and query.strip():
            vector = self.embedding.embed_query(query, self.query_timeout_s)
        return self.index.rank(
            query,
            project=project,
            top_k=top_k,
            query_vector=vector,
            root=root,
            shared_roots=shared_roots,
            include_mined=include_mined,
        )
