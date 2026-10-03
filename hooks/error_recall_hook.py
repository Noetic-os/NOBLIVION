#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Error-time recall: when a Bash call fails, show the memory that holds the
fix (WI-14).

Events (verified against Claude Code 2.1.284, the installed build, and the
transcripts under ``~/.claude/projects``)
  PostToolUseFailure  Claude Code sends this event, NOT ``PostToolUse``, when
                      a Bash call exits non-zero. The input has
                      ``tool_name``, ``tool_input.command``, ``error`` (the
                      text the model sees: ``Exit code <n>``, then stderr,
                      then stdout) and ``is_interrupt``. There is no numeric
                      exit code field: the code is the first line of
                      ``error``. Output: ``hookSpecificOutput`` with
                      ``hookEventName: "PostToolUseFailure"`` and
                      ``additionalContext``.
  PostToolUse         A Bash call that exits 0. ``tool_response`` is
                      ``{stdout, stderr, interrupted, isImage,
                      noOutputExpected, ...}``; a grep with no match comes here
                      too (exit 1 read as "no matches",
                      ``returnCodeInterpretation``). A failure hidden behind a
                      pipe (``pytest ... 2>&1 | tail -3``) exits 0, so this
                      path fires on a STRONG error line only, and never when
                      the first command of a statement reads data (``grep``,
                      ``cat``, ``tail``, ``journalctl``, ``git log``,
                      ``docker logs``, ``kubectl logs``, ``gh pr view``,
                      ``gh run view``, the remote command of ``ssh host
                      '...'`` ...), and never for a call of only ``echo``/
                      ``printf`` and silent commands: its output is data, not
                      this call's error.

Query
  The error lines of the output (``error_lines``): a Python exception line
  (after a Traceback, the last one), ``fatal:``/``error:`` diagnostics, pytest
  ``E`` and ``FAILED`` lines, ``No such file or directory``, ``Permission
  denied``, ``command not found``, ``Connection refused`` and a few more (see
  ``_STRONG``, ``_WEAK``). A failure with no error line shows nothing. The
  lines are cleaned (ANSI codes, long hex and UUIDs, absolute paths folded to
  their last two parts), secrets are redacted (``redact``: bearer tokens,
  ``ghp_``/``github_pat_``/``sk-``/``xox?-``/``AKIA`` keys, JWTs, URL
  credentials, ``password=``/``token:`` values, PEM blocks, long opaque
  strings) on each whole line before any cut, then cut to ``QUERY_MAX_CHARS``
  (400). Every cut drops the word it splits. A logic exception (TypeError,
  AttributeError ...) whose last traceback frame is own code is not an error
  line (``LOGIC_EXCEPTIONS``). A Go template error (``docker inspect -f``:
  ``map has no entry for key``, ``can't evaluate field``, ``nil pointer
  evaluating``) is read as its field chain, e.g. ``HostConfig.Tmpfs``
  (``_GO_TEMPLATE``).

Quote check (every mode)
  A hit that passes its score gate is shown only when the memory quotes the
  error's message: a run of ``QUOTE_RUN`` (3) consecutive message words, with
  paths and quoted values removed, holding a word not in ``GENERIC_WORDS``
  (``quotes_error``). Otherwise the log says ``no-quote``.

Search (env ``NOBLIVION_ERROR_RECALL_MODE``, see ``search``)
  local (default)  BM25 over the memory files (name, description, ``rule:``,
                   ``apply:``, body), about 0.1 s, no network. Ranked by BM25;
                   the gate score is the idf-weighted share of the query terms
                   the memory holds (``LOCAL_MIN_SCORE`` 0.75, env
                   ``NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE``).
  store            the local store's ranked index, ``GET /api/memories/index``
                   (rows with ``score`` and ``source``, the memory file name);
                   local when the store cannot answer, fails the listener
                   proof, or answers in keyword mode (design doc sections 3.5
                   and 8.4). Store discovery, proof, token and HTTP client are
                   those of ``recall_hook`` (loaded from the folder of this
                   file); HTTP budget ``NOBLIVION_ERROR_RECALL_TIMEOUT_S``
                   (0.8 s). Only rows of a memory file are kept (no
                   transcript-miner rows, no index or topic files). Gate
                   ``STORE_MIN_SCORE`` 0.60 (env
                   ``NOBLIVION_ERROR_RECALL_MIN_SCORE``).
  fused            both lists, reciprocal rank fusion; a hit passes when either
                   of its scores reaches its gate.
  The store and fused modes are OFF by default: only an explicit
  ``NOBLIVION_ERROR_RECALL_MODE`` turns them on. They send only the redacted
  query (never the command), and the hook time limit (``TIME_LIMIT_S``) cuts
  them like the local search.

Memory folder
  ``NOBLIVION_ERROR_RECALL_MEMORY_DIR``, else ``NOBLIVION_RECALL_MEMORY_DIR``,
  else ``NOBLIVION_MEMORY_DIR``, else the memory folder Claude Code keeps for
  the event's ``cwd`` (``~/.claude/projects/<slug of cwd>/memory``) when it
  exists, else the memory folder of the home folder.

Output and budget
  At most ``MAX_HITS`` (2) hits (WI-14b). The header says the text IS the
  memory, so no file lookup is needed. Each hit starts with
  ``- Memory <id> (...)`` and then shows the memory's own text:
  ``Rule:`` and ``Apply:`` whole from the front matter, and ``Text:`` the
  whole body when it is at most ``FULL_BODY_CHARS`` (1200) characters, else
  ``Text on this error:`` the body part on the error (``excerpt``) cut at a
  sentence end at ``TEXT_CHARS`` (1200), with the count of characters the
  file adds. A show stays under ``RENDER_MAX_CHARS`` (3200): a second hit
  that breaks it gets a shorter body part (at least ``MIN_TEXT_CHARS``),
  else Rule and Apply only, and is dropped if it still breaks it. Memory text is redacted (``redact_memory``: the
  query patterns, with the two prose-prone ones made strict) and then made
  inert with the recall hook's ``_neutral``. One memory is shown at most
  ``SHOW_REPEAT`` (3) times per agent of a session (key: ``session_id`` plus
  ``agent_id``, as in the WI-3 guard), and all shows of one agent of a
  session stay under ``SESSION_CHARS`` (6000 characters; env
  ``NOBLIVION_ERROR_RECALL_SESSION_CHARS``). Since WI-3f the formatter and the
  redaction live in ``memory_text.py`` (shared with the guard rows
  and the prompt recall hook; the output is byte-identical), so install that
  file beside this one.

