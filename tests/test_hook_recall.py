# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook (hooks/recall_hook.py) on its search path, and the MCP
server (mcp/recall_mcp.py) with its one tool.

No real store. A local ``http.server`` thread plays the store: it answers the
listener proof on ``/health`` (design doc section 3.3), requires the bearer
token on every other path, and serves the search shape
(``{"results": ["<entries joined by \\n---\\n>"]}``, or a sentinel string),
plus the failure shapes: slow, dripping, oversized, redirect, 500, 401, bad
JSON. The fail-open contract is also proven with a raised exception and with a
real subprocess against a closed port.

Covered
  * rendering: header, one line per hit, entity from the marker path,
    200-char body, score n/a when the store returns none, 2000-char cap
  * k per event (5 prompt, 3 tool), project=claude_code, root, 300-char query
  * PreToolUse only for Bash | Edit | Write; Edit/Write query shape
  * UserPromptSubmit -> plain stdout; PreToolUse -> hookSpecificOutput JSON
  * empty result -> no output; timeout / non-200 / 401 / bad JSON / closed
    port / no token / raised exception / bad stdin -> exit 0 and no stdout
  * the token goes only to a proven listener and never reaches the log
  * the transport rule: plain http only to a loopback IP literal
  * per-session dedupe, score threshold
  * the log line has no query text and no memory body
  * MCP initialize -> tools/list -> tools/call round trip, errors, no cap

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import ast
import http.server
import importlib.util
import io
import json
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

_ROOT = Path(__file__).resolve().parents[1]
_HOOK = HOOKS / "recall_hook.py"
_MCP = _ROOT / "mcp" / "recall_mcp.py"

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

hook = load_hook("recall_hook", "hooktest_recall_hook_main")


def _load_mcp():
    name = "hooktest_recall_mcp_main"
    spec = importlib.util.spec_from_file_location(name, _MCP)
    assert spec is not None and spec.loader is not None, _MCP
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()

isolated_home = recall_helpers.isolated_home

CWD = "/work/proj/demo"
ROOT = "-work-proj-demo"  # the slug Claude Code gives CWD
THREAD_NAME = "noblivion-recall-http"


def head(n: int, persona: str = "claude_code") -> str:
    """The hit header up to the elapsed time."""
    return hook.HEADER.split("{ms}")[0].format(persona=persona, n=n)


@pytest.fixture(autouse=True)
def _mcp_uses_this_hook(monkeypatch, isolated_home):
    """The MCP server loads ``sys.modules["recall_hook"]`` when present: make
    that this file's hook, so a patch on ``hook`` reaches the MCP path too.
    HOME is a tmp folder for every test."""
    monkeypatch.setitem(sys.modules, "recall_hook", hook)


# ── fixtures: the fake store ────────────────────────────────────────────────

ENTRY_A = (
    "# feedback_always_merge\n\n"
    "Always merge your own finished pull requests\n\n"
    "[claude_code_md: feedback_always_merge.md]\n\n"
    "A pull request you opened is yours to merge.  Wait for fast gates,\n"
    "then merge through tools/merge-gate. SECRET-BODY-TOKEN must not leak."
)
ENTRY_B = (
    "# MEMORY\n\n"
    "The memory index\n\n"
    "[claude_code_md: MEMORY.md]\n\n"
    "## Every session\n- writing rule\n- act on your recommendation"
)
ENTRY_C = (
    "# project_kg_soak\n\n"
    "[claude_code_md: project_kg_soak_test_sequence.md]\n\n"
    "Stage G, review, merge, then reingest."
)
BLOB_AB = ENTRY_A + hook.ENTRY_SEPARATOR + ENTRY_B


class _State:
    def __init__(self):
        self.mode = "hits"
        self.base = ""  # set by the fixture once the port is known
        self.token = ""  # set by the fixture: the token in the data dir
        self.blob = BLOB_AB
        self.results: Any = None  # when set, used verbatim as the "results" list
        self.namespace: Any = None  # when set, the "namespace" the answer names
        self.model: Any = None  # when set, the embedding model the answer names
        # When set, the handler holds every store response until ``released``
        # fires (the fixture fires it on teardown). This simulates a hung store
        # without a fixed sleep: the hook's own timeout ends the wait.
        self.hold_response = False
        self.released = threading.Event()
        self.drip_ended = threading.Event()  # "drip" mode: the client closed the connection
        self.requests: list[dict[str, Any]] = []  # every request but the proof
        self.health: list[dict[str, Any]] = []  # the listener-proof requests


