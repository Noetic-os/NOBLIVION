#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostToolUse hook (Write|Edit|MultiEdit): warn when a feedback memory lacks
its rule fields, or a project memory lacks ``status:`` and ``project:``.
Work item WI-1.

Stdin is the Claude Code hook JSON (``tool_name``, ``tool_input.file_path``,
``cwd``, ``session_id``). When the written path is a ``feedback_*.md`` or a
``project_*.md`` directly inside a memory folder of the session (the project
folder of the event's ``cwd``, ``hook_config.resolve_memory_dir``, with
``NOBLIVION_MEMORY_DIR`` as the override; and the global folder
``NOBLIVION_GLOBAL_MEMORY_DIR`` when it is set), the file is checked with
``memory_fields.check_fields`` (kind ``feedback`` or ``project``);
on a problem the hook prints one JSON object whose ``additionalContext`` lists
the problems in plain words and says what to write. A field that sits under
``metadata:`` and not at the top level is a problem too (the Edit tool moves
fields there): ``read_fields`` reads it, but the recall hook and the trigger
hook read the top level only, so the hook says which lines to move.

A write of ANY memory file directly inside a memory folder (not an index or
topic file) rebuilds the guard table first: ``guard_table.build`` reads
``violates`` from every memory file, whatever its name prefix (NOBLIVION-50).

Similar notes (NOBLIVION-156). After a write of a memory file that the store has not indexed
yet, the hook asks the store for the notes nearest to the new text. A note with a cosine of
0.70 or more that shares a trigger or a subject label with the new note (at most 3, nearest
first) is named in one line: "add supersedes or merge". The call goes to the store on the
loopback address only, starts nothing, and stops after 1.5 s. Turn it off with
``NOBLIVION_MEMORY_SIMILAR_HINT=0`` (config key ``memory.similar_hint``).

It NEVER blocks a memory write: exit 0 always, silent on any error. It makes no network
call except the store call above, and uses the standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

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
TOOLS = ("Write", "Edit", "MultiEdit")
KINDS = ("feedback", "project")  # the file name prefixes this hook checks
INDEX_FILES = ("MEMORY.md", "MEMORY_ARCHIVE.md")  # with topic_*: not in the guard table


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


def _cwd(event: dict) -> Optional[str]:
    cwd = event.get("cwd")
    return cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None


def memory_dir(cwd: Optional[str] = None) -> Optional[Path]:
    """The project memory folder of a session at ``cwd``, or None."""
    return _CFG.resolve_memory_dir(cwd)


def memory_dirs(cwd: Optional[str] = None) -> List[Path]:
    """The project folder, then the global folder when it is set."""
    return _CFG.memory_dirs(cwd)


def memory_path(event: dict) -> Optional[Path]:
    """The memory file this event wrote, of any kind, or None: a ``*.md``
    directly inside a memory folder that is not an index or topic file."""
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
        folders = [f.resolve() for f in memory_dirs(_cwd(event))]
    except OSError:
        return None
    if p.parent not in folders or p.suffix != ".md":
        return None
    if p.name in INDEX_FILES or p.name.startswith("topic_"):
        return None
    return p


def target_path(event: dict) -> Optional[Path]:
    """The feedback or project memory this event wrote, or None."""
    p = memory_path(event)
    return p if p is not None and kind_of(p) is not None else None


def kind_of(path: Path) -> Optional[str]:
    """``feedback`` or ``project`` by the file name prefix, else None."""
    for kind in KINDS:
        if path.name.startswith(kind + "_"):
            return kind
    return None


def message(mf, name: str, kind: str, problems: list, nested: Optional[list] = None) -> str:
    """The warning text. It names the exact lines to add for ``status`` and
    ``project``, the exact lines to move out of ``metadata:`` (``nested``), and
    carries the short rule schema (``SCHEMA_LINE_SHORT``) when a rule field is wrong."""
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
        msg += " " + getattr(mf, "SCHEMA_LINE_SHORT", mf.SCHEMA_LINE)
    return msg


# ── NOBLIVION-156: similar notes ───────────────────────────────────────────
SIMILAR_ENV = "NOBLIVION_MEMORY_SIMILAR_HINT"
SIMILAR_KEY = "memory.similar_hint"
SIMILAR_MIN_COSINE = 0.70
SIMILAR_MAX = 3
SIMILAR_ASK = 12  # rows asked for; the pool the 3 are taken from
SIMILAR_TIMEOUT_S = "1.5"


def similar_on(environ: Optional[dict] = None) -> bool:
    """On unless ``NOBLIVION_MEMORY_SIMILAR_HINT`` is off or ``memory.similar_hint`` is 0."""
    return bool(
        _CFG.switch(SIMILAR_ENV, True, environ if environ is not None else os.environ, SIMILAR_KEY)
    )


def _supersedes_of(text: str) -> set:
    """The stems a note names in ``supersedes:``, as the indexer reads them."""
    out: set = set()
    for line in text.split("\n")[1:40]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if stripped.startswith("supersedes:"):
            for item in (
                stripped[len("supersedes:") :].strip().strip("[]").replace(",", " ").split()
            ):
                item = item.strip("\"'")
                out.add(item[:-3] if item.endswith(".md") else item)
    return out


