# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-236: policy changes keep started learning jobs; request changes fail them."""

import hashlib
import json
from dataclasses import asdict, fields, replace

import pytest

from noblivion import db, learning_extract, learning_semantic, learning_synthesis, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_synthesis import _legacy_runtime_matches, _runtime_fingerprint
from noblivion.learning_worker import (
    DERIVATION_CONTRACT,
    EXTRACTION_CONTRACT,
    POLICY_BUDGETS,
    POLICY_SETTINGS,
    REQUEST_BUDGETS,
    REQUEST_PROVIDER,
    REQUEST_SETTINGS,
    LearningWorker,
    OpenAICompatibleProvider,
    ProviderResponse,
    WorkerSettings,
    execution_matches,
    input_fingerprint,
    worker_matches,
)
from test_learning_review_fixes import capture

# The execution_configuration that release 0.1.24 froze into the live jobs of
# 2026-11-14, copied from a backup of that database.
LIVE_0_1_24 = {
    "budgets": {
        "max_calls": 100,
        "max_concurrency": 1,
        "max_cost": 0,
        "max_seconds": 6000,
        "max_tokens": 4000000,
        "window_seconds": 86400,
    },
    "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
    "model": "qwen3.8:27b-64k",
    "provider_type": "noblivion.learning_worker.OpenAICompatibleProvider",
    "reasoning_effort": "low",
    "settings": {
        "enable_consolidation": False,
        "lease_seconds": 120,
        "max_attempts": 4,
        "max_continuations": 16,
        "max_decisions": 512,
        "max_existing_chars": 24000,
        "max_output_tokens": 8192,
        "max_pages": 256,
        "max_windows": 128,
        "overlap_chars": 2000,
        "poll_seconds": 5.0,
        "retry_delay": 30.0,
        "timeout_seconds": 180,
        "window_chars": 12000,
    },
}
LIVE_FILE = {
    "provider": {
        "endpoint": "http://127.0.0.1:11434/v1",
        "model": "qwen3.8:27b-64k",
        "local": True,
        "cost_per_million_tokens": 0,
        "reasoning_effort": "low",
    },
    "worker": {"timeout_seconds": 180, "max_output_tokens": 8192, "max_attempts": 4},
    "budget": {
        "window_seconds": 86400,
        "max_calls": 100,
        "max_tokens": 4000000,
        "max_seconds": 6000,
        "max_cost": 0,
        "max_concurrency": 1,
    },
}
# The drain-period configuration of the operator, 2026-11-14.
RAISED_BUDGET = {"max_calls": 2500, "max_tokens": 110000000, "max_seconds": 450000}
SOURCES = [{"id": "source-1", "sha256": "a" * 64}]
WINDOWS = [[{"source_id": "source-1", "offset": 0, "text": "x"}]]
# The prompt and schema text of each extraction contract version. Change a
# prompt or schema only together with EXTRACTION_CONTRACT and a new entry here.
# Version 2 (NOBLIVION-241) keeps every version 1 string and adds the batched
# semantic prompt and schema, the batch limits and the window scale.
CONTRACT_TEXT = {
    1: "d36896fdc03a381cefca3212199a2e5afae95d357f5a703085e3c5bf9c392e99",
    2: "4e3f1a2944c64beb7d5846a037031e1ef747875b4e1cbc893c5b5fb191027dfc",
}


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def configured(ledger=None, *, provider=None, worker=None, budget=None):
    """A worker built from learning.json values, without any provider call."""
    return LearningWorker(
        ledger,
        OpenAICompatibleProvider(**{**LIVE_FILE["provider"], **(provider or {})}),
        WorkerSettings(**{**LIVE_FILE["worker"], **(worker or {})}),
        BudgetLimits(**{**LIVE_FILE["budget"], **(budget or {})}),
    )._configuration()


def legacy_progress(configuration):
    """Progress as release 0.1.24 froze it: the full configuration, no contract."""
    frozen = input_fingerprint(SOURCES, WINDOWS, configuration)
    return {
        "execution_configuration": configuration,
        "execution_fingerprint": frozen,
        "fingerprint": frozen,
    }


