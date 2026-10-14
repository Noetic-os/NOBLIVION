# SPDX-License-Identifier: AGPL-3.0-or-later
"""A learned note carries a description and a rule, so the prompt index shows it."""

import hashlib
import io
import json
import re

import pytest

from hookload import load_hook
from noblivion import db, indexer, rest
from noblivion.learning import LearningLedger
from recall_helpers import INDEX_FLOOR_OFF, hook_env

hook = load_hook("recall_hook", "hooktest_recall_learned_note")

STATEMENT = "Keep progress updates to one short line. Never repeat the full plan."


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def learned_note(tmp_path, statement=STATEMENT):
    """Run one note through the ledger and project it to a memory folder."""
    conn = db.open_db(tmp_path / "learning.db", create=True)
    ledger = LearningLedger(conn)
    sha = hashlib.sha256(statement.encode()).hexdigest()
    ledger.capture_batch(
        {
            "batch_id": "b1",
            "namespace": "test",
            "root": "demo",
            "source_client": "claude",
            "session_id": "s1",
            "records": [
                {
                    "source_id": "src1",
                    "record_id": "r1",
                    "role": "user",
                    "content": statement,
                    "source_hash": sha,
                    "observed_at": "2026-10-12T09:00:00Z",
                    "metadata": {"authority": "direct_user", "direct_user": True},
                }
            ],
            "cursor": {"path_id": "t", "offset": 1},
        }
    )
    job = ledger.claim("worker")
    ledger.finish(
        job["id"],
        job["lease_token"],
        [
            {
                "action": "add",
                "logical_key": "progress-style",
                "category": "preference",
                "title": "Progress format",
                "statement": statement,
                "observed_at": "2026-10-12",
                "effective_at": None,
                "scope": "general",
                "evidence": [
                    {
                        "source_id": "src1",
                        "source_hash": sha,
                        "start": 0,
                        "end": len(statement),
                        "quote": statement,
                    }
                ],
            }
        ],
    )
    folder = tmp_path / "memory"
    ledger.project_pending("test", "demo", folder)
    return conn, ledger, folder


def serve(tmp_path, folder, monkeypatch, summary_from_store=True):
    """Serve the index as the live store would answer for the projected file.

    With ``summary_from_store`` False the store sends no summary, so only the
    local ``rule:`` can give the row its text."""
    (path,) = folder.glob("learned-*.md")
    text = path.read_text()
    parsed = indexer.parse_file(path.name, text)
    title, summary = rest.title_and_summary(indexer.build_content(path.name, parsed), 1)
    summary = summary if summary_from_store else ""
    monkeypatch.setattr(
        hook,
        "recall_index",
        lambda query, k, *a, **kw: [
            hook.IndexLine(rank=1, mid=7, title=title, summary=summary, score=0.9)
        ],
    )
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **INDEX_FLOOR_OFF,
    )
    env.update(
        {
            hook.MEMORY_DIR_ENV: str(folder),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_DROP_NO_RULE_ENV: "1",
        }
    )
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id="s", prompt="progress update style")
    )
    out = io.StringIO()
    hook.run(payload, out, env)
    return out.getvalue(), path


def test_learned_note_appears_in_the_index_with_drop_no_rule(tmp_path, monkeypatch):
    conn, _, folder = learned_note(tmp_path)
    try:
        text, path = serve(tmp_path, folder, monkeypatch, summary_from_store=False)
    finally:
        conn.close()
    front = path.read_text().split("---")[1]
    assert re.search(r"^description: Learned preference - Keep progress", front, re.M)
    assert "rule: Keep progress updates to one short line." in front
    rows = [line for line in text.splitlines() if line.startswith("- [id 7]")]
    assert rows and "Keep progress updates to one short line." in rows[0]
    assert "no rule on this memory" not in text


def test_a_note_in_the_old_format_is_dropped_so_the_test_can_fail(tmp_path, monkeypatch):
    conn, _, folder = learned_note(tmp_path)
    conn.close()
    (path,) = folder.glob("learned-*.md")
    old = [
        ln for ln in path.read_text().splitlines() if not ln.startswith(("description:", "rule:"))
    ]
    path.write_text("\n".join(old) + "\n")
    text, _ = serve(tmp_path, folder, monkeypatch, summary_from_store=False)
    assert "[id 7]" not in text


@pytest.mark.parametrize(
    "statement",
    [
        "Note: use #tags - no, never: ever; [brackets] stay.",
        '"Quoted" start and a long tail ' + "word " * 80,
        "- list style start.",
    ],
)
def test_rule_and_description_stay_plain_single_lines(tmp_path, statement):
    conn, _, folder = learned_note(tmp_path, statement)
    conn.close()
    (path,) = folder.glob("learned-*.md")
    meta = indexer.parse_frontmatter(path.read_text())[0]
    assert meta["rule"] and meta["description"]
    assert len(meta["rule"]) <= 160 and len(meta["description"]) <= 200
    for key in ("rule", "description"):
        assert ": " not in meta[key] and " #" not in meta[key]
        assert meta[key][0] not in "\"'[]{}&*!|>%@`?-,"


def test_an_existing_note_gets_the_new_fields_on_the_next_projection(tmp_path):
    conn, ledger, folder = learned_note(tmp_path)
    try:
        (path,) = folder.glob("learned-*.md")
        old = [
            ln
            for ln in path.read_text().splitlines()
            if not ln.startswith(("description:", "rule:"))
        ]
        path.write_text("\n".join(old) + "\n")
        old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        conn.execute("UPDATE learning_notes SET file_hash=?", (old_hash,))
        conn.execute("UPDATE learning_outbox SET status='indexed'")
        assert ledger.project_pending("test", "demo", folder)["written"] == 1
        assert "rule: Keep progress" in path.read_text()
        assert ledger.project_pending("test", "demo", folder)["written"] == 0
    finally:
        conn.close()
