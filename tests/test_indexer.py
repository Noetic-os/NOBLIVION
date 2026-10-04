# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexer (design doc 0001, section 5). Fictional memory folders only."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from noblivion import db, indexer, redaction


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def make_folder(base: Path, root: str, files: dict[str, str]) -> Path:
    folder = base / "projects" / root / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def rows(conn) -> dict[tuple[str, str], dict]:
    cur = conn.execute(
        "SELECT id, root, path, category, content, hash, labels, archived_at, deleted_at, rev "
        "FROM memories"
    )
    return {(r["root"], r["path"]): dict(r) for r in cur.fetchall()}


def live(conn) -> set[tuple[str, str]]:
    return {k for k, r in rows(conn).items() if r["deleted_at"] is None and not r["archived_at"]}


FEEDBACK = """---
name: Run tests before a push
description: "Always run the unit tests: a push without them breaks CI"
type: feedback
rule: run pytest
---

Run `pytest -q` before every push.
"""


# -- frontmatter and content -------------------------------------------------


def test_frontmatter_nested_and_quotes():
    text = "---\nname: 'demo'\nmetadata:\n  type: project\n  owner: alice\n---\n\nbody\n"
    meta, body = indexer.parse_frontmatter(text)
    assert meta == {"name": "demo", "metadata": {"type": "project", "owner": "alice"}}
    assert body == "body\n"
    assert indexer.frontmatter_type(meta) == "project"


def test_frontmatter_without_closing_line_is_all_body():
    text = "---\nname: demo\nno closing line\n"
    assert indexer.parse_frontmatter(text) == ({}, text)


def test_frontmatter_value_with_colon():
    meta, _ = indexer.parse_frontmatter("---\ndescription: a: b: c\n---\n")
    assert meta["description"] == "a: b: c"


@pytest.mark.parametrize(
    ("name", "meta", "expected"),
    [
        ("MEMORY.md", {"type": "user"}, "index"),
        ("MEMORY_ARCHIVE.md", {}, "index"),
        ("feedback_tests.md", {"type": "user"}, "feedback"),
        ("topic_git.md", {}, "topic"),
        ("notes.md", {"type": "Project"}, "project"),
        ("notes.md", {"metadata": {"type": "user"}}, "user"),
        ("notes.md", {"type": "other"}, "reference"),
        ("misc_notes.md", {}, "reference"),
    ],
)
def test_category(name, meta, expected):
    assert indexer.category_for(name, meta) == expected


def test_content_format_byte_for_byte():
    parsed = indexer.parse_file("feedback_tests.md", FEEDBACK)
    content = indexer.build_content("feedback_tests.md", parsed)
    assert content == (
        "# Run tests before a push\n\n"
        "Always run the unit tests: a push without them breaks CI\n\n"
        "[claude_code_md: feedback_tests.md]\n\n"
        "Run `pytest -q` before every push."
    )


def test_content_without_description_or_body():
    parsed = indexer.parse_file("user_alice.md", "---\ntype: user\n---\n")
    assert indexer.build_content("user_alice.md", parsed) == (
        "# user_alice\n\n[claude_code_md: user_alice.md]"
    )


def test_content_without_frontmatter():
    parsed = indexer.parse_file("notes.md", "plain text\n")
    assert indexer.build_content("notes.md", parsed) == (
        "# notes\n\n[claude_code_md: notes.md]\n\nplain text"
    )


# -- scan basics -------------------------------------------------------------


