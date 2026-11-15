# SPDX-License-Identifier: AGPL-3.0-or-later
"""Learning work keeps an idle store up; a clean stop gives jobs back (NOBLIVION-248).

Before 0.1.30 the store stopped `idle_exit_s` after the last client request,
also while its learning workers ran a job. The lease then expired, and each
stop cost each leased job one of its attempts. Fictional data only; no model.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from noblivion import learning_runtime
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import WorkerSettings
from test_learning_parallel import GatedProvider
from test_store import Running, make_store, wait_for

TEXT = "Use short sentences in every project."


def write_learning(st, **budget):
    memory = st.settings.memory_dirs[0]
    memory.mkdir(parents=True, exist_ok=True)
    limits = {"max_calls": 1000, "max_tokens": 10**9, "max_seconds": 10**7}
    limits.update(budget)
    value = {
        "scopes": [
            {"project": st.namespace, "root": memory.parent.name, "memory_dir": str(memory)}
        ],
        "provider": {
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "test",
            "local": True,
            "cost_per_million_tokens": 0,
        },
        "worker": {"poll_seconds": 0.05},
        "budget": limits,
    }
    (st.data_dir / "learning.json").write_text(json.dumps(value), encoding="utf-8")
    return memory.parent.name


def capture(st, root, ident="one"):
    with st.connection() as conn:
        LearningLedger(conn).capture_batch(
            {
                "batch_id": "batch-" + ident,
                "namespace": st.namespace,
                "root": root,
                "session_id": "session-" + ident,
                "source_client": "codex",
                "records": [
                    {
                        "source_id": "source-" + ident,
                        "source_hash": hashlib.sha256(TEXT.encode()).hexdigest(),
                        "role": "user",
                        "content": TEXT,
                        "observed_at": "2026-01-02T12:00:00Z",
                    }
                ],
            }
        )


def jobs(st):
    with st.connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT status,attempts,last_error,lease_owner FROM learning_jobs"
            ).fetchall()
        ]


@pytest.fixture
def provider(monkeypatch):
    gated = GatedProvider()

    def options(settings):
        return (
            gated,
            WorkerSettings(**settings.get("worker", {})),
            BudgetLimits(**settings.get("budget", {})),
        )

    monkeypatch.setattr(learning_runtime, "worker_options", options)
    monkeypatch.setattr(learning_runtime, "STOP_JOIN_SECONDS", 0.3)
    yield gated
    gated.gate.set()


def start(tmp_path, **budget):
    st = make_store(tmp_path, store_settings={"idle_exit_s": 1})
    root = write_learning(st, **budget)
    run = Running(st)
    capture(st, root)
    return st, run


def test_a_leased_job_keeps_an_idle_store_up(tmp_path, provider):
    st, run = start(tmp_path)
    try:
        assert wait_for(lambda: provider.inflight == 1)
        time.sleep(2.5)  # past the idle limit of 1 s, with no client request
        assert run.thread.is_alive() and st.stop_reason != "idle"
        assert [j["status"] for j in jobs(st)] == ["leased"]
        provider.gate.set()
        # The job ends; nothing is left to do, so the idle stop follows.
        run.thread.join(15)
        assert not run.thread.is_alive() and st.stop_reason == "idle"
        assert [(j["status"], j["attempts"]) for j in jobs(st)] == [("done", 1)]
    finally:
        provider.gate.set()
        if run.thread.is_alive():
            st.request_stop()
            run.thread.join(15)


@pytest.mark.parametrize("reason", ["SIGTERM", "idle"])
def test_a_clean_stop_gives_the_job_back_with_its_attempts(tmp_path, provider, reason):
    st, run = start(tmp_path)
    try:
        assert wait_for(lambda: provider.inflight == 1)
        assert [(j["status"], j["attempts"]) for j in jobs(st)] == [("leased", 1)]
        if reason == "SIGTERM":
            st.request_signal_stop("SIGTERM")
        else:
            # The worker is still in its call, but its learning work no longer
            # counts: the idle stop runs the same clean shutdown.
            st._learning_pool = None
        run.thread.join(15)
        assert not run.thread.is_alive() and st.stop_reason == reason
        # Pending again, with the attempt the claim counted given back.
        expected = [
            {
                "status": "pending",
                "attempts": 0,
                "last_error": "LearningWorkerStopped",
                "lease_owner": None,
            }
        ]
        assert jobs(st) == expected
        # The call in flight ends later; its lease is gone, so it writes nothing.
        provider.gate.set()
        assert wait_for(lambda: provider.inflight == 0)
        time.sleep(0.2)
        assert jobs(st) == expected
    finally:
        provider.gate.set()


def test_a_job_the_budget_blocks_does_not_keep_the_store_up(tmp_path, provider):
    st, run = start(tmp_path, max_calls=0)
    try:
        run.thread.join(15)
        assert not run.thread.is_alive() and st.stop_reason == "idle"
        assert provider.starts == []
        assert all(j["status"] == "pending" for j in jobs(st))
    finally:
        provider.gate.set()


def test_work_open_counts_only_jobs_a_claim_would_take(tmp_path):
    st = make_store(tmp_path)
    st.open()
    try:
        root = write_learning(st)
        capture(st, root)
        scopes = {(st.namespace, root)}
        limits = BudgetLimits(max_calls=10, max_seconds=1000)
        with st.connection() as conn:
            ledger = LearningLedger(conn)

            def open_(**kw):
                args = {"max_attempts": 4, "limits": limits, "call_seconds": 60}
                args.update(kw)
                return ledger.work_open(args.pop("owners", ()), args.pop("scopes", scopes), **args)

            assert open_()
            assert not open_(scopes=set())  # scope not enabled
            assert not open_(limits=None)  # learning off
            assert not open_(call_seconds=2000)  # no room for one call
            assert not open_(max_attempts=0)  # no attempts left
            ledger.set_controls(st.namespace, root, paused=True)
            assert not open_()  # paused
            ledger.set_controls(st.namespace, root, paused=False)
            job = ledger.claim("worker-a")
            assert job is not None
            assert open_(owners={"worker-a"}, scopes=set(), limits=None)
            assert not open_(owners={"worker-b"})  # another store's lease
            assert ledger.release_leases({"worker-b"}) == 0
            assert ledger.release_leases({"worker-a"}) == 1
            row = conn.execute("SELECT status,attempts,lease_token FROM learning_jobs").fetchone()
            assert tuple(row) == ("pending", 0, None)
    finally:
        st.close()
