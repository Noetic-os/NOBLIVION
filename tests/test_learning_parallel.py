# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parallel learning worker loops (NOBLIVION-242); never contact an actual model."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager

import pytest

from noblivion import db, learning_runtime
from noblivion.learning import BudgetLimits, LearningLedger, LeaseLost
from noblivion.learning_worker import ProviderResponse, WorkerSettings

LONG = "Routine context. " * 300  # two extraction windows of 4096 characters
SHORT = "Use short sentences in every project."


class FakeStore:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.db_path = tmp_path / "store.db"
        db.open_db(self.db_path, create=True).close()
        self._jobs_stop = threading.Event()

    @contextmanager
    def connection(self):
        conn = db.connect(self.db_path, create=False)
        try:
            yield conn
        finally:
            conn.close()

    def scan_once(self, conn):
        return None


class GatedProvider:
    """Blocks each call until the gate opens; counts calls in flight."""

    def __init__(self, open_gate=False):
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.starts: list[int] = []
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.starts.append(self.inflight)
        try:
            assert self.gate.wait(20)
            time.sleep(0.02)
        finally:
            with self.lock:
                self.inflight -= 1
        return ProviderResponse('{"version":1,"candidates":[]}', 50)


def write_settings(tmp_path, concurrency, **budget):
    memory = tmp_path / "memories" / "example" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    limits = {"max_calls": 1000, "max_tokens": 10**9, "max_seconds": 10**7}
    limits.update(budget)
    if concurrency is not None:
        limits["max_concurrency"] = concurrency
    value = {
        "scopes": [{"project": "demo", "root": "example", "memory_dir": str(memory)}],
        "provider": {
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "test",
            "local": True,
            "cost_per_million_tokens": 0,
        },
        "worker": {"window_chars": 4096, "poll_seconds": 0.05},
        "budget": limits,
    }
    (tmp_path / "learning.json").write_text(json.dumps(value), encoding="utf-8")


def capture(store, count, text=LONG):
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        for index in range(count):
            ledger.capture_batch(
                {
                    "batch_id": f"batch-{index}",
                    "namespace": "demo",
                    "root": "example",
                    "session_id": f"session-{index}",
                    "source_client": "codex",
                    "records": [
                        {
                            "source_id": f"source-{index}",
                            "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                            "role": "user",
                            "content": text,
                            "observed_at": "2026-01-02T12:00:00Z",
                        }
                    ],
                }
            )


def rows(store, sql, args=()):
    with store.connection() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    provider = GatedProvider()

    def options(settings):
        return (
            provider,
            WorkerSettings(**settings.get("worker", {})),
            BudgetLimits(**settings.get("budget", {})),
        )

    monkeypatch.setattr(learning_runtime, "worker_options", options)
    store = FakeStore(tmp_path)
    pool = learning_runtime.LearningPool(store)
    yield store, pool, provider
    store._jobs_stop.set()
    provider.gate.set()
    pool.shutdown(timeout=10.0)
    assert pool.alive() == 0


def all_done(store, count):
    return lambda: len(rows(store, "SELECT id FROM learning_jobs WHERE status='done'")) == count


def assert_each_job_leased_once(store):
    jobs = rows(store, "SELECT id,attempts,last_error FROM learning_jobs")
    assert all(job["attempts"] == 1 and job["last_error"] == "" for job in jobs)
    tokens = rows(
        store, "SELECT job_id,count(DISTINCT lease_token) n FROM learning_budget GROUP BY job_id"
    )
    assert tokens and all(row["n"] == 1 for row in tokens)


def test_three_workers_run_three_calls_at_once(tmp_path, pool_env):
    store, pool, provider = pool_env
    capture(store, 6)
    write_settings(tmp_path, 3)
    pool.pass_once()
    wait_for(lambda: provider.peak == 3)
    assert len(rows(store, "SELECT id FROM learning_budget WHERE status='reserved'")) == 3
    owners = rows(store, "SELECT DISTINCT lease_owner FROM learning_jobs WHERE status='leased'")
    assert len(owners) == 3
    provider.gate.set()
    wait_for(all_done(store, 6))
    assert provider.peak == 3
    assert_each_job_leased_once(store)


def test_default_is_one_worker_and_one_call(tmp_path, pool_env):
    store, pool, provider = pool_env
    capture(store, 3)
    write_settings(tmp_path, None)
    provider.gate.set()
    pool.pass_once()
    wait_for(all_done(store, 3))
    assert pool.desired == 1 and pool.alive() == 1
    assert provider.peak == 1


def test_parallel_calls_never_exceed_the_call_budget(tmp_path, pool_env):
    store, pool, provider = pool_env
    capture(store, 8, text=SHORT)
    write_settings(tmp_path, 4, max_calls=3)
    provider.gate.set()
    pool.pass_once()

    def settled():
        jobs = rows(store, "SELECT status,last_error FROM learning_jobs")
        done = sum(job["status"] == "done" for job in jobs)
        deferred = sum(
            job["status"] == "pending" and job["last_error"] == "BudgetExceeded" for job in jobs
        )
        return done + deferred == len(jobs)

    wait_for(settled)
    used = rows(store, "SELECT count(*) n,coalesce(sum(tokens),0) t FROM learning_budget")[0]
    assert used["n"] == 3 and len(provider.starts) == 3
    assert len(rows(store, "SELECT id FROM learning_jobs WHERE status='done'")) == 3


