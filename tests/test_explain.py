# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostics explain actual filters without leaking notes or writing state."""

import io
import json
import time

import pytest

from noblivion import __main__, config, db, explain
from noblivion.learning import LearningLedger
from store_helpers import add_memory


@pytest.fixture
def store(tmp_path):
    folder = tmp_path / "data"
    conn = db.open_db(folder / config.DB_FILE, create=True)
    mid = add_memory(conn, "private-title", "secret-body zebra migration rollback")
    other = add_memory(conn, "other-title", "zebra migration", root="-other-root")
    conn.close()
    return folder, mid, other


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_offline_scope_secret_safe_and_no_writes(store, monkeypatch):
    folder, mid, other = store
    before = snapshot(folder)
    monkeypatch.setattr(explain, "live_index", lambda *_: pytest.fail("network called"))
    result = explain.explain("zebra migration", folder=folder, root="-proj-alpha", env={})
    assert result["source"] == "offline_keyword"
    assert result["semantic_scoring"] == "unavailable_no_provider_calls"
    assert [r["id"] for r in result["candidates"]] == [mid]
    assert isinstance(result["candidates"][0]["keyword_score"], float)
    assert other not in [r["id"] for r in result["candidates"]]
    text = json.dumps(result)
    for secret in ("private-title", "secret-body", "zebra migration", str(folder), "-proj-alpha"):
        assert secret not in text
    assert snapshot(folder) == before


def test_use_off_denies_without_retrieval(store, monkeypatch):
    folder, _, _ = store
    conn = db.open_db(folder / config.DB_FILE)
    LearningLedger(conn).set_controls("claude_code", "-proj-alpha", "", use=False)
    conn.close()
    before = snapshot(folder)
    monkeypatch.setattr(explain.ranking.Ranker, "search", lambda *_a, **_k: pytest.fail("read"))
    result = explain.explain("zebra", folder=folder, root="-proj-alpha", env={})
    assert result["status"] == "disabled"
    assert result["candidates"] == []
    assert snapshot(folder) == before


def test_unknown_session_honors_any_session_use_off(store):
    folder, _, _ = store
    conn = db.open_db(folder / config.DB_FILE)
    LearningLedger(conn).set_controls("claude_code", "-proj-alpha", "private-session", use=False)
    conn.close()
    result = explain.explain("zebra", folder=folder, root="-proj-alpha", env={})
    assert result["status"] == "disabled"


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_active_database_refused_before_read(store, suffix):
    folder, _, _ = store
    (folder / (config.DB_FILE + suffix)).write_bytes(b"active")
    before = snapshot(folder)
    with pytest.raises(explain.Unavailable, match="checkpointed"):
        explain.explain("zebra", folder=folder, root="-proj-alpha", env={})
    assert snapshot(folder) == before


@pytest.mark.parametrize(
    "options",
    [
        dict(root=None),
        dict(root="ok", namespace="secret/path"),
        dict(root="ok", seconds=0),
        dict(root="ok", seconds=float("nan")),
    ],
)
def test_invalid_scope_or_budget_refused(store, options):
    with pytest.raises(explain.Unavailable):
        explain.explain("zebra", folder=store[0], env={}, **options)


def test_cli_redacts_parser_and_database_errors(store, capsys, monkeypatch):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(store[0]))
    assert __main__.main(["explain", "private-prompt", "--private-secret"]) == 2
    out = capsys.readouterr().out
    assert "private-prompt" not in out and "private-secret" not in out
    assert __main__.main(["explain", "private-prompt", "--root", "-bad"]) == 2
    assert "private-prompt" not in capsys.readouterr().out


def trace(hook, rows, env):
    result, stream = {}, io.StringIO()
    hook._serve_index(
        "private-prompt",
        "UserPromptSubmit",
        None,
        "/no-write",
        time.monotonic(),
        stream,
        env,
        diagnostic=result,
        candidates_override=rows,
    )
    assert stream.getvalue() == ""
    return result


