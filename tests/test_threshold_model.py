# SPDX-License-Identifier: AGPL-3.0-or-later
"""The score thresholds belong to one embedding model (NOBLIVION-33).

Each threshold sits next to ``THRESHOLD_MODEL``, the model it was measured
for. The hooks cannot import the noblivion package, so this test compares the
strings: a change of ``embedding.DEFAULT_MODEL`` fails here until the
thresholds are measured again with ``tools/eval_thresholds.py``.

The rest checks the metric code of that script without a model.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import dedup, embedding

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(scope="module")
def ev():
    name = "noblivion_tool_eval_thresholds"
    spec = importlib.util.spec_from_file_location(name, TOOLS / "eval_thresholds.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("hook", ["recall_hook", "error_recall_hook"])
def test_hook_thresholds_name_the_shipped_model(hook):
    mod = load_hook(hook, f"hooktest_threshold_model_{hook}")
    assert mod.THRESHOLD_MODEL == embedding.DEFAULT_MODEL


def test_dedup_cosine_names_the_shipped_model():
    assert dedup.THRESHOLD_MODEL == embedding.DEFAULT_MODEL


def test_shipped_values_are_on_the_eval_grid(ev):
    rh = load_hook("recall_hook", "hooktest_threshold_model_grid_rh")
    erh = load_hook("error_recall_hook", "hooktest_threshold_model_grid_erh")
    current = ev.shipped(rh, erh)
    assert current["recall"] in ev.RECALL_GRID
    assert current["error_recall"] in ev.RECALL_GRID
    assert current["dedup"] in ev.DEDUP_GRID


def test_eval_set_is_well_formed(ev):
    data = json.loads(ev.DATA.read_text(encoding="utf-8"))
    recall_ids = {m["id"] for m in data["recall"]["memories"]}
    dedup_ids = {m["id"] for m in data["dedup"]["memories"]}
    assert len(recall_ids) == len(data["recall"]["memories"])
    assert len(dedup_ids) == len(data["dedup"]["memories"])
    for key in ("queries", "error_queries"):
        items = data["recall"][key]
        assert any(i["expect"] for i in items) and any(not i["expect"] for i in items)
        for item in items:
            assert set(item["expect"]) <= recall_ids, item
    for a, b in data["dedup"]["duplicates"]:
        assert {a, b} <= dedup_ids and a != b
    for name in ("recall", "error_recall", "dedup"):
        floor = data["gate"][name]
        assert 0 < floor["precision"] <= 1 and 0 < floor["recall"] <= 1


def test_score_queries_counts_true_and_false_hits(ev):
    queries = [{"q": "a", "expect": ["x", "y"]}, {"q": "b", "expect": []}]
    got = {"a": ["x", "z"], "b": ["w"]}
    p = ev.score_queries(0.5, queries, lambda q: got[q])
    assert (p.tp, p.fp, p.fn, p.negatives_hit) == (1, 2, 1, 1)
    assert p.precision == pytest.approx(1 / 3) and p.recall == pytest.approx(0.5)


def test_score_pairs_uses_greater_or_equal(ev):
    cos = {("a", "b"): 0.8, ("a", "c"): 0.7, ("b", "c"): 0.6}
    p = ev.score_pairs(0.7, cos, {("a", "b"), ("b", "c")})
    assert (p.tp, p.fp, p.fn) == (1, 1, 1)


def test_no_hit_counts_as_full_precision(ev):
    p = ev.Point(0.9, 0, 0, 3)
    assert p.precision == 1.0 and p.recall == 0.0 and p.f1 == 0.0


def test_best_takes_the_middle_of_the_top_f1_plateau(ev):
    pts = [
        ev.Point(t, tp, fp, fn)
        for t, tp, fp, fn in [
            (0.1, 4, 4, 0),
            (0.2, 4, 0, 0),
            (0.3, 4, 0, 0),
            (0.4, 4, 0, 0),
            (0.5, 2, 0, 2),
        ]
    ]
    assert ev.best(pts).threshold == 0.3
    assert ev.best(pts[:3]).threshold == 0.2  # two middles: the lower one


def test_check_reports_a_value_under_its_floor(ev):
    results = {"recall": [ev.Point(0.5, 3, 1, 1)]}
    gate = {"recall": {"precision": 0.7, "recall": 0.8}}
    failures = ev.check(results, {"recall": 0.5}, gate)
    assert len(failures) == 1 and "recall: recall 0.750" in failures[0]
    assert ev.check(results, {"recall": 0.5}, {"recall": {"precision": 0.7, "recall": 0.7}}) == []
