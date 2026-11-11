# SPDX-License-Identifier: AGPL-3.0-or-later
"""The time-compressed run: growth steps back to back, judged as nights."""

from __future__ import annotations

import argparse
import datetime as dt
import json

import longtest_path  # noqa: F401 - puts tools on the path
import pytest
from test_longtest_validity import FakeFixture, row, step

from longtest import cli, compressed, growth, profiles, report, sandbox
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
    monkeypatch.setattr(Run, "fixture", lambda self: FakeFixture())  # the queries of fake_replay
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
    assert "the recall tool) is 0.95 or more for B1, and no row is invalid" in text
    assert "A stock arm (S2): the gate is mechanical, every query of the step" in text
    assert "INVALID" in text and "0.9 or more" not in text
    # the ruling of 2026-11-11 and its reason are in the text that is frozen
    assert "A run with fewer than 7 finished steps gets the result INCOMPLETE" in text
    assert "## Fairness" in text and "Ruling of 2026-11-11" in text
    assert "S2 is a deterministic simulation" in text and "cannot latch" in text
    assert "0.70 to 0.80 at 5,000 and 20,000 notes" in text
    assert "store.hung" in text and "One store starts at a time." in text
    # the rule that was measured: ready is no embedding backlog, then both channels warm
    assert "ready only when its index is idle and no chunk of a live note lacks a vector" in text
    assert "one call of the recall tool and one prompt per round" in text
    assert "until both answer inside 2 s, for 60 s at most" in text
    assert "(store_hung), or whose hook gave any fail reason (timeout, store_down" in text
    assert "the store did not answer), or whose call did not run, is invalid, not a miss" in text
    assert "ORT_INTRA_OP_NUM_THREADS=4" in text and "OMP_NUM_THREADS does not bind" in text
    assert "The prompt channel of a build has no floor" in text
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
    # 3 of the 7 registered steps: the report gives no verdict
    assert cli.cmd_report(argparse.Namespace(run_dir=str(run.dir))) == 0
    scales = json.loads(capsys.readouterr().out)
    for scale in ("LARGE-1", "LARGE-2"):
        assert scales[scale]["verdict"] == "INCOMPLETE"
        assert scales[scale]["incomplete_because"] == [
            "3 of 7 steps done; missing steps 4, 5, 6, 7"
        ]
        assert scales[scale]["dr1_c1"] is None and scales[scale]["dr6"] is None
    # the rest of the steps: the report judges the steps as nights, the last step for
    # DR1 and DR3, all of them for DR6
    assert compressed.run_steps(run, now=lambda: NOON)["ran"] == [4, 5, 6, 7]
    assert cli.cmd_report(argparse.Namespace(run_dir=str(run.dir))) == 0
    scales = json.loads(capsys.readouterr().out)
    doc = json.loads((run.dir / "final_report.json").read_text())["verdicts"]
    assert doc["judged_night"] == 7 and doc["dr5_in_scope"] is False and doc["incomplete"] == []
    assert doc["trend"]["20000"]["nights"] == 7 and len(doc["steps"]) == 7
    assert scales["LARGE-2"]["dr6"] is True and scales["LARGE-2"]["dr5"] is None
    assert scales["LARGE-2"]["verdict"] == "WIN"  # DR5 has no data and is not waited for
    assert "incomplete_because" not in scales["LARGE-2"]


def test_the_report_does_not_read_the_rows_of_a_step_that_did_not_finish(
    tmp_path, monkeypatch, capsys
):
    """A run killed inside step 8 of a longer series, or rows of a step with no row in
    steps.jsonl: the report judges the finished steps, not the half step."""
    run = make_run(tmp_path, monkeypatch)
    monkeypatch.setattr(growth, "grow", fake_grow([]))
    monkeypatch.setattr(compressed, "replay_all", fake_replay([]))
    compressed.run_steps(run, now=lambda: NOON)
    run.append_many("replay", step(20000, 8, 0, 55))  # rows of a step that has no row
    assert cli.cmd_report(argparse.Namespace(run_dir=str(run.dir))) == 0
    scales = json.loads(capsys.readouterr().out)
    doc = json.loads((run.dir / "final_report.json").read_text())["verdicts"]
    assert doc["judged_night"] == 7 and "8" not in doc["validity"]["20000"]["steps"]
    assert scales["LARGE-2"]["verdict"] == "WIN"


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