def release_0_1_24_configuration(run):
    """The body of LearningWorker._configuration() in release 0.1.24."""
    return {
        "settings": asdict(run.settings),
        "budgets": vars(run.budget_limits),
        "provider_type": type(run.provider).__module__ + "." + type(run.provider).__qualname__,
        "endpoint": getattr(run.provider, "endpoint", ""),
        "model": getattr(run.provider, "model", ""),
        "reasoning_effort": getattr(run.provider, "reasoning_effort", ""),
    }


def test_every_worker_setting_is_classified():
    names = {field.name for field in fields(WorkerSettings)}
    assert not REQUEST_SETTINGS & POLICY_SETTINGS
    assert names == REQUEST_SETTINGS | POLICY_SETTINGS, (
        "classify each new WorkerSettings field as request-affecting or policy-only"
    )


def test_every_budget_limit_is_classified():
    names = {field.name for field in fields(BudgetLimits)}
    assert not REQUEST_BUDGETS & POLICY_BUDGETS
    assert names == REQUEST_BUDGETS | POLICY_BUDGETS, (
        "classify each new BudgetLimits field as request-affecting or policy-only"
    )


def test_every_configuration_key_is_classified():
    assert set(configured()) == {"contract", "settings", "budgets", *REQUEST_PROVIDER}


