# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-241: extraction contract 2 needs fewer model calls per new job.

Started jobs keep the contract they froze. A contract 1 job resumes with
contract 1 windows and one semantic call per comparison task.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from noblivion import db, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_extract import SEMANTIC_PROMPT, ValidationError, semantic_targets
from noblivion.learning_semantic import (
    SEMANTIC_BATCH_PROMPT,
    eligible_part,
    learnable_window,
    semantic_batches,
    validate_semantic_batch,
)
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings
from test_learning_review_fixes import candidate, capture


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


class Oracle:
    """Answers extraction with scripted candidates and semantic calls by a pair rule.

    A pair is equivalent when both statements share their first word, else
    distinct. Both semantic formats use the same rule, so contract 1 and
    contract 2 must reach the same decisions.
    """

    def __init__(self, candidates=()):
        self.candidates = list(candidates)
        self.calls = []
        self.offsets = []

    def estimate_cost(self, tokens):
        return 0.0

    @staticmethod
    def relation(a, b):
        return "equivalent" if a.split()[0] == b.split()[0] else "distinct"

    def complete(self, messages, **kwargs):
        system, payload = messages[0]["content"], json.loads(messages[-1]["content"])
        if system.startswith(SEMANTIC_BATCH_PROMPT):
            self.calls.append("batch")
            statements = {t["target_id"]: t["statement"] for t in payload["targets"]}
            results = [
                {
                    "item_id": item["item_id"],
                    "relations": [
                        {
                            "target_id": target,
                            "relation": self.relation(item["candidate"], statements[target]),
                        }
                        for target in item["compare"]
                    ],
                }
                for item in payload["candidates"]
            ]
            return ProviderResponse(json.dumps({"version": 2, "results": results}), 50)
        if system.startswith(SEMANTIC_PROMPT):
            self.calls.append("semantic")
            relations = [
                {
                    "target_id": t["target_id"],
                    "relation": self.relation(payload["candidate"], t["statement"]),
                }
                for t in payload["targets"]
            ]
            return ProviderResponse(json.dumps({"version": 1, "relations": relations}), 50)
        self.calls.append("extract")
        self.offsets.append(payload["sources"][0]["offset"])
        found = [
            c
            for c in self.candidates
            if any(c["statement"] in part["content"] for part in payload["sources"])
            and c["statement"] not in payload["already_returned"]
        ][:12]
        return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 50)


def run(ledger, provider, **limits):
    budget = replace(
        BudgetLimits(), **{"max_calls": 1000, "max_seconds": 100000, "max_tokens": 10**8, **limits}
    )
    return LearningWorker(ledger, provider, WorkerSettings(), budget).run_once()


def progress(ledger):
    return json.loads(ledger.conn.execute("SELECT progress FROM learning_jobs").fetchone()[0])


def statements(count):
    words = ["alpha", "beta", "alpha", "gamma", "delta", "beta", "omega", "kappa", "gamma"]
    return [f"{words[i % len(words)]} rule number {i} applies here." for i in range(count)]


def many_candidates(ledger, count, prefix=""):
    lines = statements(count)
    record = capture(ledger, "many", prefix + " ".join(lines))
    result = []
    for index, line in enumerate(lines):
        c = candidate(record, key=f"rule-{index}")
        c["statement"] = line
        c["evidence"] = [{"source_id": "many", "quote": line}]
        result.append(c)
    return result


def test_new_job_freezes_contract_2_and_uses_larger_windows(ledger):
    capture(ledger, "long", "Routine context. " * 1100)  # 18,700 characters
    provider = Oracle()
    assert run(ledger, provider)["status"] == "done"
    # Contract 1 needs two windows for this text; contract 2 needs one.
    assert provider.offsets == [0]
    state = progress(ledger)
    assert state["execution_configuration"]["contract"] == 2
    assert state["call_configuration"]["contract"] == 2


def test_contract_1_job_resumes_with_contract_1_windows_and_calls(ledger, monkeypatch):
    """A job started under 0.1.25 (contract 1) keeps running after the upgrade."""
    adds = many_candidates(ledger, 3, prefix="Routine context. " * 1100)
    provider = Oracle(adds)
    monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", 1)
    first = run(ledger, provider, max_calls=1)
    assert first["status"] == "deferred" and first["error"] == "BudgetExceeded"
    frozen = progress(ledger)
    assert frozen["execution_configuration"]["contract"] == 1
    monkeypatch.undo()
    assert learning_worker.EXTRACTION_CONTRACT == 2
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    result = run(ledger, provider)
    assert result["status"] == "done"
    # Contract 1 windows (12,000 characters) and one semantic call per task.
    assert provider.offsets[:2] == [0, 4000] and len(provider.offsets) > 2
    assert "batch" not in provider.calls and provider.calls.count("semantic") == 2
    after = progress(ledger)
    assert after["execution_fingerprint"] == frozen["execution_fingerprint"]
    assert after["execution_configuration"] == frozen["execution_configuration"]
    assert after["call_configuration"]["contract"] == 1
    assert [r["action"] for r in result["results"]] == ["add", "add", "conflict"]


