# SPDX-License-Identifier: AGPL-3.0-or-later
"""The prompt recall path: the budget split, the hit path's hygiene, the MCP
query excerpt and the join of a store row to its local file (NOBLIVION-109).

1. The index cap is split between the rows and the apply blocks. The rows get a
   guaranteed share, a block that does not fit is left out and its row stays,
   and the log says how many rows the cap cut.
2. The hit path (the default, with no index switch) drops what the client
   already loads, ``MEMORY.md`` and ``topic_*``, and gives a hit its file's
   ``rule:`` and ``apply:``.
3. The MCP tool in query mode shows the part of a memory that holds the query
   word, not its first 200 characters.
4. A row joins its file on the path the store returns, so a name of 130
   characters, which the store cuts to 120, still finds its file.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook
from recall_helpers import INDEX_FLOOR_OFF, FakeStore, hook_env

hook = load_hook("recall_hook", "hooktest_recall_path")

_MCP_PATH = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"


def _load_mcp(alias: str):
    spec = importlib.util.spec_from_file_location(alias, _MCP_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp("mcptest_recall_path")


# -- 1. the budget split --------------------------------------------------------------


def _long_rows(n: int) -> list:
    """``n`` rows of about 216 characters, each with 20 lines of apply text."""
    return [
        hook.IndexLine(
            rank=i,
            mid=19000 + i,
            title=f"memory_about_a_long_name_{i:02d}_for_the_test_x",
            summary="s",
            score=0.9,
            rule=f"Rule words for step {i}. " * 14,
            apply_block="\n".join(["run the tool with the flag, then check the result"] * 20),
        )
        for i in range(1, n + 1)
    ]


def _layout(cap: int, n: int = 30):
    return hook.layout_index(_long_rows(n), 5, cap, "claude_code", True, tuple(hook.APPLY_TIERS))


def test_thirty_rows_with_long_apply_text_keep_at_least_eighteen_rows_in_9000():
    text, shown, with_block = _layout(9000)
    assert len(text) <= 9000
    assert len(shown) >= 18
    assert [ln.rank for ln in shown] == list(range(1, len(shown) + 1)), "a prefix of the ranking"
    assert with_block, "the top rows still carry their apply text"


def test_the_rows_keep_their_share_when_the_blocks_would_take_the_cap():
    """At 6000 the blocks of the top ten rows alone are 3,100 characters. Taken
    in rank order with their rows, they left room for 11 rows. The rows' share
    of 60 percent holds 16."""
    text, shown, with_block = _layout(6000)
    assert len(text) <= 6000
    assert len(shown) >= 14
    assert len(with_block) < 10, "the blocks gave way, the rows did not"


def test_a_block_is_whole_or_absent_and_marks_only_its_own_row():
    text, shown, with_block = _layout(6000)
    rendered = text.split("\n")
    first_lines = [ln for ln in rendered if ln.startswith("  APPLY: ")]
    assert len(first_lines) == len(with_block)
    for ln in with_block:
        assert ln in shown


def test_without_tiers_the_cap_is_the_old_greedy_prefix():
    rows = _long_rows(30)
    text, shown, with_block = hook.layout_index(rows, 5, 3000, "claude_code", True, ())
    assert with_block == []
    assert len(text) <= 3000
    assert [ln.rank for ln in shown] == list(range(1, len(shown) + 1))
    # the next row would not have fitted
    assert len(text) + 1 + len(rows[len(shown)].render(True)) > 3000


def test_render_index_capped_still_returns_text_and_rows():
    text, shown = hook.render_index_capped(
        _long_rows(30), 5, 9000, "claude_code", True, tuple(hook.APPLY_TIERS)
    )
    assert text.startswith("MEMORY RULES ranked") and len(shown) >= 18


# -- the index served end to end, against a fake store ---------------------------------


class _Daemon:
    def __init__(self, data_dir: Path):
        self.store = FakeStore(data_dir)
        self.index_rows: list[dict[str, Any]] = []
        self.store.route("/api/memories/index", self._index)

    def _index(self, path: str, query: dict) -> Any:
        top_k = int((query.get("top_k") or ["0"])[0])
        rows = [dict(r) for r in self.index_rows[:top_k]]
        return {"namespace": (query.get("project") or [None])[0], "reason": None, "results": rows}


@pytest.fixture
def daemon(tmp_path):
    state = _Daemon(tmp_path / "data")
    try:
        yield state
    finally:
        state.store.stop()


def _write(folder: Path, base: str, name: str, rule: str = "", apply_text: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: a description", "metadata:", "  type: feedback"]
    if rule:
        lines.append(f"rule: {rule}")
    if apply_text:
        lines.append("apply: " + json.dumps(apply_text))
    lines.append("---")
    (folder / base).write_text("\n".join(lines) + "\n\nBody.\n")


def _serve(env: dict[str, str], prompt: str = "a prompt about steps") -> str:
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id="path-test", prompt=prompt)
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _log_status(env: dict[str, str]) -> str:
    log = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log"
    return log.read_text().strip().splitlines()[-1].rsplit(" ", 1)[1]


def test_the_log_says_how_many_rows_the_cap_cut(daemon, tmp_path):
    folder = tmp_path / "memory"
    for i in range(1, 31):
        base = f"mem_{i:02d}.md"
        _write(folder, base, f"mem_{i:02d}", f"Rule words for step {i}. " * 14)
        daemon.index_rows.append(
            {
                "rank": i,
                "id": 19000 + i,
                "title": f"mem_{i:02d}",
                "summary": f"summary {i}",
                "score": 0.9 - i / 100,
                "source": base,
            }
        )
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_TIMEOUT_S="5.0",
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_RECALL_INDEX_RULE_ROWS="1",
        NOBLIVION_RECALL_INDEX_MAX_CHARS="3000",
        **{hook.MEMORY_DIR_ENV: str(folder)},
        **INDEX_FLOOR_OFF,
    )
    text = _serve(env)
    shown = sum(1 for ln in text.split("\n") if ln.startswith("- [id "))
    assert 0 < shown < 30
    assert f":capcut{30 - shown}of30" in _log_status(env)


# -- 4. the join ----------------------------------------------------------------------


def test_a_name_of_130_characters_joins_to_its_file_on_the_path():
    long_name = "feedback_" + "n" * 121
    assert len(long_name) == 130
    cut_title = long_name[:119] + "…"  # what the store keeps of it
    files = hook.corpus_by_name(
        [
            hook.MemoryFile(
                "feedback_long.md", long_name, "feedback", "", "Body.", ["body"], "Rule.", "Do it."
            )
        ]
    )
    row = hook.IndexLine(1, 19001, cut_title, "", 0.9, source="feedback_long.md")
    assert files.get(cut_title) is None, "the old join, by title, finds nothing"
    md = hook.file_of(row, files)
    assert md is not None and md.rel_path == "feedback_long.md"
    assert hook.annotate_index([row], files) == 1
    assert (row.rule, row.apply_block) == ("Rule.", "Do it.")


def test_two_files_of_one_name_each_join_on_their_own_path():
    a = hook.MemoryFile("a.md", "same name", "feedback", "", "Body.", ["a"], "Rule A.")
    b = hook.MemoryFile("b.md", "same name", "feedback", "", "Body.", ["b"], "Rule B.")
    files = hook.corpus_by_name([a, b])
    row_b = hook.IndexLine(1, 2, "same name", "", 0.9, source="b.md")
    row_a = hook.IndexLine(2, 1, "same name", "", 0.8, source="a.md")
    hook.annotate_index([row_a, row_b], files)
    assert (row_a.rule, row_b.rule) == ("Rule A.", "Rule B.")


def test_a_row_with_no_source_still_joins_on_the_title():
    files = hook.corpus_by_name(
        [hook.MemoryFile("a.md", "a name", "feedback", "", "Body.", ["a"], "Rule A.")]
    )
    row = hook.IndexLine(1, 1, "a name", "", 0.9)
    assert hook.file_of(row, files) is files["a name"]
    assert hook.file_of(row, {"a name": files["a name"]}) is files["a name"], "a plain dict"


def test_rerank_counts_a_row_joined_on_the_path():
    long_name = "n" * 130
    md = hook.MemoryFile("one.md", long_name, "feedback", "", "words here", ["words", "here"])
    other = hook.MemoryFile("two.md", "two", "feedback", "", "other text", ["other", "text"])
    corpus = [md, other]
    files = hook.corpus_by_name(corpus)
    lines = [
        hook.IndexLine(1, 11, "n" * 119 + "…", "", 0.5, source="one.md"),
        hook.IndexLine(2, 12, "two", "", 0.4, source="two.md"),
    ]
    bm25 = hook.BM25([m.tokens for m in corpus])
    _rows, joined = hook.rerank_index(lines, "words", corpus, files, bm25)
    assert joined == 2


def test_parse_index_keeps_the_source():
    rows = hook.parse_index(
        {"results": [{"id": 5, "title": "t", "summary": "", "score": 0.5, "source": " a.md "}]}
    )
    assert rows[0].source == "a.md"
    assert hook.renumber(rows)[0].source == "a.md"


def test_the_index_rule_row_of_a_long_name_comes_from_its_file(daemon, tmp_path):
    folder = tmp_path / "memory"
    long_name = "feedback_" + "q" * 121
    _write(folder, "feedback_long.md", long_name, "Do the long thing first.")
    daemon.index_rows.append(
        {
            "rank": 1,
            "id": 19001,
            "title": long_name[:119] + "…",
            "summary": "a summary",
            "score": 0.9,
            "source": "feedback_long.md",
        }
    )
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_TIMEOUT_S="5.0",
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_RECALL_INDEX_RULE_ROWS="1",
        **{hook.MEMORY_DIR_ENV: str(folder)},
        **INDEX_FLOOR_OFF,
    )
    text = _serve(env)
    assert "Do the long thing first." in text
    assert _log_status(env) == "ok:ruled1of1"


# -- 2. the hit path ------------------------------------------------------------------


def _entry(base: str, name: str, body: str) -> str:
    return f"# {name}\n\ndescription of {name}\n[claude_code_md: {base}]\n\n{body}"


@pytest.fixture
def hit_store(tmp_path):
    state = {"blob": ""}
    store = FakeStore(tmp_path / "data")
    store.route(
        "/api/memories/search",
        lambda path, query: {
            "namespace": (query.get("project") or [None])[0],
            "results": [state["blob"]],
        },
    )
    try:
        yield state
    finally:
        store.stop()


def _hit_env(tmp_path, folder: Path | None) -> dict[str, str]:
    extra = {hook.MEMORY_DIR_ENV: str(folder)} if folder is not None else {}
    return hook_env(tmp_path, NOBLIVION_RECALL_TIMEOUT_S="5.0", **extra)


def test_the_hit_path_drops_the_memory_index_and_topic_files(hit_store, tmp_path):
    folder = tmp_path / "memory"
    _write(folder, "feedback_keep.md", "feedback_keep", "Keep this rule.", "Do the kept thing.")
    hit_store["blob"] = hook.ENTRY_SEPARATOR.join(
        [
            _entry("MEMORY.md", "memory index", "- [a](a.md) the index the client loads"),
            _entry("topic_things.md", "topic things", "- [b](b.md) a topic list"),
            _entry("feedback_keep.md", "feedback_keep", "steps text"),
        ]
    )
    env = _hit_env(tmp_path, folder)
    text = _serve(env, "steps")
    assert "feedback_keep" in text
    assert "memory index" not in text and "MEMORY.md" not in text
    assert "topic things" not in text
    assert ":hygiene2" in _log_status(env)


def test_the_hit_path_drops_them_with_no_memory_folder_too(hit_store, tmp_path):
    hit_store["blob"] = hook.ENTRY_SEPARATOR.join(
        [
            _entry("MEMORY.md", "memory index", "the index"),
            _entry("reference_x.md", "reference_x", "steps text"),
        ]
    )
    env = _hit_env(tmp_path, None)
    text = _serve(env, "steps")
    assert "reference_x" in text and "memory index" not in text


def test_the_hit_path_does_not_let_a_dropped_hit_take_a_place(hit_store, tmp_path):
    folder = tmp_path / "memory"
    entries = [_entry("MEMORY.md", "memory index", "the index")] + [
        _entry(f"note_{i}.md", f"note_{i}", f"steps {i}") for i in range(hook.K_PROMPT + 1)
    ]
    hit_store["blob"] = hook.ENTRY_SEPARATOR.join(entries)
    text = _serve(_hit_env(tmp_path, folder), "steps")
    assert sum(1 for ln in text.split("\n") if ln.startswith("- Memory note_")) == hook.K_PROMPT


def test_the_hit_path_gives_a_hit_its_rule_and_apply(hit_store, tmp_path):
    folder = tmp_path / "memory"
    _write(folder, "feedback_keep.md", "feedback_keep", "Keep this rule.", "Run the kept command.")
    hit_store["blob"] = _entry("feedback_keep.md", "feedback_keep", "steps text")
    text = _serve(_hit_env(tmp_path, folder), "steps")
    assert "Rule: Keep this rule." in text
    assert "Apply: Run the kept command." in text


def test_the_hit_path_drops_a_closed_memory(hit_store, tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "project_done.md").write_text(
        "---\nname: project_done\ndescription: Resolved last week\n---\n\nsteps\n"
    )
    hit_store["blob"] = _entry("project_done.md", "project_done", "steps text")
    text = _serve(_hit_env(tmp_path, folder), "steps")
    assert "project_done" not in text


# -- 3. the MCP query excerpt ---------------------------------------------------------


FILLER = "A line of general advice that says nothing of the point. " * 6


def _mcp_run(monkeypatch, tmp_path, body: str, query: str) -> str:
    h = mcp._load_hook()
    entry = _entry("reference_long.md", "reference_long", body)
    monkeypatch.setattr(
        h, "store_get", lambda build, environ, timeout_s: {"results": [entry], "scores": [0.9]}
    )
    env = {"NOBLIVION_DATA_DIR": str(tmp_path), "HOME": str(tmp_path)}
    result = mcp.noblivion_recall(query, 3, env)
    assert result["isError"] is False, result
    return result["content"][0]["text"]


def test_the_query_excerpt_holds_the_query_term_not_the_file_head(monkeypatch, tmp_path):
    body = "\n".join([FILLER, FILLER, "The kubectl rollout needs the restart flag.", FILLER])
    text = _mcp_run(monkeypatch, tmp_path, body, "kubectl")
    assert "kubectl" in text
    assert "general advice" not in text.split("reference_long:", 1)[1][:60]


def test_the_query_excerpt_reaches_a_term_inside_a_long_line(monkeypatch, tmp_path):
    long_line = FILLER * 3 + "then the zebrafish step runs. " + FILLER
    text = _mcp_run(monkeypatch, tmp_path, long_line, "zebrafish")
    assert "zebrafish" in text


def test_the_query_excerpt_falls_back_to_the_head_when_no_word_matches(monkeypatch, tmp_path):
    text = _mcp_run(monkeypatch, tmp_path, "Head words of the memory. " + FILLER, "unrelatedterm")
    assert "Head words of the memory." in text


def test_query_excerpt_unit():
    body = "first line\nsecond line has the needle word\nthird line"
    out = hook.query_excerpt(body, "needle")
    assert out.startswith("second line has the needle word")
    assert hook.query_excerpt(body, "") == body
