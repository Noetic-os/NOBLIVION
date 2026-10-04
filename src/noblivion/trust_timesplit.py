# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion trust timesplit``: the time-split test for trust ranking.

Trust ranking stays off by default. It should turn on only when trust,
computed from older events, ranks the notes that were used later higher.
This command measures that on the user's own store (NOBLIVION-38):

1. CUT. Sort the sessions by their first event. The cut is the start of the
   first session after ``--train-share`` (default 0.7) of them, or the date
   ``--cut``.
2. FIT. Compute trust per note from the events before the cut only, with the
   store's own formula (``trust.trust_score``) and the hook's factor
   (``clamp(trust / prior, 0.8, 1.25)``, 1.0 below 5 trials).
3. TEST. For each session that starts at or after the cut, the notes that
   were used and not contradicted in it are the relevant ones. Two orders
   are compared, trust off and trust on:
   - per prompt, when the hook's trust log (``<cache>/trust-rank.jsonl``,
     written in ``shadow`` or ``on`` mode) holds the base order of the
     index: off is that order; on re-sorts it by ``2 / (60 + rank) *
     factor``, the hook's own fused-score shape;
   - per session, from the store alone: the store keeps no rank, so the
     candidates are the notes shown in the session, in a random order (the
     same 50 seeded shuffles for both arms); on sorts them by the factor.
     This asks only whether trust predicts later use. It is a necessary
     condition for a gain, not proof of one.
4. METRICS. Mean reciprocal rank of the first relevant note (MRR) and the
   share of units with a relevant note in the top ``--k`` (hit@k), for both
   arms, and how many units got better or worse.

The verdict is ``gain`` only when there are at least MIN_UNITS test units,
MRR is higher with trust on and more units got better than worse. The
command reads the database and the log only. It changes no setting.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from noblivion import db, trust

# The hook's factor (hooks/trust_rank.py). A test keeps the two equal.
F_MIN = 0.8
F_MAX = 1.25
N_MIN_TRIALS = 5
RRF_K = 60  # the fused score at rank r is about 2 / (RRF_K + r)

DEFAULT_K = 5
DEFAULT_TRAIN_SHARE = 0.7
MIN_UNITS = 30
SHUFFLES = 50
RANK_LOG = "trust-rank.jsonl"

USE_KINDS = frozenset({"use", "load_bearing"})


@dataclass(frozen=True)
class Event:
    session: str
    memory_id: int
    kind: str  # the stored kind: recall, use, load_bearing or contradiction
    ts: datetime


@dataclass(frozen=True)
class Fit:
    trust: float
    trials: int
    prior: float

    @property
    def factor(self) -> float:
        return factor(self.trust, self.trials, self.prior)


@dataclass
class Arm:
    rr: list[float] = field(default_factory=list)
    hit: list[float] = field(default_factory=list)

    def mrr(self) -> float:
        return sum(self.rr) / len(self.rr) if self.rr else 0.0

    def hit_rate(self) -> float:
        return sum(self.hit) / len(self.hit) if self.hit else 0.0


def factor(trust_value: float, trials: int, prior: float) -> float:
    """``clamp(trust / prior, F_MIN, F_MAX)``; 1.0 below N_MIN_TRIALS."""
    if trials < N_MIN_TRIALS or prior <= 0.0:
        return 1.0
    return max(F_MIN, min(F_MAX, trust_value / prior))


# -- data ----------------------------------------------------------------------


def _parse(text: str) -> datetime | None:
    try:
        return db.parse_ts(text)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_events(
    conn: sqlite3.Connection, project: str = trust.PERSONA, *, prior_mined: float = 0.3
) -> tuple[list[Event], dict[int, float]]:
    """The events of the live notes of ``project``, oldest first, and the
    prior of each note."""
    events: list[Event] = []
    priors: dict[int, float] = {}
    with db.read_tx(conn):
        rows = conn.execute(
            "SELECT e.session_id, e.memory_id, e.kind, e.ts, m.source_type "
            "FROM feedback_events e JOIN memories m ON m.id = e.memory_id "
            "WHERE m.project = ? AND m.archived_at IS NULL AND m.deleted_at IS NULL "
            "ORDER BY e.ts, e.id",
            (project,),
        ).fetchall()
    for session, memory_id, kind, ts, source_type in rows:
        moment = _parse(str(ts))
        if moment is None:
            continue
        events.append(Event(str(session), int(memory_id), str(kind), moment))
        priors[int(memory_id)] = trust.prior_of(str(source_type), prior_mined)
    return events, priors


