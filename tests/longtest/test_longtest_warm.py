# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hook latch and the warm-up, with a stub build that latches as the product does:
a store call that is too slow makes the hook write ``store.hung`` and answer empty for
30 seconds."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import buildrunner, replay, runner, warm, worker
from longtest.runner import Run
from longtest.util import read_jsonl

ROW = "- [id 1] Zeta rule: the answer is zeta-42"

STORE = '''
import os, time
from pathlib import Path

def search(env):
    """"" when the store answers, else the fail reason. ``cold``: the next call is too
    slow. ``slow_at``: the call with that number is too slow. A slow call sets the latch.
    ``gave_up_at``: the hook gives up on the call with that number and sets no latch."""
    data = Path(env["NOBLIVION_DATA_DIR"])
    stamp = data / "store.hung"
    try:
        if 0 < float(stamp.read_text().split()[0]) - time.time() <= 31:
            return "store_hung"
    except (OSError, ValueError, IndexError):
        pass
    calls = data / "calls"
    n = int(calls.read_text()) + 1 if calls.exists() else 1
    calls.write_text(str(n))
    slow = data / "slow_at"
    if (data / "cold").exists() or (slow.exists() and int(slow.read_text()) == n):
        (data / "cold").unlink(missing_ok=True)
        stamp.write_text("%.3f 2.000 store\\n" % (time.time() + 30))
        return "timeout"
    gave_up = data / "gave_up_at"
    if gave_up.exists() and int(gave_up.read_text()) == n:
        return "timeout"
    return ""
'''

HOOK = """
import json, os, stub_store

def main(stdin, stdout, env):
    event = json.loads(stdin.read())
    reason = stub_store.search(env)
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "recall.log"), "a") as fh:
        fh.write("2026-01-01T00:00:00+00:00 event=UserPromptSubmit session=%s hits=1 chars=9 "
                 "ms=1 %s\\n" % (event["session_id"], "fail:" + reason if reason else "ok"))
    if not reason:
        stdout.write(ROW)
"""

TOOL = """
import stub_store

def noblivion_recall(query, k=5, environ=None):
    reason = stub_store.search(environ)
    if reason:
        text = "noblivion_recall: memory store unavailable (%s)" % reason
        return {"content": [{"type": "text", "text": text}], "isError": True}
    return {"content": [{"type": "text", "text": ROW}]}
"""


def make_build(folder: Path) -> Path:
    (folder / "hooks").mkdir(parents=True)
    (folder / "mcp").mkdir()
    (folder / "hooks" / "stub_store.py").write_text(STORE)
    (folder / "hooks" / "recall_hook.py").write_text(f"ROW = {ROW!r}\n" + HOOK)
    (folder / "hooks" / "guard_hook.py").write_text("def main(stdin, stdout, env):\n    pass\n")
    (folder / "mcp" / "recall_mcp.py").write_text(f"ROW = {ROW!r}\n" + TOOL)
    return folder


class Fx:
    """Five V1 queries of one lesson: each is one pull row and one prompt row."""

    lessons = {"L1": {"answer": "the answer is zeta-42", "title": "Zeta rule"}}

    def shards(self, size):
        return [0]

    def scale(self, size):
        return {"lessons": {"L1": {"shard": 0, "session_project": "a"}}}

    def queries_for(self, size, variants=None, shard=None):
        return [
            {"id": f"L1-V1-{i}", "variant": "V1", "lesson": "L1", "claims": ["C1"], "prompt": "z"}
            for i in range(5)
        ]

    def prompt_of(self, q):
        return q["prompt"]

    def lesson_of(self, q):
        return q["lesson"]

    def cwd_of(self, size, q):
        return "/work/a"


class StubHarness(buildrunner.Harness):
    """A harness with no store process: every start is a cold start."""

    def prepare(self, fx, size, shard):
        self.config_dir.joinpath("projects", "-work-a", "memory").mkdir(parents=True, exist_ok=True)
        return []

    def start(self, index=True):
        (self.data / "cold").write_text("")
        (self.data / "calls").unlink(missing_ok=True)
        self.store = SimpleNamespace(proc=None, stop=lambda: None)
        self.timing.update(index_s=0.0, ready_s=0.0, backlog_chunks=948, drain_s=41.5)
        return {"memories": 1}


def harness(tmp_path: Path) -> StubHarness:
    h = StubHarness("B1", make_build(tmp_path / "b1"), tmp_path / "root", sys.executable, None)
    h.prepare(None, 30, 0)
    return h


