# SPDX-License-Identifier: AGPL-3.0-or-later
"""The back-off for a hung store (NOBLIVION-69).

A store that accepts a connection but does not answer cost every prompt and
every subagent start the full store budget. After a call times out, the hooks
write a "down until" stamp in the data dir, and every hook skips the store
until it ends. A refused connection or an error answer writes no stamp, and a
new store (a newer ``store.json``) ends the stamp at once.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import FakeStore, hook_env, write_store_files

# A short budget keeps the tests fast: the listener proof gets 0.6 s. A call
# skipped by the back-off sends nothing, so it ends far below that.
FAST = {"NOBLIVION_RECALL_TIMEOUT_S": "1.2"}
SKIP_MAX_S = 0.4


@pytest.fixture
def rh():
    return load_hook("recall_hook", "hooktest_store_backoff")


@pytest.fixture
def sc():
    return load_hook("store_client", "hooktest_store_backoff_client")


@pytest.fixture
def silent_store(tmp_path):
    """``store.json`` names a listener that accepts and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    write_store_files(tmp_path / "data", srv.getsockname()[1])
    yield srv
    srv.close()


@pytest.fixture
def hanging_search(tmp_path):
    """A store that passes the listener proof and never answers a search."""
    release = threading.Event()
    fake = FakeStore(tmp_path / "data")

    def _hang(_path, _query):
        release.wait(10)
        return {}

    fake.route("/api/", _hang)
    yield fake
    release.set()
    fake.stop()


def _prompt() -> str:
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s-demo-1", "prompt": "run the tests"}
    )


def _timed_run(rh, env: dict) -> float:
    t0 = time.monotonic()
    assert rh.main(stdin=io.StringIO(_prompt()), stdout=io.StringIO(), environ=dict(env)) == 0
    return time.monotonic() - t0


