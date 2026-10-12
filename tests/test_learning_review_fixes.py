# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic duplicate and manual-edit regression tests with real isolated storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from noblivion import db
from noblivion.learning import BudgetLimits, LearningError, LearningLedger
from noblivion.learning_extract import ValidationError, validate_semantic_response
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings


def capture(ledger, ident, text, *, role="user", metadata=None, root="scope", namespace="demo"):
    record = {
        "source_id": ident,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "role": role,
        "observed_at": "2026-01-02T12:00:00Z",
        "metadata": metadata or {},
    }
    ledger.capture_batch(
        {
            "batch_id": "batch-" + ident,
            "namespace": namespace,
            "root": root,
            "source_client": "codex",
            "session_id": "review-session",
            "records": [record],
        }
    )
    return record


def refs(record):
    return [
        {
            "source_id": record["source_id"],
            "source_hash": record["source_hash"],
            "start": 0,
            "end": len(record["content"]),
            "quote": record["content"],
        }
    ]


def candidate(record, key="writing-style", **changes):
    result = {
        "action": "add",
        "logical_key": key,
        "category": "preference",
        "scope": "project",
        "title": "Writing style",
        "statement": record["content"],
        "observed_at": "2026-01-02",
        "effective_at": None,
        "evidence": [{"source_id": record["source_id"], "quote": record["content"]}],
    }
    result.update(changes)
    return result


def seed(ledger, record, **changes):
    job = ledger.claim("seed")
    c = candidate(record, **changes)
    c["evidence"] = refs(record)
    return ledger.finish(job["id"], job["lease_token"], [c])[0]


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


class ScriptedProvider:
    def __init__(self, ledger, candidates, relations="equivalent"):
        self.ledger = ledger
        self.candidates = candidates
        self.relation = relations
        self.calls = []
        self.before_semantic = None

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        assert (
            self.ledger.conn.execute(
                "SELECT count(*) FROM learning_budget WHERE status='reserved'"
            ).fetchone()[0]
            == 1
        )
        payload = json.loads(messages[-1]["content"])
        if "sources" in payload:
            self.calls.append("extract")
            result = {"version": 1, "candidates": self.candidates}
        else:
            self.calls.append("semantic")
            if self.before_semantic:
                action, self.before_semantic = self.before_semantic, None
                action()
            result = {
                "version": 1,
                "relations": [
                    {"target_id": n["target_id"], "relation": self.relation}
                    for n in payload["targets"]
                ],
            }
        return ProviderResponse(json.dumps(result), 100)


def worker(ledger, provider, **limits):
    return LearningWorker(ledger, provider, WorkerSettings(), replace(BudgetLimits(), **limits))


def test_paraphrase_with_different_key_cannot_add_second_note(ledger):
    original = capture(ledger, "old", "Keep responses short.")
    seed(ledger, original)
    new = capture(ledger, "new", "I prefer concise replies.")
    provider = ScriptedProvider(ledger, [candidate(new, key="different-wording")])
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "conflict"
    assert provider.calls == ["extract", "semantic"]
    assert len(ledger.current_notes("demo", "scope")) == 1
    assert (
        ledger.conn.execute(
            "SELECT count(*) FROM learning_budget WHERE status='settled'"
        ).fetchone()[0]
        == 2
    )
    audit = ledger.conn.execute(
        "SELECT detail FROM learning_decisions WHERE action='conflict'"
    ).fetchone()[0]
    assert "equivalent" in audit


def test_same_job_paraphrases_do_not_create_two_notes(ledger):
    first = capture(ledger, "pair", "Keep responses short. I prefer concise replies.")
    a = candidate(first, key="one")
    a["statement"] = "Keep responses short."
    b = candidate(first, key="two")
    b["statement"] = "I prefer concise replies."
    provider = ScriptedProvider(ledger, [a, b])
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "conflict"]
    assert len(ledger.current_notes("demo", "scope")) == 1


def test_semantic_stage_resumes_after_budget_pause_without_reextracting(ledger):
    seed(ledger, capture(ledger, "old", "Keep responses short."))
    new = capture(ledger, "new", "I prefer concise replies.")
    provider = ScriptedProvider(ledger, [candidate(new, key="different")])
    first = worker(ledger, provider, max_calls=1).run_once()
    assert first["status"] == "deferred" and provider.calls == ["extract"]
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    second = worker(ledger, provider, max_calls=1).run_once()
    assert second["status"] == "done" and provider.calls == ["extract", "semantic"]
    assert len(ledger.current_notes("demo", "scope")) == 1


