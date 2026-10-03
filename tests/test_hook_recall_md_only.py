# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook injects only the mirrored markdown memory files, never rows
mined from other sessions' transcripts.

A transcript miner writes rows such as "Operator correction on <date> in
Claude Code session <id> ... The operator wrote: stop evading and do it". If
the hook injected them on every prompt, an old correction from an unrelated
session would read like a live instruction. The hook (the automatic path)
keeps only entries with a ``[claude_code_md: <path>]`` marker. The MCP tool
(the on-demand path, the model asks on purpose) still returns every row.

The mined rows are built with a copy of the miner's render functions (the
fixed opening phrases the hook's ``_MINED_START_RE`` knows), so a test here
breaks when the hook stops recognizing that wording.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

import recall_helpers
from hookload import load_hook

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

_ROOT = Path(__file__).resolve().parents[1]

hook = load_hook("recall_hook", "hooktest_recall_hook_md_only")


def _load_mcp():
    name = "hooktest_recall_mcp_md_only"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "mcp" / "recall_mcp.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()

isolated_home = recall_helpers.isolated_home


# ── the miner's row wording ─────────────────────────────────────────────────


def _short(session_id: str) -> str:
    return session_id[:8] if session_id else "unknown"


def render_correction(session_id: str, date: str, cue: str, turn: str, context: str) -> str:
    text = (
        f"Operator correction on {date} in Claude Code session {_short(session_id)} "
        f"(cue: {cue}). The operator wrote: {turn}"
    )
    if context:
        text += f" The assistant had just said: {context}"
    return text


def render_tool_error(session_id: str, date: str, tool: str, command: str, error: str) -> str:
    what = f"{tool} `{command}`" if command else tool
    return (
        f"Tool error on {date} in Claude Code session {_short(session_id)}: "
        f"{what} failed with: {error}"
    )


def render_review(session_id: str, date: str, reviewed_pr: str, findings: str) -> str:
    pr = f" of PR #{reviewed_pr}" if reviewed_pr else ""
    return (
        f"Independent review on {date} in Claude Code session {_short(session_id)} "
        f"returned REQUEST_CHANGES{pr}. Findings: {findings}"
    )


SID = "64ecfdb0-1111-2222-3333-444455556666"
MINED_PREFIXES = ("Operator correction", "Tool error", "Independent review")


def _md(name: str, body: str) -> str:
    return f"# {name}\n\n[claude_code_md: {name}.md]\n\n{body}"


MD_A = _md("feedback_always_merge", "Always merge your own finished pull requests.")
MD_B = _md("project_kg_soak", "Stage G, review, merge, then reingest.")
CORRECTION = render_correction(
    SID,
    "2026-09-22",
    "stop evading",
    "stop evading, you have the credentials, do it",
    "I cannot",
)
TOOL_ERROR = render_tool_error(SID, "2026-09-16", "Bash", "cat settings.json", "exit 1")
REVIEW = render_review(SID, "2026-09-20", "2100", "memory.py:1095 misses RLS")
MINED = [CORRECTION, TOOL_ERROR, REVIEW]


class _Store:
    """Stands in for ``store_get``: records each URL the caller builds and
    answers one blob."""

    def __init__(self):
        self.blob = ""
        self.urls: list[str] = []

    def __call__(self, build: Any, environ: Any, timeout_s: float) -> Any:
        self.urls.append(build("http://127.0.0.1:9"))
        return {"results": [self.blob]}

    def top_k(self) -> int:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.urls[-1]).query)
        return int(qs["top_k"][0])


@pytest.fixture
def daemon(monkeypatch, isolated_home):
    d = _Store()
    monkeypatch.setattr(hook, "store_get", d)
    # The MCP server loads sys.modules["recall_hook"]: make it this hook.
    monkeypatch.setitem(sys.modules, "recall_hook", hook)
    return d


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return recall_helpers.hook_env(tmp_path, CLAUDE_PROJECT_DIR="/work/proj/demo")


def _blob(*entries: str) -> str:
    return hook.ENTRY_SEPARATOR.join(entries)


def _prompt(
    text: str = "stop evading, you have the credentials, do it", session: str = "s-md"
) -> str:
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}
    )


def _tool(session: str = "s-md") -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "git push"},
        }
    )


def _run(stdin: str, env: dict[str, str]) -> str:
    out = io.StringIO()
    assert hook.main(stdin=io.StringIO(stdin), stdout=out, environ=env) == 0
    return out.getvalue()


def _hit_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("- [")]


