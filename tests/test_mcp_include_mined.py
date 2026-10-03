# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``include_mined`` argument of the ``noblivion_recall`` MCP tool.

``include_mined=true`` asks the store for the index WITH the mined rows
(``include_mined=1`` in the request, design doc section 11.3). The default
(false) never sends it, so mined rows stay out of a plain recall.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _mcp():
    path = ROOT / "mcp" / "recall_mcp.py"
    name = f"_e8_recall_mcp_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def captured(tmp_path, monkeypatch):
    """Run the tool with the store call replaced: record each request URL."""
    mcp = _mcp()
    hook = mcp._load_hook()
    urls: list[str] = []

    def fake_store_get(build, environ, timeout_s):
        url = build("http://127.0.0.1:1")
        urls.append(url)
        if "/index" in url:
            return {"namespace": "claude_code", "mode": "hybrid", "results": []}
        return {"results": []}

    monkeypatch.setattr(hook, "store_get", fake_store_get)
    env = {"NOBLIVION_DATA_DIR": str(tmp_path), "HOME": str(tmp_path)}
    return mcp, env, urls


def _query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def test_include_mined_true_sends_include_mined_1(captured):
    mcp, env, urls = captured
    result = mcp.noblivion_recall("rollout restart", 3, env, include_mined=True)
    assert result["isError"] is False, result
    (url,) = urls
    assert urllib.parse.urlsplit(url).path == "/api/memories/index"
    assert _query(url)["include_mined"] == ["1"]


def test_include_mined_defaults_to_false(captured):
    mcp, env, urls = captured
    mcp.noblivion_recall("rollout restart", 3, env)
    assert urls and all("include_mined" not in _query(u) for u in urls)


def test_include_mined_through_the_json_rpc_call(captured):
    mcp, env, urls = captured
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "noblivion_recall",
            "arguments": {"query": "rollout", "include_mined": True},
        },
    }
    answer = mcp.handle(message, env)
    assert answer["result"]["isError"] is False, answer
    assert _query(urls[-1])["include_mined"] == ["1"]


def test_include_mined_must_be_a_boolean(captured):
    mcp, env, urls = captured
    result = mcp.noblivion_recall("rollout", 3, env, include_mined="yes")
    assert result["isError"] is True
    assert urls == []
