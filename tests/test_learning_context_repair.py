# SPDX-License-Identifier: AGPL-3.0-or-later
"""Context isolation, useful clean yield and durable output-limit deferrals."""

import json
from types import SimpleNamespace

import pytest

from noblivion import db
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_context import dependent_decisions, isolate_notes
from noblivion.learning_worker import (
    LearningWorker,
    OpenAICompatibleProvider,
    ProviderError,
    WorkerSettings,
)
from noblivion.redaction import contains_secret
from test_learning_review_fixes import ScriptedProvider, candidate, capture, seed, worker


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def old_note(**changes):
    return dict(
        note_id="legacy:123",
        logical_key="legacy:123",
        revision=1,
        category="fact",
        scope="project",
        title="Cobalt service",
        statement="The cobalt service uses port 443.",
        evidence=[],
        read_only=True,
        **changes,
    )


def ready(ledger):
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE status='pending'")


def test_safe_statement_survives_unsafe_ancillary_fields():
    note = old_note()
    note.update(title="[REDACTED]", evidence=[{"quote": "[REDACTED]"}])
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe[0]["statement"] == note["statement"]
    assert safe[0]["evidence"] == [] and safe[0]["evidence_withheld"]
    assert not contains_secret(json.dumps([safe, excluded]))
    assert excluded[0]["reason"] == "ContextRedactedCandidate"
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == []


@pytest.mark.parametrize(
    "text,reason",
    [
        ("The cobalt job reads GH_TOKEN=***REDACTED*** first.", "ContextRedactedCandidate"),
        ("My password is a-fictional-value.", "ContextUnsafeCandidate"),
        ("Safe content. " * 4000, "ContextSizeLimit"),
    ],
)
def test_isolation_preserves_explicit_dependency_without_body(text, reason):
    unsafe = old_note()
    unsafe["statement"] = text
    safe, excluded = isolate_notes([old_note(), unsafe], max_chars=24000)
    assert len(safe) == 1 and excluded[0]["reason"] == reason
    assert excluded[0]["note_id"] == "legacy:123" and excluded[0]["revision"] == 1
    assert text not in json.dumps(excluded)
    assert not contains_secret(json.dumps([safe, excluded]))
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == [0]


def test_withheld_statement_defers_without_repeating_extraction_and_can_resolve(ledger):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    note = old_note()
    # A marker in a key=value position stays withheld (NOBLIVION-237).
    note["statement"] = "The cobalt job reads GH_TOKEN=***REDACTED*** first."
    notes = [old_note(), note]
    ledger.reconciliation_context = lambda *a, **k: notes
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    run = worker(ledger, provider)
    first = run.run_once()
    assert first["status"] == "deferred" and first["error"] == "ContextDependencyDeferred"
    assert provider.calls == ["extract"] and not ledger.current_notes("demo", "scope")
    ready(ledger)
    assert run.run_once()["status"] == "deferred"
    assert provider.calls == ["extract"]
    row = ledger.conn.execute("SELECT attempts,progress FROM learning_jobs").fetchone()
    assert row["attempts"] == 0 and not contains_secret(row["progress"])
    assert ledger.status()["context_rejections"]["ContextRedactedCandidate"] == 1
    notes.clear()
    ready(ledger)
    assert run.run_once()["status"] == "done"
    assert provider.calls == ["extract"]
    assert ledger.current_notes("demo", "scope")[0]["statement"] == record["content"]


def test_mixed_context_with_safe_statement_and_hidden_evidence_commits_distinct_fact(ledger):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    note = old_note()
    note.update(title="[REDACTED]", evidence=[{"quote": "[REDACTED]"}])
    ledger.reconciliation_context = lambda *a, **k: [note]
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    original = provider.complete

    def checked(messages, **kwargs):
        assert not contains_secret(json.dumps(messages))
        return original(messages, **kwargs)

    provider.complete = checked
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "add"
    assert provider.calls == ["extract", "semantic"]
    assert ledger.status()["outcomes"]["committed"] == 1


def test_unsafe_managed_note_outside_extraction_still_blocks_semantic_admission(ledger):
    saved = seed(ledger, capture(ledger, "old", "Use concise prose."))
    ledger.conn.execute(
        "UPDATE learning_notes SET statement=? WHERE note_id=?",
        ("The cobalt setting is [REDACTED].", saved["note_id"]),
    )
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    ledger.reconciliation_context = lambda *a, **k: []
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    run = worker(ledger, provider)
    assert run.run_once()["error"] == "ContextDependencyDeferred"
    ready(ledger)
    assert run.run_once()["status"] == "deferred" and provider.calls == ["extract"]
    assert len(ledger.current_notes("demo", "scope")) == 1


