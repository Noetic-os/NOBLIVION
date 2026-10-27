# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label rows: an exact file-name match counts above the label limit (NOBLIVION-131).

A label held by more than 8 memories never matches alone. The exception: a file
label that is EXACTLY the target file of the tool call (the full file name, or
the full relative path) counts, and at most 3 of those memories show, best
first, marked ``label_exact`` in the guard log. A partial or substring match
keeps the old limit.

What these tests hold:
1. FIRES: Edit, Write, Read and Bash, with a label held by 14 memories, show 3
   rows and mark them; a relative path and a path with directories count.
2. DOES NOT FIRE: the file label only in the content of the edit, in a quoted
   message, in a prompt, as a part of another file name, or as a weak label.
3. BEST FIRST: the three rows are the best three by label score.
4. A label under the limit is not marked, and a memory that matches by two
   labels is not counted in the three.
Every path is a tmp path: no test reads or writes a live file.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook

gh = load_hook("guard_hook", "guard_hook_t_label_exact")
gt = load_hook("guard_table", "guard_table_t_label_exact")
lr = load_hook("label_rows", "label_rows_t_label_exact")
ml = load_hook("memory_labels", "memory_labels_t_label_exact")

HEADER = "Memory rows by subject (label rows)"
FILE = "role_preferences.yaml"
FT = "NOBLIVION_GUARD_LABELS_FILE_TOOLS"


def _mem(folder: Path, stem: str, body: str) -> None:
    fm = [f"name: {stem}", "description: a memory", "rule: Keep the standing ruling."]
    fm += ['apply: "check the ruling first"', "scope: tool"]
    (folder / f"{stem}.md").write_text("---\n" + "\n".join(fm) + "\n---\n" + body + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    home, data, mem = tmp_path / "home", tmp_path / "data", tmp_path / "mem"
    for d in (home, data, mem):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    for i in range(14):  # 14 memories hold the file label; memory 0 also holds extra.json
        extra = " It also edits extra_settings.json." if i == 0 else ""
        _mem(mem, f"feedback_pref_{i:02d}", f"Ruling {i} about {FILE}.{extra}")
    for i in range(4):  # a label under the limit
        _mem(mem, f"project_small_{i}", f"Notes {i} on small_target.py.")
    for i in range(14):  # a weak label (a document name) held by 14 memories
        _mem(mem, f"project_doc_{i}", f"See PLAN_DOC.md for case {i}.")
    table = tmp_path / "guard-table.json"
    gt.rebuild(mem, table)
    e = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "rc"),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",
    }
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    return e


def event(tool: str, ti: dict, sid: str = "s1") -> dict:
    return {
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": ti,
        "cwd": "/tmp",
    }


def bash(cmd: str, sid: str = "s1") -> dict:
    return event("Bash", {"command": cmd}, sid)


def call(env, ev) -> Any:
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(ev)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def ids(out) -> list[str]:
    text = (out or {}).get("additionalContext", "")
    if HEADER not in text:
        return []
    block = text[text.index(HEADER) :]
    return [ln.split()[2] for ln in block.splitlines() if ln.startswith("- Memory ")]


def last_log(env) -> dict:
    lines = Path(env["NOBLIVION_GUARD_LOG"]).read_text().splitlines()
    return json.loads(lines[-1])


def test_the_index_holds_the_label_on_14_memories(env):
    idx = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())["label_index"]
    assert idx["df"][idx["labels"].index(FILE)] == 14 > idx["cutoff"]


# ── fires ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ev",
    [
        event("Edit", {"file_path": f"/proj/config/{FILE}", "old_string": "a", "new_string": "b"}),
        event("Edit", {"file_path": f"config/{FILE}", "old_string": "a", "new_string": "b"}),
        event("Edit", {"file_path": FILE, "old_string": "a", "new_string": "b"}),
        event("Write", {"file_path": f"/proj/{FILE}", "content": "x: 1"}),
        event("MultiEdit", {"file_path": f"/proj/{FILE}", "edits": []}),
        event(
            "Edit",
            {"file_path": f"/proj/Config/{FILE.upper()}", "old_string": "a", "new_string": "b"},
        ),
    ],
)
def test_an_edit_of_the_exact_file_shows_three_rows_and_marks_them(env, ev):
    out = call(env, ev)
    assert out.get("permissionDecision") is None, "a label never denies"
    assert len(ids(out)) == 3
    line = last_log(env)
    assert line["decision"] == "labels" and sorted(line["label_exact"]) == sorted(line["ids"])
    assert all(v == [FILE] or FILE in v for v in line["label_rows"].values())


@pytest.mark.parametrize(
    "cmd",
    [
        f"vim config/{FILE}",
        f"cat ./{FILE}",
        f"sed -i s/a/b/ /proj/{FILE}",
        f"echo x > {FILE}",
        f"cd /proj && cat {FILE}; ls",
        f"cat '{FILE}'",
        f"python tool.py --file={FILE}",
    ],
)
def test_a_bash_command_that_names_the_exact_file_shows_three_rows(env, cmd):
    out = call(env, bash(cmd))
    assert len(ids(out)) == 3
    assert len(last_log(env)["label_exact"]) == 3


def test_a_read_of_the_exact_file_shows_three_rows_with_the_file_tool_leg(env):
    out = call(dict(env, **{FT: "1"}), event("Read", {"file_path": f"/proj/{FILE}"}))
    assert len(ids(out)) == 3 and len(last_log(env)["label_exact"]) == 3


