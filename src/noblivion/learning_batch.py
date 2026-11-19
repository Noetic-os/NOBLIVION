# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session batch learning with a watermark (NOBLIVION-267).

Off by default: ``"batch": {"enabled": true}`` in learning.json turns it on. With
the setting on, the worker runs one *batch item* per session instead of one
extract job per capture. A batch item is an ordinary extract job with a range
(``learning_job_ranges``) and a row in ``learning_batch_items``. Its range starts
after the session's watermark and takes no earlier context, so text is not read
twice.

- Planning (``plan``) runs inside the claim transaction, so only when a worker may
  call: inside the night window and after the load probe let it go on.
- The per-capture jobs that hold no saved work (``NO_WORK``) wait for their
  session item (``superseded_ids``). Started jobs that hold work keep it and run
  as before; no item reads the text they cover.
- ``commit`` runs in the ``finish`` transaction of an item: it raises the session
  watermark and cancels the covered jobs that hold no work, with the reason ``batch``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from noblivion import learning_ranges

# last_error of a per-capture job that a committed batch item covered.
BATCH_REASON = "batch"
RECEIPT_PREFIX = "session-batch:"

_KEY = ("project", "root", "session_id", "source_client")
_SAME_SESSION = "r.project=? AND r.root=? AND r.session_id=? AND r.source_client=?"
_NOT_ITEM = "NOT EXISTS (SELECT 1 FROM learning_batch_items i WHERE i.job_id=j.id)"
_WATERMARK = (
    "coalesce((SELECT w.last_seq FROM learning_session_watermarks w WHERE w.project=r.project "
    "AND w.root=r.root AND w.session_id=r.session_id AND w.source_client=r.source_client),0)"
)
# A session whose batch item is exhausted past the watermark runs per-capture
# jobs as before, so a failing item never repeats on the same text.
_FALLBACK = (
    "EXISTS (SELECT 1 FROM learning_batch_items bi JOIN learning_jobs x ON x.id=bi.job_id "
    "JOIN learning_job_ranges xr ON xr.job_id=x.id WHERE x.status='exhausted' "
    "AND xr.project=r.project AND xr.root=r.root AND xr.session_id=r.session_id "
    "AND xr.source_client=r.source_client AND xr.last_seq>" + _WATERMARK + ")"
)

# An eligible source ``s`` in the range ``r`` of a job.
_SOURCE_IN_R = (
    "SELECT 1 FROM learning_sources s WHERE s.project=r.project AND s.root=r.root "
    "AND s.session_id=r.session_id AND s.source_client=r.source_client "
    "AND s.status='eligible' AND s.seq BETWEEN r.first_seq AND r.last_seq"
)
# A pending extract job ``j`` that holds no saved work: no extraction page, no
# window position, no continuation, no decision and no partial-work key in its
# progress (the test of LearningWorker._may_refreeze). Unlike
# ``learning_ranges.UNSTARTED``, attempts, budget rows and a frozen configuration
# do not count: a job that failed before its first saved result (a provider
# outage, for example) has nothing to lose. A batch item treats such a job as
# unstarted: it reads the job's text and cancels the job. Capture extension keeps
# ``UNSTARTED``, because it must not change the range of a frozen job.
NO_WORK = (
    "j.kind='extract' AND j.status='pending' AND json_type(j.progress)='object' "
    "AND coalesce(json_extract(j.progress,'$.next_window'),0)=0 "
    "AND coalesce(json_extract(j.progress,'$.next_continuation'),0)=0 "
    "AND (json_type(j.progress,'$.decisions') IS NULL "
    "OR (json_type(j.progress,'$.decisions')='array' "
    "AND json_array_length(j.progress,'$.decisions')=0)) "
    + "".join(
        "AND json_type(j.progress,'$." + key + "') IS NULL " for key in learning_ranges.PARTIAL_WORK
    )
    + "AND NOT EXISTS (SELECT 1 FROM learning_extraction_pages p WHERE p.job_id=j.id)"
)
# Jobs ``c`` that read their text: done ones, and started ones that still run.
# A pending job that holds no work does not read its text: an item reads it.
_DONE = "c.status='done'"
_DONE_OR_STARTED = (
    "(c.status IN ('done','leased') OR (c.status='pending' AND NOT EXISTS "
    "(SELECT 1 FROM learning_jobs j WHERE j.id=c.id AND " + NO_WORK + ")))"
)


