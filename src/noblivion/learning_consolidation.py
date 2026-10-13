# SPDX-License-Identifier: AGPL-3.0-or-later
"""Noblivion-owned, exact-support derivatives and inert procedure candidates.

Adapted design patterns from PhoenixDaemon d7b231283a1ae0bd685e0e846ffedc6d6db77114:
provenance.py (transactional withdrawal), memory_versioning.py (revision lineage),
recon_mm_candidates.py/skill_evolution.py (verified, disabled review candidates).
No Phoenix code, service, registry or release is required at runtime.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import db
from .learning import LearningError, SemanticSnapshotChanged, _hash, _json, _safe, _text
from .learning_extract import _AUTHORITY, _INJECTION, _quoted, source_date


@dataclass(frozen=True)
class ConsolidationSettings:
    max_claims: int = 12
    max_supports: int = 32
    max_notes: int = 512
    max_input_chars: int = 24000
    max_statement_chars: int = 2000
    max_context_chars: int = 12000
    max_context_claims: int = 32
    max_drafts: int = 8
    min_workflow_runs: int = 2

    def __post_init__(self):
        bounds = {
            "max_claims": 12,
            "max_supports": 64,
            "max_notes": 512,
            "max_input_chars": 64000,
            "max_statement_chars": 4000,
            "max_context_chars": 16000,
            "max_context_claims": 64,
            "max_drafts": 16,
            "min_workflow_runs": 8,
        }
        if any(
            type(getattr(self, k)) is not int or not 1 <= getattr(self, k) <= v
            for k, v in bounds.items()
        ):
            raise LearningError("invalid consolidation limits")
        if self.min_workflow_runs < 2:
            raise LearningError("workflow evidence must be independent")

    @property
    def fingerprint(self):
        return _hash(_json(asdict(self)))


def support_id(reference):
    return _hash(
        _json(
            {
                k: reference[k]
                for k in ("note_id", "revision", "source_id", "source_hash", "start", "end")
            }
        )
    )[:24]


def _reference(row):
    ref = dict(row)
    ref["revision"] = ref.pop("note_revision", ref.get("revision"))
    ref.pop("claim_id", None)
    ref.pop("claim_revision", None)
    ref.pop("draft_id", None)
    return ref


class ConsolidationLedger:
    def __init__(self, ledger, settings=None):
        self.ledger = ledger
        self.conn = ledger.conn
        self.settings = settings or ConsolidationSettings()

    def validate_supports(self, references, project, root, *, session_id=None):
        """Reference integrity is deterministic; it is not a semantic verdict."""
        if (
            not isinstance(references, list)
            or not 1 <= len(references) <= self.settings.max_supports
        ):
            raise LearningError("invalid claim support count")
        validated, seen = [], set()
        for ref in references:
            if not isinstance(ref, dict):
                raise LearningError("invalid claim support")
            note = self.conn.execute(
                "SELECT * FROM learning_notes WHERE note_id=?", (ref.get("note_id"),)
            ).fetchone()
            if (
                not note
                or note["project"] != project
                or note["status"] != "active"
                or note["revision"] != ref.get("revision")
                or (note["root"] != root and note["scope"] != "general")
            ):
                raise SemanticSnapshotChanged("claim support note is no longer current")
            if session_id is not None and not self.ledger.read_allowed(
                project, note["root"], session_id
            ):
                raise LearningError("claim support memory use disabled")
            exact = self.ledger._evidence([ref], project, note["root"])[0]
            stored = self.conn.execute(
                "SELECT 1 FROM learning_evidence WHERE note_id=? AND revision=? AND source_id=? "
                "AND source_hash=? AND start=? AND end=? AND quote=?",
                (
                    ref["note_id"],
                    ref["revision"],
                    exact["source_id"],
                    exact["source_hash"],
                    exact["start"],
                    exact["end"],
                    exact["quote"],
                ),
            ).fetchone()
            if not stored:
                raise LearningError("claim support is not part of the cited note revision")
            source = dict(
                self.conn.execute(
                    "SELECT * FROM learning_sources WHERE id=?", (exact["source_id"],)
                ).fetchone()
            )
            source["metadata"] = json.loads(source["metadata"])
            if _quoted(source["content"], exact["start"], exact["end"]):
                raise LearningError("quoted or injected claim support")
            item = {**exact, "note_id": note["note_id"], "revision": note["revision"]}
            item["support_id"] = support_id(item)
            if ref.get("support_id", item["support_id"]) != item["support_id"]:
                raise LearningError("claim support identity mismatch")
            if item["support_id"] in seen:
                raise LearningError("duplicate claim support")
            seen.add(item["support_id"])
            item.update(
                observed_at=source_date(source),
                note_root=note["root"],
                note_scope=note["scope"],
                note_category=note["category"],
                note_statement=note["statement"],
                effective_at=note["effective_at"],
                metadata=source["metadata"],
                session_id=source["session_id"],
                source_client=source["source_client"],
            )
            validated.append(item)
        return validated

    @staticmethod
    def compact_support(ref):
        return {
            k: ref[k]
            for k in (
                "support_id",
                "note_id",
                "revision",
                "source_id",
                "source_hash",
                "start",
                "end",
                "quote",
            )
        }

    def note_supports(self, note):
        return [
            self.compact_support(
                {
                    **ref,
                    "note_id": note["note_id"],
                    "revision": note["revision"],
                    "support_id": support_id(
                        {**ref, "note_id": note["note_id"], "revision": note["revision"]}
                    ),
                }
            )
            for ref in note["evidence"]
        ]

    def _guards(self, proposal, supports):
        statement = _text(
            proposal.get("statement"), "claim statement", self.settings.max_statement_chars
        )
        _safe(_json(proposal))
        kind, category, scope = (proposal.get(k) for k in ("kind", "category", "scope"))
        if kind not in {"explicit_statement", "observation", "inference"}:
            raise LearningError("invalid claim kind")
        if category not in {"fact", "preference", "workflow", "decision", "proven_fix"}:
            raise LearningError("invalid claim category")
        if scope not in {"project", "general"}:
            raise LearningError("invalid claim scope")
        if _AUTHORITY.search(statement) or _INJECTION.search(statement):
            raise LearningError("claim cannot grant authority")
        if kind == "inference":
            if (
                category != "fact"
                or scope != "project"
                or re.search(
                    r"(?i)\b(?:prefers?|preferred|preference|must|should|always|never|allow|enable|disable|"
                    r"instruction|workflow|approval|consent|opt.out|authorized|permission)\b",
                    statement,
                )
                or re.search(
                    r"(?i)^\s*(?:use|run|set|keep|follow|execute|delete|write|install)\b", statement
                )
            ):
                raise LearningError("inference cannot create preferences, instructions or controls")
        elif kind == "explicit_statement":
            if not any(
                ref["role"] == "user"
                and statement in ref["quote"]
                and ref["metadata"].get("direct_user") is not False
                for ref in supports
            ):
                raise LearningError("explicit claim requires direct user statement")
        elif not any(
            ref["role"] in {"user", "tool"} and statement in ref["quote"] for ref in supports
        ):
            raise LearningError("observation requires an extractive native user or tool passage")
        if category in {"preference", "workflow", "decision"} and kind != "explicit_statement":
            raise LearningError("user guidance requires an explicit statement")
        if category == "proven_fix":
            raise LearningError("generated claims cannot create proven-fix qualification")
        if scope == "general" and (
            category not in {"preference", "workflow"}
            or not all(ref["note_scope"] == "general" for ref in supports)
        ):
            raise LearningError("general claim scope requires explicit general support")
        if scope == "project" and any(ref["note_root"] != proposal["root"] for ref in supports):
            raise LearningError("project conclusion cannot borrow cross-root support")
        return statement

    def stage(self, proposal, project, root, *, job_id=None, lease_token=None):
        """Stage a generated statement with exact edges; never implicitly admit it."""
        if not isinstance(proposal, dict):
            raise LearningError("invalid claim proposal")
        project, root = _text(project, "project"), _text(root, "root")
        if not self.ledger.effective_controls(project, root)["contribute"]:
            raise LearningError("claim contribution is disabled")
        with db.write_tx(self.conn):
            if job_id is not None:
                job = self.ledger._lease(job_id, lease_token)
                if (job["project"], job["root"]) != (project, root):
                    raise LearningError("claim job scope mismatch")
            supports = self.validate_supports(proposal.get("supports"), project, root)
            statement = self._guards({**proposal, "root": root}, supports)
            parents = proposal.get("parents", [])
            if (
                not isinstance(parents, list)
                or len(parents) > 8
                or len(set(parents)) != len(parents)
            ):
                raise LearningError("invalid parent claim count")
            parent_rows = []
            for parent in parents:
                item = self.inspect(parent)
                if item["status"] != "active" or item["project"] != project or item["root"] != root:
                    raise LearningError("parent claim is unavailable")
                self.validate_supports(item["supports"], project, root)
                if not {s["support_id"] for s in item["supports"]}.issubset(
                    {s["support_id"] for s in supports}
                ):
                    raise LearningError("parent support chain is incomplete")
                parent_rows.append(item)
            observed = max(s["observed_at"] for s in supports)
            effective_dates = {s["effective_at"] for s in supports}
            effective = effective_dates.pop() if len(effective_dates) == 1 else None
            if (
                proposal.get("observed_at", observed) != observed
                or proposal.get("effective_at", effective) != effective
            ):
                raise LearningError("claim dates must come from exact support")
            claim_id = _hash(
                _json(
                    [
                        project,
                        root,
                        statement,
                        proposal["kind"],
                        proposal["category"],
                        proposal["scope"],
                        sorted(s["support_id"] for s in supports),
                        parents,
                    ]
                )
            )
            prior = self.conn.execute(
                "SELECT * FROM learning_claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if prior and prior["status"] != "withdrawn":
                return self.inspect(claim_id)
            if prior:
                revision = prior["revision"] + 1
                self.conn.execute(
                    "UPDATE learning_claims SET status='staged',revision=?,"
                    "semantic_relation='unreviewed',"
                    "job_id=?,config_fingerprint=?,updated_at=? WHERE claim_id=?",
                    (revision, job_id, self.settings.fingerprint, time.time(), claim_id),
                )
                self._write_supports(claim_id, revision, supports)
                for item in parent_rows:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO learning_claim_edges VALUES (?,?,?,'support')",
                        (item["claim_id"], item["revision"], claim_id),
                    )
                self._snapshot(claim_id, "restage")
                return self.inspect(claim_id)
            now = time.time()
            self.conn.execute(
                "INSERT INTO learning_claims "
                "(claim_id,project,root,revision,statement,kind,category,"
                "scope,status,observed_at,effective_at,job_id,config_fingerprint,"
                "created_at,updated_at) "
                "VALUES (?,?,?,1,?,?,?,?,'staged',?,?,?,?,?,?)",
                (
                    claim_id,
                    project,
                    root,
                    statement,
                    proposal["kind"],
                    proposal["category"],
                    proposal["scope"],
                    observed,
                    effective,
                    job_id,
                    self.settings.fingerprint,
                    now,
                    now,
                ),
            )
            self._write_supports(claim_id, 1, supports)
            for item in parent_rows:
                self.conn.execute(
                    "INSERT INTO learning_claim_edges VALUES (?,?,?,'support')",
                    (item["claim_id"], item["revision"], claim_id),
                )
            self._snapshot(claim_id, "stage")
            return self.inspect(claim_id)

    def _write_supports(self, claim_id, revision, supports):
        for ref in supports:
            self.conn.execute(
                "INSERT INTO learning_claim_supports VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    revision,
                    ref["support_id"],
                    ref["note_id"],
                    ref["revision"],
                    ref["source_id"],
                    ref["source_hash"],
                    ref["start"],
                    ref["end"],
                    ref["quote"],
                ),
            )

    def inspect(self, claim_id):
        row = self.conn.execute(
            "SELECT * FROM learning_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None:
            raise LearningError("unknown claim")
        result = dict(row)
        result["supports"] = [
            _reference(s)
            for s in self.conn.execute(
                "SELECT * FROM learning_claim_supports WHERE claim_id=? AND "
                "claim_revision=? ORDER BY support_id",
                (claim_id, row["revision"]),
            )
        ]
        result["parents"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT parent_id,parent_revision,relation FROM learning_claim_edges WHERE "
                "child_id=?",
                (claim_id,),
            )
        ]
        result["revisions"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT revision,action,snapshot,created_at FROM learning_claim_revisions "
                "WHERE claim_id=? "
                "ORDER BY revision",
                (claim_id,),
            )
        ]
        return result

    def _snapshot(self, claim_id, action):
        item = self.inspect(claim_id)
        item.pop("revisions")
        self.conn.execute(
            "INSERT INTO learning_claim_revisions VALUES (?,?,?,?,?)",
            (claim_id, item["revision"], action, _json(item), time.time()),
        )

    def admit(self, claim_id, verdict, *, job_id=None, lease_token=None, expected_snapshot=None):
        """A distinct complete semantic verdict may admit a reference-valid claim."""
        with db.write_tx(self.conn):
            if job_id is not None:
                self.ledger._lease(job_id, lease_token)
            claim = self.inspect(claim_id)
            if expected_snapshot is not None:
                current = sorted(
                    [
                        [n["note_id"], n["revision"]]
                        for n in self.ledger.current_notes(claim["project"], claim["root"])
                    ]
                )
                if current != expected_snapshot:
                    raise SemanticSnapshotChanged("complete semantic support snapshot changed")
            if claim["status"] != "staged":
                return claim
            supports = self.validate_supports(claim["supports"], claim["project"], claim["root"])
            self._guards(claim, supports)
            required = sorted(s["support_id"] for s in supports)
            if (
                not isinstance(verdict, dict)
                or verdict.get("claim_id") != claim_id
                or type(verdict.get("complete")) is not bool
                or not isinstance(verdict.get("support_ids"), list)
                or sorted(verdict["support_ids"]) != required
                or not isinstance(verdict.get("conflicts"), list)
            ):
                raise LearningError("semantic support verdict is incomplete")
            relation = verdict.get("relation")
            if relation not in {"supported", "unsupported", "uncertain", "contradictory"}:
                raise LearningError("invalid semantic support relation")
            conflicts = verdict["conflicts"]
            if len(conflicts) > 32 or len(set(conflicts)) != len(conflicts):
                raise LearningError("invalid semantic conflict count")
            for conflict_id in conflicts:
                other = self.inspect(conflict_id)
                if other["project"] != claim["project"] or other["root"] != claim["root"]:
                    raise LearningError("semantic conflict is outside claim scope")
                self.conn.execute(
                    "INSERT OR IGNORE INTO learning_claim_edges VALUES (?,?,?,'conflict')",
                    (other["claim_id"], other["revision"], claim_id),
                )
            status = (
                "active"
                if relation == "supported" and verdict["complete"] and not conflicts
                else "conflicted"
            )
            revision = claim["revision"] + 1
            self.conn.execute(
                "UPDATE learning_claims SET status=?,revision=?,semantic_relation=?,updated_at=? "
                "WHERE claim_id=?",
                (status, revision, relation, time.time(), claim_id),
            )
            self._write_supports(claim_id, revision, supports)
            self._snapshot(claim_id, "semantic_support")
            self.conn.execute(
                "DELETE FROM learning_context_cache WHERE project=?", (claim["project"],)
            )
            return self.inspect(claim_id)

    def reconcile_equivalence(
        self,
        candidate_id,
        relations,
        *,
        expected_claims,
        expected_notes,
        job_id=None,
        lease_token=None,
    ):
        """Merge only independently supported claims with a complete semantic comparison.

        The entire current claim/note snapshot is fenced inside the same write
        transaction as the canonical revision. Every predecessor keeps history.
        """
        with db.write_tx(self.conn):
            if job_id is not None:
                self.ledger._lease(job_id, lease_token)
            candidate = self.inspect(candidate_id)
            project, root = candidate["project"], candidate["root"]
            current_notes = sorted(
                [[n["note_id"], n["revision"]] for n in self.ledger.current_notes(project, root)]
            )
            current_claims = sorted(
                [
                    [r["claim_id"], r["revision"]]
                    for r in self.conn.execute(
                        "SELECT claim_id,revision FROM learning_claims WHERE project=? AND "
                        "root=? AND status='active'",
                        (project, root),
                    )
                ]
            )
            if current_notes != expected_notes or current_claims != expected_claims:
                raise SemanticSnapshotChanged("equivalence support snapshot changed")
            expected = {
                ident: revision for ident, revision in expected_claims if ident != candidate_id
            }
            if (
                candidate["status"] != "active"
                or not isinstance(relations, list)
                or len(relations) != len(expected)
            ):
                raise LearningError("incomplete semantic equivalence comparison")
            seen, equivalents, contradictions = set(), [], []
            for relation in relations:
                if (
                    not isinstance(relation, dict)
                    or set(relation) != {"target_id", "target_revision", "relation"}
                    or relation["target_id"] not in expected
                    or relation["target_id"] in seen
                    or relation["target_revision"] != expected[relation["target_id"]]
                    or relation["relation"]
                    not in {"equivalent", "distinct", "contradictory", "uncertain"}
                ):
                    raise LearningError("invalid semantic equivalence relation")
                seen.add(relation["target_id"])
                target = self.inspect(relation["target_id"])
                if relation["relation"] == "equivalent":
                    if any(
                        candidate[k] != target[k]
                        for k in ("kind", "category", "scope", "root", "effective_at")
                    ):
                        raise LearningError("equivalent claims have different applicability")
                    equivalents.append(target)
                elif relation["relation"] == "contradictory":
                    contradictions.append(target)
            if contradictions:
                withdraw(self.ledger, claim_ids=[candidate_id], project=project)
                old = self.inspect(candidate_id)
                revision = old["revision"] + 1
                supports = self.validate_supports(candidate["supports"], project, root)
                self.conn.execute(
                    "UPDATE learning_claims SET "
                    "status='conflicted',revision=?,semantic_relation='contradictory',updated_at=? "
                    "WHERE claim_id=?",
                    (revision, time.time(), candidate_id),
                )
                self._write_supports(candidate_id, revision, supports)
                for target in contradictions:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO learning_claim_edges VALUES (?,?,?,'conflict')",
                        (target["claim_id"], target["revision"], candidate_id),
                    )
                self._snapshot(candidate_id, "equivalence_conflict")
                return self.inspect(candidate_id)
            if not equivalents:
                return candidate
            members = [candidate, *equivalents]
            canonical = min(members, key=lambda c: (c["created_at"], c["claim_id"]))
            support_map = {}
            for member in members:
                for ref in self.validate_supports(member["supports"], project, root):
                    support_map[ref["support_id"]] = ref
            if len(support_map) > self.settings.max_supports:
                raise LearningError("equivalent support union exceeds hard limit")
            supports = list(support_map.values())
            effective = {s["effective_at"] for s in supports}
            if len(effective) != 1 or next(iter(effective)) != canonical["effective_at"]:
                raise LearningError("equivalent support dates differ")
            self._guards(canonical, supports)
            # Withdraw descendants of every changed revision before restoring
            # the canonical row. Their old parent revisions cannot remain live.
            withdraw(self.ledger, claim_ids=[m["claim_id"] for m in members], project=project)
            for member in members:
                current = self.inspect(member["claim_id"])
                revision = current["revision"] + 1
                status = "active" if member["claim_id"] == canonical["claim_id"] else "merged"
                self.conn.execute(
                    "UPDATE learning_claims SET "
                    "status=?,revision=?,observed_at=?,updated_at=? WHERE claim_id=?",
                    (
                        status,
                        revision,
                        max(s["observed_at"] for s in supports)
                        if status == "active"
                        else member["observed_at"],
                        time.time(),
                        member["claim_id"],
                    ),
                )
                self._write_supports(
                    member["claim_id"],
                    revision,
                    supports
                    if status == "active"
                    else self.validate_supports(member["supports"], project, root),
                )
                self._snapshot(
                    member["claim_id"],
                    "merge_canonical" if status == "active" else "merge_predecessor",
                )
                if status == "merged":
                    self.conn.execute(
                        "INSERT OR REPLACE INTO learning_claim_edges VALUES (?,?,?,'equivalent')",
                        (member["claim_id"], revision, canonical["claim_id"]),
                    )
            return self.inspect(canonical["claim_id"])

    def _current(self, claim, session_id):
        if claim["status"] != "active" or not self.ledger.read_allowed(
            claim["project"], claim["root"], session_id
        ):
            return False
        try:
            self.validate_supports(
                claim["supports"], claim["project"], claim["root"], session_id=session_id
            )
            for parent in claim["parents"]:
                if parent["relation"] != "support":
                    continue
                row = self.conn.execute(
                    "SELECT status,revision FROM learning_claims WHERE claim_id=?",
                    (parent["parent_id"],),
                ).fetchone()
                if (
                    not row
                    or row["status"] != "active"
                    or row["revision"] != parent["parent_revision"]
                ):
                    return False
        except LearningError:
            return False
        return True

    def context(self, project, root, query="", *, session_id="", limit_chars=6000):
        # One transaction prevents a concurrent revocation publishing stale context.
        with db.write_tx(self.conn):
            return self._context(
                project, root, query, session_id=session_id, limit_chars=limit_chars
            )

    def _context(self, project, root, query="", *, session_id="", limit_chars=6000):
        _text(project, "project")
        _text(root, "root")
        _text(query, "context query", 8000, empty=True)
        if type(limit_chars) is not int or not 0 <= limit_chars <= self.settings.max_context_chars:
            raise LearningError("invalid context size")
        empty = {"context": "", "claims": [], "conflicts": [], "deferred": [], "reason": None}
        if not self.ledger.read_allowed(project, root, session_id):
            return {**empty, "reason": "memory use disabled"}
        items = []
        for note in self.ledger.semantic_notes(project, root, session_id=session_id):
            if not self.ledger.read_allowed(project, note["root"], session_id):
                continue
            refs = self.note_supports(note)
            try:
                self.validate_supports(refs, project, root, session_id=session_id)
            except LearningError:
                continue
            items.append(
                {
                    "claim_id": "note:" + note["note_id"],
                    "revision": note["revision"],
                    "statement": note["statement"],
                    "kind": "explicit_statement"
                    if note["category"] in {"preference", "workflow", "decision"}
                    else "observation",
                    "category": note["category"],
                    "scope": note["scope"],
                    "root": note["root"],
                    "observed_at": note["observed_at"],
                    "effective_at": note["effective_at"],
                    "supports": refs,
                }
            )
        for row in self.conn.execute(
            "SELECT claim_id FROM learning_claims WHERE project=? AND "
            "status='active' AND (root=? OR scope='general') ORDER BY claim_id",
            (project, root),
        ):
            claim = self.inspect(row["claim_id"])
            if self._current(claim, session_id):
                claim["support_dates"] = sorted(
                    {
                        source_date(
                            dict(
                                self.conn.execute(
                                    "SELECT observed_at FROM learning_sources WHERE id=?",
                                    (ref["source_id"],),
                                ).fetchone()
                            )
                        )
                        for ref in claim["supports"]
                    }
                )
                items.append(
                    {
                        k: claim[k]
                        for k in (
                            "claim_id",
                            "revision",
                            "statement",
                            "kind",
                            "category",
                            "scope",
                            "root",
                            "observed_at",
                            "effective_at",
                            "supports",
                            "support_dates",
                        )
                    }
                )
        terms = _terms(query)
        if query.strip():
            items = [item for item in items if terms & _terms(item["statement"])]
        items.sort(
            key=lambda c: (
                -len(terms & set(re.findall(r"[\w.-]+", c["statement"].casefold()))),
                c["kind"] == "inference",
                c["claim_id"],
            )
        )
        selected, lines, omitted = [], [], []
        # Equivalent content shares one bullet, while retaining every exact edge.
        merged = {}
        for item in items:
            key = (
                item["statement"].casefold(),
                item["kind"],
                item["category"],
                item["scope"],
                item["root"],
                item["observed_at"],
                item["effective_at"],
            )
            if key in merged:
                prior = merged[key]
                prior.setdefault("equivalent_claims", []).append(
                    {"claim_id": item["claim_id"], "revision": item["revision"]}
                )
                known = {s["support_id"] for s in prior["supports"]}
                prior["supports"].extend(
                    s for s in item["supports"] if s["support_id"] not in known
                )
            else:
                merged[key] = item
        conflicts = []
        for row in self.ledger._conflict_rows(project, root):
            conflict = self.ledger._inspect_conflict(row)
            if conflict["state"] != "open":
                continue
            statement = conflict.get("proposed_statement") or ""
            if query.strip() and not (terms & _terms(statement)):
                continue
            dates = []
            for ref in conflict.get("evidence", []):
                source = self.conn.execute(
                    "SELECT observed_at FROM learning_sources WHERE id=?", (ref["source_id"],)
                ).fetchone()
                if source:
                    dates.append(source_date(dict(source)))
            conflict["observed_at"] = (
                max(dates)
                if dates
                else datetime.fromtimestamp(conflict["created_at"], tz=timezone.utc)
                .date()
                .isoformat()
            )
            conflicts.append(conflict)
            if len(conflicts) >= 16:
                break
        for row in self.conn.execute(
            "SELECT claim_id,revision,statement,observed_at,semantic_relation FROM learning_claims "
            "WHERE project=? AND root=? AND status='conflicted' ORDER BY updated_at DESC LIMIT 16",
            (project, root),
        ):
            if query.strip() and not (terms & _terms(row["statement"])):
                continue
            conflicts.append(dict(row))
        blocked_notes = set()
        for conflict in conflicts:
            label = conflict.get(
                "claim_id", conflict.get("conflict_id", conflict.get("id", "unknown"))
            )
            date = conflict.get("observed_at", "")
            statement = conflict.get("statement", conflict.get("proposed_statement"))
            affected = conflict.get("note_id", conflict.get("claim_id", "unknown"))
            revision = conflict.get("current_revision", conflict.get("revision", "unknown"))
            if statement:
                line = (
                    f"- [unresolved/not admitted {date}] {statement} "
                    f"[conflict:{label} affected:{affected} revision:{revision}]"
                )
            else:
                line = (
                    f"- Unresolved memory conflict {label} {date} affects {affected} "
                    f"revision:{revision}; details unavailable. Inspect before using guidance."
                )
            if statement and len("\n".join([*lines, line])) <= limit_chars:
                lines.append(line)
            else:
                if affected:
                    blocked_notes.add(affected)
                omitted.append(
                    {"conflict_id": label, "reason": "complete conflict does not fit context"}
                )
        for item in merged.values():
            if item["claim_id"] in blocked_notes or any(
                s["note_id"] in blocked_notes for s in item["supports"]
            ):
                omitted.append(
                    {"claim_id": item["claim_id"], "reason": "unresolved conflict omitted"}
                )
                continue
            refs = ",".join(s["support_id"] for s in item["supports"])
            equivalents = ",".join(
                e["claim_id"] + "@" + str(e["revision"]) for e in item.get("equivalent_claims", [])
            )
            dates = ",".join(item.get("support_dates", [item["observed_at"]]))
            line = (
                f"- [{item['kind']} {item['category']} {item['root']} {dates}"
                + (f" effective:{item['effective_at']}" if item["effective_at"] else "")
                + f"] {item['statement']} [claim:{item['claim_id']} revision:{item['revision']}"
                + f" supports:{refs}"
                + (f" equivalent:{equivalents}" if equivalents else "")
                + "]"
            )
            if (
                len(selected) < self.settings.max_context_claims
                and len("\n".join([*lines, line])) <= limit_chars
            ):
                lines.append(line)
                selected.append(item)
            else:
                omitted.append({"claim_id": item["claim_id"], "reason": "context limit"})
        result = {
            **empty,
            "context": "\n".join(lines),
            "claims": selected,
            "conflicts": conflicts,
            "deferred": omitted,
        }
        cache_id = _hash(_json([project, root, session_id, query, limit_chars]))
        # At most one recent context per caller/root and 256 globally. Cached
        # bytes are diagnostic only: every serve rebuilds under support validation.
        self.conn.execute(
            "DELETE FROM learning_context_cache WHERE updated_at<? OR "
            "(project=? AND root=? AND session_id=?)",
            (time.time() - 600, project, root, session_id),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO learning_context_cache VALUES (?,?,?,?,?,?)",
            (cache_id, project, root, session_id, _json(result), time.time()),
        )
        self.conn.execute(
            "DELETE FROM learning_context_cache WHERE cache_id NOT IN "
            "(SELECT cache_id FROM learning_context_cache "
            "ORDER BY updated_at DESC,cache_id LIMIT 256)"
        )
        return result

    def drafts(self, project, root, *, session_id=""):
        with db.write_tx(self.conn):
            return self._drafts(project, root, session_id=session_id)

    def _drafts(self, project, root, *, session_id=""):
        if not self.ledger.read_allowed(project, root, session_id):
            return []
        result = []
        for row in self.conn.execute(
            "SELECT * FROM learning_drafts WHERE project=? AND root=? ORDER BY created_at,draft_id",
            (project, root),
        ):
            item = dict(row)
            if item["status"] in {"review_required", "reviewed"}:
                refs = [
                    _reference(r)
                    for r in self.conn.execute(
                        "SELECT * FROM learning_draft_supports WHERE draft_id=?",
                        (item["draft_id"],),
                    )
                ]
                try:
                    self.validate_supports(refs, project, root, session_id=session_id)
                    for run in json.loads(item["qualification"]):
                        source = self.conn.execute(
                            "SELECT status,sha256 FROM learning_sources WHERE id=?",
                            (run["input_source_id"],),
                        ).fetchone()
                        if (
                            not source
                            or source["status"] != "eligible"
                            or source["sha256"] != run["input_hash"]
                        ):
                            raise LearningError("native procedure input is unavailable")
                except LearningError:
                    item.update(status="withdrawn", document="", qualification="[]")
            result.append(item)
        return result

    def qualify_drafts(self, project, root, *, job_id=None, lease_token=None):
        """Only independently observed native action+successful outcome can qualify.

        Conservative false negatives are intentional: identical copied outcomes,
        incomplete call inputs, assistant assertions and mutation commands abstain.
        """
        groups = {}
        for note in self.ledger.current_notes(project, root):
            if note["category"] != "proven_fix":
                continue
            try:
                supports = self.validate_supports(self.note_supports(note), project, root)
            except LearningError:
                continue
            revision = self.conn.execute(
                "SELECT snapshot FROM learning_revisions WHERE note_id=? AND revision=?",
                (note["note_id"], note["revision"]),
            ).fetchone()
            proof = json.loads(revision[0]).get("details", {}).get("proof", {}) if revision else {}
            after_ids = {ref.get("source_id") for ref in proof.get("after", [])}
            for ref in supports:
                meta = ref["metadata"]
                if (
                    ref["source_id"] not in after_ids
                    or ref["role"] != "tool"
                    or meta.get("kind") != "tool_result"
                    or not meta.get("call_id")
                    or not meta.get("command")
                    or meta.get("is_error") is True
                ):
                    continue
                # Recompute success from captured output. Metadata alone is not evidence.
                code = None
                try:
                    output = json.loads(ref["quote"])
                    if isinstance(output, dict):
                        code = output.get("exit_code", output.get("exitCode"))
                        if output.get("isError") is True or output.get("is_error") is True:
                            continue
                except ValueError:
                    match = re.search(r"Process exited with code (-?\d+)", ref["quote"])
                    if match:
                        code = int(match.group(1))
                if type(code) is not int or code != 0:
                    continue
                command = meta["command"]
                if not _readonly_command(command):
                    continue
                native_input = None
                for source in self.conn.execute(
                    "SELECT * FROM learning_sources WHERE project=? AND root=? "
                    "AND session_id=? AND role='assistant' AND status='eligible'",
                    (project, root, ref["session_id"]),
                ):
                    native = json.loads(source["metadata"])
                    if (
                        native.get("kind") == "tool_call"
                        and native.get("call_id") == meta["call_id"]
                    ):
                        raw = source["content"]
                        try:
                            parsed = json.loads(raw)
                            raw = (
                                parsed.get("cmd", parsed.get("command", ""))
                                if isinstance(parsed, dict)
                                else ""
                            )
                        except ValueError:
                            pass
                        if raw == command and native.get("tool_name") == meta.get("tool_name"):
                            native_input = source
                            break
                if native_input is None:
                    continue
                tool = meta.get("tool_name")
                if tool not in {"exec_command", "functions.exec_command", "Bash"}:
                    continue
                record_family = meta.get("record_hash", ref["source_hash"])
                run = {
                    "note_id": note["note_id"],
                    "revision": note["revision"],
                    "session_id": ref["session_id"],
                    "family": record_family,
                    "outcome_hash": ref["source_hash"],
                    "call_id": meta["call_id"],
                    "input_source_id": native_input["id"],
                    "input_hash": native_input["sha256"],
                    "input_start": 0,
                    "input_end": len(native_input["content"]),
                    "input_quote": native_input["content"],
                    "tool": tool,
                    "command": command,
                    "applicability": note["statement"],
                    "supports": [self.compact_support(s) for s in supports],
                }
                groups.setdefault((tool, command), []).append(run)
        created = []
        for (tool, command), runs in sorted(groups.items()):
            unique, sessions, families, outcomes = [], set(), set(), set()
            for run in runs:
                if (
                    run["session_id"] in sessions
                    or run["family"] in families
                    or run["outcome_hash"] in outcomes
                ):
                    continue
                unique.append(run)
                sessions.add(run["session_id"])
                families.add(run["family"])
                outcomes.add(run["outcome_hash"])
            if len(unique) < self.settings.min_workflow_runs:
                continue
            unique = unique[: self.settings.min_workflow_runs]
            draft_id = _hash(
                _json(
                    [project, root, tool, command, [(r["note_id"], r["revision"]) for r in unique]]
                )
            )
            refs = {ref["support_id"]: ref for run in unique for ref in run["supports"]}
            document = {
                "format": "noblivion-procedure-v1",
                "name": "verified-check-" + draft_id[:12],
                "review_state": "review_required",
                "active": False,
                "risk_tier": "read_only",
                "declared_tools": [tool],
                "applicability": [r["applicability"] for r in unique],
                "steps": [{"tool": tool, "input": command, "expected": "native exit code 0"}],
                "support_ids": sorted(refs),
            }
            validate_draft(document)
            with db.write_tx(self.conn):
                if job_id is not None:
                    self.ledger._lease(job_id, lease_token)
                for run in unique:
                    self.validate_supports(run["supports"], project, root)
                    source = self.conn.execute(
                        "SELECT status,sha256 FROM learning_sources WHERE id=?",
                        (run["input_source_id"],),
                    ).fetchone()
                    if (
                        not source
                        or source["status"] != "eligible"
                        or source["sha256"] != run["input_hash"]
                    ):
                        raise SemanticSnapshotChanged("native workflow action changed")
                now = time.time()
                self.conn.execute(
                    "INSERT OR IGNORE INTO learning_drafts VALUES "
                    "(?,?,?,'review_required',0,?,?,?,?)",
                    (draft_id, project, root, _json(document), _json(unique), now, now),
                )
                for ref in refs.values():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO learning_draft_supports VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            draft_id,
                            ref["support_id"],
                            ref["note_id"],
                            ref["revision"],
                            ref["source_id"],
                            ref["source_hash"],
                            ref["start"],
                            ref["end"],
                            ref["quote"],
                        ),
                    )
            created.append(draft_id)
            if len(created) >= self.settings.max_drafts:
                break
        return created


def _terms(text):
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "do",
        "for",
        "from",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "their",
        "then",
        "this",
        "to",
        "use",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
    return {t for t in re.findall(r"[\w.-]+", text.casefold()) if len(t) > 1 and t not in stopwords}


def _readonly_command(command):
    """A positive option grammar; executable verification commands are excluded."""
    if not isinstance(command, str) or len(command) > 2000 or re.search(r"[\n;|&<>`$]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    grammars = {
        ("git", "status"): {
            "--short",
            "-s",
            "--branch",
            "-b",
            "--porcelain",
            "--untracked-files=no",
            "--untracked-files=normal",
            "--untracked-files=all",
        },
        ("git", "diff"): {
            "--stat",
            "--name-only",
            "--name-status",
            "--numstat",
            "--check",
            "--cached",
            "--staged",
            "--no-ext-diff",
            "--no-textconv",
        },
        ("git", "log"): {"--oneline", "--no-decorate", "--all"},
        ("rg",): {
            "-n",
            "--line-number",
            "-i",
            "--ignore-case",
            "-s",
            "--case-sensitive",
            "-F",
            "--fixed-strings",
            "-w",
            "--word-regexp",
            "-l",
            "--files-with-matches",
            "--files",
            "-c",
            "--count",
            "--hidden",
            "--no-ignore",
        },
    }
    for prefix, flags in grammars.items():
        if tuple(words[: len(prefix)]) != prefix:
            continue
        positional = False
        for word in words[len(prefix) :]:
            if word == "--":
                positional = True
            elif not positional and word.startswith("-") and word not in flags:
                return False
        return True
    return False


def validate_draft(document):
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict) or set(document) != {
        "format",
        "name",
        "review_state",
        "active",
        "risk_tier",
        "declared_tools",
        "applicability",
        "steps",
        "support_ids",
    }:
        raise LearningError("invalid procedure template")
    _safe(_json(document))
    if (
        document["format"] != "noblivion-procedure-v1"
        or document["active"] is not False
        or document["review_state"] != "review_required"
        or document["risk_tier"] != "read_only"
        or not re.fullmatch(r"verified-check-[a-f0-9]{12}", document["name"])
        or not isinstance(document["applicability"], list)
        or not 2 <= len(document["applicability"]) <= 8
        or not isinstance(document["steps"], list)
        or len(document["steps"]) != 1
    ):
        raise LearningError("procedure must be inert and require review")
    tools = document["declared_tools"]
    if (
        not isinstance(tools, list)
        or len(tools) != 1
        or tools[0] not in {"exec_command", "functions.exec_command", "Bash"}
    ):
        raise LearningError("undeclared procedure tool")
    for step in document["steps"]:
        if (
            not isinstance(step, dict)
            or set(step) != {"tool", "input", "expected"}
            or step["tool"] not in tools
            or not _readonly_command(step["input"])
            or step["expected"] != "native exit code 0"
        ):
            raise LearningError("procedure fails tool or risk lint")
    if (
        not isinstance(document["support_ids"], list)
        or not document["support_ids"]
        or len(document["support_ids"]) != len(set(document["support_ids"]))
    ):
        raise LearningError("procedure lacks independent exact support")
    return document


def withdraw(
    ledger, *, note_ids=(), source_ids=(), claim_ids=(), project=None, root=None, forget=False
):
    """Called inside the parent's mutation transaction; erase all derivative text on forget."""
    conn = ledger.conn
    claims, drafts = set(claim_ids), set()
    for field, identifiers in (("note_id", note_ids), ("source_id", source_ids)):
        for ident in identifiers:
            claims.update(
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT claim_id FROM learning_claim_supports WHERE {field}=?",
                    (ident,),
                )
            )
            drafts.update(
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT draft_id FROM learning_draft_supports WHERE {field}=?",
                    (ident,),
                )
            )
    if root is not None:
        claims.update(
            r[0]
            for r in conn.execute(
                "SELECT claim_id FROM learning_claims WHERE project=? AND root=?", (project, root)
            )
        )
        drafts.update(
            r[0]
            for r in conn.execute(
                "SELECT draft_id FROM learning_drafts WHERE project=? AND root=?", (project, root)
            )
        )
    # Inputs are exact source-linked native calls, not note evidence. Include them in revocation.
    if source_ids:
        revoked = set(source_ids)
        for row in conn.execute("SELECT draft_id,qualification FROM learning_drafts"):
            if any(
                run.get("input_source_id") in revoked for run in json.loads(row["qualification"])
            ):
                drafts.add(row["draft_id"])
    queue = list(claims)
    while queue:
        ident = queue.pop()
        for row in conn.execute(
            "SELECT child_id FROM learning_claim_edges WHERE parent_id=?", (ident,)
        ):
            if row[0] not in claims:
                claims.add(row[0])
                queue.append(row[0])
    projects = {project} if project is not None else set()
    for ident in claims:
        row = conn.execute("SELECT * FROM learning_claims WHERE claim_id=?", (ident,)).fetchone()
        projects.add(row["project"])
        if forget:
            conn.execute("DELETE FROM learning_claim_revisions WHERE claim_id=?", (ident,))
            conn.execute("DELETE FROM learning_claim_supports WHERE claim_id=?", (ident,))
            conn.execute(
                "UPDATE learning_claims SET "
                "status='forgotten',statement='',semantic_relation='erased',"
                "updated_at=? WHERE claim_id=?",
                (time.time(), ident),
            )
        elif row["status"] not in {"withdrawn", "forgotten"}:
            conn.execute(
                "UPDATE learning_claims SET "
                "status='withdrawn',revision=revision+1,updated_at=? WHERE claim_id=?",
                (time.time(), ident),
            )
            item = dict(row)
            item.update(status="withdrawn", revision=row["revision"] + 1)
            conn.execute(
                "INSERT INTO learning_claim_revisions VALUES (?,?,? ,?,?)",
                (ident, item["revision"], "withdraw", _json(item), time.time()),
            )
    for ident in drafts:
        row = conn.execute(
            "SELECT project FROM learning_drafts WHERE draft_id=?", (ident,)
        ).fetchone()
        projects.add(row["project"])
        if forget:
            conn.execute("DELETE FROM learning_draft_supports WHERE draft_id=?", (ident,))
            conn.execute(
                "UPDATE learning_drafts SET "
                "status='forgotten',document='',qualification='[]',updated_at=? "
                "WHERE draft_id=?",
                (time.time(), ident),
            )
        else:
            conn.execute(
                "UPDATE learning_drafts SET status='withdrawn',updated_at=? WHERE "
                "draft_id=? AND status<>'forgotten'",
                (time.time(), ident),
            )
    for namespace in projects:
        conn.execute("DELETE FROM learning_context_cache WHERE project=?", (namespace,))


def derivative_status(ledger, project=None, root=None):
    where, params = [], []
    for name, value in (("project", project), ("root", root)):
        if value is not None:
            where.append(name + "=?")
            params.append(value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    result = {}
    for name, table in (("claims", "learning_claims"), ("drafts", "learning_drafts")):
        result[name] = {
            r[0]: r[1]
            for r in ledger.conn.execute(
                "SELECT status,count(*) FROM " + table + clause + " GROUP BY status", params
            )
        }
    result["jobs"] = []
    for row in ledger.conn.execute(
        "SELECT id,kind,status,attempts,last_error,progress FROM learning_jobs"
        + clause
        + (" AND" if clause else " WHERE")
        + " kind<>'extract' ORDER BY created_at",
        params,
    ):
        item = dict(row)
        progress = json.loads(item.pop("progress"))
        item["deferred"] = progress.get("derivation", {}).get("deferred", [])
        result["jobs"].append(item)
    return result