@pytest.mark.parametrize("index", range(20))
def test_clean_unambiguous_fact_yield(ledger, index):
    record = capture(
        ledger, f"clean-{index}", f"The indigo service listens on port {8400 + index}."
    )
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "add"
    assert ledger.current_notes("demo", "scope")[0]["statement"] == record["content"]


@pytest.mark.parametrize("reason,expected", [("length", "deferred"), ("transport", "failed")])
def test_failure_duration_separate_from_reservation_and_no_identical_length_retry(
    ledger, monkeypatch, reason, expected
):
    import noblivion.learning_worker as module

    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderError(reason, tokens=2048 if reason == "length" else None)

    provider.complete = fail
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    run = worker(ledger, provider)
    assert run.run_once()["status"] == expected
    status = ledger.status()
    assert status["provider_actual"]["seconds"] == 2.5
    assert status["provider_actual"]["reasons"] == {reason: 1}
    assert status["budget"]["seconds"] == 60
    assert status["failure_reasons"]["Provider:" + reason] == 1
    if reason == "length":
        ready(ledger)
        assert run.run_once()["status"] == "deferred" and len(calls) == 1
        assert ledger.status()["provider_actual"]["calls"] == 1
        assert ledger.conn.execute("SELECT attempts FROM learning_jobs").fetchone()[0] == 0
    else:
        assert (
            ledger.conn.execute("SELECT status FROM learning_budget").fetchone()[0] == "uncertain"
        )


@pytest.mark.parametrize(
    "reply,reason,tokens",
    [
        (
            {
                "choices": [{"finish_reason": "length", "message": {"content": "private text"}}],
                "usage": {"total_tokens": 2048},
            },
            "length",
            2048,
        ),
        ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, "empty", None),
        ({"choices": [{"finish_reason": "stop", "message": {"content": None}}]}, "invalid", None),
        (
            {"choices": [{"finish_reason": "filter", "message": {"content": "private text"}}]},
            "incomplete",
            None,
        ),
        (
            {"choices": [{"finish_reason": "length"}], "usage": {"total_tokens": "bad"}},
            "length",
            None,
        ),
        ([], "invalid", None),
    ],
)
def test_provider_failure_reason_never_retains_body(reply, reason, tokens):
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:1234", "fixture", local=True, cost_per_million_tokens=0
    )

    class Reply:
        sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, n):
            if self.sent:
                return b""
            self.sent = True
            return json.dumps(reply).encode()

    provider.opener = SimpleNamespace(open=lambda *a, **k: Reply())
    with pytest.raises(ProviderError) as caught:
        provider.complete([], max_tokens=2048, timeout=60)
    assert caught.value.reason == reason and caught.value.tokens == tokens
    assert "private text" not in str(caught.value)


def test_paired_output_timeout_limits_extend_lease_before_submission(ledger):
    import time

    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    original = provider.complete

    def checked(messages, **kwargs):
        assert kwargs["max_tokens"] == 8192 and kwargs["timeout"] == 180
        lease = ledger.conn.execute("SELECT lease_until FROM learning_jobs").fetchone()[0]
        assert lease > time.time() + 200
        return original(messages, **kwargs)

    provider.complete = checked
    run = LearningWorker(
        ledger,
        provider,
        WorkerSettings(max_output_tokens=8192, timeout_seconds=180),
        BudgetLimits(),
    )
    assert run.run_once()["status"] == "done"


@pytest.mark.parametrize("format_retry", [False, True])
def test_length_hold_can_resume_with_reviewed_output_limits(ledger, format_retry):
    from noblivion.learning_worker import ProviderResponse

    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    limits = []

    def complete(messages, **kwargs):
        limits.append(kwargs["max_tokens"])
        if format_retry and len(limits) == 1:
            return ProviderResponse("not JSON", 20)
        if kwargs["max_tokens"] == 2048:
            raise ProviderError("length", tokens=2048)
        return ProviderResponse('{"version":1,"candidates":[]}', 40)

    provider.complete = complete
    run = worker(ledger, provider)
    assert run.run_once()["status"] == "deferred"
    ready(ledger)
    assert run.run_once()["status"] == "deferred"
    assert len(limits) == 1 + format_retry
    before = ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0]
    ready(ledger)
    recovered = LearningWorker(
        ledger,
        provider,
        WorkerSettings(max_output_tokens=8192, timeout_seconds=180),
        BudgetLimits(),
    )
    assert recovered.run_once()["status"] == "done"
    assert limits[-1] == 8192
    assert ledger.conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == before + 1