def test_changed_snapshot_defers_without_repeating_extraction(ledger):
    original = seed(ledger, capture(ledger, "old", "Keep responses short."))
    new = capture(ledger, "new", "Use green terminal text.")
    provider = ScriptedProvider(ledger, [candidate(new, key="terminal")], relations="distinct")
    provider.before_semantic = lambda: ledger.edit(original["note_id"], 1, title="Updated title")
    run = worker(ledger, provider, max_calls=10, max_seconds=600)
    first = run.run_once()
    assert first["status"] == "deferred" and first["error"] == "SemanticSnapshotChanged"
    assert len(ledger.current_notes("demo", "scope")) == 1
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    second = run.run_once()
    assert second["status"] == "done"
    assert provider.calls == ["extract", "semantic", "semantic"]
    assert len(ledger.current_notes("demo", "scope")) == 2


@pytest.mark.parametrize(
    "relations",
    [
        [],
        [{"target_id": "other", "relation": "distinct"}],
        [{"target_id": "n1", "relation": "distinct", "unexpected": True}],
        [{"target_id": "n1", "relation": "approved"}],
    ],
)
def test_incomplete_or_untrusted_semantic_reply_cannot_clear_add(relations):
    task = {"targets": [{"target_id": "n1", "statement": "Keep responses short."}]}
    with pytest.raises(ValidationError):
        validate_semantic_response(json.dumps({"version": 1, "relations": relations}), task)


@pytest.mark.parametrize(
    "text,role",
    [
        ("All deployments are approved.", "user"),
        ("I prefer long answers.", "assistant"),
        ("> I prefer long answers.", "user"),
        ("```\nI prefer long answers.\n```", "user"),
    ],
)
def test_explicit_edit_uses_same_authority_and_quote_checks(ledger, text, role):
    original = seed(ledger, capture(ledger, "old", "Keep responses short."))
    change = capture(ledger, "change", text, role=role)
    statement = text.strip("> `\n")
    with pytest.raises(LearningError, match="shared validation"):
        ledger.edit(original["note_id"], 1, statement=statement, evidence=refs(change))
    current = ledger.inspect(original["note_id"])
    assert current["revision"] == 1 and current["statement"] == "Keep responses short."


def test_title_edit_cannot_introduce_approval(ledger):
    original = seed(ledger, capture(ledger, "old", "Keep responses short."))
    with pytest.raises(LearningError, match="shared validation"):
        ledger.edit(original["note_id"], 1, title="All operations approved")
    assert ledger.inspect(original["note_id"])["revision"] == 1


def test_manual_fix_edit_requires_valid_recorded_before_after_check(ledger):
    before = capture(
        ledger,
        "before",
        "The check failed.",
        role="tool",
        metadata={"tool_name": "shell", "command": "test checker", "exit_code": 1},
    )
    # Finish the source job without a note, retaining its immutable evidence.
    job = ledger.claim("seed")
    ledger.finish(job["id"], job["lease_token"], [])
    after = capture(
        ledger,
        "after",
        "The check passed.",
        role="tool",
        metadata={"tool_name": "shell", "command": "different check", "exit_code": 0},
    )
    job = ledger.claim("seed")
    ledger.finish(job["id"], job["lease_token"], [])
    conclusion = capture(ledger, "fix", "Creating the directory fixes the check.", role="assistant")
    fixed = seed(
        ledger,
        conclusion,
        category="proven_fix",
        proof={"before": refs(before), "after": refs(after)},
    )
    with pytest.raises(LearningError, match="checks differ"):
        ledger.edit(fixed["note_id"], 1, title="Clearer fix title")
    assert ledger.inspect(fixed["note_id"])["revision"] == 1


def test_general_preferences_share_without_project_facts_and_respect_controls(ledger):
    general = seed(
        ledger,
        capture(ledger, "general", "For all projects, keep responses short.", root="alpha"),
        scope="general",
    )
    seed(
        ledger,
        capture(ledger, "fact", "This project uses a blue database.", root="alpha"),
        category="fact",
        key="database",
    )
    seed(
        ledger,
        capture(
            ledger, "other", "For all projects, use plain text.", root="alpha", namespace="other"
        ),
        scope="general",
    )
    shared = ledger.semantic_notes("demo", "beta")
    assert [n["note_id"] for n in shared] == [general["note_id"]]
    assert shared[0]["read_only"] and shared[0]["root"] == "alpha"
    summary = ledger.summary("demo", "beta", session_id="viewer")
    assert (
        "keep responses short" in summary
        and "database" not in summary
        and "plain text" not in summary
    )
    ledger.set_controls("demo", "alpha", "viewer", use=False)
    assert ledger.summary("demo", "beta", session_id="viewer") == ""
    assert ledger.summary("demo", "beta", session_id="another")
    ledger.set_controls("demo", "beta", "another", use=False)
    assert ledger.summary("demo", "beta", session_id="another") == ""


