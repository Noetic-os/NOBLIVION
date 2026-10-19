# SPDX-License-Identifier: AGPL-3.0-or-later
"""The static text of the recall reflex (NOBLIVION-123): tool descriptions, server instructions
and the ``recall-first`` skill.

This text is on by default and is paid on every turn, so the tests hold it short and hold the
named moments in all three places. The nudge is in ``test_hook_recall_nudge.py``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "recall-first" / "SKILL.md"

# Characters of the compact JSON of the tool list at origin/main (before this ticket), and the
# growth that the named moments may cost: about 30 tokens, paid on every turn.
OLD_TOOL_LIST_CHARS = 2385
TOOL_LIST_GROWTH_MAX = 130
RECALL_DESCRIPTION_MAX = 520
REMEMBER_DESCRIPTION_MAX = 320
INSTRUCTIONS_MAX = 330
SKILL_DESCRIPTION_MAX = 200
SKILL_BODY_MAX = 1100

MOMENTS = ("cannot verify", "merge", "restart", "ticket", "error")


def _load_mcp():
    spec = importlib.util.spec_from_file_location("nob123_mcp", ROOT / "mcp" / "recall_mcp.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nob123_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()


def _rpc(method, env=None, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return mcp.handle(msg, env if env is not None else {})["result"]


def test_recall_description_names_the_moments_and_stays_short():
    desc = mcp.TOOL_SCHEMA["description"]
    assert len(desc) <= RECALL_DESCRIPTION_MAX
    for moment in MOMENTS:
        assert moment in desc, moment
    assert "fetch_id" in desc  # the read-by-id use stays


def test_remember_description_says_recall_first_and_stays_short():
    desc = mcp.REMEMBER_SCHEMA["description"]
    assert len(desc) <= REMEMBER_DESCRIPTION_MAX
    assert "noblivion_recall first" in desc
    assert "secrets" in desc and "evidence" in desc  # the safety text stays


def test_tool_list_did_not_grow_past_the_budget():
    listed = _rpc("tools/list")["tools"]
    assert [t["name"] for t in listed] == ["noblivion_recall", "noblivion_remember"]
    size = len(json.dumps(listed, separators=(",", ":")))
    assert size <= OLD_TOOL_LIST_CHARS + TOOL_LIST_GROWTH_MAX, size


def test_initialize_carries_short_instructions_with_the_moments():
    result = _rpc("initialize")
    text = result["instructions"]
    assert len(text) <= INSTRUCTIONS_MAX
    for moment in MOMENTS:
        assert moment in text, moment
    assert "noblivion_remember" in text


@pytest.mark.parametrize("value", ["0", "false", "off", "no", " OFF "])
def test_instructions_switch_off(value):
    result = _rpc("initialize", {"NOBLIVION_MCP_INSTRUCTIONS": value})
    assert "instructions" not in result
    assert result["serverInfo"]["name"] == "noblivion-recall"


def test_instructions_are_on_unless_switched_off():
    assert "instructions" in _rpc("initialize", {"NOBLIVION_MCP_INSTRUCTIONS": "1"})
    assert "instructions" in _rpc("initialize", {})


def _skill_parts():
    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    assert m, "front matter"
    front = {}
    for line in m.group(1).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip()
    return front, m.group(2)


def test_skill_front_matter():
    front, _body = _skill_parts()
    assert front["name"] == "recall-first" == SKILL.parent.name
    assert 0 < len(front["description"]) <= SKILL_DESCRIPTION_MAX
    for moment in ("cannot verify", "merge", "restart", "ticket", "memory", "error"):
        assert moment in front["description"], moment
    # the model may call it: the setup skill is the only one that switches that off
    assert "disable-model-invocation" not in front


def test_skill_body_is_short_and_tells_the_steps():
    _front, body = _skill_parts()
    assert len(body) <= SKILL_BODY_MAX
    for word in ("noblivion_recall", "fetch_id", "noblivion_remember", "ToolSearch", "query"):
        assert word in body, word


def test_skill_is_inside_the_plugin_folder_the_plugin_loads():
    # Claude Code loads <plugin root>/skills/<name>/SKILL.md without a manifest entry
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "skills" not in manifest
    assert SKILL.is_file()
