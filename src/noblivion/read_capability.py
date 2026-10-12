# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short-lived, one-call recall routing issued by the authenticated local store.

The native hook is a trusted same-user client. This does not attest a native
session against an attacker who controls that user's operating system account.
Nothing survives store restart, and only capability hashes are kept in memory.
This module also loads by path in the Python 3.9 hooks and stdio MCP server.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import importlib.util
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "nobrc1_"
ISSUE_PATH = "/api/recall-capability/issue"
RESOLVE_PATH = "/api/recall-capability/resolve"
TTL_S = 15.0
MAX_PENDING = 4096
_TOKEN = re.compile(r"nobrc1_[A-Za-z0-9_-]{43}")
_SESSION = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


def arguments_hash(arguments: dict) -> str:
    """Bind the original JSON arguments, with identical omitted defaults."""
    if not isinstance(arguments, dict):
        raise ValueError("recall arguments required")
    values = {"query": "", "k": 5, "fetch_id": None, "include_mined": False}
    values.update({k: v for k, v in arguments.items() if k != "read_capability"})
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scope(body: dict) -> tuple:
    namespace, root, client = (body.get(k) for k in ("namespace", "root", "client"))
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9_]{1,256}", namespace):
        raise ValueError("invalid namespace")
    if not isinstance(root, str) or not root.strip() or len(root) > 1024:
        raise ValueError("invalid root")
    if client != "codex":
        raise ValueError("invalid client")
    return namespace, root, client, arguments_hash(body.get("arguments"))


class Authority:
    """Bounded process-local authority; consume each token atomically once."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict = {}

    def issue(self, body: dict) -> str:
        binding = scope(body)
        session = body.get("session_id")
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            raise ValueError("invalid native session")
        token = PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
            if len(self._pending) >= MAX_PENDING:
                return ""
            self._pending[hashlib.sha256(token.encode()).digest()] = (now + TTL_S, binding, session)
        return token

    def resolve(self, body: dict) -> str:
        binding = scope(body)
        token = body.get("read_capability")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return ""
        with self._lock:
            entry = self._pending.pop(hashlib.sha256(token.encode()).digest(), None)
        if entry is None or entry[0] <= self.clock() or entry[1] != binding:
            return ""
        return entry[2]


@functools.lru_cache(maxsize=1)
def _store_client():
    path = Path(__file__).resolve().parents[2] / "hooks/store_client.py"
    spec = importlib.util.spec_from_file_location("_capability_store_client", path)
    if spec is None or spec.loader is None:
        raise ImportError("store_client")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(env: dict, path: str, body: dict) -> dict:
    """POST only after the existing loopback listener proof and authentication."""

    def get_json(url, timeout):
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout)
        try:
            conn.request("GET", parsed.path + "?" + parsed.query)
            response = conn.getresponse()
            if response.status != 200:
                raise OSError("listener proof failed")
            return json.loads(response.read(65536))
        finally:
            conn.close()

    base, token = _store_client().connect(env, get_json, 1)
    conn = http.client.HTTPConnection("127.0.0.1", urlsplit(base).port, timeout=2)
    try:
        conn.request(
            "POST",
            path,
            json.dumps(body, allow_nan=False).encode("utf-8"),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("recall routing unavailable")
        result = json.loads(response.read(65536))
        if not isinstance(result, dict):
            raise ValueError("invalid recall routing response")
        return result
    finally:
        conn.close()
