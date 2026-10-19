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
      Put ``<flag>`` directly after the command prefix at the very start of the
      command. Only a pair of ``FLAG_TABLE`` (a prefix and a flag that change
      the form of the output and nothing else, so the exit status stays). The
      command must be one simple command: its arguments, then at most ``2>&1``
      and, for a test runner only (``TAIL_OK``), one ``| tail`` stage, because
      the summary of a quiet run is still at the end. Any other command line
      (``&&``, ``;``, another pipe, ``| head``, a redirect to a file, a heredoc,
      a substitution) is left alone, because a later stage may read the output
      and the flag would change what it reads.

The table holds flags that change the form of the output only. It holds no flag
that truncates (``-20``, ``-n``, ``| head``): a cap hides data, so it cannot be
a silent change, and a pipe form cannot be matched at all, because
``guard_table.match`` splits a command at every pipe and tests each segment, so
a ``violates`` regex cannot tell ``git log`` from the first segment of
``git log | head``.

A rewrite is never made when it is doubtful: the command has more than one
hit, the prefix is not at the start of the raw command (``cd x && ...``,
``VAR=1 ...``, ``bash -c '...'``, a heredoc), the flag is already there, or a
flag of the same kind but another value is there (``-v`` and ``-q``). The
hook then keeps the old behaviour (deny, or ask). The hook also checks the new
command against the whole guard table: it must hit no ``violates`` rule and, if
the memory has ``complies``, it must match that regex.

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


def _starts_with(command: str, prefix: Sequence[str]) -> int:
    """The end of the prefix in the raw ``command`` (the words separated by
    blanks, at its very start, ending at a blank or the end), or -1."""
    m = re.match(r"[ \t]+".join(re.escape(w) for w in prefix) + r"(?=\s|$)", command)
    return m.end() if m else -1


# The pairs after which a ``| tail`` stage is safe (the quiet output ends with
# the summary line, as the normal output does).
TAIL_OK = frozenset(k for k in FLAG_TABLE if k[1] == "-q")

# What may follow the arguments: nothing, or 2>&1, or one windowing stage.
_STAGE = re.compile(
    r"^(?P<args>[^;&|<>`$()\\#\n\r]*?)"
    r"(?:\s+2>&1)?"
    r"(?P<stage>\s*\|\s*tail(?:\s+-(?:n\s*)?[0-9]{1,4})?)?\s*$"
)


def _apply_flag(command: str, prefix: Tuple[str, ...], flag: str) -> Optional[str]:
    end = _starts_with(command, prefix)
    if end < 0:
        return None
    m = _STAGE.match(command)
    if not m:
        return None
    if m.group("stage") and (prefix, flag) not in TAIL_OK:
        return None
    toks = _tokens(m.group("args"))
    if toks is None or tuple(toks[: len(prefix)]) != prefix:
        return None
    rest = toks[len(prefix) :]
    if flag in rest or FLAG_TABLE[(prefix, flag)](rest):
        return None
    return command[:end] + " " + flag + command[end:]


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
