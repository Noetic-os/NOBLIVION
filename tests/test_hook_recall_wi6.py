# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook's index shrink: a relevance floor and a check line.

Two variables:

- ``NOBLIVION_RECALL_INDEX_MIN_SCORE``: an index row whose store cosine is
  under the floor is not shown. The floor runs before the cut to K, so K is a
  cap. Unset, the floor is ``DEFAULT_INDEX_MIN_SCORE`` when the store answers
  with ``THRESHOLD_MODEL``, and no floor for another model (NOBLIVION-48);
  ``off`` is no floor.
- ``NOBLIVION_RECALL_INDEX_CHECK_LINE`` (rule shape only, off unless set): the
  rule header gets the verbalised check as a second line.

What these tests hold:

1. THE DEFAULT FLOOR. Unset, or set to a value that is not a cosine, the floor
   is the measured default. Set to ``off``, the text and the log status are
   what the release before the floor wrote.
2. A ROW UNDER THE FLOOR IS NOT SHOWN, the rows that stay keep their order, and
   the log counts them.
3. K IS A CAP: the next row that passes takes the place of a row that does not,
   and the ranks and the APPLY tiers follow the rows that are shown.
4. NO ROW PASSES, NO OUTPUT, and the last index file is left as it was.
5. A ROW WITH NO SCORE IS KEPT and counted.
6. THE CHECK LINE is the second header line, leaves the first line and every
   row unchanged, is counted against the cap, and needs the rule shape.
7. THE DEFAULT FLOOR BELONGS TO ONE MODEL. An answer that names another
   embedding model, or no model (a store of an older version), gets no default
   floor, and the log says so. A floor that the user sets applies to every
   model.

Every test runs on a tmp data dir, cache folder and guard table path, so no
live file is read or written, and the store call is replaced.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import INDEX_FLOOR_OFF, hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp cache folder and a tmp memory folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_wi6")

SID = "index-floor-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
FLOOR = hook.INDEX_MIN_SCORE_ENV
CHECK = {hook.INDEX_CHECK_LINE_ENV: "1"}
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
def store_model() -> list[str]:
    """The embedding model that the answer names, in item 0. A test may
    replace it; ``""`` is an answer with no ``model`` field."""
    return [hook.THRESHOLD_MODEL]


