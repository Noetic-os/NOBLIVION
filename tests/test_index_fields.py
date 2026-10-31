# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-104: the rule, apply and triggers fields and the labels column count in the
keyword score, in the store and in the hook re-rank. Fictional data only."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import recall_helpers
from hookload import load_hook
from noblivion import db, embedding, indexer, ranking, redaction, store
from recall_helpers import hook_env
from store_helpers import add_memory, change_memory
from test_store import Running, fake_service, make_store, wait_for

isolated_home = recall_helpers.isolated_home  # a fixture

ROOT = "-work-proj-demo"


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def keyword_service() -> embedding.EmbeddingService:
    """A store with no model: it ranks by keyword."""
    return embedding.EmbeddingService(embedding.EmbeddingSettings(backend="none"))


def search(conn, query: str, *, top_k: int = 10, target: str = ""):
    ranker = ranking.Ranker(None)
    return ranker.search(conn, query, project="claude_code", top_k=top_k, target=target)


def ids(result) -> list[int]:
    return [h.row.id for h in result.hits]


# -- the text of the fields -------------------------------------------------------------

FLOW = (
    "---\nname: demo\ndescription: d\ntype: feedback\n"
    "rule: Never push a tag before the changelog is merged\n"
    'apply: "Run the check first.\\nThen tag."\n'
    "triggers: [git tag, glob:release/**/*.md, tool:Bash, phrase:cut a release]\n---\nbody\n"
)
BLOCK = (
    "---\nname: demo\nrule: Keep the lease short\n"
    "triggers:\n  - phrase:lease renewal\n  - 'kubectl rollout'\n  - tool:Edit\n"
    "scope: tool\n---\nbody\n"
)
NESTED = (
    "---\nname: demo\nmetadata:\n  type: feedback\n  rule: Rotate the signing key yearly\n"
    '  apply: "Use the key ceremony."\n  triggers: [phrase:key rotation]\n---\nbody\n'
)


def test_fields_text_of_a_flow_list():
    text = indexer.index_fields_text(FLOW)
    # One line per field; a line break inside apply is folded to a space (NOBLIVION-114).
    assert text.splitlines() == [
        "Never push a tag before the changelog is merged",
        "Run the check first. Then tag.",
        "git tag release/**/*.md Bash cut a release",
    ]


def test_fields_text_of_a_block_list_and_of_nested_fields():
    assert indexer.index_fields_text(BLOCK) == (
        "Keep the lease short\n\nlease renewal kubectl rollout Edit"
    )
    assert indexer.index_fields_text(NESTED) == (
        "Rotate the signing key yearly\nUse the key ceremony.\nkey rotation"
    )


def test_a_missing_field_leaves_an_empty_line_so_the_line_is_the_field():
    only_apply = '---\nname: demo\napply: "Use the key ceremony."\n---\nbody\n'
    assert indexer.index_fields_text(only_apply) == "\nUse the key ceremony."
    only_rule = "---\nname: demo\nrule: Keep it short\n---\nbody\n"
    assert indexer.index_fields_text(only_rule) == "Keep it short"


def test_fields_text_is_empty_without_the_fields_and_without_frontmatter():
    assert indexer.index_fields_text("---\nname: demo\n---\nrule: not a field\n") == ""
    assert indexer.index_fields_text("rule: not frontmatter\n") == ""
    assert indexer.index_fields_text("---\nrule: never closed\n") == ""


def test_fields_text_is_cut_at_the_cap():
    text = f"---\nrule: {'word ' * 2000}\n---\n"
    assert len(indexer.index_fields_text(text)) == indexer.INDEX_FIELDS_MAX_CHARS


@pytest.mark.parametrize("text", [FLOW, BLOCK, NESTED, "---\nname: x\n---\nb\n"])
def test_the_hook_reads_the_same_fields_as_the_store(text):
    corpus = load_hook("corpus", "hooktest_corpus_fields")
    meta, _ = corpus.parse_frontmatter(text)
    assert corpus.index_fields_text(meta, text) == indexer.index_fields_text(text)
    assert corpus.INDEX_FIELDS_REPEAT == ranking.INDEX_FIELDS_REPEAT
    assert corpus.INDEX_FIELDS_MAX_CHARS == indexer.INDEX_FIELDS_MAX_CHARS