def test_scan_inserts_files_and_skips_the_rest(conn, tmp_path):
    folder = make_folder(
        tmp_path,
        "proj-demo",
        {
            "feedback_tests.md": FEEDBACK,
            "MEMORY.md": "- [tests](feedback_tests.md)\n",
            "empty.md": "  \n\n",
            "notes.txt": "not markdown",
            ".hidden.md": "hidden",
            "MEMORY.md.pre-compact-1": "backup",
        },
    )
    (folder / ".archive").mkdir()
    (folder / ".archive" / "old.md").write_text("archived", encoding="utf-8")
    result = indexer.scan(conn, [folder])
    assert result.inserted == 2
    assert live(conn) == {("proj-demo", "MEMORY.md"), ("proj-demo", "feedback_tests.md")}
    row = rows(conn)[("proj-demo", "feedback_tests.md")]
    assert row["category"] == "feedback"
    raw = (folder / "feedback_tests.md").read_bytes()
    import hashlib

    assert row["hash"] == hashlib.sha256(raw).hexdigest()
    assert rows(conn)[("proj-demo", "MEMORY.md")]["category"] == "index"
    assert db.get_meta(conn, "redactor_version") == redaction.REDACTOR_VERSION


def test_hash_skip_and_update_in_place(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "likes tea\n"})
    first = indexer.scan(conn, [folder])
    before = rows(conn)[("proj-demo", "user_alice.md")]
    second = indexer.scan(conn, [folder])
    assert (second.unchanged, second.changed) == (1, 0)
    assert db.revisions(conn)[0] == first.content_rev  # a no-op scan bumps nothing
    (folder / "user_alice.md").write_text("likes coffee\n", encoding="utf-8")
    third = indexer.scan(conn, [folder])
    after = rows(conn)[("proj-demo", "user_alice.md")]
    assert third.updated == 1
    assert after["id"] == before["id"]
    assert "likes coffee" in after["content"]
    assert after["rev"] == third.content_rev > before["rev"]


def test_force_rewrites_unchanged_rows(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "x\n", "user_bob.md": "y\n"})
    indexer.scan(conn, [folder])
    assert indexer.scan(conn, [folder], force=True).updated == 2


def test_new_redactor_version_reindexes(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "x\n"})
    indexer.scan(conn, [folder])
    with db.write_tx(conn):
        db.set_meta(conn, "redactor_version", "0-old")
    assert indexer.scan(conn, [folder]).updated == 1
    assert db.get_meta(conn, "redactor_version") == redaction.REDACTOR_VERSION
    assert indexer.scan(conn, [folder]).updated == 0


def test_rows_written_by_redactor_version_1_are_redone(conn, tmp_path, monkeypatch):
    # Version 1 kept a JSON-quoted password. The file does not change, so only
    # the new redactor version makes the scan write the row again.
    folder = make_folder(
        tmp_path, "proj-demo", {"reference_db.md": '{"password": "hunter2hunter2"}\n'}
    )
    with monkeypatch.context() as old:
        old.setattr(redaction, "REDACTOR_VERSION", "1")
        old.setattr(redaction, "redact_at_rest", lambda text: text)
        indexer.scan(conn, [folder])
    key = ("proj-demo", "reference_db.md")
    assert "hunter2hunter2" in rows(conn)[key]["content"]
    assert db.get_meta(conn, "redactor_version") == "1"
    assert indexer.scan(conn, [folder]).updated == 1
    assert "hunter2hunter2" not in rows(conn)[key]["content"]
    assert db.get_meta(conn, "redactor_version") == redaction.REDACTOR_VERSION
    assert indexer.scan(conn, [folder]).updated == 0


def test_secrets_are_redacted_before_storing(conn, tmp_path):
    secret = "sk" + "-" + "Q3x9" * 6
    folder = make_folder(
        tmp_path, "proj-demo", {"reference_api.md": f"key {secret}\nmail alice@example.com\n"}
    )
    indexer.scan(conn, [folder])
    content = rows(conn)[("proj-demo", "reference_api.md")]["content"]
    assert secret not in content
    assert "alice@example.com" not in content
    assert redaction.REDACTION_TOKEN in content


def test_batches_bump_one_rev_each(conn, tmp_path):
    files = {f"user_{i:03}.md": f"note {i}\n" for i in range(db.BATCH_ROWS + 10)}
    folder = make_folder(tmp_path, "proj-demo", files)
    result = indexer.scan(conn, [folder])
    assert result.inserted == db.BATCH_ROWS + 10
    assert result.content_rev == 2
    revs = {r["rev"] for r in rows(conn).values()}
    assert revs == {1, 2}