def test_length_recovery_does_not_allow_source_or_window_change(ledger):
    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderError("length")

    provider.complete = complete
    assert worker(ledger, provider).run_once()["status"] == "deferred"
    ready(ledger)
    changed = LearningWorker(
        ledger, provider, WorkerSettings(max_output_tokens=8192, window_chars=4000), BudgetLimits()
    )
    result = changed.run_once()
    assert result["status"] == "failed" and result["permanent"]
    assert len(calls) == 1


def test_withheld_old_evidence_records_deferred_correction_instead_of_mutation(ledger):
    before = capture(ledger, "old", "Use concise prose.")
    saved = seed(ledger, before)
    existing = ledger.reconciliation_context("demo", "scope")
    existing[0]["evidence"] = [{"quote": "[REDACTED]"}]
    ledger.reconciliation_context = lambda *a, **k: existing
    after = capture(ledger, "new", "Use detailed prose from now on.")
    proposal = candidate(
        after,
        action="supersede",
        note_id=saved["note_id"],
        correction={
            "previous_statement": before["content"],
            "current_statement": after["content"],
            "current_evidence": [{"source_id": after["source_id"], "quote": after["content"]}],
        },
    )
    result = worker(ledger, ScriptedProvider(ledger, [proposal])).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "conflict"
    assert ledger.current_notes("demo", "scope")[0]["statement"] == before["content"]
    assert ledger.status()["outcomes"]["deferred_claims"] == 1


def test_timeout_only_change_does_not_repeat_identical_length_request(ledger):
    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderError("length")

    provider.complete = complete
    assert worker(ledger, provider).run_once()["status"] == "deferred"
    ready(ledger)
    changed = LearningWorker(ledger, provider, WorkerSettings(timeout_seconds=180), BudgetLimits())
    assert changed.run_once()["status"] == "deferred" and len(calls) == 1


def test_output_recovery_preserves_completed_page_receipts(ledger):
    from dataclasses import replace

    from noblivion.learning_worker import ProviderResponse

    capture(ledger, "new", "Routine context. " * 1100)
    provider = ScriptedProvider(ledger, [])
    offsets = []

    def complete(messages, **kwargs):
        window = json.loads(messages[-1]["content"])["sources"]
        offsets.append(window[0]["offset"])
        if len(offsets) == 2:
            raise ProviderError("length")
        return ProviderResponse('{"version":1,"candidates":[]}', 20)

    provider.complete = complete
    limits = replace(BudgetLimits(), max_seconds=1000, max_tokens=200000)
    run = LearningWorker(ledger, provider, WorkerSettings(), limits)
    assert run.run_once()["status"] == "deferred" and offsets[0] == 0 and offsets[1] > 0
    ready(ledger)
    recovered = LearningWorker(
        ledger, provider, WorkerSettings(max_output_tokens=8192, timeout_seconds=180), limits
    )
    assert recovered.run_once()["status"] == "done"
    assert offsets[2] == offsets[1] and offsets.count(0) == 1
    assert all(offset > offsets[1] for offset in offsets[3:])
    assert (
        ledger.conn.execute("SELECT count(*) FROM learning_extraction_pages").fetchone()[0]
        == len(offsets) - 1
    )


def test_length_hold_survives_budget_settlement_failure(ledger):
    capture(ledger, "new", "The indigo service uses port 8443.")
    provider = ScriptedProvider(ledger, [])
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderError("length", tokens=2048)

    def failed_settlement(*args, **kwargs):
        raise RuntimeError("accounting unavailable")

    provider.complete = complete
    settle = ledger.settle
    ledger.settle = failed_settlement
    run = worker(ledger, provider)
    assert run.run_once()["status"] == "failed"
    ledger.settle = settle
    ready(ledger)
    assert run.run_once()["status"] == "deferred" and len(calls) == 1
    assert ledger.status()["provider_actual"]["reasons"] == {"length": 1}
