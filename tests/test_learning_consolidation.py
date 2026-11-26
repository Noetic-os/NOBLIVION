# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real persistence, support admission, lifecycle and isolated serving proofs."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from noblivion import db
from noblivion.learning import (
    FOLD_REASON,
    BudgetLimits,
    LearningError,
    LearningLedger,
    SemanticSnapshotChanged,
)
from noblivion.learning_consolidation import (
    ConsolidationLedger,
    ConsolidationSettings,
    validate_draft,
)
from noblivion.learning_synthesis import run_derivation
from noblivion.learning_values import ProposalRejected
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings


@pytest.fixture
def ledgers(tmp_path):
    conn = db.open_db(tmp_path / "store.db", create=True)
    ledger = LearningLedger(conn)
    yield ledger, ConsolidationLedger(ledger)
    conn.close()


def learn(
    ledger,
    n,
    statement,
    *,
    category="fact",
    scope="project",
    root="demo",
    session=None,
    role="user",
    metadata=None,
    extra=None,
    observed_at="2026-10-13T10:00:00Z",
    content=None,
):
    content = content or statement
    payload = {
        "batch_id": f"batch-{n}",
        "project": "test",
        "root": root,
        "session_id": session or f"s-{n}",
        "source_client": "codex",
        "records": [
            {
                "source_id": f"source-{n}",
                "role": role,
                "content": content,
                "source_hash": hashlib.sha256(content.encode()).hexdigest(),
                "observed_at": observed_at,
                "metadata": metadata or {"direct_user": role == "user"},
            }
        ],
    }
    ledger.capture_batch(payload)
    job = ledger.claim("fixture")
    decision = {
        "action": "add",
        "logical_key": f"fact-{n}",
        "title": f"Fact {n}",
        "category": category,
        "statement": statement,
        "scope": scope,
        "observed_at": observed_at[:10],
        "effective_at": None,
        "evidence": [
            {
                "source_id": f"source-{n}",
                "source_hash": payload["records"][0]["source_hash"],
                "start": 0,
                "end": len(statement),
                "quote": statement,
            }
        ],
        **(extra or {}),
    }
    result = ledger.finish(job["id"], job["lease_token"], [decision])[0]
    return next(n for n in ledger.current_notes("test", root) if n["note_id"] == result["note_id"])


def proposal(store, notes, **changes):
    return {
        "statement": "The cobalt otter service uses loopback port 4317.",
        "kind": "inference",
        "category": "fact",
        "scope": "project",
        "supports": [ref for note in notes for ref in store.note_supports(note)],
        **changes,
    }


def verdict(claim, **changes):
    return {
        "claim_id": claim["claim_id"],
        "relation": "supported",
        "complete": True,
        "support_ids": [s["support_id"] for s in claim["supports"]],
        "conflicts": [],
        **changes,
    }


def seeded(ledgers):
    ledger, store = ledgers
    a = learn(ledger, 1, "The cobalt otter service listens on port 4317.")
    b = learn(ledger, 2, "The cobalt otter service binds only to 127.0.0.1.")
    claim = store.stage(proposal(store, [a, b]), "test", "demo")
    return ledger, store, a, b, claim


def test_exact_support_and_semantic_admission_are_separate(ledgers):
    ledger, store, a, b, claim = seeded(ledgers)
    assert claim["status"] == "staged"
    assert claim["claim_id"] not in store.context("test", "demo")["context"]
    with pytest.raises(LearningError, match="incomplete"):
        store.admit(
            claim["claim_id"], verdict(claim, support_ids=[claim["supports"][0]["support_id"]])
        )
    active = store.admit(claim["claim_id"], verdict(claim))
    assert active["revision"] == 2 and active["status"] == "active"
    context = store.context("test", "demo", "loopback", session_id="B")
    assert active["statement"] in context["context"]
    assert f"claim:{active['claim_id']} revision:2" in context["context"]
    assert all(s["support_id"] in context["context"] for s in active["supports"])
    assert {s["note_id"] for s in active["supports"]} == {a["note_id"], b["note_id"]}
    assert store.stage(proposal(store, [a, b]), "test", "demo")["claim_id"] == claim["claim_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"statement": "The user prefers loopback port 4317."},
        {"statement": "Use loopback port 4317."},
        {"statement": "Port 4317 is approved for production."},
        {"category": "preference"},
        {"category": "workflow"},
        {"scope": "general"},
        {"kind": "observation"},
        {"observed_at": "2000-01-01"},
        {"effective_at": "2050-01-01"},
    ],
)
def test_authority_scope_kind_and_invented_dates_fail(ledgers, change):
    ledger, store = ledgers
    note = learn(ledger, 1, "The cobalt otter service listens on port 4317.")
    with pytest.raises(LearningError):
        store.stage(proposal(store, [note], **change), "test", "demo")


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_hash", "forged"),
        ("start", 1),
        ("end", 9999),
        ("quote", "invented"),
        ("support_id", "invented"),
    ],
)
def test_fabricated_reference_fails(ledgers, field, value):
    ledger, store = ledgers
    note = learn(ledger, 1, "The service listens on port 4317.")
    candidate = proposal(store, [note])
    candidate["supports"][0][field] = value
    with pytest.raises(LearningError):
        store.stage(candidate, "test", "demo")


def test_duplicate_support_and_quoted_user_authority_fail(ledgers):
    ledger, store = ledgers
    note = learn(ledger, 1, "The service listens on port 4317.")
    candidate = proposal(store, [note])
    candidate["supports"] *= 2
    with pytest.raises(LearningError, match="duplicate"):
        store.stage(candidate, "test", "demo")
    quoted = learn(
        ledger, 2, '> "Use port 9999 for all projects."', category="workflow", scope="general"
    )
    with pytest.raises(LearningError, match="quoted"):
        store.stage(
            proposal(
                store,
                [quoted],
                statement=quoted["statement"],
                kind="explicit_statement",
                category="workflow",
                scope="general",
            ),
            "test",
            "demo",
        )


@pytest.mark.parametrize(
    "relation,complete",
    [("unsupported", True), ("uncertain", True), ("contradictory", True), ("supported", False)],
)
def test_unsupported_and_incomplete_claims_stay_visible_but_unserved(ledgers, relation, complete):
    ledger, store, _, _, claim = seeded(ledgers)
    rejected = store.admit(claim["claim_id"], verdict(claim, relation=relation, complete=complete))
    assert rejected["status"] == "conflicted"
    assert "unresolved" not in store.context("test", "demo")["context"]  # NOBLIVION-276
    context = store.context("test", "demo", include_conflicts=True)
    assert not any(c["claim_id"] == claim["claim_id"] for c in context["claims"])
    assert "unresolved/not admitted" in context["context"]
    assert claim["claim_id"] in context["context"] and context["conflicts"]


