# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-243: the semantic calls of a job do not grow with the note count.

Extraction contract 3 compares each add candidate only with its nearest notes
and nearest earlier candidates (keyword match plus embedding similarity). A
started job of contract 1 or 2 takes the same selection while its semantic stage
has no answered comparison (NOBLIVION-257). With an answer it keeps its full
comparison while that fits its task bound. Over the bound, with no comparison
done in the current stage, it takes the nearest selection instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from noblivion import db, embedding, learning_worker
from noblivion.learning import BudgetLimits, LearningLedger
from noblivion.learning_extract import SEMANTIC_PROMPT, semantic_targets
from noblivion.learning_nearest import (
    NEAREST_CANDIDATES,
    NEAREST_NOTES,
    candidate_key,
    nearest_targets,
)
from noblivion.learning_semantic import SEMANTIC_BATCH_PROMPT
from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings
from test_learning_review_fixes import candidate, capture, refs


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "isolated.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def unit(*values, size=8):
    vector = [*values, *([0.0] * (size - len(values)))]
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def note_pool(count):
    return [
        {
            "note_id": f"n{i:04d}",
            "statement": f"Filler fact {i} about item {i}.",
            "scope": "project",
        }
        for i in range(count)
    ]


def add(statement):
    return {"action": "add", "scope": "project", "statement": statement}


# -- the selection --------------------------------------------------------------


def test_nearest_includes_a_keyword_match_and_an_embedding_near_note():
    notes = note_pool(40)
    # Shares rare words with the candidate, but its vector points away.
    notes.append(
        {"note_id": "lexical", "statement": "Indent Go code with tabs.", "scope": "project"}
    )
    # Shares no word with the candidate, but its vector is the nearest.
    notes.append(
        {"note_id": "paraphrase", "statement": "Prefer brief answers.", "scope": "project"}
    )
    decisions = [add("Indent with tabs; keep replies concise.")]
    vectors = {n["note_id"]: unit(0, 1, 0.05 * (i % 7)) for i, n in enumerate(notes)}
    vectors["lexical"] = unit(0, 0, 0, 1)
    vectors["paraphrase"] = unit(1, 0.01)
    vectors[candidate_key(0)] = unit(1, 0)
    chosen = nearest_targets(decisions, notes, vectors)["0"]
    assert "lexical" in chosen and "paraphrase" in chosen
    assert len(chosen) == NEAREST_NOTES


def test_nearest_without_vectors_uses_keyword_order():
    notes = note_pool(40)
    notes.append({"note_id": "match", "statement": "Indent Go code with tabs.", "scope": "project"})
    chosen = nearest_targets([add("Indent with tabs.")], notes, None)["0"]
    assert "match" in chosen


def test_keyword_mode_fills_every_seat_when_no_word_is_shared():
    """No vector and no shared word must not leave a candidate unchecked."""
    notes = note_pool(40)
    earlier = [add(f"Earlier lesson number {i}.") for i in range(20)]
    decisions = [*earlier, add("Prefer brief answers.")]
    chosen = nearest_targets(decisions, notes, None)["20"]
    own = [t for t in chosen if not t.startswith("candidate:")]
    prior = [t for t in chosen if t.startswith("candidate:")]
    assert len(own) == NEAREST_NOTES and len(prior) == NEAREST_CANDIDATES
    tasks = semantic_targets(decisions, notes, nearest=nearest_targets(decisions, notes, None))
    assert sum(len(t["targets"]) for t in tasks if t["candidate_index"] == 20) == 32


def test_a_small_pool_is_compared_whole():
    notes = note_pool(NEAREST_NOTES)
    decisions = [add("Something unrelated."), add("Another thing.")]
    selection = nearest_targets(decisions, notes, None)
    assert selection["1"] == [n["note_id"] for n in notes] + [candidate_key(0)]


