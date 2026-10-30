# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label rows: a file name is no subject by itself (NOBLIVION-152).

A name of the stop list (``pyproject.toml``, ``pytest.ini``, ``db.py``, ``store.py``
...) or a name that more than 3 memory folders hold counts for a memory only when
the call names the file with a relative path that the memory names too, or when the
memory is in the project folder of the table. The exact-name exception of
NOBLIVION-131 needs the same.

What these tests hold:
1. FIRES: a stop-list name with the memory in the project folder; a stop-list
   name with a path that both sides name; a name held by 4 folders, in the project
   folder; a plain name in another folder (not on the list, held by 1 folder).
2. DOES NOT FIRE: the stop-list name bare, with the memory in another folder; the
   same name with a path that is another file; a name held by 4 folders; each name
   of the stop list; the exact-name exception with the memory in another folder.
3. THE INDEX: roots, folders per label and the paths of the memories outside the
   project folder; an index without roots (an old table) needs a path.
4. THE ROW: the rule line only, 250 characters or less, with a header under 50.
Every path is a tmp path: no test reads or writes a live file.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook

gh = load_hook("guard_hook", "guard_hook_t_label_roots")
gt = load_hook("guard_table", "guard_table_t_label_roots")
lr = load_hook("label_rows", "label_rows_t_label_roots")
ml = load_hook("memory_labels", "memory_labels_t_label_roots")

HEADER = "Memory rows by subject (label rows)"


def _mem(folder: Path, stem: str, body: str, rule: str = "Keep the standing ruling.") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {stem}", "description: a memory", f"rule: {rule}"]
    fm += ['apply: "check the ruling first"', "scope: tool"]
    (folder / f"{stem}.md").write_text("---\n" + "\n".join(fm) + "\n---\n" + body + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    home, data = tmp_path / "home", tmp_path / "data"
    for d in (home, data):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    e = {
        "NOBLIVION_GUARD_TABLE": str(tmp_path / "guard-table.json"),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "rc"),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",
        # on by default (NOBLIVION-142): a note outside the project folder has no readable
        # file for the table, so it would show no row. These tests are about the matching.
        "NOBLIVION_RECALL_RULES_ONLY": "0",
    }
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    return e


def build(env, tmp_path, own: dict, *others: dict) -> None:
    """Folder 0 is the project folder; each of ``others`` is a shared folder."""
    folders = []
    for i, notes in enumerate((own,) + others):
        folder = tmp_path / f"root{i}"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        for stem, body in notes.items():
            _mem(folder, stem, body)
        folders.append(folder)
    gt.rebuild(folders, Path(env["NOBLIVION_GUARD_TABLE"]))


def bash(cmd: str, sid: str = "s1") -> dict:
    ev = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"}
    return dict(ev, hook_event_name="PreToolUse")


def edit(path: str, sid: str = "s1") -> dict:
    ti = {"file_path": path, "old_string": "a", "new_string": "b"}
    return {
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": ti,
        "cwd": "/tmp",
    }


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
    return [ln.split()[2].rstrip(":") for ln in block.splitlines() if ln.startswith("- Memory ")]


def matches(env, query, subject="command") -> list[str]:
    table = gt.load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    return [m.id for m in lr.candidates(query, subject, table, dict(env))]


STOP = ["pyproject.toml", "pytest.ini", "db.py", "store.py"]


# -- 1. the stop list --------------------------------------------------------------
def test_the_stop_list_holds_the_names_of_the_ticket():
    assert set(STOP) <= ml.STOP_FILES
    for name in STOP:  # they stay labels: the index and the store keep them
        assert ml.labels(f"edit {name} now") == {name: "file"}


@pytest.mark.parametrize("name", STOP)
def test_a_stop_list_name_in_another_folder_does_not_match_bare(env, tmp_path, name):
    build(env, tmp_path, {"project_own": "Nothing here."}, {"project_other": f"Notes on {name}."})
    assert ids(call(env, bash(f"cat {name}"))) == []
    assert ids(call(env, bash(f"cat src/{name}"))) == []  # a path, but the note names none
    assert ids(call(env, edit(f"/work/src/{name}"))) == []


@pytest.mark.parametrize("name", sorted(ml.STOP_FILES))
def test_every_stop_list_name_needs_a_path_or_the_project_folder(env, tmp_path, name):
    """Written with a folder, so the name is a label even when it is a common
    attribute name (``config.json``)."""
    note = f"Notes on tools/{name}."
    build(env, tmp_path, {"project_own": "Nothing."}, {"project_other": note})
    assert ids(call(env, bash(f"cat src/{name}"))) == []
    assert ids(call(env, bash(f"cat other/tools/{name}", "s2"))) == ["project_other"]  # same path
    build(env, tmp_path, {"project_own": note}, {"project_other": "Nothing."})
    assert ids(call(env, bash(f"cat src/{name}", "s3"))) == ["project_own"]


