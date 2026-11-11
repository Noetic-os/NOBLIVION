# SPDX-License-Identifier: AGPL-3.0-or-later
"""The schedule, the daily check, the decision rules and the export."""

from __future__ import annotations

import datetime as dt
import json

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import config, report, scheduler
from longtest.config import PARAMS
from longtest.util import append_jsonl

TZ = dt.timezone.utc


def at(hour, minute=0, day=1):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_prereg_is_deterministic_and_names_the_rules():
    one, two = config.render_prereg(), config.render_prereg()
    assert one == two and config.prereg_hash() == config.prereg_hash()
    for name in ("DR1", "DR2", "DR3", "DR4", "DR5", "DR6", "DR7", "DR8"):
        assert f"- {name}:" in one
    assert "dr1_lower: 0.1" in one and "bootstrap_resamples: 10000" in one


def test_prereg_hash_check(tmp_path):
    path = tmp_path / "PREREG.md"
    path.write_text(config.render_prereg())
    config.check_prereg(path, config.prereg_hash())
    path.write_text(path.read_text() + "tuned\n")
    with pytest.raises(SystemExit):
        config.check_prereg(path, config.prereg_hash())


def test_heavy_jobs_wait_for_the_window_and_the_load(tmp_path):
    clock = {"t": at(14)}
    load = {"v": 2.0}
    s = scheduler.Scheduler(
        tmp_path, now=lambda: clock["t"], loadavg=lambda: load["v"], free_gb=lambda p: 500.0
    )
    names = {j.name for j in s.due()}
    assert "heartbeat" in names and "replay_nightly" not in names
    assert s.waiting()["replay_nightly"] == "window"
    clock["t"] = at(2)
    assert "replay_nightly" in {j.name for j in s.due()}
    load["v"] = 40.0
    assert "replay_nightly" not in {j.name for j in s.due()}
    assert s.waiting()["replay_nightly"] == "load"
    assert "heartbeat" in {j.name for j in s.due()}  # a light job does not wait for load


def test_disk_floor_and_stop_file_pause_everything(tmp_path):
    s = scheduler.Scheduler(
        tmp_path, now=lambda: at(2), loadavg=lambda: 1.0, free_gb=lambda p: PARAMS.min_free_gb - 1
    )
    assert s.due() == []
    s.free_gb = lambda p: 500.0
    assert s.due()
    (tmp_path / "stop").write_text("stop")
    assert s.due() == [] and s.stopped()


def test_state_survives_a_restart_and_a_failed_job_is_marked(tmp_path):
    clock = {"t": at(3)}
    kw = {"now": lambda: clock["t"], "loadavg": lambda: 1.0, "free_gb": lambda p: 500.0}
    s = scheduler.Scheduler(tmp_path, **kw)

    def boom():
        raise RuntimeError("x")

    handlers = {j.name: (lambda: None) for j in s.jobs}
    handlers["soak_load"] = boom
    done = dict(s.run_due(handlers))
    assert done["soak_load"] is False and done["heartbeat"] is True
    again = scheduler.Scheduler(tmp_path, **kw)
    assert again.state["jobs"]["soak_load"]["failed"] == 1
    assert again.state["jobs"]["soak_load"]["note"] == "RuntimeError: x"
    assert again.due() == []  # nothing is due a second time
    clock["t"] = at(4)
    assert "heartbeat" in {j.name for j in again.due()}


def test_a_long_job_note_keeps_its_head_and_its_last_log_line():
    exc = RuntimeError("store exited -6: " + "x" * 2000 + " | Resource temporarily unavailable")
    note = scheduler.job_note(exc)
    assert len(note) == 500
    assert note.startswith("RuntimeError: store exited -6: ")
    assert note.endswith("| Resource temporarily unavailable")


def test_daily_summary_job_runs_once_after_0630(tmp_path):
    clock = {"t": at(6, 0)}
    s = scheduler.Scheduler(
        tmp_path, now=lambda: clock["t"], loadavg=lambda: 1.0, free_gb=lambda p: 500.0
    )
    assert "daily_summary" not in {j.name for j in s.due()}
    clock["t"] = at(6, 31)
    job = next(j for j in s.due() if j.name == "daily_summary")
    s.mark(job, True)
    assert "daily_summary" not in {j.name for j in s.due()}
    clock["t"] = at(6, 31, day=2)
    assert "daily_summary" in {j.name for j in s.due()}


# -- status -------------------------------------------------------------------------


