# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: the newer of two rows on one subject goes first (NOBLIVION-156).

Two shown rows whose notes share 2 or more triggers are two notes on one subject. The
newer note takes the higher place and the older row says so. Fictional notes only.
"""

from __future__ import annotations

import os

import pytest

from hookload import load_hook

hook = load_hook("recall_hook", "hooktest_recall_older_mark")
corpus = load_hook("corpus", "hooktest_recall_older_mark_corpus")

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = "calls the real ranking step and the real corpus reader on tmp files"


def _file(name: str, triggers: list[str], stamp: float) -> corpus.MemoryFile:
    return corpus.MemoryFile(
        f"{name}.md", name, "feedback", "", "", [], "rule", "", "tool", triggers, stamp
    )


def _row(mid: int, name: str) -> hook.IndexLine:
    return hook.IndexLine(rank=mid, mid=mid, title=name, summary="", score=0.8, rule="Do it.")


def _map(*files) -> dict:
    return corpus.corpus_by_name(list(files))


SAME = ["git push", "phrase:release"]


def test_the_newer_note_goes_first_and_the_older_row_is_marked():
    old, new = _file("old", SAME, 100.0), _file("new", SAME, 200.0)
    rows = [_row(1, "old"), _row(2, "other"), _row(3, "new")]
    got, marked = hook.mark_older_rows(rows, 5, _map(old, new))
    assert [r.title for r in got] == ["new", "other", "old"]
    assert marked == 1
    assert [r.older for r in got] == [False, False, True]


def test_the_older_row_keeps_its_place_when_it_is_already_lower():
    old, new = _file("old", SAME, 100.0), _file("new", SAME, 200.0)
    got, marked = hook.mark_older_rows([_row(1, "new"), _row(2, "old")], 5, _map(old, new))
    assert [r.title for r in got] == ["new", "old"] and marked == 1
    assert got[1].older and not got[0].older


def test_one_shared_trigger_is_not_a_pair():
    old, new = _file("old", ["git push", "a"], 100.0), _file("new", ["git push", "b"], 200.0)
    got, marked = hook.mark_older_rows([_row(1, "old"), _row(2, "new")], 5, _map(old, new))
    assert [r.title for r in got] == ["old", "new"] and marked == 0


def test_family_triggers_and_file_kinds_are_not_a_subject():
    old, new = _file("old", SAME, 100.0), _file("new", SAME, 200.0)
    crowd = [_file(f"c{i}", SAME, 50.0) for i in range(hook.OLDER_MAX_HOLDERS - 2)]
    rows = [_row(1, "old"), _row(2, "new")]
    # 3 holders: still a subject. A 4th holder makes both triggers a family.
    got, marked = hook.mark_older_rows(rows, 5, _map(old, new, *crowd))
    assert [r.title for r in got] == ["new", "old"] and marked == 1
    old, new = _file("old", SAME, 100.0), _file("new", SAME, 200.0)
    rows = [_row(1, "old"), _row(2, "new")]
    got, marked = hook.mark_older_rows(rows, 5, _map(old, new, *crowd, _file("c9", SAME, 50.0)))
    assert [r.title for r in got] == ["old", "new"] and marked == 0
    kinds = ["glob:**/*.yaml", "glob:**/*.yml", "tool:Bash"]
    old, new = _file("old", kinds, 100.0), _file("new", kinds, 200.0)
    got, marked = hook.mark_older_rows([_row(1, "old"), _row(2, "new")], 5, _map(old, new))
    assert marked == 0


@pytest.mark.parametrize("stamps", [(0.0, 200.0), (100.0, 100.0)])
def test_a_note_with_no_time_or_the_same_time_is_left_alone(stamps):
    old, new = _file("old", SAME, stamps[0]), _file("new", SAME, stamps[1])
    got, marked = hook.mark_older_rows([_row(1, "old"), _row(2, "new")], 5, _map(old, new))
    assert [r.title for r in got] == ["old", "new"] and marked == 0


def test_only_the_shown_rows_pair_up_and_trigger_case_does_not_matter():
    old, new = _file("old", SAME, 100.0), _file("new", [t.upper() for t in SAME], 200.0)
    rows = [_row(1, "old"), _row(2, "x"), _row(3, "new")]
    got, marked = hook.mark_older_rows(rows, 2, _map(old, new))
    assert [r.title for r in got] == ["old", "x", "new"] and marked == 0
    got, marked = hook.mark_older_rows(rows, 3, _map(old, new))
    assert [r.title for r in got][0] == "new" and marked == 1


def test_a_row_with_no_local_file_stays():
    old = _file("old", SAME, 100.0)
    got, marked = hook.mark_older_rows([_row(1, "old"), _row(2, "gone")], 5, _map(old))
    assert [r.title for r in got] == ["old", "gone"] and marked == 0


def test_the_mark_is_on_the_rendered_row_and_stays_inside_the_row_cap():
    row = _row(7, "old")
    plain = row.render(True)
    row.older = True
    marked = row.render(True)
    assert marked.endswith("; older, a newer note exists)")
    assert marked.startswith(plain.split(" (old")[0][:12])
    assert len(marked) <= hook.INDEX_ROW_MAX_CHARS
    long_one = hook.IndexLine(1, 8, "n" * 90, "", 0.8, rule="r " * 300)
    long_one.older = True
    assert len(long_one.render(True)) <= hook.INDEX_ROW_MAX_CHARS
    assert hook.renumber([long_one])[0].older


def test_the_setting_reads_the_switch():
    assert hook.older_mark({}) is True
    assert hook.older_mark({hook.OLDER_MARK_ENV: "0"}) is False


def test_the_corpus_stamp_is_the_modified_line_else_the_file_time(tmp_path):
    body = "---\nname: {n}\ndescription: d\nmetadata:\n{m}rule: r\ntriggers: [a, b]\n---\nbody\n"
    (tmp_path / "with.md").write_text(
        body.format(n="with", m="  modified: 2026-10-19T21:48:45.170Z\n"), encoding="utf-8"
    )
    (tmp_path / "without.md").write_text(body.format(n="without", m=""), encoding="utf-8")
    (tmp_path / "junk.md").write_text(
        body.format(n="junk", m="  modified: soon\n"), encoding="utf-8"
    )
    os.utime(tmp_path / "without.md", (1_700_000_000, 1_700_000_000))
    os.utime(tmp_path / "junk.md", (1_700_000_500, 1_700_000_500))
    cache = str(tmp_path / "cache.json")
    for _ in range(2):  # the second read comes from the cache file
        by = {md.name: md for md in corpus.load_memory_corpus(str(tmp_path), cache)}
        assert by["with"].stamp == pytest.approx(1_792_446_525.17, abs=1.0)
        assert by["without"].stamp == pytest.approx(1_700_000_000.0)
        assert by["junk"].stamp == pytest.approx(1_700_000_500.0)


# the whole hook ---------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from recall_helpers import hook_env  # noqa: E402


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _write(folder: Path, name: str, rule: str, when: int) -> None:
    text = (
        f"---\nname: {name}\ndescription: about {name}\nmetadata:\n  type: feedback\n"
        f'rule: {rule}\napply: "run it"\nscope: tool\n'
        "triggers: [deploy window, phrase:release freeze]\n---\n\nBody.\n"
    )
    (folder / f"{name}.md").write_text(text, encoding="utf-8")
    os.utime(folder / f"{name}.md", (when, when))


def _serve(monkeypatch, tmp_path, rows, extra) -> list[str]:
    folder = tmp_path / "memory"
    folder.mkdir()
    _write(folder, "feedback_window_old", "Deploy only before 17:00.", 1_700_000_000)
    _write(folder, "feedback_window_new", "Deploy only before 15:00.", 1_700_900_000)
    _write(folder, "feedback_other", "Tag every release.", 1_700_100_000)

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(rank=i, mid=100 + i, title=name, summary="", score=score)
            for i, (name, score) in enumerate(rows, start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_INDEX="1",
        **{
            hook.INDEX_MIN_SCORE_ENV: "0.5",
            hook.MEMORY_DIR_ENV: str(folder),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.RULES_ONLY_ENV: "1",
            hook.PROMPT_ROWS_ENV: "5",
            **extra,
        },
    )
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id="s1", prompt="deploy"))
    out = io.StringIO()
    hook.run(payload, out, env)
    return [ln for ln in out.getvalue().splitlines() if ln.startswith("- [id ")]


ROWS = [("feedback_window_old", 0.9), ("feedback_other", 0.8), ("feedback_window_new", 0.7)]


def test_the_hook_shows_the_newer_note_first(_isolated, monkeypatch, tmp_path):
    got = _serve(monkeypatch, tmp_path, ROWS, {hook.OLDER_MARK_ENV: "1"})
    names = [ln.rsplit("(", 1)[1] for ln in got]
    assert names[0].startswith("feedback_window_new")
    assert names[2] == "feedback_window_old; older, a newer note exists)"
    assert "feedback_other" in names[1]


def test_the_hook_keeps_the_store_order_when_the_switch_is_off(_isolated, monkeypatch, tmp_path):
    got = _serve(monkeypatch, tmp_path, ROWS, {hook.OLDER_MARK_ENV: "0"})
    assert [ln.rsplit("(", 1)[1] for ln in got] == [
        "feedback_window_old)",
        "feedback_other)",
        "feedback_window_new)",
    ]
