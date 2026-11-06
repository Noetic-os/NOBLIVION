# SPDX-License-Identifier: AGPL-3.0-or-later
"""The large-project profile: what it tests, and the rules that keep the run alive
until the last day (no job raises out of the loop, a failed job is retried, a
restart resumes from the state file, the watchdog starts a quiet unit)."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import cli, config, growth, profiles, replay, report, runner, sandbox, scheduler
from longtest import watch as watchdog
from longtest.runner import Run
from longtest.util import append_jsonl, read_jsonl

TZ = dt.timezone.utc


def at(hour, minute=0, day=1):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


# -- the profile ---------------------------------------------------------------------


def test_large_profile_fixes_scope_and_prereg_differs_from_plan2():
    large, plan2 = profiles.LARGE, profiles.PLAN2
    assert large.builds == ("B1",) and large.stock_arms == ("S2",)
    assert large.sizes == (5000, 20000) and large.growth_per_day == 500
    assert large.params.run_days == 7 and large.params.heartbeat_s == 300
    assert set(large.claims) == {
        "C1", "C2", "C3", "C4", "C10", "C13", "C16", "C17", "C11", "C14", "C24"
    }  # fmt: skip
    text = large.prereg()
    for name in ("DR1", "DR3", "DR5", "DR6"):
        assert f"- {name}:" in text
    assert "- DR2:" not in text and "- DR8:" not in text
    assert "run_days: 7" in text and "Profile: large-project" in text
    assert large.prereg_hash() != plan2.prereg_hash()
    assert plan2.prereg() == config.render_prereg()  # the first run keeps its bytes
    assert profiles.profile_of({}) is plan2 and profiles.profile_of({"profile": "large-project"})
    with pytest.raises(SystemExit):
        profiles.get("plan9")


def test_nightly_sizes_every_night_for_the_large_project():
    sizes = [30, 200, 5000, 20000]
    assert profiles.PLAN2.nightly_sizes(3, sizes) == [30, 200]
    assert profiles.PLAN2.nightly_sizes(1, sizes) == sizes
    assert profiles.LARGE.nightly_sizes(3, [5000, 20000]) == [5000, 20000]


def test_init_parser_takes_a_profile_and_no_b0():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["init", "--run-dir", "/r", "--repo", "/p", "--b1", "abc", "--profile", "large-project"]
    )
    assert args.profile == "large-project" and args.b0 == "" and args.sizes == ""
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["init", "--run-dir", "/r", "--repo", "/p", "--b1", "a", "--profile", "x"]
        )
    for name in ("watch", "seed-stores"):
        assert f"longtest.cli {name}" in cli.__doc__


# -- the never-abort guard -------------------------------------------------------------


def make_sched(tmp_path, clock, **kw):
    return scheduler.Scheduler(
        tmp_path,
        now=lambda: clock["t"],
        loadavg=lambda: 1.0,
        free_gb=lambda p: 500.0,
        params=profiles.LARGE.params,
        **kw,
    )


def test_failed_job_is_logged_and_retried_after_30_minutes(tmp_path):
    clock = {"t": at(2, 0)}
    s = make_sched(tmp_path, clock)
    handlers = {j.name: (lambda: None) for j in s.jobs}
    handlers["replay_nightly"] = lambda: (_ for _ in ()).throw(RuntimeError("store not ready"))
    done = dict(s.run_due(handlers))
    assert done["replay_nightly"] is False and done["heartbeat"] is True
    rows = read_jsonl(tmp_path / "out" / "failures.jsonl")
    assert rows[0]["job"] == "replay_nightly" and "store not ready" in rows[0]["error"]
    assert rows[0]["attempt"] == 1
    assert "replay_nightly failed" in (tmp_path / "ALERT").read_text()
    clock["t"] = at(2, 20)
    assert "replay_nightly" not in {j.name for j in s.due()}
    clock["t"] = at(2, 31)
    assert "replay_nightly" in {j.name for j in s.due()}  # the next slot, same night
    clock["t"] = at(6, 31)
    assert s.waiting().get("replay_nightly") == "window"  # outside the window it waits
    clock["t"] = at(1, 5, day=2)
    assert "replay_nightly" in {j.name for j in s.due()}
    assert not (tmp_path / "stop").exists() and not (tmp_path / "pause").exists()


def test_a_system_exit_in_a_job_does_not_leave_the_loop(tmp_path):
    clock = {"t": at(3, 0)}
    s = make_sched(tmp_path, clock)
    handlers = {j.name: (lambda: None) for j in s.jobs}
    handlers["soak_load"] = lambda: (_ for _ in ()).throw(SystemExit("fixture refused"))
    done = dict(s.run_due(handlers))
    assert done["soak_load"] is False
    assert read_jsonl(tmp_path / "out" / "failures.jsonl")[0]["error"].startswith("SystemExit")


def test_heartbeat_job_follows_the_profile_interval():
    jobs = {j.name: j for j in scheduler.jobs_for(profiles.LARGE.params)}
    assert jobs["heartbeat"].every_s == 300
    assert {j.name for j in scheduler.LIGHT_JOBS} & {"replay_nightly", "latency_bench"} == set()
    assert "heartbeat" in {j.name for j in scheduler.LIGHT_JOBS}


# -- the loop ----------------------------------------------------------------------------


def make_run(tmp_path, profile="large-project", started=None):
    run_dir = tmp_path / "run"
    for sub in ("out", "daily", "private", "stores"):
        (run_dir / sub).mkdir(parents=True)
    live = tmp_path / "live"
    (live / "data").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-t",
                "profile": profile,
                "sizes": [5000, 20000],
                "seed": 7,
                "plugin_version": "0.1.21",
                "live": {"config_dir": str(live), "data_dir": str(live / "data")},
                "builds": {"B1": {"commit": "c1"}},
            }
        )
    )
    if started:
        (run_dir / "state.json").write_text(
            json.dumps({"jobs": {}, "offsets": {}, "pause": "", "started": started.isoformat()})
        )
    return Run(run_dir)


def quiet_loop(monkeypatch, run, clock, handlers):
    from longtest import loop as loop_mod

    monkeypatch.setattr(loop_mod.Loop, "setup", lambda self: None)
    monkeypatch.setattr(loop_mod.Loop, "teardown", lambda self: None)
    monkeypatch.setattr(loop_mod.Loop, "handlers", lambda self: handlers(self))
    ticks = {"n": 0}

    def sleep(seconds):
        ticks["n"] += 1
        clock["t"] += dt.timedelta(seconds=seconds)

    loop = loop_mod.Loop(run, 20000, now=lambda: clock["t"], sleep=sleep)
    return loop, ticks


def test_loop_tick_never_raises_and_keeps_the_heartbeat(tmp_path, monkeypatch):
    run = make_run(tmp_path, started=at(8, 0))
    clock = {"t": at(9, 0)}

    def handlers(self):
        base = {j.name: (lambda: None) for j in self.sched.jobs}
        base["heartbeat"] = self.heartbeat
        base["soak_churn"] = lambda: (_ for _ in ()).throw(OSError("store gone"))
        return base

    loop, ticks = quiet_loop(monkeypatch, run, clock, handlers)
    monkeypatch.setattr(
        loop.sched, "run_due", lambda *a, **k: (_ for _ in ()).throw(ValueError("tick"))
    )
    orig_sleep = loop.sleep

    def sleep(seconds):
        orig_sleep(seconds)
        if ticks["n"] == 3:
            (run.dir / "stop").write_text("stop")

    loop.sleep = sleep
    loop.run_forever(tick_s=300)
    rows = read_jsonl(run.out / "failures.jsonl")
    assert [r["job"] for r in rows] == ["tick", "tick", "tick"]
    assert ticks["n"] == 3  # the loop went on after every failed tick
    beats = read_jsonl(run.out / "heartbeat.jsonl")
    assert beats and beats[0]["phase"] == "setup"  # the pulse beat while the stores started
    assert loop.started() == at(8, 0)  # the start came from the state file, not from now


def test_loop_ends_at_the_start_plus_run_days_and_finished_survives_a_failed_summary(
    tmp_path, monkeypatch
):
    run = make_run(tmp_path, started=at(8, 0))
    clock = {"t": at(7, 59, day=8)}
    loop, ticks = quiet_loop(
        monkeypatch, run, clock, lambda self: {j.name: (lambda: None) for j in self.sched.jobs}
    )
    assert loop.params.run_days == 7 and not loop.over()
    clock["t"] = at(8, 0, day=8)
    assert loop.over()
    monkeypatch.setattr(loop, "daily_summary", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    loop.run_forever(tick_s=1)
    assert (run.dir / "finished").exists()
    assert read_jsonl(run.out / "failures.jsonl")[-1]["job"] == "finish"
    assert not (run.dir / "stop").exists()


def test_restart_mid_night_resumes_from_the_done_markers(tmp_path, monkeypatch):
    """The night runs two tasks. The process dies after the first (its marker is
    written, the second has none). The restart runs only the second, and the start
    time does not move."""
    run = make_run(tmp_path, started=at(8, 0))

    class Fx:
        def shards(self, size):
            return [0]

        def shard_dir(self, size, shard):
            return tmp_path / "shard"

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    monkeypatch.setattr(replay, "run_stock", lambda *a, **k: [])
    calls = []
    deaths = {"left": 1}

    def task(run_, fx, build, size, shard, *rest):
        calls.append((build, size, shard))
        if size == 20000 and deaths["left"]:
            deaths["left"] -= 1
            raise SystemExit("killed")  # the unit dies in the task: no marker is written
        run_.append("replay-done", {"day": 1, "build": build, "size": size, "shard": shard})
        return {"build": build, "size": size, "shard": shard, "index_s": 1, "ready_s": 1}

    monkeypatch.setattr(runner, "replay_task", task)
    kw = dict(builds=("B1",), day=1, parallel=1, python="python3", models=None)
    with pytest.raises(SystemExit):
        runner.replay_all(run, [5000, 20000], None, None, **kw)
    assert calls == [("B1", 5000, 0), ("B1", 20000, 0)]
    assert run.done(1) >= {("B1", 5000, 0)} and ("B1", 20000, 0) not in run.done(1)
    infos = runner.replay_all(run, [5000, 20000], None, None, **kw)  # the restart
    assert calls[2:] == [("B1", 20000, 0)] and len(infos) == 1  # the first is not replayed
    assert run.done(1) == {
        ("B1", 5000, 0),
        ("B1", 20000, 0),
        ("stock", 5000, 0),
        ("stock", 20000, 0),
    }
    assert runner.replay_all(run, [5000, 20000], None, None, **kw) == []  # nothing twice
    from longtest.loop import Loop

    monkeypatch.setattr(Loop, "setup", lambda self: None)
    again = Loop(run, 20000, now=lambda: at(3, 0, day=2))
    assert again.started() == at(8, 0) and again.day() == 1  # 19 hours in: still day 1
    assert Loop(run, 20000, now=lambda: at(9, 0, day=2)).day() == 2


# -- growth ---------------------------------------------------------------------------


def test_growth_adds_linked_notes_without_an_answer_and_only_once(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    config_dir = growth.kept_config_dir(run, 5000, 0)
    for name in ("-work-alpha", "-work-beta"):
        folder = config_dir / "projects" / name / "memory"
        folder.mkdir(parents=True)
        (folder / "MEMORY.md").write_text("- [Old note](old.md)")
        (folder / "old.md").write_text("---\nname: Old note\n---\n\nold\n")

    class Fx:
        lessons = {"L1": {"answer": "--zz-answer-zz", "title": "T"}}

        def shards(self, size):
            return [0]

        def shard_dir(self, size, shard):
            return tmp_path / "shard"

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    row = growth.grow(run, 5000, 1, 30, now=at(1))
    assert row["added"] == 30 and row["projects"] == 2 and row["notes_on_disk"] == 32
    assert sum(row["by_slot"].values()) == 30 and row["by_slot"]["index"] > 0
    alpha = config_dir / "projects" / "-work-alpha" / "memory"
    beta = config_dir / "projects" / "-work-beta" / "memory"
    assert len(list(alpha.glob("growth-d01-*.md"))) > len(list(beta.glob("growth-d01-*.md")))
    index = (alpha / "MEMORY.md").read_text()
    assert index.startswith("- [Old note](old.md)\n") and "growth-d01-" in index
    for path in config_dir.rglob("growth-d01-*.md"):
        assert "--zz-answer-zz" not in path.read_text()
    topic = list(alpha.glob("topic_growth_d01.md")) + list(beta.glob("topic_growth_d01.md"))
    if row["by_slot"]["topic"]:
        assert topic and "(topic_growth_d01.md)" in index + (beta / "MEMORY.md").read_text()
    again = growth.grow(run, 5000, 1, 30)
    assert again["skipped"] and again["added"] == 0
    assert len(read_jsonl(run.out / "growth.jsonl")) == 1
    assert growth.stock_dir(run, 5000, 0) == config_dir
    assert growth.stock_dir(run, 20000, 0) == tmp_path / "shard"  # no kept store: the fixture
    with pytest.raises(RuntimeError):
        growth.grow(run, 20000, 1, 5)


def test_growth_is_deterministic_for_a_day(tmp_path, monkeypatch):
    texts = []
    for i in range(2):
        run = make_run(tmp_path / str(i))
        folder = growth.kept_config_dir(run, 5000, 0) / "projects" / "-work-a" / "memory"
        folder.mkdir(parents=True)
        (folder / "MEMORY.md").write_text("")

        class Fx:
            lessons = {}

            def shards(self, size):
                return [0]

        monkeypatch.setattr(Run, "fixture", lambda self: Fx())
        growth.grow(run, 5000, 2, 5)
        texts.append(sorted((p.name, p.read_text()) for p in folder.glob("growth-*.md")))
    assert texts[0] == texts[1] and len(texts[0]) == 5


# -- the daily check and the report -----------------------------------------------------


def test_daily_check_of_the_large_profile_needs_no_determinism_and_writes_no_stop(tmp_path):
    run = make_run(tmp_path, started=at(7, 50))
    now = at(6, 30, day=4)
    for i in range(30):
        append_jsonl(
            run.out / "heartbeat.jsonl", {"ts": (now - dt.timedelta(minutes=5 * i)).isoformat()}
        )
    append_jsonl(run.out / "sandbox.jsonl", {"external_connects": 0, "claude_processes": 0})
    (run.dir / "state.json").write_text(
        json.dumps(
            {"jobs": {"heartbeat": {"failed": 0, "ok": True}}, "started": at(7, 50).isoformat()}
        )
    )
    report.daily_summary(run.dir, now)
    got = report.status(run.dir, now, free_gb=lambda p: 900.0, plugin_version=lambda: "0.1.21")
    assert got["ok"], got["failed"]  # no determinism row, and no determinism check
    append_jsonl(run.out / "sandbox.jsonl", {"external_connects": 1})
    got = report.status(run.dir, now, free_gb=lambda p: 900.0, plugin_version=lambda: "0.1.21")
    assert got["failed"] == ["no_external_connects"] and (run.dir / "ALERT").exists()
    assert sorted(p.name for p in run.dir.iterdir() if p.is_file()) == [
        "ALERT",
        "run.json",
        "state.json",
    ]


def test_heartbeat_cadence_flags_a_gap_over_twice_the_interval():
    t0 = at(9)
    beats = [t0 + dt.timedelta(minutes=5 * i) for i in range(13)]
    assert report.heartbeat_cadence(beats, 300)["ok"]
    gap = beats[:6] + [t + dt.timedelta(minutes=6) for t in beats[6:]]
    got = report.heartbeat_cadence(gap, 300)
    assert not got["ok"] and got["max_gap_s"] == 660.0 and got["limit_s"] == 600
    assert not report.heartbeat_cadence([t0], 300)["ok"]
    assert report.heartbeat_cadence(beats, 300, since=beats[10])["n"] == 3


def test_delivery_trend_over_the_nights():
    rows = [
        {"build": "B1", "size": 5000, "day": d, "claims": ["C1", "C17"], "hit": h}
        for d in range(1, 8)
        for h in ([True] * 8 + [False] * (d % 3))
    ] + [{"build": "B1", "size": 5000, "day": 0, "claims": ["C1"], "hit": False}]
    by_night = report.delivery_by_night(rows)
    assert sorted(by_night[5000]) == [1, 2, 3, 4, 5, 6, 7]  # day 0 left out
    trend = report.delivery_trend(by_night)
    assert trend["5000"]["nights"] == 7 and trend["5000"]["ok"] in (True, False)
    short = report.delivery_trend({20000: {1: 0.5, 2: 0.6}})
    assert short["20000"]["ok"] is None and short["20000"]["nights"] == 2
    falling = {5000: {d: 1.0 - 0.1 * d for d in range(1, 8)}}
    assert report.delivery_trend(falling)["5000"]["ok"] is False
    rising = {5000: {d: 0.5 + 0.05 * d for d in range(1, 8)}}
    assert report.delivery_trend(rising)["5000"]["ok"] is True


def comparison(low, high, p):
    return {"n": 60, "p": p, "diff": (low + high) / 2, "low": low, "high": high}


def soak_eval(ok=True):
    return {
        "checks": {"budget_recall": ok, "lag_p95": ok, "drift_zero": True, "error_rate": True},
        "pass": ok,
    }


def test_verdicts_large_win_lose_tie_pending():
    s = {
        "b1_vs_s2": {
            "5000|C1": comparison(0.3, 0.6, 1e-6),
            "5000|C2": comparison(0.0, 0.2, 0.5),
            "20000|C1": comparison(-0.5, -0.1, 0.01),
            "20000|C2": comparison(-0.2, 0.1, 0.5),
        },
        "false_delivery_v5": {"5000|V5|B1": {"rate": 0.05, "high": 0.1}},
    }
    trend = {"5000": {"ok": True, "nights": 7}, "20000": {"ok": True, "nights": 7}}
    got = report.verdicts_large(s, soak_eval(), trend)
    assert got["scales"]["LARGE-1"]["verdict"] == "WIN"
    assert got["scales"]["LARGE-1"]["dr3"] is True and got["scales"]["LARGE-1"]["dr5"] is True
    assert got["scales"]["LARGE-2"]["verdict"] == "LOSE"
    assert got["claims"]["5000|C1"]["dr1"] and not got["claims"]["20000|C1"]["dr1"]
    pending = report.verdicts_large(s, None, None)
    assert pending["scales"]["LARGE-1"]["verdict"] == "PENDING"
    assert (
        pending["scales"]["LARGE-1"]["dr5"] is None and pending["scales"]["LARGE-1"]["dr6"] is None
    )
    tie = report.verdicts_large(s, soak_eval(ok=False), trend)
    assert tie["scales"]["LARGE-1"]["verdict"] == "TIE" and tie["dr5"] is False
    s["b1_vs_s2"]["5000|C2"] = comparison(-0.3, -0.1, 0.01)  # more false rows than stock
    assert report.verdicts_large(s, soak_eval(), trend)["scales"]["LARGE-1"]["verdict"] == "LOSE"
    assert (
        report.verdicts_large({"b1_vs_s2": {}}, None, None)["scales"]["LARGE-1"]["verdict"]
        == "NO DATA"
    )


def test_stock_near_miss_counts_a_wrong_note_as_a_false_row(tmp_path):
    folder = tmp_path / "projects" / "-work-p" / "memory"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text("- [Mirror staging note](near_a.md)\n- [Other](other.md)\n")
    (folder / "near_a.md").write_text("the staging mirror holds the queue drain rule\n")
    (folder / "near_b.md").write_text("read replica words only\n")
    (folder / "other.md").write_text("nothing here\n")
    from longtest.stocksim import Stock

    stock = Stock(folder)
    got = replay.stock_near_miss(
        stock, "drain the queue on the staging mirror", ["near_a.md", "near_b.md"]
    )
    assert got == {"S0": True, "S1": True, "S2": True}
    assert replay.stock_near_miss(stock, "zebra", ["near_b.md"]) == {
        "S0": False,
        "S1": False,
        "S2": True,
    } or (replay.stock_near_miss(stock, "zebra", ["near_b.md"])["S0"] is False)
    assert replay.stock_near_miss(None, "x", ["a"]) == {"S0": False, "S1": False, "S2": False}
    assert "V5" in replay.STOCK_NEGATIVE and "V5" not in replay.STOCK_VARIANTS


def test_run_stock_writes_a_v5_row_with_false_delivery(tmp_path):
    folder = tmp_path / "projects" / "-work-p" / "memory"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text("- [Near](near.md)\n")
    (folder / "near.md").write_text("near miss text\n")

    class Fx:
        root = tmp_path
        lessons = {"L1": {"answer": "--x", "near_miss_notes": ["near.md"], "title": "T"}}

        def queries_for(self, size, variants, shard):
            q = {
                "id": "q5",
                "variant": "V5",
                "near_lesson": "L1",
                "claims": ["C2"],
                "expect": "none",
            }
            return [q] if "V5" in set(variants) else []

        def lesson_of(self, q):
            return q.get("lesson") or q.get("near_lesson")

        def cwd_of(self, size, q):
            return "/work/p"

        def prompt_of(self, q):
            return "near miss"

    rows = replay.run_stock(Fx(), 5000, 0, tmp_path, ("V5",), None)
    by = {r["build"]: r for r in rows}
    assert by["S0"]["false_delivery"] is True and by["S0"]["hit"] is False
    assert by["S2"]["claims"] == ["C2"] and by["S2"]["channel"] == "stock"


# -- the units and the watchdog -------------------------------------------------------


def test_units_of_the_large_profile_restart_always_and_carry_the_watchdog(tmp_path):
    units = sandbox.render_units(
        tmp_path,
        "/usr/bin/python3",
        Path("/opt/tools"),
        params=profiles.LARGE.params,
        settings=profiles.LARGE.units,
        watchdog=True,
        systemctl="/usr/bin/systemctl",
    )
    assert sorted(units) == [
        "nob-longtest-localnet.service",
        "nob-longtest-watch.service",
        "nob-longtest-watch.timer",
        "nob-longtest.service",
    ]
    for name in ("nob-longtest.service", "nob-longtest-localnet.service"):
        text = units[name]
        for line in (
            "Restart=always",
            "RestartSec=30",
            "StartLimitIntervalSec=0",
            "RuntimeMaxSec=8d",
            f"TasksMax={sandbox.tasks_max()}",
            f"ConditionPathExists=!{tmp_path}/finished",
            f"ConditionPathExists=!{tmp_path}/stop",
            "WantedBy=default.target",
        ):
            assert line in text, (name, line)
        assert "Restart=on-failure" not in text
    assert sandbox.tasks_max(32) == 1024
    timer, service = units["nob-longtest-watch.timer"], units["nob-longtest-watch.service"]
    assert "OnCalendar=*-*-* *:00/5:00" in timer and "Unit=nob-longtest-watch.service" in timer
    assert "WantedBy=timers.target" in timer
    assert f"longtest.cli watch --run-dir {tmp_path} --systemctl /usr/bin/systemctl" in service
    assert "IPAddressDeny=any" in service and "Type=oneshot" in service
    assert "PrivateNetwork" not in service
    old = sandbox.render_units(tmp_path, "/usr/bin/python3", Path("/opt/tools"))
    assert "Restart=on-failure" in old["nob-longtest.service"] and len(old) == 2


def test_watchdog_plan_starts_quiet_units_and_restarts_a_stale_core():
    core, local = watchdog.CORE, "nob-longtest-localnet.service"
    plan = watchdog.plan({core: "active", local: "active"}, 100.0, False)
    assert plan == []
    plan = watchdog.plan({core: "inactive", local: "failed"}, None, False)
    assert plan == [("start", core, "inactive"), ("start", local, "failed")]
    plan = watchdog.plan({core: "active", local: "active"}, 901.0, False)
    assert plan == [("restart", core, "heartbeat 901 s old")]
    assert watchdog.plan({core: "activating", local: "active"}, 2000.0, False) == []
    assert watchdog.plan({core: "inactive", local: "inactive"}, 5000.0, True) == []  # finished


def fake_systemctl(tmp_path, states):
    script = tmp_path / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = is-active ]; then cat "{tmp_path}/state-$2"; exit 0; fi\n'
        f'echo "$1 $2" >> "{tmp_path}/calls"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    for unit, state in states.items():
        (tmp_path / f"state-{unit}").write_text(state + "\n")
    return script


def test_watchdog_run_once_acts_and_logs(tmp_path):
    run = make_run(tmp_path)
    ctl = fake_systemctl(
        tmp_path, {watchdog.CORE: "active", "nob-longtest-localnet.service": "inactive"}
    )
    now = dt.datetime.now().astimezone()
    beat = run.out / "heartbeat.jsonl"
    beat.write_text("{}\n")
    os.utime(beat, (now.timestamp() - 1000, now.timestamp() - 1000))
    rows = watchdog.run_once(run.dir, systemctl=[str(ctl)], now=now)
    assert [(r["action"], r["unit"]) for r in rows] == [
        ("start", "nob-longtest-localnet.service"),
        ("restart", watchdog.CORE),
    ]
    assert all(r["rc"] == 0 for r in rows)
    assert (tmp_path / "calls").read_text().splitlines() == [
        "start nob-longtest-localnet.service",
        f"restart {watchdog.CORE}",
    ]
    assert len(read_jsonl(run.out / "watchdog.jsonl")) == 2
    (run.dir / "finished").write_text("done")
    assert watchdog.run_once(run.dir, systemctl=[str(ctl)], now=now) == []
    assert watchdog.heartbeat_age_s(tmp_path / "none", now) is None


# -- the pilot gates --------------------------------------------------------------------


def test_stores_ready_gate_needs_every_store_inside_its_cap_and_its_marker(tmp_path, monkeypatch):
    run = make_run(tmp_path)

    class Fx:
        def shards(self, size):
            return [0]

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    infos = [
        {"build": "B1", "size": 5000, "index_s": 10.0, "ready_s": 20.0},
        {"build": "B1", "size": 20000, "index_s": 30.0, "ready_s": 40.0},
    ]
    got = cli.stores_ready_gate(run, infos, [5000, 20000], 900.0)
    assert got["ok"] and not got["shards_done"]
    assert got["sizes"]["20000"]["cap_s"] == 20000.0 and got["sizes"]["5000"]["ready_s"] == 30.0
    for size in (5000, 20000):
        run.append("replay-done", {"day": 0, "build": "B1", "size": size, "shard": 0})
    assert cli.stores_ready_gate(run, infos, [5000, 20000], 900.0)["shards_done"]
    assert not cli.stores_ready_gate(run, infos[:1], [5000, 20000], 900.0)["ok"]
    assert not cli.stores_ready_gate(run, infos, [5000], 900.0, error="x")["ok"]


def test_sanity_gate_of_b1_alone_reports_stock_without_gating_it():
    rows = [
        {
            "variant": "V1",
            "build": "B1",
            "hit": True,
            "channel": "pull",
            "expect": "a",
            "size": 5000,
        }
    ] * 20
    rows += [
        {
            "variant": "V1",
            "build": "S2",
            "hit": False,
            "channel": "stock",
            "expect": "a",
            "size": 5000,
        }
    ] * 20
    got = cli.sanity_gate(rows, builds=("B1",))
    assert got["ok"] and got["S2"]["delivery"] == 0.0 and "B0" not in got
    assert not cli.sanity_gate(rows)["ok"]  # the first profile gates B0 and S2 too


def test_claims_gate_is_limited_to_the_claims_in_scope():
    rows = [{"build": "B1", "claims": ["C1"], "hit": True}]
    got = cli.claims_gate(rows, {}, claims=profiles.LARGE.claims)
    assert got["ok"] and sorted(got["claims"]) == sorted(profiles.LARGE.claims)
    assert got["claims"]["C1"]["state"] == "fired"
    assert got["claims"]["C11"]["note"] == "arm d (soak)"


def test_seed_stores_refuses_another_build_or_fixture(tmp_path, monkeypatch):
    run = make_run(tmp_path / "mine")
    src = make_run(tmp_path / "theirs")
    (tmp_path / "mine" / "run" / "fixtures" / "scale-05000" / "shard-00").mkdir(parents=True)
    (tmp_path / "theirs" / "run" / "fixtures" / "scale-05000" / "shard-00").mkdir(parents=True)
    for root in (tmp_path / "mine", tmp_path / "theirs"):
        (root / "run" / "fixtures" / "manifest.json").write_text(
            json.dumps({"scales": {"5000": {}}, "seed": 7})
        )
        (root / "run" / "fixtures" / "lessons.json").write_text("[]")
        (root / "run" / "fixtures" / "queries.jsonl").write_text("")
        (root / "run" / "fixtures" / "scale-05000" / "scale.json").write_text(
            json.dumps({"lessons": {"L": {"shard": 0}}})
        )
        (root / "run" / "fixtures" / "scale-05000" / "shard-00" / "n.md").write_text("same")
    store = (
        runner.shard_root(src, "B1", 5000, 0)
        / "home"
        / ".claude"
        / "plugins"
        / "data"
        / "noblivion-noblivion"
    )
    store.mkdir(parents=True)
    (store / "store.json").write_text("{}")
    (store / "noblivion.db").write_text("db")
    doc = src.manifest
    doc["builds"]["B1"]["commit"] = "other"
    src.save_manifest(doc)
    with pytest.raises(SystemExit):
        runner.seed_stores_from(run, src.dir, [5000])
    doc["builds"]["B1"]["commit"] = "c1"
    src.save_manifest(doc)
    got = runner.seed_stores_from(run, src.dir, [5000])
    target = runner.shard_root(run, "B1", 5000, 0)
    assert (
        target / "home/.claude/plugins/data/noblivion-noblivion/noblivion.db"
    ).read_text() == "db"
    assert not (target / "home/.claude/plugins/data/noblivion-noblivion/store.json").exists()
    assert got["5000"]["commit"] == "c1" and run.manifest["seeded_stores"]["B1"]["5000"]["from"]
    assert runner.seed_stores_from(run, src.dir, [5000])["5000"] == {"skipped": "store exists"}
    (tmp_path / "theirs" / "run" / "fixtures" / "scale-05000" / "shard-00" / "n.md").write_text(
        "diff"
    )
    run2 = make_run(tmp_path / "mine2")
    (tmp_path / "mine2" / "run" / "fixtures").symlink_to(tmp_path / "mine" / "run" / "fixtures")
    with pytest.raises(SystemExit):
        runner.seed_stores_from(run2, src.dir, [5000])


# -- the review fixes ---------------------------------------------------------------------


def test_prepare_keeps_the_grown_indexes_of_a_kept_store(tmp_path):
    from longtest.buildrunner import Harness

    build = tmp_path / "build"
    (build / "config").mkdir(parents=True)
    (build / "config" / "config.default.json").write_text("{}")
    fx_root = tmp_path / "fx" / "scale-05000" / "shard-00" / "projects" / "-work-a" / "memory"
    fx_root.mkdir(parents=True)
    (fx_root / "MEMORY.md").write_text("- [Old](old.md)\n")

    class Fx:
        def shard_dir(self, size, shard):
            return tmp_path / "fx" / "scale-05000" / "shard-00"

    h = Harness("B1", build, tmp_path / "root", "python3", None)
    folders = h.prepare(Fx(), 5000, 0)
    index = folders[0] / "MEMORY.md"
    assert index.read_text() == "- [Old](old.md)\n"
    index.write_text("- [Old](old.md)\n- [Grown](growth-d01-00000-x.md)\n")
    again = Harness("B1", build, tmp_path / "root", "python3", None)
    assert again.prepare(Fx(), 5000, 0) == folders
    assert "Grown" in index.read_text()  # the second night does not put the first index back


def test_pilot_loop_jobs_beat_every_five_minutes():
    jobs = {j.name: j for j in scheduler.light_jobs(profiles.LARGE.params)}
    assert jobs["heartbeat"].every_s == 300 and "replay_nightly" not in jobs
    assert {j.name for j in scheduler.light_jobs()} == set(jobs)


def test_stores_gate_reads_the_timings_of_a_done_marker_on_a_rerun(tmp_path, monkeypatch):
    run = make_run(tmp_path)

    class Fx:
        def shards(self, size):
            return [0]

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    run.append(
        "replay-done",
        {
            "day": 0,
            "build": "B1",
            "size": 20000,
            "shard": 0,
            "index_s": 5.0,
            "ready_s": 60.0,
            "memories": 20629,
        },
    )
    got = cli.stores_ready_gate(run, [], [20000], 900.0)
    assert got["ok"] and got["shards_done"] and got["sizes"]["20000"]["ready_s"] == 65.0
    assert got["sizes"]["20000"]["memories"] == 20629


def test_unit_limits_gate_wants_a_task_limit(tmp_path):
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    proc.joinpath("cgroup").write_text("0::/user.slice/nob-longtest.service\n")
    cg = tmp_path / "cg" / "user.slice" / "nob-longtest.service"
    cg.mkdir(parents=True)
    (cg / "pids.max").write_text("1024\n")
    (cg / "memory.max").write_text("12884901888\n")
    got = cli.unit_limits(tmp_path / "proc", tmp_path / "cg")
    assert got["ok"] and got["pids_max"] == "1024" and got["memory_max"] == "12884901888"
    (cg / "pids.max").write_text("max\n")
    assert not cli.unit_limits(tmp_path / "proc", tmp_path / "cg")["ok"]
    assert not cli.unit_limits(tmp_path / "none", tmp_path / "cg")["ok"]


def test_growth_run_again_after_a_kill_adds_no_second_link(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    folder = growth.kept_config_dir(run, 5000, 0) / "projects" / "-work-a" / "memory"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text("")

    class Fx:
        lessons = {}

        def shards(self, size):
            return [0]

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    growth.grow(run, 5000, 1, 20)
    (run.out / "growth.jsonl").unlink()  # as if the kill came before the row was written
    growth.grow(run, 5000, 1, 20)
    index = (folder / "MEMORY.md").read_text()
    links = [ln for ln in index.splitlines() if ln.startswith("- [")]
    assert len(links) == len(set(links)) and len(list(folder.glob("growth-*.md"))) == 20
    for topic in folder.glob("topic_growth_*.md"):
        lines = [ln for ln in topic.read_text().splitlines() if ln.startswith("- [")]
        assert len(lines) == len(set(lines))


def test_a_start_inside_the_heavy_window_moves_to_its_end(tmp_path, monkeypatch):
    from longtest.loop import Loop

    run = make_run(tmp_path)
    loop = Loop(run, 20000, now=lambda: at(3, 30))
    assert loop.first_start() == at(6, 0)
    assert Loop(run, 20000, now=lambda: at(7, 10)).first_start() == at(7, 10)
    early = make_run(tmp_path / "early", started=at(6, 0))
    assert Loop(early, 20000, now=lambda: at(5, 30)).day() == 1  # never day 0 before the start


def test_finish_keeps_a_heartbeat_while_the_last_night_ends(tmp_path, monkeypatch):
    run = make_run(tmp_path, started=at(8, 0))
    clock = {"t": at(8, 0, day=8)}
    loop, _ = quiet_loop(
        monkeypatch, run, clock, lambda self: {j.name: (lambda: None) for j in self.sched.jobs}
    )
    import time as time_mod

    monkeypatch.setattr(loop.sched, "wait", lambda *a, **k: time_mod.sleep(0.3))
    loop.params = profiles.LARGE.params.__class__(
        **{
            **profiles.LARGE.params.as_dict(),
            "scales": profiles.LARGE.params.scales,
            "heartbeat_s": 1,
        }
    )
    loop.finish()
    phases = [r.get("phase") for r in read_jsonl(run.out / "heartbeat.jsonl")]
    assert "finish" in phases and (run.dir / "finished").exists()


def test_seed_stores_writes_nothing_in_the_source(tmp_path, monkeypatch):
    run = make_run(tmp_path / "mine")
    src = make_run(tmp_path / "theirs")
    for root in (tmp_path / "mine", tmp_path / "theirs"):
        shard = root / "run" / "fixtures" / "scale-05000" / "shard-00"
        shard.mkdir(parents=True)
        (root / "run" / "fixtures" / "manifest.json").write_text(
            json.dumps({"scales": {"5000": {}}, "seed": 7})
        )
        (root / "run" / "fixtures" / "lessons.json").write_text("[]")
        (root / "run" / "fixtures" / "queries.jsonl").write_text("")
        (root / "run" / "fixtures" / "scale-05000" / "scale.json").write_text(
            json.dumps({"lessons": {"L": {"shard": 0}}})
        )
        (shard / "n.md").write_text("same")
    src_root = runner.shard_root(src, "B1", 5000, 0)
    data = src_root / "home" / ".claude" / "plugins" / "data" / "noblivion-noblivion"
    data.mkdir(parents=True)
    (data / "noblivion.db").write_text("db")
    (src_root / "guard-state").mkdir()
    (src_root / "guard-state" / "s").write_text("volatile")
    (src_root / "guard-log.jsonl").write_text("log")
    before = runner.snapshot_tree(src.dir)
    runner.seed_stores_from(run, src.dir, [5000])
    assert runner.snapshot_tree(src.dir) == before  # the source run is only read
    target = runner.shard_root(run, "B1", 5000, 0)
    assert not (target / "guard-state").exists() and not (target / "guard-log.jsonl").exists()
