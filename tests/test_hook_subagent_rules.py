# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ranked memory rules for a subagent (``hooks/subagent_rules_hook.py``).

The hook has two legs. PreToolUse on the Agent tool saves the task text.
SubagentStart finds the text, asks the local store and prints the rules as
``additionalContext`` for the subagent.

The payloads are the shapes Claude Code 2.1.261 sends: SubagentStart carries
``agent_id`` and ``agent_type`` and no prompt; the subagent's ``meta.json``
carries ``toolUseId``. Every test uses tmp paths for HOME, the data dir, the
cache, the guard table and the memory folder, and a fake store on 127.0.0.1
that answers the listener proof (design doc section 3.3) and checks the
bearer token.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

import recall_helpers
from hookload import HOOKS, load_hook

_SCRIPT = HOOKS / "subagent_rules_hook.py"

sub = load_hook("subagent_rules_hook", "hooktest_subagent_rules")
rh = sub.rh

SESSION = "0882fa64-b3ea-4f06-947c-e12df0435b3e"
AGENT = "a3fbcb505318778d5"
TOOL_USE = "toolu_01Wp9MQHNEhxVMi6yY1kKGp3"
TASK = "Create a git worktree, fix the failing merge gate test and commit the result."
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")
LOG_RE = re.compile(r"^\S+ event=\S+ session=\S+ hits=\d+ chars=\d+ ms=\d+ \S+$")


def _row_id(row: str) -> str:
    m = ROW_RE.match(row)
    assert m is not None, row
    return m["id"]


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_STORE_AUTOSTART", "0")
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


# ── the fake store ─────────────────────────────────────────────────────────


class _State:
    def __init__(self):
        self.mode = "hits"
        self.base = ""  # set by the fixture once the port is known
        self.token = ""
        self.results: Any = []
        # When set, the handler holds every store response (not the proof)
        # until ``released`` fires (the fixture fires it on teardown). This
        # is a hung store without a fixed sleep: the hook's own budget is what
        # ends the wait.
        self.hold_response = False
        self.released = threading.Event()
        self.requests: list[dict[str, Any]] = []  # every request, the proof included


def _make_handler(state: _State):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k):  # keep pytest output clean
            pass

        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            state.requests.append(
                {
                    "path": parsed.path,
                    "qs": qs,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            if parsed.path == "/health":
                reply = recall_helpers.health_reply(state.token, qs.get("nonce", ""))
                return self._send(200, json.dumps(reply).encode())
            if state.hold_response:
                state.released.wait()
            if state.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", state.base + "/leak")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if state.mode == "500":
                return self._send(500, b'{"detail":"boom"}')
            if state.mode == "401" or self.headers.get("Authorization") != (
                f"Bearer {state.token}"
            ):
                return self._send(401, b'{"detail":"missing or wrong token"}')
            if state.mode == "badjson":
                return self._send(200, b"<html>not json")
            answer = recall_helpers.index_answer(state.results, namespace=qs.get("project", ""))
            return self._send(200, json.dumps(answer).encode())

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def store(tmp_path):
    state = _State()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    t.start()
    port = server.server_address[1]
    state.base = f"http://127.0.0.1:{port}"
    state.token = recall_helpers.write_store_files(tmp_path / "data", port)
    try:
        yield state
    finally:
        state.released.set()  # let any held handler finish before shutdown
        server.shutdown()
        server.server_close()


@pytest.fixture
def corpus(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, 13):
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


def _results(n: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "rank": i,
            "id": 19000 + i,
            "title": f"feedback_m{i:02d}",
            "summary": f"summary {i}",
            "score": round(0.9 - i / 100, 2),
        }
        for i in range(1, n + 1)
    ]


@pytest.fixture
def env(store, tmp_path, corpus) -> dict[str, str]:
    store.results = _results()
    return recall_helpers.hook_env(
        tmp_path,
        HOME=str(tmp_path / "home"),
        NOBLIVION_RECALL_TIMEOUT_S="1.0",
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_MEMORY_DIR=str(corpus),
    )


def _pre(
    prompt: str = TASK,
    tool_use_id: str = TOOL_USE,
    agent_type: str | None = "general-purpose",
    tool: str = "Agent",
    session: Any = SESSION,
) -> dict[str, Any]:
    ti: dict[str, Any] = {"description": "Fix the merge gate", "prompt": prompt}
    if agent_type is not None:
        ti["subagent_type"] = agent_type
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_use_id": tool_use_id,
        "tool_input": ti,
        "cwd": "/tmp",
        "transcript_path": f"/nonexistent/projects/p/{session}.jsonl",
    }


