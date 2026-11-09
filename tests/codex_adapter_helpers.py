# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared isolated setup for Codex adapter and miner integration tests."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_noblivion_codex_adapter", ROOT / "codex/adapter.py")
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for name in tuple(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for folder in ("home", "data", "memory", "state", "project"):
        (tmp_path / folder).mkdir()
    (tmp_path / "data/config.json").write_text(
        json.dumps(
            {
                "memory_dirs": [str(tmp_path / "memory")],
                "namespace": "claude_code",
            }
        ),
        encoding="utf-8",
    )
    return {
        "data_dir": str(tmp_path / "data"),
        "memory_dir": str(tmp_path / "memory"),
        "state_dir": str(tmp_path / "state"),
        "namespace": "claude_code",
        "repo_root": str(ROOT),
        "workspace_prefixes": [str(tmp_path / "project")],
    }


def event(cfg: dict, name: str, **fields: object) -> dict:
    return {
        "session_id": "thr_codex_test_123",
        "cwd": cfg["workspace_prefixes"][0],
        "hook_event_name": name,
        **fields,
    }
