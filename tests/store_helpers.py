# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the ranking and embedding tests. Fictional data only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from noblivion import bm25, db, embedding

FAKE_DIM = 16


def fake_vector(text: str, dim: int = FAKE_DIM) -> list[float]:
    """A deterministic bag-of-words vector: each token adds 1 to one slot.

    Texts that share words get a positive cosine. No model, no network.
    """
    vec = [0.0] * dim
    for token in bm25.tokenize(text):
        slot = int(hashlib.sha256(token.encode()).hexdigest(), 16) % dim
        vec[slot] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


class FakeEmbedder:
    """Deterministic embedder for tests. Records every call."""

    remote = False

    def __init__(self, model: str = "fake-model", backend: str = "fake", fail_on: str = ""):
        self.backend = backend
        self.model = model
        self.fail_on = fail_on
        self.calls: list[list[str]] = []
        self.queries: list[str] = []
        self.loaded = False

    @property
    def model_id(self) -> str:
        return f"{self.backend}:{self.model}"

    def load(self) -> None:
        self.loaded = True

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("fake embed failure")
        return [fake_vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return fake_vector(text)


class _RemoteHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        stub = self.server.stub
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        stub.requests.append(body)
        if stub.gate is not None:
            stub.gate.wait(10)
        texts = body["input"]
        if stub.refuse_batches and len(texts) > 1:
            self.send_error(500)
            return
        if self.path == "/api/embed":
            answer = {"embeddings": [fake_vector(t) for t in texts]}
        else:
            answer = {
                "data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(texts)]
            }
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class FakeRemote:
    """A loopback HTTP server in the place of a hosted embedding service.

    ``requests`` holds the body of every request it got, so a test can count
    the calls. ``gate`` holds each answer until the test sets it.
    ``refuse_batches`` answers 500 to a request with more than one text.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.gate: threading.Event | None = None
        self.refuse_batches = False
        self._server = HTTPServer(("127.0.0.1", 0), _RemoteHandler)
        self._server.stub = self  # type: ignore[attr-defined]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def texts(self) -> list[str]:
        return [text for body in self.requests for text in body["input"]]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


REMOTE_MODEL = "vendor/embed-1"


def remote_service(url: str, backend: str = "openrouter") -> embedding.EmbeddingService:
    """A service with a real remote backend that posts to ``FakeRemote``.

    The real ``make_embedder`` builds the backend, so its consent gate
    applies; only the target URL changes. The ``ollama`` form is configured
    with a non-loopback address, so it counts as remote.
    """
    if backend == "openrouter":
        settings = embedding.EmbeddingSettings(backend="openrouter", model=REMOTE_MODEL)
        target = f"{url}/api/v1/embeddings"
    else:
        settings = embedding.EmbeddingSettings(
            backend="ollama",
            model=REMOTE_MODEL,
            ollama_url="http://192.0.2.10:11434",
            allow_remote=True,
        )
        target = url

    def factory(s, conn, env):
        made = embedding.make_embedder(s, conn, env)
        made.url = target
        return made

    return embedding.EmbeddingService(
        settings, factory=factory, env={"OPENROUTER_API_KEY": "test-key-not-real"}
    )


def content(name: str, body: str, path: str | None = None) -> str:
    return f"# {name}\n\n[claude_code_md: {path or name + '.md'}]\n\n{body}"


def add_memory(
    conn: sqlite3.Connection,
    name: str,
    body: str,
    *,
    project: str = "claude_code",
    root: str = "-proj-alpha",
    source_type: str = db.SOURCE_MD,
    weight: float = 1.0,
) -> int:
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "content_rev")
        now = db.utc_now()
        memory_id = db.insert_memory(
            conn,
            project=project,
            root=root,
            path=f"{name}.md",
            source_type=source_type,
            category="feedback",
            content=content(name, body),
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            labels="[]",
            rev=rev,
            now=now,
        )
        if weight != 1.0:
            conn.execute("UPDATE memories SET weight = ? WHERE id = ?", (weight, memory_id))
    return memory_id


def change_memory(conn: sqlite3.Connection, memory_id: int, body: str) -> None:
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "content_rev")
        name = conn.execute("SELECT path FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
        conn.execute(
            "UPDATE memories SET content = ?, rev = ?, updated_at = ? WHERE id = ?",
            (content(name[:-3], body), rev, db.utc_now(), memory_id),
        )


def soft_delete(conn: sqlite3.Connection, memory_id: int) -> None:
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "content_rev")
        db.soft_delete(conn, memory_id, rev=rev, now=db.utc_now())


def mark_installed(data_dir: Path) -> Path:
    """Write the install stamp that ``install.sh`` writes, so the store and
    ``noblivion index`` may make a new database there (NOBLIVION-28)."""
    from noblivion import __version__, config

    stamp = config.install_stamp(data_dir)
    stamp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"version": __version__}), encoding="utf-8")
    return data_dir
