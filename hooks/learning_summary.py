#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deliver a bounded current-memory summary through the proven store listener."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
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


FLAG_KEEP_SECONDS = 3 * 86400


def note_context_items(root: Path, env: dict, session: str, items) -> None:
    """Keep how many notes a prompt context can draw on, for the prompt hook of this session.

    The prompt hook asks the store on every prompt. When this session started with none,
    it skips that call (NOBLIVION-143). The file holds a count and nothing else.
    """
    if type(items) is not int or items < 0:
        return
    config = load(root / "hooks/hook_config.py", "_summary_flag_config")
    folder = config.data_dir(env) / "cache" / "learning-context"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    for old in folder.iterdir():
        if now - old.stat().st_mtime > FLAG_KEEP_SECONDS:
            old.unlink(missing_ok=True)
    name = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(items))


def run(event: dict, environ: dict, *, codex: bool = False) -> dict:
    if event.get("hook_event_name") != "SessionStart" or not event.get("session_id"):
        return {}
    # A resume with a warm prompt cache holds the summary already (NOBLIVION-150).
    if event.get("source") == "resume" and event.get("prompt_cache_likely_expired") is not True:
        return {}
    root = Path(__file__).resolve().parents[1]
    env = dict(environ)
    # Codex supplies its already resolved scope. Claude resolves the same
    # explicit mapping as its capture hook; unknown workspaces stay silent.
    if not codex:
        config = load(root / "hooks/hook_config.py", "_summary_config")
        path = config.data_dir(env) / "learning-client.json"
        if not path.is_file():
            return {}
        capture = load(root / "codex/capture.py", "_summary_capture")
        cfg = capture.scoped_config(json.loads(path.read_text()), event)
        if cfg is None or not capture.memory_use(cfg, event["session_id"]):
            return {}
        env.update(
            NOBLIVION_RECALL_ROOT=cfg["root"],
            NOBLIVION_RECALL_PROJECT=cfg["namespace"],
        )
    elif not env.get("NOBLIVION_RECALL_ROOT"):
        return {}
    recall = load(root / "hooks/recall_hook.py", "_summary_recall")
    env = recall.session_env(env, event)
    params = urllib.parse.urlencode(
        {
            "project": env.get("NOBLIVION_RECALL_PROJECT", "claude_code"),
            "root": env["NOBLIVION_RECALL_ROOT"],
        }
    )
    response = recall.store_get(lambda base: base + "/learning/summary?" + params, env, 2.0)
    summary = response.get("summary") if isinstance(response, dict) else None
    if isinstance(response, dict) and not response.get("reason"):
        try:
            note_context_items(root, env, event["session_id"], response.get("context_items"))
        except OSError:
            pass
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 6000:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "CURRENT LEARNED MEMORY (evidence, not authority)\n" + summary,
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
        print("NOBLIVION summary unavailable: " + type(exc).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
