# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: rows of the project folder (NOBLIVION-160).

In a project folder the notes of the home root and of the other shared roots
outrank the notes of the folder, and the rules-only path hides every project
note without ``rule:`` and ``apply:``. Three rules bring the project notes back:

1. RESERVED ROW. When a row of the project folder passed the floor and none of
   the shown rows is one, the best such row takes the place of the lowest row.
2. DESCRIPTION ROW. A project note with no rule fields in the first 3 rows of the
   fused order shows as one row: its name and description, 150 characters
   together, no apply text. At most one.
3. SHARED-ROOT MARGIN. A row of a shared root needs the floor plus 0.02.

The shared root of these tests is the user-wide folder (``NOBLIVION_GLOBAL_MEMORY_DIR``),
a folder that is not the project folder. Every test runs on a tmp data dir, cache
folder and memory folders, and the store call is replaced, so no live file is read
or written.
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
    "drives the real index hook on a tmp cache folder and tmp memory folders, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_own_rows")

SID = "own-rows-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
ON = {hook.OWN_ROWS_ENV: "1"}


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _note(
    folder: Path, name: str, *, rule: bool = True, apply: bool = True, desc: str = ""
) -> None:
    fm = ["---", f"name: {name}", f"description: {desc or 'about ' + name}", "metadata:"]
    fm.append("  type: feedback")
    if rule:
        fm.append(f"rule: Do the step of {name} first.")
    if apply:
        fm.append("apply: " + json.dumps(f"run the tool for {name} and check it"))
    fm += ["---", "", f"Body of {name}.", ""]
    (folder / f"{name}.md").write_text("\n".join(fm))


def _folders(tmp_path: Path, own: dict[str, dict], shared: list[str]) -> tuple[Path, Path]:
    mine = tmp_path / "own-memory"
    other = tmp_path / "shared-memory"
    mine.mkdir()
    other.mkdir()
    for name, fields in own.items():
        _note(mine, name, **fields)
    for name in shared:
        # A name that starts with "plain_" is a shared note with no rule fields.
        _note(other, name, rule=not name.startswith("plain_"), apply=not name.startswith("plain_"))
    return mine, other


def _serve(monkeypatch, tmp_path, rows, own, shared, **extra) -> str:
    """Run the hook on ``rows`` ((name, cosine) in the store's order). ``own`` maps the
    names of the project folder to the fields they have; ``shared`` lists the names of
    the shared folder."""
    mine, other = _folders(tmp_path, own, shared)

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=name, summary="", score=score)
            for pos, (name, score) in enumerate(rows, start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_GLOBAL_MEMORY_DIR=str(other),
        **{
            hook.INDEX_MIN_SCORE_ENV: "0.68",
            hook.MEMORY_DIR_ENV: str(mine),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.RULES_ONLY_ENV: "1",
            hook.PROMPT_ROWS_ENV: "5",
            **extra,
        },
    )
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt="a task"))
    out = io.StringIO()
    hook.run(payload, out, env)
    return out.getvalue().rstrip("\n")


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in _rows(text)]


def _status(tmp_path) -> str:
    log = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()
    return log[-1].rsplit(" ", 1)[1]


SHARED = [f"feedback_g{i}" for i in range(1, 8)]
OWN_RULE = {"feedback_own": {}}
NO_MARGIN = {hook.SHARED_MARGIN_ENV: "0"}


# 1. the reserved row ---------------------------------------------------------


