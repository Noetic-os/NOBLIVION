# SPDX-License-Identifier: AGPL-3.0-or-later
"""A provider outage does not use up the attempts of learning jobs (NOBLIVION-253)."""

import contextlib
import errno
import http.client
import io
import json
import socket
import ssl
import time
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest

import noblivion.learning_worker as module
from noblivion import db, learning_cli
from noblivion.learning import BudgetLimits, LearningError, LearningLedger
from noblivion.learning_runtime import LearningPool
from noblivion.learning_worker import (
    MAX_RETURNED_DEFERRALS,
    UNAVAILABLE_MAX_DELAY,
    LearningWorker,
    OpenAICompatibleProvider,
    ProviderError,
    WorkerSettings,
    provider_answered,
)
from test_learning_context_repair import ready
from test_learning_review_fixes import ScriptedProvider, candidate, capture, seed

LIMITS = replace(BudgetLimits(), max_calls=10000, max_seconds=10**7, max_tokens=10**9)


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "outage.db", create=True)
    yield LearningLedger(conn)
    conn.close()


class OutageProvider(ScriptedProvider):
    """Raise ``reason`` while ``down`` is set; answer normally after that."""

    def __init__(self, ledger, reason="unavailable"):
        super().__init__(ledger, [])
        self.down, self.reason, self.failed = True, reason, 0

    def complete(self, messages, **kwargs):
        if self.down:
            self.failed += 1
            raise ProviderError(self.reason)
        return super().complete(messages, **kwargs)


def rows(ledger):
    return [
        (r["status"], r["attempts"], r["last_error"], json.loads(r["progress"]), r["next_attempt"])
        for r in ledger.conn.execute(
            "SELECT status,attempts,last_error,progress,next_attempt FROM learning_jobs "
            "ORDER BY rowid"
        )
    ]


def http_error(code):
    return urllib.error.HTTPError("http://127.0.0.1/x", code, "status", {}, io.BytesIO(b""))


@pytest.mark.parametrize(
    "exc, reason",
    [
        (http_error(502), "unavailable"),
        (http_error(503), "unavailable"),
        (http_error(504), "unavailable"),
        (http_error(429), "unavailable"),
        (urllib.error.URLError(ConnectionRefusedError()), "unavailable"),
        (urllib.error.URLError(socket.gaierror()), "unavailable"),
        (urllib.error.URLError(OSError(errno.EHOSTUNREACH, "no route")), "unavailable"),
        (ConnectionResetError(), "unavailable"),
        (http.client.RemoteDisconnected(), "unavailable"),
        # The endpoint answered and rejected this request: the job counts it.
        # A request that crashes the server (500) must not get the free retries.
        (http_error(500), "transport"),
        (http_error(501), "transport"),
        (http_error(400), "transport"),
        (http_error(404), "transport"),
        (urllib.error.URLError(ssl.SSLError()), "transport"),
        (urllib.error.URLError(OSError(errno.EACCES, "denied")), "transport"),
        (http.client.BadStatusLine("x"), "transport"),
    ],
)
def test_provider_names_an_outage_apart_from_a_rejected_request(exc, reason):
    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:1234", "sample", local=True, cost_per_million_tokens=0
    )

    def fail(*args, **kwargs):
        raise exc

    provider.opener = SimpleNamespace(open=fail)
    with pytest.raises(ProviderError) as raised:
        provider.complete([], max_tokens=1, timeout=5)
    assert raised.value.reason == reason


def test_a_refused_connection_on_a_real_socket_is_an_outage():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing listens on the port now.
    provider = OpenAICompatibleProvider(
        f"http://127.0.0.1:{port}", "sample", local=True, cost_per_million_tokens=0
    )
    with pytest.raises(ProviderError) as raised:
        provider.complete([], max_tokens=1, timeout=5)
    assert raised.value.reason == "unavailable"


