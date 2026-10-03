# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trust in the store (design doc 0001, sections 4.5, 4.6 and 9).

- ``parse_batch`` checks a ``POST /api/memory/feedback/batch`` body.
- ``store_batch`` writes one batch in ONE ``BEGIN IMMEDIATE`` transaction:
  resolve, insert the new events, recompute the rollup rows they touch,
  commit. Nothing is half written.
- ``recompute`` reads the event counts of some memories and writes their
  rollup rows. The caller holds the write transaction, so no other write can
  commit between the read and the write (defect 1 of section 9.4).
- ``repair`` finds rollup rows behind their events by the event id
  watermark ``folded_event_id``, never by a clock, and recomputes them in
  batches of 200, each batch one ``BEGIN IMMEDIATE`` transaction.
- ``maintenance_pass`` is the pass the store runs at start and every 24
  hours (section 9.3).
- ``report`` builds the trust report of section 4.6 in one read transaction.

Events and rollup rows reference ``memories.id`` with ``ON DELETE CASCADE``
(defect 2 of section 9.4). A soft-deleted or archived row keeps its trust
history; a rename keeps the row id, so the history follows the file.
"""

from __future__ import annotations

import bisect
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noblivion import db

log = logging.getLogger("noblivion.store")

PERSONA = "claude_code"
KINDS = ("recall", "use")  # the kinds the REST route accepts (section 9.1)
CITATION_CAPABLE = {"recall": 0, "use": 1}
MAX_EVENTS = 500
MAX_PATH_CHARS = 512
MAX_MV_ID = 2_147_483_647
MAX_ROOT_CHARS = 512
TS_MAX_SKEW = timedelta(days=1)
TS_MAX_AGE = timedelta(days=80)
PRIOR_STRENGTH = 10.0
TRUST_PRIOR_MD = 0.5
MAINTENANCE_INTERVAL_S = 24 * 3600.0

RETIRE_MIN_SHOWN_SESSIONS = 20
RETIRE_MIN_FILE_AGE_DAYS = 30
PROMOTE_MIN_TRUST = 0.7
PROMOTE_MIN_TRIALS = 5
PROMOTE_MIN_USE_SHARE = 0.20
PROMOTE_MIN_SPAN_DAYS = 14
DEMOTE_NO_USE_DAYS = 30
REPORT_LIST_CAP = 500
MEMORY_INDEX_PATH = "MEMORY.md"
INDEX_CATEGORIES = frozenset({"index", "topic"})

# The one session-id rule (NOBLIVION-21). hooks/trust_events.py holds the same
# string because the hooks do not import the package; a test checks they match.
SESSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"  # used with fullmatch
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
_MD_LINK_RE = re.compile(r"\]\(\s*<?([^()<>\s]+?\.md)>?\s*\)")

# One session counts once per memory and kind. A recall is exposure only; it
# is not a trial (section 9.3).
_COUNTS_SQL = (
    "count(DISTINCT CASE WHEN e.citation_capable = 1 OR e.kind IN ('use', 'load_bearing') "
    "THEN e.session_id END), "
    "count(DISTINCT CASE WHEN e.kind IN ('use', 'load_bearing') THEN e.session_id END), "
    "count(DISTINCT CASE WHEN e.kind = 'contradiction' THEN e.session_id END)"
)
_LIVE = "archived_at IS NULL AND deleted_at IS NULL"
_LIVE_M = "m.archived_at IS NULL AND m.deleted_at IS NULL"


class BatchRefused(Exception):
    """A feedback request refused as a whole: ``{"detail": <text>}`` with ``status``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Event:
    kind: str
    ts: str  # db.format_ts
    mv_id: int | None = None
    path: str | None = None


@dataclass(frozen=True)
class Batch:
    session_id: str
    root: str | None
    events: tuple[Event, ...]
    rejected: int


# -- the formula ----------------------------------------------------------------


def trust_score(trust_0: float, trials: int, use_pos: float, contradictions: int) -> float:
    """``clamp((10 * trust_0 + u_eff) / (10 + trials), 0, 1)`` (section 9.3)."""
    u_eff = max(0.0, max(0.0, use_pos) - 2.0 * max(0, contradictions))
    raw = (PRIOR_STRENGTH * trust_0 + u_eff) / (PRIOR_STRENGTH + max(0, trials))
    return max(0.0, min(1.0, raw))


