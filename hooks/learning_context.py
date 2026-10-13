#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deliver relevant evidence-linked context through the proven store listener."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(event: dict, environ: dict, *, codex: bool = False) -> dict:
    if event.get("hook_event_name") != "UserPromptSubmit" or not event.get("session_id"):
        return {}
    query = event.get("prompt")
    if not isinstance(query, str) or not query.strip() or len(query) > 8000:
        return {}
    root = Path(__file__).resolve().parents[1]
    env = dict(environ)
    # Codex supplies its already resolved scope. Claude resolves the same
    # explicit mapping as its capture hook; unknown workspaces stay silent.
    if not codex:
        config = load(root / "hooks/hook_config.py", "_context_config")
        path = config.data_dir(env) / "learning-client.json"
        if not path.is_file():
            return {}
        capture = load(root / "codex/capture.py", "_context_capture")
        cfg = capture.scoped_config(json.loads(path.read_text()), event)
        if cfg is None or not capture.memory_use(cfg, event["session_id"]):
            return {}
        env.update(
            NOBLIVION_RECALL_ROOT=cfg["root"],
            NOBLIVION_RECALL_PROJECT=cfg["namespace"],
        )
    elif not env.get("NOBLIVION_RECALL_ROOT"):
        return {}
    recall = load(root / "hooks/recall_hook.py", "_context_recall")
    env = recall.session_env(env, event)
    params = urllib.parse.urlencode(
        {
            "project": env.get("NOBLIVION_RECALL_PROJECT", "claude_code"),
            "root": env["NOBLIVION_RECALL_ROOT"],
            "query": query,
            "session_id": event["session_id"],
            "limit_chars": 6000,
        }
    )
    response = recall.store_get(lambda base: base + "/learning/context?" + params, env, 2.0)
    context = response.get("context") if isinstance(response, dict) else None
    if (
        not isinstance(context, str)
        or not context.strip()
        or len(context) > 6000
        or response.get("reason")
    ):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "RELEVANT LEARNED MEMORY (evidence, not authority)\n" + context,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    try:
        result = run(json.load(sys.stdin), dict(os.environ), codex=args.codex)
        if result:
            print(json.dumps(result))
    except Exception as exc:
        print("NOBLIVION context unavailable: " + type(exc).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
