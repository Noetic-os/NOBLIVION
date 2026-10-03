#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart and PreCompact hook: keep the memory rules across a compaction
and open a session with a short briefing. Work item
WI-8 (levers 6 and 13).

One script, two hook events. It reads ``hook_event_name`` from the hook JSON on
stdin. It is OFF unless ``NOBLIVION_CONTINUITY`` is set. It always exits 0, and on
any error it prints nothing.

PreCompact (matcher ``manual|auto``)
    Reads the session shown-set of the recall hook (``shown_set.json``: the
    memories whose APPLY block, full text or trigger row this session was
    given) and saves the names in this hook's own per-session file
    ``continuity/<session>.json``. It must do so here, because the live
    SessionStart(compact) hook ``recall_hook.py --reset-rows``
    empties the shown-set right after the compaction. Then it prints the rules
    of those memories. Claude Code 2.1.261 appends the stdout of a PreCompact
    hook that exits 0 to the compaction instruction.
    ``NOBLIVION_CONTINUITY_PRECOMPACT_PRINT=0`` keeps the save and drops the print.

SessionStart, source ``compact``
    Prints the rules saved by PreCompact for this session. This is the leg that
    does not depend on what the summary kept.

SessionStart, source ``startup``, ``resume``, ``clear`` (and ``fork``)
    Prints a briefing: one header line and at most 8 DECISION, 5 OPEN and 2
    TRAP rows (standing decisions, open work, live traps), within
    ``BRIEF_MAX_CHARS`` characters (1,500; ``NOBLIVION_CONTINUITY_MAX_CHARS``
    sets it, 500 to 2,000). Once per session id. Each quota is
    settable: ``NOBLIVION_CONTINUITY_QUOTA_DECISION``, ``..._QUOTA_OPEN``,
    ``..._QUOTA_TRAP`` (0 to 20; 0 drops the section). The row limit is the
    sum of the quotas. A row ends with ``[id N]`` (the store id) or, without
    one, with the whole file name less ``.md``. The name is never cut: the
    row text is cut instead, all rows to one length between 80 and 60
    characters, the longest that fits. Rows that still do not fit are left out.
    ``NOBLIVION_CONTINUITY_BRIEFING=0`` turns the briefing off.

The ``status:`` field of a memory (``open``, ``closed`` or ``parked``; read by
``memory_fields.read_fields``, at the top level of the front matter
or under ``metadata:``). A memory whose status is ``closed`` or ``parked`` is
never a briefing row, in any section. An OPEN row comes from a project memory
whose status is ``open``. A project memory with no valid ``status:`` line is
still an OPEN row, as before the field existed, unless
``NOBLIVION_CONTINUITY_REQUIRE_STATUS=1`` is set: then it is no row. A decision
with no status stays a DECISION row in both cases. The ``project:`` field is not
used: no filter, and it is not printed (the slug would cost row text).

How the briefing is ranked. Each of the three sections asks the store's ranked
index (``corpus.recall_index``, which calls the recall hook) one question that names the project
(the last part of the cwd). The store rank and the recency of the local file
are fused (reciprocal rank fusion), so a memory written minutes ago can enter
before the index has carried it to the store. When the store does not answer,
or the recall hook is not installed next to this file, the ranking is recency
alone, and the log says so (``recency_only``).

The local corpus code (memory files, the shown-set, inert text) comes from the
sibling ``corpus.py``, so this hook works without the recall hook.

The curated order. Index files in the memory folder are the first rank of two
sections: the memories that the files of the config key
``continuity.decision_files`` (default ``topic_decisions.md``) link are the
first DECISION rows, and the memories that the files of
``continuity.open_files`` (default ``topic_open_work.md``) link are the first
OPEN rows, each in file order. The fused rank fills what is left of the
section's quota. A memory a decision file links is a DECISION row whatever its name says,
and no memory is a row twice. A linked memory that is closed or parked, that
MEMORY.md links itself, or that has no file is skipped. A missing index file
leaves its section on the fused rank alone. ``NOBLIVION_CONTINUITY_CURATED=0``
turns the curated order off.

