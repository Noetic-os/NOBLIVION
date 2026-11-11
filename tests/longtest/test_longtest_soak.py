# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm d: the store client, the steps and the DR8 evaluation."""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import soak


class Handler(BaseHTTPRequestHandler):
    notes: set = set()
    hidden: set = set()  # counted, but a search does not return them

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer tok":
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/api/health"):
            body = {"memories": 5 + len(Handler.notes)}
        else:
            body = {"rows": [t for t in Handler.notes - Handler.hidden if t in self.path]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def client(tmp_path):
    class BurstServer(ThreadingHTTPServer):
        # The fixture must accept the 20 simultaneous clients exercised below.
        request_queue_size = 32

    server = BurstServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "store.json").write_text(json.dumps({"port": server.server_address[1]}))
    (tmp_path / "token").write_text("tok\n")
    Handler.notes = set()
    Handler.hidden = set()
    yield soak.StoreClient(tmp_path)
    server.shutdown()
    server.server_close()


def test_query_load_records_status_and_ms(client):
    rows = soak.query_load(client, ["a", "b", "c"], parallel=1)
    assert [r["ok"] for r in rows] == [True] * 3 and all(r["ms"] >= 0 for r in rows)
    burst = soak.query_load(client, ["q"] * 20, parallel=20)
    assert len(burst) == 20 and all(r["status"] == 200 for r in burst)


def test_bad_token_is_an_error_row(client, tmp_path):
    (tmp_path / "token").write_text("wrong\n")
    assert soak.query_load(client, ["a"])[0]["ok"] is False


def store_sync(seen: dict[Path, set[str]]):
    """A stand-in for the memory-sync hook: it indexes the markers of a note that is
    there, and forgets the markers of one that has gone."""

    def sync(path: Path) -> None:
        if path.exists():
            seen[path] = set(re.findall(r"zqx\w+?marker", path.read_text()))
            Handler.notes.update(seen[path])
        else:
            Handler.notes.difference_update(seen.pop(path, set()))

    return sync