def prior_of(source_type: str, prior_mined: float) -> float:
    return prior_mined if source_type == db.SOURCE_MINED else TRUST_PRIOR_MD


# -- parse ----------------------------------------------------------------------


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_mv_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())  # ASCII only: "²".isdigit() is True
    else:
        return None
    return number if 1 <= number <= MAX_MV_ID else None


def parse_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    while path.startswith("./"):
        path = path[2:]
    if not path or len(path) > MAX_PATH_CHARS or "\x00" in path or "\\" in path:
        return None
    if path.startswith("/") or not path.endswith(".md"):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def parse_ts(value: object, now: datetime) -> str | None:
    """ISO-8601; missing is ``now``; no offset is UTC; a future time within
    one day is clamped to ``now``. Outside the window: None."""
    if value is None:
        return db.format_ts(now)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"  # Python 3.10 does not read "Z"
    try:
        moment = _utc(datetime.fromisoformat(text))
    except (ValueError, OverflowError):
        return None
    if moment > now + TS_MAX_SKEW or moment < now - TS_MAX_AGE:
        return None
    return db.format_ts(min(moment, now))


def _parse_event(item: object, now: datetime) -> Event | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    if kind not in KINDS:
        return None
    has_id = item.get("mv_id") is not None
    has_path = item.get("path") is not None
    if has_id == has_path:  # exactly one of mv_id and path
        return None
    mv_id = parse_mv_id(item.get("mv_id")) if has_id else None
    path = parse_path(item.get("path")) if has_path else None
    if mv_id is None and path is None:
        return None
    ts = parse_ts(item.get("ts"), now)
    if ts is None:
        return None
    return Event(kind=str(kind), ts=ts, mv_id=mv_id, path=path)


def parse_batch(body: Mapping[str, object], *, now: datetime | None = None) -> Batch:
    """Check a request body (section 4.5). Raises ``BatchRefused`` for a
    request refused whole; a bad event only adds to ``rejected``.

    ``persona`` and ``project`` are ignored. ``sources`` on an event is
    ignored: v0.1 stores no sources.
    """
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise BatchRefused(
            400,
            "session_id must be 1-128 characters of [A-Za-z0-9._:-]"
            " and start with a letter or digit",
        )
    events = body.get("events")
    if not isinstance(events, list):
        raise BatchRefused(400, "events must be a list")
    if len(events) > MAX_EVENTS:
        raise BatchRefused(413, f"at most {MAX_EVENTS} events per request")
    raw_root = body.get("root")
    if raw_root is not None and (not isinstance(raw_root, str) or len(raw_root) > MAX_ROOT_CHARS):
        raise BatchRefused(400, "root must be a string")
    root = raw_root.strip() if isinstance(raw_root, str) and raw_root.strip() else None
    parsed: list[Event] = []
    rejected = 0
    for item in events:
        event = _parse_event(item, now)
        if event is None:
            rejected += 1
        else:
            parsed.append(event)
    return Batch(session_id=session_id, root=root, events=tuple(parsed), rejected=rejected)


# -- ingest ---------------------------------------------------------------------


def _resolve_ids(conn: sqlite3.Connection, project: str, ids: Sequence[int]) -> set[int]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id FROM memories WHERE project = ? AND {_LIVE} AND id IN ({marks})",
        (project, *ids),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _resolve_paths(
    conn: sqlite3.Connection, project: str, root: str | None, paths: Sequence[str]
) -> dict[str, int]:
    """A path resolves in ``root``; without a root only when exactly one live
    ``claude_code_md`` row has it (section 5.1)."""
    out: dict[str, int] = {}
    for path in paths:
        if root is not None:
            rows = conn.execute(
                f"SELECT id FROM memories WHERE project = ? AND source_type = ? AND {_LIVE} "
                "AND root = ? AND path = ?",
                (project, db.SOURCE_MD, root, path),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id FROM memories WHERE project = ? AND source_type = ? AND {_LIVE} "
                "AND path = ? LIMIT 2",
                (project, db.SOURCE_MD, path),
            ).fetchall()
        if len(rows) == 1:
            out[path] = int(rows[0][0])
    return out


