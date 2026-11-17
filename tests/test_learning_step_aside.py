# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-260: learning steps aside on a shared local model server.

Never contacts a model. The metrics endpoint is a fake HTTP server on loopback.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import test_learning_parallel as pool_fixtures
from noblivion import learning, learning_probe, learning_runtime
from noblivion.learning import BudgetLimits, LearningLedger, StepAside
from noblivion.learning_probe import LoadProbe, parse_gauge, probe_options
from noblivion.learning_worker import LearningWorker, WorkerSettings
from test_learning_parallel import (
    FakeStore,
    GatedProvider,
    all_done,
    capture,
    reserve_one,
    rows,
    wait_for,
    write_settings,
)

pool_env = pool_fixtures.pool_env  # the parallel-loop fixture of the store pool
GAUGE = "num_requests_waiting"


class FakeMetrics:
    """A Prometheus-style page on loopback whose gauge a test sets."""

    def __init__(self):
        self.page = f'# TYPE {GAUGE} gauge\n{GAUGE}{{model_name="m"}} 0.0\n'
        self.status = 200
        self.location = None
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                outer.requests += 1
                if outer.location:
                    self.send_response(302)
                    self.send_header("Location", outer.location)
                    self.end_headers()
                    return
                body = outer.page.encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/metrics"

    def waiting(self, value):
        self.page = f'# TYPE {GAUGE} gauge\n{GAUGE}{{model_name="m"}} {float(value)}\n'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def metrics():
    fake = FakeMetrics()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def fresh_probe_reads(monkeypatch):
    monkeypatch.setattr(learning_probe, "CACHE_SECONDS", 0.0)


def options(url, **extra):
    return probe_options({"load_probe": {"url": url, **extra}})


# --- the probe -------------------------------------------------------------


def test_parse_gauge_sums_series_and_ignores_other_names():
    page = "\n".join(
        [
            f"# HELP {GAUGE} help text {GAUGE} 99",
            f"# TYPE {GAUGE} gauge",
            f'{GAUGE}{{model_name="a"}} 2.0',
            f'{GAUGE}{{model_name="b",x="}}"}} 1',
            f"{GAUGE}_total 50",
            f"{GAUGE}x 70",
            f"other_{GAUGE} 80",
            "",
        ]
    )
    assert parse_gauge(page, GAUGE) == 3.0
    assert parse_gauge(f"{GAUGE} 4 1700000000000", GAUGE) == 4.0
    assert parse_gauge("nothing_here 1\n", GAUGE) is None
    assert parse_gauge(f"{GAUGE} NaN\n{GAUGE} -1\n{GAUGE} abc\n", GAUGE) is None


@pytest.mark.parametrize(
    "block",
    [
        {"url": "http://example.com/metrics"},
        {"url": "https://127.0.0.1/metrics"},
        {"url": "http://user:pw@127.0.0.1/metrics"},
        {"url": "http://127.0.0.1/metrics", "gauge": "bad name"},
        {"url": "http://127.0.0.1/metrics", "extra": 1},
        {"url": "http://127.0.0.1/metrics", "max_waiting": -1},
        {"url": "http://127.0.0.1/metrics", "timeout_seconds": 0},
        {"url": "http://127.0.0.1/metrics", "max_backoff_seconds": True},
        {"url": 7},
        "http://127.0.0.1/metrics",
    ],
    ids=str,
)
def test_probe_options_rejects_malformed_blocks(block):
    with pytest.raises(ValueError):
        probe_options({"load_probe": block})


def test_probe_options_defaults_and_absent_block():
    assert probe_options({}) is None
    assert options("http://127.0.0.1:9/metrics") == {
        "url": "http://127.0.0.1:9/metrics",
        "gauge": GAUGE,
        "max_waiting": 0,
        "timeout_seconds": 2.0,
        "max_backoff_seconds": 30.0,
    }