def session_starts(events: Iterable[Event]) -> dict[str, datetime]:
    starts: dict[str, datetime] = {}
    for ev in events:
        if ev.session not in starts or ev.ts < starts[ev.session]:
            starts[ev.session] = ev.ts
    return starts


def pick_cut(events: Sequence[Event], train_share: float) -> datetime | None:
    """The start of the first session after ``train_share`` of the sessions,
    or None with fewer than two sessions."""
    starts = sorted(session_starts(events).values())
    if len(starts) < 2:
        return None
    index = min(len(starts) - 1, max(1, int(len(starts) * train_share)))
    return starts[index]


def fit(events: Iterable[Event], cut: datetime, priors: Mapping[int, float]) -> dict[int, Fit]:
    """Trust per note from the events before ``cut``, by the store's rule:
    a trial is a session with a use or a contradiction."""
    used: dict[int, set[str]] = {}
    contra: dict[int, set[str]] = {}
    for ev in events:
        if ev.ts >= cut:
            continue
        if ev.kind in USE_KINDS:
            used.setdefault(ev.memory_id, set()).add(ev.session)
        elif ev.kind == "contradiction":
            contra.setdefault(ev.memory_id, set()).add(ev.session)
    out: dict[int, Fit] = {}
    for mid, prior in priors.items():
        u = used.get(mid, set())
        c = contra.get(mid, set())
        trials = len(u | c)
        out[mid] = Fit(trust.trust_score(prior, trials, float(len(u)), len(c)), trials, prior)
    return out


@dataclass
class SessionFacts:
    shown: set[int] = field(default_factory=set)
    used: set[int] = field(default_factory=set)
    contradicted: set[int] = field(default_factory=set)

    @property
    def relevant(self) -> set[int]:
        return self.used - self.contradicted


def after_cut(events: Iterable[Event], cut: datetime) -> dict[str, SessionFacts]:
    """The sessions that start at or after ``cut``, with what was shown and used."""
    events = list(events)
    starts = session_starts(events)
    out: dict[str, SessionFacts] = {}
    for ev in events:
        if starts[ev.session] < cut:
            continue
        s = out.setdefault(ev.session, SessionFacts())
        if ev.kind == "recall":
            s.shown.add(ev.memory_id)
        elif ev.kind in USE_KINDS:
            s.used.add(ev.memory_id)
        elif ev.kind == "contradiction":
            s.contradicted.add(ev.memory_id)
    return out


def read_rank_log(paths: Iterable[Path]) -> list[dict]:
    """The lines of the hook's trust log: ``{"ts", "session", "base"}``.
    A line that is not a JSON object with a session and a base list is skipped."""
    out: list[dict] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    base = rec.get("base")
                    moment = _parse(str(rec.get("ts") or ""))
                    if not isinstance(rec.get("session"), str) or not isinstance(base, list):
                        continue
                    ids = [int(x) for x in base if isinstance(x, int) and not isinstance(x, bool)]
                    if moment is not None and ids:
                        out.append({"ts": moment, "session": rec["session"], "base": ids})
        except OSError:
            continue
    return out


# -- ranking -------------------------------------------------------------------


def _first_hit(order: Sequence[int], relevant: set[int], k: int) -> tuple[float, float]:
    for rank, mid in enumerate(order, 1):
        if mid in relevant:
            return 1.0 / rank, 1.0 if rank <= k else 0.0
    return 0.0, 0.0


def trust_order(base: Sequence[int], fits: Mapping[int, Fit]) -> list[int]:
    """``base`` re-sorted by ``2 / (RRF_K + rank) * factor``, highest first;
    a tie goes to the lower id, as in the hook (``trust_rank.reorder``)."""
    keyed = []
    for i, mid in enumerate(base):
        f = fits[mid].factor if mid in fits else 1.0
        keyed.append((-round(2.0 / (RRF_K + i + 1) * f, 6), mid, i))
    return [base[i] for _, _, i in sorted(keyed)]


