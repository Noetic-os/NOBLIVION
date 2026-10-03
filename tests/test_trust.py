# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trust in the store (design doc 0001, sections 4.5, 4.6 and 9).

Fictional data only. What these tests hold:

1. The formula: no events gives trust_0; bounded; monotone in uses.
2. The batch checks of section 4.5, per request and per event.
3. Ingest: one transaction per batch, idempotent on (session, memory, kind),
   the earliest time of a repeat, ``path`` resolution by root, the rollup
   row recomputed in the same transaction, nothing half written.
4. NOBLIVION-1 defect 1 (recompute and ingest race) and defect 2 (orphan
   feedback rows) are designed out; a rename keeps the trust history.
5. The report rules at each threshold.
6. The maintenance pass.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from noblivion import db, indexer, trust
from store_helpers import add_memory, soft_delete

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
TS = "2026-10-03T09:15:02Z"
ROOT = "-proj-alpha"


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "data" / "noblivion.db"
    db.open_db(path, create=True).close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path, create=True)
    yield c
    c.close()


def ingest(conn, session_id: str, events: list[dict], root: str | None = None, now=NOW) -> dict:
    body: dict = {"session_id": session_id, "events": events}
    if root is not None:
        body["root"] = root
    return trust.store_batch(conn, trust.parse_batch(body, now=now), now=now)


def rollup(conn, memory_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM feedback WHERE memory_id = ?", (memory_id,)).fetchone()
    return None if row is None else dict(row)


def count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


# 1. the formula ---------------------------------------------------------------


def test_no_events_gives_trust_0():
    assert trust.trust_score(0.5, 0, 0, 0) == 0.5
    assert trust.trust_score(0.3, 0, 0, 0) == 0.3


def test_trust_is_bounded_and_monotone_in_uses():
    scores = [trust.trust_score(0.5, 20, uses, 0) for uses in range(21)]
    assert scores == sorted(scores)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert trust.trust_score(0.5, 10, 10, 0) == pytest.approx(0.75)
    assert trust.trust_score(0.5, 4, 0, 3) == pytest.approx(5 / 14)  # u_eff never below 0
    assert trust.trust_score(1.0, 0, 50, 0) == 1.0


# 2. the batch checks ------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"events": []}, 400),
        ({"session_id": "", "events": []}, 400),
        ({"session_id": "-starts-with-dash", "events": []}, 400),
        ({"session_id": "s" * 129, "events": []}, 400),
        ({"session_id": "s 1", "events": []}, 400),
        ({"session_id": "s1", "events": {}}, 400),
        ({"session_id": "s1", "events": [{}] * 501}, 413),
        ({"session_id": "s1", "events": [], "root": 7}, 400),
    ],
)
def test_a_bad_request_is_refused_whole(body, status):
    with pytest.raises(trust.BatchRefused) as err:
        trust.parse_batch(body, now=NOW)
    assert err.value.status == status


def test_persona_project_and_sources_are_ignored():
    body = {
        "session_id": "s1",
        "persona": "other",
        "project": "other",
        "events": [{"kind": "use", "mv_id": 5, "ts": TS, "sources": ["guard_rows"]}],
    }
    batch = trust.parse_batch(body, now=NOW)
    assert batch.rejected == 0 and batch.events[0].mv_id == 5