@pytest.mark.parametrize("count", [40, 400, 2000])
def test_task_count_is_bounded_independent_of_the_note_count(count):
    notes = note_pool(count)
    decisions = [add(f"Filler fact {i} about item {i}, restated.") for i in range(30)]
    vectors = {n["note_id"]: unit(1, (i % 13) / 13) for i, n in enumerate(notes)}
    vectors.update({candidate_key(i): unit(1, (i % 5) / 5) for i in range(30)})
    nearest = nearest_targets(decisions, notes, vectors)
    tasks = semantic_targets(decisions, notes, nearest=nearest)
    per = math.ceil((NEAREST_NOTES + NEAREST_CANDIDATES) / 16)
    assert len(tasks) <= len(decisions) * per
    assert max(len(ids) for ids in nearest.values()) <= NEAREST_NOTES + NEAREST_CANDIDATES
    # Without the selection the task count grows with the note count.
    assert len(semantic_targets(decisions, notes, max_tasks=10**9)) >= 30 * count // 16


# -- the worker -------------------------------------------------------------------


class Provider:
    """Scripted extraction; every semantic pair is distinct. Records the targets."""

    def __init__(self, candidates):
        self.candidates = candidates
        self.targets = []  # one set of compared target statements per call
        self.extractions = 0

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        system, payload = messages[0]["content"], json.loads(messages[-1]["content"])
        if system.startswith(SEMANTIC_BATCH_PROMPT):
            self.targets.append({t["statement"] for t in payload["targets"]})
            results = [
                {
                    "item_id": item["item_id"],
                    "relations": [
                        {"target_id": target, "relation": "distinct"} for target in item["compare"]
                    ],
                }
                for item in payload["candidates"]
            ]
            return ProviderResponse(json.dumps({"version": 2, "results": results}), 50)
        if system.startswith(SEMANTIC_PROMPT):
            self.targets.append({t["statement"] for t in payload["targets"]})
            relations = [
                {"target_id": t["target_id"], "relation": "distinct"} for t in payload["targets"]
            ]
            return ProviderResponse(json.dumps({"version": 1, "relations": relations}), 50)
        self.extractions += 1
        found = [
            c
            for c in self.candidates
            if any(c["statement"] in part["content"] for part in payload["sources"])
        ]
        return ProviderResponse(json.dumps({"version": 1, "candidates": found}), 50)


class Service:
    """An EmbeddingService stand-in: a local model with hash vectors."""

    def __init__(self, state=embedding.STATE_READY):
        self.state = state
        self.embedder = None
        self.include_mined = True
        self.calls = 0

    @property
    def model_id(self):
        return "fake:model" if self.state == embedding.STATE_READY else None

    def embed_query(self, text, timeout_s=1.0, conn=None):
        self.calls += 1
        digest = hashlib.sha256(text.encode()).digest()
        return unit(*(1 + b for b in digest[:8]))


def seed_notes(ledger, count):
    for n in range(count):
        record = capture(ledger, f"seed-{n}", f"Rule {n} applies to module {n}.")
        job = ledger.claim("seed")
        item = candidate(record, key=f"rule-{n}")
        item["evidence"] = refs(record)
        ledger.finish(job["id"], job["lease_token"], [item])


LINES = [
    "Prefer tabs in Go files.",
    "Run the linter before each commit.",
    "Keep each reply under ten lines.",
]


def new_job(ledger, lines=LINES, ident="new"):
    record = capture(ledger, ident, " ".join(lines))
    found = []
    for index, line in enumerate(lines):
        c = candidate(record, key=f"new-{index}", statement=line)
        c["evidence"] = [{"source_id": ident, "quote": line}]
        found.append(c)
    return found


def worker(ledger, provider, service=None, **limits):
    budget = replace(
        BudgetLimits(), **{"max_calls": 1000, "max_seconds": 100000, "max_tokens": 10**8, **limits}
    )
    return LearningWorker(ledger, provider, WorkerSettings(), budget, embedding=service)


