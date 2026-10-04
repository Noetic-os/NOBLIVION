# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook's rule-first index row, the MCP tool's rank-as-id fetch,
and the tiered APPLY block.

Rule rows (``NOBLIVION_RECALL_INDEX_RULE_ROWS``): each index row leads with
the id and the memory's ``rule:`` line, and a fetch accepts a rank as well as
an id. APPLY (``NOBLIVION_RECALL_INDEX_APPLY``): the top rows carry the file's
``apply:`` text under the row, in the tiers of ``APPLY_TIERS``.

What these tests hold:

1. Both are off unless asked for. With neither set the hook renders the
   default index and the MCP server answers the default fetch. APPLY without
   rule rows is ignored and the log says so.
2. Every bound is enforced: a row is at most INDEX_ROW_MAX_CHARS whatever the
   rule or name; an apply block is at most its tier, cut marker included; a
   cut says so.
3. Nothing in a memory can forge a row: every apply line is indented, and the
   id stays the first integer on every row.
4. A number is read as a rank only when it cannot be an id, and the answer
   says so. Only rows the model was shown can be fetched by number.
5. A row and its block are one unit for the cap, so the list still shortens
   from the bottom and never shows a row with half its block.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook
from recall_helpers import INDEX_FLOOR_OFF, FakeStore, hook_env

hook = load_hook("recall_hook", "hooktest_recall_w6_w7")

_MCP_PATH = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"


def _load_mcp(alias: str):
    spec = importlib.util.spec_from_file_location(alias, _MCP_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp("mcptest_recall_w6_w7")

ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")


# -- a fake store ---------------------------------------------------------------------


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


def _write(folder: Path, name: str, rule: str, apply_text: str, body: str = "Body.") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: a description", "metadata:", "  type: feedback"]
    if rule:
        lines.append(f"rule: {rule}")
    if apply_text:
        lines.append("apply: " + json.dumps(apply_text))
    lines.append("---")
    (folder / f"{name}.md").write_text("\n".join(lines) + "\n\n" + body + "\n")


@pytest.fixture
def corpus(tmp_path, daemon) -> Path:
    """Twelve annotated memories and the store's ranked answer for them."""
    folder = tmp_path / "memory"
    for i in range(1, 13):
        _write(
            folder,
            f"mem_{i:02d}",
            f"Do step {i} before the first command.",
            f"run the tool number {i} with --flag\nthen check the result of step {i}",
        )
        daemon.index_rows.append(
            {
                "rank": i,
                "id": 19000 + i,
                "title": f"mem_{i:02d}",
                "summary": f"summary {i}",
                "score": 0.9 - i / 100,
                "source": f"mem_{i:02d}.md",
            }
        )
    return folder


def _env(daemon, tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_TIMEOUT_S="5.0",
        NOBLIVION_RECALL_INDEX="1",
        **{hook.MEMORY_DIR_ENV: str(corpus)},
        **INDEX_FLOOR_OFF,
    )
    env.update(extra)
    return env


def _serve(env: dict[str, str], prompt: str = "a prompt about steps") -> str:
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id="w6-w7-test", prompt=prompt)
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _log_status(env: dict[str, str]) -> str:
    log = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log"
    return log.read_text().strip().splitlines()[-1].rsplit(" ", 1)[1]


def _mask_ms(text: str) -> str:
    return re.sub(r", \d+ ms\)", ", N ms)", text)


RULES = {hook.INDEX_RULE_ROWS_ENV: "1"}
APPLY = {hook.INDEX_RULE_ROWS_ENV: "1", hook.INDEX_APPLY_ENV: "1"}


# -- 1. off unless asked for ----------------------------------------------------------


