# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion import``: memories from a JSONL file (NOBLIVION-93).
Fictional data only; every store lives in a temporary folder."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import config, db, dedup, embedding, importer, indexer, ranking, redaction, trust
from noblivion.__main__ import main as cli_main
from store_helpers import FakeEmbedder

SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0kLmN"  # fictional key shape
SESSION_ROOT = "-work-acme-garden"


def write_jsonl(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in lines:
            fh.write((item if isinstance(item, str) else json.dumps(item)) + "\n")
    return path


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def do_import(conn, path, *, apply=True, archived=False, labels=()):
    return importer.run(
        conn, path, project="claude_code", apply=apply, archived=archived, extra_labels=labels
    )


def rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM memories WHERE root = ? ORDER BY id", (db.IMPORT_ROOT,)
        ).fetchall()
    ]


# -- parse and validate ---------------------------------------------------------


def test_invalid_lines_are_counted_with_their_line_numbers(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {"content": "Water the tomatoes at dawn."},
            "",
            "   ",
            "not json",
            "[1, 2]",
            {"content": "   "},
            {"source_type": "notes"},
            {"content": "a", "labels": "garden"},
            {"content": "b", "labels": ["ok", 3]},
            {"content": "c", "created_at": "last tuesday"},
            {"content": "d", "updated_at": 17},
            {"content": "e", "pinned": "yes"},
            {"content": "f", "category": 4},
            {"content": "g", "source_type": ["mined"]},
            {"content": "h", "unknown_field": {"nested": True}},
        ],
    )
    report = do_import(conn, src, apply=False)
    assert (report.lines, report.blank, report.valid, report.invalid) == (15, 2, 2, 11)
    assert [(i["line"], i["reason"]) for i in report.invalid_lines] == [
        (4, "not JSON"),
        (5, "not a JSON object"),
        (6, "content is missing or empty"),
        (7, "content is missing or empty"),
        (8, "labels must be a list of strings"),
        (9, "labels must be a list of strings"),
        (10, "created_at is not an ISO-8601 time"),
        (11, "updated_at is not an ISO-8601 time"),
        (12, "pinned must be true or false"),
        (13, "category must be a string"),
        (14, "source_type must be a string"),
    ]


def test_the_invalid_line_list_is_capped(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", ["nope"] * 30)
    report = do_import(conn, src, apply=False)
    assert report.invalid == 30
    assert len(report.invalid_lines) == importer.INVALID_LIST_MAX
    assert any("10 more invalid lines" in line for line in report.summary())


def test_a_line_that_is_not_utf8_is_invalid(conn, tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_bytes(b'{"content": "ok"}\n\xff\xfe\n')
    report = do_import(conn, src, apply=False)
    assert (report.valid, report.invalid) == (1, 1)
    assert report.invalid_lines == [{"line": 2, "reason": "not UTF-8"}]


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("2030-01-02T03:04:05Z", "2030-01-02T03:04:05.000000Z"),
        ("2030-01-02T03:04:05.250+02:00", "2030-01-02T01:04:05.250000Z"),
        ("2030-01-02T03:04:05", "2030-01-02T03:04:05.000000Z"),
        ("2030-01-02", "2030-01-02T00:00:00.000000Z"),
    ],
)
def test_times_are_stored_in_utc(raw, stored):
    assert importer.parse_time(raw, "created_at", "default") == stored


# -- dry run and apply ----------------------------------------------------------