Log and state
  Every show, and every error, is one JSON line in the log (env
  ``NOBLIVION_ERROR_RECALL_LOG``, default ``<data dir>/error-recall-log.jsonl``):
  ``ts``, ``session_id``, ``agent_id``, ``event``, ``decision``, ``ids``,
  ``scores``, ``source``, ``ms``, ``query`` (the redacted query). State is one
  file per agent of a session, ``er-<hash>.json``, in the WI-3 guard state
  folder (env ``NOBLIVION_ERROR_RECALL_STATE_DIR``, then
  ``NOBLIVION_GUARD_STATE_DIR``, default ``<data dir>/guard-state/``). Files are
  0600, new folders 0700.

Fail open
  Any exception, bad stdin, a missing memory folder or a run longer than
  ``TIME_LIMIT_S`` (1.5 s) prints nothing, and the exit code is 0 on every
  path. The plugin runs it as ``python3 hooks/error_recall_hook.py``.

Standard library only.
"""

from __future__ import annotations

import bisect
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_HERE = Path(__file__).resolve().parent
EVENTS = ("PostToolUseFailure", "PostToolUse")
TOOLS = ("Bash",)
QUERY_MAX_CHARS = 400
LINE_MAX_CHARS = 200
LINE_SCAN_CHARS = 4000
COMMAND_MAX_CHARS = 8192
MAX_ERROR_LINES = 3
MAX_HITS = 2
SHOW_REPEAT = 3
SESSION_CHARS = 6000
STORE_TIMEOUT_S = 0.8
STORE_TOP_K = 10
TIME_LIMIT_S = 1.5
STORE_MIN_SCORE = 0.60
LOCAL_MIN_SCORE = 0.75
RULE_CHARS = 160
CONTEXT_WEIGHT = 0.5
APPLY_CHARS = 220
# WI-14b: the hit shows the memory's own text, so the model needs no file
# lookup (paid test 5: a cut excerpt that ended in "[memory <id>]" cost 3
# calls of find, Glob and Read before the fix).
FULL_BODY_CHARS = 1200  # a body up to this length is shown whole
TEXT_CHARS = 1200  # else the body part on this error, cut at a sentence end
EXCERPT_LINES = 40  # lines of the body from the one on this error, before the cut
FIELD_CHARS = 600  # one rule or apply field (the schema caps them at 160 and 400)
RENDER_MAX_CHARS = (
    3200  # one show, all hits; a second hit that breaks it is shortened, then dropped
)
MIN_TEXT_CHARS = 300  # a second hit's body part below this shows Rule and Apply only
HEADER = (
    "Memory for this error (error recall, WI-14). This is the memory's own text, "
    "so act on it; no need to open the memory file."
)
STATE_PREFIX = "er-"
STATE_MAX_AGE_S = 7 * 24 * 3600


class _TimeUp(BaseException):
    """The time limit. A BaseException, so no ``except Exception`` handler
    (read_memory, _short, the HTTP worker) can swallow it and let the hook
    print after its limit."""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODS: Dict[str, Any] = {}


def _mod(name: str):
    if name not in _MODS:
        _MODS[name] = _load(name)
    return _MODS[name]


def _rh():
    return _mod("recall_hook")


def _mf():
    return _mod("memory_fields")


# WI-3f: the memory formatter and the redaction are shared with the guard
# rows and the prompt recall hook.
_MT = _load("memory_text")
_CFG = _load("hook_config")
DEFAULT_LOG = _CFG.data_dir() / "error-recall-log.jsonl"
DEFAULT_STATE = _CFG.data_dir() / "guard-state"
DEFAULT_MEMORY_DIR = _CFG.default_memory_dir()
MEMORY_DIR_ENVS = (
    "NOBLIVION_ERROR_RECALL_MEMORY_DIR",
    "NOBLIVION_RECALL_MEMORY_DIR",
    "NOBLIVION_MEMORY_DIR",
)


def _path_env(env: Mapping[str, str], names: Tuple[str, ...], default: Path) -> Path:
    for n in names:
        if env.get(n):
            return Path(env[n]).expanduser()
    return default


def log_path(env: Mapping[str, str]) -> Path:
    return _path_env(env, ("NOBLIVION_ERROR_RECALL_LOG",), DEFAULT_LOG)


def state_dir(env: Mapping[str, str]) -> Path:
    return _path_env(
        env, ("NOBLIVION_ERROR_RECALL_STATE_DIR", "NOBLIVION_GUARD_STATE_DIR"), DEFAULT_STATE
    )


def memory_dir(env: Mapping[str, str], cwd: Any = None) -> Path:
    """The memory folder: the env (``MEMORY_DIR_ENVS``), else the folder of
    the event's ``cwd`` when it exists, else ``DEFAULT_MEMORY_DIR``."""
    for n in MEMORY_DIR_ENVS:
        if env.get(n):
            return Path(env[n]).expanduser()
    if isinstance(cwd, str) and os.path.isabs(cwd):
        slug = _CFG.project_slug(cwd.rstrip("/") or "/")
        folder = Path.home() / ".claude" / "projects" / slug / "memory"
        if folder.is_dir():
            return folder
    return DEFAULT_MEMORY_DIR


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# error detection
# --------------------------------------------------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_EXIT_LINE = re.compile(r"^Exit code (-?\d+)\s*$")
_PY_EXC = re.compile(
    r"^\s*(?:[A-Za-z_][\w]*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Failure|Refused|Timeout)"
    r"(?:: .*)?$"
)
# Lines that are an error on their own, also in the output of a call that
# exited 0 (a pipe hides the exit code).
_STRONG = [
    re.compile(p)
    for p in (
        r"^\s*Traceback \(most recent call last\)",
        r"^\s*(?:[\w./@+-]+: ){0,3}(?:fatal|FATAL|panic|error|ERROR|Error)(?:\[\w+\])?: \S",
        r"^\s*(?:ba|z|da)?sh: (?:line \d+: )?.*(?:command not found|No such file or directory|Permission denied)",
        r"^\s*[\w./+-]+: (?:line \d+: )?[\w./+-]+: command not found",
        r"^\s*npm ERR!",
        r"^\s*curl: \(\d+\) ",
        r"^\s*ssh: .*(?:Could not resolve|Connection refused|Connection timed out|Permission denied)",
        r"^\s*(?:psql|ERROR|FATAL): .*(?:does not exist|permission denied|syntax error|violates)",
        r"^\s*(?:Error response from daemon|OCI runtime .*failed)",
        r"^\s*(?:[\w./-]+: )?(?:cannot|can't|unable to|failed to) \w+.*: .*(?:No such file|Permission denied|not found|denied|refused|exists)",
    )
]
# The Go template error of `docker inspect -f` (and any other Go template
# CLI). Measured 2026-09-30 on docker 29.8.0: a format that fails on the typed
# struct runs again on the raw JSON map, and a key docker omits (for example
# `HostConfig.Tmpfs` when no tmpfs is set) then fails with
# `template parsing error: template: :1:13: executing "" at
# <.HostConfig.Tmpfs>: map has no entry for key "Tmpfs"`. The message is Go
# boilerplate that no memory quotes; the field chain names THIS error. So the
# line is read as the chain (`HostConfig.Tmpfs`), without the leading dot,
# which the quote check would read as a path. A one-part chain (`RO`) is then
# too short to search, so it shows nothing.
_GO_TEMPLATE = re.compile(
    r'^\s*(?:template parsing error: )?template: \S*?:\d+:\d+: executing "[^"]*" at <\$?\.?([\w.]+)>: '
    r"(?:map has no entry for key|can't evaluate field|nil pointer evaluating)"
)
# Lines that are an error only in the output of a call that failed.
_WEAK = [
    re.compile(p, re.I)
    for p in (
        r"no such file or directory",
        r"permission denied",
        r"command not found",
        r"connection refused",
        r"could not resolve host",
        r"timed out",
        r"not a git repository",
        r"\bnot found\b",
        r"\bdenied\b",
        r"\bfailed\b",
        r"\berror\b",
        r"\binvalid\b",
        r"\bunknown (?:option|command|flag|argument)",
        r"\busage: ",
    )
]
# The first command of a statement that reads data: its output is not the
# error of this call (grep for "error" prints lines holding "error").
READERS = frozenset(
    (
        "grep egrep fgrep zgrep rg ack ag cat zcat bat less more head tail sed awk gawk jq yq "
        "journalctl dmesg find ls tree wc sort uniq cut strings xxd od hexdump diff comm column"
    ).split()
)
READER_GIT = frozenset("log show diff grep blame reflog".split())
_WRAPPERS = frozenset("sudo env nohup setsid timeout nice ionice time command exec".split())