def _start(
    tmp_path=None,
    agent_id: Any = AGENT,
    agent_type: str = "general-purpose",
    session: Any = SESSION,
    **extra: Any,
) -> dict[str, Any]:
    root = str(tmp_path / "projects") if tmp_path is not None else "/nonexistent/projects/p"
    doc = {
        "hook_event_name": "SubagentStart",
        "session_id": session,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "cwd": "/tmp",
        "transcript_path": f"{root}/{session}.jsonl",
    }
    doc.update(extra)
    return doc


def _run(payload: Any, env: dict[str, str], budget_s: float = 0.0):
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    out = io.StringIO()
    code = sub.main(stdin=stdin, stdout=out, environ=dict(env), budget_s=budget_s)
    return code, out.getvalue()


def _context(raw: str) -> str:
    doc = json.loads(raw)
    assert set(doc) == {"hookSpecificOutput"}
    assert doc["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    return doc["hookSpecificOutput"]["additionalContext"]


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ROW_RE.match(ln)]


def _log(tmp_path) -> list[str]:
    path = tmp_path / "cache" / sub.SUBDIR / "recall.log"
    return path.read_text().splitlines() if path.exists() else []


def _pending(tmp_path) -> list[str]:
    folder = tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _index_requests(store_state) -> list[dict[str, Any]]:
    return [r for r in store_state.requests if r["path"] == "/api/memories/index"]


# ── leg 1: PreToolUse on the Agent tool saves the task text ────────────────


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_the_agent_call_saves_the_task_text_and_prints_nothing(tmp_path, env, store, tool):
    code, out = _run(_pre(tool=tool), env)
    assert (code, out) == (0, ""), "nothing for the parent: no context, no decision"
    assert _pending(tmp_path) == [TOOL_USE + ".json"]
    doc = json.loads(
        (tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION / (TOOL_USE + ".json")).read_text()
    )
    assert doc["prompt"] == TASK and doc["agent_type"] == "general-purpose"
    assert store.requests == [], "the save leg makes no store call"


def test_another_tool_saves_nothing(tmp_path, env, store):
    payload = _pre(tool="Bash")
    assert _run(payload, env) == (0, "")
    assert _pending(tmp_path) == [] and store.requests == []
    assert _log(tmp_path)[-1].endswith("skip:tool")


def test_an_agent_call_with_no_type_is_general_purpose(tmp_path, env):
    _run(_pre(agent_type=None), env)
    folder = tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION
    assert (
        json.loads((folder / (TOOL_USE + ".json")).read_text())["agent_type"] == "general-purpose"
    )


def test_the_saved_file_is_private(tmp_path, env):
    _run(_pre(), env)
    path = tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION / (TOOL_USE + ".json")
    assert (path.stat().st_mode & 0o777) == 0o600


# ── leg 2: SubagentStart serves the rules to the subagent ─────────────────


def test_the_subagent_gets_the_ranked_rules_for_its_task_text(tmp_path, env, store):
    _run(_pre(), env)
    code, out = _run(_start(), env)
    assert code == 0
    text = _context(out)
    assert text.splitlines()[0] == sub.HEADER
    assert text.splitlines()[1].startswith("MEMORY RULES ranked for this task.")
    assert _rows(text)[0] == "- [id 19001] Do step 1 before the first command. (feedback_m01)"
    req = _index_requests(store)
    assert len(req) == 1, "one store call"
    assert req[0]["qs"]["q"] == TASK, "the query is the task text"
    assert req[0]["qs"]["project"] == "claude_code"
    assert _pending(tmp_path) == [], "the saved text is used once"


def test_the_default_is_eight_rows(tmp_path, env):
    _run(_pre(), env)
    rows = _rows(_context(_run(_start(), env)[1]))
    assert [_row_id(r) for r in rows] == [str(19000 + i) for i in range(1, 9)]


def test_the_row_count_is_settable(tmp_path, env):
    _run(_pre(), env)
    out = _run(_start(), dict(env, **{sub.K_ENV: "3"}))[1]
    assert len(_rows(_context(out))) == 3


