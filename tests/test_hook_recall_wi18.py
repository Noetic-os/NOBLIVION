# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: an index row with no rule text is not shown.

One variable, off unless set: ``NOBLIVION_RECALL_INDEX_DROP_NO_RULE`` (rule
shape only). A row that would render ``no rule on this memory; fetch it to read
it`` (no ``rule:`` from a local file and no usable summary) is left out before
the cut to K.

What these tests hold:

1. OFF UNLESS ASKED FOR. Unset or empty, the text and the log status are what
   the release before the drop wrote, fallback rows included.
2. THE ROW IS DROPPED BEFORE THE CUT TO K, so a dropped row frees a place for
   the next row that has text, and the rows that stay keep their order.
3. WHAT IS KEPT: a row with a summary and no rule, with or without a local
   file. Only the row that would render the fallback text is dropped.
4. THE LOG counts the rows dropped from the ranked list, and says so when the
   option is set without the rule shape.
5. It works together with the floor, the APPLY tiers, the row dedupe and the
   last index file, and with no row left the hook prints nothing.

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
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp cache folder and a tmp memory folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_wi18")

SID = "drop-no-rule-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
DROP = {hook.INDEX_DROP_NO_RULE_ENV: "1"}
FLOOR = hook.INDEX_MIN_SCORE_ENV
FALLBACK = hook.INDEX_ROW_NO_RULE