def make_run(tmp_path: Path, monkeypatch) -> Run:
    run_dir = tmp_path / "run"
    (run_dir / "out").mkdir(parents=True)
    make_build(run_dir / "builds" / "b1")
    live = {"config_dir": str(tmp_path / "live"), "data_dir": str(tmp_path / "live" / "data")}
    (run_dir / "run.json").write_text(json.dumps({"run_id": "t", "sizes": [30], "live": live}))
    monkeypatch.setattr(runner, "Harness", StubHarness)
    monkeypatch.setattr(Run, "fixture", lambda self: Fx())
    return Run(run_dir)


# -- the replay shard --------------------------------------------------------------------


def test_a_cold_store_does_not_latch_the_shard(tmp_path):
    """The fault of the 7-day run: a call is too slow, the hook latches, and every row of
    the shard is empty. With the warm-up the slow call is not a measured one, the stamp
    is removed, and every row reaches the store."""
    h = harness(tmp_path)
    h.start()
    rows = replay.run_shard(Fx(), 30, 0, h, "B1")
    lesson = [r for r in rows if r["variant"] == "V1"]
    assert len(lesson) == 10 and all(r["hit"] for r in lesson)  # 5 pull rows, 5 prompt rows
    assert all(r["fail"] == "" and r["latched"] is False for r in rows)  # the benign rows too
    assert h.timing["warm_tries"] == 2  # the round with the cold call, then a good one
    assert not (h.data / warm.LATCH_FILE).exists()


def test_a_latched_row_voids_the_shard(tmp_path):
    h = harness(tmp_path)
    h.start()
    (h.data / "slow_at").write_text("7")  # warm-up: calls 1 to 4; then the third row is slow
    with pytest.raises(warm.LatchedShard) as err:
        replay.run_shard(Fx(), 30, 0, h, "B1")
    assert "8 of 30 rows are invalid" in str(err.value)  # the slow row and the 7 after it
    assert "'store_hung': 7" in str(err.value) and "'timeout': 1" in str(err.value)


def test_a_row_whose_hook_gave_up_voids_the_shard_with_no_latch(tmp_path):
    """A hook that gave up on the store answers empty as a miss does. The row measured
    the wait for the store, so it is invalid, with or without the stamp."""
    h = harness(tmp_path)
    h.start()
    (h.data / "gave_up_at").write_text("7")
    with pytest.raises(warm.LatchedShard) as err:
        replay.run_shard(Fx(), 30, 0, h, "B1")
    assert "1 of 30 rows are invalid" in str(err.value)
    assert "fail reasons {'timeout': 1}" in str(err.value)
    assert not (h.data / warm.LATCH_FILE).exists()


