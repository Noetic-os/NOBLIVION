# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion migrate-from-legacy``: remove the hand-installed reference hooks.

Design doc 0001, section 14. Some users run the reference hooks by hand:
copies in ``~/.claude/hooks/claude_code_<name>.py``, hand-written entries in
``~/.claude/settings.json`` that run them, and an MCP server entry in
``~/.mcp.json``. With the plugin installed as well, every hook would run
twice.

The command:

1. Lists the old hook files (``LEGACY_STEMS``; another ``claude_code_*.py``
   is reported as unknown and left alone), links in the hooks folder that
   point to one of them, the settings entries whose command runs one of
   them, the MCP server entries that run one of them, and the old env vars
   that are set for them (with their new names, ``legacy_env.json``).
2. Without ``--apply`` it only prints this plan (a dry run).
3. With ``--apply`` it copies ``settings.json`` and ``.mcp.json`` to
   ``<file>.noblivion-backup-<utc>``, removes only the listed entries (every
   other entry keeps its value and its place), and moves the old files to
   ``<data dir>/migrated/<utc>/``. It deletes no file.
4. ``--undo`` restores the files of the last ``--apply`` from its
   ``manifest.json``.

It never changes a hook entry whose command does not run an old hook file.

The settings and hooks are those of the Claude Code config folder:
``--config-dir``, else ``CLAUDE_CONFIG_DIR``, else ``<home>/.claude``. With
another folder than ``<home>/.claude`` (a second profile), ``<home>/.mcp.json``
is not read and not changed, because the default profile reads it too.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noblivion import config

LEGACY_PREFIX = "claude_code_"
# The file stems of the reference hooks and their helper modules. Only these
# names count as old files; another ``claude_code_*.py`` is not ours.
LEGACY_STEMS = frozenset(
    {
        "continuity_hook",
        "credential_guard",
        "error_recall_hook",
        "guard_hook",
        "guard_table",
        "label_rows",
        "memory_fields",
        "memory_fields_hook",
        "memory_labels",
        "memory_sync_hook",
        "memory_text",
        "recall_hook",
        "recall_mcp",
        "stop_checks",
        "subagent_rules_hook",
        "trust_events",
        "trust_flush",
        "trust_rank",
        "trust_report",
        "trust_session_line",
    }
)
LEGACY_NAMES = frozenset(f"{LEGACY_PREFIX}{stem}.py" for stem in LEGACY_STEMS)
BACKUP_SUFFIX = ".noblivion-backup-"
MIGRATED_DIR = "migrated"
MANIFEST = "manifest.json"
ENV_TABLE_FILE = Path(__file__).resolve().parent / "legacy_env.json"

_PY_TOKEN = re.compile(r"""[^\s'"`;|&()<>=]+\.py(?=$|[\s'"`;|&()<>])""")
_ENV_ASSIGN = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*)_([A-Z0-9_]+)=")

USAGE_EPILOG = "Without --apply nothing changes: the command prints the plan."


@dataclass
class Plan:
    claude_dir: Path
    mcp_file: Path | None  # None: another profile, ``<home>/.mcp.json`` is left alone
    files: list[Path] = field(default_factory=list)  # old hook files and links to them
    unknown: list[Path] = field(default_factory=list)  # claude_code_*.py we do not know
    settings_entries: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    env_vars: dict[str, str | None] = field(default_factory=dict)  # old name -> new or None

    @property
    def settings_file(self) -> Path:
        return self.claude_dir / "settings.json"

    @property
    def empty(self) -> bool:
        return not (self.files or self.settings_entries or self.mcp_servers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claude_dir": str(self.claude_dir),
            "mcp_file": str(self.mcp_file) if self.mcp_file else None,
            "files": [str(p) for p in self.files],
            "unknown_files": [str(p) for p in self.unknown],
            "settings_entries": self.settings_entries,
            "mcp_servers": self.mcp_servers,
            "env_vars": self.env_vars,
        }


# ── detection ───────────────────────────────────────────────────────────────


def _expand(token: str, home: Path) -> Path:
    for var in ("${HOME}", "$HOME"):
        if token.startswith(var):
            token = str(home) + token[len(var) :]
    if token.startswith("~/"):
        token = str(home) + token[1:]
    return Path(token)


def is_legacy_path(token: str, home: Path) -> bool:
    """A path in a command that names an old hook file, or a link to one."""
    path = _expand(token, home)
    if path.name in LEGACY_NAMES:
        return True
    try:
        return path.is_symlink() and Path(os.path.realpath(path)).name in LEGACY_NAMES
    except OSError:
        return False


def runs_legacy(command: Any, home: Path) -> bool:
    """True when a hook or MCP command string runs an old hook file."""
    if not isinstance(command, str):
        return False
    return any(is_legacy_path(t, home) for t in _PY_TOKEN.findall(command))


