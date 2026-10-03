# SPDX-License-Identifier: AGPL-3.0-or-later
"""``recall.min_score`` on the hit path (design doc 0001, sections 4.2, 12.3).

The store sends one score per entry of the joined ``/search`` string in
``scores``. The hook pairs them with the entries and drops a hit below
``NOBLIVION_RECALL_MIN_SCORE``. A hit with no score (keyword-only mode, or an
answer whose ``scores`` does not line up) always passes.
"""

from __future__ import annotations

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
