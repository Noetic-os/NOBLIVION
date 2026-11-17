# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-266: learning calls only inside a night window, and only while the
model server runs nothing but learning (the strict load probe).

Never contacts a model. The metrics endpoint is a fake HTTP server on loopback.
The clock is the pool's injected ``clock``, so no test depends on the time zone.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import pytest

import test_learning_parallel as pool_fixtures
from noblivion import learning_probe, learning_runtime, learning_schedule
from noblivion.learning import BudgetLimits, LearningLedger, StepAside
from noblivion.learning_probe import LoadProbe, probe_options
from noblivion.learning_schedule import schedule_options, window_open, window_wait
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
from test_learning_step_aside import FakeMetrics

pool_env = pool_fixtures.pool_env

NIGHT = {"window": "22:00-06:00"}
INSIDE = datetime(2026, 1, 15, 23, 0)
BEFORE = datetime(2026, 1, 15, 21, 0)  # one hour before the window opens
NOON = datetime(2026, 1, 15, 12, 0)


def at(hour, minute=0, second=0):
    return datetime(2026, 1, 15, hour, minute, second)


@pytest.fixture(autouse=True)
def fresh_probe_reads(monkeypatch):
    monkeypatch.setattr(learning_probe, "CACHE_SECONDS", 0.0)


@pytest.fixture
def metrics():
    fake = FakeMetrics()
    yield fake
    fake.close()


def vllm_page(waiting, running):
    """A page in the shape a vLLM-style server sends: namespaced, labelled gauges."""
    return (
        "# TYPE vllm:num_requests_running gauge\n"
        f'vllm:num_requests_running{{engine="0",model_name="m"}} {float(running)}\n'
        "# TYPE vllm:num_requests_waiting gauge\n"
        f'vllm:num_requests_waiting{{engine="0",model_name="m"}} {float(waiting)}\n'
    )


def strict_options(url, **extra):
    return probe_options({"load_probe": {"url": url, "strict": True, **extra}})


# --- the window ------------------------------------------------------------


def test_a_window_that_crosses_midnight_is_open_at_night_only():
    night = schedule_options({"schedule": NIGHT})
    assert night == {"window": "22:00-06:00", "start": 22 * 3600, "end": 6 * 3600}
    assert [window_open(night, at(h, m)) for h, m in ((21, 59), (22, 0), (23, 30))] == [
        False,
        True,
        True,
    ]
    assert [window_open(night, at(h, m)) for h, m in ((0, 0), (5, 59), (6, 0), (12, 0))] == [
        True,
        True,
        False,
        False,
    ]
    day = schedule_options({"schedule": {"window": "09:30-17:00"}})
    assert not window_open(day, at(9, 29)) and window_open(day, at(9, 30))
    assert window_open(day, at(16, 59)) and not window_open(day, at(17, 0))
    assert schedule_options({}) is None and window_open(None, NOON)


def test_the_wait_runs_to_the_next_start_and_is_capped_at_an_hour():
    night = schedule_options({"schedule": NIGHT})
    assert window_wait(night, INSIDE) == 0.0
    assert window_wait(night, BEFORE) == 3600.0
    assert window_wait(night, at(21, 59, 30)) == 30.0
    assert window_wait(night, NOON) == learning_schedule.MAX_WINDOW_WAIT == 3600.0
    assert window_wait(night, at(6, 0)) == 3600.0  # 16 hours away: look again in one
    assert window_wait(None, NOON) == 0.0


@pytest.mark.parametrize(
    "block",
    [
        {"window": "22:00-6:00"},
        {"window": "24:00-06:00"},
        {"window": "22:60-06:00"},
        {"window": "22:00 - 06:00"},
        {"window": "22:00-06:00\n"},
        {"window": "06:00-06:00"},
        {"window": 22},
        {"window": None},
        {},
        {"window": "22:00-06:00", "days": "mon"},
        "22:00-06:00",
    ],
    ids=str,
)
def test_a_malformed_schedule_is_refused_and_turns_learning_off(tmp_path, block):
    with pytest.raises(ValueError):
        schedule_options({"schedule": block})
    write_settings(tmp_path, 1)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["schedule"] = block
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_runtime.load_settings(tmp_path)["enabled"] is False


def test_a_valid_schedule_keeps_learning_on(tmp_path):
    write_settings(tmp_path, 1)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["schedule"] = NIGHT
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_runtime.load_settings(tmp_path)["enabled"] is True


# --- claim and reserve outside the window ----------------------------------


def manual_pool(store, *, slots=1, schedule=NIGHT, clock=INSIDE, probe=None):
    """A pool with no worker threads: the tests drive its ledger by hand."""
    pool = learning_runtime.LearningPool(store)
    pool.budget = BudgetLimits(max_calls=1000, max_seconds=10**7, max_tokens=10**9)
    pool.schedule = schedule_options({"schedule": schedule}) if schedule else None
    pool.clock = lambda: clock
    pool.probe = probe
    pool.slots.set_limit(slots)
    return pool