def test_probe_reads_the_gauge_and_holds_while_requests_wait(metrics):
    probe = LoadProbe(options(metrics.url))
    assert probe.waiting() == 0.0 and probe.hold() == 0.0
    metrics.waiting(2)
    assert probe.waiting() == 2.0
    assert 0 < probe.hold() <= 1.0
    metrics.waiting(0)
    assert probe.hold() == 0.0


def test_probe_max_waiting_is_the_tolerated_queue(metrics):
    probe = LoadProbe(options(metrics.url, max_waiting=2))
    metrics.waiting(2)
    assert probe.hold() == 0.0
    metrics.waiting(3)
    assert probe.hold() > 0


def test_backoff_doubles_to_its_bound_and_restarts_when_the_queue_empties(metrics, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(learning_probe.time, "monotonic", lambda: clock[0])
    probe = LoadProbe(options(metrics.url, max_backoff_seconds=8))
    metrics.waiting(5)
    waits = []
    for _ in range(7):
        wait = probe.hold()
        waits.append(wait)
        # Loops that ask during the hold share it and do not double it.
        assert probe.hold() == pytest.approx(wait)
        clock[0] += wait
    assert waits == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]
    metrics.waiting(0)
    assert probe.hold() == 0.0
    metrics.waiting(1)
    assert probe.hold() == 1.0


def test_an_unreadable_probe_lets_learning_go_on_and_warns_once(metrics, caplog):
    probe = LoadProbe(options(metrics.url))
    metrics.waiting(4)
    assert probe.hold() > 0
    with caplog.at_level(logging.INFO, logger=learning_probe.log.name):
        metrics.status = 503
        assert probe.hold() == 0.0 and probe.waiting() is None
        assert probe.hold() == 0.0
        metrics.status = 200
        metrics.page = "unrelated_metric 1\n"  # the gauge is missing
        assert probe.hold() == 0.0
        metrics.waiting(1)
        assert probe.hold() > 0
        assert probe.waiting() == 1.0
    unreadable = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(unreadable) == 1 and unreadable[0].levelno == logging.WARNING


def test_a_closed_port_is_unreadable_not_an_error():
    probe = LoadProbe(options("http://127.0.0.1:9/metrics", timeout_seconds=0.5))
    assert probe.waiting() is None and probe.hold() == 0.0


def test_a_redirect_is_never_followed(metrics):
    other = FakeMetrics()
    try:
        other.waiting(9)
        metrics.location = other.url
        probe = LoadProbe(options(metrics.url))
        assert probe.waiting() is None
        assert other.requests == 0
    finally:
        other.close()


def test_a_page_over_the_size_bound_is_unreadable(metrics, monkeypatch):
    monkeypatch.setattr(learning_probe, "MAX_PAGE_BYTES", 64)
    metrics.page = "# " + "x" * 200 + f"\n{GAUGE} 3\n"
    assert LoadProbe(options(metrics.url)).waiting() is None


# --- the busy-share cap ----------------------------------------------------


class Clock:
    def __init__(self, monkeypatch):
        self.now = time.time()
        monkeypatch.setattr(learning.time, "time", lambda: self.now)


def busy_over(calls, end, window=3600.0):
    return sum(max(0.0, min(b, end) - max(a, end - window)) for a, b in calls if a <= end)