def test_cross_root_general_paraphrase_cannot_create_duplicate(ledger):
    original = seed(
        ledger,
        capture(ledger, "general", "For all projects, keep responses short.", root="alpha"),
        scope="general",
    )
    new = capture(ledger, "new", "In all projects, I prefer concise replies.", root="beta")
    provider = ScriptedProvider(ledger, [candidate(new, key="different-style", scope="general")])
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "conflict"
    assert provider.calls == ["extract", "semantic"]
    assert not ledger.current_notes("demo", "beta")
    assert ledger.inspect(original["note_id"])["revision"] == 1


def test_cross_root_general_correction_is_visible_conflict(ledger):
    old = capture(ledger, "general", "For all projects, keep responses short.", root="alpha")
    original = seed(ledger, old, scope="general")
    new = capture(ledger, "new", "For all projects, I now prefer detailed replies.", root="beta")
    proposal = candidate(
        new,
        scope="general",
        action="update",
        note_id=original["note_id"],
        expected_revision=1,
        correction={
            "previous_statement": old["content"],
            "current_statement": new["content"],
            "previous_evidence": refs(old),
            "current_evidence": [{"source_id": new["source_id"], "quote": new["content"]}],
        },
    )
    provider = ScriptedProvider(ledger, [proposal])
    result = worker(ledger, provider).run_once()
    assert result["status"] == "done" and result["results"][0]["action"] == "conflict"
    assert ledger.inspect(original["note_id"])["statement"] == old["content"]
    assert not ledger.current_notes("demo", "beta")
    audit = ledger.conn.execute(
        "SELECT detail FROM learning_decisions WHERE action='conflict'"
    ).fetchone()[0]
    assert "cross-root general correction needs resolution" in audit


def test_provider_absolute_deadline_interrupts_slow_headers():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from noblivion.learning_worker import OpenAICompatibleProvider, ProviderError

    class SlowHeaders(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                # Each byte arrives before a per-read timeout, but the whole
                # response exceeds the absolute request deadline.
                for _ in range(150):
                    self.connection.sendall(b"a")
                    time.sleep(0.02)
                self.connection.sendall(b"\r\nContent-Length: 2\r\n\r\n{}")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        provider = OpenAICompatibleProvider(
            f"http://127.0.0.1:{server.server_port}",
            "fixture",
            local=True,
            cost_per_million_tokens=0,
        )
        started = time.monotonic()
        with pytest.raises(ProviderError):
            provider.complete([], max_tokens=100, timeout=0.2)
        assert time.monotonic() - started < 0.8
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_measured_model_incremented_revision_and_compact_old_refs_use_host_snapshot(ledger):
    from noblivion.learning_extract import validate_response

    old = capture(ledger, "before-correction", "Keep responses short.")
    original = seed(ledger, old)
    new = capture(ledger, "after-correction", "Use detailed responses from now on.")
    job = ledger.claim("correction-test")
    current = ledger.current_notes("demo", "scope")
    proposal = candidate(
        new,
        action="supersede",
        note_id=original["note_id"],
        expected_revision=2,
        correction={
            "previous_statement": old["content"],
            "current_statement": new["content"],
            "previous_evidence": [{"source_id": old["source_id"], "quote": old["content"]}],
            "current_evidence": [{"source_id": new["source_id"], "quote": new["content"]}],
        },
    )
    validated = validate_response(
        json.dumps({"version": 1, "candidates": [proposal]}), job["sources"], current
    )
    assert validated[0]["expected_revision"] == 1
    assert validated[0]["correction"]["previous_evidence"] == current[0]["evidence"]
    result = ledger.finish(job["id"], job["lease_token"], validated)[0]
    assert result["action"] == "supersede"
    assert ledger.inspect(original["note_id"])["status"] == "superseded"
    assert ledger.current_notes("demo", "scope")[0]["statement"] == new["content"]
    proposal["correction"]["previous_statement"] = "An invented previous statement."
    with pytest.raises(ValidationError, match="correction values mismatch"):
        validate_response(
            json.dumps({"version": 1, "candidates": [proposal]}), job["sources"], current
        )
