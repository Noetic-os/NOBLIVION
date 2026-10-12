# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lossless capture, provenance, scope, and offline receipts. No live services."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_capture_test", ROOT / "codex/capture.py")
assert spec and spec.loader
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


@pytest.fixture
def cfg(tmp_path):
    return {
        "repo_root": str(ROOT),
        "state_dir": str(tmp_path / "state"),
        "data_dir": str(tmp_path / "data"),
        "namespace": "claude_code",
        "recall_root": "sample",
        "learning": {"contribute": True, "use": True},
        "project_mappings": [
            {"workspace": str(tmp_path / "project"), "namespace": "claude_code", "root": "sample"}
        ],
    }


def event(cfg, name="SessionStart", **extra):
    return {
        "hook_event_name": name,
        "source": "startup",
        "session_id": "new-session",
        "cwd": cfg["project_mappings"][0]["workspace"],
        **extra,
    }


def message(text, role="user"):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    }


def write_rows(path, rows):
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row) + "\n")


def records(spool):
    batches = [
        json.loads(row[0])
        for row in spool.conn.execute("SELECT body FROM batches ORDER BY created,id")
    ]
    return [record for batch in batches for record in batch["records"]]


def reassemble(chunks):
    text = ""
    for chunk in sorted(chunks, key=lambda value: value["part_index"]):
        start, end = chunk["record_start"], chunk["record_end"]
        assert start <= len(text)
        assert end - start == len(chunk["content"])
        overlap = len(text) - start
        assert text[start:] == chunk["content"][:overlap]
        text += chunk["content"][overlap:]
    return text


def test_native_tail_full_unicode_and_restart_cursor_are_lossless(cfg, tmp_path):
    text = "a" * 44000 + "é" * 9000 + "final evidence beyond former truncation"
    path = tmp_path / "native.jsonl"
    write_rows(path, [message(text)])
    spool = capture.Spool(cfg)
    assert spool.capture(event(cfg, transcript_path=str(path)))["chunks"] > 4
    chunks = records(spool)
    assert reassemble(chunks) == text
    assert all(chunk["source_hash"] == capture.digest(chunk["content"]) for chunk in chunks)
    assert all(chunk["record_hash"] == capture.digest(text) for chunk in chunks)
    assert chunks[0]["metadata"]["authority"] == "direct_user"
    spool.close()
    spool = capture.Spool(cfg)
    assert spool.capture(event(cfg, "Stop", transcript_path=str(path)))["chunks"] == 0
    write_rows(path, [message("The complete assistant tail.", "assistant")])
    assert spool.capture(event(cfg, "SessionEnd", transcript_path=str(path)))["chunks"] == 1
    assert records(spool)[-1]["content"] == "The complete assistant tail."
    spool.close()


def test_partial_native_record_waits_without_acknowledging_bytes(cfg, tmp_path):
    path = tmp_path / "native.jsonl"
    raw = json.dumps(message("Complete only after the next hook."))
    path.write_text(raw[:25])
    spool = capture.Spool(cfg)
    spool.capture(event(cfg, transcript_path=str(path)))
    assert not records(spool)
    with path.open("a") as output:
        output.write(raw[25:] + "\n")
    spool.capture(event(cfg, "Stop", transcript_path=str(path)))
    assert records(spool)[0]["content"] == "Complete only after the next hook."
    spool.close()


def test_source_cursor_and_spool_commit_are_atomic(cfg, tmp_path, monkeypatch):
    path = tmp_path / "native.jsonl"
    write_rows(path, [message("Evidence survives a failed local queue write.")])
    spool = capture.Spool(cfg)
    original = spool._queue

    def fail(*args):
        original(*args)
        raise OSError("fixture crash before commit")

    monkeypatch.setattr(spool, "_queue", fail)
    with pytest.raises(OSError):
        spool.capture(event(cfg, transcript_path=str(path)))
    assert not records(spool)
    assert spool.conn.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
    monkeypatch.setattr(spool, "_queue", original)
    spool.capture(event(cfg, transcript_path=str(path)))
    assert len(records(spool)) == 1
    spool.close()