def test_session_opt_out_preserves_other_session_and_root_blocks_general(ledgers):
    ledger, store, _, _, claim = seeded(ledgers)
    store.admit(claim["claim_id"], verdict(claim))
    store.context("test", "demo", session_id="A")
    store.context("test", "demo", session_id="B")
    ledger.set_controls("test", "demo", "A", use=False)
    assert store.context("test", "demo", session_id="A")["context"] == ""
    assert claim["statement"] in store.context("test", "demo", session_id="B")["context"]
    assert store.inspect(claim["claim_id"])["status"] == "active"
    general = learn(
        ledger,
        3,
        "Use short updates across all projects.",
        category="workflow",
        scope="general",
        root="elsewhere",
    )
    assert general["statement"] in store.context("test", "demo", session_id="B")["context"]
    ledger.set_controls("test", "elsewhere", use=False)
    assert general["statement"] not in store.context("test", "demo", session_id="B")["context"]
    ledger.set_controls("test", "demo", use=False)
    assert store.context("test", "demo", session_id="B")["context"] == ""


def test_edit_withdraws_transitive_claims_and_caches_before_return(ledgers):
    ledger, store, a, _, claim = seeded(ledgers)
    active = store.admit(claim["claim_id"], verdict(claim))
    child = store.stage(
        proposal(
            store,
            [a],
            statement="The cobalt otter has a local service port.",
            supports=active["supports"],
            parents=[active["claim_id"]],
        ),
        "test",
        "demo",
    )
    store.admit(child["claim_id"], verdict(child))
    store.context("test", "demo", session_id="B")
    ledger.edit(a["note_id"], a["revision"], title="Edited title")
    assert store.inspect(claim["claim_id"])["status"] == "withdrawn"
    assert store.inspect(child["claim_id"])["status"] == "withdrawn"
    assert ledger.conn.execute("SELECT count(*) FROM learning_context_cache").fetchone()[0] == 0
    assert active["statement"] not in store.context("test", "demo")["context"]


def test_forget_erases_derivative_text_revisions_progress_and_leaves_unaffected_fact(ledgers):
    ledger, store, a, b, claim = seeded(ledgers)
    store.admit(claim["claim_id"], verdict(claim))
    derive_id = ledger.enqueue_derivation("test", "demo", "B")
    job = ledger.claim("deriver")
    ledger.checkpoint(job["id"], job["lease_token"], {"quoted_payload": claim["statement"]})
    store.context("test", "demo", session_id="B")
    ledger.forget(a["note_id"])
    item = store.inspect(claim["claim_id"])
    assert (
        item["status"] == "forgotten"
        and item["statement"] == ""
        and item["revisions"] == []
        and item["supports"] == []
    )
    assert (
        ledger.conn.execute(
            "SELECT progress FROM learning_jobs WHERE id=?", (derive_id,)
        ).fetchone()[0]
        == "{}"
    )
    assert ledger.conn.execute("SELECT count(*) FROM learning_context_cache").fetchone()[0] == 0
    assert b["statement"] in store.context("test", "demo")["context"]


def test_missing_or_stale_support_never_serves(ledgers):
    ledger, store, a, _, claim = seeded(ledgers)
    store.admit(claim["claim_id"], verdict(claim))
    ledger.conn.execute(
        "UPDATE learning_sources SET status='excluded' WHERE id=?", (a["evidence"][0]["source_id"],)
    )
    assert claim["statement"] not in store.context("test", "demo")["context"]


def test_limits_invalid_and_no_unverified_drafts(ledgers):
    ledger, store, _, _, _ = seeded(ledgers)
    with pytest.raises(LearningError):
        ConsolidationSettings(max_supports=999)
    with pytest.raises(LearningError):
        store.context("test", "demo", limit_chars=16001)
    assert store.qualify_drafts("test", "demo") == []
    with pytest.raises(LearningError):
        validate_draft({"active": True})


class ScriptedProvider:
    model = "fixed-test-model"

    def __init__(self, *, support="supported", fail_second=False):
        self.calls = 0
        self.support = support
        self.fail_second = fail_second

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        self.calls += 1
        data = json.loads(messages[-1]["content"])
        if "notes" in data:
            ids = [s["support_id"] for n in data["notes"] for s in n["supports"]]
            return ProviderResponse(
                json.dumps(
                    {
                        "claims": [
                            {
                                "statement": "The cobalt otter service uses loopback port 4317.",
                                "kind": "inference",
                                "category": "fact",
                                "scope": "project",
                                "support_ids": ids,
                            }
                        ]
                    }
                ),
                100,
            )
        if self.fail_second:
            self.fail_second = False
            raise RuntimeError("injected provider outage")
        claim = data["claim"]
        return ProviderResponse(
            json.dumps({"verdicts": [verdict(claim, relation=self.support)]}), 100
        )


def test_derivation_restarts_after_proposal_checkpoint_without_repeating_proposal(ledgers):
    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = ScriptedProvider(fail_second=True)
    worker = LearningWorker(
        ledger,
        provider,
        WorkerSettings(retry_delay=0.01),
        BudgetLimits(max_calls=10, max_tokens=500000, max_seconds=1000),
    )
    first = worker.run_once()
    assert first["status"] == "pending" or first["status"] == "failed"
    assert provider.calls == 2
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    result = worker.run_once()
    assert result["status"] == "done" and provider.calls == 3
    assert store.context("test", "demo")["claims"]
    assert ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == 3


def test_relevance_floor_long_query_and_bounded_caches(ledgers):
    ledger, store, _, _, claim = seeded(ledgers)
    store.admit(claim["claim_id"], verdict(claim))
    assert store.context("test", "demo", "Tell me about banana orchards")["context"] == ""
    assert "cobalt" in store.context("test", "demo", "cobalt otter " + "z" * 6000)["context"]
    for index in range(300):
        store.context("test", "demo", f"query-{index}", session_id=f"caller-{index}")
    assert ledger.conn.execute("SELECT count(*) FROM learning_context_cache").fetchone()[0] == 256
    for index in range(5):
        store.context("test", "demo", f"cobalt {index}", session_id="B")
    assert (
        ledger.conn.execute(
            "SELECT count(*) FROM learning_context_cache WHERE session_id='B'"
        ).fetchone()[0]
        == 1
    )


def test_root_use_off_withdraws_cross_root_support_then_explicit_rebuild(ledgers):
    ledger, store = ledgers
    note = learn(
        ledger,
        1,
        "Use short updates across all projects.",
        category="workflow",
        scope="general",
        root="A",
    )
    candidate = proposal(
        store,
        [note],
        statement=note["statement"],
        kind="explicit_statement",
        category="workflow",
        scope="general",
    )
    claim = store.stage(candidate, "test", "B")
    store.admit(claim["claim_id"], verdict(claim))
    ledger.set_controls("test", "A", use=False)
    assert store.inspect(claim["claim_id"])["status"] == "withdrawn"
    assert not store.context("test", "B")["context"]
    ledger.set_controls("test", "A", use=True)
    restaged = store.stage(candidate, "test", "B")
    assert restaged["status"] == "staged"
    assert restaged["revision"] > claim["revision"]
    restored = store.admit(restaged["claim_id"], verdict(restaged))
    assert restored["status"] == "active"
    assert note["statement"] in store.context("test", "B")["context"]


