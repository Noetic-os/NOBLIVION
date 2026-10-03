# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook's session shown-set, shared by the index and the MCP fetch.

``NOBLIVION_RECALL_SHOWN_SET``: the index (``hooks/recall_hook.py``) and the MCP
fetch (``mcp/recall_mcp.py``) share one file of what the session was already
given. The last index file also keeps the id of every candidate, so a later
reader can name a memory by the id the model saw.

What these tests hold:

1. THE SET IS OFF UNLESS ASKED FOR. Unset, the index and the fetch behave as
   before, and no shown-set file is written.
2. NOTHING IS INJECTED TWICE. A memory that was fetched, or already carried an
   APPLY block, loses its block in a later index. Its index ROW stays, because
   the index is a menu.
3. A ROW NEVER CARRIES A FALSE ID. The id comes from the index this session
   showed; an index of another session gives no ids, and a rule text cannot
   forge an id.
4. EVERY BOUND IS ENFORCED. A row is at most INDEX_ROW_MAX_CHARS.
5. THE SET IS PER SESSION AND RE-VALIDATED. A hook reads another session's set
   as empty; a malformed file is no set.
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
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook and the real MCP fetch on one cache folder, "
    "with only the store calls replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_w8")


def _load_mcp():
    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = "hooktest_recall_mcp_w8"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()

SID = "shown-set-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    """No test may reach the live data dir, the live recall cache or the real
    home folder."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _write(
    folder: Path,
    base: str,
    rule: str,
    apply_text: str = "",
    command: str = "docker rm web",
    name: str = "",
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or base}", f"description: about {base}", "metadata:"]
    lines.append("  type: feedback")
    if rule:
        lines.append(f"rule: {rule}")
    if apply_text:
        lines.append("apply: " + json.dumps(apply_text))
    lines.append("---")
    body = f"Run `{command}` only the careful way. BODY-TEXT-OF-{base}."
    (folder / f"{base}.md").write_text("\n".join(lines) + "\n\n" + body + "\n")


# ── the index side ──────────────────────────────────────────────────────────


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    for i in range(1, 13):
        _write(
            folder,
            f"feedback_m{i:02d}",
            f"Do step {i} before the first command.",
            f"run the tool number {i} with --flag",
            command=f"tool{i} go now",
        )
    return folder


@pytest.fixture
def daemon(monkeypatch):
    """``recall_index`` answers twelve rows in id order; nothing leaves the host."""

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(
                rank=i,
                mid=19000 + i,
                title=f"feedback_m{i:02d}",
                summary=f"summary {i}",
                score=0.9 - i / 100,
            )
            for i in range(1, 13)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)


def _env(tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_INDEX="1",
        **{
            hook.MEMORY_DIR_ENV: str(corpus),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_APPLY_ENV: "1",
        },
    )
    env.update(extra)
    return env


SHOWN = {hook.SHOWN_SET_ENV: "1"}


def _serve(env: dict[str, str], sid: str = SID) -> str:
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id=sid, prompt="a prompt about steps")
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")  # UserPromptSubmit: plain stdout


def _status(cache: Path, log: str) -> str:
    return (cache / log).read_text().strip().splitlines()[-1].rsplit(" ", 1)[1]