def _read_by(jobs: str) -> str:
    """The source ``s`` lies in the range of an extract job ``c`` of its session."""
    return (
        "EXISTS (SELECT 1 FROM learning_job_ranges cr JOIN learning_jobs c ON c.id=cr.job_id "
        "WHERE cr.project=s.project AND cr.root=s.root AND cr.session_id=s.session_id "
        "AND cr.source_client=s.source_client AND s.seq BETWEEN cr.first_seq AND cr.last_seq "
        "AND c.kind='extract' AND " + jobs + ")"
    )


# An item starts after the text that a started job reads. That text (after the
# watermark at plan time, before the item's first source) is below the watermark
# once the item commits, but the item did not read it. All other text at or below
# the watermark was read by an item.
_SKIPPED = (
    "EXISTS (SELECT 1 FROM learning_batch_items g JOIN learning_job_ranges gr "
    "ON gr.job_id=g.job_id WHERE gr.project=s.project AND gr.root=s.root "
    "AND gr.session_id=s.session_id AND gr.source_client=s.source_client "
    "AND s.seq>g.mark_seq AND s.seq<gr.first_seq)"
)
# Every eligible source of the job ``r`` was read: by an item, or by a done job.
_ALL_READ = (
    "NOT EXISTS ("
    + _SOURCE_IN_R
    + " AND (s.seq>"
    + _WATERMARK
    + " OR "
    + _SKIPPED
    + ") AND NOT "
    + _read_by(_DONE)
    + ")"
)
# The job ``r`` holds text below the watermark that an item skipped and that no
# done or started job reads. No item ever reads text below the watermark, so this
# job runs as a per-capture job, as in fallback mode.
_ORPHAN = (
    "EXISTS ("
    + _SOURCE_IN_R
    + " AND s.seq<="
    + _WATERMARK
    + " AND "
    + _SKIPPED
    + " AND NOT "
    + _read_by(_DONE_OR_STARTED)
    + ")"
)


def batch_options(settings: dict) -> bool:
    """True when the ``batch`` block of learning.json turns batch learning on.

    No block means off. The block takes only ``enabled``, a bool; anything else
    is refused, and a refused block turns learning off like any malformed setting.
    """
    block = settings.get("batch")
    if block is None:
        return False
    if not isinstance(block, dict) or set(block) - {"enabled"}:
        raise ValueError("invalid learning batch settings")
    enabled = block.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("invalid learning batch settings")
    return enabled


def is_item(conn, job_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM learning_batch_items WHERE job_id=?", (job_id,)).fetchone()
        is not None
    )


def watermark(conn, key: tuple) -> int:
    row = conn.execute(
        "SELECT last_seq FROM learning_session_watermarks "
        "WHERE project=? AND root=? AND session_id=? AND source_client=?",
        key,
    ).fetchone()
    return row[0] if row else 0


def _inside(ranges: list[tuple[int, int]], seq: int) -> bool:
    return any(first <= seq <= last for first, last in ranges)


def plan(conn, now: float) -> list[str]:
    """Make one batch item for each session that has open work (``NO_WORK``). The caller
    holds the write transaction. Return the new item ids."""
    sessions = conn.execute(
        "SELECT DISTINCT r.project,r.root,r.session_id,r.source_client FROM learning_jobs j "
        "JOIN learning_job_ranges r ON r.job_id=j.id WHERE "
        + NO_WORK
        + " AND "
        + _NOT_ITEM
        + " ORDER BY r.project,r.root,r.session_id,r.source_client"
    ).fetchall()
    made = []
    for session in sessions:
        key = tuple(session[name] for name in _KEY)
        item = _plan_session(conn, key, now)
        if item is not None:
            made.append(item)
    return made