def make_run(tmp_path, now, beats=24, sandbox_rows=None, old_first=False):
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    (run / "daily").mkdir()
    (run / "run.json").write_text(json.dumps({"plugin_version": "0.1.21"}))
    (run / "state.json").write_text(json.dumps({"jobs": {"heartbeat": {"failed": 0, "ok": True}}}))
    for i in range(beats):
        append_jsonl(
            run / "out" / "heartbeat.jsonl",
            {"ts": (now - dt.timedelta(hours=i, minutes=1)).isoformat()},
        )
    if old_first:  # a run that began days ago: the whole day is due
        append_jsonl(
            run / "out" / "heartbeat.jsonl", {"ts": (now - dt.timedelta(days=3)).isoformat()}
        )
    for row in sandbox_rows or [
        {"external_connects": 0, "claude_processes": 0, "refused_opens": 0, "refused_writes": 0}
    ]:
        append_jsonl(run / "out" / "sandbox.jsonl", row)
    report.daily_summary(run, now)
    return run


def check(run, now, **kw):
    return report.status(run, now, free_gb=lambda p: 900.0, plugin_version=lambda: "0.1.21", **kw)


def test_status_passes_a_healthy_day(tmp_path):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now)
    got = check(run, now)
    assert got["ok"], got["failed"]
    assert not (run / "ALERT").exists()


@pytest.mark.parametrize(
    "name,setup",
    [
        ("heartbeats_24", dict(beats=20, old_first=True)),
        ("no_external_connects", dict(sandbox_rows=[{"external_connects": 1}])),
        ("no_claude_process", dict(sandbox_rows=[{"claude_processes": 1}])),
        ("no_refused_open", dict(sandbox_rows=[{"refused_writes": 1}])),
    ],
)
def test_status_fails_one_item_and_alerts(tmp_path, name, setup):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now, **setup)
    sent = []
    got = check(run, now, notify=sent.append)
    assert not got["ok"] and got["failed"] == [name]
    assert (run / "ALERT").exists() and sent and name in sent[0]


def test_heartbeat_gate_scales_with_the_age_of_the_run(tmp_path):
    now = at(6, 30, day=2)
    young = make_run(tmp_path, now, beats=3)  # a run of two and a half hours
    assert check(young, now)["checks"]["heartbeats_24"]
    quiet = tmp_path / "q"
    quiet.mkdir()
    ts = [now - dt.timedelta(hours=h) for h in (30, 29, 28)]  # then nothing for 28 hours
    assert report.heartbeats_needed(ts, now) == 24
    assert report.heartbeats_needed([], now) == 1
    assert report.heartbeats_needed([now - dt.timedelta(minutes=5)], now) == 1
    assert report.heartbeats_needed([now - dt.timedelta(hours=5, minutes=1)], now) == 6


def test_hourly_job_leaves_24_lines_in_any_day():
    job = next(j for j in scheduler.JOBS if j.name == "heartbeat")
    drift = 8  # seconds lost per tick and handler
    assert 86400 // (job.every_s + drift) >= 24


def test_nightly_job_runs_once_per_night_even_after_a_late_night(tmp_path):
    clock = {"t": at(1, 5)}
    s = scheduler.Scheduler(
        tmp_path, now=lambda: clock["t"], loadavg=lambda: 1.0, free_gb=lambda p: 500.0
    )
    job = next(j for j in s.due() if j.name == "replay_nightly")
    clock["t"] = at(5, 50)
    s.mark(job, True)  # the night ended late
    clock["t"] = at(1, 30, day=2)  # next night: less than 24 h after the last end
    assert "replay_nightly" in {j.name for j in s.due()}
    clock["t"] = at(23, 0)  # the same evening: not due again
    assert "replay_nightly" not in {j.name for j in s.due()}


def test_an_overrunning_night_still_runs_the_night_it_missed(tmp_path):
    """A heavy job dispatched for night 1 that overruns past night 2's window must not
    read as "already run tonight" for night 2: the night it covers is the window it
    started in, not the window its finish time happens to fall in."""
    import threading

    gate = threading.Event()
    clock = {"t": at(1, 5)}  # night 1 opens; the job is dispatched for it
    s = scheduler.Scheduler(
        tmp_path, now=lambda: clock["t"], loadavg=lambda: 1.0, free_gb=lambda p: 500.0
    )
    handlers = {j.name: (lambda: None) for j in s.jobs}
    handlers["replay_nightly"] = lambda: gate.wait(5)
    s.run_due(handlers, background=True)
    assert s.busy() == ["replay_nightly"]
    clock["t"] = at(1, 30, day=2)  # it overran into night 2's own window before ending
    gate.set()
    s.wait(10)  # it finishes now, with a timestamp already inside night 2's window
    assert s.state["jobs"]["replay_nightly"]["runs"] == 1
    clock["t"] = at(2, 0, day=2)  # still inside night 2's window
    assert "replay_nightly" in {j.name for j in s.due()}  # night 2 is still owed a run


