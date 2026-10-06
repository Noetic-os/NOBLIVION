# SPDX-License-Identifier: AGPL-3.0-or-later
"""``recall.min_score`` on the hit path (design doc 0001, sections 4.2, 12.3).

The store sends one score per entry of the joined ``/search`` string in
``scores``. The hook pairs them with the entries and drops a hit below
``NOBLIVION_RECALL_MIN_SCORE``. A hit with no score (keyword-only mode, or an
answer whose ``scores`` does not line up) always passes.
"""

from __future__ import annotations

import pytest

from hookload import load_hook

hook = load_hook("recall_hook", "hooktest_recall_hook_min_score")

SEP = "\n---\n"


def _entry(name: str) -> str:
    return f"# {name}\n\nthe {name} note\n\n[claude_code_md: {name}.md]\n\nbody of {name}"


def _payload(scores):
    blob = SEP.join(_entry(n) for n in ("alpha", "beta", "gamma"))
    return {"results": [blob], "scores": scores, "namespace": "claude_code"}


def test_parse_hits_pairs_each_entry_with_its_score():
    hits = hook.parse_hits(_payload([0.71, None, 0.12]))
    assert [(h.title, h.score) for h in hits] == [("alpha", 0.71), ("beta", None), ("gamma", 0.12)]


def test_scores_that_do_not_line_up_are_ignored():
    for bad in ([0.9], "0.9", None, [0.9, 0.8, 0.7, 0.6]):
        assert [h.score for h in hook.parse_hits(_payload(bad))] == [None, None, None]


def test_recall_drops_a_hit_below_the_floor(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.71, None, 0.12]))
    env = {"NOBLIVION_RECALL_MIN_SCORE": "0.3", "NOBLIVION_RECALL_ROOT": "r"}
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == ["alpha", "beta"]
    env["NOBLIVION_RECALL_MIN_SCORE"] = "0.05"
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == ["alpha", "beta", "gamma"]


_NO_KEY = object()  # the answer has no "model" key


def _named(model, scores=(0.71, None, 0.12)):
    payload = _payload(list(scores))
    if model is not _NO_KEY:
        payload["model"] = model
    return payload


@pytest.mark.parametrize(
    ("model", "titles"),
    [
        ("BAAI/bge-small-en-v1.5", ["alpha", "beta"]),
        ("example/other-embedder", ["alpha", "beta", "gamma"]),
        (None, ["alpha", "beta", "gamma"]),  # keyword mode names no model
        (_NO_KEY, ["alpha", "beta", "gamma"]),  # the answer of a store of an older version
        ("baai/bge-small-en-v1.5", ["alpha", "beta", "gamma"]),
    ],
)
def test_the_default_floor_is_for_the_measured_model(monkeypatch, model, titles):
    # NOBLIVION-76. DEFAULT_MIN_SCORE was measured on the cosines of
    # THRESHOLD_MODEL. Another model puts its cosines on another scale, so the
    # default applies only to an answer that names THRESHOLD_MODEL.
    assert hook.THRESHOLD_MODEL == "BAAI/bge-small-en-v1.5"
    assert hook.DEFAULT_MIN_SCORE > 0.12
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _named(model))
    env = {"NOBLIVION_RECALL_ROOT": "r"}
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == titles


@pytest.mark.parametrize("model", ["BAAI/bge-small-en-v1.5", "example/other-embedder", None])
def test_a_floor_that_the_user_sets_applies_to_every_model(monkeypatch, model):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _named(model, (0.71, 0.5, 0.12)))
    env = {"NOBLIVION_RECALL_ROOT": "r", "NOBLIVION_RECALL_MIN_SCORE": "0.3"}
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == ["alpha", "beta"]


def test_a_malformed_floor_is_the_default_of_the_model(monkeypatch):
    env = {"NOBLIVION_RECALL_ROOT": "r", "NOBLIVION_RECALL_MIN_SCORE": "high"}
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _named(hook.THRESHOLD_MODEL))
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == ["alpha", "beta"]
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _named("example/other-embedder"))
    assert len(hook.recall("q", 5, env, md_only=True)) == 3


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e9", "-1.5", "", "  "])
def test_a_floor_that_is_not_a_cosine_is_the_default_of_the_model(monkeypatch, bad):
    # ``score >= nan`` is never true: a nan floor would drop every scored hit.
    env = {"NOBLIVION_RECALL_ROOT": "r", "NOBLIVION_RECALL_MIN_SCORE": bad}
    assert hook.min_score(env, hook.THRESHOLD_MODEL) == hook.DEFAULT_MIN_SCORE
    assert hook.min_score(env, "example/other-embedder") is None
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _named(hook.THRESHOLD_MODEL))
    assert [h.title for h in hook.recall("q", 5, env, md_only=True)] == ["alpha", "beta"]


def test_a_floor_at_the_ends_of_the_cosine_range_is_kept():
    for text, value in (("-1", -1.0), ("0", 0.0), ("1", 1.0), (" 0.25 ", 0.25)):
        env = {"NOBLIVION_RECALL_MIN_SCORE": text}
        assert hook.min_score(env, hook.THRESHOLD_MODEL) == value


def test_parse_index_reads_the_store_trust_ranking():
    """Section 8.6: the store's ``trust.ranking`` reaches each index row, so
    ``trust_rank`` can keep the order in ``shadow``."""
    row = {"rank": 1, "id": 4, "title": "t", "summary": "s", "score": 0.5}
    for sent, kept in (("shadow", "shadow"), ("on", "on"), ("bogus", ""), (None, "")):
        payload = {"results": [row], "mode": "hybrid"}
        if sent is not None:
            payload["trust_ranking"] = sent
        (line,) = hook.parse_index(payload)
        assert line.trust_ranking == kept


def test_a_row_in_the_miner_format_starts_its_own_entry():
    """``noblivion.miner`` writes "User correction on <date> in session <id>"."""
    mined = "User correction on 2026-10-01 in session 1a2b3c4d (cue: no). The user wrote: no"
    blob = SEP.join([_entry("alpha"), mined])
    parts = hook.split_entries(blob)
    assert len(parts) == 2 and parts[1] == mined
    (hit,) = [h for h in hook.parse_hits({"results": [blob]}) if not h.md]
    assert hit.text.startswith("User correction on")