def _make_handler(state: _State):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k):  # keep pytest output clean
            pass

        def do_GET(self):  # noqa: N802 — http.server API
            parsed = urllib.parse.urlparse(self.path)
            req = {
                "path": parsed.path,
                "qs": {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()},
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
            if parsed.path == "/health":
                state.health.append(req)
                reply = recall_helpers.health_reply(state.token, req["qs"].get("nonce", ""))
                return self._send(200, json.dumps(reply).encode())
            state.requests.append(req)
            if state.hold_response:
                state.released.wait()
            if state.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", state.base + "/leak")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if state.mode == "401" or req["headers"].get("authorization") != (
                f"Bearer {state.token}"
            ):
                return self._send(401, b'{"detail":"missing or wrong token"}')
            if state.mode == "500":
                return self._send(500, b'{"detail":"boom"}')
            if state.mode == "badjson":
                return self._send(200, b"<html>not json")
            if state.mode == "empty":
                return self._send(200, json.dumps({"results": ["No memories available."]}).encode())
            if state.mode == "huge":
                return self._send(200, b'{"results": ["' + b"x" * hook.RESPONSE_MAX_BYTES + b'"]}')
            if state.mode == "drip":
                # One byte per socket operation, forever: the client's per-
                # operation timeout never fires. Ends when the client shuts the
                # connection (drip_ended) or the fixture tears down (released).
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "10000000")
                self.end_headers()
                try:
                    while not state.released.is_set():
                        self.wfile.write(b" ")
                        state.released.wait(0.02)
                except OSError:
                    state.drip_ended.set()
                return None
            results = state.results if state.results is not None else [state.blob]
            ns = state.namespace if state.namespace is not None else req["qs"].get("project")
            answer = {"results": results, "namespace": ns}
            if state.model is not None:
                answer["model"] = state.model
            return self._send(200, json.dumps(answer).encode())

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def daemon(tmp_path):
    state = _State()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
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
def env(tmp_path) -> dict[str, str]:
    return recall_helpers.hook_env(
        tmp_path, NOBLIVION_RECALL_TIMEOUT_S="1.0", CLAUDE_PROJECT_DIR=CWD
    )


def _prompt(text: str, session: str = "sess-1") -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt": text,
        "cwd": CWD,
        "transcript_path": CWD + "/t.jsonl",
    }


def _tool(name: str, tool_input: dict[str, Any], session: str = "sess-1") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": name,
        "tool_input": tool_input,
        "cwd": CWD,
    }


def run_hook(payload: Any, env: dict[str, str]):
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    stdout = io.StringIO()
    code = hook.main(stdin=stdin, stdout=stdout, environ=env)
    return code, stdout.getvalue()


def read_log(env: dict[str, str]) -> list[str]:
    p = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log"
    return p.read_text().splitlines() if p.exists() else []


def _base_store(monkeypatch, base: str) -> None:
    """Make the proven store answer with ``base`` as its URL (a tampered or
    misconfigured store record), to test the transport rule in store_get."""
    monkeypatch.setattr(hook._STORE, "connect", lambda env, get_json, timeout_s: (base, "t" * 64))


# ── rendering ───────────────────────────────────────────────────────────────


def test_render_header_and_hit_line_format():
    hits = hook.parse_hits({"results": [BLOB_AB]})
    text = hook.render(hits, ms=42)
    lines = text.split("\n")
    assert lines[0] == "GROUNDED MEMORY (local memory store, namespace claude_code, 2 hits, 42 ms)"
    assert lines[0] == hook.HEADER.format(persona="claude_code", n=2, ms=42)
    assert len(lines) == 3
    assert re.match(
        r"^- \[feedback\] feedback_always_merge: A pull request you opened is yours to merge\. "
        r"Wait for fast gates, then merge through tools/merge-gate\..*\(score n/a\)$",
        lines[1],
    )
    assert lines[2].startswith("- [index] MEMORY: ## Every session - writing rule")


@pytest.mark.parametrize(
    "path,entity",
    [
        ("feedback_always_merge.md", "feedback"),
        ("project_kg_soak.md", "project"),
        ("reference_tracker_access.md", "reference"),
        ("topic_ci_gates_and_git.md", "topic"),
        ("user_profile.md", "user"),
        ("MEMORY.md", "index"),
        ("MEMORY_ARCHIVE.md", "index"),
        ("estate-topology.md", "reference"),
    ],
)
def test_entity_from_marker_path_matches_the_mirror_rule(path, entity):
    assert hook.entity_for_path(path) == entity
    h = hook.hit_from_text(f"# x\n\n[claude_code_md: {path}]\n\nbody")
    assert h.entity == entity


def test_body_is_first_200_chars_with_whitespace_collapsed():
    body = "word " * 100  # 500 chars, many spaces
    h = hook.hit_from_text(
        "# t\n\n[claude_code_md: reference_x.md]\n\n" + body.replace(" ", "\n\n  ")
    )
    line = h.render()
    m = re.match(r"^- \[reference\] t: (.*) \(score n/a\)$", line)
    assert m and len(m.group(1)) == hook.BODY_MAX_CHARS
    assert "\n" not in m.group(1) and "  " not in m.group(1)


def test_dict_shaped_result_carries_score_entity_and_title():
    hits = hook.parse_hits(
        {
            "results": [
                {
                    "content": "some body text",
                    "score": 0.8712,
                    "entity": "feedback",
                    "title": "rule",
                },
            ]
        }
    )
    assert hits[0].render() == "- [feedback] rule: some body text (score 0.87)"


def test_entry_without_marker_still_renders_as_memory():
    h = hook.hit_from_text("plain text row with no marker and no title")
    assert h.entity == "memory"
    assert h.render() == "- [memory] memory: plain text row with no marker and no title (score n/a)"


def test_output_capped_at_2000_chars_whole_lines_first():
    hits = [
        hook.hit_from_text(f"# title{i}\n\n[claude_code_md: topic_{i}.md]\n\n" + ("x" * 400))
        for i in range(30)
    ]
    text = hook.render(hits, ms=1)
    assert len(text) <= hook.OUTPUT_MAX_CHARS
    lines = text.split("\n")
    assert lines[0].startswith(head(len(lines) - 1))
    assert all(ln.startswith("- [topic]") for ln in lines[1:])
    assert 5 <= len(lines) <= 11  # ~230-char lines: whole lines kept, not one giant blob


