#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install the NOBLIVION Codex adapter without changing Claude Code settings."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BEGIN = "# BEGIN NOBLIVION CODEX MCP (managed by install_codex.py)"
END = "# END NOBLIVION CODEX MCP"
EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "SubagentStart",
    "Stop",
    "SessionEnd",
)
TOOL_MATCHER = (
    r"Bash|exec_command|shell_command|apply_patch|Read|Grep|Glob|Edit|Write|MultiEdit|"
    r"spawn_agent|Agent|Task|mcp__.*read.*|mcp__noblivion__noblivion_recall"
)
POST_MATCHER = r".*"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _backup(home: Path, target: Path) -> None:
    if not target.exists():
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = home / "noblivion" / "backups" / stamp
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = folder / target.name
    if destination.exists():
        destination = folder / (target.parent.name + "-" + target.name)
    shutil.copy2(target, destination)
    destination.chmod(0o600)


def _hook_entries(command: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for event in EVENTS:
        hook: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": 3 if event == "SessionEnd" else 45,
            "statusMessage": "NOBLIVION shared memory",
        }
        if event not in ("PreCompact", "Stop", "SessionEnd"):
            hook["additionalContextLimit"] = 2500
        group: dict[str, Any] = {"hooks": [hook]}
        if event == "PreToolUse":
            group["matcher"] = TOOL_MATCHER
        elif event == "PostToolUse":
            group["matcher"] = POST_MATCHER
        result[event] = [group]
    return result


def _is_ours(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and "/codex/adapter.py" in str(h.get("command", "")) for h in hooks
    )