def test_complete_support_snapshot_fenced_against_uncited_concurrent_writer(ledgers, tmp_path):
    ledger, store, _, _, claim = seeded(ledgers)
    snapshot = sorted([[n["note_id"], n["revision"]] for n in ledger.current_notes("test", "demo")])
    other = db.open_db(tmp_path / "store.db")
    try:
        learn(LearningLedger(other), 3, "The cobalt service now uses port 443.")
    finally:
        other.close()
    with pytest.raises(SemanticSnapshotChanged, match="complete semantic"):
        store.admit(claim["claim_id"], verdict(claim), expected_snapshot=snapshot)
    assert store.inspect(claim["claim_id"])["status"] == "staged"


def test_admission_barrier_rechecks_snapshot_inside_transaction(ledgers, tmp_path, monkeypatch):
    ledger, store, _, _, claim = seeded(ledgers)
    ledger.enqueue_derivation("test", "demo", "B")
    original = ConsolidationLedger.admit
    injected = []

    def barrier(self, *args, **kwargs):
        if not injected:
            other = db.open_db(tmp_path / "store.db")
            try:
                learn(LearningLedger(other), 3, "The cobalt service now uses port 443.")
            finally:
                other.close()
            injected.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConsolidationLedger, "admit", barrier)
    worker = LearningWorker(
        ledger,
        ScriptedProvider(),
        WorkerSettings(),
        BudgetLimits(max_calls=10, max_tokens=500000, max_seconds=1000),
    )
    result = worker.run_once()
    assert result["status"] == "deferred"
    assert store.inspect(claim["claim_id"])["status"] == "staged"


def test_derivation_and_extraction_downstream_enqueue_are_atomic(ledgers, monkeypatch):
    from noblivion.learning_synthesis import _complete

    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    job = ledger.claim("complete")
    original = ledger._enqueue_derivation

    def outage(*args, **kwargs):
        raise RuntimeError("injected downstream storage outage")

    monkeypatch.setattr(ledger, "_enqueue_derivation", outage)
    with pytest.raises(RuntimeError):
        _complete(ledger, job, {}, next_kind="draft")
    assert (
        ledger.conn.execute("SELECT status FROM learning_jobs WHERE id=?", (ident,)).fetchone()[0]
        == "leased"
    )
    with pytest.raises(RuntimeError):
        ledger.finish(ident, job["lease_token"], [], enqueue_consolidation=True)
    assert (
        ledger.conn.execute("SELECT status FROM learning_jobs WHERE id=?", (ident,)).fetchone()[0]
        == "leased"
    )
    monkeypatch.setattr(ledger, "_enqueue_derivation", original)
    _complete(ledger, job, {}, next_kind="draft")
    assert (
        ledger.conn.execute("SELECT count(*) FROM learning_jobs WHERE kind='draft'").fetchone()[0]
        == 1
    )
    same = ledger.enqueue_derivation("test", "demo", "B", kind="draft")
    assert (
        same
        and ledger.conn.execute("SELECT count(*) FROM learning_jobs WHERE kind='draft'").fetchone()[
            0
        ]
        == 1
    )


def test_dated_unresolved_conflict_preserves_both_values_and_affected_revision(ledgers):
    ledger, store = ledgers
    note = learn(ledger, 1, "The cobalt service uses port 80.")
    payload = {
        "batch_id": "conflict-batch",
        "project": "test",
        "root": "demo",
        "session_id": "C",
        "source_client": "codex",
        "records": [
            {
                "source_id": "conflict-source",
                "role": "user",
                "content": "The cobalt service uses port 443.",
                "source_hash": hashlib.sha256(b"The cobalt service uses port 443.").hexdigest(),
                "observed_at": "2026-10-14T12:00:00Z",
                "metadata": {"direct_user": True},
            }
        ],
    }
    ledger.capture_batch(payload)
    job = ledger.claim("conflict")
    text = payload["records"][0]["content"]
    ledger.finish(
        job["id"],
        job["lease_token"],
        [
            {
                "action": "conflict",
                "note_id": note["note_id"],
                "expected_revision": note["revision"],
                "statement": text,
                "observed_at": "2026-10-14",
                "evidence": [
                    {
                        "source_id": "conflict-source",
                        "source_hash": payload["records"][0]["source_hash"],
                        "start": 0,
                        "end": len(text),
                        "quote": text,
                    }
                ],
            }
        ],
    )
    context = store.context("test", "demo", "cobalt port", include_conflicts=True)
    assert "port 80" in context["context"] and "port 443" in context["context"]
    assert "unresolved/not admitted 2026-10-14" in context["context"]
    assert f"affected:{note['note_id']} revision:1" in context["context"]
    tiny = store.context("test", "demo", "cobalt port", limit_chars=100, include_conflicts=True)
    assert "port 80" not in tiny["context"] and tiny["deferred"]


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=/tmp/write",
        "rg --pre=/bin/sh pattern file",
        "ruff check --fix-only",
        "pytest --basetemp=/tmp/erase",
        "git log --exec=sh",
        "git -c core.pager=sh log",
        "rg --replace=x --passthru pattern",
        "git status; echo bad",
        "git diff --ext-diff",
        "git log --format=%(shell)",
    ],
)
def test_readonly_template_rejects_executable_and_joined_write_options(command):
    from noblivion.learning_consolidation import _readonly_command

    assert not _readonly_command(command)


def workflow_run(ledger, n, *, outcome_same=False, session=None, command="git status --short"):
    session = session or f"native-{n}"
    call_id = f"native-call-{n}"
    after = json.dumps(
        {
            "exit_code": 0,
            "output": "Cobalt check is clean " + ("duplicate" if outcome_same else str(n)),
        }
    )
    before = json.dumps({"exit_code": 1, "output": f"Cobalt check failed {n}"})
    action = json.dumps({"cmd": command})
    records = []
    for phase, role, text, metadata in (
        (
            "input",
            "assistant",
            action,
            {"kind": "tool_call", "call_id": call_id, "tool_name": "exec_command"},
        ),
        (
            "before",
            "tool",
            before,
            {
                "kind": "tool_result",
                "call_id": f"failed-{n}",
                "tool_name": "exec_command",
                "command": command,
                "exit_code": 1,
            },
        ),
        (
            "after",
            "tool",
            after,
            {
                "kind": "tool_result",
                "call_id": call_id,
                "tool_name": "exec_command",
                "command": command,
                "exit_code": 0,
            },
        ),
    ):
        records.append(
            {
                "source_id": f"native-{n}-{phase}",
                "role": role,
                "content": text,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "observed_at": "2026-10-13T12:00:00Z",
                "metadata": metadata,
            }
        )
    ledger.capture_batch(
        {
            "batch_id": f"native-batch-{n}",
            "project": "test",
            "root": "demo",
            "session_id": session,
            "source_client": "codex",
            "records": records,
        }
    )
    job = ledger.claim("workflow")
    refs = {
        phase: {
            "source_id": r["source_id"],
            "source_hash": r["source_hash"],
            "start": 0,
            "end": len(r["content"]),
            "quote": r["content"],
        }
        for phase, r in zip(("input", "before", "after"), records, strict=True)
    }
    result = ledger.finish(
        job["id"],
        job["lease_token"],
        [
            {
                "action": "add",
                "logical_key": f"native-fix-{n}",
                "title": "Native check",
                "category": "proven_fix",
                "scope": "project",
                "statement": after,
                "observed_at": "2026-10-13",
                "effective_at": None,
                "evidence": [refs["after"]],
                "proof": {"before": [refs["before"]], "after": [refs["after"]]},
            }
        ],
    )[0]
    return result["note_id"]