def test_no_job_is_claimed_outside_the_window(tmp_path):
    store = FakeStore(tmp_path)
    capture(store, 1)
    pool = manual_pool(store, clock=BEFORE)
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        assert ledger.claim("w") is None
        assert pool.step_aside() == 3600.0
        pool.clock = lambda: INSIDE
        assert pool.step_aside() == 0.0
        assert ledger.claim("w") is not None


def test_a_call_outside_the_window_steps_aside_until_the_window_opens(tmp_path):
    store = FakeStore(tmp_path)
    capture(store, 1)
    pool = manual_pool(store)
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        job = ledger.claim("w", lease_seconds=120)
        pool.clock = lambda: at(21, 59, 30)
        with pytest.raises(StepAside) as aside:
            reserve_one(ledger, job, pool.budget)
        assert aside.value.reason == "window" and aside.value.retry_after == 30.0
        pool.clock = lambda: NOON
        with pytest.raises(StepAside) as aside:
            reserve_one(ledger, job, pool.budget)
        assert aside.value.retry_after == 3600.0
        # The slot is free again and no reservation row was written.
        assert pool.slots.used == 0
        assert rows(store, "SELECT 1 FROM learning_budget") == []


def test_a_job_stopped_by_the_window_end_keeps_its_attempt_and_resumes(tmp_path):
    """The first window of the job runs at night; the window closes during the call."""
    (tmp_path / "reference").mkdir()
    reference = FakeStore(tmp_path / "reference")
    capture(reference, 1)
    counted = GatedProvider(open_gate=True)
    with reference.connection() as conn:
        pool = manual_pool(reference)
        worker = LearningWorker(
            learning_runtime.PooledLedger(conn, pool),
            counted,
            WorkerSettings(window_chars=4096),
            pool.budget,
        )
        assert worker.run_once()["status"] == "done"
    total = len(counted.starts)
    assert total >= 2  # the job needs more than one call

    store = FakeStore(tmp_path)
    capture(store, 1)
    pool = manual_pool(store)

    closing = [True]

    class ClosingProvider(GatedProvider):
        def complete(self, messages, **kwargs):
            if closing[0]:
                pool.clock = lambda: at(6, 0)  # the window closes during this call
            return super().complete(messages, **kwargs)

    provider = ClosingProvider(open_gate=True)
    with store.connection() as conn:
        worker = LearningWorker(
            learning_runtime.PooledLedger(conn, pool),
            provider,
            WorkerSettings(window_chars=4096),
            pool.budget,
        )
        before = time.time()
        result = worker.run_once()
    assert result == {"status": "deferred", "job_id": result["job_id"], "error": "StepAside"}
    assert len(provider.starts) == 1
    job = rows(store, "SELECT status,attempts,last_error,next_attempt,progress FROM learning_jobs")[
        0
    ]
    assert job["status"] == "pending" and job["attempts"] == 0
    assert job["last_error"] == "StepAside"
    assert before + 3599 <= job["next_attempt"] <= time.time() + 3601
    assert json.loads(job["progress"] or "{}")  # the finished call is kept
    # The next night: the job goes on from its checkpoint and sends only the rest.
    with store.connection() as conn:
        conn.execute("UPDATE learning_jobs SET next_attempt=0")
    pool.clock = lambda: INSIDE
    closing[0] = False
    provider.starts.clear()  # the same provider: a new one would change the frozen job
    with store.connection() as conn:
        worker = LearningWorker(
            learning_runtime.PooledLedger(conn, pool),
            provider,
            WorkerSettings(window_chars=4096),
            pool.budget,
        )
        assert worker.run_once()["status"] == "done"
    assert len(provider.starts) == total - 1


def test_the_pool_claims_nothing_outside_the_window_and_starts_when_it_opens(tmp_path, pool_env):
    store, pool, provider = pool_env
    provider.gate.set()
    capture(store, 2)
    write_settings(tmp_path, 2)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["schedule"] = NIGHT
    (tmp_path / "learning.json").write_text(json.dumps(value))
    pool.clock = lambda: NOON
    pool.pass_once()
    assert pool.alive() == 2 and pool.schedule is not None
    time.sleep(0.5)
    assert provider.starts == []
    jobs = rows(store, "SELECT attempts,last_error,lease_owner FROM learning_jobs")
    assert jobs and all(not j["attempts"] and not j["last_error"] for j in jobs)
    assert all(j["lease_owner"] is None for j in jobs)
    pool.clock = lambda: INSIDE
    wait_for(all_done(store, 2))
    # The block follows learning.json live: removing it lifts the window.
    del value["schedule"]
    (tmp_path / "learning.json").write_text(json.dumps(value))
    pool.pass_once()
    assert pool.schedule is None