def test_contract_1_job_paused_inside_the_semantic_stage_resumes(ledger, monkeypatch):
    adds = many_candidates(ledger, 4)
    provider = Oracle(adds)
    monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", 1)
    # One extraction call and two of the three semantic calls fit the budget.
    first = run(ledger, provider, max_calls=3)
    assert first["status"] == "deferred"
    assert progress(ledger)["semantic"]["next_task"] == 2
    monkeypatch.undo()
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    result = run(ledger, provider)
    assert result["status"] == "done"
    assert provider.calls == ["extract", "semantic", "semantic", "semantic"]
    assert [r["action"] for r in result["results"]] == ["add", "add", "conflict", "add"]


def test_job_frozen_without_a_contract_resumes_as_contract_1(ledger):
    """Releases up to 0.1.24 stored no contract; such a job is contract 1."""
    capture(ledger, "long", "Routine context. " * 1100)
    provider = Oracle()
    worker = LearningWorker(ledger, provider, WorkerSettings(), BudgetLimits())
    job = ledger.claim("probe")
    sources = ledger._job_sources(job["id"])
    old = worker._configuration(1)
    old.pop("contract")
    windows = worker.windows(sources, 1)
    frozen = learning_worker.input_fingerprint(sources, windows, old)
    state = {
        "next_window": 0,
        "decisions": [],
        "execution_configuration": old,
        "execution_fingerprint": frozen,
        "fingerprint": frozen,
    }
    ledger.conn.execute(
        "UPDATE learning_jobs SET status='pending',lease_token=NULL,lease_owner=NULL,"
        "lease_until=NULL,progress=?",
        (json.dumps(state),),
    )
    assert worker._job_contract(state, sources) == 1
    assert run(ledger, provider)["status"] == "done"
    assert provider.offsets[:2] == [0, 4000] and len(provider.offsets) > 2


def test_unknown_stored_contract_fails_visibly(ledger):
    capture(ledger, "short", "Routine context.")
    worker = LearningWorker(ledger, Oracle(), WorkerSettings(), BudgetLimits())
    state = {"execution_fingerprint": "f", "execution_configuration": {"contract": 9}}
    with pytest.raises(ValidationError):
        worker._job_contract(state, [])


def test_batched_comparison_judges_every_pair_in_fewer_calls(ledger):
    adds = many_candidates(ledger, 9)
    provider = Oracle(adds)
    result = run(ledger, provider)
    assert result["status"] == "done"
    # Contract 1 needs eight semantic calls here (one per add after the first).
    assert provider.calls == ["extract", "batch"]
    actions = [r["action"] for r in result["results"]]
    assert actions == ["add", "add", "conflict", "add", "add", "conflict", "add", "add", "conflict"]


@pytest.mark.parametrize("seed", range(6))
def test_contract_2_decisions_equal_contract_1_decisions(tmp_path, monkeypatch, seed):
    """The same pair rule gives the same adds and conflicts under both contracts."""
    rng = random.Random(seed)
    count = rng.randint(2, 30)
    outcomes = []
    for contract in (1, 2):
        conn = db.open_db(tmp_path / f"c{contract}.db", create=True)
        ledger = LearningLedger(conn)
        lines = statements(count)
        rng2 = random.Random(seed)
        rng2.shuffle(lines)
        record = capture(ledger, "many", " ".join(lines))
        adds = []
        for index, line in enumerate(lines):
            c = candidate(record, key=f"rule-{index}")
            c.update(statement=line, evidence=[{"source_id": "many", "quote": line}])
            adds.append(c)
        monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", contract)
        provider = Oracle(adds)
        result = run(ledger, provider)
        assert result["status"] == "done", result
        outcomes.append(
            (
                [r["action"] for r in result["results"]],
                provider.calls.count("semantic") + provider.calls.count("batch"),
            )
        )
        conn.close()
    assert outcomes[0][0] == outcomes[1][0]
    assert outcomes[1][1] <= outcomes[0][1]


def test_window_without_eligible_evidence_gets_a_receipt_and_no_call(ledger):
    capture(ledger, "talk", "Assistant narration only. " * 40, role="assistant")
    provider = Oracle()
    assert run(ledger, provider)["status"] == "done"
    assert provider.calls == []
    row = ledger.conn.execute("SELECT status,reason FROM learning_extraction_pages").fetchone()
    assert tuple(row) == ("no_lesson", "no tool or direct user text in the window; no model call")


