#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trust factor on the recall hook's index ranking.
Part G of the adaptive memory trust work.

WHY IN THE HOOK. The live hook re-ranks the daemon's 200 candidates itself
(P1h, ``rerank_index``: cosine fused with a local BM25 by reciprocal rank
fusion) and throws the daemon's own order away. A factor applied in the daemon
would change nothing the model sees (REVIEW-4453 B1). So the daemon RETURNS
trust per index row (contract C3: ``trust``, ``trials``, ``trust_prior``) and
this module multiplies the hook's own fused score, after ``rerank_index`` and
before the floor and the cut to k.

The factor: ``f = clamp(trust / trust_prior, F_MIN, F_MAX)`` with F_MIN 0.8 and
F_MAX 1.25; ``f = 1.0`` below N_MIN_TRIALS trials, and for a row without the
fields. The fused score of two lists at rank r is about 2/(60+r), so 1.25 lifts
a row by about 16 places at the top and 0.8 drops one by about 15: trust
re-orders near neighbours, it does not replace relevance (REVIEW-4453 M8).
When every factor is 1.0 the order is the input order, exactly.

Switches:

- ``NOBLIVION_RECALL_INDEX_TRUST``: ``1`` (or ``on``) applies the factor;
  ``shadow`` computes it and logs the would-be order but serves the order
  unchanged; anything else is off.
- ``NOBLIVION_RECALL_TRUST_FILE``: a pinned snapshot (the replay tool writes
  one) that REPLACES the daemon fields. A row is looked up by its memory file
  stem first (the row title joined to the local memory folder), then by its
  id, so one snapshot also serves a bench copy whose ids differ. A snapshot
  that cannot be read turns the step off for the call (``:trust_off:bad_file``)
  instead of reading the daemon fields: a run that pinned a snapshot must not
  silently rank by live trust (REVIEW-4453 M5).

- ``NOBLIVION_RECALL_TRUST_MODE`` (CRIT-trust-tools.md M1: with "shown is not a
  trial" the factor is 1.0 or exactly 1.25, and 54 of 56 boosted memories got
  there only through guard ``rows``, which fire on the command, not on the
  prompt). Unset = the factor above. ``a``: guard ``rows`` are not a use; it
  acts where the events are made (trust_events), the factor here is
  unchanged. ``b``: the factor comes from the use RATE, see ``rate_factor``;
  it needs ``shown_sessions`` and ``shown_used_sessions`` (a replay snapshot
  has them; the daemon fields of C3 do not, so with no snapshot every factor
  is 1.0). ``c``: the factor applies only to a row whose relevance score (the
  daemon cosine) is at least ``NOBLIVION_RECALL_TRUST_MIN_SCORE`` (default
  TRUST_MIN_SCORE).

The log note is ``:trustNofK`` (``:trust_shadowNofK`` in shadow mode): N rows of
the K candidates have a factor other than 1. The debug line, one JSON line per
call in ``<cache>/trust-rank.jsonl``, prints trust, trials and the factor of
those rows with their place before and after, and the first ids of both orders.
It holds ids and numbers only, never memory text.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

TRUST_ENV = "NOBLIVION_RECALL_INDEX_TRUST"
TRUST_FILE_ENV = "NOBLIVION_RECALL_TRUST_FILE"
F_MIN = 0.8
F_MAX = 1.25
N_MIN_TRIALS = 5  # memory_feedback.N_MIN_TRIALS
DEFAULT_PRIOR = 0.5  # memory_feedback.calibrated_trust_0 for a memory row
DEBUG_NAME = "trust-rank.jsonl"
DEBUG_MAX_BYTES = 4_000_000  # then the file moves to .1, once
DEBUG_TOP = 40  # ids of each order on the debug line
DEBUG_ROWS = 60  # rows with f != 1 on the debug line
SNAPSHOT_FORMAT = "noblivion-trust-snapshot/1"