def test_the_hook_rerank_tokenizes_the_fields(tmp_path):
    corpus = load_hook("corpus", "hooktest_corpus_tokens")
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "feedback_demo.md").write_text(FLOW, encoding="utf-8")
    (folder / "feedback_plain.md").write_text("---\nname: plain\n---\nbody\n", encoding="utf-8")
    by_name = corpus.corpus_by_name(corpus.load_memory_corpus(str(folder)))
    tokens = by_name["demo"].tokens
    assert tokens.count("changelog") == corpus.INDEX_FIELDS_REPEAT  # in the rule only
    assert tokens.count("ceremony") == 0 and "tag" in tokens
    assert "cut" in tokens and "release/**/*.md" not in tokens
    assert "changelog" not in by_name["plain"].tokens


# -- the store ----------------------------------------------------------------------------


def test_a_word_only_in_the_index_text_finds_the_row(conn):
    plain = add_memory(conn, "plain", "Check the logs of the nightly job.")
    ruled = add_memory(
        conn,
        "ruled",
        "Check the logs of the nightly job.",
        index_text="Never restart the zeppelin gateway during the backup",
    )
    assert ids(search(conn, "zeppelin gateway")) == [ruled]
    assert plain not in ids(search(conn, "zeppelin"))
    # Both rows say the same in the body, and the fields do not change what is shown.
    shown = conn.execute("SELECT content FROM memories WHERE id = ?", (ruled,)).fetchone()[0]
    assert "zeppelin" not in shown


def test_the_index_text_counts_in_every_chunk(conn):
    sections = "\n\n".join(
        f"## Part {n}\n{f'filler words about topic {n}. ' * 40}" for n in range(6)
    )
    mid = add_memory(conn, "long", sections, index_text="quokka")
    chunks = conn.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE parent_id = ?", (mid,)
    ).fetchone()[0]
    assert chunks > 3
    ranker = ranking.Ranker(None)
    ranker.index.refresh(conn)
    units, _, _ = ranker.index._pool(
        ranker.index._snap, ranker.index.pool_key("claude_code", None, (), False)
    )
    assert all(u.tokens.count("quokka") == ranking.INDEX_FIELDS_REPEAT for u in units)
    assert ids(search(conn, "quokka")) == [mid]


def test_the_index_text_outweighs_one_use_in_a_body(conn):
    body_use = add_memory(conn, "body_use", "Remember the quokka when you plan the route.")
    in_rule = add_memory(
        conn, "in_rule", "Remember the route when you plan.", index_text="quokka route"
    )
    add_memory(conn, "filler", "Something else entirely about coffee beans.")
    assert ids(search(conn, "quokka"))[:2] == [in_rule, body_use]


def test_a_change_of_the_index_text_reaches_the_ranker(conn):
    mid = add_memory(conn, "moving", "A plain note.")
    ranker = ranking.Ranker(None)
    assert ids(ranker.search(conn, "wombat", project="claude_code", top_k=5)) == []
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "content_rev")
        conn.execute("UPDATE memories SET index_text = 'wombat', rev = ? WHERE id = ?", (rev, mid))
    assert ids(ranker.search(conn, "wombat", project="claude_code", top_k=5)) == [mid]
    change_memory(conn, mid, "A plain note, changed.")  # a content change keeps the fields
    assert ids(ranker.search(conn, "wombat", project="claude_code", top_k=5)) == [mid]


# -- the labels column ------------------------------------------------------------------------


def close_pair(conn, *, labels: str, gap_words: int = 0):
    """Two rows: A holds the query word once more than B, so A ranks first by BM25 alone.
    B has the label."""
    filler = " ".join(f"pad{n}" for n in range(gap_words))
    a = add_memory(conn, "row_a", f"deploy deploy gateway {filler}")
    b = add_memory(conn, "row_b", f"deploy gateway {filler}", labels=labels)
    for n in range(8):
        add_memory(conn, f"fill_{n}", f"unrelated text {n} about coffee and tea")
    return a, b


def test_a_label_match_reorders_rows_that_score_close(conn):
    a, b = close_pair(conn, labels='["gateway-ctl"]')
    assert ids(search(conn, "deploy gateway"))[:2] == [a, b]
    assert ids(search(conn, "deploy gateway gateway-ctl"))[:2] == [b, a]


