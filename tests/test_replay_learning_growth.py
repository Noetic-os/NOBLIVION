# SPDX-License-Identifier: AGPL-3.0-or-later
"""The growth replay tool counts one job per session at session points (NOBLIVION-143)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_growth_tool", ROOT / "tools/replay_learning_growth.py"
)
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_session_points_leave_one_job_and_no_job_source_rows_per_session():
    result = tool.run(ROOT, "session-points", sessions=3, tools=12, compactions=1, seed=7)
    assert result["jobs"] == 3
    assert result["job_source_rows"] == 0 and result["job_range_rows"] == 3
    assert result["sources"] > 3 and result["db_bytes"] > 0


def test_every_event_makes_more_jobs_than_session_points_at_the_same_ledger():
    every = tool.run(ROOT, "every-event", sessions=2, tools=10, compactions=1, seed=7)
    points = tool.run(ROOT, "session-points", sessions=2, tools=10, compactions=1, seed=7)
    # The ledger merges a session into one job either way; the spool sends fewer batches.
    assert every["jobs"] == points["jobs"] == 2
    assert every["sources"] >= points["sources"]


def test_retention_reaches_steady_state_and_preserves_recall_and_evidence():
    result = tool.run(
        ROOT,
        "session-points",
        sessions=2,
        tools=12,
        compactions=1,
        seed=7,
        days=11,
        pending_days=1,
        retention_days=1,
    )
    retention = result["retention"]
    assert retention["recall_and_evidence_unchanged"]
    assert retention["retained_source"] and retention["recall_hits"] == 1
    assert retention["pruned_totals"]["pending_cancelled"] > 0
    assert retention["pruned_totals"]["links"] > 0
    assert retention["pruned_totals"]["sources"] > 0
    assert retention["steady_state"]["measured_days"] >= 7
    assert retention["steady_state"]["max_daily_growth_bytes"] < 1_000_000


def test_retention_stays_bounded_when_worker_repeatedly_defers_every_pending_job(monkeypatch):
    from contextlib import nullcontext

    from noblivion import db, learning_ranges
    from noblivion.learning import LearningLedger

    prune = learning_ranges.prune
    held = []

    def prune_after_worker_holds(conn, **kwargs):
        ledger = LearningLedger(conn)
        # The replay calls this hook under its write lock. Reuse that fixture
        # transaction for the real claim/defer transitions instead of nesting BEGIN.
        with monkeypatch.context() as scoped:
            scoped.setattr(db, "write_tx", lambda connection: nullcontext(connection))
            while job := ledger.claim("replay-worker"):
                held.append(job["id"])
                ledger.defer(
                    job["id"], job["lease_token"], error="BudgetExceeded", retry_delay=3600
                )
        return prune(conn, **kwargs)

    monkeypatch.setattr(learning_ranges, "prune", prune_after_worker_holds)
    result = tool.run(
        ROOT,
        "session-points",
        sessions=2,
        tools=12,
        compactions=1,
        seed=7,
        days=11,
        pending_days=1,
        retention_days=1,
    )
    retention = result["retention"]
    assert len(held) > 22
    assert retention["pruned_totals"]["pending_cancelled"] > 0
    assert retention["pruned_totals"]["jobs"] > 0
    assert retention["steady_state"]["measured_days"] >= 7
    assert retention["steady_state"]["max_daily_growth_bytes"] < 1_000_000
    assert retention["recall_and_evidence_unchanged"] and retention["retained_source"]
    assert retention["recall_hits"] == 1
    assert result["jobs"] < 12 and result["sources"] < 150