# -- gone files, revive, rename, move ----------------------------------------


def _ten_files() -> dict[str, str]:
    return {f"user_{i}.md": f"note {i}\n" for i in range(10)}


def test_gone_file_is_soft_deleted_then_revived(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    old_id = rows(conn)[("proj-demo", "user_3.md")]["id"]
    (folder / "user_3.md").unlink()
    result = indexer.scan(conn, [folder])
    assert result.deleted == 1
    row = rows(conn)[("proj-demo", "user_3.md")]
    assert row["deleted_at"] is not None
    assert row["rev"] == result.content_rev
    (folder / "user_3.md").write_text("note 3 again\n", encoding="utf-8")
    result = indexer.scan(conn, [folder])
    assert result.revived == 1
    row = rows(conn)[("proj-demo", "user_3.md")]
    assert (row["id"], row["deleted_at"]) == (old_id, None)


def test_rename_keeps_the_id(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    old_id = rows(conn)[("proj-demo", "user_3.md")]["id"]
    os.rename(folder / "user_3.md", folder / "user_three.md")
    result = indexer.scan(conn, [folder])
    assert (result.moved, result.inserted, result.deleted) == (1, 0, 0)
    assert rows(conn)[("proj-demo", "user_three.md")]["id"] == old_id
    assert ("proj-demo", "user_3.md") not in rows(conn)


def test_rename_after_soft_delete_keeps_the_id(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    old_id = rows(conn)[("proj-demo", "user_3.md")]["id"]
    data = (folder / "user_3.md").read_bytes()
    (folder / "user_3.md").unlink()
    indexer.scan(conn, [folder])
    (folder / "user_renamed.md").write_bytes(data)
    assert indexer.scan(conn, [folder]).moved == 1
    assert rows(conn)[("proj-demo", "user_renamed.md")]["id"] == old_id


def test_folder_move_to_a_new_root_keeps_the_id(conn, tmp_path):
    old = make_folder(tmp_path, "proj-old", _ten_files())
    indexer.scan(conn, [old])
    ids = {k[1]: r["id"] for k, r in rows(conn).items()}
    new = tmp_path / "projects" / "proj-new" / "memory"
    new.parent.mkdir(parents=True)
    os.rename(old, new)
    result = indexer.scan(conn, [old, new])
    assert (result.moved, result.inserted, result.deleted) == (10, 0, 0)
    assert {k[0] for k in live(conn)} == {"proj-new"}
    assert {k[1]: r["id"] for k, r in rows(conn).items()} == ids


def test_archived_row_comes_back_with_its_file(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    with db.write_tx(conn):
        conn.execute(
            "UPDATE memories SET archived_at = ? WHERE path = 'user_4.md'", (db.utc_now(),)
        )
    data = (folder / "user_4.md").read_bytes()
    (folder / "user_4.md").unlink()
    assert indexer.scan(conn, [folder]).deleted == 0  # archived rows are not live
    (folder / "user_4.md").write_bytes(data)
    assert indexer.scan(conn, [folder]).unarchived == 1
    assert ("proj-demo", "user_4.md") in live(conn)


def test_purge_after_indexer_delete_leaves_no_orphans(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    memory_id = rows(conn)[("proj-demo", "user_1.md")]["id"]
    now = db.utc_now()
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s-1', ?, 'use', 1, ?, ?)",
            (memory_id, now, now),
        )
        conn.execute(
            "INSERT INTO feedback (memory_id, trust_0, trust_score) VALUES (?, 0.5, 0.5)",
            (memory_id,),
        )
    (folder / "user_1.md").unlink()
    indexer.scan(conn, [folder])
    assert db.purge_deleted(conn, grace_days=0) == 1
    for table in ("feedback", "feedback_events"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert db.foreign_key_problems(conn) == []


# -- guards ------------------------------------------------------------------


def test_shrink_guard_blocks_a_large_delete(conn, tmp_path):
    files = {f"user_{i:02}.md": f"note {i}\n" for i in range(20)}
    folder = make_folder(tmp_path, "proj-demo", files)
    indexer.scan(conn, [folder])
    for i in range(3):  # 3 of 20 is 15 %, over the 10 % limit
        (folder / f"user_{i:02}.md").unlink()
    (folder / "user_new.md").write_text("new note\n", encoding="utf-8")
    result = indexer.scan(conn, [folder])
    assert result.blocked and result.deleted == 0 and result.deletes_cancelled == 3
    assert result.inserted == 1  # inserts and updates still run
    assert "--allow-shrink" in result.block_reason
    assert len(live(conn)) == 21
    result = indexer.scan(conn, [folder], allow_shrink=True)
    assert (result.blocked, result.deleted) == (False, 3)


def test_shrink_guard_allows_a_small_delete(conn, tmp_path):
    files = {f"user_{i:02}.md": f"note {i}\n" for i in range(20)}
    folder = make_folder(tmp_path, "proj-demo", files)
    indexer.scan(conn, [folder])
    for i in range(2):  # 2 of 20 is exactly 10 %: allowed
        (folder / f"user_{i:02}.md").unlink()
    result = indexer.scan(conn, [folder])
    assert (result.blocked, result.deleted) == (False, 2)


def test_shrink_guard_small_root_allows_one_delete(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-small", {f"user_{i}.md": f"n {i}\n" for i in range(5)})
    big = make_folder(tmp_path, "proj-big", {f"user_{i:02}.md": f"b {i}\n" for i in range(50)})
    indexer.scan(conn, [folder, big])
    (folder / "user_0.md").unlink()
    assert indexer.scan(conn, [folder, big]).deleted == 1
    (folder / "user_1.md").unlink()
    (folder / "user_2.md").unlink()
    result = indexer.scan(conn, [folder, big])
    assert result.blocked and result.deleted == 0
    assert "proj-small" in result.block_reason


def test_shrink_guard_per_root_even_when_the_total_is_small(conn, tmp_path):
    a = make_folder(tmp_path, "proj-a", {f"user_{i:02}.md": f"a {i}\n" for i in range(20)})
    b = make_folder(tmp_path, "proj-b", {f"user_{i:02}.md": f"b {i}\n" for i in range(200)})
    indexer.scan(conn, [a, b])
    for i in range(5):  # 5 of 20 in proj-a, but only 5 of 220 overall
        (a / f"user_{i:02}.md").unlink()
    result = indexer.scan(conn, [a, b])
    assert result.blocked and "proj-a" in result.block_reason


def test_shrink_limit_rules():
    assert not indexer.shrink_limit_exceeded(5, 0)
    assert not indexer.shrink_limit_exceeded(1, 3)
    assert indexer.shrink_limit_exceeded(2, 9)
    assert not indexer.shrink_limit_exceeded(1, 10)
    assert indexer.shrink_limit_exceeded(2, 10)
    assert not indexer.shrink_limit_exceeded(10, 100)
    assert indexer.shrink_limit_exceeded(11, 100)


def test_missing_folder_deletes_nothing(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    for p in folder.iterdir():
        p.unlink()
    folder.rmdir()
    result = indexer.scan(conn, [folder])
    assert (result.deleted, result.blocked) == (0, False)
    assert len(live(conn)) == 10


def test_empty_folder_deletes_nothing(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    for p in folder.iterdir():
        p.unlink()
    result = indexer.scan(conn, [folder], allow_shrink=True)
    assert result.deleted == 0
    assert len(live(conn)) == 10


def test_redaction_failure_skips_the_file_and_all_deletes(conn, tmp_path, monkeypatch):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder])
    (folder / "user_0.md").unlink()
    (folder / "user_bad.md").write_text("POISON\n", encoding="utf-8")
    real = redaction.redact_at_rest

    def fake(text: str) -> str:
        return redaction.REDACTION_FAILED_TOKEN if "POISON" in text else real(text)

    monkeypatch.setattr(redaction, "redact_at_rest", fake)
    result = indexer.scan(conn, [folder])
    assert result.skipped == ["proj-demo/user_bad.md"]
    assert (result.inserted, result.deleted, result.deletes_cancelled) == (0, 0, 1)
    assert ("proj-demo", "user_bad.md") not in rows(conn)
    assert ("proj-demo", "user_0.md") in live(conn)
    monkeypatch.setattr(redaction, "redact_at_rest", real)
    result = indexer.scan(conn, [folder])
    assert (result.inserted, result.deleted, result.skipped) == (1, 1, [])


def test_failed_redaction_of_a_changed_file_keeps_the_old_row(conn, tmp_path, monkeypatch):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "old text\n"})
    indexer.scan(conn, [folder])
    (folder / "user_alice.md").write_text("POISON\n", encoding="utf-8")
    monkeypatch.setattr(redaction, "redact_at_rest", lambda t: redaction.REDACTION_FAILED_TOKEN)
    indexer.scan(conn, [folder])
    assert "old text" in rows(conn)[("proj-demo", "user_alice.md")]["content"]


# -- roots, labels, stat cache ---------------------------------------------


def test_same_file_name_in_two_roots(conn, tmp_path):
    a = make_folder(tmp_path, "proj-a", {"user_alice.md": "in a\n"})
    b = make_folder(tmp_path, "proj-b", {"user_alice.md": "in b\n"})
    indexer.scan(conn, [a, b])
    assert live(conn) == {("proj-a", "user_alice.md"), ("proj-b", "user_alice.md")}


def test_duplicate_root_is_scanned_once(conn, tmp_path, capsys):
    a = make_folder(tmp_path, "proj-a", {"user_alice.md": "a\n"})
    other = tmp_path / "elsewhere" / "proj-a" / "memory"
    other.mkdir(parents=True)
    (other / "user_bob.md").write_text("b\n", encoding="utf-8")
    indexer.scan(conn, [a, other])
    assert live(conn) == {("proj-a", "user_alice.md")}
    assert "used twice" in capsys.readouterr().err


def test_labels_drop_secret_shaped_and_skip_index_files(conn, tmp_path):
    secret = "gh" + "p_" + "Z9y8X7w6" * 4
    folder = make_folder(
        tmp_path,
        "proj-demo",
        {"feedback_tests.md": FEEDBACK, "MEMORY.md": "index\n", "topic_git.md": "git\n"},
    )

    def labeller(_text: str, _name: str) -> list[str]:
        return ["pytest", secret, "ci", "pytest"]

    indexer.scan(conn, [folder], labeller=labeller)
    got = {k[1]: json.loads(r["labels"]) for k, r in rows(conn).items()}
    assert got == {"feedback_tests.md": ["ci", "pytest"], "MEMORY.md": [], "topic_git.md": []}


def test_labeller_error_gives_no_labels(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "x\n"})

    def broken(_text: str, _name: str) -> list[str]:
        raise ValueError("labeller bug")

    assert indexer.scan(conn, [folder], labeller=broken).inserted == 1
    assert rows(conn)[("proj-demo", "user_alice.md")]["labels"] == "[]"


def test_stat_cache_skips_the_read_of_an_unchanged_file(conn, tmp_path, monkeypatch):
    folder = make_folder(tmp_path, "proj-demo", {"user_alice.md": "x\n"})
    hour_ago = time.time() - 3600
    os.utime(folder / "user_alice.md", (hour_ago, hour_ago))
    cache: indexer.StatCache = {}
    indexer.scan(conn, [folder], stat_cache=cache)
    assert str(folder / "user_alice.md") in cache
    reads: list[Path] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert indexer.scan(conn, [folder], stat_cache=cache).unchanged == 1
    assert reads == []


def test_namespaces_are_separate(conn, tmp_path):
    folder = make_folder(tmp_path, "proj-demo", _ten_files())
    indexer.scan(conn, [folder], project="ns-one")
    result = indexer.scan(conn, [folder], project="ns-two")
    assert result.inserted == 10
    assert conn.execute("SELECT count(DISTINCT project) FROM memories").fetchone()[0] == 2
