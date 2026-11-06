# SPDX-License-Identifier: AGPL-3.0-or-later
"""The run profiles: what a run tests, and the settings that keep it alive.

``plan2`` is the first run: every scale, B0 and B1, every claim, five days.
``large-project`` is the run on NOBLIVION's assumed strong point: B1 against S2 at
5,000 and 20,000 notes in 50 projects, with about 500 notes added to each replay
store every day, seven days, with the claims and the rules of the large project.

The manifest names the profile. The pre-registration text carries it, so the hash
in the manifest ties a run to one profile; a run cannot change profile after the
freeze. The old profile keeps its text byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import config
from .config import PARAMS, SCALES, Params
from .util import sha256_text

LARGE_SIZES = (5000, 20000)
LARGE_CLAIMS = ("C1", "C2", "C3", "C4", "C10", "C13", "C16", "C17", "C11", "C14", "C24")
# V1 is the sanity gate of the pilot; V3 carries C1 and C17 (and C10 for a lesson past
# the index cap), V5 carries C2, V2 carries C3, V4 carries C4, LABEL carries C16.
LARGE_VARIANTS = ("V1", "V2", "V3", "V4", "V5", "LABEL")
LARGE_RULES = (
    (
        "DR1",
        "B1 beats S2 on claim C (C1 or C3) at scale X: the lower bound of B1 minus S2 "
        "is above {dr1_lower}, and the Holm-adjusted p is under {alpha}.",
    ),
    (
        "DR3",
        "False rows on C2 (V5 near misses) not worse than S2: a correct silence counts "
        "as a hit in both arms, and the lower bound of B1 minus S2 on C2 is above "
        "{dr3_lower}.",
    ),
    (
        "DR5",
        "Health, every day: recall p95 within {budget_recall_ms} ms under steady load; "
        "write-to-searchable lag p95 {lag_p95_s} s or less; drift 0 at every check; "
        "error rate upper bound under {error_rate_upper}.",
    ),
    (
        "DR6",
        "Trend: the Theil-Sen slope of B1 delivery on C1 over the nights of the run has "
        "a 95% interval that holds 0 or lies above 0. Needs three nights or more.",
    ),
)
LARGE_VERDICT = (
    "Verdict per scale: WIN when DR1 holds on C1 or C3 and DR3, DR5 and DR6 hold. LOSE "
    "when the upper bound of B1 minus S2 is under 0 on C1 or C3, or DR3 fails. PENDING "
    "while DR5 or DR6 has no data. Else TIE."
)


@dataclass(frozen=True)
class UnitSettings:
    """The restart policy of the systemd units. ``runtime_max`` is a backstop after the
    loop's own end; ``None`` keeps the old form (``<run_days>d 6h``)."""

    restart: str = "on-failure"
    restart_sec: int = 60
    start_limit_interval_sec: int | None = None
    runtime_max: str | None = None


@dataclass(frozen=True)
class Profile:
    name: str
    params: Params
    builds: tuple[str, ...]
    sizes: tuple[int, ...]
    stock_arms: tuple[str, ...]  # the rows of the stock simulation that the run keeps
    variants: tuple[str, ...] | None  # None: every variant of the fixture
    claims: tuple[str, ...]
    growth_per_day: int  # notes added to each kept replay store before every night
    determinism: bool  # the small scales replay every night and their hash must repeat
    watchdog: bool  # the timer unit that restarts a quiet core unit
    units: UnitSettings
    soak_size: int = 20000

    def prereg(self) -> str:
        return render_prereg(self)

    def prereg_hash(self) -> str:
        return sha256_text(self.prereg())

    def nightly_sizes(self, day: int, sizes: list[int]) -> list[int]:
        """Which scales replay on ``day``. The large-project run replays every scale
        every night (the stores grow, so every night is a new point); the first run
        replays only its small scales between the first and the last day."""
        if self.determinism and 1 < day < self.params.run_days:
            return [s for s in sizes if s <= 200]
        return list(sizes)


PLAN2 = Profile(
    name="plan2",
    params=PARAMS,
    builds=("B0", "B1"),
    sizes=tuple(SCALES.values()),
    stock_arms=("S0", "S1", "S2"),
    variants=None,
    claims=config.CLAIMS,
    growth_per_day=0,
    determinism=True,
    watchdog=False,
    units=UnitSettings(),
)

