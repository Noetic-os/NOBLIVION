# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-110. The MCP recall tool knows its session, and the hook reads the
memory folder once, not on every prompt.

1. ``fetch_id`` reads a rank through the calling session's last index and marks
   the calling session's shown-set, not the newest file of any session. A
   subagent's files are under ``cache/subagent``. With no session the newest
   file is used and the tool result says so.
2. The PreToolUse hook ``mcp_session_hook.py`` writes the session id into the
   arguments of each recall call, and a model cannot choose another one.
3. The parsed corpus cache: a file is read again only when its modification time
   or size changed, and the cache is the same corpus as a plain read.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import FakeStore, hook_env

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

hook = load_hook("recall_hook", "hooktest_mcp_session")
session_hook = load_hook("mcp_session_hook", "hooktest_mcp_session_hook")
corpus = load_hook("corpus", "hooktest_mcp_session_corpus")

A = "session-aaaa"
B = "session-bbbb"
AGENT = "agent-one"
NAMESPACE = "claude_code"


def _load_mcp():
    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = "hooktest_mcp_session_mcp"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()


def _lines(first_id: int, n: int = 5):
    return [
        hook.IndexLine(
            rank=i, mid=first_id + i, title=f"memory {first_id + i}", summary="s", score=None
        )
        for i in range(1, n + 1)
    ]


@pytest.fixture
def store(tmp_path):
    def fetch(path, _query):
        mid = int(path.rsplit("/", 1)[1])
        return {"namespace": NAMESPACE, "id": mid, "title": f"memory {mid}", "text": f"text {mid}"}

    with FakeStore(tmp_path / "data") as fake:
        fake.route("/api/memories/fetch/", fetch)
        yield fake


@pytest.fixture
def env(store, tmp_path) -> dict:
    (tmp_path / "memory").mkdir()
    return hook_env(
        tmp_path,
        NOBLIVION_RECALL_TIMEOUT_S="1.0",
        NOBLIVION_MEMORY_DIR=str(tmp_path / "memory"),
        NOBLIVION_RECALL_INDEX_RULE_ROWS="1",
        NOBLIVION_RECALL_SHOWN_SET="1",
        HOME=str(tmp_path / "home"),
    )


def _fetch(env, rank, **kw):
    out = mcp.noblivion_recall(environ=dict(env), fetch_id=rank, **kw)
    assert out["isError"] is False, out
    return out["content"][0]["text"]


def _two_sessions(env):
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    hook.save_last_index(cache, A, _lines(19000))
    hook.save_last_index(cache, B, _lines(29000))  # the newest file
    return cache


# -- 1. the session id in the MCP tool --------------------------------------------------


def test_fetch_rank_3_returns_the_rank_3_of_each_session(env):
    _two_sessions(env)
    text_a = _fetch(env, 3, session_key=A)
    text_b = _fetch(env, 3, session_key=B)
    assert text_a.startswith("GROUNDED MEMORY 19003:")
    assert "read as rank 3 = id 19003" in text_a
    assert text_b.startswith("GROUNDED MEMORY 29003:")
    assert "read as rank 3 = id 29003" in text_b
    assert mcp.NO_SESSION_NOTE not in text_a + text_b


def test_a_fetch_marks_the_shown_set_of_its_own_session(env):
    cache = _two_sessions(env)
    _fetch(env, 3, session_key=A)
    assert hook.load_shown_set(cache, A)["fetched"] == ["memory 19003"]
    assert hook.load_shown_set(cache, B)["fetched"] == []


def test_without_a_session_the_newest_index_is_used_and_the_result_says_so(env):
    _two_sessions(env)
    text = _fetch(env, 3)
    assert text.startswith("GROUNDED MEMORY 29003:")
    assert mcp.NO_SESSION_NOTE in text


