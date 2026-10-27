# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: a row in the top of the fused order survives the floor (NOBLIVION-130).

The relevance floor cuts a row whose store cosine is under it. A row that the
fused order (BM25 + cosine, the local re-rank) puts in the top 30 stays when
(a) its file has a ``rule:`` or ``apply:`` line and (b) its local BM25 value is
above zero. the cosine is at most ``recall.floor_exempt_margin`` (0.08) under the floor.
The switch ``NOBLIVION_RECALL_FLOOR_EXEMPT`` is OFF by default.

What these tests hold:

1. THE ROW STAYS: under the floor, in the top 30, with a rule and a shared word.
2. THE ROW IS STILL CUT when it has no rule or apply line, when it shares no
   word with the query (a pure vector near-miss), when it stands past the
   top 30, or when its cosine is more than the margin under the floor.
   The no-rule drop still drops a row with no text.
3. THE SIZE DOES NOT GROW: the cut to k and the character cap run after the
   exemption, so the same number of rows (and no more characters) are shown.
4. THE SWITCH: off by default and off, the floor alone; read from the env and
   from the config key. The margin: default, env, config key, malformed value.

Every test runs on a tmp data dir, cache folder and guard table path, and the
store call is replaced, so no live file is read or written.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp cache folder and a tmp memory folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_floor_exempt")

SID = "floor-exempt-test-session"
PROMPT = "deploy rollback"
FLOOR = hook.INDEX_MIN_SCORE_ENV
ON = {hook.FLOOR_EXEMPT_ENV: "1"}
OFF = {hook.FLOOR_EXEMPT_ENV: "0"}
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")

# name -> (cosine, kind). The query words are "deploy" and "rollback".
#   top    a rule, at or above the floor, no query word
#   hit    a rule, UNDER the floor, a query word in the file   -> exempt
#   near   a rule, under the floor, NO query word (vector near-miss) -> cut
#   bare   NO rule or apply line, under the floor, a query word      -> cut
#   low    a rule, under the floor, no query word                    -> cut
ROWS: list[tuple[str, float, str]] = [
    ("feedback_top1", 0.80, "top"),
    ("feedback_top2", 0.78, "top"),
    ("feedback_hit1", 0.66, "hit"),
    ("feedback_near1", 0.65, "near"),
    ("feedback_bare1", 0.64, "bare"),
    ("feedback_top3", 0.70, "top"),
    ("feedback_hit2", 0.62, "hit"),
    ("feedback_low1", 0.50, "low"),
    ("feedback_low2", 0.40, "low"),
]


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _write(folder: Path, name: str, kind: str) -> None:
    fm = [f"name: {name}", f"description: about {name}", "metadata:", "  type: feedback"]
    if kind != "bare":
        fm += [f"rule: Keep {name} in mind.", "apply: " + json.dumps(f"check {name} first")]
    word = " deploy rollback" if kind in ("hit", "bare") else ""
    (folder / f"{name}.md").write_text(
        "---\n" + "\n".join(fm) + f"\n---\n\nBody of {name}{word}.\n"
    )


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir()
    for name, _score, kind in ROWS:
        _write(folder, name, kind)
    for i in range(6):  # more files, so the query words are rare in the corpus
        _write(folder, f"feedback_filler{i}", "low")
    return folder


@pytest.fixture
def daemon(monkeypatch):
    """``recall_index`` answers ``rows`` in order. Nothing leaves the host."""
    state = {"rows": list(ROWS), "asked": []}

    def fake(query, k, environ=None, *args, **kwargs):
        state["asked"].append(k)
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=name, summary="", score=score)
            for pos, (name, score, _kind) in enumerate(state["rows"], start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    return state


def _env(tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **{
            FLOOR: "0.68",
            hook.MEMORY_DIR_ENV: str(corpus),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_RERANK_ENV: "1",
            **ON,
        },
    )
    env.update(extra)
    return env


def _serve(env: dict[str, str], prompt: str = PROMPT) -> str:
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt=prompt))
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _names(text: str) -> set[str]:
    return {ln.rsplit("(", 1)[1].rstrip(")") for ln in _rows(text)}


def _status(tmp_path) -> str:
    return (
        (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1].rsplit(" ", 1)[1]
    )


def _no_ms(text: str) -> str:
    return re.sub(r"\b\d+ ms\b", "N ms", text)


# ── the row stays ───────────────────────────────────────────────────────────


def test_a_ruled_row_with_a_shared_word_stays_under_the_floor(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus))
    assert {"feedback_hit1", "feedback_hit2"} <= _names(text)
    assert _status(tmp_path).startswith("ok:") and ":floor_exempt2" in _status(tmp_path)


def test_the_rows_at_or_above_the_floor_stay_as_before(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus))
    assert {"feedback_top1", "feedback_top2", "feedback_top3"} <= _names(text)


# ── the row is still cut ────────────────────────────────────────────────────