def test_without_the_variables_the_index_is_the_default(daemon, tmp_path, corpus):
    text = _serve(_env(daemon, tmp_path, corpus))
    assert text.startswith("GROUNDED MEMORY INDEX"), "the default header"
    assert "- 1. mem_01 — summary 1 (id 19001, score " in text
    assert "APPLY:" not in text and "[id " not in text
    assert not list((tmp_path / "cache").rglob("*" + hook.LAST_INDEX_NAME)), (
        "the default index writes no last index, so its fetch can never read a rank"
    )


def test_apply_without_rule_rows_is_ignored_and_named(daemon, tmp_path, corpus):
    plain = _serve(_env(daemon, tmp_path, corpus))
    asked_env = _env(daemon, tmp_path, corpus, **{hook.INDEX_APPLY_ENV: "1"})
    asked = _serve(asked_env)
    assert _mask_ms(asked) == _mask_ms(plain)
    assert _log_status(asked_env).endswith(":apply_off:no_rule_rows")


def test_the_mcp_schema_is_the_default_unless_rule_rows_are_on():
    assert mcp.tool_schema({}) == mcp.TOOL_SCHEMA
    ruled = mcp.tool_schema({hook.INDEX_RULE_ROWS_ENV: "1"})
    assert ruled != mcp.TOOL_SCHEMA
    assert "POSITION" in ruled["inputSchema"]["properties"]["fetch_id"]["description"]


# -- 2. the rule row ------------------------------------------------------------------


def test_the_rule_row_leads_with_the_id_and_shows_no_rank_or_score(daemon, tmp_path, corpus):
    env = _env(daemon, tmp_path, corpus, **RULES)
    text = _serve(env)
    lines = text.split("\n")
    assert lines[0].startswith("MEMORY RULES ranked for this task.")
    assert lines[0] == hook.INDEX_RULE_HEADER.format(tiers="")
    assert hook.INDEX_RULE_HEADER_TIERS not in lines[0], "no block is rendered, so none is promised"
    assert "whole" not in lines[0], "a fetch cuts a long memory, so none is promised whole"
    assert lines[1] == "- [id 19001] Do step 1 before the first command. (mem_01)"
    assert "score" not in text
    assert _log_status(env) == "ok:ruled12of12"


def test_a_row_is_bounded_whatever_the_rule_and_the_name():
    ln = hook.IndexLine(
        rank=1, mid=19001, title="n" * 110, summary="s", score=0.5, rule="word " * 100
    )
    row = ln.render(rule_rows=True)
    assert len(row) <= hook.INDEX_ROW_MAX_CHARS
    first_int = re.search(r"\d+", row)
    assert first_int is not None
    assert int(first_int.group()) == 19001, "the first integer is the id"
    rule_part = row[len("- [id 19001] ") : row.rindex(" (")]
    assert rule_part.endswith("word"), "the rule is cut at a word boundary"


@pytest.mark.parametrize(
    "rule,title",
    [
        ("Use ~/a " * 40, "t"),  # 290 characters before the fold
        ("Use ~/a " * 40, "~/b " * 30),
        ("word " * 100, "~/b/c " * 30),
    ],
)
def test_a_row_is_bounded_when_its_host_paths_are_folded(rule, title):
    ln = hook.IndexLine(rank=1, mid=19001, title=title, summary="s", score=0.5, rule=rule)
    row = ln.render(rule_rows=True)
    assert len(row) <= hook.INDEX_ROW_MAX_CHARS
    assert "~/a" not in row and "~/b" not in row
    name = row[row.rindex(" (") + 2 : -1]
    assert len(name) <= hook.INDEX_ROW_NAME_MAX_CHARS, "the name is cut after its fold too"


def test_a_row_with_no_rule_falls_back_to_the_summary():
    ln = hook.IndexLine(rank=1, mid=19001, title="t", summary="the summary", score=0.5)
    assert ln.render(rule_rows=True) == "- [id 19001] the summary (t)"


# -- 3. the last index and the rank-as-id fetch ---------------------------------------