def job_row(ledger, ident=None):
    """The newest job, or the job ``ident``."""
    row = ledger.conn.execute(
        "SELECT id,status,attempts,last_error,progress FROM learning_jobs "
        "WHERE ? IS NULL OR id=? ORDER BY rowid DESC",
        (ident, ident),
    ).fetchone()
    return row["id"], row["status"], row["attempts"], row["last_error"], json.loads(row["progress"])


def compared_notes(provider, count):
    seeded = {f"Rule {n} applies to module {n}." for n in range(count)}
    return set().union(*provider.targets) & seeded if provider.targets else set()


@pytest.mark.parametrize("count", [20, 60])
def test_contract_3_job_compares_the_nearest_notes_only(ledger, count):
    seed_notes(ledger, count)
    provider = Provider(new_job(ledger))
    service = Service()
    assert worker(ledger, provider, service).run_once()["status"] == "done"
    stage = job_row(ledger)[4]["semantic"]
    assert job_row(ledger)[4]["execution_configuration"]["contract"] == 3
    assert stage["selection"] == "hybrid"
    for index in ("0", "1", "2"):
        notes = [t for t in stage["nearest"][index] if not t.startswith("candidate:")]
        assert len(notes) == NEAREST_NOTES
    # The same calls for 20 and for 60 notes, and at most 16 notes per candidate.
    assert len(provider.targets) == 1
    assert len(compared_notes(provider, count)) <= 3 * NEAREST_NOTES
    assert service.calls == count + 3  # no stored vectors here: each note is embedded


def test_the_stored_stage_keeps_the_snapshot_once(ledger):
    """One snapshot copy per add made a large job exceed the progress size limit."""
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    assert worker(ledger, provider, Service()).run_once()["status"] == "done"
    stage = job_row(ledger)[4]["semantic"]
    assert len(stage["snapshot"]) == 20
    assert all("semantic_snapshot" not in d for d in stage["decisions"])
    # The commit still got the snapshot of each add.
    details = [
        json.loads(row[0])["decision"]
        for row in ledger.conn.execute(
            "SELECT detail FROM learning_decisions WHERE job_id=?", (job_row(ledger)[0],)
        )
    ]
    assert len(details) == 3 and all(d["semantic_snapshot"] == stage["snapshot"] for d in details)


def test_stored_note_vectors_are_read_not_embedded_again(ledger):
    seed_notes(ledger, 2)
    note = ledger.current_notes("demo", "scope")[0]
    memory = db.insert_memory(
        ledger.conn,
        project="demo",
        root="scope",
        path=note["path"],
        source_type="claude_code_md",
        category="",
        content=note["statement"],
        content_hash="0" * 64,
        labels="[]",
        rev=1,
        now="2026-01-02T12:00:00Z",
    )
    blob = embedding.vector_to_blob(unit(1, 2))
    ledger.conn.execute(
        "INSERT INTO vectors (memory_id, model, dim, content_hash, blob, rev) VALUES (?,?,?,?,?,?)",
        (memory, "fake:model", 8, "0" * 64, blob, 1),
    )
    found = ledger.note_vectors(
        "demo", [n["note_id"] for n in ledger.current_notes("demo", "scope")], "fake:model"
    )
    assert list(found) == [note["note_id"]]
    assert found[note["note_id"]] == pytest.approx(unit(1, 2))
    assert ledger.note_vectors("demo", [note["note_id"]], "other:model") == {}


def test_a_loading_embedding_model_defers_the_job_and_keeps_the_attempt(ledger):
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    service = Service(state=embedding.STATE_LOADING)
    result = worker(ledger, provider, service).run_once()
    assert result == {
        "status": "deferred",
        "job_id": job_row(ledger)[0],
        "error": "SemanticEmbeddingUnavailable",
    }
    ident, status, attempts, _, progress = job_row(ledger)
    assert status == "pending" and attempts == 0
    assert progress["embedding_deferrals"] == 1 and "nearest" not in progress["semantic"]
    assert provider.targets == []
    service.state = embedding.STATE_READY
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    assert worker(ledger, provider, service).run_once()["status"] == "done"
    assert job_row(ledger)[4]["semantic"]["selection"] == "hybrid"


