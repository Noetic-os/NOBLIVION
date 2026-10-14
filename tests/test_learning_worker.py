# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provider boundary and worker failure tests; never contact an actual model."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from noblivion.learning_worker import (
    LearningWorker,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResponse,
    WorkerSettings,
)


def source(text="Use short sentences in every project."):
    return dict(
        id="s1",
        content=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        role="user",
        source_client="codex",
        observed_at="2026-01-02T12:00:00Z",
        scope={"project": "demo", "root": "example"},
    )


class Ledger:
    def __init__(self, s=None):
        self.sources = [s or source()]
        self.events = []
        self.denied = False
        self.attempt = 0
        self.finished = False
        self.progress = {}

    def claim(self, *args, **kwargs):
        self.events.append("claim")
        if self.finished:
            return None
        self.attempt += 1
        return dict(
            id=1,
            lease_token="lease",
            attempts=self.attempt,
            project="demo",
            root="example",
            sources=self.sources,
            progress=self.progress,
        )

    def reconciliation_context(self, *args, **kwargs):
        return []

    def reserve(self, *args, **kwargs):
        self.events.append("reserve")
        if self.denied:
            raise RuntimeError("accounting unavailable")
        assert kwargs["estimated_tokens"] > 2048 and kwargs["estimated_cost"] == 0
        return {"id": len(self.events)}

    def checkpoint(self, job_id, lease, progress):
        self.progress = json.loads(json.dumps(progress))

    def settle(self, *args, **kwargs):
        self.events.append("settle")

    def finish(self, *args):
        self.events.append("finish")
        self.finished = True
        return args[-1]

    def fail(self, *args, **kwargs):
        self.events.append("fail")
        self.failure = kwargs


class Provider:
    def __init__(self, ledger, error=False):
        self.ledger = ledger
        self.error = error
        self.calls = 0

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, *args, **kwargs):
        assert self.ledger.events[-1] == "reserve"
        self.ledger.events.append("provider")
        self.calls += 1
        if self.error:
            raise ProviderError("temporary failure")
        return ProviderResponse('{"version":1,"candidates":[]}', 50)


def worker(ledger, provider, **kwargs):
    return LearningWorker(ledger, provider, WorkerSettings(**kwargs), SimpleNamespace())


def test_reservation_precedes_each_retry_and_no_source_ack_on_failure():
    ledger = Ledger()
    provider = Provider(ledger, error=True)
    run = worker(ledger, provider)
    assert run.run_once()["status"] == "failed"
    assert run.run_once()["status"] == "failed"
    assert ledger.events == [
        "claim",
        "reserve",
        "provider",
        "fail",
        "claim",
        "reserve",
        "provider",
        "fail",
    ]
    assert ledger.failure["retry_delay"] == 60
    assert not ledger.finished and provider.calls == 2


def test_missing_accounting_never_calls_provider():
    ledger = Ledger()
    ledger.denied = True
    provider = Provider(ledger)
    assert worker(ledger, provider).run_once()["status"] == "failed"
    assert provider.calls == 0 and "finish" not in ledger.events


def test_secret_tail_prevents_any_model_submission():
    ledger = Ledger(source("Normal context. " * 2000 + "My password is fictional-value."))
    provider = Provider(ledger)
    result = worker(ledger, provider).run_once()
    assert result["permanent"] and provider.calls == 0 and "reserve" not in ledger.events


def test_all_windows_accounted_before_finish():
    ledger = Ledger(source("Routine context. " * 1800 + "Final evidence."))
    provider = Provider(ledger)
    result = worker(ledger, provider, window_chars=4096).run_once()
    assert result["status"] == "done" and provider.calls > 1
    assert ledger.events.count("reserve") == ledger.events.count("settle") == provider.calls
    assert ledger.events[-1] == "finish"


