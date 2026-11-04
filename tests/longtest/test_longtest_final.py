# SPDX-License-Identifier: AGPL-3.0-or-later
"""The final pieces of the long-run program: the frozen copy, the run clock, the
localhost unit, arm b and the pilot gates."""

from __future__ import annotations

import datetime as dt
import json
import shlex
import sys
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import cli, config, loop, realreplay, runner, sandbox

TZ = dt.timezone.utc


def run_folder(tmp_path):
    run = runner.Run(tmp_path / "run")
    for sub in ("out", "daily", "private"):
        (run.dir / sub).mkdir(parents=True)
    run.save_manifest(
        {
            "run_id": "r",
            "live": {"config_dir": str(tmp_path / "cfg"), "data_dir": str(tmp_path / "data")},
            "frozen": False,
        }
    )
    return run


def test_freeze_copies_the_program_and_refuses_a_change(tmp_path, monkeypatch):
    run = run_folder(tmp_path)
    tools = tmp_path / "tools"
    (tools / "longtest").mkdir(parents=True)
    (tools / "longtest" / "a.py").write_text("x = 1\n")
    (tools / "__pycache__").mkdir()
    monkeypatch.setattr(runner, "TOOLS", tools)
    copy = runner.freeze_program(run)
    assert (copy / "longtest" / "a.py").is_file() and not (copy / "__pycache__").exists()
    text = config.render_prereg()
    (run.dir / "PREREG.md").write_text(text)
    import hashlib

    doc = run.manifest
    doc.update(
        frozen=True,
        prereg_sha256=hashlib.sha256(text.encode()).hexdigest(),
        program_sha256=runner.program_hashes(copy),
    )
    run.save_manifest(doc)
    assert runner.verify_frozen(run, tools=copy) == []
    assert any("frozen copy" in p for p in runner.verify_frozen(run, tools=tools))
    (copy / "longtest" / "a.py").write_text("x = 2\n")
    assert runner.verify_frozen(run, tools=copy) == ["the frozen program changed"]
    (copy / "longtest" / "a.py").write_text("x = 1\n")
    (run.dir / "PREREG.md").write_text(text + "tuned\n")
    assert any(p.startswith("PREREG.md") for p in runner.verify_frozen(run, tools=copy))


def test_pilot_loop_files_move_away_and_stores_stay(tmp_path):
    run = run_folder(tmp_path)
    for name in ("heartbeat", "soak", "replay"):
        (run.out / f"{name}.jsonl").write_text("{}\n")
    (run.dir / "state.json").write_text("{}")
    (run.dir / "ALERT").write_text("failed")
    (run.dir / "daily" / "d.json").write_text("{}")
    moved = runner.archive_pilot_loop(run)
    assert {"out/heartbeat.jsonl", "out/soak.jsonl", "state.json", "ALERT", "daily"} <= set(moved)
    assert (run.out / "replay.jsonl").exists() and not (run.out / "heartbeat.jsonl").exists()
    assert (run.dir / "pilot-loop" / "state.json").exists() and (run.dir / "daily").is_dir()


def test_reset_volatile_clears_hook_state_only(tmp_path):
    (tmp_path / "guard-state").mkdir()
    (tmp_path / "guard-state" / "s").write_text("1")
    (tmp_path / "guard-log.jsonl").write_text("{}")
    (tmp_path / "home").mkdir()
    runner.reset_volatile(tmp_path)
    assert not (tmp_path / "guard-state").exists() and not (tmp_path / "guard-log.jsonl").exists()
    assert (tmp_path / "home").is_dir()


def test_net_isolation_is_read_from_the_device_list(tmp_path):
    (tmp_path / "lo").touch()
    assert sandbox.net_isolated(tmp_path)
    (tmp_path / "eth0").touch()
    assert not sandbox.net_isolated(tmp_path)
    assert not sandbox.net_isolated(tmp_path / "none")