def store_batch(
    conn: sqlite3.Connection,
    batch: Batch,
    *,
    project: str = PERSONA,
    prior_mined: float = 0.3,
    now: datetime | None = None,
) -> dict[str, int]:
    """Write one batch in ONE ``BEGIN IMMEDIATE`` transaction (section 9.2).

    Returns ``{"inserted", "duplicate", "unknown", "rejected"}``. An error
    rolls the whole batch back and propagates (the route answers 503).
    """
    counts = {"inserted": 0, "duplicate": 0, "unknown": 0, "rejected": int(batch.rejected)}
    if not batch.events:
        return counts
    received = db.format_ts(_utc(now) if now is not None else datetime.now(timezone.utc))
    with db.write_tx(conn):
        live_ids = _resolve_ids(
            conn, project, sorted({e.mv_id for e in batch.events if e.mv_id is not None})
        )
        path_ids = _resolve_paths(
            conn, project, batch.root, sorted({e.path for e in batch.events if e.path is not None})
        )
        first: dict[tuple[int, str], str] = {}
        for event in batch.events:
            if event.mv_id is not None:
                memory_id = event.mv_id if event.mv_id in live_ids else None
            else:
                memory_id = path_ids.get(event.path or "")
            if memory_id is None:
                counts["unknown"] += 1
                continue
            key = (memory_id, event.kind)
            if key in first:
                counts["duplicate"] += 1
                first[key] = min(first[key], event.ts)
            else:
                first[key] = event.ts
        touched: set[int] = set()
        for (memory_id, kind), ts in sorted(first.items()):
            row = conn.execute(
                "INSERT INTO feedback_events "
                "(session_id, memory_id, kind, citation_capable, ts, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (session_id, memory_id, kind) DO NOTHING RETURNING id",
                (batch.session_id, memory_id, kind, CITATION_CAPABLE[kind], ts, received),
            ).fetchone()
            if row is None:
                counts["duplicate"] += 1
            else:
                counts["inserted"] += 1
                touched.add(memory_id)
        if touched:
            recompute(conn, sorted(touched), prior_mined=prior_mined)
    return counts


# -- recompute and repair ---------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    memory_id: int
    trust_0: float
    trials: int
    use_pos: int
    contradictions: int
    last_recalled_at: str | None
    last_used_at: str | None
    folded_event_id: int


def read_counts(
    conn: sqlite3.Connection, memory_ids: Sequence[int], *, prior_mined: float
) -> list[Counts]:
    """The event counts and the prior of each memory row in ``memory_ids``."""
    if not memory_ids:
        return []
    marks = ",".join("?" * len(memory_ids))
    rows = conn.execute(
        "SELECT m.id, m.source_type, f.trust_0, "
        f"{_COUNTS_SQL}, "
        "max(CASE WHEN e.kind = 'recall' THEN e.ts END), "
        "max(CASE WHEN e.kind IN ('use', 'load_bearing') THEN e.ts END), "
        "coalesce(max(e.id), 0) "
        "FROM memories m LEFT JOIN feedback f ON f.memory_id = m.id "
        "LEFT JOIN feedback_events e ON e.memory_id = m.id "
        f"WHERE m.id IN ({marks}) GROUP BY m.id",
        tuple(memory_ids),
    ).fetchall()
    out: list[Counts] = []
    for r in rows:
        trust_0 = float(r[2]) if r[2] is not None else prior_of(str(r[1]), prior_mined)
        out.append(
            Counts(int(r[0]), trust_0, int(r[3]), int(r[4]), int(r[5]), r[6], r[7], int(r[8]))
        )
    return out


def write_rollups(conn: sqlite3.Connection, counts: Iterable[Counts]) -> int:
    """Upsert the rollup rows from ``counts``. Returns the number written."""
    written = 0
    for c in counts:
        score = trust_score(c.trust_0, c.trials, float(c.use_pos), c.contradictions)
        conn.execute(
            "INSERT INTO feedback (memory_id, trust_0, trials, use_pos, contradiction_count, "
            "trust_score, last_recalled_at, last_used_at, folded_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (memory_id) DO UPDATE SET trials = excluded.trials, "
            "use_pos = excluded.use_pos, contradiction_count = excluded.contradiction_count, "
            "trust_score = excluded.trust_score, last_recalled_at = excluded.last_recalled_at, "
            "last_used_at = excluded.last_used_at, folded_event_id = excluded.folded_event_id",
            (
                c.memory_id,
                c.trust_0,
                c.trials,
                float(c.use_pos),
                c.contradictions,
                score,
                c.last_recalled_at,
                c.last_used_at,
                c.folded_event_id,
            ),
        )
        written += 1
    return written


