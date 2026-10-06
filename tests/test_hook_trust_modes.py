# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three trust modes. They answer a measured design limit: almost every
boosted memory reaches the 1.25 factor only through guard ``rows`` events,
which fire on commands, not on the prompt.

One setting, NOBLIVION_RECALL_TRUST_MODE; unset = the behaviour before the
modes.

What these tests hold:

1. THE SETTING. ``a``, ``b`` or ``c`` (any case); anything else is the default.
   The events module and the rank module read it the same way.
2. MODE a. It has no effect now: a guard ``rows`` decision gives no
   ``use`` event in any mode (NOBLIVION-34); ``deny`` still does. The guard
   decision ``labels`` never gives a ``use`` event, in any mode.
3. MODE b. The factor comes from the use rate: sessions where the memory was
   shown AND used, over the sessions where it was shown, smoothed toward the
   pool rate and divided by it. It is graded (not 1.0 or 1.25 only), 1.0 below
   5 shown sessions, and 1.0 when the counts are missing.
4. MODE c. The trust factor applies only to a row whose relevance score (the
   daemon cosine) is at or above the threshold (default 0.58; env
   NOBLIVION_RECALL_TRUST_MIN_SCORE).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from hookload import load_hook

te = load_hook("trust_events", "trust_events_t_trust_modes")
tr = load_hook("trust_rank", "trust_rank_t_trust_modes")

TS = "2026-09-30T09:00:00+00:00"
ON = {"NOBLIVION_RECALL_INDEX_TRUST": "1"}
MODE = "NOBLIVION_RECALL_TRUST_MODE"


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited settings."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    return {"home": home, "data": data}


class Row:
    def __init__(
        self,
        mid: int,
        fused: float,
        title: str = "",
        score: float | None = None,
        trust=None,
        trials=None,
        prior=None,
    ):
        self.mid, self.fused, self.title, self.score = mid, fused, title, score
        self.trust, self.trials, self.trust_prior = trust, trials, prior


class Md:
    def __init__(self, rel_path: str):
        self.rel_path = rel_path


# 1. the setting ----------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [(None, ""), ("", ""), ("a", "a"), ("B", "b"), (" c ", "c"), ("d", ""), ("1", ""), ("ab", "")],
)
def test_the_mode_setting_reads_the_same_in_both_modules(raw, expected):
    env = {} if raw is None else {MODE: raw}
    assert tr.trust_mode(env) == expected
    assert te.trust_mode(env) == expected


# 2. mode a ---------------------------------------------------------------
def test_guard_rows_are_not_use_in_any_mode_and_deny_still_is():
    """Since NOBLIVION-34 guard rows are never a use, so mode ``a`` changes
    nothing. MUTANT: count rows again outside mode ``a``."""
    for env in ({}, {MODE: "a"}, {MODE: "b"}, {MODE: "c"}):
        assert te.guard_events("rows", ["feedback_a"], TS, env) == []
        assert te.guard_events("deny", ["feedback_a"], TS, env) == [
            {"path": "feedback_a.md", "kind": "use", "ts": TS, "src": "guard_deny"}
        ]


def test_guard_rows_are_not_use_with_no_environment_given(monkeypatch):
    monkeypatch.delenv(MODE, raising=False)
    assert te.guard_events("rows", ["feedback_a"], TS) == []


def test_mode_a_record_guard_writes_no_rows_event(tmp_path):
    env = {
        "NOBLIVION_TRUST_EVENTS": "1",
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path),
        MODE: "a",
    }
    te.record_guard(env, "sid-a", "rows", ["feedback_a"])
    path = te.events_file(str(tmp_path), "sid-a")
    assert path is None or not Path(path).exists() or Path(path).read_text() == ""
    # the paired negative: deny in mode a is still written
    assert te.record_guard(env, "sid-a", "deny", ["feedback_a"])
    assert Path(te.events_file(str(tmp_path), "sid-a")).read_text().count("\n") == 1


@pytest.mark.parametrize("mode", ["", "a", "b", "c"])
def test_the_labels_decision_never_counts_as_use(mode):
    env = {MODE: mode} if mode else {}
    assert te.guard_events("labels", ["feedback_a"], TS, env) == []


# 3. mode b ---------------------------------------------------------------
def test_rate_factor_is_graded_and_smoothed_toward_the_pool():
    pool = 0.1
    assert tr.rate_factor(0, 4, pool) == 1.0  # below 5 shown sessions
    assert math.isclose(tr.rate_factor(1, 10, pool), 1.0)  # at the pool rate
    f = tr.rate_factor(4, 35, pool)  # smoothed (4 + 10*0.1) / (35 + 10)
    assert math.isclose(f, (5 / 45) / pool)
    assert 1.0 < f < tr.rate_factor(5, 40, pool) < 1.25  # graded, not 1.0 or 1.25 only
    assert tr.rate_factor(10, 10, pool) == 1.25  # clamped high
    assert tr.rate_factor(0, 200, pool) == 0.8  # shown often, never used
    for k, n, p in (
        (None, 10, pool),
        (1, None, pool),
        (1, 10, None),
        (1, 10, 0.0),
        (True, 10, pool),
        ("x", 10, pool),
        (11, 10, pool),
    ):
        assert tr.rate_factor(k, n, p) == 1.0