def test_a_dry_run_writes_nothing(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Prune the roses in March."}])
    before = db.revisions(conn)
    report = do_import(conn, src, apply=False)
    assert report.to_insert == 1 and report.inserted == 0
    assert rows(conn) == [] and db.revisions(conn) == before


def test_apply_inserts_rows_with_their_fields(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {
                "content": "# Hose rule\nUse the garden hose before noon.",
                "category": "Feedback",
                "labels": ["garden"],
                "created_at": "2029-05-06T07:08:09Z",
                "updated_at": "2029-06-07T08:09:10Z",
                "pinned": True,
            }
        ],
    )
    report = do_import(conn, src)
    assert (report.inserted, report.content_rev) == (1, db.revisions(conn)[0])
    (row,) = rows(conn)
    text = "# Hose rule\nUse the garden hose before noon."
    assert row["source_type"] == db.SOURCE_MD
    assert row["project"] == "claude_code"
    assert (
        row["hash"]
        == row["path"]
        == importer.Entry(0, db.SOURCE_MD, text, "", [], "", "", False).hash
    )
    assert row["category"] == "feedback"
    assert json.loads(row["labels"]) == ["garden", "category:Feedback"]
    assert (row["created_at"], row["updated_at"]) == (
        "2029-05-06T07:08:09.000000Z",
        "2029-06-07T08:09:10.000000Z",
    )
    assert row["pinned"] == 1 and row["archived_at"] is None and row["deleted_at"] is None
    assert row["rev"] == report.content_rev
    assert row["content"] == (
        f"# Hose rule\n\n[claude_code_md: {row['path']}]\n\nUse the garden hose before noon."
    )


def test_missing_times_default_to_now(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Mulch the beds."}])
    start = db.utc_now()
    do_import(conn, src)
    (row,) = rows(conn)
    assert row["created_at"] >= start and row["updated_at"] >= start


def test_a_second_run_inserts_nothing(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl", [{"content": "Rake the leaves."}, {"content": "Oil the shears."}]
    )
    assert do_import(conn, src).inserted == 2
    rev = db.revisions(conn)
    again = do_import(conn, src)
    assert (again.inserted, again.already_present, again.to_insert) == (0, 2, 0)
    assert db.revisions(conn) == rev and len(rows(conn)) == 2


def test_the_same_content_twice_in_a_file_is_stored_once(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {"content": "Feed the hens."},
            {"content": "  Feed the hens.  ", "source_type": "mined"},
            {"content": "Feed the goats."},
        ],
    )
    report = do_import(conn, src)
    assert (report.valid, report.duplicates_in_file, report.inserted) == (3, 1, 2)
    assert [r["source_type"] for r in rows(conn)] == [db.SOURCE_MD, db.SOURCE_MD]


def test_a_memory_file_row_with_the_same_hash_is_not_a_match(conn, tmp_path):
    text = "Close the gate at night."
    path = write_jsonl(tmp_path / "in.jsonl", [{"content": text}])
    hash_ = importer.Entry(0, db.SOURCE_MD, text, "", [], "", "", False).hash
    with db.write_tx(conn):
        rev = db.bump_rev(conn)
        db.insert_memory(
            conn,
            project="claude_code",
            root=SESSION_ROOT,
            path="gate.md",
            source_type=db.SOURCE_MD,
            category="feedback",
            content=text,
            content_hash=hash_,
            labels="[]",
            rev=rev,
            now=db.utc_now(),
        )
    report = do_import(conn, path)
    assert (report.already_present, report.inserted) == (0, 1)


def test_labels_merge_in_order_and_once(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {
                "content": "Sharpen the hoe.",
                "labels": ["tools", " garden ", "tools", ""],
                "source_type": "Notebook",
                "category": "chores",
            }
        ],
    )
    do_import(conn, src, labels=["garden", "batch-1"])
    (row,) = rows(conn)
    assert json.loads(row["labels"]) == [
        "tools",
        "garden",
        "batch-1",
        "source:Notebook",
        "category:chores",
    ]
    assert row["category"] == importer.CATEGORY_FALLBACK


def test_known_source_types_get_no_source_label(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {"content": "one", "source_type": "claude_code_md"},
            {"content": "two", "source_type": "MINED"},
            {"content": "three", "source_type": ""},
        ],
    )
    do_import(conn, src)
    assert [json.loads(r["labels"]) for r in rows(conn)] == [[], [], []]