@pytest.mark.parametrize(
    "event",
    [
        "not an object",
        {"kind": "shown", "mv_id": 1},
        {"kind": "contradiction", "mv_id": 1},
        {"kind": "use"},
        {"kind": "use", "mv_id": 1, "path": "a.md"},
        {"kind": "use", "mv_id": True},
        {"kind": "use", "mv_id": 0},
        {"kind": "use", "mv_id": 2_147_483_648},
        {"kind": "use", "mv_id": "²"},
        {"kind": "use", "mv_id": "12a"},
        {"kind": "use", "path": "/abs/a.md"},
        {"kind": "use", "path": "../a.md"},
        {"kind": "use", "path": "a/./b.md"},
        {"kind": "use", "path": "a//b.md"},
        {"kind": "use", "path": "a\\b.md"},
        {"kind": "use", "path": "a\x00.md"},
        {"kind": "use", "path": "notes.txt"},
        {"kind": "use", "path": "p" * 510 + ".md"},
        {"kind": "use", "path": ""},
        {"kind": "use", "mv_id": 1, "ts": "yesterday"},
        {"kind": "use", "mv_id": 1, "ts": ""},
        {"kind": "use", "mv_id": 1, "ts": 1759480000},
        {"kind": "use", "mv_id": 1, "ts": "2026-10-04T12:00:01Z"},  # > 1 day ahead
        {"kind": "use", "mv_id": 1, "ts": "2026-07-14T11:59:59Z"},  # > 80 days old
    ],
)
def test_a_bad_event_is_rejected_and_counted(event):
    batch = trust.parse_batch({"session_id": "s1", "events": [event]}, now=NOW)
    assert batch.events == () and batch.rejected == 1


def test_good_event_forms():
    events = [
        {"kind": "recall", "mv_id": "1000001"},  # no ts: server time
        {"kind": "use", "path": "./feedback_a.md", "ts": "2026-10-03T09:00:00"},  # no offset: UTC
        {"kind": "use", "path": "sub/b.md", "ts": "2026-10-03T11:00:00+02:00"},
        {"kind": "use", "mv_id": 7, "ts": "2026-10-04T11:00:00Z"},  # ahead < 1 day: clamped
    ]
    batch = trust.parse_batch({"session_id": "a.B:c-1_", "events": events}, now=NOW)
    assert batch.rejected == 0
    got = [(e.mv_id, e.path, e.ts) for e in batch.events]
    assert got == [
        (1000001, None, db.format_ts(NOW)),
        (None, "feedback_a.md", "2026-10-03T09:00:00.000000Z"),
        (None, "sub/b.md", "2026-10-03T09:00:00.000000Z"),
        (7, None, db.format_ts(NOW)),
    ]


# 3. ingest ------------------------------------------------------------------------


def test_ingest_counts_and_is_idempotent(conn):
    a = add_memory(conn, "feedback_a", "Run the tests first.")
    b = add_memory(conn, "feedback_b", "Read the plan first.")
    events = [
        {"kind": "recall", "mv_id": a, "ts": "2026-10-03T09:20:00Z"},
        {"kind": "recall", "mv_id": a, "ts": "2026-10-03T09:10:00Z"},  # repeat: earliest kept
        {"kind": "use", "path": "feedback_b.md", "ts": TS},
        {"kind": "use", "mv_id": 999_999_999, "ts": TS},  # no such row
        {"kind": "bogus"},
    ]
    assert ingest(conn, "s1", events, root=ROOT) == {
        "inserted": 2,
        "duplicate": 1,
        "unknown": 1,
        "rejected": 1,
    }
    ts = conn.execute(
        "SELECT ts FROM feedback_events WHERE memory_id = ? AND kind = 'recall'", (a,)
    ).fetchone()[0]
    assert ts == "2026-10-03T09:10:00.000000Z"
    before = [rollup(conn, a), rollup(conn, b)]
    # The same batch again: every event is a duplicate, nothing moves.
    assert ingest(conn, "s1", events, root=ROOT) == {
        "inserted": 0,
        "duplicate": 3,
        "unknown": 1,
        "rejected": 1,
    }
    assert [rollup(conn, a), rollup(conn, b)] == before
    assert count(conn, "feedback_events") == 2