def test_the_label_boost_is_bounded(conn):
    """A row with the label does not pass a row that holds one more query word and scores
    more than ``LABEL_BOOST_MAX_LABELS * LABEL_BOOST`` higher."""
    strong = add_memory(conn, "strong", "alpha beta gamma delta epsilon zeta")
    weak = add_memory(conn, "weak", "alpha", labels='["alpha.py", "beta.py", "gamma.py"]')
    for n in range(8):
        add_memory(conn, f"fill_{n}", f"unrelated text {n} about coffee and tea")
    query = "alpha beta gamma delta epsilon zeta alpha.py beta.py gamma.py"
    result = search(conn, query)
    assert ids(result)[:2] == [strong, weak]
    assert result.hits[0].bm25 - result.hits[1].bm25 > (
        ranking.LABEL_BOOST * ranking.LABEL_BOOST_MAX_LABELS
    )


def test_label_boost_counts_at_most_the_bound():
    labels = ("a.py", "b.py", "c.py", "d.py")
    assert ranking.label_boost(labels, ["a.py", "b.py", "c.py", "d.py"]) == pytest.approx(
        ranking.LABEL_BOOST * ranking.LABEL_BOOST_MAX_LABELS
    )
    assert ranking.label_boost(labels, ["a.py"]) == pytest.approx(ranking.LABEL_BOOST)
    assert ranking.label_boost(labels, ["x.py"]) == 0.0
    assert ranking.label_boost((), ["a.py"]) == 0.0


def test_a_label_matches_a_path_that_ends_in_it(conn):
    a, b = close_pair(conn, labels='["ranking.py"]')
    assert ids(search(conn, "deploy gateway in src/pkg/ranking.py"))[:2] == [b, a]
    assert ids(search(conn, "deploy gateway in src/pkg/notranking.py"))[:2] == [a, b]


def test_the_tool_target_counts_for_the_label_only(conn):
    a, b = close_pair(conn, labels='["ranking.py"]')
    assert ids(search(conn, "deploy gateway", target="src/pkg/ranking.py"))[:2] == [b, a]
    # A target word is not a keyword: it finds no row by itself.
    assert ids(search(conn, "zzz", target="src/pkg/ranking.py")) == []
    assert ids(search(conn, "zzz", target="ranking.py")) == []


def test_a_label_does_not_make_a_row_a_keyword_match(conn):
    add_memory(conn, "labelled", "text about nothing", labels='["psql"]')
    other = add_memory(conn, "other", "run psql with the read only role")
    assert ids(search(conn, "psql")) == [other]


def test_labels_read_as_lower_case_and_bad_json_reads_as_none():
    assert ranking.parse_labels('["PROJ-12", "Run.SH", "", 3]') == ("proj-12", "run.sh")
    assert ranking.parse_labels("not json") == ()
    assert ranking.parse_labels('{"a": 1}') == ()


def test_the_rest_index_route_passes_the_target(tmp_path):
    run = Running(make_store(tmp_path, embedding_service=keyword_service()))
    try:
        assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
        with run.store.connection() as conn:
            first = add_memory(conn, "first", "deploy deploy gateway", root=ROOT)
            second = add_memory(
                conn, "second", "deploy gateway", root=ROOT, labels='["ranking.py"]'
            )
            for n in range(8):
                add_memory(conn, f"fill_{n}", f"unrelated text {n} about coffee", root=ROOT)

        def order(path: str) -> list[int]:
            status, body = run.get(path)
            assert status == 200
            return [row["id"] for row in body["results"]]

        assert order("/api/memories/index?q=deploy+gateway&root=" + ROOT) == [first, second]
        target = "&target=src%2Fpkg%2Franking.py"
        assert order("/api/memories/index?q=deploy+gateway&root=" + ROOT + target) == [
            second,
            first,
        ]
    finally:
        run.stop()


# -- the indexer ---------------------------------------------------------------------------------

RULED = (
    "---\nname: gateway safety\ndescription: Keep the gateway up.\ntype: feedback\n"
    "rule: Never restart the zeppelin gateway during the backup window\n"
    'apply: "Check the backup schedule, then restart."\n'
    "triggers: [phrase:restart the gateway]\n---\nSee the runbook for the window.\n"
)


def write_note(tmp_path: Path, name: str, text: str) -> Path:
    folder = tmp_path / "projects" / ROOT / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_the_scan_stores_the_fields_and_keeps_the_content(conn, tmp_path):
    folder = write_note(tmp_path, "feedback_gateway.md", RULED)
    indexer.scan(conn, [folder])
    content, index_text = conn.execute("SELECT content, index_text FROM memories").fetchone()
    assert "zeppelin" not in content and "zeppelin" in index_text
    assert content == indexer.build_content(
        "feedback_gateway.md", indexer.parse_file("feedback_gateway.md", RULED)
    )
    assert ids(search(conn, "zeppelin")) != []