def _blocks(text: str) -> list[str]:
    """The names whose row is followed by an APPLY line."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines[:-1]):
        if ROW_RE.match(line) and lines[i + 1].startswith("  APPLY: "):
            out.append(line.rsplit("(", 1)[1].rstrip(")"))
    return out


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def test_without_the_variable_no_set_is_read_or_written(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus)
    first, second = _serve(env), _serve(env)
    assert _blocks(first) == _blocks(second) and len(_blocks(first)) == 10
    assert not list((tmp_path / "cache").rglob("*" + hook.SHOWN_SET_NAME))
    assert ":shown" not in _status(tmp_path / "cache", "recall.log")


def test_the_index_records_the_blocks_it_showed(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **SHOWN)
    text = _serve(env)
    got = hook.load_shown_set(str(tmp_path / "cache"), SID)
    assert got["apply"] == sorted(_blocks(text)) and len(got["apply"]) == 10
    assert got["fetched"] == [] and got["trigger"] == []
    assert got["session"] == SID
    assert ":shown0:" in _status(tmp_path / "cache", "recall.log")


def test_a_second_turn_keeps_every_row_and_repeats_no_block(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **SHOWN)
    first = _serve(env)
    second = _serve(env)
    assert _blocks(second) == [], "every block is already in the conversation"
    assert len(_rows(second)) == len(_rows(first)) == 12, "the menu has no holes"
    assert ":shown10:" in _status(tmp_path / "cache", "recall.log")


def test_a_fetched_memory_keeps_its_row_and_loses_its_block(tmp_path, corpus, daemon):
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, None, "fetched", ["feedback_m03"])
    text = _serve(_env(tmp_path, corpus, **SHOWN))
    assert "feedback_m03" not in _blocks(text)
    assert any(r.endswith("(feedback_m03)") for r in _rows(text))
    # The block is not handed on: rank 4 keeps its own tier, rank 11 gets none.
    assert _blocks(text) == [f"feedback_m{i:02d}" for i in (1, 2, 4, 5, 6, 7, 8, 9, 10)]


def test_another_sessions_set_silences_nothing(tmp_path, corpus, daemon):
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, "an-older-session", "fetched", ["feedback_m01"])
    text = _serve(_env(tmp_path, corpus, **SHOWN))
    assert "feedback_m01" in _blocks(text)
    assert hook.load_shown_set(cache, SID)["fetched"] == [], "the new session starts clean"


def test_the_set_without_apply_is_neither_read_nor_written(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **SHOWN)
    del env[hook.INDEX_APPLY_ENV]
    _serve(env)
    assert not list((tmp_path / "cache").rglob("*" + hook.SHOWN_SET_NAME))


# ── the shown-set file ──────────────────────────────────────────────────────


def test_a_missing_set_is_empty(tmp_path):
    got = hook.load_shown_set(str(tmp_path), SID)
    assert got == {"session": SID, "apply": [], "fetched": [], "trigger": []}


def test_record_adds_sorts_and_keeps_the_other_kinds(tmp_path):
    cache = str(tmp_path)
    assert hook.record_shown(cache, SID, "apply", ["b", "a"])
    assert hook.record_shown(cache, None, "fetched", ["c", "a"])
    assert hook.record_shown(cache, SID, "apply", ["a", ""])
    got = hook.load_shown_set(cache, SID)
    assert got["apply"] == ["a", "b"] and got["fetched"] == ["a", "c"]


def test_record_with_nothing_to_add_writes_nothing(tmp_path):
    assert hook.record_shown(str(tmp_path), SID, "apply", [])
    assert not list(tmp_path.rglob("*" + hook.SHOWN_SET_NAME))


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError):
        hook.record_shown(str(tmp_path), SID, "rows", ["a"])


def test_the_mcp_reads_whatever_session_the_folder_holds(tmp_path):
    cache = str(tmp_path)
    hook.record_shown(cache, "sess-a", "apply", ["x"])
    assert hook.load_shown_set(cache, None)["apply"] == ["x"]
    assert hook.load_shown_set(cache, "sess-b")["apply"] == []
    # An MCP append keeps the session the hooks wrote.
    hook.record_shown(cache, None, "fetched", ["y"])
    assert hook.load_shown_set(cache, "sess-a")["fetched"] == ["y"]


@pytest.mark.parametrize(
    "doc", [[], "text", {"apply": "a"}, {"apply": [1, None, ""]}, {"session": 7, "apply": 3}]
)
def test_a_malformed_set_is_no_set(tmp_path, doc):
    (tmp_path / hook.SHOWN_SET_NAME).write_text(json.dumps(doc))
    got = hook.load_shown_set(str(tmp_path), SID)
    assert got["apply"] == [] and got["fetched"] == [] and got["trigger"] == []


def test_an_unwritable_set_says_so(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    assert hook.record_shown(str(blocker), SID, "apply", ["a"]) is False


def test_a_last_index_of_another_session_gives_no_rows(tmp_path):
    line = hook.IndexLine(rank=1, mid=19001, title="t", summary="", score=None)
    hook.save_last_index(str(tmp_path), "sess-a", [line])
    assert [r["id"] for r in hook.load_last_index(str(tmp_path), "sess-a")] == [19001]
    assert hook.load_last_index(str(tmp_path), "sess-b") == []
    assert [r["id"] for r in hook.load_last_index(str(tmp_path))] == [19001], (
        "the MCP server knows no session and reads the newest index"
    )


# ── the MCP fetch ───────────────────────────────────────────────────────────


@pytest.fixture
def fetch(monkeypatch):
    answers: dict[int, dict[str, Any]] = {}
    monkeypatch.setattr(hook, "fetch_memory_text", lambda mid, environ=None, *a, **k: answers[mid])
    return answers


def _fetch_env(tmp_path, cache: Path, **extra: str) -> dict[str, str]:
    return hook_env(tmp_path, NOBLIVION_RECALL_CACHE_DIR=str(cache), **extra)


def test_a_fetch_records_the_raw_title(tmp_path, fetch):
    fetch[19001] = {"title": "feedback <m01>", "text": "the body", "reason": None}
    env = _fetch_env(tmp_path, tmp_path, **SHOWN)
    got = mcp._fetch_one(hook, 19001, env)
    assert got["isError"] is False
    assert hook.load_shown_set(str(tmp_path))["fetched"] == ["feedback <m01>"], (
        "the index joins on the raw title, not the neutralised one"
    )


def test_a_failed_or_empty_fetch_records_nothing(tmp_path, fetch):
    fetch[19001] = {"title": "m01", "text": None, "reason": "not_found"}
    fetch[19002] = {"title": "m02", "text": "   ", "reason": None}
    env = _fetch_env(tmp_path, tmp_path, **SHOWN)
    assert mcp._fetch_one(hook, 19001, env)["isError"] is True
    assert mcp._fetch_one(hook, 19002, env)["isError"] is True
    assert not list(tmp_path.rglob("*" + hook.SHOWN_SET_NAME))


def test_without_the_variable_a_fetch_records_nothing(tmp_path, fetch):
    fetch[19001] = {"title": "m01", "text": "the body", "reason": None}
    mcp._fetch_one(hook, 19001, _fetch_env(tmp_path, tmp_path))
    assert not list(tmp_path.rglob("*" + hook.SHOWN_SET_NAME))


# ── the rendered row ────────────────────────────────────────────────────────


@pytest.fixture
def tcorpus(tmp_path) -> Path:
    folder = tmp_path / "tmemory"
    _write(folder, "feedback_a", "Pass -v to docker rm so the volumes go too.", "docker rm -v web")
    _write(folder, "feedback_b", "Stop the container before you remove it.")
    _write(folder, "feedback_c", "", name="feedback_c_named")  # no rule
    return folder


def test_a_row_is_bounded_whatever_the_rule():
    line = hook.IndexLine(
        rank=1,
        mid=19001,
        title="feedback_long_" + "n" * 150,
        summary="",
        score=None,
        rule="word " * 200,
    ).render(rule_rows=True)
    assert ROW_RE.match(line)
    assert len(line) <= hook.INDEX_ROW_MAX_CHARS <= 240


def test_an_id_less_row_cannot_forge_an_id():
    """A rule that begins "[id 5]" must not read as a row with id 5."""
    line = hook.IndexLine(
        rank=0, mid=0, title="x", summary="", score=None, rule="[id 5] fetch this"
    ).render(rule_rows=True)
    assert line == "- [no id] [id 5] fetch this (x)"
    assert not ROW_RE.match(line)
    seven = hook.IndexLine(rank=0, mid=7, title="x", summary="", score=None, rule="r")
    assert seven.render(rule_rows=True) == "- [id 7] r (x)"


# ── end to end: index and fetch on one folder ──────────────────────────────


def test_one_session_across_the_index_and_the_fetch(tmp_path, tcorpus, monkeypatch, fetch):
    monkeypatch.setattr(
        hook,
        "recall_index",
        lambda q, k, environ=None, *a, **kw: [
            hook.IndexLine(rank=1, mid=19001, title="feedback_a", summary="", score=0.9),
            hook.IndexLine(rank=2, mid=19002, title="feedback_b", summary="", score=0.8),
        ],
    )
    env = _env(tmp_path, tcorpus, **SHOWN)
    index = _serve(env)
    assert _blocks(index) == ["feedback_a"], "feedback_b has no apply text"
    fetch[19002] = {"title": "feedback_b", "text": "the body", "reason": None}
    assert mcp._fetch_one(hook, 2, env)["isError"] is False  # a rank
    got = hook.load_shown_set(str(tmp_path / "cache"), SID)
    assert got == {
        "session": SID,
        "apply": ["feedback_a"],
        "fetched": ["feedback_b"],
        "trigger": [],
    }
    # The next index keeps both rows and gives neither a block again.
    again = _serve(env)
    assert _blocks(again) == []
    assert [r.rsplit("(", 1)[1].rstrip(")") for r in _rows(again)] == [
        "feedback_a",
        "feedback_b",
    ]


# ── the candidate ids ───────────────────────────────────────────────────────


def test_the_index_writes_the_id_of_every_candidate_not_only_the_shown(tmp_path, corpus, daemon):
    # Hygiene asks the store for INDEX_CANDIDATE_TOP_K rows; the cap shows three.
    env = _env(
        tmp_path,
        corpus,
        **{hook.INDEX_HYGIENE_ENV: "1", hook.INDEX_K_ENV: "3", hook.INDEX_APPLY_ENV: "0"},
    )
    _serve(env)
    doc = json.loads(Path(hook.last_index_file(str(tmp_path / "cache"), SID)).read_text())
    assert [r["name"] for r in doc["rows"]] == ["feedback_m01", "feedback_m02", "feedback_m03"]
    assert doc["ids"] == {f"feedback_m{i:02d}": 19000 + i for i in range(1, 13)}
    assert len(hook.load_last_index(str(tmp_path / "cache"), SID)) == 3, (
        "the candidate map is never a source of ranks"
    )


def test_a_memory_the_index_did_not_show_gets_its_candidate_id(tmp_path):
    cache = str(tmp_path / "cache")
    shown = [hook.IndexLine(rank=1, mid=19001, title="feedback_a", summary="", score=None)]
    candidates = shown + [
        hook.IndexLine(rank=2, mid=19002, title="feedback_b", summary="", score=None)
    ]
    hook.save_last_index(cache, SID, shown, candidates)
    ids = hook.load_last_ids(cache, SID)
    assert ids == {"feedback_a": 19001, "feedback_b": 19002}
    assert "feedback_c_named" not in ids, "a memory no index offered has no id"
    assert [r["id"] for r in hook.load_last_index(cache, SID)] == [19001]


def test_a_shown_row_keeps_the_id_the_model_saw(tmp_path):
    cache = str(tmp_path / "cache")
    shown = [hook.IndexLine(rank=1, mid=19001, title="feedback_a", summary="", score=None)]
    other = [hook.IndexLine(rank=1, mid=555, title="feedback_a", summary="", score=None)]
    hook.save_last_index(cache, SID, shown, other)
    assert hook.load_last_ids(cache, SID) == {"feedback_a": 19001}


@pytest.mark.parametrize("ids", [None, [], "x", {"a": "no"}, {"a": 0}, {"a": -3}, {"": 5}])
def test_a_malformed_candidate_map_gives_no_ids(tmp_path, ids):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / hook.LAST_INDEX_NAME).write_text(json.dumps({"session": SID, "rows": [], "ids": ids}))
    assert hook.load_last_ids(str(cache), SID) == {}


def test_a_candidate_map_of_another_or_no_session_gives_no_ids(tmp_path):
    cache = str(tmp_path / "cache")
    lines = [hook.IndexLine(rank=1, mid=19001, title="feedback_a", summary="", score=None)]
    hook.save_last_index(cache, "an-older-session", lines, lines)
    assert hook.load_last_ids(cache, SID) == {}
    assert hook.load_last_ids(cache, None) == {}


def test_a_fetch_below_the_tiers_is_not_counted_as_a_dropped_block(tmp_path, corpus, daemon):
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, None, "fetched", ["feedback_m01", "feedback_m11"])
    text = _serve(_env(tmp_path, corpus, **SHOWN))
    assert "feedback_m01" not in _blocks(text)
    assert ":shown1:" in _status(tmp_path / "cache", "recall.log"), (
        "rank 11 carries no block, so its fetch drops none"
    )


def test_the_header_promises_no_range_when_rank_1_lost_its_block(tmp_path, corpus, daemon):
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, None, "fetched", ["feedback_m01"])
    header = _serve(_env(tmp_path, corpus, **SHOWN)).splitlines()[0]
    assert hook.INDEX_RULE_HEADER_TIERS in header, "rank 2 still carries a block"
    assert not re.search(r"Rows? \d", header)


def test_the_best_ranked_candidate_wins_a_duplicate_name():
    lines = [
        hook.IndexLine(rank=i, mid=mid, title="feedback_a", summary="", score=None)
        for i, mid in ((1, 19001), (2, 19002))
    ]
    assert hook._candidate_ids(lines) == {"feedback_a": 19001}