def test_heavy_job_runs_in_the_background_and_light_jobs_go_on(tmp_path):
    import threading

    gate = threading.Event()
    clock = {"t": at(2)}
    s = scheduler.Scheduler(
        tmp_path, now=lambda: clock["t"], loadavg=lambda: 1.0, free_gb=lambda p: 500.0
    )
    seen = []
    handlers = {j.name: (lambda n=j.name: seen.append(n)) for j in scheduler.JOBS}
    handlers["replay_nightly"] = lambda: (seen.append("replay"), gate.wait(5))
    done = s.run_due(handlers, background=True)
    assert "heartbeat" in [n for n, _ in done] and s.busy() == ["replay_nightly"]
    clock["t"] = at(2, 30)
    assert "replay_nightly" not in [n for n, _ in s.run_due(handlers, background=True)]
    gate.set()
    s.wait(10)
    assert s.state["jobs"]["replay_nightly"]["runs"] == 1


def test_status_reports_a_slow_lag_as_a_note_not_an_alert(tmp_path):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now)
    day = now.date().isoformat()
    rows = [
        {"day": day, "kind": "query", "ok": True, "status": 200, "ms": 30.0},
        {"day": day, "kind": "write", "ok": True, "lag_s": 23.0},
        {"day": day, "kind": "drift", "drift": 0},
    ]
    for r in rows:
        append_jsonl(run / "out" / "soak.jsonl", r)
    got = check(run, now)
    assert got["ok"], got["failed"]
    assert got["notes"]["dr8_lag_p95_s"] == 23.0 and not got["notes"]["dr8_lag_rule_holds"]
    append_jsonl(
        run / "out" / "soak.jsonl", {"day": day, "kind": "write", "ok": False, "lag_s": None}
    )
    assert check(run, now)["failed"] == ["soak_rules"]


def test_status_fails_a_changed_plugin_version_and_low_disk(tmp_path):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now)
    got = report.status(run, now, free_gb=lambda p: 10.0, plugin_version=lambda: "0.1.22")
    assert got["failed"] == ["free_disk", "plugin_version_same"]


def test_status_fails_a_determinism_mismatch(tmp_path):
    now = at(6, 30, day=3)
    run = make_run(tmp_path, now)
    for day, h in ((1, "aaa"), (2, "aaa"), (3, "bbb")):
        append_jsonl(
            run / "out" / "determinism.jsonl", {"day": day, "scale": 30, "part": "all", "hash": h}
        )
    assert check(run, now)["failed"] == ["determinism_same"]


def started_at(run, when):
    state = json.loads((run / "state.json").read_text())
    (run / "state.json").write_text(json.dumps({**state, "started": when.isoformat()}))


def test_night_day_counts_the_day_of_the_last_night():
    assert report.night_day(None, at(6, 30, day=3)) == 0
    assert report.night_day(at(7, 50).isoformat(), at(6, 30, day=2)) == 1
    assert report.night_day(at(7, 50).isoformat(), at(6, 30, day=3)) == 2
    # a run that began after 01:00: its first night is still day 1 at 25 hours
    assert report.night_day(at(5, 0).isoformat(), at(6, 30, day=2)) == 1


@pytest.mark.parametrize(
    "days,ok",
    [((), False), ((2,), False), ((1,), False), ((1, 2), True)],
)
def test_status_fails_determinism_with_nothing_to_compare(tmp_path, days, ok):
    now = at(6, 30, day=3)
    run = make_run(tmp_path, now)
    started_at(run, at(7, 50))
    for day in days:
        append_jsonl(
            run / "out" / "determinism.jsonl", {"day": day, "scale": 30, "part": "all", "hash": "a"}
        )
    assert check(run, now)["checks"]["determinism_same"] is ok


def test_status_waits_for_the_second_night_before_it_needs_a_hash(tmp_path):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now)
    started_at(run, at(5, 0))  # day 2 by the clock, but the last night was on day 1
    assert check(run, now)["checks"]["determinism_same"]


def test_status_fails_a_missing_daily_summary(tmp_path):
    now = at(6, 30, day=2)
    run = make_run(tmp_path, now)
    next((run / "daily").glob("*.json")).unlink()
    assert check(run, now)["failed"] == ["summary_written"]


# -- decision rules -----------------------------------------------------------------


def comparison(low, high, p, diff=None, n=60):
    return {
        "n": n,
        "p": p,
        "diff": (low + high) / 2 if diff is None else diff,
        "low": low,
        "high": high,
    }


