#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart and UserPromptSubmit hook: tell the user when this session
runs an older NOBLIVION than the one installed.

Claude Code fixes ``${CLAUDE_PLUGIN_ROOT}`` to a version folder when the
session starts. A plugin update puts a new folder on disk, but this session
keeps running the old one until the user runs ``/reload-plugins`` or starts a
new session. Nothing tells the user. This hook does.

It reads two small files and makes no call:

- ``.claude-plugin/plugin.json`` of the folder this hook runs from: the version
  this session runs.
- ``installed_plugins.json`` in the plugins folder: the version installed now.

It works only from the plugin cache layout
``<plugins>/cache/<marketplace>/<plugin>/<version>/``. From a checkout, a
Codex release folder or any other layout it stays silent.

When the versions differ it prints one ``systemMessage`` line for the user,
once for each session and installed version. Nothing goes into the model
context. A missing or unreadable file gives no line. Exit 0 on every path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATE_NAME = "version-notice.json"  # in <data dir>/cache
STATE_KEEP = 20  # sessions remembered
MAX_FILE_BYTES = 1_000_000
ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _key(version: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in version.split("."):
        if not piece.isdigit():
            return ()
        parts.append(int(piece))
    return tuple(parts)


def running_version(root: Path = ROOT) -> Optional[Tuple[str, str, str, Path]]:
    """``(plugin, marketplace, version, plugins dir)`` of the cache folder this
    hook runs from, or None for any other layout."""
    doc = _read_json(root / ".claude-plugin" / "plugin.json")
    if not isinstance(doc, dict):
        return None
    name, version = doc.get("name"), doc.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    folders = root.parents
    if len(folders) < 5 or root.name != version or folders[0].name != name:
        return None
    if folders[2].name != "cache":
        return None
    return name, folders[1].name, version, folders[3]


def installed_versions(plugins_dir: Path, name: str, marketplace: str) -> List[str]:
    doc = _read_json(plugins_dir / "installed_plugins.json")
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    rows = plugins.get(f"{name}@{marketplace}") if isinstance(plugins, dict) else None
    if not isinstance(rows, list):
        return []
    return [r["version"] for r in rows if isinstance(r, dict) and isinstance(r.get("version"), str)]


def newer_installed(root: Path = ROOT) -> Optional[Tuple[str, str]]:
    """``(installed, running)`` when a newer version is installed and no
    install entry names the running version, else None."""
    here = running_version(root)
    if here is None:
        return None
    name, marketplace, running, plugins_dir = here
    installed = installed_versions(plugins_dir, name, marketplace)
    if not installed or running in installed:
        return None
    newest = max(installed, key=lambda v: (_key(v) or (-1,), v))
    if (_key(newest) or (-1,)) <= (_key(running) or (-1,)):
        return None
    return newest, running


def _cache_dir(environ: Mapping[str, str]) -> Path:
    path = Path(__file__).resolve().parent / "hook_config.py"
    spec = importlib.util.spec_from_file_location("hook_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.cache_dir(environ)


def first_notice(session: str, installed: str, state_file: Path) -> bool:
    """True once for each (session, installed version). Records the notice."""
    doc = _read_json(state_file)
    seen: Dict[str, str] = {}
    if isinstance(doc, dict):
        seen = {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)}
    if seen.get(session) == installed:
        return False
    seen.pop(session, None)
    seen[session] = installed
    seen = dict(list(seen.items())[-STATE_KEEP:])
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_file.parent), prefix=".version-notice-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
        os.replace(tmp, state_file)
    except OSError:
        pass
    return True


def message(installed: str, running: str) -> str:
    return (
        f"NOBLIVION {installed} is installed, but this session runs {running}. "
        "Run /reload-plugins to switch."
    )


def main(stdin=None, stdout=None, environ: Optional[Mapping[str, str]] = None) -> int:
    try:
        env = os.environ if environ is None else environ
        raw = (stdin or sys.stdin).read()
        try:
            event = json.loads(raw) if raw.strip() else {}
        except ValueError:
            event = {}
        found = newer_installed()
        if found is None:
            return 0
        installed, running = found
        session = str(event.get("session_id") or "") if isinstance(event, dict) else ""
        if not first_notice(session or "unknown", installed, _cache_dir(env) / STATE_NAME):
            return 0
        w = stdout or sys.stdout
        w.write(json.dumps({"systemMessage": message(installed, running)}) + "\n")
        w.flush()
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
