# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 20 supersede pairs of the scale fixture (C21, NOBLIVION-156).

The pairs are written apart from the scales. Here they run through the real code: the
recall hook's older-row step for the 15 pairs with no link, and the indexer for the 5 pairs
whose newer note says ``supersedes:``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import db, indexer

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "eval" / "scale_fixture.py"
PAIRS = ROOT / "tools" / "eval" / "scale_fixture_pairs.json"

_spec = importlib.util.spec_from_file_location("scale_fixture_pairs_t", TOOL)
assert _spec is not None and _spec.loader is not None
sf = importlib.util.module_from_spec(_spec)
sys.modules["scale_fixture_pairs_t"] = sf
_spec.loader.exec_module(sf)

hook = load_hook("recall_hook", "pairs_recall_hook")
corpus = load_hook("corpus", "pairs_corpus")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("pairs")
    entry = sf.write_pairs(out, PAIRS.read_bytes())
    pairs = json.loads((out / "supersede" / "pairs.json").read_text(encoding="utf-8"))
    memory = out / "supersede" / "projects" / sf.PAIR_PROJECT / "memory"
    return out, entry, pairs, memory


def test_twenty_pairs_with_fifteen_marks_and_five_links(made):
    _, entry, pairs, memory = made
    assert entry["pairs"] == len(pairs) == 20
    assert sum(p["explicit"] for p in pairs) == 5
    assert len(list(memory.glob("*.md"))) == 40
    assert len({p["query"] for p in pairs}) == 20


def test_the_newer_note_ranks_above_the_older_one_for_the_fifteen_unlinked_pairs(made):
    _, _, pairs, memory = made
    files = {md.rel_path: md for md in corpus.load_memory_corpus(str(memory))}
    by_name = corpus.corpus_by_name(list(files.values()))
    before, after = [], []
    for pair in (p for p in pairs if not p["explicit"]):
        # The store ranks the older note first in the worst case: its text was there first.
        rows = [
            hook.IndexLine(1, 1, files[pair["old_note"]].name, "", 0.90, rule="r"),
            hook.IndexLine(2, 2, files[pair["new_note"]].name, "", 0.89, rule="r"),
        ]
        before.append([r.title for r in rows])
        got, marked = hook.mark_older_rows(rows, 5, by_name)
        after.append([r.title for r in got])
        assert marked == 1
        assert [r.title for r in got] == [pair["new_note"][:-3], pair["old_note"][:-3]]
        assert [r.older for r in got] == [False, True]
    assert len(before) == len(after) == 15
    assert all(b[0] != a[0] for b, a in zip(before, after, strict=True))


def test_the_older_note_of_a_linked_pair_is_withdrawn_by_the_indexer(made, tmp_path):
    _, _, pairs, memory = made
    conn = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    try:
        indexer.scan(conn, [memory])
        live = {
            r["path"] for r in conn.execute("SELECT path FROM memories WHERE archived_at IS NULL")
        }
    finally:
        conn.close()
    for pair in pairs:
        assert pair["new_note"] in live
        assert (pair["old_note"] in live) is (not pair["explicit"])


def test_the_files_verify_and_the_queries_hold_no_value(made):
    out, entry, _, _ = made
    problems: list[str] = []
    sf._verify_pairs(out, {"supersede": entry}, problems)
    assert problems == []