def test_a_label_the_redactor_would_change_is_dropped(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Fix the fence.", "labels": [SECRET]}])
    report = do_import(conn, src)
    assert report.labels_dropped == 1
    assert json.loads(rows(conn)[0]["labels"]) == []


# -- redaction ------------------------------------------------------------------


def test_secrets_are_redacted_before_the_hash(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": f"the pump key is {SECRET}"}])
    report = do_import(conn, src)
    assert report.redacted == 1
    (row,) = rows(conn)
    assert SECRET not in row["content"]
    stored = redaction.redact_at_rest(f"the pump key is {SECRET}")
    assert row["hash"] == importer.Entry(0, db.SOURCE_MD, stored, "", [], "", "", False).hash


def test_a_line_that_gives_the_fail_token_is_skipped(conn, tmp_path, monkeypatch):
    real = redaction.redact_at_rest

    def redact(text: str) -> str:
        return redaction.REDACTION_FAILED_TOKEN if "POISON" in text else real(text)

    monkeypatch.setattr(redaction, "redact_at_rest", redact)
    src = write_jsonl(
        tmp_path / "in.jsonl", [{"content": "POISON in the shed"}, {"content": "Paint the shed."}]
    )
    report = do_import(conn, src)
    assert (report.valid, report.redaction_skipped, report.inserted) == (2, 1, 1)
    assert all("POISON" not in r["content"] for r in rows(conn))


# -- source types and recall ------------------------------------------------------


def test_mined_rows_are_stored_as_transcript_mined_and_recalled_on_request(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": "Tool error: sprinkler valve stuck", "source_type": "transcript_mined"}],
    )
    do_import(conn, src)
    (row,) = rows(conn)
    assert row["source_type"] == db.SOURCE_MINED
    assert row["content"] == "Tool error: sprinkler valve stuck"  # no marker, like a miner row
    ranker = ranking.Ranker()
    plain = ranker.search(
        conn, "sprinkler valve", project="claude_code", top_k=5, root=SESSION_ROOT
    )
    assert plain.hits == []
    mined = ranker.search(
        conn,
        "sprinkler valve",
        project="claude_code",
        top_k=5,
        root=SESSION_ROOT,
        include_mined=True,
    )
    assert [h.row.id for h in mined.hits] == [row["id"]]


def test_plain_rows_enter_recall_in_every_session_root(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Compost needs turning weekly."}])
    do_import(conn, src)
    ranker = ranking.Ranker()
    for root in (SESSION_ROOT, "-work-other-project", None):
        found = ranker.search(conn, "compost turning", project="claude_code", top_k=5, root=root)
        assert [h.row.root for h in found.hits] == [db.IMPORT_ROOT]


def test_the_recall_hook_reads_a_plain_row_as_one_memory(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl", [{"content": "Seed rule\nSow carrots after the last frost."}]
    )
    do_import(conn, src)
    hook = load_hook("recall_hook", "_recall_hook_import_test")
    hit = hook.hit_from_text(rows(conn)[0]["content"])
    assert hit.md and hit.title == "Seed rule"
    assert "Sow carrots" in hit.body


def test_a_long_first_line_stays_in_the_body():
    text = "word " * 60
    out = importer.render_plain("abc", text.strip())
    title = out.split("\n", 1)[0]
    assert title.startswith("# word") and title.endswith("…")
    assert len(title) <= importer.TITLE_MAX_CHARS + 3
    assert out.endswith(text.strip())


# -- archived rows ----------------------------------------------------------------


def test_archived_rows_stay_out_of_recall_dedup_and_purges(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": "Old rule: water at dusk."}, {"content": "Old rule: water at dusk!"}],
    )
    report = do_import(conn, src, archived=True)
    assert report.inserted == 2 and report.to_dict()["by_archived"] == {"archived": 2, "live": 0}
    assert report.archived_without_vector == 2 and report.vectors_waiting == 0
    assert all(r["archived_at"] is not None for r in rows(conn))
    ranker = ranking.Ranker()
    for root in (SESSION_ROOT, None):
        hits = ranker.search(conn, "water dusk", project="claude_code", top_k=5, root=root).hits
        assert hits == []
    # The backfill embeds live rows only; give the rows vectors anyway.
    fake = FakeEmbedder()
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "vector_rev")
        for r in rows(conn):
            text = embedding.embed_text(r["content"])
            conn.execute(
                "INSERT INTO vectors (memory_id, model, dim, content_hash, blob, rev) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r["id"],
                    fake.model_id,
                    16,
                    embedding.text_hash(text),
                    embedding.vector_to_blob(fake.embed_documents([text])[0]),
                    rev,
                ),
            )
    assert dedup.load_vectors(conn, "claude_code", fake.model_id) == []
    later = datetime.now(timezone.utc) + timedelta(days=3650)
    assert db.purge_archived(conn, 0, now=later) == 0
    assert db.purge_deleted(conn, 0, now=later) == 0
    assert len(rows(conn)) == 2


def test_live_imported_rows_are_never_dedup_candidates(conn, tmp_path, monkeypatch):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": "Stake the beans early."}, {"content": "Stake the beans early!"}],
    )
    do_import(conn, src)
    fake = FakeEmbedder()
    embedding.backfill(conn, fake, finish=False)
    assert len(dedup.load_vectors(conn, "claude_code", fake.model_id)) == 2
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    found = dedup.find_candidates(
        conn,
        config.load_settings(),
        dedup.load_dedup_settings({}),
        model_id=fake.model_id,
        folders=dedup.root_folders([tmp_path / SESSION_ROOT / "memory"]),
        min_age_s=0,
    )
    assert found == []


