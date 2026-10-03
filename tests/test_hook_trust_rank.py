# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trust factor on the recall hook's index ranking.

What these tests hold:

1. THE FACTOR. ``f = clamp(trust / trust_prior, 0.8, 1.25)``; ``f = 1.0`` below
   5 trials, and for a missing, null, boolean or non-number field, or a prior of
   0 or less. The bounds are read when the factor is computed.
2. IDENTITY. With every factor 1.0 the order is the input order exactly, ties
   included: the step can be on without moving anything until trust exists.
3. WHERE IT RUNS. Only with NOBLIVION_RECALL_INDEX_TRUST; only on a list the
   local re-rank fused (``:trust_off:no_rerank`` otherwise).
4. THE SNAPSHOT (NOBLIVION_RECALL_TRUST_FILE) replaces the daemon fields, is
   looked up by memory file stem before id (a bench copy with other ids still
   matches), and a snapshot that cannot be read turns the step off rather than
   falling back to live trust.
5. SHADOW. The order served is unchanged; the log note says
   ``:trust_shadowNofK`` and the debug line holds both orders and the trust of
   every moved row.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook

tr = load_hook("trust_rank", "trust_rank_t_trust_rank")

SID = "trust-g-session"
TRUST_ON = {"NOBLIVION_RECALL_INDEX_TRUST": "1"}


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


# 1. the factor ----------------------------------------------------------
@pytest.mark.parametrize(
    "trust,trials,prior,expected",
    [
        (0.55, 5, 0.5, 1.1),  # inside the bounds
        (0.9, 20, 0.5, 1.25),  # clamped high
        (0.1, 20, 0.5, 0.8),  # clamped low
        (0.9, 4, 0.5, 1.0),  # below 5 trials
        (0.5, 50, 0.5, 1.0),  # at the prior
        (None, 9, 0.5, 1.0),
        (0.9, None, 0.5, 1.0),
        (0.9, 9, None, 1.0),
        (0.9, 9, 0.0, 1.0),
        (0.9, 9, -1.0, 1.0),
        (0.9, True, 0.5, 1.0),
        (float("nan"), 9, 0.5, 1.0),
        ("x", 9, 0.5, 1.0),
    ],
)
def test_factor_bounds_and_the_five_trial_rule(trust, trials, prior, expected):
    assert math.isclose(tr.factor(trust, trials, prior), expected)


def test_factor_reads_the_bounds_when_called(monkeypatch):
    monkeypatch.setattr(tr, "F_MAX", 1.5)
    assert math.isclose(tr.factor(0.9, 20, 0.5), 1.5)
    monkeypatch.setattr(tr, "N_MIN_TRIALS", 30)
    assert tr.factor(0.9, 20, 0.5) == 1.0


# 2. identity ------------------------------------------------------------
class Row:
    def __init__(
        self,
        mid: int,
        fused: float | None,
        title: str = "",
        trust=None,
        trials=None,
        prior=None,
    ):
        self.mid, self.fused, self.title = mid, fused, title
        self.trust, self.trials, self.trust_prior = trust, trials, prior


def test_every_factor_one_gives_the_input_order_exactly():
    rows = [Row(5, 0.03), Row(2, 0.025), Row(9, 0.025), Row(1, 0.02)]  # a tie: id 2 before 9
    assert tr.reorder(rows, [1.0] * 4) == rows


def test_a_boost_moves_a_near_neighbour_but_not_a_far_one():
    rows = [Row(1, 2 / 61), Row(2, 2 / 62), Row(3, 2 / 63), Row(4, 2 / 100)]
    out = tr.reorder(rows, [1.0, 1.0, 1.25, 1.25])
    assert [r.mid for r in out] == [3, 1, 2, 4]


# 3. where it runs -------------------------------------------------------
def test_off_unless_the_variable_is_set():
    rows = [Row(1, 0.02, trust=0.9, trials=9, prior=0.5), Row(2, 0.03)]
    for env in (
        {},
        {"NOBLIVION_RECALL_INDEX_TRUST": "0"},
        {"NOBLIVION_RECALL_INDEX_TRUST": "off"},
    ):
        out, note = tr.apply_index_trust(rows, env, {})
        assert out == rows and note == ""


def test_no_fused_score_means_no_step():
    rows = [Row(1, None, trust=0.9, trials=9, prior=0.5), Row(2, None)]
    out, note = tr.apply_index_trust(rows, TRUST_ON, {})
    assert out == rows and note == ":trust_off:no_rerank"


def test_daemon_fields_reorder_and_the_note_counts_the_moved_rows(tmp_path):
    rows = [Row(1, 0.0328), Row(2, 0.0320, trust=0.9, trials=9, prior=0.5), Row(3, 0.03)]
    out, note = tr.apply_index_trust(rows, TRUST_ON, {}, str(tmp_path), SID, "UserPromptSubmit")
    assert [r.mid for r in out] == [2, 1, 3] and note == ":trust1of3"
    line = json.loads((tmp_path / "trust-rank.jsonl").read_text())
    assert line["rows"] == [[2, 0.9, 9, 1.25, 2, 1]] and line["mode"] == "on"
    assert line["source"] == "store" and line["session"] == SID


# 4. the snapshot --------------------------------------------------------
class MD:
    def __init__(self, rel_path: str):
        self.rel_path = rel_path


