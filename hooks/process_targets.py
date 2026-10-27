# SPDX-License-Identifier: AGPL-3.0-or-later
"""The processes that a Bash command would signal (NOBLIVION-127).

A note can protect a process by its target, not by one spelling of the action:
``protects_process:`` is a regex over a process command line. Before a Bash
call, the guard hook asks this module which processes the command would
signal, and denies the call when the command line of one of them matches the
regex of a note.

The shapes that name targets:

  * ``kill [-SIG | -s SIG | -n N | --signal SIG] [--] PID...``: each PID that
    exists. A PID that does not exist is ignored. A negative PID is a process
    group: every member of the group. ``-1`` is every process. ``0`` (the
    group of the caller) is not resolved.
  * ``kill $(pgrep [opts] PAT)`` and ``kill `pgrep ...```: the processes that
    pgrep would print (a pipe after pgrep inside the substitution, such as
    ``| head -1``, is not followed: every match counts).
  * ``pgrep [opts] PAT | xargs [opts] kill``: the same, for a pipeline.
  * ``pkill [opts] PAT``: as pgrep. ``-f`` matches the full command line, else
    the process name (the 15 characters of ``/proc/PID/stat``); ``-x`` the
    whole text; ``-i`` any case; ``-v`` the processes that do not match;
    ``-P``, ``-g`` and ``-s`` filter by parent, group and session. Other
    filters (``-u``, ``-t``, ``-n``, ``-o``) are not applied, so more
    processes count, never fewer.
  * ``killall [opts] NAME...``: a process whose name, or the base name of its
    first argument, is NAME; ``-r`` takes NAME as a regex, ``-I`` any case,
    ``-g`` adds the group of each match.

The command is cut into simple commands by the lexer of the deny path
(``guard_table``): quoted data, comments and heredoc bodies are no command. A
command behind a wrapper (``sudo``, ``env``, ``timeout``, ``VAR=value``, ...)
counts, and so does a command inside ``bash -c '...'``, ``sh -c``, ``su -c``,
``eval`` and ``$( )``. A command inside ``ssh host '...'``, ``docker exec`` or
``kubectl exec`` runs on another machine or in another process table, so it is
left out.

Linux only: the table is ``/proc``. On another system ``ProcTable.system()``
is empty and the check finds nothing. The hook process itself is never a
target. Standard library only (hooks run on python 3.9).
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

EXEC_DEPTH = 3  # bash -c inside bash -c ...
_QUICK = re.compile(r"\b(?:kill|pkill|killall)\b")
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
_SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INT = re.compile(r"^-?[0-9]+$")
_SIGNAL = re.compile(r"^-(?:[0-9]+|[A-Z][A-Z0-9+-]+)$")  # -9, -KILL, -SIGTERM, -RTMIN+1
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
_SUB = "\x01"  # a substitution in a word: \x01<n>\x01


class Proc:
    """One process: ``pid``, ``ppid``, ``pgid``, ``sid``, ``name`` (the
    process name of ``/proc/PID/stat``) and ``argv`` (the words of
    ``/proc/PID/cmdline``)."""

    __slots__ = ("pid", "ppid", "pgid", "sid", "name", "argv")

    def __init__(
        self,
        pid: int,
        name: str,
        argv: Sequence[str] = (),
        ppid: int = 0,
        pgid: int = 0,
        sid: int = 0,
    ) -> None:
        self.pid, self.name, self.argv = int(pid), str(name), list(argv)
        self.ppid, self.pgid, self.sid = int(ppid), int(pgid), int(sid)

    @property
    def cmdline(self) -> str:
        """The command line as ``ps`` shows it, ``[name]`` when it is empty
        (a kernel thread, a zombie)."""
        return " ".join(self.argv) if self.argv else f"[{self.name}]"

    def __repr__(self) -> str:  # pragma: no cover - debug help
        return f"Proc({self.pid}, {self.cmdline!r})"


def _read(root: str, pid: int) -> Optional[Proc]:
    try:
        with open(f"{root}/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "replace")
        with open(f"{root}/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None  # gone, or not ours to read
    a, b = stat.find("("), stat.rfind(")")
    if a < 0 or b < a:
        return None
    rest = stat[b + 1 :].split()  # state ppid pgrp session ...
    try:
        ppid, pgid, sid = int(rest[1]), int(rest[2]), int(rest[3])
    except (IndexError, ValueError):
        ppid = pgid = sid = 0
    argv = [w.decode("utf-8", "replace") for w in raw.split(b"\0")]
    while argv and argv[-1] == "":
        argv.pop()
    return Proc(pid, stat[a + 1 : b], argv, ppid, pgid, sid)


class ProcTable:
    """A process table. ``ProcTable(procs)`` is a fixed table (the tests);
    ``ProcTable.system()`` reads ``/proc`` when it is asked, one PID at a
    time for ``kill PID`` and the whole table only for a pattern or a group."""

    def __init__(
        self, procs: Optional[Iterable[Proc]] = None, root: str = "/proc", skip: Iterable[int] = ()
    ) -> None:
        self._root = root
        self._skip = set(skip)
        self._all: Optional[Dict[int, Proc]] = (
            None if procs is None else {p.pid: p for p in procs if p.pid not in self._skip}
        )
        self._one: Dict[int, Optional[Proc]] = {}

    @classmethod
    def system(cls) -> ProcTable:
        """``/proc`` without the hook process; empty on a system without it."""
        if not sys.platform.startswith("linux") or not os.path.isdir("/proc/self"):
            return cls([])
        return cls(skip=(os.getpid(),))

    def get(self, pid: int) -> Optional[Proc]:
        if self._all is not None:
            return self._all.get(pid)
        if pid in self._skip or pid <= 0:
            return None
        if pid not in self._one:
            self._one[pid] = _read(self._root, pid)
        return self._one[pid]

    def all(self) -> List[Proc]:
        if self._all is None:
            procs: Dict[int, Proc] = {}
            try:
                names = os.listdir(self._root)
            except OSError:
                names = []
            for name in names:
                if name.isdigit() and int(name) not in self._skip:
                    p = _read(self._root, int(name))
                    if p is not None:
                        procs[p.pid] = p
            self._all = procs
        return list(self._all.values())

    def group(self, pgid: int) -> List[Proc]:
        return [p for p in self.all() if p.pgid == pgid]


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------
_GT_MOD: Any = None


def _gt() -> Any:
    """``guard_table``, the lexer of the deny path, loaded once from this
    folder. The guard hook sets ``_GT_MOD`` to its own copy."""
    global _GT_MOD
    if _GT_MOD is None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent / "guard_table.py"
        spec = importlib.util.spec_from_file_location("process_targets_guard_table", path)
        if spec is None or spec.loader is None:
            raise ImportError("guard_table")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GT_MOD = mod
    return _GT_MOD


def may_signal(command: str) -> bool:
    """A cheap first test: the command holds ``kill``, ``pkill`` or
    ``killall`` as a word."""
    return isinstance(command, str) and bool(_QUICK.search(command))


def _close(text: str, i: int) -> int:
    """The index of the ``)`` that closes the ``$(`` whose body starts at
    ``i``, or -1."""
    depth, sq, dq, n = 1, False, False, len(text)
    while i < n:
        c = text[i]
        if sq:
            sq = c != "'"
        elif c == "\\":
            i += 1
        elif c == "'" and not dq:
            sq = True
        elif c == '"':
            dq = not dq
        elif c == "(" and not dq:
            depth += 1
        elif c == ")" and not dq:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _substitutions(text: str) -> Optional[Tuple[str, List[str]]]:
    """``text`` with each ``$( )`` and backtick substitution outside single
    quotes replaced by ``\\x01<n>\\x01``, and the bodies. ``None`` when one is
    not closed."""
    out: List[str] = []
    subs: List[str] = []
    i, n, sq, dq = 0, len(text), False, False
    while i < n:
        c = text[i]
        if sq:
            sq = c != "'"
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if c == "'" and not dq:
            sq = True
        elif c == '"':
            dq = not dq
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            j = _close(text, i + 2)
            if j < 0:
                return None
            subs.append(text[i + 2 : j])
            out.append(f"{_SUB}{len(subs) - 1}{_SUB}")
            i = j + 1
            continue
        elif c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                return None
            subs.append(text[i + 1 : j])
            out.append(f"{_SUB}{len(subs) - 1}{_SUB}")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), subs


class _Cmd:
    """One simple command: its words (quotes removed; a substitution is
    ``\\x01<n>\\x01``), the bodies of its substitutions, and whether the
    output of the command before it comes in on a pipe."""

    __slots__ = ("words", "subs", "piped")

    def __init__(self, words: List[str], subs: List[str], piped: bool) -> None:
        self.words, self.subs, self.piped = words, subs, piped


def commands(text: str) -> List[_Cmd]:
    """The simple commands of ``text`` in order, cut as the deny path cuts
    them (``guard_table.command_views`` and ``segments``)."""
    gt = _gt()
    masked, raw, _dead = gt.command_views(text)[0]
    out: List[_Cmd] = []
    pos = 0
    for seg in gt.segments(masked):
        at = masked.find(seg, pos)
        if at < 0:
            continue
        piped = bool(_PIPE.search(masked[pos:at])) and bool(out)
        pos = at + len(seg)
        got = _substitutions(raw[at:pos].replace(gt.MASK, ""))  # a comment is no word
        if got is None:
            continue
        body, subs = got
        try:
            words = shlex.split(body)
        except ValueError:
            continue  # an open quote: not a command this check can read
        out.append(_Cmd(words, subs, piped))
    return out


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


# Wrappers that run the next word as the command, with the options of each
# that take a value.
_WRAP_VALUE = {
    "sudo": frozenset("ugCDhpRrTtU"),
    "doas": frozenset("uC"),
    "env": frozenset("uCS"),
    "timeout": frozenset("sk"),
    "nice": frozenset("n"),
    "ionice": frozenset("cnpPu"),
    "stdbuf": frozenset("ioe"),
    "exec": frozenset("a"),
    "setsid": frozenset(),
    "nohup": frozenset(),
    "time": frozenset(),
    "command": frozenset(),
    "builtin": frozenset(),
}
_KEYWORDS = frozenset(("if", "then", "else", "elif", "while", "until", "do", "{", "!"))


def unwrap(words: Sequence[str]) -> List[str]:
    """``words`` without the leading wrappers and ``VAR=value`` words."""
    w = list(words)
    i = 0
    while i < len(w):
        word = w[i]
        if _ASSIGN.match(word) or word in _KEYWORDS:
            i += 1
            continue
        name = _base(word)
        if name not in _WRAP_VALUE:
            break
        i += 1
        if name == "command" and i < len(w) and w[i] in ("-v", "-V"):
            return []  # a lookup, not a run
        while i < len(w) and w[i].startswith("-") and w[i] != "-":
            opt = w[i]
            i += 1
            if opt == "--":
                break
            if not opt.startswith("--") and len(opt) == 2 and opt[1] in _WRAP_VALUE[name]:
                i += 1  # the value of the option
        if name == "env":
            while i < len(w) and _ASSIGN.match(w[i]):
                i += 1
        if name == "timeout" and i < len(w):
            i += 1  # the duration
    return w[i:]


def _restore(word: str, subs: Sequence[str]) -> str:
    """``word`` with each ``\\x01<n>\\x01`` put back as ``$( )``: the outer
    shell expands it before the inner shell reads the string."""
    return re.sub(
        _SUB + r"([0-9]+)" + _SUB,
        lambda m: "$(" + subs[int(m.group(1))] + ")" if int(m.group(1)) < len(subs) else "",
        word,
    )


def _sub_index(word: str) -> Optional[int]:
    if len(word) > 2 and word[0] == _SUB and word[-1] == _SUB and word[1:-1].isdigit():
        return int(word[1:-1])
    return None


# --------------------------------------------------------------------------
# pgrep, pkill and killall
# --------------------------------------------------------------------------
_PG_VALUE = frozenset("dgGPstuUFrq")  # short options of pgrep and pkill that take a value
_PG_LONG_VALUE = frozenset(
    (
        "--delimiter",
        "--pgroup",
        "--group",
        "--parent",
        "--session",
        "--terminal",
        "--euid",
        "--uid",
        "--pidfile",
        "--runstates",
        "--signal",
        "--queue",
        "--ns",
        "--nslist",
        "--env",
        "--cgroup",
    )
)
_PG_FILTER_LONG = {"--parent": "P", "--pgroup": "g", "--session": "s"}


def _ids(value: str) -> Optional[List[int]]:
    """``1,2,3`` as numbers; ``None`` when one is not a number (``$$``)."""
    parts = [x for x in value.split(",") if x]
    if not parts or not all(x.isdigit() for x in parts):
        return None
    return [int(x) for x in parts]


def pgrep_matches(args: Sequence[str], table: ProcTable, signals: bool = False) -> List[Proc]:
    """The processes ``pgrep <args>`` would print (``pkill <args>`` with
    ``signals``: a ``-SIG`` word is the signal). ``[]`` when a filter value is
    not known before the command runs (``-P $$``)."""
    full = exact = icase = inverse = False
    pat: Optional[str] = None
    filters: Dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            if i < len(args) and pat is None:
                pat = args[i]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _PG_LONG_VALUE and not eq:
                val = args[i] if i < len(args) else ""
                i += 1
            full = full or name == "--full"
            exact = exact or name == "--exact"
            icase = icase or name == "--ignore-case"
            inverse = inverse or name == "--inverse"
            if name in _PG_FILTER_LONG:
                filters[_PG_FILTER_LONG[name]] = val
            continue
        if a.startswith("-") and len(a) > 1:
            if signals and _SIGNAL.match(a):
                continue
            for j in range(1, len(a)):
                ch = a[j]
                if ch in _PG_VALUE:
                    val = a[j + 1 :]
                    if not val:
                        val = args[i] if i < len(args) else ""
                        i += 1
                    if ch in "Pgs":
                        filters[ch] = val
                    break
                full = full or ch == "f"
                exact = exact or ch == "x"
                icase = icase or ch == "i"
                inverse = inverse or ch == "v"
            continue
        if pat is None:
            pat = a
    if pat is None and not filters:
        return []  # pgrep needs a pattern or a filter
    want: Dict[str, List[int]] = {}
    for key, val in filters.items():
        got = _ids(val)
        if got is None or (key in "gs" and 0 in got):
            return []  # not known before the command runs
        want[key] = got
    rx = None
    if pat is not None:
        flags = re.IGNORECASE if icase else 0
        try:
            rx = re.compile(pat, flags)
        except re.error:  # an extended regex Python does not read: as text
            rx = re.compile(re.escape(pat), flags)
    out: List[Proc] = []
    for p in table.all():
        hit = True
        if rx is not None:
            subject = p.cmdline if full else p.name
            hit = bool(rx.fullmatch(subject) if exact else rx.search(subject))
        if inverse:
            hit = not hit
        if hit and "P" in want and p.ppid not in want["P"]:
            hit = False
        if hit and "g" in want and p.pgid not in want["g"]:
            hit = False
        if hit and "s" in want and p.sid not in want["s"]:
            hit = False
        if hit:
            out.append(p)
    return out


_KA_VALUE = frozenset("usoyZn")  # short options of killall that take a value
_KA_LONG_VALUE = frozenset(
    ("--user", "--signal", "--older-than", "--younger-than", "--context", "--ns")
)


def killall_matches(args: Sequence[str], table: ProcTable) -> List[Proc]:
    """The processes ``killall <args>`` would signal."""
    regex = icase = group = False
    names: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            names.extend(args[i:])
            break
        if a.startswith("--"):
            name, eq, _val = a.partition("=")
            if name in _KA_LONG_VALUE and not eq:
                i += 1
            if name in ("--list", "--version", "--help"):
                return []
            regex = regex or name == "--regexp"
            icase = icase or name == "--ignore-case"
            group = group or name == "--process-group"
            continue
        if a.startswith("-") and len(a) > 1:
            if _SIGNAL.match(a):
                continue
            for j in range(1, len(a)):
                ch = a[j]
                if ch in _KA_VALUE:
                    if j + 1 == len(a):
                        i += 1
                    break
                if ch in "lV":
                    return []
                regex = regex or ch == "r"
                icase = icase or ch == "I"
                group = group or ch == "g"
            continue
        names.append(a)
    out: Dict[int, Proc] = {}
    flags = re.IGNORECASE if icase else 0
    for name in names:
        rx = None
        if regex:
            try:
                rx = re.compile(name, flags)
            except re.error:
                rx = re.compile(re.escape(name), flags)
        for p in table.all():
            first = p.argv[0] if p.argv else ""
            if rx is not None:
                hit = bool(rx.search(p.name))
            elif "/" in name:
                hit = first == name
            elif icase:
                hit = name.lower() in (p.name.lower(), _base(first).lower())
            else:
                hit = name in (p.name, _base(first))
            if hit:
                out[p.pid] = p
                if group:
                    for q in table.group(p.pgid):
                        out[q.pid] = q
    return list(out.values())


# --------------------------------------------------------------------------
# kill
# --------------------------------------------------------------------------
_KILL_VALUE = frozenset(("-s", "-n", "--signal", "-q", "--queue"))


def _kill_targets(
    args: Sequence[str], subs: Sequence[str], table: ProcTable, depth: int
) -> List[Proc]:
    if args and (args[0] in ("-l", "-L", "--table") or args[0].startswith("--list")):
        return []
    out: List[Proc] = []
    i, first, opts = 0, True, True
    while i < len(args):
        a = args[i]
        i += 1
        if opts:
            if a == "--":
                opts = False
                continue
            if a in _KILL_VALUE:
                i += 1
                first = False
                continue
            if a.startswith("--"):
                first = False
                continue
            if first and a.startswith("-") and len(a) > 1 and _sub_index(a) is None:
                first = False  # the signal: -9, -KILL, -SIGTERM
                continue
        first = False
        k = _sub_index(a)
        if k is not None:
            if k < len(subs):
                out.extend(_pgrep_of(subs[k], table, depth + 1))
            continue
        if not _INT.match(a):
            continue  # a job (%1), a variable, a word this check cannot know
        n = int(a)
        if n > 0:
            p = table.get(n)
            if p is not None:
                out.append(p)
        elif n == -1:
            out.extend(table.all())
        elif n < -1:
            out.extend(table.group(-n))
    return out


def _pgrep_of(text: str, table: ProcTable, depth: int) -> List[Proc]:
    """The processes that the pgrep stages of ``text`` (the body of a
    substitution) would print."""
    if depth > EXEC_DEPTH:
        return []
    out: List[Proc] = []
    for c in commands(text):
        w = unwrap(c.words)
        if w and _base(w[0]) == "pgrep":
            out.extend(pgrep_matches(w[1:], table))
    return out


def _xargs_command(args: Sequence[str]) -> List[str]:
    """The command that ``xargs <args>`` runs."""
    value = frozenset("adEILnPs")
    long_value = frozenset(
        (
            "--arg-file",
            "--delimiter",
            "--max-args",
            "--max-procs",
            "--max-chars",
            "--max-lines",
            "--replace",
            "--eof",
            "--process-slot-var",
        )
    )
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        i += 1
        if a.startswith("--"):
            if "=" not in a and a in long_value:
                i += 1
        elif len(a) == 2 and a[1] in value:
            i += 1
    return list(args[i:])


def targets(command: str, table: ProcTable, depth: int = 0) -> List[Proc]:
    """Every process that ``command`` would signal, each once, in the order
    found. ``[]`` when it signals none this check can name."""
    if depth > EXEC_DEPTH or not may_signal(command):
        return []
    found: Dict[int, Proc] = {}
    cmds = commands(command)
    for k, c in enumerate(cmds):
        for body in c.subs:  # a command inside $( ) runs too
            if may_signal(body):
                for p in targets(body, table, depth + 1):
                    found.setdefault(p.pid, p)
        w = unwrap(c.words)
        if not w:
            continue
        name = _base(w[0])
        got: List[Proc] = []
        if name == "kill":
            got = _kill_targets(w[1:], c.subs, table, depth)
        elif name == "pkill":
            got = pgrep_matches(w[1:], table, signals=True)
        elif name == "killall":
            got = killall_matches(w[1:], table)
        elif name == "xargs" and c.piped:
            run = unwrap(_xargs_command(w[1:]))
            if run and _base(run[0]) == "kill":
                j = k
                while j > 0 and cmds[j].piped:  # every pgrep stage of this pipeline
                    j -= 1
                    up = unwrap(cmds[j].words)
                    if up and _base(up[0]) == "pgrep":
                        got.extend(pgrep_matches(up[1:], table))
        elif name in _SHELLS or name == "su":
            for x, word in enumerate(w[1:-1], 1):
                if _SHELL_C.fullmatch(word):
                    got = targets(_restore(w[x + 1], c.subs), table, depth + 1)
                    break
        elif name == "eval":
            got = targets(_restore(" ".join(w[1:]), c.subs), table, depth + 1)
        for p in got:
            found.setdefault(p.pid, p)
    return list(found.values())
