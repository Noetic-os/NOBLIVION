# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: the shown-set and the last index are one file per session.

Before this change both were one file per cache folder, and a file written
for another session reads as empty. So two sessions that ran at the same time
reset each other's row list on every prompt. Also covered: ``--reset-rows``,
the single file for an event with no session id, a reader with no session id
(the MCP server, ``mcp/recall_mcp.py``) and the pruning of old session files.

Every test uses tmp paths for the data dir, the cache, the guard table and the
memory folder, and a fake store on 127.0.0.1 (``recall_helpers.FakeStore``).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import FakeStore, hook_env, index_answer, index_row

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

hook = load_hook("recall_hook", "hooktest_recall_wi9_session_files")

A = "session-files-a"
B = "session-files-b"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")


def _load_mcp():
    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = "hooktest_recall_mcp_wi9_session_files"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_STORE_AUTOSTART", "0")
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, 7):
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


def _results(n: int = 6) -> list[dict]:
    return [
        index_row(i, 19000 + i, f"feedback_m{i:02d}", f"summary {i}", 0.9 - i / 100)
        for i in range(1, n + 1)
    ]


@pytest.fixture
def store(tmp_path):
    with FakeStore(tmp_path / "data") as fake:
        fake.route("/api/memories/index", index_answer(_results()))
        yield fake


@pytest.fixture
def env(store, tmp_path, corpus) -> dict[str, str]:
    return hook_env(
        tmp_path,
        NOBLIVION_RECALL_TIMEOUT_S="1.0",
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_MEMORY_DIR=str(corpus),
        NOBLIVION_RECALL_INDEX="1",
        **{
            hook.MEMORY_DIR_ENV: str(corpus),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_ROW_DEDUPE_ENV: "1",
        },
    )


def _serve(env: dict[str, str], sid: object) -> str:
    payload: dict[str, object] = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "a prompt about steps",
    }
    if sid is not None:
        payload["session_id"] = sid
    out = io.StringIO()
    assert hook.main(stdin=io.StringIO(json.dumps(payload)), stdout=out, environ=dict(env)) == 0
    return out.getvalue().rstrip("\n")


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _line(mid: int, title: str):
    return hook.IndexLine(rank=1, mid=mid, title=title, summary="s", score=None)


# ── the defect this item fixes ──────────────────────────────────────────────


def test_a_parallel_session_does_not_reset_the_row_list_of_another(tmp_path, env, store):
    assert len(_rows(_serve(env, A))) == 6
    assert len(_rows(_serve(env, B))) == 6, "B's first index is whole"
    assert _serve(env, A) == "", "A was shown all 6 rows; B's prompt must not bring them back"
    assert _serve(env, B) == ""
    assert store.store_paths and all(p == "/api/memories/index" for p in store.store_paths)


def test_each_session_has_its_own_two_files(tmp_path, env):
    _serve(env, A)
    _serve(env, B)
    folder = tmp_path / "cache" / hook.SESSION_DIR_NAME
    names = sorted(p.name for p in folder.iterdir() if not p.name.endswith(".lock"))
    # The trust event spool (on by default, design doc section 12.3) is a
    # third per-session file; this test is about the two index files.
    assert {n for n in names if n.endswith(".trust-events.jsonl")} <= {
        f"{A}.trust-events.jsonl",
        f"{B}.trust-events.jsonl",
    }
    names = [n for n in names if not n.endswith(".trust-events.jsonl")]
    assert names == [
        f"{A}.last_index.json",
        f"{A}.shown_set.json",
        f"{B}.last_index.json",
        f"{B}.shown_set.json",
    ]
    assert not (tmp_path / "cache" / hook.SHOWN_SET_NAME).exists()
    assert not (tmp_path / "cache" / hook.LAST_INDEX_NAME).exists()


def test_a_session_never_reads_the_set_of_another(tmp_path):
    cache = str(tmp_path)
    assert hook.record_shown(cache, A, "apply", ["x"])
    assert hook.record_shown(cache, B, "apply", ["y"])
    assert hook.load_shown_set(cache, A)["apply"] == ["x"]
    assert hook.load_shown_set(cache, B)["apply"] == ["y"]
    assert hook.load_shown_set(cache, "session-files-c")["apply"] == []