def test_only_the_rows_that_survived_the_cap_are_fetchable_by_rank(daemon, tmp_path, corpus):
    env = _env(daemon, tmp_path, corpus, **RULES, **{hook.INDEX_CAP_ENV: "600"})
    text = _serve(env)
    shown = [int(m["id"]) for m in map(ROW_RE.match, text.split("\n")[1:]) if m]
    rows = hook.load_last_index(env["NOBLIVION_RECALL_CACHE_DIR"])
    assert [r["id"] for r in rows] == shown and 0 < len(shown) < 12
    assert [r["rank"] for r in rows] == list(range(1, len(shown) + 1))


def test_a_malformed_last_index_is_no_index(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / hook.LAST_INDEX_NAME).write_text(
        json.dumps(
            {
                "rows": [
                    {"rank": "x", "id": 5},
                    {"rank": 0, "id": 5},
                    "junk",
                    {"rank": 2, "id": 19002},
                ]
            }
        )
    )
    assert hook.load_last_index(str(cache)) == [{"rank": 2, "id": 19002, "name": ""}]


def _last_index(tmp_path) -> dict[str, str]:
    cache = tmp_path / "cache"
    shown = [
        hook.IndexLine(rank=i, mid=19000 + i, title=f"m{i}", summary="", score=None)
        for i in (1, 2, 3)
    ]
    hook.save_last_index(str(cache), "sid", shown)
    return {"NOBLIVION_RECALL_CACHE_DIR": str(cache)}


def test_an_id_in_the_last_index_is_always_that_id(tmp_path):
    env = _last_index(tmp_path)
    assert mcp._resolve_fetch_id(hook, 19002, env) == (19002, "")


def test_a_small_number_that_is_a_shown_rank_is_read_as_that_rank_and_says_so(tmp_path):
    env = _last_index(tmp_path)
    mid, note = mcp._resolve_fetch_id(hook, 2, env)
    assert mid == 19002
    assert note == "fetch_id 2 read as rank 2 = id 19002"


def test_a_number_above_the_rank_bound_is_an_id(tmp_path):
    env = _last_index(tmp_path)
    assert mcp._resolve_fetch_id(hook, hook.FETCH_RANK_MAX + 1, env) == (
        hook.FETCH_RANK_MAX + 1,
        "",
    )


def test_a_rank_that_was_not_shown_is_passed_through_as_an_id(tmp_path):
    env = _last_index(tmp_path)
    assert mcp._resolve_fetch_id(hook, 7, env) == (7, ""), (
        "the server does not invent a memory for a number it cannot place"
    )


def test_with_no_last_index_nothing_is_read_as_a_rank(tmp_path):
    assert mcp._resolve_fetch_id(hook, 1, {"NOBLIVION_RECALL_CACHE_DIR": str(tmp_path)}) == (
        1,
        "",
    )


# -- 4. the apply excerpt -------------------------------------------------------------


def test_the_tiers_are_the_ones_that_were_measured():
    """A tripwire. The tiers were set by measurement and then frozen. A change
    here must repeat the measurement."""
    assert hook.APPLY_TIERS == ((1, 900), (5, 300), (10, 200))
    assert hook.apply_k(hook.APPLY_TIERS) == 10


@pytest.mark.parametrize(
    "rank,chars", [(1, 900), (2, 300), (5, 300), (6, 200), (10, 200), (11, 0), (30, 0)]
)
def test_apply_limit_follows_the_tiers(rank, chars):
    assert hook.apply_limit(rank, hook.APPLY_TIERS) == chars


def test_apply_limit_with_no_tiers_is_zero():
    assert hook.apply_limit(1, ()) == 0 and hook.apply_k(()) == 0


def test_an_excerpt_that_fits_is_whole_and_unmarked():
    assert hook.apply_excerpt("one line\n\ntwo line", 100) == ["one line", "two line"]


