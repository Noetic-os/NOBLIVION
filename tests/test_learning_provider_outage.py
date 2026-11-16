# SPDX-License-Identifier: AGPL-3.0-or-later
"""A provider outage does not use up the attempts of learning jobs (NOBLIVION-253)."""

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
)
from test_learning_context_repair import ready
from test_learning_review_fixes import ScriptedProvider, capture

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
        (http_error(500), "unavailable"),
        (http_error(429), "unavailable"),
        (urllib.error.URLError(ConnectionRefusedError()), "unavailable"),
        (urllib.error.URLError(socket.gaierror()), "unavailable"),
        (urllib.error.URLError(OSError(errno.EHOSTUNREACH, "no route")), "unavailable"),
        (ConnectionResetError(), "unavailable"),
        (http.client.RemoteDisconnected(), "unavailable"),
        # The endpoint answered and rejected this request: the job counts it.
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
    job_id = ledger.conn.execute("SELECT id FROM learning_jobs").fetchone()[0]
    ledger.retry(job_id)
    assert "unavailable_deferrals" not in rows(ledger)[0][3]


def test_a_rejected_request_still_counts_attempts(ledger):
    capture(ledger, "a", "Keep replies short.")
    settings = WorkerSettings()
    run = LearningWorker(ledger, OutageProvider(ledger, "transport"), settings, LIMITS)
    for _ in range(settings.max_attempts):
        ready(ledger)
        assert run.run_once()["status"] == "failed"
    assert rows(ledger)[0][:3] == ("exhausted", settings.max_attempts, "Provider:transport")
    assert MAX_RETURNED_DEFERRALS == 24


def test_status_shows_the_outage_and_clears_when_the_provider_answers(ledger):
    capture(ledger, "a", "Keep replies short.")
    provider = OutageProvider(ledger)
    run = LearningWorker(ledger, provider, WorkerSettings(), LIMITS)
    assert ledger.status("demo", "scope")["provider"] == {"state": "ok"}
    run.run_once()
    shown = ledger.status("demo", "scope")["provider"]
    assert shown["state"] == "unreachable" and shown["waiting_jobs"] == 1
    assert shown["since"].endswith("Z") and shown["last_failure"] >= shown["since"]
    assert ledger.status("demo", "other")["provider"] == {"state": "ok"}
    provider.down = False
    ready(ledger)
    assert run.run_once()["status"] == "done"
    assert ledger.status("demo", "scope")["provider"] == {"state": "ok"}


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
