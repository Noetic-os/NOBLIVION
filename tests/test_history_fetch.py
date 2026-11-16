# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-189: the historical fetch from one current note to the notes it replaces."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from noblivion import config, db, history, indexer, rest
from noblivion.learning import LearningLedger

NS = config.DEFAULT_NAMESPACE
ROOT = "-proj"
OLD = "---\nname: old limit\ndescription: the old limit\n---\nThe limit is 100 units.\n"
NEW = (
    "---\nname: new limit\ndescription: the new limit\nsupersedes: feedback_old\n---\n"
    "The limit is 200 units since 2026-03-01.\n"
)


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "data" / "noblivion.db", create=True)
    yield c
    c.close()


def folder_of(tmp_path: Path, root: str = ROOT) -> Path:
    folder = tmp_path / "projects" / root / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def row_id(conn, path: str, root: str = ROOT) -> int:
    return int(
        conn.execute("SELECT id FROM memories WHERE root=? AND path=?", (root, path)).fetchone()[0]
    )


@pytest.fixture
def linked(conn, tmp_path):
    """The old note indexed first, then the new note that replaces it."""
    folder = folder_of(tmp_path)
    (folder / "feedback_old.md").write_text(OLD, encoding="utf-8")
    indexer.scan(conn, [folder])
    (folder / "feedback_new.md").write_text(NEW, encoding="utf-8")
    assert indexer.scan(conn, [folder]).superseded == 1
    return folder, row_id(conn, "feedback_new.md"), row_id(conn, "feedback_old.md")


def ask(conn, folder, mid, *, namespace=NS, root=ROOT, session=""):
    return history.history_answer(conn, str(mid), namespace, root, session, [folder])


def refused(answer) -> bool:
    return answer["history"] == [] and answer["reason"] == history.NO_HISTORY_REASON


# -- the allowed path -----------------------------------------------------------------


def test_the_current_note_returns_the_archived_note_it_replaces(conn, linked):
    folder, new, old = linked
    answer = ask(conn, folder, new)
    assert answer["reason"] is None and answer["id"] == new
    [item] = answer["history"]
    assert item["id"] == old and item["source"] == "feedback_old.md"
    assert item["title"] == "old limit" and "100 units" in item["text"]
    # Ordinary fetch still refuses the archived note and still returns the current one.
    assert rest.fetch_answer(conn, str(old), NS, ROOT)["reason"] == rest.NOT_FOUND_REASON
    assert "200 units" in rest.fetch_answer(conn, str(new), NS, ROOT)["text"]


def test_the_answer_stays_below_the_cap(conn, tmp_path):
    folder = folder_of(tmp_path)
    big = OLD + ("line of a long note\n" * 12_000)
    (folder / "feedback_old.md").write_text(big, encoding="utf-8")
    indexer.scan(conn, [folder])
    (folder / "feedback_new.md").write_text(NEW, encoding="utf-8")
    indexer.scan(conn, [folder])
    answer = ask(conn, folder, row_id(conn, "feedback_new.md"))
    assert answer["history"] and len(json.dumps(answer).encode()) < rest.ANSWER_CAP_BYTES


# -- refusals -------------------------------------------------------------------------


def test_a_note_with_no_supersedes_link_has_no_history(conn, tmp_path):
    folder = folder_of(tmp_path)
    (folder / "feedback_plain.md").write_text(OLD.replace("old", "plain"), encoding="utf-8")
    indexer.scan(conn, [folder])
    assert refused(ask(conn, folder, row_id(conn, "feedback_plain.md")))


def test_the_archived_note_itself_is_not_a_successor(conn, linked):
    folder, _new, old = linked
    assert refused(ask(conn, folder, old))


def test_another_root_another_project_or_no_root_is_refused(conn, linked, tmp_path):
    folder, new, _old = linked
    other = folder_of(tmp_path, "-other")
    assert refused(ask(conn, other, new, root="-other"))
    assert refused(ask(conn, folder, new, namespace="other_space"))
    assert refused(ask(conn, folder, new, root=None))
    assert refused(ask(conn, folder, new, root=""))


def test_a_same_named_note_in_another_root_is_not_reached(conn, tmp_path):
    other = folder_of(tmp_path, "-other")
    (other / "feedback_old.md").write_text(OLD, encoding="utf-8")
    mine = folder_of(tmp_path)
    (mine / "feedback_new.md").write_text(NEW, encoding="utf-8")
    indexer.scan(conn, [mine, other])
    assert refused(ask(conn, mine, row_id(conn, "feedback_new.md")))