How the briefing avoids MEMORY.md. MEMORY.md loads in every session. A memory
file that MEMORY.md links to is never a briefing row. The rows come from the
files that only a ``topic_*.md`` file links, or that nothing links.

Log: one line per call in ``continuity.log`` under ``NOBLIVION_RECALL_CACHE_DIR``
(default ``<data dir>/cache``). It holds no memory text and no query.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
LIVE_MEMORY_DIR = str(_CFG.default_memory_dir())
DEFAULT_CACHE_DIR = str(_CFG.cache_dir())

ON_ENV = "NOBLIVION_CONTINUITY"
BRIEFING_ENV = "NOBLIVION_CONTINUITY_BRIEFING"
PRECOMPACT_PRINT_ENV = "NOBLIVION_CONTINUITY_PRECOMPACT_PRINT"
BUDGET_ENV = "NOBLIVION_CONTINUITY_BUDGET_S"
REQUIRE_STATUS_ENV = "NOBLIVION_CONTINUITY_REQUIRE_STATUS"  # default off
CURATED_ENV = "NOBLIVION_CONTINUITY_CURATED"  # default on
# The index files whose links are the first rows of a section, in file order:
# (section, config key, default files). DECISION is first: a memory that files
# of both sections link is a DECISION row.
CURATED_FILES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("DECISION", "continuity.decision_files", ("topic_decisions.md",)),
    ("OPEN", "continuity.open_files", ("topic_open_work.md",)),
)

STATE_SUBDIR = "continuity"
LOG_NAME = "continuity.log"

# The briefing. One header line plus the rows; the row limit is the sum of the
# quotas (15 by default).
BRIEF_MAX_CHARS = 1500
BRIEF_TEXT_MAX = 80  # the row text: the longest of 80, 75, ... 60 that fits
BRIEF_TEXT_MIN = 60
BRIEF_TEXT_STEP = 5
BRIEF_REF_MAX = 255  # a file name is never cut; this only bounds it
QUOTA: Tuple[Tuple[str, int], ...] = (("DECISION", 8), ("OPEN", 5), ("TRAP", 2))
QUOTA_ENV_PREFIX = "NOBLIVION_CONTINUITY_QUOTA_"  # + DECISION, OPEN or TRAP
QUOTA_MAX = 20
MAX_CHARS_ENV = "NOBLIVION_CONTINUITY_MAX_CHARS"
MAX_CHARS_RANGE = (500, 2000)
BRIEF_MAX_LINES = 1 + sum(n for _, n in QUOTA)  # with the default quotas
BRIEF_HEADER = (
    "Session briefing from NOBLIVION memory (rows that MEMORY.md does not list). "
    "For the full text: noblivion_recall with the id, or read the file in the "
    "memory folder."
)
QUERIES: Mapping[str, str] = {
    "DECISION": "standing decision agreed approved rule for {project}",
    "OPEN": "open work next step in progress pending for {project}",
    "TRAP": "trap hazard rule never repeat this mistake in {project}",
}
INDEX_K = 60
RRF_K = 60
DEFAULT_BUDGET_S = 4.0  # all store calls of one briefing together

# The rules kept across a compaction.
RULES_MAX_ROWS = 12
RULES_MAX_CHARS = 1200
RULE_TEXT_MAX = 150
RULE_NAME_MAX = 40
APPLIED_MAX_NAMES = 60  # the per-session file is bounded
PRECOMPACT_HEADER = (
    "Keep the next lines in the summary, word for word, under the heading "
    '"Memory rules applied in this session". They are the user\'s '
    "memory rules that this session was given and must still follow:"
)
COMPACT_HEADER = (
    "Memory rules this session was given before the compaction (from NOBLIVION "
    "memory). They still apply:"
)