def _plan_session(conn, key: tuple, now: float) -> str | None:
    mark = watermark(conn, key)
    jobs = conn.execute(
        "SELECT j.status,r.first_seq,r.last_seq,"
        "(" + NO_WORK + ") AS fresh,"
        "EXISTS (SELECT 1 FROM learning_batch_items i WHERE i.job_id=j.id) AS item,"
        "(" + _ORPHAN + ") AS orphan "
        "FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id "
        "WHERE j.kind='extract' AND " + _SAME_SESSION,
        key,
    ).fetchall()
    # A job with no work and with skipped text below the watermark runs on its own
    # (``_ORPHAN``): it is not open work for an item, and its text is covered.
    waiting = [j for j in jobs if j["fresh"] and not j["item"]]
    for job in jobs:
        if job["item"] and (
            job["status"] in ("pending", "leased")
            or (job["status"] == "exhausted" and job["last_seq"] > mark)
        ):
            # One live item per session; a failed one hands the session back
            # to per-capture jobs until a retry or prune.
            return None
    open_ranges = [(j["first_seq"], j["last_seq"]) for j in waiting if not j["orphan"]]
    if not open_ranges:
        return None
    # Text that a done job read, or that a started job is reading, is not read again.
    covered = [
        (j["first_seq"], j["last_seq"])
        for j in jobs
        if j["status"] == "done" or (j["status"] in ("pending", "leased") and not j["fresh"])
    ] + [(j["first_seq"], j["last_seq"]) for j in waiting if j["orphan"]]
    newest = max(last for _, last in open_ranges)
    sources = conn.execute(
        "SELECT seq,length(content) size FROM learning_sources WHERE project=? AND root=? "
        "AND session_id=? AND source_client=? AND status='eligible' AND seq>? AND seq<=? "
        "ORDER BY seq",
        (*key, mark, newest),
    ).fetchall()
    start = next(
        (
            index
            for index, source in enumerate(sources)
            if _inside(open_ranges, source["seq"]) and not _inside(covered, source["seq"])
        ),
        None,
    )
    if start is None:
        # All open text is already handled: the open jobs hold nothing new. Only
        # the jobs whose text is read are cancelled; the others wait for the
        # started jobs that read it (``cancel_covered``).
        cancel_covered(conn, key, newest, now)
        return None
    total, last = 0, None
    for source in sources[start:]:
        if _inside(covered, source["seq"]):
            break
        if last is not None and total + source["size"] > learning_ranges.JOB_CHARS:
            break
        total += source["size"]
        last = source["seq"]
    first = sources[start]["seq"]
    job_id = uuid.uuid4().hex
    receipt = RECEIPT_PREFIX + job_id
    digest = hashlib.sha256(
        json.dumps([*key, first, last], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    project, root, session, client = key
    conn.execute(
        "INSERT INTO learning_batches VALUES (?,?,?,?,?,0,?)",
        (receipt, digest, project, root, session, now),
    )
    conn.execute(
        "INSERT INTO learning_jobs "
        "(id,batch_id,project,root,session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, receipt, project, root, session, now, now),
    )
    conn.execute(
        "INSERT INTO learning_job_ranges VALUES (?,?,?,?,?,?,?)",
        (job_id, project, root, session, client, first, last),
    )
    conn.execute(
        "INSERT INTO learning_batch_items (job_id,created_at,mark_seq) VALUES (?,?,?)",
        (job_id, now, mark),
    )
    return job_id


def superseded_ids(conn) -> set[str]:
    """Pending per-capture jobs that wait for their session's batch item."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT j.id FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id "
            "WHERE "
            + NO_WORK
            + " AND "
            + _NOT_ITEM
            + " AND NOT "
            + _FALLBACK
            + " AND NOT "
            + _ORPHAN
        )
    }


# The waiting per-capture jobs whose session already has a pending or leased item.
HELD = (
    "SELECT j.id FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id WHERE "
    + NO_WORK
    + " AND "
    + _NOT_ITEM
    + " AND NOT "
    + _FALLBACK
    + " AND NOT "
    + _ORPHAN
    + " AND EXISTS (SELECT 1 FROM learning_batch_items li JOIN learning_jobs l "
    "ON l.id=li.job_id JOIN learning_job_ranges lr ON lr.job_id=l.id "
    "WHERE l.status IN ('pending','leased') AND lr.project=r.project AND lr.root=r.root "
    "AND lr.session_id=r.session_id AND lr.source_client=r.source_client)"
)


def held_ids(conn) -> set[str]:
    """No worker takes these jobs; the item counts for itself (``work_open``)."""
    return {row[0] for row in conn.execute(HELD)}


def cancel_covered(conn, key: tuple, upto: int, now: float) -> int:
    """Cancel the session's per-capture jobs that hold no work (``NO_WORK``), end at
    or below ``upto`` and whose text was read (``_ALL_READ``): by an item, or by a
    done job. Such a job may hold attempts, budget rows and a frozen configuration.

    A job with text that an item skipped, or that only a started job reads, stays.
    It waits for that job, or runs on its own when no job reads the text
    (``_ORPHAN``). The caller holds the write transaction. One UPDATE of pending
    rows only, so a second run changes nothing. Jobs that hold work and batch items are
    never cancelled.
    """
    return conn.execute(
        "UPDATE learning_jobs SET status='cancelled',last_error=?,lease_token=NULL,"
        "lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id IN ("
        "SELECT j.id FROM learning_jobs j JOIN learning_job_ranges r ON r.job_id=j.id "
        "WHERE " + _SAME_SESSION + " AND r.last_seq<=? AND " + NO_WORK + " "
        "AND " + _NOT_ITEM + " AND " + _ALL_READ + ")",
        (BATCH_REASON, now, *key, upto),
    ).rowcount


def drop_read_items(conn, now: float) -> list[str]:
    """Cancel each pending item whose whole text done jobs already read, and commit it.

    This happens when an exhausted item hands its session to per-capture jobs,
    they finish, and an operator then retries the item. The item must not read
    the text again. The caller holds the write transaction. Return the ids.
    """
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT j.id FROM learning_jobs j JOIN learning_batch_items i ON i.job_id=j.id "
            "JOIN learning_job_ranges r ON r.job_id=j.id WHERE j.status='pending' "
            "AND NOT EXISTS (" + _SOURCE_IN_R + " AND NOT " + _read_by(_DONE) + ")"
        )
    ]
    for job_id in ids:
        conn.execute(
            "UPDATE learning_jobs SET status='cancelled',last_error=?,updated_at=? WHERE id=?",
            (BATCH_REASON, now, job_id),
        )
        commit(conn, job_id, now)
    return ids


def commit(conn, job_id: str, now: float) -> int | None:
    """In the ``finish`` transaction of a job: when it is a batch item, raise the
    session watermark to its range end and cancel the covered jobs. Return the
    number cancelled, or None for a job that is not a batch item."""
    if not is_item(conn, job_id):
        return None
    row = conn.execute(
        "SELECT project,root,session_id,source_client,last_seq FROM learning_job_ranges "
        "WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    key = tuple(row[name] for name in _KEY)
    conn.execute(
        "INSERT INTO learning_session_watermarks VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(project,root,session_id,source_client) DO UPDATE SET "
        "job_id=CASE WHEN excluded.last_seq>last_seq THEN excluded.job_id ELSE job_id END,"
        "last_seq=max(last_seq,excluded.last_seq),updated_at=excluded.updated_at",
        (*key, row["last_seq"], job_id, now),
    )
    return cancel_covered(conn, key, watermark(conn, key), now)


def report(conn, where: str, params: list) -> dict[str, Any]:
    """The batch part of ``noblivion learning batch-report``. ``where`` filters ``j``."""
    items: dict[str, int] = {}
    seconds, calls, done = 0.0, 0, 0
    for row in conn.execute(
        "SELECT j.status,j.progress FROM learning_jobs j JOIN learning_batch_items i "
        "ON i.job_id=j.id WHERE " + where,
        params,
    ):
        items[row["status"]] = items.get(row["status"], 0) + 1
        if row["status"] != "done":
            continue
        done += 1
        try:
            metrics = json.loads(row["progress"]).get("provider_metrics") or {}
        except (ValueError, AttributeError):
            metrics = {}
        if isinstance(metrics, dict):
            value = metrics.get("seconds", 0)
            seconds += value if isinstance(value, (int, float)) else 0
            value = metrics.get("calls", 0)
            calls += value if isinstance(value, int) else 0
    cancelled = conn.execute(
        "SELECT count(*) FROM learning_jobs j WHERE " + where + " AND j.status='cancelled' "
        "AND j.last_error=?",
        (*params, BATCH_REASON),
    ).fetchone()[0]
    marks = conn.execute(
        "SELECT count(*) FROM learning_session_watermarks j WHERE "
        + where.replace("j.kind='extract'", "1"),
        params,
    ).fetchone()[0]
    return {
        "items": dict(sorted(items.items())),
        "cancelled_jobs": cancelled,
        "sessions_with_watermark": marks,
        "done_items": done,
        "model_calls": calls,
        "model_seconds": round(seconds, 3),
        "model_seconds_per_done_item": round(seconds / done, 3) if done else None,
    }
