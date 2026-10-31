# SPDX-License-Identifier: AGPL-3.0-or-later
"""The schedule, the daily check, the decision rules and the export."""

from __future__ import annotations

import datetime as dt
import json

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
    assert again.due() == []  # nothing is due a second time
    clock["t"] = at(4)
    assert "heartbeat" in {j.name for j in again.due()}


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


def make_run(tmp_path, now, beats=24, sandbox_rows=None):
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
        ("heartbeats_24", dict(beats=20)),
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