@pytest.mark.parametrize("source", ["resume", "compact", "unknown", ""])
def test_existing_sessions_are_never_swept_on_activation(cfg, tmp_path, source):
    path = tmp_path / "old.jsonl"
    write_rows(path, [message("An old transcript must remain untouched.")])
    spool = capture.Spool(cfg)
    assert (
        spool.capture(event(cfg, source=source, transcript_path=str(path)))["status"]
        == "not_new_session"
    )
    assert not records(spool)
    assert (
        spool.capture(event(cfg, "Stop", transcript_path=str(path)))["status"] == "not_new_session"
    )
    spool.close()


def test_native_tool_ids_names_and_observed_exit_code_survive(cfg, tmp_path):
    path = tmp_path / "native.jsonl"
    write_rows(
        path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-real",
                    "arguments": json.dumps({"cmd": "python -m pytest -q"}),
                },
            }
        ],
    )
    spool = capture.Spool(cfg)
    spool.capture(event(cfg, transcript_path=str(path)))
    write_rows(
        path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-real",
                    "output": "Process exited with code 1\nFAILED check_one\n" + "details" * 2000,
                },
            }
        ],
    )
    spool.capture(event(cfg, "Stop", transcript_path=str(path)))
    result = records(spool)[-1]
    assert result["role"] == "tool"
    assert result["tool_name"] == "exec_command"
    assert result["call_id"] == "call-real"
    assert result["metadata"]["exit_code"] == 1
    assert result["metadata"]["command"] == "python -m pytest -q"
    assert result["metadata"]["authority"] == "untrusted"
    assert result["content"].endswith("details" * 2000)
    spool.close()


def test_custom_tool_and_claude_tool_result_are_not_human_claims():
    native = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "call_id": "patch-id",
            "input": "*** Begin Patch\n*** End Patch",
        },
    }
    assert capture.normalized(native, "codex")[0]["call_id"] == "patch-id"
    claude = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "agent-id",
                    "content": "An untrusted agent report.",
                    "is_error": False,
                }
            ]
        },
    }
    assert capture.normalized(claude, "claude")[0]["role"] == "tool"