def test_the_backfill_embeds_live_imported_rows(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Net the berries."}])
    report = do_import(conn, src)
    assert report.vectors_waiting == 1
    embedding.backfill(conn, FakeEmbedder(), finish=False)
    ids = [r["id"] for r in rows(conn)]
    assert importer.count_without_vector(conn, ids) == (0, 0)


# -- the indexer ------------------------------------------------------------------


def test_an_index_scan_leaves_imported_rows_alone(conn, tmp_path):
    text = "Turn the compost every week."
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": text}, {"content": "Cover the beds in winter."}, {"content": "x"}],
    )
    do_import(conn, src)
    do_import(conn, write_jsonl(tmp_path / "old.jsonl", [{"content": "y"}]), archived=True)
    before = rows(conn)
    folder = tmp_path / SESSION_ROOT / "memory"
    folder.mkdir(parents=True)
    # A file whose raw bytes hash like an imported row: a scan that saw the
    # imported rows would move that row to the file.
    (folder / "compost.md").write_bytes(text.encode("utf-8"))
    (folder / "other.md").write_text("---\nname: other\n---\nSomething else.\n", encoding="utf-8")
    first = indexer.scan(conn, [folder], project="claude_code", allow_shrink=True)
    assert (first.inserted, first.moved, first.deleted) == (2, 0, 0)
    for name in ("compost.md", "other.md"):
        (folder / name).unlink()
    (folder / "last.md").write_text("Keep one file.\n", encoding="utf-8")
    indexer.scan(conn, [folder], project="claude_code", allow_shrink=True, force=True)
    indexer.scan(conn, [], project="claude_code", allow_shrink=True, delete_grace_days=0)
    assert rows(conn) == before
    later = datetime.now(timezone.utc) + timedelta(days=3650)
    db.purge_deleted(conn, 0, now=later)
    assert rows(conn) == before


# -- CLI --------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db.open_db(data / "noblivion.db", create=True).close()  # install.sh made it
    return data


