# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held-out replay scores external effects and preserves its frozen labels."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "tools/eval_consolidation.py"
DATA = ROOT / "tools/eval"


def test_heldout_corpus_and_executor_match_frozen_manifest():
    manifest = json.loads((DATA / "consolidation_manifest.json").read_text())
    for key, file in (
        ("corpus_sha256", "consolidation_heldout.json"),
        ("executor_sha256", "consolidation_task.py"),
    ):
        assert hashlib.sha256((DATA / file).read_bytes()).hexdigest() == manifest[key]
    corpus = json.loads((DATA / "consolidation_heldout.json").read_text())
    assert corpus["split"] == "heldout-consolidation-2026-10-13"
    assert corpus["tuning_allowed"] is False and len(corpus["facts"]) > 32
    threshold_data = json.loads((DATA / "thresholds.json").read_text())
    assert "archive_partition_39" not in json.dumps(threshold_data)


def test_external_executor_cannot_guess_missing_or_conflicting_values(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "heldout_executor", DATA / "consolidation_task.py"
    )
    executor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(executor)
    context, output = tmp_path / "context.txt", tmp_path / "config.json"
    context.write_text("unrelated context")
    executor.execute("archive_partition_39", context, output)
    assert json.loads(output.read_text()) == {}
    context.write_text("Set archive_partition_39 to 1739. Set archive_partition_39 to 9999.")
    executor.execute("archive_partition_39", context, output)
    assert json.loads(output.read_text()) == {}
    context.write_text("Set archive_partition_39 to 1739.")
    executor.execute("archive_partition_39", context, output)
    assert json.loads(output.read_text()) == {"archive_partition_39": 1739}


@pytest.mark.parametrize("memory,expected", [("on", 8), ("off", 0)])
def test_changed_source_real_store_hook_and_file_outcome(tmp_path, memory, expected):
    run = subprocess.run(
        [sys.executable, str(EVAL), "--child", "--source", str(ROOT), "--memory", memory],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(run.stdout)
    assert report["tasks_completed"] == expected
    assert report["task_count"] == 8 and report["retained_particulars"] == 40
    assert report["unsupported_claims"] == 0 and report["stale_guidance"] == 0
    assert report["recovery"]["all_labels_recovered"] is True
    assert report["recovery"]["duplicates"] == 0
    assert report["zero_relevance_count"] == 2
    assert report["zero_relevance_empty"] == (0 if memory == "on" else 2)
    assert report["query_hook_zero_relevance_empty"] == 2
    assert "hooks/learning_summary.py" in report["hook_paths"]
    assert "hooks/recall_hook.py" in report["hook_paths"]
    assert (
        all(t["startup_context_chars"] > 0 for t in report["tasks"])
        if memory == "on"
        else all(t["startup_context_chars"] == 0 for t in report["tasks"])
    )
    assert (
        all(task["actual"] == {task["key"]: task["expected"]} for task in report["tasks"])
        if memory == "on"
        else all(task["actual"] == {} for task in report["tasks"])
    )
