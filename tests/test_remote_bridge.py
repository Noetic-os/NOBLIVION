# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portable stdio forwarding boundaries; no SSH, models or external stores."""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "codex" / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = load("remote_bridge")
gateway = load("remote_gateway")


@pytest.fixture
def cfg(tmp_path):
    return {
        "repo_root": str(ROOT),
        "data_dir": str(tmp_path / "data"),
        "memory_dir": str(tmp_path / "memory"),
        "state_dir": str(tmp_path / "state"),
        "namespace": "test",
        "recall_root": "demo",
        "learning": {"contribute": False, "sessions": {"chat-a": {"use": False}}},
        "gateway_argv": [
            sys.executable,
            str(ROOT / "codex/remote_gateway.py"),
            "--config",
            str(tmp_path / "server.json"),
        ],
        "project_mappings": [
            {
                "workspace": str(tmp_path),
                "namespace": "test",
                "root": "demo",
                "learning": {"contribute": True},
            }
        ],
    }


def event(cfg, session="chat-b"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": cfg["project_mappings"][0]["workspace"],
        "tool_name": "mcp__noblivion__noblivion_recall",
        "tool_input": {"query": "a verified lesson"},
        "transcript_path": "/client/native.jsonl",
        "noblivion_scope": {"namespace": "test", "root": "demo"},
    }


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def test_mcp_startup_uses_scope_without_inherited_session_or_other_chat_optout(cfg, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "chat-a")
    cwd = cfg["project_mappings"][0]["workspace"]
    encoded = bridge.mcp_context(cfg, cwd)
    assert gateway.mcp_config(cfg, encoded)["recall_root"] == "demo"
    decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert decoded == {"cwd": cwd, "namespace": "test", "root": "demo"}
    with pytest.raises(ValueError, match="mapping"):
        bridge.mcp_context(cfg, "/unmapped")


@pytest.mark.parametrize(
    "changes", [{"namespace": "other"}, {"root": "other"}, {"cwd": "/unmapped"}]
)
def test_gateway_refuses_context_outside_exact_mapped_scope(cfg, changes):
    context = {
        "cwd": cfg["project_mappings"][0]["workspace"],
        "namespace": "test",
        "root": "demo",
        **changes,
    }
    with pytest.raises(ValueError, match="scope"):
        gateway.mcp_config(cfg, encode(context))


def test_gateway_always_requires_capability_and_removes_process_ids(cfg, monkeypatch):
    incoming = {
        "CODEX_THREAD_ID": "chat-b",
        "CODEX_SESSION_ID": "chat-b",
        "_learning_session_id": "chat-b",
        "CLAUDE_PROJECT_DIR": "/wrong",
        "NOBLIVION_MCP_REQUIRE_CAPABILITY": "0",
    }
    monkeypatch.setattr(gateway.os, "environ", incoming)
    gateway.activate_env(cfg)
    assert incoming["NOBLIVION_MCP_REQUIRE_CAPABILITY"] == "1"
    assert incoming["NOBLIVION_RECALL_ROOT"] == "demo"
    assert not any(
        name in incoming
        for name in (
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "_learning_session_id",
            "CLAUDE_PROJECT_DIR",
        )
    )


def test_local_optout_denies_recall_even_when_capture_off(cfg, monkeypatch):
    cfg["project_mappings"][0]["learning"]["contribute"] = False
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("opted-out recall reached gateway"),
    )
    output = bridge.hook(cfg, event(cfg, "chat-a"))["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output


def test_forwarding_preserves_full_native_rewrite_and_scrubs_before_capture(cfg, monkeypatch):
    captured = []
    sent = []
    original_module = bridge.module
    fake_capture = SimpleNamespace(
        scoped_config=lambda *args: {**cfg, "recall_root": "demo"},
        capture_hook=lambda cfg, event: captured.append(event),
        memory_use=lambda *args: True,
        safe_content=lambda *args: True,
    )
    monkeypatch.setattr(
        bridge,
        "module",
        lambda path, name: (
            fake_capture if path.name == "capture.py" else original_module(path, name)
        ),
    )
    token = "nobrc1_" + "x" * 43
    expected = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"query": "a verified lesson", "read_capability": "host-issued"},
        }
    }

    def run(argv, **kwargs):
        sent.append((argv, json.loads(kwargs["input"])))
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected))

    monkeypatch.setattr(bridge.subprocess, "run", run)
    raw = event(cfg)
    raw["tool_input"]["read_capability"] = token
    assert bridge.hook(cfg, raw) == expected
    assert token not in json.dumps(captured)
    assert token not in json.dumps(sent)
    assert sent[0][0] == cfg["gateway_argv"] + ["hook"]
    assert sent[0][1]["session_id"] == "chat-b"