_cut = _MT._cut  # WI-3f: shared (memory_text)


def _clean_line(line: str) -> str:
    """One output line: ANSI codes gone, secrets redacted over the WHOLE line
    (only a very long line is first cut, at a word, to bound the regex cost),
    then cut to ``2 * LINE_MAX_CHARS`` at a word."""
    line = _ANSI.sub("", line).replace("\t", " ").strip()
    return _cut(redact(_cut(line, LINE_SCAN_CHARS)), LINE_MAX_CHARS * 2)


_HEREDOC = re.compile(r"(?<!<)<<(-?)\s*['\"]?(\w+)['\"]?")


def _strip_heredocs(command: str) -> str:
    """``command`` without heredoc bodies, by a line scan (linear): a line
    with ``<<WORD`` (or ``<<-WORD``) drops the lines after it up to the first
    line that is ``WORD`` (tabs allowed before it for ``<<-``). A ``<<`` with
    no such line later is no heredoc (a bit shift in ``python3 -c``), so
    nothing is dropped for it."""
    lines = command.split("\n")
    where: Dict[str, List[int]] = {}
    for i, x in enumerate(lines):
        where.setdefault(x.rstrip(), []).append(i)
        if x.startswith("\t"):
            where.setdefault("\t" + x.strip(), []).append(i)
    out: List[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        end = -1
        for m in _HEREDOC.finditer(lines[i]):
            key = m.group(2)
            nxt = [
                c[k]
                for c in (where.get(key, []), where.get("\t" + key, []) if m.group(1) else [])
                for k in (bisect.bisect_right(c, i),)
                if k < len(c)
            ]
            if nxt:
                end = max(end, min(nxt))
                break
        i = end + 1 if end > i else i + 1
    return "\n".join(out)


def _statements(command: str) -> List[str]:
    """Rough statements of a shell command: split on ``&&``, ``||``, ``;``
    and newlines; heredoc bodies are dropped. Only the first
    ``COMMAND_MAX_CHARS`` are read: this runs on every Bash call."""
    command = _strip_heredocs((command or "")[:COMMAND_MAX_CHARS])
    return [s for s in re.split(r"&&|\|\||;|\n", command) if s.strip()]


def _command_words(stage: str) -> List[str]:
    """The words of one pipeline stage from the command name on: leading
    ``VAR=value`` words, wrappers (``sudo``, ``timeout 30`` ...) and their
    flags are skipped."""
    words = stage.strip().lstrip("({ ").split()
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_]\w*=", w) or w in _WRAPPERS or (w.startswith("-") and i > 0):
            i += 1
            continue
        if re.fullmatch(r"\d+[smhd]?", w) and i > 0 and words[i - 1] in ("timeout",):
            i += 1
            continue
        break
    return words[i:]


def _first_word(stage: str) -> Tuple[str, str]:
    words = _command_words(stage)
    if not words:
        return "", ""
    name = os.path.basename(words[0])
    sub = ""
    if name == "git":
        rest = words[1:]
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in ("-C", "-c") else 1
        sub = rest[j] if j < len(rest) else ""
    return name, sub


