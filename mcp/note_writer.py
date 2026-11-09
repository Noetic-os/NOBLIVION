# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate and atomically save source notes, independently of the MCP transport.

Standard library only. The normal indexer owns database updates; a successful
source-file save remains durable when its best-effort sync notification fails.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
FIELD_LIMITS = {"title": 160, "rule": 400, "apply": 1200, "body": 6000, "evidence": 1500}


class NoteError(Exception):
    """A note could not be saved; its message is safe to return to the caller."""


def _session_memory_dir(env: Mapping[str, str]) -> Optional[Path]:
    """The session's memory folder by the hooks' rule (``resolve_memory_dir``
    in ``hooks/hook_config.py``), for the project in ``CLAUDE_PROJECT_DIR``,
    else this process's cwd. Loaded by path, so sys.path is never touched."""
    spec = importlib.util.spec_from_file_location(
        "noblivion_mcp_hook_config", _HOOKS / "hook_config.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("hook_config")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_memory_dir(os.getcwd(), env)


def _redactor():
    """Load the store's stdlib redactor without importing its venv package."""
    path = Path(__file__).resolve().parent.parent / "src" / "noblivion" / "redaction.py"
    spec = importlib.util.spec_from_file_location("noblivion_mcp_redaction", path)
    if spec is None or spec.loader is None:
        raise ImportError("redaction")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _remember_supersedes(raw: Any, folder: Path, own_stem: str, redactor: Any) -> list[str]:
    """Validate exact local source identities before any write; never echo them on error."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("invalid supersedes list")
    stems = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("invalid supersedes item")
        stem = item[:-3] if item.endswith(".md") else item
        if (
            not 1 <= len(stem) <= 252
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*", stem) is None
            or stem == own_stem
            or stem in stems
        ):
            raise ValueError("invalid supersedes identity")
        clean = redactor.redact_at_rest(stem)
        if clean == redactor.REDACTION_FAILED_TOKEN or clean != stem:
            raise ValueError("unsafe supersedes identity")
        stems.append(stem)
    # Exact spelling matters even on a case-insensitive filesystem: the indexer
    # matches stored paths, not filesystem aliases. Do not read target contents.
    names = {entry.name for entry in folder.iterdir()}
    for stem in stems:
        target = folder / (stem + ".md")
        if (
            target.name not in names
            or target.is_symlink()
            or not target.is_file()
            or target.resolve().parent != folder
        ):
            raise ValueError("invalid supersedes target")
    # Keep the extension so the indexer's one-suffix removal also preserves
    # unusual but valid names such as old.md.md (whose stem is old.md).
    return [stem + ".md" for stem in sorted(stems)]


def save_note(args: Dict[str, Any], environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Save a redacted source note and return its path, status and indexing details.

    ``NoteError`` contains a safe failure message. Validate before creating a
    session folder or source file; notify the indexer only after a durable save.
    """
    env = os.environ if environ is None else environ
    source = env.get("NOBLIVION_SOURCE_CLIENT", "claude_code").strip()
    if source not in ("codex", "claude_code"):
        raise NoteError("NOBLIVION_SOURCE_CLIENT must be codex or claude_code")
    raw_dir = env.get("NOBLIVION_MEMORY_DIR", "").strip()
    session_dir = False
    if raw_dir:
        folder = Path(os.path.expanduser(raw_dir))
        if not folder.is_absolute() or not folder.is_dir():
            raise NoteError("NOBLIVION_MEMORY_DIR must be an existing absolute folder")
    elif source == "claude_code":
        # NOBLIVION-94: the plugin's .mcp.json sets no NOBLIVION_MEMORY_DIR,
        # so use the session's memory folder, the one the hooks and the store
        # use. Claude Code creates it on the first memory write; so do we,
        # after the note is valid.
        try:
            resolved = _session_memory_dir(env)
        except Exception as exc:  # noqa: BLE001 - fail closed before any file write
            raise NoteError(f"memory folder lookup failed ({type(exc).__name__})") from None
        if resolved is None or not resolved.is_absolute():
            raise NoteError("no memory folder: set NOBLIVION_MEMORY_DIR or CLAUDE_PROJECT_DIR")
        folder, session_dir = resolved, True
    else:
        raise NoteError("NOBLIVION_MEMORY_DIR must name the shared memory folder")
    if (
        not isinstance(args, dict)
        or not set(FIELD_LIMITS) <= set(args)
        or not set(args) <= set(FIELD_LIMITS) | {"supersedes"}
    ):
        raise NoteError(
            "title, rule, apply, body, and evidence are required; only supersedes is optional"
        )
    try:
        redactor = _redactor()
        values = {}
        for name, limit in FIELD_LIMITS.items():
            value = args[name]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise NoteError(f"{name} must be nonempty and at most {limit} characters")
            clean = redactor.redact_at_rest(value.strip())
            if clean == redactor.REDACTION_FAILED_TOKEN:
                raise NoteError("secret redaction failed; note was not saved")
            values[name] = clean
    except NoteError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed before any file write
        raise NoteError(f"note validation failed ({type(exc).__name__})") from None
    ident = hashlib.sha256(values["title"].encode("utf-8")).hexdigest()[:16]
    stem = f"reference_{source}_{ident}"
    supersedes = None
    if "supersedes" in args:
        try:
            supersedes = _remember_supersedes(args["supersedes"], folder.resolve(), stem, redactor)
        except Exception:  # noqa: BLE001 - identifiers and redactor errors may contain secrets
            raise NoteError(
                "supersedes must name 1 to 8 distinct, safe, existing notes in this memory "
                "folder, with exact spelling; no paths, symlinks or self-reference"
            ) from None
    if session_dir:
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise NoteError(f"memory folder create failed ({type(exc).__name__})") from None
        if not folder.is_dir():
            raise NoteError("the session's memory folder is not a folder")
    path = folder.resolve() / (stem + ".md")
    front = {
        "name": f"reference-{source}-{ident}",
        "description": values["title"],
        "type": "reference",
        "source_client": source,
        "status": "active",
        "rule": values["rule"],
        "apply": values["apply"],
        "scope": "always",
    }
    if supersedes is not None:
        front["supersedes"] = supersedes
    content = "---\n" + "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in front.items()
    )
    content += "---\n\n# " + values["title"].replace("\n", " ") + "\n\n"
    content += values["body"] + "\n\nEvidence: " + values["evidence"] + "\n"
    try:
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise NoteError("a note with this title already exists; edit it directly")
            saved = "already saved"
        else:
            fd, temp = tempfile.mkstemp(prefix=".noblivion-note-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.write(content)
                os.chmod(temp, 0o600)
                os.link(temp, path)  # atomic and refuses to replace a concurrent writer
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            saved = "saved"
    except FileExistsError:
        raise NoteError("a note with this title was saved concurrently; retry recall") from None
    except OSError as exc:
        raise NoteError(f"note save failed ({type(exc).__name__})") from None
    # The existing sync hook schedules the normal indexer. The store's periodic
    # scan remains the fallback if this short hook call fails.
    sync_event = {
        "hook_event_name": "PostToolUse",
        "session_id": "mcp-memory-write",
        "cwd": str(path.parent),
        "tool_name": "Write",
        "tool_input": {"file_path": str(path)},
    }
    with contextlib.suppress(Exception):  # a saved note remains durable
        subprocess.run(
            [sys.executable, str(_HOOKS / "memory_sync_hook.py")],
            input=json.dumps(sync_event),
            text=True,
            capture_output=True,
            env=dict(env),
            timeout=5,
            check=False,
        )
    return {
        "saved": str(path),
        "status": saved,
        "source_client": source,
        "indexing": "scheduled or next store scan",
    }
