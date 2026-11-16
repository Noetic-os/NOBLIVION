# SPDX-License-Identifier: AGPL-3.0-or-later
"""A provider outage does not use up the attempts of learning jobs.

NOBLIVION-253: refused connections and outage status codes. NOBLIVION-255: a
provider that accepts the connection and never answers.
"""

import contextlib
import errno
import http.client
import http.server
import io
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
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


# NOBLIVION-255: a provider that accepts the connection and never answers.


class FakeEndpoint(http.server.ThreadingHTTPServer):
    """A loopback chat completions endpoint with a switchable fault mode.

    mode "hung": every request waits and never gets an answer.
    mode "probe": a one-token request gets an answer; any other request hangs.
    mode "up": every request gets a valid empty extraction answer.
    With ``health`` set, GET /health and GET /v1/models answer in every mode.
    """

    daemon_threads = True
    block_on_close = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.mode, self.health, self.seen = "hung", False, []
        self.release = threading.Event()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def posts(self, max_tokens=None):
        return [s for s in self.seen if s[0] == "POST" and s[2] == max_tokens]


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def hang(self):
        self.server.release.wait(30)

    def do_GET(self):
        self.server.seen.append(("GET", self.path, None))
        if self.server.health and self.path in {"/health", "/v1/models"}:
            self.answer({"status": "ok", "data": [{"id": "sample"}]})
        else:
            self.hang()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(("POST", self.path, body.get("max_tokens")))
        mode = self.server.mode
        if self.path != "/v1/chat/completions" or mode == "hung":
            return self.hang()
        if mode == "probe" and body["max_tokens"] == 1:
            reply = {"finish_reason": "length", "message": {"content": "Sure"}}
            return self.answer({"choices": [reply], "usage": {"total_tokens": 9}})
        if mode == "probe":
            return self.hang()
        content = json.dumps({"version": 1, "candidates": []})
        reply = {"finish_reason": "stop", "message": {"content": content}}
        self.answer({"choices": [reply], "usage": {"total_tokens": 100}})


@pytest.fixture
def endpoint():
    server = FakeEndpoint()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()


def local_provider(endpoint):
    return OpenAICompatibleProvider(endpoint.url, "sample", local=True, cost_per_million_tokens=0)


# A short real deadline keeps the test fast; the job delays are simulated.
SHORT = 0.2


def run_hung_rounds(ledger, endpoint, monkeypatch, roots, rounds):
    """Run every job once per round; return the job delays of each round."""
    monkeypatch.setattr(module, "PROBE_TIMEOUT_SECONDS", SHORT)
    run = LearningWorker(
        ledger, local_provider(endpoint), WorkerSettings(timeout_seconds=SHORT), LIMITS
    )
    delays = []
    for _ in range(rounds):
        ready(ledger)
        before = time.time()
        for _ in roots:
            result = run.run_once()
            assert result["status"] == "deferred", result
            assert result["error"] == "Provider:unavailable"
        delays.append(min(r[4] for r in rows(ledger)) - before)
    return delays


def test_a_hung_provider_for_thirty_minutes_exhausts_no_job(ledger, endpoint, monkeypatch):
    roots = ("a", "b")
    for root in roots:
        capture(ledger, root, f"Keep replies short in scope {root}.", root=root)
    settings = WorkerSettings()
    # 30 s, 60 s, ... 900 s: six rounds are more than 30 minutes of outage, and
    # more rounds than max_attempts, so a timeout that counted would exhaust.
    delays = run_hung_rounds(ledger, endpoint, monkeypatch, roots, 6)
    assert sum(delays) >= 1800 - 10 and len(delays) > settings.max_attempts
    for status, attempts, error, progress, _ in rows(ledger):
        assert (status, attempts, error) == ("pending", 0, "Provider:unavailable")
        assert progress["unavailable_deferrals"] == 6 and "unavailable_since" in progress
        assert progress["provider_metrics"]["reasons"] == {"unavailable": 6}
    # Every timed-out call got one probe on the completions path.
    assert len(endpoint.posts(max_tokens=1)) == 6 * len(roots)
    assert not [s for s in endpoint.seen if s[0] == "GET"]
    # The probe runs inside the reservation of the call: one budget row per
    # call, and the measured time holds the call and the probe, not two timeouts.
    budget = ledger.conn.execute("SELECT seconds FROM learning_budget").fetchall()
    assert len(budget) == 6 * len(roots)
    assert all(2 * SHORT * 0.9 <= row[0] < SHORT + 1.5 for row in budget), budget
    endpoint.mode = "up"
    endpoint.release.set()
    run = LearningWorker(ledger, local_provider(endpoint), WorkerSettings(), LIMITS)
    ready(ledger)
    assert [run.run_once()["status"] for _ in roots] == ["done", "done"]
    assert run.run_once()["status"] == "idle"