def test_an_outage_of_twelve_hours_exhausts_no_job_and_all_jobs_end_after_it(ledger):
    for ident in ("a", "b", "c"):
        capture(ledger, ident, f"Keep replies short in scope {ident}.", root=ident)
    provider = OutageProvider(ledger)
    settings = WorkerSettings()
    run = LearningWorker(ledger, provider, settings, LIMITS)
    # 12 h at the longest delay is 48 deferrals per job; the short delays add more.
    for _ in range(56):
        ready(ledger)
        for _ in range(3):
            result = run.run_once()
            assert result["status"] == "deferred" and result["error"] == "Provider:unavailable"
    for status, attempts, error, progress, _ in rows(ledger):
        assert (status, attempts, error) == ("pending", 0, "Provider:unavailable")
        assert progress["unavailable_deferrals"] == 56
        # The outage does not spend the deferrals of a dependency or length hold.
        assert "returned_deferrals" not in progress
    provider.down = False
    ready(ledger)
    results = [run.run_once() for _ in range(3)]
    assert [r["status"] for r in results] == ["done"] * 3
    assert run.run_once()["status"] == "idle"


def test_the_delay_grows_to_the_longest_delay_and_stays_there(ledger):
    capture(ledger, "a", "Keep replies short.")
    run = LearningWorker(ledger, OutageProvider(ledger), WorkerSettings(), LIMITS)
    delays = []
    for _ in range(8):
        ready(ledger)
        before = time.time()
        run.run_once()
        delays.append(rows(ledger)[0][4] - before)
    expected = [30, 60, 120, 240, 480, 900, 900, 900]
    assert all(abs(d - e) < 5 for d, e in zip(delays, expected, strict=True)), delays
    assert UNAVAILABLE_MAX_DELAY == 900


def test_past_the_cap_each_outage_deferral_counts_an_attempt(ledger, monkeypatch):
    monkeypatch.setattr(module, "MAX_UNAVAILABLE_DEFERRALS", 3)
    capture(ledger, "a", "Keep replies short.")
    settings = WorkerSettings()
    run = LearningWorker(ledger, OutageProvider(ledger), settings, LIMITS)
    statuses = []
    for _ in range(3 + settings.max_attempts + 1):
        ready(ledger)
        statuses.append(run.run_once()["status"])
    assert statuses == ["deferred"] * 3 + ["failed"] * settings.max_attempts + ["idle"]
    status, attempts, error, progress, _ = rows(ledger)[0]
    assert (status, attempts, error) == ("exhausted", settings.max_attempts, "Provider:unavailable")
    assert "unavailable_since" in progress
    job_id = ledger.conn.execute("SELECT id FROM learning_jobs").fetchone()[0]
    ledger.retry(job_id)
    assert "unavailable_deferrals" not in rows(ledger)[0][3]
    assert "unavailable_since" not in rows(ledger)[0][3]


def test_a_rejected_request_still_counts_attempts(ledger):
    capture(ledger, "a", "Keep replies short.")
    settings = WorkerSettings()
    run = LearningWorker(ledger, OutageProvider(ledger, "transport"), settings, LIMITS)
    for _ in range(settings.max_attempts):
        ready(ledger)
        assert run.run_once()["status"] == "failed"
    assert rows(ledger)[0][:3] == ("exhausted", settings.max_attempts, "Provider:transport")
    assert MAX_RETURNED_DEFERRALS == 24


def iso(value):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def test_status_shows_the_outage_and_clears_when_the_provider_answers(ledger, monkeypatch):
    clock = [float(int(time.time()))]
    monkeypatch.setattr("noblivion.learning.time.time", lambda: clock[0])
    capture(ledger, "a", "Keep replies short.")
    provider = OutageProvider(ledger)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert ledger.status("demo", "scope")["provider"] == {"state": "ok"}
    began = clock[0]
    run.run_once()
    # A second deferral of the same job 600 s later must not move since.
    clock[0] += 600
    ready(ledger)
    run.run_once()
    shown = ledger.status("demo", "scope")["provider"]
    assert shown == {
        "state": "unreachable",
        "waiting_jobs": 1,
        "since": iso(began),
        "last_failure": iso(began + 600),
    }
    # The provider is one endpoint for the store: every scope shows the same block.
    assert ledger.status("demo", "other")["provider"] == shown
    assert ledger.status()["provider"] == shown
    provider.down = False
    ready(ledger)
    assert run.run_once()["status"] == "done"
    assert ledger.status("demo", "scope")["provider"] == {"state": "ok"}


