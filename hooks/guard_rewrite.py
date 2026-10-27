# SPDX-License-Identifier: AGPL-3.0-or-later
"""The closed set of command rewrites for the guard hook (NOBLIVION-122).

A feedback memory that carries ``violates:`` may also carry ``rewrite:``, one
JSON string in a fixed grammar. The guard hook runs a rewrite only when that
memory's ``violates`` regex hits, the hook switch is on and the rewrite
applies without doubt. There is no free-form text: the code below is the whole
list of what a rewrite can do, so a memory file can never make the hook run a
command of its own.

Grammar of the value of ``rewrite:`` (one form)

  ``add-flag <flag> after <prefix words>``
      Put ``<flag>`` directly after the command prefix. Only a pair of
      ``FLAG_TABLE`` (a prefix and a flag that change the form of the output
      and nothing else, so the exit status stays).

Where a rewrite acts (NOBLIVION-126). The command is split with the lexer of
the deny path (``guard_table.command_views`` and ``segments``), so the rewrite
sees the same simple commands as ``guard_table.match``:

  * Each simple command of a list or a pipeline that starts with the prefix is
    rewritten: ``cd x && pytest``, ``a; pytest``, ``pytest | tail``. Every other
    byte of the command stays as it was.
  * The leading wrappers of the deny path are skipped first (``env``,
    ``timeout <n>``, ``VAR=value``, ``sudo``, ``nice``, ``nohup``, ...).
  * ``python -m <prog>``, ``python3 -m <prog>`` and a path to a python binary
    (``.venv/bin/python -m <prog>``) count as ``<prog>``, in the command and in
    the prefix of the spec.
  * The arguments of a rewritten command are plain words, then at most
    ``2>&1``. A pipe after it is allowed only into one ``tail`` stage, for a
    test runner only (``TAIL_OK``), because the summary of a quiet run is still
    at the end, and that ``tail`` must not pipe on. Any other shape of a
    command that starts with the prefix (a redirect to a file, another pipe,
    ``| head``, a substitution, a variable, a command inside ``( )``, ``{ }``,
    ``if``, ``for``, ``while`` or ``case``) leaves the whole command alone,
    because a later reader may read the output and the flag would change what
    it reads.
  * Quoted data, heredoc bodies and comments are never rewritten. A command
    with a heredoc or a line continuation is left alone. A command inside
    ``bash -c '...'``, ``eval`` or ``ssh host '...'`` is quoted data here.

The table holds flags that change the form of the output only. It holds no flag
that truncates (``-20``, ``-n``, ``| head``): a cap hides data, so it cannot be
a silent change, and a pipe form cannot be matched at all, because
``guard_table.match`` splits a command at every pipe and tests each segment, so
a ``violates`` regex cannot tell ``git log`` from the first segment of
``git log | head``.

A rewrite is never made when it is doubtful: the command has more than one
hit, no simple command starts with the prefix, or a shape above leaves it
alone. A simple command that already holds the flag, or a flag of the same kind
but another value (``-v`` and ``-q``), is kept as it is. The hook then keeps
the old behaviour (deny, or ask) when nothing changed. The hook also checks the
new command against the whole guard table: it must hit no ``violates`` rule
and, if the memory has ``complies``, it must match that regex.

``apply(command, spec)`` returns the new command or ``None``. It is pure
and idempotent: ``apply(apply(c, s), s)`` is ``None``, because the new command
holds the flag. ``problems(...)`` is the write-time check the memory
fields hook runs.

Standard library only (hooks run on python 3.9).
"""

from __future__ import annotations

import re
import shlex
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_PYTEST_CONFLICT = re.compile(
    r"^(--quiet|--verbose|--collect-only|--co|--help|--version|-h|-[A-Za-z]*[qvh][A-Za-z]*)$"
)
_LOG_SHAPE = re.compile(
    r"^(--pretty.*|--format.*|--oneline|--graph|--stat.*|--shortstat|--numstat|--name-only|"
    r"--name-status|--patch|--raw|--cc|--summary|--abbrev-commit|--no-patch|-p|-u|-s)$"
)
_STATUS_SHAPE = re.compile(
    r"^(--short|--porcelain.*|--long|--verbose|--branch|--column|--no-column|"
    r"-[A-Za-z]*[svb][A-Za-z]*)$"
)


def _any(rx: re.Pattern) -> Callable[[Sequence[str]], bool]:
    return lambda toks: any(rx.match(t) for t in toks)