def test_render_without_cap_is_unbounded_for_mcp():
    hits = [
        hook.hit_from_text(f"# t{i}\n\n[claude_code_md: topic_{i}.md]\n\n" + ("y" * 300))
        for i in range(30)
    ]
    assert len(hook.render(hits, ms=1, cap=None)) > hook.OUTPUT_MAX_CHARS


def test_a_body_with_front_matter_and_rules_stays_one_hit():
    """Mirrored memory files carry YAML front matter and horizontal rules, so
    the store's ``\\n---\\n`` join is ambiguous. Only a separator followed by
    a marker starts a new entry; anything else continues the entry before it."""
    body_a = "---\nname: rule\n---\n\n# Rule\n\nfirst half\n\n---\n\nsecond half"
    entry_a = "# feedback_rule\n\nA rule\n\n[claude_code_md: feedback_rule.md]\n\n" + body_a
    hits = hook.parse_hits({"results": [entry_a + hook.ENTRY_SEPARATOR + ENTRY_B]})
    assert [h.title for h in hits] == ["feedback_rule", "MEMORY"]
    assert "first half" in hits[0].body and "second half" in hits[0].body
    assert len({h.key for h in hits}) == 2


def test_render_zero_hits_is_empty_string():
    assert hook.render([], ms=5) == ""


@pytest.mark.parametrize("sentinel", sorted(hook.EMPTY_SENTINELS))
def test_every_store_sentinel_is_zero_hits(sentinel):
    assert hook.parse_hits({"results": [sentinel]}) == []


@pytest.mark.parametrize("payload", [{"foo": 1}, {"results": "not a list"}, [], "str", None])
def test_bad_shape_raises_recall_error(payload):
    with pytest.raises(hook.RecallError) as ei:
        hook.parse_hits(payload)
    assert ei.value.reason == "bad_shape"


# ── the hook: query, k, output shape ────────────────────────────────────────


def test_user_prompt_submit_k5_project_root_and_query(daemon, env):
    code, out = run_hook(_prompt("how do I merge a pull request"), env)
    assert code == 0
    req = daemon.requests[-1]
    assert req["path"] == "/api/memories/search"
    # k = 5 hits shown; the hook asks for k * MD_ONLY_OVERFETCH candidates
    assert req["qs"] == {
        "q": "how do I merge a pull request",
        "project": "claude_code",
        "top_k": "15",
        "root": ROOT,
    }
    assert out.startswith(head(2))
    assert not out.lstrip().startswith("{")  # plain text for this event


def test_pretooluse_bash_k3_query_is_the_command(daemon, env):
    code, out = run_hook(_tool("Bash", {"command": "git worktree add /work/x -b feat/y"}), env)
    assert code == 0
    assert daemon.requests[-1]["qs"]["top_k"] == "9"  # k = 3, times MD_ONLY_OVERFETCH
    assert daemon.requests[-1]["qs"]["q"] == "git worktree add /work/x -b feat/y"
    doc = json.loads(out)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["additionalContext"].startswith(head(2))
    assert "permissionDecision" not in hso  # the tool goes through the normal flow


def test_pretooluse_edit_query_is_path_plus_first_200_chars(daemon, env):
    new = "N" * 500
    run_hook(
        _tool("Edit", {"file_path": "/repo/src/a.py", "old_string": "o", "new_string": new}), env
    )
    q = daemon.requests[-1]["qs"]["q"]
    assert q.startswith("/repo/src/a.py N")
    assert q == ("/repo/src/a.py " + "N" * 200)


def test_pretooluse_write_query_uses_content(daemon, env):
    run_hook(_tool("Write", {"file_path": "/repo/docs/x.md", "content": "hello world"}), env)
    assert daemon.requests[-1]["qs"]["q"] == "/repo/docs/x.md hello world"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "NotebookEdit", "Agent", "WebFetch"])
def test_pretooluse_other_tools_are_skipped(daemon, env, tool):
    # Every query field is present, so a tool wrongly let through WOULD call the store.
    payload = _tool(tool, {"file_path": "/x", "command": "ls", "content": "c", "new_string": "n"})
    code, out = run_hook(payload, env)
    assert (code, out) == (0, "")
    assert daemon.requests == [] and daemon.health == []
    assert read_log(env)[-1].endswith("skip:tool")


def test_unknown_event_is_skipped(daemon, env):
    code, out = run_hook({"hook_event_name": "Stop", "session_id": "s"}, env)
    assert (code, out) == (0, "")
    assert daemon.requests == [] and daemon.health == []
    assert read_log(env)[-1].endswith("skip:event")


def test_blank_prompt_makes_no_call(daemon, env):
    code, out = run_hook(_prompt("   \n\t "), env)
    assert (code, out) == (0, "")
    assert daemon.requests == [] and daemon.health == []
    assert read_log(env)[-1].endswith("skip:empty_query")


def test_query_trimmed_to_300_chars(daemon, env):
    run_hook(_prompt("q" * 900), env)
    assert len(daemon.requests[-1]["qs"]["q"]) == hook.QUERY_MAX_CHARS == 300


def test_hits_above_k_are_dropped_client_side(daemon, env):
    daemon.blob = hook.ENTRY_SEPARATOR.join(
        f"# t{i}\n\n[claude_code_md: topic_{i}.md]\n\nbody {i}" for i in range(8)
    )
    _code, out = run_hook(_tool("Bash", {"command": "ls"}), env)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(head(3))


def test_empty_result_prints_nothing_and_logs_ok(daemon, env):
    daemon.mode = "empty"
    code, out = run_hook(_prompt("anything"), env)
    assert (code, out) == (0, "")
    line = read_log(env)[-1]
    assert " hits=0 chars=0 " in line and line.endswith(" ok")