def store_rows(query: str, root: str) -> list:
    """The store's index rows for ``query`` (an ``IndexLine`` has ``source`` and ``score``).
    Loopback only; it starts no store. Raises on any failure."""
    recall = _load("recall_hook")
    if recall is None:
        return []
    env = dict(os.environ)
    env["NOBLIVION_STORE_AUTOSTART"] = "0"  # a note hint never starts a store
    env["NOBLIVION_RECALL_TIMEOUT_S"] = SIMILAR_TIMEOUT_S
    return list(recall.recall_index(query[:600], SIMILAR_ASK, environ=env, root=root))


def similar_notes(path: Path, text: str, cwd: Optional[str] = None) -> List[tuple]:
    """``[(stem, cosine)]``: up to 3 stored notes at 0.70 or more that share a trigger or a
    label with the note at ``path``, nearest first. ``[]`` when the note is in the store
    already, when the store is down, and on any error."""
    corpus = _load("corpus")
    labels = _load("memory_labels")
    if corpus is None or labels is None:
        return []
    meta, _ = corpus.parse_frontmatter(text)
    rule, _apply = corpus.read_rule_apply(meta)
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    query = " ".join(p.strip() for p in (rule, str(description or "")) if p and p.strip())
    if not query:
        return []
    rows = store_rows(query, path.parent.parent.name)
    if any(getattr(r, "source", "") == path.name for r in rows):
        return []  # a note that is indexed already: an edit, not a new note
    own_triggers = {t.strip().lower() for t in corpus.read_trigger_items(text, keep_kind=True)}
    own_labels = set(labels.memory_labels(text, path.stem))
    named = _supersedes_of(text)
    folders = memory_dirs(cwd)
    found: List[tuple] = []
    for row in sorted(rows, key=lambda r: -(r.score if r.score is not None else -1.0)):
        score = row.score
        source = getattr(row, "source", "")
        stem = source[:-3] if source.endswith(".md") else source
        if score is None or score < SIMILAR_MIN_COSINE or not stem or stem in named:
            continue
        if stem == path.stem or "/" in source:
            continue
        other = next((d / source for d in [path.parent, *folders] if (d / source).is_file()), None)
        if other is None:
            continue
        other_text = other.read_text(encoding="utf-8", errors="replace")
        theirs = {t.strip().lower() for t in corpus.read_trigger_items(other_text, keep_kind=True)}
        if own_triggers & theirs or own_labels & set(labels.memory_labels(other_text, stem)):
            found.append((stem, float(score)))
        if len(found) >= SIMILAR_MAX:
            break
    return found


def similar_hint(path: Path, text: str, cwd: Optional[str] = None) -> str:
    """The one-line hint for the note at ``path``, or ``""``."""
    try:
        if not similar_on():
            return ""
        found = similar_notes(path, text, cwd)
    except Exception:  # noqa: BLE001 - a hook fails open
        return ""
    if not found:
        return ""
    return "; ".join(
        f"similar note {stem} exists (cosine {score:.2f}): add supersedes or merge"
        for stem, score in found
    )


# ── WI-2 call site ─────────────────────────────────────────────────────────
# tools/guard_table.py (WI-2) rebuilds the local guard table after a
# memory write. A missing module or a failed rebuild is skipped silently.
def rebuild_guard_table(cwd: Optional[str] = None) -> None:
    try:
        mod = _load("guard_table")
        folders = memory_dirs(cwd)
        if mod is not None and hasattr(mod, "rebuild") and folders:
            mod.rebuild(folders)
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass


# ── end WI-2 call site ─────────────────────────────────────────────────────


def run(stdin_text: str) -> str:
    """The text to print for one event, ``""`` for none."""
    event = json.loads(stdin_text)
    if not isinstance(event, dict):
        return ""
    path = memory_path(event)
    if path is None or not path.is_file():
        return ""
    rebuild_guard_table(_cwd(event))  # every kind: the table reads them all
    kind = kind_of(path)
    if kind is None:
        return ""
    mf = _load("memory_fields")
    if mf is None:
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    fields = mf.read_fields(text)
    problems = mf.check_fields(fields, kind)
    nested = mf.nested_keys(text)
    warnings = getattr(mf, "rewrite_warnings", lambda f: [])(fields)  # NOBLIVION-122
    warnings += getattr(mf, "violates_warnings", lambda f: [])(fields)  # NOBLIVION-155
    hint = similar_hint(path, text, _cwd(event))
    if not problems and not nested:
        if not warnings and not hint:
            return ""
        parts = []
        if warnings:
            parts.append(f"Memory file {path.name} has a warning: " + "; ".join(warnings) + ".")
        if hint:
            parts.append(f"Memory file {path.name}: {hint}.")
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": " ".join(parts),
                }
            }
        )
    msg = message(mf, path.name, kind, problems, nested)
    if hint:
        msg += f" {hint}."
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