# The store's answer, in rank order: (title, summary, score).
#   m..  a local file with a rule                 -> the row renders the rule
#   T..  a store capture, no file, no summary     -> the row renders FALLBACK
#   S1   a store capture with a summary           -> the row renders the summary
#   P1   a local file with no rule, with a summary -> renders the summary
#   P2   a local file with no rule, no summary     -> renders FALLBACK
T1 = "Tool error on 2026-09-16 in Claude Code session 6ff3ca0"
T2 = "Operator correction on 2026-09-17 in Claude Code session 0a1b2c3"
T3 = "Tool error on 2026-09-18 in Claude Code session 9d8e7f6"
S1 = "Captured note with a summary"
ROWS: list[tuple[str, str, float | None]] = [
    ("feedback_m01", "summary 1", 0.70),
    (T1, "", 0.69),
    ("feedback_m02", "summary 2", 0.68),
    (T2, "", 0.67),
    (S1, "what the capture says", 0.66),
    ("project_p01", "the state of project one", 0.65),
    (T3, "   ", 0.64),
    ("feedback_m03", "summary 3", 0.63),
    ("project_p02", "", 0.62),
    ("feedback_m04", "summary 4", 0.61),
    ("feedback_m05", "summary 5", 0.60),
]


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
    for i in range(1, 6):
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
    for i in (1, 2):
        base = f"project_p{i:02d}"
        (folder / f"{base}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {base}",
                    f"description: about {base}",
                    "metadata:",
                    "  type: project",
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
    """``recall_index`` answers ``rows`` in order. Nothing leaves the host.
    ``asked`` records the k of each call."""
    state = {"rows": list(ROWS), "asked": []}

    def fake(query, k, environ=None, *args, **kwargs):
        state["asked"].append(k)
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=t, summary=s, score=sc)
            for pos, (t, s, sc) in enumerate(state["rows"], start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    return state


def _base_env(tmp_path, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
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


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in _rows(text)]


def _log(tmp_path) -> str:
    return (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]


def _status(tmp_path) -> str:
    return _log(tmp_path).rsplit(" ", 1)[1]


KEPT = [
    "feedback_m01",
    "feedback_m02",
    S1,
    "project_p01",
    "feedback_m03",
    "feedback_m04",
    "feedback_m05",
]


# ── off unless asked for ────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_without_the_variable_the_text_and_the_log_are_unchanged(tmp_path, corpus, daemon, raw):
    base = _serve(_env(tmp_path, corpus))
    status = _status(tmp_path)
    assert status == "ok:ruled7of11", "the join count of the release before"
    assert len(_rows(base)) == 11
    assert sum(FALLBACK in ln for ln in _rows(base)) == 4, "T1, T2, T3 and p02"
    if raw is not None:
        assert not hook.index_drop_no_rule({hook.INDEX_DROP_NO_RULE_ENV: raw})
        assert _serve(_env(tmp_path, corpus, **{hook.INDEX_DROP_NO_RULE_ENV: raw})) == base
        assert _status(tmp_path) == status
    assert daemon["asked"] == [hook.INDEX_K_DEFAULT] * len(daemon["asked"]), (
        "off, the rule shape alone still does not overfetch"
    )


def test_the_fallback_text_is_the_text_of_the_release_before():
    assert FALLBACK == "no rule on this memory; fetch it to read it"
    row = hook.IndexLine(rank=1, mid=7, title=T1, summary="", score=0.6)
    name = T1[: hook.INDEX_ROW_NAME_MAX_CHARS].rstrip()
    assert row.render(True) == f"- [id 7] {FALLBACK} ({name})"
    assert not row.has_rule_text()


# ── on: the row is dropped, before the cut ──────────────────────────────────


def test_a_row_with_no_rule_text_is_not_shown(tmp_path, corpus, daemon):
    base = _serve(_env(tmp_path, corpus))
    text = _serve(_env(tmp_path, corpus, **DROP))
    assert _names(text) == KEPT, "the order of the rows that stay is unchanged"
    assert FALLBACK not in text
    assert _rows(text) == [ln for ln in _rows(base) if FALLBACK not in ln], (
        "every row that stays is byte for byte the row of the release before"
    )
    assert text.splitlines()[0] == base.splitlines()[0], "the header did not move"


def test_the_log_counts_the_rows_dropped(tmp_path, corpus, daemon):
    _serve(_env(tmp_path, corpus, **DROP))
    assert _status(tmp_path) == "ok:norule_drop4:ruled6of7"
    assert " hits=7 " in _log(tmp_path)


def test_a_dropped_row_frees_a_place_for_the_next_row(tmp_path, corpus, daemon):
    k4 = {hook.INDEX_K_ENV: "4"}
    off = _serve(_env(tmp_path, corpus, **k4))
    assert _names(off) == [
        "feedback_m01",
        T1[: hook.INDEX_ROW_NAME_MAX_CHARS].rstrip(),
        "feedback_m02",
        T2[: hook.INDEX_ROW_NAME_MAX_CHARS].rstrip(),
    ]
    on = _serve(_env(tmp_path, corpus, **k4, **DROP))
    assert _names(on) == KEPT[:4], "four rows with text, not two rows and two holes"
    assert _status(tmp_path) == "ok:norule_drop4:ruled3of4", (
        "the drop ran on the ranked list, not on the first k"
    )


def test_the_drop_asks_the_store_for_the_deep_list(tmp_path, corpus, daemon):
    _serve(_env(tmp_path, corpus, **DROP, **{hook.INDEX_K_ENV: "4"}))
    assert daemon["asked"] == [hook.INDEX_CANDIDATE_TOP_K]


def test_a_row_with_a_summary_and_no_rule_is_kept(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **DROP))
    by_name = dict(zip(_names(text), _rows(text), strict=False))
    assert "what the capture says" in by_name[S1], "no local file, a summary"
    assert "the state of project one" in by_name["project_p01"], "a file, no rule"
    assert "project_p02" not in by_name, "a file with no rule and no summary"


def test_a_summary_of_hidden_characters_only_is_no_summary(tmp_path, corpus, daemon):
    daemon["rows"] = [
        ("feedback_m01", "", 0.7),
        ("Tool error A", "​​", 0.6),
        ("Tool error B", "real words", 0.5),
    ]
    text = _serve(_env(tmp_path, corpus, **DROP))
    assert _names(text) == ["feedback_m01", "Tool error B"]
    assert _status(tmp_path) == "ok:norule_drop1:ruled1of2"


