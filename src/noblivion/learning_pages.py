# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotent extraction receipts, separate from a job's small resume cursor.

A saturated response never establishes complete coverage. A following bounded
continuation must explicitly return fewer than twelve remaining candidates.
Receipts preserve every accepted candidate until one atomic ledger admission.
"""

from __future__ import annotations

import json
import time

from . import db
from .learning_extract import ValidationError, contains_secret


class CoverageExhausted(ValidationError):
    """Coverage cannot complete inside the explicitly configured job bounds."""


def erase_jobs(conn, job_ids):
    """Erase quoted candidate payloads synchronously in the caller's transaction."""
    for job_id in set(job_ids):
        conn.execute("DELETE FROM learning_extraction_pages WHERE job_id=?", (job_id,))


def coverage(conn, job_id=None):
    where, args = (" WHERE job_id=?", (job_id,)) if job_id is not None else ("", ())
    return [
        dict(row)
        for row in conn.execute(
            "SELECT job_id,unit_index,continuation,status,reason FROM learning_extraction_pages"
            + where
            + " ORDER BY job_id,unit_index,continuation",
            args,
        )
    ]


class ExtractionPages:
    def __init__(self, ledger, job, state, fingerprint):
        self.ledger, self.job, self.state = ledger, job, state
        self.fingerprint = fingerprint
        # The worker accepts ledger test doubles through the same checkpoint API.
        # Production receipts always live in their own durable SQLite table.
        self.conn = getattr(ledger, "conn", None)

    def _rows(self):
        if self.conn is None:
            return self.state.setdefault("extraction_pages", [])
        rows = self.conn.execute(
            "SELECT * FROM learning_extraction_pages WHERE job_id=? "
            "ORDER BY unit_index,continuation",
            (self.job["id"],),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["candidates"] = json.loads(item["candidates"])
            result.append(item)
        return result

    def receipt(self, unit, continuation):
        for row in self._rows():
            if row["fingerprint"] != self.fingerprint:
                raise ValidationError("extraction receipt configuration changed")
            if row["unit_index"] == unit and row["continuation"] == continuation:
                return row
        return None

    def decisions(self):
        return [candidate for row in self._rows() for candidate in row["candidates"]]

    def count(self):
        return len(self._rows())

    def save(self, unit, continuation, window, candidates, status, reason):
        serialized = json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > 262144 or contains_secret(serialized):
            raise ValidationError("unsafe extraction receipt")
        prior = self.receipt(unit, continuation)
        if prior is not None:
            return prior
        spans = [
            {
                "source_id": part["id"],
                "start": part["offset"],
                "end": part["offset"] + len(part["content"]),
            }
            for part in window
        ]
        row = dict(
            unit_index=unit,
            continuation=continuation,
            fingerprint=self.fingerprint,
            spans=spans,
            candidates=candidates,
            status=status,
            reason=reason,
        )
        if self.conn is None:
            self.state.setdefault("extraction_pages", []).append(row)
            self.ledger.checkpoint(self.job["id"], self.job["lease_token"], self.state)
        else:
            with db.write_tx(self.conn):
                self.ledger._lease(self.job["id"], self.job["lease_token"])
                self.conn.execute(
                    "INSERT INTO learning_extraction_pages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        self.job["id"],
                        unit,
                        continuation,
                        self.fingerprint,
                        json.dumps(spans, separators=(",", ":")),
                        serialized,
                        status,
                        reason,
                        time.time(),
                    ),
                )
        return row


def retry_rejected(conn, job_id):
    """Explicit retry can repair a rejected page without losing prior receipts."""
    conn.execute(
        "DELETE FROM learning_extraction_pages WHERE job_id=? AND status='rejected'", (job_id,)
    )
