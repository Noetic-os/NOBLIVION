# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nearest-target selection for the semantic stage (extraction contract 3).

Contracts 1 and 2 compare each new add candidate with every managed note of its
scope and with every earlier add candidate of the job. The number of calls of a
job then grows with the note count (NOBLIVION-243). Contract 3 compares each
candidate only with its nearest notes and its nearest earlier candidates. It
uses the two legs of the recall path: BM25 keyword match and cosine similarity
of the stored embeddings. Without vectors the order is the keyword order alone.

Each selected (candidate, target) pair still gets one explicit relation. The
worker computes the selection once per semantic stage and stores it, so a resume
builds the same tasks whatever the embedding service does later.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from . import bm25

# The managed notes and the earlier add candidates that one candidate is
# compared with. Part of the pinned contract 3 text: a change selects other
# targets for a started job, so it needs a new contract version.
NEAREST_NOTES = 16
NEAREST_CANDIDATES = 16
# The smaller selection that the over-bound fallback of contracts 1 and 2 tries
# when the one above still exceeds the task bound of the job.
FALLBACK_SELECTIONS = ((NEAREST_NOTES, NEAREST_CANDIDATES), (8, 8))
# One seat in this many goes to the best keyword match (see _nearest).
KEYWORD_SHARE = 4
# How the order was made: cosine plus keyword, or keyword alone.
MODE_HYBRID = "hybrid"
MODE_KEYWORD = "keyword"


def candidate_key(index: int) -> str:
    """The vector key and the target id of an earlier add candidate."""
    return f"candidate:{index}"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    value = math.fsum(x * y for x, y in zip(a, b))  # noqa: B905 - 3.9 lane
    return value if math.isfinite(value) else 0.0


def _nearest(
    query: str,
    query_vector: Sequence[float] | None,
    pool: list[dict],
    vectors: Mapping[str, Sequence[float]],
    limit: int,
) -> set[str]:
    """The ids of the ``limit`` nearest pool targets.

    ``limit // KEYWORD_SHARE`` seats go to the best BM25 keyword matches, so a
    target that shares rare words with the candidate is always compared. The
    other seats go to the highest cosine similarity. Seats left over, for
    example when there are no vectors, go to the next keyword matches. A target
    with no vector can still be selected by its keyword match. A pool no larger
    than ``limit`` is selected whole, so a small store keeps the full
    comparison of contracts 1 and 2.
    """
    if len(pool) <= limit:
        return {target["target_id"] for target in pool}
    index = bm25.BM25([bm25.tokenize(target["statement"]) for target in pool])
    tokens = bm25.tokenize(query)
    scores = index.scores(tokens)
    matched = index.matches(tokens)
    keyword = sorted((i for i in range(len(pool)) if matched[i]), key=lambda i: (-scores[i], i))
    cosine = []
    if query_vector is not None:
        near = {
            i: _cosine(query_vector, vectors[target["target_id"]])
            for i, target in enumerate(pool)
            if target["target_id"] in vectors
        }
        cosine = sorted(near, key=lambda i: (-near[i], i))
    chosen = dict.fromkeys(keyword[: limit // KEYWORD_SHARE])
    for i in [*cosine, *keyword]:
        if len(chosen) >= limit:
            break
        chosen.setdefault(i)
    return {pool[i]["target_id"] for i in chosen}


def nearest_targets(
    decisions: list[dict],
    notes: list[dict],
    vectors: Mapping[str, Sequence[float]] | None,
    *,
    notes_limit: int = NEAREST_NOTES,
    candidates_limit: int = NEAREST_CANDIDATES,
) -> dict[str, list[str]]:
    """Map each add candidate index (as text) to the target ids it is compared with.

    ``notes`` and the scope rule are those of ``semantic_targets``. ``vectors``
    maps a note id, and ``candidate:<index>`` for a candidate, to its normalized
    embedding; None means keyword order only. The ids keep the target order of
    ``semantic_targets``, so the tasks are a subset of the contract 1 tasks.
    """
    vectors = vectors or {}
    selection: dict[str, list[str]] = {}
    for index, decision in enumerate(decisions):
        if decision["action"] != "add":
            continue
        own = [
            {"target_id": str(note["note_id"]), "statement": note["statement"]}
            for note in notes
            if note.get("scope") == decision["scope"] or note.get("read_only")
        ]
        prior = [
            {"target_id": candidate_key(i), "statement": d["statement"]}
            for i, d in enumerate(decisions[:index])
            if d["action"] == "add" and d["scope"] == decision["scope"]
        ]
        query_vector = vectors.get(candidate_key(index))
        chosen = _nearest(decision["statement"], query_vector, own, vectors, notes_limit)
        chosen |= _nearest(decision["statement"], query_vector, prior, vectors, candidates_limit)
        selection[str(index)] = [
            target["target_id"] for target in [*own, *prior] if target["target_id"] in chosen
        ]
    return selection