@pytest.fixture
def daemon(monkeypatch, store_model):
    """``recall_index`` answers memories 1..12 with the cosines in ``scores``
    (memory number -> score, None = the store sent no score), for the model
    in ``store_model``. Nothing leaves the host."""
    scores: dict[int, float | None] = {i: round(0.70 - i / 100, 2) for i in range(1, 13)}

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(
                rank=pos,
                mid=19000 + i,
                title=f"feedback_m{i:02d}",
                summary=f"summary {i}",
                score=scores[i],
                model=store_model[0],
            )
            for pos, i in enumerate(sorted(scores), start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    return scores


def _base_env(tmp_path, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **INDEX_FLOOR_OFF,  # a test of the floor sets the variable, or removes it
    )
    env.update(extra)
    return env


def _env(tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = _base_env(
        tmp_path,
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


def _no_ms(text: str) -> str:
    """The hook text without its run time: the header shows it in ms, and a
    slow runner can take 1 ms more on one of two otherwise equal runs."""
    return re.sub(r"\b\d+ ms\b", "N ms", text)


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in text.splitlines() if ROW_RE.match(ln)]


def _m(*nums: int) -> list[str]:
    return [f"feedback_m{i:02d}" for i in nums]


def _log(tmp_path) -> str:
    return (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]


def _status(tmp_path) -> str:
    return _log(tmp_path).rsplit(" ", 1)[1]


def _before_floor(n: int = 12) -> str:
    """The rule-shape text of the release before the floor for rows 1..n."""
    rows = [
        hook.IndexLine(
            rank=i,
            mid=19000 + i,
            title=f"feedback_m{i:02d}",
            summary=f"summary {i}",
            score=None,
            rule=f"Do step {i} before the first command.",
        )
        for i in range(1, n + 1)
    ]
    return "\n".join([hook.INDEX_RULE_HEADER.format(tiers="")] + [r.render(True) for r in rows])


# ── the default floor, and off ──────────────────────────────────────────────


def test_without_the_variable_the_default_floor_applies(tmp_path, corpus, daemon):
    # NOBLIVION-48. The fake cosines are 0.69, 0.68, 0.67, ...: two rows are
    # at or above the default floor.
    assert hook.DEFAULT_INDEX_MIN_SCORE == 0.68
    env = _env(tmp_path, corpus)
    env.pop(FLOOR, None)
    assert hook.index_min_score(env) == hook.DEFAULT_INDEX_MIN_SCORE
    text = _serve(env)
    assert text == _before_floor(2)
    assert _status(tmp_path) == "ok:floor2of12:ruled2of2"


@pytest.mark.parametrize("raw", ["off", "OFF", " Off ", "none", "None", "false", "no", " NO "])
def test_off_is_no_floor_and_the_text_and_the_log_are_unchanged(tmp_path, corpus, daemon, raw):
    assert hook.index_min_score({FLOOR: raw}) is None
    text = _serve(_env(tmp_path, corpus, **{FLOOR: raw}))
    assert text == _before_floor()
    assert _status(tmp_path) == "ok:ruled12of12"
    assert hook.INDEX_CHECK_LINE not in text


@pytest.mark.parametrize("raw", ["", "   ", "abc", "nan", "inf", "1.5", "-2", "0,5"])
def test_a_value_that_is_not_a_cosine_is_the_default_floor(tmp_path, corpus, daemon, raw):
    # A malformed setting must not turn the floor off.
    assert hook.index_min_score({FLOOR: raw}) == hook.DEFAULT_INDEX_MIN_SCORE
    text = _serve(_env(tmp_path, corpus, **{FLOOR: raw}))
    assert text == _before_floor(2)
    assert _status(tmp_path) == "ok:floor2of12:ruled2of2"


@pytest.mark.parametrize(
    "raw,want", [("0.52", 0.52), (" 0.6 ", 0.6), ("0", 0.0), ("-1", -1.0), ("1", 1.0)]
)
def test_a_cosine_is_read_as_the_floor(raw, want):
    assert hook.index_min_score({FLOOR: raw}) == want


def test_a_floor_under_every_row_changes_no_row(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **{FLOOR: "0.10"}))
    assert text == _before_floor()
    assert _status(tmp_path) == "ok:floor12of12:ruled12of12"


# ── the default floor belongs to one model (NOBLIVION-48) ───────────────────

OTHER_MODEL = "example/other-embedder"


def test_the_default_floor_is_for_the_measured_model_only():
    assert hook.index_min_score({}, hook.THRESHOLD_MODEL) == hook.DEFAULT_INDEX_MIN_SCORE
    for model in (OTHER_MODEL, "", None, hook.THRESHOLD_MODEL.lower(), "fastembed:" + OTHER_MODEL):
        assert hook.index_min_score({}, model) is None, model
        assert hook.index_min_score({FLOOR: "abc"}, model) is None, model
    # A floor that the user sets is for every model, and so is "off".
    assert hook.index_min_score({FLOOR: "0.5"}, OTHER_MODEL) == 0.5
    assert hook.index_min_score({FLOOR: "0.5"}, None) == 0.5
    assert hook.index_min_score({FLOOR: "off"}, hook.THRESHOLD_MODEL) is None


def test_another_model_gets_no_default_floor_and_the_log_says_so(
    tmp_path, corpus, daemon, store_model
):
    # The cosines of another model are on another scale: here every row is
    # under 0.68, and the measured floor would leave an empty index.
    store_model[0] = OTHER_MODEL
    daemon.update({i: round(0.40 - i / 100, 2) for i in range(1, 13)})
    env = _env(tmp_path, corpus)
    env.pop(FLOOR, None)
    text = _serve(env)
    assert text == _before_floor()
    assert _status(tmp_path) == "ok:floor_off:model:ruled12of12"
    assert " hits=12 " in _log(tmp_path)


@pytest.mark.parametrize("raw", ["", "abc", "1.5"])
def test_a_value_that_is_not_a_cosine_is_no_floor_for_another_model(
    tmp_path, corpus, daemon, store_model, raw
):
    store_model[0] = OTHER_MODEL
    text = _serve(_env(tmp_path, corpus, **{FLOOR: raw}))
    assert text == _before_floor()
    assert _status(tmp_path) == "ok:floor_off:model:ruled12of12"


def test_an_answer_with_no_model_gets_no_default_floor(tmp_path, corpus, daemon, store_model):
    # Version skew: the hook of this version asks a store of an older version,
    # whose answer has no ``model`` field. The model is not known, so the
    # measured floor is not applied.
    store_model[0] = ""
    env = _env(tmp_path, corpus)
    env.pop(FLOOR, None)
    assert _serve(env) == _before_floor()
    assert _status(tmp_path) == "ok:floor_off:no_model:ruled12of12"


@pytest.mark.parametrize("model", [OTHER_MODEL, ""])
def test_a_set_floor_applies_to_every_model(tmp_path, corpus, daemon, store_model, model):
    store_model[0] = model
    text = _serve(_env(tmp_path, corpus, **{FLOOR: "0.645"}))
    assert _names(text) == _m(1, 2, 3, 4, 5)
    assert _status(tmp_path) == "ok:floor5of12:ruled5of5"


def test_k_still_cuts_the_index_of_another_model(tmp_path, corpus, daemon, store_model):
    # The hook asks for the candidate depth before it knows the model, so the
    # cut to k must not depend on the floor.
    store_model[0] = OTHER_MODEL
    env = _env(tmp_path, corpus, **{hook.INDEX_K_ENV: "4"})
    env.pop(FLOOR, None)
    text = _serve(env)
    assert text == _before_floor(4)
    assert _status(tmp_path) == "ok:floor_off:model:ruled4of4"


def test_keyword_mode_is_named_before_the_model(tmp_path, corpus, monkeypatch):
    # A keyword answer has no cosine and names no model: the log keeps the
    # keyword note.
    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(i, 19000 + i, f"feedback_m{i:02d}", f"summary {i}", None, keyword=True)
            for i in range(1, 4)
        ]

    monkeypatch.setattr(hook, "recall_index", fake)
    env = _env(tmp_path, corpus)
    env.pop(FLOOR, None)
    assert _names(_serve(env)) == _m(1, 2, 3)
    assert _status(tmp_path) == "ok:floor_off:keyword:ruled3of3"