def test_cli_dry_run_by_default_then_apply(cli_env, tmp_path, capsys):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Weed on Saturdays."}, "oops"])
    assert cli_main(["import", str(src)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "would_insert=1" in out and "invalid line 2: not JSON" in out
    assert cli_main(["import", str(src), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["mode"], report["would_insert"], report["inserted"]) == ("dry_run", 1, 0)
    assert report["embedding"]["mode"] == importer.EMBED_MODE
    assert cli_main(["import", str(src), "--apply", "--json", "--label", "batch"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["inserted"], report["embedding"]["vectors_waiting"]) == (1, 1)
    with closing(db.connect(cli_env / "noblivion.db")) as c:
        assert json.loads(rows(c)[0]["labels"]) == ["batch"]
    assert cli_main(["import", str(src), "--apply"]) == 0
    assert "inserted=0" in capsys.readouterr().out


def test_cli_dry_run_and_apply_together_is_a_usage_error(cli_env, tmp_path, capsys):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "a"}])
    with pytest.raises(SystemExit) as exc:
        cli_main(["import", str(src), "--dry-run", "--apply"])
    assert exc.value.code == importer.EXIT_USAGE
    assert "not allowed" in capsys.readouterr().err


def test_cli_apply_refuses_a_file_with_no_valid_line(cli_env, tmp_path, capsys):
    src = write_jsonl(tmp_path / "in.jsonl", ["", "nope", {"content": ""}])
    assert cli_main(["import", str(src), "--apply"]) == importer.EXIT_REFUSED
    assert "no line it can import" in capsys.readouterr().err
    with closing(db.connect(cli_env / "noblivion.db")) as c:
        assert rows(c) == [] and db.revisions(c)[0] == 0


def test_cli_refuses_a_missing_file_and_a_missing_database(cli_env, tmp_path, capsys):
    assert cli_main(["import", str(tmp_path / "nope.jsonl")]) == importer.EXIT_REFUSED
    assert "no file at" in capsys.readouterr().err
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "a"}])
    other = tmp_path / "elsewhere" / "noblivion.db"
    assert cli_main(["import", str(src), "--db", str(other)]) == importer.EXIT_NO_DB
    assert not other.parent.exists()


def test_cli_exits_4_when_another_import_holds_the_lock(cli_env, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "a"}])
    with indexer.index_lock(cli_env / importer.IMPORT_LOCK_FILE):
        assert cli_main(["import", str(src), "--apply"]) == importer.EXIT_LOCKED


def test_cli_dry_run_never_creates_a_schema(cli_env, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "a"}])
    empty = tmp_path / "empty" / "noblivion.db"
    empty.parent.mkdir()
    empty.touch()
    assert cli_main(["import", str(src), "--db", str(empty)]) == importer.EXIT_SCHEMA
    with closing(db.connect(empty)) as c:
        assert db.user_version(c) == 0


# -- review round 1 ------------------------------------------------------------------


def test_a_deep_or_out_of_range_line_is_invalid_and_the_run_goes_on(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            "[" * 100000,
            {"content": "a", "created_at": "9999-12-31T23:59:59-01:00"},
            {"content": "b", "updated_at": "0999-01-01T00:00:00Z"},
            {"content": "c", "created_at": "2030-02-30"},
            {"content": "d", "created_at": "2030-01-01T10:00:00+24:00"},
            {"content": "Keep this one."},
        ],
    )
    report = do_import(conn, src)
    assert [(i["line"], i["reason"]) for i in report.invalid_lines] == [
        (1, "not JSON"),
        (2, "created_at is out of range"),
        (3, "updated_at is out of range"),
        (4, "created_at is out of range"),
        (5, "created_at is out of range"),
    ]
    assert report.inserted == 1


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("2030-01-02T03:04:05+00", "2030-01-02T03:04:05.000000Z"),
        ("2030-01-02T03:04:05+0130", "2030-01-02T01:34:05.000000Z"),
        ("2030-01-02 03:04", "2030-01-02T03:04:00.000000Z"),
        ("2030-01-02T03:04:05.1Z", "2030-01-02T03:04:05.100000Z"),
        ("2030-01-02T03:04:05.123456789z", "2030-01-02T03:04:05.123456Z"),
    ],
)
def test_more_time_forms_read_the_same_on_every_python(raw, stored):
    assert importer.parse_time(raw, "created_at", "default") == stored


@pytest.mark.parametrize("raw", ["20300102T030405", "2030-01-02T03", "2030-W01-1", "2030-01-02Z"])
def test_other_time_forms_are_refused(raw):
    with pytest.raises(importer.LineError):
        importer.parse_time(raw, "created_at", "default")