def recompute(
    conn: sqlite3.Connection, memory_ids: Sequence[int], *, prior_mined: float = 0.3
) -> int:
    """Read the event counts and write the rollup rows of ``memory_ids``.

    Call it inside ``db.write_tx``: the read and the write must be one
    ``BEGIN IMMEDIATE`` transaction, so no ingest commits between them.
    """
    if not conn.in_transaction:
        raise RuntimeError("recompute runs inside a write transaction")
    return write_rollups(conn, read_counts(conn, memory_ids, prior_mined=prior_mined))


def stale_ids(conn: sqlite3.Connection, limit: int = db.BATCH_ROWS) -> list[int]:
    """Memory rows with an event above their rollup's ``folded_event_id``."""
    rows = conn.execute(
        "SELECT e.memory_id FROM feedback_events e "
        "LEFT JOIN feedback f ON f.memory_id = e.memory_id GROUP BY e.memory_id "
        "HAVING max(e.id) > coalesce(max(f.folded_event_id), 0) ORDER BY e.memory_id LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [int(r[0]) for r in rows]


def repair(conn: sqlite3.Connection, *, prior_mined: float = 0.3) -> int:
    """Recompute every stale rollup row, 200 rows per ``BEGIN IMMEDIATE``
    transaction. The stale scan, the read and the write of one batch are one
    transaction. Returns the number of rows recomputed."""
    total = 0
    while True:
        with db.write_tx(conn):
            ids = stale_ids(conn, db.BATCH_ROWS)
            if ids:
                recompute(conn, ids, prior_mined=prior_mined)
        total += len(ids)
        if len(ids) < db.BATCH_ROWS:
            return total


# -- maintenance ----------------------------------------------------------------


@dataclass
class MaintenanceResult:
    repaired: int = 0
    purged_deleted: int = 0
    purged_archived: int = 0
    orphans: int = 0
    failed: str = ""

    def summary(self) -> str:
        text = (
            f"repaired={self.repaired} purged_deleted={self.purged_deleted} "
            f"purged_archived={self.purged_archived} orphans={self.orphans}"
        )
        return f"{text} failed={self.failed}" if self.failed else text


def maintenance_pass(
    conn: sqlite3.Connection,
    *,
    delete_grace_days: int,
    archive_retention_days: int,
    prior_mined: float = 0.3,
    backups_dir: Path | None = None,
    now: datetime | None = None,
) -> MaintenanceResult:
    """Section 9.3: trust repair, the purges, the orphan check, the backup
    prune, then ``meta.last_maintenance_at``. Each step runs in its own
    batches; a failed step is logged and the next step still runs."""
    result = MaintenanceResult()
    steps = (
        ("repair", lambda: setattr(result, "repaired", repair(conn, prior_mined=prior_mined))),
        (
            "purge_deleted",
            lambda: setattr(
                result, "purged_deleted", db.purge_deleted(conn, delete_grace_days, now=now)
            ),
        ),
        (
            "purge_archived",
            lambda: setattr(
                result,
                "purged_archived",
                db.purge_archived(conn, archive_retention_days, now=now),
            ),
        ),
        ("fk_check", lambda: setattr(result, "orphans", len(db.foreign_key_problems(conn)))),
    )
    for name, step in steps:
        try:
            step()
        except (sqlite3.Error, OSError) as exc:
            log.error("maintenance step %s failed: %s", name, type(exc).__name__)
            result.failed = name
    if result.orphans:
        log.error("maintenance: %d rows fail the foreign key check", result.orphans)
    if backups_dir is not None and backups_dir.is_dir():
        try:
            db.prune_backups(backups_dir)
        except OSError as exc:
            log.error("backup prune failed: %s", type(exc).__name__)
    try:
        with db.write_tx(conn):
            stamp = _utc(now) if now is not None else datetime.now(timezone.utc)
            db.set_meta(conn, "last_maintenance_at", db.format_ts(stamp))
    except sqlite3.Error as exc:
        log.error("maintenance stamp failed: %s", type(exc).__name__)
    return result


# -- the report -----------------------------------------------------------------


@dataclass(frozen=True)
class FileRow:
    mv_id: int
    root: str
    path: str
    category: str
    created_at: datetime


@dataclass(frozen=True)
class EventAgg:
    first_ts: datetime
    shown_sessions: int
    use_sessions: int
    trials: int
    contradictions: int
    last_use: datetime | None


@dataclass(frozen=True)
class ReportInputs:
    files: tuple[FileRow, ...]
    index_links: Mapping[str, frozenset[str]]  # root -> paths MEMORY.md links to
    aggs: Mapping[int, EventAgg]
    trust0: Mapping[int, float]
    sessions: tuple[tuple[datetime, datetime], ...]  # (first, last) event per session


def is_index_file(path: str, category: str = "") -> bool:
    """``MEMORY*.md`` and ``topic_*.md`` are navigation, not rules."""
    name = path.rsplit("/", 1)[-1]
    return category in INDEX_CATEGORIES or name.startswith(("topic_", "MEMORY"))


def memory_index_links(text: str) -> frozenset[str]:
    """The ``.md`` files a ``MEMORY.md`` body links to, relative to its folder."""
    out: set[str] = set()
    for match in _MD_LINK_RE.finditer(text or ""):
        path = parse_path(match.group(1))
        if path:
            out.add(path)
    return frozenset(out)


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return db.parse_ts(text)
    except ValueError:
        return None


def load_report_inputs(conn: sqlite3.Connection, project: str = PERSONA) -> ReportInputs:
    """Everything the report needs, in one deferred read transaction."""
    live_md = f"m.project = ? AND m.source_type = ? AND {_LIVE_M}"
    with db.read_tx(conn):
        files = tuple(
            FileRow(int(r[0]), str(r[1]), str(r[2]), str(r[3] or ""), db.parse_ts(str(r[4])))
            for r in conn.execute(
                f"SELECT m.id, m.root, m.path, m.category, m.created_at FROM memories m "
                f"WHERE {live_md}",
                (project, db.SOURCE_MD),
            )
        )
        links = {
            str(r[0]): memory_index_links(str(r[1]))
            for r in conn.execute(
                f"SELECT m.root, m.content FROM memories m WHERE {live_md} AND m.path = ?",
                (project, db.SOURCE_MD, MEMORY_INDEX_PATH),
            )
        }
        aggs: dict[int, EventAgg] = {}
        for r in conn.execute(
            "SELECT e.memory_id, min(e.ts), "
            "count(DISTINCT CASE WHEN e.kind = 'recall' THEN e.session_id END), "
            f"{_COUNTS_SQL}, "
            "max(CASE WHEN e.kind IN ('use', 'load_bearing') THEN e.ts END) "
            f"FROM feedback_events e JOIN memories m ON m.id = e.memory_id WHERE {live_md} "
            "GROUP BY e.memory_id",
            (project, db.SOURCE_MD),
        ):
            first_ts = _ts(r[1])
            if first_ts is None:
                continue
            aggs[int(r[0])] = EventAgg(
                first_ts=first_ts,
                shown_sessions=int(r[2]),
                trials=int(r[3]),
                use_sessions=int(r[4]),
                contradictions=int(r[5]),
                last_use=_ts(r[6]),
            )
        trust0 = {
            int(r[0]): float(r[1])
            for r in conn.execute(
                f"SELECT f.memory_id, f.trust_0 FROM feedback f "
                f"JOIN memories m ON m.id = f.memory_id WHERE {live_md}",
                (project, db.SOURCE_MD),
            )
        }
        sessions = []
        for r in conn.execute(
            "SELECT min(e.ts), max(e.ts) FROM feedback_events e "
            "JOIN memories m ON m.id = e.memory_id WHERE m.project = ? GROUP BY e.session_id",
            (project,),
        ):
            first, last = _ts(r[0]), _ts(r[1])
            if first is not None and last is not None:
                sessions.append((first, last))
    return ReportInputs(files, links, aggs, trust0, tuple(sessions))


def _days(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 86400))


