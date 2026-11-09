# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal redaction through the stdlib-only Codex adapter boundary."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_codex_journal_adapter", ROOT / "codex/adapter.py")
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
OMITTED = "[REDACTED: unsafe evidence]"


@pytest.fixture
def cfg(tmp_path):
    return {
        "repo_root": str(ROOT),
        "memory_dir": str(tmp_path / "memory"),
        "state_dir": str(tmp_path / "state"),
        "recall_root": "fixture-root",
    }


def journal(cfg, payload):
    event = {
        "hook_event_name": "PostToolUse",
        "session_id": "fixture-session",
        "cwd": "/work/fixture",
        "tool_name": "fixture_tool",
        "tool_use_id": "fixture-call",
        "tool_input": payload,
        "tool_response": {"output": "done"},
    }
    return adapter._journal(cfg, event, [])


def recorded_input(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "assistant"
    call = rows[0]["message"]["content"][0]
    assert call["type"] == "tool_use"
    assert call["id"] == "fixture-call"
    assert rows[1]["message"]["content"][0]["type"] == "tool_result"
    return call["input"]


@pytest.mark.parametrize(
    "secret",
    [
        "fixture-secret-value",
        987654321,
        ["fixture-secret-value"],
        {"value": "fixture-secret-value"},
    ],
)
def test_journal_masks_structured_secrets_without_losing_safe_fields(cfg, secret):
    payload = {
        "items": [{"password": secret, "label": "keep"}],
        "count": 100,
        "token": 12,
    }
    path = journal(cfg, payload)
    assert recorded_input(path) == {
        "items": [{"password": OMITTED, "label": "keep"}],
        "count": 100,
        "token": 12,
    }
    raw = path.read_text(encoding="utf-8")
    assert "fixture-secret-value" not in raw
    assert "987654321" not in raw


@pytest.mark.parametrize(
    "text",
    [
        'password="fixture-secret-value"',
        "The password is fixture-secret-value",
        'password="fixture-escaped\\"value"',
    ],
)
def test_journal_keeps_decoded_string_secret_checks(cfg, text):
    path = journal(cfg, {"details": text, "label": 'keep "quoted" text\\path'})
    assert recorded_input(path) == {"details": OMITTED, "label": 'keep "quoted" text\\path'}
    assert "fixture-secret-value" not in path.read_text(encoding="utf-8")
    assert "fixture-escaped" not in path.read_text(encoding="utf-8")


def test_journal_scrubs_capabilities_and_redaction_remains_idempotent(cfg):
    payload = {
        "read_capability": {"invalid": "fixture-value"},
        "nested": [{"read_capability": 123}],
        "text": "before nobrc1_fixture123 after",
    }
    expected = {
        "read_capability": "[capability removed]",
        "nested": [{"read_capability": "[capability removed]"}],
        "text": "before [capability removed] after",
    }
    assert recorded_input(journal(cfg, payload)) == expected
    redacted = adapter._redact(cfg, {"password": "fixture-secret-value", "safe": "keep"})
    assert adapter._redact(cfg, redacted) == redacted


def test_journal_reuses_one_redactor_per_repository(cfg, tmp_path, monkeypatch):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        target = root / "src" / "noblivion" / "redaction.py"
        target.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "src" / "noblivion" / "redaction.py", target)
    adapter._redactor.cache_clear()
    loaded = []
    real = adapter.importlib.util.spec_from_file_location

    def counted(name, path, *args, **kwargs):
        if name == "noblivion_codex_redaction":
            loaded.append(Path(path))
        return real(name, path, *args, **kwargs)

    monkeypatch.setattr(adapter.importlib.util, "spec_from_file_location", counted)
    for root in (roots[0], roots[0], roots[1]):
        journal({**cfg, "repo_root": str(root)}, {"nested": [{"safe": "keep"}]})
    assert loaded == [root / "src" / "noblivion" / "redaction.py" for root in roots]


def test_journal_does_not_append_when_contextual_redaction_fails(cfg, monkeypatch):
    path = journal(cfg, {"label": "already recorded"})
    before = path.read_bytes()
    redactor = adapter._redactor(ROOT.resolve())

    def fail(_text):
        raise RuntimeError("fixture redaction failure")

    monkeypatch.setattr(redactor, "would_change", fail)
    with pytest.raises(RuntimeError, match="fixture redaction failure"):
        journal(cfg, {"password": "fixture-secret-value"})
    assert path.read_bytes() == before
