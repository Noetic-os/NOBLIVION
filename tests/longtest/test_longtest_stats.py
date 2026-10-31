# SPDX-License-Identifier: AGPL-3.0-or-later
"""The statistics of the long-run test against known values."""

from __future__ import annotations

import math
import random

from longtest import stats


def test_wilson_known_values():
    low, high = stats.wilson(8, 10)
    assert abs(low - 0.4902) < 1e-3 and abs(high - 0.9433) < 1e-3
    assert stats.wilson(0, 0) == (0.0, 1.0)
    assert stats.wilson(0, 30)[0] == 0.0 and stats.wilson(30, 30)[1] == 1.0


def test_mcnemar_exact():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert abs(stats.mcnemar_exact(0, 5) - 0.0625) < 1e-12
    assert stats.mcnemar_exact(3, 3) == 1.0
    assert abs(stats.mcnemar_exact(2, 9) - stats.mcnemar_exact(9, 2)) < 1e-12


def test_paired_counts():
    got = stats.paired_counts([True, True, False, False], [True, False, True, False])
    assert got == {"both": 1, "only_first": 1, "only_second": 1, "neither": 1}


def test_holm_is_monotone_and_capped():
    adj = stats.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert abs(adj["a"] - 0.03) < 1e-12
    assert abs(adj["c"] - 0.06) < 1e-12
    assert abs(adj["b"] - 0.06) < 1e-12
    assert all(v <= 1.0 for v in adj.values())


def test_cluster_bootstrap_is_seeded_and_brackets_the_difference():
    clusters = {f"L{i}": [(1.0, 0.0), (1.0, 1.0)] for i in range(20)}
    one = stats.cluster_bootstrap(clusters, resamples=500, seed=3)
    two = stats.cluster_bootstrap(clusters, resamples=500, seed=3)
    assert one == two
    assert one["diff"] == 0.5 and one["low"] <= 0.5 <= one["high"]


def test_theil_sen_slope_and_zero_interval():
    xs = list(range(10))
    line = stats.theil_sen(xs, [2 * x + 1 for x in xs], resamples=200)
    assert line["slope"] == 2 and not line["holds_zero"]
    rng = random.Random(1)
    flat = stats.theil_sen(xs, [5 + rng.random() for _ in xs], resamples=300)
    assert flat["holds_zero"]


def test_cohen_kappa():
    assert stats.cohen_kappa(list("yynn"), list("yynn")) == 1.0
    assert abs(stats.cohen_kappa(list("yynn"), list("nnyy")) + 1.0) < 1e-9
    assert stats.cohen_kappa([], []) == 0.0


def test_logistic_cluster_finds_the_sign_of_an_effect():
    rng = random.Random(5)
    rows = []
    for i in range(300):
        x = rng.random() * 2 - 1
        y = 1 if rng.random() < 1 / (1 + math.exp(-(2.0 * x))) else 0
        rows.append(([1.0, x], y, f"c{i % 30}"))
    fit = stats.logistic_cluster(rows)
    assert fit["beta"][1] > 1.0 and fit["se"][1] > 0


def test_scale_trend_names_its_terms():
    data = [(1 if (i % 3) else 0, i % 2, 30 * (1 + i % 4), f"L{i % 7}") for i in range(200)]
    doc = stats.scale_trend(data)
    assert "arm_x_log10_notes" in doc and "arm_x_log10_notes_se" in doc
