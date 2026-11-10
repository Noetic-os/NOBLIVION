# SPDX-License-Identifier: AGPL-3.0-or-later
"""The time-compressed run: growth steps back to back, judged as nights."""

from __future__ import annotations

import argparse
import datetime as dt
import json

import longtest_path  # noqa: F401 - puts tools on the path
import pytest
from test_longtest_validity import row, step

from longtest import cli, compressed, growth, profiles, sandbox
from longtest.runner import Run
from longtest.util import read_jsonl

NOON = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)  # outside the heavy window


def make_run(tmp_path, monkeypatch, profile="large-project-steps", sizes=(5000, 20000)):
    run_dir = tmp_path / "run"
    for sub in ("out", "private"):
        (run_dir / sub).mkdir(parents=True)
    live = {"config_dir": str(tmp_path / "live"), "data_dir": str(tmp_path / "live" / "data")}
    doc = {"run_id": "t", "profile": profile, "sizes": list(sizes), "live": live}
    (run_dir / "run.json").write_text(json.dumps(doc))

    class Off:
        refused = refused_reads = refused_writes = 0

        def __init__(self, *a, **k):
            pass

        def install(self):
            pass

        uninstall = install

    monkeypatch.setattr(sandbox, "start_check", lambda env, program: {"ok": True})
    monkeypatch.setattr(sandbox, "OpenGuard", Off)
    monkeypatch.setattr(sandbox, "SocketGuard", Off)
    monkeypatch.setattr(sandbox, "claude_processes", lambda pid: 0)
    return Run(run_dir)


def fake_grow(calls):
    def grow(run, size, day, count, now=None):
        if (size, day) in growth.done(run):  # as the real one: a step never grows twice
            return {"size": size, "day": day, "added": 0, "skipped": True}
        calls.append((size, day, count))
        run.append("growth", {"size": size, "day": day, "added": count})
        return {"size": size, "day": day, "added": count}

    return grow


def fake_replay(calls, b1=60, s2=55, fail=None):
    """``replay_all`` of one step: the rows and the markers of the tasks not done yet.
    ``fail(day, attempt)`` may raise before a size is written."""

    def replay_all(run, sizes, shards, limit, **kw):
        day = kw["day"]
        assert kw["parallel"] == 1 and kw["builds"] == ("B1",) and kw["stock_arms"] == ("S2",)
        calls.append(day)
        for size in sizes:
            if ("B1", size, 0) in run.done(day):
                continue
            if fail:
                fail(day, calls.count(day), size)
            rows = step(size, day, b1, s2)
            rows += [
                dict(row(b, size, day, i, "V5", True, ["C2"]))
                for b in ("B1", "S2")
                for i in range(60)
            ]
            run.append_many("replay", rows)
            run.append("replay-done", {"day": day, "build": "B1", "size": size, "shard": 0})
        return []

    return replay_all


def test_the_steps_profile_has_its_own_pre_registration():
    prof = profiles.get("large-project-steps")
    assert prof.steps == 7 and prof.growth_per_day == 500 and prof.builds == ("B1",)
    assert prof.sizes == (5000, 20000) and prof.params == profiles.LARGE.params
    assert set(prof.claims) == {"C1", "C2", "C3", "C4", "C10", "C13", "C16", "C17"}
    text = prof.prereg()
    assert "7 growth steps, back to back, with no nightly window and no wait" in text
    assert "0.95 or more for B1 and 0.9 or more for S2" in text and "INVALID" in text
    assert "store.hung" in text and "One store starts at a time." in text
    assert "DR5" in text and "- DR5:" not in text  # named as out of scope, not as a rule
    assert prof.prereg_hash() != profiles.LARGE.prereg_hash()
    nightly = profiles.LARGE.prereg()  # the text of the 7-day run did not move
    assert "## Validity" not in nightly and "24 hours" in nightly and profiles.LARGE.steps == 0
    a = cli.build_parser().parse_args(["steps", "--run-dir", "x", "--steps", "3", "--pilot"])
    assert a.func is cli.cmd_steps and a.steps == 3 and a.pilot and a.retries == 2


