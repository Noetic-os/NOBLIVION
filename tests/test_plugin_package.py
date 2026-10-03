# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Claude Code plugin files (design doc 0001, section 13).

The shapes follow the Claude Code plugin reference (plugin manifest,
``hooks/hooks.json``, ``.mcp.json``, marketplace manifest). When the
``claude`` command is on PATH, ``claude plugin validate`` checks them too.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import noblivion

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
MCP_JSON = ROOT / ".mcp.json"
DEFAULT_CONFIG = ROOT / "config" / "config.default.json"

KEBAB = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SEMVER = re.compile(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?")
PLUGIN_KEYS = {
    "name",
    "version",
    "description",
    "displayName",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "defaultEnabled",
    "dependencies",
    "skills",
    "commands",
    "agents",
    "hooks",
    "mcpServers",
    "lspServers",
    "userConfig",
    "settings",
    "channels",
    "experimental",
}
HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionStart",
    "SessionEnd",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
}
SESSION_START_SOURCES = {"startup", "resume", "clear", "compact", "fork"}
PRE_COMPACT_TRIGGERS = {"manual", "auto"}
TOOL_NAMES = {"Bash", "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Agent", "Task"}
HOOK_COMMAND = re.compile(
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([a-z_]+\.py)"((?: --[a-z-]+)*)'
)

# The bindings of the reference install (event, matcher, hook, args): the
# hand-installed hooks under their new names. The reference install bound no
# trust report hook (the flush refreshes the report); store_client.py (E4) and
# mine_session_end.py (E10) are new.
EXPECTED = {
    ("SessionStart", "", "store_client.py", ""),
    ("SessionStart", "", "guard_table.py", " --rebuild"),
    ("SessionStart", "compact", "recall_hook.py", " --reset-rows"),
    ("SessionStart", "startup|resume|clear|compact", "continuity_hook.py", ""),
    ("SessionStart", "startup|resume|clear", "trust_session_line.py", ""),
    ("UserPromptSubmit", "", "stop_checks.py", " --mark-correction"),
    ("UserPromptSubmit", "", "recall_hook.py", ""),
    ("PreToolUse", "Bash", "guard_hook.py", ""),
    ("PreToolUse", "Read|Grep", "guard_hook.py", ""),
    ("PreToolUse", "Edit|Write|MultiEdit", "guard_hook.py", ""),
    ("PreToolUse", "Agent|Task", "subagent_rules_hook.py", ""),
    ("PostToolUse", "Write|Edit|MultiEdit", "memory_fields_hook.py", ""),
    ("PostToolUse", "Write|Edit|MultiEdit", "memory_sync_hook.py", ""),
    ("PostToolUse", "Bash", "error_recall_hook.py", ""),
    ("PostToolUse", "Bash", "memory_sync_hook.py", ""),
    ("PostToolUseFailure", "Bash", "error_recall_hook.py", ""),
    ("PostToolUseFailure", "Bash", "guard_hook.py", ""),
    ("PreCompact", "manual|auto", "continuity_hook.py", ""),
    ("SubagentStart", "", "subagent_rules_hook.py", ""),
    ("Stop", "", "stop_checks.py", ""),
    ("Stop", "", "trust_flush.py", ""),  # the trust flush (E5)
    ("SessionEnd", "", "mine_session_end.py", ""),  # the transcript miner (E10)
}