# --- the strict probe ------------------------------------------------------


def test_strict_defaults_off_and_is_validated():
    assert probe_options({"load_probe": {"url": "http://127.0.0.1:9/m"}})["strict"] is False
    for bad in ({"strict": "yes"}, {"strict": 1}, {"running_gauge": "bad name"}):
        with pytest.raises(ValueError):
            probe_options({"load_probe": {"url": "http://127.0.0.1:9/m", **bad}})
    assert strict_options("http://127.0.0.1:9/m", running_gauge="busy")["running_gauge"] == "busy"


def test_a_strict_probe_holds_while_the_page_cannot_be_read(metrics, caplog):
    probe = LoadProbe(strict_options(metrics.url))
    metrics.page = vllm_page(0, 0)
    assert probe.hold() == 0.0
    with caplog.at_level(logging.WARNING, logger=learning_probe.log.name):
        metrics.status = 503
        assert probe.hold() > 0
        metrics.status = 200
        metrics.page = "unrelated_metric 1\n"
        assert probe.hold() > 0
        # The waiting gauge alone is not enough: the running gauge is missing.
        metrics.page = 'vllm:num_requests_waiting{model_name="m"} 0\n'
        assert probe.hold() > 0
    assert any("learning waits" in r.getMessage() for r in caplog.records)
    metrics.page = vllm_page(0, 0)
    assert probe.hold() == 0.0
    closed = LoadProbe(strict_options("http://127.0.0.1:9/metrics", timeout_seconds=0.5))
    assert closed.hold() > 0


def test_a_strict_probe_holds_while_others_run_and_goes_for_learnings_own(metrics):
    probe = LoadProbe(strict_options(metrics.url))
    metrics.page = vllm_page(0, 1)
    assert probe.hold(own_running=0) > 0  # one request of another client
    metrics.page = vllm_page(0, 2)
    assert probe.hold(own_running=1) > 0  # learning's one call and one other
    assert probe.hold(own_running=2) == 0.0  # both are learning's own
    metrics.page = vllm_page(1, 2)
    assert probe.hold(own_running=2) > 0  # a request waits
    # Without strict, the running gauge is not read and nothing holds for it.
    loose = LoadProbe(probe_options({"load_probe": {"url": metrics.url}}))
    metrics.page = vllm_page(0, 5)
    assert loose.hold() == 0.0
    metrics.status = 503
    assert loose.hold() == 0.0  # the old fail-open behaviour is kept


def test_reserve_counts_only_the_other_calls_of_the_store_as_its_own(tmp_path, metrics):
    """The caller's own slot is held at the check, but the server has not seen it."""
    store = FakeStore(tmp_path)
    capture(store, 1)
    pool = manual_pool(store, slots=2, schedule=None, probe=LoadProbe(strict_options(metrics.url)))
    with store.connection() as conn:
        ledger = learning_runtime.PooledLedger(conn, pool)
        job = ledger.claim("w", lease_seconds=120)
        # One slot: a job between two calls, and one request of another client.
        metrics.page = vllm_page(0, 1)
        with pytest.raises(StepAside) as aside:
            reserve_one(ledger, job, pool.budget)
        assert aside.value.reason == "queue" and pool.slots.used == 0
        # Another loop of this store has a call in flight: it is the one request.
        assert pool.slots.acquire(pool.stop, lambda: None)
        assert pool.step_aside() == 0.0
        reservation = reserve_one(ledger, job, pool.budget)
        ledger.settle(reservation["id"], actual_seconds=1.0)
        # Two requests run, one is learning's: the other belongs to a client.
        metrics.page = vllm_page(0, 2)
        assert pool.step_aside() > 0
        with pytest.raises(StepAside):
            reserve_one(ledger, job, pool.budget)
        pool.slots.release()
    assert pool.slots.used == 0


def test_a_failed_step_aside_check_holds_when_the_probe_is_strict(tmp_path, metrics):
    store = FakeStore(tmp_path)
    pool = manual_pool(store, schedule=None, probe=LoadProbe(strict_options(metrics.url)))

    def broken(own_running=0):
        raise RuntimeError("broken")

    pool.probe.hold = broken
    assert pool.step_aside() == learning_runtime.PASS_INTERVAL
    pool.probe.options["strict"] = False
    assert pool.step_aside() == 0.0  # not strict: a failed look never blocks


# --- the gate replay -------------------------------------------------------