def test_steps_run_back_to_back_resume_after_a_kill_and_are_judged_as_nights(
    tmp_path, monkeypatch, capsys
):
    run = make_run(tmp_path, monkeypatch)
    grown, replays = [], []

    def kill(day, attempt, size):
        if day == 2 and size == 20000 and attempt == 1:
            raise SystemExit("killed")  # the process dies in the middle of step 2

    monkeypatch.setattr(growth, "grow", fake_grow(grown))
    monkeypatch.setattr(compressed, "replay_all", fake_replay(replays, fail=kill))
    monkeypatch.setattr(compressed.time, "sleep", lambda s: pytest.fail("a step waited"))
    with pytest.raises(SystemExit):
        compressed.run_steps(run, 3, now=lambda: NOON)
    assert sorted(compressed.steps_done(run)) == [1]  # step 2 has no row: it is not done
    assert run.done(2) == {("B1", 5000, 0)}
    res = compressed.run_steps(run, 3, now=lambda: NOON)  # the restart
    assert res["ran"] == [2, 3] and res["done"] == [1, 2, 3] and res["complete"]
    assert res["stopped"] == "" and res["valid_steps"] == [1, 2, 3] and res["invalid"] == {}
    assert replays == [1, 2, 2, 3]
    assert grown == [(s, d, 500) for d in (1, 2, 3) for s in (5000, 20000)]  # each step once
    rows = read_jsonl(run.out / "replay.jsonl")
    per_step = {d: sum(1 for r in rows if r["day"] == d and r["size"] == 5000) for d in (1, 2, 3)}
    assert per_step[1] == per_step[2] == per_step[3] > 0  # no shard of step 2 was replayed twice
    steps = read_jsonl(run.out / "steps.jsonl")
    assert [s["step"] for s in steps] == [1, 2, 3] and all(s["ok"] for s in steps)
    assert steps[0]["gate"]["20000"]["arms"]["B1"]["delivery"] == 1.0
    assert not (run.dir / "state.json").exists()  # no scheduler, no window, no day clock
    assert compressed.run_steps(run, 3, now=lambda: NOON)["ran"] == []  # nothing twice
    # the report judges the steps as nights: the last step for DR1 and DR3, all for DR6
    assert cli.cmd_report(argparse.Namespace(run_dir=str(run.dir))) == 0
    scales = json.loads(capsys.readouterr().out)
    doc = json.loads((run.dir / "final_report.json").read_text())["verdicts"]
    assert doc["judged_night"] == 3 and doc["dr5_in_scope"] is False
    assert doc["trend"]["20000"]["nights"] == 3 and len(doc["steps"]) == 3
    assert scales["LARGE-2"]["dr6"] is True and scales["LARGE-2"]["dr5"] is None
    assert scales["LARGE-2"]["verdict"] == "WIN"  # DR5 has no data and is not waited for