# (prefix words, flag) -> does one of the later words conflict with the flag.
# The table holds the flags that cut noise and keep what the caller reads: the
# failures and the summary of a test run, the subject line of a commit, the
# status code of a file. A flag that drops the result line of a command is not
# here (``git push -q`` hides the line that proves the push, ``git fetch -q``
# the refs fetched): the replay tool showed that callers read those lines. A new
# pair needs a replay on real commands first (tools/replay_guard_rewrite.py).
FLAG_TABLE: Dict[Tuple[Tuple[str, ...], str], Callable[[Sequence[str]], bool]] = {
    (("pytest",), "-q"): _any(_PYTEST_CONFLICT),
    (("python", "-m", "pytest"), "-q"): _any(_PYTEST_CONFLICT),
    (("python3", "-m", "pytest"), "-q"): _any(_PYTEST_CONFLICT),
    (("uv", "run", "pytest"), "-q"): _any(_PYTEST_CONFLICT),
    (("git", "log"), "--oneline"): _any(_LOG_SHAPE),
    (("git", "status"), "-sb"): _any(_STATUS_SHAPE),
}

_FLAG_SPEC = re.compile(
    r"^add-flag (-{1,2}[A-Za-z][A-Za-z-]*) after ([A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+){0,3})$"
)


def parse(spec: object) -> Tuple[str, object, str]:
    """``(kind, argument, problem)``. ``kind`` is ``"flag"`` and the argument is
    ``(prefix words, flag)``; ``problem`` is ``""`` when the spec is in the
    grammar and in the closed table."""
    if not isinstance(spec, str):
        return "", None, "rewrite is not a string"
    m = _FLAG_SPEC.match(spec)
    if m:
        key = (tuple(m.group(2).split(" ")), m.group(1))
        if key not in FLAG_TABLE:
            return "", None, "this flag after this command prefix is not in the table of safe pairs"
        return "flag", key, ""
    return "", None, "rewrite is not 'add-flag <flag> after <prefix>'"


def _tokens(text: str) -> Optional[List[str]]:
    try:
        return shlex.split(text)
    except ValueError:
        return None


# The pairs after which a ``| tail`` stage is safe (the quiet output ends with
# the summary line, as the normal output does).
TAIL_OK = frozenset(k for k in FLAG_TABLE if k[1] == "-q")