def test_no_embedding_service_uses_keyword_order(ledger):
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    assert worker(ledger, provider).run_once()["status"] == "done"
    assert job_row(ledger)[4]["semantic"]["selection"] == "keyword"


# -- started jobs of contracts 1 and 2 -------------------------------------------


def freeze(ledger, monkeypatch, provider, contract, **limits):
    """Start the job as a release with ``contract`` as its newest one would."""
    monkeypatch.setattr(learning_worker, "EXTRACTION_CONTRACT", contract)
    first = worker(ledger, provider, **limits).run_once()
    monkeypatch.undo()
    return first


def start_old(ledger, monkeypatch, provider, contract, max_calls):
    """Run a job as the release before NOBLIVION-257 until the budget stops it."""
    with monkeypatch.context() as old:
        # 0.1.34 kept the full comparison for a started old job under its bound.
        old.setattr(learning_worker.LearningWorker, "_bound_stage", lambda *args: None)
        first = freeze(ledger, old, provider, contract, max_calls=max_calls)
    assert first["error"] == "BudgetExceeded"


def wake(ledger):
    """A new budget day: the waiting job may run again."""
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")


def add_note(ledger, ident):
    """A note from another job: the stored stage of a waiting job is refreshed."""
    record = capture(ledger, ident, f"Other rule {ident}.")
    job = ledger.claim("seed")
    item = candidate(record, key=ident)
    item["evidence"] = refs(record)
    ledger.finish(job["id"], job["lease_token"], [item])


@pytest.mark.parametrize("count", [20, 60])
@pytest.mark.parametrize("contract", [1, 2])
def test_an_old_job_with_no_semantic_answer_takes_the_nearest_selection(
    ledger, monkeypatch, contract, count
):
    """NOBLIVION-257: stored extraction answers, a semantic stage at task 0."""
    seed_notes(ledger, count)
    provider = Provider(new_job(ledger))
    # One extraction call fits: the window is done, so the job keeps its contract.
    start_old(ledger, monkeypatch, provider, contract, max_calls=1)
    wake(ledger)
    ident, _, _, _, before = job_row(ledger)
    # With 60 notes the budget stops the job before its semantic stage.
    assert before.get("semantic", {}).get("next_task", 0) == 0
    assert provider.extractions == 1 and before["next_window"] == 1
    assert worker(ledger, provider, Service()).run_once()["status"] == "done"
    progress = job_row(ledger, ident)[4]
    # The frozen contract and fingerprint are unchanged, and the stored
    # extraction answer is used: each window is asked once.
    assert progress["execution_configuration"]["contract"] == contract
    assert progress["execution_fingerprint"] == before["execution_fingerprint"]
    assert provider.extractions == progress["next_window"]
    stage = progress["semantic"]
    assert stage["nearest_upgrade"] == [NEAREST_NOTES, NEAREST_CANDIDATES]
    assert stage["selection"] == "hybrid" and "bound_fallback" not in stage
    # Bounded tasks, the same for 20 and for 60 notes.
    assert len(provider.targets) == (5 if contract == 1 else 1)
    assert len(compared_notes(provider, count)) <= 3 * NEAREST_NOTES


class FirstEquivalent(Provider):
    """The first semantic answer blocks its first target as equivalent."""

    def complete(self, messages, **kwargs):
        before = len(self.targets)
        response = super().complete(messages, **kwargs)
        if before or not self.targets:
            return response
        body = json.loads(response.content)
        relations = body["results"][0]["relations"] if "results" in body else body["relations"]
        relations[0]["relation"] = "equivalent"
        return replace(response, content=json.dumps(body))


