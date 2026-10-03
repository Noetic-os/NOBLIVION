#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostToolUse hook (Write|Edit|MultiEdit): warn when a feedback memory lacks
its rule fields, or a project memory lacks ``status:`` and ``project:``.
Work item WI-1.

Stdin is the Claude Code hook JSON (``tool_name``, ``tool_input.file_path``,
``cwd``, ``session_id``). When the written path is a ``feedback_*.md`` or a
``project_*.md`` directly inside the memory folder (env ``NOBLIVION_MEMORY_DIR``,
default the memory folder of the home-folder project), the file is checked with
``memory_fields.check_fields`` (kind ``feedback`` or ``project``);
on a problem the hook prints one JSON object whose ``additionalContext`` lists
the problems in plain words and says what to write. A field that sits under
``metadata:`` and not at the top level is a problem too (the Edit tool moves
fields there): ``read_fields`` reads it, but the recall hook and the trigger
hook read the top level only, so the hook says which lines to move.

It NEVER blocks a memory write: exit 0 always, silent on any error, no network
call, standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Optional

_TOOLS = Path(__file__).resolve().parent


def _hook_config():
    """``hook_config.py`` from this file's folder (data dir, config file)."""
    path = Path(__file__).resolve().parent / "hook_config.py"
    spec = importlib.util.spec_from_file_location("hook_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CFG = _hook_config()
LIVE = _CFG.default_memory_dir()
TOOLS = ("Write", "Edit", "MultiEdit")
KINDS = ("feedback", "project")  # the file name prefixes this hook checks


def _load(name: str):
    path = _TOOLS / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def memory_dir() -> Path:
    return Path(os.environ.get("NOBLIVION_MEMORY_DIR") or LIVE).expanduser()


def target_path(event: dict) -> Optional[Path]:
    """The feedback or project memory this event wrote, or None."""
    if event.get("tool_name") not in TOOLS:
        return None
    ti = event.get("tool_input") or {}
    raw = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(event.get("cwd") or os.getcwd()) / p
    try:
        p = p.resolve()
        folder = memory_dir().resolve()
    except OSError:
        return None
    if p.parent != folder or kind_of(p) is None or p.suffix != ".md":
        return None
    return p


def kind_of(path: Path) -> Optional[str]:
    """``feedback`` or ``project`` by the file name prefix, else None."""
    for kind in KINDS:
        if path.name.startswith(kind + "_"):
            return kind
    return None


def message(mf, name: str, kind: str, problems: list, nested: Optional[list] = None) -> str:
    """The warning text. It names the exact lines to add for ``status`` and
    ``project``, the exact lines to move out of ``metadata:`` (``nested``), and
    carries the rule schema when a rule field is wrong."""
    nested = list(nested or [])
    sp = set(mf.status_project_problems_of(problems))
    rule_problems = [p for p in problems if p not in sp]
    what = "the rule format" if kind == "feedback" else "the project memory format"
    listed = list(problems) + ([mf.nested_problem(nested)] if nested else [])
    msg = f"Memory file {name} does not meet {what}: " + "; ".join(listed) + "."
    fixes = ([mf.PROJECT_FIX_LINE] if sp else []) + ([mf.nested_fix_line(nested)] if nested else [])
    msg += " Fix the front matter"
    if fixes:
        msg += ": " + "; ".join(fixes)
    msg += "."
    if kind == "feedback" or rule_problems:
        msg += " " + mf.SCHEMA_LINE
    return msg


# ── WI-2 call site ─────────────────────────────────────────────────────────
# tools/guard_table.py (WI-2) rebuilds the local guard table after a
# memory write. A missing module or a failed rebuild is skipped silently.
def rebuild_guard_table() -> None:
    try:
        mod = _load("guard_table")
        if mod is not None and hasattr(mod, "rebuild"):
            mod.rebuild(memory_dir())
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass


# ── end WI-2 call site ─────────────────────────────────────────────────────


def run(stdin_text: str) -> str:
    """The text to print for one event, ``""`` for none."""
    event = json.loads(stdin_text)
    if not isinstance(event, dict):
        return ""
    path = target_path(event)
    if path is None or not path.is_file():
        return ""
    kind = kind_of(path) or "feedback"
    if kind == "feedback":  # as before: a project write does not rebuild the table
        rebuild_guard_table()
    mf = _load("memory_fields")
    if mf is None:
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    problems = mf.check_fields(mf.read_fields(text), kind)
    nested = mf.nested_keys(text)
    if not problems and not nested:
        return ""
    msg = message(mf, path.name, kind, problems, nested)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}
    )


def main() -> int:
    try:
        out = run(sys.stdin.read())
        if out:
            sys.stdout.write(out + "\n")
    except Exception:  # noqa: BLE001, S110 - never block, never raise
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
