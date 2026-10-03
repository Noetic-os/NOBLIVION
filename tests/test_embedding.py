# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding backends, consent gate and backfill (design doc 0001, section 7).

No test downloads a model or leaves the machine. Remote backends talk to a
fake server on 127.0.0.1. The one real-model test runs only when
``NOBLIVION_TEST_REAL_MODEL=1`` and fastembed is installed.
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from noblivion import db, embedding
from store_helpers import FAKE_DIM, FakeEmbedder, add_memory, change_memory, fake_vector


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db")
    yield c
    c.close()


def vectors(conn):
    cur = conn.execute("SELECT memory_id, model, dim, content_hash, blob, rev FROM vectors")
    return {(r["memory_id"], r["model"]): dict(r) for r in cur.fetchall()}


# -- text and blobs -------------------------------------------------------------------


def test_blob_is_normalised_float32_little_endian():
    blob = embedding.vector_to_blob([3.0, 4.0])
    assert len(blob) == 2 * 4
    assert embedding.blob_to_vector(blob) == pytest.approx([0.6, 0.8])
    assert blob == bytes.fromhex("9a99193fcdcc4c3f")  # 0.6f, 0.8f little-endian


def test_zero_or_non_finite_vectors_are_refused():
    for bad in ([0.0, 0.0], [], [math.nan, 1.0]):
        with pytest.raises(embedding.EmbeddingError):
            embedding.vector_to_blob(bad)


def test_embed_text_drops_the_source_marker():
    text = "# tests_use_venv\n\nRun tests.\n\n[claude_code_md: tests_use_venv.md]\n\nBody line."
    assert embedding.embed_text(text) == "# tests_use_venv\n\nRun tests.\n\n\nBody line."
    assert len(embedding.embed_text("x" * 20000)) == embedding.MAX_EMBED_CHARS


def test_scrub_outbound():
    text = "see /home/alice/proj, ask alice at 10.1.2.3 or fe80::1:2:3, alice2 stays at 12:30:45"
    out = embedding.scrub_outbound(text, home="/home/alice", user="alice")
    assert out == "see ~/proj, ask <user> at <ip> or <ip>, alice2 stays at 12:30:45"
    assert embedding.scrub_outbound("loop ::1 and 2001:db8::7", home="", user="") == (
        "loop <ip> and <ip>"
    )
    assert "sk-" not in embedding.scrub_outbound("token sk-ant-api03-" + "a" * 40, home="", user="")


# -- settings ---------------------------------------------------------------------------


def test_settings_defaults_and_env(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path)}
    s = embedding.load_embedding_settings(env)
    assert (s.backend, s.model, s.allow_remote) == ("fastembed", "BAAI/bge-small-en-v1.5", False)
    assert s.models_dir == tmp_path / "models"
    env.update(NOBLIVION_EMBED_BACKEND="OpenRouter", NOBLIVION_EMBED_MODEL="vendor/embed-1")
    s = embedding.load_embedding_settings(env)
    assert (s.backend, s.model, s.model_id) == (
        "openrouter",
        "vendor/embed-1",
        "openrouter:vendor/embed-1",
    )
    assert embedding.load_embedding_settings({**env, "NOBLIVION_EMBED_BACKEND": "bad"}).backend == (
        "fastembed"
    )


def test_settings_from_the_config_file(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"embedding": {"backend": "ollama", "allow_remote": True}}), encoding="utf-8"
    )
    s = embedding.load_embedding_settings({"NOBLIVION_DATA_DIR": str(tmp_path)})
    assert (s.backend, s.allow_remote, s.remote_include_mined) == ("ollama", True, False)


# -- backfill and model change ----------------------------------------------------------