@pytest.mark.parametrize(
    "role,metadata,learnable",
    [
        ("assistant", {}, False),
        ("user", {"direct_user": False}, False),
        ("user", {}, True),
        ("user", {"direct_user": True}, True),
        ("tool", {}, True),
        ("tool", {"tool_name": "shell"}, True),
    ],
)
def test_eligible_part_follows_the_evidence_rules(role, metadata, learnable):
    part = {"role": role, "metadata": metadata, "content": "x"}
    assert eligible_part(part) is learnable
    assert learnable_window([{"role": "assistant", "metadata": {}}, part]) is learnable


def test_tool_window_is_still_sent_to_the_model(ledger):
    capture(ledger, "out", "exit code 0 " * 40, role="tool", metadata={"tool_name": "shell"})
    provider = Oracle()
    assert run(ledger, provider)["status"] == "done"
    assert provider.calls == ["extract"]


def tasks_for(count, notes=0):
    decisions = [{"action": "add", "scope": "project", "statement": s} for s in statements(count)]
    managed = [
        {"note_id": f"note-{i}", "statement": f"note {i}", "scope": "project"} for i in range(notes)
    ]
    return semantic_targets(decisions, managed)


def test_batches_keep_every_pair_and_respect_the_bounds():
    tasks = tasks_for(30, notes=20)
    batches = semantic_batches(tasks, max_chars=16000)
    pairs = [
        (task["candidate_index"], target["target_id"])
        for task in tasks
        for target in task["targets"]
    ]
    packed = [
        (item["candidate_index"], target)
        for batch in batches
        for item in batch["items"]
        for target in item["target_ids"]
    ]
    assert packed == pairs
    for batch in batches:
        assert len(batch["items"]) <= 8
        assert sum(len(i["target_ids"]) for i in batch["items"]) <= 64
        aliases = {t["target_id"] for t in batch["request"]["targets"]}
        assert all(set(c["compare"]) <= aliases for c in batch["request"]["candidates"])
    assert len(batches) < len(tasks)


def reply(batch, relation="distinct"):
    return {
        "version": 2,
        "results": [
            {
                "item_id": c["item_id"],
                "relations": [{"target_id": t, "relation": relation} for t in c["compare"]],
            }
            for c in batch["request"]["candidates"]
        ],
    }


def test_batch_reply_maps_aliases_back_to_real_targets():
    batch = semantic_batches(tasks_for(3, notes=2))[0]
    answers = validate_semantic_batch(json.dumps(reply(batch)), batch)
    assert [[r["target_id"] for r in a] for a in answers] == [
        item["target_ids"] for item in batch["items"]
    ]
    assert "note-0" in answers[0][0]["target_id"]


@pytest.mark.parametrize(
    "damage",
    [
        lambda r: r["results"].pop(),
        lambda r: r["results"][0]["relations"].pop(),
        lambda r: r["results"][0]["relations"].append(dict(r["results"][0]["relations"][0])),
        lambda r: r["results"][0].update(item_id="c99"),
        lambda r: r["results"][1].update(item_id=r["results"][0]["item_id"]),
        lambda r: r["results"][0]["relations"][0].update(target_id="note-0"),
        lambda r: r["results"][0]["relations"][0].update(relation="same"),
        lambda r: r.update(version=1),
        lambda r: r["results"][0].update(extra=True),
    ],
)
def test_incomplete_or_wrong_batch_reply_clears_nothing(damage):
    batch = semantic_batches(tasks_for(3, notes=2))[0]
    value = reply(batch)
    damage(value)
    with pytest.raises(ValidationError):
        validate_semantic_batch(json.dumps(value), batch)


def test_a_compare_list_only_covers_its_own_targets():
    """A candidate is not asked about a target that contract 1 did not pair it with."""
    batch = semantic_batches(tasks_for(3))[0]
    first, second = batch["request"]["candidates"][:2]
    assert first["compare"] == ["t1"] and second["compare"] == ["t1", "t2"]


def test_contract_2_job_paused_inside_the_semantic_stage_resumes(ledger):
    adds = many_candidates(ledger, 11)
    provider = Oracle(adds)
    first = run(ledger, provider, max_calls=2)
    assert first["status"] == "deferred" and first["error"] == "BudgetExceeded"
    assert progress(ledger)["semantic"]["next_batch"] == 1
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    result = run(ledger, provider)
    assert result["status"] == "done"
    # The first batch is not asked again; ten contract 1 calls became two.
    assert provider.calls == ["extract", "batch", "batch"]
    assert sum(r["action"] == "conflict" for r in result["results"]) == 5