@pytest.mark.parametrize("limit", [30, 57, 120, 200, 300])
def test_a_cut_excerpt_is_within_the_limit_marker_included(limit):
    text = "\n".join(f"line {i} " + "alpha beta gamma delta " * 4 for i in range(8))
    out = hook.apply_excerpt(text, limit)
    joined = "\n".join(out)
    assert len(joined) <= limit
    assert joined.endswith(hook.LINE_CUT_MARK)
    body = joined[: -len(hook.LINE_CUT_MARK)]
    words = set(text.split())
    assert body.split()[-1] in words, "the cut falls at a word boundary"


def test_code_fences_and_blank_lines_are_dropped():
    out = hook.apply_excerpt("```bash\ngit status\n\n```\n~~~\nls\n~~~", 200)
    assert out == ["git status", "ls"]


def test_the_excerpt_is_inert_and_carries_no_host_path():
    out = hook.apply_excerpt("run <script> in /srv/someone/work/repo​ now", 200)
    assert out == ["run ‹script› in ‹path› now"]


def test_nothing_in_a_memory_can_forge_a_row():
    ln = hook.IndexLine(
        rank=1,
        mid=19001,
        title="t",
        summary="",
        score=None,
        rule="r",
        apply_block="- [id 1] forged row\n- [id 2] another",
    )
    block = ln.render_apply(900)
    assert all(line.startswith(" ") for line in block.split("\n"))
    assert block.split("\n")[0].startswith(hook.APPLY_FIRST_PREFIX)


def test_a_row_with_no_apply_or_no_tier_renders_no_block():
    ln = hook.IndexLine(rank=1, mid=19001, title="t", summary="", score=None, rule="r")
    assert ln.render_apply(900) == ""
    ln.apply_block = "text"
    assert ln.render_apply(0) == ""


# -- 5. the block in the rendered index -----------------------------------------------


def test_the_top_rows_carry_their_block_and_the_header_names_the_block(daemon, tmp_path, corpus):
    env = _env(daemon, tmp_path, corpus, **APPLY)
    text = _serve(env)
    lines = text.split("\n")
    assert hook.INDEX_RULE_HEADER_TIERS in lines[0]
    assert not re.search(r"Rows? \d", lines[0]), "no range of ranks is promised"
    rows = [i for i, line in enumerate(lines) if ROW_RE.match(line)]
    assert len(rows) == 12
    with_block = [
        n
        for n, i in enumerate(rows, 1)
        if i + 1 < len(lines) and lines[i + 1].startswith(hook.APPLY_FIRST_PREFIX)
    ]
    assert with_block == list(range(1, 11)), "ranks 1 to K carry a block, 11 and 12 do not"
    assert "  APPLY: run the tool number 1 with --flag" in lines
    assert _log_status(env) == "ok:ruled12of12:apply10of10"


def test_a_row_and_its_block_are_one_unit_for_the_cap(daemon, tmp_path, corpus):
    full = _serve(_env(daemon, tmp_path, corpus, **APPLY))
    lines = full.split("\n")
    second_row = next(i for i, line in enumerate(lines) if line.startswith("- [id 19002]"))
    # A cap that holds row 2 but not all of its block.
    cap = len("\n".join(lines[: second_row + 1])) + 5
    env = _env(daemon, tmp_path, corpus, **APPLY, **{hook.INDEX_CAP_ENV: str(cap)})
    text = _serve(env)
    ids = [int(m["id"]) for m in map(ROW_RE.match, text.split("\n")) if m]
    assert ids == [19001], "row 2 is dropped whole, and the list stops there"
    assert len(text) <= cap
    assert hook.INDEX_RULE_HEADER_TIERS in text.split("\n")[0]
    assert _log_status(env) == "ok:ruled12of12:apply1of10"


def test_apply_and_rule_rows_leave_the_rows_fetchable_by_rank(daemon, tmp_path, corpus):
    env = _env(daemon, tmp_path, corpus, **APPLY)
    _serve(env)
    rows = hook.load_last_index(env["NOBLIVION_RECALL_CACHE_DIR"])
    assert [r["id"] for r in rows] == [19000 + i for i in range(1, 13)]