def test_remote_failure_denies_recall(cfg, monkeypatch):
    cfg["project_mappings"][0]["learning"]["contribute"] = False

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(bridge.subprocess, "run", fail)
    assert bridge.hook(cfg, event(cfg))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gateway_forwards_pretool_to_adapter_without_second_capture(cfg, monkeypatch):
    seen = []
    selected = {**cfg, **cfg["project_mappings"][0]}
    adapter = SimpleNamespace(
        handle_hook=lambda cfg, event: (
            seen.append((cfg, event))
            or {"hookSpecificOutput": {"updatedInput": {"read_capability": "opaque"}}}
        )
    )
    capture = SimpleNamespace(scoped_config=lambda *args: selected)
    monkeypatch.setattr(
        gateway, "module", lambda path, name: capture if path.name == "capture.py" else adapter
    )
    monkeypatch.setattr(gateway, "activate_env", lambda *args: None)
    output = gateway.hook(cfg, event(cfg))
    assert output["hookSpecificOutput"]["updatedInput"]["read_capability"] == "opaque"
    forwarded_cfg, forwarded_event = seen[0]
    assert forwarded_cfg["learning"]["contribute"] is False
    assert forwarded_cfg["project_mappings"] == []
    assert "transcript_path" not in forwarded_event
    assert forwarded_event["session_id"] == "chat-b"
    with pytest.raises(ValueError, match="scope"):
        gateway.hook(cfg, {**event(cfg), "noblivion_scope": {"namespace": "other", "root": "demo"}})


def test_gateway_cli_mcp_rejects_unidentified_read_before_store_access(cfg, tmp_path):
    path = tmp_path / "server.json"
    path.write_text(json.dumps(cfg))
    encoded = bridge.mcp_context(cfg, cfg["project_mappings"][0]["workspace"])
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "noblivion_recall", "arguments": {"query": "a verified lesson"}},
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "codex/remote_gateway.py"),
            "--config",
            str(path),
            "mcp",
            encoded,
        ],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "native recall routing unavailable" in result.stdout


def remote_health(**changes):
    return {
        "schema_version": 1,
        "scope": {"status": "mapped"},
        "gateway": {"version": bridge.release(ROOT), "source_consistent": True},
        "store": {"version": bridge.release(ROOT), "authenticated": True},
        "learning": {"enabled": False, "scope_configured": True},
        **changes,
    }


def test_health_is_scoped_redacted_and_honest_about_unknowns(cfg, monkeypatch):
    calls = []
    answer = remote_health(secret="credential-canary")

    def run(argv, **kwargs):
        calls.append(argv)
        assert "input" not in kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer))

    monkeypatch.setattr(bridge.subprocess, "run", run)
    out = bridge.health(cfg, cfg["project_mappings"][0]["workspace"])
    assert out["status"] == "partial" and out["versions_match"] is True
    assert out["local_controls"] == {
        "use": True,
        "contribute": True,
        "scope_paused": False,
        "session_controls": "unavailable",
    }
    assert out["scope"] == {"status": "mapped", "namespace": "test", "root": "demo"}
    assert out["learning"]["enabled"] is False
    assert out["learning"]["effective_paused"] == "unavailable"
    assert out["capture"]["last_acknowledgement"] == "unavailable"
    assert calls[0][:-1] == cfg["gateway_argv"] + ["health"]
    assert gateway.mcp_config(cfg, calls[0][-1])["recall_root"] == "demo"
    assert "credential-canary" not in json.dumps(out)
    assert not Path(cfg["state_dir"]).exists()


def test_health_unmapped_scope_never_runs_transport(cfg, monkeypatch):
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: pytest.fail("transport"))
    assert bridge.health(cfg, "/unmapped")["reason"] == "workspace_unmapped"


