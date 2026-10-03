# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local memory corpus helpers of the prompt recall hook.

These helpers are pure: they read the memory folder and the recall hook's
per-session cache files, and need no store. The recall hook
(``recall_hook.py``) re-exports them under their old names, and the
continuity hook and ``memory_text`` use them without loading the recall hook.

``recall_index`` is the one REST call. It uses ``recall_hook.py`` from this
folder (loaded once). Without that file it raises ``RecallUnavailable``; when
the store is down it raises the recall hook's ``RecallError``. A caller then
falls back to its local order (the continuity hook ranks by recency alone and
logs ``recency_only``).

Standard library only.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
RECALL_HOOK = "recall_hook"

SHOWN_SET_NAME = "shown_set.json"
SHOWN_KINDS: Tuple[str, ...] = ("apply", "fetched", "trigger")
SHOWN_ROW_KIND = "row"
SHOWN_OPTIONAL_KINDS: Tuple[str, ...] = (SHOWN_ROW_KIND,)
SESSION_DIR_NAME = "by-session"
_CLOSED_STATUS_RE = re.compile(
    "^\\W{0,4}\\s*(\u2705|FIXED|RESOLVED|Resolved|resolved|CLOSED|Closed"
    "|SUPERSEDED|Superseded|superseded|DONE|Done|MERGED|Merged|Archived"
    "|ARCHIVED|Stale|STALE|OBSOLETE|Obsolete)"
)
_DROPPED_KINDS = frozenset({"index", "topic"})
_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][\w\-]*):\s*(.*)$")
_SUB_TOKEN_SPLIT_RE = re.compile(r"[_/.\-]+")
_DAEMON_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9_.\-/]+")
_KNOWN_KINDS = frozenset({"feedback", "project", "reference", "topic", "user", "index"})
_INDEX_FILES = frozenset({"MEMORY.md", "MEMORY_ARCHIVE.md"})
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# Characters that are not whitespace but can still break a line, hide text or
# smuggle it past a reader: every Unicode "other" category (Cc controls, Cf
# format characters such as zero-width, bidi overrides and tag characters, Cs
# lone surrogates, Co private use, Cn unassigned) and the variation selectors
# (category Mn, which can hide bytes behind one visible character).
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_VARIATION_SELECTORS = (range(0x180B, 0x1810), range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))
_ANGLE = str.maketrans({"<": "\u2039", ">": "\u203a"})
_BRACKET = str.maketrans({"[": "(", "]": ")"})
# Raw characters read per output character before the rules run: the cut only
# drops text, so it cannot make a line less inert, and it bounds the work on a
# very long body (the response may be 1 MiB).
_NEUTRAL_READ_FACTOR = 8


def _hidden(ch: str) -> bool:
    if unicodedata.category(ch) in _HIDDEN_CATEGORIES:
        return True
    o = ord(ch)
    return any(o in r for r in _VARIATION_SELECTORS)


def _neutral(text: str, limit: Optional[int] = None) -> str:
    """One line of inert text, at most ``limit`` characters. In order: NFKC
    (so a full-width or small ``<`` becomes ``<``), every whitespace
    character (``\\n``, ``\\r``, U+0085, U+2028, U+2029, ...) becomes a space and
    every hidden character (see above) goes, space runs become one space,
    ``<``/``>`` become ``‹``/``›``. See "Neutral rendering" above."""
    text = text or ""
    if limit is not None:
        text = text[: limit * _NEUTRAL_READ_FACTOR + 64]
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or not _hidden(ch))
    text = re.sub(" {2,}", " ", text).strip().translate(_ANGLE)
    return text if limit is None else text[:limit]