def test_independent_native_success_yields_only_validated_inert_draft(ledgers):
    ledger, store = ledgers
    workflow_run(ledger, 1)
    assert store.qualify_drafts("test", "demo") == []
    workflow_run(ledger, 2)
    ids = store.qualify_drafts("test", "demo")
    assert len(ids) == 1
    item = store.drafts("test", "demo")[0]
    document = validate_draft(item["document"])
    assert (
        item["status"] == "review_required" and item["active"] == 0 and document["active"] is False
    )
    assert len(json.loads(item["qualification"])) == 2
    assert store.qualify_drafts("test", "demo") == ids
    assert len(store.drafts("test", "demo")) == 1
    ledger.exclude_source("native-1-input")
    assert store.drafts("test", "demo")[0]["status"] == "withdrawn"


@pytest.mark.parametrize("duplicate", ["outcome", "session", "assistant", "failed", "unsafe_input"])
def test_repeated_unqualified_workflows_never_draft(ledgers, duplicate):
    ledger, store = ledgers
    workflow_run(ledger, 1, outcome_same=duplicate == "outcome")
    workflow_run(
        ledger,
        2,
        outcome_same=duplicate == "outcome",
        session="native-1" if duplicate == "session" else None,
        command="git diff --output=/tmp/write"
        if duplicate == "unsafe_input"
        else "git status --short",
    )
    if duplicate == "assistant":
        ledger.conn.execute(
            "UPDATE learning_sources SET role='assistant' WHERE id='native-2-after'"
        )
    if duplicate == "failed":
        # Metadata claiming success cannot override observed failed output.
        content = json.dumps({"exit_code": 1, "output": "Cobalt check failed 2"})
        ledger.conn.execute(
            "UPDATE learning_sources SET content=?,sha256=? WHERE id='native-2-after'",
            (content, hashlib.sha256(content.encode()).hexdigest()),
        )
    assert store.qualify_drafts("test", "demo") == []


def test_draft_read_time_guard_and_forget_erase_qualification_and_input_snapshots(ledgers):
    ledger, store = ledgers
    first = workflow_run(ledger, 1)
    workflow_run(ledger, 2)
    store.qualify_drafts("test", "demo")
    ledger.conn.execute("UPDATE learning_sources SET status='excluded' WHERE id='native-1-input'")
    assert store.drafts("test", "demo")[0]["document"] == ""
    ledger.forget(first)
    item = store.drafts("test", "demo")[0]
    assert (
        item["status"] == "forgotten" and item["document"] == "" and item["qualification"] == "[]"
    )


def equivalence(ledger, store, first, second, relation="equivalent"):
    return store.reconcile_equivalence(
        second["claim_id"],
        [
            {
                "target_id": first["claim_id"],
                "target_revision": first["revision"],
                "relation": relation,
            }
        ],
        expected_claims=sorted(
            [[first["claim_id"], first["revision"]], [second["claim_id"], second["revision"]]]
        ),
        expected_notes=sorted(
            [[n["note_id"], n["revision"]] for n in ledger.current_notes("test", "demo")]
        ),
    )


def two_supported_paraphrases(ledgers):
    ledger, store = ledgers
    a = learn(ledger, 1, "The cobalt otter service listens at 127.0.0.1:4317.")
    b = learn(
        ledger,
        2,
        "The cobalt otter service has loopback port 4317.",
        observed_at="2026-10-14T10:00:00Z",
    )
    first = store.stage(proposal(store, [a]), "test", "demo")
    first = store.admit(first["claim_id"], verdict(first))
    second = store.stage(
        proposal(store, [b], statement="The cobalt otter service binds only to 127.0.0.1:4317."),
        "test",
        "demo",
    )
    second = store.admit(second["claim_id"], verdict(second))
    return ledger, store, a, b, first, second


def test_supported_paraphrases_merge_canonical_with_all_exact_support_and_history(ledgers):
    ledger, store, a, b, first, second = two_supported_paraphrases(ledgers)
    merged = equivalence(ledger, store, first, second)
    assert merged["claim_id"] == first["claim_id"] and merged["status"] == "active"
    assert merged["observed_at"] == "2026-10-14"
    assert {s["note_id"] for s in merged["supports"]} == {a["note_id"], b["note_id"]}
    assert store.inspect(second["claim_id"])["status"] == "merged"
    assert len(store.inspect(second["claim_id"])["revisions"]) >= 4
    assert merged["parents"][0]["relation"] == "equivalent"
    context = store.context("test", "demo", "cobalt")
    assert len([c for c in context["claims"] if c["kind"] == "inference"]) == 1
    assert "2026-10-13,2026-10-14" in context["context"]
    assert all(s["support_id"] in context["context"] for s in merged["supports"])
    ledger.edit(b["note_id"], b["revision"], title="Corrected source title")
    assert store.inspect(first["claim_id"])["status"] == "withdrawn"
    assert not any(c["kind"] == "inference" for c in store.context("test", "demo")["claims"])


@pytest.mark.parametrize("relation", ["distinct", "uncertain", "contradictory"])
def test_non_equivalence_never_merges_supported_claims(ledgers, relation):
    ledger, store, _, _, first, second = two_supported_paraphrases(ledgers)
    result = equivalence(ledger, store, first, second, relation)
    assert result["claim_id"] == second["claim_id"]
    assert store.inspect(first["claim_id"])["supports"] == first["supports"]
    assert store.inspect(second["claim_id"])["status"] == (
        "conflicted" if relation == "contradictory" else "active"
    )


def test_merge_snapshot_changes_and_forget_of_predecessor_fail_closed(ledgers):
    ledger, store, a, b, first, second = two_supported_paraphrases(ledgers)
    old_snapshot = sorted(
        [[n["note_id"], n["revision"]] for n in ledger.current_notes("test", "demo")]
    )
    note = learn(ledger, 3, "Another service uses port 9090.")
    with pytest.raises(SemanticSnapshotChanged):
        store.reconcile_equivalence(
            second["claim_id"],
            [
                {
                    "target_id": first["claim_id"],
                    "target_revision": first["revision"],
                    "relation": "equivalent",
                }
            ],
            expected_claims=sorted(
                [[first["claim_id"], first["revision"]], [second["claim_id"], second["revision"]]]
            ),
            expected_notes=old_snapshot,
        )
    merged = equivalence(ledger, store, first, second)
    store.context("test", "demo")
    ledger.forget(b["note_id"])
    assert store.inspect(merged["claim_id"])["status"] == "forgotten"
    assert store.inspect(merged["claim_id"])["revisions"] == []
    assert store.inspect(second["claim_id"])["status"] == "forgotten"
    assert note["statement"] in store.context("test", "demo")["context"]