def test_a_bom_on_the_first_line_is_accepted(conn, tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_bytes(importer.BOM + b'{"content": "Label the jars."}\n{"content": "x"}\n')
    report = do_import(conn, src)
    assert (report.valid, report.invalid, report.inserted) == (2, 0, 2)


def test_content_over_the_size_cap_is_invalid(conn, tmp_path):
    big = "a" * (importer.MAX_CONTENT_BYTES + 1)
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": big}, {"content": "a" * importer.MAX_CONTENT_BYTES}],
    )
    report = do_import(conn, src)
    assert report.invalid_lines == [{"line": 1, "reason": "content larger than 256 KB"}]
    assert report.inserted == 1


def test_only_a_claude_code_md_marker_passes_through(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [
            {"content": "Mined quote\n[transcript_mined: s1#4]\nThe valve stuck."},
            {"content": "# Kept\n\n[claude_code_md: feedback_kept.md]\n\nAs exported."},
        ],
    )
    do_import(conn, src)
    first, second = rows(conn)
    assert first["content"].startswith(f"# Mined quote\n\n[claude_code_md: {first['path']}]")
    assert second["content"] == "# Kept\n\n[claude_code_md: feedback_kept.md]\n\nAs exported."
    hook = load_hook("recall_hook", "_recall_hook_import_marker_test")
    assert hook.hit_from_text(first["content"]).md


def test_apply_refuses_when_every_valid_line_fails_redaction(
    cli_env, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(redaction, "redact_at_rest", lambda t: redaction.REDACTION_FAILED_TOKEN)
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "one"}, {"content": "two"}])
    assert cli_main(["import", str(src), "--apply"]) == importer.EXIT_REFUSED
    assert "no line it can import" in capsys.readouterr().err
    with closing(db.connect(cli_env / "noblivion.db")) as c:
        assert db.revisions(c)[0] == 0