def test_backfill_writes_vectors_with_model_dim_and_hash(conn):
    a = add_memory(conn, "a", "alpha beta")
    b = add_memory(conn, "b", "gamma delta")
    embedder = FakeEmbedder()
    _, rev_before = db.revisions(conn)
    result = embedding.backfill(conn, embedder)
    assert (result.embedded, result.failed, result.switched) == (2, 0, True)
    rows = vectors(conn)
    assert set(rows) == {(a, "fake:fake-model"), (b, "fake:fake-model")}
    row = rows[(a, "fake:fake-model")]
    assert row["dim"] == FAKE_DIM and len(row["blob"]) == FAKE_DIM * 4
    text = embedding.embed_text(conn.execute("SELECT content FROM memories WHERE id = ?", (a,))
                                .fetchone()[0])  # fmt: skip
    assert row["content_hash"] == embedding.text_hash(text)
    assert row["rev"] == db.revisions(conn)[1] > rev_before
    meta = {k: db.get_meta(conn, k) for k in ("embed_backend", "embed_model", "embed_dim")}
    assert meta == {"embed_backend": "fake", "embed_model": "fake-model", "embed_dim": "16"}
    # A second pass has nothing to do.
    assert embedding.backfill(conn, embedder).embedded == 0


def test_backfill_reembeds_a_changed_row_only(conn):
    a = add_memory(conn, "a", "alpha beta")
    add_memory(conn, "b", "gamma delta")
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    change_memory(conn, a, "alpha epsilon")
    embedder.calls.clear()
    assert embedding.backfill(conn, embedder).embedded == 1
    assert len(embedder.calls) == 1 and "epsilon" in embedder.calls[0][0]


def test_backfill_batches_and_isolates_a_failing_row(conn):
    ids = [add_memory(conn, f"m{i}", f"row number {i}") for i in range(5)]
    bad = add_memory(conn, "poison", "this row breaks the model")
    embedder = FakeEmbedder(fail_on="breaks")
    result = embedding.backfill(conn, embedder, batch_size=2)
    assert (result.embedded, result.failed) == (5, 1)
    stored = {m for m, _ in vectors(conn)}
    assert stored == set(ids) and bad not in stored


def test_backfill_skips_mined_rows_when_asked(conn):
    add_memory(conn, "a", "alpha")
    mined = add_memory(conn, "t", "tool output", source_type=db.SOURCE_MINED)
    result = embedding.backfill(conn, FakeEmbedder(), include_mined=False)
    assert (result.embedded, result.skipped_mined) == (1, 1)
    assert mined not in {m for m, _ in vectors(conn)}


def test_model_change_triggers_reembed_and_drops_old_vectors(conn):
    a = add_memory(conn, "a", "alpha beta")
    old = FakeEmbedder("old")
    embedding.backfill(conn, old)
    new = FakeEmbedder("new")
    assert embedding.needs_reembed(conn, new) is True
    assert embedding.needs_reembed(conn, old) is False
    partial = embedding.backfill(conn, new, finish=False)
    assert partial.embedded == 1 and not partial.switched
    assert set(vectors(conn)) == {(a, "fake:old"), (a, "fake:new")}  # old kept until the end
    assert embedding.finish_model_switch(conn, new) is True
    assert set(vectors(conn)) == {(a, "fake:new")}
    assert db.get_meta(conn, "embed_model") == "new"
    assert embedding.needs_reembed(conn, new) is False


def test_service_reports_reembedding_then_ready(conn):
    add_memory(conn, "a", "alpha beta")
    old = FakeEmbedder("old")
    embedding.backfill(conn, old)
    settings = embedding.EmbeddingSettings(model="new")
    service = embedding.EmbeddingService(settings, factory=lambda *a: FakeEmbedder("new"))
    assert service.start(conn) == "reembedding"
    assert service.model_id == "fake:new"
    service.backfill(conn)
    assert service.state == "ready"


def test_a_failed_load_logs_its_reason_once_per_failure(conn, caplog):
    now = [1000.0]

    def factory(*_a):
        raise embedding.EmbeddingError("model file missing at /m/x.onnx")

    service = embedding.EmbeddingService(
        embedding.EmbeddingSettings(), factory=factory, clock=lambda: now[0]
    )
    with caplog.at_level("WARNING", logger="noblivion.store"):
        service.start(conn)
        now[0] += 60
        service.start(conn)  # inside the backoff: no attempt, no new line
        lines = [r.getMessage() for r in caplog.records if "model load failed" in r.getMessage()]
        assert len(lines) == 1
        assert "EmbeddingError" in lines[0] and "model file missing at /m/x.onnx" in lines[0]
        now[0] += 3600
        service.start(conn)
        lines = [r.getMessage() for r in caplog.records if "model load failed" in r.getMessage()]
        assert len(lines) == 2
    service.close()


