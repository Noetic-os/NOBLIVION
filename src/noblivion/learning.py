# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactional source ledger, reconciliation and recoverable file projections.

The caller owns its connection (one per thread). Provider calls never run in a
transaction. Leases fence late workers; budget reservations survive process loss.
Every content mutation invalidates the old recall row before committing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noblivion import db

CATEGORIES = frozenset({"preference", "workflow", "fact", "decision", "proven_fix"})
MAX_RECORDS = 256
MAX_CONTENT = 65536
MAX_STATEMENT = 12000


class LearningError(ValueError):
    """Invalid input or a refused learning operation."""


class LeaseLost(LearningError):
    """The worker no longer owns a live lease."""


class SemanticSnapshotChanged(LearningError):
    """Managed memories changed after semantic comparison; retry the comparison."""


class BudgetExceeded(LearningError):
    """No call may start under the configured persistent limits."""


class RevisionConflict(LearningError):
    """The note changed since the caller read it."""


@dataclass(frozen=True)
class BudgetLimits:
    window_seconds: float = 86400.0
    max_calls: int = 20
    max_tokens: int = 100000
    max_seconds: float = 300.0
    max_cost: float = 0.0
    max_concurrency: int = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _id() -> str:
    return str(uuid.uuid4())


def _text(value: Any, name: str, maximum: int = 512, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not value.strip() and not empty):
        raise LearningError(f"invalid {name}")
    if "\x00" in value:
        raise LearningError(f"invalid {name}")
    return value


def _safe(text: str) -> None:
    # Keep the validator authoritative at every writer, including direct CLI edits.
    from noblivion.learning_extract import contains_secret

    if contains_secret(text):
        raise LearningError("unsafe source or memory content")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetExceeded(f"missing or invalid {name} accounting")
    if not math.isfinite(value) or value < 0:
        raise BudgetExceeded(f"invalid {name} accounting")
    return float(value)


RULE_MAX_CHARS = 160
DESCRIPTION_MAX_CHARS = 200


def _first_sentence(text: str) -> str:
    flat = " ".join(str(text).split())
    match = re.search(r"[.!?](?=\s|$)", flat)
    return flat[: match.end()] if match else flat