#: An absolute path, or one under the home directory: a leading "/" or "~" and
#: at least two segments, so a lone "/tmp" and a date such as 2026/09/27 are left
#: alone. The lookbehind keeps a repository-relative path out of it: in
#: ``tools/worktree_guard.py`` the "/" follows a word character.
_HOST_PATH = re.compile(r"(?<![\w.~])(?:~|/[A-Za-z0-9._+-]+)(?:/[A-Za-z0-9._+-]+)+/?")
_HOST_PATH_MARK = "‹path›"


def strip_host_paths(text: str) -> str:
    """An index row with every absolute path folded to a marker.

    Measured on 16 benchmark runs that injected an index: 15 of them named a
    host path, 42 occurrences and 9 distinct ones, including the path of a
    secrets file. The index exists so the
    model can CHOOSE what to read next, and it does not need the host's layout to
    choose; a benchmark run that is handed the path to the secrets is no longer
    isolated from the host that runs it. A memory the model then fetches by id
    still carries its own text, paths and all, so nothing is lost but the leak.

    A repository-relative path such as ``tools/worktree_guard.py`` stays. It names
    the repository, not the host, and it is often the whole point of the memory.
    """
    return _HOST_PATH.sub(_HOST_PATH_MARK, text or "")


def session_state_file(cache: str, name: str, session_id: Any = None) -> str:
    """WI-9. The path a WRITER of ``name`` uses: the session's own file for a
    valid session id, the single file in the cache folder without one."""
    sid = _valid_sid(session_id)
    if sid:
        return os.path.join(cache, SESSION_DIR_NAME, f"{sid}.{name}")
    return os.path.join(cache, name)


def _newest_state_file(cache: str, name: str) -> str:
    """The newest file of ``name``: the single file or any session's. For a
    reader that has no session id. The single file's path when none exists."""
    single = os.path.join(cache, name)
    best, best_m = single, -1.0
    with contextlib.suppress(OSError):
        best_m = os.stat(single).st_mtime
    folder = os.path.join(cache, SESSION_DIR_NAME)
    try:
        entries = os.listdir(folder)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.endswith("." + name):
            continue
        path = os.path.join(folder, entry)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if mtime > best_m:
            best, best_m = path, mtime
    return best


def session_read_file(cache: str, name: str, session_id: Any = None, newest: bool = True) -> str:
    """WI-9. The path a READER of ``name`` opens. With a session id: the
    session's own file, or the single file while the session has none yet (the
    caller's session check on the content still applies). Without one: the
    newest file, or with ``newest=False`` the single file only."""
    if _valid_sid(session_id):
        own = session_state_file(cache, name, session_id)
        return own if os.path.exists(own) else os.path.join(cache, name)
    return _newest_state_file(cache, name) if newest else os.path.join(cache, name)


def _valid_sid(session_id: Any) -> Optional[str]:
    """The session id if it has the shape ``session_file`` accepts, else None."""
    if isinstance(session_id, str) and _SESSION_ID_RE.match(session_id):
        return session_id
    return None


def _empty_shown(session: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"session": session}
    for kind in SHOWN_KINDS:
        out[kind] = []
    return out


def load_shown_set(
    cache: str, session_id: Any = None, path: Optional[str] = None
) -> Dict[str, Any]:
    """The shown-set: ``{"session": ..., "apply": [...], "fetched": [...],
    "trigger": [...]}``, each list the sorted memory NAMES (the frontmatter
    ``name``, which is the daemon's title and the index's join key). Never raises.

    A hook knows its session and passes it. A file written for a different
    session is then read as empty, so where sessions follow each other in one
    folder, one session's set cannot silence the next. The MCP server knows no
    session and passes None: it reads what the folder holds, which in the bench
    is the run's one session. Two sessions running AT ONCE in one folder each
    have their own file (WI-9, SESSION_DIR_NAME); ``path`` names the file for a
    caller that already chose and locked one.

    Every field is re-validated, for the reason ``load_last_index`` gives.
    """
    sid = _valid_sid(session_id)
    try:
        with open(
            path or session_read_file(cache, SHOWN_SET_NAME, session_id), encoding="utf-8"
        ) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _empty_shown(sid)
    if not isinstance(doc, dict):
        return _empty_shown(sid)
    stored = _valid_sid(doc.get("session"))
    if sid and stored and stored != sid:
        return _empty_shown(sid)
    out = _empty_shown(sid or stored)
    for kind in SHOWN_KINDS:
        items = doc.get(kind)
        if isinstance(items, list):
            out[kind] = sorted({x for x in items if isinstance(x, str) and x})
    # WI-17. An optional list is a key only when the file has it, so a set that
    # never held one reads exactly as it did before.
    for kind in SHOWN_OPTIONAL_KINDS:
        items = doc.get(kind)
        if isinstance(items, list):
            out[kind] = sorted({x for x in items if isinstance(x, str) and x})
    return out