def test_ingest_recomputes_the_rollup_in_the_same_transaction(conn):
    a = add_memory(conn, "feedback_a", "Run the tests first.")
    ingest(conn, "s1", [{"kind": "recall", "mv_id": a, "ts": TS}])
    r = rollup(conn, a)
    assert (r["trials"], r["use_pos"], r["trust_score"], r["trust_0"]) == (0, 0.0, 0.5, 0.5)
    assert r["last_recalled_at"] == "2026-10-03T09:15:02.000000Z" and r["last_used_at"] is None
    ingest(conn, "s1", [{"kind": "use", "mv_id": a, "ts": TS}])
    ingest(conn, "s2", [{"kind": "use", "mv_id": a, "ts": TS}])
    r = rollup(conn, a)
    assert (r["trials"], r["use_pos"]) == (2, 2.0)
    assert r["trust_score"] == pytest.approx(trust.trust_score(0.5, 2, 2, 0))
    max_id = conn.execute("SELECT max(id) FROM feedback_events").fetchone()[0]
    assert r["folded_event_id"] == max_id
    assert trust.stale_ids(conn) == []


def test_a_mined_row_gets_the_mined_prior(conn):
    m = add_memory(conn, "mined_note", "A mined note.", source_type=db.SOURCE_MINED)
    trust.store_batch(
        conn,
        trust.parse_batch({"session_id": "s1", "events": [{"kind": "use", "mv_id": m}]}),
        prior_mined=0.25,
    )
    assert rollup(conn, m)["trust_0"] == 0.25


def test_path_resolution_by_root(conn):
    a1 = add_memory(conn, "feedback_same", "Rule in alpha.", root="-proj-alpha")
    b1 = add_memory(conn, "feedback_same", "Rule in beta.", root="-proj-beta")
    only = add_memory(conn, "feedback_only", "Only in alpha.", root="-proj-alpha")
    use = [{"kind": "use", "path": "feedback_same.md", "ts": TS}]
    assert ingest(conn, "s1", use, root="-proj-beta")["inserted"] == 1
    assert rollup(conn, b1)["use_pos"] == 1 and rollup(conn, a1) is None
    # Without a root, an ambiguous path is unknown; a unique one resolves.
    assert ingest(conn, "s2", use)["unknown"] == 1
    assert ingest(conn, "s2", [{"kind": "use", "path": "feedback_only.md"}])["inserted"] == 1
    assert rollup(conn, only)["use_pos"] == 1
    # A root that holds no such file: unknown, even when another root has it.
    assert (
        ingest(conn, "s3", [{"kind": "use", "path": "feedback_only.md"}], root="-x")["unknown"] == 1
    )


def test_a_path_never_resolves_to_a_mined_row(conn):
    add_memory(conn, "mined_note", "A mined note.", source_type=db.SOURCE_MINED)
    assert ingest(conn, "s1", [{"kind": "use", "path": "mined_note.md"}], root=ROOT)["unknown"] == 1


def test_deleted_and_archived_rows_are_unknown(conn):
    gone = add_memory(conn, "feedback_gone", "Gone.")
    kept = add_memory(conn, "feedback_kept", "Archived.")
    soft_delete(conn, gone)
    with db.write_tx(conn):
        conn.execute("UPDATE memories SET archived_at = ? WHERE id = ?", (db.utc_now(), kept))
    events = [{"kind": "use", "mv_id": gone}, {"kind": "use", "path": "feedback_kept.md"}]
    assert ingest(conn, "s1", events, root=ROOT)["unknown"] == 2
    assert count(conn, "feedback_events") == 0


def test_a_failed_batch_writes_nothing(conn, monkeypatch):
    a = add_memory(conn, "feedback_a", "Run the tests first.")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(trust, "write_rollups", broken)
    with pytest.raises(sqlite3.OperationalError):
        ingest(conn, "s1", [{"kind": "use", "mv_id": a}])
    assert count(conn, "feedback_events") == 0 and count(conn, "feedback") == 0
    assert not conn.in_transaction


def test_recompute_refuses_to_run_outside_a_transaction(conn):
    with pytest.raises(RuntimeError):
        trust.recompute(conn, [1])


# 4. NOBLIVION-1, defect 1: recompute and ingest race ------------------------------