@pytest.mark.parametrize("name", STOP)
def test_a_stop_list_name_in_the_project_folder_matches(env, tmp_path, name):
    build(env, tmp_path, {"project_own": f"Notes on {name}."}, {"project_other": f"See {name}."})
    assert ids(call(env, bash(f"cat {name}"))) == ["project_own"]


def test_a_path_that_both_sides_name_matches_another_folder(env, tmp_path):
    other = {"project_other": "The fix is in src/app/store.py and nowhere else."}
    build(env, tmp_path, {"project_own": "Nothing here."}, other)
    assert ids(call(env, edit("/work/src/app/store.py"))) == ["project_other"]
    assert ids(call(env, bash("sed -n 1,5p src/app/store.py", "s1b"))) == ["project_other"]
    assert ids(call(env, bash("sed -n 1,5p ./src/app/store.py", "s2"))) == ["project_other"]
    # the same name in another place, or with no folder, is another file
    assert ids(call(env, edit("/work/src/other/store.py", "s3"))) == []
    assert ids(call(env, bash("sed -n 1,5p store.py", "s4"))) == []
    assert ids(call(env, bash("sed -n 1,5p app/store.py", "s5"))) == ["project_other"]


def test_a_path_in_the_note_does_not_match_a_bare_call(env, tmp_path):
    build(env, tmp_path, {"project_own": "x"}, {"project_other": "Fix src/app/store.py first."})
    assert matches(env, "cat store.py") == []
    assert matches(env, "cat /work/src/app/store.py") == ["project_other"]


# -- the name held by more than 3 folders -----------------------------------------------
def four_roots(env, tmp_path, own_has_it: bool):
    own = {"project_own": "Rules for engine_core.py." if own_has_it else "Nothing."}
    shared = [{f"project_s{i}": "Rules for engine_core.py."} for i in range(3)]
    build(env, tmp_path, own, *shared)


def test_a_name_held_by_four_folders_needs_a_path_or_the_project_folder(env, tmp_path):
    four_roots(env, tmp_path, own_has_it=True)
    idx = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())["label_index"]
    i = idx["labels"].index("engine_core.py")
    assert idx["roots"] == 4 and idx["rdf"][i] == 4 > ml.ROOTS_MAX
    assert matches(env, "cat engine_core.py") == ["project_own"]  # the project folder only


def test_a_name_held_by_three_folders_still_matches_across_folders(env, tmp_path):
    build(
        env,
        tmp_path,
        {"project_own": "Nothing."},
        {"project_a": "Rules for engine_core.py."},
        {"project_b": "More rules for engine_core.py."},
    )
    assert sorted(matches(env, "cat engine_core.py")) == ["project_a", "project_b"]


def test_a_plain_name_in_another_folder_matches(env, tmp_path):
    build(env, tmp_path, {"project_own": "Nothing."}, {"project_other": "Rules for rotor_map.py."})
    assert matches(env, "cat rotor_map.py") == ["project_other"]


# -- the exact-name exception of NOBLIVION-131 --------------------------------------------
def test_the_exact_name_exception_needs_a_path_or_the_project_folder(env, tmp_path):
    name = "role_preferences.yaml"
    shared = {f"feedback_pref_{i:02d}": f"Ruling {i} about {name}." for i in range(14)}
    build(env, tmp_path, {"project_own": "Nothing."}, shared)
    assert matches(env, f"vi {name}") == []  # 14 memories, none in the project folder
    assert matches(env, f"vi config/{name}") == []
    own = {f"feedback_pref_{i:02d}": f"Ruling {i} about {name}." for i in range(14)}
    build(env, tmp_path, own, {"project_other": "x"})
    assert len(matches(env, f"vi {name}")) == 3  # EXACT_MAX, in the project folder
    paths = {f"feedback_pref_{i:02d}": f"Ruling {i} about config/{name}." for i in range(14)}
    build(env, tmp_path, {"project_own": "Nothing."}, paths)
    assert matches(env, f"vi {name}") == []
    assert len(matches(env, f"vi /srv/config/{name}")) == 3


# -- 3. the index ------------------------------------------------------------------------
def test_the_index_keeps_roots_folder_counts_and_paths(env, tmp_path):
    build(
        env,
        tmp_path,
        {"project_own": "Notes on pyproject.toml and tools/x/run.sh."},
        {"project_other": "Fix src/app/store.py and db.py."},
    )
    idx = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())["label_index"]
    docs = {d["id"]: d for d in idx["docs"]}
    assert idx["roots"] == 2 and "r" not in docs["project_own"] and docs["project_other"]["r"] == 1
    assert len(idx["rdf"]) == len(idx["labels"])
    # paths only for the labels that need one, and only for memories outside the project folder
    assert idx["paths"] == {
        "store.py": [[idx["docs"].index(docs["project_other"]), "src/app/store.py"]]
    }