def test_a_session_with_no_index_does_not_read_the_index_of_another(env):
    _two_sessions(env)
    out = mcp.noblivion_recall(environ=dict(env), fetch_id=3, session_key="session-cccc")
    # Rank 3 is not an id in the store of this test: it is passed on as the id 3.
    assert "GROUNDED MEMORY 3:" in out["content"][0]["text"]
    assert "19003" not in json.dumps(out) and "29003" not in json.dumps(out)


def test_a_model_supplied_bad_session_key_is_no_session(env):
    _two_sessions(env)
    text = _fetch(env, 3, session_key="../../etc/passwd")
    assert mcp.NO_SESSION_NOTE in text


def test_a_subagent_reads_its_own_cache(env):
    cache = _two_sessions(env)
    sub = os.path.join(cache, "subagent")
    hook.save_last_index(sub, f"{A}.{AGENT}", _lines(39000))
    text = _fetch(env, 3, session_key=A, agent_key=AGENT)
    assert text.startswith("GROUNDED MEMORY 39003:")
    assert hook.load_shown_set(sub, f"{A}.{AGENT}")["fetched"] == ["memory 39003"]
    # The parent session still reads its own index and its shown-set is untouched.
    assert _fetch(env, 3, session_key=A).startswith("GROUNDED MEMORY 19003:")
    assert hook.load_shown_set(cache, A)["fetched"] == ["memory 19003"]


def test_the_store_proved_session_wins_over_the_argument(env):
    _two_sessions(env)
    hook_obj = mcp._load_hook()
    session, _env = mcp._cache_identity(hook_obj, {"_learning_session_id": B}, A, None)
    assert session == B


def test_the_tool_schema_names_the_two_reserved_arguments():
    props = mcp.TOOL_SCHEMA["inputSchema"]["properties"]
    assert "session_key" in props and "agent_key" in props


# -- 2. the PreToolUse hook --------------------------------------------------------------

TOOL = "mcp__plugin_noblivion_noblivion__noblivion_recall"


def _payload(**extra):
    base = {"hook_event_name": "PreToolUse", "tool_name": TOOL, "tool_input": {"query": "x"}}
    base.update(extra)
    return base


def _updated(payload):
    out = session_hook.decide(payload)
    assert out is not None
    return out["hookSpecificOutput"]["updatedInput"]


def test_the_hook_adds_the_session_id():
    assert _updated(_payload(session_id=A)) == {"query": "x", "session_key": A}


def test_the_hook_adds_the_agent_id_inside_a_subagent():
    got = _updated(_payload(session_id=A, agent_id=AGENT))
    assert got == {"query": "x", "session_key": A, "agent_key": AGENT}


def test_the_hook_overwrites_what_the_model_supplied():
    ti = {"query": "x", "session_key": "other", "agent_key": "other"}
    assert _updated(_payload(session_id=A, tool_input=ti)) == {"query": "x", "session_key": A}


def test_the_hook_removes_the_model_value_when_it_has_no_valid_id():
    ti = {"query": "x", "session_key": "other"}
    assert _updated(_payload(tool_input=ti)) == {"query": "x"}
    assert _updated(_payload(session_id="a b/c", tool_input=ti)) == {"query": "x"}


@pytest.mark.parametrize(
    "name", ["Bash", "mcp__plugin_noblivion_noblivion__noblivion_remember", "noblivion_recall"]
)
def test_the_hook_ignores_other_tools(name):
    assert session_hook.decide(_payload(tool_name=name, session_id=A)) is None