def test_unknown_hook_tools_and_full_tails_are_captured(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    spool.capture(
        event(
            cfg,
            "PostToolUse",
            tool_name="Agent",
            call_id="agent-call",
            tool_input={"message": "Review this change."},
            tool_response="A" * 50000 + "test evidence at tail",
        )
    )
    rows = records(spool)
    result = [r for r in rows if r["role"] == "tool"]
    assert reassemble(result) == "A" * 50000 + "test evidence at tail"
    assert all(r["call_id"] == "agent-call" for r in rows)
    spool.close()


def test_secret_rejected_before_spool_and_cursor_advances(cfg, tmp_path):
    path = tmp_path / "native.jsonl"
    secret = "a-fictional-fixture-value"
    write_rows(path, [message("The password is " + secret), message("Use concise explanations.")])
    spool = capture.Spool(cfg)
    spool.capture(event(cfg, transcript_path=str(path)))
    assert len(records(spool)) == 1
    assert records(spool)[0]["content"] == "Use concise explanations."
    assert spool.conn.execute("SELECT count(*) FROM rejected").fetchone()[0] == 1
    assert not any(secret in str(tuple(row)) for row in spool.conn.execute("SELECT * FROM batches"))
    assert spool.capture(event(cfg, "Stop", transcript_path=str(path)))["chunks"] == 0
    spool.close()


def test_offline_or_ambiguous_receipt_keeps_exact_batch_for_retry(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    spool.capture(
        event(cfg, "UserPromptSubmit", prompt="Use two spaces in examples.", event_id="prompt-id")
    )
    body = spool.conn.execute("SELECT body FROM batches").fetchone()[0]

    def offline(payload):
        raise TimeoutError("fixture transport failure")

    assert spool.drain(offline) == {"sent": 0, "pending": 1}
    spool.conn.execute("UPDATE batches SET next_attempt=0")
    spool.conn.commit()
    assert spool.drain(lambda payload: {"accepted": True, "batch_id": "wrong"})["pending"] == 1
    assert spool.conn.execute("SELECT body FROM batches").fetchone()[0] == body
    spool.conn.execute("UPDATE batches SET next_attempt=0")
    spool.conn.commit()
    assert spool.drain(lambda payload: {"accepted": True, "batch_id": payload["batch_id"]}) == {
        "sent": 1,
        "pending": 0,
    }
    spool.close()


def test_duplicate_hook_with_native_id_is_idempotent(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    prompt = event(cfg, "UserPromptSubmit", prompt="Keep source evidence.", event_id="stable-id")
    spool.capture(prompt)
    spool.capture(prompt)
    assert len(records(spool)) == 1
    spool.close()


def test_explicit_longest_scope_and_separate_use_contribution(cfg):
    workspace = cfg["project_mappings"][0]["workspace"]
    assert capture.scoped_config(cfg, {"cwd": workspace + "-other"}) is None
    cfg["project_mappings"].append(
        {"workspace": workspace + "/nested", "namespace": "claude_code", "root": "nested"}
    )
    assert capture.scoped_config(cfg, {"cwd": workspace + "/nested/src"})["recall_root"] == "nested"
    cfg["learning"].update(use=False, contribute=True)
    assert capture.allowed(cfg)
    assert not capture.memory_use(cfg)
    cfg["learning"].update(use=True, contribute=False)
    assert not capture.allowed(cfg)
    assert capture.memory_use(cfg)


def test_pause_blocks_already_queued_delivery(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    spool.capture(event(cfg, "UserPromptSubmit", prompt="Keep short notes."))
    cfg["learning"]["paused"] = True

    def unexpected(payload):
        pytest.fail("paused content was sent")

    assert spool.drain(unexpected)["pending"] == 1
    spool.close()


def test_spools_are_isolated_per_namespace_and_root(cfg):
    first = capture.Spool(cfg)
    first.capture(event(cfg))
    first.capture(event(cfg, "UserPromptSubmit", prompt="A scoped preference."))
    second = capture.Spool({**cfg, "recall_root": "another"})
    assert not records(second)
    first.close()
    second.close()


def test_spool_limit_rolls_back_source_and_cursor_without_discarding_pending(cfg, tmp_path):
    path = tmp_path / "native.jsonl"
    write_rows(path, [message("Keep this accepted source.")])
    spool = capture.Spool(cfg)
    spool.capture(event(cfg, transcript_path=str(path)))
    old_offset = spool.conn.execute("SELECT offset FROM cursors").fetchone()[0]
    cfg["learning"]["max_spool_bytes"] = 1
    write_rows(path, [message("This source must remain retryable.")])
    with pytest.raises(capture.SpoolFull):
        spool.capture(event(cfg, "Stop", transcript_path=str(path)))
    assert len(records(spool)) == 1
    assert spool.conn.execute("SELECT offset FROM cursors").fetchone()[0] == old_offset
    cfg["learning"]["max_spool_bytes"] = 100000
    spool.capture(event(cfg, "Stop", transcript_path=str(path)))
    assert len(records(spool)) == 2
    spool.close()


def test_exclude_erases_pending_content_and_prevents_backfill(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    spool.capture(event(cfg, "UserPromptSubmit", prompt="A contribution to withdraw."))
    spool.exclude_session("new-session")
    assert spool.status()["pending"] == 0
    assert (
        spool.capture(event(cfg, "UserPromptSubmit", prompt="Do not backfill."))["status"]
        == "not_new_session"
    )
    spool.close()


def test_pause_then_resume_requires_a_new_session(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    cfg["learning"]["paused"] = True
    assert (
        spool.capture(event(cfg, "UserPromptSubmit", prompt="During pause."))["status"]
        == "disabled"
    )
    cfg["learning"]["paused"] = False
    assert (
        spool.capture(event(cfg, "UserPromptSubmit", prompt="Same old session."))["status"]
        == "not_new_session"
    )
    assert spool.capture(event(cfg, session_id="fresh-session"))["status"] == "captured"
    spool.close()


def test_internal_instructions_are_not_learning_evidence():
    assert capture.normalized(message("Internal instruction.", "developer"), "codex") == []
    assert capture.normalized(message("Internal instruction.", "system"), "codex") == []


def test_full_records_over_extractor_limit_make_bounded_lossless_batches(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    text = ("Detailed source observations. " * 15000) + "the final source byte"
    assert len(text) > 262144
    spool.capture(event(cfg, "UserPromptSubmit", prompt=text, event_id="large-record"))
    batches = [json.loads(row[0]) for row in spool.conn.execute("SELECT body FROM batches")]
    assert len(batches) > 1
    assert all(
        sum(len(r["content"]) for r in b["records"]) <= capture.BATCH_CONTENT_CHARS for b in batches
    )
    assert reassemble(records(spool)) == text
    assert all(r["record_hash"] == capture.digest(text) for r in records(spool))
    spool.close()


def test_every_maximum_evidence_quote_fits_an_overlapping_chunk(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    text = "Complete source evidence. " * 5000
    spool.capture(event(cfg, "UserPromptSubmit", prompt=text, event_id="boundary-record"))
    chunks = records(spool)
    assert reassemble(chunks) == text
    # The worst case begins one character after a preceding chunk can hold it.
    starts = {0, len(text) - 8192}
    starts.update(r["record_end"] - 8192 + 1 for r in chunks if r["record_end"] < len(text))
    for start in starts:
        assert any(r["record_start"] <= start and start + 8192 <= r["record_end"] for r in chunks)
    spool.close()


def test_paused_sessions_cannot_starve_later_sessions_across_pages(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    for index in range(130):
        spool.capture(
            event(
                cfg,
                "UserPromptSubmit",
                prompt="Keep this paused contribution.",
                event_id="paused-prompt-" + str(index),
            )
        )
    spool.capture(event(cfg, session_id="eligible-session"))
    for index in range(3):
        spool.capture(
            event(
                cfg,
                "UserPromptSubmit",
                session_id="eligible-session",
                prompt="Deliver this eligible contribution.",
                event_id="active-" + str(index),
            )
        )
    cfg["learning"]["sessions"] = {"new-session": {"paused": True}}
    sent = []

    def acknowledge(payload):
        sent.append(payload)
        return {"batch_id": payload["batch_id"], "accepted": True}

    assert spool.drain(acknowledge, limit=2) == {"sent": 2, "pending": 131}
    assert spool.drain(acknowledge, limit=2) == {"sent": 1, "pending": 130}
    assert all(payload["session_id"] == "eligible-session" for payload in sent)
    assert spool.status()["failed_delivery"] == 0
    cfg["learning"]["sessions"]["new-session"]["paused"] = False
    assert spool.drain(acknowledge, limit=200) == {"sent": 130, "pending": 0}
    spool.close()


def test_acknowledged_tool_replay_keeps_receipt_and_stable_call_id(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    tool = event(
        cfg,
        "PostToolUse",
        event_id="stable-tool-event",
        tool_name="exec_command",
        tool_input={"cmd": "pytest -q"},
        tool_response={"exit_code": 0, "output": "2 passed"},
    )
    spool.capture(tool)
    original = records(spool)
    assert all(record["call_id"] == "stable-tool-event" for record in original)

    def acknowledge(body):
        return {"accepted": True, "batch_id": body["batch_id"]}

    assert spool.drain(acknowledge) == {"sent": 1, "pending": 0}
    spool.close()
    spool = capture.Spool(cfg)
    assert spool.capture(tool)["chunks"] == 0
    assert spool.status()["pending"] == 0
    with pytest.raises(ValueError, match="hook event identity collision"):
        spool.capture({**tool, "tool_response": {"exit_code": 1, "output": "different result"}})
    assert spool.status()["pending"] == 0
    spool.close()


def test_hook_receipt_rolls_back_when_capacity_refuses_capture(cfg):
    spool = capture.Spool(cfg)
    spool.capture(event(cfg))
    cfg["learning"]["max_spool_bytes"] = 1
    prompt = event(
        cfg, "UserPromptSubmit", event_id="retryable-prompt", prompt="Keep useful context."
    )
    with pytest.raises(capture.SpoolFull):
        spool.capture(prompt)
    assert spool.conn.execute("SELECT count(*) FROM hook_receipts").fetchone()[0] == 0
    cfg["learning"]["max_spool_bytes"] = 100000
    assert spool.capture(prompt)["chunks"] == 1
    assert spool.conn.execute("SELECT count(*) FROM hook_receipts").fetchone()[0] == 1
    spool.close()
