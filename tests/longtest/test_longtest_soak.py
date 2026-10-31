# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm d: the store client, the steps and the DR8 evaluation."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import soak


class Handler(BaseHTTPRequestHandler):
    notes: set = set()

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer tok":
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/api/health"):
            body = {"memories": 5 + len(Handler.notes)}
        else:
            body = {"rows": [t for t in Handler.notes if t in self.path]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def client(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "store.json").write_text(json.dumps({"port": server.server_address[1]}))
    (tmp_path / "token").write_text("tok\n")
    Handler.notes = set()
    yield soak.StoreClient(tmp_path)
    server.shutdown()


def test_query_load_records_status_and_ms(client):
    rows = soak.query_load(client, ["a", "b", "c"], parallel=1)
    assert [r["ok"] for r in rows] == [True] * 3 and all(r["ms"] >= 0 for r in rows)
    burst = soak.query_load(client, ["q"] * 20, parallel=20)
    assert len(burst) == 20 and all(r["status"] == 200 for r in burst)


def test_bad_token_is_an_error_row(client, tmp_path):
    (tmp_path / "token").write_text("wrong\n")
    assert soak.query_load(client, ["a"])[0]["ok"] is False


def test_churn_measures_the_lag_of_a_new_note(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()

    def sync(path: Path) -> None:
        if path.exists():  # the store "indexes" the marker of a written note
            Handler.notes.add(f"zqx{int(path.stem.split('_')[-1]):06d}marker")

    rows = soak.churn(folder, client, 1, sync, timeout_s=5)
    assert rows[0]["kind"] == "write" and rows[0]["ok"] and rows[0]["lag_s"] < 2
    for n in range(2, 6):
        rows = soak.churn(folder, client, n, sync, timeout_s=5)
    assert {r["kind"] for r in rows} >= {"write", "delete", "edit"}
    assert len(list(folder.glob("project_soak_*.md"))) <= 5


def test_churn_reports_a_note_that_never_appears(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    rows = soak.churn(folder, client, 1, lambda p: None, timeout_s=1)
    assert rows[0]["ok"] is False and rows[0]["lag_s"] is None


def test_drift_is_disk_change_minus_store_change(client, tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    (folder / "a.md").write_text("x")
    (folder / "MEMORY.md").write_text("index")
    base = soak.baseline(client, [folder])
    assert base["disk"] == 1
    (folder / "b.md").write_text("y")
    assert soak.drift(client, [folder], base)["drift"] == 1  # the store has not followed
    Handler.notes.add("b")
    assert soak.drift(client, [folder], base)["drift"] == 0


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


def rows_for(days, p95=100.0, errors=0, lag=2.0, drift=0, rss=None):
    rows = []
    for d, day in enumerate(days):
        for i in range(400):
            rows.append(
                {"day": day, "kind": "query", "ok": i >= errors, "status": 200, "ms": p95 - (i % 5)}
            )
        rows.append({"day": day, "kind": "write", "ok": True, "lag_s": lag})
        rows.append({"day": day, "kind": "drift", "drift": drift})
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
