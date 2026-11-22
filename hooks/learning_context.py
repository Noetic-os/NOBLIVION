#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deliver relevant evidence-linked context through the proven store listener."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
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


FLAG_MAX_AGE = 86400


def no_context_items(root: Path, env: dict, session: str) -> bool:
    """True when the session-start hook found nothing a prompt context could draw on.

    The store call then costs a round trip for an empty answer (NOBLIVION-143). A missing,
    old or unreadable file means unknown, so the call goes ahead.
    """
    try:
        config = load(root / "hooks/hook_config.py", "_context_flag_config")
        name = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
        path = config.data_dir(env) / "cache" / "learning-context" / name
        if time.time() - path.stat().st_mtime > FLAG_MAX_AGE:
            return False
        return path.read_text(encoding="utf-8").strip() == "0"
    except (OSError, ValueError, AttributeError):
        return False


SHOWN_MAX = 2000
CLAIM_KEY = re.compile(r"\[claim:(\S+) revision:(\d+)")


def shown_path(root: Path, env: dict, session: str) -> Path:
    config = load(root / "hooks/hook_config.py", "_context_shown_config")
    name = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    return config.data_dir(env) / "cache" / "learning-shown" / name


def drop_shown(context: str, shown: set) -> tuple:
    """Remove the notes this session already received. Return the text and the new keys.

    A note counts as shown by claim id plus revision, so a changed note appears again.
    A row without a claim key (an open conflict) always stays.
    """
    entries = []
    for line in context.split("\n"):
        if line.startswith("- ") or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    kept, new = [], []
    for entry in entries:
        text = "\n".join(entry)
        found = CLAIM_KEY.search(text)
        key = found.group(1) + "@" + found.group(2) if found else None
        if key is not None and key in shown:
            continue
        if key is not None:
            new.append(key)
        kept.append(text)
    return "\n".join(kept), new


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
    if no_context_items(root, env, event["session_id"]):
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
    # A note shown once in this session is not shown again (NOBLIVION-160 follow-up).
    # The file is a convenience: any read or write error means "show everything".
    seen, path = set(), None
    try:
        path = shown_path(root, env, event["session_id"])
        seen = set(path.read_text(encoding="utf-8").split())
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        path = None
    context, new = drop_shown(context, seen)
    if not context.strip():
        return {}
    if path is not None and new and len(seen) < SHOWN_MAX:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(new) + "\n")
        except OSError:
            pass
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