class MemoryFile:
    """One memory file of the run's own folder, as this hook reads it.

    ``name`` is the frontmatter ``name``, which is also the ``title`` the daemon
    puts on an index row, so it is the join key between a row and its file.
    ``tokens`` is the sub-token list P1h scores; it is computed once per file.
    """

    __slots__ = ("rel_path", "name", "kind", "description", "body", "tokens", "rule", "apply_block")

    def __init__(
        self,
        rel_path: str,
        name: str,
        kind: str,
        description: str,
        body: str,
        tokens: List[str],
        rule: str = "",
        apply_block: str = "",
    ):
        self.rel_path = rel_path
        self.name = name
        self.kind = kind
        self.description = description
        self.body = body
        self.tokens = tokens
        # W5 writes these two into the frontmatter of the ANNOTATED corpus only.
        # Both are "" for the un-annotated corpus arms A and D read, so a file
        # that has never been annotated is not a different object here.
        self.rule = rule
        self.apply_block = apply_block

    @property
    def dropped(self) -> bool:
        """True when P3 drops every row that points at this file.

        The same rule the offline replay used, in the same order: an index or
        topic file, a ``MEMORY*`` index, or a description that opens with a
        closed-status marker. When a file has no description the first 200
        characters of the body stand in for it, because a file without
        frontmatter still carries its status in its first sentence.
        """
        if self.kind in _DROPPED_KINDS:
            return True
        if os.path.basename(self.rel_path).startswith("MEMORY"):
            return True
        head = (self.description or self.body[:200]).strip()
        return bool(_CLOSED_STATUS_RE.match(head))


def _unquote_frontmatter(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """``(frontmatter, body)``. The mirror's parser, reproduced.

    Hand-rolled for the reason the mirror gives: the corpus has unquoted
    ``description:`` values holding ``": "`` and emoji, which a strict YAML
    parser rejects. Only the shapes the corpus uses are read: top-level
    ``key: value`` and one level of indented nesting (``metadata:`` then
    ``  type:``). A file with no leading ``---``, or with frontmatter that is
    never closed, has no frontmatter and is all body.
    """
    lines = text.split("\n")  # not splitlines(): the body stays exact
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, Any] = {}
    parent: Optional[str] = None
    end: Optional[int] = None
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            end = i
            break
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            m = _FRONTMATTER_KEY_RE.match(line.strip())
            if parent is not None and m:
                sub = meta.get(parent)
                if not isinstance(sub, dict):
                    sub = {}
                    meta[parent] = sub
                sub[m.group(1)] = _unquote_frontmatter(m.group(2))
            continue
        m = _FRONTMATTER_KEY_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value.strip() == "":
            parent = key
            meta.setdefault(key, {})
        else:
            parent = None
            meta[key] = _unquote_frontmatter(value)
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def _rule_field(meta: Mapping[str, Any], key: str) -> Any:
    """The frontmatter value of rule field ``key``: the top-level line, else
    the line indented under ``metadata:``.

    The Edit and Write tools sometimes rewrite a memory's frontmatter and move
    the rule fields under ``metadata:``. ``parse_frontmatter`` already keeps
    that one level of nesting, so no second parser and no import is needed.
    A top-level value that holds text wins, the rule
    ``memory_fields.read_fields`` uses. With nothing usable under
    ``metadata:`` the top-level value is returned as it stands, so a file
    without nested fields reads exactly as it did before. Never raises.
    """
    top = meta.get(key)
    if isinstance(top, str) and top.strip():
        return top
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        low = nested.get(key)
        if isinstance(low, str) and low.strip():
            return low
    return top