def test_the_three_rows_are_the_best_three_by_label_score(env):
    # Memory 0 holds a second label of the call; the content of the edit names it.
    ev = event(
        "Edit",
        {"file_path": f"/proj/{FILE}", "old_string": "a", "new_string": "see extra_settings.json"},
    )
    out = call(dict(env, **{FT: "1"}), ev)
    got = ids(out)
    assert len(got) == 3 and got[0] == "feedback_pref_00", "two labels outrank one"
    assert last_log(env)["label_rows"]["feedback_pref_00"] == [
        "extra_settings.json",
        FILE,
    ] or set(last_log(env)["label_rows"]["feedback_pref_00"]) == {"extra_settings.json", FILE}


# ── does not fire ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ev",
    [
        # a part of the name, not the name
        event(
            "Edit", {"file_path": "/proj/preferences.yaml", "old_string": "a", "new_string": "b"}
        ),
        event("Edit", {"file_path": f"/proj/old_{FILE}", "old_string": "a", "new_string": "b"}),
        event("Edit", {"file_path": f"/proj/{FILE}.bak", "old_string": "a", "new_string": "b"}),
        event(
            "Edit", {"file_path": f"/proj/{FILE}/notes.txt", "old_string": "a", "new_string": "b"}
        ),
        # the file only in the folder name, or the other file of the same folder
        event("Edit", {"file_path": "/proj/other.yaml", "old_string": "a", "new_string": "b"}),
    ],
)
def test_a_partial_match_keeps_the_old_limit(env, ev):
    assert call(env, ev) is None


def test_the_label_only_in_the_edit_content_is_no_target(env):
    ev = event(
        "Edit",
        {"file_path": "/proj/other.yaml", "old_string": "a", "new_string": f"see {FILE}"},
    )
    assert call(dict(env, **{FT: "1"}), ev) is None


@pytest.mark.parametrize(
    "cmd",
    [
        f'git commit -m "fix {FILE}"',
        f"echo see {FILE}.bak",
        f"cat old_{FILE}",
        f"# {FILE}\ncat other.yaml",
    ],
)
def test_a_bash_command_that_only_mentions_the_label_keeps_the_old_limit(env, cmd):
    assert call(env, bash(cmd)) is None


def test_an_unbalanced_quote_in_the_command_gives_no_exact_match(env):
    assert call(env, bash(f'cat "{FILE}')) is None


def test_the_prompt_leg_has_no_exact_match(env):
    got = lr.candidates(
        f"please edit {FILE}",
        "prompt",
        gt.load_table(Path(env["NOBLIVION_GUARD_TABLE"])),
        {**env, "NOBLIVION_RECALL_LABELS": "1"},
    )
    assert got == []


def test_a_weak_label_never_counts_even_when_it_is_the_exact_file(env):
    assert (
        call(
            env,
            event("Edit", {"file_path": "/proj/PLAN_DOC.md", "old_string": "a", "new_string": "b"}),
        )
        is None
    )
    assert call(env, bash("cat PLAN_DOC.md")) is None


# ── a label under the limit, and the unit ───────────────────────────────────


def test_a_label_under_the_limit_is_not_marked(env):
    out = call(
        env,
        event("Edit", {"file_path": "/proj/small_target.py", "old_string": "a", "new_string": "b"}),
    )
    assert len(ids(out)) == 3
    assert "label_exact" not in last_log(env)


def _index(extra_labels: dict[str, list[int]] | None = None) -> dict:
    docs = [
        {
            "id": f"m{i}",
            "labels": {FILE: "file", **({"other.py": "file"} if i < 2 else {})},
            "mtime": 100 - i,
            "text": "t",
        }
        for i in range(14)
    ]
    return ml.build_index(docs)


def test_match_exact_keeps_three_and_marks_only_those_it_let_in():
    idx = _index()
    plain = ml.match([FILE], idx)
    assert plain == []
    got = ml.match([FILE], idx, exact=[FILE])
    assert len(got) == ml.EXACT_MAX == 3 and all(m.exact for m in got)
    assert all(m.as_dict()["exact"] is True for m in got)


def test_a_memory_that_matches_by_two_labels_is_not_marked_or_counted():
    # other.py is held by 2 memories (specific), so m0 and m1 match without the rule.
    idx = _index()
    got = ml.match([FILE, "other.py"], idx, exact=[FILE])
    by_id = {m.id: m for m in got}
    assert not by_id["m0"].exact and not by_id["m1"].exact
    assert sum(1 for m in got if m.exact) == 3 and len(got) == 5
    assert [m.id for m in got][:2] == ["m0", "m1"], "two labels outrank one"


def test_exact_labels_of_the_call():
    found = {FILE: "file", "x.md": "file", "git": "tool", "c.py": "file"}
    assert lr.exact_file_labels(f"/a/b/{FILE}", "file", found) == [FILE]
    assert lr.exact_file_labels(f"/a/b/{FILE}\nc.py", "file", found) == [FILE]
    assert lr.exact_file_labels(f"cat {FILE} c.py", "command", found) == [FILE, "c.py"]
    assert lr.exact_file_labels("/a/preferences.yaml", "file", found) == []
    assert lr.exact_file_labels(f"{FILE}", "prompt", found) == []