class EquivalenceProvider(ScriptedProvider):
    def complete(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        if "candidate" in task:
            self.calls += 1
            return ProviderResponse(
                json.dumps(
                    {
                        "relations": [
                            {
                                "target_id": c["claim_id"],
                                "target_revision": c["revision"],
                                "relation": "equivalent",
                            }
                            for c in task["targets"]
                        ]
                    }
                ),
                100,
            )
        if "notes" in task:
            self.calls += 1
            ids = [s["support_id"] for n in task["notes"] for s in n["supports"]]
            return ProviderResponse(
                json.dumps(
                    {
                        "claims": [
                            {
                                "statement": (
                                    "The cobalt otter service binds only to 127.0.0.1:4317."
                                ),
                                "kind": "inference",
                                "category": "fact",
                                "scope": "project",
                                "support_ids": ids,
                            }
                        ]
                    }
                ),
                100,
            )
        return super().complete(messages, **kwargs)


def test_worker_equivalence_is_a_distinct_reserved_stage(ledgers):
    ledger, store = ledgers
    note = learn(ledger, 1, "The cobalt otter service listens at 127.0.0.1:4317.")
    claim = store.stage(proposal(store, [note]), "test", "demo")
    store.admit(claim["claim_id"], verdict(claim))
    learn(ledger, 2, "The cobalt otter service has loopback port 4317.")
    ledger.enqueue_derivation("test", "demo", "B")
    provider = EquivalenceProvider()
    result = LearningWorker(
        ledger,
        provider,
        WorkerSettings(),
        BudgetLimits(max_calls=10, max_tokens=500000, max_seconds=1000),
    ).run_once()
    assert result["status"] == "done" and provider.calls == 3
    assert ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == 3
    context = store.context("test", "demo", "cobalt")
    assert len([c for c in context["claims"] if c["kind"] == "inference"]) == 1


def test_context_public_http_cli_controls_and_input_bounds(tmp_path, capsys):
    from urllib.parse import urlencode

    from noblivion import config
    from noblivion.learning_cli import main
    from test_learning import batch
    from test_learning import learn as existing_learn
    from test_store import Running, make_store

    st = make_store(tmp_path)
    run = Running(st)
    root = st.settings.memory_dirs[0].parent.name
    try:
        with st.connection() as conn:
            ledger = LearningLedger(conn)
            payload = batch(1, "For every project, I prefer brief cobalt updates.")
            payload.update(namespace=st.namespace, root=root)
            existing_learn(ledger, payload)
        body = {"project": st.namespace, "root": root, "session_id": "B", "query": "cobalt"}
        status, result, _ = run.request("POST", "/learning/context", body=body)
        assert status == 200 and "brief cobalt" in result["context"] and result["reason"] is None
        query = urlencode(body)
        status, result = run.get("/learning/context?" + query)
        assert status == 200 and result["claims"][0]["supports"][0]["source_id"] == "source-1"
        assert run.request("POST", "/learning/context", body=body, token=False)[0] == 401
        assert (
            run.request("POST", "/learning/context", body={**body, "project": "foreign"})[0] == 400
        )
        assert (
            run.request("POST", "/learning/context", body={**body, "query": "x" * 8001})[0] == 400
        )
        assert (
            run.request("POST", "/learning/context", body={**body, "limit_chars": True})[0] == 400
        )
        assert (
            main(
                [
                    "--database",
                    str(st.data_dir / config.DB_FILE),
                    "context",
                    "--project",
                    st.namespace,
                    "--root=" + root,
                    "--session",
                    "B",
                    "--query",
                    "cobalt",
                ]
            )
            == 0
        )
        assert "brief cobalt" in json.loads(capsys.readouterr().out)["context"]
        run.request(
            "POST",
            "/learning/control",
            body={"project": st.namespace, "root": root, "session_id": "A", "use": False},
        )
        status, blocked, _ = run.request(
            "POST", "/learning/context", body={**body, "session_id": "A"}
        )
        assert status == 200 and blocked["context"] == ""
        assert "brief cobalt" in run.request("POST", "/learning/context", body=body)[1]["context"]
    finally:
        run.stop()


@pytest.mark.parametrize("failure", ["unknown_price", "accounting_outage", "near_limit"])
def test_new_provider_stages_fail_closed_on_accounting_and_budget(ledgers, failure):
    ledger, store, _, _, claim = seeded(ledgers)
    ledger.enqueue_derivation("test", "demo", "B")
    provider = ScriptedProvider()
    if failure == "unknown_price":
        provider.estimate_cost = lambda tokens: None
    elif failure == "accounting_outage":

        def unavailable_accounting(*args, **kwargs):
            from noblivion.learning import BudgetExceeded

            raise BudgetExceeded("persistent accounting unavailable")

        ledger.reserve = unavailable_accounting
    limits = BudgetLimits(
        max_calls=1 if failure == "near_limit" else 10, max_tokens=500000, max_seconds=1000
    )
    result = LearningWorker(ledger, provider, WorkerSettings(), limits).run_once()
    assert result["status"] == "deferred"
    assert provider.calls == (1 if failure == "near_limit" else 0)
    assert store.inspect(claim["claim_id"])["status"] == "staged"
    if failure == "accounting_outage":
        assert ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == 0


class RejectingProvider(ScriptedProvider):
    """Adds a non-extractive observation after the good claim (NOBLIVION-280)."""

    def __init__(self, *, good=True, bad=None):
        super().__init__()
        self.good = good
        self.bad = bad or {"kind": "observation", "statement": "The otter likes cobalt paint."}

    def complete(self, messages, **kwargs):
        response = super().complete(messages, **kwargs)
        data = json.loads(messages[-1]["content"])
        if "notes" not in data:
            return response
        claims = json.loads(response.content)["claims"]
        bad = {**claims[0], **self.bad}
        claims = (claims if self.good else []) + [bad]
        return ProviderResponse(json.dumps({"claims": claims}), 100)


def derivation_worker(ledger, provider):
    return LearningWorker(
        ledger,
        provider,
        WorkerSettings(retry_delay=0.01),
        BudgetLimits(max_calls=10, max_tokens=500000, max_seconds=1000),
    )


def derivation_progress(ledger, ident):
    row = ledger.conn.execute("SELECT progress FROM learning_jobs WHERE id=?", (ident,)).fetchone()
    return json.loads(row[0])["derivation"]


def test_a_rejected_proposal_is_dropped_and_recorded_not_fatal(ledgers):
    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = RejectingProvider()
    result = derivation_worker(ledger, provider).run_once()
    assert result["status"] == "done" and provider.calls == 2
    stage = derivation_progress(ledger, ident)
    assert len(stage["staged"]) == 1
    assert stage["rejected"] == [
        {
            "unit": 0,
            "proposal": 1,
            "reason": "observation requires an extractive native user or tool passage",
        }
    ]
    assert "otter likes" not in json.dumps(stage["rejected"])


def test_a_derivation_whose_every_proposal_is_rejected_ends_clean(ledgers):
    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = RejectingProvider(good=False)
    result = derivation_worker(ledger, provider).run_once()
    assert result["status"] == "done" and provider.calls == 1
    stage = derivation_progress(ledger, ident)
    assert stage["staged"] == [] and len(stage["rejected"]) == 1


def test_only_a_guard_refusal_is_a_proposal_rejection(ledgers):
    ledger, store, a, b, _ = seeded(ledgers)
    with pytest.raises(ProposalRejected, match="extractive"):
        store.stage(proposal(store, [a, b], kind="observation"), "test", "demo")
    ledger.set_controls("test", "demo", "", contribute=False)
    with pytest.raises(LearningError, match="contribution is disabled") as refused:
        store.stage(proposal(store, [a, b]), "test", "demo")
    assert not isinstance(refused.value, ProposalRejected)


def test_a_claim_field_of_the_wrong_type_is_a_rejected_proposal(ledgers):
    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = RejectingProvider(bad={"kind": ["inference"]})
    assert derivation_worker(ledger, provider).run_once()["status"] == "done"
    assert derivation_progress(ledger, ident)["rejected"] == [
        {"unit": 0, "proposal": 1, "reason": "invalid claim kind, category or scope"}
    ]


def test_a_stage_refusal_outside_the_guards_still_fails_the_derivation(ledgers, monkeypatch):
    ledger, store, _, _, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")

    def refuse(*args, **kwargs):
        raise LearningError("claim dates must come from exact support")

    monkeypatch.setattr(type(store), "stage", refuse)
    result = derivation_worker(ledger, RejectingProvider()).run_once()
    assert result["status"] == "failed"
    row = ledger.conn.execute(
        "SELECT last_error FROM learning_jobs WHERE id=?", (ident,)
    ).fetchone()
    assert row[0] == "LearningError: claim dates must come from exact support"


# --- NOBLIVION-269: one consolidate job per folder per night -----------------


def consolidate_rows(ledger):
    return [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT id,status,last_error FROM learning_jobs WHERE kind='consolidate' "
            "AND project='test' AND root='demo' ORDER BY created_at,id"
        )
    ]


