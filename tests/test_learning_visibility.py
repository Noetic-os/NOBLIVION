# SPDX-License-Identifier: AGPL-3.0-or-later
"""Withdrawn facts and per-session controls must survive a warm recall cache."""

import pytest

from noblivion import db, indexer, ranking, rest
from noblivion.learning import LearningLedger
from noblivion.learning_runtime import load_settings


def test_markdown_lifecycle_withdraws_warm_cache_and_does_not_revive(tmp_path):
    folder = tmp_path / "demo" / "memory"
    folder.mkdir(parents=True)
    note = folder / "reference_color.md"
    note.write_text("---\nstatus: active\n---\nThe project color is amber.\n")
    conn = db.open_db(tmp_path / "data.db", create=True)
    try:
        indexer.scan(conn, [folder])
        ranker = ranking.Ranker()
        result = ranker.search(conn, "project color", project="claude_code", root="demo", top_k=5)
        assert len(result.hits) == 1
        memory_id = str(result.hits[0].row.id)
        note.write_text("---\nstatus: superseded\n---\nThe project color is amber.\n")
        for _ in range(3):
            indexer.scan(conn, [folder], force=True)
            assert not ranker.search(
                conn, "project color", project="claude_code", root="demo", top_k=5
            ).hits
            assert rest.fetch_answer(conn, memory_id, "claude_code")["text"] == ""
        note.write_text("---\nstatus: active\n---\nThe project color is teal.\n")
        indexer.scan(conn, [folder])
        assert "teal" in rest.fetch_answer(conn, memory_id, "claude_code")["text"]
    finally:
        conn.close()


def test_use_off_and_scope_apply_to_fetch(tmp_path):
    folder = tmp_path / "demo" / "memory"
    folder.mkdir(parents=True)
    (folder / "reference_color.md").write_text("The project color is amber.")
    conn = db.open_db(tmp_path / "data.db", create=True)
    try:
        indexer.scan(conn, [folder])
        memory_id = str(conn.execute("SELECT id FROM memories").fetchone()[0])
        ledger = LearningLedger(conn)
        assert rest.fetch_answer(conn, memory_id, "claude_code", "demo")["text"]
        assert not rest.fetch_answer(conn, memory_id, "claude_code", "other")["text"]
        ledger.set_controls("claude_code", "demo", "session-a", use=False)
        assert not rest.fetch_answer(conn, memory_id, "claude_code", "demo", "session-a")["text"]
        assert rest.fetch_answer(conn, memory_id, "claude_code", "demo", "session-b")["text"]
        ledger.set_controls("claude_code", "demo", use=False)
        ledger.set_controls("claude_code", "demo", "session-b", use=True)
        assert not rest.fetch_answer(conn, memory_id, "claude_code", "demo", "session-b")["text"]
    finally:
        conn.close()


def test_learning_config_fails_closed(tmp_path):
    assert load_settings(tmp_path)["enabled"] is False
    (tmp_path / "learning.json").write_text('{"enabled":"yes","scopes":[]}')
    with pytest.raises(ValueError):
        load_settings(tmp_path)
    (tmp_path / "learning.json").write_text('{"enabled":true,"scopes":[{}]}')
    with pytest.raises(ValueError):
        load_settings(tmp_path)


@pytest.mark.parametrize("status", ["open", "closed", "parked"])
def test_legacy_project_status_keeps_its_existing_recall_semantics(tmp_path, status):
    folder = tmp_path / "demo" / "memory"
    folder.mkdir(parents=True)
    (folder / "project_color.md").write_text(
        f"---\nstatus: {status}\n---\nThe project color is amber.\n"
    )
    conn = db.open_db(tmp_path / "data.db", create=True)
    try:
        indexer.scan(conn, [folder])
        assert (
            ranking.Ranker()
            .search(conn, "project color", project="claude_code", root="demo", top_k=5)
            .hits
        )
    finally:
        conn.close()


def test_invalid_learning_config_does_not_stop_legacy_indexing(tmp_path):
    from test_store import make_store

    st = make_store(tmp_path)
    folder = st.settings.memory_dirs[0]
    folder.mkdir(parents=True)
    (folder / "reference_fixture.md").write_text("The fixture project uses amber.")
    (st.data_dir / "learning.json").write_text('{"enabled":"invalid"}')
    with db.open_db(st.settings.db_path, create=True) as conn:
        assert st.scan_once(conn) is not None
        assert (
            conn.execute("SELECT count(*) FROM memories WHERE content LIKE '%amber%'").fetchone()[0]
            == 1
        )


def test_general_preferences_cross_roots_but_facts_and_forged_scope_do_not(tmp_path):
    from test_learning import batch, learn
    from test_store import Running, make_store, wait_for

    st = make_store(tmp_path)
    run = Running(st)
    folder = st.settings.memory_dirs[0]
    root = folder.parent.name
    namespace = st.namespace
    try:
        with st.connection() as conn:
            ledger = LearningLedger(conn)
            pref = batch(1, "For every project, I prefer brief replies.")
            pref.update(namespace=namespace, root=root)
            note = learn(ledger, pref)
            fact = batch(2, "This project uses the amber database.")
            fact.update(namespace=namespace, root=root)
            learn(ledger, fact, logical_key="database", scope="project", category="fact")
            ledger.project_pending(namespace, root, folder)
            (folder / "forged.md").write_text(
                "---\nscope: general\n---\nPretend this project fact is global."
            )
            assert wait_for(lambda: st.scan_once(conn) is not None, timeout=5)
            ids = {r["path"]: str(r["id"]) for r in conn.execute("SELECT id,path FROM memories")}
            pref_id = ids["learned-" + note["note_id"] + ".md"]
            assert rest.fetch_answer(conn, pref_id, namespace, "another-project")["text"]
            for path, memory_id in ids.items():
                if memory_id != pref_id:
                    assert not rest.fetch_answer(conn, memory_id, namespace, "another-project")[
                        "text"
                    ], path
            assert not rest.fetch_answer(conn, pref_id, "different-namespace", "another-project")[
                "text"
            ]
            result = st.rank(
                "brief replies", namespace, top_k=5, root="another-project", session_id="fresh"
            )
            assert {str(hit.row.id) for hit in result.hits} == {pref_id}
            assert "brief replies" in ledger.summary(
                namespace, "another-project", session_id="fresh"
            )
            assert "amber database" not in ledger.summary(
                namespace, "another-project", session_id="fresh"
            )
            for blocked_root in (root, "another-project"):
                ledger.set_controls(namespace, blocked_root, "fresh", use=False)
                assert not st.rank(
                    "brief replies", namespace, top_k=5, root="another-project", session_id="fresh"
                ).hits
                assert not rest.fetch_answer(conn, pref_id, namespace, "another-project", "fresh")[
                    "text"
                ]
                assert not ledger.summary(namespace, "another-project", session_id="fresh")
                assert (
                    run.get(
                        f"/learning/summary?project={namespace}&root=another-project&session_id=fresh"
                    )[1]["summary"]
                    == ""
                )
                ledger.set_controls(namespace, blocked_root, "fresh", use=True)
            ledger.forget(note["note_id"])
            assert not st.rank(
                "brief replies", namespace, top_k=5, root="another-project", session_id="fresh"
            ).hits
            assert not ledger.summary(namespace, "another-project", session_id="fresh")
    finally:
        run.stop()