def test_an_index_without_roots_needs_a_path(env, tmp_path):
    build(
        env, tmp_path, {"project_own": "Notes on store.py."}, {"project_other": "Fix src/store.py."}
    )
    table = gt.load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    idx = {k: v for k, v in table["label_index"].items() if k != "roots"}
    got = lambda q, p: [m.id for m in ml.match(ml.labels(q), idx, paths=p)]  # noqa: E731
    assert got("cat store.py", []) == []
    assert got("cat src/store.py", [ml.norm_path("/w/src/store.py")]) == ["project_other"]


def test_the_table_is_stale_when_the_table_version_changes(env, tmp_path, monkeypatch):
    folder = tmp_path / "root0"
    _mem(folder, "project_a", "x.py")
    before = gt.folder_stamp(folder)
    monkeypatch.setattr(gt, "TABLE_VERSION", gt.TABLE_VERSION + 1)
    assert gt.folder_stamp(folder) != before


# -- the paths of a call -----------------------------------------------------------------
def test_query_paths_are_paths_of_two_parts_or_more():
    assert lr.query_paths("cat store.py", "command") == []
    assert lr.query_paths("sed -n 1,3p ./Src/App/store.py", "command") == ["src/app/store.py"]
    assert lr.query_paths("/work/src/store.py\nbody store.py", "file") == ["/work/src/store.py"]
    assert lr.query_paths("see tools/run.sh and a.py", "prompt") == ["tools/run.sh"]
    assert lr.query_paths("git commit -m 'fix src/a.py'", "command") == []  # quoted text: one word


def test_paths_match_at_a_folder_border_only():
    n = ml.norm_path
    assert ml.paths_match(n("src/app/store.py"), n("/w/src/app/store.py"))
    assert ml.paths_match(n("/w/src/app/store.py"), n("app/store.py"))
    assert not ml.paths_match(n("src/app/store.py"), n("src/myapp/store.py"))
    assert not ml.paths_match("", n("a/b.py")) and n("store.py") == ""


# -- 4. the row --------------------------------------------------------------------------
def test_a_label_row_is_the_rule_line_only(env, tmp_path):
    rule = "Check the lock first. " * 30
    folder = tmp_path / "root0"
    _mem(folder, "project_own", "Notes on rotor_map.py.", rule=rule)
    gt.rebuild([folder], Path(env["NOBLIVION_GUARD_TABLE"]))
    ctx = call(env, bash("cat rotor_map.py"))["additionalContext"]
    lines = ctx.splitlines()
    assert lines[0] == lr.HEADER and len(lines[0]) < 50 and len(lines) == 2
    assert lines[1].startswith("- Memory project_own: Check the lock first.")
    assert len(lines[1].split(": ", 1)[1]) <= lr.ROW_RULE_CHARS == 250
    assert "Apply" not in ctx and "Labels" not in ctx and "Text" not in ctx


# -- a project folder that is itself a shared root ---------------------------------------
def test_a_hub_folder_has_no_own_project(env, tmp_path, monkeypatch):
    """A session in the home folder reads the notes of every project from one
    folder. None of them is "in the own project", so a stop-list name needs a path
    there too (the labelled rows of NOBLIVION-152 came from such sessions)."""
    projects = tmp_path / "home" / ".claude" / "projects"
    hub = projects / "-hub" / "memory"
    _mem(hub, "project_other_repo", "Notes on pyproject.toml of another repo, and rotor_map.py.")
    data = Path(os.environ["NOBLIVION_DATA_DIR"])
    (data / "config.json").write_text(json.dumps({"recall": {"shared_roots": ["-hub"]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    gt.rebuild([hub], Path(env["NOBLIVION_GUARD_TABLE"]))
    idx = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())["label_index"]
    assert idx["own"] is False
    assert matches(env, "cat pyproject.toml") == []  # a stop-list name: needs a path
    assert matches(env, "cat rotor_map.py") == ["project_other_repo"]  # a plain name still matches
    # the same folder as a plain project folder: its notes are the own project
    (data / "config.json").write_text(json.dumps({"recall": {"shared_roots": ["-elsewhere"]}}))
    gt.rebuild([hub], Path(env["NOBLIVION_GUARD_TABLE"]))
    assert matches(env, "cat pyproject.toml") == ["project_other_repo"]