def test_defect1_an_ingest_during_a_recompute_is_counted(db_path, conn):
    """Section 9.4: the repair reads the events, an ingest commits, the
    repair writes. The ingest must not be lost.

    By design the ingest blocks on ``BEGIN IMMEDIATE`` until the repair
    commits, so the repair cannot write stale counts over it.
    """
    a = add_memory(conn, "feedback_a", "Run the tests first.")
    # A stale rollup: an event with no recompute (a crash, or a CLI import).
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s-old', ?, 'use', 1, ?, ?)",
            (a, db.utc_now(), db.utc_now()),
        )
    assert trust.stale_ids(conn) == [a]

    main = threading.current_thread()
    real_read = trust.read_counts
    seen: dict[str, object] = {}

    def ingest_other_session() -> None:
        other = db.connect(db_path, create=True)
        try:
            seen["counts"] = ingest(other, "s-new", [{"kind": "use", "mv_id": a}])
        finally:
            other.close()

    def paused_read(c, ids, **kwargs):
        out = real_read(c, ids, **kwargs)
        if threading.current_thread() is main and "thread" not in seen:
            worker = threading.Thread(target=ingest_other_session)
            seen["thread"] = worker
            worker.start()
            worker.join(0.5)  # the ingest gets this long to commit between read and write
            seen["ingest_done_before_write"] = not worker.is_alive()
        return out

    trust.read_counts = paused_read
    try:
        assert trust.repair(conn) == 1
    finally:
        trust.read_counts = real_read
    worker = seen["thread"]
    assert isinstance(worker, threading.Thread)
    worker.join(10)
    assert seen["counts"] == {"inserted": 1, "duplicate": 0, "unknown": 0, "rejected": 0}
    r = rollup(conn, a)
    assert (r["trials"], r["use_pos"]) == (2, 2.0)  # the ingest is counted, not lost
    assert r["folded_event_id"] == conn.execute("SELECT max(id) FROM feedback_events").fetchone()[0]
    assert trust.stale_ids(conn) == []
    assert seen["ingest_done_before_write"] is False  # it waited for the write lock


def test_defect1_a_backdated_event_after_a_recompute_is_still_found(conn):
    """The stale mark is the event id watermark, not a clock: an event with
    an old ``ts`` that lands after a recompute is still repaired."""
    a = add_memory(conn, "feedback_a", "Run the tests first.")
    ingest(conn, "s1", [{"kind": "use", "mv_id": a, "ts": TS}])
    assert trust.stale_ids(conn) == []
    old = db.format_ts(NOW - timedelta(days=60))
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s-late', ?, 'use', 1, ?, ?)",
            (a, old, db.utc_now()),
        )
    assert trust.stale_ids(conn) == [a]
    assert trust.repair(conn) == 1
    assert rollup(conn, a)["use_pos"] == 2.0


def test_repair_runs_in_batches_of_200(conn):
    ids = [add_memory(conn, f"feedback_{i:03d}", f"Rule number {i}.") for i in range(205)]
    with db.write_tx(conn):
        conn.executemany(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s1', ?, 'recall', 0, ?, ?)",
            [(i, db.utc_now(), db.utc_now()) for i in ids],
        )
    assert len(trust.stale_ids(conn, 1000)) == 205
    assert trust.repair(conn) == 205
    assert trust.stale_ids(conn) == [] and count(conn, "feedback") == 205


# 4. NOBLIVION-1, defect 2: orphan feedback rows -----------------------------------


def _memory_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "projects" / ROOT / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write(folder: Path, name: str, body: str) -> None:
    text = f"---\nname: {name}\ndescription: {body}\ntype: feedback\n---\n{body}\n"
    (folder / name).write_text(text, encoding="utf-8")


def _id_of(conn, path: str) -> int:
    return int(conn.execute("SELECT id FROM memories WHERE path = ?", (path,)).fetchone()[0])


