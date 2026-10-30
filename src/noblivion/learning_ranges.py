# SPDX-License-Identifier: AGPL-3.0-or-later
"""Range links, one job per session, and pruning for the learner tables (NOBLIVION-143).

A capture job names the sources it reads as a range of per-session sequence numbers
(``learning_sources.seq``), so one job is one small row instead of one row per source.
A session has at most one pending job: later batches extend its range. Pruning drops
stale pending jobs, the links of finished-with-error jobs and old sources that no
note, claim or live job needs. A forgotten or excluded source stays, because its hash
blocks the same text from coming back.
"""

from __future__ import annotations

import time
from typing import Any

# Context before the first new source of a job: at most this many sources and bytes.
CONTEXT_SOURCES = 64
CONTEXT_BYTES = 120 * 1024
# The extractor refuses a job of more than 262144 characters. A pending job takes new
# sources only while its range stays under this size; past it, a new job starts.
JOB_CHARS = 200_000

DEFAULT_PENDING_DAYS = 14.0
DEFAULT_RETENTION_DAYS = 14.0

_IN_RANGE = (
    "s.project=r.project AND s.root=r.root AND s.session_id=r.session_id "
    "AND s.source_client=r.source_client AND s.seq BETWEEN r.first_seq AND r.last_seq"
)


def next_seq(conn, project: str, root: str, session: str, client: str) -> int:
    """The highest sequence number stored for this session and client."""
    return conn.execute(
        "SELECT coalesce(max(seq),0) FROM learning_sources "
        "WHERE project=? AND root=? AND session_id=? AND source_client=?",
        (project, root, session, client),
    ).fetchone()[0]


def source_seqs(conn, source_ids: list[str]) -> list[int]:
    seqs = []
    for source_id in source_ids:
        row = conn.execute("SELECT seq FROM learning_sources WHERE id=?", (source_id,)).fetchone()
        seqs.append(row[0] if row else 0)
    return seqs


def attach_job(
    conn,
    ids: tuple[str, str, str, str, str],
    low: int,
    high: int,
    now: float,
    new_job_id: str,
    added_chars: int,
) -> str:
    """Give the new sources (seq low..high) to the session's pending job, or make one.

    ids = (project, root, session, client, batch_id). A pending job that no worker has
    started takes the new sources into its range while it stays within JOB_CHARS.
    Otherwise a new job starts, with the newest earlier sources of the session as context.
    ``added_chars`` is the size of the new sources.
    """
    project, root, session, client, batch_id = ids
    open_job = conn.execute(
        "SELECT j.id,r.first_seq,r.last_seq FROM learning_jobs j "
        "JOIN learning_job_ranges r ON r.job_id=j.id "
        "WHERE j.project=? AND j.root=? AND j.session_id=? AND r.source_client=? "
        "AND j.kind='extract' AND j.status='pending' AND j.attempts=0 "
        "ORDER BY j.created_at DESC LIMIT 1",
        (project, root, session, client),
    ).fetchone()
    if open_job and (
        conn.execute(
            "SELECT coalesce(sum(length(s.content)),0) FROM learning_job_ranges r "
            "JOIN learning_sources s ON " + _IN_RANGE + " WHERE r.job_id=? AND s.status='eligible'",
            (open_job["id"],),
        ).fetchone()[0]
        + added_chars
        <= JOB_CHARS
    ):
        conn.execute(
            "UPDATE learning_job_ranges SET first_seq=?,last_seq=? WHERE job_id=?",
            (min(open_job["first_seq"], low), max(open_job["last_seq"], high), open_job["id"]),
        )
        conn.execute("UPDATE learning_jobs SET updated_at=? WHERE id=?", (now, open_job["id"]))
        return open_job["id"]
    first, total = low, added_chars
    for earlier in conn.execute(
        "SELECT seq,length(content) size FROM learning_sources WHERE project=? AND root=? "
        "AND session_id=? AND source_client=? AND status='eligible' AND seq>0 AND seq<? "
        "ORDER BY seq DESC LIMIT ?",
        (project, root, session, client, low, CONTEXT_SOURCES),
    ).fetchall():
        # Stop at the first source that does not fit: the range must stay one piece.
        if total + earlier["size"] > CONTEXT_BYTES:
            break
        total += earlier["size"]
        first = earlier["seq"]
    conn.execute(
        "INSERT INTO learning_jobs "
        "(id,batch_id,project,root,session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (new_job_id, batch_id, project, root, session, now, now),
    )
    conn.execute(
        "INSERT INTO learning_job_ranges VALUES (?,?,?,?,?,?,?)",
        (new_job_id, project, root, session, client, first, high),
    )
    return new_job_id


