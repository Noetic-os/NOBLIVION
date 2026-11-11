# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan 4 Part C: the compressed soak mix (C2), cold start (C3) and the kill test
(C4)."""

from __future__ import annotations

import json
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import buildrunner, cli, compressed, health, runner, soak
from longtest.config import PARAMS
from longtest.runner import Run
from longtest.util import read_jsonl


def test_compressed_schedule_counts_match_the_registered_mix():
    events, compressed_s = health.compressed_schedule(speed=20.0)
    by_kind = {}
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    assert by_kind["burst"] == health.MIX["burst_events"]
    assert by_kind["churn"] == health.MIX["churns"]
    assert by_kind["query"] == health.MIX["queries"] - by_kind["burst"] * health.MIX["burst_size"]
    assert by_kind["query"] + by_kind["burst"] * health.MIX["burst_size"] == health.MIX["queries"]
    # 7 real days at 20x is 8.4 hours
    assert compressed_s == health.WEEK_S / 20.0


def test_compressed_schedule_is_sorted_and_interleaved():
    events, _ = health.compressed_schedule(speed=20.0)
    assert [e["fire_s"] for e in events] == sorted(e["fire_s"] for e in events)
    kinds_seen_early = {e["kind"] for e in events[:50]}
    assert len(kinds_seen_early) > 1  # the mix interleaves, not kind by kind


def test_compressed_schedule_rejects_a_mix_with_more_burst_queries_than_queries():
    bad = dict(health.MIX, queries=100, burst_events=10, burst_size=20)
    try:
        health.compressed_schedule(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_day_of_never_exceeds_the_number_of_days():
    compressed_s = 7000.0
    assert health.day_of(0.0, compressed_s) == 1
    assert health.day_of(compressed_s - 0.001, compressed_s) == 7
    assert health.day_of(compressed_s, compressed_s) == 7  # rounding past the edge
    assert health.day_of(compressed_s / 7 + 0.5, compressed_s) == 2


def test_run_compressed_waits_for_each_event_and_tags_rows():
    events = [
        {"fire_s": 10.0, "kind": "query", "day": 1},
        {"fire_s": 20.0, "kind": "burst", "day": 1},
        {"fire_s": 30.0, "kind": "query", "day": 2},
    ]
    clock = {"t": 0.0}
    waits = []

    def elapsed():
        return clock["t"]

    def sleep(s):
        waits.append(round(s, 1))
        clock["t"] += s

    sunk = []
    ops = {
        "query": lambda e: [{"kind": "query", "ok": True, "status": 200, "ms": 5.0}],
        "burst": lambda e: [{"kind": "query", "ok": True, "status": 200, "ms": 5.0}] * 3,
    }
    ran = health.run_compressed(events, ops, sunk.append, elapsed, sleep=sleep, poll_s=5.0)
    assert ran == [0, 1, 2]
    assert len(sunk) == 1 + 3 + 1
    assert sunk[0]["day"] == "vday-1" and "burst" not in sunk[0]
    assert all(r["day"] == "vday-1" and r["burst"] is True for r in sunk[1:4])
    assert sunk[-1]["day"] == "vday-2"
    assert clock["t"] >= 30.0
    assert waits  # it slept to reach each event


def test_run_compressed_calls_pause_before_and_while_waiting():
    events = [{"fire_s": 10.0, "kind": "query", "day": 1}]
    clock = {"t": 0.0}
    pauses = []

    def pause():
        pauses.append(clock["t"])

    def sleep(s):
        clock["t"] += s

    health.run_compressed(
        events,
        {"query": lambda e: []},
        lambda r: None,
        lambda: clock["t"],
        sleep=sleep,
        pause=pause,
        poll_s=3.0,
    )
    assert len(pauses) >= 2  # once before the wait, again during it


def test_run_compressed_skips_events_already_done():
    events = [
        {"fire_s": 1.0, "kind": "query", "day": 1},
        {"fire_s": 2.0, "kind": "query", "day": 1},
    ]
    calls = []
    ran = health.run_compressed(
        events,
        {"query": lambda e: calls.append(e) or []},
        lambda r: None,
        lambda: 5.0,
        sleep=lambda s: None,
        done=[0],
    )
    assert ran == [1]
    assert len(calls) == 1


def test_c2_report_judges_with_the_registered_params_and_names_the_lag_ticket():
    days = [f"vday-{n}" for n in range(1, 8)]
    rows = []
    for d in days:
        for _ in range(800):
            rows.append({"day": d, "kind": "query", "ok": True, "status": 200, "ms": 100.0})
        rows.append({"day": d, "kind": "write", "ok": True, "lag_s": 30.0})  # the known fault
        rows.append({"day": d, "kind": "drift", "drift": 0})
        for _ in range(3):
            rows.append({"day": d, "kind": "resources", "quick_check": "ok", "rss_mb": 500.0})
    got = health.c2_report(rows, params=PARAMS)
    assert not got["evaluate"]["checks"]["lag_p95"]
    assert "NOBLIVION-192" in got["failed_rules"]["lag_p95"]
    assert got["evaluate"]["checks"]["drift_zero"]
    assert got["mix"] == health.MIX and got["speed"] == 20.0


def test_c2_report_has_no_failed_rules_on_a_clean_week():
    days = [f"vday-{n}" for n in range(1, 8)]
    rows = []
    for d in days:
        for _ in range(800):
            rows.append({"day": d, "kind": "query", "ok": True, "status": 200, "ms": 100.0})
        rows.append({"day": d, "kind": "write", "ok": True, "lag_s": 2.0})
        rows.append({"day": d, "kind": "drift", "drift": 0})
        for _ in range(3):
            rows.append({"day": d, "kind": "resources", "quick_check": "ok", "rss_mb": 500.0})
    got = health.c2_report(rows, params=PARAMS)
    assert got["evaluate"]["pass"]
    assert got["failed_rules"] == {}


def test_cold_start_trial_within_budget_with_rows():
    got = health.cold_start_trial(
        start=lambda: 1.2,
        search=lambda: (200, {"results": [{"id": 1}]}, 150.0),
        clock=lambda: next(iter([0.0, 1.35])),
    )
    assert got["within_budget"] and got["got_rows"]
    assert got["ready_s"] == 1.2 and got["first_answer_ms"] == 150.0


def test_cold_start_trial_over_budget_or_empty():
    slow = health.cold_start_trial(start=lambda: 0.5, search=lambda: (200, {"results": []}, 2500.0))
    assert not slow["within_budget"] and not slow["got_rows"]
    empty = health.cold_start_trial(start=lambda: 0.5, search=lambda: (200, {"results": []}, 50.0))
    assert empty["within_budget"] and not empty["got_rows"]
    failed = health.cold_start_trial(start=lambda: 0.5, search=lambda: (0, {}, 50.0))
    assert not failed["within_budget"] and not failed["got_rows"]


def test_cold_start_summary_shares_and_pass():
    trials = [
        {"within_budget": True, "got_rows": True, "first_answer_s": 1.0},
        {"within_budget": True, "got_rows": False, "first_answer_s": 1.1},
        {"within_budget": False, "got_rows": True, "first_answer_s": 3.0},
    ]
    got = health.cold_start_summary(trials)
    assert got["n"] == 3
    assert got["within_budget_share"] == round(2 / 3, 3)
    assert got["no_rows_share"] == round(1 / 3, 3)
    assert not got["pass"]  # one trial missed the budget
    assert got["first_answer_s"]["n"] == 3


def test_cold_start_summary_of_no_trials_is_not_a_pass():
    got = health.cold_start_summary([])
    assert got["n"] == 0 and not got["pass"]
    assert got["within_budget_share"] is None and got["first_answer_s"] is None


def test_kill_test_evaluate_passes_on_a_clean_restart():
    got = health.kill_test_evaluate(
        {"quick_check": "ok", "rss_mb": 1.0}, {"drift": 0, "disk": 1, "memories": 1}
    )
    assert got["pass"] and got["quick_check_ok"] and got["drift_zero"]


def test_kill_test_evaluate_fails_on_a_bad_quick_check_or_nonzero_drift():
    bad_check = health.kill_test_evaluate(
        {"quick_check": "row 3 missing"}, {"drift": 0, "disk": 1, "memories": 1}
    )
    assert not bad_check["pass"] and not bad_check["quick_check_ok"]
    bad_drift = health.kill_test_evaluate(
        {"quick_check": "ok"}, {"drift": 2, "disk": 3, "memories": 1}
    )
    assert not bad_drift["pass"] and not bad_drift["drift_zero"]


# -- cli.py: health_harness, health_sync, cmd_health_c2/c3/c4, argparse wiring --
#
# These exercise the health commands end to end with fakes in place of the real
# store, worker and HTTP client (the same idiom as test_longtest_compressed.py and
# test_longtest_final.py), using a controllable clock so a resume test can prove the
# schedule offset without a real wait.


def make_run(tmp_path: Path) -> Run:
    run = Run(tmp_path / "run")
    for sub in ("out", "private"):
        (run.dir / sub).mkdir(parents=True)
    run.save_manifest(
        {
            "run_id": "t",
            "live": {
                "config_dir": str(tmp_path / "cfg"),
                "data_dir": str(tmp_path / "cfg" / "data"),
            },
        }
    )
    return run


class FakeClock:
    """A monotonic clock and sleep that advance together, so a wait in the schedule
    loop costs no real wall time but still moves ``elapsed()`` forward."""

    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeProc:
    def __init__(self, pid: int | None) -> None:
        self.pid = pid


class FakeHarness:
    def __init__(self, data: Path, pid: int | None = 4242) -> None:
        self.data = data
        self.store = type("S", (), {"proc": FakeProc(pid)})()
        self.starts: list[bool] = []
        self.stops = 0

    def start(self, index: bool = True) -> dict:
        self.starts.append(index)
        self.store = type("S", (), {"proc": FakeProc(4242)})()
        return {"memories": 1}

    def stop(self) -> None:
        self.stops += 1
        self.store = None


class FakeFixture:
    def __init__(self) -> None:
        self.texts = ["hello world"]

    def queries_for(self, size, variants, shard=None):
        return [{"id": "L00-V3"}]

    def prompt_of(self, q) -> str:
        return self.texts[0]


class FakeWorker:
    def __init__(self, harness) -> None:
        self.harness = harness
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call(self, op, **kw):
        self.calls.append((op, kw))
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


def fake_health_c2_events():
    return [
        {"fire_s": 0.0, "kind": "query", "day": 1},
        {"fire_s": 50.0, "kind": "churn", "day": 1},
        {"fire_s": 100.0, "kind": "drift", "day": 1},
    ]


def patch_c2_soak(monkeypatch, drift_fn=None):
    monkeypatch.setattr(soak, "StoreClient", lambda data: object())
    monkeypatch.setattr(soak, "baseline", lambda client, folders: {"disk": 0, "memories": 0})
    monkeypatch.setattr(
        soak, "query_load", lambda client, texts, n: [{"kind": "query", "ok": True, "ms": 1.0}]
    )
    monkeypatch.setattr(
        soak,
        "churn",
        lambda folder, client, serial, sync, **kw: (
            sync(Path("x")),
            [
                {"kind": "write", "ok": True, "lag_s": 0.1},
                {"kind": "edit", "ok": True},
                {"kind": "delete", "ok": True},
            ],
        )[1],
    )
    monkeypatch.setattr(
        soak,
        "resources",
        lambda data, pid: {
            "kind": "resources",
            "rss_mb": 10.0,
            "db_mb": 1.0,
            "wal_mb": 0.0,
            "quick_check": "ok",
        },
    )
    monkeypatch.setattr(
        soak,
        "drift",
        drift_fn
        or (lambda client, folders, base: {"kind": "drift", "disk": 0, "memories": 0, "drift": 0}),
    )


def c2_args(run_dir: Path, out_dir: Path, size: int = 20000, speed: float = 20.0):
    import argparse

    return argparse.Namespace(run_dir=str(run_dir), out_dir=str(out_dir), size=size, speed=speed)


def test_cmd_health_c2_progress_is_written_as_each_event_finishes_not_only_at_the_end(
    tmp_path, monkeypatch
):
    """BLOCKER (review-209.md): a kill mid-run must not lose progress already made.
    The third event raises; the command never returns normally, but the first two
    events must already be in c2-progress.jsonl."""
    run = make_run(tmp_path)
    out_dir = tmp_path / "out-c2"
    harness = FakeHarness(tmp_path / "data")
    fx = FakeFixture()
    clock = FakeClock()
    monkeypatch.setattr(
        cli, "health_harness", lambda run_, size: (harness, [tmp_path / "wk"], fx, 0)
    )
    monkeypatch.setattr(buildrunner, "Worker", FakeWorker)
    monkeypatch.setattr(compressed, "wait_out_quiet", lambda *a, **k: 0.0)
    monkeypatch.setattr(
        health, "compressed_schedule", lambda **kw: (fake_health_c2_events(), 100.0)
    )
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)

    def killed_drift(client, folders, base):
        raise KeyboardInterrupt("simulated kill -9")

    patch_c2_soak(monkeypatch, drift_fn=killed_drift)

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_health_c2(c2_args(run.dir, out_dir))

    progress = read_jsonl(out_dir / "c2-progress.jsonl")
    assert [r["i"] for r in progress] == [0, 1]  # event 2 (drift) never finished
    assert not (out_dir / "c2.json").exists()  # the command never returned to write it
    assert harness.stops == 1  # the try/finally leak guard still closed the store


def test_cmd_health_c2_resume_offsets_the_schedule_clock_to_the_last_event_done(
    tmp_path, monkeypatch
):
    """MAJOR (review-209.md, health.py:104-110): resume must not restart the elapsed
    clock at 0. With events 0 and 1 already done (fire_s 0 and 50), the remaining
    event at fire_s 100 must wait only the 50 seconds still owed, not the full 100."""
    run = make_run(tmp_path)
    out_dir = tmp_path / "out-c2"
    out_dir.mkdir(parents=True)
    for i in (0, 1):
        from longtest.util import append_jsonl

        append_jsonl(out_dir / "c2-progress.jsonl", {"i": i})
    harness = FakeHarness(tmp_path / "data")
    fx = FakeFixture()
    clock = FakeClock()
    start_t = clock.t
    monkeypatch.setattr(
        cli, "health_harness", lambda run_, size: (harness, [tmp_path / "wk"], fx, 0)
    )
    monkeypatch.setattr(buildrunner, "Worker", FakeWorker)
    monkeypatch.setattr(compressed, "wait_out_quiet", lambda *a, **k: 0.0)
    monkeypatch.setattr(
        health, "compressed_schedule", lambda **kw: (fake_health_c2_events(), 100.0)
    )
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    seen_elapsed = []

    def watching_drift(client, folders, base):
        seen_elapsed.append(clock.t - start_t)
        return {"kind": "drift", "disk": 0, "memories": 0, "drift": 0}

    patch_c2_soak(monkeypatch, drift_fn=watching_drift)

    assert cli.cmd_health_c2(c2_args(run.dir, out_dir)) == 0
    assert seen_elapsed == [50.0]  # only the 50s still owed, not the full fire_s of 100
    progress = read_jsonl(out_dir / "c2-progress.jsonl")
    assert [r["i"] for r in progress] == [0, 1, 2]


def test_cmd_health_c2_folds_a_quiet_window_pause_back_into_the_schedule_clock(
    tmp_path, monkeypatch
):
    """MAJOR (review-209.md, cli.py:888-889): pause()'s returned seconds must be added
    back to the schedule clock offset, so a quiet-window pause does not make the
    events after it bunch back to back."""
    run = make_run(tmp_path)
    out_dir = tmp_path / "out-c2"
    harness = FakeHarness(tmp_path / "data")
    fx = FakeFixture()
    clock = FakeClock()
    start_t = clock.t
    monkeypatch.setattr(
        cli, "health_harness", lambda run_, size: (harness, [tmp_path / "wk"], fx, 0)
    )
    monkeypatch.setattr(buildrunner, "Worker", FakeWorker)
    events = [
        {"fire_s": 0.0, "kind": "query", "day": 1},
        {"fire_s": 10.0, "kind": "drift", "day": 1},
    ]
    monkeypatch.setattr(health, "compressed_schedule", lambda **kw: (events, 10.0))
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)

    pauses = iter([0.0, 30.0, 0.0])  # "health c2 start", then before event 0, then event 1

    def fake_pause(run_, now, sleep, what, window=None):
        seconds = next(pauses, 0.0)
        clock.t += seconds  # a real pause really spends wall time
        return seconds

    monkeypatch.setattr(compressed, "wait_out_quiet", fake_pause)
    seen_elapsed = []

    def watching_drift(client, folders, base):
        seen_elapsed.append(clock.t - start_t)
        return {"kind": "drift", "disk": 0, "memories": 0, "drift": 0}

    patch_c2_soak(monkeypatch, drift_fn=watching_drift)

    assert cli.cmd_health_c2(c2_args(run.dir, out_dir)) == 0
    # 30s of pause before event 0 must not count against the schedule: event 1's
    # fire_s of 10 is reached 10s after the pause, not 40s after the run began.
    assert seen_elapsed == [40.0]  # 30s pause + 10s of real schedule wait, none bunched
    doc = json.loads((out_dir / "c2.json").read_text())
    assert doc["mix"] and doc["speed"] == 20.0


def test_cmd_health_c3_cold_starts_without_reindexing_and_waits_out_the_quiet_window(
    tmp_path, monkeypatch
):
    """MAJOR (review-209.md, cli.py:935): a real cold start reads the vectors already
    on disk, so C3 must call ``harness.start(index=False)`` -- the opposite of C4's
    own restart. MAJOR (cli.py:927-944): C3 must call ``wait_out_quiet`` too."""
    run = make_run(tmp_path)
    out_dir = tmp_path / "out-c3"
    harness = FakeHarness(tmp_path / "data")
    fx = FakeFixture()
    monkeypatch.setattr(
        cli, "health_harness", lambda run_, size: (harness, [tmp_path / "wk"], fx, 0)
    )
    quiet_calls = []
    monkeypatch.setattr(
        compressed, "wait_out_quiet", lambda run_, now, sleep, what, **k: quiet_calls.append(what)
    )
    monkeypatch.setattr(
        soak,
        "StoreClient",
        lambda data: type("C", (), {"search": lambda self, t: (200, {"results": [1]}, 5.0)})(),
    )

    import argparse

    a = argparse.Namespace(run_dir=str(run.dir), out_dir=str(out_dir), sizes="5000", starts=2)
    assert cli.cmd_health_c3(a) == 0
    assert harness.starts == [False, False]  # never re-indexes: a real cold start, not a rebuild
    assert len(quiet_calls) == 2  # one wait_out_quiet before each trial
    doc = json.loads((out_dir / "c3.json").read_text())
    assert doc["sizes"]["5000"]["pass"] is True


def test_cmd_health_c4_reindexes_on_restart_and_fails_the_trial_if_the_burst_thread_hangs(
    tmp_path, monkeypatch
):
    """MAJOR (review-209.md, cli.py:991-997): C4's restart after a kill must re-index
    (the opposite of C3) so a write orphaned by the crash gets picked up. MAJOR
    (cli.py:988-991): the burst thread's join must be checked, not assumed."""
    run = make_run(tmp_path)
    out_dir = tmp_path / "out-c4"
    harness = FakeHarness(tmp_path / "data")
    fx = FakeFixture()
    monkeypatch.setattr(
        cli, "health_harness", lambda run_, size: (harness, [tmp_path / "wk"], fx, 0)
    )
    monkeypatch.setattr(buildrunner, "Worker", FakeWorker)
    monkeypatch.setattr(compressed, "wait_out_quiet", lambda *a, **k: 0.0)
    monkeypatch.setattr(soak, "StoreClient", lambda data: object())
    monkeypatch.setattr(soak, "baseline", lambda client, folders: {"disk": 0, "memories": 0})
    monkeypatch.setattr(
        soak, "churn", lambda folder, client, serial, sync, **kw: sync(Path("x")) or []
    )
    monkeypatch.setattr(
        soak,
        "resources",
        lambda data, pid: {"kind": "resources", "rss_mb": 1.0, "quick_check": "ok"},
    )
    monkeypatch.setattr(
        soak,
        "drift",
        lambda client, folders, base: {"kind": "drift", "disk": 0, "memories": 0, "drift": 0},
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    import argparse

    a = argparse.Namespace(
        run_dir=str(run.dir),
        out_dir=str(out_dir),
        size=20000,
        burst=1,
        kill_after=0.0,
        churn_timeout=1.0,
    )
    assert cli.cmd_health_c4(a) == 0
    doc = json.loads((out_dir / "c4.json").read_text())
    assert doc["reindexed_on_restart"] is True
    # the first start (default index=True), then the reindex restart after the kill
    assert harness.starts == [True, True]
    assert doc["pass"] is True

    # Now make the burst thread outlive the join timeout: the trial must fail, not
    # race worker.close()/harness.start() against a still-running thread.
    import threading

    real_join = threading.Thread.join

    def hanging_join(self, timeout=None):
        return None  # never actually joins -- thread.is_alive() stays true

    monkeypatch.setattr(threading.Thread, "join", hanging_join)
    monkeypatch.setattr(threading.Thread, "is_alive", lambda self: True)
    out_dir2 = tmp_path / "out-c4-hang"
    a2 = argparse.Namespace(
        run_dir=str(run.dir),
        out_dir=str(out_dir2),
        size=20000,
        burst=1,
        kill_after=0.0,
        churn_timeout=0.01,
    )
    with pytest.raises(RuntimeError, match="still running"):
        cli.cmd_health_c4(a2)
    monkeypatch.setattr(threading.Thread, "join", real_join)


def test_health_c2_c3_c4_are_wired_into_the_argument_parser():
    """The ticket's acceptance line covers the commands reaching ``main``, not just
    the functions existing: the parser must route to them with the documented flags."""
    parser = cli.build_parser()
    c2 = parser.parse_args(
        ["health-c2", "--run-dir", "r", "--out-dir", "o", "--size", "5000", "--speed", "10"]
    )
    assert c2.func is cli.cmd_health_c2 and c2.size == 5000 and c2.speed == 10.0
    c3 = parser.parse_args(["health-c3", "--run-dir", "r", "--out-dir", "o", "--starts", "3"])
    assert c3.func is cli.cmd_health_c3 and c3.starts == 3
    c4 = parser.parse_args(
        ["health-c4", "--run-dir", "r", "--out-dir", "o", "--burst", "2", "--kill-after", "0.1"]
    )
    assert c4.func is cli.cmd_health_c4 and c4.burst == 2 and c4.kill_after == 0.1


def test_health_sync_calls_worker_sync_with_an_increasing_serial(tmp_path):
    worker = FakeWorker(harness=None)
    serial = {"n": 0}
    folder = tmp_path / "work-abc" / "home" / "proj"
    folder.mkdir(parents=True)
    sync = cli.health_sync(worker, folder, "health-c2", serial)
    sync(folder / "note1.md")
    sync(folder / "note2.md")
    assert [c[1]["sid"] for c in worker.calls] == ["health-c2-1", "health-c2-2"]
    assert all(c[0] == "sync" for c in worker.calls)


def test_health_harness_seeds_a_separate_health_copy_not_runs_own_b1_store(tmp_path, monkeypatch):
    """MAJOR (review-209.md, cli.py:796,863,978): health work must not open run-d's
    own kept B1 store (growth.py and runner.py read that same store elsewhere); it
    must seed and use the separate ``stores/health/<size>`` copy instead."""
    run = make_run(tmp_path)
    fx = FakeFixture()
    monkeypatch.setattr(Run, "fixture", lambda self: fx)
    fx.shards = lambda size: [0]
    seeded_root = tmp_path / "seeded-health-copy"
    monkeypatch.setattr(runner, "seed_health_store", lambda run_, size: seeded_root)

    captured = {}

    class RecordingHarness(FakeHarness):
        def __init__(self, name, build, root, python, models, wait_s=900.0, settings=None):
            captured["root"] = root
            super().__init__(data=root)

        def prepare(self, fx_, size, shard):
            return [self.data / "wk"]

    monkeypatch.setattr(buildrunner, "Harness", RecordingHarness)
    monkeypatch.setattr(buildrunner, "index_wait_s", lambda size: 1.0)
    monkeypatch.setattr(buildrunner, "store_python", lambda live_data: "python3")

    harness, folders, returned_fx, shard = cli.health_harness(run, 5000)
    assert captured["root"] == seeded_root  # the separate health copy, not run.dir/stores/b1
    assert folders == [seeded_root / "wk"]
    assert shard == 0 and returned_fx is fx