def read_rule_apply(meta: Mapping[str, Any]) -> Tuple[str, str]:
    """``(rule, apply)`` from one file's frontmatter. ``("", "")`` when absent.

    The ONE reader of the two fields the reference rule annotator
    writes, so the format is agreed in one place and not guessed twice.

    ``rule:`` is one plain line and arrives here as it was written.

    Each field is read at the top level and, when it has no text there, from
    the line indented under ``metadata:`` (see ``_rule_field``).

    ``apply:`` is one line holding a double-quoted JSON string, so a block with
    newlines survives three hand-rolled single-line frontmatter parsers without
    any of them reading a continuation line as a sub-key or as a trigger item.
    ``parse_frontmatter`` has already stripped the pair of outer quotes, which
    leaves a valid JSON string body, so the quotes go back on before decoding.
    A value that does not decode is returned as it stands rather than dropped:
    a malformed line must degrade to text, never to an exception on a hook that
    has a 2 s budget and fails open.
    """
    rule_v = _rule_field(meta, "rule")
    apply_v = _rule_field(meta, "apply")
    rule = rule_v.strip() if isinstance(rule_v, str) else ""
    if not isinstance(apply_v, str) or not apply_v:
        return rule, ""
    for candidate in ('"%s"' % apply_v, apply_v):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(decoded, str):
            return rule, decoded
    return rule, apply_v


def frontmatter_kind(rel_path: str, meta: Mapping[str, Any]) -> str:
    """The file's kind: the filename prefix first, then the frontmatter type.
    The mirror's ``kind_for``, reproduced, including its fallback."""
    base = os.path.basename(rel_path)
    if base in _INDEX_FILES:
        return "index"
    prefix = base.split("_", 1)[0].lower() if "_" in base else ""
    if prefix in _KNOWN_KINDS:
        return prefix
    declared = meta.get("type")
    if not (isinstance(declared, str) and declared.strip()):
        nested = meta.get("metadata")
        declared = nested.get("type") if isinstance(nested, dict) else None
    if isinstance(declared, str) and declared.strip().lower() in _KNOWN_KINDS:
        return declared.strip().lower()
    return "reference"  # the mirror's CATEGORY_FALLBACK


def tokens_daemon(text: str) -> List[str]:
    """The daemon's tokenizer (store/memory.py `_tokenize`)."""
    return [t for t in _DAEMON_TOKEN_SPLIT_RE.split(text.lower()) if len(t) >= 2]


def tokens_sub(text: str) -> List[str]:
    """P1h's tokenizer: the daemon's tokens, plus the parts of any token that
    splits on ``_ / . -`` into two or more parts of two or more characters.

    This is the whole of change P1. ``feedback_never_git_checkout`` is one token
    to the daemon, so a prompt that says "git checkout" scores zero against the
    one memory that is about it. Splitting the token makes the match possible.
    """
    out: List[str] = []
    for t in tokens_daemon(text):
        out.append(t)
        parts = [p for p in _SUB_TOKEN_SPLIT_RE.split(t) if len(p) >= 2]
        if len(parts) > 1:
            out.extend(parts)
    return out