# Commands whose output is data (logs, a pull request, a run log), by their
# first non-option words after the command name.
READER_SUBS = {
    "docker": (("logs",), ("compose", "logs"), ("container", "logs"), ("service", "logs")),
    "podman": (("logs",),),
    "docker-compose": (("logs",),),
    "kubectl": (("logs",), ("describe",), ("get",)),
    "gh": (
        ("run", "view"),
        ("pr", "view"),
        ("pr", "diff"),
        ("pr", "checks"),
        ("issue", "view"),
        ("run", "list"),
        ("pr", "list"),
    ),
}
# echo / printf print their own words. A call made ONLY of them and of
# commands that print nothing (cd, export ...) reads data; one that also runs
# ``make`` does not (``make; echo "exit=$?"`` must still fire).
ECHOES = frozenset("echo printf".split())
SILENT = frozenset(
    "cd export set unset true false sleep pushd popd : umask local declare shopt trap wait".split()
)
_SSH_ARG_OPTS = frozenset("-b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w".split())


def _stage_reads(stage: str, remote: bool) -> bool:
    words = _command_words(stage)
    if not words:
        return False
    name, sub = _first_word(stage)
    if name in READERS or (name == "git" and sub in READER_GIT):
        return True
    subs = tuple(w for w in words[1:] if not w.startswith("-"))
    if any(subs[: len(t)] == t for t in READER_SUBS.get(name, ())):
        return True
    if name == "ssh" and not remote:
        # ssh [options] host 'remote command': read the remote command.
        rest, i = words[1:], 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in _SSH_ARG_OPTS else 1
        tail = " ".join(rest[i + 1 :]).replace("'", " ").replace('"', " ")
        return bool(tail.strip()) and reads_data(tail, remote=True)
    return False


def reads_data(command: str, remote: bool = False) -> bool:
    """True when the first command of some statement reads data (``grep``,
    ``cat``, ``tail``, ``journalctl``, ``git log``, ``docker logs``, ``gh pr
    view``, ``ssh host 'grep ...'`` ...), or when the call is made only of
    ``echo``/``printf`` and commands that print nothing."""
    names = []
    for st in _statements(command or ""):
        first_stage = re.split(r"(?<!\|)\|(?!\|)", st)[0]
        if _stage_reads(first_stage, remote):
            return True
        names.append(_first_word(first_stage)[0])
    names = [n for n in names if n]
    return (
        bool(names)
        and any(n in ECHOES for n in names)
        and all(n in ECHOES or n in SILENT for n in names)
    )


_PYTEST_E = re.compile(r"^E\s+(.*)$")
_PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR) \S+(?: - (.*))?$")
_ASSERTION = re.compile(r"^(?:AssertionError\b|assert\b|Failed: )")


def _pytest_line(x: str) -> Optional[str]:
    """A pytest ``E`` or ``FAILED``/``ERROR`` summary line -> the exception it
    names, or "" when the line is the test's own assertion (a failing test of
    the code under work: no memory holds that fix). None for any other line."""
    m = _PYTEST_E.match(x) or _PYTEST_SUMMARY.match(x)
    if not m:
        return None
    rest = (m.group(1) or "").strip()
    if (
        not rest
        or _ASSERTION.match(rest)
        or not (_PY_EXC.match(rest) or any(p.search(rest) for p in _STRONG))
    ):
        return ""
    return rest


def command_terms(command: str) -> str:
    """The tool names of a command: the first word of every pipeline stage,
    plus the git or docker subcommand (``git rev-parse``, ``docker exec``).
    Used to RANK hits (a ``No module named`` in a ``docker exec`` is a
    container lesson), never to gate them, and never logged or shown: the
    quote cut below can turn a quoted secret into a "tool name"."""
    out: List[str] = []
    # Quotes are cut points here, so the command inside `ssh h '...'` or
    # `bash -c "..."` is read too. Ranking only, so a rough cut is enough.
    for st in _statements(
        re.sub(r"['\"]", "\n", _strip_heredocs((command or "")[:COMMAND_MAX_CHARS]))
    ):
        for stage in re.split(r"(?<!\|)\|(?!\|)", st):
            name, sub = _first_word(stage)
            words = stage.split()
            if name in ("docker", "kubectl", "systemctl", "gh") and name in [
                os.path.basename(w) for w in words
            ]:
                i = [os.path.basename(w) for w in words].index(name)
                sub = next((w for w in words[i + 1 :] if not w.startswith("-")), "")
            for w in (name, sub):
                if w and re.fullmatch(r"[\w.-]{2,40}", w) and w not in out:
                    out.append(w)
    return " ".join(out[:12])


# A logic exception names a bug in the code that raised it. When the failing
# frame (the last one of the traceback) is the session's own code, a script,
# ``<stdin>`` or ``<string>``, no memory holds the fix: the WI-14 tune set
# (100 sessions) had 5 of 8 noise shows of this shape and no useful one. The
# same exception raised in a library frame is a misuse of that library
# (module_from_spec + @dataclass), and an environment exception
# (ModuleNotFoundError, PermissionError) from own code still fires.
LOGIC_EXCEPTIONS = frozenset(
    (
        "AttributeError TypeError KeyError ValueError NameError IndexError LookupError UnboundLocalError "
        "ZeroDivisionError AssertionError RecursionError StopIteration NotImplementedError"
    ).split()
)
_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+')
_LIBRARY_FRAME = re.compile(r"/(?:site|dist)-packages/|/lib/python\d[\d.]*/|^<frozen ")


def _own_code_bug(frames: List[str], exc_line: str) -> bool:
    """True when ``exc_line`` is a logic exception and the last traceback
    frame is not a library frame."""
    name = exc_line.split(":", 1)[0].strip().rsplit(".", 1)[-1]
    return name in LOGIC_EXCEPTIONS and bool(frames) and not _LIBRARY_FRAME.search(frames[-1])