def test_an_empty_answer_of_another_model_prints_nothing(tmp_path, corpus, monkeypatch):
    monkeypatch.setattr(hook, "recall_index", lambda *a, **k: [])
    env = _env(tmp_path, corpus)
    env.pop(FLOOR, None)
    assert _serve(env) == ""
    assert _status(tmp_path) == "ok:floor0of0:ruled0of0"


@pytest.mark.parametrize(
    "answer,want",
    [
        ({"model": OTHER_MODEL}, OTHER_MODEL),
        ({"model": hook.THRESHOLD_MODEL}, hook.THRESHOLD_MODEL),
        ({}, ""),
        ({"model": None}, ""),
        ({"model": 7}, ""),
        ({"model": ["x"]}, ""),
    ],
)
def test_the_index_answer_model_is_read_as_text_or_nothing(answer, want):
    payload = {"results": [{"rank": 1, "id": 5, "title": "t", "summary": "s", "score": 0.5}]}
    payload.update(answer)
    (line,) = hook.parse_index(payload)
    assert line.model == want
    assert hook.renumber([line])[0].model == want


# ── the floor ───────────────────────────────────────────────────────────────


def test_a_row_under_the_floor_is_not_shown(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **{FLOOR: "0.645"}))
    assert _names(text) == _m(1, 2, 3, 4, 5), "0.69 .. 0.65 pass, 0.64 and lower do not"
    assert text == _before_floor(5)
    assert _status(tmp_path) == "ok:floor5of12:ruled5of5"
    assert " hits=5 " in _log(tmp_path)