def test_defect2_a_purged_memory_takes_its_trust_rows_with_it(tmp_path, conn):
    """Section 9.4: delete through the indexer path, then the grace purge.
    Zero rows in ``feedback`` and ``feedback_events``; no orphan rows."""
    folder = _memory_dir(tmp_path)
    for name in ("feedback_keep.md", "feedback_drop.md"):
        _write(folder, name, f"The rule of {name}.")
    indexer.scan(conn, [folder])
    drop, keep = _id_of(conn, "feedback_drop.md"), _id_of(conn, "feedback_keep.md")
    ingest(conn, "s1", [{"kind": "use", "mv_id": drop}, {"kind": "use", "mv_id": keep}])
    assert count(conn, "feedback") == 2

    (folder / "feedback_drop.md").unlink()
    indexer.scan(conn, [folder])
    # Soft delete: the row leaves every pool but keeps its history (grace period).
    assert count(conn, "feedback_events") == 2
    report = trust.report(conn, now=NOW)
    assert all(r["mv_id"] != drop for name in ("retire", "promote", "demote") for r in report[name])
    assert ingest(conn, "s2", [{"kind": "use", "mv_id": drop}])["unknown"] == 1

    assert db.purge_deleted(conn, 0, now=datetime.now(timezone.utc) + timedelta(seconds=1)) == 1
    left = {
        int(r[0])
        for r in conn.execute(
            "SELECT memory_id FROM feedback UNION SELECT memory_id FROM feedback_events"
        )
    }
    assert left == {keep}
    assert db.foreign_key_problems(conn) == []


def test_defect2_a_renamed_file_keeps_its_id_and_trust_history(tmp_path, conn):
    folder = _memory_dir(tmp_path)
    _write(folder, "feedback_other.md", "Another rule.")
    _write(folder, "feedback_old_name.md", "Keep the tests green.")
    indexer.scan(conn, [folder])
    memory_id = _id_of(conn, "feedback_old_name.md")
    ingest(conn, "s1", [{"kind": "use", "mv_id": memory_id}])
    ingest(conn, "s2", [{"kind": "use", "path": "feedback_old_name.md"}], root=ROOT)

    (folder / "feedback_old_name.md").rename(folder / "feedback_new_name.md")
    indexer.scan(conn, [folder])
    assert _id_of(conn, "feedback_new_name.md") == memory_id
    assert rollup(conn, memory_id)["use_pos"] == 2.0
    # The new path resolves to the same row; the old one no longer does.
    assert (
        ingest(conn, "s3", [{"kind": "use", "path": "feedback_new_name.md"}], root=ROOT)["inserted"]
        == 1
    )
    assert (
        ingest(conn, "s4", [{"kind": "use", "path": "feedback_old_name.md"}], root=ROOT)["unknown"]
        == 1
    )
    assert rollup(conn, memory_id)["use_pos"] == 3.0


def test_every_connection_enforces_foreign_keys(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError), db.write_tx(conn):
        conn.execute(
            "INSERT INTO feedback (memory_id, trust_0, trust_score) VALUES (123456789, 0.5, 0.5)"
        )


# 5. the report ----------------------------------------------------------------------


def _file(mv_id: int, path: str, *, age_days: int = 60, root: str = ROOT, category="feedback"):
    return trust.FileRow(mv_id, root, path, category, NOW - timedelta(days=age_days))


def _agg(shown=0, used=0, first_days=40, last_use_days=None, contra=0):
    last_use = NOW - timedelta(days=last_use_days) if last_use_days is not None else None
    return trust.EventAgg(
        first_ts=NOW - timedelta(days=first_days),
        shown_sessions=shown,
        use_sessions=used,
        trials=used,
        contradictions=contra,
        last_use=last_use,
    )


def _sessions(n: int, days: int = 40):
    """``n`` sessions spread over the last ``days`` days."""
    return tuple(
        (
            NOW - timedelta(days=days) + timedelta(hours=i),
            NOW - timedelta(days=days) + timedelta(hours=i),
        )
        for i in range(n)
    )