def test_a_pure_vector_near_miss_with_no_shared_word_is_still_cut(tmp_path, corpus, daemon):
    names = _names(_serve(_env(tmp_path, corpus)))
    assert "feedback_near1" not in names and "feedback_low1" not in names


def test_a_row_with_no_rule_or_apply_line_is_still_cut(tmp_path, corpus, daemon):
    names = _names(_serve(_env(tmp_path, corpus)))
    assert "feedback_bare1" not in names, "a shared word, but no rule: the floor cuts it"


def test_the_no_rule_drop_still_drops_a_row_with_no_text(tmp_path, corpus, daemon):
    # A row the floor lets pass but that has no rule and no summary is dropped.
    daemon["rows"] = [("feedback_bare1", 0.90, "bare"), ("feedback_top1", 0.80, "top")]
    env = _env(tmp_path, corpus, **{hook.INDEX_DROP_NO_RULE_ENV: "1"})
    assert _names(_serve(env)) == {"feedback_top1"}
    assert ":norule_drop1" in _status(tmp_path)


def test_a_row_without_a_local_file_is_cut(tmp_path, corpus, daemon):
    daemon["rows"] = ROWS + [("feedback_nofile", 0.60, "hit")]
    assert "feedback_nofile" not in _names(_serve(_env(tmp_path, corpus)))


def test_a_word_of_more_than_half_the_notes_is_no_shared_word(tmp_path, corpus, daemon):
    # "body", "of" and "keep" are in nearly every note: a prompt made of them
    # shares "a word" with every note, and would exempt every row.
    names = _names(_serve(_env(tmp_path, corpus), prompt="keep in mind the body of it"))
    assert names == {"feedback_top1", "feedback_top2", "feedback_top3"}
    assert "floor_exempt" not in _status(tmp_path)


def test_bm25_lists_the_words_of_more_than_half_the_documents():
    docs = [["the", "alpha"], ["the", "beta"], ["the", "gamma"]]
    bm = hook.BM25(docs)
    assert bm.common == frozenset({"the"})
    assert hook.BM25([["a"], ["b"], ["c"]]).common == frozenset()


def test_without_the_rerank_nothing_is_exempt(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **{hook.INDEX_RERANK_ENV: "0"})
    names = _names(_serve(env))
    assert "feedback_hit1" not in names and "feedback_hit2" not in names
    assert "floor_exempt" not in _status(tmp_path)


# ── the unit: the position and the conditions ───────────────────────────────


def _line(mid: int, score, rule="r", apply_block="", lexical=1.0):
    ln = hook.IndexLine(rank=mid, mid=mid, title=f"t{mid}", summary="", score=score)
    ln.rule, ln.apply_block, ln.lexical = rule, apply_block, lexical
    return ln


def test_the_top_constant_is_the_row_cap():
    assert hook.FLOOR_EXEMPT_TOP == 30


def test_a_row_at_position_30_is_exempt_and_at_position_31_is_not():
    lines = [_line(i, 0.9) for i in range(1, 30)] + [_line(100, 0.6), _line(101, 0.6)]
    assert hook.floor_exempt_ids(lines, 0.68, 30) == {100}
    assert lines[29].mid == 100 and lines[30].mid == 101  # positions 30 and 31 (1-based)


def test_an_apply_line_alone_is_enough_and_no_line_is_not():
    lines = [
        _line(1, 0.6, rule="", apply_block="do it"),
        _line(2, 0.6, rule="", apply_block=""),
        _line(3, 0.6, rule="   ", apply_block="  "),
    ]
    assert hook.floor_exempt_ids(lines, 0.68, 30) == {1}


def test_a_zero_or_missing_lexical_value_is_not_exempt():
    lines = [_line(1, 0.6, lexical=0.0), _line(2, 0.6, lexical=None), _line(3, 0.6, lexical=0.1)]
    assert hook.floor_exempt_ids(lines, 0.68, 30) == {3}


def test_a_row_at_or_above_the_floor_or_with_no_score_is_not_listed():
    lines = [_line(1, 0.68), _line(2, 0.9), _line(3, None)]
    assert hook.floor_exempt_ids(lines, 0.68, 30) == set()


def test_floor_index_keeps_the_exempt_ids_in_order():
    lines = [_line(1, 0.9), _line(2, 0.6), _line(3, 0.4), _line(4, 0.3, lexical=0.0)]
    kept, unscored = hook.floor_index(lines, 0.68, {3})
    assert [int(ln.mid) for ln in kept] == [1, 3] and unscored == 0
    assert [int(ln.mid) for ln in hook.floor_index(lines, 0.68)[0]] == [1]


# ── the size does not grow ──────────────────────────────────────────────────


def test_k_and_the_row_count_are_unchanged(tmp_path, corpus, daemon):
    k = {hook.INDEX_K_ENV: "3"}
    on = _rows(_serve(_env(tmp_path, corpus, **k)))
    off = _rows(_serve(_env(tmp_path, corpus, **k, **OFF)))
    assert len(on) == len(off) == 3
    assert on != off, "the exempt rows take places of lower rows"