def test_a_latched_shard_is_not_marked_done_and_its_rows_are_not_counted(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    args = (run, Fx(), "B1", 30, 0, sys.executable, None, None, None, 1, None, True, 900.0)
    slow = runner.shard_root(run, "B1", 30, 0) / "home/.claude/plugins/data" / "slow_at"
    data = next(p for p in [slow.parent / buildrunner.DATA_NAME] if p.mkdir(parents=True) is None)
    (data / "slow_at").write_text("7")
    with pytest.raises(warm.LatchedShard):
        runner.replay_task(*args)
    assert run.done(1) == set() and read_jsonl(run.out / "replay.jsonl") == []
    void = read_jsonl(run.out / "replay-void.jsonl")
    assert len(void) == 1 and void[0]["build"] == "B1" and "hook latch" in void[0]["why"]
    (data / "slow_at").unlink()
    (data / warm.LATCH_FILE).write_text("%.3f 2.000 store\n" % (time.time() + 30))  # left over
    info = runner.replay_task(*args)  # the task runs again, as the scheduler retries it
    assert info["rows"] == 30 and info["warm_tries"] == 2
    assert run.done(1) == {("B1", 30, 0)}
    rows = read_jsonl(run.out / "replay.jsonl")
    assert len(rows) == 30 and all(r["fail"] == "" and not r["latched"] for r in rows)
    marker = read_jsonl(run.out / "replay-done.jsonl")[0]
    assert marker["warm_tries"] == 2 and marker["fails"] == {}
    # the marker keeps what the start saw: the embedding backlog and its drain time
    assert marker["backlog_chunks"] == 948 and marker["drain_s"] == 41.5
    assert info["backlog_chunks"] == 948 and info["drain_s"] == 41.5


def test_a_row_whose_hook_gave_up_is_not_marked_done_either(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    args = (run, Fx(), "B1", 30, 0, sys.executable, None, None, None, 1, None, True, 900.0)
    data = runner.shard_root(run, "B1", 30, 0) / "home/.claude/plugins/data" / buildrunner.DATA_NAME
    data.mkdir(parents=True)
    (data / "gave_up_at").write_text("9")
    with pytest.raises(warm.LatchedShard):
        runner.replay_task(*args)
    assert run.done(1) == set() and read_jsonl(run.out / "replay.jsonl") == []
    assert "'timeout': 1" in read_jsonl(run.out / "replay-void.jsonl")[0]["why"]


# -- the warm-up -------------------------------------------------------------------------


class SlowWorker:
    """Answers too late ``slow`` times and sets the latch each time, as the hook does."""

    def __init__(self, data: Path, slow: int, fail: str = "timeout") -> None:
        self.data, self.slow, self.fail, self.calls = data, slow, fail, []

    def call(self, op, **args):
        assert not (self.data / warm.LATCH_FILE).exists()  # each try reaches the store
        self.calls.append((op, args["sid"]))
        if len(self.calls) <= self.slow:
            (self.data / warm.LATCH_FILE).write_text("1 2 s\n")
            return {"ok": True, "out": "", "ms": 2000.4, "fail": self.fail, "latched": False}
        return {"ok": True, "out": ROW, "ms": 310.0, "fail": "", "latched": False}


def test_warm_up_waits_for_a_round_inside_the_budget_and_removes_the_stamp(tmp_path):
    w, naps = SlowWorker(tmp_path, slow=3), []
    got = warm.warm_up(w, tmp_path, "/work/a", sleep=naps.append)
    assert got["warm_tries"] == 3 and naps == [1.0, 1.0]
    assert got["warm_ms"] == 310.0 and got["warm_prompt_ms"] == 310.0
    assert [op for op, _ in w.calls] == ["pull", "prompt"] * 3  # one of each per round
    assert len({sid for _, sid in w.calls}) == 6  # no warm-up call shares a session
    assert not (tmp_path / warm.LATCH_FILE).exists()


def test_a_store_is_not_warm_while_its_prompt_hook_is_slow(tmp_path):
    """The recall tool alone is not the proof: both channels are measured, so both
    must answer inside the budget in the same round."""

    class SlowPrompt(SlowWorker):
        def call(self, op, **args):
            self.calls.append((op, args["sid"]))
            late = op == "prompt" and len(self.calls) <= 4
            return {"ok": True, "out": ROW, "ms": 2100.0 if late else 40.0, "fail": ""}

    w = SlowPrompt(tmp_path, 0)
    got = warm.warm_up(w, tmp_path, "/work/a", sleep=lambda s: None)
    assert got["warm_tries"] == 3 and got["warm_prompt_ms"] == 40.0


def test_warm_up_is_bounded_and_says_why_it_gave_up(tmp_path):
    w, clock = SlowWorker(tmp_path, slow=10**6), {"t": 0.0}
    with pytest.raises(warm.WarmupError) as err:
        warm.warm_up(
            w,
            tmp_path,
            "/work/a",
            sleep=lambda s: clock.update(t=clock["t"] + 10),
            clock=lambda: clock["t"],
        )
    assert warm.WARM_BOUND_S == 60.0 and warm.HOOK_BUDGET_S == 2.0
    assert len(w.calls) == 14 and "in 7 rounds over 60 s" in str(err.value)
    assert "'pull': \"2000.4 ms, fail 'timeout'\"" in str(err.value)
    assert "'prompt': \"2000.4 ms, fail 'timeout'\"" in str(err.value)


def test_a_call_over_the_budget_is_not_warm_even_with_no_fail_reason(tmp_path):
    class Late(SlowWorker):
        def call(self, op, **args):
            self.calls.append(op)
            ms = 2500.0 if len(self.calls) == 1 else 40.0
            return {"ok": True, "out": ROW, "ms": ms, "fail": ""}

    assert warm.warm_up(Late(tmp_path, 0), tmp_path, "/w", sleep=lambda s: None)["warm_tries"] == 2


def test_invalid_rows_are_the_latched_ones_and_the_ones_whose_hook_gave_up():
    rows = [
        {"qid": "a", "fail": "", "latched": False},
        {"qid": "b", "fail": "timeout", "latched": False},
        {"qid": "c", "fail": "store_hung", "latched": True},
        {"qid": "d", "fail": "", "latched": True},  # the stamp was there when it began
        {"qid": "e", "fail": "store_down", "latched": False},  # a fault, but not the wait
        {"qid": "f"},  # a row of a program that did not keep the reason
    ]
    assert [r["qid"] for r in warm.invalid_rows(rows)] == ["b", "c", "d"]
    assert warm.INVALID_FAILS == ("store_hung", "timeout")


# -- the worker --------------------------------------------------------------------------


def test_worker_reads_the_stamp_as_the_build_does(tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    stamp = tmp_path / "store.hung"
    assert worker.latch_in_force() is False  # no stamp
    stamp.write_text("%.3f 2.000 store\n" % (time.time() + 30))
    assert worker.latch_in_force() is True
    stamp.write_text("%.3f 2.000 store\n" % (time.time() - 1))
    assert worker.latch_in_force() is False  # it ended
    stamp.write_text("%.3f 2.000 store\n" % (time.time() + 3600))
    assert worker.latch_in_force() is False  # too far ahead: the clock went back
    stamp.write_text("not a time\n")
    assert worker.latch_in_force() is False


def test_worker_takes_the_fail_reason_from_the_lines_of_its_own_call(tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path))
    log = tmp_path / "recall.log"
    log.write_text("t event=UserPromptSubmit session=s0 hits=0 chars=0 ms=0 fail:store_down\n")
    mark = worker.log_mark()
    assert worker.hook_fail(mark) == ""  # the line of an earlier call
    with open(log, "a") as fh:
        fh.write("t event=UserPromptSubmit session=s1 hits=2 chars=90 ms=12 ok\n")
        fh.write("t event=UserPromptSubmit session=s1 hits=0 chars=0 ms=0 fail:store_hung\n")
    assert worker.hook_fail(mark) == "store_hung"
    log.write_text("t event=X session=s2 hits=0 chars=0 ms=2001 fail:timeout\n")  # rotated
    assert worker.hook_fail(mark + 500) == "timeout"
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "none"))
    assert worker.log_mark() == 0 and worker.hook_fail(0) == ""