@pytest.mark.parametrize("lanes,call", [(1, 60.0), (2, 60.0), (2, 180.0)])
def test_replay_keeps_call_seconds_at_or_under_the_share(tmp_path, monkeypatch, lanes, call):
    """Six hours of calls, each reserved at the 180 s timeout."""
    share, timeout = 0.5, 180.0
    clock = Clock(monkeypatch)
    store = FakeStore(tmp_path)
    capture(store, lanes, text="Job {index} context.")
    limits = BudgetLimits(
        max_calls=10**6,
        max_tokens=10**9,
        max_seconds=10**9,
        max_concurrency=lanes,
        max_busy_share=share,
    )
    allowed = share * 3600 * lanes
    calls, open_calls = [], {}
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        jobs = [ledger.claim(f"w{i}", lease_seconds=10**6) for i in range(lanes)]
        start = clock.now
        while clock.now - start < 6 * 3600:
            for index, job in enumerate(jobs):
                running = open_calls.get(index)
                if running and clock.now >= running[0] + call:
                    ledger.settle(running[1]["id"], actual_seconds=call)
                    calls.append((running[0], running[0] + call))
                    del open_calls[index]
                if index not in open_calls:
                    try:
                        reservation = reserve_one(ledger, job, limits, seconds=timeout)
                    except StepAside as aside:
                        assert aside.reason == "share" and 1 <= aside.retry_after <= 300
                    else:
                        open_calls[index] = (clock.now, reservation)
            running_now = [(a, a + call) for a, _ in open_calls.values()]
            assert busy_over(calls + running_now, clock.now) <= allowed + 1e-6
            clock.now += 5.0
    total = sum(b - a for a, b in calls)
    # The cap bites, and the pessimistic charge of the open calls does not
    # starve learning: well over half of the allowance is used.
    assert total <= allowed * 6 + 1e-6
    assert total >= 0.6 * allowed * 6