def test_hook_floor_top_k_and_cap_explain_actual_stages(monkeypatch):
    hook = explain.hook_module()
    for name in ("emit", "log_line", "save_last_index", "record_shown", "prune_cache"):
        if hasattr(hook, name):
            monkeypatch.setattr(hook, name, lambda *_a, **_k: pytest.fail("state write"))

    def rows():
        return [
            hook.IndexLine(
                i, i, "private-name", "private-summary", score=score, model=hook.THRESHOLD_MODEL
            )
            for i, score in enumerate([0.1, 0.9, 0.8], 1)
        ]

    env = {"NOBLIVION_RECALL_INDEX_K": "1", "NOBLIVION_RECALL_INDEX_MIN_SCORE": "0.5"}
    result = trace(hook, rows(), env)
    assert [r["decision"] for r in result["candidates"]] == ["relevance_floor", "shown", "top_k"]
    result = trace(hook, rows(), dict(env, NOBLIVION_RECALL_INDEX_MAX_CHARS="0"))
    assert [r["decision"] for r in result["candidates"]] == [
        "relevance_floor",
        "character_cap",
        "top_k",
    ]
    assert "private" not in json.dumps(result)


def test_hook_hygiene_and_missing_rule(tmp_path, monkeypatch):
    hook = explain.hook_module()
    memory = tmp_path / "-proj-alpha" / "memory"
    memory.mkdir(parents=True)
    (memory / "topic_notes.md").write_text("# Topic\n\nprivate-body\n")
    rows = [
        hook.IndexLine(1, 1, "topic_notes.md", "", score=None, source="topic_notes.md"),
        hook.IndexLine(2, 2, "absent.md", "", score=None),
    ]
    env = {
        "NOBLIVION_MEMORY_DIR": str(memory),
        "NOBLIVION_RECALL_INDEX_HYGIENE": "on",
        "NOBLIVION_RECALL_INDEX_RULE_ROWS": "on",
        "NOBLIVION_RECALL_INDEX_DROP_NO_RULE": "on",
    }
    result = trace(hook, rows, env)
    assert [r["decision"] for r in result["candidates"]] == ["hygiene", "missing_rule"]
    assert set(p.name for p in memory.iterdir()) == {"topic_notes.md"}


def test_explicit_live_uses_server_scores_without_output_text(store, monkeypatch):
    folder, mid, _ = store
    called = []

    def live(*args):
        called.append(args)
        return {"namespace": "claude_code", "mode": "keyword", "results": []}

    monkeypatch.setattr(explain, "live_index", live)
    result = explain.explain("private-prompt", folder=folder, root="-proj-alpha", live=True, env={})
    assert len(called) == 1 and result["source"] == "live_store"
    assert result["candidates"] == []


def test_installed_hook_uses_install_stamp(tmp_path, monkeypatch):
    source = explain.hook_module().__file__
    fake = tmp_path / "site-packages" / "noblivion" / "explain.py"
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(explain, "__file__", str(fake))
    venv = tmp_path / "data" / "venv"
    venv.mkdir(parents=True)
    from pathlib import Path

    (venv / config.INSTALL_STAMP).write_text(
        json.dumps({"plugin_root": str(Path(source).parent.parent)})
    )
    assert callable(explain.hook_module(venv.parent)._serve_index)
    (venv / config.INSTALL_STAMP).write_text("{}")
    with pytest.raises(explain.Unavailable, match="update plugin and store"):
        explain.hook_module(venv.parent)


def test_deadline_interrupts_broad_hook_exception_handling(store, monkeypatch):
    hook = explain.hook_module()
    monkeypatch.setattr(explain, "hook_module", lambda *_: hook)

    def slow(*_a, **_k):
        time.sleep(1)

    monkeypatch.setattr(hook, "_serve_index", slow)
    with pytest.raises(explain.DiagnosticTimeout):
        explain.explain("zebra", folder=store[0], root="-proj-alpha", seconds=0.1, env={})


def test_invalid_live_answer_is_redacted_unavailable(store, monkeypatch, capsys):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(store[0]))
    monkeypatch.setattr(explain, "live_index", lambda *_: {"private-secret": "private-note"})
    assert (
        __main__.main(["explain", "private-prompt", "--root=-proj-alpha", "--live", "--json"]) == 2
    )
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "unavailable"
    assert "private" not in output


def test_chunk_budget_refuses_instead_of_partial_explanation(store, monkeypatch):
    folder, _, _ = store
    monkeypatch.setattr(explain, "MAX_ROWS", 0)
    with pytest.raises(explain.Unavailable, match="row limit"):
        explain.explain("zebra", folder=folder, root="-proj-alpha", env={})