# -- the latency benchmark ---------------------------------------------------------------


def test_latency_row_of_a_kept_store_is_warmed_and_says_it_is_valid(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch)
    runner.shard_root(run, "B1", 30, 0).joinpath("home").mkdir(parents=True)  # a kept store
    row = runner.bench_kept_store(run, "B1", 30, budget_s=20)
    assert row["warm_tries"] == 2 and row["n"] == 100
    assert row["latched"] == 0 and row["fails"] == {} and row["valid"] is True


def test_latency_calls_under_the_latch_make_the_row_invalid():
    class W:
        n = 0

        def call(self, op, **args):
            self.n += 1
            if self.n == 1:
                return {"ok": True, "out": "", "ms": 2000.8, "fail": "timeout", "latched": False}
            return {"ok": True, "out": "", "ms": 0.5, "fail": "store_hung", "latched": True}

    call = warm.HookCalls(W(), "/work/a", "bench")
    assert [call("p") for _ in range(4)] == [2000.8, 0.5, 0.5, 0.5]
    got = call.validity()
    assert got == {"latched": 3, "fails": {"store_hung": 3, "timeout": 1}, "valid": False}


# -- one kept store at a time ------------------------------------------------------------


def test_kept_stores_start_one_at_a_time_whatever_the_parallel_number(tmp_path, monkeypatch):
    """Two cold stores of thousands of notes that start together share the CPU quota:
    the replay of the 7-day run started two at once. A kept store now runs alone; the
    small scales still run side by side."""
    import inspect
    import threading

    from longtest import loop as loop_mod

    run = make_run(tmp_path, monkeypatch)
    lock, active, worst = threading.Lock(), {"kept": 0, "small": 0}, {"kept": 0, "small": 0}
    together = threading.Barrier(2, timeout=30)

    class Counting(StubHarness):
        def kind(self):
            return "kept" if "large" in str(self.root) else "small"

        def start(self, index=True):
            with lock:
                active[self.kind()] += 1
                worst[self.kind()] = max(worst[self.kind()], active[self.kind()])
            if self.kind() == "small":
                together.wait()  # the two small stores are up at the same time
            else:
                time.sleep(0.05)  # room for a second kept store to start, if one could
            return super().start(index)

        def stop(self):
            if self.store is not None:
                with lock:
                    active[self.kind()] -= 1
            self.store = None

    class Shards(Fx):
        def shards(self, size):
            return [0, 1]

        def shard_dir(self, size, shard):
            return tmp_path / "shard"

    monkeypatch.setattr(runner, "Harness", Counting)
    monkeypatch.setattr(Run, "fixture", lambda self: Shards())
    monkeypatch.setattr(replay, "run_stock", lambda *a, **k: [])
    monkeypatch.setattr(replay, "run_shard", lambda *a, **k: [])
    infos = runner.replay_all(
        run, [30, 5000, 20000], None, None, builds=("B1",), parallel=4, python=sys.executable
    )
    assert len(infos) == 6 and worst["kept"] == 1 and worst["small"] == 2
    nightly = inspect.signature(loop_mod.Loop).parameters["replay_parallel"]
    assert nightly.default == 1  # the nightly job asks for one task at a time as well