def _replace_adapter_command(previous: str, command: str) -> str:
    """Keep the existing launcher and environment prefix verbatim.

    Only the adapter path and its --config argument belong to this installer.
    Refuse compound commands instead of silently dropping another guard.
    """
    lexer = shlex.shlex(previous, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    parts = []
    while True:
        start = lexer.instream.tell()
        token = lexer.get_token()
        if token == lexer.eof:
            break
        parts.append((token, start))
    if any(token and all(char in ";&|<>" for char in token) for token, _ in parts):
        raise ValueError("review compound NOBLIVION hook command before upgrading")
    matches = [
        index for index, (token, _) in enumerate(parts) if token.endswith("/codex/adapter.py")
    ]
    if len(matches) != 1 or matches[0] == 0:
        raise ValueError("cannot preserve NOBLIVION hook launcher")
    index = matches[0]
    tail = [token for token, _ in parts[index + 1 :]]
    if len(tail) != 2 or tail[0] != "--config":
        raise ValueError("review custom NOBLIVION adapter arguments before upgrading")
    replacement = shlex.split(command)
    return previous[: parts[index][1]] + shlex.join(replacement[1:])


def _merge_hooks(old: str, command: str) -> str:
    doc = json.loads(old) if old.strip() else {}
    if not isinstance(doc, dict):
        raise ValueError("hooks.json must contain a JSON object")
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json hooks must be a JSON object")
    for event, entries in _hook_entries(command).items():
        current = hooks.get(event, [])
        if not isinstance(current, list):
            raise ValueError("hooks.json event must contain a list: " + event)
        preserved = []
        launchers = []
        for group in current:
            if not _is_ours(group):
                preserved.append(group)
                continue
            foreign = []
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or "/codex/adapter.py" not in str(
                    hook.get("command", "")
                ):
                    foreign.append(hook)
                    continue
                upgraded = _replace_adapter_command(hook["command"], command)
                if upgraded not in launchers:
                    launchers.append(upgraded)
            if foreign:
                preserved.append({**group, "hooks": foreign})
        for launcher in launchers or [command]:
            entry = json.loads(json.dumps(entries[0]))
            entry["hooks"][0]["command"] = launcher
            preserved.append(entry)
        hooks[event] = preserved
    return json.dumps(doc, indent=2) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _mcp_block(repo: Path, data: Path, memory: Path, namespace: str, root: str) -> str:
    args = [str(repo / "mcp/recall_mcp.py")]
    env = {
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_MEMORY_DIR": str(memory),
        "NOBLIVION_RECALL_MEMORY_DIR": str(memory),
        "NOBLIVION_RECALL_PROJECT": namespace,
        "NOBLIVION_RECALL_ROOT": root,
        "NOBLIVION_PROJECT": namespace,
        "NOBLIVION_SOURCE_CLIENT": "codex",
        "NOBLIVION_MCP_REQUIRE_CAPABILITY": "1",
    }
    # MCP's cwd is the active project. The hook adapter uses the same folder
    # and namespace through its private config file.
    lines = [
        BEGIN,
        "[mcp_servers.noblivion]",
        'command = "python3"',
        "args = [" + ", ".join(_toml_string(x) for x in args) + "]",
        "env = { " + ", ".join(k + " = " + _toml_string(v) for k, v in env.items()) + " }",
        END,
    ]
    return "\n".join(lines)


def _toml_table_lines(text: str) -> tuple[dict[int, tuple[str, ...]], dict[int, str]]:
    """Find table headers without mistaking multiline values for sections."""
    key = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
    headers: dict[int, tuple[str, ...]] = {}
    markers: dict[int, str] = {}
    quote, depth = "", 0
    for number, line in enumerate(text.splitlines(keepends=True)):
        if not quote and depth == 0:
            if line.strip() in (BEGIN, END):
                markers[number] = line.strip()
            header = re.fullmatch(r"\s*(\[\[?)(.*?)(\]\]?)\s*(?:#.*)?", line)
            if header and len(header[1]) == len(header[3]):
                body = header[2].strip()
                if re.fullmatch(key + r"(?:\s*\.\s*" + key + r")*", body):
                    parts = []
                    for part in re.findall(key, body):
                        if part.startswith('"'):
                            try:
                                part = json.loads(part)
                            except ValueError as exc:
                                raise ValueError(
                                    "review unsupported TOML table key encoding"
                                ) from exc
                        elif part.startswith("'"):
                            part = part[1:-1]
                        parts.append(part)
                    headers[number] = tuple(parts)
                    continue
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if quote.startswith('"') and char == "\\":
                    index += 2
                    continue
                if line.startswith(quote, index):
                    width = len(quote)
                    if width == 3:
                        # TOML permits one or two literal quotes immediately
                        # before the closing triple-quote delimiter.
                        while index + width < len(line) and line[index + width] == quote[0]:
                            width += 1
                    index += width
                    quote = ""
                    continue
            elif char == "#":
                break
            elif char in "\"'":
                quote = char * 3 if line.startswith(char * 3, index) else char
                index += len(quote)
                continue
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            index += 1
    return headers, markers


def _merge_toml(old: str, block: str) -> str:
    lines = old.splitlines(keepends=True)
    headers, markers = _toml_table_lines(old)
    starts = [index for index, marker in markers.items() if marker == BEGIN]
    ends = [index for index, marker in markers.items() if marker == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("config.toml has an incomplete or repeated NOBLIVION block")
    owned = {index for index, path in headers.items() if path[:2] == ("mcp_servers", "noblivion")}
    if not starts and owned:
        raise ValueError("config.toml already has an unmanaged mcp_servers.noblivion section")
    preserved = []
    dropping = False
    for index, line in enumerate(lines):
        if index in starts or index in ends:
            continue
        if index in headers:
            dropping = index in owned
        if not dropping or not line.strip() or line.lstrip().startswith("#"):
            preserved.append(line)
    # A marker is not ownership: Codex may insert hook trust state or other
    # sections inside it. Their original lines remain outside the fresh block.
    return "".join(preserved).rstrip("\r\n") + "\n\n" + block + "\n"


def _git_common(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if proc.returncode or not proc.stdout.strip():
            return ""
        return str((path / proc.stdout.strip()).resolve())
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _store_config(data: Path) -> dict[str, Any]:
    path = data / "config.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("data dir needs a readable config.json") from exc
    if not isinstance(doc, dict):
        raise ValueError("data dir config.json must contain an object")
    return doc


def _store_default_memory_dirs() -> list[Path]:
    """The store's default memory folders (``noblivion.config``), which
    follow Claude Code's config dir and ``autoMemoryDirectory`` (NOBLIVION-30).
    Loaded by path: this script runs on the system python, outside the venv."""
    path = Path(__file__).resolve().parent.parent / "src/noblivion/config.py"
    spec = importlib.util.spec_from_file_location("noblivion_store_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module.default_memory_dirs()


def _memory_indexed(memory: Path, store_cfg: dict[str, Any]) -> bool:
    configured = store_cfg.get("memory_dirs")
    if configured is None:
        candidates = iter(_store_default_memory_dirs())
    elif isinstance(configured, list) and all(isinstance(x, str) for x in configured):
        candidates = (Path(x).expanduser() for x in configured)
    else:
        return False
    return any(path.resolve() == memory for path in candidates)


def install(args: argparse.Namespace) -> dict[str, str]:
    repo = Path(__file__).resolve().parent.parent
    data = Path(args.data_dir).expanduser().resolve()
    memory = Path(args.memory_dir).expanduser().resolve()
    workspaces = [Path(raw).expanduser().resolve() for raw in args.workspace]
    home = Path(args.codex_home).expanduser().resolve()
    if not data.is_dir() or not (data / "venv/bin/noblivion").is_file():
        raise ValueError(
            "data dir needs an installed NOBLIVION store; run scripts/install.sh first"
        )
    if not memory.is_dir():
        raise ValueError("memory dir must be an existing folder")
    store_cfg = _store_config(data)
    if not _memory_indexed(memory, store_cfg):
        raise ValueError(
            "memory dir is not indexed; add it to data dir config.json memory_dirs first"
        )
    if not workspaces or any(not path.is_dir() for path in workspaces):
        raise ValueError("each --workspace must be an existing folder")
    namespace = args.namespace or store_cfg.get("namespace") or "claude_code"
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", namespace):
        raise ValueError("namespace must use lower-case letters, digits, and _")
    recall_root = getattr(args, "root", None) or memory.parent.name
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", recall_root):
        raise ValueError("root must be a single memory parent folder name")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    own = home / "noblivion"
    own.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg_path = own / "config.json"
    hook_path = home / "hooks.json"
    toml_path = home / "config.toml"
    previous = {}
    if cfg_path.is_file():
        previous = json.loads(cfg_path.read_text(encoding="utf-8"))
    learning = {"use": True, "contribute": False, **previous.get("learning", {})}
    if getattr(args, "contribute", None) is not None:
        learning["contribute"] = args.contribute
    cfg = {
        "repo_root": str(repo),
        "data_dir": str(data),
        "memory_dir": str(memory),
        "state_dir": str(own / "state"),
        "namespace": namespace,
        "recall_root": recall_root,
        "learning": learning,
        "project_mappings": [
            {
                "workspace": str(path),
                "namespace": namespace,
                "root": recall_root,
                "memory_dir": str(memory),
            }
            for path in workspaces
        ],
        "workspace_prefixes": list(dict.fromkeys(str(p) for p in workspaces)),
        "workspace_git_dirs": list(
            dict.fromkeys(common for path in workspaces if (common := _git_common(path)))
        ),
    }
    command = shlex.join(["python3", str(repo / "codex/adapter.py"), "--config", str(cfg_path)])
    old_hooks = hook_path.read_text(encoding="utf-8") if hook_path.exists() else ""
    old_toml = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    new_hooks = _merge_hooks(old_hooks, command)
    new_toml = _merge_toml(old_toml, _mcp_block(repo, data, memory, namespace, recall_root))
    # Validate before changing either file. A failed merge leaves Codex intact.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: generated TOML uses fixed syntax
        pass
    else:
        tomllib.loads(new_toml)
    for target in (cfg_path, hook_path, toml_path):
        _backup(home, target)
    _write_private(cfg_path, json.dumps(cfg, indent=2) + "\n")
    _write_private(hook_path, new_hooks)
    _write_private(toml_path, new_toml)
    return {"config": str(cfg_path), "hooks": str(hook_path), "mcp": str(toml_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--memory-dir", required=True)
    parser.add_argument("--workspace", action="append", required=True)
    parser.add_argument("--namespace", default=None)
    parser.add_argument(
        "--contribute",
        action="store_true",
        default=None,
        help="capture new sessions for background learning; off by default",
    )
    parser.add_argument("--no-contribute", dest="contribute", action="store_false")
    parser.add_argument("--root", default=None)
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    args = parser.parse_args()
    try:
        result = install(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                **result,
                "hook_trust": "review and trust NOBLIVION hooks with /hooks in Codex CLI",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