def test_the_last_index_of_a_session_survives_the_index_of_another(tmp_path):
    cache = str(tmp_path)
    hook.save_last_index(cache, A, [_line(19001, "a")])
    hook.save_last_index(cache, B, [_line(19002, "b")])
    assert [r["id"] for r in hook.load_last_index(cache, A)] == [19001]
    assert [r["id"] for r in hook.load_last_index(cache, B)] == [19002]
    assert hook.load_last_ids(cache, A) == {"a": 19001}
    assert hook.load_last_index(cache, "session-files-c") == []


# ── --reset-rows ───────────────────────────────────────────────────────────


def test_reset_rows_empties_only_the_calling_session(tmp_path):
    cache = str(tmp_path)
    hook.record_shown(cache, A, hook.SHOWN_ROW_KIND, ["a1", "a2"])
    hook.record_shown(cache, B, hook.SHOWN_ROW_KIND, ["b1"])
    stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": A}))
    assert hook.main_reset_rows(stdin, {"NOBLIVION_RECALL_CACHE_DIR": cache}) == 0
    assert hook.load_shown_set(cache, A).get(hook.SHOWN_ROW_KIND, []) == []
    assert hook.load_shown_set(cache, B)[hook.SHOWN_ROW_KIND] == ["b1"]


def test_reset_rows_with_no_session_id_touches_the_single_file_only(tmp_path):
    cache = str(tmp_path)
    hook.record_shown(cache, A, "apply", ["a1"])
    (tmp_path / hook.SHOWN_SET_NAME).write_text(
        json.dumps({"session": None, "apply": ["single"], "fetched": [], "trigger": []})
    )
    old = time.time() - 60
    os.utime(tmp_path / hook.SHOWN_SET_NAME, (old, old))  # the newest file is A's
    assert hook.reset_shown_rows(cache, None)
    assert hook.load_shown_set(cache, A)["apply"] == ["a1"], "A's set is not the caller's"
    assert json.loads((tmp_path / hook.SHOWN_SET_NAME).read_text())["apply"] == []


def test_reset_rows_with_no_session_id_reads_the_single_file_not_the_newest(tmp_path):
    cache = str(tmp_path)
    (tmp_path / hook.SHOWN_SET_NAME).write_text(
        json.dumps({"session": None, "apply": ["single"], "fetched": [], "trigger": []})
    )
    old = time.time() - 60
    os.utime(tmp_path / hook.SHOWN_SET_NAME, (old, old))
    hook.save_shown_set(cache, hook._empty_shown(A))  # the newest file is A's, and empty
    assert hook.reset_shown_rows(cache, None)
    assert json.loads((tmp_path / hook.SHOWN_SET_NAME).read_text())["apply"] == [], (
        "an empty newest file must not hide that the single file has names"
    )


def test_after_a_reset_the_session_gets_its_whole_index_again(tmp_path, env):
    _serve(env, A)
    _serve(env, B)
    hook.reset_shown_rows(str(tmp_path / "cache"), A)
    assert len(_rows(_serve(env, A))) == 6
    assert _serve(env, B) == "", "B was not reset"


# ── no session id: the single file, as before ──────────────────────────────


def test_an_event_with_no_session_id_keeps_the_single_files(tmp_path, env):
    env = dict(env, **{hook.INDEX_APPLY_ENV: "1", hook.SHOWN_SET_ENV: "1"})
    first = _serve(env, None)
    assert len(_rows(first)) == 6
    second = _serve(env, None)
    assert len(_rows(second)) == 6, "no session: no row dedupe, as before"
    assert "APPLY:" in first and "APPLY:" not in second, (
        "the APPLY dedupe reads the single file, as before"
    )
    assert (tmp_path / "cache" / hook.LAST_INDEX_NAME).exists()
    assert (tmp_path / "cache" / hook.SHOWN_SET_NAME).exists()
    assert not (tmp_path / "cache" / hook.SESSION_DIR_NAME).exists()


