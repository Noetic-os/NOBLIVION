# SPDX-License-Identifier: AGPL-3.0-or-later
"""The store process and its REST routes (design doc 0001, sections 3 and 4).

Fictional data only. No model download: a fake embedder. Every socket is on
127.0.0.1.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from noblivion import __version__, config, db, embedding, launcher, rest, store
from store_helpers import FakeEmbedder, add_memory

TOKEN_KEYS = {"status", "service", "version"}
FULL_HEALTH_KEYS = TOKEN_KEYS | {
    "uptime_s",
    "memories",
    "schema_version",
    "content_rev",
    "vector_rev",
    "embedding",
    "mode",
    "trust_ranking",
    "index_state",
    "index_blocked",
}
INDEX_ROW_KEYS = {
    "rank",
    "id",
    "title",
    "summary",
    "score",
    "fusion_score",
    "source",
    "source_type",
}


def write_memory(folder: Path, name: str, description: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\ntype: feedback\n---\n{body}\n"
    (folder / f"feedback_{name.replace(' ', '_')}.md").write_text(text, encoding="utf-8")


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def fake_service(embedder: FakeEmbedder | None = None) -> embedding.EmbeddingService:
    embedder = embedder or FakeEmbedder()
    settings = embedding.EmbeddingSettings(backend="fastembed", model=embedder.model)
    return embedding.EmbeddingService(settings, factory=lambda *a: embedder)


def make_store(tmp_path: Path, **overrides) -> store.Store:
    memory = tmp_path / "projects" / "-work-proj-demo" / "memory"
    settings = config.Settings(
        data_dir=tmp_path / "data",
        namespace="claude_code",
        memory_dirs=(memory,),
        delete_grace_days=14,
        archive_retention_days=90,
    )
    store_settings = config.StoreSettings(port=0, idle_exit_s=0, index_interval_s=0)
    store_settings = replace(store_settings, **overrides.pop("store_settings", {}))
    overrides.setdefault("embedding_service", fake_service())
    overrides.setdefault("labeller", None)
    return store.Store(settings, store_settings, **overrides)


class Running:
    """A store serving in a thread of the test process."""

    def __init__(self, st: store.Store) -> None:
        self.store = st
        self.exit_code: int | None = None
        st.open()
        st.start_background()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.exit_code = self.store.serve()

    @property
    def port(self) -> int:
        return self.store.port

    def request(self, method, path, *, token=True, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        if token:
            hdrs.setdefault("Authorization", f"Bearer {self.store.token}")
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.getheaders())
        finally:
            conn.close()

    def get(self, path, **kw):
        status, body, _ = self.request("GET", path, **kw)
        return status, body

    def stop(self) -> None:
        self.store.request_stop("test")
        self.thread.join(15)


@pytest.fixture
def running(tmp_path):
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    write_memory(
        folder,
        "tests use venv",
        "The system python has no pytest; use the project venv.",
        "Run pytest with the venv python of the project.",
    )
    write_memory(
        folder,
        "deploy checklist",
        "Check the release notes before a deploy.",
        "Read the changelog, then tag the release.",
    )
    run = Running(make_store(tmp_path))
    assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
    assert wait_for(lambda: run.store.embedding.state == embedding.STATE_READY)
    yield run
    run.stop()


def raw_request(port: int, data: bytes) -> tuple[int, dict]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


# -- health and the listener proof (sections 3.3, 4.7) ------------------------------


def test_health_short_form_without_token(running):
    status, body = running.get("/health", token=False)
    assert status == 200
    assert set(body) == TOKEN_KEYS
    assert body == {"status": "ok", "service": "noblivion", "version": __version__}
    assert running.get("/api/health", token=False)[1] == body


def test_health_proof_matches_the_token_and_a_wrong_token_fails(running):
    nonce = "0123456789abcdef0123456789abcdef"
    status, body = running.get(f"/health?nonce={nonce}", token=False)
    assert status == 200
    assert body["proof"] == launcher.health_proof(running.store.token, nonce)
    assert launcher.probe(running.port, running.store.token)["version"] == __version__
    assert launcher.probe(running.port, "f" * 64) is None


def test_health_with_a_bad_nonce_has_no_proof(running):
    assert "proof" not in running.get("/health?nonce=xyz", token=False)[1]


def test_health_full_form_with_token(running):
    status, body = running.get("/health")
    assert status == 200
    assert set(body) == FULL_HEALTH_KEYS
    assert body["memories"] == 2
    assert body["schema_version"] == db.latest_version()
    assert body["index_state"] == "idle"
    assert body["index_blocked"] is False
    assert body["trust_ranking"] == "off"
    assert set(body["embedding"]) == {"backend", "model", "dim", "state", "missing_vectors"}
    assert wait_for(lambda: running.get("/health")[1]["embedding"]["missing_vectors"] == 0)
    body = running.get("/health")[1]
    assert body["mode"] == "hybrid"
    assert body["embedding"]["state"] == "ready"
    assert body["embedding"]["dim"] == 16
    text = json.dumps(body)
    assert str(running.store.data_dir) not in text and "venv" not in text


def test_health_with_a_wrong_token_is_the_short_form(running):
    status, body = running.get("/health", token=False, headers={"Authorization": "Bearer nope"})
    assert status == 200 and set(body) == TOKEN_KEYS


# -- request checks (section 4.1) --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/memories/search?q=venv",
        "/api/memories/index?q=venv",
        "/api/memories/fetch/1",
        "/api/memory/trust/report",
        "/no/such/route",
    ],
)
def test_routes_need_the_token(running, path):
    assert running.get(path, token=False) == (401, {"detail": "missing or wrong token"})
    wrong = {"Authorization": "Bearer " + "0" * 64}
    assert running.get(path, token=False, headers=wrong)[0] == 401
    assert running.get(path, token=False, headers={"Authorization": "Basic abc"})[0] == 401


def test_bad_host_is_421(running):
    status, body = running.get("/api/memories/search?q=x", headers={"Host": "evil.example:80"})
    assert (status, body) == (421, {"detail": "bad host"})
    status, _ = running.get("/health", token=False, headers={"Host": "evil.example"})
    assert status == 421
    assert running.get("/health", headers={"Host": f"localhost:{running.port}"})[0] == 200


def test_post_without_content_length_is_411(running):
    request = (
        f"POST /api/memory/feedback/batch HTTP/1.1\r\nHost: 127.0.0.1:{running.port}\r\n"
        f"Authorization: Bearer {running.store.token}\r\n"
        "Content-Type: application/json\r\nConnection: close\r\n\r\n"
    ).encode()
    assert raw_request(running.port, request) == (411, {"detail": "Content-Length is required"})


def test_post_checks(running):
    path = "/api/memory/feedback/batch"
    plain = {"Content-Type": "text/plain"}
    assert running.request("POST", path, body=b"{}", headers=plain)[0] == 415
    big = {"Content-Length": str(rest.MAX_BODY_BYTES + 1)}
    request = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{running.port}\r\n"
        f"Authorization: Bearer {running.store.token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {big['Content-Length']}\r\nConnection: close\r\n\r\n"
    ).encode()
    assert raw_request(running.port, request)[0] == 413
    assert running.request("POST", path, body=b"{not json")[:2] == (
        400,
        {"detail": "body is not valid JSON"},
    )
    assert running.request("POST", path, body=b"[1]")[:2] == (
        400,
        {"detail": "body must be a JSON object"},
    )


def test_query_string_cap(running):
    status, body = running.get("/api/memories/search?q=" + "a" * (rest.MAX_QUERY_BYTES + 1))
    assert (status, body) == (414, {"detail": "query string too long"})


def test_unknown_route_wrong_method_and_no_cors(running):
    assert running.get("/api/memories/other") == (404, {"detail": "not found"})
    assert running.get("/api/memories/fetch/1/extra")[0] == 404
    assert running.get("/api/memory/feedback/batch")[0] == 405
    assert running.request("POST", "/api/memories/search", body={})[0] == 405
    status, _, headers = running.request(
        "OPTIONS", "/api/memories/search", headers={"Origin": "http://evil.example"}
    )
    assert status == 405
    assert not any(k.lower().startswith("access-control-") for k in headers)
    assert headers["Content-Type"].startswith("application/json")


# -- search (section 4.2) -----------------------------------------------------------


def test_search_shape(running):
    status, body = running.get("/api/memories/search?q=pytest+venv&top_k=1")
    assert status == 200
    assert set(body) == {"results", "namespace"}
    assert body["namespace"] == "claude_code"
    assert len(body["results"]) == 1
    entries = body["results"][0].split(rest.ENTRY_SEPARATOR)
    assert len(entries) == 1
    assert entries[0].startswith("# tests use venv\n")
    assert "[claude_code_md: feedback_tests_use_venv.md]" in entries[0]


def test_search_joins_entries(running):
    body = running.get("/api/memories/search?q=release+venv+pytest&top_k=50")[1]
    entries = body["results"][0].split(rest.ENTRY_SEPARATOR)
    assert len(entries) == 2
    assert all("[claude_code_md: " in e for e in entries)


@pytest.mark.parametrize("query", ["", "q=", "q=+++"])
def test_search_empty_query(running, query):
    body = running.get(f"/api/memories/search?{query}")[1]
    assert body == {"results": ["No memories available."], "namespace": "claude_code"}


def test_search_echoes_the_project_and_scopes_by_it(running):
    body = running.get("/api/memories/search?q=venv&project=other_space")[1]
    assert body == {"results": ["No memories available."], "namespace": "other_space"}


def test_search_root_scoping(running):
    body = running.get("/api/memories/search?q=venv&root=-work-other")[1]
    assert body["results"] == ["No memories available."]
    body = running.get("/api/memories/search?q=venv&root=-work-proj-demo")[1]
    assert "[claude_code_md: " in body["results"][0]


# -- index (section 4.3) ------------------------------------------------------------


def test_index_shape(running):
    status, body = running.get("/api/memories/index?q=pytest+venv")
    assert status == 200
    assert set(body) == {"namespace", "reason", "mode", "results"}
    assert body["namespace"] == "claude_code"
    assert body["reason"] is None
    assert body["mode"] in ("hybrid", "keyword")
    rows = body["results"]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    top = rows[0]
    assert set(top) == INDEX_ROW_KEYS
    assert isinstance(top["id"], int)
    assert top["title"] == "tests use venv"
    assert top["summary"] == "The system python has no pytest; use the project venv."
    assert top["source"] == "feedback_tests_use_venv.md"
    assert top["source_type"] == "claude_code_md"
    assert isinstance(top["fusion_score"], float)
    assert top["score"] is None or -1.0 <= top["score"] <= 1.0


def test_index_empty_query(running):
    body = running.get("/api/memories/index?q=")[1]
    assert body["results"] == [] and body["reason"] == "No memories available."


def test_index_top_k_clamp_and_non_number(running):
    assert len(running.get("/api/memories/index?q=venv+release&top_k=0")[1]["results"]) == 1
    assert len(running.get("/api/memories/index?q=venv+release&top_k=abc")[1]["results"]) == 2


def test_index_include_mined(tmp_path):
    run = Running(make_store(tmp_path))
    try:
        with closing(db.connect(run.store.settings.db_path)) as conn:
            add_memory(conn, "mined note", "kubectl rollout restart", source_type=db.SOURCE_MINED)
        plain = run.get("/api/memories/index?q=rollout+restart")[1]
        assert plain["results"] == []
        mined = run.get("/api/memories/index?q=rollout+restart&include_mined=1")[1]
        assert [r["source_type"] for r in mined["results"]] == ["transcript_mined"]
        search = run.get("/api/memories/search?q=rollout+restart")[1]
        assert search["results"] == ["No memories available."]
    finally:
        run.stop()


def test_index_trust_fields_in_shadow_mode(tmp_path):
    run = Running(make_store(tmp_path, store_settings={"trust_ranking": "shadow"}))
    try:
        with closing(db.connect(run.store.settings.db_path)) as conn:
            a = add_memory(conn, "alpha rule", "alpha bravo charlie")
            add_memory(conn, "alpha mined", "alpha bravo", source_type=db.SOURCE_MINED)
            with db.write_tx(conn):
                conn.execute(
                    "INSERT INTO feedback (memory_id, trust_0, trials, trust_score) "
                    "VALUES (?, 0.5, 6, 0.71234)",
                    (a,),
                )
        rows = run.get("/api/memories/index?q=alpha+bravo&include_mined=1")[1]["results"]
        by_type = {r["source_type"]: r for r in rows}
        md, mined = by_type["claude_code_md"], by_type["transcript_mined"]
        assert set(md) == INDEX_ROW_KEYS | {"trust", "trials", "trust_prior"}
        assert (md["trust"], md["trials"], md["trust_prior"]) == (0.7123, 6, 0.5)
        assert (mined["trust"], mined["trials"], mined["trust_prior"]) == (None, None, 0.3)
    finally:
        run.stop()


def test_keyword_mode_has_null_scores(tmp_path):
    service = embedding.EmbeddingService(embedding.EmbeddingSettings(backend="none"))
    run = Running(make_store(tmp_path, embedding_service=service))
    try:
        with closing(db.connect(run.store.settings.db_path)) as conn:
            add_memory(conn, "alpha rule", "alpha bravo charlie")
        body = run.get("/api/memories/index?q=alpha")[1]
        assert body["mode"] == "keyword"
        assert body["results"][0]["score"] is None
        health = run.get("/health")[1]
        assert health["mode"] == "keyword" and health["embedding"]["state"] == "off"
    finally:
        run.stop()


def test_ranker_fault_answers_200_with_the_fixed_reason(running, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(running.store.ranker, "search", boom)
    body = running.get("/api/memories/index?q=venv")[1]
    assert body["results"] == [] and body["reason"] == rest.INTERNAL_REASON
    body = running.get("/api/memories/search?q=venv")[1]
    assert body["results"] == ["No memories available."]
    assert body["reason"] == rest.INTERNAL_REASON


# -- fetch (section 4.4) ------------------------------------------------------------


def test_fetch_found(running):
    top = running.get("/api/memories/index?q=pytest+venv")[1]["results"][0]
    status, body = running.get(f"/api/memories/fetch/{top['id']}")
    assert status == 200
    assert set(body) == {"namespace", "id", "title", "source", "text", "reason"}
    assert body["id"] == top["id"] and body["reason"] is None
    assert body["title"] == "tests use venv"
    assert body["source"] == "feedback_tests_use_venv.md"
    assert "Run pytest with the venv python" in body["text"]


def test_fetch_unknown_id(running):
    status, body = running.get("/api/memories/fetch/987654")
    assert status == 200
    assert body == {
        "namespace": "claude_code",
        "id": 987654,
        "title": "",
        "source": None,
        "text": "",
        "reason": "no memory with that id in this namespace",
    }
    assert running.get("/api/memories/fetch/99999999999999999999999")[1]["reason"] == (
        "no memory with that id in this namespace"
    )


def test_fetch_id_not_a_number(running):
    body = running.get("/api/memories/fetch/abc")[1]
    assert body["id"] == "abc" and body["reason"] == "id is not a number"
    assert running.get("/api/memories/fetch/")[1]["reason"] == "id is not a number"


def test_fetch_other_namespace_and_archived(running):
    top = running.get("/api/memories/index?q=pytest+venv")[1]["results"][0]
    body = running.get(f"/api/memories/fetch/{top['id']}?project=other_space")[1]
    assert body["namespace"] == "other_space" and body["reason"].startswith("no memory")
    with closing(db.connect(running.store.settings.db_path)) as conn, db.write_tx(conn):
        rev = db.bump_rev(conn)
        conn.execute(
            "UPDATE memories SET archived_at = ?, rev = ? WHERE id = ?",
            (db.utc_now(), rev, top["id"]),
        )
    assert running.get(f"/api/memories/fetch/{top['id']}")[1]["reason"].startswith("no memory")


# -- answer helpers -----------------------------------------------------------------


def test_search_answer_stays_below_the_cap():
    from noblivion.ranking import Hit, PoolRow, RankResult

    big = "# big\n\n[claude_code_md: big.md]\n\n" + "word " * 40000
    row = PoolRow(1, "claude_code", "r", "big.md", "claude_code_md", big, 1.0, ())
    hits = [Hit(i + 1, row, 0.5, 0.1, 1.0) for i in range(10)]
    answer = rest.search_answer(RankResult("hybrid", hits, 10), "claude_code")
    size = len(json.dumps(answer, ensure_ascii=False).encode())
    assert size < rest.ANSWER_CAP_BYTES
    assert answer["results"][0].count(rest.ENTRY_SEPARATOR) == 1  # 2 of 10 entries fit


def test_title_rules():
    content = "# " + "x" * 200 + "\n\n[claude_code_md: a.md]\n\nsecond   line\n"
    title, summary = rest.title_and_summary(content, 7)
    assert len(title) == 120 and title.endswith("…")
    assert summary == "second line"
    assert rest.title_and_summary("[claude_code_md: a.md]\n", 7) == ("memory 7", "")


# -- lifecycle (section 3) ----------------------------------------------------------


def test_store_json_and_token_files(tmp_path):
    run = Running(make_store(tmp_path))
    data = run.store.data_dir
    info = json.loads((data / "store.json").read_text())
    assert set(info) == {"pid", "port", "version", "started_at"}
    assert info["pid"] == os.getpid() and info["port"] == run.port
    assert info["version"] == __version__
    for name in ("store.json", "token"):
        assert stat.S_IMODE((data / name).stat().st_mode) == 0o600
    assert launcher.read_token(data) == run.store.token
    assert launcher.read_store_json(data)["port"] == run.port
    run.stop()
    assert run.exit_code == 0
    assert not (data / "store.json").exists()
    assert (data / "token").read_text().strip() == run.store.token  # kept for the next start


def test_existing_token_is_kept(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "token").write_text("ab" * 32 + "\n")
    assert store.ensure_token(data) == "ab" * 32
    (data / "token").write_text("short\n")
    token = store.ensure_token(data)
    assert launcher.TOKEN_RE.fullmatch(token) and token != "short"


def test_non_loopback_bind_is_refused(tmp_path):
    st = make_store(tmp_path)
    with pytest.raises(store.BindRefusedError):
        store.make_server("0.0.0.0", 0, st)
    st = make_store(tmp_path, host="0.0.0.0")
    with pytest.raises(store.BindRefusedError):
        st.open()
    assert not (st.data_dir / "store.json").exists()


def test_the_server_binds_loopback(running):
    assert running.store.host == "127.0.0.1"
    assert running.store.server.server_address[0] == "127.0.0.1"


def test_second_store_is_refused_by_the_lock(tmp_path):
    run = Running(make_store(tmp_path))
    try:
        second = make_store(tmp_path)
        with pytest.raises(store.StoreLockedError):
            second.open()
        assert json.loads((run.store.data_dir / "store.json").read_text())["port"] == run.port
    finally:
        run.stop()
    third = make_store(tmp_path)  # the lock is free again
    third.open()
    third.close()


def test_port_in_use_fails_and_releases_the_lock(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        st = make_store(tmp_path, store_settings={"port": port})
        with pytest.raises(OSError):
            st.open()
    st = make_store(tmp_path)
    st.open()
    st.close()


def test_idle_exit(tmp_path):
    run = Running(make_store(tmp_path, store_settings={"idle_exit_s": 1}))
    data = run.store.data_dir
    assert (data / "store.json").exists()
    run.thread.join(10)
    assert not run.thread.is_alive()
    assert run.store.stop_reason == "idle"
    assert run.exit_code == 0
    assert not (data / "store.json").exists()


def test_requests_keep_the_store_alive(tmp_path):
    run = Running(make_store(tmp_path, store_settings={"idle_exit_s": 1}))
    try:
        for _ in range(6):
            assert run.get("/health", token=False)[0] == 200
            time.sleep(0.3)
        assert run.thread.is_alive()
    finally:
        run.stop()


def test_background_scan_and_backfill_feed_search(tmp_path):
    folder = tmp_path / "projects" / "-work-proj-demo" / "memory"
    embedder = FakeEmbedder()
    run = Running(
        make_store(
            tmp_path,
            embedding_service=fake_service(embedder),
            store_settings={"index_interval_s": 1},
        )
    )
    try:
        assert run.get("/api/memories/index?q=lantern")[1]["results"] == []
        write_memory(folder, "lantern rule", "Light the lantern first.", "Lantern before dusk.")
        assert wait_for(lambda: len(run.get("/api/memories/index?q=lantern")[1]["results"]) == 1)
        assert wait_for(lambda: run.get("/health")[1]["embedding"]["missing_vectors"] == 0)
        assert embedder.calls  # the backfill embedded the new row
    finally:
        run.stop()


def test_answers_while_the_model_loads(tmp_path):
    gate = threading.Event()

    class SlowEmbedder(FakeEmbedder):
        def load(self) -> None:
            gate.wait(10)
            super().load()

    run = Running(make_store(tmp_path, embedding_service=fake_service(SlowEmbedder())))
    try:
        with closing(db.connect(run.store.settings.db_path)) as conn:
            add_memory(conn, "alpha rule", "alpha bravo")
        body = run.get("/api/memories/index?q=alpha")[1]
        assert body["mode"] == "keyword" and len(body["results"]) == 1
        assert run.get("/health")[1]["embedding"]["state"] == "loading"
    finally:
        gate.set()
        run.stop()


# -- ensure-running (section 3.2) ----------------------------------------------------


def test_ensure_running_starts_once_then_waits(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    calls = []
    spawn = lambda data_dir, lock_wait_s=0.0: calls.append(lock_wait_s)  # noqa: E731
    assert launcher.ensure_running(env, spawn=spawn) == "started"
    assert launcher.ensure_running(env, spawn=spawn) == "starting"
    assert calls == [0.0]
    later = lambda: time.time() + 31  # noqa: E731
    assert launcher.ensure_running(env, spawn=spawn, clock=later) == "started"


def test_ensure_running_sees_a_running_store(tmp_path):
    run = Running(make_store(tmp_path))
    try:
        env = {"NOBLIVION_DATA_DIR": str(run.store.data_dir)}
        assert launcher.ensure_running(env, spawn=lambda *a, **k: pytest.fail("spawned")) == (
            "running"
        )
    finally:
        run.stop()


def test_ensure_running_replaces_a_store_of_another_version(tmp_path, monkeypatch):
    run = Running(make_store(tmp_path))
    killed, spawned = [], []
    monkeypatch.setattr(launcher, "__version__", "9.9.9")
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    try:
        env = {"NOBLIVION_DATA_DIR": str(run.store.data_dir)}
        state = launcher.ensure_running(
            env, spawn=lambda d, lock_wait_s=0.0: spawned.append(lock_wait_s)
        )
        assert state == "started"
        assert killed == [(os.getpid(), signal.SIGTERM)]
        assert spawned == [launcher.RESTART_LOCK_WAIT_S]
    finally:
        run.stop()


def test_ensure_running_ignores_a_foreign_listener(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "token").write_text("cd" * 32)
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen(1)
        port = foreign.getsockname()[1]
        (data / "store.json").write_text(json.dumps({"pid": 4242, "port": port, "version": "x"}))
        spawned = []
        state = launcher.ensure_running(
            {"NOBLIVION_DATA_DIR": str(data)}, spawn=lambda d, lock_wait_s=0.0: spawned.append(1)
        )
    assert state == "started" and spawned == [1]


# -- the real process: SIGTERM and spawn ---------------------------------------------


def store_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOBLIVION_")}
    env.update(
        NOBLIVION_DATA_DIR=str(tmp_path / "data"),
        NOBLIVION_PORT="0",
        NOBLIVION_EMBED_BACKEND="none",
        NOBLIVION_MEMORY_DIRS=str(tmp_path / "memory"),
        NOBLIVION_IDLE_EXIT_S="60",
        HOME=str(tmp_path),
    )
    env.pop("CLAUDE_PLUGIN_DATA", None)
    return env


def test_process_serves_and_stops_on_sigterm(tmp_path):
    env = store_env(tmp_path)
    data = tmp_path / "data"
    child = subprocess.Popen(
        [sys.executable, "-m", "noblivion", "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_for(lambda: launcher.read_store_json(data) is not None, 20)
        info = launcher.read_store_json(data)
        assert info["pid"] == child.pid
        token = launcher.read_token(data)
        assert launcher.probe(info["port"], token, timeout=2)["version"] == __version__
        # A second process finds the lock held and exits 0 at once.
        second = subprocess.run(
            [sys.executable, "-m", "noblivion.store"], env=env, timeout=30, check=False
        )
        assert second.returncode == 0
        child.send_signal(signal.SIGTERM)
        assert child.wait(20) == 0
        assert not (data / "store.json").exists()
        assert "listening on port" in (data / "logs" / "store.log").read_text()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def test_spawn_store_starts_a_detached_process(tmp_path, monkeypatch):
    env = store_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    data = tmp_path / "data"
    pid = launcher.spawn_store(data)
    try:
        assert wait_for(lambda: launcher.read_store_json(data) is not None, 20)
        assert launcher.read_store_json(data)["pid"] == pid
        assert os.getsid(pid) == pid  # its own session: detached from the caller
    finally:
        os.kill(pid, signal.SIGTERM)
        assert wait_for(lambda: launcher.read_store_json(data) is None, 20)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def test_sigterm_right_after_store_json_stops_cleanly(tmp_path, monkeypatch):
    """NOBLIVION-20: a SIGTERM sent as soon as store.json exists must reach the
    handler, so the store exits 0 and deletes store.json. Before the fix the
    handler was installed after open() wrote store.json; about 1 in 10 such
    SIGTERMs killed the process and left a stale store.json."""
    for attempt in range(8):
        base = tmp_path / f"run{attempt}"
        for key, value in store_env(base).items():
            monkeypatch.setenv(key, value)
        data = base / "data"
        pid = launcher.spawn_store(data)
        deadline = time.monotonic() + 30
        while launcher.read_store_json(data) is None and time.monotonic() < deadline:
            time.sleep(0.0005)
        os.kill(pid, signal.SIGTERM)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (attempt, status)
        assert launcher.read_store_json(data) is None, attempt


# -- config keys of the store (section 12.3) -----------------------------------------


def test_store_settings_from_env_file_and_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "port": 9000,
                "idle_exit_s": 60,
                "index": {"interval_s": 5},
                "recall": {"shared_roots": ["-work-shared", ""]},
                "trust.ranking": "shadow",
                "trust": {"prior_mined": 0.25},
            }
        )
    )
    env = {"NOBLIVION_CONFIG": str(cfg), "NOBLIVION_PORT": "0"}
    got = config.load_store_settings(env)
    assert got == config.StoreSettings(
        port=0,
        idle_exit_s=60,
        index_interval_s=5,
        shared_roots=("-work-shared",),
        trust_ranking="shadow",
        prior_mined=0.25,
    )
    bad = {"NOBLIVION_CONFIG": str(tmp_path / "none.json"), "NOBLIVION_PORT": "70000"}
    bad.update(NOBLIVION_IDLE_EXIT_S="-1", NOBLIVION_TRUST_RANKING="loud")
    assert config.load_store_settings(bad) == config.StoreSettings()