_DECISION_RE = re.compile(r"decision|ruling|approval|approved", re.I)
_LINK_RE = re.compile(r"\]\(\s*([^)#\s]+\.md)\s*\)")
_NAME_DATE_RE = re.compile(r"(20\d\d)[_-](\d\d)[_-](\d\d)")
_SPACE_RE = re.compile(r"\s+")
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


# ── the local corpus helpers (corpus.py), imported and never edited ────────


def _load_recall():
    path = _TOOLS / "corpus.py"
    spec = importlib.util.spec_from_file_location("corpus", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("corpus", mod)
    spec.loader.exec_module(mod)
    return mod


_RH: Any = None


def recall_module():
    global _RH
    if _RH is None:
        _RH = _load_recall()
    return _RH


# ── settings ────────────────────────────────────────────────────────────────


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    return bool(_CFG.switch(name, default, env))


def is_on(env: Mapping[str, str]) -> bool:
    return _flag(env, ON_ENV, False)


def cache_dir(env: Mapping[str, str]) -> str:
    return os.path.expanduser(env.get("NOBLIVION_RECALL_CACHE_DIR") or DEFAULT_CACHE_DIR)


def memory_dir(env: Mapping[str, str]) -> str:
    return os.path.expanduser(
        env.get("NOBLIVION_RECALL_MEMORY_DIR") or env.get("NOBLIVION_MEMORY_DIR") or LIVE_MEMORY_DIR
    )


def _sid(session_id: Any) -> Optional[str]:
    return recall_module()._valid_sid(session_id)


def log_line(
    cache: str, event: str, session_id: Any, rows: int, chars: int, ms: int, status: str
) -> None:
    """Append one line. Never raises. No memory text, no query."""
    sid = (
        session_id
        if isinstance(session_id, str) and _SAFE_RE.sub("", session_id) == session_id and session_id
        else "-"
    )
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} event={event} session={sid[:128]} rows={rows} chars={chars} ms={ms} {status}\n"
    try:
        os.makedirs(cache, mode=0o700, exist_ok=True)
        with open(os.path.join(cache, LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


# ── the per-session file ────────────────────────────────────────────────────


def state_file(cache: str, session_id: Any) -> Optional[str]:
    sid = _sid(session_id)
    if sid is None:
        return None
    return os.path.join(cache, STATE_SUBDIR, f"{sid}.json")


def load_state(cache: str, session_id: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"applied": [], "briefed": False, "compactions": 0}
    path = state_file(cache, session_id)
    if path is None:
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(doc, dict):
        return out
    applied = doc.get("applied")
    if isinstance(applied, list):
        out["applied"] = [x for x in applied if isinstance(x, str) and x][:APPLIED_MAX_NAMES]
    out["briefed"] = doc.get("briefed") is True
    if isinstance(doc.get("compactions"), int):
        out["compactions"] = doc["compactions"]
    return out


def save_state(cache: str, session_id: Any, state: Mapping[str, Any]) -> bool:
    path = state_file(cache, session_id)
    if path is None:
        return False
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "applied": list(state.get("applied") or [])[:APPLIED_MAX_NAMES],
                    "briefed": bool(state.get("briefed")),
                    "compactions": int(state.get("compactions") or 0),
                },
                fh,
            )
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ── text ────────────────────────────────────────────────────────────────────