def ranged_sources(conn, job_id: str) -> list[Any]:
    """Eligible sources in the job's range, oldest first."""
    return conn.execute(
        "SELECT s.* FROM learning_job_ranges r JOIN learning_sources s ON " + _IN_RANGE + " "
        "WHERE r.job_id=? AND s.status='eligible' ORDER BY s.seq",
        (job_id,),
    ).fetchall()


def in_job_range(conn, job_id: str, source_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM learning_job_ranges r JOIN learning_sources s ON " + _IN_RANGE + " "
            "WHERE r.job_id=? AND s.id=? LIMIT 1",
            (job_id, source_id),
        ).fetchone()
        is not None
    )


def jobs_with_source(conn, source_id: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT r.job_id FROM learning_job_ranges r JOIN learning_sources s ON "
            + _IN_RANGE
            + " WHERE s.id=?",
            (source_id,),
        )
    }


def prune(
    conn,
    *,
    pending_days: float = DEFAULT_PENDING_DAYS,
    retention_days: float = DEFAULT_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """Drop work and data the learner no longer needs. The caller holds the write lock.

    - A pending extract job older than ``pending_days`` is cancelled.
    - A cancelled or exhausted extract job older than ``retention_days`` loses its source
      links, its budget rows and its extraction pages. The job row goes too unless a
      decision names it.
    - An eligible source older than ``retention_days`` goes when no note, claim, draft or
      live job uses it.
    """
    now = time.time() if now is None else now
    late, old = now - pending_days * 86400, now - retention_days * 86400
    counts = dict.fromkeys(
        ("pending_cancelled", "links", "jobs", "budget_rows", "sources", "source_bytes"), 0
    )
    counts["pending_cancelled"] = conn.execute(
        "UPDATE learning_jobs SET status='cancelled',last_error='pruned',lease_token=NULL,"
        "lease_owner=NULL,lease_until=NULL,updated_at=? "
        "WHERE kind='extract' AND status='pending' AND created_at<?",
        (now, late),
    ).rowcount
    dead = (
        "SELECT id FROM learning_jobs WHERE kind='extract' "
        "AND status IN ('cancelled','exhausted') AND updated_at<?"
    )
    counts["links"] = (
        conn.execute(
            "DELETE FROM learning_job_sources WHERE job_id IN (" + dead + ")", (old,)
        ).rowcount
        + conn.execute(
            "DELETE FROM learning_job_ranges WHERE job_id IN (" + dead + ")", (old,)
        ).rowcount
    )
    conn.execute("DELETE FROM learning_extraction_pages WHERE job_id IN (" + dead + ")", (old,))
    counts["budget_rows"] = conn.execute(
        "DELETE FROM learning_budget WHERE created_at<? AND job_id IN (" + dead + ")",
        (old, old),
    ).rowcount
    counts["jobs"] = conn.execute(
        "DELETE FROM learning_jobs WHERE id IN (" + dead + ") "
        "AND NOT EXISTS (SELECT 1 FROM learning_decisions d WHERE d.job_id=learning_jobs.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_budget b WHERE b.job_id=learning_jobs.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_claims c WHERE c.job_id=learning_jobs.id)",
        (old,),
    ).rowcount
    unused = (
        "FROM learning_sources s WHERE s.status='eligible' AND s.created_at<? "
        "AND NOT EXISTS (SELECT 1 FROM learning_evidence e WHERE e.source_id=s.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_job_sources l WHERE l.source_id=s.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_claim_supports c WHERE c.source_id=s.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_draft_supports d WHERE d.source_id=s.id) "
        "AND NOT EXISTS (SELECT 1 FROM learning_job_ranges r JOIN learning_jobs j "
        "ON j.id=r.job_id WHERE j.status IN ('pending','leased') AND " + _IN_RANGE + ")"
    )
    counts["source_bytes"] = conn.execute(
        "SELECT coalesce(sum(length(s.content)),0) " + unused, (old,)
    ).fetchone()[0]
    counts["sources"] = conn.execute(
        "DELETE FROM learning_sources WHERE id IN (SELECT s.id " + unused + ")", (old,)
    ).rowcount
    return counts