def test_scale_down_from_three_to_one_without_deferral(tmp_path, pool_env):
    store, pool, provider = pool_env
    capture(store, 6)
    write_settings(tmp_path, 3)
    pool.pass_once()
    wait_for(lambda: provider.peak == 3)
    write_settings(tmp_path, 1)
    pool.pass_once()
    assert pool.desired == 1 and pool.slots.limit == 1
    mark = len(provider.starts)
    provider.gate.set()
    wait_for(all_done(store, 6))
    assert provider.starts[mark:] and max(provider.starts[mark:]) == 1
    wait_for(lambda: pool.alive() == 1)
    assert_each_job_leased_once(store)


def test_scale_up_mid_job_uses_the_current_limit(tmp_path, pool_env):
    store, pool, provider = pool_env
    capture(store, 4)
    write_settings(tmp_path, 1)
    pool.pass_once()
    wait_for(lambda: provider.peak == 1)
    # The first job started under limit 1 and holds a reservation. Its next
    # call must not fail on that old limit while two new workers hold calls.
    write_settings(tmp_path, 3)
    pool.pass_once()
    wait_for(lambda: provider.peak == 3)
    provider.gate.set()
    wait_for(all_done(store, 4))
    assert pool.alive() == 3
    assert_each_job_leased_once(store)


def test_invalid_settings_stop_the_workers(tmp_path, pool_env):
    store, pool, provider = pool_env
    write_settings(tmp_path, 2)
    pool.pass_once()
    assert pool.alive() == 2
    (tmp_path / "learning.json").write_text("{not json", encoding="utf-8")
    pool.pass_once()
    assert pool.desired == 0
    wait_for(lambda: pool.alive() == 0)


def claimed_job(store):
    capture(store, 1, text=SHORT)
    with store.connection() as conn:
        return LearningLedger(conn).claim("holder", lease_seconds=120)


def test_stop_while_waiting_gives_the_job_back(pool_env):
    store, pool, _ = pool_env
    job = claimed_job(store)
    pool.slots.set_limit(1)
    assert pool.slots.acquire(store._jobs_stop, lambda: None)
    store._jobs_stop.set()
    with store.connection() as conn:
        with pytest.raises(LeaseLost):
            learning_runtime.PooledLedger(conn, pool).reserve(
                job["id"],
                job["lease_token"],
                limits=BudgetLimits(),
                estimated_tokens=10,
                estimated_seconds=1,
                estimated_cost=0,
            )
    (row,) = rows(store, "SELECT status,attempts,last_error,next_attempt FROM learning_jobs")
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["last_error"] == "LearningWorkerStopped" and row["next_attempt"] <= time.time()
    assert rows(store, "SELECT id FROM learning_budget") == []


def test_waiting_for_a_slot_renews_the_lease(pool_env, monkeypatch):
    store, pool, _ = pool_env
    monkeypatch.setattr(learning_runtime, "SLOT_WAIT_SECONDS", 0.05)
    job = claimed_job(store)
    pool.slots.set_limit(1)
    assert pool.slots.acquire(store._jobs_stop, lambda: None)
    (before,) = rows(store, "SELECT lease_until FROM learning_jobs")
    result = {}

    def reserve():
        with store.connection() as conn:
            ledger = learning_runtime.PooledLedger(conn, pool)
            result["reservation"] = ledger.reserve(
                job["id"],
                job["lease_token"],
                limits=BudgetLimits(),
                estimated_tokens=10,
                estimated_seconds=1,
                estimated_cost=0,
            )
            ledger.settle(result["reservation"]["id"], actual_tokens=10)

    thread = threading.Thread(target=reserve)
    thread.start()
    wait_for(
        lambda: (
            rows(store, "SELECT lease_until FROM learning_jobs")[0]["lease_until"]
            > before["lease_until"]
        )
    )
    assert "reservation" not in result
    pool.slots.release()
    thread.join(10)
    assert result["reservation"] and pool.slots.used == 0


def test_renew_lease_never_revives_a_lost_lease(tmp_path):
    store = FakeStore(tmp_path)
    job = claimed_job(store)
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        with pytest.raises(LeaseLost):
            ledger.renew_lease(job["id"], "not-the-token", 60)
        ledger.renew_lease(job["id"], job["lease_token"], 1)
        (row,) = conn.execute("SELECT lease_until FROM learning_jobs").fetchall()
        assert row["lease_until"] == job["lease_until"]  # never shortened


def test_concurrent_claims_lease_each_job_once_without_lock_errors(tmp_path):
    store = FakeStore(tmp_path)
    capture(store, 40, text=SHORT)
    claimed: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index):
        try:
            with store.connection() as conn:
                ledger = LearningLedger(conn)
                while (job := ledger.claim(f"w{index}", lease_seconds=120)) is not None:
                    reservation = ledger.reserve(
                        job["id"],
                        job["lease_token"],
                        limits=BudgetLimits(max_calls=1000, max_concurrency=8),
                        estimated_tokens=10,
                        estimated_seconds=1,
                        estimated_cost=0,
                    )
                    ledger.checkpoint(job["id"], job["lease_token"], {"step": index})
                    ledger.settle(reservation["id"], actual_tokens=10)
                    with lock:
                        claimed.append(job["id"])
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    assert len(claimed) == 40 and len(set(claimed)) == 40


@pytest.mark.parametrize(
    ("budget", "enabled", "expected"),
    [
        ({}, True, 1),
        ({"max_concurrency": 0}, True, 0),
        ({"max_concurrency": 2.5}, True, 3),
        ({"max_concurrency": 100}, True, learning_runtime.MAX_WORKERS),
        ({"max_concurrency": 4}, False, 0),
    ],
)
def test_worker_count(budget, enabled, expected):
    settings = {
        "enabled": enabled,
        "provider": {
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "test",
            "local": True,
            "cost_per_million_tokens": 0,
        },
        "budget": budget,
    }
    assert learning_runtime.worker_count(settings) == expected