def test_the_scan_redacts_the_fields(conn, tmp_path):
    secret = "AKIAIOSFODNN7EXAMPLE"
    text = RULED.replace("zeppelin", f"zeppelin {secret}")
    folder = write_note(tmp_path, "feedback_gateway.md", text)
    indexer.scan(conn, [folder])
    index_text = conn.execute("SELECT index_text FROM memories").fetchone()[0]
    assert redaction.redact_at_rest(secret) != secret  # the redactor knows this form
    assert secret not in index_text and "zeppelin" in index_text


def test_a_row_from_before_schema_6_gets_its_fields_on_the_next_scan(conn, tmp_path):
    folder = write_note(tmp_path, "feedback_gateway.md", RULED)
    indexer.scan(conn, [folder])
    # The state after the migration: a row without fields, and no mark of the fields version.
    with db.write_tx(conn):
        conn.execute("UPDATE memories SET index_text = ''")
        conn.execute("DELETE FROM meta WHERE key LIKE 'index_fields_version:%'")
    again = indexer.scan(conn, [folder])
    assert again.updated == 1
    assert "zeppelin" in conn.execute("SELECT index_text FROM memories").fetchone()[0]
    # The mark is set, so the scan after it leaves the row alone.
    assert indexer.scan(conn, [folder]).unchanged == 1


def test_an_edit_of_the_rule_alone_is_picked_up(conn, tmp_path):
    folder = write_note(tmp_path, "feedback_gateway.md", RULED)
    indexer.scan(conn, [folder])
    write_note(tmp_path, "feedback_gateway.md", RULED.replace("zeppelin", "narwhal"))
    assert indexer.scan(conn, [folder]).updated == 1
    assert ids(search(conn, "narwhal")) != [] and ids(search(conn, "zeppelin")) == []


# -- the prompt index end to end ------------------------------------------------------------


def _run_prompt(rh, env: dict, prompt: str) -> str:
    out = io.StringIO()
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s-fields-1", "prompt": prompt}
    rh.run(json.dumps(event), out, env)
    return out.getvalue()


def _shipped_env(tmp_path: Path, folder: Path, **extra: str) -> dict:
    shipped = Path(__file__).resolve().parent.parent / "config" / "config.default.json"
    return hook_env(
        tmp_path, NOBLIVION_CONFIG=str(shipped), NOBLIVION_RECALL_MEMORY_DIR=str(folder), **extra
    )


def _folder_with_notes(tmp_path: Path) -> Path:
    folder = write_note(tmp_path, "feedback_gateway.md", RULED)
    write_note(
        tmp_path,
        "feedback_other.md",
        "---\nname: other note\ndescription: Something about coffee.\ntype: feedback\n"
        "rule: Brew the coffee slowly\n---\nCoffee body.\n",
    )
    write_note(
        tmp_path,
        "feedback_third.md",
        "---\nname: third note\ndescription: Something about tea.\ntype: feedback\n"
        "rule: Steep the tea for three minutes\n---\nTea body.\n",
    )
    return folder


def test_the_prompt_index_finds_a_note_by_a_word_only_in_its_rule(tmp_path):
    """The shipped config file, the default floor and no model: the store ranks by keyword."""
    folder = _folder_with_notes(tmp_path)
    run = Running(make_store(tmp_path, embedding_service=keyword_service()))
    try:
        assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
        rh = load_hook("recall_hook", "hooktest_fields_keyword")
        env = _shipped_env(tmp_path, folder)
        out = _run_prompt(rh, env, "is it fine to touch the zeppelin now")
        assert "gateway safety" in out and "Never restart the zeppelin gateway" in out
        assert "other note" not in out
    finally:
        run.stop()


def test_the_prompt_index_finds_a_note_by_a_word_only_in_its_rule_with_a_model(tmp_path):
    """A fake model, so the hook re-rank runs too. The floor is off: the cosines of the fake
    model are not on the scale of the real one."""
    folder = _folder_with_notes(tmp_path)
    run = Running(make_store(tmp_path, embedding_service=fake_service()))
    try:
        assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
        assert wait_for(lambda: run.store.embedding.state == embedding.STATE_READY)
        rh = load_hook("recall_hook", "hooktest_fields_hybrid")
        env = _shipped_env(tmp_path, folder, **recall_helpers.INDEX_FLOOR_OFF)
        out = _run_prompt(rh, env, "is it fine to touch the zeppelin now")
        assert "Never restart the zeppelin gateway" in out
        log = (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log").read_text()
        assert ":joined" in log
    finally:
        run.stop()