def test_invalid_model_output_never_written():
    ledger = Ledger()
    provider = Provider(ledger)
    provider.complete = lambda *a, **k: ProviderResponse("not json")
    result = worker(ledger, provider).run_once()
    assert result["permanent"] and "finish" not in ledger.events
    assert "settle" in ledger.events  # malformed output still incurred a request


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "endpoint": "https://example.test/v1",
            "model": "sample",
            "local": True,
            "cost_per_million_tokens": 0,
        },
        {"endpoint": "http://127.0.0.1:1234/v1", "model": "sample"},
        {"endpoint": "http://127.0.0.1:1234/v1", "model": "sample", "local": True},
        {
            "endpoint": "http://127.0.0.1:1234/v1",
            "model": "sample",
            "local": True,
            "cost_per_million_tokens": 1,
        },
        {
            "endpoint": "http://127.0.0.1:1234/v1?target=example.test",
            "model": "sample",
            "local": True,
            "cost_per_million_tokens": 0,
        },
    ],
)
def test_provider_rejects_external_or_unpriced_configuration(kwargs):
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(**kwargs)


def test_local_provider_has_no_proxy_or_redirect_escape():
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:1234/v1", "sample", local=True, cost_per_million_tokens=0
    )
    assert provider.endpoint == "http://127.0.0.1:1234/v1/chat/completions"
    redirect = next(h for h in provider.opener.handlers if hasattr(h, "redirect_request"))
    with pytest.raises(ProviderError):
        redirect.redirect_request(None, None, 302, "", {}, "https://example.test")


def test_provider_rejects_truncated_response_without_exposing_body():
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:1234", "sample", local=True, cost_per_million_tokens=0
    )

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, n):
            if hasattr(self, "sent"):
                return b""
            self.sent = True
            return json.dumps(
                {"choices": [{"finish_reason": "length", "message": {"content": "private data"}}]}
            ).encode()

    provider.opener = SimpleNamespace(open=lambda *a, **k: Reply())
    with pytest.raises(ProviderError, match="incomplete"):
        provider.complete([], max_tokens=1, timeout=1)


def test_resume_skips_validated_windows_after_provider_failure():
    ledger = Ledger(source("Routine context. " * 1800 + "Final evidence."))
    provider = Provider(ledger)
    original = provider.complete

    def break_second(*args, **kwargs):
        if provider.calls == 1:
            provider.error = True
        return original(*args, **kwargs)

    provider.complete = break_second
    run = worker(ledger, provider, window_chars=4096)
    assert run.run_once()["status"] == "failed"
    assert ledger.progress["next_window"] == 1
    first_window_calls = provider.calls
    provider.error = False
    provider.complete = original
    assert run.run_once()["status"] == "done"
    assert first_window_calls == 2
    assert provider.calls == ledger.progress["next_window"] + 1


def test_real_ledger_budget_deferral_and_process_recovery(tmp_path):
    from noblivion import db
    from noblivion.learning import BudgetLimits, LearningLedger

    text = "Routine context. " * 700
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    ledger = LearningLedger(conn)
    receipt = ledger.capture_batch(
        {
            "batch_id": "resume-batch",
            "namespace": "demo",
            "root": "example",
            "session_id": "resume-session",
            "source_client": "codex",
            "records": [
                {
                    "source_id": "resume-source",
                    "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "role": "user",
                    "content": text,
                    "observed_at": "2026-01-02T12:00:00Z",
                }
            ],
        }
    )
    calls = []

    class ObservedProvider:
        def estimate_cost(self, tokens):
            return 0.0

        def complete(self, messages, **kwargs):
            assert (
                conn.execute(
                    "SELECT count(*) FROM learning_budget WHERE status='reserved'"
                ).fetchone()[0]
                == 1
            )
            parts = json.loads(messages[1]["content"])["sources"]
            calls.append(parts[0]["offset"])
            return ProviderResponse('{"version":1,"candidates":[]}', 50)

    provider = ObservedProvider()
    settings = WorkerSettings(window_chars=4096)
    limits = BudgetLimits(max_calls=1)
    for _ in range(10):
        # A fresh worker instance represents a restarted worker process. The
        # only progress available is in the durable ledger.
        result = LearningWorker(ledger, provider, settings, limits).run_once()
        if result["status"] == "done":
            break
        assert result["status"] == "deferred"
        job = conn.execute(
            "SELECT * FROM learning_jobs WHERE id=?", (receipt["job_id"],)
        ).fetchone()
        assert job["attempts"] == 0 and json.loads(job["progress"])["next_window"] >= 1
        conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
        conn.execute("UPDATE learning_jobs SET next_attempt=0")
    assert result["status"] == "done"
    assert len(calls) > 1 and len(calls) == len(set(calls))
    assert calls == sorted(calls)
    conn.close()