class FlakyProvider(ScriptedProvider):
    """Follow a script of "down" and "up", one entry per call."""

    def __init__(self, ledger, candidates, script):
        super().__init__(ledger, candidates)
        self.script = list(script)

    def complete(self, messages, **kwargs):
        if self.script.pop(0) == "down":
            raise ProviderError("unavailable")
        return super().complete(messages, **kwargs)


def test_a_successful_call_resets_the_outage_count_of_the_job(ledger):
    seed(ledger, capture(ledger, "old", "Keep responses short."))
    new = capture(ledger, "new", "I prefer concise replies.")
    # Three outages on the extraction call, then the extraction answers and
    # the semantic call meets a new outage.
    provider = FlakyProvider(
        ledger, [candidate(new, key="different-wording")], ["down"] * 3 + ["up", "down"]
    )
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)

    def job(ident):
        row = ledger.conn.execute(
            "SELECT progress,next_attempt FROM learning_jobs WHERE id=?", (ident,)
        ).fetchone()
        return json.loads(row["progress"]), row["next_attempt"]

    for count in (1, 2, 3):
        ready(ledger)
        result = run.run_once()
        assert result["error"] == "Provider:unavailable"
        assert job(result["job_id"])[0]["unavailable_deferrals"] == count
    first_since = job(result["job_id"])[0]["unavailable_since"]
    ready(ledger)
    before = time.time()
    result = run.run_once()
    assert result["error"] == "Provider:unavailable" and provider.calls == ["extract"]
    progress, next_attempt = job(result["job_id"])
    # The new outage starts at retry_delay (30 s), not at 2**3 * 30 s.
    assert abs(next_attempt - before - 30) < 5, next_attempt - before
    assert progress["unavailable_deferrals"] == 1
    assert progress["unavailable_since"] >= before > first_since


@pytest.mark.parametrize(
    "result, answered",
    [
        ({"status": "done", "job_id": "j"}, True),
        ({"status": "failed", "job_id": "j", "error": "Provider:invalid"}, True),
        ({"status": "deferred", "job_id": "j", "error": "Provider:length"}, True),
        ({"status": "deferred", "job_id": "j", "error": "Provider:unavailable"}, False),
        ({"status": "failed", "job_id": "j", "error": "Provider:timeout"}, False),
        ({"status": "failed", "job_id": "j", "error": "Provider:transport"}, False),
        ({"status": "deferred", "job_id": "j", "error": "BudgetExceeded"}, False),
        ({"status": "deferred", "job_id": "j", "error": "ContextDependencyDeferred"}, False),
        ({"status": "failed"}, False),
        ({"status": "idle"}, False),
    ],
)
def test_only_a_real_answer_counts_as_the_provider_answering(result, answered):
    assert provider_answered(result) is answered


class FakeStop:
    """A stop event whose wait advances a fake monotonic clock."""

    def __init__(self, clock, limit=50):
        self.clock, self.limit, self.waits, self.flag = clock, limit, [], False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds):
        self.waits.append(seconds)
        assert len(self.waits) <= self.limit, "the loop does not end"
        self.clock[0] += seconds
        return self.flag


UNAVAILABLE = {"status": "deferred", "job_id": "j", "error": "Provider:unavailable"}
TIMEOUT = {"status": "failed", "job_id": "j", "error": "Provider:timeout", "permanent": False}
DONE = {"status": "done", "job_id": "j", "results": []}


def scripted(stop, clock, script, seen):
    script = list(script)

    def run_once(*args, **kwargs):
        seen.append(clock[0])
        result = script.pop(0)
        if not script:
            stop.set()
        return result

    return run_once


def test_run_forever_resets_its_delay_only_after_a_real_answer(ledger):
    clock = [0.0]
    stop = FakeStop(clock)
    run = LearningWorker(ledger, ScriptedProvider(ledger, []), WorkerSettings(), LIMITS)
    seen = []
    run.run_once = scripted(
        stop, clock, [UNAVAILABLE, TIMEOUT, UNAVAILABLE, DONE, UNAVAILABLE], seen
    )
    run.run_forever(stop)
    # A timeout is no answer: the next outage doubles. A done result resets it.
    assert stop.waits == [30, 5, 60, 30]