def test_service_retries_a_failed_load_at_most_once_per_hour(conn):
    now = [1000.0]
    attempts = []

    def factory(*_a):
        attempts.append(now[0])
        if len(attempts) < 3:
            raise embedding.EmbeddingError("not yet")
        return FakeEmbedder()

    service = embedding.EmbeddingService(
        embedding.EmbeddingSettings(), factory=factory, clock=lambda: now[0]
    )
    assert service.start(conn) == "failed" and service.model_id is None
    now[0] += 60
    assert service.start(conn) == "failed" and len(attempts) == 1
    now[0] += 3600
    assert service.start(conn) == "failed" and len(attempts) == 2
    now[0] += 3600
    assert service.start(conn) in ("ready", "reembedding") and len(attempts) == 3
    assert service.embed_query("alpha") == pytest.approx(embedding.normalize(fake_vector("alpha")))
    service.close()


# -- consent gate ---------------------------------------------------------------------


def test_openrouter_needs_consent_for_the_same_model(conn):
    s = embedding.EmbeddingSettings(backend="openrouter", model="vendor/embed-1")
    env = {"OPENROUTER_API_KEY": "test-key-not-real"}
    with pytest.raises(embedding.ConsentRequiredError):
        embedding.make_embedder(s, conn, env)
    embedding.grant_consent(conn, "openrouter", "vendor/other")
    with pytest.raises(embedding.ConsentRequiredError):
        embedding.make_embedder(s, conn, env)
    embedding.grant_consent(conn, "openrouter", "vendor/embed-1")
    made = embedding.make_embedder(s, conn, env)
    assert made.remote and made.model_id == "openrouter:vendor/embed-1"
    assert "test-key" not in repr(made)
    embedding.revoke_consent(conn)
    with pytest.raises(embedding.ConsentRequiredError):
        embedding.make_embedder(s, conn, env)


def test_consent_of_an_old_text_version_is_not_consent(conn):
    with db.write_tx(conn):
        db.set_meta(conn, "embed_consent", json.dumps(
            {"version": 0, "provider": "openrouter", "model": "m", "at": "x"}))  # fmt: skip
    assert embedding.has_consent(conn, "openrouter", "m") is False


def test_openrouter_without_consent_leaves_the_service_failed_and_keyword_only(conn):
    s = embedding.EmbeddingSettings(backend="openrouter", model="vendor/embed-1")
    service = embedding.EmbeddingService(s, env={"OPENROUTER_API_KEY": "test-key-not-real"})
    assert service.start(conn) == "failed"
    assert "consent" in service.error
    assert service.model_id is None and service.embed_query("alpha") is None


def test_ollama_loopback_needs_no_consent_remote_needs_both_switches(conn):
    local = embedding.EmbeddingSettings(backend="ollama", model="embed-x")
    assert embedding.make_embedder(local, conn).remote is False
    far = embedding.EmbeddingSettings(
        backend="ollama", model="embed-x", ollama_url="http://192.0.2.10:11434"
    )
    with pytest.raises(embedding.EmbeddingError):
        embedding.make_embedder(far, conn)
    allowed = embedding.EmbeddingSettings(
        backend="ollama", model="embed-x", ollama_url="http://192.0.2.10:11434", allow_remote=True
    )
    with pytest.raises(embedding.ConsentRequiredError):
        embedding.make_embedder(allowed, conn)
    embedding.grant_consent(conn, "ollama", "embed-x")
    assert embedding.make_embedder(allowed, conn).remote is True


def test_backend_none_gives_no_embedder(conn):
    assert embedding.make_embedder(embedding.EmbeddingSettings(backend="none"), conn) is None


