# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native hook routing with a running REST store, real SQLite, and public MCP."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from noblivion import read_capability, redaction
from test_codex_adapter import adapter
from test_hook_recall_store import load_hook_mcp
from test_learning_capture import capture, records
from test_learning_read_access import memory_service as memory_service_fixture

memory_service = memory_service_fixture


@pytest.fixture
def clients(memory_service, tmp_path, monkeypatch):
    st, run, ledger, mid, root = memory_service
    cfg = {
        "repo_root": str(Path(__file__).resolve().parents[1]),
        "data_dir": str(st.data_dir),
        "memory_dir": str(st.settings.memory_dirs[0]),
        "state_dir": str(tmp_path / "state"),
        "namespace": st.namespace,
        "recall_root": root,
        "workspace_prefixes": [str(tmp_path)],
    }
    monkeypatch.setattr(adapter, "_invoke", lambda *args: {})
    ledger.set_controls(st.namespace, root, "native-a", use=False)
    mcp = load_hook_mcp()
    # The public stdio process loads once before serving requests. Preload for
    # this fixture's concurrent calls, which intentionally share its module.
    mcp._load_hook()
    return cfg, mcp, run, ledger, mid


def hook_output(cfg, session, arguments):
    event = {
        "session_id": session,
        "turn_id": "turn-1",
        "tool_use_id": "call-1",
        "hook_event_name": "PreToolUse",
        "cwd": cfg["workspace_prefixes"][0],
        "transcript_path": "",
        "tool_name": "mcp__noblivion__noblivion_recall",
        "tool_input": arguments,
    }
    return adapter.handle_hook(cfg, event)["hookSpecificOutput"]


def hook_input(cfg, session, arguments):
    return hook_output(cfg, session, arguments)["updatedInput"]


def dispatch(clients, arguments, **env_changes):
    cfg, mcp, _, _, _ = clients
    env = adapter.environment(cfg, {"session_id": "shared-mcp"})
    env["NOBLIVION_MCP_REQUIRE_CAPABILITY"] = "1"
    env.update(env_changes)
    answer = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "noblivion_recall", "arguments": arguments},
        },
        env,
    )
    return "brief replies" in json.dumps(answer), answer


def recall_arguments(mid):
    return (
        {"query": "brief replies"},
        {"query": "brief replies", "include_mined": True},
        {"fetch_id": int(mid)},
    )


def test_two_chats_concurrent_search_index_fetch(clients):
    cfg, _, _, _, mid = clients
    for arguments in recall_arguments(mid):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(hook_input, cfg, "native-a", arguments)
            b = pool.submit(hook_input, cfg, "native-b", arguments)
            ai, bi = a.result(), b.result()
        assert ai["read_capability"] == ""
        assert bi["read_capability"].startswith(read_capability.PREFIX)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(dispatch, clients, ai)
            b = pool.submit(dispatch, clients, bi)
            assert not a.result()[0]
            assert b.result()[0], b.result()[1]


@pytest.mark.parametrize("capability", [None, "", "native-b", "forged", "nobrc1_" + "x" * 43])
def test_untrusted_capability_and_inherited_ids_remain_unknown(clients, capability):
    _, _, _, _, mid = clients
    for arguments in recall_arguments(mid):
        assert not dispatch(
            clients,
            {**arguments, "read_capability": capability},
            CODEX_THREAD_ID="native-b",
            CODEX_SESSION_ID="native-b",
            _learning_session_id="native-b",
        )[0]