@pytest.mark.parametrize("bad", ["", "0", "-2", "many"])
def test_an_unreadable_row_count_is_the_default(tmp_path, env, bad):
    _run(_pre(), env)
    out = _run(_start(), dict(env, **{sub.K_ENV: bad}))[1]
    assert len(_rows(_context(out))) == 8


def test_the_default_cap_is_2500_characters_header_included(tmp_path, env, corpus):
    for i in range(1, 13):
        path = corpus / f"feedback_m{i:02d}.md"
        path.write_text(
            path.read_text()
            .replace(
                f"rule: Do step {i} before the first command.",
                f"rule: Do step {i} " + "and keep doing it " * 22 + "to the end.",
            )
            .replace(
                f"run the tool number {i} with --flag",
                f"run the tool number {i} " + "with one more flag " * 40,
            )
        )
    _run(_pre(), env)
    text = _context(_run(_start(), env)[1])
    assert 1500 < len(text) <= 2500
    assert 1 <= len(_rows(text)) < 8, "the cap cuts whole rows"


def test_the_cap_is_settable(tmp_path, env):
    _run(_pre(), env)
    text = _context(_run(_start(), dict(env, **{sub.CAP_ENV: "600"}))[1])
    assert len(text) <= 600 and 1 <= len(_rows(text)) < 8


def test_the_floor_and_the_no_rule_drop_of_the_recall_hook_apply(tmp_path, env, store):
    rows = _results(4)
    rows[1]["score"] = 0.40  # under the 0.52 floor
    rows.append(
        {
            "rank": 5,
            "id": 19099,
            "title": "Tool error on 2026-09-16 in Claude Code session 6ff3",
            "summary": "",
            "score": 0.80,
        }
    )  # no file, no summary: no rule text
    store.results = rows
    _run(_pre(), env)
    text = _context(_run(_start(), env)[1])
    assert [_row_id(r) for r in _rows(text)] == ["19001", "19003", "19004"]
    status = _log(tmp_path)[-1].rsplit(" ", 1)[1]
    assert ":floor4of5" in status and ":norule_drop1" in status
    assert _index_requests(store)[0]["qs"]["top_k"] == str(rh.INDEX_CANDIDATE_TOP_K)


def test_no_row_over_the_floor_prints_nothing(tmp_path, env, store):
    store.results = [dict(r, score=0.30) for r in _results(3)]
    _run(_pre(), env)
    assert _run(_start(), env) == (0, "")
    assert _log(tmp_path)[-1].split(" hits=")[1].startswith("0 chars=0 ")


def test_an_index_option_in_the_command_wins_over_the_default(tmp_path, env):
    _run(_pre(), env)
    with_apply = _context(_run(_start(), env)[1])
    assert "  APPLY: run the tool number 1 with --flag" in with_apply
    _run(_pre(), env)
    off = _context(
        _run(_start(agent_id="a000000000000002"), dict(env, **{rh.INDEX_APPLY_ENV: ""}))[1]
    )
    assert "APPLY:" not in off and len(_rows(off)) == 8


# ── the parent session's state is not touched ─────────────────────────────


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and sub.SUBDIR not in p.relative_to(root).parts
    }


def test_the_parent_session_s_set_and_index_are_not_read_or_written(tmp_path, env):
    cache = str(tmp_path / "cache")
    # The parent was already shown rows 1 to 8, and has a last index.
    rh.record_shown(cache, SESSION, rh.SHOWN_ROW_KIND, [f"feedback_m{i:02d}" for i in range(1, 9)])
    rh.record_shown(cache, SESSION, "apply", ["feedback_m01"])
    rh.save_last_index(
        cache,
        SESSION,
        [rh.IndexLine(rank=1, mid=19001, title="feedback_m01", summary="s", score=None)],
    )
    before = _tree(tmp_path / "cache")
    _run(_pre(), env)
    text = _context(_run(_start(), env)[1])
    assert len(_rows(text)) == 8, "rows the PARENT saw are new to the subagent"
    assert "  APPLY: run the tool number 1 with --flag" in text
    assert _tree(tmp_path / "cache") == before, "nothing outside <cache>/subagent changed"


def test_the_subagent_has_its_own_set_keyed_by_session_and_agent(tmp_path, env):
    _run(_pre(), env)
    _run(_start(), env)
    own = (
        tmp_path / "cache" / sub.SUBDIR / rh.SESSION_DIR_NAME / f"{SESSION}.{AGENT}.shown_set.json"
    )
    doc = json.loads(own.read_text())
    assert doc["session"] == f"{SESSION}.{AGENT}" and len(doc[rh.SHOWN_ROW_KIND]) == 8
    assert rh.load_shown_set(str(tmp_path / "cache"), SESSION).get(rh.SHOWN_ROW_KIND, []) == []


