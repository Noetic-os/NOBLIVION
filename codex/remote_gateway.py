#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Same-user SSH endpoint for scoped Codex hooks, MCP and capture delivery.

This is a stdio process, not a network service. An explicit configuration maps
client workspace paths onto memory roots on this host. SSH authenticates the
client account; the existing loopback store authenticates local requests.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

from remote_bridge import MAX_INPUT, module


def activate_env(cfg: dict) -> None:
    for name in (
        "_learning_session_id",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
    ):
        os.environ.pop(name, None)
    os.environ.update(
        {
            "NOBLIVION_DATA_DIR": cfg["data_dir"],
            "NOBLIVION_MEMORY_DIR": cfg["memory_dir"],
            "NOBLIVION_RECALL_MEMORY_DIR": cfg["memory_dir"],
            "NOBLIVION_RECALL_PROJECT": cfg["namespace"],
            "NOBLIVION_RECALL_ROOT": cfg["recall_root"],
            "NOBLIVION_PROJECT": cfg["namespace"],
            "NOBLIVION_SOURCE_CLIENT": "codex",
            "NOBLIVION_MCP_REQUIRE_CAPABILITY": "1",
        }
    )


def mcp_config(cfg: dict, encoded: str) -> dict:
    if len(encoded) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("invalid MCP context")
    context = json.loads(base64.urlsafe_b64decode(encoded + "=" * ((-len(encoded)) % 4)))
    if not isinstance(context, dict):
        raise ValueError("invalid MCP context")
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_gateway_capture")
    selected = capture.scoped_config(cfg, context)
    if selected is None or (context.get("namespace"), context.get("root")) != (
        selected["namespace"],
        selected["recall_root"],
    ):
        raise ValueError("MCP scope does not match explicit mapping")
    return selected


def hook(cfg: dict, event: dict) -> dict:
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_gateway_capture")
    selected = capture.scoped_config(cfg, event)
    if selected is None:
        raise ValueError("hook workspace has no explicit mapping")
    if event.get("noblivion_scope") != {
        "namespace": selected["namespace"],
        "root": selected["recall_root"],
    }:
        raise ValueError("hook scope does not match explicit mapping")
    session = event.get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", session):
        raise ValueError("native hook session required")
    activate_env(selected)
    adapter = module(Path(cfg["repo_root"]) / "codex/adapter.py", "_gateway_adapter")
    # The client owns native evidence. Avoid reselecting a mapping that could
    # turn contribution back on or reading client transcript paths on this host.
    selected = {
        **selected,
        "workspace_prefixes": [selected["workspace"]],
        "workspace_git_dirs": [],
        "project_mappings": [],
        "learning": {**selected.get("learning", {}), "contribute": False},
    }
    event = {k: v for k, v in event.items() if k != "transcript_path"}
    if event.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure", "Stop"):
        adapter._journal(selected, event, adapter.tool_events(event))
        return {}
    return adapter.handle_hook(selected, event)


def receive(cfg: dict, body: dict) -> dict:
    matches = [
        mapping
        for mapping in cfg.get("project_mappings", [])
        if (mapping.get("namespace"), mapping.get("root"))
        == (body.get("namespace"), body.get("root"))
    ]
    if not matches:
        raise ValueError("capture scope is not authorized")
    selected = {**cfg, **matches[0], "learning": {**cfg.get("learning", {}), "delivery_argv": None}}
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_gateway_capture")
    redaction = module(Path(cfg["repo_root"]) / "src/noblivion/redaction.py", "_gateway_redaction")
    return capture.deliver(selected, redaction.scrub_read_capabilities(body))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("remote-server.json")))
    parser.add_argument("mode", choices=("hook", "mcp", "receive"))
    parser.add_argument("context", nargs="?")
    args = parser.parse_args()
    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        base = Path(cfg["repo_root"])
        if args.mode == "mcp":
            activate_env(mcp_config(cfg, args.context or ""))
            os.execv(sys.executable, [sys.executable, str(base / "mcp/recall_mcp.py")])
            return 0
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("gateway input too large")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("gateway input must be an object")
        print(json.dumps(receive(cfg, event) if args.mode == "receive" else hook(cfg, event)))
        return 0
    except Exception as exc:  # noqa: BLE001 - never print source, credentials or tracebacks
        print("NOBLIVION remote gateway: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