def test_units_end_with_the_run_and_the_localnet_command_exists(tmp_path):
    units = sandbox.render_units(tmp_path, "/usr/bin/python3", Path("/opt/tools"))
    for text in units.values():
        assert f"ConditionPathExists=!{tmp_path}/finished" in text
        assert f"ConditionPathExists=!{tmp_path}/stop" in text
        assert "RestartSec=60" in text
    parser = cli.build_parser()
    for name in ("run", "localnet"):
        assert f"longtest.cli {name} " in "".join(units.values())
        parser.parse_args([name, "--run-dir", str(tmp_path)])


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_localnet_probe_writes_a_row_without_a_token(tmp_path, monkeypatch):
    run = run_folder(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "token").write_text("secret")
    asked = []
    monkeypatch.setattr(loop.live, "probe_health", lambda d: asked.append(d) or {"ok": True})
    net = loop.LocalNet(
        run, now=Clock(dt.datetime(2026, 11, 3, 10, tzinfo=TZ)), free_gb=lambda p: 500.0
    )
    net.sched.run_due({"health_probe": net.probe})
    row = json.loads((run.out / "health.jsonl").read_text())
    assert row["up"] and row["ok"] and asked == [data]
    assert "secret" not in (run.out / "health.jsonl").read_text()
    assert (run.dir / "state-localnet.json").exists() and not (run.dir / "state.json").exists()


def test_run_day_counts_from_the_start_of_the_soak_not_from_init(tmp_path):
    run = run_folder(tmp_path)
    clock = Clock(dt.datetime(2026, 11, 3, 14, tzinfo=TZ))
    lp = loop.Loop(run, 30, now=clock, sleep=lambda s: None)
    assert lp.day() == 1 and not lp.over()
    lp.sched.state["started"] = clock().isoformat()
    for hours, day, over in ((0, 1, False), (23, 1, False), (24, 2, False), (167, 7, False)):
        clock.t = dt.datetime(2026, 11, 3, 14, tzinfo=TZ) + dt.timedelta(hours=hours)
        assert (lp.day(), lp.over()) == (day, over)
    clock.t += dt.timedelta(hours=1)
    assert lp.over()


def test_each_24_hours_hold_exactly_one_night():
    from longtest import scheduler

    start = dt.datetime(2026, 11, 3, 7, 0, tzinfo=TZ)
    nights = 0
    for minute in range(7 * 24 * 60):
        t = start + dt.timedelta(minutes=minute)
        nights += 1 if (t.hour, t.minute) == (1, 0) else 0
    assert nights == 7
    assert scheduler.in_window(dt.datetime(2026, 11, 4, 1, 0, tzinfo=TZ))


def test_real_corpus_copy_skips_credentials_and_reads_only(tmp_path):
    cfg = tmp_path / "cfg" / "projects" / "-home-user" / "memory"
    cfg.mkdir(parents=True)
    (cfg / "note_a.md").write_text("a")
    (cfg / "note_b.md").write_text("b")
    (cfg / "token_dump.md").write_text("no")
    target = tmp_path / "t"
    counts = realreplay.copy_real_corpus(tmp_path / "cfg", target)
    assert counts == {"-home-user": 2}
    assert sorted(p.name for p in (target / "projects/-home-user/memory").iterdir()) == [
        "note_a.md",
        "note_b.md",
    ]
    assert realreplay.session_cwd(counts) == "/home/user"
    assert realreplay.session_cwd({}) == "/work/none"


def test_real_replay_rows_hold_counts_and_hashes_only(tmp_path):
    prompts = [{"group": "g1", "text": "secret words", "stratum": "home", "lines": ["- old"]}]

    def call(sid, cwd, text):
        return {"ok": True, "out": "OUT", "ms": 5.0}

    ask = lambda event, out: "intro\n- new line one\n- old\nmore"  # noqa: E731
    rows, shown = realreplay.replay_prompts(call, prompts, "/x", ask, "salt", "B1")
    assert rows[0]["rows"] == 2 and "secret" not in json.dumps(rows)
    assert shown == {"g1": ["- new line one", "- old"]}
    assert len(rows[0]["row_hashes"][0]) == 12
    out = tmp_path / "pairs.jsonl"
    assert realreplay.export_pool_additions(prompts, shown, out) == 1
    assert "new line one" in out.read_text() and (out.stat().st_mode & 0o777) == 0o600


def test_real_replay_summary_pairs_the_builds():
    rows = {
        "B0": [{"rows": 3, "chars": 300}, {"rows": 0, "chars": 0}],
        "B1": [{"rows": 1, "chars": 90}, {"rows": 0, "chars": 0}],
    }
    got = realreplay.summary(rows)
    assert got["char_ratio_b1_over_b0"] == 0.3 and got["B0"]["with_rows"] == 1