def test_a_row_written_between_plan_and_store_counts_as_present(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Race one."}, {"content": "Race two."}])
    report = importer.Report(file=str(src), apply=True)
    entries = importer.read_entries(src, extra_labels=(), report=report, now=db.utc_now())
    todo = importer.plan(entries, importer.present_rows(conn, "claude_code"), report)
    assert report.to_insert == 2
    do_import(conn, write_jsonl(tmp_path / "other.jsonl", [{"content": "Race one."}]))
    importer.store_entries(conn, "claude_code", todo, report, archived=False)
    assert (report.inserted, report.already_present) == (1, 1)
    assert len(rows(conn)) == 2


def test_remove_soft_deletes_the_rows_of_the_file(conn, tmp_path):
    src = write_jsonl(
        tmp_path / "in.jsonl",
        [{"content": "Drop me."}, {"content": "Drop me too.", "source_type": "mined"}],
    )
    do_import(conn, src)
    do_import(conn, write_jsonl(tmp_path / "arch.jsonl", [{"content": "Old one."}]), archived=True)
    keep = do_import(conn, write_jsonl(tmp_path / "keep.jsonl", [{"content": "Keep me."}]))
    assert keep.inserted == 1
    gone = write_jsonl(
        tmp_path / "gone.jsonl",
        [{"content": "Drop me."}, {"content": "Drop me too."}, {"content": "Old one."}, "bad"],
    )
    gone_rows = write_jsonl(tmp_path / "missing.jsonl", [{"content": "Never imported."}])
    dry = importer.run(conn, gone, project="claude_code", apply=False, remove=True)
    assert (dry.to_remove, dry.removed, dry.invalid) == (3, 0, 1)
    assert all(r["deleted_at"] is None for r in rows(conn))
    rev = db.revisions(conn)[0]
    done = importer.run(conn, gone, project="claude_code", apply=True, remove=True)
    assert (done.removed, done.content_rev) == (3, rev + 1)
    by_text = {r["hash"]: r for r in rows(conn)}
    assert sum(1 for r in by_text.values() if r["deleted_at"] is not None) == 3
    again = importer.run(conn, gone, project="claude_code", apply=True, remove=True)
    assert (again.removed, again.already_removed) == (0, 3)
    other = importer.run(conn, gone_rows, project="claude_code", apply=False, remove=True)
    assert other.not_present == 1
    later = datetime.now(timezone.utc) + timedelta(days=3650)
    assert db.purge_deleted(conn, 0, now=later) == 3
    assert [r["content"].split("\n")[0] for r in rows(conn)] == ["# Keep me."]


def test_a_new_import_revives_a_removed_row(conn, tmp_path):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Come back."}])
    do_import(conn, src)
    (before,) = rows(conn)
    importer.run(conn, src, project="claude_code", apply=True, remove=True)
    again = do_import(conn, src)
    assert (again.to_revive, again.revived, again.inserted) == (1, 1, 1)
    (after,) = rows(conn)
    assert after["id"] == before["id"] and after["deleted_at"] is None


def test_cli_remove_dry_run_apply_and_usage(cli_env, tmp_path, capsys):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "Short lived."}])
    assert cli_main(["import", str(src), "--apply"]) == 0
    capsys.readouterr()
    assert cli_main(["import", "--remove", str(src), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["action"], report["mode"], report["would_remove"]) == ("remove", "dry_run", 1)
    assert cli_main(["import", "--remove", str(src), "--apply"]) == 0
    assert "removed=1" in capsys.readouterr().out
    for extra in (["--archived"], ["--label", "x"]):
        with pytest.raises(SystemExit) as exc:
            cli_main(["import", "--remove", str(src), *extra])
        assert exc.value.code == importer.EXIT_USAGE
    empty = write_jsonl(tmp_path / "empty.jsonl", ["nope"])
    assert cli_main(["import", "--remove", str(empty), "--apply"]) == importer.EXIT_REFUSED
    assert "no line it can remove" in capsys.readouterr().err


def test_a_lock_file_error_names_the_lock_file(cli_env, tmp_path, monkeypatch, capsys):
    src = write_jsonl(tmp_path / "in.jsonl", [{"content": "a"}])

    def broken(path, timeout_s=60.0):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(importer.indexer, "index_lock", broken)
    assert cli_main(["import", str(src), "--apply"]) == importer.EXIT_REFUSED
    err = capsys.readouterr().err
    assert importer.IMPORT_LOCK_FILE in err and "data dir" in err and "in.jsonl" not in err


def _report_inputs(root: str, path: str, created_days: int, first_shown_days: int):
    now = datetime(2031, 6, 1, tzinfo=timezone.utc)
    files = (trust.FileRow(7, root, path, "reference", now - timedelta(days=created_days)),)
    agg = trust.EventAgg(
        first_ts=now - timedelta(days=first_shown_days),
        shown_sessions=25,
        use_sessions=0,
        trials=0,
        contradictions=0,
        last_use=None,
    )
    return trust.ReportInputs(files, {}, {7: agg}, {}, ()), now


def test_trust_retire_age_of_an_imported_row_starts_at_its_first_event():
    hash_ = "ab" * 32
    young, now = _report_inputs(db.IMPORT_ROOT, hash_, created_days=900, first_shown_days=10)
    assert trust.build_report(young, now=now)["retire"] == []
    old, now = _report_inputs(db.IMPORT_ROOT, hash_, created_days=900, first_shown_days=40)
    report = trust.build_report(old, now=now)
    assert report["retire"][0]["reason"].endswith("first shown 40 days ago")
    text = trust.render_report(report)
    assert f"- imported row {hash_[:12]} (id 7)" in text and db.IMPORT_ROOT not in text
    hook = load_hook("trust_report", "_trust_report_import_test")
    assert f"- imported row {hash_[:12]} (id 7)" in hook.render(report)
    md, now = _report_inputs(SESSION_ROOT, "a.md", created_days=900, first_shown_days=10)
    assert trust.build_report(md, now=now)["retire"][0]["reason"].endswith(
        "first indexed 900 days ago"
    )