def contract_text(version):
    text = [
        learning_extract.SYSTEM_PROMPT,
        learning_worker.CONTINUATION_PROMPT,
        learning_worker._FORMAT_RETRY,
        learning_extract.EXTRACTION_SCHEMA,
        learning_extract.SEMANTIC_PROMPT,
        learning_extract.SEMANTIC_SCHEMA,
        learning_synthesis.PROPOSAL_PROMPT,
        learning_synthesis.PROPOSAL_SCHEMA,
        learning_synthesis.SUPPORT_PROMPT,
        learning_synthesis.SUPPORT_SCHEMA,
        learning_synthesis.EQUIVALENCE_PROMPT,
        learning_synthesis.EQUIVALENCE_SCHEMA,
    ]
    if version >= 2:
        text += [
            learning_semantic.SEMANTIC_BATCH_PROMPT,
            learning_semantic.SEMANTIC_BATCH_SCHEMA,
            learning_semantic.MAX_BATCH_ITEMS,
            learning_semantic.MAX_BATCH_PAIRS,
            learning_worker.WINDOW_SCALE[2],
        ]
    return hashlib.sha256(json.dumps(text, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("version", learning_worker.SUPPORTED_CONTRACTS)
def test_contract_text_is_pinned_to_its_version(version):
    # A started job resumes under its own version, so every supported version
    # must keep its exact text, not only the newest one.
    assert CONTRACT_TEXT.get(version) == contract_text(version), (
        "a learning prompt or schema changed: bump EXTRACTION_CONTRACT and pin the new text"
    )
    assert EXTRACTION_CONTRACT == max(learning_worker.SUPPORTED_CONTRACTS)


def test_live_legacy_job_matches_its_own_file():
    # configured() is the derivation configuration, which stays version 1.
    assert DERIVATION_CONTRACT == 1
    assert configured() == {**LIVE_0_1_24, "contract": DERIVATION_CONTRACT}
    assert execution_matches(legacy_progress(LIVE_0_1_24), SOURCES, WINDOWS, configured())


@pytest.mark.parametrize(
    "change",
    [
        {"budget": RAISED_BUDGET, "worker": {"enable_consolidation": True}},
        {"budget": {"max_cost": 0, "max_concurrency": 2, "window_seconds": 3600}},
        {
            "worker": {
                "max_attempts": 10,
                "lease_seconds": 600,
                "retry_delay": 5.0,
                "poll_seconds": 1.0,
                "timeout_seconds": 300,
            }
        },
    ],
)
def test_policy_change_keeps_a_legacy_job(change):
    assert execution_matches(legacy_progress(LIVE_0_1_24), SOURCES, WINDOWS, configured(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"provider": {"model": "other:8b"}},
        {"provider": {"reasoning_effort": "none"}},
        {"provider": {"endpoint": "http://127.0.0.1:11435/v1"}},
        {"worker": {"max_output_tokens": 4096}},
        {"worker": {"window_chars": 8000}},
        {"worker": {"overlap_chars": 1000}},
        {"worker": {"max_windows": 64}},
        {"worker": {"max_existing_chars": 12000}},
        {"worker": {"max_pages": 128}},
        {"worker": {"max_continuations": 8}},
        {"worker": {"max_decisions": 256}},
    ],
)
def test_request_change_still_fails_a_legacy_job(change):
    assert not execution_matches(
        legacy_progress(LIVE_0_1_24), SOURCES, WINDOWS, configured(**change)
    )


def test_contract_change_fails_a_legacy_job():
    """A legacy job never matches a version 2 configuration.

    The worker therefore resolves such a job to version 1 (NOBLIVION-241); it
    never re-freezes it under version 2.
    """
    current = {**configured(), "contract": 2}
    assert not execution_matches(legacy_progress(LIVE_0_1_24), SOURCES, WINDOWS, current)
    assert execution_matches(legacy_progress(LIVE_0_1_24), SOURCES, WINDOWS, configured())


def test_changed_sources_or_windows_fail_a_legacy_job():
    progress = legacy_progress(LIVE_0_1_24)
    assert not execution_matches(
        progress, [{"id": "source-1", "sha256": "b" * 64}], WINDOWS, configured()
    )
    assert not execution_matches(progress, SOURCES, [], configured())


def test_stored_configuration_that_does_not_reproduce_the_hash_is_rejected():
    changed = configured(provider={"model": "other:8b"})
    progress = legacy_progress(LIVE_0_1_24)
    progress["execution_configuration"] = changed
    assert not execution_matches(progress, SOURCES, WINDOWS, changed)


def test_derivation_job_policy_change():
    legacy = {"worker_fingerprint": learning_worker.worker_fingerprint(LIVE_0_1_24)}
    raised = configured(budget=RAISED_BUDGET)
    # Hash only and unchanged: continues.
    assert worker_matches(legacy, configured())
    # Hash only and changed: no stored configuration can prove the old one.
    assert not worker_matches(legacy, raised)
    # A stored call configuration proves the frozen one.
    assert worker_matches({**legacy, "call_configuration": LIVE_0_1_24}, raised)
    other = configured(provider={"model": "other:8b"})
    assert not worker_matches({**legacy, "call_configuration": LIVE_0_1_24}, other)
    # The new shape ignores policy changes and keeps request changes.
    new = {
        "worker_fingerprint": learning_worker.worker_fingerprint(
            learning_worker.request_configuration(configured())
        )
    }
    assert worker_matches(new, raised) and not worker_matches(new, other)


def test_derivation_stage_policy_change():
    consolidation = "c" * 64
    raised = configured(budget=RAISED_BUDGET, worker={"enable_consolidation": True})
    other = configured(provider={"model": "other:8b"})
    assert _runtime_fingerprint(consolidation, configured()) == _runtime_fingerprint(
        consolidation, raised
    )
    assert _runtime_fingerprint(consolidation, configured()) != _runtime_fingerprint(
        consolidation, other
    )
    legacy = learning_synthesis._legacy_runtime_fingerprint(consolidation, LIVE_0_1_24)
    state = {"call_configuration": LIVE_0_1_24}
    assert _legacy_runtime_matches(legacy, consolidation, configured(), {})
    assert _legacy_runtime_matches(legacy, consolidation, raised, state)
    assert not _legacy_runtime_matches(legacy, consolidation, raised, {})
    assert not _legacy_runtime_matches(legacy, consolidation, other, state)
    assert not _legacy_runtime_matches(legacy, "d" * 64, raised, state)


class Pages:
    """Answers every extraction page with no lesson and records the window offsets."""

    def __init__(self):
        self.offsets = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        window = json.loads(messages[-1]["content"])["sources"]
        self.offsets.append(window[0]["offset"])
        return ProviderResponse('{"version":1,"candidates":[]}', 20)


def budget_paused_job(ledger, provider):
    """A two-window job that used its one-call budget on the first window.

    It starts under contract 1, as every job started by 0.1.25 or older did.
    """
    capture(ledger, "new", "Routine context. " * 1100)
    limits = replace(BudgetLimits(), max_calls=1, max_seconds=1000, max_tokens=200000)
    newest, learning_worker.EXTRACTION_CONTRACT = learning_worker.EXTRACTION_CONTRACT, 1
    try:
        result = LearningWorker(ledger, provider, WorkerSettings(), limits).run_once()
    finally:
        learning_worker.EXTRACTION_CONTRACT = newest
    assert result["status"] == "deferred" and result["error"] == "BudgetExceeded"
    assert provider.offsets == [0]
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE status='pending'")
    return limits


def progress(ledger):
    return json.loads(ledger.conn.execute("SELECT progress FROM learning_jobs").fetchone()[0])


def pages(ledger):
    return ledger.conn.execute("SELECT count(*) FROM learning_extraction_pages").fetchone()[0]


def test_budget_raise_resumes_a_budget_paused_job(ledger):
    provider = Pages()
    limits = budget_paused_job(ledger, provider)
    raised = replace(limits, max_calls=2500, max_tokens=110000000, max_seconds=450000)
    settings = WorkerSettings(enable_consolidation=True, max_attempts=10, timeout_seconds=180)
    result = LearningWorker(ledger, provider, settings, raised).run_once()
    assert result["status"] == "done"
    # The completed first window is not asked again.
    assert (
        sorted(set(provider.offsets)) == provider.offsets
        and len(provider.offsets) == pages(ledger) > 2
    )


def test_job_frozen_by_0_1_24_resumes_after_budget_raise_and_retry(ledger):
    provider = Pages()
    limits = budget_paused_job(ledger, provider)
    run = LearningWorker(ledger, provider, WorkerSettings(), limits)
    state = progress(ledger)
    sources = ledger._job_sources(ledger.conn.execute("SELECT id FROM learning_jobs").fetchone()[0])
    old = release_0_1_24_configuration(run)
    frozen = input_fingerprint(sources, run.windows(sources), old)
    ledger.conn.execute("UPDATE learning_extraction_pages SET fingerprint=?", (frozen,))
    state.update(execution_configuration=old, execution_fingerprint=frozen, fingerprint=frozen)
    # 0.1.24 exhausted this job when the budget changed; the operator retried it.
    ledger.conn.execute(
        "UPDATE learning_jobs SET progress=?,status='exhausted'", (json.dumps(state),)
    )
    ledger.retry(ledger.conn.execute("SELECT id FROM learning_jobs").fetchone()[0])
    raised = replace(limits, max_calls=2500, max_tokens=110000000, max_seconds=450000)
    settings = WorkerSettings(enable_consolidation=True)
    result = LearningWorker(ledger, provider, settings, raised).run_once()
    assert result["status"] == "done"
    assert (
        sorted(set(provider.offsets)) == provider.offsets
        and len(provider.offsets) == pages(ledger) > 2
    )
    after = progress(ledger)
    # The original freeze and its proof stay, so a rollback to 0.1.24 still matches.
    assert after["fingerprint"] == after["execution_fingerprint"] == frozen
    assert after["execution_configuration"] == old


def test_model_change_still_fails_a_budget_paused_job(ledger):
    provider = Pages()
    limits = budget_paused_job(ledger, provider)
    provider.model = "other:8b"
    raised = replace(limits, max_calls=2500)
    result = LearningWorker(ledger, provider, WorkerSettings(), raised).run_once()
    assert result["status"] == "failed" and result["permanent"]
    assert provider.offsets == [0]
    error = ledger.conn.execute("SELECT last_error FROM learning_jobs").fetchone()[0]
    assert error == "ValidationError: window configuration changed during a job"
