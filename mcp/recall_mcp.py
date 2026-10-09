#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
recall_mcp — stdio MCP server for shared recall and notes
=================================================================
The prompt hook (``hooks/recall_hook.py``) recalls memory automatically.
This server is the explicit path: the model calls
``noblivion_recall(query, k)`` when it wants to look something up. Same
store route, same rendering, no session dedupe and no output cap beyond k.

``noblivion_recall(fetch_id=<n>)`` reads ONE memory in full, by the id a
ranked-index line carries (``GET /api/memories/fetch/{id}``). Exactly one of
``query`` and ``fetch_id`` per call. ``include_mined=true`` (design doc
section 11.3) lists candidates from the ranked index with the
transcript-mined rows included (``GET /api/memories/index?include_mined=1``);
the model reads one with ``fetch_id``.

``noblivion_remember`` saves a verified, redacted Markdown note in
``NOBLIVION_MEMORY_DIR``, else in the session's project memory folder (the
folder the hooks read). The same indexer then serves it to both Codex and
Claude Code.

The server is stdlib only, so it speaks the MCP stdio transport directly: one
JSON-RPC 2.0 message per line on stdin, one per line on stdout. Methods:
``initialize``, ``ping``, ``tools/list``, ``tools/call``;
``notifications/*`` get no reply; anything else is ``-32601 Method not found``.

Failures are NOT silent here (unlike the hook): a store that is down or fails
the listener proof comes back as a tool result with ``isError: true`` and
"memory store not running (<reason>)", never a traceback and never the token
(design doc section 3.5).

Run: ``python3 mcp/recall_mcp.py`` (the plugin's ``.mcp.json`` names it).

Hit lines come from the hook's ``render``, so the tool output is as neutral
as the hook's (one inert line per hit, see "Neutral rendering" there).

The session root (design doc section 5.1) comes from ``CLAUDE_PROJECT_DIR``,
else the server's working dir, the same way the hook derives it from ``cwd``.

Environment: the hook's variables (see its docstring; ``NOBLIVION_RECALL_PROJECT``
selects the namespace for both), plus
  NOBLIVION_RECALL_MD_ONLY   any non-empty value: return memory files only, no
                             transcript-miner rows (the hook's filter). Default
                             off: the tool returns every row the route returns.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

SERVER_NAME = "noblivion-recall"


def _plugin_version() -> str:
    """The ``version`` of ``.claude-plugin/plugin.json``, so the MCP
    ``serverInfo`` names the plugin version (NOBLIVION-24)."""
    path = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return "0.0.0"
    return version if isinstance(version, str) else "0.0.0"


SERVER_VERSION = _plugin_version()
PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "noblivion_recall"
REMEMBER_NAME = "noblivion_remember"
K_DEFAULT = 5
K_MAX = 20
# W6, change F3. The re-ordered fetch puts the
# memory's RULE line and its APPLY block ABOVE the body, because a model that
# stops reading after the first lines must still get the instruction. Both are
# bounded here, on top of the body's own 6,000-character cap, so the answer
# cannot grow without a bound: in a measured corpus a rule was at most 160
# characters and an apply block at most 400, so these caps are headroom.
FETCH_RULE_MAX_CHARS = 400
FETCH_APPLY_MAX_CHARS = 1200

_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
STORE_DOWN_REASONS = frozenset({"store_down", "no_token", "foreign_listener"})


def _load_hook():
    """Import ``hooks/recall_hook.py`` by path, so sys.path is never touched."""
    name = "recall_hook"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HOOKS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TOOL_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Search the local NOBLIVION memory store (indexed project Markdown "
        "files) and return the matching memories as data: one line per hit, "
        "'- [entity] title: body (score)'. Use it before you act on a repo, host, "
        "ticket or process question that a past session may have settled. "
        "Pass fetch_id instead of query to read ONE memory in full, by the id shown "
        "on a line of a GROUNDED MEMORY INDEX."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall, free text (trimmed to 300 chars).",
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": K_MAX,
                "default": K_DEFAULT,
                "description": "Maximum number of hits.",
            },
            "fetch_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "(R1): read one memory in full by its id, as "
                    "shown on a GROUNDED MEMORY INDEX line. Use this OR query, "
                    "not both."
                ),
            },
            "include_mined": {
                "type": "boolean",
                "default": False,
                "description": (
                    "List candidates from the ranked index with the rows "
                    "mined from past transcripts included (lower trust; "
                    "they quote other sessions). Read one with fetch_id."
                ),
            },
        },
        # `query` is not required, because a fetch has no
        # query. Exactly one of the two is required, which a JSON Schema this
        # simple cannot say, so the tool body says it — with a message that
        # names both, rather than a validation error that names neither.
        "required": [],
        "additionalProperties": False,
    },
}

REMEMBER_SCHEMA: Dict[str, Any] = {
    "name": REMEMBER_NAME,
    "description": (
        "Save one verified, durable project lesson in the shared NOBLIVION "
        "Markdown corpus. Supply evidence. Do not save secrets, guesses, "
        "raw transcripts, or task chatter. The same note is available to "
        "Codex and Claude Code after indexing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            name: {"type": "string"} for name in ("title", "rule", "apply", "body", "evidence")
        },
        "required": ["title", "rule", "apply", "body", "evidence"],
        "additionalProperties": False,
    },
}


def _redactor():
    """Load the store's stdlib redactor without importing its venv package."""
    path = Path(__file__).resolve().parent.parent / "src" / "noblivion" / "redaction.py"
    spec = importlib.util.spec_from_file_location("noblivion_mcp_redaction", path)
    if spec is None or spec.loader is None:
        raise ImportError("redaction")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _remember_folder(env: Mapping[str, str]) -> Path | Dict[str, Any]:
    """The folder ``noblivion_remember`` writes to, or a tool error.

    ``NOBLIVION_MEMORY_DIR`` when it is set. Else the session's memory
    folder by the hooks' own rule (``hook_config.resolve_memory_dir``): the
    project dir is ``CLAUDE_PROJECT_DIR``, which Claude Code passes to this
    server, else the server's working dir. The tool never makes the folder.
    """
    raw_dir = env.get("NOBLIVION_MEMORY_DIR", "").strip()
    if raw_dir:
        folder = Path(os.path.expanduser(raw_dir))
        if not folder.is_absolute() or not folder.is_dir():
            return _tool_error("NOBLIVION_MEMORY_DIR must be an existing absolute folder")
        return folder
    try:
        cfg = _load_hook()._sibling_module("hook_config")
        found = cfg.resolve_memory_dir(os.getcwd(), env)
    except Exception as exc:  # noqa: BLE001 - fail closed before any file write
        return _tool_error(f"memory folder lookup failed ({type(exc).__name__})")
    if found is None or not found.is_absolute():
        return _tool_error("no project memory folder found; set NOBLIVION_MEMORY_DIR")
    if not found.is_dir():
        return _tool_error(
            f"the project memory folder {found} does not exist; create it, "
            "or set NOBLIVION_MEMORY_DIR to an existing absolute folder"
        )
    return found


def noblivion_remember(
    args: Dict[str, Any], environ: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Save a redacted note as a source file. The existing indexer owns DB writes."""
    env = os.environ if environ is None else environ
    folder = _remember_folder(env)
    if isinstance(folder, dict):
        return folder
    if not isinstance(args, dict) or set(args) != set(REMEMBER_SCHEMA["inputSchema"]["required"]):
        return _tool_error("title, rule, apply, body, and evidence are required; no other fields")
    caps = {"title": 160, "rule": 400, "apply": 1200, "body": 6000, "evidence": 1500}
    try:
        redactor = _redactor()
        values = {}
        for name, limit in caps.items():
            value = args[name]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return _tool_error(f"{name} must be nonempty and at most {limit} characters")
            clean = redactor.redact_at_rest(value.strip())
            if clean == redactor.REDACTION_FAILED_TOKEN:
                return _tool_error("secret redaction failed; note was not saved")
            values[name] = clean
    except Exception as exc:  # noqa: BLE001 - fail closed before any file write
        return _tool_error(f"note validation failed ({type(exc).__name__})")
    source = env.get("NOBLIVION_SOURCE_CLIENT", "claude_code").strip()
    if source not in ("codex", "claude_code"):
        return _tool_error("NOBLIVION_SOURCE_CLIENT must be codex or claude_code")
    ident = hashlib.sha256(values["title"].encode("utf-8")).hexdigest()[:16]
    path = folder.resolve() / f"reference_{source}_{ident}.md"
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
    content = "---\n" + "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in front.items()
    )
    content += "---\n\n# " + values["title"].replace("\n", " ") + "\n\n"
    content += values["body"] + "\n\nEvidence: " + values["evidence"] + "\n"
    try:
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                return _tool_error("a note with this title already exists; edit it directly")
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
        return _tool_error("a note with this title was saved concurrently; retry recall")
    except OSError as exc:
        return _tool_error(f"note save failed ({type(exc).__name__})")
    # The existing sync hook schedules the normal indexer. The store's periodic
    # scan remains the fallback if this short hook call fails. The hook gets
    # the note's folder as the override: its ``cwd`` is that folder, so it
    # cannot derive the session's folder from it.
    sync_env = dict(env)
    sync_env["NOBLIVION_MEMORY_DIR"] = str(folder)
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
            env=sync_env,
            timeout=5,
            check=False,
        )
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "saved": str(path),
                        "status": saved,
                        "source_client": source,
                        "indexing": "scheduled or next store scan",
                    }
                ),
            }
        ],
        "isError": False,
    }


def tool_schema(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The advertised tool. Byte-identical to ``TOOL_SCHEMA`` unless W6's
    ``NOBLIVION_RECALL_INDEX_RULE_ROWS`` is set, so arm D lists what it always
    listed.

    With the rule shape on, the index shows no rank, so the model is told that
    a small number is read as a position. Saying it in the schema and not only
    in the error path matters: a model that does not know it may fetch nothing
    at all rather than guess.
    """
    hook = _load_hook()
    if not hook.index_rule_rows(os.environ if environ is None else environ):
        return TOOL_SCHEMA
    doc = copy.deepcopy(TOOL_SCHEMA)
    doc["inputSchema"]["properties"]["fetch_id"]["description"] += (
        " The rule index shows no rank, so a number from 1 to "
        f"{hook.FETCH_RANK_MAX} that is not an id is read as the POSITION of a "
        "row in the index you were last shown."
    )
    return doc


def tool_env(hook: Any, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The env with the session's memory folder and root filled in, from
    ``CLAUDE_PROJECT_DIR`` or the working dir (``recall_hook.session_env``)."""
    env = os.environ if environ is None else environ
    cwd = env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return hook.session_env(env, {"cwd": cwd})


def noblivion_recall(
    query: str = "",
    k: int = K_DEFAULT,
    environ: Optional[Dict[str, str]] = None,
    fetch_id: Any = None,
    include_mined: Any = False,
) -> Dict[str, Any]:
    """The tool body. Returns an MCP tools/call result dict.

    Two modes, and exactly one of them per call: ``query`` searches and returns
    one line per hit, and ``fetch_id`` (R1) returns ONE memory in
    full, by the id a ranked index line carries.
    """
    hook = _load_hook()
    environ = tool_env(hook, environ)
    if not isinstance(include_mined, bool):
        return _tool_error("include_mined must be true or false")
    if fetch_id is not None:
        if isinstance(query, str) and query.strip():
            return _tool_error("pass query OR fetch_id, not both")
        return _fetch_one(hook, fetch_id, environ)
    if not isinstance(query, str) or not query.strip():
        return _tool_error(
            "query must be a non-empty string, or pass fetch_id to read one memory by id"
        )
    try:
        k = int(k)
    except (TypeError, ValueError):
        return _tool_error("k must be an integer")
    k = max(1, min(K_MAX, k))
    q = hook._collapse(query)[: hook.QUERY_MAX_CHARS]
    env = environ
    if include_mined:
        return _mined_index(hook, q, k, env)
    md_only = bool(hook.md_only_on(env))
    t0 = time.monotonic()
    try:
        # md_only is passed only when set, so the default call is unchanged.
        hits = hook.recall(q, k, environ, md_only=True) if md_only else hook.recall(q, k, environ)
    except hook.RecallError as exc:
        if exc.reason == "bad_project":
            return _tool_error(
                "NOBLIVION_RECALL_PROJECT refused (bad_project): lower-case letters, digits and _ only"
            )
        return _store_error(exc.reason)
    except Exception as exc:  # noqa: BLE001 — a tool result, never a traceback
        return _tool_error(f"recall failed ({type(exc).__name__})")
    ms = int((time.monotonic() - t0) * 1000)
    project = hook.recall_project(environ)
    text = hook.render(hits, ms, cap=None, project=project) or hook.HEADER.format(
        persona=project, n=0, ms=ms
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _mined_index(hook: Any, query: str, k: int, env: Mapping[str, str]) -> Dict[str, Any]:
    """``include_mined``: the ranked index with the mined rows, one line per
    candidate (design doc section 11.3). No bodies; ``fetch_id`` reads one."""
    t0 = time.monotonic()
    try:
        lines = hook.recall_index(query, k, env, include_mined=True)
    except hook.RecallError as exc:
        if exc.reason == "bad_project":
            return _tool_error(
                "NOBLIVION_RECALL_PROJECT refused (bad_project): lower-case letters, digits and _ only"
            )
        return _store_error(exc.reason)
    except Exception as exc:  # noqa: BLE001 — a tool result, never a traceback
        return _tool_error(f"recall failed ({type(exc).__name__})")
    ms = int((time.monotonic() - t0) * 1000)
    project = hook.recall_project(env)
    text, _shown = hook.render_index_capped(lines, ms, None, project, False)
    return {
        "content": [{"type": "text", "text": text or hook.index_header(0, ms, project, False)}],
        "isError": False,
    }


def _resolve_fetch_id(hook: Any, value: int, env: Mapping[str, str]) -> tuple[int, str]:
    """W6 F3: ``(id, note)``. Read a small number as a RANK when it is not an id.

    The order of the tests is the safe one and not the convenient one:

    1. a value that IS an id in the last index is that memory, always. A rank
       can never take an id away from the model.
    2. a value above ``FETCH_RANK_MAX`` is an id. Ids in this pool start at
       18,947 and an index shows 30 to 32 rows, so the two number spaces do not
       meet and no threshold guess is being made.
    3. a value at most ``FETCH_RANK_MAX`` that matches a rank of the last index
       is that row's id, and the answer SAYS so, because a model that meant an
       id must be able to see that it was read as a position.

    Anything else is passed through as an id and fails in the store, which is
    the honest failure: the server does not invent a memory for a number it
    cannot place.
    """
    rows = hook.load_last_index(hook.cache_dir(env))
    if not rows:
        return value, ""
    if any(row["id"] == value for row in rows):
        return value, ""
    if value > hook.FETCH_RANK_MAX:
        return value, ""
    for row in rows:
        if row["rank"] == value:
            return row["id"], f"fetch_id {value} read as rank {value} = id {row['id']}"
    return value, ""


def _local_rule_apply(hook: Any, env: Mapping[str, str], name: str) -> tuple[str, str]:
    """W6 F3: the ``rule:`` and ``apply:`` of the LOCAL file this memory came
    from, or ``("", "")``.

    They are read from the session's memory folder and never from the store,
    because the local file is the one source of the two fields, and the store
    row may lag behind an edit until the next index scan.

    The join key is the frontmatter ``name``, which is the ``title`` the store
    returns, exactly as the index rows join. ``load_memory_corpus`` is the one
    reader of the folder, shared with the hook; it costs 0.111 s over 674 files,
    measured 2026-09-29, which a tool call can pay and a 2 s hook could not.
    """
    folder = hook.memory_dir(env)
    if not folder or not name:
        return "", ""
    try:
        by_name = hook.corpus_by_name(hook.load_memory_corpus(folder))
    except (OSError, ValueError):
        return "", ""
    md = by_name.get(name)
    return (md.rule, md.apply_block) if md is not None else ("", "")


def _fetch_one(
    hook: Any, fetch_id: Any, environ: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """(R1) — read one memory in full, by id.

    The other half of the ranked index. The index offers candidates with no
    bodies; this returns the body of the one the model chose. Failures are loud
    here, as everywhere in this server: the model asked for a specific id, so
    "nothing came back" must say why.

    The body is rendered with ``hook.neutral_block``, not ``hook.render``: it is
    multi-line text and must keep its line breaks while staying as inert as a
    one-line hit. The store has already redacted it at rest.
    """
    # int(1.5) is 1, and int(True) is 1: either would fetch a DIFFERENT memory
    # than the caller named and answer as though nothing was wrong. Parsing the
    # value's own text form refuses both, and still accepts the JSON integer and
    # the string a transport may hand over.
    try:
        mid = int(str(fetch_id).strip())
    except (TypeError, ValueError):
        return _tool_error("fetch_id must be a whole number, as shown on an index line")
    if mid < 1:
        return _tool_error("fetch_id must be a positive integer")
    env = os.environ if environ is None else environ
    # W6 F3, both halves behind the one variable arm E sets. Arm D reaches
    # neither branch, so its fetch is the Phase 3 fetch, character for character.
    rule_rows = hook.index_rule_rows(env)
    note = ""
    if rule_rows:
        mid, note = _resolve_fetch_id(hook, mid, env)
    try:
        answer = hook.fetch_memory_text(mid, environ)
    except hook.RecallError as exc:
        if exc.reason == "bad_project":
            return _tool_error(
                "NOBLIVION_RECALL_PROJECT refused (bad_project): lower-case letters, digits and _ only"
            )
        return _store_error(exc.reason)
    except Exception as exc:  # noqa: BLE001 — a tool result, never a traceback
        return _tool_error(f"fetch failed ({type(exc).__name__})")
    reason = answer.get("reason")
    if reason:
        return _tool_error(f"memory {mid} not returned ({reason})")
    # Review round 1 on #2222: every other field of this answer is treated as
    # untrusted shape (title through str(), source through isinstance), and the
    # body must be too. A truthy non-string would make neutral_block call string
    # methods on it and raise, which this tool answers with a traceback instead
    # of an isError result. A bad shape is a tool error, like every other one.
    raw_text = answer.get("text")
    if raw_text is not None and not isinstance(raw_text, str):
        return _tool_error(
            f"memory {mid} came back with a {type(raw_text).__name__} body, not text"
        )
    body = hook.neutral_block(raw_text or "")
    if not body.strip():
        return _tool_error(f"memory {mid} is empty")
    title = hook._neutral(str(answer.get("title") or ""), hook.TITLE_MAX_CHARS) or "memory"
    source = answer.get("source")
    source_line = (
        f" [{hook._neutral(str(source), hook.TITLE_MAX_CHARS)}]"
        if isinstance(source, str) and source
        else ""
    )
    head = f"GROUNDED MEMORY {mid}: {title}{source_line}"
    if hook.shown_set_on(env):
        # W8 DP-6. What this fetch returned, so that for the rest of the session
        # the index gives it no block and the trigger leg no row. Recorded only
        # after the body proved non-empty, so a failed fetch silences nothing.
        # The key is the raw title, the name the index joins on, not the
        # neutralised one above. A failed write is not a failed fetch: the model
        # still gets the memory it asked for.
        raw_title = answer.get("title")
        if isinstance(raw_title, str) and raw_title:
            hook.record_shown(hook.cache_dir(env), None, "fetched", [raw_title])
    if not rule_rows:
        return {"content": [{"type": "text", "text": f"{head}\n{body}"}], "isError": False}
    # W6 F3: RULE, then APPLY, then the body. The body keeps its own 6,000-char
    # cap untouched, so a memory that fitted whole before still fits whole; the
    # two blocks above it are bounded separately and add at most 1,600
    # characters plus their labels.
    parts = [head]
    if note:
        parts.append(note)
    rule, apply_block = _local_rule_apply(hook, env, str(answer.get("title") or ""))
    if rule:
        parts.append("RULE: " + hook._neutral(rule, FETCH_RULE_MAX_CHARS))
    if apply_block:
        parts.append("APPLY:\n" + hook.neutral_block(apply_block, total_max=FETCH_APPLY_MAX_CHARS))
    parts.append(body)
    return {"content": [{"type": "text", "text": "\n".join(parts)}], "isError": False}


def _store_error(reason: str) -> Dict[str, Any]:
    """The tool error for a store call that failed (design doc section 3.5)."""
    if reason in STORE_DOWN_REASONS:
        return _tool_error(f"memory store not running ({reason})")
    if reason in ("plaintext_url", "bad_url"):
        return _tool_error(f"store URL refused ({reason})")
    return _tool_error(f"memory store unavailable ({reason})")


def _tool_error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": f"{TOOL_NAME}: {message}"}], "isError": True}


# ── JSON-RPC ────────────────────────────────────────────────────────────────


def _result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _valid_request_id(req_id: Any) -> bool:
    """MCP 2025-06-18: "Requests MUST include a string or integer ID. Unlike
    base JSON-RPC, the ID MUST NOT be null." bool is an int in Python; reject it."""
    return isinstance(req_id, str) or (isinstance(req_id, int) and not isinstance(req_id, bool))


def handle(message: Any, environ: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """One request in, one response out. None for a notification: any message
    without an ``id`` member (JSON-RPC 2.0, whatever the method) and any
    ``notifications/*`` method. MCP narrows JSON-RPC: a request id MUST be a
    string or an integer, never null, so ``"id": null`` (or any other type)
    is an Invalid Request and the error carries id null, as JSON-RPC 2.0
    requires when the id cannot be trusted. A request whose ``jsonrpc``
    member is missing or not exactly "2.0" is an Invalid Request too, with
    its id, and is never dispatched."""
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request")
    method = message.get("method")
    is_request = "id" in message
    req_id = message.get("id")
    if is_request and not _valid_request_id(req_id):
        return _error(None, -32600, "Invalid Request: id must be a string or an integer")
    raw_params = message.get("params")
    params: Dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if not isinstance(method, str):
        return _error(req_id, -32600, "Invalid Request")  # the spec answers this one with id null
    if not is_request or method.startswith("notifications/"):
        return None
    if message.get("jsonrpc") != "2.0":
        # JSON-RPC 2.0: the member MUST be exactly "2.0". Checked before any
        # dispatch, so a mis-versioned tools/call never reaches the store.
        return _error(req_id, -32600, 'Invalid Request: jsonrpc must be "2.0"')
    if method == "initialize":
        # MCP negotiation: answer the requested version only when the server
        # supports it, else a version it does support. This server implements
        # exactly one, so the answer is always PROTOCOL_VERSION; echoing an
        # unknown request would claim support the server does not have.
        return _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [tool_schema(environ), REMEMBER_SCHEMA]})
    if method == "tools/call":
        name = params.get("name")
        raw_args = params.get("arguments")
        args: Dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        if name == REMEMBER_NAME:
            return _result(req_id, noblivion_remember(args, environ))
        if name != TOOL_NAME:
            return _error(req_id, -32602, f"Unknown tool: {name}")
        return _result(
            req_id,
            noblivion_recall(
                args.get("query", ""),
                args.get("k", K_DEFAULT),
                environ,
                fetch_id=args.get("fetch_id"),
                include_mined=args.get("include_mined", False),
            ),
        )
    return _error(req_id, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None, environ: Optional[Dict[str, str]] = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: Optional[Dict[str, Any]] = _error(None, -32700, "Parse error")
        else:
            try:
                response = handle(message, environ)
            except Exception as exc:  # noqa: BLE001 — the loop must survive any one request
                rid = message.get("id") if isinstance(message, dict) else None
                response = _error(rid, -32603, f"Internal error: {type(exc).__name__}")
        if response is not None:
            try:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
            except (BrokenPipeError, OSError):
                # The client closed its end. Leave quietly with exit 0: a
                # traceback here is noise in the client's log, nothing more.
                if stdout is sys.stdout:
                    _silence_stdout()
                return 0
    return 0


def _silence_stdout() -> None:
    """After a broken pipe the interpreter flushes sys.stdout once more at
    exit and reports the same error again. Point the descriptor at /dev/null
    first so the exit stays clean."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    sys.exit(serve())