def test_real_ledger_unconfigured_scope_never_submits(tmp_path):
    from noblivion import db
    from noblivion.learning import BudgetLimits, LearningLedger

    conn = db.open_db(tmp_path / "isolated.db", create=True)
    ledger = LearningLedger(conn)
    s = source()
    ledger.capture_batch(
        {
            "batch_id": "scope-batch",
            "namespace": "demo",
            "root": "example",
            "session_id": "scope-session",
            "source_client": "codex",
            "records": [
                {
                    "source_id": "scope-source",
                    "source_hash": s["sha256"],
                    "role": "user",
                    "content": s["content"],
                    "observed_at": s["observed_at"],
                }
            ],
        }
    )
    result = LearningWorker(
        ledger, None, WorkerSettings(), BudgetLimits(), allowed_scopes=set()
    ).run_once()
    assert result["error"] == "ScopeNotEnabled"
    assert conn.execute("SELECT count(*) FROM learning_budget").fetchone()[0] == 0
    assert conn.execute("SELECT attempts FROM learning_jobs").fetchone()[0] == 0
    conn.close()


def test_two_workers_on_one_job_the_stale_one_stops_quietly(tmp_path, caplog):
    """A lease that expires mid-call hands the job to a second worker. The first
    worker must stop with no write and no pass failure, and the second finishes."""
    from noblivion import db
    from noblivion.learning import BudgetLimits, LearningLedger

    conn = db.open_db(tmp_path / "isolated.db", create=True)
    ledger = LearningLedger(conn)
    s = source()
    ledger.capture_batch(
        {
            "batch_id": "lease-batch",
            "namespace": "demo",
            "root": "example",
            "session_id": "lease-session",
            "source_client": "codex",
            "records": [
                {
                    "source_id": "lease-source",
                    "source_hash": s["sha256"],
                    "role": "user",
                    "content": s["content"],
                    "observed_at": s["observed_at"],
                }
            ],
        }
    )
    second = []

    class StealingProvider:
        """While the first worker waits on the model, its lease expires."""

        def estimate_cost(self, tokens):
            return 0.0

        def complete(self, messages, **kwargs):
            if not second:
                conn.execute("UPDATE learning_jobs SET lease_until=0")
                second.append(ledger.claim("worker-b"))
            return ProviderResponse('{"version":1,"candidates":[]}', 50)

    scope = {("demo", "example")}
    first = LearningWorker(
        ledger,
        StealingProvider(),
        WorkerSettings(),
        BudgetLimits(),
        worker_id="worker-a",
        allowed_scopes=scope,
    )
    with caplog.at_level("WARNING"):
        result = first.run_once()
    assert result == {"status": "lease_lost"}
    assert not caplog.records
    job = second[0]
    assert job["lease_owner"] == "worker-b" and job["attempts"] == 2
    row = conn.execute("SELECT status,lease_owner FROM learning_jobs").fetchone()
    assert (row["status"], row["lease_owner"]) == ("leased", "worker-b")
    # The new owner is not disturbed by the stale worker and completes the job.
    ledger.finish(job["id"], job["lease_token"], [])
    assert conn.execute("SELECT status FROM learning_jobs").fetchone()[0] == "done"
    conn.close()
