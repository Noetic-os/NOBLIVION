# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema, migrations, connection rules and delete cascades (design doc 0001, section 6)."""

from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from noblivion import db

TABLES = {
    "meta",
    "memories",
    "vectors",
    "feedback_events",
    "feedback",
    "dedup_actions",
    "dedup_vetoes",
    "miner_state",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
    return {r[0] for r in rows} - {"sqlite_sequence"}


def _add_memory(conn: sqlite3.Connection, path: str = "user_alice.md") -> int:
    with db.write_tx(conn):
        rev = db.bump_rev(conn)
        return db.insert_memory(
            conn,
            project="claude_code",
            root="proj-demo",
            path=path,
            source_type=db.SOURCE_MD,
            category="user",
            content="# alice",
            content_hash="0" * 64,
            labels="[]",
            rev=rev,
            now=db.utc_now(),
        )


def test_create_schema_version_1(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    assert db.user_version(conn) == 1
    assert _tables(conn) == TABLES
    assert db.get_meta(conn, "content_rev") == "0"
    assert db.get_meta(conn, "vector_rev") == "0"
    assert len(db.get_meta(conn, "db_id") or "") == 32
    db.parse_ts(db.get_meta(conn, "created_at") or "")


def test_connection_pragmas(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0


def test_database_file_mode_0600(tmp_path):
    path = tmp_path / "data" / "noblivion.db"
    conn = db.open_db(path, create=True)
    _add_memory(conn)
    for p in (path, path.with_name("noblivion.db-wal")):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "noblivion.db"
    conn = db.open_db(path, create=True)
    db_id = db.get_meta(conn, "db_id")
    _add_memory(conn)
    conn.close()
    for _ in range(3):
        conn = db.open_db(path, create=True)
        assert db.user_version(conn) == 1
        assert db.get_meta(conn, "db_id") == db_id
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        conn.close()
    assert not (tmp_path / "backups").exists()


def test_cli_mode_creates_a_new_database(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", allow_migrate=False, create=True)
    assert db.user_version(conn) == 1


def test_cli_mode_refuses_an_old_schema(tmp_path):
    path = tmp_path / "noblivion.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE legacy (x INTEGER)")
    raw.commit()
    raw.close()
    with pytest.raises(db.SchemaTooOldError):
        db.open_db(path, allow_migrate=False, create=True)


def test_store_mode_migrates_an_old_schema_with_backup(tmp_path):
    path = tmp_path / "noblivion.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE legacy (x INTEGER)")
    raw.commit()
    raw.close()
    conn = db.open_db(path, create=True)
    assert db.user_version(conn) == 1
    backups = list((tmp_path / "backups").glob("noblivion-v0-*.db"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_refuses_a_newer_schema(tmp_path):
    path = tmp_path / "noblivion.db"
    db.open_db(path, create=True).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(db.SchemaTooNewError, match="newer than this NOBLIVION"):
        db.open_db(path, create=True)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "noblivion.db"
    good = db.load_migrations()
    bad = [db.Migration(1, good[0].name, good[0].sql + "\nTHIS IS NOT SQL;")]
    monkeypatch.setattr(db, "load_migrations", lambda: bad)
    with pytest.raises(sqlite3.OperationalError):
        db.open_db(path, create=True)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    assert raw.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] == 0


def test_prune_backups_keeps_three(tmp_path):
    for i in range(5):
        p = tmp_path / f"noblivion-v1-2026010{i}T000000000000Z.db"
        p.write_bytes(b"")
        ts = 1_700_000_000 + i
        import os

        os.utime(p, (ts, ts))
    db.prune_backups(tmp_path)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [f"noblivion-v1-2026010{i}T000000000000Z.db" for i in (2, 3, 4)]


def test_ids_start_at_a_random_base(tmp_path):
    conn = db.connect(tmp_path / "a.db", create=True)
    db.migrate(conn, random_base=5_000_000)
    assert _add_memory(conn) == 5_000_001
    conn2 = db.open_db(tmp_path / "b.db", create=True)
    assert 1_000_000 < _add_memory(conn2) <= 1_000_000_001


def test_strict_tables_and_checks(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memories (project, root, path, source_type, content, hash, rev, "
            "created_at, updated_at) VALUES ('p', 'r', 'x.md', 'other', 'c', 'h', 1, 't', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memories (project, root, path, source_type, content, hash, rev, "
            "created_at, updated_at, labels) VALUES "
            "('p', 'r', 'x.md', 'claude_code_md', 'c', 'h', 1, 't', 't', 'not json')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO meta (key, value) VALUES ('k', X'00')")  # STRICT: no blob


def test_bump_rev_counts_up(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    with db.write_tx(conn):
        assert db.bump_rev(conn) == 1
        assert db.bump_rev(conn) == 2
        assert db.bump_rev(conn, "vector_rev") == 1
    assert db.revisions(conn) == (2, 1)
    with pytest.raises(ValueError):
        db.bump_rev(conn, "db_id")


def test_write_tx_rolls_back_on_error(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    with pytest.raises(RuntimeError), db.write_tx(conn):
        db.bump_rev(conn)
        raise RuntimeError("boom")
    assert db.revisions(conn) == (0, 0)


def _attach_children(conn: sqlite3.Connection, memory_id: int) -> None:
    now = db.utc_now()
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO vectors (memory_id, model, dim, content_hash, blob, rev) "
            "VALUES (?, 'demo-model', 2, 'h', ?, 1)",
            (memory_id, b"\x00" * 8),
        )
        conn.execute(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s-1', ?, 'recall', 1, ?, ?)",
            (memory_id, now, now),
        )
        conn.execute(
            "INSERT INTO feedback (memory_id, trust_0, trust_score) VALUES (?, 0.5, 0.5)",
            (memory_id,),
        )


def _child_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("vectors", "feedback_events", "feedback")
    )


def test_delete_cascades_to_vectors_events_and_rollup(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    memory_id = _add_memory(conn)
    _attach_children(conn, memory_id)
    assert _child_counts(conn) == (1, 1, 1)
    with db.write_tx(conn):
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    assert _child_counts(conn) == (0, 0, 0)
    assert db.foreign_key_problems(conn) == []


def test_soft_delete_keeps_children_until_purge(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    keep = _add_memory(conn, "user_keep.md")
    gone = _add_memory(conn, "user_gone.md")
    _attach_children(conn, gone)
    old = db.format_ts(datetime.now(timezone.utc) - timedelta(days=20))
    with db.write_tx(conn):
        assert db.soft_delete(conn, gone, rev=db.bump_rev(conn), now=old)
        assert not db.soft_delete(conn, gone, rev=db.bump_rev(conn), now=old)
    assert _child_counts(conn) == (1, 1, 1)
    assert db.purge_deleted(conn, grace_days=30) == 0
    assert db.purge_deleted(conn, grace_days=14) == 1
    assert _child_counts(conn) == (0, 0, 0)
    ids = [r[0] for r in conn.execute("SELECT id FROM memories")]
    assert ids == [keep]
    assert db.foreign_key_problems(conn) == []


def test_purge_runs_in_batches(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    old = db.format_ts(datetime.now(timezone.utc) - timedelta(days=30))
    with db.write_tx(conn):
        rev = db.bump_rev(conn)
        for i in range(db.BATCH_ROWS + 5):
            mid = db.insert_memory(
                conn,
                project="claude_code",
                root="proj-demo",
                path=f"user_{i}.md",
                source_type=db.SOURCE_MD,
                category="user",
                content="x",
                content_hash=str(i),
                labels="[]",
                rev=rev,
                now=old,
            )
            db.soft_delete(conn, mid, rev=rev, now=old)
    assert db.purge_deleted(conn, grace_days=14) == db.BATCH_ROWS + 5
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_purge_archived_needs_a_done_dedup_action(tmp_path):
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    a = _add_memory(conn, "user_a.md")
    b = _add_memory(conn, "user_b.md")
    old = db.format_ts(datetime.now(timezone.utc) - timedelta(days=100))
    with db.write_tx(conn):
        conn.execute("UPDATE memories SET archived_at = ? WHERE id IN (?, ?)", (old, a, b))
        conn.execute(
            "INSERT INTO dedup_actions (run_id, root, kept_id, archived_id, kept_path, "
            "archived_path, archive_to, status, judge_model, verdict, created_at) VALUES "
            "('run-1', 'proj-demo', 1, ?, 'k.md', 'user_a.md', '.archive/user_a.md', 'done', "
            "'demo-judge', '{}', ?)",
            (a, old),
        )
    assert db.purge_archived(conn, retention_days=90) == 1
    ids = [r[0] for r in conn.execute("SELECT id FROM memories")]
    assert ids == [b]
    # The undo record has no foreign key and survives the purge.
    assert conn.execute("SELECT count(*) FROM dedup_actions").fetchone()[0] == 1


def test_split_statements_ignores_semicolons_in_comments():
    script = "CREATE TABLE t (\n  a TEXT -- one; two\n);\n-- only a comment;\nSELECT 1;\n"
    statements = db.split_statements(script)
    assert len(statements) == 2
    assert statements[0] == "CREATE TABLE t (\n  a TEXT -- one; two\n);"
    assert statements[1].endswith("SELECT 1;")
    with pytest.raises(ValueError):
        db.split_statements("SELECT 1")


def test_read_then_write_transactions_do_not_lose_updates(tmp_path):
    """Two threads read a counter and write it back. BEGIN IMMEDIATE serialises them."""
    import threading

    path = tmp_path / "noblivion.db"
    db.open_db(path, create=True).close()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        conn = db.connect(path, create=True)
        try:
            barrier.wait()
            for _ in range(200):
                with db.write_tx(conn):
                    value = int(db.get_meta(conn, "demo_counter", "0") or 0)
                    db.set_meta(conn, "demo_counter", str(value + 1))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    conn = db.connect(path, create=True)
    assert db.get_meta(conn, "demo_counter") == "400"