def test_claims_gate_accounts_for_every_claim():
    rows = [
        {"build": "B0", "claims": ["C1"], "hit": False},
        {"build": "B1", "claims": ["C1"], "hit": True},
        {"build": "B1", "claims": ["C7"], "hit": False},
    ]
    got = cli.claims_gate(rows, {})
    assert got["ok"] and len(got["claims"]) == 24
    assert got["claims"]["C1"]["state"] == "fired"
    assert got["claims"]["C7"]["state"].startswith("not fired")
    assert got["claims"]["C24"]["state"] == "other arm or pending"


def test_a_run_does_not_start_without_a_frozen_copy_and_a_passed_pilot(tmp_path):
    run = run_folder(tmp_path)
    with pytest.raises(SystemExit, match="may not start"):
        cli.cmd_run(type("A", (), {"run_dir": str(run.dir), "soak_size": 30, "max_seconds": 1})())


def test_index_wait_comes_from_the_measured_build_time():
    from longtest import buildrunner

    assert buildrunner.index_wait_s(30) == 900.0  # the floor holds for small scales
    assert buildrunner.index_wait_s(5000) == 5000.0
    # measured: 20,629 notes took about 10,900 s with two stores on a host at load 65
    assert buildrunner.index_wait_s(20000) >= 1.8 * 0.53 * 20629 * 0.99
    assert buildrunner.index_wait_s(20000, floor=30000) == 30000.0


def test_soak_store_is_seeded_from_the_kept_replay_store_once(tmp_path, monkeypatch):
    run = run_folder(tmp_path)

    class Fx:
        def shards(self, size):
            return [0]

    monkeypatch.setattr(runner.Run, "fixture", lambda self: Fx())
    source = runner.shard_root(run, "B1", 20000, 0)
    data = source / "home/.claude/plugins/data/noblivion-noblivion"
    data.mkdir(parents=True)
    (data / "noblivion.db").write_text("db")
    (data / "store.json").write_text("{}")
    (source / "guard-state").mkdir()
    soak = run.dir / "stores" / "soak" / "shard-00"
    assert runner.seed_soak_store(run, soak, 20000)
    copy = soak / "home/.claude/plugins/data/noblivion-noblivion"
    assert (copy / "noblivion.db").read_text() == "db" and not (copy / "store.json").exists()
    assert (data / "noblivion.db").exists()  # the replay store stays
    assert not runner.seed_soak_store(run, soak, 20000)  # already there: left alone
    assert not runner.seed_soak_store(run, run.dir / "stores" / "x", 5000)  # no source


def test_a_store_that_is_not_ready_is_stopped_and_not_left_running(tmp_path, monkeypatch):
    run = run_folder(tmp_path)
    stopped = []

    class Fx:
        pass

    class H:
        def __init__(self, *a, **kw):
            self.root = tmp_path / "root"
            self.timing = {}

        def prepare(self, *a):
            pass

        def start(self):
            raise RuntimeError("store not ready after 900 s")

        def stop(self):
            stopped.append(1)

    monkeypatch.setattr(runner, "Harness", H)
    with pytest.raises(RuntimeError, match="not ready"):
        runner.replay_task(run, Fx(), "B1", 20000, 0, "py", None, None, None, 0, None, False, 900.0)
    assert stopped == [1]


def test_fillers_go_into_the_project_of_the_session(tmp_path):
    fill = tmp_path / "f"
    fill.mkdir()
    for n in ("a.md", "b.md"):
        (fill / n).write_text(n)
    target = tmp_path / "t"
    stale = target / "projects" / realreplay.DISTRACTOR_PROJECT / "memory"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("x")
    got = realreplay.add_distractors(sorted(fill.glob("*.md")), target, "-home-user")
    assert got == 2 and not stale.exists()
    assert sorted(p.name for p in (target / "projects/-home-user/memory").iterdir()) == [
        "a.md",
        "b.md",
    ]
    assert realreplay.session_project({"-a": 1, "-b": 3}) == "-b"


