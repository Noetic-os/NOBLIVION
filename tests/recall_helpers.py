# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the tests of the REST hooks (recall hook, MCP tool,
error recall, subagent rules): a fake store on 127.0.0.1 that answers the
listener proof of design doc section 3.3, and the env that points a hook at it.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

PROOF_PREFIX = "noblivion-health:"


def proof_for(token: str, nonce: str) -> str:
    return hmac.new(
        token.encode("utf-8"), (PROOF_PREFIX + nonce).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_store_files(data_dir: Path, port: int, token: str | None = None) -> str:
    """Make ``data_dir`` name a store on ``127.0.0.1:port``: the ``token`` file
    and ``store.json``, as the store writes them. Returns the token.

    For a test that keeps its own fake HTTP handler: the handler answers
    ``GET /health?nonce=<n>`` with ``health_reply(token, nonce)`` and checks
    ``Authorization: Bearer <token>`` on every other path.
    """
    token = token or secrets.token_hex(32)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    write_private(data_dir / "token", token + "\n")
    write_private(
        data_dir / "store.json",
        json.dumps({"pid": os.getpid(), "port": int(port), "version": "0.0.0"}),
    )
    return token


def health_reply(token: str, nonce: str) -> dict:
    """The short ``/health`` answer with the listener proof for ``nonce``."""
    return {
        "status": "ok",
        "service": "noblivion",
        "version": "0.0.0",
        "proof": proof_for(token, nonce),
    }


class FakeStore:
    """A fake store. ``proof`` is ``good`` (the HMAC of the token in the data
    dir), ``wrong`` (the HMAC of another token), ``none`` (no proof field) or
    ``echo`` (the nonce itself).

    ``route(prefix, payload, status)`` sets the answer for every GET path that
    starts with ``prefix``; ``payload`` may be a callable ``(path, query) ->
    payload`` or ``(status, payload)``, or ``bytes`` sent as they are. Every
    request is kept in ``requests`` as ``(path, query, headers)``.
    """

    def __init__(self, data_dir: Path, *, proof: str = "good", write_files: bool = True):
        self.data_dir = Path(data_dir)
        self.token = secrets.token_hex(32)
        self.proof = proof
        self.routes: dict[str, Any] = {}
        self.requests: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []
        self.lock = threading.Lock()
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                store._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if write_files:
            self.write_files()

    def write_files(self) -> None:
        write_store_files(self.data_dir, self.port, self.token)

    def route(self, prefix: str, payload: Any, status: int = 200) -> None:
        self.routes[prefix] = (status, payload)

    @property
    def store_paths(self) -> list[str]:
        """The paths of the requests other than the health proof."""
        return [p for p, _q, _h in self.requests if p != "/health"]

    def _send(self, handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = urllib.parse.urlsplit(handler.path)
        query = urllib.parse.parse_qs(parts.query)
        headers = {k.lower(): v for k, v in handler.headers.items()}
        with self.lock:
            self.requests.append((parts.path, query, headers))
        if parts.path == "/health":
            nonce = (query.get("nonce") or [""])[0]
            answer: dict[str, Any] = {"status": "ok", "service": "noblivion", "version": "0.0.0"}
            if self.proof == "good":
                answer["proof"] = proof_for(self.token, nonce)
            elif self.proof == "wrong":
                answer["proof"] = proof_for(secrets.token_hex(32), nonce)
            elif self.proof == "echo":
                answer["proof"] = nonce
            self._send(handler, 200, answer)
            return
        if headers.get("authorization") != f"Bearer {self.token}":
            self._send(handler, 401, {"detail": "missing or wrong token"})
            return
        match = max((p for p in self.routes if parts.path.startswith(p)), key=len, default=None)
        if match is None:
            self._send(handler, 404, {"detail": "not found"})
            return
        status, payload = self.routes[match]
        if callable(payload):
            payload = payload(parts.path, query)
            if isinstance(payload, tuple):
                status, payload = payload
        self._send(handler, status, payload)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> FakeStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def hook_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An env for a REST hook: its own data dir and cache, no config file, no
    store start, and ``PATH`` kept."""
    env = {
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
        "NOBLIVION_CONFIG": str(tmp_path / "no-config.json"),
        "NOBLIVION_STORE_AUTOSTART": "0",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    env.update(extra)
    return env


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` (and so ``~/.claude/projects``) inside the test's tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA", "XDG_DATA_HOME", "NOBLIVION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    return home


def index_row(rank: int, mid: int, title: str, summary: str = "", score: Any = 0.5, **extra: Any):
    """One row of the ``/api/memories/index`` answer (design doc section 4.3)."""
    row = {
        "rank": rank,
        "id": mid,
        "title": title,
        "summary": summary,
        "score": score,
        "fusion_score": round(1.0 / (60 + rank), 6),
        "source": extra.pop("source", None),
        "source_type": extra.pop("source_type", "claude_code_md"),
    }
    row.update(extra)
    return row


def index_answer(rows: list, mode: str = "hybrid", namespace: str = "claude_code") -> dict:
    return {"namespace": namespace, "reason": None, "mode": mode, "results": rows}


Handler = Callable[[str, dict], Any]
