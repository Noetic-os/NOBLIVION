# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only size report of the learning queue, as input for batch learning (NOBLIVION-265).

A capture job reads a range of per-session sequence numbers. A new job also takes the
newest earlier sources of its session as context (``learning_ranges.attach_job``), so
the same text can go through extraction more than once. This report counts, per job
status, the characters in the job ranges and the part that an earlier job of the same
session already covered. It also splits the pending jobs into those that hold stored
work and those that do not. It makes no model call and writes nothing.

Sizes are characters of eligible source text (``length(content)``), not bytes. Prune
deletes sources of finished jobs, so the figures for done jobs can be low.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

_FRESH_PROGRESS = {"next_window": 0, "decisions": []}


def _holds_work(progress: str, pages: int) -> bool:
    """True when the job stored extraction pages, a window position or decisions."""
    if pages:
        return True
    try:
        state = json.loads(progress)
    except ValueError:
        return True
    if not isinstance(state, dict):
        return True
    return bool(state.get("next_window")) or bool(state.get("decisions"))


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def batch_report(
    conn: sqlite3.Connection, project: str | None = None, root: str | None = None
) -> dict[str, Any]:
    """Count sessions, range characters, re-read overlap and started jobs."""
    where, params = "j.kind='extract'", []
    if project is not None:
        where += " AND j.project=?"
        params.append(project)
    if root is not None:
        where += " AND j.root=?"
        params.append(root)
    jobs = conn.execute(
        "SELECT j.id,j.status,j.attempts,j.progress,j.created_at,r.project,r.root,"
        "r.session_id,r.source_client,r.first_seq,r.last_seq FROM learning_jobs j "
        "JOIN learning_job_ranges r ON r.job_id=j.id WHERE " + where,
        params,
    ).fetchall()
    unranged = conn.execute(
        "SELECT count(*) FROM learning_jobs j WHERE " + where + " AND j.status='pending' "
        "AND NOT EXISTS (SELECT 1 FROM learning_job_ranges r WHERE r.job_id=j.id)",
        params,
    ).fetchone()[0]

    sessions: dict[tuple, list] = defaultdict(list)
    for job in jobs:
        key = (job["project"], job["root"], job["session_id"], job["source_client"])
        sessions[key].append(job)

    by_status: dict[str, dict[str, int]] = defaultdict(
        lambda: {"jobs": 0, "chars": 0, "overlap_chars": 0}
    )
    pending_sessions = 0
    pending_union = 0
    pending_union_not_done = 0
    for key, session_jobs in sessions.items():
        sizes = dict(
            conn.execute(
                "SELECT seq,length(content) FROM learning_sources WHERE project=? AND root=? "
                "AND session_id=? AND source_client=? AND status='eligible' AND seq>0",
                key,
            ).fetchall()
        )
        session_jobs.sort(key=lambda job: (job["created_at"], job["id"]))
        covered: set[int] = set()
        done: set[int] = set()
        pending: set[int] = set()
        for job in session_jobs:
            seqs = [s for s in range(job["first_seq"], job["last_seq"] + 1) if s in sizes]
            row = by_status[job["status"]]
            row["jobs"] += 1
            row["chars"] += sum(sizes[s] for s in seqs)
            row["overlap_chars"] += sum(sizes[s] for s in seqs if s in covered)
            if job["status"] == "pending":
                pending.update(seqs)
            elif job["status"] == "done":
                done.update(seqs)
            if job["status"] != "cancelled":
                covered.update(seqs)
        if pending:
            pending_sessions += 1
            pending_union += sum(sizes[s] for s in pending)
            pending_union_not_done += sum(sizes[s] for s in pending - done)

    started = {"holds_work": 0, "charged_no_work": 0, "unstarted": 0}
    for job in jobs:
        if job["status"] != "pending":
            continue
        pages = conn.execute(
            "SELECT count(*) FROM learning_extraction_pages WHERE job_id=?", (job["id"],)
        ).fetchone()[0]
        charged = conn.execute(
            "SELECT 1 FROM learning_budget WHERE job_id=? LIMIT 1", (job["id"],)
        ).fetchone()
        if _holds_work(job["progress"], pages):
            started["holds_work"] += 1
        elif charged or job["attempts"] or json.loads(job["progress"]) not in ({}, _FRESH_PROGRESS):
            started["charged_no_work"] += 1
        else:
            started["unstarted"] += 1

    other = conn.execute(
        "SELECT j.kind,j.status,count(*) FROM learning_jobs j WHERE "
        + where.replace("j.kind='extract'", "j.kind!='extract'")
        + " GROUP BY j.kind,j.status ORDER BY j.kind,j.status",
        params,
    ).fetchall()

    statuses = {}
    for status, row in sorted(by_status.items()):
        statuses[status] = dict(row, overlap_share=_share(row["overlap_chars"], row["chars"]))
    pending_row = by_status.get("pending", {"jobs": 0, "chars": 0, "overlap_chars": 0})
    return {
        "unit": "characters of eligible source text",
        "extract_jobs": statuses,
        "pending": {
            "jobs": pending_row["jobs"],
            "sessions": pending_sessions,
            "chars": pending_row["chars"],
            "overlap_chars": pending_row["overlap_chars"],
            "overlap_share": _share(pending_row["overlap_chars"], pending_row["chars"]),
            "distinct_chars": pending_union,
            "distinct_chars_not_done": pending_union_not_done,
            "started": started,
            "unranged_jobs": unranged,
        },
        "other_jobs": [{"kind": kind, "status": status, "jobs": n} for kind, status, n in other],
        "note": "prune deletes sources of finished jobs, so done and exhausted counts can be low",
    }