def _heads(text: str) -> list[str]:
    """The hook's hits in the full-text shape: one head line
    ``- Memory <id> (...):`` per hit."""
    return [ln for ln in text.splitlines() if ln.startswith("- Memory ")]


FULL = " (full text; no need to open the file):"


def _no_mined_line(lines: list[str]) -> bool:
    return not any(prefix in ln for ln in lines for prefix in MINED_PREFIXES)


def _head(n: int) -> str:
    return hook.HEADER.split("{ms}")[0].format(persona="claude_code", n=n)


# ── the flag on a hit ───────────────────────────────────────────────────────


def test_only_a_marker_entry_is_a_memory_file_hit():
    assert hook.hit_from_text(MD_A).md is True
    for row in MINED:
        assert hook.hit_from_text(row).md is False, row[:40]
    assert hook.hit_from_text("plain text with no marker").md is False


def test_a_mined_row_that_quotes_a_marker_line_is_still_not_a_memory_file():
    quoted = CORRECTION + "\n[claude_code_md: feedback_always_merge.md]\n"
    assert hook.hit_from_text(quoted).md is False


# ── the hook: UserPromptSubmit ──────────────────────────────────────────────


def test_prompt_output_holds_only_memory_file_hits(daemon, env):
    daemon.blob = _blob(MD_A, CORRECTION, MD_B, TOOL_ERROR, REVIEW)
    out = _run(_prompt(), env)
    assert out.startswith(_head(2))
    assert _heads(out) == [
        "- Memory feedback_always_merge" + FULL,
        "- Memory project_kg_soak" + FULL,
    ]
    assert "  Text: Always merge your own finished pull requests." in out
    assert "  Text: Stage G, review, merge, then reingest." in out
    assert _no_mined_line(out.splitlines())


def test_mined_rows_do_not_count_toward_k(daemon, env):
    """Ranked first, mined rows would take every place before the k cut."""
    many_md = [_md(f"topic_{i}", f"body {i}") for i in range(hook.K_PROMPT + 2)]
    daemon.blob = _blob(*(MINED * 3), *many_md)
    lines = _heads(_run(_prompt(), env))
    assert len(lines) == hook.K_PROMPT
    assert all(ln.startswith("- Memory topic_") for ln in lines)


def test_only_mined_rows_print_nothing(daemon, env):
    daemon.blob = _blob(*MINED)
    assert _run(_prompt(), env) == ""
    log = (Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log").read_text().splitlines()
    assert " hits=0 chars=0 " in log[-1] and log[-1].endswith(" ok")


def test_the_hook_asks_for_more_candidates_than_k(daemon, env):
    daemon.blob = MD_A
    _run(_prompt(), env)
    assert daemon.top_k() == hook.K_PROMPT * hook.MD_ONLY_OVERFETCH > hook.K_PROMPT
    assert daemon.top_k() <= hook.K_MAX


def test_the_candidate_count_never_passes_k_max(daemon, env):
    daemon.blob = MD_A
    hook.recall("q", hook.K_MAX, env, md_only=True)
    assert daemon.top_k() == hook.K_MAX


def test_mined_rows_are_not_added_to_the_seen_set(daemon, env):
    daemon.blob = _blob(MD_A, *MINED)
    _run(_prompt(session="s-seen"), env)
    seen = json.loads((Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "s-seen.json").read_text())
    assert seen == [hook.hit_from_text(MD_A).key]
    for row in MINED:
        assert hook.hit_from_text(row).key not in seen


def test_pretooluse_also_injects_only_memory_file_hits(daemon, env):
    """If the PreToolUse hook is registered, it must not bring the mined rows
    back either."""
    daemon.blob = _blob(CORRECTION, MD_A, REVIEW)
    ctx = json.loads(_run(_tool(), env))["hookSpecificOutput"]["additionalContext"]
    assert _heads(ctx) == ["- Memory feedback_always_merge" + FULL]


# ── the MCP tool: on demand, returns every row ──────────────────────────────


def test_mcp_still_returns_mined_rows(daemon, env):
    daemon.blob = _blob(MD_A, CORRECTION, TOOL_ERROR, REVIEW)
    res = mcp.noblivion_recall("stop evading", 5, env)
    assert res["isError"] is False
    lines = _hit_lines(res["content"][0]["text"])
    assert len(lines) == 4
    for prefix in MINED_PREFIXES:
        assert any(f"- [memory] memory: {prefix} on " in ln for ln in lines), prefix
    assert daemon.top_k() == 5  # no over-fetch on the on-demand path