@dataclass
class UnitResult:
    off: Arm = field(default_factory=Arm)
    on: Arm = field(default_factory=Arm)
    better: int = 0
    worse: int = 0
    changed: int = 0  # units where trust changed the order

    def add(self, off: tuple[float, float], on: tuple[float, float], moved: bool) -> None:
        self.off.rr.append(off[0])
        self.off.hit.append(off[1])
        self.on.rr.append(on[0])
        self.on.hit.append(on[1])
        if on[0] > off[0] + 1e-12:
            self.better += 1
        elif on[0] < off[0] - 1e-12:
            self.worse += 1
        if moved:
            self.changed += 1

    def summary(self, k: int) -> dict:
        return {
            "units": len(self.off.rr),
            "changed": self.changed,
            "mrr_off": round(self.off.mrr(), 4),
            "mrr_on": round(self.on.mrr(), 4),
            f"hit_at_{k}_off": round(self.off.hit_rate(), 4),
            f"hit_at_{k}_on": round(self.on.hit_rate(), 4),
            "better": self.better,
            "worse": self.worse,
        }


def by_session(
    sessions: Mapping[str, SessionFacts], fits: Mapping[int, Fit], k: int, shuffles: int = SHUFFLES
) -> UnitResult:
    """Store-only test: in each test session, rank the shown notes in a
    random order (off) and by the factor over the same order (on)."""
    result = UnitResult()
    for sid in sorted(sessions):
        s = sessions[sid]
        relevant = s.relevant & s.shown
        if not relevant or len(s.shown) < 2:
            continue
        off_rr = off_hit = on_rr = on_hit = 0.0
        moved = False
        for n in range(shuffles):
            base = sorted(s.shown)
            random.Random(f"{sid}:{n}").shuffle(base)
            on = sorted(base, key=lambda m: -(fits[m].factor if m in fits else 1.0))
            moved = moved or on != base
            a, b = _first_hit(base, relevant, k)
            c, d = _first_hit(on, relevant, k)
            off_rr, off_hit, on_rr, on_hit = off_rr + a, off_hit + b, on_rr + c, on_hit + d
        result.add(
            (off_rr / shuffles, off_hit / shuffles), (on_rr / shuffles, on_hit / shuffles), moved
        )
    return result


def by_prompt(
    log: Iterable[Mapping], sessions: Mapping[str, SessionFacts], fits: Mapping[int, Fit], k: int
) -> UnitResult:
    """Order-aware test from the hook's trust log: each logged index of a
    test session is one unit."""
    result = UnitResult()
    for rec in log:
        s = sessions.get(rec["session"])
        if s is None:
            continue
        base = list(dict.fromkeys(rec["base"]))
        relevant = s.relevant & set(base)
        if not relevant:
            continue
        on = trust_order(base, fits)
        result.add(_first_hit(base, relevant, k), _first_hit(on, relevant, k), on != base)
    return result


def verdict(unit: Mapping) -> str:
    if unit["units"] < MIN_UNITS:
        return "too little data"
    if unit["mrr_on"] > unit["mrr_off"] and unit["better"] > unit["worse"]:
        return "gain"
    return "no gain"


def run(
    events: Sequence[Event],
    priors: Mapping[int, float],
    *,
    cut: datetime | None = None,
    train_share: float = DEFAULT_TRAIN_SHARE,
    k: int = DEFAULT_K,
    log: Sequence[Mapping] = (),
    shuffles: int = SHUFFLES,
) -> dict:
    """The whole test. Returns the result as a dict (the ``--json`` shape)."""
    cut = cut or pick_cut(events, train_share)
    out: dict = {
        "cut": cut.strftime("%Y-%m-%dT%H:%M:%SZ") if cut else None,
        "k": k,
        "min_units": MIN_UNITS,
        "events": len(events),
    }
    if cut is None:
        out.update(train_events=0, test_sessions=0, session=None, prompt=None)
        out["verdict"] = "too little data"
        return out
    fits = fit(events, cut, priors)
    sessions = after_cut(events, cut)
    session_unit = by_session(sessions, fits, k, shuffles).summary(k)
    test_log = [r for r in log if r["ts"] >= cut]
    prompt_unit = by_prompt(test_log, sessions, fits, k).summary(k) if test_log else None
    out.update(
        train_events=sum(1 for e in events if e.ts < cut),
        test_sessions=len(sessions),
        trusted_notes=sum(1 for f in fits.values() if f.factor != 1.0),
        session=session_unit,
        prompt=prompt_unit,
    )
    main = prompt_unit if prompt_unit and prompt_unit["units"] >= MIN_UNITS else session_unit
    out["verdict_from"] = "prompt" if main is prompt_unit else "session"
    out["verdict"] = verdict(main)
    return out