def test_a_failed_step_runs_again_at_once_and_gives_up_after_its_retries(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    replays = []

    def latched(day, attempt, size):
        if attempt <= 2:
            raise RuntimeError("B1 size 5000 shard 0: 7 of 30 rows ran under the hook latch")

    monkeypatch.setattr(growth, "grow", fake_grow([]))
    monkeypatch.setattr(compressed, "replay_all", fake_replay(replays, fail=latched))
    with pytest.raises(RuntimeError):
        compressed.run_steps(run, 1, now=lambda: NOON, retries=1)
    assert compressed.steps_done(run) == {} and replays == [1, 1]
    replays.clear()
    res = compressed.run_steps(run, 1, now=lambda: NOON, retries=2)
    assert res["complete"] and read_jsonl(run.out / "steps.jsonl")[0]["attempts"] == 3
    failures = read_jsonl(run.out / "failures.jsonl")
    assert [f["job"] for f in failures] == ["step-1"] * 4 and "hook latch" in failures[0]["error"]


def test_a_build_that_did_not_fire_stops_the_run_and_a_stock_miss_does_not(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, sizes=(5000,))
    monkeypatch.setattr(growth, "grow", fake_grow([]))
    monkeypatch.setattr(compressed, "replay_all", fake_replay([], b1=0))
    res = compressed.run_steps(run, 3, now=lambda: NOON)
    assert res["ran"] == [1] and not res["complete"]
    assert res["stopped"] == "step 1: a build did not fire on the sanity set"
    assert res["invalid"]["1"]["5000"]["causes"] == [
        "step 1: B1 sanity delivery 0.0 of 60, under the floor 0.95"
    ]
    assert compressed.run_steps(run, 3, now=lambda: NOON, keep_going=True)["done"] == [1, 2, 3]

    other = make_run(tmp_path / "b", monkeypatch, sizes=(5000,))
    monkeypatch.setattr(compressed, "replay_all", fake_replay([], s2=47))
    res = compressed.run_steps(other, 2, now=lambda: NOON)
    assert res["complete"] and res["stopped"] == "" and res["valid_steps"] == []
    assert "S2 sanity delivery 0.783 of 60" in res["invalid"]["2"]["5000"]["causes"][0]
    (other.dir / "stop").write_text("stop")
    assert compressed.run_steps(other, 3, now=lambda: NOON)["stopped"] == "stop file"


def test_the_commands_keep_the_two_run_modes_apart(tmp_path, monkeypatch):
    nightly = make_run(tmp_path / "n", monkeypatch, profile="large-project")
    args = dict(steps=0, pilot=False, retries=2, keep_going=False, wait=900.0)
    with pytest.raises(SystemExit, match="runs nights"):
        cli.cmd_steps(argparse.Namespace(run_dir=str(nightly.dir), **args))
    run = make_run(tmp_path / "s", monkeypatch)
    monkeypatch.setattr(cli, "verify_frozen", lambda run: [])
    with pytest.raises(SystemExit, match="pilot has not passed"):
        cli.cmd_steps(argparse.Namespace(run_dir=str(run.dir), **args))
    (run.dir / "pilot.json").write_text(json.dumps({"all_gates": True}))
    with pytest.raises(SystemExit, match="runs steps"):
        cli.cmd_run(argparse.Namespace(run_dir=str(run.dir), notify_cmd="", soak_size=20000))


def test_the_short_pilot_gates_the_build_and_shows_what_the_report_gate_would_say(
    tmp_path, monkeypatch, capsys
):
    run = make_run(tmp_path, monkeypatch, sizes=(5000,))

    class Fx:
        def shards(self, size):
            return [0]

    def pilot_replay(run_, sizes, shards, limit, **kw):
        assert kw["day"] == 0 and shards == 1 and limit == 10
        run_.append_many("replay", step(5000, 0, 10, 7, n=10))
        marker = {"day": 0, "build": "B1", "size": 5000, "shard": 0, "index_s": 1, "ready_s": 2}
        run_.append("replay-done", marker)
        return [dict(marker, memories=5000)]

    gate2 = {"ok": True, "public_connect_refused": True, "claude_processes": 0}
    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    monkeypatch.setattr(cli, "zero_token_gate", lambda run, python: dict(gate2))
    monkeypatch.setattr(cli, "freeze_gate", lambda run: {"ok": True})
    monkeypatch.setattr(cli, "replay_all", pilot_replay)
    monkeypatch.setattr(cli, "claims_gate", lambda rows, fired, claims: {"ok": True})
    a = cli.build_parser().parse_args(["steps", "--run-dir", str(run.dir), "--pilot"])
    assert cli.cmd_steps(a) == 0
    shown = json.loads(capsys.readouterr().out)
    doc = json.loads((run.dir / "pilot.json").read_text())
    assert doc["mode"] == "compressed" and doc["all_gates"] is True
    assert set(doc["gates"]) == {
        "freeze",
        "zero_tokens",
        "stores_ready",
        "replay_shard_done",
        "sanity",
        "claims",
        "isolation",
        "no_external_connects",
    }  # no soak hour, no heartbeat, no unit: the run has none of them
    arms = shown["sanity_by_scale"]["5000"]["arms"]
    assert arms["B1"] == {"n": 10, "delivery": 1.0, "floor": 0.95, "ok": True}
    assert arms["S2"] == {"n": 10, "delivery": 0.7, "floor": 0.9, "ok": False}
    assert shown["report_gate_would_pass"] is False  # stock under its floor: said, not hidden