def _statuses(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [ln.rsplit(" ", 1)[-1] for ln in lines]


def _reason(call) -> str:
    with pytest.raises(Exception) as err:
        call()
    return getattr(err.value, "reason", repr(err.value))


def test_a_silent_store_is_waited_for_once_then_skipped(rh, tmp_path, silent_store):
    env = hook_env(tmp_path, **FAST)
    first = _timed_run(rh, env)
    second = _timed_run(rh, env)
    assert _statuses(tmp_path / "cache" / "recall.log") == ["fail:store_down", "fail:store_hung"]
    assert first >= 0.5
    assert second < SKIP_MAX_S


def test_a_search_that_times_out_is_not_sent_again_during_the_back_off(
    rh, tmp_path, hanging_search
):
    env = hook_env(tmp_path, **FAST)
    assert _reason(lambda: rh.recall_index("run the tests", 5, env)) == "timeout"
    t0 = time.monotonic()
    assert _reason(lambda: rh.recall_index("run the tests", 5, env)) == "store_hung"
    assert time.monotonic() - t0 < SKIP_MAX_S
    # The second call sent nothing: one proof and one search in all.
    assert [p for p, _q, _h in hanging_search.requests] == ["/health", "/api/memories/index"]


def test_the_subagent_hook_skips_the_store_after_a_prompt_timed_out(rh, tmp_path, silent_store):
    sub = load_hook("subagent_rules_hook", "hooktest_store_backoff_subagent")
    env = hook_env(tmp_path, HOME=str(tmp_path / "home"), **FAST)
    _timed_run(rh, env)
    start = {
        "hook_event_name": "SubagentStart",
        "session_id": "s-demo-1",
        "agent_id": "a-demo-2",
        "cwd": "/tmp",
        "prompt": "Fix the failing test and commit the result.",
    }
    t0 = time.monotonic()
    out = io.StringIO()
    assert sub.main(stdin=io.StringIO(json.dumps(start)), stdout=out, environ=dict(env)) == 0
    assert time.monotonic() - t0 < SKIP_MAX_S
    assert out.getvalue() == ""
    assert _statuses(tmp_path / "cache" / "subagent" / "recall.log") == ["fail:store_hung"]


def test_no_back_off_after_a_refused_connection_or_an_error_answer(rh, sc, tmp_path):
    env = hook_env(tmp_path, **FAST)
    stamp = tmp_path / "data" / sc.HUNG_STAMP_FILE
    fake = FakeStore(tmp_path / "data")
    fake.route("/api/", {"detail": "boom"}, status=500)
    assert _reason(lambda: rh.recall_index("run the tests", 5, env)) == "http_500"
    assert not stamp.exists()
    fake.stop()  # store.json now names a port nobody listens on
    reasons = [_reason(lambda: rh.recall_index("run the tests", 5, env)) for _ in range(3)]
    # The kept proof sends the first call to the closed port; then it is proved again.
    assert reasons == ["unreachable:ConnectionRefusedError", "store_down", "store_down"]
    assert not stamp.exists()


def test_the_stamp_ends_after_the_back_off_and_with_a_new_store(sc, tmp_path):
    env = hook_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    write_store_files(data, 40123)
    now = 1_000_000.0
    sc.mark_hung(env, clock=lambda: now)
    assert sc.hung(env, clock=lambda: now)
    assert sc.hung(env, clock=lambda: now + sc.HUNG_BACKOFF_S - 1)
    assert not sc.hung(env, clock=lambda: now + sc.HUNG_BACKOFF_S + 1)
    # A stamp that ends too far ahead (a clock that jumped back) does not count.
    assert not sc.hung(env, clock=lambda: now - sc.HUNG_BACKOFF_S)
    # A store.json newer than the stamp names a new store: prove it again.
    later = (data / sc.HUNG_STAMP_FILE).stat().st_mtime + 5
    os.utime(data / "store.json", (later, later))
    assert not sc.hung(env, clock=lambda: now + 1)
    # A broken stamp does not count.
    (data / sc.HUNG_STAMP_FILE).write_text("not a time\n", encoding="ascii")
    assert not sc.hung(env, clock=lambda: now + 1)


def test_a_call_with_a_short_budget_does_not_stop_a_call_with_a_longer_one(sc, tmp_path):
    env = hook_env(tmp_path)
    write_store_files(Path(env["NOBLIVION_DATA_DIR"]), 40123)
    now = 1_000_000.0
    sc.mark_hung(env, 0.8, clock=lambda: now)  # the error hook: a store that needs 1 s
    assert sc.hung(env, 0.8, clock=lambda: now)
    assert sc.hung(env, 0.5, clock=lambda: now)
    assert not sc.hung(env, 2.0, clock=lambda: now)  # the prompt hook may still be answered
    sc.mark_hung(env, 2.0, clock=lambda: now)
    assert sc.hung(env, 2.0, clock=lambda: now)
    assert sc.hung(env, 0.8, clock=lambda: now)
    assert not sc.hung(env, 4.5, clock=lambda: now)


def test_a_slow_search_of_a_short_budget_leaves_the_prompt_hook_its_store(rh, sc, tmp_path):
    # The error recall hook asks with 0.8 s. A store that needs 1.0 s for one
    # search must not be put on the back-off list for the prompt hook (2.0 s).
    fake = FakeStore(tmp_path / "data")

    def _slow(_path, _query):
        time.sleep(1.0)
        return {"results": [], "scores": [], "namespace": "claude_code"}

    fake.route("/api/", _slow)
    try:
        env = hook_env(tmp_path)
        short = _reason(lambda: rh.store_get(lambda base: base + "/api/x", env, 0.8))
        assert short == "timeout"
        stamp = tmp_path / "data" / sc.HUNG_STAMP_FILE
        # The slow call is on record, with its budget and the store it went to.
        assert stamp.read_text(encoding="ascii").split()[1:] == ["0.800", sc.store_identity(env)]
        assert rh.store_get(lambda base: base + "/api/x", env, 3.0)["namespace"] == "claude_code"
    finally:
        fake.stop()


def test_a_stamp_for_another_store_does_not_count(sc, tmp_path):
    # A call that began on an old store may time out after a new store started
    # and wrote ``store.json``: its stamp is newer than ``store.json``, but it
    # names the old store.
    env = hook_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    write_store_files(data, 40123)
    old = sc.store_identity(env)
    assert old.endswith(":40123")
    now = 1_000_000.0
    write_store_files(data, 40124)  # the new store
    sc.mark_hung(env, 2.0, old, clock=lambda: now)
    assert not sc.hung(env, 2.0, clock=lambda: now)
    sc.mark_hung(env, 2.0, sc.store_identity(env), clock=lambda: now)
    later = (data / sc.HUNG_STAMP_FILE).stat().st_mtime
    os.utime(data / "store.json", (later - 5, later - 5))
    assert sc.hung(env, 2.0, clock=lambda: now)


def test_a_stamp_for_a_store_with_another_pid_on_the_same_port_does_not_count(sc, tmp_path):
    # A new store can listen on the port of the old one: the pid tells them
    # apart.
    env = hook_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    write_store_files(data, 40123, pid=4001)
    old = sc.store_identity(env)
    now = 1_000_000.0
    write_store_files(data, 40123, pid=4002)  # the new store, the same port
    sc.mark_hung(env, 2.0, old, clock=lambda: now)
    later = (data / sc.HUNG_STAMP_FILE).stat().st_mtime
    os.utime(data / "store.json", (later - 5, later - 5))  # the stamp is the newer file
    assert not sc.hung(env, 2.0, clock=lambda: now)
    sc.mark_hung(env, 2.0, sc.store_identity(env), clock=lambda: now)
    assert sc.hung(env, 2.0, clock=lambda: now)


def test_the_stamp_never_makes_the_data_dir(sc, tmp_path):
    env = hook_env(tmp_path)
    sc.mark_hung(env)
    assert not Path(env["NOBLIVION_DATA_DIR"]).exists()
    assert not sc.hung(env)