# -- CLI -----------------------------------------------------------------------


def render(result: Mapping) -> str:
    k = result["k"]
    lines = [
        f"Trust time-split test: cut {result['cut'] or '-'}, "
        f"{result.get('train_events', 0)} events before it, "
        f"{result.get('test_sessions', 0)} test sessions after it.",
    ]
    for name, title in (("prompt", "Per prompt (hook trust log)"), ("session", "Per session")):
        unit = result.get(name)
        if not unit:
            continue
        lines.append(
            f"{title}: {unit['units']} units, {unit['changed']} reordered. "
            f"MRR {unit['mrr_off']:.3f} off, {unit['mrr_on']:.3f} on. "
            f"hit@{k} {unit[f'hit_at_{k}_off']:.3f} off, {unit[f'hit_at_{k}_on']:.3f} on. "
            f"{unit['better']} better, {unit['worse']} worse."
        )
    lines.append(f"Verdict: {result['verdict']} (needs at least {result['min_units']} units).")
    lines.append(
        "This command changes no setting. Trust ranking stays off until you turn it on "
        "(docs/trust.md)."
    )
    return "\n".join(lines)


def _cut_arg(text: str) -> datetime:
    moment = _parse(text if "T" in text else f"{text}T00:00:00+00:00")
    if moment is None:
        raise ValueError(f"not a date: {text!r}")
    return moment


def main(argv: Sequence[str] | None = None) -> int:
    """``noblivion trust timesplit [--cut DATE] [--train-share F] [--k N]
    [--rank-log PATH] [--json]``. Exit codes as ``noblivion trust``."""
    import argparse
    import sys

    from noblivion import config

    parser = argparse.ArgumentParser(prog="noblivion trust timesplit")
    parser.add_argument("--cut", type=_cut_arg, help="cut date, YYYY-MM-DD (UTC)")
    parser.add_argument("--train-share", type=float, default=DEFAULT_TRAIN_SHARE)
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="hit@k (default 5)")
    parser.add_argument(
        "--rank-log",
        action="append",
        default=None,
        help="the hook's trust log (default <cache>/trust-rank.jsonl and its .1 file)",
    )
    parser.add_argument("--no-rank-log", action="store_true", help="per session only")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    if not 0.0 < args.train_share < 1.0 or args.k < 1:
        parser.error("--train-share must be between 0 and 1, --k at least 1")
    settings = config.load_settings()
    store_settings = config.load_store_settings()
    try:
        conn = db.open_db(settings.db_path, allow_migrate=False)
    except db.SchemaError as exc:
        print(f"noblivion trust: {exc}")
        return 3
    except db.DatabaseMissing as exc:
        print(f"noblivion trust: {exc}", file=sys.stderr)
        return 6
    try:
        events, priors = load_events(
            conn, settings.namespace, prior_mined=store_settings.prior_mined
        )
    except sqlite3.OperationalError as exc:
        print(f"noblivion trust: database error ({exc}); try again later", file=sys.stderr)
        return 5
    finally:
        conn.close()
    log: list[dict] = []
    if not args.no_rank_log:
        if args.rank_log:
            paths = [Path(p).expanduser() for p in args.rank_log]
        else:
            cache = os.environ.get("NOBLIVION_RECALL_CACHE_DIR") or str(settings.data_dir / "cache")
            base = Path(cache).expanduser() / RANK_LOG
            paths = [Path(f"{base}.1"), base]
        log = read_rank_log(paths)
    result = run(events, priors, cut=args.cut, train_share=args.train_share, k=args.k, log=log)
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0