@pytest.mark.parametrize("sid", [None, "", "bad/../id", 7, "x" * 200])
def test_an_unusable_session_id_is_the_single_file(tmp_path, sid):
    cache = str(tmp_path)
    assert hook.shown_set_file(cache, sid) == os.path.join(cache, hook.SHOWN_SET_NAME)
    assert hook.last_index_file(cache, sid) == os.path.join(cache, hook.LAST_INDEX_NAME)


def test_a_session_id_cannot_leave_the_session_folder(tmp_path):
    for sid in ("..", ".", "a.b", "A_b-c.9"):
        path = Path(hook.shown_set_file(str(tmp_path), sid))
        assert path.parent == tmp_path / hook.SESSION_DIR_NAME
        assert path.name == f"{sid}.{hook.SHOWN_SET_NAME}"


# ── a session that was open when the hook changed ──────────────────────────


def test_a_session_with_no_own_file_reads_the_single_file_of_its_session(tmp_path):
    cache = str(tmp_path)
    (tmp_path / hook.SHOWN_SET_NAME).write_text(
        json.dumps(
            {"session": A, "apply": [], "fetched": [], "trigger": [], "row": ["old1", "old2"]}
        )
    )
    (tmp_path / hook.LAST_INDEX_NAME).write_text(
        json.dumps({"session": A, "rows": [{"rank": 1, "id": 19009, "name": "old1"}], "ids": {}})
    )
    assert hook.load_shown_set(cache, A)[hook.SHOWN_ROW_KIND] == ["old1", "old2"]
    assert [r["id"] for r in hook.load_last_index(cache, A)] == [19009]
    assert hook.load_shown_set(cache, B).get(hook.SHOWN_ROW_KIND, []) == [], "not B's session"
    assert hook.load_last_index(cache, B) == []
    # The first write moves the set to the session's own file and keeps it.
    assert hook.record_shown(cache, A, hook.SHOWN_ROW_KIND, ["new"])
    own = json.loads(Path(hook.shown_set_file(cache, A)).read_text())
    assert own[hook.SHOWN_ROW_KIND] == ["new", "old1", "old2"]


def test_the_own_file_wins_over_the_single_file(tmp_path):
    cache = str(tmp_path)
    hook.record_shown(cache, A, "apply", ["own"])
    (tmp_path / hook.SHOWN_SET_NAME).write_text(
        json.dumps({"session": A, "apply": ["single"], "fetched": [], "trigger": []})
    )
    assert hook.load_shown_set(cache, A)["apply"] == ["own"]


# ── a reader with no session id (the MCP server) ───────────────────────────


def test_a_reader_with_no_session_id_reads_the_newest_file(tmp_path):
    cache = str(tmp_path)
    hook.save_last_index(cache, A, [_line(19001, "a")])
    hook.record_shown(cache, A, "apply", ["a"])
    old = time.time() - 60
    for path in (hook.last_index_file(cache, A), hook.shown_set_file(cache, A)):
        os.utime(path, (old, old))
    hook.save_last_index(cache, B, [_line(19002, "b")])
    hook.record_shown(cache, B, "apply", ["b"])
    assert [r["id"] for r in hook.load_last_index(cache)] == [19002]
    assert hook.load_shown_set(cache)["apply"] == ["b"]
    # The fetch records into that same file, and A's file is unchanged.
    assert hook.record_shown(cache, None, "fetched", ["got"])
    assert hook.load_shown_set(cache, B)["fetched"] == ["got"]
    assert hook.load_shown_set(cache, A)["fetched"] == []
    assert not (tmp_path / hook.SHOWN_SET_NAME).exists()


def test_the_mcp_rank_fetch_reads_the_session_file(tmp_path):
    mcp = _load_mcp()
    cache = str(tmp_path)
    hook.save_last_index(cache, A, [_line(19004, "a")])
    got, note = mcp._resolve_fetch_id(hook, 1, {"NOBLIVION_RECALL_CACHE_DIR": cache})
    assert got == 19004 and "rank 1" in note