def test_a_row_exactly_on_the_floor_is_shown(tmp_path, corpus, daemon):
    assert _names(_serve(_env(tmp_path, corpus, **{FLOOR: "0.65"}))) == _m(1, 2, 3, 4, 5)


def test_k_is_a_cap_and_the_next_passing_row_takes_the_place(tmp_path, corpus, daemon):
    daemon.update({1: 0.40, 3: 0.41, 4: 0.42})  # three early rows fail
    text = _serve(_env(tmp_path, corpus, **{FLOOR: "0.50", hook.INDEX_K_ENV: "4"}))
    assert _names(text) == _m(2, 5, 6, 7), "not rows 1..4 with holes"
    assert _status(tmp_path) == "ok:floor9of12:ruled4of4"


def test_without_the_floor_k_takes_the_first_rows(tmp_path, corpus, daemon):
    daemon.update({1: 0.40, 3: 0.41, 4: 0.42})
    assert _names(_serve(_env(tmp_path, corpus, **{hook.INDEX_K_ENV: "4"}))) == _m(1, 2, 3, 4)


def test_the_apply_tier_follows_the_rows_that_are_shown(tmp_path, corpus, daemon):
    """Rank 1's tier goes to the first row SHOWN, not to a row the floor left out."""
    daemon.update({1: 0.40})
    text = _serve(_env(tmp_path, corpus, **APPLY, **{FLOOR: "0.50"}))
    lines = text.splitlines()
    assert _names(text)[0] == "feedback_m02"
    assert lines[2] == hook.APPLY_FIRST_PREFIX + "run the tool number 2 with --flag"
    assert text.count(hook.APPLY_FIRST_PREFIX) == hook.apply_k(hook.APPLY_TIERS)
    assert _status(tmp_path) == "ok:floor11of12:ruled11of11:apply10of10"


def test_the_last_index_file_numbers_the_rows_that_are_shown(tmp_path, corpus, daemon):
    daemon.update({1: 0.40, 2: 0.40})
    _serve(_env(tmp_path, corpus, **{FLOOR: "0.50"}))
    rows = hook.load_last_index(str(tmp_path / "cache"), SID)
    assert [(r["rank"], r["name"]) for r in rows[:2]] == [
        (1, "feedback_m03"),
        (2, "feedback_m04"),
    ]
    assert len(rows) == 10


def test_no_row_passes_no_output_and_the_last_index_stays(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus)
    _serve(env)
    last = Path(hook.last_index_file(str(tmp_path / "cache"), SID))
    before = last.read_bytes()
    assert _serve(dict(env, **{FLOOR: "0.95"})) == ""
    assert last.read_bytes() == before, "the index on the model's screen is the old one"
    assert _status(tmp_path) == "ok:floor0of12:ruled0of0"
    assert " hits=0 chars=0 " in _log(tmp_path)


def test_a_row_with_no_score_is_kept_and_counted(tmp_path, corpus, daemon):
    daemon.update({2: None, 7: None, 1: 0.10})
    text = _serve(_env(tmp_path, corpus, **{FLOOR: "0.675"}))
    assert _names(text) == _m(2, 7), "row 1 fails, the two unscored rows stay"
    assert _status(tmp_path) == "ok:floor2of12:unscored2:ruled2of2"


def test_the_floor_runs_after_the_local_rerank(tmp_path, corpus, daemon):
    rerank = {hook.INDEX_RERANK_ENV: "1", hook.INDEX_HYGIENE_ENV: "1"}
    whole = _names(_serve(_env(tmp_path, corpus, **rerank)))
    floored = _names(_serve(_env(tmp_path, corpus, **rerank, **{FLOOR: "0.645"})))
    passing = set(_m(1, 2, 3, 4, 5))
    assert floored == [n for n in whole if n in passing], "the re-ranked order, filtered"
    assert ":joined12of12:floor5of12:ruled5of5" in _status(tmp_path)