@pytest.mark.parametrize("status", ["forgotten", "excluded"])
def test_an_older_note_later_forgotten_or_excluded_is_refused(conn, linked, status):
    folder, new, _old = linked
    (folder / "feedback_old.md").write_text(
        OLD.replace("---\nThe", f"status: {status}\n---\nThe"), encoding="utf-8"
    )
    indexer.scan(conn, [folder])
    assert refused(ask(conn, folder, new))


@pytest.mark.parametrize("status", ["excluded", "forgotten", "superseded"])
def test_an_older_learning_note_not_active_in_the_ledger_is_refused(conn, linked, status):
    folder, new, old = linked
    stored = conn.execute("SELECT hash FROM memories WHERE id=?", (old,)).fetchone()[0]
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO learning_notes (note_id, project, root, logical_key, path, revision, "
            "status, scope, category, title, statement, observed_at, file_hash, created_at, "
            "updated_at) VALUES ('n1', ?, ?, 'k', 'feedback_old.md', 1, ?, 'project', "
            "'workflow', 't', 's', '2026-01-01', ?, 0, 0)",
            (NS, ROOT, status, stored),
        )
    assert refused(ask(conn, folder, new))
    with db.write_tx(conn):
        conn.execute("UPDATE learning_notes SET status='active'")
    assert ask(conn, folder, new)["history"]  # the same row, active again, is read


def test_an_older_note_that_is_gone_from_disk_or_deleted_is_refused(conn, linked):
    folder, new, old = linked
    (folder / "feedback_old.md").unlink()
    assert refused(ask(conn, folder, new))  # the row is still archived, the file is gone
    (folder / "feedback_old.md").write_text(OLD, encoding="utf-8")
    assert ask(conn, folder, new)["history"]
    with db.write_tx(conn):
        conn.execute("UPDATE memories SET deleted_at=? WHERE id=?", (db.utc_now(), old))
    assert refused(ask(conn, folder, new))


def test_an_older_note_edited_after_it_was_archived_is_refused(conn, linked):
    folder, new, _old = linked
    (folder / "feedback_old.md").write_text(OLD.replace("100", "150"), encoding="utf-8")
    assert refused(ask(conn, folder, new))


def test_a_successor_edited_since_the_scan_or_that_dropped_the_link_is_refused(conn, linked):
    folder, new, _old = linked
    (folder / "feedback_new.md").write_text(NEW.replace("200", "250"), encoding="utf-8")
    assert refused(ask(conn, folder, new))
    (folder / "feedback_new.md").write_text(NEW.replace("supersedes: feedback_old\n", ""))
    indexer.scan(conn, [folder])
    assert refused(ask(conn, folder, new))


def test_an_older_note_that_was_never_stored_is_refused(conn, tmp_path):
    # Both notes in one first scan: the older one is never stored, so there is no row.
    folder = folder_of(tmp_path)
    (folder / "feedback_old.md").write_text(OLD, encoding="utf-8")
    (folder / "feedback_new.md").write_text(NEW, encoding="utf-8")
    indexer.scan(conn, [folder])
    assert refused(ask(conn, folder, row_id(conn, "feedback_new.md")))


def test_memory_use_switched_off_refuses(conn, linked):
    folder, new, _old = linked
    LearningLedger(conn).set_controls(NS, ROOT, "", use=False)
    assert refused(ask(conn, folder, new))


def test_one_link_only_no_chain(conn, tmp_path):
    folder = folder_of(tmp_path)
    first = OLD.replace("old", "first")
    second = NEW.replace("feedback_old", "feedback_first").replace("new", "second")
    third = NEW.replace("feedback_old", "feedback_second").replace("new", "third")
    (folder / "feedback_first.md").write_text(first, encoding="utf-8")
    indexer.scan(conn, [folder])
    (folder / "feedback_second.md").write_text(second, encoding="utf-8")
    indexer.scan(conn, [folder])
    (folder / "feedback_third.md").write_text(third, encoding="utf-8")
    indexer.scan(conn, [folder])
    answer = ask(conn, folder, row_id(conn, "feedback_third.md"))
    assert [i["source"] for i in answer["history"]] == ["feedback_second.md"]


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5", "²", "", str(2**63)])
def test_an_invalid_id_is_refused(conn, linked, raw):
    folder, _new, _old = linked
    answer = history.history_answer(conn, raw, NS, ROOT, "", [folder])
    assert answer["history"] == [] and answer["reason"] == rest.BAD_ID_REASON


def test_a_missing_id_and_a_store_failure_are_refused(conn, linked):
    folder, _new, _old = linked
    assert refused(ask(conn, folder, 999_999))
    assert refused(history.history_answer(None, "5", NS, ROOT, "", ()))


# -- the MCP tool -----------------------------------------------------------------------


def _load_mcp():
    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = "hooktest_recall_mcp_history"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()
seen_envs: list[list[dict]] = []