def _inputs(files, aggs=None, links=None, sessions=None, trust0=None):
    return trust.ReportInputs(
        files=tuple(files),
        index_links=links or {},
        aggs=aggs or {},
        trust0=trust0 or {},
        sessions=sessions if sessions is not None else _sessions(30),
    )


def _paths(report, name):
    return [r["path"] for r in report[name]]


def test_report_shape():
    report = trust.build_report(_inputs([_file(1, "a.md")], {1: _agg(shown=20)}), now=NOW)
    assert set(report) == {
        "persona",
        "generated_at",
        "trust_prior",
        "retire",
        "promote",
        "demote",
        "truncated",
    }
    assert report["persona"] == "claude_code" and report["trust_prior"] == 0.5
    assert report["generated_at"] == "2026-10-03T12:00:00Z" and report["truncated"] is False
    row = report["retire"][0]
    assert set(row) == {
        "mv_id",
        "root",
        "path",
        "trust",
        "trials",
        "shown_sessions",
        "use_sessions",
        "reason",
    }
    assert row["root"] == ROOT and row["reason"].startswith("shown in 20 sessions, used in none")


def test_retire_thresholds():
    files = [
        _file(1, "at_limit.md"),
        _file(2, "shown_19.md"),
        _file(3, "used_once.md"),
        _file(4, "young.md", age_days=29),
        _file(5, "MEMORY.md"),
        _file(6, "topic_x.md"),
        _file(7, "indexed.md", category="index"),
    ]
    aggs = {
        1: _agg(shown=20),
        2: _agg(shown=19),
        3: _agg(shown=40, used=1),
        4: _agg(shown=40),
        5: _agg(shown=40),
        6: _agg(shown=40),
        7: _agg(shown=40),
    }
    report = trust.build_report(_inputs(files, aggs), now=NOW)
    assert _paths(report, "retire") == ["at_limit.md"]


def test_promote_thresholds():
    # trust_0 0.5: 7 uses in 7 trials give (5 + 7) / 17 = 0.706; 6 give 0.6875.
    files = [_file(i, f"f{i}.md") for i in range(1, 7)]
    aggs = {
        1: _agg(shown=7, used=7, first_days=14),  # 7 of the 30 later sessions: 23 %
        2: _agg(shown=6, used=6, first_days=20),  # trust below 0.7
        3: _agg(shown=7, used=7, first_days=13),  # first event less than 14 days ago
        4: _agg(shown=7, used=7, first_days=61),  # 7 of all 40 sessions: 17.5 %
        5: _agg(shown=8, used=8, first_days=20),
        6: _agg(shown=4, used=4, first_days=20),  # trust 0.93 but only 4 trials
    }
    sessions = _sessions(30, days=10) + _sessions(10, days=60)
    report = trust.build_report(_inputs(files, aggs, sessions=sessions, trust0={6: 0.9}), now=NOW)
    assert _paths(report, "promote") == ["f5.md", "f1.md"]  # by trust, then uses
    assert "used in 7 of 30 sessions (23%) over 14 days" in report["promote"][1]["reason"]


def test_demote_needs_30_days_of_history_and_no_recent_use():
    files = [
        _file(1, "never_used.md"),
        _file(2, "used_31_days_ago.md"),
        _file(3, "used_29_days_ago.md"),
        _file(4, "other_root.md", root="-proj-beta"),
    ]
    aggs = {2: _agg(used=1, last_use_days=31), 3: _agg(used=1, last_use_days=29)}
    links = {
        ROOT: frozenset({"never_used.md", "used_31_days_ago.md", "used_29_days_ago.md"}),
        "-proj-beta": frozenset(),
        "-proj-gamma": frozenset({"other_root.md"}),  # a link in another root does not count
    }
    report = trust.build_report(_inputs(files, aggs, links, _sessions(5, days=30)), now=NOW)
    assert _paths(report, "demote") == ["never_used.md", "used_31_days_ago.md"]
    short = trust.build_report(_inputs(files, aggs, links, _sessions(5, days=29)), now=NOW)
    assert short["demote"] == []
    # A linked file is never a retire or promote candidate.
    linked_and_shown = trust.build_report(
        _inputs([_file(1, "never_used.md")], {1: _agg(shown=50)}, links), now=NOW
    )
    assert linked_and_shown["retire"] == []