def test_health_and_models_answering_do_not_hide_a_hung_completions_path(
    ledger, endpoint, monkeypatch
):
    endpoint.health = True
    for path in ("/health", "/v1/models"):
        with urllib.request.urlopen(endpoint.url + path, timeout=5) as response:
            assert response.status == 200
    capture(ledger, "a", "Keep replies short.")
    settings = WorkerSettings()
    run_hung_rounds(ledger, endpoint, monkeypatch, ("a",), settings.max_attempts + 1)
    assert rows(ledger)[0][:3] == ("pending", 0, "Provider:unavailable")
    probes = endpoint.posts(max_tokens=1)
    assert probes == [("POST", "/v1/chat/completions", 1)] * (settings.max_attempts + 1)


def test_a_timeout_on_a_healthy_provider_still_counts_attempts(ledger, endpoint, monkeypatch):
    endpoint.mode, endpoint.health = "probe", True
    capture(ledger, "a", "Keep replies short.")
    settings = WorkerSettings(timeout_seconds=1.0)
    run = LearningWorker(ledger, local_provider(endpoint), settings, LIMITS)
    for _ in range(settings.max_attempts):
        ready(ledger)
        result = run.run_once()
        assert (result["status"], result["error"]) == ("failed", "Provider:timeout")
    ready(ledger)
    assert run.run_once()["status"] == "idle"
    status, attempts, error, progress, _ = rows(ledger)[0]
    assert (status, attempts, error) == ("exhausted", settings.max_attempts, "Provider:timeout")
    assert "unavailable_deferrals" not in progress
    # The probe got its answer each time: the server is up, the request is at fault.
    assert len(endpoint.posts(max_tokens=1)) == settings.max_attempts


def test_a_hung_provider_holds_the_pool_until_it_answers(ledger, endpoint, monkeypatch):
    monkeypatch.setattr(module, "PROBE_TIMEOUT_SECONDS", SHORT)
    clock = [1000.0]

    class PoolTime:
        """Fake only the clock of the pool: the provider deadline stays real."""

        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self):
            return clock[0]

    monkeypatch.setattr("noblivion.learning_runtime.time", PoolTime())
    stop = FakeStop(clock)
    capture(ledger, "a", "Keep replies short.")
    worker = LearningWorker(
        ledger, local_provider(endpoint), WorkerSettings(timeout_seconds=SHORT), LIMITS
    )
    pool = LearningPool(SimpleNamespace(_jobs_stop=stop))
    seen = []

    results = []

    def run_once(*args, **kwargs):
        seen.append((pool._provider_hold_until, pool.provider_unreachable_since))
        if len(seen) == 2:
            # The hold of the first failure is over; the provider is back.
            endpoint.mode = "up"
            endpoint.release.set()
            worker.settings = WorkerSettings()
            stop.set()
        ready(ledger)
        results.append(worker.run_once())
        return results[-1]

    class Store:
        @contextlib.contextmanager
        def connection(self):
            yield object()

    monkeypatch.setattr("noblivion.learning_runtime.run_once", run_once)
    monkeypatch.setattr(
        "noblivion.learning_runtime.PooledLedger",
        lambda conn, pool: SimpleNamespace(release=lambda: None),
    )
    pool.store = Store()
    pool.settings, pool.desired, pool.poll_seconds, pool.retry_delay = {}, 1, 5.0, 30.0
    pool._work(0)
    # The hung call held every loop for retry_delay; the next call came after it.
    assert [r.get("error") for r in results] == ["Provider:unavailable", None]
    assert seen[0] == (0.0, None)
    assert seen[1][0] == 1030.0 and seen[1][1] is not None
    assert stop.waits == [30]
    assert rows(ledger)[0][0] == "done"
    assert pool._provider_hold_until == 0 and pool.provider_unreachable_since is None


@pytest.mark.parametrize(
    "exc, answered",
    [
        (TimeoutError(), False),
        (urllib.error.URLError(TimeoutError()), False),
        (http_error(503), False),
        (http_error(429), False),
        (urllib.error.URLError(ConnectionRefusedError()), False),
        # Any answer shows that the server is up, also an error status.
        (http_error(500), True),
        (http_error(400), True),
        (None, True),
    ],
)
def test_the_probe_is_false_only_when_the_completions_path_does_not_answer(exc, answered):
    provider = local_provider(SimpleNamespace(url="http://127.0.0.1:1234"))
    sent = []

    def open_(request, timeout):
        sent.append((request.get_method(), request.full_url, json.loads(request.data), timeout))
        if exc is not None:
            raise exc
        return contextlib.nullcontext(io.BytesIO(b"{}"))

    provider.opener = SimpleNamespace(open=open_)
    assert provider.probe(timeout=7) is answered
    method, url, body, timeout = sent[0]
    assert (method, url, timeout) == ("POST", "http://127.0.0.1:1234/v1/chat/completions", 7)
    assert body["max_tokens"] == 1 and body["model"] == "sample"