def test_no_row_has_text_no_output_and_the_last_index_stays(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DROP)
    _serve(env)
    last = Path(hook.last_index_file(str(tmp_path / "cache"), SID))
    before = last.read_bytes()
    daemon["rows"] = [(T1, "", 0.7), (T2, "", 0.6)]
    assert _serve(env) == ""
    assert last.read_bytes() == before
    assert _status(tmp_path) == "ok:norule_drop2:ruled0of0"
    assert " hits=0 chars=0 " in _log(tmp_path)


# ── with the other options ──────────────────────────────────────────────────


def test_the_drop_needs_the_rule_shape_and_the_log_says_so(tmp_path, daemon):
    env = _base_env(tmp_path)
    plain = _serve(env)
    assert _serve(dict(env, **DROP)) == plain
    assert _status(tmp_path) == "ok:drop_no_rule_off:no_rule_rows"
    assert daemon["asked"] == [hook.INDEX_K_DEFAULT] * 2, "ignored: no overfetch"


def test_the_drop_runs_after_the_floor(tmp_path, corpus, daemon):
    text = _serve(_env(tmp_path, corpus, **DROP, **{FLOOR: "0.645"}))
    assert _names(text) == ["feedback_m01", "feedback_m02", S1, "project_p01"]
    assert _status(tmp_path) == "ok:floor6of11:norule_drop2:ruled3of4", (
        "T3 and p02 are under the floor, so the drop counts T1 and T2 only"
    )


def test_the_drop_runs_after_the_local_rerank(tmp_path, corpus, daemon):
    rerank = {hook.INDEX_RERANK_ENV: "1", hook.INDEX_HYGIENE_ENV: "1"}
    whole = _serve(_env(tmp_path, corpus, **rerank))
    on = _serve(_env(tmp_path, corpus, **rerank, **DROP))
    assert _rows(on) == [ln for ln in _rows(whole) if FALLBACK not in ln], (
        "the re-ranked order, filtered"
    )
    assert ":norule_drop4:ruled6of7" in _status(tmp_path)


def test_the_apply_tier_follows_the_rows_that_are_shown(tmp_path, corpus, daemon):
    """Rank 2's tier goes to the second row SHOWN, not to the row dropped there."""
    text = _serve(_env(tmp_path, corpus, **DROP, **{hook.INDEX_APPLY_ENV: "1"}))
    lines = text.splitlines()
    assert lines[1].endswith("(feedback_m01)")
    assert lines[2] == hook.APPLY_FIRST_PREFIX + "run the tool number 1 with --flag"
    assert lines[3].endswith("(feedback_m02)")
    assert lines[4] == hook.APPLY_FIRST_PREFIX + "run the tool number 2 with --flag"
    assert _status(tmp_path) == "ok:norule_drop4:ruled6of7:apply5of10"


def test_the_last_index_file_numbers_the_rows_that_are_shown(tmp_path, corpus, daemon):
    _serve(_env(tmp_path, corpus, **DROP))
    rows = hook.load_last_index(str(tmp_path / "cache"), SID)
    assert [(r["rank"], r["name"]) for r in rows] == list(enumerate(KEPT, start=1))


def test_the_drop_and_the_row_dedupe_work_together(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DROP, **{hook.INDEX_ROW_DEDUPE_ENV: "1", hook.INDEX_K_ENV: "3"})
    assert _names(_serve(env)) == KEPT[:3]
    env[hook.INDEX_K_ENV] = "5"
    assert _names(_serve(env)) == KEPT[3:5], "the next rows with text, no fallback row"
    assert _status(tmp_path) == "ok:norule_drop4:ruled4of5:rows2of5"


def test_with_no_memory_folder_only_the_rows_with_a_summary_stay(tmp_path, corpus, daemon):
    env = _env(tmp_path, corpus, **DROP)
    env[hook.MEMORY_DIR_ENV] = str(tmp_path / "no-such-folder")
    text = _serve(env)
    assert FALLBACK not in text
    assert _names(text) == KEPT, "here every kept row has a summary"
    assert all("Do step" not in ln for ln in _rows(text)), "no rule could be read"
    status = _status(tmp_path)
    assert status.startswith("ok:local_off:") and status.endswith(":norule_drop4:ruled0of7")