class FirstRejected(Provider):
    """The first semantic answer is rejected: invalid JSON, or an answer that
    parses but does not match its task, which waits for a re-ask."""

    def __init__(self, candidates, broken):
        super().__init__(candidates)
        self.broken = broken

    def complete(self, messages, **kwargs):
        before = len(self.targets)
        response = super().complete(messages, **kwargs)
        if before or not self.targets:
            return response
        if self.broken.endswith("format_repair"):
            return replace(response, content="{not json")
        body = json.loads(response.content)
        if "results" in body:
            body["results"] = []  # a batch answer with no result
        else:
            body["relations"][0]["target_id"] = "invented-target"
        return replace(response, content=json.dumps(body))


@pytest.mark.parametrize(
    "kept",
    [
        "stage",
        "covered",
        "conflict",
        "format_repair",
        "batch_format_repair",
        "reask",
        "batch_reask",
    ],
)
def test_an_old_job_with_an_answered_comparison_keeps_the_full_comparison(
    ledger, monkeypatch, kept
):
    """One answer, in the stage, kept in ``covered`` or as a conflict after a
    refresh, or a rejected answer that waits for its format retry or re-ask."""
    seed_notes(ledger, 20)
    lines = [f"Lesson {word} holds for every build." for word in "abc"]
    contract = 2 if kept.startswith("batch_") else 1
    if kept == "conflict":
        provider = FirstEquivalent(new_job(ledger, lines))
    elif kept.endswith(("format_repair", "reask")):
        provider = FirstRejected(new_job(ledger, lines), kept)
    else:
        provider = Provider(new_job(ledger, lines))
    start_old(ledger, monkeypatch, provider, contract, max_calls=2)
    ident, _, _, _, before = job_row(ledger)
    assert len(provider.targets) == 1
    if kept == "format_repair":
        assert before["format_repair"]["step"] == "semantic:0"
        assert before["semantic"]["next_task"] == 0
    elif kept == "batch_format_repair":
        # The step of a contract 2 batch, not "semantic:<n>".
        assert before["format_repair"]["step"] == "semantic-batch:0"
        assert before["semantic"].get("next_batch", 0) == 0
        assert not before["semantic"].get("batch_reasks")
    elif kept == "reask":
        # Only the re-ask of task 0 holds the stage.
        assert before["semantic"]["reasks"]["0"]["failures"] == 1
        assert before["semantic"]["next_task"] == 0 and "format_repair" not in before
    elif kept == "batch_reask":
        assert before["semantic"]["batch_reasks"]["0"]["failures"] == 1
        assert before["semantic"].get("next_batch", 0) == 0
    else:
        # One call answered; a conflict also skips the other tasks of its candidate.
        assert before["semantic"]["next_task"] >= 1
    if kept in ("covered", "conflict"):
        add_note(ledger, "later")
    wake(ledger)
    assert worker(ledger, provider, Service()).run_once()["status"] == "done"
    progress = job_row(ledger, ident)[4]
    stage = progress["semantic"]
    assert "nearest" not in stage and "nearest_upgrade" not in stage
    if kept == "covered":
        assert progress["semantic_restarts"] == 1 and stage["covered"]
    if kept == "conflict":
        assert progress["semantic_restarts"] == 1 and "covered" not in stage
        assert stage["decisions"][0]["action"] == "conflict"
    # Every note is compared, as before this change.
    assert compared_notes(provider, 20) == {f"Rule {n} applies to module {n}." for n in range(20)}


