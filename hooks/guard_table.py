#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compile the memory rule fields into a local guard table, and match commands.

Work item WI-2 ("guard table compiler").

``rebuild(folder)`` reads every memory file in ``folder`` ONLY through
``memory_fields.read_fields`` and writes one JSON table, atomically,
to ``table_path(folder)`` (env ``NOBLIVION_GUARD_TABLE``, default
``<data dir>/guard-tables/<slug of the memory folder>.json``, one table per
project). NOBLIVION-31: ``folder`` is the project memory folder of the
session's ``cwd`` (``hook_config.resolve_memory_dir``), and also the global
folder (``NOBLIVION_GLOBAL_MEMORY_DIR``) when it is set. A file of the project
folder wins over a file with the same name in the global folder. Each entry carries the memory id (the file
stem), rule, apply, scope, triggers (for the later rows-only leg) and the
``violates`` regex. A ``violates`` regex is kept only when
``memory_fields.check_fields`` reports no problem for that file; a
refused one goes to ``skipped`` with the problems in plain words, and never
stops the rebuild. A skipped rule does not block, so the SessionStart rebuild
names the skipped rules in one line for the user (``skipped_line``,
NOBLIVION-50).

The key ``stamp`` (NOBLIVION-50) is ``folder_stamp`` of the folders at the
build: a hash over the name, the size and the change times of each memory
file. ``stale(folder)`` compares it with the folders now, so a reader sees a
file that a shell command deleted, edited, moved or added (``rm``, ``sed``,
``mv``, ``git checkout``) with one ``scandir``, and reads no file.

WI-3d: an entry also carries ``complies`` (the optional ``complies:`` regex, a
run of the rule's own command) and ``run_first`` (the shortest piece of
``example_ok`` that ``complies`` matches: a ``$( )`` substitution, a shell
segment, or the whole example; the guard hook prints it as the command to run
before an override). A ``complies`` with problems is dropped alone (the
``violates`` stays) and listed in ``skipped``. ``complies_match(command)`` is
what the hook calls on a failed Bash call.

``match(command)`` is what the WI-3 PreToolUse hook calls. It compiles each
``violates`` regex once and applies it to the raw Bash command AND to each shell
segment of it (split on ``&&``, ``||``, ``;``, ``|``, ``&``, bare parentheses and
newlines, outside quotes; heredoc bodies are data and are dropped from the
segments, and the whole-command test skips them too, except a body or a
here-string that a shell reads, which is matched as a command line of its
own, NOBLIVION-52). So
``^git\\s+stash\\s+pop\\s*$`` also fires on
``cd x && git stash pop``. A segment is also tried with its leading ``VAR=value``
assignments removed. Each hit records whether a segment or only the whole
command matched.

Table version 2: the key ``label_index`` holds the subject
label index over EVERY memory file, not only the files with rule fields
(``memory_labels.build_index``: labels, the number of memories per
label, and per memory its stem, name, mtime and a short text). The guard hook
and the recall hook match it (``label_rows``). Only a key is
added: a reader of version 1 reads ``entries`` as before. When the labeller
cannot be loaded or fails, the table has no ``label_index`` (no label rows)
and ``label_error`` says why; the guard entries are built all the same.

Callers:
  * the WI-1 PostToolUse hook ``memory_fields_hook.py`` calls
    ``rebuild(source_dirs(cwd))`` after a memory write;
  * the guard hook calls ``rebuild`` before it reads a table that is
    ``stale`` (or missing);
  * SessionStart runs ``guard_table.py --rebuild`` with the hook JSON on
    stdin (its ``cwd`` names the project): fail-open, exit 0 always, silent
    when no rule was skipped.

No daemon call, no network. Standard library only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from re import Pattern
from typing import Any, Dict, List, Optional, Tuple

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
# The table of a session with no project folder (no cwd and no override).
DEFAULT_TABLE = _CFG.data_dir() / "guard-table.json"
TABLES_SUBDIR = "guard-tables"
TABLE_VERSION = 2  # 2: the label index key
# Index and topic files are not memories.
_NOT_MEMORY = re.compile(r"^(MEMORY|MEMORY_ARCHIVE|topic_.*)\.md$")


def _load(name: str):
    path = _TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MF = None
_ML = None


def _mf():
    global _MF
    if _MF is None:
        _MF = _load("memory_fields")
    return _MF


def _ml():
    global _ML
    if _ML is None:
        _ML = _load("memory_labels")
    return _ML


def _label_doc(path: Path, text: str) -> Dict[str, object]:
    """The label index row of one memory file."""
    ml = _ml()
    name, desc, body = ml.name_and_description(text, path.stem)
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    return {
        "id": path.stem,
        "name": name,
        "labels": ml.memory_labels(text, path.stem),
        "mtime": mtime,
        "text": ml.short_text(desc, body),
    }


def _cwd_of(cwd: Optional[str]) -> Optional[str]:
    if isinstance(cwd, str) and os.path.isabs(cwd):
        return cwd
    try:
        return os.getcwd()
    except OSError:
        return None