def test_a_build_that_did_not_fire_stops_the_run_and_a_stock_fault_does_not(tmp_path, monkeypatch):
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

    # a low stock rate is a result, not a fault: 0.70 passes (ruling 2026-11-11)
    low = make_run(tmp_path / "c", monkeypatch, sizes=(5000,))
    monkeypatch.setattr(compressed, "replay_all", fake_replay([], s2=42))
    res = compressed.run_steps(low, 2, now=lambda: NOON)
    assert res["valid_steps"] == [1, 2] and res["invalid"] == {}
    gate = read_jsonl(low.out / "steps.jsonl")[0]["gate"]["5000"]["arms"]["S2"]
    assert gate["delivery"] == 0.7 and gate["floor"] is None and gate["ok"] is True
    # a stock query that was not evaluated fails the step, and the run goes on
    other = make_run(tmp_path / "b", monkeypatch, sizes=(5000,))
    monkeypatch.setattr(Run, "fixture", lambda self: FakeFixture(n=61))
    monkeypatch.setattr(compressed, "replay_all", fake_replay([], s2=47))
    res = compressed.run_steps(other, 2, now=lambda: NOON)
    assert res["complete"] and res["stopped"] == "" and res["valid_steps"] == []
    assert (
        "S2 evaluated 180 queries, the step replays 183" in res["invalid"]["2"]["5000"]["causes"][0]
    )
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

    def pilot_replay(run_, sizes, shards, limit, **kw):
        assert kw["day"] == 0 and shards == 1 and limit == 10
        run_.append_many("replay", step(5000, 0, 10, 7, n=10))
        marker = {"day": 0, "build": "B1", "size": 5000, "shard": 0, "index_s": 1, "ready_s": 2}
        marker.update(backlog_chunks=948, drain_s=41.5)
        run_.append("replay-done", marker)
        return [dict(marker, memories=5000)]

    gate2 = {"ok": True, "public_connect_refused": True, "claude_processes": 0}
    monkeypatch.setattr(Run, "fixture", lambda self: FakeFixture(n=10, variants=("V1", "V3")))
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
    assert arms["B1"] == {"n": 10, "delivery": 1.0, "floor": 0.95, "invalid": 0, "ok": True}
    assert arms["S2"] == {
        "n": 10,
        "delivery": 0.7,
        "floor": None,
        "queries": 20,
        "expected": 20,
        "errors": 0,
        "invalid": 0,
        "ok": True,
    }  # stock at 0.7 is reported; its gate is that every query was evaluated
    assert shown["report_gate_would_pass"] is True
    assert shown["stores"]["sizes"]["5000"]["backlog_chunks"] == 948
    assert shown["stores"]["sizes"]["5000"]["drain_s"] == 41.5


def pilot_rows(pull_hits, prompt_hits, n=60):
    """The pilot rows of one scale: the sanity set on the recall tool and on the prompt."""
    rows = [dict(r, day=1) for r in step(5000, 1, pull_hits, 55, n=n)]
    rows += [dict(row("B1", 5000, 1, i, "V1", i < prompt_hits), channel="prompt") for i in range(n)]
    return rows


def pilot_gate(rows):
    prof = profiles.get("large-project-steps")
    floors = report.validity(rows, ("B1", "S2"), prof.params, [5000])
    gate = {size: {"ok": g["ok"], "arms": g["steps"].get("1", {})} for size, g in floors.items()}
    return cli.pilot_sanity(gate, prof.builds)