@pytest.mark.parametrize("grown", [0, 1])
def test_an_over_bound_contract_1_job_takes_the_nearest_selection_on_retry(
    ledger, monkeypatch, grown
):
    """The shape of a job that failed with the task bound: a stage at task 0.

    ``grown`` notes come from other jobs before the retry, so the stored stage
    is refreshed first.
    """
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    # Full comparison: 1 + 2 + 2 tasks. The bound of 4 is exceeded.
    monkeypatch.setattr(learning_worker, "_max_semantic_tasks", lambda job: 4)
    with monkeypatch.context() as old:
        # The release before this change: no selection, so the bound fails the job.
        old.setattr(learning_worker.LearningWorker, "_bound_stage", lambda *args: None)
        first = freeze(ledger, old, provider, 1)
    assert first["status"] == "failed" and first["permanent"] is True
    ident, status, _, error, progress = job_row(ledger)
    assert status == "exhausted"
    assert error == "ValidationError: semantic reconciliation exceeds bounded task count"
    assert progress["semantic"]["next_task"] == 0 and provider.targets == []
    for n in range(grown):
        record = capture(ledger, f"later-{n}", f"Later rule {n}.")
        job = ledger.claim("seed")
        item = candidate(record, key=f"later-{n}")
        item["evidence"] = refs(record)
        ledger.finish(job["id"], job["lease_token"], [item])
    ledger.retry(ident)
    assert worker(ledger, provider, Service()).run_once()["status"] == "done"
    progress = job_row(ledger, ident)[4]
    assert progress["execution_configuration"]["contract"] == 1
    stage = progress["semantic"]
    # 16 notes + earlier candidates still give 5 tasks; 8 + 8 gives 3.
    assert stage["bound_fallback"] == [8, 8] and stage["selection"] == "hybrid"
    assert len(provider.targets) == 3
    assert len(ledger.current_notes("demo", "scope")) == 23 + grown


# -- the store's embedding service reaches the worker ------------------------------


def test_the_pool_gives_the_store_embedding_service_to_each_worker(monkeypatch):
    import contextlib
    import threading

    from noblivion import learning_runtime

    service = Service()
    stop = threading.Event()
    seen = []

    class Ledger:
        def __init__(self, conn, pool):
            pass

        def release(self):
            pass

    def run_once(ledger, settings, **kwargs):
        stop.set()  # first, so a missing argument cannot keep the loop going
        seen.append(kwargs.get("embedding"))
        return {"status": "done"}

    class Store:
        embedding = service
        _jobs_stop = stop

        @contextlib.contextmanager
        def connection(self):
            yield object()

    monkeypatch.setattr(learning_runtime, "PooledLedger", Ledger)
    monkeypatch.setattr(learning_runtime, "run_once", run_once)
    pool = learning_runtime.LearningPool(Store())
    pool.settings, pool.desired = {}, 1
    pool._work(0)
    assert seen == [service]

    monkeypatch.undo()
    monkeypatch.setattr(
        learning_runtime,
        "worker_options",
        lambda settings: (None, WorkerSettings(), BudgetLimits()),
    )
    monkeypatch.setattr(LearningWorker, "run_once", lambda self: self.embedding)
    settings = {"scopes": [{"project": "demo", "root": "scope"}]}
    assert learning_runtime.run_once(SimpleNamespace(), settings, embedding=service) is service


def test_a_ready_model_that_gives_no_vector_defers_the_job(ledger):
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    service = Service()
    service.embed_query = lambda text, timeout_s=1.0, conn=None: None
    result = worker(ledger, provider, service).run_once()
    assert result["error"] == "SemanticEmbeddingUnavailable"
    assert job_row(ledger)[1] == "pending" and job_row(ledger)[2] == 0
    assert provider.targets == []


def test_a_remote_model_without_mined_consent_gets_no_candidate_text(ledger):
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    service = Service()
    service.embedder = SimpleNamespace(remote=True)
    service.include_mined = False
    assert worker(ledger, provider, service).run_once()["status"] == "done"
    assert job_row(ledger)[4]["semantic"]["selection"] == "keyword"
    assert service.calls == 0


def test_each_embedding_call_renews_the_job_lease(ledger):
    """A slow model and many notes without a vector must not lose the lease."""
    seed_notes(ledger, 20)
    provider = Provider(new_job(ledger))
    service = Service()
    left = []
    embed = service.embed_query

    def watched(text, timeout_s=1.0, conn=None):
        until = conn.execute("SELECT lease_until FROM learning_jobs WHERE status='leased'")
        left.append(until.fetchone()[0] - time.time())
        # Time passes: the lease is about to end when this call returns.
        conn.execute(
            "UPDATE learning_jobs SET lease_until=? WHERE status='leased'", (time.time() + 1,)
        )
        return embed(text, timeout_s, conn)

    service.embed_query = watched
    assert worker(ledger, provider, service).run_once()["status"] == "done"
    assert len(left) == 23
    assert min(left) > learning_worker.EMBEDDING_TIMEOUT_S