def test_two_subagents_of_one_session_each_get_the_whole_list(tmp_path, env):
    _run(_pre(tool_use_id="toolu_a"), env)
    first = _context(_run(_start(agent_id="a000000000000001"), env)[1])
    _run(_pre(tool_use_id="toolu_b"), env)
    second = _context(_run(_start(agent_id="a000000000000002"), env)[1])
    assert _rows(first) == _rows(second) and len(_rows(first)) == 8


# ── how the task text is found ────────────────────────────────────────────


def _via(tmp_path) -> str:
    m = re.search(r"event=SubagentStart:(\S+)", _log(tmp_path)[-1])
    assert m is not None
    return m.group(1)


def _meta(tmp_path, agent_id: str, tool_use_id: str) -> None:
    folder = tmp_path / "projects" / SESSION / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Build the fix",
                "toolUseId": tool_use_id,
                "spawnDepth": 1,
                "requestShape": "background",
                "requestNonInteractive": True,
            }
        )
    )


def test_one_saved_text_is_found_without_a_meta_file(tmp_path, env, store):
    _run(_pre(), env)
    _run(_start(tmp_path), env)
    assert _via(tmp_path) == "fifo" and _index_requests(store)[0]["qs"]["q"] == TASK


def test_two_parallel_agents_of_one_type_are_told_apart_by_the_meta_file(tmp_path, env, store):
    _run(_pre("the first task text, about docker volumes", "toolu_first"), env)
    _run(_pre("the second task text, about postgres roles", "toolu_second"), env)
    _meta(tmp_path, AGENT, "toolu_second")
    _run(_start(tmp_path), env)
    assert _via(tmp_path) == "meta"
    assert _index_requests(store)[-1]["qs"]["q"] == "the second task text, about postgres roles"
    assert _pending(tmp_path) == ["toolu_first.json"], "the other agent's text stays"
    _meta(tmp_path, "a000000000000002", "toolu_first")
    _run(_start(tmp_path, agent_id="a000000000000002"), env)
    assert _index_requests(store)[-1]["qs"]["q"] == "the first task text, about docker volumes"


def test_without_a_meta_file_the_oldest_text_of_the_same_agent_type_is_used(tmp_path, env, store):
    _run(_pre("task for the explorer", "toolu_explore", agent_type="Explore"), env)
    time.sleep(0.01)
    _run(_pre("task for the general agent", "toolu_general"), env)
    _run(_start(tmp_path, agent_type="general-purpose"), env)
    assert _via(tmp_path) == "fifo"
    assert _index_requests(store)[-1]["qs"]["q"] == "task for the general agent"
    assert _pending(tmp_path) == ["toolu_explore.json"]


def test_with_no_text_of_that_type_the_oldest_of_the_session_is_used(tmp_path, env, store):
    _run(_pre("task for some custom agent", "toolu_c", agent_type="my-plugin:reviewer"), env)
    _run(_start(tmp_path, agent_type="reviewer"), env)
    assert _via(tmp_path) == "fifo_any"
    assert _index_requests(store)[-1]["qs"]["q"] == "task for some custom agent"


def _backdate(tmp_path, tool_use_id: str, seconds: float) -> None:
    path = tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION / f"{tool_use_id}.json"
    doc = json.loads(path.read_text())
    doc["ts"] -= seconds
    path.write_text(json.dumps(doc))


def test_a_text_of_a_call_that_never_started_is_not_given_to_a_later_subagent(tmp_path, env, store):
    _run(_pre("the task of a denied agent call", "toolu_denied"), env)
    _backdate(tmp_path, "toolu_denied", sub.PENDING_FIFO_MAX_AGE_S + 5)
    assert _run(_start(tmp_path), env) == (0, "")
    assert _log(tmp_path)[-1].endswith("skip:no_task_text")
    _run(_pre("the task of the agent that starts now", "toolu_now"), env)
    _run(_start(tmp_path), env)
    assert _index_requests(store)[-1]["qs"]["q"] == "the task of the agent that starts now"
    # The exact match by toolUseId has no age limit.
    _meta(tmp_path, "a000000000000009", "toolu_denied")
    _run(_start(tmp_path, agent_id="a000000000000009"), env)
    assert _via(tmp_path) == "meta"
    assert _index_requests(store)[-1]["qs"]["q"] == "the task of a denied agent call"


