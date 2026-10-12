# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portable stdio forwarding boundaries; no SSH, models or external stores."""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