def test_the_character_cap_is_unchanged(tmp_path, corpus, daemon):
    cap = {hook.INDEX_CAP_ENV: "420"}
    on = _serve(_env(tmp_path, corpus, **cap))
    off = _serve(_env(tmp_path, corpus, **cap, **OFF))
    assert 0 < len(on) <= 420 and 0 < len(off) <= 420


def test_the_exemption_never_shows_more_rows_than_the_candidates_over_the_floor_would(
    tmp_path, corpus, daemon
):
    on = _rows(_serve(_env(tmp_path, corpus)))
    off = _rows(_serve(_env(tmp_path, corpus, **OFF)))
    assert len(on) - len(off) == 2, "exactly the two exempt rows are added when k does not bind"
    assert all(r in on for r in off), "no row of the old list is lost"


# ── the switch ──────────────────────────────────────────────────────────────


def test_the_switch_is_off_by_default():
    assert hook.floor_exempt_on({}) is False
    assert hook.floor_exempt_on({hook.FLOOR_EXEMPT_ENV: ""}) is False
    assert hook.floor_exempt_on({hook.FLOOR_EXEMPT_ENV: "1"}) is True


def test_unset_the_text_is_the_floor_alone(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus)
    del env[hook.FLOOR_EXEMPT_ENV]
    names = _names(_serve(env))
    assert names == {"feedback_top1", "feedback_top2", "feedback_top3"}
    assert "floor_exempt" not in _status(tmp_path)


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_the_switch_off_gives_the_old_floor_back(tmp_path, corpus, daemon, raw):
    plain = _env(tmp_path, corpus)
    plain[hook.FLOOR_EXEMPT_ENV] = raw
    text = _no_ms(_serve(plain))
    names = _names(text)
    assert names == {"feedback_top1", "feedback_top2", "feedback_top3"}
    assert "floor_exempt" not in _status(tmp_path)
    assert ":floor3of9" in _status(tmp_path)


def test_the_switch_reads_the_config_key(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"floor_exempt": True}}))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(cfg))
    assert hook.floor_exempt_on({"NOBLIVION_CONFIG": str(cfg)}) is True
    assert hook.floor_exempt_on({"NOBLIVION_CONFIG": str(cfg), hook.FLOOR_EXEMPT_ENV: "0"}) is False


# ── the margin ──────────────────────────────────────────────────────────────


def _far(corpus, daemon, score=0.55):
    """A ruled row with a shared word whose cosine is 0.13 under the floor."""
    _write(corpus, "feedback_far1", "hit")
    daemon["rows"] = [*ROWS, ("feedback_far1", score, "hit")]


def test_the_default_margin_is_0_08():
    assert hook.FLOOR_EXEMPT_MARGIN_DEFAULT == 0.08
    assert hook.floor_exempt_margin({}) == 0.08


def test_a_row_more_than_the_margin_under_the_floor_is_cut(tmp_path, corpus, daemon):
    _far(corpus, daemon)
    names = _names(_serve(_env(tmp_path, corpus)))
    assert "feedback_far1" not in names, "0.55 is 0.13 under 0.68: outside 0.08"
    assert {"feedback_hit1", "feedback_hit2"} <= names, "0.66 and 0.62 are inside"


def test_a_wider_margin_keeps_the_far_row(tmp_path, corpus, daemon):
    _far(corpus, daemon)
    env = _env(tmp_path, corpus, **{hook.FLOOR_EXEMPT_MARGIN_ENV: "0.15"})
    assert "feedback_far1" in _names(_serve(env))
    assert ":floor_exempt3" in _status(tmp_path)


def test_a_narrower_margin_cuts_the_row_that_the_default_keeps(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **{hook.FLOOR_EXEMPT_MARGIN_ENV: "0.05"})
    names = _names(_serve(env))
    assert "feedback_hit1" in names, "0.66 is 0.02 under the floor"
    assert "feedback_hit2" not in names, "0.62 is 0.06 under the floor"


def test_the_margin_edge_is_inside():
    lines = [_line(1, 0.60), _line(2, 0.59)]
    assert hook.floor_exempt_ids(lines, 0.68, 30, 0.08) == {1}
    assert hook.floor_exempt_ids(lines, 0.68, 30, 0.0) == set()


def test_the_margin_reads_the_config_key_and_the_env_wins(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"floor_exempt_margin": 0.15}}))
    env = {"NOBLIVION_CONFIG": str(cfg)}
    assert hook.floor_exempt_margin(env) == 0.15
    assert hook.floor_exempt_margin({**env, hook.FLOOR_EXEMPT_MARGIN_ENV: "0.02"}) == 0.02


@pytest.mark.parametrize("raw", ["x", "-0.1", "3", "nan", "inf"])
def test_a_malformed_margin_is_the_default(raw):
    assert hook.floor_exempt_margin({hook.FLOOR_EXEMPT_MARGIN_ENV: raw}) == 0.08