@pytest.fixture
def calls(monkeypatch):
    hook = mcp._load_hook()
    seen: list[tuple] = []
    answers: dict[bool, dict] = {}

    def stub(mid, environ=None, root=None, chunk=None, section="", history=False):
        seen.append((mid, chunk, section, history))
        envs.append(dict(environ or {}))
        return answers[history]

    envs: list[dict] = []
    seen_envs.clear()
    seen_envs.append(envs)

    monkeypatch.setattr(hook, "fetch_memory_text", stub)
    return seen, answers


def test_the_tool_renders_the_older_note_as_historical(tmp_path, calls):
    seen, answers = calls
    answers[True] = {
        "id": 7,
        "reason": None,
        "history": [{"id": 3, "title": "old limit", "source": "a.md", "text": "100 units"}],
    }
    got = mcp.noblivion_recall(environ={"HOME": str(tmp_path)}, fetch_id=7, history=True)
    assert got["isError"] is False and seen == [(7, None, "", True)]
    text = got["content"][0]["text"]
    assert text.startswith("HISTORICAL MEMORY 3, replaced by 7: old limit [a.md]\n")
    assert "GROUNDED MEMORY" not in text and "100 units" in text


def test_the_tool_refuses_an_old_store_and_a_refusal(tmp_path, calls):
    _seen, answers = calls
    env = {"HOME": str(tmp_path)}
    answers[True] = {"id": 7, "title": "new", "text": "current text", "reason": None}
    got = mcp.noblivion_recall(environ=env, fetch_id=7, history=True)
    assert got["isError"] is True and "no historical fetch" in got["content"][0]["text"]
    answers[True] = {"id": 7, "history": [], "reason": history.NO_HISTORY_REASON}
    got = mcp.noblivion_recall(environ=env, fetch_id=7, history=True)
    assert got["isError"] is True and "no older version" in got["content"][0]["text"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fetch_id": [7, 8], "history": True},
        {"history": True},
        {"fetch_id": 7, "history": True, "chunk": 1},
        {"fetch_id": 7, "history": True, "section": "x"},
        {"fetch_id": 7, "history": True, "query": "x"},
        {"fetch_id": 7, "history": "yes"},
        {"fetch_id": "abc", "history": True},
    ],
)
def test_the_tool_refuses_a_bad_history_call(tmp_path, calls, kwargs):
    seen, _answers = calls
    got = mcp.noblivion_recall(environ={"HOME": str(tmp_path)}, **kwargs)
    assert got["isError"] is True and seen == []


def test_without_history_the_fetch_url_and_call_are_unchanged(tmp_path, calls):
    hook = mcp._load_hook()
    assert (
        hook.fetch_url("http://h", 5, "p", "r") == "http://h/api/memories/fetch/5?project=p&root=r"
    )
    assert hook.fetch_url("http://h", 5, "p", "r", None, "", True).endswith("&history=1&root=r")
    seen, answers = calls
    answers[False] = {"id": 7, "title": "new", "text": "current text", "reason": None}
    plain = mcp.noblivion_recall(environ={"HOME": str(tmp_path)}, fetch_id=7)
    explicit = mcp.noblivion_recall(environ={"HOME": str(tmp_path)}, fetch_id=7, history=False)
    assert plain == explicit and plain["content"][0]["text"].startswith("GROUNDED MEMORY 7: new")
    assert [s[3] for s in seen] == [False, False]


def test_history_reaches_the_store_with_the_same_session_env(tmp_path, calls):
    # The store's use controls read the session from this env, so both paths must send the same.
    _seen, answers = calls
    answers[False] = {"id": 7, "title": "new", "text": "current text", "reason": None}
    answers[True] = {"id": 7, "history": [], "reason": history.NO_HISTORY_REASON}
    env = {"HOME": str(tmp_path), "NOBLIVION_RECALL_ROOT": "-proj"}
    mcp.noblivion_recall(environ=dict(env), fetch_id=7)
    mcp.noblivion_recall(environ=dict(env), fetch_id=7, history=True)
    plain, hist = seen_envs[0]
    keys = ("_learning_session_id", "NOBLIVION_RECALL_ROOT", "NOBLIVION_RECALL_PROJECT")
    assert {k: plain.get(k) for k in keys} == {k: hist.get(k) for k in keys}


def test_the_session_hook_passes_history_through():
    path = Path(__file__).resolve().parent.parent / "hooks" / "mcp_session_hook.py"
    spec = importlib.util.spec_from_file_location("hooktest_mcp_session_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.decide(
        {
            "tool_name": "mcp__plugin_noblivion_noblivion__noblivion_recall",
            "tool_input": {"fetch_id": 7, "history": True},
            "session_id": "sess-abc",
        }
    )
    assert out["hookSpecificOutput"]["updatedInput"]["history"] is True