def test_the_floor_runs_in_the_title_shape_too(tmp_path, daemon):
    env = _base_env(tmp_path)
    daemon.update({1: 0.40})
    whole = _serve(env).splitlines()
    floored = _serve(dict(env, **{FLOOR: "0.645"})).splitlines()
    assert len(whole) == 13 and len(floored) == 5
    assert [ln.split(" ", 2)[1] for ln in floored[1:]] == ["1.", "2.", "3.", "4."]
    assert "feedback_m02" in floored[1], "ranks 1..n with no hole"
    assert _status(tmp_path) == "ok:floor4of12"


def test_the_floor_and_the_row_dedupe_work_together(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **{hook.INDEX_ROW_DEDUPE_ENV: "1", FLOOR: "0.645"})
    assert _names(_serve(env)) == _m(1, 2, 3, 4, 5)
    daemon.update({9: 0.68})
    second = _serve(env)
    assert _names(second) == _m(9), "only the row that is new and passes"
    assert _status(tmp_path) == "ok:floor6of12:ruled6of6:rows1of6"


# ── the check line ──────────────────────────────────────────────────────────


def test_the_check_line_is_the_second_header_line(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **CHECK))
    lines = text.splitlines()
    assert lines[0] == hook.INDEX_RULE_HEADER.format(tiers="")
    assert lines[1] == hook.INDEX_CHECK_LINE
    assert "\n".join([lines[0]] + lines[2:]) == _before_floor(), "nothing else moved"
    assert "\n" not in hook.INDEX_CHECK_LINE and not hook.INDEX_CHECK_LINE.startswith("- ")
    assert _status(tmp_path) == "ok:ruled12of12", "the check line adds no log note"


def test_the_check_line_is_the_plan_wording():
    assert hook.INDEX_CHECK_LINE == (
        "Before a command that matches a shown trigger, write one line naming the rule."
    )


def test_the_check_line_follows_the_reduced_header_too(tmp_path, corpus, daemon):
    env = _env(
        tmp_path,
        corpus,
        **CHECK,
        **{hook.INDEX_ROW_DEDUPE_ENV: "1", hook.INDEX_K_ENV: "6"},
    )
    _serve(env)
    daemon.update({12: 0.99, 11: 0.98})
    monkey = sorted(daemon)  # the store answers by number
    assert monkey[0] == 1
    env[hook.INDEX_K_ENV] = "8"
    lines = _serve(env).splitlines()
    assert lines[0] == hook.INDEX_RULE_HEADER_REDUCED.format(tiers="")
    assert lines[1] == hook.INDEX_CHECK_LINE
    assert _names("\n".join(lines)) == _m(7, 8)


def test_the_check_line_without_the_rule_shape_is_ignored_and_logged(tmp_path, daemon):
    env = _base_env(tmp_path)
    plain = _serve(env)
    assert _no_ms(_serve(dict(env, **CHECK))) == _no_ms(plain)
    assert _status(tmp_path) == "ok:check_line_off:no_rule_rows"


def test_the_check_line_is_counted_against_the_cap(tmp_path, corpus, daemon):
    base = _env(tmp_path, corpus, **{hook.INDEX_CAP_ENV: "600"})
    plain = _serve(base)
    checked = _serve(dict(base, **CHECK))
    assert len(plain) <= 600 and len(checked) <= 600
    assert len(_names(checked)) < len(_names(plain)), "the line costs a row under this cap"
    assert checked.splitlines()[1] == hook.INDEX_CHECK_LINE


def test_no_row_passes_no_check_line_either(tmp_path, corpus, daemon):
    assert _serve(_env(tmp_path, corpus, **CHECK, **{FLOOR: "0.95"})) == ""


def test_the_pretooluse_shape_carries_both(tmp_path, corpus, daemon):
    """The header is one JSON string there; the check line must survive it."""
    text, shown = hook.render_index_capped(
        hook.renumber(hook.recall_index("q", 3)),
        0,
        9000,
        rule_rows=True,
        check_line=True,
    )
    assert text.splitlines()[1] == hook.INDEX_CHECK_LINE and len(shown) == 3
    out = io.StringIO()
    hook.emit("PreToolUse", text, out)
    doc = json.loads(out.getvalue())
    assert doc["hookSpecificOutput"]["additionalContext"] == text