def test_a_text_saved_for_another_session_is_never_used(tmp_path, env, store):
    _run(_pre(session="another-session"), env)
    assert _run(_start(tmp_path), env) == (0, "")
    assert _log(tmp_path)[-1].endswith("skip:no_task_text") and _index_requests(store) == []


@pytest.mark.parametrize("key", ["subagent_prompt", "task_description", "prompt"])
def test_a_prompt_in_the_event_is_used_first(tmp_path, env, store, key):
    _run(_pre("the saved text"), env)
    out = _run(_start(tmp_path, **{key: "the text the event carries"}), env)[1]
    assert _rows(_context(out)) and _via(tmp_path) == "input"
    assert _index_requests(store)[-1]["qs"]["q"] == "the text the event carries"


def test_the_first_user_line_of_the_subagent_transcript_is_a_source(tmp_path, env, store):
    folder = tmp_path / "projects" / SESSION / "subagents"
    folder.mkdir(parents=True)
    (folder / f"agent-{AGENT}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "isSidechain": True,
                "agentId": AGENT,
                "sessionId": SESSION,
                "message": {"role": "user", "content": "the task text from the transcript"},
            }
        )
        + "\n"
    )
    out = _run(_start(tmp_path), env)[1]
    assert _rows(_context(out)) and _via(tmp_path) == "transcript"
    assert _index_requests(store)[-1]["qs"]["q"] == "the task text from the transcript"


def test_no_task_text_prints_nothing_and_says_so_in_the_log(tmp_path, env, store):
    assert _run(_start(tmp_path), env) == (0, "")
    assert _log(tmp_path)[-1].endswith("skip:no_task_text")
    assert _index_requests(store) == []


def test_the_query_is_cut_to_the_query_limit(tmp_path, env, store):
    _run(_pre("word " * 2000), env)
    _run(_start(), env)
    assert len(_index_requests(store)[-1]["qs"]["q"]) <= 2000
    _run(_pre("word " * 2000, "toolu_2"), env)
    _run(_start(agent_id="a000000000000002"), dict(env, **{sub.QUERY_CHARS_ENV: "120"}))
    assert len(_index_requests(store)[-1]["qs"]["q"]) <= 120


def test_old_saved_texts_are_removed(tmp_path, env):
    _run(_pre(tool_use_id="toolu_old"), env)
    path = tmp_path / "cache" / sub.SUBDIR / sub.PENDING / SESSION / "toolu_old.json"
    then = time.time() - sub.PENDING_MAX_AGE_S - 60
    os.utime(path, (then, then))
    _run(_pre(tool_use_id="toolu_new"), env)
    assert _pending(tmp_path) == ["toolu_new.json"]


# ── fail open, the budget, the switch, the log ────────────────────────────


@pytest.mark.parametrize("mode_", ["500", "401", "badjson", "redirect"])
def test_a_store_failure_prints_nothing_and_exits_0(tmp_path, env, store, mode_):
    _run(_pre(), env)
    store.mode = mode_
    assert _run(_start(), env) == (0, "")
    assert " fail:" in _log(tmp_path)[-1]


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null"])
def test_bad_stdin_prints_nothing_and_exits_0(tmp_path, env, raw):
    assert _run(raw, env) == (0, "")
    assert _log(tmp_path)[-1].endswith("fail:bad_stdin")


def test_an_error_inside_the_hook_prints_nothing_and_exits_0(tmp_path, env, monkeypatch):
    _run(_pre(), env)

    def boom(*_a, **_k):
        raise RuntimeError("secret text that must not reach the log")

    monkeypatch.setattr(rh, "_serve_index", boom)
    assert _run(_start(), env) == (0, "")
    assert _log(tmp_path)[-1].endswith("fail:internal:RuntimeError")
    assert "secret" not in "\n".join(_log(tmp_path))


def test_a_hung_store_is_cut_at_the_budget(tmp_path, env, store):
    _run(_pre(), env)
    store.hold_response = True
    t0 = time.monotonic()
    code, out = _run(_start(), dict(env, NOBLIVION_RECALL_TIMEOUT_S="6"), budget_s=0.4)
    assert (code, out) == (0, "")
    assert time.monotonic() - t0 < 3.0
    assert _log(tmp_path)[-1].endswith("fail:budget")


