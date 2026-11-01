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
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

MAX_INPUT = 4 * 1024 * 1024


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("configuration exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


def version(value):
    return (
        value
        if isinstance(value, str)
        and len(value) <= 64
        and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", value)
        else None
    )


def release(root: Path):
    try:
        return version(read_json(root / ".claude-plugin/plugin.json").get("version"))
    except (OSError, ValueError):
        return None


def source_health(cfg: dict) -> dict:
    own = Path(__file__).resolve().parents[1]
    return {
        "version": release(own),
        "source_consistent": Path(cfg["repo_root"]).resolve() == own,
    }


def spool_health(capture, selected: dict) -> dict:
    result = {"status": "unavailable", "last_acknowledgement": "unavailable"}
    try:
        path = capture.spool_folder(selected) / "capture.sqlite3"
        if not path.exists():
            return {**result, "status": "not_created", "pending": 0, "failed_delivery": 0}
        if (
            path.is_symlink()
            or not path.is_file()
            or any(
                p.exists() and p.stat().st_size
                for p in (
                    path.with_name(path.name + "-wal"),
                    path.with_name(path.name + "-journal"),
                )
            )
        ):
            return {**result, "reason": "active_or_unsafe_spool"}
        before = path.stat()
        uri = "file:" + quote(str(path.resolve())) + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            deadline = time.monotonic() + 1
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            pending, failed = conn.execute(
                "SELECT count(*),coalesce(sum(attempts>0),0) FROM batches"
            ).fetchone()
        finally:
            conn.close()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or any(
            p.exists() and p.stat().st_size
            for p in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal"))
        ):
            return {**result, "reason": "spool_changed"}
        return {**result, "status": "observed", "pending": pending, "failed_delivery": failed}
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return result


def health(cfg: dict, cwd: str) -> dict:
    """An explicit scoped probe; never capture, drain, start a store or change controls."""
    result = {"schema_version": 1, "status": "partial", "client": source_health(cfg)}
    if not result["client"]["source_consistent"]:
        return {**result, "status": "mismatch", "reason": "client_source_mismatch"}
    capture = module(Path(cfg["repo_root"]) / "codex/capture.py", "_bridge_capture")
    selected = capture.scoped_config(cfg, {"cwd": cwd})
    if selected is None:
        return {**result, "status": "unavailable", "reason": "workspace_unmapped"}
    result["scope"] = {"status": "mapped"}
    identity = {"namespace": selected["namespace"], "root": selected["recall_root"]}
    if all(
        isinstance(v, str) and len(v) <= 256 for v in identity.values()
    ) and capture.safe_content(selected, json.dumps(identity)):
        result["scope"].update(identity)
    result["local_controls"] = {
        "use": capture.memory_use(selected),
        "contribute": capture.allowed(selected),
        "scope_paused": selected.get("learning", {}).get("paused") is True,
        "session_controls": "unavailable",
    }
    result["capture"] = spool_health(capture, selected)
    try:
        response = subprocess.run(
            gateway_argv(cfg) + ["health", mcp_context(cfg, cwd)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        remote = json.loads(response.stdout)
        if response.returncode or not isinstance(remote, dict) or remote.get("schema_version") != 1:
            raise ValueError("invalid health response")
        # Copy only known primitive fields. An arbitrary configured command's
        # output must not turn this diagnostic into a config or credential dump.
        for name, fields in {
            "gateway": ("version", "source_consistent"),
            "store": ("version", "authenticated"),
            "learning": ("enabled", "scope_configured"),
        }.items():
            body = remote.get(name)
            if not isinstance(body, dict):
                body = {}
            result[name] = {
                key: version(body.get(key))
                if key == "version"
                else body.get(key)
                if type(body.get(key)) is bool
                else None
                for key in fields
            }
        result["learning"]["effective_paused"] = "unavailable"
        versions = [result[name].get("version") for name in ("client", "gateway", "store")]
        result["versions_match"] = len(set(versions)) == 1 if all(versions) else None
        if result["versions_match"] is False or any(
            result[name].get("source_consistent") is False for name in ("client", "gateway")
        ):
            result.update(status="mismatch", reason="release_or_source_mismatch")
        elif remote.get("scope") != {"status": "mapped"}:
            result.update(status="unavailable", reason="gateway_scope_mismatch")
        elif result["store"]["authenticated"] is not True or not all(versions):
            result.update(status="unavailable", reason="store_or_version_unverified")
        else:
            result["reason"] = "session_controls_and_last_acknowledgement_unavailable"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result.update(status="unavailable", reason="gateway_unavailable")
    return result


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
    parser.add_argument("mode", choices=("hook", "mcp", "health"))
    args = parser.parse_args()
    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        cfg["source_config"] = str(Path(args.config).resolve())
        if args.mode == "health":
            print(json.dumps(health(cfg, os.getcwd())))
            return 0
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