def _hook_files(hooks_dir: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    unknown: list[Path] = []
    if not hooks_dir.is_dir():
        return files, unknown
    for path in sorted(hooks_dir.iterdir()):
        if path.name in LEGACY_NAMES:
            files.append(path)
        elif path.name.startswith(LEGACY_PREFIX) and path.suffix == ".py":
            unknown.append(path)
        elif path.is_symlink() and Path(os.path.realpath(path)).name in LEGACY_NAMES:
            files.append(path)  # an alias link to an old file
    return files, unknown


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SystemExit(f"noblivion: cannot read {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise SystemExit(f"noblivion: {path} is not a JSON object")
    return doc


def _legacy_handlers(settings: Mapping[str, Any], home: Path) -> list[dict[str, Any]]:
    found = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for handler in group["hooks"]:
                if isinstance(handler, dict) and runs_legacy(handler.get("command"), home):
                    found.append(
                        {
                            "event": event,
                            "matcher": group.get("matcher", ""),
                            "command": handler.get("command"),
                        }
                    )
    return found


def _mcp_command(server: Any) -> str:
    if not isinstance(server, dict):
        return ""
    parts = [server.get("command")]
    args = server.get("args")
    if isinstance(args, list):
        parts.extend(args)
    return " ".join(p for p in parts if isinstance(p, str))


def _legacy_servers(mcp: Mapping[str, Any], home: Path) -> list[str]:
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    return [name for name, s in servers.items() if runs_legacy(_mcp_command(s), home)]


def env_table() -> tuple[str, frozenset[str], frozenset[str]]:
    doc = json.loads(ENV_TABLE_FILE.read_text(encoding="utf-8"))
    return doc["new_prefix"], frozenset(doc["renamed"]), frozenset(doc["removed"])


def rename_env(name: str) -> tuple[bool, str | None]:
    """``(known, new name)``. ``known`` is False for a var that is not an old
    reference var; the new name is None for a removed var."""
    new_prefix, renamed, removed = env_table()
    head, _, suffix = name.partition("_")
    if not suffix or f"{head}_" == new_prefix:
        return False, None
    if suffix in renamed:
        return True, new_prefix + suffix
    if suffix in removed:
        return True, None
    return False, None


def _env_vars(texts: Sequence[str], names: Sequence[str]) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    candidates = list(names)
    for text in texts:
        candidates.extend(f"{a}_{b}" for a, b in _ENV_ASSIGN.findall(text))
    for name in candidates:
        known, new = rename_env(name)
        if known:
            found[name] = new
    return dict(sorted(found.items()))


def resolve_claude_dir(home: Path, config_dir: Path | None = None) -> Path:
    """``config_dir``, else ``CLAUDE_CONFIG_DIR``, else ``<home>/.claude``."""
    if config_dir is not None:
        return config_dir
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return config.claude_config_dir()
    return home / ".claude"


def build_plan(home: Path, claude_dir: Path | None = None) -> Plan:
    claude_dir = home / ".claude" if claude_dir is None else claude_dir
    default_profile = claude_dir == home / ".claude"
    plan = Plan(claude_dir=claude_dir, mcp_file=home / ".mcp.json" if default_profile else None)
    plan.files, plan.unknown = _hook_files(claude_dir / "hooks")
    settings = _read_json(plan.settings_file) or {}
    plan.settings_entries = _legacy_handlers(settings, home)
    mcp = (_read_json(plan.mcp_file) if plan.mcp_file else None) or {}
    plan.mcp_servers = _legacy_servers(mcp, home)
    texts = [str(e["command"]) for e in plan.settings_entries]
    names: list[str] = []
    env_block = settings.get("env")
    if isinstance(env_block, dict):
        names.extend(k for k in env_block if isinstance(k, str))
    servers = mcp.get("mcpServers") if isinstance(mcp.get("mcpServers"), dict) else {}
    for name in plan.mcp_servers:
        env = servers[name].get("env") if isinstance(servers[name], dict) else None
        if isinstance(env, dict):
            names.extend(k for k in env if isinstance(k, str))
    plan.env_vars = _env_vars(texts, names)
    return plan


# ── apply and undo ──────────────────────────────────────────────────────────


def strip_settings(settings: dict[str, Any], home: Path) -> dict[str, Any]:
    """``settings`` without the handlers that run an old hook file. A matcher
    group left with no handler is removed, and so is an event left with no
    group. Every other key and entry keeps its value and its order."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    new_hooks: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [
                h
                for h in group["hooks"]
                if not (isinstance(h, dict) and runs_legacy(h.get("command"), home))
            ]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups or not groups:
            new_hooks[event] = kept_groups
    return {**settings, "hooks": new_hooks}


def strip_mcp(mcp: dict[str, Any], names: Sequence[str]) -> dict[str, Any]:
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict):
        return mcp
    return {**mcp, "mcpServers": {k: v for k, v in servers.items() if k not in names}}


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    """Atomic write that keeps the file mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def apply(plan: Plan, home: Path, data_dir: Path) -> dict[str, Any]:
    """Back up, remove the old entries and move the old files. Returns the
    manifest that ``undo`` reads."""
    stamp = _stamp()
    target = data_dir / MIGRATED_DIR / stamp
    target.mkdir(parents=True, mode=0o700)
    manifest: dict[str, Any] = {"stamp": stamp, "backups": [], "moved": []}
    if plan.settings_entries:
        backup = plan.settings_file.with_name(plan.settings_file.name + BACKUP_SUFFIX + stamp)
        shutil.copy2(plan.settings_file, backup)
        manifest["backups"].append({"file": str(plan.settings_file), "backup": str(backup)})
        settings = _read_json(plan.settings_file) or {}
        _write_json(plan.settings_file, strip_settings(settings, home))
    if plan.mcp_servers and plan.mcp_file is not None:
        backup = plan.mcp_file.with_name(plan.mcp_file.name + BACKUP_SUFFIX + stamp)
        shutil.copy2(plan.mcp_file, backup)
        manifest["backups"].append({"file": str(plan.mcp_file), "backup": str(backup)})
        mcp = _read_json(plan.mcp_file) or {}
        _write_json(plan.mcp_file, strip_mcp(mcp, plan.mcp_servers))
    for path in plan.files:
        dest = target / path.name
        shutil.move(str(path), dest)  # a link moves as a link
        manifest["moved"].append({"from": str(path), "to": str(dest)})
    (target / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def undo(data_dir: Path) -> dict[str, Any] | None:
    """Restore the last ``apply``. Returns its manifest, or None."""
    root = data_dir / MIGRATED_DIR
    runs = sorted(p for p in root.glob(f"*/{MANIFEST}")) if root.is_dir() else []
    if not runs:
        return None
    manifest_path = runs[-1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("backups", []):
        shutil.copy2(item["backup"], item["file"])
    for item in manifest.get("moved", []):
        src, dest = Path(item["to"]), Path(item["from"])
        if (src.exists() or src.is_symlink()) and not (dest.exists() or dest.is_symlink()):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
    manifest_path.rename(manifest_path.with_name(MANIFEST + ".undone"))
    return manifest


# ── output ──────────────────────────────────────────────────────────────────


def render(plan: Plan) -> str:
    if plan.empty and not plan.unknown:
        return "No hand-installed reference hooks found. Nothing to do."
    lines = []
    if plan.files:
        lines.append(f"Old hook files ({len(plan.files)}), moved by --apply:")
        lines.extend(f"  {p}" for p in plan.files)
    if plan.settings_entries:
        lines.append(f"Entries in {plan.settings_file} ({len(plan.settings_entries)}):")
        for e in plan.settings_entries:
            matcher = f" [{e['matcher']}]" if e["matcher"] else ""
            lines.append(f"  {e['event']}{matcher}: {str(e['command'])[:120]}")
    if plan.mcp_servers:
        lines.append(f"MCP servers in {plan.mcp_file}: {', '.join(plan.mcp_servers)}")
    if plan.unknown:
        lines.append("Not ours, left alone:")
        lines.extend(f"  {p}" for p in plan.unknown)
    if plan.env_vars:
        lines.append("Old env vars. Set the new name, or drop the var:")
        lines.extend(f"  {k} -> {v or '(removed)'}" for k, v in plan.env_vars.items())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="noblivion migrate-from-legacy",
        description="Remove the hand-installed reference hooks (a dry run by default).",
        epilog=USAGE_EPILOG,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="back up, then remove the entries")
    mode.add_argument("--undo", action="store_true", help="restore the last --apply")
    parser.add_argument("--home", type=Path, default=None, help="home folder (default: yours)")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Claude Code config folder (default: CLAUDE_CONFIG_DIR, else <home>/.claude)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    home = (args.home or Path.home()).expanduser().resolve()
    data_dir = config.data_dir()
    if args.undo:
        manifest = undo(data_dir)
        if args.json:
            print(json.dumps({"undone": manifest}))
        else:
            print("Nothing to undo." if manifest is None else f"Restored run {manifest['stamp']}.")
        return 0
    config_dir = args.config_dir.expanduser().resolve() if args.config_dir else None
    plan = build_plan(home, resolve_claude_dir(home, config_dir))
    manifest = apply(plan, home, data_dir) if args.apply and not plan.empty else None
    if args.json:
        print(json.dumps({"plan": plan.as_dict(), "applied": manifest}))
        return 0
    print(render(plan))
    if manifest is not None:
        print(f"Applied. Backups and moved files: {data_dir / MIGRATED_DIR / manifest['stamp']}")
        print("Undo with: noblivion migrate-from-legacy --undo")
    elif not plan.empty:
        print("Dry run: nothing changed. Run again with --apply to do it.")
    return 0