def test_the_budget_is_under_the_five_second_hook_timeout():
    assert 0 < sub.BUDGET_S < 5.0


@pytest.mark.parametrize("name", ["NOBLIVION_SUBAGENT_RULES_OFF", "NOBLIVION_RECALL_DISABLE"])
def test_the_switch_turns_both_legs_off(tmp_path, env, store, name):
    off = dict(env, **{name: "1"})
    assert _run(_pre(), off) == (0, "")
    assert _pending(tmp_path) == []
    _run(_pre(), env)
    assert _run(_start(), off) == (0, "")
    assert store.requests == []
    assert _log(tmp_path)[-1].endswith("skip:disabled")
    assert _pending(tmp_path) == [TOOL_USE + ".json"], "an off call uses nothing up"


def test_one_log_line_per_call_with_no_task_text_and_no_memory_text(tmp_path, env):
    _run(_pre(), env)
    out = _run(_start(), env)[1]
    lines = _log(tmp_path)
    assert len(lines) == 2 and all(LOG_RE.match(ln) for ln in lines), lines
    assert "event=AgentSave " in lines[0] and f"session={SESSION} " in lines[0]
    assert f"event=SubagentStart:fifo session={SESSION}.{AGENT} hits=8 " in lines[1]
    m = re.search(r"chars=(\d+)", lines[1])
    assert m is not None
    chars = int(m.group(1))
    assert chars == len(_context(out)) - len(sub.HEADER) - 1, (
        "chars is the index without the header"
    )
    joined = "\n".join(lines)
    assert "worktree" not in joined and "Do step" not in joined and "feedback_m" not in joined


def test_an_event_this_hook_does_not_serve_is_skipped(tmp_path, env):
    assert _run(
        {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt": "x"}, env
    ) == (0, "")
    assert _log(tmp_path)[-1].endswith("skip:event")


@pytest.mark.parametrize("field", ["session_id", "agent_id"])
def test_a_start_with_no_usable_id_is_skipped(tmp_path, env, field):
    _run(_pre(), env)
    payload = _start()
    payload[field] = "bad/../id"
    assert _run(payload, env) == (0, "")
    assert _log(tmp_path)[-1].endswith("skip:no_session")


def test_the_script_exits_0_on_garbage(tmp_path, env):
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input="garbage",
        text=True,
        capture_output=True,
        timeout=20,
        env={**env, "PATH": os.environ.get("PATH", "")},
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_the_script_serves_a_start_end_to_end(tmp_path, env):
    base = {**env, "PATH": os.environ.get("PATH", "")}
    subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(_pre()),
        text=True,
        capture_output=True,
        timeout=20,
        env=base,
        check=True,
    )
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(_start()),
        text=True,
        capture_output=True,
        timeout=20,
        env=base,
    )
    assert proc.returncode == 0 and proc.stderr == ""
    assert len(_rows(_context(proc.stdout))) == 8


# ── the rewrite mode (off by default) ─────────────────────────────────────


def test_rewrite_mode_appends_the_rules_to_the_prompt(tmp_path, env, store):
    rw = dict(env, **{sub.MODE_ENV: "rewrite"})
    code, out = _run(_pre(), rw)
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "allow"
    new = hso["updatedInput"]
    assert new["prompt"].startswith(TASK + "\n\n" + sub.HEADER + "\n")
    assert len(_rows(new["prompt"])) == 8
    assert new["description"] == "Fix the merge gate" and new["subagent_type"] == "general-purpose"
    assert _pending(tmp_path) == [], "no text is saved in this mode"
    assert _run(_start(), rw) == (0, ""), "the start leg adds nothing a second time"
    assert _log(tmp_path)[-1].endswith("skip:mode_rewrite")
    assert len(_index_requests(store)) == 1


def test_rewrite_mode_with_no_rows_leaves_the_call_alone(tmp_path, env, store):
    store.results = []
    assert _run(_pre(), dict(env, **{sub.MODE_ENV: "rewrite"})) == (0, "")


def test_the_default_mode_never_decides_or_rewrites(tmp_path, env):
    assert sub.mode({}) == "context" and sub.mode({sub.MODE_ENV: "other"}) == "context"
    assert _run(_pre(), env) == (0, "")