def summary(vs2, vb0=None, v5=None, s2_high=0.5):
    rates = {}
    for key in vs2:
        size, claim = key.split("|")
        rates[f"{size}|{claim}|S2"] = {"hits": 1, "n": 10, "rate": 0.2, "low": 0.0, "high": s2_high}
    return {"b1_vs_s2": vs2, "b1_vs_b0": vb0 or {}, "rates": rates, "false_delivery_v5": v5 or {}}


def test_win_needs_dr1_dr5_and_dr6():
    s = summary(
        {"1000|C1": comparison(0.3, 0.6, 1e-6), "1000|C3": comparison(0.0, 0.2, 0.3)},
        v5={"1000|V5|B1": {"high": 0.05}},
    )
    labels = {"precision_b1": 0.7, "b1_minus_b0": 0.1}
    got = report.verdicts(s, labels, None, None)
    assert got["scales"]["MEDIUM-2"]["verdict"] == "WIN"
    assert got["claims"]["1000|C1"]["dr1"]
    pending = report.verdicts(s, None, None, None)["scales"]["MEDIUM-2"]["verdict"]
    assert pending == "PENDING"


def test_dr2_stock_that_does_not_fail_is_no_test_not_a_win():
    s = summary(
        {"1000|C1": comparison(0.3, 0.6, 1e-6)}, s2_high=0.95, v5={"1000|V5|B1": {"high": 0.05}}
    )
    got = report.verdicts(s, {"precision_b1": 0.7, "b1_minus_b0": 0}, None, None)
    claim = got["claims"]["1000|C1"]
    assert claim["no_test"] and not claim["dr1"]
    assert got["scales"]["MEDIUM-2"]["verdict"] == "TIE"


def test_lose_when_the_upper_bound_is_below_zero_or_dr5_fails():
    s = summary({"200|C1": comparison(-0.5, -0.1, 0.01)}, v5={"200|V5|B1": {"high": 0.02}})
    assert report.verdicts(s, None, None, None)["scales"]["MEDIUM-1"]["verdict"] == "LOSE"
    s = summary({"5000|C1": comparison(0.3, 0.6, 1e-6)}, v5={"5000|V5|B1": {"high": 0.2}})
    got = report.verdicts(s, {"precision_b1": 0.9, "b1_minus_b0": 0}, None, None)
    assert (
        got["scales"]["LARGE-1"]["dr5"] is False and got["scales"]["LARGE-1"]["verdict"] == "LOSE"
    )


def test_holm_blocks_a_borderline_dr1():
    vs2 = {f"1000|C{i}": comparison(0.2, 0.5, 0.03) for i in range(1, 8)}
    got = report.verdicts(summary(vs2), None, None, None)
    assert not any(c["dr1"] for c in got["claims"].values())  # 7 x 0.03 > 0.05


def test_dr3_dr4_dr7_dr8():
    s = summary({"200|C1": comparison(0.2, 0.5, 1e-5)}, vb0={"200|C1": comparison(-0.02, 0.1, 0.5)})
    s["char_ratio"] = {"200": {"diff": 0.5, "low": 0.4, "high": 0.62}}
    got = report.verdicts(s, None, {"pass": True}, 430.0, live={"resent_ratio": 0.4})
    assert got["claims"]["200|C1"]["dr3"] is True
    assert got["dr4"] == {"chars": True, "resent": True, "pass": True}
    assert got["dr7"] is True and got["dr8"] is True
    s["char_ratio"]["200"]["high"] = 0.9
    assert report.verdicts(s, None, {"pass": False}, 650.0)["dr7"] is False


def test_char_ratio_is_paired_by_query():
    rows = [
        {
            "channel": "prompt",
            "build": b,
            "size": 200,
            "qid": f"q{i}",
            "lesson": f"L{i % 5}",
            "chars": 100 if b == "B1" else 400,
        }
        for i in range(20)
        for b in ("B0", "B1")
    ]
    got = report.char_ratio(rows, resamples=100)
    assert got["200"]["diff"] == 0.25 and got["200"]["high"] == 0.25


def test_export_has_no_row_files(tmp_path):
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    (run / "daily").mkdir()
    (run / "private").mkdir()
    (run / "run.json").write_text("{}")
    (run / "daily" / "d.json").write_text("{}")
    (run / "out" / "replay.jsonl").write_text('{"qid": "x"}\n')
    (run / "private" / "note.txt").write_text("secret text")
    names = report.export(run, tmp_path / "export")
    assert sorted(names) == ["d.json", "run.json"]
    assert sorted(p.name for p in (tmp_path / "export").iterdir()) == [
        "d.json",
        "files.sha256",
        "run.json",
    ]
