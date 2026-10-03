# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stop flush of trust events (``hooks/trust_flush.py``).

Fictional data only. What these tests hold:

1. THE HOOK. Off when the trust events are off; on by default. On, it
   starts one detached worker and returns: nothing on stdout, exit 0, and
   (end to end, as a subprocess) it does not wait for a slow store.
2. THE BODY is the section 4.5 shape: ``session_id``, the session ``root``
   and events with ``mv_id`` or ``path``, ``kind``, ``ts`` (and ``sources``),
   one per memory and kind, at most 500 events and 256 KB per request.
3. IDEMPOTENT. A second flush with nothing new sends nothing; new lines are
   sent once; the sent offset moves only on HTTP 200.
4. FAIL OPEN. A timeout, an error answer, a store that is down or a
   listener that fails the proof keeps the offset and prints nothing.
5. FETCHES FROM THE TRANSCRIPT become ``use`` events once, also when a call
   and its result are split over two Stops.
6. RETRY AND PRUNE: another session's unsent lines are retried; sent files
   older than 7 days are removed, unsent ones are kept until 30 days.
7. END TO END against a live store: found through ``store.json``, proved
   with the HMAC nonce, the events stored.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from hookload import HOOKS, load_hook

fl = load_hook("trust_flush", "trust_flush_t_flush")
te = fl.te()

SID = "flush-session-1"
TS = "2026-10-01T10:00:00+00:00"
TOKEN = "ab" * 32
BASE = "http://127.0.0.1:9"


class Recorder:
    def __init__(self, answer: Any = None, exc: Exception | None = None):
        self.bodies: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.answer = (
            answer
            if answer is not None
            else {"inserted": 1, "duplicate": 0, "unknown": 0, "rejected": 0}
        )
        self.exc = exc

    def __call__(self, url: str, token: str, body: bytes, timeout: float) -> Any:
        assert url == BASE + "/api/memory/feedback/batch" and timeout == fl.POST_TIMEOUT_S
        self.bodies.append(json.loads(body))
        self.tokens.append(token)
        if self.exc is not None:
            raise self.exc
        return self.answer


def _connect(_env):
    return BASE, TOKEN


def _down(_env):
    raise fl.PostError("store_down")


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
        "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
        "NOBLIVION_TRUST_REPORT_FILE": str(tmp_path / "report.json"),
        "NOBLIVION_RECALL_ROOT": "-work-proj-demo",
    }


def _no_refresh(_env):
    return "off"


def _run(env, post=None, transcript="", connect=_connect):
    return fl.run_child(SID, transcript, env, post=post, refresh=_no_refresh, connect=connect)


def _append(env: dict[str, str], events: list[dict[str, Any]], sid: str = SID) -> None:
    assert te.append_events(env["NOBLIVION_RECALL_CACHE_DIR"], sid, events, env)


def _events_path(env: dict[str, str], sid: str = SID) -> Path:
    return Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "by-session" / f"{sid}.trust-events.jsonl"


# 1. the hook ------------------------------------------------------------
def test_hook_is_off_when_the_events_are_off_and_never_prints(env):
    calls = []
    out = io.StringIO()
    payload = json.dumps({"session_id": SID, "transcript_path": "/x.jsonl"})
    for value in ("0", "off", "false", "no"):
        e = dict(env, NOBLIVION_TRUST_EVENTS=value)
        assert fl.main(io.StringIO(payload), out, e, spawn=lambda *a: calls.append(a)) == 0
    assert calls == [] and out.getvalue() == ""


def test_hook_is_on_by_default_and_spawns_one_worker(env):
    calls = []
    out = io.StringIO()
    payload = json.dumps(
        {"session_id": SID, "transcript_path": "/t/s.jsonl", "cwd": "/work/proj/demo"}
    )
    assert fl.main(io.StringIO(payload), out, env, spawn=lambda *a: calls.append(a) or True) == 0
    assert out.getvalue() == ""
    assert [(c[0], c[1], c[3]) for c in calls] == [(SID, "/t/s.jsonl", "/work/proj/demo")]
    for bad in ("not json", json.dumps({"session_id": "../etc"}), json.dumps([1])):
        assert fl.main(io.StringIO(bad), out, env, spawn=lambda *a: calls.append(a)) == 0
    assert len(calls) == 1 and out.getvalue() == ""