def build_report(inputs: ReportInputs, *, now: datetime | None = None) -> dict:
    """The section 4.6 answer from its inputs. Pure, except for the clock
    when ``now`` is None. Trust is computed on read with ``trust_score``."""
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    session_last = sorted(last for _first, last in inputs.sessions)
    history_start = min((first for first, _last in inputs.sessions), default=None)
    history_days = _days(now - history_start) if history_start else 0
    no_use_since = now - timedelta(days=DEMOTE_NO_USE_DAYS)
    retire: list[dict] = []
    promote: list[dict] = []
    demote: list[dict] = []
    for f in inputs.files:
        if is_index_file(f.path, f.category):
            continue
        agg = inputs.aggs.get(f.mv_id)
        shown = agg.shown_sessions if agg else 0
        used = agg.use_sessions if agg else 0
        trials = agg.trials if agg else 0
        trust_0 = inputs.trust0.get(f.mv_id, TRUST_PRIOR_MD)
        trust = trust_score(trust_0, trials, float(used), agg.contradictions if agg else 0)
        row = {
            "mv_id": f.mv_id,
            "root": f.root,
            "path": f.path,
            "trust": round(trust, 4),
            "trials": trials,
            "shown_sessions": shown,
            "use_sessions": used,
        }
        if f.path in inputs.index_links.get(f.root, frozenset()):
            last_use = agg.last_use if agg else None
            if history_days >= DEMOTE_NO_USE_DAYS and (last_use is None or last_use < no_use_since):
                when = f"last use {_days(now - last_use)} days ago" if last_use else "never used"
                reason = f"linked from MEMORY.md, no use in the last {DEMOTE_NO_USE_DAYS} days"
                demote.append({**row, "reason": f"{reason} ({when})"})
            continue
        age = _days(now - f.created_at)
        if shown >= RETIRE_MIN_SHOWN_SESSIONS and used == 0 and age >= RETIRE_MIN_FILE_AGE_DAYS:
            reason = f"shown in {shown} sessions, used in none; first indexed {age} days ago"
            retire.append({**row, "reason": reason})
            continue
        if agg and trust >= PROMOTE_MIN_TRUST and trials >= PROMOTE_MIN_TRIALS:
            span = _days(now - agg.first_ts)
            since = len(session_last) - bisect.bisect_left(session_last, agg.first_ts)
            share = used / since if since else 0.0
            if span >= PROMOTE_MIN_SPAN_DAYS and share >= PROMOTE_MIN_USE_SHARE:
                reason = (
                    f"used in {used} of {since} sessions ({share:.0%}) over {span} days, "
                    f"trust {trust:.2f}"
                )
                promote.append({**row, "reason": reason})
    retire.sort(key=lambda r: (-r["shown_sessions"], r["path"], r["root"]))
    promote.sort(key=lambda r: (-r["trust"], -r["use_sessions"], r["path"], r["root"]))
    demote.sort(key=lambda r: (r["path"], r["root"]))
    truncated = any(len(rows) > REPORT_LIST_CAP for rows in (retire, promote, demote))
    return {
        "persona": PERSONA,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trust_prior": TRUST_PRIOR_MD,
        "retire": retire[:REPORT_LIST_CAP],
        "promote": promote[:REPORT_LIST_CAP],
        "demote": demote[:REPORT_LIST_CAP],
        "truncated": truncated,
    }


def report(
    conn: sqlite3.Connection, project: str = PERSONA, *, now: datetime | None = None
) -> dict:
    """``GET /api/memory/trust/report``. Raises on a database error."""
    return build_report(load_report_inputs(conn, project), now=now)


# -- CLI: noblivion trust recompute ------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """``noblivion trust recompute``: the repair pass, same as at store start."""
    import argparse
    import json

    from noblivion import config

    parser = argparse.ArgumentParser(prog="noblivion trust")
    parser.add_argument("action", choices=("recompute",))
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    settings = config.load_settings()
    store_settings = config.load_store_settings()
    try:
        conn = db.open_db(settings.db_path, allow_migrate=False)
    except db.SchemaError as exc:
        print(f"noblivion trust: {exc}")
        return 3
    try:
        repaired = repair(conn, prior_mined=store_settings.prior_mined)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"repaired": repaired}))
    else:
        print(f"trust recompute: {repaired} rollup rows recomputed")
    return 0
