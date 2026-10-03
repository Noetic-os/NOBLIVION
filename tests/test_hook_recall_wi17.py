# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: a session's later index does not repeat a row.

``NOBLIVION_RECALL_INDEX_ROW_DEDUPE`` (off unless set, rule shape only): the
first index of a session is whole; a later index of the same session shows only
the rows whose memory was not an index row earlier in that session. The MCP
server (``mcp/recall_mcp.py``) resolves a fetch by position against the index
that was printed last.

What these tests hold:

1. OFF UNLESS ASKED FOR. Unset, every prompt gets the same text as before, the
   header is INDEX_RULE_HEADER, and the shown-set file gets no ``row`` list.
2. THE FIRST PROMPT IS WHOLE, A LATER ONE HAS ONLY NEW ROWS, in rank order, and
   its header says that earlier rows still apply.
3. A NEW SESSION STARTS FRESH.
4. NO NEW ROW, NO OUTPUT, and the last index file is left as it was.
5. A POSITION STILL RESOLVES TO THE ROW AT THAT POSITION after a reduced index.
6. AN APPLY BLOCK THAT WAS SHOWN COUNTS AS SHOWN; a tier stays with its rank.

Every test runs on a tmp data dir, cache folder and guard table path, so no
live file is read or written, and the store call is replaced.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook and the real MCP rank resolution on one tmp "
    "cache folder, with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_wi17")


def _load_mcp():
    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = "hooktest_recall_mcp_wi17"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()