def error_lines(text: str, failed: bool) -> List[str]:
    """The error lines of ``text``, at most ``MAX_ERROR_LINES``, most telling
    first: the exception line that ends a Python traceback (not a logic
    exception of own code, see ``LOGIC_EXCEPTIONS``), then strong lines, then
    (only when ``failed``) weak lines. Duplicates dropped."""
    lines = [_clean_line(x) for x in (text or "").splitlines()]
    lines = [x for x in lines if x and not _EXIT_LINE.match(x)]
    picked: List[str] = []

    def add(x: str) -> None:
        x = _cut(x, LINE_MAX_CHARS)
        if x not in picked:
            picked.append(x)

    tested, from_pytest = [], set()
    for x in lines:
        y = _pytest_line(x)
        if y is None:
            tested.append(x)
        elif y:
            tested.append(y)
            from_pytest.add(y)
    lines = tested
    dropped = set()
    for i, x in enumerate(lines):
        if x.startswith("Traceback (most recent call last)"):
            frames: List[str] = []
            for y in lines[i + 1 :]:
                m = _FRAME.match(y)
                if m:
                    frames.append(m.group(1))
                elif _PY_EXC.match(y):
                    if _own_code_bug(frames, y):
                        dropped.add(y)
                    else:
                        add(y)
                    break
    lines = [x for x in lines if x not in dropped]
    for x in lines:
        if x.startswith("Traceback (most recent call last)"):
            continue
        m = _GO_TEMPLATE.match(x)
        if m:
            add(m.group(1).strip("."))
            continue
        if x in from_pytest or any(p.search(x) for p in _STRONG):
            add(x)
    if failed and not picked:
        for x in lines:
            if not _GO_TEMPLATE.match(x) and any(p.search(x) for p in _WEAK):
                add(x)
    return picked[:MAX_ERROR_LINES]


def failure_of(event: Mapping[str, object]) -> Optional[Tuple[str, List[str]]]:
    """``(kind, error lines)`` for a failed Bash call, or None. ``kind`` is
    ``exit:<n>`` for a non-zero exit, ``output`` for an error line in the
    output of a call that exited 0."""
    if not isinstance(event, dict) or str(event.get("tool_name") or "") not in TOOLS:
        return None
    name = str(event.get("hook_event_name") or "")
    raw_ti = event.get("tool_input")
    ti: Dict[str, Any] = raw_ti if isinstance(raw_ti, dict) else {}
    raw_command = ti.get("command")
    command = raw_command if isinstance(raw_command, str) else ""
    if name == "PostToolUseFailure":
        if event.get("is_interrupt"):
            return None
        err = event.get("error")
        if not isinstance(err, str) or not err.strip():
            return None
        first = err.strip().splitlines()[0].strip()
        if first.startswith("Error: "):
            first = first[7:]
        m = _EXIT_LINE.match(first)
        kind = f"exit:{m.group(1)}" if m else "error"
        lines = error_lines(err, failed=True)
        return (kind, lines) if lines else None
    if name == "PostToolUse":
        tr = event.get("tool_response")
        if isinstance(tr, dict):
            if tr.get("interrupted"):
                return None
            text = "\n".join(str(tr.get(k) or "") for k in ("stderr", "stdout"))
        elif isinstance(tr, str):
            text = tr
        else:
            return None
        if reads_data(command):
            return None
        lines = error_lines(text, failed=False)
        return ("output", lines) if lines else None
    return None


# --------------------------------------------------------------------------
# query: clean and redact
# --------------------------------------------------------------------------
# WI-3f: the redaction sets and ``redact`` live in memory_text
# (shared with the guard rows and the prompt recall hook).
_REDACT = _MT._REDACT
_SECRET_PATTERNS = _MT._SECRET_PATTERNS
_SECRET_RX = _MT._SECRET_RX
_MEMORY_SECRET_RX = _MT._MEMORY_SECRET_RX
redact = _MT.redact


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I)
_ABS_PATH = re.compile(r"(?<![\w.~:/])(?:~|/[\w.+@-]+)(?:/[\w.+@-]+)+/?")


def _fold_path(m: re.Match[str]) -> str:
    parts = [p for p in m.group(0).split("/") if p]
    return "/".join(parts[-2:])


def query_of(lines: List[str]) -> str:
    """The search text: the error lines, secrets redacted, ids and long hex
    removed, absolute paths folded to their last two parts, at most
    ``QUERY_MAX_CHARS``. Redaction runs first, so a cut never leaves half a
    secret."""
    text = redact(" | ".join(lines))
    text = _UUID.sub("x", text)
    text = _HEX.sub("", text)
    text = _ABS_PATH.sub(_fold_path, text)
    text = re.sub(r"\s+", " ", text).strip()
    return _cut(text, QUERY_MAX_CHARS)


# --------------------------------------------------------------------------
# memory files
# --------------------------------------------------------------------------
_SKIP_FILES = frozenset({"MEMORY.md", "MEMORY_ARCHIVE.md"})
_TOKEN_RX = _MT._TOKEN_RX
STOPWORDS = _MT.STOPWORDS


def is_memory_file(name: str) -> bool:
    return (
        name.endswith(".md")
        and name not in _SKIP_FILES
        and not name.startswith("topic_")
        and not name.startswith("MEMORY")
    )


def read_memory(path: Path) -> Dict[str, str]:
    """``{id, rule, apply, description, body}`` of one memory file
    (``memory_text.read_memory``, with this hook's ``_mf``)."""
    return _MT.read_memory(path, lambda: _mf())


tokens = _MT.tokens


# --------------------------------------------------------------------------
# quote check: the memory must quote the error's message
# --------------------------------------------------------------------------
# WI-14 precision work (tune set: the 100 newest sessions). The useful shows
# were memories that QUOTE the error (`fatal: Needed a single revision`,
# `No module named 'daemon'`). The noise shared words with the error but did
# not quote its message: a path or a name in the error, or a generic message
# ("No such file or directory", "Error response from daemon"). So a hit is
# shown only when the memory holds a run of QUOTE_RUN consecutive words of the
# error's message (fewer when the message is shorter, never fewer than 2), and
# the run holds a word that is not in GENERIC_WORDS.
QUOTE_RUN = 3
GENERIC_WORDS = frozenset(
    (
        "fatal failed fail failure exit exited code status returned found such directory command permission "
        "denied access response daemon container unable invalid unknown option usage bash sh zsh dash "
        "warning panic exception traceback refused connection timed out"
    ).split()
)
# Leading words of a line that name the shell or the severity, not the message.
_PREFIX_WORDS = frozenset("fatal bash sh zsh dash warning panic".split())
_MSG_LINE_NO = re.compile(r"\bline \d+:")
_MSG_QUOTED = re.compile(r"'[^'\n]{0,80}'|\"[^\"\n]{0,80}\"")
_MSG_PATH = re.compile(r"(?<!\S)\S*/\S*|(?<!\S)[.~]\S+")