def test_a_relative_cwd_is_not_passed_on(env):
    calls = []
    payload = json.dumps({"session_id": SID, "cwd": "work/proj"})
    fl.main(io.StringIO(payload), io.StringIO(), env, spawn=lambda *a: calls.append(a))
    assert calls[0][3] == ""


# 2. + 3. body and idempotence -------------------------------------------
def test_body_shape_and_a_second_flush_sends_nothing(env):
    later = "2026-10-01T11:00:00+00:00"
    _append(env, te.index_events([19001, 19002], later))
    _append(env, te.index_events([19001], TS) + te.guard_events("rows", ["feedback_a"], TS))
    post = Recorder()
    status = _run(env, post)
    assert status.startswith("ok events=3 inserted=1")  # four lines, three merged events
    assert len(post.bodies) == 1
    body = post.bodies[0]
    assert set(body) == {"session_id", "root", "events"}
    assert body["session_id"] == SID and body["root"] == "-work-proj-demo"
    assert body["events"] == [  # merged: 19001 once, at its earliest time
        {"kind": "recall", "mv_id": 19001, "ts": TS, "sources": ["index"]},
        {"kind": "use", "path": "feedback_a.md", "ts": TS, "sources": ["guard_rows"]},
        {"kind": "recall", "mv_id": 19002, "ts": later, "sources": ["index"]},
    ]
    assert post.tokens == [TOKEN]
    # a repeated Stop: nothing new, nothing sent
    assert _run(env, post).startswith("ok:nothing")
    assert len(post.bodies) == 1
    # new lines are sent once
    _append(env, te.guard_events("deny", ["feedback_b"], later))
    _run(env, post)
    assert [e.get("path") for e in post.bodies[1]["events"]] == ["feedback_b.md"]


def test_the_root_comes_from_the_session_cwd(env):
    env = {k: v for k, v in env.items() if k != "NOBLIVION_RECALL_ROOT"}
    _append(env, te.index_events([5], TS))
    post = Recorder()
    fl.run_child(SID, "", env, post=post, refresh=_no_refresh, cwd="/work/proj.x", connect=_connect)
    assert post.bodies[0]["root"] == "-work-proj-x"
    # The root is kept in the session state for a later retry.
    assert fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["root"] == "-work-proj-x"


def test_batches_hold_at_most_500_events_and_256_kb():
    events = [{"mv_id": i, "kind": "recall", "ts": TS} for i in range(1, 1201)]
    bodies = fl.batches(SID, events, root="-r")
    assert [len(json.loads(b)["events"]) for b in bodies] == [500, 500, 200]
    assert all(json.loads(b)["root"] == "-r" for b in bodies)
    big = [{"path": ("p" * 180) + f"{i:06d}.md", "kind": "use", "ts": TS} for i in range(3000)]
    bodies = fl.batches(SID, big, max_events=10_000)
    assert len(bodies) > 1 and all(len(b) <= fl.MAX_BYTES for b in bodies)
    assert sum(len(json.loads(b)["events"]) for b in bodies) == 3000


# 4. fail open -----------------------------------------------------------
def test_timeout_keeps_the_offset_and_the_next_flush_sends_again(env):
    _append(env, te.index_events([5], TS))
    failing = Recorder(exc=fl.PostError("timeout"))
    assert _run(env, failing).startswith("fail:timeout")
    assert fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["sent"] == 0
    ok = Recorder()
    assert _run(env, ok).startswith("ok events=1")
    assert ok.bodies[0]["events"] == [
        {"mv_id": 5, "kind": "recall", "ts": TS, "sources": ["index"]}
    ]
    sent = fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["sent"]
    assert sent == _events_path(env).stat().st_size


@pytest.mark.parametrize("answer", [["not", "a", "dict"], {"inserted": "1"}, {"inserted": True}])
def test_a_bad_answer_is_a_failure(env, answer):
    _append(env, te.index_events([5], TS))
    assert _run(env, Recorder(answer=answer)).startswith("fail:bad_answer")
    assert fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["sent"] == 0


def test_a_store_that_is_down_is_never_sent_to(env):
    _append(env, te.index_events([5], TS))
    post = Recorder()
    assert _run(env, post, connect=_down).startswith("fail:store_down")
    assert post.bodies == [] and fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["sent"] == 0


def test_no_store_json_means_store_down(env):
    """The real client: no ``store.json`` in the data dir, no request at all."""
    _append(env, te.index_events([5], TS))
    status = fl.run_child(SID, "", env, refresh=_no_refresh)
    assert status.startswith("fail:store_down")


