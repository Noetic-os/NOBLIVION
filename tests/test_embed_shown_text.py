# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary chunk embeds the rule and apply text the index row shows (NOBLIVION-114).

Fictional data only. The fake embedder is a bag of words, so a word in the embedded text moves
the cosine of a query that holds it.
"""

from __future__ import annotations

import json

import pytest

from noblivion import config, db, embedding, explain, indexer, ranking
from store_helpers import FakeEmbedder, add_memory
from test_chunks import long_body, make_service

FILE_TEXT = (
    "---\nname: lease_note\ndescription: lease handling\nrule: Rotate the signing key yearly\n"
    'apply: "Use the key ceremony before any release."\n'
    "triggers: [phrase:key rotation]\n---\nBody about shelves and boxes.\n"
)


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def test_the_shown_fields_are_the_first_two_lines_of_the_index_text():
    index_text = indexer.index_fields_text(FILE_TEXT)
    assert embedding.shown_fields(index_text) == (
        "Rotate the signing key yearly",
        "Use the key ceremony before any release.",
    )
    # A missing rule leaves an empty first line, so the apply text is not read as the rule.
    only_apply = '---\nname: x\napply: "Run the check."\n---\nb\n'
    assert embedding.shown_fields(indexer.index_fields_text(only_apply)) == ("", "Run the check.")
    # The triggers are line 3 and are never embedded.
    only_triggers = "---\nname: x\ntriggers: [phrase:key rotation]\n---\nb\n"
    assert embedding.shown_fields(indexer.index_fields_text(only_triggers)) == ("", "")
    assert embedding.shown_fields("") == ("", "")


def test_only_chunk_zero_reads_the_fields_and_a_row_without_them_is_unchanged():
    index_text = indexer.index_fields_text(FILE_TEXT)
    zero = embedding.chunk_embed_text("# T\n\nbody", 0, index_text)
    assert zero == (
        "# T\n\nbody\nrule: Rotate the signing key yearly\n"
        "apply: Use the key ceremony before any release."
    )
    assert embedding.chunk_embed_text("section text", 2, index_text) == "section text"
    assert embedding.chunk_embed_text("  # T\n\nbody ", 0, "") == "# T\n\nbody"


def test_a_long_field_is_cut():
    text = embedding.chunk_embed_text("t", 0, "word " * 500)
    assert len(text) <= len("t\nrule: ") + embedding.SHOWN_FIELD_CHARS


def test_the_backfill_embeds_the_fields_once_and_a_second_pass_does_nothing(conn):
    index_text = indexer.index_fields_text(FILE_TEXT)
    add_memory(conn, "lease_note", "Body about shelves and boxes.", index_text=index_text)
    embedder = FakeEmbedder()
    first = embedding.backfill(conn, embedder)
    assert first.chunks_embedded == 1
    embedded = [t for batch in embedder.calls for t in batch]
    assert any("rule: Rotate the signing key yearly" in t for t in embedded)
    calls = len(embedder.calls)
    assert embedding.backfill(conn, embedder).chunks_embedded == 0
    assert len(embedder.calls) == calls


def test_an_edit_of_the_fields_alone_embeds_the_chunk_again(conn):
    memory_id = add_memory(
        conn, "lease_note", "Body about shelves.", index_text="Rotate the key yearly"
    )
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    with db.write_tx(conn):
        conn.execute(
            "UPDATE memories SET index_text = ?, rev = ? WHERE id = ?",
            ("Rotate the signing key weekly", db.bump_rev(conn, "content_rev"), memory_id),
        )
    done = embedding.backfill(conn, embedder)
    assert done.chunks_embedded == 1


def test_the_old_vector_is_replaced_when_the_chunk_text_gains_the_fields(conn):
    """The re-embed after an upgrade: a vector made from the text alone is stale by hash."""
    memory_id = add_memory(conn, "lease_note", "Body about shelves.", index_text="")
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    old = conn.execute(
        "SELECT v.blob FROM chunk_vectors v JOIN memory_chunks c ON c.id = v.chunk_id "
        "WHERE c.parent_id = ?",
        (memory_id,),
    ).fetchone()[0]
    with db.write_tx(conn):
        conn.execute(
            "UPDATE memories SET index_text = ?, rev = ? WHERE id = ?",
            ("Rotate the signing key yearly", db.bump_rev(conn, "content_rev"), memory_id),
        )
    assert embedding.backfill(conn, embedder).chunks_embedded == 1
    new = conn.execute(
        "SELECT v.blob FROM chunk_vectors v JOIN memory_chunks c ON c.id = v.chunk_id "
        "WHERE c.parent_id = ?",
        (memory_id,),
    ).fetchone()[0]
    assert new != old


def test_a_note_that_matches_only_through_its_rule_gets_a_cosine_in_hybrid_mode(conn):
    """The reproducer of the ticket: the title and the body share no word with the query,
    the rule does."""
    index_text = indexer.index_fields_text(FILE_TEXT)
    with_fields = add_memory(
        conn, "lease_note", "Body about shelves and boxes.", index_text=index_text
    )
    without = add_memory(conn, "twin_note", "Body about shelves and boxes.", index_text="")
    for n in range(4):
        add_memory(conn, f"filler_{n}", f"filler text number {n}")
    service = make_service(FakeEmbedder(), conn)
    service.backfill(conn)
    result = ranking.Ranker(service).search(
        conn, "rotate the signing key", project="claude_code", top_k=10
    )
    assert result.mode == "hybrid"
    scores = {h.row.id: h.score for h in result.hits}
    assert scores[with_fields] is not None and scores[with_fields] > 0.5
    assert scores[with_fields] > (scores.get(without) or 0.0) + 0.3


def test_a_long_note_summary_chunk_reads_the_fields_and_a_section_chunk_does_not(conn):
    memory_id = add_memory(
        conn, "gateway", long_body(), index_text="Rotate the signing key yearly\nUse the ceremony"
    )
    embedder = FakeEmbedder()
    embedding.backfill(conn, embedder)
    texts = [t for batch in embedder.calls for t in batch]
    summary = [t for t in texts if "rule: Rotate the signing key yearly" in t]
    assert len(summary) == 1 and summary[0].startswith("# gateway")
    assert not any("apply: Use the ceremony" in t for t in texts if t.startswith("## "))
    assert (
        conn.execute(
            "SELECT count(*) FROM chunk_vectors v JOIN memory_chunks c ON c.id = v.chunk_id "
            "WHERE c.parent_id = ?",
            (memory_id,),
        ).fetchone()[0]
        == conn.execute(
            "SELECT count(*) FROM memory_chunks WHERE parent_id = ?", (memory_id,)
        ).fetchone()[0]
    )


def test_the_keyword_text_of_a_chunk_does_not_gain_the_fields(conn):
    """The fields reach the vector only. The keyword score reads them once, at its weight."""
    memory_id = add_memory(
        conn, "lease_note", "Body about shelves.", index_text="Rotate the signing key yearly"
    )
    (text,) = conn.execute(
        "SELECT search_text FROM memory_chunks WHERE parent_id = ?", (memory_id,)
    ).fetchone()
    assert "signing" not in text


# -- hubs as candidates for the index (NOBLIVION-114) --------------------------------------


def _write_hub_stats(folder, sessions, shown):
    import datetime as dt

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cache = folder / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "hub-stats.json").write_text(
        json.dumps({"v": 1, "days": {today: {"s": sessions, "n": shown}}})
    )
    return cache


def test_explain_lists_hub_notes_as_candidates_and_writes_nothing(tmp_path, capsys):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 20, {"note_a": 15, "note_b": 11, "note_c": 10, "note_d": 2})
    env = {"NOBLIVION_DATA_DIR": str(folder), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    result = explain.hub_notes(folder=folder, env=env)
    assert [c["note"] for c in result["candidates"]] == ["note_a", "note_b"]  # note_c: exactly half
    assert result["candidates"][0] == {"note": "note_a", "sessions": 15, "share": 0.75}
    assert result["sessions"] == 20 and "never edits" in result["action"]
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_explain_hubs_reports_too_few_sessions_and_a_window_that_is_off(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 3, {"note_a": 3})
    env = {"NOBLIVION_DATA_DIR": str(folder), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
    few = explain.hub_notes(folder=folder, env=env)
    assert few["candidates"] == [] and "Fewer than" in few["reason"]
    off = explain.hub_notes(folder=folder, env=dict(env, NOBLIVION_RECALL_HUB_DAYS="0"))
    assert off["candidates"] == [] and "off" in off["reason"]


def test_the_hubs_option_needs_no_prompt_and_a_missing_prompt_is_still_refused(
    tmp_path, capsys, monkeypatch
):
    folder = tmp_path / "data"
    folder.mkdir()
    cache = _write_hub_stats(folder, 20, {"note_a": 15})
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(folder))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(cache))
    assert explain.main(["--hubs", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["note"] for c in doc["candidates"]] == ["note_a"]
    assert explain.main(["--hubs"]) == 0
    assert "note_a: shown in 15 sessions (0.75)" in capsys.readouterr().out
    assert explain.main([]) == 2
    assert "prompt is missing" in capsys.readouterr().out
    assert config.DB_FILE not in {p.name for p in folder.iterdir()}