LARGE = Profile(
    name="large-project",
    params=replace(
        PARAMS,
        run_days=7,
        heartbeat_s=300,
        heartbeats_per_day=288,
        scales=tuple((k, v) for k, v in SCALES.items() if v in LARGE_SIZES),
    ),
    builds=("B1",),
    sizes=LARGE_SIZES,
    stock_arms=("S2",),
    variants=LARGE_VARIANTS,
    claims=LARGE_CLAIMS,
    growth_per_day=500,
    determinism=False,
    watchdog=True,
    units=UnitSettings(
        restart="always", restart_sec=30, start_limit_interval_sec=0, runtime_max="8d"
    ),
)

PROFILES = {p.name: p for p in (PLAN2, LARGE)}
DEFAULT = PLAN2.name


def get(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(f"unknown profile {name!r}; the profiles are {sorted(PROFILES)}") from None


def profile_of(manifest: dict) -> Profile:
    """The profile a run manifest names. A manifest from before the profiles is the
    first run."""
    return get(str(manifest.get("profile") or DEFAULT))


def render_prereg(profile: Profile) -> str:
    """The pre-registration text of a profile. Same profile, same bytes."""
    if profile.name == PLAN2.name:
        return config.render_prereg(profile.params)
    doc = profile.params.as_dict()
    lines = [
        "# Pre-registration of the long-run test on a large project",
        "",
        "Written before day 1. The sha256 of this text goes into the run manifest.",
        "",
        f"Profile: {profile.name}. Builds: {', '.join(profile.builds)} against "
        f"{', '.join(profile.stock_arms)} (stock memory simulation).",
        f"Scales: {', '.join(str(s) for s in profile.sizes)} notes, 50 projects at the "
        "largest. Every scale replays every night.",
        f"Growth: about {profile.growth_per_day} filler notes a day are added to each "
        "kept replay store before its night, so the last night tests the grown store. "
        "Growth notes hold no answer string of a lesson.",
        f"Length: {profile.params.run_days} days of 24 hours from the start in state.json. "
        "A restart keeps the same end.",
        "",
        "## Parameters",
        "",
    ]
    for key in sorted(doc):
        lines.append(f"- {key}: {doc[key]}")
    lines += [
        "",
        "## Statistics",
        "",
        "- Rates: Wilson 95% interval.",
        "- Paired differences: exact two-sided McNemar test on discordant pairs, and a "
        "cluster bootstrap 95% interval (cluster = lesson, fixed seed).",
        "- Holm correction across all claims and scales tested.",
        "- Trend: Theil-Sen slope over the nights, with a bootstrap 95% interval.",
        "",
        "## Decision rules",
        "",
    ]
    for name, text in LARGE_RULES:
        lines.append(f"- {name}: " + text.format(**doc))
    lines += ["", LARGE_VERDICT, ""]
    lines += [
        "## Claims",
        "",
        "- In scope: " + ", ".join(profile.claims) + ".",
        "- Variants replayed: " + ", ".join(profile.variants or ()) + ".",
        "- DR1 and DR3 are judged on the rows of the last night, on the grown store. "
        "The nights before feed DR6.",
        "- C2 (V5, a near miss): a false row of B1 is any row on the prompt. A false "
        "row of S2 is a near-miss note of the lesson that stock loads in its index or "
        "opens (stock loads its index on every prompt, so 'any row' would be 1.0 by "
        "construction). The rule is conservative against B1.",
        "- C4 has no stock mechanism (by construction): the result reports the "
        "NOBLIVION rate and counts no win.",
        "- C10 is reported from the V3 rows whose lesson sits past the index cap.",
        "- C11, C14 and C24 come from the soak store and the nightly latency benchmark.",
        "- A validity check that fires gets a cause kind: T test, I implementation, "
        "D design, M model.",
        "",
        "## The run must not fail before the last day",
        "",
        "- No job raises out of the loop: a failed job is logged to out/failures.jsonl "
        "and ALERT and retried after 30 minutes, inside its window.",
        "- The daily check writes daily/<day>.json and ALERT only; never stop or pause.",
        "- The units restart always; a watchdog timer starts a unit that is inactive "
        "and restarts the core unit when the heartbeat is older than 15 minutes.",
        "- A start inside the heavy window (01:00 to 06:00) is written as the end of "
        "that window, so every day of the run holds one night.",
        "",
    ]
    return "\n".join(lines)
