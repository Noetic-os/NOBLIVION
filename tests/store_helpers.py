# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the ranking and embedding tests. Fictional data only."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence

from noblivion import bm25, db

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
