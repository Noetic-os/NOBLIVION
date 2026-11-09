# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure relevance selection preserves admission, order and stage diagnostics."""

import pytest

from hookload import load_hook

hook = load_hook("recall_hook", "hooktest_recall_selection")


def row(mid, score, lexical=0.0):
    result = hook.IndexLine(mid, mid, f"note{mid}", "summary", score)
    result.lexical = lexical
    return result


def select(lines, candidates=None, floor=0.68, shared_ids=None, margin=0.0, window=0.0):
    return hook.select_relevant_index(
        lines,
        lines if candidates is None else candidates,
        floor=floor,
        shared_ids=set() if shared_ids is None else shared_ids,
        margin=margin,
        window=window,
    )


def test_stage_snapshots_preserve_input_rows_ranks_and_shared_ids(monkeypatch):
    candidates = [row(1, 0.67, 1.0), row(2, 0.71), row(3, 0.73), row(4, None), row(5, 0.9)]
    lines = [candidates[i] for i in (2, 1, 0, 3, 4)]
    shared = {1, 2}
    original_lines = list(lines)
    original_candidates = list(candidates)
    original_fields = [
        tuple(getattr(item, field) for field in item.__slots__) for item in candidates
    ]

    def no_external_state(*args, **kwargs):
        pytest.fail("selection must not load settings, corpus, session state or logs")

    for name in (
        "_sibling_module",
        "index_min_score",
        "shared_root_margin",
        "score_window",
        "load_session_corpus",
        "load_shown_set",
        "save_shown_set",
        "log_line",
    ):
        monkeypatch.setattr(hook, name, no_external_state)
    result = select(lines, candidates, shared_ids=shared, margin=0.04, window=0.2)
    assert result.floor_lines == [candidates[i] for i in (2, 0, 3, 4)]
    assert result.lines == [candidates[i] for i in (2, 3, 4)]
    assert (result.rescued, result.unscored, result.shared_cut) == (1, 1, 1)
    assert [item.rank for item in result.lines] == [3, 4, 5]
    assert lines == original_lines and candidates == original_candidates and shared == {1, 2}
    assert original_fields == [
        tuple(getattr(item, field) for field in item.__slots__) for item in candidates
    ]
    result.lines.clear()
    assert result.floor_lines == [candidates[i] for i in (2, 0, 3, 4)]
    assert lines == original_lines and candidates == original_candidates


@pytest.mark.parametrize(
    "score,lexical,kept,rescued",
    [
        (0.66, 1.0, True, 1),
        (0.66 - 5e-10, 1.0, True, 1),
        (0.66 - 2e-9, 1.0, False, 0),
        (0.67, 0.0, False, 0),
        (0.68, 1.0, True, 0),
        (None, 1.0, True, 0),
    ],
)
def test_rescue_boundary(score, lexical, kept, rescued):
    candidate = row(1, score, lexical)
    result = select([candidate])
    assert result.lines == ([candidate] if kept else [])
    assert result.rescued == rescued
    assert result.unscored == int(score is None)


def test_rescue_never_restores_an_earlier_drop_or_moves_to_the_next_candidate():
    candidates = [row(1, 0.67, 1.0), row(2, 0.67, 1.0)]
    result = select([candidates[1]], candidates)
    assert result.lines == []
    assert result.rescued == 0


@pytest.mark.parametrize("score,kept", [(0.72, True), (0.72 - 5e-10, True), (0.72 - 2e-9, False)])
def test_shared_margin_boundary_and_store_top_exemption(score, kept):
    candidates = [row(1, 0.69), row(2, score)]
    result = select(list(reversed(candidates)), candidates, shared_ids={1, 2}, margin=0.04)
    assert result.lines == ([candidates[1], candidates[0]] if kept else [candidates[0]])
    assert result.shared_cut == int(not kept)


@pytest.mark.parametrize("score,kept", [(0.75, True), (0.75 - 5e-10, True), (0.75 - 2e-9, False)])
def test_window_boundary_keeps_unscored_rows(score, kept):
    candidates = [row(1, 0.8), row(2, score), row(3, None)]
    result = select(candidates, window=0.05)
    assert result.floor_lines == candidates
    assert result.lines == (candidates if kept else [candidates[0], candidates[2]])


def test_disabled_window_and_empty_input_still_return_independent_snapshots():
    candidates = [row(1, None), row(2, 0.7)]
    result = select(candidates)
    assert result.lines == result.floor_lines == candidates
    assert result.lines is not result.floor_lines and result.floor_lines is not candidates
    empty = select([])
    assert empty.lines == empty.floor_lines == []
    assert (empty.rescued, empty.unscored, empty.shared_cut) == (0, 0, 0)