def _plain_line(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    """One plain front matter line: no newline, no ': ' or ' #', no leading YAML mark."""
    flat = "".join(ch for ch in " ".join(str(text).split()) if ch.isprintable())
    flat = flat.replace(": ", " - ").replace(" #", " ").strip()
    flat = flat.lstrip("\"'[]{}&*!|>%@`?-, ").rstrip(":")
    if len(flat) > limit:
        cut = flat[:limit]
        flat = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:-")
    return flat or "learned note"


class LearningLedger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def effective_controls(self, project: str, root: str, session_id: str = "") -> dict[str, bool]:
        """Session controls can restrict, but cannot override a root opt-out."""
        rows = self.conn.execute(
            "SELECT use_memory,contribute,paused FROM learning_controls "
            "WHERE project=? AND root=? AND session_id IN ('',?)",
            (project, root, session_id),
        ).fetchall()
        return {
            "use": all(row["use_memory"] != 0 for row in rows),
            "contribute": all(row["contribute"] != 0 for row in rows),
            "paused": any(row["paused"] == 1 for row in rows),
        }

    def read_allowed(self, project: str, root: str, session_id: str = "") -> bool:
        """Unknown clients cannot bypass any session's explicit memory-use opt-out.

        Session IDs are client-supplied routing metadata, not authenticated identity.
        Contribution and worker reconciliation continue using effective_controls.
        """
        if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", session_id):
            return self.effective_controls(project, root, session_id)["use"]
        return (
            self.conn.execute(
                "SELECT 1 FROM learning_controls WHERE project=? AND root=? "
                "AND use_memory=0 LIMIT 1",
                (project, root),
            ).fetchone()
            is None
        )

    def set_controls(
        self,
        project: str,
        root: str,
        session_id: str = "",
        *,
        use: bool | None = None,
        contribute: bool | None = None,
        paused: bool | None = None,
    ) -> dict[str, bool]:
        _text(project, "project")
        _text(root, "root")
        _text(session_id, "session_id", empty=True)
        for value in (use, contribute, paused):
            if value is not None and type(value) is not bool:
                raise LearningError("controls must be booleans")
        with db.write_tx(self.conn):
            self.conn.execute(
                "INSERT INTO learning_controls "
                "(project,root,session_id,use_memory,contribute,paused,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(project,root,session_id) DO UPDATE SET "
                "use_memory=coalesce(excluded.use_memory,use_memory),"
                "contribute=coalesce(excluded.contribute,contribute),"
                "paused=coalesce(excluded.paused,paused),updated_at=excluded.updated_at",
                (project, root, session_id, use, contribute, paused, time.time()),
            )
            # Fence a request already running when the operator pauses contribution.
            if contribute is False or paused is True:
                params: list[Any] = [time.time(), project, root]
                session_clause = ""
                if session_id:
                    session_clause = " AND session_id=?"
                    params.append(session_id)
                self.conn.execute(
                    "UPDATE learning_jobs SET status='pending',lease_token=NULL,lease_until=NULL,"
                    "lease_owner=NULL,updated_at=? WHERE project=? AND root=? "
                    "AND status='leased'" + session_clause,
                    params,
                )
                self._expire_reservations()
            from .learning_consolidation import withdraw

            if use is False:
                if session_id:
                    self.conn.execute(
                        "DELETE FROM learning_context_cache WHERE project=? AND session_id=?",
                        (project, session_id),
                    )
                else:
                    owned_notes = [
                        row[0]
                        for row in self.conn.execute(
                            "SELECT note_id FROM learning_notes WHERE project=? AND root=?",
                            (project, root),
                        )
                    ]
                    withdraw(self, project=project, root=root, note_ids=owned_notes)
            db.bump_rev(self.conn)
        return self.effective_controls(project, root, session_id)

    def capture_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Commit immutable sources, one extraction job and its cursor together.

        A repeated receipt is safe after an acknowledgement is lost. Reusing an ID
        with different bytes is an error. Disabled sessions receive a receipt but
        their source text is never retained.
        """
        if not isinstance(payload, dict):
            raise LearningError("capture must be an object")
        batch_id = _text(payload.get("batch_id"), "batch_id")
        project = _text(payload.get("namespace", payload.get("project")), "project")
        root = _text(payload.get("root"), "root")
        session = _text(payload.get("session_id"), "session_id")
        client = _text(payload.get("source_client"), "source_client", 64)
        records = payload.get("records")
        if not isinstance(records, list) or not records or len(records) > MAX_RECORDS:
            raise LearningError("invalid capture records")
        cursor = payload.get("cursor", {})
        if not isinstance(cursor, dict):
            raise LearningError("invalid cursor")
        path_id = _text(cursor.get("path_id", batch_id), "cursor path_id")
        offset = cursor.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise LearningError("invalid cursor offset")
        serialized = _json(payload)
        if len(serialized) > 4 * 1024 * 1024:
            raise LearningError("capture batch too large")
        digest = _hash(serialized)
        now = time.time()
        with db.write_tx(self.conn):
            receipt = self.conn.execute(
                "SELECT * FROM learning_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if receipt:
                if receipt["payload_hash"] != digest:
                    raise LearningError("batch identity collision")
                job = self.conn.execute(
                    "SELECT id FROM learning_jobs WHERE batch_id=?", (batch_id,)
                ).fetchone()
                return {
                    "batch_id": batch_id,
                    "accepted": True,
                    "duplicate": True,
                    "skipped": bool(receipt["skipped"]),
                    "job_id": job[0] if job else None,
                }
            controls = self.effective_controls(project, root, session)
            skipped = not controls["contribute"] or controls["paused"]
            self.conn.execute(
                "INSERT INTO learning_batches VALUES (?,?,?,?,?,?,?)",
                (batch_id, digest, project, root, session, int(skipped), now),
            )
            source_ids = []
            new_sources = 0
            if not skipped:
                _safe(serialized)
                for record in records:
                    if not isinstance(record, dict):
                        raise LearningError("source must be an object")
                    source_id = _text(record.get("source_id"), "source_id")
                    content = _text(record.get("content"), "source content", MAX_CONTENT)
                    sha256 = _hash(content)
                    if record.get("source_hash") != sha256:
                        raise LearningError("source hash mismatch")
                    role = record.get("role")
                    if role not in {"user", "assistant", "tool", "system"}:
                        raise LearningError("invalid source role")
                    observed_at = _text(record.get("observed_at"), "observed_at", 64)
                    metadata = record.get("metadata", {})
                    if not isinstance(metadata, dict):
                        raise LearningError("invalid source metadata")
                    metadata = {
                        **metadata,
                        **{
                            key: value
                            for key, value in record.items()
                            if key
                            not in {
                                "source_id",
                                "content",
                                "source_hash",
                                "role",
                                "observed_at",
                                "metadata",
                            }
                        },
                    }
                    metadata_json = _json(metadata)
                    revoked = self.conn.execute(
                        "SELECT 1 FROM learning_sources WHERE project=? AND root=? AND sha256=? "
                        "AND status IN ('excluded','forgotten') LIMIT 1",
                        (project, root, sha256),
                    ).fetchone()
                    if revoked:
                        continue
                    previous = self.conn.execute(
                        "SELECT * FROM learning_sources WHERE id=?", (source_id,)
                    ).fetchone()
                    if previous:
                        if (
                            previous["project"],
                            previous["root"],
                            previous["session_id"],
                            previous["source_client"],
                            previous["sha256"],
                        ) != (project, root, session, client, sha256):
                            raise LearningError("source identity collision")
                        if previous["status"] != "eligible":
                            continue
                        if (previous["content"], previous["role"], previous["metadata"]) != (
                            content,
                            role,
                            metadata_json,
                        ):
                            raise LearningError("source identity collision")
                    else:
                        new_sources += 1
                        self.conn.execute(
                            "INSERT INTO learning_sources "
                            "(id,project,root,session_id,source_client,role,content,sha256,"
                            "observed_at,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                source_id,
                                project,
                                root,
                                session,
                                client,
                                role,
                                content,
                                sha256,
                                observed_at,
                                metadata_json,
                                now,
                            ),
                        )
                    source_ids.append(source_id)
            job_id = None
            if source_ids and new_sources:
                # Conversation evidence often crosses hook batches. Attach earlier
                # sources to the same immutable job, without dropping any new record.
                prior = self.conn.execute(
                    "SELECT id,length(content) size FROM learning_sources WHERE project=? "
                    "AND root=? "
                    "AND session_id=? AND source_client=? AND status='eligible' "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 64",
                    (project, root, session, client),
                ).fetchall()
                total = sum(len(record["content"]) for record in records)
                context = []
                for previous in prior:
                    if previous["id"] not in source_ids and total + previous["size"] <= 120 * 1024:
                        context.append(previous["id"])
                        total += previous["size"]
                source_ids = list(reversed(context)) + source_ids
                job_id = _id()
                self.conn.execute(
                    "INSERT INTO learning_jobs "
                    "(id,batch_id,project,root,session_id,created_at,updated_at) VALUES "
                    "(?,?,?,?,?,?,?)",
                    (job_id, batch_id, project, root, session, now, now),
                )
                for ordinal, source_id in enumerate(dict.fromkeys(source_ids)):
                    self.conn.execute(
                        "INSERT INTO learning_job_sources VALUES (?,?,?)",
                        (job_id, source_id, ordinal),
                    )
            self.conn.execute(
                "INSERT INTO learning_cursors VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(project,root,client,session_id,path_id) DO UPDATE SET "
                "offset=max(offset,excluded.offset),updated_at=excluded.updated_at",
                (project, root, client, session, path_id, offset, now),
            )
            return {
                "batch_id": batch_id,
                "accepted": True,
                "duplicate": False,
                "skipped": skipped,
                "job_id": job_id,
            }

    def enqueue_derivation(
        self, project: str, root: str, session_id: str = "", *, kind: str = "consolidate"
    ) -> str | None:
        """An internal immutable receipt reuses jobs/leases/budgets; no new provider path."""
        _text(project, "project")
        _text(root, "root")
        _text(session_id, "session_id", empty=True)
        if kind not in {"consolidate", "draft"}:
            raise LearningError("invalid derivation kind")
        with db.write_tx(self.conn):
            return self._enqueue_derivation(project, root, session_id, kind=kind)

    def _enqueue_derivation(self, project, root, session_id, *, kind="consolidate"):
        """Internal form runs inside an existing capture/admission transaction."""
        from .learning_consolidation import ConsolidationSettings

        controls = self.effective_controls(project, root, session_id)
        if not controls["contribute"] or controls["paused"]:
            return None
        notes = self.current_notes(project, root)
        if not notes:
            return None
        snapshot = sorted([[n["note_id"], n["revision"]] for n in notes])
        controls_snapshot = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT session_id,use_memory,contribute,paused,updated_at "
                "FROM learning_controls "
                "WHERE project=? AND root=? AND session_id IN ('',?) ORDER BY session_id",
                (project, root, session_id),
            )
        ]
        digest = _hash(
            _json(
                [
                    project,
                    root,
                    session_id,
                    kind,
                    snapshot,
                    controls_snapshot,
                    ConsolidationSettings().fingerprint,
                ]
            )
        )
        receipt = "derive:" + digest
        existing = self.conn.execute(
            "SELECT id FROM learning_jobs WHERE batch_id=?", (receipt,)
        ).fetchone()
        if existing:
            return existing[0]
        ident, now = _id(), time.time()
        self.conn.execute(
            "INSERT INTO learning_batches VALUES (?,?,?,?,?,0,?)",
            (receipt, digest, project, root, session_id, now),
        )
        self.conn.execute(
            "INSERT INTO learning_jobs "
            "(id,batch_id,project,root,session_id,kind,progress,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?, ?,?)",
            (ident, receipt, project, root, session_id, kind, "{}", now, now),
        )
        # Explicit source ancestry is used for erasing durable provider checkpoints.
        # It never substitutes for the exact per-claim support edges.
        sources = sorted(
            {
                r[0]
                for r in self.conn.execute(
                    "SELECT e.source_id FROM learning_evidence e JOIN learning_notes n "
                    "ON n.note_id=e.note_id "
                    "AND n.revision=e.revision WHERE n.project=? AND n.root=? AND "
                    "n.status='active'",
                    (project, root),
                )
            }
        )
        if kind == "draft":
            sources = sorted(
                set(sources)
                | {
                    r[0]
                    for r in self.conn.execute(
                        "SELECT id FROM learning_sources WHERE project=? AND root=? "
                        "AND role='assistant' AND status='eligible'",
                        (project, root),
                    )
                }
            )
        for ordinal, source in enumerate(sources):
            self.conn.execute(
                "INSERT INTO learning_job_sources VALUES (?,?,?)", (ident, source, ordinal)
            )
        return ident

    def _expire_reservations(self) -> None:
        self.conn.execute(
            "UPDATE learning_budget SET status='uncertain',updated_at=? WHERE status='reserved' "
            "AND NOT EXISTS (SELECT 1 FROM learning_jobs j WHERE j.id=learning_budget.job_id "
            "AND j.lease_token=learning_budget.lease_token AND j.status='leased' "
            "AND j.lease_until>?)",
            (time.time(), time.time()),
        )

    def claim(
        self, worker_id: str, lease_seconds: float = 120, max_attempts: int = 4
    ) -> dict[str, Any] | None:
        _text(worker_id, "worker_id")
        if lease_seconds <= 0 or max_attempts < 1:
            raise LearningError("invalid lease limits")
        now = time.time()
        with db.write_tx(self.conn):
            self.conn.execute(
                "UPDATE learning_jobs SET status=CASE WHEN attempts>=? THEN 'exhausted' "
                "ELSE 'pending' END,lease_token=NULL,lease_owner=NULL,lease_until=NULL,"
                "last_error='lease expired',updated_at=? WHERE status='leased' AND lease_until<=?",
                (max_attempts, now, now),
            )
            self._expire_reservations()
            self.conn.execute(
                "UPDATE learning_jobs SET status='exhausted',updated_at=? "
                "WHERE status='pending' AND attempts>=?",
                (now, max_attempts),
            )
            rows = self.conn.execute(
                "SELECT * FROM learning_jobs WHERE status='pending' AND next_attempt<=? "
                "AND attempts<? ORDER BY created_at,id",
                (now, max_attempts),
            ).fetchall()
            for row in rows:
                controls = self.effective_controls(row["project"], row["root"], row["session_id"])
                if not controls["contribute"] or controls["paused"]:
                    continue
                sources = self._job_sources(row["id"])
                if not sources and row["kind"] == "extract":
                    self.conn.execute(
                        "UPDATE learning_jobs SET status='cancelled',updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    continue
                token = _id()
                self.conn.execute(
                    "UPDATE learning_jobs SET status='leased',attempts=attempts+1,lease_token=?,"
                    "lease_owner=?,lease_until=?,updated_at=? WHERE id=?",
                    (token, worker_id, now + lease_seconds, now, row["id"]),
                )
                result = dict(row)
                result["progress"] = json.loads(row["progress"])
                result.update(
                    lease_token=token,
                    lease_owner=worker_id,
                    lease_until=now + lease_seconds,
                    attempts=row["attempts"] + 1,
                    status="leased",
                    sources=sources,
                )
                return result
        return None

    def _job_sources(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.* FROM learning_sources s JOIN learning_job_sources j ON j.source_id=s.id "
            "WHERE j.job_id=? AND s.status='eligible' ORDER BY j.ordinal",
            (job_id,),
        ).fetchall()
        result = []
        for row in rows:
            source = dict(row)
            source["metadata"] = json.loads(source["metadata"])
            source["tool_name"] = source["metadata"].get("tool_name", "")
            source["scope"] = {"project": source["project"], "root": source["root"]}
            result.append(source)
        return result

    def _lease(self, job_id: str, lease_token: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM learning_jobs WHERE id=?", (job_id,)).fetchone()
        if (
            not row
            or row["status"] != "leased"
            or row["lease_token"] != lease_token
            or row["lease_until"] <= time.time()
        ):
            raise LeaseLost("learning lease is no longer current")
        controls = self.effective_controls(row["project"], row["root"], row["session_id"])
        if not controls["contribute"] or controls["paused"]:
            raise LeaseLost("learning contribution is paused or disabled")
        return row

    def reserve(
        self,
        job_id: str,
        lease_token: str,
        *,
        limits: BudgetLimits,
        estimated_tokens: int,
        estimated_seconds: float,
        estimated_cost: float,
    ) -> dict[str, Any]:
        tokens = _number(estimated_tokens, "token")
        seconds = _number(estimated_seconds, "time")
        cost = _number(estimated_cost, "cost")
        if tokens != int(tokens):
            raise BudgetExceeded("token accounting must be integral")
        for name in (
            "window_seconds",
            "max_calls",
            "max_tokens",
            "max_seconds",
            "max_cost",
            "max_concurrency",
        ):
            _number(getattr(limits, name), name)
        if limits.window_seconds <= 0:
            raise BudgetExceeded("invalid budget window")
        now = time.time()
        with db.write_tx(self.conn):
            self._lease(job_id, lease_token)
            self._expire_reservations()
            used = self.conn.execute(
                "SELECT count(*) calls,coalesce(sum(tokens),0) tokens,"
                "coalesce(sum(seconds),0) seconds,coalesce(sum(cost),0) cost "
                "FROM learning_budget WHERE created_at>=?",
                (now - limits.window_seconds,),
            ).fetchone()
            active = self.conn.execute(
                "SELECT count(*) FROM learning_budget WHERE status='reserved'"
            ).fetchone()[0]
            if (
                used["calls"] + 1 > limits.max_calls
                or used["tokens"] + tokens > limits.max_tokens
                or used["seconds"] + seconds > limits.max_seconds
                or used["cost"] + cost > limits.max_cost
                or active >= limits.max_concurrency
            ):
                raise BudgetExceeded("persistent learning budget exhausted")
            reservation = {"id": _id(), "tokens": int(tokens), "seconds": seconds, "cost": cost}
            self.conn.execute(
                "INSERT INTO learning_budget VALUES (?,?,?,?,?,?,'reserved',?,?)",
                (reservation["id"], job_id, lease_token, int(tokens), seconds, cost, now, now),
            )
            self.conn.execute(
                "UPDATE learning_jobs SET lease_until=max(lease_until,?),updated_at=? WHERE id=?",
                (now + seconds + 30, now, job_id),
            )
            return reservation

    def settle(
        self,
        reservation_id: str,
        *,
        actual_tokens: int | None = None,
        actual_seconds: float | None = None,
        actual_cost: float | None = None,
    ) -> None:
        """Never refund an uncertain reservation; count overruns when measured."""
        values = [
            None if v is None else _number(v, name)
            for v, name in (
                (actual_tokens, "token"),
                (actual_seconds, "time"),
                (actual_cost, "cost"),
            )
        ]
        if values[0] is not None and int(values[0]) != values[0]:
            raise BudgetExceeded("token accounting must be integral")
        with db.write_tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM learning_budget WHERE id=?", (reservation_id,)
            ).fetchone()
            if not row:
                raise LearningError("unknown reservation")
            self.conn.execute(
                "UPDATE learning_budget SET tokens=?,seconds=?,cost=?,status='settled',"
                "updated_at=? WHERE id=?",
                (
                    max(row["tokens"], int(values[0] or 0)),
                    max(row["seconds"], values[1] or 0),
                    max(row["cost"], values[2] or 0),
                    time.time(),
                    reservation_id,
                ),
            )

    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        retry_delay: float = 30,
        max_attempts: int = 4,
        permanent: bool = False,
    ) -> None:
        # Exceptions may contain provider response text. Persist a bounded, sanitized
        # diagnostic only; never preserve a rejected model response or source here.
        from noblivion.redaction import redact_at_rest

        diagnostic = redact_at_rest(str(error))[:500]
        try:
            _safe(diagnostic)
        except LearningError:
            diagnostic = "learning validation failed"
        with db.write_tx(self.conn):
            job = self._lease(job_id, lease_token)
            exhausted = permanent or job["attempts"] >= max_attempts
            self.conn.execute(
                "UPDATE learning_jobs SET status=?,next_attempt=?,lease_token=NULL,"
                "lease_owner=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                (
                    "exhausted" if exhausted else "pending",
                    time.time() + max(0, retry_delay),
                    diagnostic,
                    time.time(),
                    job_id,
                ),
            )
            self._expire_reservations()

    def checkpoint(self, job_id: str, lease_token: str, progress: dict[str, Any]) -> None:
        if not isinstance(progress, dict):
            raise LearningError("invalid job progress")
        serialized = _json(progress)
        if len(serialized) > 1024 * 1024:
            raise LearningError("job progress too large")
        _safe(serialized)
        with db.write_tx(self.conn):
            self._lease(job_id, lease_token)
            self.conn.execute(
                "UPDATE learning_jobs SET progress=?,updated_at=? WHERE id=?",
                (serialized, time.time(), job_id),
            )

    def defer(
        self,
        job_id: str,
        lease_token: str,
        *,
        retry_delay: float = 3600,
        error: str = "budget unavailable",
    ) -> None:
        """Budget denial did not make a provider call and must not exhaust retries."""
        _safe(error)
        with db.write_tx(self.conn):
            self._lease(job_id, lease_token)
            self.conn.execute(
                "UPDATE learning_jobs SET status='pending',attempts=max(0,attempts-1),"
                "next_attempt=?,"
                "lease_token=NULL,lease_owner=NULL,lease_until=NULL,last_error=?,"
                "updated_at=? WHERE id=?",
                (time.time() + max(0, retry_delay), error[:500], time.time(), job_id),
            )
            self._expire_reservations()

    def retry(self, job_id: str) -> None:
        """Explicit operator recovery; retained budget usage still applies."""
        with db.write_tx(self.conn):
            row = self.conn.execute(
                "SELECT progress FROM learning_jobs WHERE id=? AND status='exhausted'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LearningError("job is not exhausted")
            progress = json.loads(row["progress"])
            # Explicit recovery renews only the bounded format retry. Validated
            # windows, semantic comparisons and durable budget charges remain.
            progress.pop("format_repair", None)
            from .learning_pages import retry_rejected

            retry_rejected(self.conn, job_id)
            self.conn.execute(
                "UPDATE learning_jobs SET status='pending',attempts=0,next_attempt=0,last_error='',"
                "progress=?,updated_at=? WHERE id=? AND status='exhausted'",
                (_json(progress), time.time(), job_id),
            )

    def current_notes(self, project: str, root: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM learning_notes WHERE project=? AND root=? AND status='active' "
            "ORDER BY logical_key,note_id",
            (project, root),
        ).fetchall()
        return [self._note(row) for row in rows]

    def semantic_notes(
        self, project: str, root: str, *, session_id: str = ""
    ) -> list[dict[str, Any]]:
        """Owned notes plus read-only general preferences from enabled source roots."""
        notes = self.current_notes(project, root)
        for row in self.conn.execute(
            "SELECT * FROM learning_notes WHERE project=? AND root<>? AND status='active' "
            "AND scope='general' AND category IN ('preference','workflow') ORDER BY note_id",
            (project, root),
        ):
            if not self.effective_controls(project, row["root"], session_id)["use"]:
                continue
            note = self._note(row)
            note.update(read_only=True, cross_root_general=True)
            notes.append(note)
        return sorted(notes, key=lambda note: note["note_id"])

    def reconciliation_context(
        self, project: str, root: str, query: str = "", limit: int = 12
    ) -> list[dict[str, Any]]:
        """Retrieve compact, scoped legacy and managed candidates before reconciliation.

        Legacy files stay read-only. Relevance is scored over the entire scoped
        corpus before the limit; an older matching note is not hidden by row order.
        """
        if not 1 <= limit <= 50:
            raise LearningError("invalid reconciliation limit")
        stop = {
            "that",
            "this",
            "with",
            "from",
            "have",
            "will",
            "should",
            "would",
            "user",
            "tool",
            "source",
            "assistant",
            "into",
            "then",
            "than",
            "were",
            "when",
        }
        terms = list(
            dict.fromkeys(
                word for word in re.findall(r"[\w-]{4,}", query.lower()) if word not in stop
            )
        )[:24]
        score = "+".join("(instr(lower(content),?)>0)" for _ in terms) or "0"
        rows = self.conn.execute(
            "SELECT m.id,m.path,m.content,m.rev,(" + score + ") relevance FROM memories m "
            "WHERE m.project=? AND m.root=? AND m.archived_at IS NULL AND m.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM learning_notes n WHERE n.project=m.project "
            "AND n.root=m.root AND n.path=m.path) ORDER BY relevance DESC,m.id LIMIT ?",
            [*terms, project, root, limit],
        ).fetchall()
        candidates = []
        for row in rows:
            if terms and row["relevance"] == 0:
                continue
            content = row["content"]
            start = min(
                (content.lower().find(term) for term in terms if term in content.lower()), default=0
            )
            start = max(0, start - 200)
            candidates.append(
                (
                    row["relevance"],
                    {
                        "note_id": f"legacy:{row['id']}",
                        "logical_key": f"legacy:{row['id']}",
                        "revision": row["rev"],
                        "title": row["path"],
                        "statement": content[start : start + 1200],
                        "category": "fact",
                        "scope": "project",
                        "evidence": [],
                        "read_only": True,
                    },
                )
            )
        for note in self.semantic_notes(project, root):
            relevance = sum(term in note["statement"].lower() for term in terms)
            if terms and relevance == 0:
                continue
            compact = {
                key: note[key]
                for key in (
                    "note_id",
                    "logical_key",
                    "revision",
                    "category",
                    "scope",
                    "title",
                    "statement",
                )
            }
            if note.get("cross_root_general"):
                compact.update(read_only=True, cross_root_general=True)
            compact["evidence"] = []
            for reference in note["evidence"]:
                offset = reference["quote"].find(note["statement"])
                if offset >= 0:
                    compact["evidence"].append(
                        {
                            **reference,
                            "quote": note["statement"],
                            "start": reference["start"] + offset,
                            "end": reference["start"] + offset + len(note["statement"]),
                        }
                    )
                    break
            candidates.append((relevance, compact))
        candidates.sort(key=lambda item: (-item[0], item[1]["note_id"]))
        notes, size = [], 0
        for _, note in candidates[:limit]:
            length = len(_json(note))
            if size + length <= 18000:
                notes.append(note)
                size += length
        return notes

    def _note(self, row: sqlite3.Row) -> dict[str, Any]:
        note = dict(row)
        note["evidence"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT source_id,source_hash,start,end,quote FROM learning_evidence "
                "WHERE note_id=? AND revision=? ORDER BY source_id,start",
                (row["note_id"], row["revision"]),
            ).fetchall()
        ]
        return note

    def _conflict_rows(
        self, project: str | None = None, root: str | None = None, *, note_id: str | None = None
    ) -> list[sqlite3.Row]:
        conditions, params = ["d.action='conflict'"], []
        if note_id is not None:
            conditions.append("d.note_id=?")
            params.append(note_id)
        # A proposal belongs to its source scope and, when known, its target owner.
        scopes = []
        for alias in ("j", "n"):
            scope = []
            for column, value in (("project", project), ("root", root)):
                if value is not None:
                    scope.append(f"{alias}.{column}=?")
                    params.append(value)
            if scope:
                scopes.append("(" + " AND ".join(scope) + ")")
        if scopes:
            conditions.append("(" + " OR ".join(scopes) + ")")
        return self.conn.execute(
            "SELECT d.*,j.project AS source_project,j.root AS source_root "
            "FROM learning_decisions d LEFT JOIN learning_jobs j ON j.id=d.job_id "
            "LEFT JOIN learning_notes n ON n.note_id=d.note_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY d.id DESC",
            params,
        ).fetchall()

    def _inspect_conflict(self, row: sqlite3.Row, *, compact: bool = False) -> dict[str, Any]:
        detail = json.loads(row["detail"])
        decision, result = detail.get("decision", {}), detail.get("result", {})
        target_id = row["note_id"] or decision.get("note_id")
        target = self.conn.execute(
            "SELECT note_id,project,root,revision,status FROM learning_notes WHERE note_id=?",
            (target_id,),
        ).fetchone()
        if target is None and isinstance(target_id, str) and target_id.startswith("legacy:"):
            target = self.conn.execute(
                "SELECT id AS note_id,project,root,rev AS revision,"
                "CASE WHEN deleted_at IS NOT NULL "
                "THEN 'forgotten' WHEN archived_at IS NOT NULL THEN 'excluded' ELSE 'active' END "
                "AS status FROM memories WHERE id=?",
                (target_id.removeprefix("legacy:"),),
            ).fetchone()
        expected_revision = decision.get("expected_revision")
        recorded_revision = expected_revision
        if target is not None and not str(target_id).startswith("legacy:"):
            recorded = self.conn.execute(
                "SELECT max(revision) FROM learning_revisions WHERE note_id=? AND created_at<=?",
                (target_id, row["created_at"]),
            ).fetchone()[0]
            if recorded is not None:
                recorded_revision = recorded
        cleared_reason = None
        if target is not None:
            if target["status"] != "active":
                cleared_reason = "target " + target["status"]
            elif recorded_revision is not None and target["revision"] != recorded_revision:
                cleared_reason = "target revision changed"

        # Decisions can also record reconciliation refusals. Verify evidence at read
        # time instead of treating every stored proposal as validated memory text.
        evidence, unavailable = [], False
        references = decision.get("evidence", [])
        if not isinstance(references, list) or len(references) > 64:
            references, unavailable = [], True
        for reference in references:
            if not isinstance(reference, dict):
                unavailable = True
                continue
            source = self.conn.execute(
                "SELECT * FROM learning_sources WHERE id=?", (reference.get("source_id"),)
            ).fetchone()
            start, end, quote = (reference.get(key) for key in ("start", "end", "quote"))
            if (
                source is None
                or source["status"] == "forgotten"
                or source["project"] != row["source_project"]
                or source["root"] != row["source_root"]
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(source["content"])
                or source["content"][start:end] != quote
                or source["sha256"] != reference.get("source_hash")
            ):
                unavailable = True
                continue
            if source["status"] == "excluded":
                cleared_reason = cleared_reason or "source excluded"
            evidence.append(
                {
                    key: reference[key]
                    for key in ("source_id", "source_hash", "start", "end", "quote")
                }
            )
        reason = result.get("reason") or decision.get("reason") or "explicit resolution required"
        statement = decision.get("statement")
        try:
            _safe(_json({"reason": reason, "statement": statement, "evidence": evidence}))
        except LearningError:
            reason, unavailable = "proposal content unavailable", True
        if unavailable:
            statement, evidence = None, []
        value = {
            "conflict_id": f"conflict:{row['id']}",
            "decision_id": row["id"],
            "state": "cleared" if cleared_reason else "open",
            "cleared_reason": cleared_reason,
            "reason": reason,
            "note_id": target_id,
            "target_revision": expected_revision,
            "recorded_target_revision": recorded_revision,
            "current_revision": target["revision"] if target else None,
            "current_status": target["status"] if target else None,
            "target_scope": {"project": target["project"], "root": target["root"]}
            if target
            else None,
            "source_scope": {"project": row["source_project"], "root": row["source_root"]},
            "created_at": row["created_at"],
        }
        if compact:
            value["reason"] = str(reason)[:240]
        else:
            value.update(
                job_id=row["job_id"],
                proposed_statement=statement,
                evidence=evidence,
                evidence_unavailable=unavailable,
                semantic_target_id=decision.get("semantic_review", {}).get("target_id"),
            )
        return value

    def _conflict_status(self, project: str | None, root: str | None) -> dict[str, Any]:
        pending, count = [], 0
        for row in self._conflict_rows(project, root):
            conflict = self._inspect_conflict(row, compact=True)
            if conflict["state"] == "open":
                count += 1
                if len(pending) < 20:
                    pending.append(conflict)
        return {"open_count": count, "items": pending, "limit": 20}

    def inspect(
        self, note_id: str, *, project: str | None = None, root: str | None = None
    ) -> dict[str, Any]:
        if note_id.startswith("conflict:"):
            if not project or not root:
                raise LearningError("conflict inspection requires project and root")
            # Filter before reading proposal text or evidence. A sequential decision
            # ID is an identifier, not authorization to inspect another scope.
            row = self.conn.execute(
                "SELECT d.*,j.project AS source_project,j.root AS source_root "
                "FROM learning_decisions d LEFT JOIN learning_jobs j ON j.id=d.job_id "
                "LEFT JOIN learning_notes n ON n.note_id=d.note_id "
                "LEFT JOIN memories m ON json_extract(d.detail,'$.decision.note_id')="
                "'legacy:' || m.id WHERE d.id=? AND d.action='conflict' AND "
                "((j.project=? AND j.root=?) OR (n.project=? AND n.root=?) "
                "OR (m.project=? AND m.root=?))",
                (note_id.removeprefix("conflict:"), project, root, project, root, project, root),
            ).fetchone()
            if row is None:
                raise LearningError("unknown conflict")
            return self._inspect_conflict(row)
        row = self.conn.execute(
            "SELECT * FROM learning_notes WHERE note_id=?", (note_id,)
        ).fetchone()
        if not row:
            raise LearningError("unknown note")
        note = self._note(row)
        note["history"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT revision,action,snapshot,created_at FROM learning_revisions "
                "WHERE note_id=? ORDER BY revision",
                (note_id,),
            ).fetchall()
        ]
        for revision in note["history"]:
            revision["snapshot"] = json.loads(revision["snapshot"])
        outbox = self.conn.execute(
            "SELECT * FROM learning_outbox WHERE note_id=?", (note_id,)
        ).fetchone()
        note["projection"] = dict(outbox) if outbox else None
        note["conflicts"] = [
            self._inspect_conflict(conflict, compact=True)
            for conflict in self._conflict_rows(note_id=note_id)
        ]
        return note

    def _evidence(
        self, evidence: Any, project: str, root: str, *, job_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(evidence, list) or not evidence or len(evidence) > 64:
            raise LearningError("memory requires bounded exact source evidence")
        validated = []
        for reference in evidence:
            if not isinstance(reference, dict):
                raise LearningError("invalid evidence")
            source = self.conn.execute(
                "SELECT * FROM learning_sources WHERE id=?", (reference.get("source_id"),)
            ).fetchone()
            if (
                not source
                or source["status"] != "eligible"
                or source["project"] != project
                or source["root"] != root
            ):
                raise LearningError("evidence is unavailable or outside the source scope")
            start, end = reference.get("start"), reference.get("end")
            quote = reference.get("quote")
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(source["content"])
                or source["content"][start:end] != quote
                or reference.get("source_hash") != source["sha256"]
            ):
                raise LearningError("evidence span or hash mismatch")
            # Existing revision references are allowed for an explicit correction;
            # new evidence must still come from this job or a stored note revision.
            if (
                job_id
                and not self.conn.execute(
                    "SELECT 1 FROM learning_job_sources WHERE job_id=? AND source_id=? "
                    "UNION SELECT 1 FROM learning_evidence WHERE source_id=? LIMIT 1",
                    (job_id, source["id"], source["id"]),
                ).fetchone()
            ):
                raise LearningError("evidence is not part of this job or existing memory")
            validated.append(
                {
                    "source_id": source["id"],
                    "source_hash": source["sha256"],
                    "start": start,
                    "end": end,
                    "quote": quote,
                    "role": source["role"],
                }
            )
        return validated

    def _validate_decision(self, decision: dict[str, Any], job: sqlite3.Row) -> dict[str, Any]:
        action = decision.get("action")
        if action not in {"add", "unchanged", "update", "supersede", "conflict", "reject"}:
            raise LearningError("invalid reconciliation action")
        if action == "add":
            progress = json.loads(job["progress"])
            snapshot = decision.get("semantic_snapshot")
            if "semantic" in progress and snapshot is None:
                raise SemanticSnapshotChanged("automatic addition lacks semantic snapshot")
            if snapshot is not None:
                current = sorted(
                    [
                        [note["note_id"], note["revision"]]
                        for note in self.semantic_notes(
                            job["project"], job["root"], session_id=job["session_id"]
                        )
                    ]
                )
                if snapshot != current:
                    raise SemanticSnapshotChanged("managed memory snapshot changed")
        _safe(_json(decision))
        if action in {"reject", "conflict"}:
            return dict(decision)
        target = decision.get("note_id")
        if isinstance(target, str) and target.startswith("legacy:"):
            if action != "unchanged":
                raise LearningError("legacy memory requires an explicit adoption before editing")
            try:
                legacy_id = int(target.split(":", 1)[1])
            except ValueError as exc:
                raise LearningError("invalid legacy target") from exc
            row = self.conn.execute(
                "SELECT 1 FROM memories WHERE id=? AND project=? AND root=? "
                "AND archived_at IS NULL AND deleted_at IS NULL",
                (legacy_id, job["project"], job["root"]),
            ).fetchone()
            if not row:
                raise LearningError("legacy memory is no longer current")
            return dict(decision)
        result = dict(decision)
        for field, maximum in (
            ("logical_key", 256),
            ("title", 300),
            ("statement", MAX_STATEMENT),
            ("observed_at", 64),
        ):
            _text(result.get(field), field, maximum)
        if result.get("category") not in CATEGORIES or result.get("scope") not in {
            "project",
            "general",
        }:
            raise LearningError("invalid memory category or scope")
        if result.get("effective_at") is not None:
            _text(result["effective_at"], "effective_at", 64)
        result["evidence"] = self._evidence(
            result.get("evidence"), job["project"], job["root"], job_id=job["id"]
        )
        statement = result["statement"]
        if not any(statement in ref["quote"] for ref in result["evidence"]):
            raise LearningError("statement must be extractive and directly evidenced")
        category = result["category"]
        if category in {"preference", "workflow", "decision"} and not any(
            ref["role"] == "user" and statement in ref["quote"] for ref in result["evidence"]
        ):
            raise LearningError("user memory requires direct user evidence")
        if category == "fact" and not any(
            ref["role"] in {"user", "tool"} and statement in ref["quote"]
            for ref in result["evidence"]
        ):
            raise LearningError("fact requires user or tool evidence")
        if result["scope"] == "general" and category not in {"preference", "workflow"}:
            raise LearningError("only user preferences and workflows may have general scope")
        if category == "proven_fix":
            proof = result.get("proof")
            if not isinstance(proof, dict):
                raise LearningError("proven fix requires before and after evidence")
            result["proof"] = {}
            for phase in ("before", "after"):
                refs = self._evidence(
                    proof.get(phase), job["project"], job["root"], job_id=job["id"]
                )
                if not any(ref["role"] == "tool" for ref in refs):
                    raise LearningError("fix proof requires observed tool results")
                result["proof"][phase] = refs
        return result

    def finish(
        self,
        job_id: str,
        lease_token: str,
        decisions: list[dict[str, Any]],
        *,
        enqueue_consolidation: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(decisions, list) or len(decisions) > 512:
            raise LearningError("invalid decision count")
        with db.write_tx(self.conn):
            job = self._lease(job_id, lease_token)
            validated = [self._validate_decision(item, job) for item in decisions]
            results = [self._reconcile(item, job) for item in validated]
            self.conn.execute(
                "UPDATE learning_jobs SET status='done',lease_token=NULL,lease_owner=NULL,"
                "lease_until=NULL,last_error='',updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            self._expire_reservations()
            if enqueue_consolidation:
                self._enqueue_derivation(job["project"], job["root"], job["session_id"])
            return results

    def _reconcile(self, decision: dict[str, Any], job: sqlite3.Row) -> dict[str, Any]:
        action = decision["action"]
        note_id = decision.get("note_id")
        result: dict[str, Any] = {"action": action, "note_id": note_id}
        if isinstance(note_id, str) and note_id.startswith("legacy:") and action == "unchanged":
            # The validated legacy target stays untouched, including its provenance.
            self.conn.execute(
                "INSERT INTO learning_decisions(job_id,action,detail,created_at) VALUES (?,?,?,?)",
                (job["id"], action, _json({"decision": decision, "result": result}), time.time()),
            )
            return result
        if action not in {"reject", "conflict"}:
            row = None
            if note_id:
                row = self.conn.execute(
                    "SELECT * FROM learning_notes WHERE note_id=? AND project=? AND root=?",
                    (note_id, job["project"], job["root"]),
                ).fetchone()
                if row is None:
                    result.update(action="conflict", reason="target note is unavailable")
            else:
                row = self.conn.execute(
                    "SELECT * FROM learning_notes WHERE project=? AND root=? AND logical_key=? "
                    "AND status='active'",
                    (job["project"], job["root"], decision["logical_key"]),
                ).fetchone()
            if row is None and not note_id and action == "add":
                normalized = " ".join(decision["statement"].casefold().split())
                for candidate in self.conn.execute(
                    "SELECT * FROM learning_notes WHERE project=? AND root=? AND status='active'",
                    (job["project"], job["root"]),
                ):
                    if " ".join(candidate["statement"].casefold().split()) == normalized:
                        row = candidate
                        break
            if result["action"] != "conflict":
                if (
                    row
                    and row["status"] == "active"
                    and " ".join(row["statement"].casefold().split())
                    == " ".join(decision["statement"].casefold().split())
                ):
                    result.update(
                        action="unchanged", note_id=row["note_id"], revision=row["revision"]
                    )
                elif action == "add" and row is None:
                    result = self._write_note(decision, job["project"], job["root"], None, "add")
                elif action in {"update", "supersede"} and row is not None:
                    if (
                        not note_id
                        or row["status"] != "active"
                        or decision.get("expected_revision") != row["revision"]
                        or decision["logical_key"] != row["logical_key"]
                    ):
                        result.update(action="conflict", reason="expected revision does not match")
                    elif action == "update":
                        result = self._write_note(
                            decision, job["project"], job["root"], row, "update"
                        )
                    else:
                        self._set_status(row, "superseded")
                        result = self._write_note(
                            decision, job["project"], job["root"], None, "supersede"
                        )
                        result["superseded_note_id"] = row["note_id"]
                else:
                    result.update(action="conflict", reason="explicit current target required")
        self.conn.execute(
            "INSERT INTO learning_decisions(job_id,note_id,action,detail,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                job["id"],
                result.get("note_id") if self._known_note(result.get("note_id")) else None,
                result["action"],
                _json({"decision": decision, "result": result}),
                time.time(),
            ),
        )
        return result

    def _known_note(self, note_id: str | None) -> bool:
        return bool(
            note_id
            and self.conn.execute(
                "SELECT 1 FROM learning_notes WHERE note_id=?", (note_id,)
            ).fetchone()
        )

    def _write_note(
        self,
        decision: dict[str, Any],
        project: str,
        root: str,
        old: sqlite3.Row | None,
        action: str,
    ) -> dict[str, Any]:
        now = time.time()
        note = {
            key: decision.get(key)
            for key in (
                "logical_key",
                "scope",
                "category",
                "title",
                "statement",
                "observed_at",
                "effective_at",
            )
        }
        note.update(
            note_id=old["note_id"] if old else _id(),
            project=project,
            root=root,
            revision=old["revision"] + 1 if old else 1,
            status="active",
            created_at=old["created_at"] if old else now,
            updated_at=now,
        )
        note["path"] = f"learned-{note['note_id']}.md"
        note["evidence"] = decision["evidence"]
        note["file_hash"] = _hash(self.render_note(note))
        fields = (
            "note_id",
            "project",
            "root",
            "logical_key",
            "path",
            "revision",
            "status",
            "scope",
            "category",
            "title",
            "statement",
            "observed_at",
            "effective_at",
            "file_hash",
            "created_at",
            "updated_at",
        )
        self.conn.execute(
            "INSERT INTO learning_notes("
            + ",".join(fields)
            + ") VALUES ("
            + ",".join("?" for _ in fields)
            + ") ON CONFLICT(note_id) DO UPDATE SET "
            + ",".join(f"{field}=excluded.{field}" for field in fields if field != "note_id"),
            [note[field] for field in fields],
        )
        evidence = list(decision["evidence"])
        for refs in decision.get("proof", {}).values():
            evidence.extend(refs)
        correction = decision.get("correction", {})
        if isinstance(correction, dict):
            for key in ("previous_evidence", "current_evidence"):
                if key in correction:
                    evidence.extend(self._evidence(correction[key], project, root))
        for ref in evidence:
            self.conn.execute(
                "INSERT OR IGNORE INTO learning_evidence VALUES (?,?,?,?,?,?,?)",
                (
                    note["note_id"],
                    note["revision"],
                    ref["source_id"],
                    ref["source_hash"],
                    ref["start"],
                    ref["end"],
                    ref["quote"],
                ),
            )
        # Render from the complete persisted evidence, including proof/correction.
        note = self._note(
            self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (note["note_id"],)
            ).fetchone()
        )
        note["file_hash"] = _hash(self.render_note(note))
        self.conn.execute(
            "UPDATE learning_notes SET file_hash=? WHERE note_id=?",
            (note["file_hash"], note["note_id"]),
        )
        snapshot = dict(note)
        snapshot["details"] = {
            key: decision[key] for key in ("correction", "proof") if key in decision
        }
        self.conn.execute(
            "INSERT INTO learning_revisions VALUES (?,?,?,?,?)",
            (note["note_id"], note["revision"], action, _json(snapshot), now),
        )
        self._queue(note["note_id"], note["revision"], "write")
        self._invalidate(note)
        return {
            "action": action,
            "note_id": note["note_id"],
            "revision": note["revision"],
            "stored": True,
            "indexed": False,
        }

    def _queue(self, note_id: str, revision: int, operation: str) -> None:
        self.conn.execute(
            "INSERT INTO learning_outbox(note_id,revision,operation,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(note_id) DO UPDATE SET revision=excluded.revision,"
            "operation=excluded.operation,status='pending',attempts=0,last_error='',"
            "updated_at=excluded.updated_at",
            (note_id, revision, operation, time.time()),
        )

    def _invalidate(self, note: dict[str, Any] | sqlite3.Row) -> None:
        from .learning_consolidation import withdraw

        withdraw(self, note_ids=[note["note_id"]], project=note["project"])
        rev = db.bump_rev(self.conn)
        self.conn.execute(
            "UPDATE memories SET archived_at=?,rev=?,updated_at=? WHERE project=? AND "
            "root=? AND path=?",
            (db.utc_now(), rev, db.utc_now(), note["project"], note["root"], note["path"]),
        )

    def _set_status(self, row: sqlite3.Row, status: str) -> None:
        revision = row["revision"] + 1
        self.conn.execute(
            "UPDATE learning_notes SET status=?,revision=?,updated_at=? WHERE note_id=?",
            (status, revision, time.time(), row["note_id"]),
        )
        self.conn.execute(
            "INSERT INTO learning_revisions VALUES (?,?,?,?,?)",
            (row["note_id"], revision, status, _json({"status": status}), time.time()),
        )
        self._queue(row["note_id"], revision, "remove")
        self._invalidate(row)

    def edit(
        self,
        note_id: str,
        expected_revision: int,
        *,
        title: str | None = None,
        statement: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate explicit edits with the automatic evidence/authority contract."""
        from .learning_extract import ValidationError, source_date, validate_stored_candidate

        with db.write_tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (note_id,)
            ).fetchone()
            if (
                type(expected_revision) is not int
                or not row
                or row["status"] != "active"
                or row["revision"] != expected_revision
            ):
                raise RevisionConflict("expected active note revision does not match")
            previous = self._note(row)
            current_statement = previous["statement"] if statement is None else statement
            references = (
                evidence
                if evidence is not None
                else [ref for ref in previous["evidence"] if current_statement in ref["quote"]]
            )
            references = self._evidence(references, row["project"], row["root"])
            # Load the actual immutable sources, including tool metadata needed
            # to establish a proven fix. Existing text is not human authority.
            all_refs = list(references) + previous["evidence"]
            snapshot = self.conn.execute(
                "SELECT snapshot FROM learning_revisions WHERE note_id=? AND revision=?",
                (note_id, row["revision"]),
            ).fetchone()
            details = json.loads(snapshot[0]).get("details", {}) if snapshot else {}
            proof = details.get("proof")
            if proof:
                all_refs.extend(ref for refs in proof.values() for ref in refs)
            sources = []
            for source_id in dict.fromkeys(ref["source_id"] for ref in all_refs):
                source_row = self.conn.execute(
                    "SELECT * FROM learning_sources WHERE id=? AND status='eligible'",
                    (source_id,),
                ).fetchone()
                if source_row is None:
                    raise LearningError("edited memory source is unavailable")
                source = dict(source_row)
                source["metadata"] = json.loads(source["metadata"])
                source["tool_name"] = source["metadata"].get("tool_name", "")
                source["scope"] = {"project": source["project"], "root": source["root"]}
                sources.append(source)
            source_map = {source["id"]: source for source in sources}
            candidate = {
                key: previous[key] for key in ("logical_key", "category", "scope", "effective_at")
            }
            candidate.update(
                action="update",
                note_id=note_id,
                expected_revision=expected_revision,
                title=previous["title"] if title is None else title,
                statement=current_statement,
                evidence=references,
                observed_at=max(source_date(source_map[ref["source_id"]]) for ref in references),
                correction={
                    "previous_statement": previous["statement"],
                    "current_statement": current_statement,
                    "previous_evidence": previous["evidence"],
                    "current_evidence": references,
                },
            )
            if proof is not None:
                candidate["proof"] = proof
            try:
                validated = validate_stored_candidate(candidate, sources, [previous])
            except ValidationError as exc:
                raise LearningError("edited memory failed shared validation: " + str(exc)) from exc
            return self._write_note(validated, row["project"], row["root"], row, "edit")

    def exclude(self, note_id: str) -> dict[str, Any]:
        with db.write_tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (note_id,)
            ).fetchone()
            if not row:
                raise LearningError("unknown note")
            sources = [
                reference[0]
                for reference in self.conn.execute(
                    "SELECT DISTINCT source_id FROM learning_evidence WHERE note_id=?",
                    (note_id,),
                )
            ]
            affected = self._revoke_sources(sources, forget=False)
            if row["status"] == "active" and note_id not in affected:
                self._set_status(row, "excluded")
        return {
            "note_id": note_id,
            "status": "excluded" if row["status"] == "active" else row["status"],
        }

    def _revoke_sources(self, source_ids: list[str], *, forget: bool) -> list[str]:
        # Identical source text can be delivered under different transcript IDs.
        # Revoke all copies in the same scope before any queued worker can reuse it.
        expanded = set(source_ids)
        for source_id in source_ids:
            expanded.update(
                row[0]
                for row in self.conn.execute(
                    "SELECT copy.id FROM learning_sources original JOIN learning_sources copy "
                    "ON copy.project=original.project AND copy.root=original.root "
                    "AND copy.sha256=original.sha256 WHERE original.id=?",
                    (source_id,),
                )
            )
        source_ids = sorted(expanded)
        from .learning_consolidation import withdraw

        withdraw(self, source_ids=source_ids, forget=forget)
        affected: set[str] = set()
        jobs: set[str] = set()
        for source_id in source_ids:
            self.conn.execute(
                "UPDATE learning_sources SET status=? WHERE id=?",
                ("forgotten" if forget else "excluded", source_id),
            )
            if forget:
                self.conn.execute(
                    "UPDATE learning_sources SET content='',metadata='{}' WHERE id=?", (source_id,)
                )
            affected.update(
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT note_id FROM learning_evidence WHERE source_id=?", (source_id,)
                )
            )
            jobs.update(
                row[0]
                for row in self.conn.execute(
                    "SELECT job_id FROM learning_job_sources WHERE source_id=?", (source_id,)
                )
            )
        from .learning_pages import erase_jobs

        if forget:
            erase_jobs(self.conn, sorted(jobs))
        for job_id in jobs:
            self.conn.execute(
                "UPDATE learning_jobs SET status='cancelled',lease_token=NULL,lease_owner=NULL,"
                "lease_until=NULL,last_error='',progress='{}',updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            if forget:
                self.conn.execute("DELETE FROM learning_decisions WHERE job_id=?", (job_id,))
        for note_id in affected:
            row = self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (note_id,)
            ).fetchone()
            if row["status"] != "forgotten":
                self._set_status(row, "forgotten" if forget else "excluded")
            if forget:
                self._erase_note(note_id)
        self._expire_reservations()
        return sorted(affected)

    def exclude_source(self, source_id: str) -> dict[str, Any]:
        with db.write_tx(self.conn):
            if not self.conn.execute(
                "SELECT 1 FROM learning_sources WHERE id=?", (source_id,)
            ).fetchone():
                raise LearningError("unknown source")
            affected = self._revoke_sources([source_id], forget=False)
        return {"source_id": source_id, "excluded_notes": affected}

    def _erase_note(self, note_id: str) -> None:
        from .learning_consolidation import withdraw

        withdraw(self, note_ids=[note_id], forget=True)
        self.conn.execute("DELETE FROM learning_evidence WHERE note_id=?", (note_id,))
        self.conn.execute("DELETE FROM learning_revisions WHERE note_id=?", (note_id,))
        self.conn.execute("DELETE FROM learning_decisions WHERE note_id=?", (note_id,))
        self.conn.execute(
            "UPDATE learning_notes SET title='',statement='',logical_key=?,file_hash='' "
            "WHERE note_id=?",
            ("forgotten:" + note_id, note_id),
        )
        # Erase the existing search row, vectors and feedback via the FK cascade.
        self.conn.execute(
            "DELETE FROM memories WHERE (project,root,path) IN "
            "(SELECT project,root,path FROM learning_notes WHERE note_id=?)",
            (note_id,),
        )

    def forget(self, note_id: str) -> dict[str, Any]:
        """Erase note text, history and source-derived notes; retain eligibility tombstones.

        This removes logical records, not bytes from historical database backups.
        The caller projects the removal before reporting filesystem completion.
        """
        with db.write_tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (note_id,)
            ).fetchone()
            if not row:
                raise LearningError("unknown note")
            sources = [
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT source_id FROM learning_evidence WHERE note_id=?", (note_id,)
                )
            ]
            affected = self._revoke_sources(sources, forget=True)
            if note_id not in affected:
                if row["status"] != "forgotten":
                    self._set_status(row, "forgotten")
                self._erase_note(note_id)
                affected.append(note_id)
        return {"note_id": note_id, "forgotten_notes": sorted(affected), "projection_pending": True}

    @staticmethod
    def render_note(note: dict[str, Any]) -> str:
        category = {
            "preference": "user",
            "workflow": "feedback",
            "decision": "project",
            "fact": "project",
            "proven_fix": "reference",
        }[note["category"]]
        lines = [
            "---",
            "name: " + _json(note["title"]),
            "description: "
            + _plain_line(
                "Learned " + note["category"].replace("_", " ") + " - " + note["statement"]
            ),
            "type: " + category,
            "noblivion_id: " + note["note_id"],
            "revision: " + str(note["revision"]),
            "status: " + note["status"],
            "scope: " + note["scope"],
            "rule: " + _plain_line(_first_sentence(note["statement"]), RULE_MAX_CHARS),
            "---",
            "",
            note["statement"],
            "",
            "Observed: " + note["observed_at"],
        ]
        if note.get("effective_at"):
            lines.append("Effective: " + note["effective_at"])
        lines += ["", "Evidence:"]
        for ref in sorted(note["evidence"], key=lambda r: (r["source_id"], r["start"], r["end"])):
            lines.append(
                f"- source {ref['source_id']} sha256:{ref['source_hash']} "
                f"characters {ref['start']}:{ref['end']}"
            )
        return "\n".join(lines) + "\n"

    def _refresh_format(self, project: str, root: str) -> None:
        """Queue a rewrite of an active note whose file text changed with the format.

        The database row is the authority and the file is a projection, so the
        stored hash follows the current rendering. Revision and evidence stay.
        """
        stale = []
        for row in self.conn.execute(
            "SELECT * FROM learning_notes WHERE project=? AND root=? AND status='active'",
            (project, root),
        ).fetchall():
            digest = _hash(self.render_note(self._note(row)))
            if digest != row["file_hash"]:
                stale.append((row["note_id"], digest))
        if not stale:
            return
        with db.write_tx(self.conn):
            for note_id, digest in stale:
                row = self.conn.execute(
                    "SELECT revision,status FROM learning_notes WHERE note_id=?", (note_id,)
                ).fetchone()
                if row is None or row["status"] != "active":
                    continue
                self.conn.execute(
                    "UPDATE learning_notes SET file_hash=? WHERE note_id=?", (digest, note_id)
                )
                self._queue(note_id, row["revision"], "write")

    def project_pending(self, project: str, root: str, notes_dir: Path | str) -> dict[str, int]:
        """Project only managed UUID paths; each file and outbox acknowledgement is retryable."""
        folder = Path(notes_dir)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink():
            raise LearningError("projection folder must not be a symlink")
        self._refresh_format(project, root)
        counts = {"written": 0, "removed": 0, "failed": 0, "pending": 0}
        ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT o.note_id FROM learning_outbox o JOIN learning_notes n USING(note_id) "
                "WHERE n.project=? AND n.root=? AND o.status='pending' ORDER BY o.updated_at",
                (project, root),
            )
        ]
        for note_id in ids:
            try:
                # The lock prevents correction/exclusion racing a stale file write.
                with db.write_tx(self.conn):
                    row = self.conn.execute(
                        "SELECT n.*,o.operation,o.status outbox_status FROM learning_notes n "
                        "JOIN learning_outbox o USING(note_id) WHERE n.note_id=?",
                        (note_id,),
                    ).fetchone()
                    if not row or row["outbox_status"] != "pending":
                        continue
                    path = folder / row["path"]
                    if path.name != "learned-" + str(uuid.UUID(note_id)) + ".md":
                        raise LearningError("invalid managed note path")
                    if row["operation"] == "remove":
                        path.unlink(missing_ok=True)
                        counts["removed"] += 1
                    else:
                        content = self.render_note(self._note(row))
                        if _hash(content) != row["file_hash"] or row["status"] != "active":
                            raise LearningError("projection revision mismatch")
                        fd, temporary = tempfile.mkstemp(prefix=".learning-", dir=folder)
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                                stream.write(content)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(temporary, path)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                        counts["written"] += 1
                    directory_fd = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    self.conn.execute(
                        "UPDATE learning_outbox SET status='written',attempts=attempts+1,"
                        "last_error='',updated_at=? WHERE note_id=?",
                        (time.time(), note_id),
                    )
            except (OSError, LearningError):
                counts["failed"] += 1
                with db.write_tx(self.conn):
                    self.conn.execute(
                        "UPDATE learning_outbox SET attempts=attempts+1,"
                        "last_error='projection failed',"
                        "updated_at=? WHERE note_id=?",
                        (time.time(), note_id),
                    )
        counts["pending"] = self.conn.execute(
            "SELECT count(*) FROM learning_outbox o JOIN learning_notes n USING(note_id) "
            "WHERE n.project=? AND n.root=? AND o.status!='indexed'",
            (project, root),
        ).fetchone()[0]
        return counts

    def acknowledge_index(self, project: str, root: str) -> int:
        """A saved file is not recall-ready until the matching live index row exists."""
        with db.write_tx(self.conn):
            return self.conn.execute(
                "UPDATE learning_outbox SET status='indexed',updated_at=? WHERE status='written' "
                "AND note_id IN (SELECT n.note_id FROM learning_notes n WHERE n.project=? "
                "AND n.root=? "
                "AND ((n.status='active' AND EXISTS (SELECT 1 FROM memories m WHERE "
                "m.project=n.project "
                "AND m.root=n.root AND m.path=n.path AND m.hash=n.file_hash AND "
                "m.archived_at IS NULL "
                "AND m.deleted_at IS NULL)) OR (n.status!='active' AND NOT EXISTS "
                "(SELECT 1 FROM memories m WHERE m.project=n.project AND m.root=n.root "
                "AND m.path=n.path "
                "AND m.archived_at IS NULL AND m.deleted_at IS NULL))))",
                (time.time(), project, root),
            ).rowcount

    def summary(
        self, project: str, root: str, *, session_id: str = "", limit_chars: int = 6000
    ) -> str:
        if not self.read_allowed(project, root, session_id):
            return ""
        if not 0 <= limit_chars <= 64000:
            raise LearningError("invalid summary size")
        lines: list[str] = []
        for note in self.semantic_notes(project, root, session_id=session_id):
            if not self.read_allowed(project, note["root"], session_id):
                continue
            line = f"- {note['statement']} [memory:{note['note_id']} revision:{note['revision']}]"
            if len("\n".join([*lines, line])) <= limit_chars:
                lines.append(line)
        return "\n".join(lines)

    def _hold_reason(self, row: sqlite3.Row, now: float) -> tuple[str, float, bool]:
        """Say why a pending job waits, and since when. Text here is never provider output."""
        held = self.conn.execute(
            "SELECT session_id,use_memory,contribute,paused,updated_at FROM learning_controls "
            "WHERE project=? AND root=? AND session_id IN ('',?) "
            "AND (paused=1 OR contribute=0) ORDER BY updated_at DESC",
            (row["project"], row["root"], row["session_id"]),
        ).fetchone()
        if held is not None:
            level = "session" if held["session_id"] else "scope"
            what = "paused" if held["paused"] == 1 else "contribution off"
            return level + " " + what, max(held["updated_at"], row["created_at"]), True
        error = str(row["last_error"] or "")
        if error in {"budget unavailable", "BudgetExceeded"}:
            return "budget", row["updated_at"], False
        if error == "ScopeNotEnabled":
            return "scope not enabled", row["updated_at"], False
        if error:
            return "provider error: " + error[:120], row["updated_at"], False
        if row["next_attempt"] > now:
            return "waiting for retry time", row["updated_at"], False
        return "waiting for a worker", row["created_at"], False

    def pending_jobs(
        self,
        project: str | None = None,
        root: str | None = None,
        *,
        stale_days: float = 7,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List jobs that wait, with the reason and the age. Nothing is changed or dropped."""
        where, params = "WHERE status='pending'", []
        if project is not None:
            where += " AND project=?"
            params.append(project)
        if root is not None:
            where += " AND root=?"
            params.append(root)
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM learning_jobs " + where + " ORDER BY created_at,id", params
        ).fetchall()
        jobs, stale = [], 0
        for row in rows:
            reason, since, held = self._hold_reason(row, now)
            is_stale = held and (now - since) > stale_days * 86400
            stale += is_stale
            jobs.append(
                {
                    "job_id": row["id"],
                    "kind": row["kind"],
                    "project": row["project"],
                    "root": row["root"],
                    "session_id": row["session_id"],
                    "reason": reason,
                    "age_days": round((now - row["created_at"]) / 86400, 2),
                    "held_days": round((now - since) / 86400, 2),
                    "attempts": row["attempts"],
                    "stale": is_stale,
                }
            )
        return {"total": len(jobs), "stale": stale, "jobs": jobs[:limit]}

    def status(self, project: str | None = None, root: str | None = None) -> dict[str, Any]:
        where, params = "", []
        if project is not None:
            where, params = " WHERE project=?", [project]
        if root is not None:
            where += " AND root=?" if where else " WHERE root=?"
            params.append(root)
        from .learning_consolidation import derivative_status
        from .learning_pages import coverage

        return {
            "consolidation": derivative_status(self, project, root),
            "coverage": coverage(self.conn),
            "conflicts": self._conflict_status(project, root),
            "jobs": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT status,count(*) FROM learning_jobs" + where + " GROUP BY status", params
                )
            },
            "pending": self.pending_jobs(project, root),
            "notes": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT status,count(*) FROM learning_notes" + where + " GROUP BY status",
                    params,
                )
            },
            "outbox": {
                r[0]: r[1]
                for r in self.conn.execute(
                    "SELECT status,count(*) FROM learning_outbox WHERE note_id IN "
                    "(SELECT note_id FROM learning_notes" + where + ") GROUP BY status",
                    params,
                )
            },
            "revisions": self.conn.execute(
                "SELECT count(*) FROM learning_revisions WHERE note_id IN "
                "(SELECT note_id FROM learning_notes" + where + ")",
                params,
            ).fetchone()[0],
            "budget": dict(
                self.conn.execute(
                    "SELECT count(*) calls,coalesce(sum(tokens),0) tokens,"
                    "coalesce(sum(seconds),0) seconds,coalesce(sum(cost),0) cost FROM "
                    "learning_budget "
                    "WHERE created_at>=?",
                    (time.time() - 86400,),
                ).fetchone()
            ),
            "exhausted": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id,attempts,last_error FROM learning_jobs"
                    + where
                    + (" AND" if where else " WHERE")
                    + " status='exhausted' ORDER BY updated_at",
                    params,
                )
            ],
        }