def test_bearer_header_comes_from_the_token_file_and_never_reaches_the_log(daemon, env):
    run_hook(_prompt("x"), env)
    assert daemon.requests[-1]["headers"]["authorization"] == f"Bearer {daemon.token}"
    assert daemon.health and all("authorization" not in h["headers"] for h in daemon.health)
    assert daemon.token not in "\n".join(read_log(env))


def test_no_token_file_sends_nothing_and_fails_open(daemon, env, tmp_path):
    (tmp_path / "data" / "token").unlink()
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert daemon.requests == [] and daemon.health == []
    assert read_log(env)[-1].endswith("fail:no_token")


def test_disable_switch_makes_no_call(daemon, env):
    code, out = run_hook(_prompt("x"), dict(env, NOBLIVION_RECALL_DISABLE="1"))
    assert (code, out) == (0, "")
    assert daemon.requests == [] and daemon.health == []
    assert read_log(env)[-1].endswith("skip:disabled")


# ── fail open ───────────────────────────────────────────────────────────────


def test_timeout_exits_0_with_no_output_within_budget(daemon, env):
    daemon.hold_response = True
    env = dict(env, NOBLIVION_RECALL_TIMEOUT_S="0.5")
    t0 = time.monotonic()
    code, out = run_hook(_prompt("slow"), env)
    elapsed = time.monotonic() - t0
    assert (code, out) == (0, "")
    assert elapsed < 2.0
    assert read_log(env)[-1].endswith("fail:timeout")


def test_a_dripping_store_is_cut_at_the_deadline_and_the_worker_thread_ends(daemon):
    """A peer that sends one byte per socket operation never trips the
    per-operation timeout. So at the total deadline the caller shuts the
    socket down instead of merely ceasing to wait: the worker's read fails,
    the thread ends, and the server sees its connection close. Without this
    the long-lived MCP server would keep one thread and a growing buffer per
    timed-out call."""
    daemon.mode = "drip"
    t0 = time.monotonic()
    with pytest.raises(hook.RecallError) as info:
        hook.http_get_json(daemon.base + "/api/memories/search?q=x", daemon.token, 0.4)
    assert info.value.reason == "timeout"
    assert time.monotonic() - t0 < 1.5
    workers = [t for t in threading.enumerate() if t.name == THREAD_NAME]
    for t in workers:
        t.join(2.0)
    assert not any(t.is_alive() for t in workers)
    assert daemon.drip_ended.wait(2.0)  # the client closed it, not the fixture


def test_an_oversized_body_is_a_failure_not_a_buffer(daemon, env):
    """The client reads at most RESPONSE_MAX_BYTES + 1; a longer body is
    ``too_large`` and the hook fails open. No unbounded read exists."""
    daemon.mode = "huge"
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:too_large")


@pytest.mark.parametrize(
    "base,reason",
    [
        ("https://store.example.net:8443", None),  # TLS, certificate verified
        ("http://127.0.0.1:8893", None),
        ("http://127.0.0.2:8893", None),  # all of 127.0.0.0/8 is loopback
        ("http://localhost:8893", "plaintext_url"),  # a name, even this one: hosts/NSS decide it
        ("http://[::1]:8893", None),
        ("http://192.0.2.10:8893", "plaintext_url"),  # a LAN: a network observer exists
        ("http://store.example.net:8893", "plaintext_url"),  # a name is never trusted
        ("http://203.0.113.9:8893", "plaintext_url"),
        ("http://[2001:db8::1]:8893", "plaintext_url"),
        ("ftp://127.0.0.1", "bad_url"),
        ("http:///api", "bad_url"),
        ("http://127.0.0.1:notaport", "bad_url"),
    ],
)
def test_transport_rule_sends_the_token_only_over_tls_or_loopback(base, reason):
    assert hook.transport_reason(base + "/api/memories/search?q=x", {}) == reason


def test_the_store_url_passes_the_transport_rule(daemon, env):
    """The URL the hook builds from ``store.json`` is a loopback IP literal."""
    base = hook.base_url(env)
    assert base == daemon.base
    assert hook.transport_reason(hook.search_url(base, "x", 1), env) is None


def test_stuck_workers_cannot_accumulate_past_the_cap(monkeypatch):
    """getaddrinfo() has no timeout and cannot be cancelled, so a worker stuck
    in connect() outlives the deadline. The cap bounds how many can exist:
    past WORKER_MAX a call is ``busy`` at once and starts no thread. When the
    stuck workers end, the permits come back."""
    release = threading.Event()

    class _Stuck:
        sock = None

        def connect(self):
            release.wait()

        def request(self, *_a, **_k):
            raise ConnectionRefusedError("stub")

        def close(self):
            pass

    def alive():
        return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]

    for t in alive():  # leftovers from earlier tests, if any
        t.join(2.0)
    monkeypatch.setattr(hook, "_connection", lambda url, timeout_s: (_Stuck(), "/"))
    try:
        for _ in range(hook.WORKER_MAX):
            with pytest.raises(hook.RecallError) as info:
                hook.http_get_json("https://stalled.example.net/api", "k", 0.05)
            assert info.value.reason == "timeout"
        assert len(alive()) == hook.WORKER_MAX
        t0 = time.monotonic()
        with pytest.raises(hook.RecallError) as info:
            hook.http_get_json("https://stalled.example.net/api", "k", 5.0)
        assert info.value.reason == "busy"
        assert time.monotonic() - t0 < 1.0  # no thread, no wait
        assert len(alive()) == hook.WORKER_MAX
    finally:
        release.set()
    for t in alive():
        t.join(2.0)
    assert not alive()
    with pytest.raises(hook.RecallError) as info:  # the permits are back
        hook.http_get_json("https://stalled.example.net/api", "k", 1.0)
    assert info.value.reason == "unreachable:ConnectionRefusedError"