def test_the_hook_prints_one_json_line_and_fails_open():
    out = io.StringIO()
    assert session_hook.main(io.StringIO(json.dumps(_payload(session_id=A))), out) == 0
    doc = json.loads(out.getvalue())
    assert doc["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    for junk in ("", "not json", "[]", "null"):
        out = io.StringIO()
        assert session_hook.main(io.StringIO(junk), out) == 0
        assert out.getvalue() == ""


def test_the_hook_accepts_the_same_ids_as_the_cache_files():
    assert session_hook.SESSION_ID_RE.pattern == corpus._SESSION_ID_RE.pattern


def test_the_plugin_matcher_selects_the_recall_tool_only():
    doc = json.loads((Path(__file__).resolve().parent.parent / "hooks/hooks.json").read_text())
    groups = [
        g
        for g in doc["hooks"]["PreToolUse"]
        if any("mcp_session_hook.py" in h.get("command", "") for h in g["hooks"])
    ]
    assert len(groups) == 1
    matcher = groups[0]["matcher"]
    assert re.fullmatch(matcher, TOOL)
    assert not re.fullmatch(matcher, TOOL.replace("recall", "remember"))
    assert not re.fullmatch(matcher, "Bash")


# -- 3. the parsed corpus cache -----------------------------------------------------------


def _write_memories(folder: Path, n: int = 12) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"feedback_item_{i:02d}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: feedback_item_{i:02d}",
                    f"description: about item {i}",
                    f"rule: Do step {i} first.",
                    "apply: " + json.dumps(f"run tool {i}\nthen check"),
                    "triggers:",
                    "  - phrase: step five",
                    "---",
                    "",
                    f"Body of item {i} with some words. " * 30,
                ]
            )
        )
    (folder / "empty.md").write_text("  \n")
    (folder / "note.txt").write_text("not memory")


def _same(a, b):
    assert [
        (m.rel_path, m.name, m.kind, m.description, m.rule, m.apply_block, m.tokens) for m in a
    ] == [(m.rel_path, m.name, m.kind, m.description, m.rule, m.apply_block, m.tokens) for m in b]
    assert [m.body[: corpus.CACHE_BODY_CHARS] for m in a] == [
        m.body[: corpus.CACHE_BODY_CHARS] for m in b
    ]
    assert [m.dropped for m in a] == [m.dropped for m in b]


@pytest.fixture
def reads(monkeypatch):
    """The names of the files ``load_memory_corpus`` read."""
    seen: list[str] = []
    real = corpus._read_memory_file

    def spy(path, base):
        seen.append(base)
        return real(path, base)

    monkeypatch.setattr(corpus, "_read_memory_file", spy)
    return seen