# The arguments of a rewritten command: plain words only. A variable, a
# substitution, a redirect or an escape could hide a flag or send the output
# to a later reader.
_PLAIN_ARGS = re.compile(r"^[^;&|<>`$()\\#\n\r]*$")
# The one stage that may read the output of a test runner.
_TAIL = re.compile(r"^tail(?:[ \t]+-(?:n[ \t]*)?[0-9]{1,4})?$")
# A pipe between two simple commands (``|`` or ``|&``, not ``||``).
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
# One shell word of the raw text: quoted parts, escapes and plain characters.
_WORD = re.compile(r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s'"\\])+""")
# A python launcher: ``python``, ``python3``, ``python3.12``, also by path.
_PYTHON = re.compile(r"(?:[A-Za-z0-9_.~+/-]*/)?python(?:[0-9]+(?:\.[0-9]+)*)?")
# The first word of a simple command that opens or closes a group or a
# compound command: the output of a command inside may go to a later reader.
_OPENS = frozenset(("if", "while", "until", "for", "case", "select", "{"))
_CLOSES = frozenset(("fi", "done", "esac", "}"))

_GT_MOD = None


def _gt():
    """``guard_table``, the lexer of the deny path, loaded once from this
    folder. The guard hook sets ``_GT_MOD`` to its own copy."""
    global _GT_MOD
    if _GT_MOD is None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent / "guard_table.py"
        spec = importlib.util.spec_from_file_location("guard_rewrite_guard_table", path)
        if spec is None or spec.loader is None:
            raise ImportError("guard_table")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GT_MOD = mod
    return _GT_MOD


def program(prefix: Sequence[str]) -> Tuple[str, ...]:
    """The program words of a prefix: ``python -m pytest`` and
    ``python3 -m pytest`` are ``pytest``; any other prefix is itself."""
    words = tuple(prefix)
    if len(words) >= 3 and _PYTHON.fullmatch(words[0]) and words[1] == "-m":
        return words[2:]
    return words


def _spans(command: str) -> Optional[Tuple[str, str, List[Tuple[int, int]]]]:
    """``(masked, raw, spans)``: the simple commands of the top level of
    ``command`` as ``(start, end)`` offsets in ``command``, cut by the lexer of
    the deny path. ``None`` when that view is not ``command`` byte for byte (a
    heredoc body or a line continuation was cut out)."""
    gt = _gt()
    if gt.without_heredoc_bodies(command) != command:
        return None
    masked, raw, _dead = gt.command_views(command)[0]
    if len(masked) != len(command):
        return None
    spans: List[Tuple[int, int]] = []
    pos = 0
    for seg in gt.segments(masked):
        at = masked.find(seg, pos)
        if at < 0:
            return None
        pos = at + len(seg)
        spans.append((at, pos))
    return masked, raw, spans


def _after_wrappers(text: str) -> int:
    """The offset in ``text`` (one simple command) after its leading wrappers,
    as ``guard_table._normalize`` strips them: a wrapper goes only when a word
    is left after it."""
    wrapper = _gt()._WRAPPER
    p = 0
    while True:
        m = wrapper.match(text[p:])
        if not m or not text[p + m.end() :].strip():
            return p
        p += m.end()


def _apply_flag(command: str, prefix: Tuple[str, ...], flag: str) -> Optional[str]:
    view = _spans(command)
    if view is None:
        return None
    masked, raw, spans = view
    gt = _gt()
    prog = program(prefix)
    conflict = FLAG_TABLE[(prefix, flag)]
    inserts: List[int] = []
    depth = 0
    for k, (at, end) in enumerate(spans):
        before = masked[spans[k - 1][1] if k else 0 : at]
        depth += before.count("(") - before.count(")")  # a bare ( ) between commands
        text = raw[at:end]
        words = [m for m in _WORD.finditer(text) if gt.MASK not in m.group(0)]  # no comment
        first = words[0].group(0) if words else ""
        if first in _OPENS:
            depth += 1
        inside = depth > 0
        if first in _CLOSES:
            depth -= 1
        start = _after_wrappers(text)
        cmd = [m for m in words if m.start() >= start]
        if len(cmd) >= 2 and _PYTHON.fullmatch(cmd[0].group(0)) and cmd[1].group(0) == "-m":
            cmd = cmd[2:]
        if tuple(m.group(0) for m in cmd[: len(prog)]) != prog:
            continue  # another command: kept byte for byte
        if inside:
            return None
        rest = cmd[len(prog) :]
        if rest and rest[-1].group(0) == "2>&1":
            rest = rest[:-1]
        args = text[rest[0].start() : rest[-1].end()] if rest else ""
        if not _PLAIN_ARGS.match(args):
            return None
        toks = _tokens(args)
        if toks is None:
            return None
        nxt = spans[k + 1][0] if k + 1 < len(spans) else len(command)
        if _PIPE.search(masked[end:nxt]):  # the output goes to the next command
            if (prefix, flag) not in TAIL_OK or k + 1 >= len(spans):
                return None
            t_at, t_end = spans[k + 1]
            after = spans[k + 2][0] if k + 2 < len(spans) else len(command)
            if not _TAIL.match(raw[t_at:t_end].strip()) or _PIPE.search(masked[t_end:after]):
                return None
        if flag in toks or conflict(toks):
            continue  # it holds the flag, or a flag of the same kind: kept as it is
        inserts.append(at + cmd[len(prog) - 1].end())
    new = command
    for pos in reversed(inserts):
        new = new[:pos] + " " + flag + new[pos:]
    return new


def apply(command: object, spec: object) -> Optional[str]:
    """The rewritten command, or ``None`` when ``spec`` is not valid, the
    command is not exactly the shape the rewrite knows, or the result would not
    differ from ``command``."""
    if not isinstance(command, str) or not command.strip() or len(command) > 20000:
        return None
    kind, arg, problem = parse(spec)
    if problem:
        return None
    if kind != "flag":
        return None
    prefix, flag = arg  # type: ignore[misc]
    new = _apply_flag(command, prefix, flag)
    return new if new and new != command else None


def problems(fields: Dict[str, object]) -> List[str]:
    """The write-time problems of the ``rewrite:`` field of one memory (the
    check that ``memory_fields.check_fields`` runs). The examples decide: the
    rewrite must change ``example_repeat`` into a command that the memory's
    ``violates`` regex no longer hits and its ``complies`` regex (if any) does
    hit; it must not apply to that new command (idempotence) nor to
    ``example_ok``."""
    spec = fields.get("rewrite") or ""
    if not spec:
        return []
    kind, _arg, problem = parse(spec)
    if problem:
        return [problem]
    pat = str(fields.get("violates") or "")
    if not pat:
        return ["rewrite is allowed only with violates"]
    rep = str(fields.get("example_repeat") or "")
    new = apply(rep, spec)
    if new is None:
        return ["rewrite does not apply to example_repeat"]
    out: List[str] = []
    try:
        if re.search(pat, new):
            out.append("the rewritten example_repeat still matches violates")
        cpat = str(fields.get("complies") or "")
        if cpat and not re.search(cpat, new):
            out.append("the rewritten example_repeat does not match complies")
    except re.error:
        return []  # a bad regex is reported by the violates check
    if apply(new, spec) is not None:
        out.append("rewrite is not idempotent: it applies to its own result")
    ok = str(fields.get("example_ok") or "")
    if ok and apply(ok, spec) is not None:
        out.append("rewrite applies to example_ok, so it would change a compliant command")
    return out
