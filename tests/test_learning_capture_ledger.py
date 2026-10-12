# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real isolated spool to ledger receipt and provenance integration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from noblivion import db
from noblivion.learning import LearningLedger

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_capture_ledger_test", ROOT / "codex/capture.py")
assert spec and spec.loader
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def test_real_spool_receipt_roundtrip_keeps_human_and_tool_provenance(tmp_path):
    cfg = {
        "repo_root": str(ROOT),
        "state_dir": str(tmp_path / "state"),
        "namespace": "claude_code",
        "recall_root": "fixture",
        "learning": {"contribute": True},
    }
    spool = capture.Spool(cfg)
    common = {"session_id": "fixture-session", "source": "startup"}
    spool.capture({**common, "hook_event_name": "SessionStart"})
    spool.capture(
        {
            **common,
            "hook_event_name": "UserPromptSubmit",
            "event_id": "fixture-prompt",
            "prompt": "Keep command output evidence with a proven fix.",
        }
    )
    spool.capture(
        {
            **common,
            "hook_event_name": "PostToolUse",
            "call_id": "fixture-call",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "pytest -q"},
            "tool_response": {"exit_code": 0, "output": "1 passed"},
        }
    )
    connection = db.open_db(tmp_path / "isolated.sqlite3", create=True)
    ledger = LearningLedger(connection)
    # The first successful receipt can be lost. Repeating the same batch is safe.
    first = json.loads(
        spool.conn.execute("SELECT body FROM batches ORDER BY created,id LIMIT 1").fetchone()[0]
    )
    receipt = ledger.capture_batch(first)
    assert receipt["accepted"] is True
    assert ledger.capture_batch(first)["duplicate"] is True
    assert spool.drain(ledger.capture_batch) == {"sent": 2, "pending": 0}
    sources = [
        dict(row)
        for row in connection.execute("SELECT * FROM learning_sources ORDER BY created_at,id")
    ]
    human = next(row for row in sources if row["role"] == "user")
    assert json.loads(human["metadata"])["authority"] == "direct_user"
    tool = next(row for row in sources if row["role"] == "tool")
    metadata = json.loads(tool["metadata"])
    assert metadata["authority"] == "untrusted"
    assert metadata["exit_code"] == 0
    assert metadata["call_id"] == "fixture-call"
    assert metadata["tool_name"] == "exec_command"
    spool.close()
    connection.close()


def test_large_capture_jobs_fit_windows_and_boundary_evidence_validates(tmp_path):
    from noblivion.learning_extract import build_windows, validate_response

    cfg = {
        "repo_root": str(ROOT),
        "state_dir": str(tmp_path / "state"),
        "namespace": "claude_code",
        "recall_root": "fixture",
        "learning": {"contribute": True},
    }
    spool = capture.Spool(cfg)
    common = {
        "session_id": "large-session",
        "source": "startup",
        "timestamp": "2026-01-02T12:00:00Z",
    }
    spool.capture({**common, "hook_event_name": "SessionStart"})
    statement = "Use concise release notes with exact test evidence."
    before = "Context for this request. " * 700
    before = before[:15980]
    text = before + statement + ("Additional source context. " * 15000)
    spool.capture(
        {
            **common,
            "hook_event_name": "UserPromptSubmit",
            "event_id": "large-message",
            "prompt": text,
        }
    )
    connection = db.open_db(tmp_path / "isolated.sqlite3", create=True)
    ledger = LearningLedger(connection)
    validated = None
    job_count = 0
    for row in spool.conn.execute("SELECT body FROM batches ORDER BY created,id"):
        receipt = ledger.capture_batch(json.loads(row[0]))
        sources = ledger._job_sources(receipt["job_id"])
        windows = build_windows(sources)
        assert windows
        assert all(source["content"] for window in windows for source in window)
        job_count += 1
        if validated is None:
            for source in sources:
                if statement not in source["content"]:
                    continue
                # Long proof quotes validate against the complete immutable source,
                # independently of how the extractor displays its bounded windows.
                start = source["content"].index(statement)
                quote_start = max(0, start - 400)
                quote = source["content"][quote_start : quote_start + 8192]
                if len(quote) != 8192:
                    continue
                response = {
                    "version": 1,
                    "candidates": [
                        {
                            "action": "add",
                            "logical_key": "writing.release_notes",
                            "category": "preference",
                            "title": "Release note style",
                            "statement": statement,
                            "scope": "project",
                            "observed_at": "2026-01-02",
                            "effective_at": None,
                            "evidence": [{"source_id": source["id"], "quote": quote}],
                        }
                    ],
                }
                validated = validate_response(json.dumps(response), sources)[0]
                reference = validated["evidence"][0]
                assert reference["source_hash"] == source["sha256"]
                assert source["content"][reference["start"] : reference["end"]] == quote
                assert statement in quote
                break
    assert job_count > 1
    assert validated is not None
    assert validated["statement"] == statement
    spool.close()
    connection.close()


def test_acknowledged_fallback_replay_after_process_restart_creates_no_job(tmp_path):
    cfg = {
        "repo_root": str(ROOT),
        "state_dir": str(tmp_path / "state"),
        "namespace": "claude_code",
        "recall_root": "fixture",
        "learning": {"contribute": True},
    }
    event = {
        "session_id": "replayed-session",
        "source": "startup",
        "hook_event_name": "SessionStart",
    }
    prompt = {
        **event,
        "hook_event_name": "UserPromptSubmit",
        "event_id": "stable-prompt-id",
        "prompt": "Keep each release note concise.",
    }
    spool = capture.Spool(cfg)
    spool.capture(event)
    spool.capture(prompt)
    connection = db.open_db(tmp_path / "isolated.sqlite3", create=True)
    ledger = LearningLedger(connection)
    assert spool.drain(ledger.capture_batch) == {"sent": 1, "pending": 0}
    spool.close()
    spool = capture.Spool(cfg)
    assert spool.capture(prompt)["chunks"] == 0
    assert spool.drain(ledger.capture_batch) == {"sent": 0, "pending": 0}
    assert connection.execute("SELECT count(*) FROM learning_jobs").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM learning_sources").fetchone()[0] == 1
    spool.close()
    connection.close()