def test_churn_measures_the_lag_of_a_new_note(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    sync = store_sync({})
    rows = soak.churn(folder, client, 1, sync, timeout_s=5)
    assert rows[0]["kind"] == "write" and rows[0]["ok"] and rows[0]["lag_s"] < 2
    for n in range(2, 6):
        rows = soak.churn(folder, client, n, sync, timeout_s=5)
    assert {r["kind"] for r in rows} >= {"write", "delete", "edit"}
    assert len(list(folder.glob("project_soak_*.md"))) <= 5


def test_churn_waits_for_the_delete_to_be_registered(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    sync = store_sync({})
    for n in range(1, 6):
        rows = soak.churn(folder, client, n, sync, timeout_s=5)
    gone = [r for r in rows if r["kind"] == "delete"]
    assert gone and gone[0]["ok"] and gone[0]["lag_s"] is not None
    # nothing the churn deleted is still searchable: the store caught up before the
    # churn returned, so a drift reading right after it is not a race
    assert len(Handler.notes) == len(list(folder.glob("project_soak_*.md")))


def test_churn_reports_a_delete_the_store_keeps(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()

    def sync(path: Path) -> None:  # indexes a write, never forgets a delete
        if path.exists():
            Handler.notes.update(re.findall(r"zqx\w+?marker", path.read_text()))

    for n in range(1, 6):
        rows = soak.churn(folder, client, n, sync, timeout_s=1)
    gone = [r for r in rows if r["kind"] == "delete"]
    assert gone and gone[0]["ok"] is False and gone[0]["lag_s"] is None


def hiding_sync(seen: dict[Path, set[str]], forget_after_s: float | None):
    """A sync whose store drops a deleted note from its search at once but keeps
    counting it until a scan ``forget_after_s`` later (never, with ``None``): the
    shape of a delete the ranking loses before the index registered it."""

    def sync(path: Path) -> None:
        if path.exists():
            seen[path] = set(re.findall(r"zqx\w+?marker", path.read_text()))
            Handler.notes.update(seen[path])
            return
        gone = seen.pop(path, set())
        Handler.hidden.update(gone)
        if forget_after_s is not None:
            threading.Timer(forget_after_s, Handler.notes.difference_update, [gone]).start()

    return sync


def test_churn_waits_for_the_count_not_only_the_search(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    sync = hiding_sync({}, forget_after_s=0.6)
    for n in range(1, 5):
        rows = soak.churn(folder, client, n, sync, timeout_s=5)
    gone = [r for r in rows if r["kind"] == "delete"]
    # the search lost the note at once; the lag is the time until the count dropped
    assert gone and gone[0]["ok"] and gone[0]["lag_s"] >= 0.5
    assert len(Handler.notes) == len(list(folder.glob("project_soak_*.md")))


def test_churn_reports_a_delete_the_search_lost_but_the_store_counts(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    sync = hiding_sync({}, forget_after_s=None)
    for n in range(1, 5):
        rows = soak.churn(folder, client, n, sync, timeout_s=1)
    gone = [r for r in rows if r["kind"] == "delete"]
    assert gone and gone[0]["ok"] is False and gone[0]["lag_s"] is None


def test_churn_reports_a_note_that_never_appears(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    rows = soak.churn(folder, client, 1, lambda p: None, timeout_s=1)
    assert rows[0]["ok"] is False and rows[0]["lag_s"] is None


NO_WAIT = {"poll_s": 0.0, "sleep": lambda s: None}


def test_drift_is_disk_change_minus_store_change(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    (folder / "a.md").write_text("x")
    (folder / "MEMORY.md").write_text("index")
    base = soak.baseline(client, [folder], **NO_WAIT)
    assert base["disk"] == 1
    (folder / "b.md").write_text("y")
    got = soak.drift(client, [folder], base, **NO_WAIT)
    assert got["drift"] == 1 and got["settled"]  # the store has not followed
    Handler.notes.add("b")
    got = soak.drift(client, [folder], base, **NO_WAIT)
    assert got["drift"] == 0 and got["settled"]


def test_drift_says_so_when_the_store_never_stops_moving(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    (folder / "a.md").write_text("x")
    base = soak.baseline(client, [folder], **NO_WAIT)
    counted = itertools.count()

    def moving(folders):  # a note is indexed between every two readings
        Handler.notes.add(f"n{next(counted)}")
        return 1

    with pytest.MonkeyPatch.context() as m:
        m.setattr(soak, "disk_notes", moving)
        got = soak.drift(client, [folder], base, settle_s=0.3, poll_s=0.0, sleep=lambda s: None)
    assert got["settled"] is False


def test_a_store_that_counts_nothing_is_a_failed_call(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    base = {"disk": 1, "memories": 20000}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(soak.StoreClient, "health", lambda self: (True, {"memories": 0}))
        with pytest.raises(soak.HealthCallFailed):
            soak.drift(client, [folder], base, **NO_WAIT)


def test_a_baseline_that_does_not_settle_stops_the_run(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    counted = itertools.count()

    def moving(folders):
        Handler.notes.add(f"n{next(counted)}")
        return len(Handler.notes)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(soak, "disk_notes", moving)
        with pytest.raises(soak.HealthCallFailed):
            soak.baseline(client, [folder], settle_s=0.3, poll_s=0.0, sleep=lambda s: None)


def test_resources_reads_sqlite_read_only(tmp_path):
    con = sqlite3.connect(tmp_path / "noblivion.db")
    con.execute("create table t (x)")
    con.commit()
    con.close()
    got = soak.resources(tmp_path, None)
    assert got["quick_check"] == "ok" and got["db_mb"] >= 0
    assert soak.resources(tmp_path / "none", None)["quick_check"].startswith("error")


def test_rss_of_missing_pid_is_zero(tmp_path):
    assert soak.rss_mb(999999, tmp_path) == 0.0
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "status").write_text("Name: x\nVmRSS:\t   2048 kB\n")
    assert soak.rss_mb(7, tmp_path) == 2.0


def test_latency_benchmark_caps_by_count():
    seen = []
    out = soak.latency_benchmark(lambda p: seen.append(p) or 10.0, ["a", "b"], n=7)
    assert out["n"] == 7 and out["p95"] == 10.0 and len(seen) == 7


def rows_for(days, p95=100.0, errors=0, lag=2.0, drift=0, rss=None, settled=True):
    rows = []
    for d, day in enumerate(days):
        for i in range(400):
            rows.append(
                {"day": day, "kind": "query", "ok": i >= errors, "status": 200, "ms": p95 - (i % 5)}
            )
        rows.append({"day": day, "kind": "write", "ok": True, "lag_s": lag})
        rows.append({"day": day, "kind": "drift", "drift": drift, "settled": settled})
        for h in range(3):
            rows.append(
                {
                    "day": day,
                    "kind": "resources",
                    "quick_check": "ok",
                    "rss_mb": 500.0 if rss is None else rss(d, h),
                }
            )
    return rows


DAYS = [f"2026-11-0{i}" for i in range(1, 8)]


def test_evaluate_passes_a_healthy_week():
    got = soak.evaluate(rows_for(DAYS))
    assert got["pass"], got["checks"]
    assert got["lag_p95_s"] == 2.0 and got["drift_max"] == 0


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("budget_recall", {"p95": 2500.0}),
        ("error_rate", {"errors": 20}),
        ("drift_zero", {"drift": 1}),
        ("drift_settled", {"settled": False}),
        ("lag_p95", {"lag": 12.0}),
        ("memory_trend", {"rss": lambda d, h: 500.0 + 80 * d + h}),
    ],
)
def test_evaluate_fails_one_rule_at_a_time(name, kwargs):
    got = soak.evaluate(rows_for(DAYS, **kwargs))
    assert not got["checks"][name], got
    assert not got["pass"]
    assert all(v for k, v in got["checks"].items() if k != name)


def test_evaluate_fails_a_bad_quick_check():
    rows = rows_for(DAYS)
    rows.append(
        {"day": DAYS[0], "kind": "resources", "quick_check": "row 3 missing", "rss_mb": 500.0}
    )
    assert not soak.evaluate(rows)["checks"]["quick_check"]


def test_a_short_run_is_underpowered_not_failed_on_the_error_rate():
    short = [r for r in rows_for(DAYS[:1]) if r["kind"] != "query"] + [
        {"day": DAYS[0], "kind": "query", "ok": True, "status": 200, "ms": 20.0} for _ in range(10)
    ]
    got = soak.evaluate(short)
    assert got["checks"]["error_rate"] and got["error_rate_underpowered"]
    assert got["pass"], got["checks"]
    bad = short + [{"day": DAYS[0], "kind": "query", "ok": False, "status": 500, "ms": 5.0}]
    assert not soak.evaluate(bad)["checks"]["error_rate"]  # one error is a failure at any size
    assert soak.queries_needed(0.005) == 765
    assert not soak.evaluate(rows_for(DAYS))["error_rate_underpowered"]


def test_a_slow_lag_misses_dr8_but_a_lost_write_fails_a_separate_check():
    slow = soak.evaluate(rows_for(DAYS, lag=23.0))
    assert not slow["checks"]["lag_p95"] and slow["checks"]["writes_searchable"]
    lost = rows_for(DAYS) + [{"day": DAYS[0], "kind": "write", "ok": False, "lag_s": None}]
    got = soak.evaluate(lost)
    assert not got["checks"]["writes_searchable"] and got["failed_writes"] == 1


def test_the_budget_is_for_steady_load_and_the_burst_tail_is_reported():
    rows = rows_for(DAYS)
    for i in range(20):  # an hourly burst of 20 parallel requests that queue in the store
        rows.append(
            {
                "day": DAYS[0],
                "kind": "query",
                "ok": True,
                "status": 200,
                "ms": 350.0 * (i + 1),
                "burst": True,
            }
        )
    got = soak.evaluate(rows)
    assert got["checks"]["budget_recall"] and got["burst_p95_ms"][DAYS[0]] > 6000
    assert got["queries"] == len(DAYS) * 400 + 20
    rows.append(
        {"day": DAYS[0], "kind": "query", "ok": False, "status": 500, "ms": 5.0, "burst": True}
    )
    assert got["errors"] == 0 and soak.evaluate(rows)["errors"] == 1  # a burst error counts


def test_a_churn_marker_is_new_after_a_restart(tmp_path):
    seen = []

    class Client:
        def search(self, token, project, top_k):
            seen.append(token)
            return 200, {"rows": [token]}, 1.0

    folder = tmp_path / "m"
    folder.mkdir()
    for _ in range(2):  # the serial starts at 1 again after a restart
        assert soak.churn(folder, Client(), 1, lambda p: None)[0]["ok"]
    assert len(set(seen)) == 2 and all(t.startswith("zqx000001") for t in seen)