def test_the_pool_loop_holds_until_the_hold_ends_and_a_done_result_clears_it(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("noblivion.learning_runtime.time.monotonic", lambda: clock[0])
    stop = FakeStop(clock)
    released = []

    class Store:
        @contextlib.contextmanager
        def connection(self):
            yield object()

    class Ledger:
        def __init__(self, conn, pool):
            pass

        def release(self):
            released.append(True)

    monkeypatch.setattr("noblivion.learning_runtime.PooledLedger", Ledger)
    seen = []
    script = [UNAVAILABLE, UNAVAILABLE, TIMEOUT, UNAVAILABLE, DONE, UNAVAILABLE]
    monkeypatch.setattr("noblivion.learning_runtime.run_once", scripted(stop, clock, script, seen))
    pool = LearningPool(SimpleNamespace(_jobs_stop=stop))
    pool.store = Store()
    pool.settings, pool.desired, pool.poll_seconds, pool.retry_delay = {}, 1, 5.0, 30.0
    pool._work(0)
    # 1000: down, hold 30. 1030: down, hold 60. 1090: timeout, no answer, poll
    # 5 s; the hold keeps its delay. 1095: down, hold 120. 1215: done clears the
    # hold; no wait. 1215: down, hold 30 again.
    assert seen == [1000, 1030, 1090, 1095, 1215, 1215]
    assert stop.waits == [30, 60, 5, 120]
    assert pool._provider_hold_until - clock[0] == 30
    assert pool.provider_unreachable_since is not None
    assert len(released) == len(script)


def test_the_pool_holds_every_loop_and_doubles_only_after_a_new_probe(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("noblivion.learning_runtime.time.monotonic", lambda: clock[0])
    pool = LearningPool(SimpleNamespace(_jobs_stop=None))
    pool.retry_delay = 30.0
    pool._provider_down()
    assert pool._provider_hold_until == 1030.0 and pool.provider_unreachable_since
    # Two other loops fail inside the same hold: the hold stays.
    pool._provider_down()
    pool._provider_down()
    assert pool._provider_hold_until == 1030.0
    waits = []
    for _ in range(7):
        clock[0] = pool._provider_hold_until
        pool._provider_down()
        waits.append(pool._provider_hold_until - clock[0])
    assert waits == [60, 120, 240, 480, 900, 900, 900]
    pool._provider_up()
    assert pool._provider_hold_until == 0 and pool.provider_unreachable_since is None
    pool._provider_down()
    assert pool._provider_hold_until - clock[0] == 30


def test_retry_by_error_retries_only_matching_exhausted_jobs_of_the_scope(ledger):
    # One job per root: the helper puts the captures of one root in one job.
    for root in ("scope", "other", "third"):
        capture(ledger, root, f"Keep replies short in {root}.", root=root)
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='exhausted',attempts=4,last_error='Provider:transport'"
    )
    ledger.conn.execute("UPDATE learning_jobs SET last_error='Provider:timeout' WHERE root='third'")
    ids = ledger.retry_by_error("demo", "scope", "Provider:transport")
    assert len(ids) == 1
    assert [r[0] for r in rows(ledger)] == ["pending", "exhausted", "exhausted"]
    assert ledger.retry_by_error("demo", "third", "Provider:transport") == []
    with pytest.raises(LearningError):
        ledger.retry_by_error("demo", "", "Provider:transport")

    args = SimpleNamespace(command="retry", project="demo", root="third", job_id=None, error=None)
    with pytest.raises(LearningError):
        learning_cli._run(ledger, args)
    args.error = "Provider:timeout"
    assert learning_cli._run(ledger, args)["retried"] == 1
    assert [r[0] for r in rows(ledger)] == ["pending", "exhausted", "pending"]
    args.job_id = "x"
    with pytest.raises(LearningError):
        learning_cli._run(ledger, args)