def test_consent_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOBLIVION_EMBED_BACKEND", "openrouter")
    monkeypatch.setenv("NOBLIVION_EMBED_MODEL", "vendor/embed-1")
    out = io.StringIO()
    assert embedding.consent_main([], stdin=io.StringIO("no\n"), stdout=out) == 1
    assert "sent as written" in out.getvalue()
    assert embedding.consent_main([], stdin=io.StringIO("yes\n"), stdout=io.StringIO()) == 0
    c = db.open_db(tmp_path / "noblivion.db")
    try:
        assert embedding.has_consent(c, "openrouter", "vendor/embed-1")
    finally:
        c.close()
    assert embedding.consent_main(["--revoke"], stdout=io.StringIO()) == 0
    c = db.open_db(tmp_path / "noblivion.db")
    try:
        assert embedding.read_consent(c) is None
    finally:
        c.close()


# -- HTTP backends against a fake loopback server ----------------------------------------


class _Fake(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, dict(self.headers), body))
        texts = body["input"]
        if self.path == "/api/embed":
            answer = {"embeddings": [fake_vector(t) for t in texts]}
        else:
            answer = {"data": [{"index": i, "embedding": fake_vector(t)}
                               for i, t in reversed(list(enumerate(texts)))]}  # fmt: skip
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_server():
    _Fake.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_ollama_backend(fake_server):
    e = embedding.OllamaEmbedder("embed-x", fake_server)
    assert e.embed_documents(["alpha", "beta"]) == [fake_vector("alpha"), fake_vector("beta")]
    path, _, body = _Fake.seen[0]
    assert path == "/api/embed" and body["model"] == "embed-x"


def test_openrouter_backend_scrubs_and_denies_data_collection(fake_server, monkeypatch):
    home = os.path.expanduser("~")
    e = embedding.OpenRouterEmbedder(
        "vendor/embed-1", "test-key-not-real", url=f"{fake_server}/api/v1/embeddings"
    )
    got = e.embed_documents([f"file {home}/notes.md", "second"])
    assert got == [
        fake_vector(embedding.scrub_outbound(f"file {home}/notes.md")),
        fake_vector("second"),
    ]
    _, headers, body = _Fake.seen[0]
    assert headers["Authorization"] == "Bearer test-key-not-real"
    assert body["provider"] == {"data_collection": "deny"}
    assert home not in body["input"][0] and body["input"][0].startswith("file ~")


def test_http_failure_is_an_embedding_error_without_the_key():
    e = embedding.OpenRouterEmbedder(
        "m", "test-key-not-real", url="http://127.0.0.1:9/v1/embeddings", timeout=1
    )
    with pytest.raises(embedding.EmbeddingError) as info:
        e.embed_query("alpha")
    assert "test-key" not in str(info.value)


def test_missing_fastembed_is_an_embedding_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_fastembed(name, *args, **kwargs):
        if name == "fastembed":
            raise ImportError("no fastembed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastembed)
    with pytest.raises(embedding.EmbeddingError, match="noblivion\\[embed\\]"):
        embedding.FastEmbedEmbedder(embedding.DEFAULT_MODEL).load()


@pytest.mark.skipif(
    os.environ.get("NOBLIVION_TEST_REAL_MODEL") != "1",
    reason="set NOBLIVION_TEST_REAL_MODEL=1 to load the real fastembed model",
)
def test_real_fastembed_model(tmp_path):  # pragma: no cover - opt-in
    pytest.importorskip("fastembed")
    e = embedding.FastEmbedEmbedder(embedding.DEFAULT_MODEL, allow_download=True)
    docs = e.embed_documents(["Run pytest with the venv.", "Deploy after the backup."])
    assert len(docs[0]) == embedding.DEFAULT_DIM
    q = embedding.normalize(e.embed_query("how do I run the tests"))
    sims = [sum(a * b for a, b in zip(q, embedding.normalize(d), strict=True)) for d in docs]
    assert sims[0] > sims[1]
