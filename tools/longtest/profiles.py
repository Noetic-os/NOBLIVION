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
# The time-compressed run: the same scales, growth, variants and thresholds; the steps
# run back to back, with no soak store, so the replay claims and rules only.
STEPS_CLAIMS = ("C1", "C2", "C3", "C4", "C10", "C13", "C16", "C17")
STEPS_DR6 = (
    "DR6",
    "Trend: the Theil-Sen slope of B1 delivery on C1 over the steps of the run has "
    "a 95% interval that holds 0 or lies above 0. Needs three steps or more.",
)
STEPS_VERDICT = (
    "Verdict per scale: INVALID when the validity gate fails on a judged step. WIN when "
    "DR1 holds on C1 or C3 and DR3 and DR6 hold. LOSE when the upper bound of B1 minus "
    "S2 is under 0 on C1 or C3, or DR3 fails. PENDING while DR6 has no data. Else TIE."
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
    steps: int = 0  # growth steps that `steps` runs back to back; 0: nights of 24 hours

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
        sanity_s2_delivery=None,  # the mechanical gate of S2 (report.stock_gate)
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

STEPS = replace(
    LARGE,
    name="large-project-steps",
    claims=STEPS_CLAIMS,
    steps=7,
    watchdog=False,
    units=UnitSettings(),
)

PROFILES = {p.name: p for p in (PLAN2, LARGE, STEPS)}
LARGE_FAMILY = (LARGE.name, STEPS.name)  # judged by the rules of the large project
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
    if profile.steps:
        return render_steps_prereg(profile)
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


def render_steps_prereg(profile: Profile) -> str:
    """The pre-registration text of the time-compressed run. Same profile, same bytes."""
    from . import warm
    from .buildrunner import EMBED_THREADS

    doc = profile.params.as_dict()
    lines = [
        "# Pre-registration of the time-compressed long-run test on a large project",
        "",
        "Written before step 1. The sha256 of this text goes into the run manifest.",
        "",
        f"Profile: {profile.name}. Builds: {', '.join(profile.builds)} against "
        f"{', '.join(profile.stock_arms)} (stock memory simulation).",
        f"Scales: {', '.join(str(s) for s in profile.sizes)} notes, 50 projects at the "
        "largest. Every scale replays at every step.",
        f"Steps: {profile.steps} growth steps, back to back, with no nightly window and no "
        f"wait between them. Before each step about {profile.growth_per_day} filler notes "
        "are added to each kept replay store, so the last step tests the grown store. "
        "Growth notes hold no answer string of a lesson. A step is written as a night: "
        "its number is the day of its rows.",
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
        "- Trend: Theil-Sen slope over the steps, with a bootstrap 95% interval.",
        "",
        "## Validity",
        "",
        "- Before every shard the store is started. It is ready only when its index is "
        "idle and no chunk of a live note lacks a vector: a store that still embeds "
        "answers late, and the hooks then latch. The wait is the store wait of the "
        "scale; a store that is not ready inside it fails the shard and says how many "
        "chunks were left.",
        "- Then the store is warmed: one call of the recall tool and one prompt per "
        f"round, until both answer inside {warm.HOOK_BUDGET_S:g} s, for "
        f"{warm.WARM_BOUND_S:g} s at most, and the latch file {warm.LATCH_FILE} of the "
        "hooks is removed.",
        "- Every row carries the fail reason of its hook. A row that ran under the latch "
        f"({warm.LATCHED}), or whose hook gave any fail reason ({warm.GAVE_UP}, "
        "store_down, unreachable, busy and the others: the store did not answer), or "
        "whose call did not run, is invalid, not a miss: its shard is run again and is "
        "not judged, and a step that holds such a row fails the gate below. A product "
        "decision to stay silent is a skip, not a fail.",
        "- One store starts at a time.",
        f"- No measured work between {config.QUIET_WINDOW.replace('-', ' and ')} local "
        "time, because the host load peaks then: the run pauses before the next step "
        "and before the next store, and resumes when the window ends.",
        "- Threads: the embedder of the build takes no thread count. The harness sets "
        f"ORT_INTRA_OP_NUM_THREADS={EMBED_THREADS} for the store, the name that the model "
        "runtime reads (one batch then took 3.5 cores, and 15 without it). "
        "OMP_NUM_THREADS does not bind the embedder.",
        "- Gate per step, scale and arm. A build: the delivery on the sanity set (V1, "
        f"the recall tool) is {doc['sanity_pull_delivery']} or more for "
        f"{', '.join(profile.builds)}, and no row is invalid. A stock arm "
        f"({', '.join(profile.stock_arms)}): the gate is mechanical, every query of the "
        "step is evaluated once and no row has an error; its delivery on the sanity set "
        "is reported and has no floor (see Fairness). A scale with a failed gate on any "
        "step gets the result INVALID with the cause, and DR1, DR3 and DR6 are not judged "
        "there. INVALID is never printed as a win or a loss.",
        f"- A run with fewer than {profile.steps} finished steps gets the result "
        "INCOMPLETE at every scale, with the steps that are missing. No rule is judged "
        "on it. The rows of a step that did not finish are not read.",
        "- The prompt channel of a build has no floor: the prompt hook holds a row back "
        "by its own rules (52 of 60 on a healthy store). Its delivery is reported.",
        "",
        "## Fairness",
        "",
        "- Ruling of 2026-11-11: the gate of S2 is mechanical (every query evaluated, 0 "
        "errors). Its sanity rate is reported, not gated. The gate of B1 stays the "
        f"delivery of the recall tool on the sanity set, {doc['sanity_pull_delivery']} or "
        "more.",
        "- Reason: the validity gate asks whether an arm fired, not how well it did. B1 "
        "can fail to fire: a hook that waits for a busy store latches and answers empty, "
        "so its floor proves that the store answered. S2 is a deterministic simulation of "
        "the stock loads over the files: it cannot latch and it has no store to wait for. "
        "Its sanity rate is a result of stock at the scale (0.70 to 0.80 at 5,000 and "
        "20,000 notes), not a proof that it ran. A floor on it would void every scale for "
        "the hypothesis under test, and it would not catch a fault that the mechanical "
        "gate does not catch.",
        "- What stays the same: the fixture, the queries, DR1, DR3, DR6 and their "
        "thresholds. A low S2 rate counts in the comparison as it is.",
        "",
        "## Decision rules",
        "",
    ]
    for name, text in (*(r for r in LARGE_RULES if r[0] in ("DR1", "DR3")), STEPS_DR6):
        lines.append(f"- {name}: " + text.format(**doc))
    lines += ["", STEPS_VERDICT, ""]
    lines += [
        "## Claims",
        "",
        "- In scope: " + ", ".join(profile.claims) + ".",
        "- Variants replayed: " + ", ".join(profile.variants or ()) + ".",
        "- DR1 and DR3 are judged on the rows of the last step, on the grown store. "
        "The steps before feed DR6.",
        "- C2 (V5, a near miss): a false row of B1 is any row on the prompt. A false "
        "row of S2 is a near-miss note of the lesson that stock loads in its index or "
        "opens (stock loads its index on every prompt, so 'any row' would be 1.0 by "
        "construction). The rule is conservative against B1.",
        "- C4 has no stock mechanism (by construction): the result reports the "
        "NOBLIVION rate and counts no win.",
        "- C10 is reported from the V3 rows whose lesson sits past the index cap.",
        "- Health (DR5) is not judged by this run: it has no soak store.",
        "- A validity check that fires gets a cause kind: T test, I implementation, "
        "D design, M model.",
        "",
    ]
    return "\n".join(lines)
