#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Forward Codex hooks and stdio MCP to an explicitly configured store host.

Local capture owns native transcript access. The gateway issues recall routing
through its own authenticated store; this client holds no capability authority.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_INPUT = 4 * 1024 * 1024


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def gateway_argv(cfg: dict) -> list:
    argv = cfg.get("gateway_argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise ValueError("gateway_argv must be an explicit argument list")
    return argv


def mcp_context(cfg: dict, cwd: str) -> str:
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_bridge_capture")
    selected = capture.scoped_config(cfg, {"cwd": cwd})
    if selected is None:
        raise ValueError("MCP workspace has no explicit memory mapping")
    # MCP startup has no chat identity. Per-call native capabilities, not an
    # inherited session or another chat's local controls, authorize its reads.
    context = {"cwd": cwd, "namespace": selected["namespace"], "root": selected["recall_root"]}
    return base64.urlsafe_b64encode(json.dumps(context).encode()).decode().rstrip("=")


def _recall(event: dict) -> bool:
    name = event.get("tool_name")
    return (
        event.get("hook_event_name") == "PreToolUse"
        and isinstance(name, str)
        and name.startswith("mcp__")
        and name.endswith("__noblivion_recall")
    )


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def hook(cfg: dict, event: dict) -> dict:
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_bridge_capture")
    redaction = module(Path(cfg["repo_root"]) / "src/noblivion/redaction.py", "_bridge_redaction")
    # A rewritten argument may appear in a later native tool result/transcript.
    # Strip it before local capture and before it crosses the transport again.
    event = redaction.scrub_read_capabilities(event)
    selected = capture.scoped_config(cfg, event)
    if selected is None:
        return _deny("Memory workspace has no explicit mapping.") if _recall(event) else {}
    try:
        capture.capture_hook(cfg, event)
    except Exception as exc:  # noqa: BLE001 - local evidence stays retryable
        print("NOBLIVION local capture deferred: " + type(exc).__name__, file=sys.stderr)
    if not capture.memory_use(selected, str(event.get("session_id") or "")):
        return _deny("Memory use disabled for this chat.") if _recall(event) else {}
    if not capture.safe_content(selected, json.dumps(event)):
        if _recall(event):
            return _deny("Unsafe recall input omitted.")
        event = {
            key: event[key] for key in ("hook_event_name", "session_id", "cwd") if key in event
        }
        event["prompt"] = "[unsafe source omitted]"
    event["noblivion_scope"] = {"namespace": selected["namespace"], "root": selected["recall_root"]}
    try:
        timeout = 2 if event.get("hook_event_name") == "SessionEnd" else 25
        result = subprocess.run(
            gateway_argv(cfg) + ["hook"],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise OSError("remote hook failed")
        output = json.loads(result.stdout)
        if not isinstance(output, dict):
            raise ValueError("invalid remote hook output")
        return output
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("NOBLIVION shared memory unavailable: " + type(exc).__name__, file=sys.stderr)
        if _recall(event):
            return _deny("Shared memory recall routing unavailable.")
        return {
            "hookSpecificOutput": {
                "hookEventName": event.get("hook_event_name", "UserPromptSubmit"),
                "additionalContext": (
                    "Shared NOBLIVION recall is unavailable. Local capture remains queued; "
                    "do not claim it is accepted or indexed."
                ),
            }
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("remote-client.json")))
    parser.add_argument("mode", choices=("hook", "mcp"))
    args = parser.parse_args()
    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        cfg["source_config"] = str(Path(args.config).resolve())
        if args.mode == "mcp":
            return subprocess.call(gateway_argv(cfg) + ["mcp", mcp_context(cfg, os.getcwd())])
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("hook input too large")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook event must be an object")
        print(json.dumps(hook(cfg, event)))
        return 0
    except Exception as exc:  # noqa: BLE001 - no source, credential or traceback in client logs
        print("NOBLIVION remote bridge: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