def test_report_sort_order_and_cap():
    files = [_file(i, f"r{i:04d}.md") for i in range(1, 503)]
    aggs = {i: _agg(shown=20 + (i % 3)) for i in range(1, 503)}
    report = trust.build_report(_inputs(files, aggs), now=NOW)
    assert len(report["retire"]) == 500 and report["truncated"] is True
    keys = [(-r["shown_sessions"], r["path"]) for r in report["retire"]]
    assert keys == sorted(keys)


def test_memory_index_links():
    text = "- [A](feedback_a.md) and [B](<sub/b.md>) [C]( ./c.md ) [D](../x.md) [E](e.txt)"
    assert trust.memory_index_links(text) == {"feedback_a.md", "sub/b.md", "c.md"}


def test_report_from_the_database(tmp_path, conn):
    folder = _memory_dir(tmp_path)
    _write(folder, "MEMORY.md", "- [Linked](feedback_linked.md)")
    _write(folder, "feedback_linked.md", "A linked rule.")
    _write(folder, "feedback_shown.md", "A rule shown often.")
    indexer.scan(conn, [folder])
    mined = add_memory(conn, "mined_note", "Mined.", source_type=db.SOURCE_MINED)
    shown = _id_of(conn, "feedback_shown.md")
    old = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.write_tx(conn):
        conn.execute(
            "UPDATE memories SET created_at = ?", (db.format_ts(NOW - timedelta(days=45)),)
        )
    for i in range(20):
        ingest(
            conn,
            f"s{i}",
            [{"kind": "recall", "mv_id": shown, "ts": old}, {"kind": "use", "mv_id": mined}],
        )
    report = trust.report(conn, now=NOW)
    assert [(r["mv_id"], r["root"], r["path"]) for r in report["retire"]] == [
        (shown, ROOT, "feedback_shown.md")
    ]
    assert report["retire"][0]["shown_sessions"] == 20
    assert report["retire"][0]["reason"].endswith("first indexed 45 days ago")
    assert _paths(report, "demote") == ["feedback_linked.md"]  # history spans 40 days
    assert all(
        r["mv_id"] != mined for name in ("retire", "promote", "demote") for r in report[name]
    )


# 6. maintenance ------------------------------------------------------------------------


def test_maintenance_pass_repairs_purges_and_stamps(tmp_path, conn):
    a = add_memory(conn, "feedback_a", "Run the tests first.")
    gone = add_memory(conn, "feedback_gone", "Gone.")
    ingest(conn, "s1", [{"kind": "use", "mv_id": gone}])
    soft_delete(conn, gone)
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO feedback_events (session_id, memory_id, kind, citation_capable, ts, "
            "received_at) VALUES ('s2', ?, 'use', 1, ?, ?)",
            (a, db.utc_now(), db.utc_now()),
        )
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(5):
        (backups / f"noblivion-v1-2026100{i}.db").write_bytes(b"")
    later = datetime.now(timezone.utc) + timedelta(days=15)
    result = trust.maintenance_pass(
        conn, delete_grace_days=14, archive_retention_days=90, backups_dir=backups, now=later
    )
    assert (result.repaired, result.purged_deleted, result.orphans, result.failed) == (1, 1, 0, "")
    assert rollup(conn, a)["use_pos"] == 1.0
    assert count(conn, "feedback_events") == 1 and rollup(conn, gone) is None
    assert len(list(backups.iterdir())) == 3
    assert db.get_meta(conn, "last_maintenance_at") == db.format_ts(later)
