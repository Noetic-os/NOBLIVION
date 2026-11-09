# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard deadlines survive unraisable cleanup exceptions and leave no timer behind."""

import io
import json
import signal
import sys
import time
import weakref

import pytest

from hookload import load_hook_with_config

gh = load_hook_with_config("guard_hook", {}, "guard_timeout_test")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    env = {"NOBLIVION_DATA_DIR": str(tmp_path), "HOME": str(tmp_path)}
    errors = []
    monkeypatch.setattr(gh, "log_line", lambda *args, **kwargs: errors.append(kwargs))
    try:
        yield env, errors
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def call(env, limit=0.2):
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "fictional-timeout-test",
        "tool_name": "Read",
        "tool_input": {"file_path": "/fictional/never-read.txt"},
    }
    output = io.StringIO()
    started = time.monotonic()
    result = gh.main(io.StringIO(json.dumps(event)), output, env, time_limit=limit)
    return result, time.monotonic() - started, output.getvalue()


def collect_across_alarm():
    class FictionalObject:
        pass

    def cleanup(_reference):
        time.sleep(0.4)

    target = FictionalObject()
    reference = weakref.ref(target, cleanup)
    del target
    assert reference() is None


@pytest.mark.parametrize("cleanups", [1, 3])
@pytest.mark.parametrize("continuation", [0.0, 1.6])
def test_swallowed_alarms_still_end_the_deny_check(isolated, monkeypatch, cleanups, continuation):
    env, errors = isolated
    ignored = []
    monkeypatch.setattr(sys, "unraisablehook", lambda error: ignored.append(error.exc_type))

    def decide(*args):
        for _ in range(cleanups):
            collect_across_alarm()
        if continuation:
            time.sleep(continuation)
        return gh.Decision()

    monkeypatch.setattr(gh, "decide", decide)
    result, elapsed, output = call(env)
    assert result == 0 and elapsed < 1.5
    assert 1 <= len(ignored) <= cleanups
    assert all(kind is gh._TimeUp for kind in ignored)
    assert json.loads(output)["systemMessage"] == gh.TIMEOUT_LINE
    assert errors == [{"error": "timeout"}]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.08)  # no retry alarm may escape after main returned


@pytest.mark.parametrize("outcome", ["normal", "error", "timeout"])
def test_prior_handler_and_timer_are_restored(isolated, monkeypatch, outcome):
    env, _ = isolated
    previous_calls = []

    def previous_handler(*args):
        previous_calls.append(args)

    signal.signal(signal.SIGALRM, previous_handler)
    signal.setitimer(signal.ITIMER_REAL, 3.0, 0.25)

    def decide(*args):
        if outcome == "error":
            raise ValueError("fictional failure")
        if outcome == "timeout":
            signal.raise_signal(signal.SIGALRM)
        return gh.Decision()

    monkeypatch.setattr(gh, "decide", decide)
    call(env)
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    assert 0 < remaining <= 3.0 and interval == 0.25
    assert previous_calls == []


def test_commit_timeout_keeps_the_printed_decision(isolated, monkeypatch):
    env, errors = isolated
    ignored = []
    monkeypatch.setattr(sys, "unraisablehook", lambda error: ignored.append(error.exc_type))
    monkeypatch.setattr(gh, "decide", lambda *args: gh.Decision('{"result":"already printed"}'))
    monkeypatch.setattr(gh.Decision, "commit", lambda self: collect_across_alarm())
    result, _, output = call(env)
    assert result == 0
    assert json.loads(output) == {"result": "already printed"}
    assert errors == [{"error": "timeout"}]
    assert ignored == [gh._TimeUp]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.08)


@pytest.mark.parametrize("swallowed", [False, True])
@pytest.mark.parametrize("error_after", [False, True])
def test_rebuild_timeout_does_not_expire_the_outer_guard(
    isolated, monkeypatch, tmp_path, swallowed, error_after
):
    env, errors = isolated
    ignored = []
    monkeypatch.setattr(sys, "unraisablehook", lambda error: ignored.append(error.exc_type))
    monkeypatch.setattr(gh, "REBUILD_LIMIT_S", 0.03)

    class Table:
        @staticmethod
        def folder_stamp(folders):
            return "fictional-stamp"

        @staticmethod
        def probes_path(path):
            return path.with_suffix(".probes")

        @staticmethod
        def _read_probes(path):
            return {}

        @staticmethod
        def rebuild(*args, **kwargs):
            if swallowed:
                collect_across_alarm()
            if error_after:
                raise ValueError("fictional rebuild error")
            if swallowed:
                return {}
            signal.raise_signal(signal.SIGALRM)

        @staticmethod
        def skipped_line(table):
            return ""

    def decide(event, environ, pending, notes):
        outer_handler = signal.getsignal(signal.SIGALRM)
        gh._rebuild_locked(Table, [], tmp_path / "fictional-table", notes)
        assert signal.getsignal(signal.SIGALRM) == outer_handler
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        return gh.Decision()

    monkeypatch.setattr(gh, "decide", decide)
    result, _, output = call(env)
    assert result == 0
    if error_after and not swallowed:
        assert output == ""
        assert errors == [{"error": "ValueError: fictional rebuild error"}]
    else:
        assert json.loads(output)["systemMessage"] == gh.REBUILD_NONE_LINE
        assert errors == [{"error": "table rebuild timeout"}]
    assert ignored == ([gh._TimeUp] if swallowed else [])
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.08)


def test_unavailable_thread_alarm_preserves_process_timer(isolated):
    import threading

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.setitimer(signal.ITIMER_REAL, 3.0, 0.25)
    result = []
    worker = threading.Thread(target=lambda: result.append(gh._Deadline(0.2).start()))
    worker.start()
    worker.join(2.0)
    assert not worker.is_alive()
    assert result == [False]
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    assert 0 < remaining <= 3.0 and interval == 0.25


@pytest.mark.parametrize("expire_first", [False, True])
def test_error_after_swallowed_alarm_keeps_timeout_notice(isolated, monkeypatch, expire_first):
    env, errors = isolated
    ignored = []
    monkeypatch.setattr(sys, "unraisablehook", lambda error: ignored.append(error.exc_type))

    def decide(*args):
        if expire_first:
            collect_across_alarm()
        raise ValueError("fictional error after callback")

    monkeypatch.setattr(gh, "decide", decide)
    result, _, output = call(env)
    assert result == 0
    if expire_first:
        assert json.loads(output)["systemMessage"] == gh.TIMEOUT_LINE
        assert errors == [{"error": "timeout"}]
        assert ignored == [gh._TimeUp]
    else:
        assert output == ""
        assert errors == [{"error": "ValueError: fictional error after callback"}]
        assert ignored == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.08)


def test_alarm_during_startup_has_an_owner_for_cleanup(isolated, monkeypatch):
    env, errors = isolated
    previous_handler = signal.getsignal(signal.SIGALRM)
    set_timer = signal.setitimer

    def interrupted_start(which, seconds, interval=0.0):
        result = set_timer(which, seconds, interval)
        if seconds == 0.03:
            signal.raise_signal(signal.SIGALRM)  # deliver the armed alarm before start returns
        return result

    monkeypatch.setattr(signal, "setitimer", interrupted_start)
    result, _, output = call(env, limit=0.03)
    assert result == 0 and output == ""  # event has not been read yet
    assert errors == [{"error": "timeout"}]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    time.sleep(0.08)
