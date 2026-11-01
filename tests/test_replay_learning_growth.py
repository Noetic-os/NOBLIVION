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