TRUST_MODE_ENV = "NOBLIVION_RECALL_TRUST_MODE"  # as trust_events
TRUST_MODES = ("a", "b", "c")
MODE_RATE = "b"
MODE_MIN_SCORE = "c"
RATE_PRIOR_M = 10  # memory_feedback.PRIOR_STRENGTH_M: pseudo-sessions at the pool rate
MIN_SCORE_ENV = "NOBLIVION_RECALL_TRUST_MIN_SCORE"
# Fixed 2026-10-02 BEFORE any mode-c run, from candidate scores only (no trap
# rank read): the median daemon cosine of the 5234 candidates at or above the
# 0.52 index floor, pooled over the 200 candidates of each of the 30 final2
# bank prompts, is 0.578 -> 0.58. Trust then re-orders the more relevant half
# of the rows that can be shown.
TRUST_MIN_SCORE = 0.58

MODE_OFF = ""
MODE_ON = "on"
MODE_SHADOW = "shadow"

_SNAPSHOTS: Dict[Tuple[str, float, int], Snapshot] = {}


def mode(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    raw = (env.get(TRUST_ENV) or "").strip().lower()
    if raw in ("1", "on", "true", "yes"):
        return MODE_ON
    if raw == MODE_SHADOW:
        return MODE_SHADOW
    return MODE_OFF


def trust_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """``a``, ``b`` or ``c`` from NOBLIVION_RECALL_TRUST_MODE; ``""`` otherwise."""
    env = os.environ if environ is None else environ
    raw = (env.get(TRUST_MODE_ENV) or "").strip().lower()
    return raw if raw in TRUST_MODES else ""


def min_score(environ: Mapping[str, str]) -> float:
    """The mode ``c`` threshold; TRUST_MIN_SCORE when unset or not a number."""
    try:
        value = float((environ.get(MIN_SCORE_ENV) or "").strip())
    except ValueError:
        return TRUST_MIN_SCORE
    return value if value == value else TRUST_MIN_SCORE


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def rate_factor(
    shown_used: Any,
    shown: Any,
    pool_rate: Any,
    f_min: Optional[float] = None,
    f_max: Optional[float] = None,
    n_min: Optional[int] = None,
) -> float:
    """Mode ``b``: the factor from the use rate, not the raw use count.

    rate = sessions shown AND used / sessions shown, smoothed toward the pool
    rate with RATE_PRIOR_M pseudo-sessions, divided by the pool rate, clamped
    to [f_min, f_max]. 1.0 below ``n_min`` shown sessions and when a count is
    missing, not a count, or larger than ``shown``, or the pool rate is not
    above 0. Graded: a memory used in most of the sessions that showed it rises,
    one shown often and never used falls, whatever its raw count."""
    f_min = F_MIN if f_min is None else f_min
    f_max = F_MAX if f_max is None else f_max
    n_min = N_MIN_TRIALS if n_min is None else n_min
    k, n = _count(shown_used), _count(shown)
    if k is None or n is None or k > n or n < n_min:
        return 1.0
    if isinstance(pool_rate, bool) or not isinstance(pool_rate, (int, float)):
        return 1.0
    pool = float(pool_rate)
    if not pool > 0.0:
        return 1.0
    smoothed = (k + RATE_PRIOR_M * pool) / (n + RATE_PRIOR_M)
    return max(f_min, min(f_max, smoothed / pool))


def factor(
    trust: Any,
    trials: Any,
    prior: Any,
    f_min: Optional[float] = None,
    f_max: Optional[float] = None,
    n_min: Optional[int] = None,
) -> float:
    """``clamp(trust / prior, f_min, f_max)``; 1.0 below ``n_min`` trials and
    whenever a field is missing or not a number. The bounds default to the
    module constants, read at call time."""
    f_min = F_MIN if f_min is None else f_min
    f_max = F_MAX if f_max is None else f_max
    n_min = N_MIN_TRIALS if n_min is None else n_min
    try:
        t = float(trust)
        n = int(trials)
        p = float(prior)
    except (TypeError, ValueError):
        return 1.0
    if isinstance(trials, bool) or n < n_min or p <= 0.0 or t != t or p != p:
        return 1.0
    return max(f_min, min(f_max, t / p))


class Snapshot:
    """A pinned trust snapshot: ``rows`` of ``{stem, mid, trust, trials}`` and
    one ``trust_prior``. Lookup by stem first, then by id."""

    def __init__(self, doc: Mapping[str, Any]):
        if not isinstance(doc, Mapping) or not isinstance(doc.get("rows"), list):
            raise ValueError("not a trust snapshot")
        prior = doc.get("trust_prior", DEFAULT_PRIOR)
        self.prior = (
            float(prior)
            if isinstance(prior, (int, float)) and not isinstance(prior, bool)
            else DEFAULT_PRIOR
        )
        self.kind = str(doc.get("kind") or "")
        self.by_stem: Dict[str, Tuple[Any, Any]] = {}
        self.by_mid: Dict[int, Tuple[Any, Any]] = {}
        self.rate_by_stem: Dict[str, Tuple[Any, Any]] = {}
        self.rate_by_mid: Dict[int, Tuple[Any, Any]] = {}
        used_sum = shown_sum = 0
        for row in doc["rows"]:
            if not isinstance(row, Mapping):
                continue
            pair = (row.get("trust"), row.get("trials"))
            rate = (row.get("shown_used_sessions"), row.get("shown_sessions"))
            k, n = _count(rate[0]), _count(rate[1])
            if k is not None and n is not None and k <= n:
                used_sum += k
                shown_sum += n
            stem = row.get("stem")
            if isinstance(stem, str) and stem:
                self.by_stem[stem] = pair
                self.rate_by_stem[stem] = rate
            mid = row.get("mid")
            if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0:
                self.by_mid[mid] = pair
                self.rate_by_mid[mid] = rate
        # Mode b: the pool use rate, over every row that has both counts.
        self.pool_rate: Optional[float] = used_sum / shown_sum if shown_sum else None

    def lookup(self, stem: Optional[str], mid: Any) -> Tuple[Any, Any, float]:
        pair = self.by_stem.get(stem) if stem else None
        if pair is None and isinstance(mid, int):
            pair = self.by_mid.get(mid)
        if pair is None:
            return None, None, self.prior
        return pair[0], pair[1], self.prior

    def rate(self, stem: Optional[str], mid: Any) -> Tuple[Any, Any]:
        """``(shown_used_sessions, shown_sessions)`` by stem first, then by id."""
        pair = self.rate_by_stem.get(stem) if stem else None
        if pair is None and isinstance(mid, int):
            pair = self.rate_by_mid.get(mid)
        return pair if pair is not None else (None, None)


def load_snapshot(path: str) -> Snapshot:
    """Read and cache a snapshot by (path, mtime, size). Raises on any problem."""
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    snap = _SNAPSHOTS.get(key)
    if snap is None:
        with open(path, encoding="utf-8") as fh:
            snap = Snapshot(json.load(fh))
        _SNAPSHOTS.clear()
        _SNAPSHOTS[key] = snap
    return snap


def _stem_of(line: Any, by_name: Mapping[str, Any]) -> Optional[str]:
    """The memory file stem a row names: its title joined to the local memory
    folder (the join ``rerank_index`` uses), or None."""
    md = by_name.get(getattr(line, "title", "") or "")
    rel = getattr(md, "rel_path", "") if md is not None else ""
    if not rel:
        return None
    base = os.path.basename(rel)
    return base[:-3] if base.endswith(".md") else base


def factors(
    lines: Sequence[Any],
    by_name: Mapping[str, Any],
    snapshot: Optional[Snapshot] = None,
    f_min: Optional[float] = None,
    f_max: Optional[float] = None,
    n_min: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> List[Tuple[float, Any, Any]]:
    """``(f, trust, trials)`` per row: from the snapshot when one is given, else
    from the row's own daemon fields (C3). ``environ`` selects the trust mode
    (``b``: the use-rate factor; ``c``: only rows at or above the threshold)."""
    env: Mapping[str, str] = {} if environ is None else environ
    tmode = trust_mode(env)
    threshold = min_score(env) if tmode == MODE_MIN_SCORE else 0.0
    out: List[Tuple[float, Any, Any]] = []
    for ln in lines:
        stem = _stem_of(ln, by_name) if snapshot is not None else None
        if snapshot is not None:
            trust, trials, prior = snapshot.lookup(stem, getattr(ln, "mid", None))
        else:
            trust = getattr(ln, "trust", None)
            trials = getattr(ln, "trials", None)
            prior = getattr(ln, "trust_prior", None)
        if tmode == MODE_RATE:
            if snapshot is not None:
                k, n = snapshot.rate(stem, getattr(ln, "mid", None))
                pool: Any = snapshot.pool_rate
            else:  # C3 carries no shown counts: inert
                k = n = pool = None
            f = rate_factor(k, n, pool, f_min, f_max, n_min)
        else:
            f = factor(trust, trials, prior, f_min, f_max, n_min)
        if tmode == MODE_MIN_SCORE:
            score = getattr(ln, "score", None)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or score < threshold:
                f = 1.0
        out.append((f, trust, trials))
    return out


def _zip_same(a: Sequence[Any], b: Sequence[Any]) -> List[Tuple[Any, Any]]:
    """``zip(a, b, strict=True)`` for python 3.9: the hooks run on 3.9, where
    ``zip`` takes no ``strict`` argument."""
    if len(a) != len(b):
        raise ValueError("zip() arguments differ in length")
    return list(zip(a, b))


def reorder(lines: Sequence[Any], fs: Sequence[float]) -> List[Any]:
    """The rows by ``fused * f``, highest first; a tie goes to the lower id, the
    tie rule of the hook's own fusion. Rounded to six decimals like the fusion,
    so every factor 1.0 gives the input order back exactly."""
    keyed = [
        (-round(float(ln.fused) * f, 6), int(ln.mid), i)
        for i, (ln, f) in enumerate(_zip_same(lines, fs))
    ]
    return [lines[i] for _, _, i in sorted(keyed)]


def _debug(cache: str, record: Mapping[str, Any]) -> None:
    """One JSON line; the file moves to ``.1`` past DEBUG_MAX_BYTES. Never raises."""
    path = os.path.join(cache, DEBUG_NAME)
    try:
        os.makedirs(cache, exist_ok=True)
        with contextlib.suppress(OSError):
            if os.path.getsize(path) > DEBUG_MAX_BYTES:
                os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        return


def apply_index_trust(
    lines: Sequence[Any],
    environ: Mapping[str, str],
    by_name: Mapping[str, Any],
    cache: str = "",
    session_id: Any = None,
    event: str = "",
) -> Tuple[List[Any], str]:
    """The hook's step: ``(rows, note)``. Rows come back re-ordered in mode
    ``on``, unchanged in mode ``shadow`` and when the step cannot run."""
    md = mode(environ)
    rows = list(lines)
    if md == MODE_OFF:
        return rows, ""
    if not rows:
        return rows, ""
    if any(getattr(ln, "fused", None) is None for ln in rows):
        # No local fused score: the re-rank did not run (P1h off, or the
        # memory folder failed). The daemon's order is not this hook's to scale.
        return rows, ":trust_off:no_rerank"
    snapshot: Optional[Snapshot] = None
    source = "daemon"
    path = (environ.get(TRUST_FILE_ENV) or "").strip()
    if path:
        try:
            snapshot = load_snapshot(os.path.expanduser(path))
        except (OSError, ValueError, TypeError):
            return rows, ":trust_off:bad_file"
        source = "file"
    fs = factors(rows, by_name, snapshot, environ=environ)
    trusted = reorder(rows, [f for f, _, _ in fs])
    changed = [(ln, f, trust, trials) for ln, (f, trust, trials) in _zip_same(rows, fs) if f != 1.0]
    tag = "trust" if md == MODE_ON else "trust_shadow"
    note = f":{tag}{len(changed)}of{len(rows)}"
    if cache:
        pos_after = {id(ln): i + 1 for i, ln in enumerate(trusted)}
        pos_before = {id(ln): i + 1 for i, ln in enumerate(rows)}
        _debug(
            cache,
            {
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "session": session_id if isinstance(session_id, str) else None,
                "event": event,
                "mode": md,
                "source": source,
                "k": len(rows),
                "changed": len(changed),
                # [id, trust, trials, factor, place before, place after]
                "rows": [
                    [int(ln.mid), trust, trials, round(f, 4), pos_before[id(ln)], pos_after[id(ln)]]
                    for ln, f, trust, trials in changed[:DEBUG_ROWS]
                ],
                "base": [int(ln.mid) for ln in rows[:DEBUG_TOP]],
                "trust": [int(ln.mid) for ln in trusted[:DEBUG_TOP]],
            },
        )
    if md == MODE_SHADOW:
        return rows, note
    return trusted, note
