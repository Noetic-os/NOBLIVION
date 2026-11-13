#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enqueue new Claude sessions with explicit scope; never run a model in a hook."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        root = Path(__file__).resolve().parent.parent
        hook_config = load(root / "hooks/hook_config.py", "_learning_hook_config")
        path = hook_config.data_dir() / "learning-client.json"
        if not path.is_file():
            return 0
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg.update(repo_root=str(root), source_config=str(path))
        event = json.load(sys.stdin)
        capture = load(root / "codex/capture.py", "_learning_capture")
        capture.capture_hook(cfg, event, "claude")
    except Exception as exc:
        print("NOBLIVION capture deferred: " + type(exc).__name__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from input_check import run as _checked  # NOBLIVION-118
    except Exception:  # noqa: BLE001 - no check: the hook runs as before
        def _checked(_hook, fn, notify=True): return fn()
    raise SystemExit(_checked("learning_capture", main))