def test_health_source_mismatch_does_not_import_another_checkout(cfg, monkeypatch):
    cfg["repo_root"] = "/different/release"
    monkeypatch.setattr(bridge, "module", lambda *a, **kw: pytest.fail("imported wrong release"))
    monkeypatch.setattr(gateway, "module", lambda *a, **kw: pytest.fail("imported wrong release"))
    assert bridge.health(cfg, "/unmapped")["reason"] == "client_source_mismatch"
    assert gateway.health(cfg, "ignored")["gateway"]["source_consistent"] is False


def test_health_cli_returns_structured_unmapped_reason(cfg, tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps(cfg))
    result = subprocess.run(
        [sys.executable, str(ROOT / "codex/remote_bridge.py"), "--config", str(path), "health"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["reason"] == "workspace_unmapped"


@pytest.mark.parametrize(
    "remote,reason",
    [
        (
            remote_health(store={"version": "0.0.1", "authenticated": True}),
            "release_or_source_mismatch",
        ),
        (
            remote_health(store={"version": "credential-canary", "authenticated": True}),
            "store_or_version_unverified",
        ),
        (remote_health(scope={"status": "mismatch"}), "gateway_scope_mismatch"),
    ],
)
def test_health_reports_mismatch_and_missing_versions(cfg, monkeypatch, remote, reason):
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(remote)),
    )
    out = bridge.health(cfg, cfg["project_mappings"][0]["workspace"])
    assert out["reason"] == reason and out["status"] != "healthy"
    assert "credential-canary" not in json.dumps(out)


@pytest.mark.parametrize("empty_wal", [False, True])
def test_health_reads_spool_without_creating_or_changing_files(cfg, empty_wal):
    capture = bridge.module(ROOT / "codex/capture.py", "_health_capture")
    spool = capture.Spool(cfg)
    with spool.conn:
        spool.conn.execute("INSERT INTO batches VALUES ('b','private-body',1,2,0,'private-error')")
    spool.close()
    path = capture.spool_folder(cfg) / "capture.sqlite3"
    if empty_wal:
        path.with_name(path.name + "-wal").touch()

    def snapshot():
        return {
            str(p.relative_to(path.parent)): p.read_bytes()
            for p in path.parent.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    result = bridge.spool_health(capture, cfg)
    assert result["pending"] == result["failed_delivery"] == 1
    assert "private" not in json.dumps(result)
    assert snapshot() == before


@pytest.mark.parametrize("valid_proof", [True, False])
def test_gateway_health_proves_store_without_starting_it(cfg, monkeypatch, valid_proof):
    data = Path(cfg["data_dir"])
    data.mkdir()
    token = "a" * 64
    (data / "token").write_text(token)
    (data / "store.json").write_text(json.dumps({"port": 12345}))
    (data / "learning.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": {"key": "credential-canary"},
                "scopes": [{"project": "test", "root": "demo"}],
            }
        )
    )
    store = bridge.module(ROOT / "hooks/store_client.py", "_health_store_test")
    monkeypatch.setattr(store, "request_start", lambda *a, **kw: pytest.fail("autostart"))
    calls = []

    def get(url, credential, timeout):
        calls.append(credential)
        nonce = parse_qs(urlsplit(url).query).get("nonce", [None])[0]
        if nonce:
            assert credential == ""
            return {"proof": store.health_proof(token, nonce) if valid_proof else "wrong"}
        assert credential == token
        return {"version": bridge.release(ROOT), "private": "credential-canary"}

    original = gateway.module
    monkeypatch.setattr(
        gateway,
        "module",
        lambda path, name: (
            store
            if path.name == "store_client.py"
            else SimpleNamespace(http_get_json=get)
            if path.name == "recall_hook.py"
            else original(path, name)
        ),
    )
    context = bridge.mcp_context(cfg, cfg["project_mappings"][0]["workspace"])
    out = gateway.health(cfg, context)
    assert out["store"]["authenticated"] is valid_proof
    assert len(calls) == (2 if valid_proof else 1)
    assert out["learning"] == {"enabled": True, "scope_configured": True}
    assert "credential-canary" not in json.dumps(out)


def test_gateway_health_refuses_mismatched_scope_before_probing(cfg, monkeypatch):
    context = encode({"cwd": "/unmapped", "namespace": "test", "root": "demo"})
    out = gateway.health(cfg, context)
    assert out["scope"]["status"] == "mismatch" and "store" not in out