def test_http_json_refuses_anything_but_the_loopback_literal():
    for url in ("http://localhost:8894/x", "https://127.0.0.1:8894/x", "http://10.1.2.3:8894/x"):
        with pytest.raises(fl.PostError) as err:
            fl.http_json("POST", url, TOKEN, b"{}", 0.5)
        assert err.value.reason == "not_loopback"


class _FakeStore(http.server.BaseHTTPRequestHandler):
    """A store stand-in: answers the health proof for TOKEN, delays POSTs."""

    delay = 3.0
    token = TOKEN
    seen: list[dict[str, Any]] = []

    def do_GET(self):  # noqa: N802 - the http.server name
        parts = urlsplit(self.path)
        nonce = parse_qs(parts.query).get("nonce", [""])[0]
        msg = ("noblivion-health:" + nonce).encode()
        proof = hmac.new(type(self).token.encode(), msg, hashlib.sha256).hexdigest()
        self._answer({"status": "ok", "service": "noblivion", "version": "0", "proof": proof})

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)}
        )
        time.sleep(type(self).delay)
        self._answer({"inserted": 1, "duplicate": 0, "unknown": 0, "rejected": 0})

    def _answer(self, doc):
        data = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            return

    def log_message(self, *a):
        return


@pytest.fixture
def fake_store(env):
    _FakeStore.seen = []
    _FakeStore.delay = 3.0
    _FakeStore.token = TOKEN
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeStore)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    data = Path(env["NOBLIVION_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "token").write_text(TOKEN + "\n")
    (data / "store.json").write_text(json.dumps({"pid": 1, "port": srv.server_address[1]}))
    yield srv
    srv.shutdown()
    srv.server_close()


def test_real_post_times_out_on_the_total_budget(fake_store):
    url = f"http://127.0.0.1:{fake_store.server_address[1]}/api/memory/feedback/batch"
    t0 = time.monotonic()
    with pytest.raises(fl.PostError) as err:
        fl.http_json("POST", url, "k", b'{"session_id": "s", "events": []}', 0.5)
    assert err.value.reason == "timeout" and time.monotonic() - t0 < 2.0


def test_a_listener_that_fails_the_proof_gets_no_token(env, fake_store):
    _FakeStore.token = "cd" * 32  # it does not hold our token
    _append(env, te.index_events([5], TS))
    status = fl.run_child(SID, "", env, refresh=_no_refresh)
    assert status.startswith("fail:foreign_listener")
    assert _FakeStore.seen == []


def test_hook_subprocess_returns_at_once_and_the_worker_posts(env, fake_store):
    _append(env, te.guard_events("deny", ["feedback_x"], TS))
    payload = json.dumps({"session_id": SID, "transcript_path": "", "cwd": "/work/proj/demo"})
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(HOOKS / "trust_flush.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )
    took = time.monotonic() - t0
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    # The worker holds no pipe of the hook: had it inherited stdout, run()
    # would wait for its POST timeout (2 s) before it saw the end of the pipe.
    assert took < fl.POST_TIMEOUT_S - 0.5, took
    deadline = time.monotonic() + 15
    while not _FakeStore.seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _FakeStore.seen and _FakeStore.seen[0]["auth"] == f"Bearer {TOKEN}"
    assert _FakeStore.seen[0]["body"] == {
        "session_id": SID,
        "root": "-work-proj-demo",
        "events": [{"path": "feedback_x.md", "kind": "use", "ts": TS, "sources": ["guard_deny"]}],
    }


# 5. fetches from the transcript -----------------------------------------
def _write(path: Path, records: list[dict[str, Any]], mode: str = "a") -> None:
    with open(path, mode, encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _call(tid: str, fid: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": "2026-10-01T10:00:00Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": "mcp__noblivion__noblivion_recall",
                    "input": {"fetch_id": fid},
                }
            ]
        },
    }


def _result(tid: str, mid: int) -> dict[str, Any]:
    text = f"GROUNDED MEMORY {mid}: n [feedback_n.md]\nbody"
    return {
        "type": "user",
        "timestamp": "2026-10-01T10:00:01Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


def test_transcript_fetches_become_use_events_once(tmp_path, env):
    tr = tmp_path / f"{SID}.jsonl"
    _write(tr, [_call("t1", 18046), _result("t1", 18046), _call("t2", 4)])
    sub = tmp_path / SID / "subagents"
    sub.mkdir(parents=True)
    _write(sub / "agent-a1.jsonl", [_call("s1", 17000), _result("s1", 17000)])
    post = Recorder()
    assert "fetch_uses=2" in _run(env, post, str(tr))
    assert sorted(e["mv_id"] for e in post.bodies[0]["events"]) == [17000, 18046]
    # the second Stop: the result of t2 (a fetch by rank 4) arrives now
    _write(tr, [_result("t2", 19999)])
    assert "fetch_uses=1" in _run(env, post, str(tr))
    assert [e["mv_id"] for e in post.bodies[1]["events"]] == [19999]
    # a third Stop reads nothing new
    assert "fetch_uses=0" in _run(env, post, str(tr))
    lines = _events_path(env).read_text().splitlines()
    assert sorted(json.loads(x)["mv_id"] for x in lines) == [17000, 18046, 19999]


# 6. retry and prune -----------------------------------------------------
def test_other_sessions_with_unsent_lines_are_retried_with_their_own_root(env):
    _append(env, te.index_events([1], TS), sid="older-session")
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    fl.save_state(cache, "older-session", {"sent": 0, "transcripts": {}, "root": "-other-root"})
    _append(env, te.index_events([2], TS))
    post = Recorder()
    status = _run(env, post)
    assert "others=1" in status
    roots = {b["session_id"]: b.get("root") for b in post.bodies}
    assert roots == {"older-session": "-other-root", SID: "-work-proj-demo"}


def test_prune_removes_old_sent_files_and_keeps_unsent_ones(env):
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    for sid in ("sent-old", "unsent-old", "unsent-ancient", SID):
        _append(env, te.index_events([1], TS), sid=sid)
    st = fl.load_state(cache, "sent-old")
    st["sent"] = _events_path(env, "sent-old").stat().st_size
    fl.save_state(cache, "sent-old", st)
    now = time.time()
    folder = Path(cache) / "by-session"
    for p in folder.iterdir():
        if p.name.startswith("unsent-ancient"):
            age = 40
        else:
            age = 8 if not p.name.startswith(SID) else 9
        os.utime(p, (now - age * 86400, now - age * 86400))
    removed = fl.prune(cache, SID, now)
    names = sorted(p.name for p in folder.iterdir())
    assert removed == 3
    assert names == [f"{SID}.trust-events.jsonl", "unsent-old.trust-events.jsonl"]


def test_report_refresh_is_daily_and_backs_off(env, monkeypatch):
    report = fl._load("trust_report")
    calls = []
    monkeypatch.setattr(report, "refresh", lambda e: calls.append(1))
    now = time.time()
    assert fl.refresh_report(env, now) == "refreshed" and len(calls) == 1
    assert fl.refresh_report(env, now + 60) == "skip:recent_attempt" and len(calls) == 1
    Path(env["NOBLIVION_TRUST_REPORT_FILE"]).write_text("{}")
    assert fl.refresh_report(env, time.time()) == "fresh"


# review findings of the reference implementation ------------------------
def test_a_failed_append_keeps_the_transcript_offset(tmp_path, env):
    """The fetch events of a failed append are read again, not lost."""
    tr = tmp_path / f"{SID}.jsonl"
    _write(tr, [_call("t1", 777), _result("t1", 777)])
    block = _events_path(env)
    block.mkdir(parents=True)  # a folder where the file must be: the append fails
    post = Recorder()
    status = _run(env, post, str(tr))
    assert "fetch_append_failed" in status and post.bodies == []
    assert str(tr) not in fl.load_state(env["NOBLIVION_RECALL_CACHE_DIR"], SID)["transcripts"]
    block.rmdir()
    assert "fetch_uses=1" in _run(env, post, str(tr))
    assert [e["mv_id"] for e in post.bodies[0]["events"]] == [777]


def test_the_retry_reads_the_other_sessions_state_under_its_lock(env, monkeypatch):
    """A state saved by that session's own worker just before the lock is
    not overwritten with the older copy."""
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    _append(env, te.index_events([1], TS), sid="other-session")
    _append(env, te.index_events([2], TS))
    size = _events_path(env, "other-session").stat().st_size
    newer = {"sent": size, "transcripts": {"/t/other.jsonl": {"offset": 99, "pending": {}}}}
    real_lock = fl.session_lock

    def lock(cache_, sid, wait_s=None):
        if sid == "other-session":
            fl.save_state(cache_, sid, newer)  # its own worker finished a moment ago
        return real_lock(cache_, sid, wait_s)

    monkeypatch.setattr(fl, "session_lock", lock)
    post = Recorder()
    assert fl.flush_others(cache, SID, env, post, connect=_connect) == 0
    assert post.bodies == [] and fl.load_state(cache, "other-session") == newer


def test_a_held_lock_skips_the_flush_without_the_long_wait(env, monkeypatch):
    monkeypatch.setattr(fl, "LOCK_WAIT_S", 0.2)
    _append(env, te.index_events([5], TS))
    post = Recorder()
    with fl.session_lock(env["NOBLIVION_RECALL_CACHE_DIR"], SID, 0.0) as held:
        assert held
        t0 = time.monotonic()
        assert _run(env, post) == "skip:locked"
        assert time.monotonic() - t0 < 2.0
    assert post.bodies == []
    log_text = (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "trust-flush.log").read_text()
    assert "skip:locked" in log_text


def test_a_replaced_events_file_is_sent_from_its_start(env):
    _append(env, te.index_events([1, 2, 3], TS))
    post = Recorder()
    _run(env, post)
    _events_path(env).write_text("")
    _append(env, te.index_events([9], TS))
    _run(env, post)
    assert [e["mv_id"] for e in post.bodies[-1]["events"]] == [9]


def test_a_rewritten_transcript_is_read_from_its_start(tmp_path, env):
    tr = tmp_path / f"{SID}.jsonl"
    _write(tr, [_call("t1", 18046), _result("t1", 18046), _call("t2", 5), _result("t2", 18047)])
    post = Recorder()
    _run(env, post, str(tr))
    _write(tr, [_call("t3", 19000), _result("t3", 19000)], mode="w")
    assert "fetch_uses=1" in _run(env, post, str(tr))
    assert [e["mv_id"] for e in post.bodies[-1]["events"]] == [19000]


def test_a_missing_module_is_logged_by_the_stop_hook(env, monkeypatch):
    def missing():
        raise ImportError("trust_events")

    monkeypatch.setattr(fl, "te", missing)
    calls = []
    payload = json.dumps({"session_id": SID, "transcript_path": ""})
    assert fl.main(io.StringIO(payload), io.StringIO(), env, spawn=lambda *a: calls.append(a)) == 0
    assert calls == []
    log_text = (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "trust-flush.log").read_text()
    assert "skip:no_module:ImportError" in log_text


def test_the_state_file_is_private(env):
    _append(env, te.index_events([5], TS))
    _run(env, Recorder())
    path = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "by-session" / f"{SID}.trust-flush.json"
    assert path.stat().st_mode & 0o777 == 0o600


# 7. end to end against a live store ---------------------------------------
def test_end_to_end_the_events_reach_a_live_store(tmp_path):
    from noblivion import store as store_mod
    from test_store import Running, make_store, wait_for, write_memory

    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(folder, "tests use venv", "Use the venv python.", "Run pytest with the venv.")
    run = Running(make_store(tmp_path))
    try:
        assert wait_for(lambda: run.store.index_state == store_mod.INDEX_IDLE)
        with run.store.connection() as conn:
            memory_id = conn.execute("SELECT id FROM memories").fetchone()[0]
        env = {
            "NOBLIVION_DATA_DIR": str(run.store.data_dir),
            "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
            "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
            "NOBLIVION_STORE_AUTOSTART": "0",
        }
        _append(env, te.index_events([memory_id], TS))
        _append(env, te.guard_events("rows", ["feedback_tests_use_venv"], TS))
        status = fl.run_child(SID, "", env, refresh=_no_refresh, cwd="/work/proj/demo")
        assert status.startswith("ok events=2 inserted=2 duplicate=0 unknown=0 rejected=0")
        # A second worker over the same lines (a lost offset): duplicates only.
        cache = env["NOBLIVION_RECALL_CACHE_DIR"]
        fl.save_state(cache, SID, dict(fl.load_state(cache, SID), sent=0))
        status = fl.run_child(SID, "", env, refresh=_no_refresh, cwd="/work/proj/demo")
        assert status.startswith("ok events=2 inserted=0 duplicate=2")
        with run.store.connection() as conn:
            row = conn.execute(
                "SELECT trials, use_pos FROM feedback WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        assert tuple(row) == (1, 1.0)
    finally:
        run.stop()