def test_replay_changed_arguments_scope_and_expiry(clients):
    cfg, _, run, _, _ = clients
    args = {"query": "brief replies"}
    original = hook_input(cfg, "native-b", args)
    assert dispatch(clients, original)[0]
    assert not dispatch(clients, original)[0]
    for changed in ({"query": "brief replies changed"}, {"k": 1}, {"include_mined": True}):
        issued = hook_input(cfg, "native-b", args)
        assert not dispatch(clients, {**issued, **changed})[0]
    issued = hook_input(cfg, "native-b", args)
    assert not dispatch(clients, issued, NOBLIVION_RECALL_ROOT="other-root")[0]
    assert not dispatch(clients, issued, NOBLIVION_RECALL_PROJECT="other")[0]
    issued = hook_input(cfg, "native-b", args)
    clock = run.store.read_capabilities.clock
    run.store.read_capabilities.clock = lambda: clock() + read_capability.TTL_S + 1
    assert not dispatch(clients, issued)[0]
    run.store.read_capabilities.clock = clock
    issued = hook_input(cfg, "native-b", args)
    run.store.read_capabilities = read_capability.Authority()
    assert not dispatch(clients, issued)[0]


def test_live_opt_out_after_issuance_and_independent_contribution(clients):
    cfg, _, _, ledger, mid = clients
    for arguments in recall_arguments(mid):
        ledger.set_controls(
            cfg["namespace"], cfg["recall_root"], "native-b", use=True, contribute=False
        )
        issued = hook_input(cfg, "native-b", arguments)
        assert dispatch(clients, issued)[0]
        issued = hook_input(cfg, "native-b", arguments)
        ledger.set_controls(cfg["namespace"], cfg["recall_root"], "native-b", use=False)
        assert not dispatch(clients, issued)[0]
        assert ledger.effective_controls(cfg["namespace"], cfg["recall_root"], "native-a")[
            "contribute"
        ]


def test_live_root_and_general_source_controls(clients):
    cfg, _, _, ledger, mid = clients
    receiver = {**cfg, "recall_root": "receiving-project"}
    for arguments in recall_arguments(mid):
        ledger.set_controls(cfg["namespace"], cfg["recall_root"], use=True)
        issued = hook_input(receiver, "native-b", arguments)
        # The fixture note is general; its source's session A does not block B.
        receiver_clients = (receiver, *clients[1:])
        assert dispatch(receiver_clients, issued)[0]
        issued = hook_input(receiver, "native-b", arguments)
        ledger.set_controls(cfg["namespace"], cfg["recall_root"], use=False)
        assert not dispatch(receiver_clients, issued)[0]
    ledger.set_controls(cfg["namespace"], "receiving-project", use=False)
    assert hook_input(receiver, "native-b", {"query": "brief replies"})["read_capability"] == ""


def test_hook_replaces_model_token_when_local_use_off_or_store_unavailable(clients, monkeypatch):
    cfg, _, _, _, _ = clients
    args = {"query": "brief replies", "read_capability": "nobrc1_" + "x" * 43}
    cfg = {**cfg, "learning": {"use": False}}
    output = hook_output(cfg, "native-b", args)
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output
    cfg = {**cfg, "learning": {"use": True}}
    monkeypatch.setenv("NOBLIVION_STORE_AUTOSTART", "0")
    cfg["data_dir"] = cfg["state_dir"]
    assert hook_input(cfg, "native-b", args)["read_capability"] == ""


def test_installed_codex_without_hook_cannot_read_even_before_store_knows_optout(clients):
    cfg, _, _, ledger, mid = clients
    ledger.set_controls(cfg["namespace"], cfg["recall_root"], "native-a", use=True)
    # The store permits anonymous reads. Installed Codex still needs a native
    # capability, including when local use is off and its hook fails or is absent.
    for arguments in recall_arguments(mid):
        for token in (None, "", "native-b", "nobrc1_" + "x" * 43):
            assert not dispatch(clients, {**arguments, "read_capability": token})[0]
        assert dispatch(clients, arguments, NOBLIVION_MCP_REQUIRE_CAPABILITY="")[0]
        issued = hook_input(cfg, "native-b", arguments)
        assert dispatch(clients, issued)[0]