def build_memory_content(name: str, description: str, rel_path: str, body: str) -> str:
    """The text the daemon stored for this file, un-redacted.

    The mirror's ``build_content``, reproduced, because P1h must tokenize the
    same text the daemon indexed or the two legs would rank different documents.
    The at-rest redactor is deliberately NOT applied: measured 2026-09-29 over
    the frozen 674-file corpus, redaction changes the sub-token list of 51 files
    and moves exactly one target rank, o08 from 428 to 427, a trap that is out of
    reach either way. It costs 0.46 s of the 0.5 s latency budget of gate G-P1h.
    So it buys one rank on an unreachable trap and would spend the budget; the
    evidence is in `precision-replay/w4_probe_redact.py`.
    """
    parts = ["# %s" % name]
    if description:
        parts.append(description)
    parts.append("[claude_code_md: %s]" % rel_path)
    if body.strip():
        parts.append(body.rstrip())
    return "\n\n".join(parts)


def memory_name(base: str, meta: Mapping[str, Any]) -> str:
    """The join key of a memory file: its frontmatter ``name``, else the file
    name without ``.md``. One rule for the corpus reader and the trigger leg's
    rows (W8), so a row and the shown-set can never name one file two ways."""
    name_v = meta.get("name")
    name = (name_v if isinstance(name_v, str) else "").strip()
    return name or (base[:-3] if base.endswith(".md") else base)


def load_memory_corpus(folder: str) -> List[MemoryFile]:
    """Every non-empty top-level ``*.md`` of ``folder``, sorted by name.

    Top level only, which is the corpus Claude Code loads and the corpus the
    mirror ingested. The folder also holds a hidden ``.archive/`` of retired
    files and ``MEMORY.md.pre-compact-*`` backups, and neither is memory.
    """
    out: List[MemoryFile] = []
    for base in sorted(os.listdir(folder)):
        if not base.endswith(".md"):
            continue
        path = os.path.join(folder, base)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.strip():
            continue
        text = raw.decode("utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        name = memory_name(base, meta)
        desc_v = meta.get("description")
        description = (desc_v if isinstance(desc_v, str) else "").strip()
        rule, apply_block = read_rule_apply(meta)
        out.append(
            MemoryFile(
                rel_path=base,
                name=name,
                kind=frontmatter_kind(base, meta),
                description=description,
                body=body,
                tokens=tokens_sub(build_memory_content(name, description, base, body)),
                rule=rule,
                apply_block=apply_block,
            )
        )
    return out


def corpus_by_name(corpus: Sequence[MemoryFile]) -> Dict[str, MemoryFile]:
    """``name -> file``. On a duplicate name the first file in sorted order
    wins, so the map is a function of the folder and not of the read order."""
    out: Dict[str, MemoryFile] = {}
    for md in corpus:
        out.setdefault(md.name, md)
    return out


class RecallUnavailable(Exception):
    """The recall hook is not installed next to this file."""


_RECALL_HOOK: Any = None


def _recall_hook():
    """``recall_hook.py`` from this folder, loaded once per process."""
    global _RECALL_HOOK
    if _RECALL_HOOK is not None:
        return _RECALL_HOOK
    path = _HERE / f"{RECALL_HOOK}.py"
    if not path.is_file():
        raise RecallUnavailable("recall_hook_absent")
    spec = importlib.util.spec_from_file_location(RECALL_HOOK, path)
    if spec is None or spec.loader is None:
        raise RecallUnavailable("recall_hook_unloadable")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(RECALL_HOOK, mod)
    spec.loader.exec_module(mod)
    _RECALL_HOOK = mod
    return mod


def recall_index(
    query: str,
    k: int,
    environ: Optional[Mapping[str, str]] = None,
    root: Optional[str] = None,
) -> List[Any]:
    """The store's ranked index for ``query`` (rows with ``title`` and
    ``mid``), through ``recall_hook.recall_index``. ``root`` defaults to the
    root of the memory folder named by the env (``recall_hook.session_root``).
    Raises ``RecallUnavailable`` when the recall hook is not installed, and
    ``recall_hook.RecallError`` when the store is down, unproven or does not
    answer."""
    return _recall_hook().recall_index(query, k, environ, root)