def _snapshot(tmp_path: Path, rows: list[dict[str, Any]], prior: float = 0.5) -> str:
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"format": tr.SNAPSHOT_FORMAT, "trust_prior": prior, "rows": rows}))
    return str(path)


def test_snapshot_beats_the_daemon_fields_and_matches_by_stem_first(tmp_path):
    # The daemon says row 1 is trusted; the pinned snapshot says row 3 (by its
    # stem, under a different id, as in a bench copy) and row 2 (by id).
    rows = [
        Row(1, 0.0328, title="name one", trust=0.9, trials=9, prior=0.5),
        Row(2, 0.0322, title="name two"),
        Row(3, 0.0320, title="name three"),
    ]
    by_name = {"name one": MD("feedback_one.md"), "name three": MD("sub/feedback_three.md")}
    snap = _snapshot(
        tmp_path,
        [
            {"stem": "feedback_three", "mid": 99999, "trust": 0.625, "trials": 5},
            {"stem": None, "mid": 2, "trust": 0.6, "trials": 6},
        ],
    )
    env = dict(TRUST_ON, NOBLIVION_RECALL_TRUST_FILE=snap)
    out, note = tr.apply_index_trust(rows, env, by_name)
    assert [r.mid for r in out] == [3, 2, 1] and note == ":trust2of3"


def test_stem_wins_over_an_id_that_names_another_memory(tmp_path):
    # A bench copy: row id 2 is "name one" here, but id 2 is another memory in
    # the pool the snapshot came from. The stem decides.
    rows = [Row(1, 0.0328, title="other"), Row(2, 0.0320, title="name one")]
    snap = _snapshot(
        tmp_path,
        [
            {"stem": "feedback_one", "mid": 50, "trust": 0.625, "trials": 5},
            {"stem": "feedback_elsewhere", "mid": 2, "trust": 0.1, "trials": 9},
        ],
    )
    env = dict(TRUST_ON, NOBLIVION_RECALL_TRUST_FILE=snap)
    out, _ = tr.apply_index_trust(rows, env, {"name one": MD("feedback_one.md")})
    assert [r.mid for r in out] == [2, 1]


@pytest.mark.parametrize("content", ["not json", json.dumps({"rows": "x"}), json.dumps([1, 2])])
def test_a_snapshot_that_cannot_be_read_turns_the_step_off(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    rows = [Row(1, 0.03), Row(2, 0.02, trust=0.9, trials=9, prior=0.5)]
    for p in (str(path), str(tmp_path / "missing.json")):
        env = dict(TRUST_ON, NOBLIVION_RECALL_TRUST_FILE=p)
        out, note = tr.apply_index_trust(rows, env, {})
        assert out == rows and note == ":trust_off:bad_file"


# 5. shadow --------------------------------------------------------------
def test_shadow_serves_the_old_order_and_logs_the_would_be_order(tmp_path):
    rows = [Row(1, 0.0328), Row(2, 0.0320, trust=0.9, trials=9, prior=0.5)]
    env = {"NOBLIVION_RECALL_INDEX_TRUST": "shadow"}
    out, note = tr.apply_index_trust(rows, env, {}, str(tmp_path))
    assert out == rows and note == ":trust_shadow1of2"
    line = json.loads((tmp_path / "trust-rank.jsonl").read_text())
    assert line["base"] == [1, 2] and line["trust"] == [2, 1] and line["mode"] == "shadow"


@pytest.mark.parametrize(("store_mode", "applied"), [("shadow", False), ("on", True), ("", True)])
def test_the_store_shadow_mode_is_computed_and_logged_but_never_applied(
    tmp_path, store_mode, applied
):
    """trust.ranking shadow (design doc section 8.6): the hook switch is on,
    but the store says shadow, so the order stays and the log has the factor."""
    rows = [Row(1, 0.0328), Row(2, 0.0320, trust=0.9, trials=9, prior=0.5)]
    for r in rows:
        r.trust_ranking = store_mode
    out, note = tr.apply_index_trust(rows, TRUST_ON, {}, str(tmp_path))
    line = json.loads((tmp_path / "trust-rank.jsonl").read_text())
    assert line["trust"] == [2, 1]
    if applied:
        assert [r.mid for r in out] == [2, 1] and note == ":trust1of2" and line["mode"] == "on"
    else:
        assert out == rows and note == ":trust_shadow1of2" and line["mode"] == "shadow"


def test_debug_file_rolls_over_once(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "DEBUG_MAX_BYTES", 10)
    rows = [Row(1, 0.03), Row(2, 0.02)]
    for _ in range(3):
        tr.apply_index_trust(rows, TRUST_ON, {}, str(tmp_path))
    assert (tmp_path / "trust-rank.jsonl.1").exists()
    assert len((tmp_path / "trust-rank.jsonl").read_text().splitlines()) == 1


def test_reorder_refuses_factors_of_another_length():
    """The rows and the factors pair one to one, also on python 3.9 (no ``zip`` strict)."""
    rows = [Row(1, 0.03), Row(2, 0.02)]
    with pytest.raises(ValueError):
        tr.reorder(rows, [1.0])
    with pytest.raises(ValueError):
        tr.reorder(rows, [1.0, 1.0, 1.0])
