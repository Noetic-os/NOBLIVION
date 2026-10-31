# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen parameters, decision rules and the pre-registration text.

Every number the verdict depends on lives here, so ``render_prereg`` can write it
down once, ``freeze`` can store its hash, and a later run refuses to start when the
text differs. Nothing here is tuned on the sealed split.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import sha256_text

SCALES = {"SMALL": 30, "MEDIUM-1": 200, "MEDIUM-2": 1000, "LARGE-1": 5000, "LARGE-2": 20000}
CLAIMS = tuple(f"C{n}" for n in range(1, 25))
BY_CONSTRUCTION = ("C4", "C5", "C5b", "C7", "C8", "C9")  # stock has no mechanism
BUILDS = ("B0", "B1")
ARMS = ("S0", "S1", "S2", "S-cal")
SEED = 20261030
WINDOW = "01:00-06:00"  # heavy jobs only


@dataclass(frozen=True)
class Params:
    seed: int = SEED
    bootstrap_resamples: int = 10_000
    bootstrap_seed: int = 7_121
    # DR1: B1 better than stock
    dr1_lower: float = 0.10
    alpha: float = 0.05
    # DR2: stock fails
    dr2_upper: float = 0.80
    # DR3: B1 not worse than B0
    dr3_lower: float = -0.05
    # DR4: B1 cheaper than B0
    dr4_char_ratio: float = 0.70
    dr4_resent_ratio: float = 0.50
    # DR5: false delivery on V5
    dr5_small_medium: float = 0.10
    dr5_large: float = 0.15
    # DR6: precision on the sealed set
    dr6_small_medium: float = 0.60
    dr6_large: float = 0.50
    # DR7: prompt hook p95 at 20,000 notes, milliseconds
    dr7_p95_ms: float = 500.0
    # DR8: soak
    budget_recall_ms: float = 2000.0
    budget_embedding_ms: float = 1000.0
    budget_error_recall_ms: float = 800.0
    budget_guard_ms: float = 1500.0
    error_rate_upper: float = 0.005
    lag_p95_s: float = 10.0
    memory_growth_mb_per_day: float = 10.0
    # stock calibration: runs that opened the lesson note, of recorded stock runs
    calibration_hits: int = 8
    calibration_runs: int = 32
    # limits of the run
    min_free_gb: float = 50.0
    cpu_quota: str = "400%"
    memory_max: str = "12G"
    load_pause: float = 16.0
    soak_load_factor: int = 10
    soak_burst: int = 20
    retention_days: int = 30
    # pilot sanity (section 12, gate 3)
    sanity_pull_delivery: float = 0.95
    sanity_s2_delivery: float = 0.90
    # the stock loads (Claude Code): first 200 lines or 25,000 bytes of MEMORY.md
    index_lines: int = 200
    index_bytes: int = 25_000
    linked_files: int = 3
    search_files: int = 5
    label_kappa_min: float = 0.60
    scales: tuple = field(default_factory=lambda: tuple(SCALES.items()))

    def as_dict(self) -> dict:
        doc = asdict(self)
        doc["scales"] = dict(self.scales)
        return doc


PARAMS = Params()

RULES = (
    (
        "DR1",
        "B1 better than stock for claim C at scale X: lower bound of B1 minus S2 above "
        "{dr1_lower}, and the Holm-adjusted p under {alpha}.",
    ),
    (
        "DR2",
        "Stock fails at scale X: the S2 Wilson upper bound is under {dr2_upper}. If stock "
        "does not fail, the claim at that scale is 'no test', not a loss.",
    ),
    (
        "DR3",
        "B1 not worse than B0: the lower bound of B1 minus B0 delivery is above "
        "{dr3_lower} for every claim and scale.",
    ),
    (
        "DR4",
        "B1 cheaper than B0: the upper bound of the paired character ratio is "
        "{dr4_char_ratio} or less, and live re-sent tokens per session are "
        "{dr4_resent_ratio} of the B0 week or less.",
    ),
    (
        "DR5",
        "False delivery: the B1 upper bound on V5 is {dr5_small_medium} or less (SMALL, "
        "MEDIUM) and {dr5_large} or less (LARGE).",
    ),
    (
        "DR6",
        "Precision on the sealed set: {dr6_small_medium} or more (SMALL, MEDIUM), "
        "{dr6_large} or more (LARGE), and B1 minus B0 at 0 or more.",
    ),
    ("DR7", "Latency: B1 prompt hook p95 is {dr7_p95_ms} ms or less at 20,000 notes."),
    (
        "DR8",
        "Soak pass: every day p95 within the hook budgets (recall {budget_recall_ms} ms, "
        "embedding {budget_embedding_ms} ms, error recall {budget_error_recall_ms} ms, "
        "guard {budget_guard_ms} ms); error rate upper bound under {error_rate_upper}; "
        "drift 0 at every hourly check; lag p95 {lag_p95_s} s or less; Theil-Sen slope of "
        "latency and memory with an interval that holds 0, or memory growth under "
        "{memory_growth_mb_per_day} MB a day; PRAGMA quick_check ok.",
    ),
)

VERDICT = (
    "Verdict per scale: WIN when DR1 holds on C1 or C3 and DR5 and DR6 hold. LOSE when the "
    "upper bound of B1 minus S2 is under 0 on a primary claim, or DR5 fails. Else TIE."
)


def render_prereg(params: Params = PARAMS) -> str:
    """The pre-registration text. Same parameters, same bytes."""
    doc = params.as_dict()
    lines = [
        "# Pre-registration of the long-run test",
        "",
        "Written before day 1. The sha256 of this text goes into the run manifest.",
        "",
        "## Parameters",
        "",
    ]
    for key in sorted(doc):
        lines.append(f"- {key}: {doc[key]}")
    lines += ["", "## Statistics", ""]
    lines += [
        "- Rates: Wilson 95% interval.",
        "- Paired differences: exact two-sided McNemar test on discordant pairs, and a "
        "cluster bootstrap 95% interval (cluster = lesson, fixed seed).",
        "- Holm correction across all claims and scales tested.",
        "- Scale trend: logistic regression of delivered on arm, log10(notes) and their "
        "interaction, with cluster-robust errors.",
        "",
        "## Decision rules",
        "",
    ]
    for name, text in RULES:
        lines.append(f"- {name}: " + text.format(**doc))
    lines += ["", VERDICT, ""]
    lines += [
        "## Claims",
        "",
        "- Claims without a stock mechanism (by construction): " + ", ".join(BY_CONSTRUCTION),
        "  The result reports the NOBLIVION rate and its false-fire rate and counts no win.",
        "- A validity check that fires gets a cause kind: T test, I implementation, "
        "D design, M model.",
        "",
    ]
    return "\n".join(lines)


def prereg_hash(params: Params = PARAMS) -> str:
    return sha256_text(render_prereg(params))


def check_prereg(path: Path, expected: str) -> None:
    """Refuse a run whose pre-registration file differs from the frozen hash."""
    actual = sha256_text(Path(path).read_text(encoding="utf-8"))
    if actual != expected:
        raise SystemExit(f"PREREG.md changed since freeze: {actual[:12]} is not {expected[:12]}")