def test_a_refused_url_opens_no_connection_and_the_hook_fails_open(daemon, env, monkeypatch):
    """The reason comes from the URL alone. ``localhost`` names the live fake
    store here, so a connection would reach it; the log names the rule, the
    store sees no request, and the run ends well inside the budget."""
    port = daemon.base.rsplit(":", 1)[1]
    _base_store(monkeypatch, f"http://localhost:{port}")
    env = dict(env, NOBLIVION_RECALL_TIMEOUT_S="2.0")
    t0 = time.monotonic()
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:plaintext_url")
    assert time.monotonic() - t0 < 1.0
    assert daemon.requests == []


def test_a_redirect_is_a_failure_and_is_never_followed(daemon, env):
    """urllib would follow a 30x and copy the Authorization header onto the
    new URL; http.client never follows one. A 30x is a failure like any other
    non-200, the token cannot leave the store's origin, and the hook fails
    open."""
    daemon.mode = "redirect"
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:http_302")
    assert [r["path"] for r in daemon.requests] == ["/api/memories/search"]  # /leak never requested


def test_non_200_exits_0_with_no_output(daemon, env):
    daemon.mode = "500"
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:http_500")


def test_401_exits_0_with_no_output(daemon, env):
    daemon.mode = "401"
    code, out = run_hook(_tool("Bash", {"command": "ls"}), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:http_401")


def test_bad_json_exits_0_with_no_output(daemon, env):
    daemon.mode = "badjson"
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:bad_json")


def test_results_not_a_list_exits_0(daemon, env):
    daemon.results = "not-a-list"  # the fake serialises whatever it is given
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:bad_shape")


def test_closed_port_exits_0_with_no_output(env, tmp_path):
    recall_helpers.write_store_files(tmp_path / "data", 9)
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:store_down")


def test_bad_stdin_exits_0_with_no_output(daemon, env):
    code, out = run_hook("this is not json", env)
    assert (code, out) == (0, "")
    assert read_log(env)[-1].endswith("fail:bad_stdin")


def test_raised_exception_is_swallowed_exit_0_no_stdout(daemon, env, monkeypatch):
    """Negative test for the fail-open contract. ``run`` itself DOES raise
    here (proved first), so a ``main`` that stopped catching would fail this."""

    def _boom(*_a, **_k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(hook, "recall", _boom)
    with pytest.raises(RuntimeError):
        hook.run(json.dumps(_prompt("x")), io.StringIO(), env)
    code, out = run_hook(_prompt("x"), env)
    assert code == 0
    assert out == ""


def test_an_unexpected_exception_still_leaves_one_sanitized_log_line(daemon, env, monkeypatch):
    """The outer handler swallows everything, but the call must still leave
    its one log line: the exception's type, never its message, so diagnosis
    sees the failure and no secret can leak through the text."""

    def _boom(*_a, **_k):
        raise RuntimeError("secret-in-message")

    monkeypatch.setattr(hook, "render_capped", _boom)
    monkeypatch.setattr(hook, "render_full_capped", _boom)
    before = len(read_log(env))
    code, out = run_hook(_prompt("x"), env)
    assert (code, out) == (0, "")
    log = read_log(env)
    assert len(log) == before + 1
    assert log[-1].endswith(" fail:internal:RuntimeError")
    assert "secret-in-message" not in log[-1]


def test_raised_base_exception_is_swallowed(daemon, env, monkeypatch):
    def _boom(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(hook, "run", _boom)
    assert run_hook(_prompt("x"), env) == (0, "")


def test_subprocess_against_closed_port_exit_0_empty_stdout(tmp_path):
    recall_helpers.write_store_files(tmp_path / "data", 9)
    envp = recall_helpers.hook_env(
        tmp_path, HOME=str(tmp_path / "home"), NOBLIVION_RECALL_TIMEOUT_S="1.0"
    )
    p = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(_prompt("x")),
        capture_output=True,
        text=True,
        env=envp,
        timeout=20,
    )
    assert p.returncode == 0
    assert p.stdout == ""
    log = (tmp_path / "cache" / "recall.log").read_text()
    assert log.count("\n") == 1
    assert log.rstrip("\n").endswith("fail:store_down")


# ── session dedupe ──────────────────────────────────────────────────────────


def test_same_session_does_not_reinject_seen_hits(daemon, env):
    _c, first = run_hook(_prompt("merge"), env)
    assert first.startswith("GROUNDED MEMORY")
    _c, second = run_hook(_tool("Bash", {"command": "merge"}), env)
    assert second == ""
    assert " hits=0 chars=0 " in read_log(env)[-1]
    assert (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "sess-1.json").exists()


def test_other_session_sees_the_hits_again(daemon, env):
    run_hook(_prompt("merge", session="sess-1"), env)
    _c, out = run_hook(_prompt("merge", session="sess-2"), env)
    assert out.startswith(head(2))


def test_partial_dedupe_renders_only_the_fresh_hit(daemon, env):
    daemon.blob = ENTRY_A
    run_hook(_prompt("a"), env)
    daemon.blob = ENTRY_A + hook.ENTRY_SEPARATOR + ENTRY_C
    _c, out = run_hook(_prompt("a and c"), env)
    lines = out.rstrip("\n").split("\n")
    assert lines[0].startswith(head(1))
    # the full-text shape: the id head, then the memory's own text
    assert lines[1].startswith("- Memory project_kg_soak")
    assert "Stage G, review, merge, then reingest." in out
    assert sum(ln.startswith("- Memory ") for ln in lines) == 1


def test_a_hit_the_cap_left_out_is_not_marked_seen(daemon, env, monkeypatch):
    """A hit left out by the cap must stay fresh for the next prompt, and the
    header and log count only what was shown. The hook shows the full-text
    shape under FULL_OUTPUT_MAX_CHARS: an 1800-char cap fits two of five
    ~700-char hits."""
    monkeypatch.setattr(hook, "FULL_OUTPUT_MAX_CHARS", 1800)
    daemon.blob = hook.ENTRY_SEPARATOR.join(
        f"# {'t' * 600}{i}\n\n[claude_code_md: topic_{i}.md]\n\nbody {i}"
        for i in range(hook.K_PROMPT)
    )
    _c, first = run_hook(_prompt("x"), env)
    shown = [ln for ln in first.rstrip("\n").split("\n")[1:] if ln.startswith("- Memory ")]
    assert 0 < len(shown) < hook.K_PROMPT
    assert first.startswith(head(len(shown)))
    assert f" hits={len(shown)} " in read_log(env)[-1]
    _c, second = run_hook(_prompt("x"), env)
    rest = [ln for ln in second.rstrip("\n").split("\n")[1:] if ln.startswith("- Memory ")]
    assert rest and not set(shown) & set(rest)
    _c, third = run_hook(_prompt("x"), env)
    last = [ln for ln in third.rstrip("\n").split("\n")[1:] if ln.startswith("- Memory ")]
    assert not (set(shown) | set(rest)) & set(last)
    assert len(shown) + len(rest) + len(last) == hook.K_PROMPT


def test_concurrent_hooks_in_one_session_inject_each_hit_once(daemon, env):
    """Claude Code runs parallel tool calls, so hooks of one session overlap.
    The seen-set update is serialized, so each hit reaches the model once."""
    outs: list[str] = []
    guard = threading.Lock()

    def one(i: int) -> None:
        _c, out = run_hook(_prompt(f"merge {i}"), env)
        with guard:
            outs.append(out)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [
        ln for out in outs for ln in out.rstrip("\n").split("\n") if ln.startswith("- Memory ")
    ]
    assert len(lines) == 2 and len(set(lines)) == 2  # BLOB_AB holds two hits
    assert (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "sess-1.json.lock").exists()


def test_a_held_session_lock_bounds_the_wait_and_the_hook_still_answers(daemon, env):
    """Another hook process paused while holding the lock must not hold this
    one past its budget: after LOCK_WAIT_S the hook proceeds unlocked (fail
    open) and still answers, instead of blocking until Claude Code's outer
    timeout. The holder here is a separate open file description, which is
    what flock serializes on."""
    fcntl = pytest.importorskip("fcntl")
    cache = Path(env["NOBLIVION_RECALL_CACHE_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / "sess-1.json.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t0 = time.monotonic()
        code, out = run_hook(_prompt("x"), env)
        elapsed = time.monotonic() - t0
    assert code == 0 and out.startswith("GROUNDED MEMORY")
    assert hook.LOCK_WAIT_S <= elapsed < hook.LOCK_WAIT_S + 1.5
    assert read_log(env)[-1].endswith(" ok")


def test_unsafe_session_id_writes_no_file_but_still_answers(daemon, env):
    _c, out = run_hook(_prompt("x", session="../../etc/passwd"), env)
    assert out.startswith("GROUNDED MEMORY")
    cache = Path(env["NOBLIVION_RECALL_CACHE_DIR"])
    # Only log files: no session file. (memory-dir.log: this env has no memory folder.)
    assert sorted(p.name for p in cache.iterdir()) == ["memory-dir.log", "recall.log"]
    assert " session=- " in read_log(env)[-1]


# ── score threshold ─────────────────────────────────────────────────────────


def test_hits_below_min_score_are_skipped(daemon, env):
    daemon.model = hook.THRESHOLD_MODEL  # the default floor is for this model only
    daemon.results = [
        {"content": "# low\n\n[claude_code_md: topic_low.md]\n\nlow body", "score": 0.10},
        {"content": "# high\n\n[claude_code_md: topic_high.md]\n\nhigh body", "score": 0.95},
    ]
    _c, out = run_hook(_prompt("x"), env)
    assert "1 hits" in out.split("\n")[0]
    assert "- Memory topic_high (full text; no need to open the file):" in out
    assert "  Text: high body" in out
    assert "low" not in out


def test_min_score_env_overrides_default(daemon, env):
    daemon.results = [{"content": "# mid\n\n[claude_code_md: topic_mid.md]\n\nmid", "score": 0.5}]
    assert run_hook(_prompt("x"), dict(env, NOBLIVION_RECALL_MIN_SCORE="0.9"))[1] == ""
    assert (
        run_hook(_prompt("x", session="s2"), dict(env, NOBLIVION_RECALL_MIN_SCORE="0.4"))[1] != ""
    )


def test_scoreless_hit_passes_the_threshold(daemon, env):
    """A store answer with no score must pass the filter."""
    _c, out = run_hook(_prompt("x"), dict(env, NOBLIVION_RECALL_MIN_SCORE="0.99"))
    assert "2 hits" in out.split("\n")[0]


# ── log ─────────────────────────────────────────────────────────────────────


def test_log_line_has_the_fields_and_no_query_or_body(daemon, env):
    run_hook(_prompt("SECRET-QUERY-TOKEN please"), env)
    line = read_log(env)[-1]
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 event=UserPromptSubmit session=sess-1 "
        r"hits=2 chars=\d+ ms=\d+ ok$",
        line,
    ), line
    assert "SECRET-QUERY-TOKEN" not in line
    assert "SECRET-BODY-TOKEN" not in line
    assert "merge" not in line.lower()


def _logged_chars(env: dict[str, str]) -> int:
    last = read_log(env)[-1]
    m = re.search(r" chars=(\d+) ", last)
    assert m is not None, last
    return int(m.group(1))


def test_log_chars_equals_the_context_handed_over(daemon, env):
    _c, out = run_hook(_prompt("x"), env)
    assert _logged_chars(env) == len(out.rstrip("\n"))
    _c, out2 = run_hook(_tool("Bash", {"command": "y"}, session="s9"), env)
    ctx = json.loads(out2)["hookSpecificOutput"]["additionalContext"]
    assert _logged_chars(env) == len(ctx)  # the context, not the JSON envelope


def test_log_is_one_line_per_call(daemon, env):
    for i in range(4):
        run_hook(_prompt(f"q{i}", session=f"s{i}"), env)
    assert len(read_log(env)) == 4


# ── MCP server ──────────────────────────────────────────────────────────────


def _rpc(lines: list[dict[str, Any]], env: dict[str, str]) -> list[dict[str, Any]]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))
    stdout = io.StringIO()
    assert mcp.serve(stdin=stdin, stdout=stdout, environ=env) == 0
    return [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]


def _call(req_id: Any, arguments: dict[str, Any], name: str = "noblivion_recall") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_mcp_initialize_list_call_round_trip(daemon, env):
    out = _rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            _call(3, {"query": "merge gate", "k": 2}),
        ],
        env,
    )
    assert [m["id"] for m in out] == [1, 2, 3]  # the notification got no reply
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert out[0]["result"]["serverInfo"]["name"] == "noblivion-recall"
    tools = out[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["noblivion_recall", "noblivion_remember"]
    # `query` is not required, because a fetch has no query. Exactly one of
    # the two is required, which this schema cannot express, so the tool
    # body says so instead (tested below).
    assert tools[0]["inputSchema"]["required"] == []
    assert set(tools[0]["inputSchema"]["properties"]) == {
        "query",
        "k",
        "fetch_id",
        "include_mined",
    }
    assert tools[0]["inputSchema"]["properties"]["k"]["default"] == 5
    res = out[2]["result"]
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert text.startswith(head(2))
    assert "- [feedback] feedback_always_merge:" in text
    assert daemon.requests[-1]["qs"] == {
        "q": "merge gate",
        "project": "claude_code",
        "top_k": "2",
        "root": ROOT,
    }


def test_mcp_never_answers_a_message_without_an_id(daemon, env):
    """JSON-RPC 2.0: a message without an ``id`` member is a notification,
    whatever the method, and the server must not respond."""
    out = _rpc(
        [
            {"jsonrpc": "2.0", "method": "ping"},
            {"jsonrpc": "2.0", "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "noblivion_recall", "arguments": {"query": "x"}},
            },
            {"jsonrpc": "2.0", "method": "no/such/method"},
        ],
        env,
    )
    assert out == []
    assert daemon.requests == [] and daemon.health == []  # the id-less tools/call never ran


def test_mcp_rejects_a_request_without_jsonrpc_2_0(daemon, env):
    """JSON-RPC 2.0: the ``jsonrpc`` member MUST be exactly "2.0". A request
    without it, or with another value, is Invalid Request (with its id) and
    is never dispatched, so a mis-versioned tools/call cannot reach the
    store. A well-formed request in the same stream still works."""
    out = _rpc(
        [
            {"id": 1, "method": "ping"},
            {"jsonrpc": "1.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": 2.0,
                "id": 3,
                "method": "tools/call",
                "params": {"name": "noblivion_recall", "arguments": {"query": "x"}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ],
        env,
    )
    assert [m.get("id") for m in out] == [1, 2, 3, 4]
    assert all(m["error"]["code"] == -32600 for m in out[:3])
    assert out[3]["result"] == {}
    assert daemon.requests == [] and daemon.health == []  # the mis-versioned call never ran


def test_mcp_rejects_a_null_or_non_scalar_request_id(daemon, env):
    """MCP: a request id MUST be a string or an integer, never null. Such a
    message is neither a request nor a notification, so the server answers
    Invalid Request with id null (JSON-RPC 2.0) and never runs the tool."""
    out = _rpc(
        [
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            _call([1], {"query": "x"}),
            {"jsonrpc": "2.0", "id": "s-1", "method": "ping"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ],
        env,
    )
    assert [m.get("id") for m in out] == [None, None, None, "s-1", 7]
    assert all(m["error"]["code"] == -32600 for m in out[:3])
    assert all(m["result"] == {} for m in out[3:])
    assert daemon.requests == [] and daemon.health == []  # the bad-id tools/call never ran


def test_mcp_serve_exits_cleanly_when_the_client_closes_the_pipe(daemon, env):
    """A client that goes away mid-stream closes our stdout. The server must
    return 0 without a traceback and must not process the rest of the stream."""

    class _ClosedPipe(io.StringIO):
        def write(self, _s):
            raise BrokenPipeError(32, "Broken pipe")

    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"noblivion_recall","arguments":{"query":"x"}}}\n'
    )
    assert mcp.serve(stdin=stdin, stdout=_ClosedPipe(), environ=env) == 0
    assert daemon.requests == [] and daemon.health == []  # the loop stopped at the first write


def test_mcp_initialize_does_not_echo_an_unsupported_protocol_version(daemon, env):
    """MCP negotiation: an unsupported request gets a version the server does
    support, never an echo that claims support it does not have."""
    out = _rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "1999-01-01",
                    "capabilities": {},
                    "clientInfo": {"name": "t"},
                },
            }
        ],
        env,
    )
    assert out[0]["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_mcp_default_k_is_5_and_no_dedupe(daemon, env):
    out = _rpc([_call(1, {"query": "x"}), _call(2, {"query": "x"})], env)
    assert daemon.requests[-1]["qs"]["top_k"] == "5"
    first = out[0]["result"]["content"][0]["text"].split("\n")
    second = out[1]["result"]["content"][0]["text"].split("\n")
    assert first[0].startswith(head(2))
    assert second[0].startswith(head(2))
    assert first[1:] == second[1:] and len(first) == 3  # the second call repeats both hits
    assert not (Path(env["NOBLIVION_RECALL_CACHE_DIR"])).exists()  # no session file, no log


def test_mcp_has_no_output_cap(daemon, env):
    daemon.blob = hook.ENTRY_SEPARATOR.join(
        f"# t{i}\n\n[claude_code_md: topic_{i}.md]\n\n" + ("z" * 300) for i in range(20)
    )
    out = _rpc([_call(1, {"query": "x", "k": 20})], env)
    assert len(out[0]["result"]["content"][0]["text"]) > hook.OUTPUT_MAX_CHARS


def test_mcp_applies_the_min_score_threshold(daemon, env):
    """The threshold lives in recall(), so the MCP path honours it too."""
    daemon.model = hook.THRESHOLD_MODEL  # the default floor is for this model only
    daemon.results = [
        {"content": "# low\n\n[claude_code_md: topic_low.md]\n\nlow body", "score": 0.10},
        {"content": "# high\n\n[claude_code_md: topic_high.md]\n\nhigh body", "score": 0.95},
    ]
    call = _call(1, {"query": "x"})
    text = _rpc([call], env)[0]["result"]["content"][0]["text"]
    assert "1 hits" in text.split("\n")[0] and "high body" in text and "low" not in text
    text = _rpc([call], dict(env, NOBLIVION_RECALL_MIN_SCORE="0.05"))[0]["result"]["content"][0][
        "text"
    ]
    assert "2 hits" in text.split("\n")[0] and "low body" in text


def test_mcp_empty_result_returns_zero_hit_header(daemon, env):
    daemon.mode = "empty"
    out = _rpc([_call(1, {"query": "x"})], env)
    assert out[0]["result"]["content"][0]["text"].startswith(head(0))


def test_mcp_refuses_a_plaintext_url_as_a_tool_error(daemon, env, monkeypatch):
    port = daemon.base.rsplit(":", 1)[1]
    _base_store(monkeypatch, f"http://localhost:{port}")
    out = _rpc([_call(7, {"query": "x"})], env)
    res = out[0]["result"]
    assert res["isError"] is True
    assert res["content"][0]["text"] == "noblivion_recall: store URL refused (plaintext_url)"
    assert daemon.requests == []


def test_mcp_store_down_is_a_tool_error_not_a_crash(env, tmp_path):
    recall_helpers.write_store_files(tmp_path / "data", 9)
    out = _rpc([_call(7, {"query": "x"})], env)
    res = out[0]["result"]
    assert res["isError"] is True
    assert res["content"][0]["text"] == "noblivion_recall: memory store not running (store_down)"
    assert "Traceback" not in res["content"][0]["text"]


def test_mcp_bad_arguments_are_tool_errors(daemon, env):
    out = _rpc([_call(1, {}), _call(2, {}, name="other_tool")], env)
    assert out[0]["result"]["isError"] is True
    assert out[1]["error"]["code"] == -32602


def test_mcp_unknown_method_parse_error_and_ping(daemon, env):
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"nope"}\n{not json\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        '{"jsonrpc":"2.0","method":"notifications/cancelled"}\n'
    )
    stdout = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout, environ=env)
    out = [json.loads(ln) for ln in stdout.getvalue().splitlines()]
    assert out[0]["error"]["code"] == -32601
    assert out[1]["error"]["code"] == -32700 and out[1]["id"] is None
    assert out[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert len(out) == 3


# ── packaging contracts ─────────────────────────────────────────────────────


def _imported_names(path: Path) -> set:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# sys.stdlib_module_names is new in 3.10; the 3.10+ runs cover the 3.9 code
NEEDS_STDLIB_NAMES = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="sys.stdlib_module_names needs Python 3.10"
)


@NEEDS_STDLIB_NAMES
@pytest.mark.parametrize(
    "name", ["recall_hook", "corpus", "store_client", "hook_config", "memory_text"]
)
def test_hook_and_its_siblings_import_standard_library_only(name):
    names = _imported_names(HOOKS / f"{name}.py")
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


@NEEDS_STDLIB_NAMES
def test_mcp_imports_standard_library_only():
    names = _imported_names(_MCP)
    assert names <= set(sys.stdlib_module_names)
    assert "mcp" not in names  # hand-rolled on purpose, see the module docstring


def test_scripts_have_a_python3_shebang():
    for p in (_HOOK, _MCP):
        assert p.read_text().splitlines()[0] == "#!/usr/bin/env python3"