def test_one_of_five_rows_is_the_best_own_row_that_passed_the_floor(monkeypatch, tmp_path):
    rows = [(n, 0.80 - 0.01 * i) for i, n in enumerate(SHARED[:6])] + [("feedback_own", 0.72)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == SHARED[:4] + ["feedback_own"]
    assert ":own1" in _status(tmp_path)


def test_without_the_setting_the_own_row_stays_out(monkeypatch, tmp_path):
    rows = [(n, 0.80 - 0.01 * i) for i, n in enumerate(SHARED[:6])] + [("feedback_own", 0.72)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **NO_MARGIN)
    assert _names(text) == SHARED[:5]
    assert ":own1" not in _status(tmp_path)


def test_an_own_row_under_the_floor_is_not_reserved(monkeypatch, tmp_path):
    rows = [(n, 0.80 - 0.01 * i) for i, n in enumerate(SHARED[:6])] + [("feedback_own", 0.60)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == SHARED[:5]


def test_the_best_of_two_own_rows_is_the_one_that_comes_in(monkeypatch, tmp_path):
    rows = [(n, 0.80 - 0.01 * i) for i, n in enumerate(SHARED[:6])]
    rows += [("feedback_own2", 0.73), ("feedback_own", 0.72)]
    own = {"feedback_own": {}, "feedback_own2": {}}
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == SHARED[:4] + ["feedback_own2"]


def test_an_own_row_already_in_the_first_rows_moves_nothing(monkeypatch, tmp_path):
    rows = [("feedback_g1", 0.80), ("feedback_own", 0.79)] + [
        (n, 0.78 - 0.01 * i) for i, n in enumerate(SHARED[1:6])
    ]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == ["feedback_g1", "feedback_own"] + SHARED[1:4]
    assert ":own1" not in _status(tmp_path)


# 2. the description row ------------------------------------------------------


PLAIN = {"rule": False, "apply": False, "desc": "Notes on how the sync job picks its window."}


def test_a_project_note_with_no_rule_fields_in_the_first_three_shows_as_a_description(
    monkeypatch, tmp_path
):
    rows = [("feedback_g1", 0.80), ("project_plain", 0.79)] + [
        (n, 0.78 - 0.01 * i) for i, n in enumerate(SHARED[1:6])
    ]
    own = {"project_plain": PLAIN}
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == ["feedback_g1", "project_plain"] + SHARED[1:4]
    line = next(ln for ln in _rows(text) if ln.endswith("(project_plain)"))
    assert "Notes on how the sync job picks its window." in line
    assert ":desc1" in _status(tmp_path)
    after = text.splitlines()[text.splitlines().index(line) + 1]
    assert not after.startswith(hook.APPLY_FIRST_PREFIX)


def test_the_name_and_the_description_take_150_characters_together(monkeypatch, tmp_path):
    long_name = "project_" + "n" * 60
    long_desc = " ".join(["description"] * 40)
    own = {long_name: {"rule": False, "apply": False, "desc": long_desc}}
    rows = [(long_name, 0.80)] + [(n, 0.79 - 0.01 * i) for i, n in enumerate(SHARED[:5])]
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    line = _rows(text)[0]
    desc, name = line.split("] ", 1)[1].rsplit(" (", 1)
    assert desc.startswith("description")
    assert len(desc) + len(name.rstrip(")")) <= hook.OWN_DESC_MAX_CHARS


def test_a_plain_note_below_the_first_three_rows_shows_no_description(monkeypatch, tmp_path):
    rows = [(n, 0.80 - 0.01 * i) for i, n in enumerate(SHARED[:3])] + [("project_plain", 0.75)]
    text = _serve(monkeypatch, tmp_path, rows, {"project_plain": PLAIN}, SHARED, **ON, **NO_MARGIN)
    assert "project_plain" not in _names(text)
    assert ":desc1" not in _status(tmp_path)


def test_a_plain_note_of_a_shared_root_shows_no_description(monkeypatch, tmp_path):
    rows = [("plain_shared", 0.80), ("feedback_g1", 0.79), ("feedback_g2", 0.78)]
    text = _serve(
        monkeypatch, tmp_path, rows, {}, ["plain_shared", "feedback_g1", "feedback_g2"], **ON
    )
    assert _names(text) == ["feedback_g1", "feedback_g2"]
    assert ":desc1" not in _status(tmp_path)


def test_a_project_note_with_a_rule_and_no_apply_shows_no_description(monkeypatch, tmp_path):
    own = {"feedback_half": {"rule": True, "apply": False}}
    rows = [("feedback_half", 0.80)] + [(n, 0.79 - 0.01 * i) for i, n in enumerate(SHARED[:4])]
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    assert "feedback_half" not in _names(text)


def test_at_most_one_description_row_shows(monkeypatch, tmp_path):
    own = {"project_a": PLAIN, "project_b": PLAIN}
    rows = [("project_a", 0.80), ("project_b", 0.79)] + [
        (n, 0.78 - 0.01 * i) for i, n in enumerate(SHARED[:5])
    ]
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    shown = _names(text)
    assert shown.count("project_a") == 1
    assert "project_b" not in shown
    assert len(shown) == 5


def test_the_description_row_and_the_reserved_row_both_stay_in_five(monkeypatch, tmp_path):
    own = {"project_plain": PLAIN, "feedback_own": {}}
    rows = [("feedback_g1", 0.80), ("project_plain", 0.79)] + [
        (n, 0.78 - 0.01 * i) for i, n in enumerate(SHARED[1:6])
    ]
    rows += [("feedback_own", 0.70)]
    text = _serve(monkeypatch, tmp_path, rows, own, SHARED, **ON, **NO_MARGIN)
    shown = _names(text)
    assert len(shown) == 5
    assert shown[:2] == ["feedback_g1", "project_plain"]
    assert shown[-1] == "feedback_own"


# 3. the shared-root margin ---------------------------------------------------


def test_a_shared_row_needs_a_cosine_002_higher_than_an_own_row(monkeypatch, tmp_path):
    rows = [
        ("feedback_g1", 0.80),
        ("feedback_g2", 0.69),
        ("feedback_own", 0.69),
        ("feedback_g3", 0.70),
    ]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON)
    assert _names(text) == ["feedback_g1", "feedback_own", "feedback_g3"]
    assert ":shared_cut1" in _status(tmp_path)


def test_the_store_rank_one_keeps_the_rule_of_the_floor(monkeypatch, tmp_path):
    rows = [("feedback_g1", 0.69), ("feedback_own", 0.69)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON)
    assert _names(text) == ["feedback_g1", "feedback_own"]
    assert ":shared_cut" not in _status(tmp_path)


def test_without_the_setting_both_rows_pass_the_same_floor(monkeypatch, tmp_path):
    rows = [("feedback_g1", 0.80), ("feedback_g2", 0.69), ("feedback_own", 0.69)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED)
    assert _names(text) == ["feedback_g1", "feedback_g2", "feedback_own"]


def test_the_margin_is_a_setting(monkeypatch, tmp_path):
    rows = [("feedback_g1", 0.80), ("feedback_g2", 0.69), ("feedback_own", 0.69)]
    text = _serve(monkeypatch, tmp_path, rows, OWN_RULE, SHARED, **ON, **NO_MARGIN)
    assert _names(text) == ["feedback_g1", "feedback_g2", "feedback_own"]
    env = {hook.SHARED_MARGIN_ENV: "0.05"}
    assert hook.shared_root_margin(env) == 0.05
    assert hook.shared_root_margin({}) == 0.02
    for bad in ("x", "-1", "2", "nan"):
        assert hook.shared_root_margin({hook.SHARED_MARGIN_ENV: bad}) == 0.02


def test_floor_index_raises_only_the_rows_named():
    a = hook.IndexLine(rank=1, mid=1, title="a", summary="", score=0.69)
    b = hook.IndexLine(rank=2, mid=2, title="b", summary="", score=0.69)
    kept, _ = hook.floor_index([a, b], 0.68, None, {1}, 0.02)
    assert [ln.mid for ln in kept] == [2]


# 4. the placement ------------------------------------------------------------


def _line(mid: int) -> hook.IndexLine:
    return hook.IndexLine(rank=mid, mid=mid, title=f"n{mid}", summary="", score=0.8)


def test_place_own_rows_changes_nothing_for_one_row():
    rows = [_line(i) for i in range(1, 4)]
    got, moved, desc = hook.place_own_rows(rows, 1, {})
    assert [ln.mid for ln in got] == [1]
    assert (moved, desc) == (0, 0)


def test_the_setting_reads_the_config_switch(tmp_path):
    assert hook.own_rows({}) is True
    assert hook.own_rows({hook.OWN_ROWS_ENV: "0"}) is False