def test_the_cached_corpus_is_the_plain_corpus(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = str(tmp_path / "cache" / "corpus_cache.json")
    plain = corpus.load_memory_corpus(str(folder))
    _same(corpus.load_memory_corpus(str(folder), cache), plain)  # fills the cache
    _same(corpus.load_memory_corpus(str(folder), cache), plain)  # reads the cache
    assert len(plain) == 12


def test_a_second_load_reads_no_file(tmp_path, reads):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = str(tmp_path / "corpus_cache.json")
    corpus.load_memory_corpus(str(folder), cache)
    assert len(reads) == 13  # twelve notes and the blank file
    reads.clear()
    corpus.load_memory_corpus(str(folder), cache)
    assert reads == []


def test_a_changed_file_is_read_again_and_only_that_one(tmp_path, reads):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = str(tmp_path / "corpus_cache.json")
    corpus.load_memory_corpus(str(folder), cache)
    reads.clear()
    target = folder / "feedback_item_03.md"
    target.write_text(target.read_text().replace("Do step 3 first.", "Do the new step first."))
    got = {m.rel_path: m for m in corpus.load_memory_corpus(str(folder), cache)}
    assert reads == ["feedback_item_03.md"]
    assert got["feedback_item_03.md"].rule == "Do the new step first."
    reads.clear()
    corpus.load_memory_corpus(str(folder), cache)
    assert reads == []


def test_a_same_size_edit_with_a_new_mtime_is_read_again(tmp_path, reads):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = str(tmp_path / "corpus_cache.json")
    corpus.load_memory_corpus(str(folder), cache)
    reads.clear()
    target = folder / "feedback_item_04.md"
    target.write_text(target.read_text().replace("Do step 4 first.", "Do step 9 first."))
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    got = {m.rel_path: m for m in corpus.load_memory_corpus(str(folder), cache)}
    assert reads == ["feedback_item_04.md"]
    assert got["feedback_item_04.md"].rule == "Do step 9 first."


def test_a_removed_and_an_added_file_follow_the_folder(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = str(tmp_path / "corpus_cache.json")
    corpus.load_memory_corpus(str(folder), cache)
    (folder / "feedback_item_00.md").unlink()
    (folder / "feedback_new.md").write_text("---\nname: brand new\nrule: R.\n---\nbody\n")
    names = [m.rel_path for m in corpus.load_memory_corpus(str(folder), cache)]
    assert "feedback_item_00.md" not in names and "feedback_new.md" in names
    assert names == sorted(names)
    rows = json.loads(Path(cache).read_text())["files"]
    assert not any(k.endswith("feedback_item_00.md") for k in rows)


def test_two_folders_share_one_cache_file(tmp_path, reads):
    one, two = tmp_path / "one", tmp_path / "two"
    _write_memories(one, 3)
    _write_memories(two, 4)
    cache = str(tmp_path / "corpus_cache.json")
    corpus.load_memory_corpus(str(one), cache)
    corpus.load_memory_corpus(str(two), cache)
    reads.clear()
    assert len(corpus.load_memory_corpus(str(one), cache)) == 3
    assert len(corpus.load_memory_corpus(str(two), cache)) == 4
    assert reads == []


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"signature": "other", "files": {}}'])
def test_an_unusable_cache_file_is_rebuilt(tmp_path, junk):
    folder = tmp_path / "memory"
    _write_memories(folder)
    cache = tmp_path / "corpus_cache.json"
    cache.write_text(junk)
    _same(
        corpus.load_memory_corpus(str(folder), str(cache)), corpus.load_memory_corpus(str(folder))
    )
    assert json.loads(cache.read_text())["signature"] == corpus._CACHE_SIGNATURE


def test_a_malformed_row_is_read_from_the_file(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder, 3)
    cache = tmp_path / "corpus_cache.json"
    corpus.load_memory_corpus(str(folder), str(cache))
    doc = json.loads(cache.read_text())
    key = next(k for k in doc["files"] if k.endswith("feedback_item_01.md"))
    doc["files"][key][3] = 5  # a number where the kind should be text
    cache.write_text(json.dumps(doc))
    _same(
        corpus.load_memory_corpus(str(folder), str(cache)), corpus.load_memory_corpus(str(folder))
    )


def test_the_cache_file_is_private(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder, 2)
    cache = tmp_path / "corpus_cache.json"
    corpus.load_memory_corpus(str(folder), str(cache))
    assert (cache.stat().st_mode & 0o077) == 0


def test_an_unwritable_cache_still_loads_the_corpus(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder, 2)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    got = corpus.load_memory_corpus(str(folder), str(blocker / "corpus_cache.json"))
    assert len(got) == 2


def test_the_hook_uses_the_cache_unless_it_is_off(tmp_path):
    env = {"NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache")}
    assert hook.corpus_cache_path(env) == str(tmp_path / "cache" / "corpus_cache.json")
    assert hook.corpus_cache_path({**env, hook.CORPUS_CACHE_ENV: "0"}) is None
    assert hook.corpus_cache_path({**env, hook.CORPUS_CACHE_ENV: "off"}) is None


def test_the_session_corpus_fills_the_cache_once(tmp_path):
    folder = tmp_path / "memory"
    _write_memories(folder, 5)
    env = {
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "cache"),
        "NOBLIVION_RECALL_MEMORY_DIR": str(folder),
        "HOME": str(tmp_path / "home"),
    }
    first = hook.load_session_corpus(env)
    path = tmp_path / "cache" / "corpus_cache.json"
    stamp = path.stat().st_mtime_ns
    again = hook.load_session_corpus(env)
    assert [m.rel_path for m in again] == [m.rel_path for m in first] and len(first) == 5
    assert path.stat().st_mtime_ns == stamp, "an unchanged folder does not rewrite the cache"
    off = dict(env, **{hook.CORPUS_CACHE_ENV: "0"})
    path.unlink()
    assert len(hook.load_session_corpus(off)) == 5
    assert not path.exists()