def _clean(text: str, limit: int) -> str:
    """One inert line, no host path, at most ``limit`` characters."""
    rh = recall_module()
    out = _SPACE_RE.sub(" ", rh.strip_host_paths(rh._neutral(text or ""))).strip()
    if len(out) <= limit:
        return out
    cut = out[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _corpus(env: Mapping[str, str]) -> List[Any]:
    folder = memory_dir(env)
    if not os.path.isdir(folder):
        return []
    return recall_module().load_memory_corpus(folder)


# ── the rules kept across a compaction ──────────────────────────────────────


def applied_names(cache: str, session_id: Any) -> List[str]:
    """The memories this session was given with their text: fetched first, then
    the APPLY blocks, then the trigger rows. Read from the recall hook's
    shown-set; ``[]`` when that file belongs to another session."""
    rh = recall_module()
    shown = rh.load_shown_set(cache, session_id)
    out: List[str] = []
    for kind in ("fetched", "apply", "trigger"):
        for name in shown.get(kind) or ():
            if name not in out:
                out.append(name)
    return out


def rules_block(names: Sequence[str], corpus: Sequence[Any], header: str) -> Tuple[str, int]:
    """``(text, rows)``. One line per memory that has a local file with a
    ``rule:`` line (or, without one, a description). ``("", 0)`` for no row."""
    by_name = recall_module().corpus_by_name(corpus)
    lines: List[str] = []
    size = len(header)
    for name in names:
        md = by_name.get(name)
        if md is None or md.dropped:
            continue
        text = _clean(md.rule or md.description, RULE_TEXT_MAX)
        if not text:
            continue
        line = f"- {text} ({_clean(md.name, RULE_NAME_MAX)})"
        if len(lines) >= RULES_MAX_ROWS or size + 1 + len(line) > RULES_MAX_CHARS:
            break
        lines.append(line)
        size += 1 + len(line)
    if not lines:
        return "", 0
    return "\n".join([header] + lines), len(lines)


def pre_compact(event: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[str, int, str]:
    """Save the session's applied memories; return ``(text, rows, status)``."""
    cache = cache_dir(env)
    sid = event.get("session_id")
    state = load_state(cache, sid)
    # The newest compaction's names first; the earlier ones stay behind them.
    merged = applied_names(cache, sid)
    for name in state["applied"]:
        if name not in merged:
            merged.append(name)
    state["applied"] = merged[:APPLIED_MAX_NAMES]
    state["compactions"] = int(state["compactions"]) + 1
    saved = save_state(cache, sid, state)
    status = "saved" if saved else "not_saved"
    if not merged:
        return "", 0, status + "_empty"
    if not _flag(env, PRECOMPACT_PRINT_ENV, True):
        return "", 0, status + "_print_off"
    text, rows = rules_block(merged, _corpus(env), PRECOMPACT_HEADER)
    return text, rows, status


def after_compact(event: Mapping[str, Any], env: Mapping[str, str]) -> Tuple[str, int, str]:
    cache = cache_dir(env)
    state = load_state(cache, event.get("session_id"))
    if not state["applied"]:
        return "", 0, "no_saved_rules"
    text, rows = rules_block(state["applied"], _corpus(env), COMPACT_HEADER)
    return text, rows, "ok" if rows else "no_rule_text"


# ── the briefing ────────────────────────────────────────────────────────────


def linked_files(folder: str, base: str) -> List[str]:
    """The file names the index file ``base`` links to, in file order, each
    once. ``[]`` when the file is missing."""
    try:
        with open(os.path.join(folder, base), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    out: List[str] = []
    for m in _LINK_RE.finditer(text):
        name = os.path.basename(m.group(1))
        if name not in out:
            out.append(name)
    return out


def memory_md_links(folder: str) -> set:
    """The file names MEMORY.md links to. Those memories load in every session,
    so the briefing never repeats them."""
    return set(linked_files(folder, "MEMORY.md"))


def curated_files(key: str, defaults: Tuple[str, ...], env: Mapping[str, str]) -> Tuple[str, ...]:
    """The index file names of one curated section: the config list ``key``,
    else ``defaults``. A name with a folder part is ignored."""
    value = _CFG.get(key, None, env)
    if not isinstance(value, list):
        return defaults
    return tuple(
        n.strip()
        for n in value
        if isinstance(n, str) and n.strip() and os.path.basename(n.strip()) == n.strip()
    )


def curated_order(folder: str, env: Mapping[str, str]) -> Dict[str, List[str]]:
    """``section -> file names``: the links of the section's index file, in
    file order. A file two index files link is in the first section only.
    Empty lists when ``NOBLIVION_CONTINUITY_CURATED`` is off."""
    out: Dict[str, List[str]] = {label: [] for label, _, _ in CURATED_FILES}
    if not _flag(env, CURATED_ENV, True):
        return out
    seen: set = set()
    for label, key, defaults in CURATED_FILES:
        for base in curated_files(key, defaults, env):
            for name in linked_files(folder, base):
                if name not in seen:
                    seen.add(name)
                    out[label].append(name)
    return out


_MF: Any = None
STATUS_NOT_A_ROW = ("closed", "parked")


def fields_module():
    """``memory_fields`` from this script's folder, or None."""
    global _MF
    if _MF is None:
        path = _TOOLS / "memory_fields.py"
        spec = importlib.util.spec_from_file_location("memory_fields", path)
        if spec is None or spec.loader is None or not path.is_file():
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MF = mod
    return _MF


def status_and_rule(folder: str, md: Any) -> Tuple[str, str]:
    """``(status, rule)`` from the file's front matter. The status is ``open``,
    ``closed`` or ``parked``, or ``""`` when the file has no ``status:`` line
    or an unknown value. The rule is the ``rule:`` field, also when it sits
    under ``metadata:``. ``("", "")`` when the file cannot be read (so a fault
    keeps the behaviour from before the fields existed)."""
    try:
        mf = fields_module()
        if mf is None:
            return "", ""
        with open(os.path.join(folder, md.rel_path), encoding="utf-8", errors="replace") as fh:
            fields = mf.read_fields(fh.read())
        status = str(fields.get("status") or "")
        return (status if status in mf.STATUSES else ""), str(fields.get("rule") or "")
    except Exception:  # noqa: BLE001 - a hook fails open
        return "", ""


def status_of(folder: str, md: Any) -> str:
    return status_and_rule(folder, md)[0]


def section_of(md: Any, status: str = "", require_status: bool = False) -> Optional[str]:
    """DECISION, OPEN or TRAP, or None for a file the briefing does not use.
    ``status`` is the file's ``status:`` field, ``""`` for none."""
    if md.dropped or md.kind not in ("project", "feedback"):
        return None
    if status in STATUS_NOT_A_ROW:
        return None
    head = f"{md.rel_path} {md.name} {md.description[:160]}"
    if _DECISION_RE.search(head):
        return "DECISION"
    if md.kind != "project":
        return "TRAP"
    if status != "open" and require_status:
        return None
    return "OPEN"


def recency_key(folder: str, md: Any) -> float:
    """Seconds since the epoch: the date in the file name when it has one (a
    decision is named by its day), else the file's mtime."""
    m = _NAME_DATE_RE.search(md.rel_path)
    if m:
        try:
            return _dt.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=_dt.timezone.utc
            ).timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(os.path.join(folder, md.rel_path))
    except OSError:
        return 0.0


def quotas(env: Mapping[str, str]) -> Tuple[Tuple[str, int], ...]:
    """``((section, rows), ...)``: the default quota of each section, or the
    value of ``NOBLIVION_CONTINUITY_QUOTA_<SECTION>`` when that is a whole number
    from 0 to ``QUOTA_MAX``. Any other value keeps the default."""
    out = []
    for label, default in QUOTA:
        raw = (env.get(QUOTA_ENV_PREFIX + label) or "").strip()
        n = int(raw) if raw.isdigit() and len(raw) <= 3 else default
        out.append((label, n if n <= QUOTA_MAX else default))
    return tuple(out)


def max_chars(env: Mapping[str, str]) -> int:
    """``BRIEF_MAX_CHARS``, or ``NOBLIVION_CONTINUITY_MAX_CHARS`` when that is a
    whole number from 500 to 2,000."""
    raw = (env.get(MAX_CHARS_ENV) or "").strip()
    if raw.isdigit() and len(raw) <= 5 and MAX_CHARS_RANGE[0] <= int(raw) <= MAX_CHARS_RANGE[1]:
        return int(raw)
    return BRIEF_MAX_CHARS


def reference(rel: str, mid: Optional[int]) -> str:
    """What a row ends with: the daemon id, or the whole file name less ``.md``."""
    if mid is not None:
        return f"id {mid}"
    return _clean(rel[:-3] if rel.endswith(".md") else rel, BRIEF_REF_MAX)


def fit_rows(picked: Sequence[Tuple[str, str, str]], max_chars: int) -> List[str]:
    """The row lines for ``picked`` (``(section, text, reference)`` in print
    order). Every text is cut to one length: the longest of ``BRIEF_TEXT_MAX``
    down to ``BRIEF_TEXT_MIN`` with which the header and all rows stay within
    ``max_chars``. When the shortest does not fit either, a section stops at
    its first row that does not fit."""
    limit = BRIEF_TEXT_MIN
    for cand in range(BRIEF_TEXT_MAX, BRIEF_TEXT_MIN - 1, -BRIEF_TEXT_STEP):
        total = len(BRIEF_HEADER) + sum(
            1 + len(f"- {label}: {_clean(text, cand)} [{ref}]") for label, text, ref in picked
        )
        if total <= max_chars:
            limit = cand
            break
    lines: List[str] = []
    size = len(BRIEF_HEADER)
    full: set = set()
    for label, text, ref in picked:
        if label in full:
            continue
        line = f"- {label}: {_clean(text, limit)} [{ref}]"
        if size + 1 + len(line) > max_chars:
            full.add(label)
            continue
        lines.append(line)
        size += 1 + len(line)
    return lines


def fuse(recent: Sequence[str], daemon: Sequence[str]) -> List[str]:
    """Reciprocal rank fusion of two orders of file names. A name in one list
    only keeps that list's term. Ties keep the recency order."""
    score: Dict[str, float] = {}
    for order in (recent, daemon):
        for rank, name in enumerate(order, start=1):
            score[name] = score.get(name, 0.0) + 1.0 / (RRF_K + rank)
    pos = {name: i for i, name in enumerate(recent)}
    return sorted(score, key=lambda n: (-score[n], pos.get(n, len(pos))))


def project_label(event: Mapping[str, Any]) -> str:
    cwd = event.get("cwd")
    base = os.path.basename(str(cwd).rstrip("/")) if isinstance(cwd, str) and cwd else ""
    return _SAFE_RE.sub(" ", base)[:60].strip() or "this project"


def briefing(
    event: Mapping[str, Any],
    env: Mapping[str, str],
    index: Optional[Callable[[str, int, Mapping[str, str]], Sequence[Any]]] = None,
) -> Tuple[str, int, str]:
    """``(text, rows, status)``. ``index`` is the daemon call; the tests pass a fake."""
    rh = recall_module()
    folder = memory_dir(env)
    corpus = _corpus(env)
    if not corpus:
        return "", 0, "no_corpus"
    linked = memory_md_links(folder)
    by_name = rh.corpus_by_name(corpus)
    quota_of = quotas(env)
    sections: Dict[str, List[Any]] = {label: [] for label, _ in quota_of}
    require_status = _flag(env, REQUIRE_STATUS_ENV, False)
    curated = curated_order(folder, env)
    curated_label = {rel: label for label, rels in curated.items() for rel in rels}
    rules: Dict[str, str] = {}
    for md in corpus:
        if md.rel_path in linked:
            continue
        label = curated_label.get(md.rel_path)
        if label:  # the index file decides the section
            if md.dropped:
                continue
            status, rule = status_and_rule(folder, md)
            if status in STATUS_NOT_A_ROW:
                continue
        else:
            if section_of(md) is None:
                continue
            status, rule = status_and_rule(folder, md)
            label = section_of(md, status, require_status)
        if label:
            sections[label].append(md)
            rules[md.rel_path] = rule
    call = index if index is not None else rh.recall_index
    try:
        budget = float(env.get(BUDGET_ENV) or DEFAULT_BUDGET_S)
    except ValueError:
        budget = DEFAULT_BUDGET_S
    deadline = time.monotonic() + budget
    project = project_label(event)
    daemon_ok = True
    used_daemon = 0
    picked: List[Tuple[str, str, str]] = []
    asked = 0
    for label, quota in quota_of:
        files = sections[label]
        if not files or quota <= 0:
            continue
        asked += 1
        recent = [
            md.rel_path
            for md in sorted(files, key=lambda md: (-recency_key(folder, md), md.rel_path))
        ]
        ids: Dict[str, int] = {}
        daemon_order: List[str] = []
        if daemon_ok and time.monotonic() < deadline:
            try:
                in_section = set(recent)
                for ln in call(QUERIES[label].format(project=project), INDEX_K, env):
                    md = by_name.get(ln.title)
                    if md is None or md.rel_path not in in_section or md.rel_path in ids:
                        continue
                    ids[md.rel_path] = ln.mid
                    daemon_order.append(md.rel_path)
                used_daemon += 1
            except Exception:  # noqa: BLE001 - the daemon is optional here
                daemon_ok = False
        by_path = {md.rel_path: md for md in files}
        taken = 0
        first = [rel for rel in curated.get(label, ()) if rel in by_path]
        for rel in first + [rel for rel in fuse(recent, daemon_order) if rel not in first]:
            if taken >= quota:
                break
            md = by_path[rel]
            text = md.rule or rules.get(rel) or md.description or md.name
            if not _clean(text, BRIEF_TEXT_MIN):
                continue
            picked.append((label, text, reference(rel, ids.get(rel))))
            taken += 1
    lines = fit_rows(picked, max_chars(env))
    rows = len(lines)
    if rows == 0:
        return "", 0, "no_rows"
    status = (
        "ok" if used_daemon == asked else ("recency_only" if used_daemon == 0 else "store_partial")
    )
    return "\n".join([BRIEF_HEADER] + lines), rows, status


def session_start(
    event: Mapping[str, Any],
    env: Mapping[str, str],
    index: Optional[Callable[..., Sequence[Any]]] = None,
) -> Tuple[str, int, str]:
    if event.get("source") == "compact":
        return after_compact(event, env)
    if not _flag(env, BRIEFING_ENV, True):
        return "", 0, "briefing_off"
    cache = cache_dir(env)
    sid = event.get("session_id")
    state = load_state(cache, sid)
    if state["briefed"]:
        return "", 0, "already_briefed"
    text, rows, status = briefing(event, env, index)
    if rows:
        state["briefed"] = True
        save_state(cache, sid, state)
    return text, rows, status


# ── entry ───────────────────────────────────────────────────────────────────


def run(
    stdin_text: str, env: Mapping[str, str], index: Optional[Callable[..., Sequence[Any]]] = None
) -> str:
    """The text to print for one hook event, ``""`` for none."""
    if not is_on(env):
        return ""
    event = json.loads(stdin_text or "{}")
    if not isinstance(event, dict):
        return ""
    name = event.get("hook_event_name")
    t0 = time.monotonic()
    if name == "PreCompact":
        text, rows, status = pre_compact(event, env)
        tag = "PreCompact"
    elif name == "SessionStart":
        text, rows, status = session_start(event, env, index)
        tag = "SessionStart." + _SAFE_RE.sub("", str(event.get("source") or "-"))[:16]
    else:
        return ""
    log_line(
        cache_dir(env),
        tag,
        event.get("session_id"),
        rows,
        len(text),
        int((time.monotonic() - t0) * 1000),
        status,
    )
    return text


def main(stdin=None, stdout=None, environ: Optional[Mapping[str, str]] = None) -> int:
    try:
        env = dict(os.environ if environ is None else environ)
        text = run((stdin or sys.stdin).read(), env)
        if text:
            out = stdout or sys.stdout
            out.write(text + "\n")
            out.flush()
    except Exception:  # noqa: BLE001, S110 - a hook fails open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