def test_the_pilot_gates_the_recall_tool_at_its_floor_and_not_the_prompt_channel():
    """A healthy store gives 60 of 60 on the recall tool and 52 of 60 on the prompt: the
    prompt hook holds rows back by its own rules. The pilot gate is the recall tool at
    0.95 or more; the prompt channel is shown and never stops the start."""
    healthy = pilot_rows(pull_hits=60, prompt_hits=52)
    assert pilot_gate(healthy) is True
    shown = cli.prompt_by_scale(healthy, [5000], ("B1",))
    assert shown == {"5000": {"B1": {"n": 60, "delivery": 0.867}}}  # under 0.95, no gate
    assert pilot_gate(pilot_rows(pull_hits=60, prompt_hits=0)) is True
    assert pilot_gate(pilot_rows(pull_hits=57, prompt_hits=52)) is True  # 0.95: the floor
    assert pilot_gate(pilot_rows(pull_hits=56, prompt_hits=60)) is False  # 0.933
    # an invalid row fails the gate at any delivery: it is not a miss
    latched = pilot_rows(pull_hits=60, prompt_hits=52)
    latched[-1].update(fail="store_hung", latched=True, hit=False)
    assert pilot_gate(latched) is False


class Clock:
    """A local clock that moves only when the run sleeps."""

    def __init__(self, at):
        self.at, self.slept = at, []

    def now(self):
        return self.at

    def sleep(self, s):
        self.slept.append(s)
        self.at += dt.timedelta(seconds=s)


CET = dt.timezone(dt.timedelta(hours=1))


def test_the_steps_pause_in_the_quiet_window_and_resume_after(tmp_path, monkeypatch):
    """Plan rule 4: no measured work between 00:45 and 03:00 local time. A series that
    reaches the window waits before its next step and before its next store."""
    run = make_run(tmp_path, monkeypatch, sizes=(5000,))
    clock = Clock(dt.datetime(2026, 11, 12, 0, 50, tzinfo=CET))
    began = []

    def replay(run_, sizes, shards, limit, **kw):
        kw["pause"]()  # replay_all calls it before each store starts
        began.append((kw["day"], clock.now().time()))
        return fake_replay([])(run_, sizes, shards, limit, **kw)

    monkeypatch.setattr(growth, "grow", fake_grow([]))
    monkeypatch.setattr(compressed, "replay_all", replay)
    res = compressed.run_steps(run, 2, now=clock.now, sleep=clock.sleep)
    assert res["complete"] and began[0] == (1, dt.time(3, 0))  # 2 h 10 min of pause
    assert sum(clock.slept) == 130 * 60 and max(clock.slept) <= compressed.QUIET_NAP_S
    pauses = read_jsonl(run.out / "pauses.jsonl")
    assert pauses == [
        {
            "ts": "2026-11-12T00:50:00+01:00",
            "before": "step 1",
            "until": "2026-11-12T03:00:00+01:00",
        }
    ]
    # a store that is due inside the window waits too, even in the middle of a step
    clock.at = dt.datetime(2026, 11, 12, 2, 59, 30, tzinfo=CET)
    assert compressed.wait_out_quiet(run, clock.now, clock.sleep, "x") == 30.0
    clock.at = dt.datetime(2026, 11, 12, 0, 44, 59, tzinfo=CET)  # just before: no wait
    assert compressed.wait_out_quiet(run, clock.now, clock.sleep, "x") == 0.0
    clock.at = dt.datetime(2026, 11, 12, 3, 0, tzinfo=CET)
    assert compressed.wait_out_quiet(run, clock.now, clock.sleep, "x") == 0.0
    assert "No measured work between 00:45 and 03:00 local time" in profiles.STEPS.prereg()


def test_replay_all_calls_the_pause_before_each_store(tmp_path, monkeypatch):
    from longtest import runner

    run = make_run(tmp_path, monkeypatch, sizes=(5000,))
    seen = []

    class Fx(FakeFixture):
        def shards(self, size):
            return [0, 1]

        def shard_dir(self, size, shard):
            return tmp_path

    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    monkeypatch.setattr(runner.replay, "run_stock", lambda *a: [])
    monkeypatch.setattr(runner, "replay_task", lambda run, fx, b, s, sh, *a: seen.append(sh))
    runner.replay_all(
        run, [5000], None, None, builds=("B1",), parallel=1, python="py", models=tmp_path,
        pause=lambda: seen.append("pause"),
    )  # fmt: skip
    assert seen == ["pause", 0, "pause", 1]