class OtherService(Service):
    """Another model: the same texts get other vectors."""

    def embed_query(self, text, timeout_s=1.0, conn=None):
        self.calls += 1
        digest = hashlib.sha256(b"other:" + text.encode()).digest()
        return unit(*(1 + b for b in digest[:8]))


def test_a_resume_keeps_the_stored_selection(ledger):
    """The selection is made once; a resume with other vectors builds the same tasks."""
    seed_notes(ledger, 40)
    lines = [f"Lesson {word} holds for every build." for word in "abcdefghij"]
    provider = Provider(new_job(ledger, lines))
    # One extraction call and one semantic batch fit; then the budget stops the job.
    first = worker(ledger, provider, Service(), max_calls=2).run_once()
    assert first["error"] == "BudgetExceeded"
    stage = job_row(ledger)[4]["semantic"]
    stored, done = stage["nearest"], stage["next_batch"]
    assert done == 1 and len(provider.targets) == 1
    other = OtherService()
    ledger.conn.execute("UPDATE learning_budget SET created_at=created_at-86401")
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0")
    assert worker(ledger, provider, other).run_once()["status"] == "done"
    after = job_row(ledger)[4]["semantic"]
    assert after["nearest"] == stored and other.calls == 0
    # Every compared note of the resumed batches is in the stored selection.
    statements = {str(n["note_id"]): n["statement"] for n in stage["notes"]}
    allowed = {statements[t] for ids in stored.values() for t in ids if t in statements}
    seeded = {n["statement"] for n in stage["notes"]}
    assert all(targets & seeded <= allowed for targets in provider.targets[1:])


def test_an_old_job_resumes_and_refreshes_with_the_nearest_selection(ledger, monkeypatch):
    """NOBLIVION-257: a switched old job keeps its stored selection on a resume,
    and selects again, never the full comparison, after a refresh."""
    seed_notes(ledger, 40)
    lines = [f"Lesson {word} holds for every build." for word in "abcde"]
    provider = Provider(new_job(ledger, lines))
    start_old(ledger, monkeypatch, provider, 1, max_calls=1)
    wake(ledger)
    # The new release: one semantic task fits, then the budget stops the job.
    first = worker(ledger, provider, Service(), max_calls=1).run_once()
    assert first["error"] == "BudgetExceeded"
    ident, _, _, _, progress = job_row(ledger)
    stage = progress["semantic"]
    stored = stage["nearest"]
    assert stage["next_task"] == 1 and stage["nearest_upgrade"] == [16, 16]
    wake(ledger)
    other = OtherService()
    second = worker(ledger, provider, other, max_calls=1).run_once()
    assert second["error"] == "BudgetExceeded"
    stage = job_row(ledger, ident)[4]["semantic"]
    assert stage["nearest"] == stored and stage["next_task"] == 2 and other.calls == 0
    # Another job adds a note: the stage refreshes with its answers in covered.
    add_note(ledger, "later")
    wake(ledger)
    assert worker(ledger, provider, Service()).run_once()["status"] == "done"
    progress = job_row(ledger, ident)[4]
    stage = progress["semantic"]
    assert progress["semantic_restarts"] == 1 and stage["covered"]
    assert stage["nearest_upgrade"] == [16, 16] and "nearest" in stage
    # Each candidate is compared with at most 16 notes, never with all 41.
    for ids in stage["nearest"].values():
        assert len([t for t in ids if not t.startswith("candidate:")]) == NEAREST_NOTES
    assert len(compared_notes(provider, 40)) < 40
