# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-261: reasoning tokens per call, and a reasoning effort change keeps started jobs."""

import json
from dataclasses import replace

import pytest

from noblivion import db
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import (
    LearningWorker,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResponse,
    WorkerSettings,
    output_usage,
    request_configuration,
    worker_fingerprint,
    worker_matches,
)
from test_learning_fingerprint import LIVE_FILE, Pages, budget_paused_job, progress
from test_learning_review_fixes import capture


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def provider(effort="low"):
    return OpenAICompatibleProvider(**{**LIVE_FILE["provider"], "reasoning_effort": effort})


def answer(content='{"version":1,"candidates":[]}', finish="stop", **message):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, **message},
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150},
    }


def test_none_reaches_the_request_body_of_a_call_and_a_probe():
    sent = []
    local = provider("none")
    local._post = lambda payload, timeout: sent.append(payload) or answer()
    local.complete([{"role": "user", "content": "x"}], max_tokens=10, timeout=1)
    local.probe(timeout=1)
    assert [payload["reasoning_effort"] for payload in sent] == ["none", "none"]


def test_an_unknown_effort_is_refused():
    with pytest.raises(ValueError):
        provider("medium")


def test_reported_reasoning_tokens_are_used_as_they_are():
    usage = {"completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 70}}
    assert output_usage(usage, {"reasoning": "x" * 10, "content": "y"}) == {
        "completion_tokens": 100,
        "reasoning_tokens": 70,
    }


def test_reasoning_text_gives_a_marked_estimate():
    # 300 characters of thinking and 100 of answer: three quarters of 100 tokens.
    message = {"reasoning": "x" * 300, "content": "y" * 100}
    assert output_usage({"completion_tokens": 100}, message) == {
        "completion_tokens": 100,
        "reasoning_tokens": 75,
        "reasoning_estimated": True,
    }
    assert (
        output_usage({"completion_tokens": 100}, {"reasoning_content": "x", "content": ""})[
            "reasoning_tokens"
        ]
        == 100
    )


def test_a_null_reasoning_field_is_zero_and_no_field_is_unknown():
    assert output_usage({"completion_tokens": 9}, {"reasoning": None, "content": "y"}) == {
        "completion_tokens": 9,
        "reasoning_tokens": 0,
        "reasoning_estimated": True,
    }
    assert output_usage({"completion_tokens": 9}, {"content": "y"}) == {"completion_tokens": 9}
    assert output_usage({"completion_tokens": -1}, {}) == {}
    assert output_usage(None, {}) == {}


def test_the_provider_returns_output_counts_also_for_a_cut_off_answer():
    local = provider()
    local._post = lambda payload, timeout: answer(reasoning="x" * 300, content="y" * 100)
    response = local.complete([{"role": "user", "content": "x"}], max_tokens=10, timeout=1)
    assert response.output["reasoning_tokens"] == 75
    local._post = lambda payload, timeout: answer(finish="length", reasoning="x" * 10)
    with pytest.raises(ProviderError) as cut:
        local.complete([{"role": "user", "content": "x"}], max_tokens=10, timeout=1)
    assert cut.value.reason == "length" and cut.value.output["completion_tokens"] == 100


@pytest.mark.parametrize(
    "finish,content,reason",
    [("stop", "  ", "empty"), ("stop", None, "invalid"), ("tool_calls", "y", "incomplete")],
)
def test_an_all_thinking_answer_that_fails_still_reports_its_output(finish, content, reason):
    local = provider()
    thinking = "x" * 300
    local._post = lambda payload, timeout: answer(
        finish=finish, reasoning=thinking, content=content
    )
    with pytest.raises(ProviderError) as failed:
        local.complete([{"role": "user", "content": "x"}], max_tokens=10, timeout=1)
    assert failed.value.reason == reason
    # Before the fix these errors carried no output, so the thinking was not counted.
    assert failed.value.output is not None and failed.value.output["reasoning_tokens"] >= 75


class Thinking:
    """Answers with no lesson and reports 100 output tokens, 60 of them thinking."""

    reasoning_effort = "low"

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        output = {"completion_tokens": 100, "reasoning_tokens": 60, "reasoning_estimated": True}
        return ProviderResponse('{"version":1,"candidates":[]}', 150, output)


def test_status_shows_reasoning_tokens_per_call(ledger):
    capture(ledger, "new", "Routine context. " * 1100)
    result = LearningWorker(ledger, Thinking(), WorkerSettings(), BudgetLimits()).run_once()
    assert result["status"] == "done"
    metrics = progress(ledger)["provider_metrics"]
    calls = metrics["calls"]
    assert calls >= 1 and metrics["reasoning_calls"] == metrics["output_calls"] == calls
    actual = ledger.status()["provider_actual"]
    assert actual["reasoning_tokens"] == 60 * calls and actual["reasoning_estimated"] == calls
    assert actual["reasoning_tokens_per_call"] == 60.0
    assert actual["completion_tokens_per_call"] == 100.0


def test_status_has_no_mean_when_no_call_reported_reasoning(ledger):
    actual = ledger.status()["provider_actual"]
    assert actual["reasoning_tokens_per_call"] is None and actual["reasoning_calls"] == 0


class Efforts(Pages):
    """Pages that also records the reasoning effort of every call."""

    def __init__(self, effort="low"):
        super().__init__()
        self.reasoning_effort = effort
        self.sent = []

    def complete(self, messages, **kwargs):
        self.sent.append(self.reasoning_effort)
        return super().complete(messages, **kwargs)


def test_a_started_job_keeps_its_frozen_effort_after_a_change(ledger):
    first = Efforts("low")
    limits = budget_paused_job(ledger, first)
    frozen = progress(ledger)["execution_fingerprint"]
    second = Efforts("none")
    second.offsets = first.offsets
    worker = LearningWorker(ledger, second, WorkerSettings(), replace(limits, max_calls=2500))
    result = worker.run_once()
    assert result["status"] == "done"
    # The rest of the job ran with "low", never mixed with "none".
    assert first.sent == ["low"] and set(second.sent) == {"low"} and len(second.sent) > 1
    after = progress(ledger)
    assert after["execution_fingerprint"] == frozen
    assert after["execution_configuration"]["reasoning_effort"] == "low"
    # The worker gives the configured provider back after the job.
    assert worker.provider is second and second.reasoning_effort == "none"


def test_a_model_change_still_fails_a_job_frozen_under_another_effort(ledger):
    first = Efforts("low")
    limits = budget_paused_job(ledger, first)
    second = Efforts("none")
    second.model = "other:8b"
    result = LearningWorker(
        ledger, second, WorkerSettings(), replace(limits, max_calls=2500)
    ).run_once()
    assert result["status"] == "failed" and result["permanent"]
    assert second.sent == []


def test_a_job_with_no_work_freezes_again_under_the_new_effort(ledger):
    capture(ledger, "new", "Routine context. " * 1100)
    first = Efforts("low")
    # A token budget below one call: the job freezes, then pauses before any call.
    small = replace(BudgetLimits(), max_tokens=10)
    result = LearningWorker(ledger, first, WorkerSettings(), small).run_once()
    assert result["status"] == "deferred" and first.sent == []
    assert progress(ledger)["execution_configuration"]["reasoning_effort"] == "low"
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE status='pending'")
    second = Efforts("none")
    result = LearningWorker(ledger, second, WorkerSettings(), BudgetLimits()).run_once()
    assert result["status"] == "done"
    assert set(second.sent) == {"none"}
    after = progress(ledger)
    assert after["refrozen_effort"] == "low"
    assert after["execution_configuration"]["reasoning_effort"] == "none"


def test_a_started_derivation_job_matches_under_its_frozen_effort(ledger):
    old = LearningWorker(ledger, provider("low"), WorkerSettings(), BudgetLimits())
    frozen = old._configuration()
    state = {
        "worker_fingerprint": worker_fingerprint(request_configuration(frozen)),
        "worker_configuration": frozen,
    }
    new = LearningWorker(ledger, provider("none"), WorkerSettings(), BudgetLimits())
    assert not worker_matches(state, new._configuration())
    assert new._keep_frozen_effort(state)
    assert worker_matches(state, new._configuration())
    assert new._configuration()["reasoning_effort"] == "low"


def test_an_output_limit_hold_or_a_bad_stored_effort_is_not_pinned(ledger):
    worker = LearningWorker(ledger, provider("none"), WorkerSettings(), BudgetLimits())
    low = {"execution_configuration": {"reasoning_effort": "low"}}
    assert not worker._keep_frozen_effort({**low, "output_limit_hold": "a" * 64})
    assert not worker._keep_frozen_effort({"execution_configuration": {"reasoning_effort": "x"}})
    assert not worker._keep_frozen_effort({})
    assert worker.provider.reasoning_effort == "none"


def started_consolidation(tmp_path):
    """A consolidation job frozen under "low" that stopped on an outage."""
    from test_learning_pages import DenseProvider, run, seed

    conn = db.open_db(tmp_path / "derivation.db", create=True)
    ledger, _ = seed(conn, 1)
    provider = DenseProvider()
    provider.reasoning_effort = "low"
    assert run(ledger, provider, enable_consolidation=True)["status"] == "done"
    provider.complete = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("outage"))
    assert run(ledger, provider, enable_consolidation=True)["status"] == "failed"
    started = json.loads(
        conn.execute("SELECT progress FROM learning_jobs WHERE kind='consolidate'").fetchone()[0]
    )
    assert started["worker_configuration"]["reasoning_effort"] == "low"
    conn.execute("UPDATE learning_jobs SET next_attempt=0")
    return conn, ledger, run