# A recorded sequence of metrics pages, as the probe saw them on a shared
# server: (label, waiting, foreign running, learning calls of other loops,
# page readable). Every call learning makes is checked against the page that
# was current when it started.
RECORDED = [
    ("idle", 0, 0, 0, True),
    ("another client queues", 3, 1, 0, True),
    ("another client runs", 0, 1, 0, True),
    ("server restarts", 0, 0, 0, False),
    ("idle", 0, 0, 0, True),
    ("learning runs elsewhere", 0, 0, 1, True),
    ("learning and a client run", 0, 1, 1, True),
    ("a client queues behind learning", 1, 0, 1, True),
    ("server restarts", 0, 0, 0, False),
    ("idle", 0, 0, 0, True),
]


def test_replay_of_recorded_pages_makes_no_call_while_another_client_is_busy(tmp_path, metrics):
    store = FakeStore(tmp_path)
    capture(store, 8)
    probe = LoadProbe(strict_options(metrics.url, max_backoff_seconds=1))
    pool = manual_pool(store, slots=2, schedule=None, probe=probe)
    state = {"waiting": 0, "foreign": 0, "own": 0, "readable": True}
    calls: list[tuple[str, dict]] = []
    label = [""]

    class RecordingProvider(GatedProvider):
        def complete(self, messages, **kwargs):
            calls.append((label[0], dict(state)))
            return super().complete(messages, **kwargs)

    def page():
        running = state["foreign"] + state["own"] + provider.inflight
        metrics.status = 200 if state["readable"] else 503
        metrics.page = vllm_page(state["waiting"], running)

    provider = RecordingProvider(open_gate=True)
    went = {}
    for step, (name, waiting, foreign, own, readable) in enumerate(RECORDED):
        label[0] = f"{step}:{name}"
        state.update(waiting=waiting, foreign=foreign, own=own, readable=readable)
        page()
        # Learning calls in flight in another loop hold their call slots.
        for _ in range(own):
            assert pool.slots.acquire(pool.stop, lambda: None)
        before = len(calls)
        if pool.step_aside() == 0:
            with store.connection() as conn:
                conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE status='pending'")
                LearningWorker(
                    learning_runtime.PooledLedger(conn, pool),
                    provider,
                    WorkerSettings(window_chars=4096),
                    pool.budget,
                ).run_once()
        went[label[0]] = len(calls) - before
        for _ in range(own):
            pool.slots.release()
    busy = [c for c in calls if c[1]["waiting"] or c[1]["foreign"] or not c[1]["readable"]]
    assert busy == []
    # Learning still works on the idle pages, its own calls included.
    assert went["0:idle"] and went["4:idle"] and went["9:idle"]
    assert went["5:learning runs elsewhere"]
    for step, (name, waiting, foreign, _, readable) in enumerate(RECORDED):
        if waiting or foreign or not readable:
            assert went[f"{step}:{name}"] == 0, name
    # A job that stepped aside got its attempt back.
    waiting = rows(store, "SELECT attempts FROM learning_jobs WHERE status='pending'")
    assert all(j["attempts"] == 0 for j in waiting)


# --- the status report -----------------------------------------------------


def test_status_shows_the_window_whether_it_is_open_and_the_strict_flag(tmp_path):
    write_settings(tmp_path, 1)
    report = learning_runtime.settings_report(tmp_path, NOON)
    assert report["schedule"] == {"window": None, "open": True}
    assert report["load_probe"] is None
    value = json.loads((tmp_path / "learning.json").read_text())
    value["schedule"] = NIGHT
    value["load_probe"] = {"url": "http://127.0.0.1:9/metrics", "strict": True}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    report = learning_runtime.settings_report(tmp_path, at(21, 30))
    assert report["schedule"] == {"window": "22:00-06:00", "open": False, "opens_in_seconds": 1800}
    assert report["load_probe"] == {"strict": True, "gauge": "num_requests_waiting"}
    assert learning_runtime.settings_report(tmp_path, INSIDE)["schedule"]["open"] is True
    value["schedule"] = {"window": "late"}
    (tmp_path / "learning.json").write_text(json.dumps(value))
    report = learning_runtime.settings_report(tmp_path, NOON)
    assert report["enabled"] is False and report["schedule"] == {"error": "invalid"}
    (tmp_path / "learning.json").write_text("{not json")
    assert learning_runtime.settings_report(tmp_path)["enabled"] is False


def test_the_status_command_reports_the_settings_beside_the_database(tmp_path, capsys):
    from noblivion import learning_cli

    store = FakeStore(tmp_path)
    write_settings(tmp_path, 1)
    value = json.loads((tmp_path / "learning.json").read_text())
    value["schedule"] = NIGHT
    (tmp_path / "learning.json").write_text(json.dumps(value))
    assert learning_cli.main(["--database", str(store.db_path), "status"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["settings"]["schedule"]["window"] == "22:00-06:00"
    assert isinstance(printed["settings"]["schedule"]["open"], bool)
    with store.connection() as conn:
        assert LearningLedger(conn).status() is not None