def memory_dir(
    cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[Path]:
    """The project memory folder of a session at ``cwd`` (default the process
    working dir): ``NOBLIVION_MEMORY_DIR``, else the folder Claude Code uses
    (``hook_config.resolve_memory_dir``). None when there is none."""
    return _CFG.resolve_memory_dir(_cwd_of(cwd), env)


def source_dirs(cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """The folders the table of a session at ``cwd`` is built from: the
    project folder, then the global folder when it is set."""
    return _CFG.memory_dirs(_cwd_of(cwd), env)


def table_path(folder: Optional[Path] = None, env: Optional[Mapping[str, str]] = None) -> Path:
    """``NOBLIVION_GUARD_TABLE``, else the table of the project memory folder
    ``folder`` (default ``memory_dir()``):
    ``<data dir>/guard-tables/<slug of the folder>.json``. Each project has its
    own table, so two sessions in two projects never overwrite each other."""
    e = os.environ if env is None else env
    raw = e.get("NOBLIVION_GUARD_TABLE")
    if raw:
        return Path(raw).expanduser()
    if folder is None:
        folder = memory_dir(None, env)
    if folder is None:
        return DEFAULT_TABLE
    return _CFG.data_dir(env) / TABLES_SUBDIR / (_CFG.project_slug(str(folder)) + ".json")


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------
def _folders(folder: Any) -> List[Path]:
    if isinstance(folder, (list, tuple)):
        return [Path(f) for f in folder]
    return [Path(folder)]


def folder_stamp(folder: Any) -> str:
    """A hash over what ``build`` reads from ``folder`` (one folder or a
    list): each folder path and, per memory file, its name, size, mtime and
    ctime. A write, a delete, a rename or a ``cp -p`` of a memory file changes
    it; an index or topic file does not. One ``scandir`` and one ``stat`` per
    file; no file is read."""
    h = hashlib.sha256()
    for f in _folders(folder):
        rows: Optional[List[Tuple[str, int, int, int]]] = []
        try:
            with os.scandir(f) as it:
                for entry in it:
                    if not entry.name.endswith(".md") or _NOT_MEMORY.match(entry.name):
                        continue
                    try:
                        st = entry.stat()
                        rows.append((entry.name, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
                    except OSError:
                        rows.append((entry.name, -1, 0, 0))
        except OSError:
            rows = None  # no folder
        h.update(repr((str(f), sorted(rows) if rows is not None else None)).encode("utf-8"))
    return h.hexdigest()[:32]


def stale(folder: Any, path: Optional[Path] = None) -> bool:
    """True when the table at ``path`` (default the table of the first
    folder) was not built from ``folder`` as it is now: a memory file changed
    since the build, the table is missing or broken, or it has no ``stamp``
    (a table of an older version)."""
    folders = _folders(folder)
    p = Path(path) if path is not None else table_path(folders[0])
    return load_table(p).get("stamp") != folder_stamp(folders)


def build(folder: Any) -> Dict[str, Any]:
    """The table for ``folder`` (one folder, or a list: the project folder
    first, then the global folder), not written. On a file name that two
    folders hold, the first folder wins. Raises when no folder is a
    directory, so a missing folder never replaces a good table."""
    folders = _folders(folder)
    present = [f for f in folders if f.is_dir()]
    if not present:
        raise FileNotFoundError(f"memory folder {folders[0] if folders else ''} does not exist")
    # Before the files are read: a write during the build gives a table that
    # is stale, never a new stamp on old text.
    stamp = folder_stamp(folders)
    mf = _mf()
    entries: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    label_docs: List[Dict[str, object]] = []
    label_error = ""
    for path in _CFG.memory_files(present):
        if _NOT_MEMORY.match(path.name):
            continue
        mem_id = path.stem
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append({"id": mem_id, "violates": "", "problems": [f"unreadable: {exc}"]})
            continue
        if not label_error:
            try:  # every memory file, rule fields or not
                label_docs.append(_label_doc(path, text))
            except Exception as exc:  # noqa: BLE001 - labels never stop the guard build
                label_error = f"{type(exc).__name__}: {exc}"[:300]
        fields = mf.read_fields(text)
        pat = str(fields.get("violates") or "")
        triggers = [
            t
            for t in (fields.get("triggers") or [])
            if isinstance(t, str) and not mf.trigger_problem(t)
        ]
        if not (fields.get("rule") or pat or triggers):
            continue
        kind = "feedback" if path.name.startswith("feedback_") else "other"
        violates, complies, run_first = "", "", ""
        cpat = str(fields.get("complies") or "")
        if pat:
            try:
                problems = mf.check_fields(fields, kind)
                # A bad status: or project: line is a briefing problem. It must
                # never switch off a guard regex.
                sp = getattr(mf, "status_project_problems", None)
                other = sp(fields, kind) if sp else []
                problems = [p for p in problems if p not in other]
                cprobs = mf.complies_problems(fields) if cpat else []
            except Exception as exc:  # noqa: BLE001 - one file never stops the build
                problems, cprobs = [f"check failed: {exc}"], []
            vprobs = [p for p in problems if p not in cprobs]
            if vprobs:
                skipped.append({"id": mem_id, "violates": pat, "problems": vprobs})
            else:
                violates = pat
                if cprobs:  # a bad complies is dropped alone
                    skipped.append({"id": mem_id, "complies": cpat, "problems": cprobs})
                elif cpat:
                    complies = cpat
                    run_first = first_command(str(fields.get("example_ok") or ""), cpat, pat)
                    if not run_first:
                        complies = ""
                        skipped.append(
                            {
                                "id": mem_id,
                                "complies": cpat,
                                "problems": ["no piece of example_ok is a run of complies"],
                            }
                        )
        entries.append(
            {
                "id": mem_id,
                "rule": str(fields.get("rule") or ""),
                "apply": str(fields.get("apply") or ""),
                "scope": str(fields.get("scope") or ""),
                "triggers": triggers,
                "violates": violates,
                "complies": complies,
                "run_first": run_first,
            }
        )
    table: Dict[str, Any] = {
        "version": TABLE_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(folders[0]),
        "sources": [str(f) for f in present],
        "stamp": stamp,
        "entries": entries,
        "skipped": skipped,
    }
    if not label_error:
        try:
            table["label_index"] = _ml().build_index(label_docs)
        except Exception as exc:  # noqa: BLE001 - labels never stop the guard build
            label_error = f"{type(exc).__name__}: {exc}"[:300]
    if label_error:
        table["label_error"] = label_error
    return table


def write_atomic(path: Path, data: Dict[str, object]) -> None:
    """Write ``data`` as JSON to ``path`` through a temp file in the same folder
    and ``os.replace``: a reader sees the old table or the new one, never half."""
    path = Path(path)
    _make_dirs(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


PROBES_SUFFIX = ".probes"


def probes_path(table: Path) -> Path:
    """The file next to the table at ``table`` that keeps the regex probe
    times of its builds: ``{hash of the probe input: seconds}``."""
    return table.with_name(table.name + PROBES_SUFFIX)


def _read_probes(path: Path) -> Dict[str, float]:
    """The probe times in ``path``; none when the file is missing or broken."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if type(v) in (int, float)}


def _write_probes(table: Path, probes: Dict[str, float], old: Dict[str, float]) -> None:
    """Best effort: without the file the next build is only slower."""
    if probes == old:
        return
    try:
        write_atomic(probes_path(table), probes)
    except OSError:
        pass


def rebuild(folder: Any = None, out: Optional[Path] = None) -> Dict[str, Any]:
    """Build the table for ``folder`` (one folder or a list, default
    ``source_dirs()``) and write it to ``out`` (default the table of the
    first folder, ``table_path``). Returns the table.

    The build times every ``violates`` and ``complies`` regex in a child
    process (``memory_fields.regex_too_slow``, about 10 ms each). The time of
    a probe that passed is kept in ``probes_path(out)``, so the next build
    times only a new or changed regex. A build that is cut off (the time
    limit of the guard hook) keeps the times it measured: the next build goes
    on from there."""
    folders = _folders(folder) if folder is not None else source_dirs()
    if not folders:
        raise FileNotFoundError("no memory folder")
    path = Path(out) if out is not None else table_path(folders[0])
    mf = _mf()
    old = _read_probes(probes_path(path))
    new: Dict[str, float] = {}
    mf.PROBE_TIMES = (old, new)
    try:
        table = build(folders)
    except BaseException:
        _write_probes(path, {**old, **new}, old)
        raise
    finally:
        mf.PROBE_TIMES = None
    write_atomic(path, table)
    _write_probes(path, new, old)
    return table


SKIPPED_NAMED = 3  # rules that ``skipped_line`` names per kind
SKIPPED_ID_CHARS = 60
SKIPPED_WHY_CHARS = 100


def _one_line(text: object, n: int) -> str:
    out = " ".join(str(text or "").split())
    return out if len(out) <= n else out[: n - 3] + "..."


def skipped_line(table: Mapping[str, Any]) -> str:
    """One line for the user about the rules that ``build`` skipped, ``""``
    when it skipped none. A skipped ``violates`` (or an unreadable file) is a
    rule that does not block; a skipped ``complies`` leaves the deny in
    place. It names at most ``SKIPPED_NAMED`` rules per kind, each with its
    first problem; ``--report`` prints them all."""
    rows = [r for r in table.get("skipped") or [] if isinstance(r, dict)]
    parts: List[str] = []
    for kind, one, many in (
        ("violates", "rule is skipped and does not block", "rules are skipped and do not block"),
        (
            "complies",
            "complies field is skipped (the rule still blocks)",
            "complies fields are skipped (the rules still block)",
        ),
    ):
        group = [r for r in rows if ("complies" in r) == (kind == "complies")]
        if not group:
            continue
        named = []
        for r in group[:SKIPPED_NAMED]:
            probs = [p for p in r.get("problems") or [] if p]
            why = _one_line(probs[0] if probs else "no reason", SKIPPED_WHY_CHARS)
            if len(probs) > 1:
                why += f"; {len(probs) - 1} more problem" + ("s" if len(probs) > 2 else "")
            named.append(f"{_one_line(r.get('id'), SKIPPED_ID_CHARS)} ({why})")
        if len(group) > SKIPPED_NAMED:
            named.append(f"and {len(group) - SKIPPED_NAMED} more")
        parts.append(f"{len(group)} {one if len(group) == 1 else many}: " + "; ".join(named))
    if not parts:
        return ""
    return (
        "NOBLIVION: "
        + ". ".join(parts)
        + ". Fix the memory file. For the full list, run guard_table.py --rebuild --report."
    )


# --------------------------------------------------------------------------
# shell segments
# --------------------------------------------------------------------------
# ``<<WORD``, ``<<-WORD``, ``<<'WORD'``, ``<<"WORD"``, ``<<\WORD``. A quoted
# terminator is any text (``'END-DOC'``, ``"END OF DOC"``). A bare one starts
# with a letter or ``_`` (so ``1<<2`` is not a heredoc) and runs to the next
# blank, quote or shell operator (``END-DOC``, ``EOF.1``).
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:(['\"])([^'\"\n]+)\2|\\?([A-Za-z_][^\s'\"<>;&|()]*))")


def _heredoc_word(m: re.Match[str]) -> str:
    return m.group(3) if m.group(3) is not None else m.group(4)


def segments(command: str) -> List[str]:
    """The top-level simple commands of ``command``: split on ``&&``, ``||``,
    ``;``, ``|``, ``&``, bare ``(`` / ``)`` and newlines outside quotes and
    outside ``$( )``. Heredoc bodies are dropped. Stripped, empty ones left out."""
    return _lex(command)[0]


def without_heredoc_bodies(command: str) -> str:
    """``command`` with every heredoc body (and its terminator line) removed.
    A body is data (a file, a script, a commit message), not a command, unless
    a shell reads it: ``command_views`` then matches it as a command line of
    its own (NOBLIVION-52). A backslash-newline outside single quotes and
    heredoc bodies is removed too: the shell joins those lines."""
    return _lex(command)[1]


def _lex(command: str) -> Tuple[List[str], str, Dict[int, str]]:
    """``(segments, text, bodies)``: the segments, the text without heredoc
    bodies and line continuations, and each heredoc body keyed by the index of
    its ``<<`` operator in that text (for ``<<-`` with the leading tabs
    removed)."""
    out: List[str] = []
    cut: List[Tuple[int, int]] = []  # heredoc body ranges and line continuations
    cur: List[str] = []
    stack: List[str] = []  # "'", '"', "(" for $( ), <( ), >( )
    pending: List[Tuple[str, bool, int]] = []  # heredoc terminators waiting for a newline
    bodies: List[Tuple[int, str]] = []  # (index of the ``<<`` in ``command``, body)
    i, n = 0, len(command)

    def flush() -> None:
        s = "".join(cur).strip()
        if s:
            out.append(s)
        cur.clear()

    while i < n:
        c = command[i]
        top = stack[-1] if stack else ""
        if top == "'":
            cur.append(c)
            if c == "'":
                stack.pop()
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] == "\n":  # a line continuation: the shell joins the lines
                cut.append((i, i + 2))
                i += 2
                continue
            cur.append(command[i : i + 2])
            i += 2
            continue
        if top == '"':
            cur.append(c)
            if c == '"':
                stack.pop()
            elif c == "$" and command.startswith("$(", i):
                stack.append("(")
                cur.append("(")
                i += 1
            i += 1
            continue
        # top level or inside $( )
        if c == "#" and (i == 0 or command[i - 1] in " \t\n;|&()"):
            # a comment runs to the line end: a quote character in it opens no
            # quote, and its text is no part of a segment (as ``_Scan.cmd``)
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "'\"":
            stack.append(c)
            cur.append(c)
            i += 1
            continue
        if c == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC.match(command, i)
            if m:
                pending.append((_heredoc_word(m), m.group(1) == "-", i))
                cur.append(m.group(0))
                i = m.end()
                continue
        if c == "\n" and pending:
            i += 1
            start = i
            for word, dash, op in pending:  # skip each body up to its terminator
                b0, b1 = i, n
                while i <= n:
                    j = command.find("\n", i)
                    line = command[i:] if j < 0 else command[i:j]
                    a, i = i, (n if j < 0 else j + 1)
                    if (line.lstrip("\t") if dash else line) == word:
                        b1 = a
                        break
                    if j < 0:
                        break
                body = command[b0:b1]
                if dash:
                    body = "\n".join(x.lstrip("\t") for x in body.split("\n"))
                bodies.append((op, body))
            pending.clear()
            cut.append((start, i))
            if not stack:
                flush()
            else:
                cur.append("\n")
            continue
        if c == "(" and i > 0 and command[i - 1] in "$<>":
            stack.append("(")
            cur.append(c)
            i += 1
            continue
        if top == "(":
            cur.append(c)
            if c == "(":
                stack.append("(")
            elif c == ")":
                stack.pop()
            i += 1
            continue
        # top level: separators
        two = command[i : i + 2]
        if two in ("&&", "||"):
            flush()
            i += 2
            continue
        if c in ";|\n()":
            flush()
            i += 1
            continue
        if c == "&":
            prev = command[i - 1] if i > 0 else ""
            nxt = command[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":  # 2>&1, &>file, <&3
                cur.append(c)
            else:
                flush()
            i += 1
            continue
        cur.append(c)
        i += 1
    flush()
    kept, last = [], 0
    for a, b in cut:
        kept.append(command[last:a])
        last = b
    kept.append(command[last:])
    at: Dict[int, str] = {}  # the ``<<`` index moves left by the cuts before it
    k, shift = 0, 0
    for op, body in bodies:
        while k < len(cut) and cut[k][1] <= op:
            shift += cut[k][1] - cut[k][0]
            k += 1
        at[op - shift] = body
    return out, "".join(kept), at


_ASSIGN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+)+")


# One shell word: quoted parts, escapes and plain characters, glued together.
_ARG = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s'"\\])+"""
# Leading wrappers that run the next word as the command.
_WRAPPER = re.compile(
    r"^(?:"
    r"[A-Za-z_][A-Za-z0-9_]*=" + _ARG + r"?"
    r"|env(?:\s+(?:-C\s*"
    + _ARG
    + r"|--chdir="
    + _ARG
    + r"|-u\s*"
    + _ARG
    + r"|--unset="
    + _ARG
    + r"|-i|--ignore-environment|-0|--null|--"
    r"|[A-Za-z_][A-Za-z0-9_]*=" + _ARG + r"?))*"
    r"|sudo(?:\s+(?:-[ugCDprtUh]\s*" + _ARG + r"|--[a-z-]+=" + _ARG + r"|-[A-Za-z]+|--))*"
    r"|nohup"
    r"|command(?:\s+-p)?"
    r"|exec(?:\s+(?:-[cl]+|-a\s*" + _ARG + r"))*"
    r"|time(?:\s+-p)?"
    r"|nice(?:\s+(?:-n\s*" + _ARG + r"|--adjustment=" + _ARG + r"|-\d+))*"
    r"|if|then|else|elif|while|until|do|\{|!"
    r"|setsid(?:\s+(?:-[cfw]+|--ctty|--fork|--wait))*"
    r"|timeout(?:\s+(?:-[sk]\s*"
    + _ARG
    + r"|--signal="
    + _ARG
    + r"|--kill-after="
    + _ARG
    + r"|--preserve-status|--foreground|-v|--verbose))*\s+\d+(?:\.\d+)?[smhd]?"
    r")\s+"
)
# git global options between ``git`` and its subcommand. Memory regexes are
# written as ``git\s+<subcommand>``; agents here run ``git -C <path> ...``.
_GIT_OPT = (
    r"(?:-[Cc]\s+"
    + _ARG
    + r"|--(?:git-dir|work-tree|namespace|config-env)(?:=|\s+)"
    + _ARG
    + r"|--(?:exec-path|super-prefix)(?:="
    + _ARG
    + r")?"
    + r"|--no-pager|--paginate|--no-optional-locks|--bare|--no-replace-objects"
    + r"|--(?:literal|glob|noglob|icase)-pathspecs|--no-lazy-fetch|--no-advice|-[Pp])"
)
_GIT_GLOBALS = re.compile(r"(?<![^\s/(\"'`])git(?:\s+" + _GIT_OPT + r")+(?=\s|$)")


def _normalize(text: str, lead: bool = True) -> str:
    """``text`` with leading wrappers (``VAR=val``, ``env ...``, ``sudo``,
    ``nohup``, ``setsid``, ``timeout <n>``, ``command``, ``exec``, ``time``,
    ``nice``, and the shell words ``if then else elif while until do { !``)
    removed when ``lead``, and with
    the git global options removed after every ``git`` word."""
    if lead:
        while True:
            t = _WRAPPER.sub("", text, count=1)
            if t == text or not t.strip():
                break
            text = t
    return _GIT_GLOBALS.sub("git", text)


def _variants(segment: str) -> List[str]:
    """The raw segment, the segment without leading assignments, and the
    segment normalized by ``_normalize``. Duplicates and empties left out."""
    out = [segment]
    for v in (_ASSIGN.sub("", segment, count=1), _normalize(segment)):
        if v and v not in out:
            out.append(v)
    return out


# --------------------------------------------------------------------------
# quoted text and comments (review finding F1)
# --------------------------------------------------------------------------
# A command that only MENTIONS a guarded command must not fire: ``grep -rn
# "gh pr merge"``, ``git commit -m "... git push --force ..."``, ``echo x  #
# git push --force``. So a guard regex is searched on a MASKED view of the
# command: the text inside single and double quotes and every ``#`` comment is
# replaced by ``MASK`` (one character for one character, so positions stay
# where they were; the quote characters stay). Not masked, because the shell
# runs it: ``$( )`` and backticks inside double quotes, and a quoted string
# that is itself a command line (``bash|sh|zsh|dash|ksh|su ... -c '...'``,
# ``eval '...'``, ``ssh host '...'``; this covers ``sudo -u x bash -c``,
# ``docker exec c sh -c`` and ``kubectl exec p -- sh -c``). Such an executed
# string is masked by the same rules inside, and is ALSO matched as a command
# line of its own (``EXEC_DEPTH`` levels deep; deeper text stays unmasked).
MASK = "\x00"
EXEC_DEPTH = 3
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "su"))
_EXEC_WORDS = frozenset(("eval", "ssh"))
_LEAD_WORDS = frozenset(
    (
        "sudo",
        "env",
        "nohup",
        "setsid",
        "timeout",
        "time",
        "command",
        "exec",
        "nice",
        "ionice",
        "stdbuf",
        "doas",
    )
)
_SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_DQ_UNESCAPE = re.compile(r"\\([\\\"$`\n])")


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _executes(words: List[str]) -> bool:
    """True when a quoted word that follows ``words`` (the earlier words of the
    same simple command) is run as a command line by the shell."""
    if not words:
        return False
    if _SHELL_C.fullmatch(words[-1]) and any(_base(w) in _SHELLS for w in words[:-1]):
        return True
    for w in words:  # eval / ssh as the command word, after wrappers
        b = _base(w)
        if b in _EXEC_WORDS:
            return True
        if b in _LEAD_WORDS or w.startswith("-") or "=" in w or w.replace(".", "").isdigit():
            continue
        return False
    return False


# NOBLIVION-52: a heredoc body or a here-string is data, except when a shell
# reads it from stdin: ``bash <<EOF``, ``sh -s <<'X'``, ``sudo bash <<EOF``,
# ``ssh host <<EOF``, ``bash <<< "..."``, ``cat <<EOF | bash``. Then the shell
# runs it, so it is matched as a command line of its own. ``cat > f <<EOF``,
# ``git commit -F - <<EOF``, ``python3 <<EOF``, ``bash script.sh <<EOF`` and
# ``bash -c '...' <<EOF`` keep it as data.
_STDIN_SHELLS = _SHELLS - {"su"}
_KEYWORDS = frozenset(("if", "then", "else", "elif", "while", "until", "do", "{", "!"))
_SSH_ARG_OPTS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")  # ssh options that take a value
_SUDO_ARG_OPTS = frozenset("CDghpRrTtUu")  # sudo and doas options that take a value
_REDIR_WORD = re.compile(r"(?:\d+|&)?(?:>>|>\||>&|<&|<>|>|<)(.*)", re.S)
_NUMBER = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_NAME_EQ = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _without_redirections(argv: List[str]) -> List[str]:
    """``argv`` without ``>f``, ``2> f``, ``<f`` and their targets."""
    out: List[str] = []
    skip = False
    for w in argv:
        if skip:
            skip = False
            continue
        m = _REDIR_WORD.fullmatch(w)
        if m:
            skip = not m.group(1)
            continue
        out.append(w)
    return out


def _shell_reads_stdin(args: List[str]) -> bool:
    """True when ``bash ARGS`` (or sh, zsh, dash, ksh) reads its commands from
    stdin: no ``-c``, and ``-s`` or no script file."""
    k, s_flag = 0, False
    while k < len(args):
        w = args[k]
        if w in ("-", "--"):
            k += 1
            break
        if w.startswith("--"):
            k += 2 if w in ("--rcfile", "--init-file") else 1
            continue
        if len(w) > 1 and w[0] in "-+":
            if w[0] == "-" and "c" in w:
                return False
            s_flag = s_flag or (w[0] == "-" and "s" in w)
            k += 2 if ("o" in w or "O" in w) else 1  # -o pipefail, +O extglob
            continue
        break
    return s_flag or k >= len(args)


def _ssh_reads_stdin(args: List[str]) -> bool:
    """True when ``ssh ARGS`` gives its stdin to a shell: no remote command (the
    login shell reads it), or a remote command that is a shell reading stdin."""
    k = 0
    while k < len(args):
        w = args[k]
        if w == "--":
            k += 1
            break
        if len(w) < 2 or w[0] != "-":
            break
        for p, ch in enumerate(w[1:], 1):
            if ch in _SSH_ARG_OPTS:
                k += p == len(w) - 1  # the value is the next word
                break
        k += 1
    if k >= len(args):
        return False  # no host
    rest = " ".join(args[k + 1 :]).split()  # ssh joins the words for the remote shell
    return not rest or _stdin_reader(rest) == "shell"


def _stdin_reader(argv: List[str]) -> str:
    """Who reads the stdin of a simple command (``argv``: its words with the
    quotes removed). ``"shell"`` when a shell runs it as commands (``bash``,
    ``sh``, ``zsh``, ``dash``, ``ksh``, ``su``, ``sudo -s``/``-i``, ``ssh``,
    also by path and behind ``sudo``, ``env``, ``nohup``, ``exec`` and the
    other lead words), ``"cat"`` when ``cat`` with no file passes it on,
    ``""`` otherwise."""
    words = _without_redirections(argv)
    lead, after_opt, login = "", False, False
    for k, w in enumerate(words):
        b = _base(w)
        if b in _STDIN_SHELLS:
            return "shell" if _shell_reads_stdin(words[k + 1 :]) else ""
        if b == "su":
            rest = words[k + 1 :]
            run = any(
                x.startswith("--command") or re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", x) for x in rest
            )
            return "" if run else "shell"
        if b == "ssh":
            return "shell" if _ssh_reads_stdin(words[k + 1 :]) else ""
        if b in _LEAD_WORDS or w in _KEYWORDS:
            lead, after_opt = b, False
        elif w.startswith("-") and lead:
            after_opt = True
            if lead in ("sudo", "doas"):
                if w in ("--shell", "--login"):
                    login = True
                elif not w.startswith("--"):
                    for ch in w[1:]:
                        if ch in "si":
                            login = True
                        if ch in _SUDO_ARG_OPTS:
                            break
        elif _NAME_EQ.match(w) or _NUMBER.fullmatch(w):
            after_opt = False
        elif after_opt:
            after_opt = False  # the value of a wrapper option (``sudo -u bob``)
        else:
            if b == "cat" and all(x.startswith("-") for x in words[k + 1 :]):
                return "cat"
            return ""
    return "shell" if login else ""


class _Scan:
    """One pass over a command line (heredoc bodies already removed; ``bodies``
    maps the index of each ``<<`` to its body). Sets ``dead[i]`` for quoted
    text that is data and ``comment[i]`` for comment text; collects the
    executed strings in ``execd``, with each heredoc body and here-string that
    a shell reads (``_stdin_reader``)."""

    def __init__(self, s: str, depth: int, bodies: Optional[Dict[int, str]] = None):
        self.s, self.depth = s, depth
        self.bodies = bodies or {}
        self.dead = bytearray(len(s))
        self.comment = bytearray(len(s))
        self.execd: List[str] = []

    def run(self) -> _Scan:
        self.cmd(0, "")
        return self

    def _mark(self, arr: bytearray, a: int, b: int) -> None:
        if b > a:
            arr[a:b] = b"\x01" * (b - a)

    def _sub(self, off: int, text: str) -> None:
        """An executed string at ``off``: mask inside by the same rules, and
        keep it for its own match. Past ``EXEC_DEPTH`` it stays unmasked."""
        if self.depth >= EXEC_DEPTH:
            return
        self.execd.append(text)
        inner = _Scan(text, self.depth + 1).run()
        self.dead[off : off + len(text)] = inner.dead
        self.comment[off : off + len(text)] = inner.comment

    def _feed(
        self, argv: List[str], stdin: List[Tuple[int, str]], pipe: bool
    ) -> List[Tuple[int, str]]:
        """The end of a simple command with the words ``argv`` and the stdin
        texts ``stdin`` (``(index, text)``: a here-string in ``s`` at
        ``index``; ``-1`` for a heredoc body or text not in ``s``). A shell
        reader runs them; ``cat`` before a ``|`` passes them on (returned)."""
        if not stdin:
            return []
        reader = _stdin_reader(argv)
        if reader == "shell":
            for off, text in stdin:
                if off >= 0:
                    self._sub(off, text)
                elif self.depth < EXEC_DEPTH:
                    self.execd.append(text)
        return list(stdin) if reader == "cat" and pipe else []

    def cmd(self, i: int, stop: str) -> int:
        """Scan command text from ``i`` up to ``stop`` (``)`` or a backtick;
        ``""`` for the end). Returns the index after ``stop``."""
        s, n = self.s, len(self.s)
        words: List[str] = []
        cur: List[str] = []
        argv: List[str] = []  # the words with the quotes removed
        arg: List[str] = []
        stdin: List[Tuple[int, str]] = []  # heredoc bodies and here-strings
        carry: List[Tuple[int, str]] = []  # what ``cat`` pipes to the next command
        here = False  # the next word is a here-string (``<<< word``)
        depth = 0

        def end_word() -> None:
            nonlocal here
            if cur:
                words.append("".join(cur))
                if here:
                    stdin.append((-1, "".join(arg)))
                else:
                    argv.append("".join(arg))
                cur.clear()
                arg.clear()
                here = False

        def end_cmd(pipe: bool) -> None:
            nonlocal carry
            end_word()
            if argv or stdin:  # an empty command (``|&``, ``|`` newline) keeps the carry
                carry = self._feed(argv, stdin or carry, pipe)
            argv.clear()
            stdin.clear()

        while i < n:
            c = s[i]
            if stop == ")" and c == ")" and depth == 0:
                end_cmd(False)
                return i + 1
            if stop == "`" and c == "`":
                end_cmd(False)
                return i + 1
            if c == "\\":
                cur.append(s[i : i + 2])
                arg.append(s[i + 1 : i + 2])
                i += 2
                continue
            if c in " \t":
                end_word()
                i += 1
                continue
            if c == "#" and not cur:
                j = s.find("\n", i)
                j = n if j < 0 else j
                self._mark(self.comment, i, j)
                i = j
                continue
            if c == "'":
                j = s.find("'", i + 1)
                j = n if j < 0 else j
                if not cur and _executes(words):
                    self._sub(i + 1, s[i + 1 : j])
                else:
                    self._mark(self.dead, i + 1, j)
                cur.append("'")
                arg.append(s[i + 1 : j])
                i = j + 1
                continue
            if c == '"':
                run = not cur and _executes(words)
                j = self.dq(i, live=run)
                if run:
                    body = s[i + 1 : j - 1] if j - 1 > i else ""
                    if "\\" in body:  # positions shift: leave it unmasked here
                        if self.depth < EXEC_DEPTH:
                            self.execd.append(_DQ_UNESCAPE.sub(r"\1", body))
                    else:
                        self._sub(i + 1, body)
                cur.append('"')
                arg.append(_DQ_UNESCAPE.sub(r"\1", s[i + 1 : max(i + 1, j - 1)]))
                i = j
                continue
            if c == "$" and s.startswith("$(", i):
                j = self.cmd(i + 2, ")")
                cur.append("$")
                arg.append(s[i:j])
                i = j
                continue
            if c == "`":
                j = self.cmd(i + 1, "`")
                cur.append("`")
                arg.append(s[i:j])
                i = j
                continue
            if c == "<" and s.startswith("<<<", i):  # a here-string: its word is stdin
                end_word()
                i += 3
                while i < n and s[i] in " \t":
                    i += 1
                if i < n and s[i] == "'":
                    j = s.find("'", i + 1)
                    j = n if j < 0 else j
                    self._mark(self.dead, i + 1, j)
                    stdin.append((i + 1, s[i + 1 : j]))
                    i = j + 1
                elif i < n and s[i] == '"':
                    j = self.dq(i, live=False)
                    body = s[i + 1 : j - 1] if j - 1 > i else ""
                    unesc = _DQ_UNESCAPE.sub(r"\1", body)
                    stdin.append((i + 1, body) if "\\" not in body else (-1, unesc))
                    i = j
                else:
                    here = True
                continue
            if c == "<" and s.startswith("<<", i):
                m = _HEREDOC.match(s, i)
                if m:  # the body is gone; hide the operator too
                    self._mark(self.dead, i, m.end())
                    fd = "".join(arg) if cur and not here and "".join(arg).isdigit() else ""
                    end_word()
                    if fd:
                        argv.pop()  # ``3<<EOF``: the number names the descriptor
                    body = self.bodies.get(i)
                    if body is not None and fd in ("", "0"):
                        stdin.append((-1, body))
                    i = m.end()
                    continue
            if c in ";|&\n":
                # a pipe passes what ``cat`` read to the next command; ``||`` does not
                end_cmd(c == "|" and not s.startswith("||", i) and s[i - 1 : i] != "|")
                words.clear()
                i += 1
                continue
            if c == "(":
                depth += 1
                end_cmd(False)
                words.clear()
                i += 1
                continue
            if c == ")":
                depth = max(0, depth - 1)
                end_cmd(False)
                words.clear()
                i += 1
                continue
            cur.append(c)
            arg.append(c)
            i += 1
        end_cmd(False)
        return n

    def dq(self, i: int, live: bool) -> int:
        """A double-quoted string that opens at ``i``. Its text is dead unless
        ``live``; ``$( )`` and backticks in it are scanned as commands.
        Returns the index after the closing quote."""
        s, n = self.s, len(self.s)
        j = i + 1
        while j < n:
            c = s[j]
            if c == "\\":
                if not live:
                    self._mark(self.dead, j, min(j + 2, n))
                j += 2
                continue
            if c == '"':
                return j + 1
            if c == "$" and s.startswith("$(", j):
                j = self.cmd(j + 2, ")")
                continue
            if c == "`":
                j = self.cmd(j + 1, "`")
                continue
            if not live:
                self.dead[j] = 1
            j += 1
        return n


def command_views(command: str, depth: int = 0) -> List[Tuple[str, str, bytearray]]:
    """``[(masked, raw, dead)]``: the command line and, after it, each string
    it executes (``bash -c '...'`` ...), recursively. ``raw`` is the line
    without heredoc bodies and with comments masked; ``masked`` also masks the
    quoted data; ``dead[i]`` is set where ``raw`` holds quoted data. A heredoc
    body or a here-string that a shell reads is an executed string too."""
    _segs, body, bodies = _lex(command)
    sc = _Scan(body, depth, bodies).run()
    raw = "".join(MASK if sc.comment[k] else ch for k, ch in enumerate(body))
    masked = "".join(MASK if (sc.comment[k] or sc.dead[k]) else ch for k, ch in enumerate(body))
    out = [(masked, raw, sc.dead)]
    for x in sc.execd:
        out.extend(command_views(x, depth + 1))
    return out


def command_segments(command: str) -> List[str]:
    """The shell segments of every masked view of ``command``: what the rows
    leg (triggers) of the guard hook matches against."""
    out: List[str] = []
    for masked, _raw, _dead in command_views(command):
        for seg in _lex(masked)[0]:
            if seg not in out:
                out.append(seg)
    return out


def variants(segment: str) -> List[str]:
    """Public name of ``_variants`` for the guard hook."""
    return _variants(segment)


_RAW_TRIES = 32


def _live_hit(rx: Pattern[str], raw: str, dead: bytearray) -> bool:
    """True when ``rx`` has a match in ``raw`` that STARTS outside quoted data
    and does not END in the quoted data of another simple command. It keeps a
    quoted argument of a live command visible (``docker restart "x-runner"``)
    while a mention inside quotes stays out: ``grep "gh pr merge"`` (the match
    starts in quotes) and ``docker compose logs | grep "-p obs"`` (the match
    starts on a live command, crosses a live ``|``, ``;``, ``&`` or newline
    and ends in the quotes of the next command). At most ``_RAW_TRIES``
    matches are tried."""
    size = len(dead)
    pos = 0
    for _ in range(_RAW_TRIES):
        m = rx.search(raw, pos)
        if m is None:
            return False
        a, b = m.start(), m.end()
        if a >= size or not dead[a]:
            last = b - 1
            if last <= a or last >= size or not dead[last]:
                return True
            cut = next((k for k in range(a, last) if raw[k] in ";|&\n" and not dead[k]), -1)
            if cut < 0:
                return True
            # the greedy match ran into the next command: try inside this one
            m = rx.search(raw, a, cut)
            if m is not None and not dead[m.start()]:
                return True
        pos = a + 1
    return False


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
_COMPILED: Dict[str, Optional[Pattern[str]]] = {}
_LOADED: Dict[str, Any] = {"key": None, "table": None}


def _compile(pat: str) -> Optional[Pattern[str]]:
    if pat not in _COMPILED:
        try:
            _COMPILED[pat] = re.compile(pat)
        except re.error:
            _COMPILED[pat] = None
    return _COMPILED[pat]


def load_table(path: Optional[Path] = None) -> Dict[str, Any]:
    """The table at ``path`` (default ``table_path()``), cached by mtime and
    size. An absent or broken table reads as empty: the guard fails open."""
    p = Path(path) if path is not None else table_path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return {"entries": []}
    if _LOADED["key"] != key:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                data = {"entries": []}
        except (OSError, ValueError):
            data = {"entries": []}
        _LOADED["key"], _LOADED["table"] = key, data
    return _LOADED["table"]


def match(command: str, table: Optional[Mapping[str, Any]] = None) -> List[Dict[str, object]]:
    """Every entry whose ``violates`` regex matches ``command``. One hit per
    memory. The regex runs on the views of ``command_views``: quoted data,
    ``#`` comments and heredoc bodies are masked, executed strings (``bash -c
    '...'``) are matched as their own command lines. ``via`` is ``"segment"``
    when a shell segment of a masked view matched (``segment`` holds it, the
    masked characters shown as ``_``), ``"whole"`` when only a whole masked
    view matched (a regex that spans ``&&`` or ``|``), and ``"raw"`` when only
    the unmasked view matched with the match starting outside quoted data and
    not ending in the quoted data of another simple command (a quoted argument
    of a live command, ``docker restart "x-runner"``)."""
    if not isinstance(command, str) or not command.strip():
        return []
    if table is None:
        table = load_table()
    views = []
    for masked, raw, dead in command_views(command):
        segs = [(seg, _variants(seg)) for seg in _lex(masked)[0]]
        views.append((segs, masked, _normalize(masked, lead=False), raw, dead))
    hits: List[Dict[str, object]] = []
    for e in table.get("entries") or []:
        pat = e.get("violates") if isinstance(e, dict) else None
        if not pat:
            continue
        rx = _compile(pat)
        if rx is None:
            continue
        via, seg = "", ""
        for segs, masked, masked_n, raw, dead in views:
            for sg, vs in segs:
                if any(rx.search(v) for v in vs):
                    via, seg = "segment", sg.replace(MASK, "_")
                    break
            if not via and (rx.search(masked) or rx.search(masked_n)):
                via = "whole"
            if not via and _live_hit(rx, raw, dead):
                via = "raw"
            if via:
                break
        if via:
            hits.append(
                {
                    "id": e.get("id", ""),
                    "regex": pat,
                    "rule": e.get("rule", ""),
                    "apply": e.get("apply", ""),
                    "scope": e.get("scope", ""),
                    "via": via,
                    "segment": seg,
                    "complies": e.get("complies", "") or "",
                    "run_first": e.get("run_first", "") or "",
                }
            )
    return hits


# --------------------------------------------------------------------------
# WI-3d: the compliant form
# --------------------------------------------------------------------------
_SUBST = re.compile(r"\$\(([^()]*)\)")


def _complies_hit(rx: Pattern[str], command: str) -> bool:
    """``rx`` over the raw command, the command with the git global options
    removed, and each shell segment variant (wrappers and ``VAR=`` removed)."""
    if rx.search(command) or rx.search(_normalize(command, lead=False)):
        return True
    return any(rx.search(v) for seg in command_segments(command) for v in variants(seg))


_SHELL_WORDS = frozenset(
    {
        "do",
        "done",
        "then",
        "else",
        "elif",
        "fi",
        "esac",
        "while",
        "until",
        "for",
        "if",
        "case",
        "in",
        "{",
        "}",
        "!",
    }
)


def _raw_segments(command: str) -> List[str]:
    """The shell segments of ``command`` as the ORIGINAL text. The masked view
    blanks quoted data but keeps every offset, so each masked segment is cut
    from the raw text at the same place. A segment the lexer changed (not found
    in the masked view) is left out: a suggested command must run as shown."""
    out: List[str] = []
    for masked, raw, _dead in command_views(command):
        pos = 0
        for seg in _lex(masked)[0]:
            at = masked.find(seg, pos)
            if at < 0:
                continue
            pos = at + len(seg)
            piece = raw[at:pos]
            if piece not in out:
                out.append(piece)
    return out


def first_command(example_ok: str, complies: str, violates: str) -> str:
    """The shortest piece of ``example_ok`` (a ``$( )`` substitution, a shell
    segment, or the whole example) that ``complies`` matches and ``violates``
    does not. ``""`` when there is none."""
    rx = _compile(complies)
    if rx is None or not example_ok.strip():
        return ""
    one = {"entries": [{"id": "_", "violates": violates}]}
    pieces = [m.group(1) for m in _SUBST.finditer(example_ok)] + _raw_segments(example_ok)
    pieces.append(example_ok)
    best = ""
    for p in pieces:
        p = p.strip()
        if not p or (best and len(p) >= len(best)):
            continue
        if p.split(None, 1)[0] in _SHELL_WORDS:  # a loop or if body cut out: cannot run alone
            continue
        if _complies_hit(rx, p) and not match(p, one):
            best = p
    return best


def complies_match(command: str, table: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The ids of the entries whose ``complies`` matches ``command``: a run of
    the rule's own command. An entry whose ``violates`` also matches
    ``command`` is left out (a violation is never a compliant run). Fails
    open: a broken regex is skipped."""
    if not isinstance(command, str) or not command.strip():
        return []
    if table is None:
        table = load_table()
    out: List[str] = []
    hit_ids = None
    for e in table.get("entries") or []:
        if not isinstance(e, dict) or not e.get("violates") or not e.get("complies"):
            continue
        rx = _compile(str(e["complies"]))
        if rx is None or not _complies_hit(rx, command):
            continue
        if hit_ids is None:
            hit_ids = {str(h["id"]) for h in match(command, table)}
        if str(e.get("id", "")) not in hit_ids:
            out.append(str(e.get("id", "")))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    """``--rebuild``: rebuild the table of the session's project; exit 0
    always, silent when no rule was skipped and when the memory folder does
    not exist, one stderr line on failure (a SessionStart hook fails open).
    When the build skipped a rule, it prints ``skipped_line`` as one JSON
    object: ``systemMessage`` for the user and ``additionalContext`` for the
    model (the shape of ``store_client.main``). The hook JSON on
    stdin gives the ``cwd``; without it the process working dir is used.
    ``--report``: print the skipped regexes after a rebuild, as plain text.
    ``--match CMD``: print the hits as JSON."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--match" in args:
        try:
            cmd = args[args.index("--match") + 1]
            print(json.dumps(match(cmd, load_table(table_path())), indent=1))
        except Exception as exc:  # noqa: BLE001
            print(f"guard table: match failed: {exc}", file=sys.stderr)
        return 0
    if "--rebuild" in args:
        try:
            folders = source_dirs(_event_cwd())
            if not any(f.is_dir() for f in folders):
                # A fresh install has no memory folder yet: no memory, no
                # guards. That is not an error (NOBLIVION-27). An absent
                # table reads as empty; an old table stays as it is.
                if "--report" in args:
                    where = folders[0] if folders else "(none)"
                    print(f"no memory folder {where}: no guards")
                return 0
            table = rebuild(folders)
            if "--report" in args:
                n = sum(1 for e in table["entries"] if e["violates"])
                li = table.get("label_index") or {}
                labels = (
                    f"label index {li.get('n', 0)} memories, {len(li.get('labels') or [])} labels"
                    if li
                    else f"no label index ({table.get('label_error', 'unknown')})"
                )
                print(
                    f"{len(table['entries'])} entries, {n} guards, "
                    f"{len(table['skipped'])} skipped, {labels} -> {table_path(folders[0])}"
                )
                for s in table["skipped"]:
                    print(f"  skipped {s['id']}: {'; '.join(s['problems'])}")
            else:
                line = skipped_line(table)
                if line:
                    print(
                        json.dumps(
                            {
                                "systemMessage": line,
                                "hookSpecificOutput": {
                                    "hookEventName": "SessionStart",
                                    "additionalContext": line,
                                },
                            }
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - fail open
            try:
                print(f"guard table: rebuild failed: {exc}", file=sys.stderr)
            except Exception:  # noqa: BLE001, S110 - a hook fails open
                pass
        return 0
    print((__doc__ or "").split("\n\n")[0], file=sys.stderr)
    return 0


def _event_cwd() -> Optional[str]:
    """The ``cwd`` of the hook JSON on stdin, or None (a terminal, no JSON)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        event = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return None
    cwd = event.get("cwd") if isinstance(event, dict) else None
    return cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _CFG.make_dirs(path)


if __name__ == "__main__":
    sys.exit(main())
