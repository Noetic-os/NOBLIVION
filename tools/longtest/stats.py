# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statistics of the pre-registration, in the standard library only.

Wilson interval; exact two-sided McNemar test; a cluster bootstrap of a paired
difference (cluster = lesson); Holm correction; Theil-Sen slope with an interval;
Cohen's kappa; a logistic regression with cluster-robust errors.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p = hits / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, mid - half), min(1.0, mid + half))


def rate(hits: int, n: int) -> dict[str, float | int]:
    low, high = wilson(hits, n)
    return {
        "hits": hits,
        "n": n,
        "rate": round(hits / n, 4) if n else 0.0,
        "low": round(low, 4),
        "high": round(high, 4),
    }


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p on the discordant pairs: b = only first arm, c = only second."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_counts(first: Sequence[bool], second: Sequence[bool]) -> dict[str, int]:
    if len(first) != len(second):
        raise ValueError("paired arms need the same queries")
    b = sum(1 for x, y in zip(first, second, strict=False) if x and not y)
    c = sum(1 for x, y in zip(first, second, strict=False) if y and not x)
    return {
        "both": sum(1 for x, y in zip(first, second, strict=False) if x and y),
        "only_first": b,
        "only_second": c,
        "neither": sum(1 for x, y in zip(first, second, strict=False) if not x and not y),
    }


def cluster_bootstrap(
    clusters: Mapping[str, Sequence[tuple[float, float]]],
    resamples: int = 10_000,
    seed: int = 7121,
    stat: Callable[[list[tuple[float, float]]], float] | None = None,
) -> dict[str, float]:
    """Bootstrap 95% interval of a paired difference. ``clusters`` maps a cluster
    id (a lesson) to its (first, second) outcome pairs. The default statistic is
    mean(first) - mean(second) over the resampled pairs."""
    keys = sorted(clusters)
    if not keys:
        return {"diff": 0.0, "low": 0.0, "high": 0.0}
    stat = stat or (lambda pairs: sum(a - b for a, b in pairs) / len(pairs))
    flat = [p for k in keys for p in clusters[k]]
    point = stat(flat)
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        pairs: list[tuple[float, float]] = []
        for k in rng.choices(keys, k=len(keys)):
            pairs.extend(clusters[k])
        draws.append(stat(pairs) if pairs else 0.0)
    draws.sort()
    low = draws[int(0.025 * (len(draws) - 1))]
    high = draws[int(math.ceil(0.975 * (len(draws) - 1)))]
    return {"diff": round(point, 4), "low": round(low, 4), "high": round(high, 4)}


def holm(pvalues: Mapping[str, float]) -> dict[str, float]:
    """Holm-adjusted p for each key."""
    order = sorted(pvalues, key=lambda k: pvalues[k])
    m = len(order)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvalues[key]))
        adjusted[key] = running
    return adjusted


def theil_sen(
    xs: Sequence[float], ys: Sequence[float], resamples: int = 2000, seed: int = 7121
) -> dict[str, float | bool]:
    """Median of pairwise slopes, with a bootstrap 95% interval (seeded). ``holds_zero``
    is true when the interval contains 0."""
    pts = [(x, y) for x, y in zip(xs, ys, strict=False)]

    def slope(points: list[tuple[float, float]]) -> float:
        slopes = [
            (y2 - y1) / (x2 - x1)
            for i, (x1, y1) in enumerate(points)
            for (x2, y2) in points[i + 1 :]
            if x2 != x1
        ]
        slopes.sort()
        if not slopes:
            return 0.0
        mid = len(slopes) // 2
        return slopes[mid] if len(slopes) % 2 else (slopes[mid - 1] + slopes[mid]) / 2

    if len(pts) < 3:
        return {"slope": 0.0, "low": 0.0, "high": 0.0, "holds_zero": True, "n": len(pts)}
    point = slope(pts)
    rng = random.Random(seed)
    draws = sorted(slope([pts[rng.randrange(len(pts))] for _ in pts]) for _ in range(resamples))
    low, high = draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]
    return {"slope": point, "low": low, "high": high, "holds_zero": low <= 0 <= high, "n": len(pts)}


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    n = len(a)
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b, strict=False) if x == y) / n
    expected = sum((a.count(k) / n) * (b.count(k) / n) for k in labels)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    rows = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(n):
            if r != col:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [x - factor * y for x, y in zip(rows[r], rows[col], strict=False)]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def _invert(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix)
    cols = [_solve(matrix, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def logistic_cluster(
    rows: Sequence[tuple[Sequence[float], int, str]], iterations: int = 30
) -> dict[str, list[float]]:
    """Logistic regression by IRLS with cluster-robust (sandwich) standard errors.
    ``rows`` hold (features, outcome, cluster); add the intercept yourself. Returns
    the coefficients and their robust standard errors."""
    k = len(rows[0][0])
    beta = [0.0] * k
    for _ in range(iterations):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for x, y, _c in rows:
            z = sum(b * v for b, v in zip(beta, x, strict=False))
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            w = max(p * (1 - p), 1e-9)
            for i in range(k):
                grad[i] += (y - p) * x[i]
                for j in range(k):
                    hess[i][j] += w * x[i] * x[j]
        for i in range(k):
            hess[i][i] += 1e-6  # keeps a separated sample finite
        step = _solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step, strict=False)]
        if max(abs(s) for s in step) < 1e-8:
            break
    # The sandwich: bread^-1 * meat * bread^-1, with one score per cluster.
    bread = [[0.0] * k for _ in range(k)]
    scores: dict[str, list[float]] = {}
    for x, y, c in rows:
        z = sum(b * v for b, v in zip(beta, x, strict=False))
        p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
        w = p * (1 - p)
        acc = scores.setdefault(c, [0.0] * k)
        for i in range(k):
            acc[i] += (y - p) * x[i]
            for j in range(k):
                bread[i][j] += w * x[i] * x[j]
    for i in range(k):
        bread[i][i] += 1e-6
    inv = _invert(bread)
    meat = [[0.0] * k for _ in range(k)]
    for s in scores.values():
        for i in range(k):
            for j in range(k):
                meat[i][j] += s[i] * s[j]
    sandwich = [
        [
            sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k))
            for j in range(k)
        ]
        for i in range(k)
    ]
    return {"beta": beta, "se": [math.sqrt(max(sandwich[i][i], 0.0)) for i in range(k)]}


def scale_trend(outcomes: Sequence[tuple[int, int, int, str]]) -> dict[str, float]:
    """``outcomes`` hold (delivered, arm, notes, lesson) with arm 1 for B1 and 0 for
    stock. Model: delivered ~ arm + log10(notes) + arm x log10(notes). Returns the
    coefficients and their robust errors."""
    rows = [
        ([1.0, float(arm), math.log10(notes), arm * math.log10(notes)], delivered, lesson)
        for delivered, arm, notes, lesson in outcomes
    ]
    fit = logistic_cluster(rows)
    names = ("intercept", "arm", "log10_notes", "arm_x_log10_notes")
    doc: dict[str, float] = {}
    for name, b, s in zip(names, fit["beta"], fit["se"], strict=False):
        doc[name], doc[name + "_se"] = round(b, 4), round(s, 4)
    return doc
