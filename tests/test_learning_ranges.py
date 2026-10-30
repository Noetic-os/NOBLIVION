# SPDX-License-Identifier: AGPL-3.0-or-later
"""One pending job per session, range links, pruning and the empty-context flag (NOBLIVION-143)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from noblivion import db, learning_ranges, learning_runtime
from noblivion.learning import LearningError, LearningLedger

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
PROJECT, SCOPE = "test", "demo"


def load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def batch(number, session="s1", text=None, client="claude"):
    text = text or f"Use short progress updates {number}."
    return {
        "batch_id": f"batch-{session}-{number}",
        "namespace": PROJECT,
        "root": SCOPE,
        "source_client": client,
        "session_id": session,
        "records": [
            {
                "source_id": f"source-{session}-{number}",
                "record_id": f"record-{session}-{number}",
                "role": "user",
                "content": text,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "observed_at": "2026-10-12T09:00:00Z",
                "metadata": {"authority": "direct_user", "direct_user": True},
            }
        ],
        "cursor": {"path_id": "transcript", "offset": number * 100},
    }


def count(ledger, table):
    return ledger.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "ranges.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def test_batches_of_one_session_share_one_pending_job(ledger):
    jobs = {ledger.capture_batch(batch(n))["job_id"] for n in range(1, 6)}
    assert len(jobs) == 1
    assert count(ledger, "learning_jobs") == 1
    assert count(ledger, "learning_job_sources") == 0
    assert count(ledger, "learning_job_ranges") == 1
    sources = ledger._job_sources(jobs.pop())
    assert [s["id"] for s in sources] == [f"source-s1-{n}" for n in range(1, 6)]


def test_another_session_gets_its_own_job(ledger):
    first = ledger.capture_batch(batch(1, "s1"))["job_id"]
    second = ledger.capture_batch(batch(1, "s2"))["job_id"]
    assert first != second and count(ledger, "learning_jobs") == 2
    assert [s["id"] for s in ledger._job_sources(second)] == ["source-s2-1"]


def test_a_started_job_is_not_changed_and_the_next_job_reads_context(ledger):
    first = ledger.capture_batch(batch(1))["job_id"]
    ledger.capture_batch(batch(2))
    leased = ledger.claim("worker")
    assert leased["id"] == first and len(leased["sources"]) == 2
    second = ledger.capture_batch(batch(3))["job_id"]
    assert second != first
    ids = [s["id"] for s in ledger._job_sources(second)]
    assert ids == ["source-s1-1", "source-s1-2", "source-s1-3"]
    assert [s["id"] for s in ledger._job_sources(first)] == ["source-s1-1", "source-s1-2"]


def test_a_job_that_would_pass_the_size_limit_starts_a_new_job(ledger, monkeypatch):
    monkeypatch.setattr(learning_ranges, "JOB_CHARS", 100)
    first = ledger.capture_batch(batch(1, text="a" * 60))["job_id"]
    same = ledger.capture_batch(batch(2, text="b" * 30))["job_id"]
    other = ledger.capture_batch(batch(3, text="c" * 60))["job_id"]
    assert same == first and other != first
    assert count(ledger, "learning_job_ranges") == 2


def test_evidence_inside_the_range_is_accepted_and_outside_is_refused(ledger):
    first = batch(1)
    ledger.capture_batch(first)
    second = batch(2)
    job = ledger.capture_batch(second)["job_id"]
    ledger.capture_batch(batch(1, "other"))
    claimed = ledger.claim("worker")
    assert claimed["id"] == job

    def cite(payload):
        record = payload["records"][0]
        return [
            {
                "source_id": record["source_id"],
                "source_hash": record["source_hash"],
                "start": 0,
                "end": len(record["content"]),
                "quote": record["content"],
            }
        ]

    assert ledger._evidence(cite(second), PROJECT, SCOPE, job_id=job)
    with pytest.raises(LearningError):
        ledger._evidence(cite(batch(1, "other")), PROJECT, SCOPE, job_id=job)


def test_excluding_a_source_cancels_the_ranged_job(ledger):
    job = ledger.capture_batch(batch(1))["job_id"]
    ledger.capture_batch(batch(2))
    ledger.exclude_source("source-s1-2")
    status = ledger.conn.execute("SELECT status FROM learning_jobs WHERE id=?", (job,)).fetchone()
    assert status[0] == "cancelled"


def test_a_source_stored_before_the_range_links_keeps_explicit_rows(ledger):
    ledger.capture_batch(batch(1))
    ledger.conn.execute("UPDATE learning_sources SET seq=0")
    ledger.conn.execute("DELETE FROM learning_job_ranges")
    ledger.conn.execute("DELETE FROM learning_jobs")
    job = ledger.capture_batch({**batch(2), "records": batch(1)["records"] + batch(2)["records"]})[
        "job_id"
    ]
    assert count(ledger, "learning_job_sources") == 2
    assert [s["id"] for s in ledger._job_sources(job)] == ["source-s1-1", "source-s1-2"]


def age(ledger, days):
    ledger.conn.execute(
        "UPDATE learning_jobs SET created_at=created_at-?,updated_at=updated_at-?",
        (days * DAY, days * DAY),
    )
    ledger.conn.execute("UPDATE learning_sources SET created_at=created_at-?", (days * DAY,))


def test_prune_cancels_stale_pending_jobs_and_drops_unused_sources(ledger):
    ledger.capture_batch(batch(1, "old"))
    age(ledger, 20)
    ledger.capture_batch(batch(1, "new"))
    counts = ledger.prune(pending_days=14, retention_days=14)
    assert counts["pending_cancelled"] == 1
    # The cancelled job is not yet 14 days past its cancel time: its row stays.
    assert count(ledger, "learning_job_ranges") == 2
    age(ledger, 20)
    counts = ledger.prune(pending_days=14, retention_days=14)
    assert counts["links"] >= 1 and counts["jobs"] >= 1 and counts["sources"] >= 1
    assert counts["source_bytes"] > 0
    left = ledger.conn.execute("SELECT session_id FROM learning_sources").fetchall()
    assert left == []


def test_prune_keeps_a_recent_pending_job_and_its_sources(ledger):
    job = ledger.capture_batch(batch(1))["job_id"]
    counts = ledger.prune()
    assert not any(counts.values())
    assert len(ledger._job_sources(job)) == 1


def test_prune_keeps_a_source_that_a_live_job_reads_and_a_source_that_backs_a_note(ledger):
    first = batch(1)
    ledger.capture_batch(first)
    ledger.capture_batch(batch(1, "other"))
    claimed = ledger.claim("worker")
    record = first["records"][0]
    ledger.finish(
        claimed["id"],
        claimed["lease_token"],
        [
            {
                "action": "add",
                "logical_key": "progress-style",
                "category": "preference",
                "title": "Progress format",
                "statement": record["content"],
                "observed_at": "2026-10-12",
                "effective_at": None,
                "scope": "general",
                "evidence": [
                    {
                        "source_id": record["source_id"],
                        "source_hash": record["source_hash"],
                        "start": 0,
                        "end": len(record["content"]),
                        "quote": record["content"],
                    }
                ],
            }
        ],
    )
    age(ledger, 40)
    ledger.conn.execute("UPDATE learning_jobs SET status='cancelled' WHERE session_id='s1'")
    ledger.prune(pending_days=1000, retention_days=14)
    kept = {r[0] for r in ledger.conn.execute("SELECT session_id FROM learning_sources")}
    assert kept == {"s1", "other"}


def test_prune_dry_run_counts_and_changes_nothing(ledger):
    ledger.capture_batch(batch(1))
    age(ledger, 30)
    before = [
        count(ledger, t) for t in ("learning_jobs", "learning_sources", "learning_job_ranges")
    ]
    counts = ledger.prune(dry_run=True)
    assert counts["pending_cancelled"] == 1
    after = [count(ledger, t) for t in ("learning_jobs", "learning_sources", "learning_job_ranges")]
    assert before == after
    assert ledger.conn.execute("SELECT status FROM learning_jobs").fetchone()[0] == "pending"


def test_prune_refuses_a_bad_retention(ledger):
    for bad in (0, -1, True, "7", 4000):
        with pytest.raises(LearningError):
            ledger.prune(retention_days=bad)


def test_prune_settings_are_opt_in_and_validated():
    assert learning_runtime.prune_options({}) is None
    assert learning_runtime.prune_options({"prune": {"enabled": False}}) is None
    assert learning_runtime.prune_options({"prune": {"enabled": True, "pending_days": 3}}) == {
        "pending_days": 3
    }
    for bad in ({"enabled": "yes"}, {"enabled": True, "other": 1}, [], 5):
        with pytest.raises(ValueError):
            learning_runtime.prune_options({"prune": bad})


def test_context_items_count_notes_and_zero_when_none(ledger):
    assert ledger.context_items(PROJECT, SCOPE) == 0
    assert learning_runtime.summary(
        SimpleNamespace(connection=lambda: _Conn(ledger.conn)), PROJECT, SCOPE
    ) == {"summary": "", "reason": None, "context_items": 0}


class _Conn:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *args):
        return False


def test_claude_capture_runs_only_at_session_start_compaction_and_end(tmp_path):
    capture = load("codex/capture.py", "_ranges_capture")
    cfg = {
        "repo_root": str(ROOT),
        "state_dir": str(tmp_path / "state"),
        "namespace": PROJECT,
        "recall_root": SCOPE,
        "learning": {"contribute": True},
        "project_mappings": [{"workspace": str(tmp_path), "root": SCOPE, "namespace": PROJECT}],
    }
    event = {"session_id": "s1", "cwd": str(tmp_path)}
    for name in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "SubagentStop"):
        result = capture.capture_hook(cfg, {**event, "hook_event_name": name}, "claude")
        assert result == {"status": "ignored"}
    assert not (tmp_path / "state").exists()
    assert capture.CLAUDE_CAPTURE_EVENTS == {"SessionStart", "PreCompact", "SessionEnd"}


def test_prompt_hook_skips_its_store_call_when_the_session_started_with_no_items(
    tmp_path, monkeypatch
):
    summary = load("hooks/learning_summary.py", "_ranges_summary")
    context = load("hooks/learning_context.py", "_ranges_context")
    calls = []

    def get(build, env, timeout):
        url = build("http://127.0.0.1:1")
        calls.append(url)
        if "/learning/summary" in url:
            return {"summary": "", "reason": None, "context_items": 0}
        return {"context": "- x", "reason": None}

    recall = SimpleNamespace(store_get=get, session_env=lambda env, event: dict(env))
    real_load = summary.load

    def fake_load(path, name):
        return recall if path.name == "recall_hook.py" else real_load(path, name)

    monkeypatch.setattr(summary, "load", fake_load)
    monkeypatch.setattr(context, "load", fake_load)
    env = {
        "NOBLIVION_RECALL_ROOT": SCOPE,
        "NOBLIVION_RECALL_PROJECT": PROJECT,
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
    }
    start = {"hook_event_name": "SessionStart", "session_id": "s-empty", "source": "startup"}
    assert summary.run(start, env, codex=True) == {}
    flags = list((tmp_path / "data" / "cache" / "learning-context").iterdir())
    assert len(flags) == 1 and flags[0].read_text() == "0"
    assert oct(flags[0].stat().st_mode & 0o777) == "0o600"
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-empty", "prompt": "hello"}
    assert context.run(prompt, env, codex=True) == {}
    assert len(calls) == 1  # the summary call only
    # A session with no flag still asks the store.
    other = {**prompt, "session_id": "s-unknown"}
    assert context.run(other, env, codex=True) != {}
    assert len(calls) == 2


def test_prompt_hook_asks_the_store_when_the_session_started_with_items(tmp_path, monkeypatch):
    summary = load("hooks/learning_summary.py", "_ranges_summary2")
    context = load("hooks/learning_context.py", "_ranges_context2")
    calls = []

    def get(build, env, timeout):
        url = build("http://127.0.0.1:1")
        calls.append(url)
        if "/learning/summary" in url:
            return {"summary": "- a", "reason": None, "context_items": 2}
        return {"context": "- x", "reason": None}

    recall = SimpleNamespace(store_get=get, session_env=lambda env, event: dict(env))
    real_load = summary.load

    def fake_load(path, name):
        return recall if path.name == "recall_hook.py" else real_load(path, name)

    monkeypatch.setattr(summary, "load", fake_load)
    monkeypatch.setattr(context, "load", fake_load)
    env = {
        "NOBLIVION_RECALL_ROOT": SCOPE,
        "NOBLIVION_RECALL_PROJECT": PROJECT,
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
    }
    start = {"hook_event_name": "SessionStart", "session_id": "s-full", "source": "startup"}
    assert summary.run(start, env, codex=True) != {}
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-full", "prompt": "hello"}
    assert context.run(prompt, env, codex=True) != {}
    assert len(calls) == 2


def test_an_old_flag_is_ignored(tmp_path, monkeypatch):
    summary = load("hooks/learning_summary.py", "_ranges_summary3")
    context = load("hooks/learning_context.py", "_ranges_context3")
    env = {"NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    summary.note_context_items(ROOT, env, "s-old", 0)
    flag = next((tmp_path / "data" / "cache" / "learning-context").iterdir())
    assert context.no_context_items(ROOT, env, "s-old") is True
    stale = time.time() - 2 * DAY
    os.utime(flag, (stale, stale))
    assert context.no_context_items(ROOT, env, "s-old") is False
    summary.note_context_items(ROOT, env, "s-bad", "x")
    assert len(list(flag.parent.iterdir())) == 1


def test_database_files_have_range_tables(ledger):
    names = {r[0] for r in ledger.conn.execute("SELECT name FROM sqlite_schema")}
    assert {"learning_job_ranges", "learning_sources_seq"} <= names
    assert json.loads(json.dumps(ledger.prune(dry_run=True)))