def queued_copy(ledger, n, *, progress="{}"):
    """A second pending consolidate job on the folder, as earlier releases queued one."""
    now, batch = time.time(), f"derive:copy-{n}"
    ledger.conn.execute(
        "INSERT INTO learning_batches VALUES (?,?,?,?,?,0,?)",
        (batch, batch, "test", "demo", f"copy-{n}", now),
    )
    ledger.conn.execute(
        "INSERT INTO learning_jobs "
        "(id,batch_id,project,root,session_id,kind,progress,created_at,updated_at) "
        "VALUES (?,?,'test','demo',?,'consolidate',?,?,?)",
        (f"copy-{n}", batch, f"copy-{n}", progress, now, now),
    )
    return f"copy-{n}"


class Frozen(Exception):
    pass


def test_a_new_enqueue_folds_into_the_pending_job_which_reads_the_current_notes(ledgers):
    ledger, store, a, b, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    # Hold the job back, so that learn() claims its own extract job.
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=1e12 WHERE id=?", (first,))
    c = learn(ledger, 3, "The cobalt otter service writes its log to /var/log/otter.")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (first,))
    assert ledger.enqueue_derivation("test", "demo", "C") == first
    assert [row["id"] for row in consolidate_rows(ledger)] == [first]
    job = ledger.claim("merge")
    assert job["id"] == first

    def stop(*args, **kwargs):
        raise Frozen

    with pytest.raises(Frozen):
        run_derivation(ledger, job, stop)
    snapshot = derivation_progress(ledger, first)["snapshot"]
    assert sorted(note["note_id"] for note in (a, b, c)) == sorted(n for n, _ in snapshot)