def _words(text: str) -> List[str]:
    return [t for t in tokens(text) if not t.isdigit()]


def message_words(line: str) -> List[str]:
    """The message of one error line as words: line numbers, quoted values
    and paths removed (they name THIS case, not the error), then the leading
    shell or severity words."""
    words = _words(_MSG_PATH.sub(" ", _MSG_QUOTED.sub(" ", _MSG_LINE_NO.sub(" ", line))))
    while words and words[0] in _PREFIX_WORDS:
        words = words[1:]
    return words


def quotes_error(mem: Mapping[str, object], query: str) -> bool:
    """True when the memory quotes the message of one of the query's error
    lines (see ``QUOTE_RUN``). The memory text is read as it is and with its
    quoted values and paths removed, so `'NoneType' ... '__dict__'` in the
    memory matches `'str' ... 'get'` in the error only through the words
    around the values."""
    text = " ".join(str(mem.get(k) or "") for k in ("description", "rule", "apply", "body"))
    seqs = (_words(text), _words(_MSG_PATH.sub(" ", _MSG_QUOTED.sub(" ", text))))
    for line in query.split(" | "):
        msg = message_words(line)
        n = min(QUOTE_RUN, len(msg))
        if n < 2:
            continue
        runs = {
            tuple(msg[i : i + n])
            for i in range(len(msg) - n + 1)
            if any(t not in GENERIC_WORDS for t in msg[i : i + n])
        }
        for seq in seqs:
            if runs and any(tuple(seq[i : i + n]) in runs for i in range(len(seq) - n + 1)):
                return True
    return False


class LocalIndex:
    """BM25 over the memory files. Built per call (716 files: about 0.1 s)."""

    K1 = 1.2
    B = 0.75

    def __init__(self, folder: Path):
        self.docs: List[Dict[str, str]] = []
        self.tf: List[Dict[str, int]] = []
        self.len: List[int] = []
        df: Dict[str, int] = {}
        for p in sorted(folder.glob("*.md")):
            if not is_memory_file(p.name):
                continue
            try:
                d = read_memory(p)
            except OSError:
                continue
            toks = tokens(
                " ".join(
                    (d["id"].replace("_", " "), d["description"], d["rule"], d["apply"], d["body"])
                )
            )
            tf: Dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
            self.docs.append(d)
            self.tf.append(tf)
            self.len.append(len(toks))
        n = len(self.docs)
        self.avg = (sum(self.len) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(
        self, query: str, k: int = MAX_HITS, context: str = ""
    ) -> List[Tuple[float, Dict[str, str]]]:
        """``[(coverage, memory)]``, ranked by BM25, best first. ``coverage``
        is the idf-weighted share of the query terms the memory holds, in
        [0, 1]; a query term that no memory holds counts with the highest
        idf, so an error whose key word no memory mentions is never "fully
        covered". The WI-14 replay chose coverage over normalized BM25 as the
        gate score: a known error scores 1.0 on its memory, while BM25 depends
        on term counts and document length."""
        allq = list(dict.fromkeys(tokens(query)))
        q = [t for t in allq if t in self.idf]
        if not q:
            return []
        unseen = math.log(1 + (len(self.docs) + 0.5) / 0.5)
        total = sum(self.idf.get(t, unseen) for t in allq)
        extra = [t for t in dict.fromkeys(tokens(context)) if t in self.idf and t not in q]
        scored = []
        for i, tf in enumerate(self.tf):
            s = cov = 0.0
            norm = self.K1 * (1 - self.B + self.B * self.len[i] / self.avg)
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.K1 + 1) / (f + norm)
                    cov += self.idf[t]
            if s > 0:
                for t in extra:  # the command's tool names rank, never gate
                    f = tf.get(t)
                    if f:
                        s += CONTEXT_WEIGHT * self.idf[t] * f * (self.K1 + 1) / (f + norm)
            if s > 0:
                scored.append((s, cov / total, self.docs[i]))
        scored.sort(key=lambda x: (-x[0], x[2]["id"]))
        return [(cov, d) for _s, cov, d in scored[:k]]