def test_the_share_check_is_off_at_the_default_and_at_one(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    store = FakeStore(tmp_path)
    capture(store, 1, text="Job {index} context.")
    assert BudgetLimits().max_busy_share == 1.0
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        job = ledger.claim("w", lease_seconds=10**6)
        limits = BudgetLimits(max_calls=100, max_seconds=10**9, max_tokens=10**9)
        for _ in range(3):
            first = reserve_one(ledger, job, limits, seconds=3600.0)
            ledger.settle(first["id"], actual_seconds=3600.0)
            clock.now += 1.0
        assert ledger.share_wait(limits, 10**6) == 0.0


def test_a_call_longer_than_the_allowance_is_never_admitted(tmp_path):
    store = FakeStore(tmp_path)
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        limits = BudgetLimits(max_busy_share=0.01)  # 36 s of one slot per hour
        assert ledger.share_wait(limits, 36.0) == 0.0
        assert ledger.share_wait(limits, 37.0) == learning.SHARE_RETRY_MAX
        assert ledger.share_wait(BudgetLimits(max_busy_share=0), 1.0) == learning.SHARE_RETRY_MAX


def test_a_busy_share_above_one_is_invalid(tmp_path):
    store = FakeStore(tmp_path)
    capture(store, 1, text="Job {index} context.")
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        job = ledger.claim("w", lease_seconds=120)
        with pytest.raises(learning.BudgetExceeded):
            reserve_one(ledger, job, BudgetLimits(max_busy_share=1.5))


def test_work_open_keeps_a_quiet_store_up_while_only_the_share_waits(tmp_path, monkeypatch):
    """The cap is a routine throttle that frees within an hour: the store must not
    stop idle and leave the backlog until a client wakes it."""
    clock = Clock(monkeypatch)
    store = FakeStore(tmp_path)
    capture(store, 2, text="Job {index} context.")
    scopes = {("demo", "example")}
    limits = BudgetLimits(max_calls=100, max_seconds=10**9, max_tokens=10**9, max_busy_share=0.09)
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        job = ledger.claim("w", lease_seconds=10**6)
        reservation = reserve_one(ledger, job, limits, seconds=300.0)
        clock.now += 300.0
        ledger.settle(reservation["id"], actual_seconds=300.0)
        assert ledger.share_wait(limits, 60.0) > 0
        assert ledger.work_open(set(), scopes, max_attempts=4, limits=limits, call_seconds=60)


# --- the worker defers, the pool waits -------------------------------------


def test_a_refused_call_goes_back_with_its_attempt_and_makes_no_provider_call(tmp_path):
    store = FakeStore(tmp_path)
    capture(store, 1)

    class Aside(LearningLedger):
        def reserve(self, *args, **kwargs):
            raise StepAside("queue", 42.0)

    provider = GatedProvider(open_gate=True)
    with store.connection() as conn:
        worker = LearningWorker(
            Aside(conn), provider, WorkerSettings(window_chars=4096), BudgetLimits(max_calls=10)
        )
        before = time.time()
        result = worker.run_once()
    assert result == {"status": "deferred", "job_id": result["job_id"], "error": "StepAside"}
    job = rows(store, "SELECT status,attempts,last_error,next_attempt FROM learning_jobs")[0]
    assert job["status"] == "pending" and job["attempts"] == 0
    assert job["last_error"] == "StepAside"
    assert before + 41 <= job["next_attempt"] <= time.time() + 43
    assert provider.starts == [] and rows(store, "SELECT 1 FROM learning_budget") == []


def probe_pool(tmp_path, pool_env, metrics, **extra):
    store, pool, provider = pool_env
    write_settings(tmp_path, 2)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["load_probe"] = {"url": metrics.url, "max_backoff_seconds": 1, **extra}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    return store, pool, provider


def test_with_another_client_queueing_learning_sends_nothing_until_it_is_empty(
    tmp_path, pool_env, metrics
):
    store, pool, provider = probe_pool(tmp_path, pool_env, metrics)
    provider.gate.set()
    capture(store, 3)
    metrics.waiting(3)
    pool.pass_once()
    assert pool.alive() == 2 and pool.probe is not None
    wait_for(lambda: metrics.requests >= 3)
    time.sleep(1.5)
    assert provider.starts == []
    assert rows(store, "SELECT 1 FROM learning_budget") == []
    assert {j["status"] for j in rows(store, "SELECT status FROM learning_jobs")} == {"pending"}
    # No job was even claimed: nothing was leased and nothing was deferred.
    held = rows(store, "SELECT attempts,last_error,lease_owner FROM learning_jobs")
    assert held and all(not j["attempts"] and not j["last_error"] for j in held)
    metrics.waiting(0)
    wait_for(all_done(store, 3))
    assert provider.starts


def test_a_waiting_loop_notices_a_cleared_queue_before_its_backoff_ends(
    tmp_path, pool_env, metrics, monkeypatch
):
    monkeypatch.setattr(learning_runtime, "PASS_INTERVAL", 0.2)
    store, pool, provider = probe_pool(tmp_path, pool_env, metrics, max_backoff_seconds=30)
    provider.gate.set()
    capture(store, 1)
    metrics.waiting(2)
    pool.pass_once()
    time.sleep(3.2)  # the back-off has grown to 4 s: 1 + 2, then 4
    assert provider.starts == [] and pool.probe.hold() > 0.5
    metrics.waiting(0)
    started = time.monotonic()
    wait_for(all_done(store, 1), timeout=2.0)
    assert time.monotonic() - started < 2.0


def test_a_queue_that_appears_mid_job_stops_the_next_call(tmp_path, pool_env, metrics):
    store, pool, _ = probe_pool(tmp_path, pool_env, metrics)
    capture(store, 1)
    pool.budget = BudgetLimits(max_calls=10, max_seconds=10**6, max_tokens=10**9)
    pool.probe = LoadProbe(options(metrics.url))
    pool.slots.set_limit(1)
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        job = LearningLedger(conn).claim("w", lease_seconds=120)
        metrics.waiting(1)
        with pytest.raises(StepAside) as aside:
            reserve_one(ledger, job, pool.budget)
        assert aside.value.reason == "queue" and aside.value.retry_after > 0
        assert pool.slots.used == 0
        assert rows(store, "SELECT 1 FROM learning_budget") == []
        metrics.waiting(0)
        reservation = reserve_one(ledger, job, pool.budget)
        ledger.settle(reservation["id"], actual_seconds=1.0)
    assert pool.slots.used == 0


def test_the_probe_follows_learning_json_live(tmp_path, pool_env, metrics):
    store, pool, provider = probe_pool(tmp_path, pool_env, metrics)
    metrics.waiting(1)
    pool.pass_once()
    first = pool.probe
    assert first is not None
    pool.pass_once()
    assert pool.probe is first  # the same block keeps its probe and its back-off
    value = json.loads((tmp_path / "learning.json").read_text())
    value["load_probe"]["gauge"] = "other_gauge"
    (tmp_path / "learning.json").write_text(json.dumps(value))
    pool.pass_once()
    assert pool.probe is not first and pool.probe.options["gauge"] == "other_gauge"
    del value["load_probe"]
    (tmp_path / "learning.json").write_text(json.dumps(value))
    pool.pass_once()
    assert pool.probe is None


def test_no_probe_configured_makes_no_request_and_changes_nothing(tmp_path, pool_env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no load probe is configured")

    monkeypatch.setattr(learning_probe.LoadProbe, "__init__", forbidden)
    monkeypatch.setattr(learning_probe.LoadProbe, "waiting", forbidden)
    store, pool, provider = pool_env
    provider.gate.set()
    capture(store, 3)
    write_settings(tmp_path, 2)
    pool.pass_once()
    wait_for(all_done(store, 3))
    assert pool.probe is None
    assert pool.step_aside() == 0.0


def test_the_pool_waits_before_it_claims_when_the_share_is_spent(tmp_path, pool_env):
    store, pool, provider = pool_env
    provider.gate.set()
    capture(store, 2)
    # One slot, a 0.05 share: 180 s an hour. A finished call of 150 s leaves no
    # room for the next call at the 60 s default timeout.
    write_settings(tmp_path, 1, max_busy_share=0.05, max_seconds=10**7)
    seed_call(store, started_ago=200.0, seconds=150.0)
    pool.pass_once()
    time.sleep(0.5)
    assert provider.starts == []
    assert pool.step_aside() > 0
    jobs = rows(store, "SELECT attempts,last_error FROM learning_jobs")
    # No worker claimed and deferred a job.
    assert jobs and all(not j["attempts"] and not j["last_error"] for j in jobs)
    # The same store with a call of 100 s has room: learning goes on.
    with store.connection() as conn:
        conn.execute("DELETE FROM learning_budget")
    seed_call(store, started_ago=200.0, seconds=100.0)
    assert pool.step_aside() == 0.0
    wait_for(all_done(store, 2))


def seed_call(store, *, started_ago, seconds):
    """A finished call of ``seconds`` that began ``started_ago`` seconds ago."""
    now = time.time()
    with store.connection() as conn:
        job = conn.execute("SELECT id FROM learning_jobs LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO learning_budget VALUES (?,?,?,?,?,?,'settled',?,?)",
            (
                f"seed-{started_ago}-{seconds}",
                job["id"],
                "seed-token",
                10,
                seconds,
                0,
                now - started_ago,
                now - started_ago + seconds,
            ),
        )


def test_the_probe_page_is_read_once_a_second_for_all_loops(metrics, monkeypatch):
    monkeypatch.setattr(learning_probe, "CACHE_SECONDS", 60.0)
    probe = LoadProbe(options(metrics.url))
    metrics.waiting(1)
    assert [probe.hold() > 0 for _ in range(5)] == [True] * 5
    assert metrics.requests == 1


# --- configuration ---------------------------------------------------------


def test_malformed_load_probe_or_share_turns_learning_off(tmp_path):
    for change in (
        {"load_probe": {"url": "http://example.com/metrics"}},
        {"load_probe": {"url": "http://127.0.0.1:9/metrics", "typo": 1}},
        {"budget": {"max_busy_share": 1.5}},
        {"budget": {"max_busy_share": -0.1}},
        {"budget": {"max_busy_share": "half"}},
    ):
        write_settings(tmp_path, 1)
        value = json.loads((tmp_path / "learning.json").read_text())
        for key, new in change.items():
            value[key] = {**value.get(key, {}), **new}
        (tmp_path / "learning.json").write_text(json.dumps(value))
        assert learning_runtime.load_settings(tmp_path)["enabled"] is False, change
    write_settings(tmp_path, 1, max_busy_share=0.5)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["load_probe"] = {"url": "http://127.0.0.1:9/metrics"}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_runtime.load_settings(tmp_path)["enabled"] is True