def _snapshot(tmp_path, rows):
    path = tmp_path / "snap.json"
    path.write_text(
        json.dumps(
            {"format": tr.SNAPSHOT_FORMAT, "kind": "replay", "trust_prior": 0.5, "rows": rows}
        )
    )
    return str(path)


def test_snapshot_pool_rate_is_shown_and_used_over_shown(tmp_path):
    snap = tr.load_snapshot(
        _snapshot(
            tmp_path,
            [
                {
                    "stem": "x",
                    "trust": 0.5,
                    "trials": 0,
                    "shown_sessions": 30,
                    "shown_used_sessions": 3,
                },
                {
                    "stem": "y",
                    "trust": 0.5,
                    "trials": 0,
                    "shown_sessions": 10,
                    "shown_used_sessions": 1,
                },
                {"stem": "z", "trust": 0.7, "trials": 9},  # no rate fields: not in the pool
            ],
        )
    )
    assert math.isclose(snap.pool_rate, 4 / 40)
    assert snap.rate("x", None) == (3, 30) and snap.rate("z", None) == (None, None)


def test_mode_b_boosts_by_rate_not_by_raw_use_count(tmp_path):
    path = _snapshot(
        tmp_path,
        [
            # many uses, but shown in 60 sessions and used in 5: under the pool rate -> down
            {
                "stem": "guarded",
                "trust": 0.75,
                "trials": 40,
                "shown_sessions": 60,
                "shown_used_sessions": 5,
            },
            # few uses, but used in 4 of the 6 sessions where it was shown -> boost
            {
                "stem": "useful",
                "trust": 0.6,
                "trials": 4,
                "shown_sessions": 6,
                "shown_used_sessions": 4,
            },
            {
                "stem": "filler",
                "trust": 0.5,
                "trials": 0,
                "shown_sessions": 34,
                "shown_used_sessions": 0,
            },
        ],
    )
    by_name = {"G": Md("guarded.md"), "U": Md("useful.md")}
    rows = [Row(1, 0.0328, "G"), Row(2, 0.0322), Row(3, 0.0318, "U")]
    env = dict(ON, NOBLIVION_RECALL_TRUST_FILE=path)
    out_default, _ = tr.apply_index_trust(rows, env, by_name)
    # default: guarded at 1.25, useful below 5 trials
    assert [r.mid for r in out_default] == [1, 2, 3]
    out_b, note = tr.apply_index_trust(rows, dict(env, **{MODE: "b"}), by_name)
    # pool 9/100; guarded f 0.937, useful 1.25
    assert [r.mid for r in out_b] == [3, 2, 1]
    assert note == ":trust2of3"


def test_mode_b_without_rate_fields_is_inert():
    rows = [Row(1, 0.0328), Row(2, 0.0320, trust=0.9, trials=9, prior=0.5)]
    out, note = tr.apply_index_trust(rows, dict(ON, **{MODE: "b"}), {})
    assert out == rows and note == ":trust0of2"


# 4. mode c ---------------------------------------------------------------
def test_mode_c_applies_trust_only_at_or_above_the_relevance_threshold():
    rows = [
        Row(1, 0.0330, score=0.60),
        Row(2, 0.0326, score=0.57, trust=0.9, trials=9, prior=0.5),
        Row(3, 0.0320, score=0.58, trust=0.9, trials=9, prior=0.5),
        Row(4, 0.0318, score=None, trust=0.9, trials=9, prior=0.5),
    ]
    out, note = tr.apply_index_trust(rows, ON, {})
    assert note == ":trust3of4"  # default: every trusted row
    out, note = tr.apply_index_trust(rows, dict(ON, **{MODE: "c"}), {})
    assert [r.mid for r in out] == [3, 1, 2, 4] and note == ":trust1of4"
    env = dict(ON, **{MODE: "c", "NOBLIVION_RECALL_TRUST_MIN_SCORE": "0.5"})
    assert tr.apply_index_trust(rows, env, {})[1] == ":trust2of4"
    env["NOBLIVION_RECALL_TRUST_MIN_SCORE"] = "junk"  # unreadable -> the default 0.58
    assert tr.apply_index_trust(rows, env, {})[1] == ":trust1of4"
    for raw in ("nan", "inf", "-inf", "1.5", "-2"):  # not a cosine -> the default 0.58
        env["NOBLIVION_RECALL_TRUST_MIN_SCORE"] = raw
        assert tr.apply_index_trust(rows, env, {})[1] == ":trust1of4", raw


def test_the_default_threshold_is_the_pre_registered_value():
    assert tr.TRUST_MIN_SCORE == 0.58
