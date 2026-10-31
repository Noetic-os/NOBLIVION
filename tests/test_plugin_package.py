# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Claude Code plugin files (design doc 0001, section 13).

The shapes follow the Claude Code plugin reference (plugin manifest,
``hooks/hooks.json``, ``.mcp.json``, marketplace manifest). When the
``claude`` command is on PATH, ``claude plugin validate`` checks them too.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import noblivion
from hookload import load_hook

dispatch = load_hook("dispatch", "hooktest_package_dispatch")
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
NUDGE_MCP_MATCHER = "mcp__.+__([Cc]reate|[Aa]dd).*([Ii]ssue|[Tt]icket|[Cc]omment).*"
# NOBLIVION-162 step 2: the prompt command may start with the test of the warm-hook flag file.
WARM_GUARD = (
    r'(?:\[ -e "\$\{NOBLIVION_DATA_DIR:-\$\{CLAUDE_PLUGIN_DATA\}\}/warm-prompt-hooks" \] \|\| )?'
)
HOOK_COMMAND = re.compile(
    WARM_GUARD
    + r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([a-z_]+\.py)"((?: --[a-z-]+)*(?: [A-Z][A-Za-z]+)?)'
)

# The bindings of the reference install (event, matcher, hook, args): the
# hand-installed hooks under their new names. The reference install bound no
# trust report hook (the flush refreshes the report); store_client.py (E4) is new.
EXPECTED = {
    # NOBLIVION-162: one dispatcher command per event runs the modules of hooks/dispatch.py PLAN
    # (tests/test_hook_dispatch.py holds that table against the old per-module commands).
    ("SessionStart", "", "dispatch.py", " SessionStart"),
    ("UserPromptSubmit", "", "dispatch.py", " UserPromptSubmit"),
    ("PreCompact", "", "dispatch.py", " PreCompact"),
    ("SessionEnd", "", "learning_capture.py", ""),
    ("SessionStart", "startup|resume|clear", "version_check.py", ""),  # async
    ("PreToolUse", "Bash", "guard_hook.py", ""),
    # NOBLIVION-123, off by default. NOBLIVION-149: started only by `if` on a merge command.
    ("PreToolUse", "Bash if Bash(gh pr merge*)", "recall_nudge.py", ""),
    ("PreToolUse", "Bash if Bash(glab mr merge*)", "recall_nudge.py", ""),
    ("PreToolUse", "Read|Grep", "guard_hook.py", ""),
    ("PreToolUse", "Edit|Write|MultiEdit", "guard_hook.py", ""),
    ("PreToolUse", "mcp__.*noblivion_recall", "mcp_session_hook.py", ""),  # NOBLIVION-110
    ("PreToolUse", NUDGE_MCP_MATCHER, "recall_nudge.py", ""),  # create or comment on a ticket
    ("PreToolUse", "Agent|Task", "subagent_rules_hook.py", ""),
    ("PostToolUse", "Write|Edit|MultiEdit", "memory_fields_hook.py", ""),
    ("PostToolUse", "Write|Edit|MultiEdit", "memory_sync_hook.py", ""),
    ("PostToolUse", "Bash", "error_recall_hook.py", ""),
    ("PostToolUse", "Bash", "memory_sync_hook.py", ""),
    ("PostToolUseFailure", "Bash", "error_recall_hook.py", ""),
    ("PostToolUseFailure", "Bash", "guard_hook.py", ""),
    ("SubagentStart", "", "subagent_rules_hook.py", ""),
    ("Stop", "", "stop_checks.py", ""),
    ("Stop", "", "trust_flush.py", ""),  # the trust flush (E5)
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
                if handler["type"] == "mcp_tool":  # NOBLIVION-162 step 2; see the test of it below
                    continue
                m = HOOK_COMMAND.fullmatch(handler["command"])
                assert m, handler["command"]
                matcher = group.get("matcher", "")
                if "if" in handler:
                    matcher += " if " + handler["if"]
                out.append((event, matcher, m.group(1), m.group(2)))
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
                    # NOBLIVION-110: an MCP tool matcher is a pattern that starts with mcp__.
                    assert parts <= TOOL_NAMES or matcher.startswith("mcp__"), matcher
            assert isinstance(group["hooks"], list) and group["hooks"]
            for handler in group["hooks"]:
                if handler["type"] == "mcp_tool":
                    assert set(handler) == {"type", "server", "tool", "input", "timeout"}
                    assert event == "UserPromptSubmit"
                else:
                    assert set(handler) <= {"type", "command", "timeout", "async", "if"}
                    assert handler["type"] == "command"
                assert handler.get("async", True) is True
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
    # Claude Code sets CLAUDE_PLUGIN_DATA for the server by itself. An env
    # entry "${CLAUDE_PLUGIN_DATA}" reached the server as literal text
    # (NOBLIVION-24), so the server has no env block.
    assert "env" not in server
    source = (ROOT / "mcp" / "recall_mcp.py").read_text(encoding="utf-8")
    assert 'TOOL_NAME = "noblivion_recall"' in source


# The ${...} names Claude Code expands in a plugin's JSON files, per field.
def test_the_mcp_server_loads_its_tools_eagerly():
    """NOBLIVION-141: ``alwaysLoad`` (read by Claude Code 2.1.261 in the config of a stdio
    server) keeps the two tools out of the deferred list, so no ToolSearch call is needed."""
    (name,) = _load(MCP_JSON)["mcpServers"]
    assert _load(MCP_JSON)["mcpServers"][name].get("alwaysLoad") is True


# Measured on Claude Code 2.1.92 and 2.1.261 (NOBLIVION-24): in .mcp.json
# "command" and "args" expand ${CLAUDE_PLUGIN_ROOT}, but an "env" value
# "${CLAUDE_PLUGIN_DATA}" reached the server as literal text. The plugin
# reference names only the plugin variables, never a user variable. A hook
# command runs in a shell that has CLAUDE_PLUGIN_ROOT, CLAUDE_PLUGIN_DATA and
# CLAUDE_PROJECT_DIR in its environment, so those expand there.
VAR_REF = re.compile(r"\$\{([^}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
EXPANDED = {
    "mcp.command": {"CLAUDE_PLUGIN_ROOT"},
    "mcp.args": {"CLAUDE_PLUGIN_ROOT"},
    "hook.command": {
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_PROJECT_DIR",
        # The shell expands this one, in the flag test of the prompt command (the pattern reads the
        # nested reference up to its first closing brace).
        "NOBLIVION_DATA_DIR:-${CLAUDE_PLUGIN_DATA",
    },
    # The hook runner fills an mcp_tool input from the event JSON (NOBLIVION-162 step 2).
    "hook.input": set(dispatch.WARM_FIELDS),
}


def _var_refs(value, field, out):
    """Collect ``(field, variable)`` for each ``$NAME`` or ``${...}`` in
    every string under ``value``."""
    if isinstance(value, str):
        for match in VAR_REF.finditer(value):
            out.append((field, match.group(1) or match.group(2)))
    elif isinstance(value, dict):
        for key, item in value.items():
            _var_refs(item, field, out)
            _var_refs(key, field, out)
    elif isinstance(value, list):
        for item in value:
            _var_refs(item, field, out)


def _unexpanded_refs(mcp_json: Path = MCP_JSON):
    """Every variable reference in the plugin JSON files that Claude Code
    does not expand in that field."""
    found = []
    for name, server in _load(mcp_json)["mcpServers"].items():
        for key, value in server.items():
            refs = []
            _var_refs(value, "mcp." + key, refs)
            found += [
                (f".mcp.json {name}.{key}", v) for f, v in refs if v not in EXPANDED.get(f, ())
            ]
    for event, groups in _load(HOOKS_JSON)["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                for key, value in handler.items():
                    refs = []
                    _var_refs(value, "hook." + key, refs)
                    found += [
                        (f"hooks.json {event}.{key}", v)
                        for f, v in refs
                        if v not in EXPANDED.get(f, ())
                    ]
    for path in (PLUGIN, MARKETPLACE):
        refs = []
        _var_refs(_load(path), "manifest", refs)
        found += [(path.name, v) for _, v in refs]
    return found


def test_plugin_json_passes_no_unexpanded_variable():
    assert _unexpanded_refs() == []


def test_unexpanded_variable_check_catches_the_v010_env_block(tmp_path):
    """The check above fails on the shipped v0.1.0 .mcp.json (NOBLIVION-24)."""
    bad = tmp_path / ".mcp.json"
    doc = _load(MCP_JSON)
    (name,) = doc["mcpServers"]
    doc["mcpServers"][name]["env"] = {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert _unexpanded_refs(bad) == [(f".mcp.json {name}.env", "CLAUDE_PLUGIN_DATA")]


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


def test_shipped_config_turns_on_the_rules_only_prompt_path(tmp_path):
    """NOBLIVION-121: the shipped config sets recall.rules_only, prompt_rows and hub_days."""
    recall = _load(DEFAULT_CONFIG)["recall"]
    assert recall["rules_only"] == 1 and recall["prompt_rows"] == 5 and recall["hub_days"] == 14
    hook = _recall_hook()
    env = {"NOBLIVION_CONFIG": str(DEFAULT_CONFIG), "NOBLIVION_DATA_DIR": str(tmp_path)}
    assert hook.rules_only(env) is True and hook.prompt_rows(env) == 5
    # an env var that is set wins over the file
    assert hook.rules_only(dict(env, NOBLIVION_RECALL_RULES_ONLY="0")) is False
    assert hook.prompt_rows(dict(env, NOBLIVION_RECALL_PROMPT_ROWS="0")) == 0


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


def test_the_index_floor_default_reaches_the_shipped_config_and_no_config(tmp_path):
    # NOBLIVION-48. The floor is a default of the code, next to the model it
    # was measured for. The shipped file does not set it, so a config file of
    # an older release gets it too, and a new measurement reaches every user.
    hook = _recall_hook()
    assert "NOBLIVION_RECALL_INDEX_MIN_SCORE" not in _load(DEFAULT_CONFIG)["recall"]["env"]
    data = tmp_path / "data"
    data.mkdir()
    base = {"NOBLIVION_DATA_DIR": str(data), "HOME": str(tmp_path)}
    assert hook.index_min_score(hook.session_env(base, {})) == hook.DEFAULT_INDEX_MIN_SCORE
    shutil.copy(DEFAULT_CONFIG, data / "config.json")
    env = hook.session_env(base, {})
    assert env["NOBLIVION_RECALL_INDEX"] == "1"
    assert hook.index_min_score(env) == hook.DEFAULT_INDEX_MIN_SCORE
    # A config file or the environment can set another floor, or none.
    doc = _load(DEFAULT_CONFIG)
    doc["recall"]["env"]["NOBLIVION_RECALL_INDEX_MIN_SCORE"] = "off"
    (data / "config.json").write_text(json.dumps(doc))
    assert hook.index_min_score(hook.session_env(base, {})) is None
    env = hook.session_env({**base, "NOBLIVION_RECALL_INDEX_MIN_SCORE": "0.6"}, {})
    assert hook.index_min_score(env) == 0.6


def test_hook_commands_parse_as_shell_words():
    for event, groups in _load(HOOKS_JSON)["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if handler["type"] != "command":
                    continue
                words = shlex.split(handler["command"])
                if words[:1] == ["["]:  # the flag test of the warm prompt hook
                    words = words[words.index("||") + 1 :]
                assert words[0] == "python3", (event, words)


# ── hook process count (NOBLIVION-149) ──────────────────────────────────────


def _handlers(event, tool="", command=""):
    """The handlers Claude Code would start for one event: the matcher is a full-string
    regex on the tool name; an ``if`` is a ``Tool(pattern*)`` rule on the command."""
    out = []
    for group in _load(HOOKS_JSON)["hooks"].get(event, []):
        matcher = group.get("matcher")
        if matcher and not re.fullmatch(matcher, tool):
            continue
        for handler in group["hooks"]:
            rule = handler.get("if")
            if rule:
                name, _, pattern = rule.partition("(")
                if name != tool or not fnmatch.fnmatchcase(command, pattern[:-1]):
                    continue
            out.append(handler)
    return out


def _scripts(handlers):
    return [HOOK_COMMAND.fullmatch(h["command"]).group(1) for h in handlers if "command" in h]


def _sync(handlers):
    return [h for h in handlers if not h.get("async")]


def test_learning_capture_runs_once_for_each_session_not_for_each_event():
    where = {e for e, _, script, _ in _bindings() if script == "learning_capture.py"}
    # NOBLIVION-162: SessionStart and PreCompact reach it through the dispatcher.
    where |= {
        e
        for e, (steps, _) in dispatch.PLAN.items()
        for s in steps
        if s.module == "learning_capture"
    }
    assert where == {"SessionStart", "PreCompact", "SessionEnd"}


def test_version_check_runs_at_session_start_only_and_in_the_background():
    (binding,) = [b for b in _bindings() if b[2] == "version_check.py"]
    assert binding[0] == "SessionStart"
    group = _load(HOOKS_JSON)["hooks"]["SessionStart"]
    (handler,) = [h for g in group for h in g["hooks"] if "version_check.py" in h["command"]]
    assert handler["async"] is True


def test_the_silent_hooks_run_in_the_background():
    silent = {"trust_flush.py", "memory_sync_hook.py", "version_check.py"}
    for event, groups in _load(HOOKS_JSON)["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                if h["type"] == "mcp_tool":
                    assert "async" not in h  # it answers the prompt, so Claude Code waits for it
                    continue
                script = _scripts([h])[0]
                assert h.get("async", False) is (script in silent), (event, script)


def test_the_nudge_starts_only_for_a_merge_or_a_ticket_tool():
    for tool, command in (
        ("Bash", "ls -la"),
        ("Bash", "git status"),
        ("Read", ""),
        ("Edit", ""),
        ("mcp__srv__search_issues", ""),
        ("mcp__srv__get_issue", ""),
        ("mcp__plugin_noblivion_noblivion__noblivion_recall", ""),
    ):
        assert "recall_nudge.py" not in _scripts(_handlers("PreToolUse", tool, command)), tool
    for tool, command in (
        ("Bash", "gh pr merge 12 --squash"),
        ("Bash", "glab mr merge 3"),
        ("mcp__srv__create_issue", ""),
        ("mcp__srv__createJiraIssue", ""),
        ("mcp__srv__addCommentToJiraIssue", ""),
        ("mcp__srv__add_issue_comment", ""),
    ):
        assert "recall_nudge.py" in _scripts(_handlers("PreToolUse", tool, command)), tool


def test_hook_processes_waited_for_on_the_common_events():
    """Count of foreground command starts. The review R2-F05/F13 counted 5 on a Bash call and 5
    on a prompt before NOBLIVION-149; NOBLIVION-162 runs the prompt's three modules in one."""
    # One command, and the mcp_tool hook that answers nothing unless the flag file is there.
    prompt = _handlers("UserPromptSubmit")
    assert [h["type"] for h in prompt] == ["command", "mcp_tool"]
    assert len(_sync(prompt[:1])) == 1
    assert len(_sync(_handlers("PreToolUse", "Bash", "ls -la"))) == 1
    assert len(_sync(_handlers("PostToolUse", "Bash", "ls -la"))) == 1
    assert len(_sync(_handlers("PostToolUse", "Read"))) == 0
    assert len(_sync(_handlers("PostToolUse", "Write"))) == 1
    assert len(_sync(_handlers("PostToolUseFailure", "Bash", "false"))) == 2
    assert len(_sync(_handlers("Stop"))) == 1
    # Every tool call: starts of any kind, background ones included.
    assert len(_handlers("PostToolUse", "Bash", "ls -la")) == 2
    assert len(_handlers("PostToolUse", "Grep")) == 0