class Outage:
    """Records the effort of each call, then fails it as an outage."""

    def __init__(self, effort):
        self.reasoning_effort = effort
        self.sent = []

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        self.sent.append(self.reasoning_effort)
        raise OSError("outage")


def test_a_started_consolidation_job_keeps_its_frozen_effort(tmp_path):
    conn, ledger, run = started_consolidation(tmp_path)
    provider = Outage("none")
    # The frozen provider type is test_learning_pages.DenseProvider. Keep that
    # identity and change only the effort; a bound method of the original
    # instance would not see the effort of the worker's per-job copy.
    provider.__class__ = type("DenseProvider", (Outage,), {"__module__": "test_learning_pages"})
    result = run(ledger, provider, enable_consolidation=True)
    # The job passed the frozen checks and called the model with "low".
    assert result["error"] == "Provider:transport" and not result["permanent"]
    assert provider.sent == ["low"]
    conn.close()


def test_a_model_change_still_fails_a_started_consolidation_job(tmp_path):
    conn, ledger, run = started_consolidation(tmp_path)
    provider = Outage("none")
    provider.__class__ = type("DenseProvider", (Outage,), {"__module__": "test_learning_pages"})
    provider.model = "other:8b"
    result = run(ledger, provider, enable_consolidation=True)
    assert result["error"] == "ValidationError" and result["permanent"]
    assert provider.sent == []
    conn.close()