# --------------------------------------------------------------------------
# store search
# --------------------------------------------------------------------------
def store_search(
    query: str, env: Mapping[str, str], folder: Path, k: int = STORE_TOP_K
) -> List[Tuple[float, Dict[str, str]]]:
    """``[(score, memory)]`` from the store's ranked index, memory-file rows
    only, in the store's order. Raises on any failure, and on a keyword-mode
    answer (no cosine: design doc section 8.4), so the caller falls back to
    the local search. GET only; the store is read-only for this hook."""
    rh = _rh()
    project = rh.recall_project(env)
    root = os.path.basename(os.path.dirname(os.path.normpath(str(folder)))) or None
    timeout_s = _float_env(env, "NOBLIVION_ERROR_RECALL_TIMEOUT_S", STORE_TIMEOUT_S)
    payload = rh.store_get(
        lambda base: rh.index_url(base, query, int(k), project, root), env, timeout_s
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("bad_shape")
    if payload.get("mode") == "keyword":
        raise RuntimeError("keyword_mode")
    out: List[Tuple[float, Dict[str, str]]] = []
    seen = set()
    for row in payload["results"]:
        if not isinstance(row, dict):
            continue
        src = row.get("source")
        score = row.get("score")
        if (
            not isinstance(src, str)
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
        ):
            continue
        name = os.path.basename(src)
        if not is_memory_file(name) or name in seen:
            continue
        seen.add(name)
        path = folder / name
        try:
            mem = read_memory(path)
        except OSError:  # not in the local folder: use the store row
            mem = {
                "id": Path(name).stem,
                "rule": str(row.get("summary") or ""),
                "apply": "",
                "description": "",
                "body": "",
            }
        out.append((float(score), mem))
    return out


RRF_K = 60
MODES = ("local", "fused", "store")
DEFAULT_MODE = "local"


def fuse(
    store_rows: List[Tuple[float, Dict[str, str]]],
    local_rows: List[Tuple[float, Dict[str, str]]],
    store_floor: float,
    local_floor: float,
) -> List[Tuple[float, Dict[str, Any]]]:
    """Reciprocal rank fusion (k = ``RRF_K``) of the two ranked lists, by
    memory id. A memory is kept when its store score reaches
    ``store_floor`` OR its local score reaches ``local_floor``. Each kept
    memory is a copy with ``d_score`` and ``l_score`` (None when that list did
    not hold it). Best first."""
    table: Dict[str, Dict[str, Any]] = {}
    for key, rows in (("d_score", store_rows), ("l_score", local_rows)):
        for rank, (score, mem) in enumerate(rows, start=1):
            e = table.setdefault(
                mem["id"], {"mem": mem, "d_score": None, "l_score": None, "rrf": 0.0}
            )
            if e[key] is None:
                e[key] = score
                e["rrf"] = float(e["rrf"]) + 1.0 / (RRF_K + rank)
    kept = [
        e
        for e in table.values()
        if (e["d_score"] is not None and float(e["d_score"]) >= store_floor)
        or (e["l_score"] is not None and float(e["l_score"]) >= local_floor)
    ]
    kept.sort(key=lambda e: (-float(e["rrf"]), str(e["mem"]["id"])))
    return [
        (float(e["rrf"]), {**e["mem"], "d_score": e["d_score"], "l_score": e["l_score"]})
        for e in kept
    ]


def search(
    query: str, env: Mapping[str, str], folder: Path, context: str = ""
) -> Tuple[str, List[Tuple[float, Dict[str, Any]]], str]:
    """``(source, hits that pass a floor, store error)``.

    Mode (env ``NOBLIVION_ERROR_RECALL_MODE``):
      ``local`` (default)  local BM25 only, no network. The WI-14 replay of
                           the known errors measured it as the strongest list
                           on error text: top-2 79% (28 quoted errors) and 90%
                           (58 errors), against 61% / 66% for the store and
                           68% / 78% for the fusion of both.
      ``fused``            store and local lists, reciprocal rank fusion;
                           ``local`` when the store fails.
      ``store``           store only, local when it
                           fails. The store score did not separate right from
                           wrong hits in the replay (both 0.48-0.67).
    """
    mode = (env.get("NOBLIVION_ERROR_RECALL_MODE") or DEFAULT_MODE).strip().lower()
    mode = mode if mode in MODES else DEFAULT_MODE
    d_floor = _float_env(env, "NOBLIVION_ERROR_RECALL_MIN_SCORE", STORE_MIN_SCORE)
    l_floor = _float_env(env, "NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE", LOCAL_MIN_SCORE)
    store_rows: Optional[List[Tuple[float, Dict[str, str]]]] = None
    store_error = ""
    if mode != "local":
        try:
            store_rows = store_search(query, env, folder)
        except _TimeUp:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the local search
            store_error = f"{type(exc).__name__}: {exc}"[:120]
    if mode == "store" and store_rows is not None:
        return "store", fuse(store_rows, [], d_floor, l_floor), ""
    local_rows = LocalIndex(folder).search(query, k=STORE_TOP_K, context=context)
    if mode == "fused" and store_rows is not None:
        return "fused", fuse(store_rows, local_rows, d_floor, l_floor), ""
    return "local", fuse([], local_rows, d_floor, l_floor), store_error


# --------------------------------------------------------------------------
# state, log, output
# --------------------------------------------------------------------------
def agent_key(event: Mapping[str, object]) -> str:
    aid = event.get("agent_id") if isinstance(event, dict) else None
    return str(aid) if aid else "main"


def _state_file(env: Mapping[str, str], session_id: object, agent: str) -> Path:
    raw = f"{session_id or 'no-session'}\0{agent or 'main'}"
    return state_dir(env) / f"{STATE_PREFIX}{hashlib.sha256(raw.encode()).hexdigest()[:32]}.json"


def prune(folder: Path, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    n = 0
    for f in folder.glob(f"{STATE_PREFIX}*.json"):
        try:
            if now - f.stat().st_mtime > STATE_MAX_AGE_S:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


class State:
    """``{"shows": {id: n}, "chars": n}`` for one agent of one session, under
    an exclusive lock (parallel tool calls)."""

    def __init__(self, env: Mapping[str, str], session_id: object, agent: str):
        folder = state_dir(env)
        _make_dirs(folder)
        self.path = _state_file(env, session_id, agent)
        if not self.path.exists():
            prune(folder)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._fh = os.fdopen(fd, "r+", encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        try:
            data = json.loads(self._fh.read() or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        self.shows: Dict[str, int] = dict(data.get("shows") or {})
        self.chars = int(data.get("chars") or 0)

    def close(self, save: bool) -> None:
        try:
            if save:
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(json.dumps({"shows": self.shows, "chars": self.chars}))
                self._fh.flush()
        finally:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()


def log_line(env: Mapping[str, str], rec: Dict[str, object]) -> bool:
    rec = dict(rec)
    rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        p = log_path(env)
        _make_dirs(p.parent)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


redact_memory = _MT.redact_memory


def _short(text: str, n: int) -> str:
    try:
        text = _rh()._neutral(str(text or ""))
    except Exception:  # noqa: BLE001
        text = " ".join(str(text or "").split()).replace("<", "‹").replace(">", "›")
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


excerpt = _MT.excerpt
cut_at_sentence = _MT.cut_at_sentence
_SENTENCE_END = _MT._SENTENCE_END


def _neutral(text: str) -> str:
    return _rh()._neutral(text)


def _inert(text: str) -> str:
    """Memory text as one inert line (``memory_text.inert``)."""
    return _MT.inert(text, _neutral)


def render_hit(
    mem: Mapping[str, object], query: str = "", compact: bool = False, text_chars: int = TEXT_CHARS
) -> str:
    """One hit (``memory_text.render_hit``, the WI-14b shape)."""
    return _MT.render_hit(
        mem,
        query,
        compact,
        text_chars,
        subject="error",
        full_body_chars=FULL_BODY_CHARS,
        field_chars=FIELD_CHARS,
        excerpt_lines=EXCERPT_LINES,
        neutral=_neutral,
    )


def render(hits: List[Mapping[str, object]], query: str = "") -> str:
    """The header and the hits, under ``RENDER_MAX_CHARS``
    (``memory_text.render``). ``decide`` drops a hit that still
    breaks the bound."""
    return _MT.render(
        hits,
        query,
        header=HEADER,
        max_chars=RENDER_MAX_CHARS,
        text_chars=TEXT_CHARS,
        min_text_chars=MIN_TEXT_CHARS,
        render_one=render_hit,
    )


def _out(event_name: str, context: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}},
        ensure_ascii=False,
    )


def _round(x: object) -> Optional[float]:
    return round(float(x), 4) if isinstance(x, (int, float)) else None


def decide(
    event: Mapping[str, object], env: Mapping[str, str]
) -> Tuple[str, Optional[Dict[str, object]]]:
    """``(stdout text, log record)`` for one event. The budget is charged
    here, under the session lock, only when there is something to print."""
    t0 = time.time()
    fail = failure_of(event)
    if fail is None:
        return "", None
    kind, lines = fail
    query = query_of(lines)
    if len(tokens(query)) < 2:
        return "", None
    folder = memory_dir(env, event.get("cwd"))
    if not folder.is_dir():
        return "", {"decision": "error", "error": f"memory folder missing: {folder}"}
    raw_ti = event.get("tool_input")
    ti: Dict[str, Any] = raw_ti if isinstance(raw_ti, dict) else {}
    raw_command = ti.get("command")
    context = command_terms(raw_command if isinstance(raw_command, str) else "")
    source, hits, store_error = search(query, env, folder, context)
    # The command's words rank the hits and are never logged: a quoted value
    # in the command (a token, ``-p'<password>'``) would read as a tool name.
    rec: Dict[str, object] = {
        "event": str(event.get("hook_event_name") or ""),
        "kind": kind,
        "source": source,
        "query": query,
    }
    if store_error:
        rec["store_error"] = store_error
    if not hits:
        rec.update(decision="no-hit", ids=[], ms=int((time.time() - t0) * 1000))
        return "", rec
    quoted = [(s, m) for s, m in hits if quotes_error(m, query)]
    if not quoted:
        rec.update(
            decision="no-quote",
            ids=[m["id"] for _, m in hits[:MAX_HITS]],
            ms=int((time.time() - t0) * 1000),
        )
        return "", rec
    hits = quoted
    state = State(env, event.get("session_id"), agent_key(event))
    saved = False
    try:
        picked = [(s, m) for s, m in hits if state.shows.get(m["id"], 0) < SHOW_REPEAT][:MAX_HITS]
        cap = int(_float_env(env, "NOBLIVION_ERROR_RECALL_SESSION_CHARS", SESSION_CHARS))
        while picked and state.chars + len(render([m for _, m in picked], query)) > cap:
            picked = picked[:-1]
        while len(picked) > 1 and len(render([m for _, m in picked], query)) > RENDER_MAX_CHARS:
            picked = picked[:-1]
        if not picked:
            rec.update(
                decision="budget",
                ids=[m["id"] for _, m in hits[:MAX_HITS]],
                ms=int((time.time() - t0) * 1000),
            )
            return "", rec
        text = render([m for _, m in picked], query)
        state.chars += len(text)
        for _, m in picked:
            state.shows[m["id"]] = state.shows.get(m["id"], 0) + 1
        state.close(save=True)
        saved = True
    finally:
        if not saved:
            state.close(save=False)
    rec.update(
        decision="show",
        ids=[m["id"] for _, m in picked],
        scores=[[_round(m.get("d_score")), _round(m.get("l_score"))] for _, m in picked],
        chars=len(text),
        ms=int((time.time() - t0) * 1000),
    )
    return _out(str(event.get("hook_event_name") or "PostToolUse"), text), rec


def _alarm(_signum, _frame):
    raise _TimeUp()


def main(
    stdin=None,
    stdout=None,
    environ: Optional[Mapping[str, str]] = None,
    time_limit: float = TIME_LIMIT_S,
) -> int:
    """Read one event, print the context (or nothing), log. Exit 0 always."""
    import signal

    env = dict(os.environ if environ is None else environ)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    base: Dict[str, object] = {"session_id": "", "agent_id": "main"}
    armed = False
    try:
        try:
            signal.signal(signal.SIGALRM, _alarm)
            signal.setitimer(signal.ITIMER_REAL, time_limit)
            armed = True
        except (ValueError, OSError, AttributeError):
            armed = False
        event = json.loads(stdin.read())
        if isinstance(event, dict):
            base = {"session_id": str(event.get("session_id") or ""), "agent_id": agent_key(event)}
        out, rec = decide(event, env)
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            armed = False
        if out:
            stdout.write(out + "\n")
            stdout.flush()
        if rec:
            log_line(env, {**base, **rec})
    except BaseException as exc:  # noqa: BLE001 - fail open
        if armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            except Exception:  # noqa: BLE001, S110 - a hook fails open
                pass
            armed = False
        log_line(
            env,
            {
                **base,
                "decision": "error",
                "error": (
                    "timeout" if isinstance(exc, _TimeUp) else f"{type(exc).__name__}: {exc}"
                )[:300],
            },
        )
    finally:
        if armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            except Exception:  # noqa: BLE001, S110 - a hook fails open
                pass
    return 0


def _make_dirs(path: Any) -> None:
    """Make a state folder, but never the data dir itself: after an uninstall
    deleted it, a hook must not make it again (hook_config.make_dirs,
    NOBLIVION-28). Raises OSError."""
    _CFG.make_dirs(path)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001, S110 - a hook fails open
        pass
    try:
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook fails open
        pass
    os._exit(0)