# ── pruning ────────────────────────────────────────────────────────────────


def _age(path: str, days: float) -> None:
    then = time.time() - days * 86400
    os.utime(path, (then, then))


def _session(cache: str, sid: str, days: float) -> None:
    hook.record_shown(cache, sid, "apply", ["x"])
    hook.save_last_index(cache, sid, [_line(19001, "x")])
    for name in os.listdir(os.path.join(cache, hook.SESSION_DIR_NAME)):
        if name.startswith(sid + "."):
            _age(os.path.join(cache, hook.SESSION_DIR_NAME, name), days)


def _sessions(cache: str) -> list[str]:
    names = os.listdir(os.path.join(cache, hook.SESSION_DIR_NAME))
    return sorted({n.split(".", 1)[0] for n in names})


def test_files_older_than_seven_days_are_removed(tmp_path):
    cache = str(tmp_path)
    _session(cache, "old", 8)
    _session(cache, "fresh", 6)
    assert hook.prune_session_files(cache, None, {}) == 3, "set, lock and index of 'old'"
    assert _sessions(cache) == ["fresh"]


def test_the_calling_session_is_never_removed(tmp_path):
    cache = str(tmp_path)
    _session(cache, "mine", 30)
    _session(cache, "other", 30)
    hook.prune_session_files(cache, "mine", {})
    assert _sessions(cache) == ["mine"]


def test_at_most_n_sessions_are_kept_the_newest_first(tmp_path):
    cache = str(tmp_path)
    for i in range(6):
        _session(cache, f"s{i}", i * 0.1)  # s0 is the newest
    hook.prune_session_files(cache, None, {hook.SESSION_KEEP_MAX_ENV: "3"})
    assert _sessions(cache) == ["s0", "s1", "s2"]
    hook.prune_session_files(cache, "s2", {hook.SESSION_KEEP_MAX_ENV: "2"})
    assert _sessions(cache) == ["s0", "s2"], "the caller counts as one of the N"


def test_the_age_limit_is_settable(tmp_path):
    cache = str(tmp_path)
    _session(cache, "two-days", 2)
    hook.prune_session_files(cache, None, {hook.SESSION_KEEP_DAYS_ENV: "3"})
    assert _sessions(cache) == ["two-days"]
    hook.prune_session_files(cache, None, {hook.SESSION_KEEP_DAYS_ENV: "1"})
    assert _sessions(cache) == []


@pytest.mark.parametrize("bad", ["", "abc", "0", "-4"])
def test_an_unreadable_limit_is_the_default(tmp_path, bad):
    cache = str(tmp_path)
    _session(cache, "six-days", 6)
    _session(cache, "eight-days", 8)
    hook.prune_session_files(
        cache, None, {hook.SESSION_KEEP_DAYS_ENV: bad, hook.SESSION_KEEP_MAX_ENV: bad}
    )
    assert _sessions(cache) == ["six-days"]


def test_pruning_a_folder_that_does_not_exist_is_nothing(tmp_path):
    assert hook.prune_session_files(str(tmp_path / "none"), A, {}) == 0


def test_pruning_leaves_files_that_are_not_session_state(tmp_path):
    cache = str(tmp_path)
    _session(cache, "old", 9)
    other = tmp_path / hook.SESSION_DIR_NAME / "notes.txt"
    other.write_text("keep")
    _age(str(other), 90)
    hook.prune_session_files(cache, None, {})
    assert other.exists() and _sessions(cache) == ["notes"]


def test_a_prompt_prunes_the_old_files_of_other_sessions(tmp_path, env):
    cache = str(tmp_path / "cache")
    _session(cache, "gone", 9)
    _session(cache, "kept", 1)
    _serve(env, A)
    assert _sessions(cache) == sorted([A, "kept"])
    log = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()
    assert len(log) == 1 and " ok:" in log[0], "pruning adds no log line and no status"