def test_a_started_pending_job_takes_no_fold(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    ledger.conn.execute(
        "UPDATE learning_jobs SET progress='{\"derivation\":{}}' WHERE id=?", (first,)
    )
    assert ledger.enqueue_derivation("test", "demo", "C") not in {None, first}


def test_claim_never_leases_two_consolidate_jobs_on_one_folder(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    assert ledger.claim("one")["id"] == first
    copy = queued_copy(ledger, 1)
    assert ledger.claim("two") is None
    ledger.conn.execute("UPDATE learning_jobs SET status='done' WHERE id=?", (first,))
    # No night window given: no per-night limit.
    assert ledger.claim("two")["id"] == copy


@pytest.mark.parametrize("finished", ["done", "exhausted"])
def test_one_consolidate_run_per_folder_per_night(ledgers, finished):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    ledger.claim("one")
    copy = queued_copy(ledger, 1)
    now = time.time()
    ledger.conn.execute(
        "UPDATE learning_jobs SET status=?,updated_at=? WHERE id=?", (finished, now, first)
    )
    assert ledger.claim("two", window_start=now - 60) is None
    assert ledger.claim("two", window_start=now + 60)["id"] == copy


def test_claim_folds_the_queued_copies_into_the_leased_job(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    copies = [queued_copy(ledger, n) for n in (1, 2, 3)]
    started = queued_copy(ledger, 4, progress='{"derivation":{}}')
    assert ledger.claim("one")["id"] == first
    rows = {row["id"]: row for row in consolidate_rows(ledger)}
    assert {rows[c]["status"] for c in copies} == {"cancelled"}
    assert {rows[c]["last_error"] for c in copies} == {FOLD_REASON}
    assert rows[started]["status"] == "pending"


def test_a_started_job_between_attempts_holds_its_folder(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "B")
    ledger.claim("one")
    # A failed attempt: the job waits for its retry with its stage.
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='pending',lease_token=NULL,lease_until=NULL,"
        "next_attempt=1e12,progress='{\"derivation\":{}}' WHERE id=?",
        (first,),
    )
    queued_copy(ledger, 1)
    assert ledger.claim("two", window_start=time.time() - 60) is None


def test_a_paused_session_does_not_stop_the_folded_consolidate_job(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "A")
    ledger.set_controls("test", "demo", "A", paused=True)
    assert ledger.enqueue_derivation("test", "demo", "B") == first
    assert ledger.claim("w")["id"] == first


def test_a_session_opt_out_does_not_stop_the_folded_consolidate_job(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "A")
    ledger.set_controls("test", "demo", "A", contribute=False)
    assert ledger.enqueue_derivation("test", "demo", "B") == first
    job = ledger.claim("w")
    assert job["id"] == first
    ledger.validate_lease(first, job["lease_token"])


def test_a_paused_session_with_a_started_job_does_not_block_the_folder(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "A")
    ledger.claim("w")
    # The started job waits for its retry, and its session pauses.
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='pending',lease_token=NULL,lease_until=NULL,"
        "progress='{\"derivation\":{}}' WHERE id=?",
        (first,),
    )
    ledger.set_controls("test", "demo", "A", paused=True)
    assert ledger.enqueue_derivation("test", "demo", "B") != first
    # The folder is not blocked: the older started job runs first.
    assert ledger.claim("w2")["id"] == first


@pytest.mark.parametrize("control", [{"paused": True}, {"contribute": False}])
def test_a_folder_pause_or_opt_out_still_stops_consolidate(ledgers, control):
    ledger, _, _, _, _ = seeded(ledgers)
    ledger.enqueue_derivation("test", "demo", "A")
    ledger.set_controls("test", "demo", "", **control)
    assert ledger.claim("w") is None


def test_a_request_after_the_folded_job_exhausted_queues_a_new_job(ledgers):
    ledger, _, _, _, _ = seeded(ledgers)
    first = ledger.enqueue_derivation("test", "demo", "A")
    job = ledger.claim("w")
    second = ledger.enqueue_derivation("test", "demo", "B")
    assert second != first
    ledger.fail(first, job["lease_token"], "boom", permanent=True)
    assert ledger.enqueue_derivation("test", "demo", "C") == second
    job = ledger.claim("w2")
    assert job["id"] == second
    ledger.fail(second, job["lease_token"], "boom", permanent=True)
    again = ledger.enqueue_derivation("test", "demo", "C")
    status = ledger.conn.execute("SELECT status FROM learning_jobs WHERE id=?", (again,))
    assert status.fetchone()[0] == "pending"


class SliceProvider(ScriptedProvider):
    """Records every request; answers proposal, support and equivalence (NOBLIVION-284)."""

    def __init__(self, *, first_claims=1, fail_on=None):
        super().__init__()
        self.first_claims = first_claims
        self.fail_on = fail_on
        self.requests = []

    def complete(self, messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        self.requests.append(data)
        if "candidate" in data:
            self.calls += 1
            relations = [
                {
                    "target_id": c["claim_id"],
                    "target_revision": c["revision"],
                    "relation": "distinct",
                }
                for c in data["targets"]
            ]
            return ProviderResponse(json.dumps({"relations": relations}), 100)
        if "notes" in data and self.fail_on in {n["note_id"] for n in data["notes"]}:
            self.fail_on = None
            self.calls += 1
            raise RuntimeError("injected provider outage")
        response = super().complete(messages, **kwargs)
        if "notes" not in data or self.first_claims == 1:
            return response
        claim = json.loads(response.content)["claims"][0]
        count, self.first_claims = self.first_claims, 1
        claims = [
            {**claim, "statement": f"The cobalt otter service uses loopback port {4300 + n}."}
            for n in range(count)
        ]
        return ProviderResponse(json.dumps({"claims": claims}), 100)

    def proposals(self):
        return [[n["note_id"] for n in r["notes"]] for r in self.requests if "notes" in r]


def slice_worker(ledger, provider):
    return LearningWorker(
        ledger,
        provider,
        WorkerSettings(retry_delay=0.01),
        BudgetLimits(max_calls=100, max_tokens=10**7, max_seconds=10**5),
    )


def with_settings(monkeypatch, **changes):
    import noblivion.learning_synthesis as synthesis

    settings = ConsolidationSettings(**changes)
    monkeypatch.setattr(
        synthesis, "ConsolidationLedger", lambda ledger: ConsolidationLedger(ledger, settings)
    )


def job_progress(ledger, ident):
    row = ledger.conn.execute("SELECT status,progress FROM learning_jobs WHERE id=?", (ident,))
    status, progress = row.fetchone()
    return status, json.loads(progress)


def test_a_full_proposal_reply_splits_its_unit_and_the_job_finishes(ledgers):
    ledger, _, a, b, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = SliceProvider(first_claims=12)
    result = slice_worker(ledger, provider).run_once()
    assert result["status"] == "done"
    # The full reply of twelve claims asked each half of its unit once more.
    first = provider.proposals()[0]
    assert sorted(first) == sorted([a["note_id"], b["note_id"]])
    assert provider.proposals()[1:] == [[first[0]], [first[1]]]
    status, progress = job_progress(ledger, ident)
    assert status == "done" and progress["derivation"]["deferred"] == []
    assert len(progress["derivation"]["units"]) == 3


def test_a_folder_over_the_note_limit_runs_in_slices_and_finishes(ledgers, monkeypatch):
    ledger, _, a, b, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = SliceProvider()
    result = slice_worker(ledger, provider).run_once()
    assert result["status"] == "done"
    assert sorted(provider.proposals()) == sorted([[a["note_id"]], [b["note_id"]]])
    status, progress = job_progress(ledger, ident)
    assert status == "done" and progress["slices_done"] == 2


def test_a_restart_in_a_later_slice_does_not_repeat_finished_proposals(ledgers, monkeypatch):
    ledger, _, _, _, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    order = [n["note_id"] for n in ledger.current_notes("test", "demo")]
    provider = SliceProvider(fail_on=order[1])
    worker = slice_worker(ledger, provider)
    assert worker.run_once()["status"] in {"pending", "failed"}
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    assert worker.run_once()["status"] == "done"
    # The first slice was asked once; the failed proposal of the second slice once more.
    assert provider.proposals() == [[order[0]], [order[1]], [order[1]]]


def test_the_support_check_holds_only_the_notes_of_the_claim_unit(ledgers, monkeypatch):
    from noblivion.learning_values import canonical_json

    ledger, store, _, _, _ = seeded(ledgers)
    items = []
    for note in ledger.current_notes("test", "demo"):
        item = {
            k: note[k]
            for k in (
                "note_id",
                "revision",
                "statement",
                "category",
                "scope",
                "observed_at",
                "effective_at",
            )
        }
        item["supports"] = store.note_supports(note)
        items.append(item)
    # Two notes no longer fit one unit, so each note is its own unit.
    with_settings(monkeypatch, max_input_chars=len(canonical_json(items)) - 1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = SliceProvider()
    assert slice_worker(ledger, provider).run_once()["status"] == "done"
    assert len(provider.proposals()) == 2
    checks = [r for r in provider.requests if "claim" in r]
    assert len(checks) == 2
    for check in checks:
        cited = {s["note_id"] for s in check["claim"]["supports"]}
        assert {n["note_id"] for n in check["other_current_notes"]} == cited
    assert job_progress(ledger, ident)[0] == "done"


def test_a_snapshot_reset_restarts_only_the_current_slice(ledgers, monkeypatch):
    ledger, _, _, _, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    order = [n["note_id"] for n in ledger.current_notes("test", "demo")]

    class ForgettingProvider(SliceProvider):
        def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            added = getattr(self, "added", None)
            if (
                not added
                and "claim" in data
                and data["other_current_notes"][0]["note_id"] == order[1]
            ):
                # A new note between the two slices joins the current slice.
                self.added = learn(ledger, 15, "The cobalt otter service logs to stderr.")
            return super().complete(messages, **kwargs)

    provider = ForgettingProvider()
    worker = slice_worker(ledger, provider)
    first = worker.run_once()
    assert first.get("error") == "SemanticSnapshotChanged", first
    status, progress = job_progress(ledger, ident)
    assert "derivation" not in progress and progress["slices_done"] == 1
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    second = worker.run_once()
    assert second["status"] == "done", (second, job_progress(ledger, ident))
    # The finished first slice was not asked again; the current slice restarted.
    added = provider.added["note_id"]
    assert provider.proposals() == [[order[0]], [order[1]], [added], [order[1]]]
    assert job_progress(ledger, ident)[1]["slices_done"] == 3


def test_a_note_added_between_runs_restarts_only_the_current_slice(ledgers, monkeypatch):
    ledger, _, _, _, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    order = [n["note_id"] for n in ledger.current_notes("test", "demo")]
    provider = SliceProvider(fail_on=order[1])
    worker = slice_worker(ledger, provider)
    assert worker.run_once()["status"] in {"pending", "failed"}
    status, progress = job_progress(ledger, ident)
    assert progress["slices_done"] == 1 and "derivation" in progress
    # The job waits in slice 2; a new note joins the folder of that slice.
    added = learn(ledger, 15, "The cobalt otter service logs to stderr.")["note_id"]
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    first = worker.run_once()
    assert first.get("error") == "SemanticSnapshotChanged", (first, job_progress(ledger, ident))
    status, progress = job_progress(ledger, ident)
    assert "derivation" not in progress and progress["slices_done"] == 1
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    second = worker.run_once()
    assert second["status"] == "done", (second, job_progress(ledger, ident))
    # The finished first slice was not asked again; the current slice restarted.
    assert provider.proposals() == [[order[0]], [order[1]], [added], [order[1]]]
    assert job_progress(ledger, ident)[1]["slices_done"] == 3


def test_a_retried_stage_of_0_1_48_with_deferred_limit_records_finishes(ledgers):
    ledger, _, a, b, _ = seeded(ledgers)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    worker = slice_worker(ledger, SliceProvider(fail_on=a["note_id"]))
    assert worker.run_once()["status"] in {"pending", "failed"}
    _, progress = job_progress(ledger, ident)
    # Rewrite the stage to the 0.1.48 shape: units hold the note items, and a
    # saturation record sits in deferred, so 0.1.48 ended the job exhausted.
    stage = progress["derivation"]
    stage["units"] = [[stage["notes"][i] for i in unit] for unit in stage["units"]]
    for key in ("notes", "unit_of", "proposals"):
        stage.pop(key, None)
    stage["next_unit"] = len(stage["units"])
    stage["deferred"] = [{"unit": 0, "reason": "synthesis proposal saturation"}]
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='exhausted',last_error='DerivationCoverageExhausted',"
        "progress=? WHERE id=?",
        (json.dumps(progress), ident),
    )
    ledger.retry(ident)
    provider = SliceProvider()
    result = slice_worker(ledger, provider).run_once()
    assert result["status"] == "done", (result, job_progress(ledger, ident))
    # The old stage was rebuilt in the new shape and asked again.
    assert sorted(provider.proposals()[0]) == sorted([a["note_id"], b["note_id"]])


def test_a_note_over_the_support_limit_is_omitted_and_the_job_finishes(ledgers, monkeypatch):
    ledger, _, a, b, _ = seeded(ledgers)
    text = "The cobalt otter service logs to stderr."
    content = text + "\n" + text
    source = {"source_id": "source-15", "source_hash": hashlib.sha256(content.encode()).hexdigest()}
    spans = [(0, len(text)), (len(text) + 1, len(content))]
    evidence = [{**source, "start": s, "end": e, "quote": text} for s, e in spans]
    wide = learn(ledger, 15, text, content=content, extra={"evidence": evidence})
    assert len(wide["evidence"]) == 2
    with_settings(monkeypatch, max_notes=1, max_supports=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    provider = SliceProvider()
    result = slice_worker(ledger, provider).run_once()
    assert result["status"] == "done", (result, job_progress(ledger, ident))
    assert sorted(provider.proposals()) == sorted([[a["note_id"]], [b["note_id"]]])
    status, progress = job_progress(ledger, ident)
    assert [o["note_id"] for o in progress["omitted"]] == [wide["note_id"]]
    assert result["omitted"] == 1 and result["slices"] == 3


def test_the_result_counts_every_slice_after_a_restart(ledgers, monkeypatch):
    ledger, store, _, _, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    order = [n["note_id"] for n in ledger.current_notes("test", "demo")]
    provider = SliceProvider(fail_on=order[1])
    worker = slice_worker(ledger, provider)
    assert worker.run_once()["status"] in {"pending", "failed"}
    # The last slice gets a full reply, so its single note is saturated.
    provider.first_claims = 12
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (ident,))
    result = worker.run_once()
    assert result["status"] == "done", (result, job_progress(ledger, ident))
    assert result["slices"] == 2 and result["saturated"] == 1
    assert result["overflow"] == 0 and result["omitted"] == 0
    # Both slices count: the claims the job staged, not the seeded claim.
    staged = ledger.conn.execute("SELECT count(*) FROM learning_claims").fetchone()[0] - 1
    assert result["claims"] == staged and staged > 12
    assert len(job_progress(ledger, ident)[1]["saturated"]) == 1


def test_a_note_added_after_the_cursor_joins_a_later_slice_in_the_same_run(ledgers, monkeypatch):
    ledger, _, _, _, _ = seeded(ledgers)
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    order = [n["note_id"] for n in ledger.current_notes("test", "demo")]

    class AddingProvider(SliceProvider):
        def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            if (
                not getattr(self, "added", None)
                and "claim" in data
                and data["other_current_notes"][0]["note_id"] == order[0]
            ):
                # A new note arrives while slice 1 runs; it sorts into slice 2.
                self.added = learn(ledger, 15, "The cobalt otter service logs to stderr.")
            return super().complete(messages, **kwargs)

    provider = AddingProvider()
    result = slice_worker(ledger, provider).run_once()
    assert result["status"] == "done", (result, job_progress(ledger, ident))
    added = provider.added["note_id"]
    assert provider.proposals() == [[order[0]], [added], [order[1]]]
    assert job_progress(ledger, ident)[1]["slices_done"] == 3


def test_the_saved_progress_keeps_a_claim_count_not_the_claim_ids(ledgers, monkeypatch):
    ledger, _, _, _, claim = seeded(ledgers)
    for n in range(21, 27):
        learn(ledger, n, f"The cobalt otter worker {n} runs on host {n}.")
    with_settings(monkeypatch, max_notes=1)
    ident = ledger.enqueue_derivation("test", "demo", "B")
    result = slice_worker(ledger, SliceProvider()).run_once()
    assert result["status"] == "done" and result["slices"] == 8
    _, progress = job_progress(ledger, ident)
    # A claim id outside the current stage would grow the progress with every
    # slice, toward the 1 MiB checkpoint cap (NOBLIVION-284).
    outside = json.dumps({k: v for k, v in progress.items() if k != "derivation"})
    ids = [r[0] for r in ledger.conn.execute("SELECT claim_id FROM learning_claims")]
    assert [i for i in ids if i != claim["claim_id"] and i in outside] == []
    assert progress["claims"] == result["claims"] == len(ids) - 1