def test_strict_read_routing_does_not_disable_explicit_contribution(clients, monkeypatch):
    cfg, mcp, _, _, _ = clients
    env = adapter.environment(cfg, {"session_id": "shared-mcp"})
    env.update(NOBLIVION_MCP_REQUIRE_CAPABILITY="1", NOBLIVION_SOURCE_CLIENT="codex")
    monkeypatch.setattr(mcp.subprocess, "Popen", lambda *args, **kwargs: None)
    answer = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "noblivion_remember",
                "arguments": {
                    "title": "Synthetic verified lesson",
                    "rule": "Keep the synthetic fixture concise.",
                    "apply": "For synthetic fixture tests.",
                    "body": "Verified fixture evidence remains available.",
                    "evidence": "A deterministic fixture check.",
                },
            },
        },
        env,
    )
    assert answer["result"]["isError"] is False, answer


def test_atomic_consume_and_bounded_pending_authority():
    authority = read_capability.Authority()
    body = {
        "namespace": "test",
        "root": "demo",
        "client": "codex",
        "session_id": "native-b",
        "arguments": {"query": "brief replies"},
    }
    token = authority.issue(body)
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(
            pool.map(lambda _: authority.resolve({**body, "read_capability": token}), range(8))
        )
    assert result.count("native-b") == 1
    for _ in range(read_capability.MAX_PENDING):
        assert authority.issue(body)
    assert authority.issue(body) == ""


def test_store_routes_require_auth_and_native_session_shape(clients):
    cfg, _, run, _, _ = clients
    body = {
        "namespace": cfg["namespace"],
        "root": cfg["recall_root"],
        "client": "codex",
        "session_id": "native-b",
        "arguments": {"query": "brief replies"},
    }
    for path in (read_capability.ISSUE_PATH, read_capability.RESOLVE_PATH):
        assert run.request("POST", path, token=False, body=body)[0] == 401
    for session in ("", "bad/id", "native-b\n", "x" * 257, None):
        assert (
            run.request("POST", read_capability.ISSUE_PATH, body={**body, "session_id": session})[0]
            == 400
        )
    assert (
        run.request("POST", read_capability.ISSUE_PATH, body={**body, "client": "other"})[0] == 400
    )


def test_guard_denial_wins_over_capability_rewrite():
    rewritten = {
        "hookSpecificOutput": {
            "updatedInput": {"query": "brief replies", "read_capability": "opaque"}
        }
    }
    denied = {
        "hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "guard"}
    }
    for outputs in ([rewritten, denied], [denied, rewritten]):
        assert (
            adapter._merge("PreToolUse", outputs)["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )


@pytest.mark.parametrize("native_transcript", [False, True])
def test_capture_and_journal_remove_capability_without_dropping_contribution(
    clients, tmp_path, native_transcript
):
    cfg, _, _, _, _ = clients
    cfg = {**cfg, "learning": {"use": True, "contribute": True}}
    value = hook_input(cfg, "native-b", {"query": "brief replies"})["read_capability"]
    # _recall_input itself does not save its output. Exercise later native output
    # containing the rewritten argument, including the transcript-only path.
    event = {
        "session_id": "capture-session",
        "cwd": cfg["workspace_prefixes"][0],
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    spool = capture.Spool(cfg)
    spool.capture(event)
    post = {
        **event,
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__noblivion__noblivion_recall",
        "tool_use_id": "call-1",
        "tool_input": {"query": "brief replies", "read_capability": value},
        "tool_response": {"output": "Verified useful memory."},
    }
    if native_transcript:
        path = tmp_path / "native.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": post["tool_name"],
                        "call_id": "call-1",
                        "arguments": json.dumps(post["tool_input"]),
                    },
                }
            )
            + "\n"
        )
        post["transcript_path"] = str(path)
    spool.capture(post)
    assert records(spool)
    assert value not in json.dumps(records(spool))
    assert spool.conn.execute("SELECT count(*) FROM rejected").fetchone()[0] == 0
    for table in ("batches", "calls"):
        assert value not in json.dumps(
            [tuple(r) for r in spool.conn.execute("SELECT * FROM " + table)]
        )
    spool.close()
    journal = adapter._journal(cfg, post, [])
    assert value not in journal.read_text()
    assert "Verified useful memory" in journal.read_text()
    assert value not in redaction.redact_at_rest("Evidence " + value)