def test_one_failed_replay_task_does_not_hide_the_others(tmp_path, monkeypatch):
    run = run_folder(tmp_path)

    class Fx:
        def shards(self, size):
            return [0, 1]

        def shard_dir(self, size, shard):
            return tmp_path

    monkeypatch.setattr(runner.Run, "fixture", lambda self: Fx())
    monkeypatch.setattr(runner.replay, "run_stock", lambda *a: [])
    ran = []

    def task(run, fx, build, size, shard, *rest):
        ran.append((build, shard))
        if (build, shard) in (("B0", 1), ("B1", 0)):
            raise RuntimeError(f"store exited -6: {build}")
        return {}

    monkeypatch.setattr(runner, "replay_task", task)
    with pytest.raises(RuntimeError) as err:
        runner.replay_all(run, [30], None, None, python="py", models=tmp_path)
    assert sorted(ran) == [("B0", 0), ("B0", 1), ("B1", 0), ("B1", 1)]
    text = str(err.value)
    assert text.startswith("2 of 4 replay tasks failed (B1 size 30 shard 0, B0 size 30 shard 1)")
    assert "store exited -6: B0" in text and "store exited -6: B1" in text


def test_a_failed_replay_still_runs_arm_b_and_writes_the_hashes(tmp_path, monkeypatch):
    run = run_folder(tmp_path)
    run.save_manifest({**run.manifest, "sizes": [30, 200], "heldout": str(tmp_path / "h")})
    lp = loop.Loop(run, 30, now=Clock(dt.datetime(2026, 11, 3, 2, tzinfo=TZ)))
    arm_b = []

    def replay_all(*a, **kw):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(loop, "replay_all", replay_all)
    monkeypatch.setattr(loop, "real_replay", lambda *a, **kw: arm_b.append(1))
    with pytest.raises(RuntimeError, match="^replay failed$"):
        lp.replay_nightly()
    assert arm_b == [1]
    rows = [json.loads(line) for line in (run.out / "determinism.jsonl").read_text().splitlines()]
    assert [r["scale"] for r in rows] == [30, 200]

    def real_replay(*a, **kw):
        raise ValueError("arm b failed")

    monkeypatch.setattr(loop, "real_replay", real_replay)
    with pytest.raises(RuntimeError, match="replay failed; ValueError: arm b failed"):
        lp.replay_nightly()
    assert len((run.out / "determinism.jsonl").read_text().splitlines()) == 4


def say_script(tmp_path):
    script = tmp_path / "say.py"
    script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[-1])\n")
    return f"{shlex.quote(sys.executable)} {script} {tmp_path / 'said'}"


def test_notify_command_gets_the_alert_and_a_failure_is_only_logged(tmp_path, capsys):
    notify = loop.command_notifier(say_script(tmp_path))
    notify("long-run test: jobs_ran")
    assert (tmp_path / "said").read_text() == "long-run test: jobs_ran"
    py = shlex.quote(sys.executable)
    loop.command_notifier(f"{py} -c 'raise SystemExit(4)'")("x")
    loop.command_notifier(f"{py} -c 'import time; time.sleep(5)'", timeout_s=0.2)("x")
    err = capsys.readouterr().err
    assert "CalledProcessError" in err and "TimeoutExpired" in err
    with pytest.raises(SystemExit):
        loop.command_notifier("/no/such/program")


def test_run_passes_the_notify_command_to_the_loop(tmp_path, monkeypatch):
    run = run_folder(tmp_path)
    (run.dir / "pilot.json").write_text(json.dumps({"all_gates": True}))
    monkeypatch.setattr(cli, "verify_frozen", lambda run: [])
    got = {}

    class FakeLoop:
        def __init__(self, run, soak_size, notify=None):
            got["notify"] = notify

        def run_forever(self, max_seconds):
            pass

    monkeypatch.setattr(loop, "Loop", FakeLoop)
    cli.main(["run", "--run-dir", str(run.dir), "--notify-cmd", say_script(tmp_path)])
    got["notify"]("ALERT")
    assert (tmp_path / "said").read_text() == "ALERT"
    cli.main(["run", "--run-dir", str(run.dir)])
    assert got["notify"] is None


def test_units_command_puts_the_notify_command_in_the_core_unit(tmp_path, capsys):
    cli.main(["units", "--run-dir", str(tmp_path), "--notify-cmd", "/usr/bin/logger -t nob"])
    core = (tmp_path / "units" / "nob-longtest.service").read_text()
    assert '--notify-cmd "/usr/bin/logger -t nob"' in core
