# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local steps read the memory folders of the shared roots (NOBLIVION-133, -134).

The store ranks the notes of a shared root (``recall.shared_roots``, default
the home-folder project) in every session. The local steps must read the same
notes: the join of a prompt row to its note file (the rule text), the guard
table (``violates`` and triggers), and the continuity print and restore.

Fictional data only. ``HOME`` is a folder in the test's tmp dir.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from hookload import load_hook

hc = load_hook("hook_config", "hooktest_shared_folders_config")
gh = load_hook("guard_hook", "hooktest_shared_folders_guard")
ch = load_hook("continuity_hook", "hooktest_shared_folders_continuity")
rh = load_hook("recall_hook", "hooktest_shared_folders_recall")

RULE = "Never list labels with the search flag: the API lags."
NOTE = f"""---
name: label-list-search-lags
description: label list search lags the API
type: feedback
rule: {RULE}
apply: "Use gh label list without --search."
scope: tool
triggers: [gh label list --search, label-sync sync labels]
violates: "gh\\\\s+label\\\\s+list\\\\s+--search"
example_repeat: "gh label list --search heavy"
example_ok: "gh label list"
---

Body text.
"""
OWN_NOTE = """---
name: own-note
description: a note of the project
type: feedback
rule: Keep the project note short.
apply: "One line."
---

Body.
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name.startswith("CLAUDE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work" / "proj-b"
    work.mkdir(parents=True)
    projects = home / ".claude" / "projects"
    home_mem = projects / hc.project_slug(home) / "memory"
    own_mem = projects / hc.project_slug(work) / "memory"
    home_mem.mkdir(parents=True)
    own_mem.mkdir(parents=True)
    (home_mem / "feedback_label_list.md").write_text(NOTE)
    (own_mem / "feedback_own.md").write_text(OWN_NOTE)
    env = {
        "HOME": str(home),
        "NOBLIVION_DATA_DIR": str(tmp_path / "data"),
        "NOBLIVION_CONFIG": str(tmp_path / "config.json"),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }
    return {
        "home": home,
        "work": work,
        "home_mem": home_mem,
        "own_mem": own_mem,
        "env": env,
        "config": tmp_path / "config.json",
    }


def set_roots(world, roots):
    world["config"].write_text(json.dumps({"recall": {"shared_roots": roots}}))


def names(folders):
    return [f.parent.name for f in folders]


# 1. the folders --------------------------------------------------------------------------
def test_memory_dirs_holds_the_home_folder_by_default(world):
    got = hc.memory_dirs(str(world["work"]), world["env"])
    assert got == [world["own_mem"], world["home_mem"]]


def test_an_explicit_empty_list_turns_the_shared_folders_off(world):
    set_roots(world, [])
    assert hc.memory_dirs(str(world["work"]), world["env"]) == [world["own_mem"]]


def test_an_explicit_root_names_its_folder(world, tmp_path):
    other = world["home"] / ".claude" / "projects" / "-srv-rules" / "memory"
    other.mkdir(parents=True)
    set_roots(world, ["-srv-rules", "-srv-missing", "../escape"])
    got = hc.memory_dirs(str(world["work"]), world["env"])
    assert got == [world["own_mem"], other]


def test_a_home_session_lists_the_home_folder_once(world):
    got = hc.memory_dirs(str(world["home"]), world["env"])
    assert got == [world["home_mem"]]


def test_the_global_folder_is_not_listed_twice(world):
    env = dict(world["env"], NOBLIVION_GLOBAL_MEMORY_DIR=str(world["home_mem"]))
    got = hc.memory_dirs(str(world["work"]), env)
    assert got == [world["own_mem"], world["home_mem"]]


# 2. the guard table (NOBLIVION-133) ------------------------------------------------------
def guard(world, cmd):
    ev = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(world["work"]),
    }
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(ev)), stdout=out, environ=world["env"]) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def test_the_guard_table_of_a_second_folder_holds_the_home_note(world):
    out = guard(world, "gh label list --search heavy")
    assert out is not None
    assert out.get("permissionDecision") == "deny"
    assert "label-list-search-lags" in json.dumps(out) or RULE in json.dumps(out)


def test_the_guard_shows_a_row_for_the_trigger_of_a_home_note(world):
    out = guard(world, "label-sync sync labels now")
    assert out is not None and "permissionDecision" not in out
    assert RULE in out["additionalContext"]


def test_the_guard_stays_quiet_with_the_shared_folders_off(world):
    set_roots(world, [])
    assert guard(world, "gh label list --search heavy") is None


# 3. the local join of a prompt row (NOBLIVION-133) ---------------------------------------
def test_the_session_corpus_holds_the_home_note_after_the_project_notes(world):
    env = dict(world["env"], NOBLIVION_RECALL_MEMORY_DIR=str(world["own_mem"]))
    corpus = rh.load_session_corpus(env)
    assert sorted(m.rel_path for m in corpus) == ["feedback_label_list.md", "feedback_own.md"]


def test_the_project_note_shadows_a_home_note_of_the_same_file_name(world):
    (world["own_mem"] / "feedback_label_list.md").write_text(OWN_NOTE)
    env = dict(world["env"], NOBLIVION_RECALL_MEMORY_DIR=str(world["own_mem"]))
    got = {m.rel_path: m for m in rh.load_session_corpus(env)}
    assert got["feedback_label_list.md"].rule == "Keep the project note short."


# 4. the continuity print (NOBLIVION-134) -------------------------------------------------
def test_the_continuity_corpus_holds_the_home_note(world):
    env = dict(world["env"], NOBLIVION_RECALL_MEMORY_DIR=str(world["own_mem"]))
    got = sorted(m.rel_path for m in ch._corpus(env))
    assert got == ["feedback_label_list.md", "feedback_own.md"]


def test_the_precompact_print_names_a_home_lesson(world, tmp_path):
    cache = tmp_path / "cache"
    env = dict(
        world["env"],
        NOBLIVION_RECALL_MEMORY_DIR=str(world["own_mem"]),
        NOBLIVION_RECALL_CACHE_DIR=str(cache),
    )
    sid = "11111111-2222-3333-4444-555555555555"
    path = rh.session_state_file(str(cache), rh.SHOWN_SET_NAME, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = {"session": sid}
    state.update({kind: [] for kind in rh.SHOWN_KINDS})
    state["rows"] = ["feedback_label_list.md"]
    Path(path).write_text(json.dumps(state))
    text, rows = ch.rules_block(["label-list-search-lags"], ch._corpus(env), ch.PRECOMPACT_HEADER)
    assert rows == 1
    assert RULE in text
