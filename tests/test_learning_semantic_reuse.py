# SPDX-License-Identifier: AGPL-3.0-or-later
"""A changed note set keeps the valid semantic comparisons (NOBLIVION-250).

Before 0.1.30 a job that met a new or revised note dropped its whole semantic
stage and started again at task 0. With many jobs in one root, a long stage
never finished, because another job always committed first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from noblivion import db
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings

TWO = "Prefer tabs over spaces. Run the tests before each commit."


def capture(ledger, ident, text, *, session):
    record = {
        "source_id": ident,
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "role": "user",
        "observed_at": "2026-01-02T12:00:00Z",
        "metadata": {},
    }
    ledger.capture_batch(
        {
            "batch_id": "batch-" + ident,
            "namespace": "demo",
            "root": "scope",
            "source_client": "codex",
            "session_id": session,
            "records": [record],
        }
    )
    return record


def candidate(record, key, statement=None):
    return {
        "action": "add",
        "logical_key": key,
        "category": "preference",
        "scope": "project",
        "title": "Preference " + key,
        "statement": statement or record["content"],
        "observed_at": "2026-01-02",
        "effective_at": None,
        "evidence": [{"source_id": record["source_id"], "quote": record["content"]}],
    }


def seed(ledger, ident, text, key, finish=None):
    """Store one active note through a direct commit, with no provider call."""
    record = capture(ledger, ident, text, session="seed-" + ident)
    job = ledger.claim("seed")
    c = candidate(record, key)
    c["evidence"] = [
        {
            "source_id": record["source_id"],
            "source_hash": record["source_hash"],
            "start": 0,
            "end": len(text),
            "quote": text,
        }
    ]
    return (finish or ledger.finish)(job["id"], job["lease_token"], [c])[0]


class Provider:
    """Extraction by source marker; each semantic call logs its target ids."""

    def __init__(self, extractions, equivalent=()):
        self.extractions = extractions
        self.equivalent = set(equivalent)
        self.targets = []
        self.before_semantic = {}

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "sources" in payload:
            text = json.dumps(payload["sources"])
            found = [c for marker, cs in self.extractions.items() if marker in text for c in cs]
            return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 100)
        ids = [t["target_id"] for t in payload["targets"]]
        self.targets.append(ids)
        hook = self.before_semantic.pop(len(self.targets), None)
        if hook:
            hook()
        relations = [
            {"target_id": i, "relation": "equivalent" if i in self.equivalent else "distinct"}
            for i in ids
        ]
        return ProviderResponse(json.dumps({"version": 1, "relations": relations}), 100)


def worker(ledger, provider, **limits):
    limits.setdefault("max_seconds", 3600)
    return LearningWorker(ledger, provider, WorkerSettings(), replace(BudgetLimits(), **limits))


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


@pytest.fixture
def setup(ledger):
    """Two notes, then job B with two add candidates in the same root."""
    n1 = seed(ledger, "s1", "Keep responses short.", "short")["note_id"]
    n2 = seed(ledger, "s2", "Use green terminal text.", "green")["note_id"]
    b = capture(ledger, "b", TWO, session="job-b")
    cands = [
        candidate(b, "tabs", "Prefer tabs over spaces."),
        candidate(b, "tests", "Run the tests before each commit."),
    ]
    job_b = ledger.conn.execute("SELECT id FROM learning_jobs WHERE session_id='job-b'").fetchone()[
        0
    ]
    return {"n1": n1, "n2": n2, "record": b, "candidates": cands, "job": job_b}


def pause_after_first_comparison(ledger, setup, provider):
    """Job B runs its extraction and its first semantic task, then the budget stops it."""
    first = worker(ledger, provider, max_calls=2).run_once()
    assert first == {"status": "deferred", "job_id": setup["job"], "error": "BudgetExceeded"}
    assert provider.targets == [sorted([setup["n1"], setup["n2"]])]
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    return progress(ledger, setup["job"])


def progress(ledger, job_id):
    return json.loads(
        ledger.conn.execute("SELECT progress FROM learning_jobs WHERE id=?", (job_id,)).fetchone()[
            0
        ]
    )


def resume(ledger, provider, job_id):
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE id=?", (job_id,))
    return worker(ledger, provider, max_calls=50).run_once()


def as_sets(calls):
    return [set(call) for call in calls]


def test_job_a_commit_mid_stage_costs_job_b_only_the_new_note(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]})
    pause_after_first_comparison(ledger, setup, provider)
    # Job A in the same root commits a new note while job B waits mid-stage.
    a = capture(ledger, "a", "Write commit messages in English.", session="job-a")
    provider.extractions["commit messages"] = [candidate(a, "english")]
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=1e12 WHERE id=?", (setup["job"],))
    job_a = worker(ledger, provider, max_calls=50).run_once()
    assert job_a["status"] == "done" and job_a["results"][0]["action"] == "add"
    n3 = job_a["results"][0]["note_id"]
    provider.targets.clear()
    job_b = resume(ledger, provider, setup["job"])
    assert job_b["status"] == "done"
    assert [r["action"] for r in job_b["results"]] == ["add", "add"]
    # Candidate 0 needs only the new note. Candidate 1 had no comparison yet.
    assert as_sets(provider.targets) == [
        {n3},
        {setup["n1"], setup["n2"], n3, "candidate:0"},
    ]
    state = progress(ledger, setup["job"])
    assert state["semantic_restarts"] == 1 and state["semantic_reused"] == 2
    report = ledger.status()
    assert report["semantic_restarts"] == 1 and report["semantic_reused"] == 2
    assert len(ledger.current_notes("demo", "scope")) == 5


def test_revised_note_is_compared_again(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]})
    pause_after_first_comparison(ledger, setup, provider)
    ledger.edit(setup["n2"], 1, title="Green text")
    provider.targets.clear()
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert as_sets(provider.targets) == [
        {setup["n2"]},
        {setup["n1"], setup["n2"], "candidate:0"},
    ]
    assert progress(ledger, setup["job"])["semantic_reused"] == 1


def test_removed_note_needs_no_new_comparison(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]})
    pause_after_first_comparison(ledger, setup, provider)
    ledger.forget(setup["n2"])
    provider.targets.clear()
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    # Candidate 0 keeps its comparison with n1 and needs no call at all.
    assert as_sets(provider.targets) == [{setup["n1"], "candidate:0"}]


def test_block_by_removed_note_gets_a_fresh_comparison(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]}, equivalent={setup["n2"]})
    stage = pause_after_first_comparison(ledger, setup, provider)["semantic"]
    assert stage["decisions"][0]["action"] == "conflict"
    assert stage["decisions"][0]["semantic_review"]["target_id"] == setup["n2"]
    ledger.forget(setup["n2"])
    provider.targets.clear()
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert "semantic_review" not in result["results"][0]
    assert as_sets(provider.targets) == [{setup["n1"]}, {setup["n1"], "candidate:0"}]


def test_block_by_unchanged_note_stays(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]}, equivalent={setup["n2"]})
    pause_after_first_comparison(ledger, setup, provider)
    ledger.edit(setup["n1"], 1, title="Short replies")
    provider.targets.clear()
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    # Candidate 0 keeps its block with no call. Candidate 1 compares with n2
    # once, and the equivalent answer blocks it too.
    assert [r["action"] for r in result["results"]] == ["conflict", "conflict"]
    assert as_sets(provider.targets) == [{setup["n1"], setup["n2"], "candidate:0"}]


def test_note_added_before_the_commit_forces_only_the_missing_comparisons(ledger, setup):
    provider = Provider({"Prefer tabs": setup["candidates"]})
    added = {}
    # The note arrives during the last comparison of the stage.
    provider.before_semantic[2] = lambda: added.update(
        seed(ledger, "late", "Use dark mode in the editor.", "dark")
    )
    result = worker(ledger, provider, max_calls=50).run_once()
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    n1, n2, n3 = setup["n1"], setup["n2"], added["note_id"]
    assert as_sets(provider.targets) == [
        {n1, n2},
        {n1, n2, "candidate:0"},
        {n3},
        {n3},
    ]
    stored = [
        json.loads(row[0])["decision"]["semantic_snapshot"]
        for row in ledger.conn.execute(
            "SELECT detail FROM learning_decisions WHERE job_id=? AND action='add'",
            (setup["job"],),
        )
    ]
    # Each stored add carries the snapshot that includes the late note.
    assert len(stored) == 2
    assert all({item[0] for item in snapshot} == {n1, n2, n3} for snapshot in stored)


def test_note_added_inside_the_commit_race_is_refused_then_compared(ledger, setup, monkeypatch):
    provider = Provider({"Prefer tabs": setup["candidates"]})
    real_finish = ledger.finish
    added = {}

    def racing_finish(job_id, lease, decisions, **kwargs):
        if not added:
            added.update(seed(ledger, "race", "Use dark mode in the editor.", "dark", real_finish))
        return real_finish(job_id, lease, decisions, **kwargs)

    monkeypatch.setattr(ledger, "finish", racing_finish)
    first = worker(ledger, provider, max_calls=50).run_once()
    # The write transaction sees the new note and stores no add.
    assert first["status"] == "deferred" and first["error"] == "SemanticSnapshotChanged"
    assert len(ledger.current_notes("demo", "scope")) == 3
    assert "semantic" in progress(ledger, setup["job"])
    provider.targets.clear()
    second = resume(ledger, provider, setup["job"])
    assert second["status"] == "done"
    assert [r["action"] for r in second["results"]] == ["add", "add"]
    assert as_sets(provider.targets) == [{added["note_id"]}, {added["note_id"]}]
    assert len(ledger.current_notes("demo", "scope")) == 5


@pytest.mark.parametrize("change", [False, True])
def test_stage_checkpointed_by_0_1_29_resumes(ledger, setup, change):
    """A 0.1.29 stage has only notes, snapshot, next_task, decisions and reasks."""
    provider = Provider({"Prefer tabs": setup["candidates"]})
    state = pause_after_first_comparison(ledger, setup, provider)
    fingerprint = state["fingerprint"], state["execution_fingerprint"]
    # The stage that 0.1.29 writes, with a pending re-ask of task 1.
    assert set(state["semantic"]) == {"notes", "snapshot", "next_task", "decisions", "reasks"}
    assert state["semantic"]["next_task"] == 1
    assert not {"semantic_restarts", "semantic_reused"} & set(state)
    state["semantic"]["reasks"] = {"1": {"failures": 1, "error": "invalid semantic target"}}
    ledger.conn.execute(
        "UPDATE learning_jobs SET progress=? WHERE id=?", (json.dumps(state), setup["job"])
    )
    n1, n2 = setup["n1"], setup["n2"]
    expected = [{n1, n2, "candidate:0"}]
    if change:
        n3 = seed(ledger, "new", "Use dark mode in the editor.", "dark")["note_id"]
        expected = [{n3}, {n1, n2, n3, "candidate:0"}]
    provider.targets.clear()
    result = resume(ledger, provider, setup["job"])
    assert result["status"] == "done"
    assert [r["action"] for r in result["results"]] == ["add", "add"]
    assert as_sets(provider.targets) == expected
    after = progress(ledger, setup["job"])
    assert (after["fingerprint"], after["execution_fingerprint"]) == fingerprint
    assert after.get("semantic_restarts", 0) == int(change)


def test_other_snapshot_refusal_drops_the_stage_and_ends(ledger, setup, monkeypatch):
    """The note set did not change, so the stage cannot be refreshed: drop it."""
    provider = Provider({"Prefer tabs": setup["candidates"]})
    real_finish = ledger.finish
    refusals = []

    def finish_without_snapshot(job_id, lease, decisions, **kwargs):
        if not refusals:
            refusals.append(1)
            # learning.py refuses an automatic add with no semantic snapshot.
            decisions = [
                {k: v for k, v in d.items() if k != "semantic_snapshot"} for d in decisions
            ]
        return real_finish(job_id, lease, decisions, **kwargs)

    monkeypatch.setattr(ledger, "finish", finish_without_snapshot)
    first = worker(ledger, provider, max_calls=50).run_once()
    assert first["status"] == "deferred" and first["error"] == "SemanticSnapshotChanged"
    state = progress(ledger, setup["job"])
    assert "semantic" not in state and "semantic_restarts" not in state
    assert len(provider.targets) == 2
    provider.targets.clear()
    second = resume(ledger, provider, setup["job"])
    # A fresh stage compares everything once, and the job ends: no defer loop.
    assert second["status"] == "done"
    assert [r["action"] for r in second["results"]] == ["add", "add"]
    assert as_sets(provider.targets) == [
        {setup["n1"], setup["n2"]},
        {setup["n1"], setup["n2"], "candidate:0"},
    ]