def _load(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path
    return doc


def _bindings() -> list[tuple[str, str, str, str]]:
    out = []
    for event, groups in _load(HOOKS_JSON)["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                m = HOOK_COMMAND.fullmatch(handler["command"])
                assert m, handler["command"]
                out.append((event, group.get("matcher", ""), m.group(1), m.group(2)))
    return out


def test_plugin_manifest():
    doc = _load(PLUGIN)
    assert set(doc) <= PLUGIN_KEYS
    assert KEBAB.fullmatch(doc["name"]) and doc["name"] == "noblivion"
    assert SEMVER.fullmatch(doc["version"])
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"$', pyproject, re.M).group(1)
    project_license = re.search(r'^license = "([^"]+)"$', pyproject, re.M).group(1)
    assert doc["version"] == version == noblivion.__version__
    assert isinstance(doc["author"], dict) and doc["author"]["name"]
    assert doc["license"] == project_license
    assert isinstance(doc["description"], str) and doc["description"]


def test_marketplace_manifest():
    doc = _load(MARKETPLACE)
    plugin = _load(PLUGIN)
    assert KEBAB.fullmatch(doc["name"])
    assert doc["owner"]["name"]
    (entry,) = doc["plugins"]
    assert entry["name"] == plugin["name"]
    assert entry["version"] == plugin["version"]
    # The marketplace and the plugin are the same repo: the source is its root.
    assert entry["source"] == "./"
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_hooks_json_shape():
    doc = _load(HOOKS_JSON)
    assert set(doc) <= {"description", "hooks"}
    assert isinstance(doc["hooks"], dict) and doc["hooks"]
    for event, groups in doc["hooks"].items():
        assert event in HOOK_EVENTS, event
        assert isinstance(groups, list) and groups
        for group in groups:
            assert set(group) <= {"matcher", "hooks"}
            matcher = group.get("matcher")
            if matcher is not None:
                parts = set(matcher.split("|"))
                if event == "SessionStart":
                    assert parts <= SESSION_START_SOURCES, matcher
                elif event == "PreCompact":
                    assert parts <= PRE_COMPACT_TRIGGERS, matcher
                else:
                    assert event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
                    assert parts <= TOOL_NAMES, matcher
            assert isinstance(group["hooks"], list) and group["hooks"]
            for handler in group["hooks"]:
                assert set(handler) <= {"type", "command", "timeout"}
                assert handler["type"] == "command"
                assert isinstance(handler["timeout"], int) and 0 < handler["timeout"] <= 60


def test_every_hook_command_points_at_an_existing_script():
    for _, _, script, args in _bindings():
        path = ROOT / "hooks" / script
        assert path.is_file(), script
        source = path.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source, script
        for flag in args.split():
            assert f'"{flag}"' in source, (script, flag)


def test_hook_bindings_match_the_reference_install():
    bindings = _bindings()
    assert len(bindings) == len(set(bindings)), "a hook is bound twice"
    assert set(bindings) == EXPECTED


def test_mcp_json_runs_the_recall_server():
    doc = _load(MCP_JSON)
    (name,) = doc["mcpServers"]
    server = doc["mcpServers"][name]
    assert server["command"] == "python3"
    (arg,) = server["args"]
    assert arg == "${CLAUDE_PLUGIN_ROOT}/mcp/recall_mcp.py"
    assert (ROOT / "mcp" / "recall_mcp.py").is_file()
    # MCP servers get no CLAUDE_PLUGIN_DATA of their own: pass the hooks' data dir.
    assert server["env"] == {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}
    source = (ROOT / "mcp" / "recall_mcp.py").read_text(encoding="utf-8")
    assert 'TOOL_NAME = "noblivion_recall"' in source


def test_json_files_parse_and_hold_no_comment_lines():
    for path in (PLUGIN, MARKETPLACE, HOOKS_JSON, MCP_JSON, DEFAULT_CONFIG):
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        assert text.endswith("\n")


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_claude_plugin_validate(tmp_path):
    for target in (ROOT, PLUGIN):
        proc = subprocess.run(
            ["claude", "plugin", "validate", str(target)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "warning" not in proc.stdout.lower(), proc.stdout


# ── default config ──────────────────────────────────────────────────────────


def _recall_hook():
    path = ROOT / "hooks" / "recall_hook.py"
    spec = importlib.util.spec_from_file_location("_e8_recall_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_config_recall_env_names_are_real_settings():
    doc = _load(DEFAULT_CONFIG)
    env = doc["recall"]["env"]
    assert env["NOBLIVION_RECALL_INDEX"] == "1"
    sources = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "hooks").glob("*.py"))
    for name, value in env.items():
        assert re.fullmatch(r"NOBLIVION_RECALL_[A-Z0-9_]+", name), name
        assert f'"{name}"' in sources, f"{name} is read by no hook"
        assert isinstance(value, str) and value.strip()
    assert isinstance(doc["recall"]["index_k"], int)


def test_recall_env_defaults_reach_the_session_env(tmp_path):
    hook = _recall_hook()
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(DEFAULT_CONFIG, data / "config.json")
    base = {"NOBLIVION_DATA_DIR": str(data), "HOME": str(tmp_path)}
    env = hook.session_env(base, {})
    assert env["NOBLIVION_RECALL_INDEX"] == "1"
    assert env["NOBLIVION_RECALL_INDEX_RERANK"] == "1"
    assert env["NOBLIVION_RECALL_INDEX_K"] == "30"  # recall.index_k
    # An env var that is set wins over the config.
    env = hook.session_env({**base, "NOBLIVION_RECALL_INDEX_MAX_CHARS": "500"}, {})
    assert env["NOBLIVION_RECALL_INDEX_MAX_CHARS"] == "500"
    # Only NOBLIVION_RECALL_* names, and only plain values, are applied.
    (data / "config.json").write_text(
        json.dumps(
            {
                "recall": {
                    "env": {"PATH": "/x", "NOBLIVION_GUARD_LOG": "1", "NOBLIVION_RECALL_X": True}
                }
            }
        )
    )
    env = hook.session_env(base, {})
    assert "PATH" not in env and "NOBLIVION_GUARD_LOG" not in env
    assert "NOBLIVION_RECALL_X" not in env
    assert "NOBLIVION_RECALL_INDEX" not in env


def test_without_config_the_index_stays_off(tmp_path):
    hook = _recall_hook()
    env = hook.session_env({"NOBLIVION_DATA_DIR": str(tmp_path), "HOME": str(tmp_path)}, {})
    assert "NOBLIVION_RECALL_INDEX" not in env


def test_hook_commands_parse_as_shell_words():
    for event, groups in _load(HOOKS_JSON)["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                words = shlex.split(handler["command"])
                assert words[0] == "python3", (event, words)