SID = "row-dedupe-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
DEDUPE = {hook.INDEX_ROW_DEDUPE_ENV: "1"}
APPLY = {hook.INDEX_APPLY_ENV: "1"}


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    """No test may reach the live guard table, the live data dir, the live
    recall cache or the real home folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, 21):
        base = f"feedback_m{i:02d}"
        (folder / f"{base}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {base}",
                    f"description: about {base}",
                    "metadata:",
                    "  type: feedback",
                    f"rule: Do step {i} before the first command.",
                    "apply: " + json.dumps(f"run the tool number {i} with --flag"),
                    "---",
                    "",
                    f"BODY-TEXT-OF-{base}.",
                    "",
                ]
            )
        )
    return folder


@pytest.fixture
def daemon(monkeypatch):
    """``recall_index`` answers the memories named in ``order``, in that order.
    A test changes ``order`` between prompts; nothing leaves the host."""
    order: list[int] = list(range(1, 13))

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(
                rank=pos,
                mid=19000 + i,
                title=f"feedback_m{i:02d}",
                summary=f"summary {i}",
                score=0.9 - pos / 100,
            )
            for pos, i in enumerate(order, start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    return order


def _env(tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **{hook.MEMORY_DIR_ENV: str(corpus), hook.INDEX_RULE_ROWS_ENV: "1"},
    )
    env.update(extra)
    return env


def _serve(env: dict[str, str], sid: object = SID) -> str:
    payload = json.dumps(
        dict(
            hook_event_name="UserPromptSubmit",
            session_id=sid,
            prompt="a prompt about steps",
        )
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in text.splitlines() if ROW_RE.match(ln)]


def _m(*nums: int) -> list[str]:
    return [f"feedback_m{i:02d}" for i in nums]


def _status(tmp_path) -> str:
    log = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()
    return log[-1].rsplit(" ", 1)[1]


# ── off unless asked for ────────────────────────────────────────────────────


def test_without_the_variable_every_prompt_is_the_same_bytes(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus)
    first, second = _serve(env), _serve(env)
    assert first == second
    rows = [
        hook.IndexLine(
            rank=i,
            mid=19000 + i,
            title=f"feedback_m{i:02d}",
            summary=f"summary {i}",
            score=None,
            rule=f"Do step {i} before the first command.",
        )
        for i in range(1, 13)
    ]
    want = "\n".join([hook.INDEX_RULE_HEADER.format(tiers="")] + [r.render(True) for r in rows])
    assert first == want, "the text of the release before the row dedupe"
    assert not list((tmp_path / "cache").rglob("*" + hook.SHOWN_SET_NAME))
    assert ":rows" not in _status(tmp_path) and "row_dedupe" not in _status(tmp_path)


def test_without_the_variable_the_shown_set_file_gets_no_row_list(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **APPLY, **{hook.SHOWN_SET_ENV: "1"})
    _serve(env)
    doc = json.loads(Path(hook.shown_set_file(str(tmp_path / "cache"), SID)).read_text())
    assert sorted(doc) == ["apply", "fetched", "session", "trigger"]
    assert hook.SHOWN_ROW_KIND not in hook.load_shown_set(str(tmp_path / "cache"), SID)


def test_the_variable_without_the_rule_shape_is_ignored_and_logged(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE)
    del env[hook.INDEX_RULE_ROWS_ENV]
    first, second = _serve(env), _serve(env)
    assert first == second and first.count("\n") == 12
    assert ":row_dedupe_off:no_rule_rows" in _status(tmp_path)
    assert not list((tmp_path / "cache").rglob("*" + hook.SHOWN_SET_NAME))


def test_without_a_session_id_nothing_is_left_out(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE)
    first, second = _serve(env, sid=None), _serve(env, sid=None)
    assert first == second and len(_names(first)) == 12
    assert ":row_dedupe_off:no_session" in _status(tmp_path)


# ── the first prompt, a later prompt, a new session ─────────────────────────


def test_the_first_prompt_is_the_full_index_with_the_usual_header(tmp_path, corpus, daemon):
    on = _serve(_env(tmp_path, corpus, **DEDUPE))
    assert _names(on) == _m(*range(1, 13))
    assert on.splitlines()[0] == hook.INDEX_RULE_HEADER.format(tiers="")
    assert ":rows12of12" in _status(tmp_path)
    got = hook.load_shown_set(str(tmp_path / "cache"), SID)
    assert got[hook.SHOWN_ROW_KIND] == _m(*range(1, 13)) and got["session"] == SID


def test_the_first_prompt_is_the_same_bytes_with_the_option_on_and_off(tmp_path, corpus, daemon):
    off = _serve(_env(tmp_path, corpus), sid="row-dedupe-off")
    on = _serve(_env(tmp_path, corpus, **DEDUPE), sid="row-dedupe-on")
    assert on == off


def test_a_later_prompt_shows_only_the_new_rows_in_rank_order(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE)
    _serve(env)
    daemon[:] = [15, 3, 14, 1, 13, 7, 16]
    second = _serve(env)
    assert _names(second) == _m(15, 14, 13, 16), "repeats left out, order kept"
    assert second.splitlines()[0] == hook.INDEX_RULE_HEADER_REDUCED.format(tiers="")
    assert ":rows4of7" in _status(tmp_path)
    # The third prompt knows the rows of BOTH earlier prompts.
    daemon[:] = [16, 17, 2, 15, 18]
    assert _names(_serve(env)) == _m(17, 18)


def test_a_new_session_shows_every_row_again(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE)
    first = _serve(env)
    other = _serve(env, sid="row-dedupe-another-session")
    assert other == first and len(_names(other)) == 12
    got = hook.load_shown_set(str(tmp_path / "cache"), "row-dedupe-another-session")
    assert got["session"] == "row-dedupe-another-session"


# ── no new row ──────────────────────────────────────────────────────────────


def test_with_no_new_row_nothing_is_printed(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE)
    _serve(env)
    before = Path(hook.last_index_file(str(tmp_path / "cache"), SID)).read_bytes()
    assert _serve(env) == "", "no header for an empty list"
    status = _status(tmp_path)
    assert ":rows0of12" in status and status.startswith("ok")
    last_log = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]
    assert " hits=0 chars=0 " in last_log
    assert Path(hook.last_index_file(str(tmp_path / "cache"), SID)).read_bytes() == before, (
        "the index the model was last shown is still the first one"
    )


# ── fetch by position ───────────────────────────────────────────────────────


def test_a_position_resolves_to_the_row_at_that_position_of_a_reduced_index(
    tmp_path, corpus, daemon
):
    env = _env(tmp_path, corpus, **DEDUPE)
    _serve(env)
    assert mcp._resolve_fetch_id(hook, 2, env)[0] == 19002
    daemon[:] = [15, 3, 14, 1, 13, 7, 16]
    second = _serve(env)
    shown_ids = [int(m.group("id")) for ln in second.splitlines() if (m := ROW_RE.match(ln))]
    assert shown_ids == [19015, 19014, 19013, 19016]
    rows = hook.load_last_index(str(tmp_path / "cache"), SID)
    assert [(r["rank"], r["id"]) for r in rows] == list(enumerate(shown_ids, start=1))
    for position, mid in enumerate(shown_ids, start=1):
        got, note = mcp._resolve_fetch_id(hook, position, env)
        assert got == mid and f"= id {mid}" in note
    # Position 5 is not on the screen: passed through, never a left-out row.
    assert mcp._resolve_fetch_id(hook, 5, env) == (5, "")
    # An id is always that id, shown now or earlier.
    assert mcp._resolve_fetch_id(hook, 19003, env) == (19003, "")


def test_after_an_empty_index_a_position_is_read_from_the_last_printed_one(
    tmp_path, corpus, daemon
):
    env = _env(tmp_path, corpus, **DEDUPE)
    _serve(env)
    assert _serve(env) == ""
    assert mcp._resolve_fetch_id(hook, 12, env)[0] == 19012


# ── with the APPLY block and the shown-set ──────────────────────────────────


def test_a_memory_whose_apply_block_was_shown_counts_as_shown(tmp_path, corpus, daemon):
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, SID, "apply", _m(2, 5))
    text = _serve(_env(tmp_path, corpus, **DEDUPE))
    assert _names(text) == _m(1, 3, 4, 6, 7, 8, 9, 10, 11, 12)
    assert text.splitlines()[0] == hook.INDEX_RULE_HEADER_REDUCED.format(tiers="")


def test_a_row_keeps_the_tier_of_its_rank_when_rows_above_it_are_left_out(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE, **APPLY, **{hook.SHOWN_SET_ENV: "1"})
    daemon[:] = list(range(1, 11))
    _serve(env)
    daemon[:] = list(range(1, 11)) + [11, 12]  # 11 and 12 rank below the tiers
    second = _serve(env)
    assert _names(second) == _m(11, 12)
    assert "APPLY: " not in second, "rank 11 did not become rank 1"
    daemon[:] = [13] + list(range(1, 11))  # a new memory at rank 1
    third = _serve(env)
    assert _names(third) == _m(13)
    assert "  APPLY: run the tool number 13 with --flag" in third
    got = hook.load_shown_set(str(tmp_path / "cache"), SID)
    assert "feedback_m13" in got["apply"] and "feedback_m13" in got[hook.SHOWN_ROW_KIND]
    assert got[hook.SHOWN_ROW_KIND] == _m(*range(1, 14))


def test_a_fetch_record_keeps_the_row_list(tmp_path, corpus, daemon):
    """The MCP server records a fetch with no session id; the rows must survive."""
    env = _env(tmp_path, corpus, **DEDUPE)
    _serve(env)
    cache = str(tmp_path / "cache")
    assert hook.record_shown(cache, None, "fetched", ["feedback_m03"])
    got = hook.load_shown_set(cache, SID)
    assert got["fetched"] == ["feedback_m03"]
    assert got[hook.SHOWN_ROW_KIND] == _m(*range(1, 13))
    assert _serve(env) == ""


def test_a_row_the_cap_dropped_is_not_recorded_and_comes_back(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DEDUPE, **{hook.INDEX_CAP_ENV: "500"})
    first = _names(_serve(env))
    assert 0 < len(first) < 12
    second = _names(_serve(env))
    assert second and second[0] == _m(len(first) + 1)[0], "the next rows, not a repeat"
    assert not set(first) & set(second)


def test_a_malformed_row_list_is_no_list(tmp_path, corpus, daemon):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / hook.SHOWN_SET_NAME).write_text(
        json.dumps(
            {
                "session": SID,
                "apply": [],
                "fetched": [],
                "trigger": [],
                "row": {"feedback_m01": 1},
            }
        )
    )
    assert hook.SHOWN_ROW_KIND not in hook.load_shown_set(str(cache), SID)
    assert len(_names(_serve(_env(tmp_path, corpus, **DEDUPE)))) == 12


def test_reset_rows_empties_the_shown_set(tmp_path):
    """A compaction drops the earlier rows from the context: --reset-rows makes
    the next index full again: every shown list is emptied."""
    fresh = load_hook("recall_hook", "hooktest_recall_wi17_reset")
    sid = "11111111-2222-3333-4444-555555555555"
    cache = str(tmp_path)
    assert fresh.record_shown(cache, sid, fresh.SHOWN_ROW_KIND, ["a", "b"])
    assert fresh.record_shown(cache, sid, "apply", ["a"])
    env = {"NOBLIVION_RECALL_CACHE_DIR": cache, "NOBLIVION_DATA_DIR": str(tmp_path / "data")}
    assert fresh.main_reset_rows(io.StringIO(json.dumps({"session_id": sid})), env) == 0
    state = fresh.load_shown_set(cache, sid)
    assert not state.get(fresh.SHOWN_ROW_KIND)
    assert state["apply"] == []
    assert fresh.main_reset_rows(io.StringIO("not json"), env) == 0
