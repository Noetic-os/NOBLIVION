# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: a prompt path of rules only (NOBLIVION-121).

Three settings, each off by default:

* ``NOBLIVION_RECALL_RULES_ONLY`` (config ``recall.rules_only``): only a row
  whose local file has both a ``rule:`` and an ``apply:`` is shown.
* ``NOBLIVION_RECALL_PROMPT_ROWS`` (config ``recall.prompt_rows``): the most
  rows one prompt shows.
* ``NOBLIVION_RECALL_HUB_DAYS`` (config ``recall.hub_days``) with
  ``NOBLIVION_RECALL_HUB_ROWS``: a note shown in more than half of the sessions
  of the window goes behind the other rows, and only a few of them stay.

What these tests hold:

1. OFF BY DEFAULT. With none of the settings the text and the log status are
   what the release before wrote.
2. ADMISSION. A row with a rule and no apply, a row with neither, and a store
   row with no local file are left out; a row with both stays. The rows carry
   their apply text without ``NOBLIVION_RECALL_INDEX_APPLY``.
3. CAP. The cap shortens the list from the bottom, after the admission.
4. HUB. A hub row goes behind the others, at most ``hub_rows`` stay, and with
   fewer than the minimum number of sessions no row is a hub.
5. COUNTS. A session counts a note once, however many prompts show it.
6. THE LOG holds the characters the old shape would have printed (``:wasC``).
7. The config keys work, and an env var wins over them.

