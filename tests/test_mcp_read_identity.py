# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static process IDs cannot bypass the unknown-session MCP read guard."""

import json
from types import SimpleNamespace

import pytest

from noblivion.learning import LearningLedger
from test_hook_recall_store import _store_env, load_hook_mcp
from test_hook_recall_store import real_store as real_store_fixture

real_store = real_store_fixture
IDENTITY_KEYS = ("_learning_session_id", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
STATIC_ID = "00000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize("from_process", [False, True])
def test_tool_env_strips_identity_before_and_after_resolution(tmp_path, monkeypatch, from_process):
    mcp = load_hook_mcp()
    env = {key: STATIC_ID for key in IDENTITY_KEYS}
    env.update(CLAUDE_PROJECT_DIR=str(tmp_path), NOBLIVION_RECALL_ROOT="demo-root")
    before = dict(env)

    def session_env(incoming, payload):
        assert not any(key in incoming for key in IDENTITY_KEYS)
        assert payload == {"cwd": str(tmp_path)}
        return dict(incoming, **{key: STATIC_ID for key in IDENTITY_KEYS})

    if from_process:
        monkeypatch.setattr(mcp.os, "environ", env)
    resolved = mcp.tool_env(SimpleNamespace(session_env=session_env), None if from_process else env)
    assert not any(key in resolved for key in IDENTITY_KEYS)
    assert resolved["NOBLIVION_RECALL_ROOT"] == "demo-root"
    assert env == before


@pytest.mark.parametrize("key", IDENTITY_KEYS)
def test_mcp_query_and_fetch_ignore_other_sessions_static_id(tmp_path, real_store, key):
    run, folder = real_store
    env = _store_env(tmp_path, folder, **{key: STATIC_ID})
    mcp = load_hook_mcp()
    hook = mcp._load_hook()
    with run.store.connection() as conn:
        ledger = LearningLedger(conn)
        memory_id = conn.execute(
            "SELECT id FROM memories WHERE path='feedback_tests_use_venv.md'"
        ).fetchone()[0]

        def read_results():
            return [
                mcp.noblivion_recall("pytest venv", 3, env),
                mcp.noblivion_recall(environ=env, fetch_id=memory_id),
                mcp.noblivion_recall("pytest venv", 3, env, include_mined=True),
            ]

        for result in read_results():
            assert "tests use venv" in json.dumps(result)
        ledger.set_controls(run.store.namespace, folder.parent.name, "opted-out-session", use=False)
        assert ledger.read_allowed(run.store.namespace, folder.parent.name, STATIC_ID)
        for result in read_results():
            assert "tests use venv" not in json.dumps(result)
            assert "never the system python" not in json.dumps(result)
        # Genuine hook event IDs retain identified-session routing.
        native_env = hook.session_env(env, {"cwd": str(tmp_path), "session_id": STATIC_ID})
        assert native_env["_learning_session_id"] == STATIC_ID
        assert hook.recall("pytest venv", 3, native_env)
        ledger.set_controls(run.store.namespace, folder.parent.name, "opted-out-session", use=True)
        for result in read_results():
            assert "tests use venv" in json.dumps(result)
        assert env[key] == STATIC_ID