Every test runs on a tmp data dir, cache folder and guard table path, so no
live file is read or written, and the store call is replaced.
"""

from __future__ import annotations

import datetime as dt
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

hook = load_hook("recall_hook", "hooktest_recall_rules_only")
hub = load_hook("hub_stats", "hooktest_hub_stats_rules_only")

SID = "rules-only-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
RULES = {hook.RULES_ONLY_ENV: "1"}

# The store's answer, in rank order: (title, summary, score).
#   r01..r08  a file with a rule and an apply text
#   q01       a file with a rule and no apply text
#   p01, p02  a file with neither (a project note)
#   T1        a store capture with no local file
ROWS: list[tuple[str, str, float | None]] = [
    ("project_p01", "state of project one", 0.80),
    ("feedback_r01", "summary 1", 0.79),
    ("feedback_q01", "summary q1", 0.78),
    ("Tool error on 2026-09-16 in session 6ff3ca0", "a capture", 0.77),
    ("feedback_r02", "summary 2", 0.76),
    ("feedback_r03", "summary 3", 0.75),
    ("project_p02", "state of project two", 0.74),
    ("feedback_r04", "summary 4", 0.73),
    ("feedback_r05", "summary 5", 0.72),
    ("feedback_r06", "summary 6", 0.71),
    ("feedback_r07", "summary 7", 0.70),
    ("feedback_r08", "summary 8", 0.69),
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


def _file(folder: Path, base: str, *, rule: bool, apply: bool) -> None:
    fm = ["---", f"name: {base}", f"description: about {base}", "metadata:", "  type: feedback"]
    if rule:
        fm.append(f"rule: Do step {base} before the first command.")
    if apply:
        fm.append("apply: " + json.dumps(f"run the tool for {base} with --flag and check it"))
    fm += ["---", "", f"BODY-TEXT-OF-{base}.", ""]
    (folder / f"{base}.md").write_text("\n".join(fm))


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, 9):
        _file(folder, f"feedback_r{i:02d}", rule=True, apply=True)
    _file(folder, "feedback_q01", rule=True, apply=False)
    for i in (1, 2):
        _file(folder, f"project_p{i:02d}", rule=False, apply=False)
    return folder


@pytest.fixture
def daemon(monkeypatch):
    state = {"rows": list(ROWS), "asked": []}

    def fake(query, k, environ=None, *args, **kwargs):
        state["asked"].append(k)
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=t, summary=s, score=sc)
            for pos, (t, s, sc) in enumerate(state["rows"], start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    return state


def _env(tmp_path, corpus, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **INDEX_FLOOR_OFF,
        **{hook.MEMORY_DIR_ENV: str(corpus), hook.INDEX_RULE_ROWS_ENV: "1"},
    )
    env.update(extra)
    return env


def _serve(env: dict[str, str], sid: object = SID) -> str:
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id=sid, prompt="a prompt about steps")
    )
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in _rows(text)]


def _log(tmp_path) -> str:
    return (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]


def _status(tmp_path) -> str:
    return _log(tmp_path).rsplit(" ", 1)[1]


def _no_ms(text: str) -> str:
    return re.sub(r"\b\d+ ms\b", "N ms", text)


def _apply_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(hook.APPLY_FIRST_PREFIX)]


# 1. off by default -----------------------------------------------------------


def test_nothing_changes_without_the_settings(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **{hook.INDEX_APPLY_ENV: "1"}))
    status = _status(tmp_path)
    assert len(_rows(text)) == 12
    for note in (":rules_drop", ":hub", ":rowcap", ":was", "rules_only_off"):
        assert note not in status
    assert hook.rules_only({}) is False
    assert hook.prompt_rows({}) == 0
    assert hub.window_days({}) == 0


# 2. admission ----------------------------------------------------------------


def test_only_rows_with_a_rule_and_an_apply_text_are_shown(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **RULES))
    assert _names(text) == [f"feedback_r{i:02d}" for i in range(1, 9)]
    assert ":rules_drop4" in _status(tmp_path)  # q01, p01, p02 and the capture


def test_rows_carry_their_apply_text_without_the_apply_switch(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **RULES))
    assert len(_apply_lines(text)) >= 5
    assert "An indented APPLY line" in text


def test_the_admission_runs_before_the_cut_to_k(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_INDEX_K="4", **RULES))
    assert _names(text) == [f"feedback_r{i:02d}" for i in range(1, 5)]


def test_the_store_is_asked_for_the_candidate_depth(tmp_path, corpus, daemon):
    _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_INDEX_K="3", **RULES))
    assert daemon["asked"] == [hook.INDEX_CANDIDATE_TOP_K]


def test_no_row_with_both_fields_prints_nothing(tmp_path, corpus, daemon):
    daemon["rows"] = [r for r in ROWS if not r[0].startswith("feedback_r")]
    assert _serve(_env(tmp_path, corpus, **RULES)) == ""


def test_rules_only_needs_the_rule_rows(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **RULES)
    env.pop(hook.INDEX_RULE_ROWS_ENV)
    text = _serve(env)
    assert text.count("(id 190") == 12  # the title shape, every row
    assert ":rules_only_off:no_rule_rows" in _status(tmp_path)


def test_rules_only_needs_a_memory_folder(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **RULES)
    env[hook.MEMORY_DIR_ENV] = str(tmp_path / "missing")
    _serve(env)
    assert ":rules_only_off:no_memory_dir" in _status(tmp_path)


def test_a_row_of_a_file_with_a_rule_only_is_dropped_by_the_helper():
    a = hook.IndexLine(1, 1, "a", "s", 0.9, rule="r", apply_block="x")
    b = hook.IndexLine(2, 2, "b", "s", 0.9, rule="r", apply_block="")
    c = hook.IndexLine(3, 3, "c", "s", 0.9, rule="  ", apply_block="x")
    d = hook.IndexLine(4, 4, "d", "s", 0.9)
    kept, dropped = hook.drop_unruled_index([a, b, c, d])
    assert kept == [a]
    assert dropped == 3


# 3. cap ----------------------------------------------------------------------


def test_the_cap_shortens_the_list_from_the_bottom(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_PROMPT_ROWS="3", **RULES))
    assert _names(text) == ["feedback_r01", "feedback_r02", "feedback_r03"]
    assert ":rowcap5" in _status(tmp_path)


def test_the_cap_alone_cuts_the_ranked_list(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_PROMPT_ROWS="2"))
    assert len(_rows(text)) == 2
    assert _names(text)[0] == "project_p01"


def test_the_cap_value_is_bounded_and_bad_values_are_zero():
    assert hook.prompt_rows({hook.PROMPT_ROWS_ENV: "5"}) == 5
    assert hook.prompt_rows({hook.PROMPT_ROWS_ENV: "100000"}) == hook.PROMPT_ROWS_MAX
    assert hook.prompt_rows({hook.PROMPT_ROWS_ENV: "-3"}) == 0
    assert hook.prompt_rows({hook.PROMPT_ROWS_ENV: "many"}) == 0


def test_five_rows_with_apply_text_stay_short(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_PROMPT_ROWS="5", **RULES))
    assert len(_rows(text)) == 5
    assert len(text) < 2000


# 4. hub rows -----------------------------------------------------------------


def _fill_hub_stats(tmp_path, sessions: int, shown: dict[str, int]) -> None:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    path = tmp_path / "cache" / hub.STATS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "days": {today: {"s": sessions, "n": shown}}}))


HUB = {hook.PROMPT_ROWS_ENV.replace("PROMPT_ROWS", "HUB_DAYS"): "14"}


def test_a_hub_row_goes_behind_the_other_rows(tmp_path, corpus, daemon):
    _fill_hub_stats(tmp_path, 20, {"feedback_r01": 15, "feedback_r02": 15, "feedback_r03": 4})
    text = _serve(_env(tmp_path, corpus, **RULES, **HUB))
    names = _names(text)
    assert names[:6] == [f"feedback_r{i:02d}" for i in (3, 4, 5, 6, 7, 8)]
    assert names[6:] == ["feedback_r01"]  # one hub row stays, the other is cut
    assert ":hub2:hubcut1" in _status(tmp_path)


def test_hub_rows_option_sets_how_many_stay(tmp_path, corpus, daemon):
    _fill_hub_stats(tmp_path, 20, {"feedback_r01": 15, "feedback_r02": 15})
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_HUB_ROWS="2", **RULES, **HUB))
    assert _names(text)[-2:] == ["feedback_r01", "feedback_r02"]
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_HUB_ROWS="0", **RULES, **HUB), "other")
    assert "feedback_r01" not in _names(text)


def test_a_hub_row_still_fills_a_place_when_few_rows_are_left(tmp_path, corpus, daemon):
    _fill_hub_stats(tmp_path, 20, {"feedback_r01": 15})
    text = _serve(_env(tmp_path, corpus, NOBLIVION_RECALL_PROMPT_ROWS="3", **RULES, **HUB))
    assert _names(text) == ["feedback_r02", "feedback_r03", "feedback_r04"]


def test_no_hub_with_too_few_sessions(tmp_path, corpus, daemon):
    _fill_hub_stats(tmp_path, hub.MIN_SESSIONS - 1, {"feedback_r01": hub.MIN_SESSIONS - 1})
    text = _serve(_env(tmp_path, corpus, **RULES, **HUB))
    assert _names(text)[0] == "feedback_r01"
    assert ":hub0" in _status(tmp_path)


def test_a_note_at_exactly_half_the_sessions_is_no_hub(tmp_path):
    _fill_hub_stats(tmp_path, 20, {"a": 10, "b": 11})
    names, sessions = hub.hubs(str(tmp_path / "cache"), 14)
    assert (names, sessions) == ({"b"}, 20)


def test_the_window_leaves_out_old_days(tmp_path):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)).date().isoformat()
    path = tmp_path / "cache" / hub.STATS_NAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"v": 1, "days": {old: {"s": 50, "n": {"a": 50}}}}))
    assert hub.hubs(str(tmp_path / "cache"), 14) == (set(), 0)
    assert hub.hubs(str(tmp_path / "cache"), 30) == ({"a"}, 50)


# 5. counts -------------------------------------------------------------------


def test_a_session_counts_a_note_once(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **RULES, **HUB)
    _serve(env)
    _serve(env)
    days = hub.load_days(str(tmp_path / "cache"))
    (rec,) = days.values()
    assert rec["s"] == 1
    assert rec["n"]["feedback_r01"] == 1
    _serve(env, "second-session")
    (rec,) = hub.load_days(str(tmp_path / "cache")).values()
    assert rec["s"] == 2
    assert rec["n"]["feedback_r01"] == 2


def test_nothing_is_counted_when_the_window_is_off(tmp_path, corpus, daemon):
    _serve(_env(tmp_path, corpus, **RULES))
    assert not (tmp_path / "cache" / hub.STATS_NAME).exists()


def test_a_broken_stats_file_reads_as_empty(tmp_path, corpus, daemon):
    path = tmp_path / "cache" / hub.STATS_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    text = _serve(_env(tmp_path, corpus, **RULES, **HUB))
    assert _names(text)[0] == "feedback_r01"
    assert hub.load_days(str(tmp_path / "cache"))  # rewritten


def test_the_session_file_of_the_counts_is_pruned_with_the_others(tmp_path):
    assert hook.HUB_SEEN_SUFFIX == hub.SEEN_SUFFIX
    cache = tmp_path / "cache"
    folder = cache / hook.SESSION_DIR_NAME
    folder.mkdir(parents=True)
    old = folder / f"old-session{hub.SEEN_SUFFIX}"
    old.write_text("[]")
    import os

    os.utime(old, (1, 1))
    assert hook.prune_session_files(str(cache), None, {}) == 1
    assert not old.exists()


# 6. the log ------------------------------------------------------------------


def test_the_log_holds_the_characters_the_old_shape_printed(tmp_path, corpus, daemon):
    old_env = _env(
        tmp_path, corpus, **{hook.INDEX_APPLY_ENV: "1", "NOBLIVION_RECALL_INDEX_K": "30"}
    )
    old = _serve(old_env)
    new = _serve(
        _env(
            tmp_path,
            corpus,
            **{hook.INDEX_APPLY_ENV: "1", "NOBLIVION_RECALL_INDEX_K": "30"},
            NOBLIVION_RECALL_PROMPT_ROWS="5",
            **RULES,
        ),
        "second",
    )
    was = int(re.search(r":was(\d+)", _status(tmp_path)).group(1))
    assert abs(was - len(old)) <= 6  # the header says "N ms"
    assert len(new) < len(old)
    assert len(_rows(new)) == 5


# 7. config keys --------------------------------------------------------------


def test_the_config_keys_turn_the_settings_on_and_an_env_var_wins(tmp_path, corpus, daemon):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"rules_only": True, "prompt_rows": 2, "hub_days": 7}}))
    env = _env(tmp_path, corpus, NOBLIVION_CONFIG=str(cfg))
    text = _serve(env)
    assert _names(text) == ["feedback_r01", "feedback_r02"]
    status = _status(tmp_path)
    assert ":rules_drop4" in status
    assert ":hub0" in status
    env[hook.RULES_ONLY_ENV] = "0"
    env[hook.PROMPT_ROWS_ENV] = "0"
    text = _serve(env, "other")
    assert len(_rows(text)) == 12
    assert ":rules_drop" not in _status(tmp_path)


def test_the_hub_counts_work_with_a_replaced_clock(tmp_path):
    """The replay tool replaces ``_dt`` with an object that has only datetime,
    timezone and timedelta."""
    import types

    fixed = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)

    class _Datetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    mod = load_hook("hub_stats", "hooktest_hub_stats_clock")
    mod._dt = types.SimpleNamespace(
        datetime=_Datetime, timezone=dt.timezone, timedelta=dt.timedelta
    )
    cache = str(tmp_path / "cache")
    assert mod.record(cache, "s1", ["a", "b"], 14) is True
    assert mod.record(cache, "s2", ["a"], 14) is True
    assert mod.load_days(cache) == {"2026-10-19": {"s": 2, "n": {"a": 2, "b": 1}}}
    assert mod.window(mod.load_days(cache), 14) == (2, {"a": 2, "b": 1})


def test_the_counts_cover_the_rows_before_the_cap_and_the_hub_rule(tmp_path, corpus, daemon):
    """A hub must stay a hub while it is demoted, so the count is of the rows the
    hook would show without the settings, not of the rows it shows."""
    _fill_hub_stats(tmp_path, 20, {"feedback_r01": 15})
    env = _env(tmp_path, corpus, NOBLIVION_RECALL_PROMPT_ROWS="2", **RULES, **HUB)
    text = _serve(env)
    assert _names(text) == ["feedback_r02", "feedback_r03"]  # the hub row r01 is behind
    (rec,) = hub.load_days(str(tmp_path / "cache")).values()
    assert rec["n"]["feedback_r01"] == 16  # still counted
    assert rec["n"]["feedback_r08"] == 1  # rank 8 of the admitted rows, past the cap of 2
