#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stop-hook process checks and lesson capture. Work items WI-5 and WI-15.

Two modes, one file:

  (no args)            Stop hook. Reads the Stop event JSON on stdin
                       (``session_id``, ``transcript_path``, ``stop_hook_active``,
                       ``last_assistant_message``). Reads the tail of the transcript back
                       to the prompt that started this turn, runs the check table, and
                       blocks the stop ONCE with the unmet rules as the reason: it prints
                       ``{"decision": "block", "reason": ...}`` on stdout.
  --mark-correction    UserPromptSubmit hook. Reads ``prompt`` and ``session_id``. When the
                       prompt is a correction ("no, ...", "wrong", "I told you", ...) it
                       writes a per-session marker file; any other prompt removes the marker.

The check table (``CHECKS``). Each row has a checker that returns a reason or None:

  commit   a git worktree the session edited this turn has uncommitted changes.
           An edit to a shared checkout (config ``stop.shared_checkouts``) is reported,
           never a demand to commit there.
  tests    code files changed this turn and no test command ran after the last change.
  notify   the reply says deferred / skipped / cannot finish / blocked and no
           PushNotification tool call happened this turn.
  deploy   a merge command ran this turn, the reply says "merged", and no deploy check
           (a command that reads the deployed rev or health on a deploy host) ran this
           turn. Off until the config key ``stop.deploy_hosts`` names a host.
  lesson   (WI-15) the turn started with a correction prompt and no memory file under the
           memory folder was written this turn with ``rule:`` and ``apply:`` fields.

Every check runs on every stop. Only the checks in ``NOBLIVION_STOP_CHECKS`` (default
``DEFAULT_ON``) can block; the others are logged as ``shadow`` so the one-week gate has
data for them. ``NOBLIVION_STOP_CHECK_MODE=shadow`` logs and never blocks.

Bounds: when ``stop_hook_active`` is true it never blocks. Fail open: any exception or the
time limit (``TIME_LIMIT`` seconds) means exit 0 and no block. Standard library only.

Env (all optional; tests set them to tmp paths):
  NOBLIVION_STOP_CHECK_LOG     decision log, default <data dir>/stop-check-log.jsonl
  NOBLIVION_STOP_CHECK_STATE   marker folder, default <data dir>/stop-check-state
  NOBLIVION_MEMORY_DIR         memory folder, default the memory folder of the
                               home-folder project (~/.claude/projects/<slug>/memory)

Config file keys (hook_config; all optional):
  stop.shared_checkouts   list of checkout paths: an edit there is reported, never
                          a demand to commit there. Default: none.
  stop.worktree_prefixes  list of path prefixes (``~/wt-``): a path under
                          ``<prefix><name>/`` belongs to the worktree ``<prefix><name>``
                          even when that folder is gone (offline replay). Default: none.
  stop.test_command       the test command the ``tests`` reason names.
                          Default: ``python3 -m pytest -q <absolute test path>``.
  stop.deploy_hosts       list of host names or addresses a deploy check reads.
                          Default: none, and then the ``deploy`` check never fires.
  NOBLIVION_STOP_CHECKS        comma list of blocking checks, "all", or "none"
  NOBLIVION_STOP_CHECK_MODE    enforce (default) or shadow
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


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

HOME = Path.home()


def _paths(key: str) -> Tuple[str, ...]:
    return tuple(os.path.normpath(os.path.expanduser(p)) for p in _CFG.string_list(key))


# Shared checkouts: an edit there is reported, never a demand to commit there.
SHARED_CHECKOUTS: Tuple[str, ...] = _paths("stop.shared_checkouts")
# Worktree folders by prefix (``~/wt-`` -> ``~/wt-<name>``), for paths whose folder is gone.
WORKTREE_PREFIXES: Tuple[str, ...] = tuple(
    os.path.expanduser(p)
    for p in _CFG.string_list("stop.worktree_prefixes")
    if p.startswith(("/", "~"))
)
WT_RE = (
    re.compile("^(" + "|".join(re.escape(p) + r"[^/]+" for p in WORKTREE_PREFIXES) + ")(?:/|$)")
    if WORKTREE_PREFIXES
    else None
)
LIVE_MEMORY = _CFG.default_memory_dir()
DEFAULT_LOG = _CFG.data_dir() / "stop-check-log.jsonl"
DEFAULT_STATE = _CFG.data_dir() / "stop-check-state"
_TEST_COMMAND = _CFG.get("stop.test_command")
PYTEST = (
    _TEST_COMMAND.strip()
    if isinstance(_TEST_COMMAND, str) and _TEST_COMMAND.strip()
    else "python3 -m pytest -q <absolute test path>"
)
# Hosts a deploy check reads. None: the deploy check never fires.
DEPLOY_HOSTS: Tuple[str, ...] = _CFG.string_list("stop.deploy_hosts")

# Set from the offline replay over the last 40 sessions (P/wi5/RESULT-WI5.md).
DEFAULT_ON = ("commit", "tests", "notify", "deploy", "lesson")
TIME_LIMIT = 1.8  # seconds for the whole hook
TAIL_MAX = 24_000_000  # bytes of transcript read back at most
BLOCK = 1 << 18
GIT_TIMEOUT = 0.8
LOG_MAX = 5_000_000

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
CODE_EXT = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".swift",
    ".sh",
    ".bash",
}
SKIP_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/", "/proc/")


class Timeout(BaseException):
    """The time limit. Not an ``Exception``: no fail-open ``except Exception``
    may swallow the one-shot alarm (``main`` catches BaseException)."""


# ---------------------------------------------------------------- transcript


@dataclass
class Turn:
    prompt: str = ""
    reply: str = ""
    cwd: str = ""
    # (index, tool name, input dict) for every tool call that did not fail
    calls: List[Tuple[int, str, dict]] = field(default_factory=list)
    stop_feedback: bool = False  # a "Stop hook feedback" entry is inside this turn
    truncated: bool = False  # the tail limit was hit before the turn start


def _text_of(content) -> Tuple[str, bool]:
    """(joined text, has a tool_result block)."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    has_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    text = "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )
    return text, has_result


def entry_kind(row: dict) -> str:
    """prompt | feedback | result | assistant | other."""
    if row.get("isSidechain"):
        return "other"
    t = row.get("type")
    if t == "assistant":
        return "assistant"
    if t != "user" or row.get("isMeta"):
        return "other"
    text, has_result = _text_of((row.get("message") or {}).get("content"))
    if has_result:
        return "result"
    if text.startswith("Stop hook feedback"):
        return "feedback"
    return "prompt"


def tail_rows(path: str, max_bytes: int = TAIL_MAX, deadline: float = 0.0) -> Iterator[dict]:
    """User and assistant rows of a JSONL transcript, newest first."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = pos = fh.tell()
        buf = b""
        while pos > 0 and end - pos < max_bytes:
            if deadline and time.monotonic() > deadline:
                raise Timeout("transcript read")
            step = min(BLOCK, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            lines = buf.split(b"\n")
            buf = lines[0]
            for raw in reversed(lines[1:]):
                row = _parse(raw)
                if row is not None:
                    yield row
        if pos == 0 and buf:
            row = _parse(buf)
            if row is not None:
                yield row


def _parse(raw: bytes) -> Optional[dict]:
    if b'"assistant"' not in raw and b'"user"' not in raw:
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def turn_from_rows(rows_oldest_first: List[dict]) -> Turn:
    """Build a Turn from the rows after the prompt (the prompt row may be first)."""
    turn = Turn()
    errors: Set[str] = set()
    for row in rows_oldest_first:
        if entry_kind(row) == "result":
            for b in (row.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errors.add(str(b.get("tool_use_id")))
    idx = 0
    for row in rows_oldest_first:
        kind = entry_kind(row)
        if kind == "prompt":
            turn.prompt = _text_of((row.get("message") or {}).get("content"))[0]
            turn.cwd = row.get("cwd") or turn.cwd
            continue
        if kind == "feedback":
            turn.stop_feedback = True
            continue
        if kind != "assistant":
            continue
        turn.cwd = turn.cwd or row.get("cwd") or ""
        content = (row.get("message") or {}).get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                turn.reply = b["text"]
            elif b.get("type") == "tool_use":
                idx += 1
                if str(b.get("id")) in errors:
                    continue
                raw_inp = b.get("input")
                inp: Dict[str, Any] = dict(raw_inp) if isinstance(raw_inp, dict) else {}
                inp["_cwd"] = row.get("cwd") or turn.cwd
                turn.calls.append((idx, str(b.get("name") or ""), inp))
    return turn


def read_turn(path: str, deadline: float = 0.0, max_bytes: int = TAIL_MAX) -> Turn:
    """The current turn: rows back to (and including) the last prompt row."""
    rows: List[dict] = []
    found = False
    for row in tail_rows(path, max_bytes, deadline):
        rows.append(row)
        if entry_kind(row) == "prompt":
            found = True
            break
    rows.reverse()
    turn = turn_from_rows(rows)
    turn.truncated = not found
    return turn


# ---------------------------------------------------------------- paths


def repo_of(path: str, cwd: str = "") -> Optional[str]:
    """The git top level that holds ``path``, or None. Scratch and tmp paths are None."""
    if not path:
        return None
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    p = os.path.normpath(p)
    if p.startswith(SKIP_PREFIXES):
        return None
    d = p if os.path.isdir(p) else os.path.dirname(p)
    probe = d
    for _ in range(40):
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    m = WT_RE.match(p) if WT_RE is not None else None
    if m:
        return m.group(1)
    for shared in SHARED_CHECKOUTS:
        if p == shared or p.startswith(shared + "/"):
            return shared
    return None


def _abspath(path: str, cwd: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.normpath(p)


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(.*?)\n(.*?)\n\s*\2\s*(?=\n|$)", re.S)
ASSIGN_RE = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?([A-Za-z_]\w*)=(['\"]?)([^\s;'\"&|)]*)\2")
DIR_OPT_RE = re.compile(r"(?:\bgit\s+-C|\benv\s+-C|(?:^|\s)cd)\s+(['\"]?)(/[^\s'\";|&)]+)\1")
PATH_RE = re.compile(r"(?<![\w.-])(/(?:home|opt|srv|etc|usr)/[^\s'\";|&)<>`,]+)")
REDIRECT_RE = re.compile(r"(?<![0-9&<>])>{1,2}\|?\s*(['\"]?)(/[^\s'\";|&)]+)\1")
INPLACE_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:sed|perl)\s+(?:-\w+\s+)*-\w*i\w*\b|^\s*(?:sudo\s+)?(?:perl)\s+-\w*pi"
)
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?(['\"]?)(/[^\s'\";|&)]+)\1")
CPMV_RE = re.compile(r"^\s*(?:sudo\s+)?(?:cp|mv|install|rsync)\b(?!.*--dry-run)")
PY_WRITE_RE = re.compile(
    r"open\([^)]*,\s*['\"][wa]b?\+?['\"]|\.write_text\(|\.write_bytes\(|"
    r"shutil\.(?:copy\w*|move)\(|os\.replace\(|os\.rename\("
)
GIT_WRITE_RE = re.compile(
    r"\bgit\s+(?:-C\s+(\S+)\s+)?(?:-c\s+\S+\s+)*(?:apply(?!\s+(?:\S+\s+)*--(?:check|stat|numstat)\b)|"
    r"mv|rm|stash\s+pop|stash\s+apply)\b"
)
RESTORE_RE = r"(?:\bcp\s+(?:-\w+\s+)*\S*(?:/tmp/|\.bak|\.orig|\.keep|backup)\S*\s+\S*{name}\b|\bgit\s+(?:-C\s+\S+\s+)?(?:checkout\s+(?:-q\s+)?--|restore)\s[^;&|\n]*{name}\b)"
COMMIT_RE = re.compile(
    r"\bgit\s+(?:-C\s+(['\"]?)(\S+?)\1\s+)?(?:-c\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+)*commit\b"
)


REMOTE_RE = re.compile(
    r"""\b(?:ssh|scp|docker\s+(?:exec|run)|kubectl\s+exec)\b(?:[^'"\n;&|]|\\\n)*"""
    r"""(?:'(?:[^']|'\\'')*'|"(?:[^"\\]|\\.)*")""",
    re.S,
)
REMOTE_SEG_RE = re.compile(r"^\s*(?:timeout\s+\d+\s+)?(?:ssh|scp)\b")


def strip_remote(cmd: str) -> str:
    """Drop the quoted command a Bash line runs on another host or in a container:
    paths in it are not local files (``ssh example-host 'cd /srv/app && git apply'``)."""
    return REMOTE_RE.sub(" REMOTE ", cmd)


def expand(cmd: str) -> str:
    """Substitute simple ``NAME=value`` assignments and ``~`` in a command."""
    values: Dict[str, str] = {}
    for m in ASSIGN_RE.finditer(HEREDOC_RE.sub(" ", cmd)):
        if m.group(3) and "$(" not in m.group(3):
            values[m.group(1)] = m.group(3)
    if values:

        def sub(m: re.Match[str]) -> str:
            return values.get(m.group(1) or m.group(2), m.group(0))

        for _ in range(2):
            cmd = re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, cmd)
    return re.sub(r"(?<![\w/])~(?=/)", str(HOME), cmd)


def strip_heredocs(cmd: str) -> str:
    """The command with heredoc bodies removed (the ``<<TAG`` line itself is kept)."""
    return HEREDOC_RE.sub(lambda m: " " + m.group(3) + "\n", cmd)


def bash_segments(cmd: str) -> List[str]:
    """Simple commands of a command line: split on unquoted ; & | and newlines, heredocs out."""
    text = strip_heredocs(cmd)
    out, cur, quote, i = [], [], "", 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                cur.append(text[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\\" and i + 1 < len(text):
            cur.append(text[i : i + 2])
            i += 2
            continue
        elif ch in ";&|\n":
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [seg for seg in out if seg.strip()]


def _seg_dirs(cmd: str, cwd: str) -> Iterator[Tuple[str, str]]:
    """(segment, directory it runs in), following ``cd`` inside the command line."""
    base = cwd
    for seg in bash_segments(cmd):
        if REMOTE_SEG_RE.search(seg):
            continue
        dm = DIR_OPT_RE.search(seg)
        if dm and re.match(r"\s*(?:\()?\s*cd\b", seg):
            base = _abspath(dm.group(2), base)
            yield seg, base
            continue
        em = re.search(r"\b(?:git|env)\s+-C\s+(['\"]?)(/[^\s'\";|&)]+)\1", seg)
        yield seg, _abspath(em.group(2), base) if em else base


PY_LIT = r"'[^'\n]+'|\"[^\"\n]+\""
PY_TARGET_RES = (
    re.compile(r"open\(\s*(?P<x>\w+(?:\[\d\])?|" + PY_LIT + r")\s*,\s*['\"][wa]b?\+?['\"]"),
    re.compile(r"(?:Path\(\s*(?P<y>" + PY_LIT + r")\s*\)|(?P<x>\w+))\.write_(?:text|bytes)\("),
    re.compile(
        r"(?:os\.replace|os\.rename|shutil\.(?:copy\w*|move))\([^,()]+,\s*(?P<x>\w+|"
        + PY_LIT
        + r")\s*\)"
    ),
)


def _python_argv(cmd: str) -> List[str]:
    """argv[1:] of the first ``python ... -`` or ``python script.py`` call in ``cmd``."""
    m = re.search(
        r"\bpython[\d.]*\s+((?:-[A-Za-z]\S*\s+)*)(-|\S+\.py)\s+([^<|;&\n]*)", strip_heredocs(cmd)
    )
    if not m:
        return []
    return [a.strip("'\"") for a in re.findall(r"(?:'[^']*'|\"[^\"]*\"|\S+)", m.group(3))]


def py_write_targets(cmd: str) -> List[str]:
    """Paths that Python code inside a Bash command opens for writing, resolved through
    simple ``name = '/path'``, ``name = Path('/path')`` and ``name = sys.argv[N]`` assignments."""
    argv = _python_argv(cmd)
    out: List[str] = []

    def resolve(tok: str, depth: int = 0) -> Optional[str]:
        tok = tok.strip()
        if tok[:1] in "'\"":
            return tok.strip("'\"")
        am = re.fullmatch(r"sys\.argv\[(\d)\]", tok)
        if am:
            i = int(am.group(1)) - 1
            return argv[i] if 0 <= i < len(argv) else None
        if depth > 3 or not re.fullmatch(r"\w+", tok):
            return None
        vms = list(
            re.finditer(
                r"(?:^|[\s;(,])" + tok + r"\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?"
                r"(" + PY_LIT + r"|sys\.argv\[\d\]|\w+)",
                cmd,
            )
        )
        vm = vms[-1] if vms else None
        return resolve(vm.group(1), depth + 1) if vm else None

    for rx in PY_TARGET_RES:
        for m in rx.finditer(cmd):
            tok = m.groupdict().get("x") or m.groupdict().get("y") or ""
            p = resolve(tok) if tok else None
            if p and (p.startswith(("/", "~"))):
                out.append(os.path.expanduser(p))
    return out


def bash_write_paths(cmd: str, cwd: str) -> List[str]:
    """Absolute paths a Bash command writes. A trailing "/" means "somewhere in this repo"."""
    cmd = strip_remote(expand(cmd))
    out: List[str] = []
    for seg, where in _seg_dirs(cmd, cwd):
        for m in REDIRECT_RE.finditer(seg):
            out.append(m.group(2))
        for m in TEE_RE.finditer(seg):
            out.append(m.group(2))
        if INPLACE_RE.search(seg) or CPMV_RE.search(seg):
            args = [
                a for a in re.findall(r"(?:'[^']*'|\"[^\"]*\"|\S+)", seg) if not a.startswith("-")
            ]
            files = [a.strip("'\"") for a in args[1:]]
            if CPMV_RE.search(seg):
                files = files[-1:]
            out.extend(
                _abspath(f, where)
                for f in files
                if f.startswith("/") or (re.search(r"\.\w+$", f) and "/" in f)
            )
        g = GIT_WRITE_RE.search(seg)
        if g:
            out.append(_abspath((g.group(1) or where).strip("'\""), where) + "/")
    if PY_WRITE_RE.search(cmd) and re.search(r"\bpython[\d.]*\b", cmd):
        out.extend(_abspath(p, cwd) for p in py_write_targets(cmd))
    return [p for p in dict.fromkeys(out) if not p.startswith(("/dev/", "/proc/")) and "$" not in p]


COMMIT_WORD_RE = re.compile(r"(?<![-\w./])commit\b(?![-\w])")
NOT_COMMIT_RE = re.compile(r"--grep|\b(?:log|show|rev-parse|rev-list|diff|cat-file|merge-base)\b")


def commit_dirs(cmd: str, cwd: str) -> List[str]:
    """Directories a Bash command runs ``git commit`` in."""
    cmd = strip_remote(expand(cmd))
    out = []
    for seg, where in _seg_dirs(cmd, cwd):
        seg = re.sub(
            r"\"[^\"]*\"|'[^']*'",
            lambda q: " " if re.search(r"\s", q.group(0)) else q.group(0),
            seg,
        )  # quoted text with spaces is data, not a command
        gm = re.search(r"\bgit\b(.*)", seg)
        if not gm:
            continue
        rest = gm.group(1)
        if COMMIT_WORD_RE.search(rest) and not NOT_COMMIT_RE.search(rest.split("commit")[0]):
            cm = re.match(r"\s+-C\s+(['\"]?)(\S+?)\1(?:\s|$)", gm.group(1))
            out.append(_abspath(cm.group(2), where) if cm else where)
    return out


@dataclass
class Change:
    idx: int
    path: str  # absolute; a trailing "/" means "somewhere in this repo"
    repo: str
    via: str  # tool name or "Bash"


def call_cwd(turn: Turn, inp: dict) -> str:
    return str(inp.get("_cwd") or turn.cwd or "")


def changes_of(turn: Turn) -> List[Change]:
    out: List[Change] = []
    for idx, name, inp in turn.calls:
        cwd = call_cwd(turn, inp)
        if name in EDIT_TOOLS:
            raw = inp.get("file_path") or inp.get("notebook_path") or ""
            if isinstance(raw, str) and raw:
                ap = _abspath(raw, cwd)
                repo = repo_of(ap, cwd)
                if repo:
                    out.append(Change(idx, ap, repo, name))
        elif name == "Bash":
            for ap in bash_write_paths(str(inp.get("command") or ""), cwd):
                repo = repo_of(ap.rstrip("/") or "/", cwd)
                if repo:
                    out.append(Change(idx, ap, repo, "Bash"))
    return out


# ---------------------------------------------------------------- checkers


@dataclass
class Ctx:
    live: bool = True  # run git status (False in the offline replay)
    deadline: float = 0.0
    session: str = ""
    memory_dir: Path = LIVE_MEMORY
    state_dir: Path = DEFAULT_STATE
    marker: Optional[dict] = None  # the correction marker of this session, if any
    notes: Dict[str, object] = field(default_factory=dict)


def _git_dirty(repo: str, paths: List[str], deadline: float) -> Optional[List[str]]:
    """Porcelain lines, or None when git cannot answer in time."""
    left = deadline - time.monotonic() if deadline else GIT_TIMEOUT
    if left <= 0.05:
        return None
    args = ["git", "-C", repo, "status", "--porcelain"]
    if paths:
        args += ["--untracked-files=all", "--"] + paths
    else:
        args += ["--untracked-files=no"]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=min(GIT_TIMEOUT, left))
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def _proxy_reverted(ch: Change, turn: Turn) -> bool:
    """Replay only: a later (or the same) command restores the file from a backup or HEAD."""
    if ch.path.endswith("/"):
        return False
    rx = re.compile(RESTORE_RE.format(name=re.escape(os.path.basename(ch.path))))
    return any(
        i >= ch.idx and n == "Bash" and rx.search(str(inp.get("command") or ""))
        for i, n, inp in turn.calls
    )


def _ignored(repo: str, path: str) -> bool:
    """Replay only: the path is git-ignored in the repo as it is today."""
    if path.endswith("/") or not os.path.isdir(repo):
        return False
    try:
        return (
            subprocess.run(
                ["git", "-C", repo, "check-ignore", "-q", path], capture_output=True, timeout=2
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def check_commit(turn: Turn, ctx: Ctx) -> Optional[str]:
    by_repo: Dict[str, List[Change]] = {}
    for ch in changes_of(turn):
        by_repo.setdefault(ch.repo, []).append(ch)
    if not by_repo:
        return None
    commits: List[Tuple[int, str]] = []
    for idx, name, inp in turn.calls:
        if name == "Bash":
            cwd = call_cwd(turn, inp)
            for d in commit_dirs(str(inp.get("command") or ""), cwd):
                commits.append((idx, repo_of(d, cwd) or d))
    dirty, shared = [], []
    for repo, chs in sorted(by_repo.items())[:6]:
        files = sorted({c.path for c in chs if not c.path.endswith("/")})
        if ctx.live:
            lines = _git_dirty(repo, files, ctx.deadline)
            if lines is None or not lines:
                continue
            shown = [ln[3:] for ln in lines][:3]
        else:  # replay proxy: no commit in this repo after the last change
            chs = [c for c in chs if not _proxy_reverted(c, turn) and not _ignored(repo, c.path)]
            if not chs:
                continue
            files = sorted({c.path for c in chs if not c.path.endswith("/")})
            last = max(c.idx for c in chs)
            if any(i >= last and r == repo for i, r in commits):
                continue
            shown = [os.path.relpath(f, repo) for f in files][:3] or ["(Bash write)"]
        (shared if repo in SHARED_CHECKOUTS else dirty).append((repo, shown))
    reasons = []
    for repo, shown in dirty:
        reasons.append(
            f"Uncommitted changes in {repo} ({', '.join(shown)}), which you edited this "
            f"turn. Commit them locally now: `git -C {repo} add <files>` and "
            f"`git -C {repo} commit`."
        )
    for repo, shown in shared:
        reasons.append(
            f"You edited the shared checkout {repo} this turn ({', '.join(shown)}). "
            "Do not commit there. Say so in your reply, and move the change to a "
            "git worktree."
        )
    ctx.notes["commit"] = [r for r, _ in dirty + shared]
    return " ".join(reasons) or None


TEST_RE = re.compile(
    r"(?:^|[\s/;&|(])(?:pytest|py\.test)\b|-m\s+(?:pytest|unittest)\b|\btox\b|\bnox\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|\bnpx\s+(?:jest|vitest|mocha)\b|\b(?:jest|vitest)\b|"
    r"\bgo\s+test\b|\bcargo\s+test\b|\bmake\s+(?:test|check)\b|\bbats\b|\bctest\b|"
    r"\b(?:mvn|gradle|gradlew)\s+test\b|\brspec\b|\bphpunit\b|/test_[\w-]+\.py\b"
)


def _is_code(path: str) -> bool:
    return os.path.splitext(path.rstrip("/"))[1].lower() in CODE_EXT


def check_tests(turn: Turn, ctx: Ctx) -> Optional[str]:
    code = [c for c in changes_of(turn) if not c.path.endswith("/") and _is_code(c.path)]
    if not code:
        return None
    last = max(c.idx for c in code)
    for idx, name, inp in turn.calls:
        if idx >= last and name == "Bash" and TEST_RE.search(str(inp.get("command") or "")):
            return None
    files = sorted({os.path.basename(c.path) for c in code})
    ctx.notes["tests"] = files[:5]
    return (
        f"You changed code this turn ({', '.join(files[:4])}) and no test ran after the last "
        f"change. Run the tests for it (Python: `{PYTEST}`) and report the result."
    )


def prose_of(text: str) -> str:
    """The reply without code blocks, inline code, quotes and table rows."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"\"[^\"\n]{0,200}\"|“[^”\n]{0,200}”", " ", text)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(("|", ">")))


# First person only: the agent itself did not do an asked-for thing. A passive "the lane is
# skipped" or "the ticket stays blocked" describes other work and is not matched (replay v1 had
# 8 false of 10 with passive forms in).
NOT_DONE_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+|had\s+)?(?:deferred|skipped|parked|postponed|dropped)\b|"
    r"\b(?:i|we)\s+(?:did\s+not|didn't|could\s+not|couldn't|cannot|can\s?not|can't|was\s+unable\s+to|"
    r"am\s+unable\s+to|will\s+not\s+be\s+able\s+to)\s+(?:finish|complete|continue|proceed)\b|"
    r"\b(?:i\s+am|i'm|we\s+are)\s+(?:blocked|stuck)\b|"
    r"\bi\s+(?:stopped|am\s+stopping|stop)\s+(?:here|short|before)\b|"
    r"^\s*(?:[-*]\s*)?\**(?:deferred|skipped|not\s+done|not\s+finished|left\s+undone)\**\s*:",
    re.I | re.M,
)


def check_notify(turn: Turn, ctx: Ctx) -> Optional[str]:
    m = NOT_DONE_RE.search(prose_of(turn.reply))
    if not m:
        return None
    if any(name == "PushNotification" for _, name, _ in turn.calls):
        return None
    ctx.notes["notify"] = m.group(0)
    return (
        f'Your reply says work is not done ("{m.group(0)}") and no push notification went out '
        "this turn. Send one with the PushNotification tool: say what is not done and why."
    )


MERGED_RE = re.compile(r"\b(?:squash-)?merged\b", re.I)
NEG_RE = re.compile(
    r"\b(?:not|never|no|nothing|before|until|unless|if|once|when|awaiting|had\s+i|"
    r"wait(?:s|ing)?\s+(?:to\s+be|for)|target(?:\s+is)?|goal(?:\s+is)?)\W+(?:[\w#-]+\W+){0,3}$",
    re.I,
)
POST_NEG_RE = re.compile(r"\W*(?:nothing|no\s+\w+|yet\b|when\b|once\b|later\b)", re.I)
MERGE_SEG_RE = re.compile(
    r"^\s*(?:timeout\s+\d+\s+)?(?:bash\s+|sh\s+|python3?\s+)?(?:\S*/)?"
    r"(?:gh\s+pr\s+merge\b)"
)
DEPLOY_HOST_RE = (
    re.compile(
        r"(?<![\w.-])(?:" + "|".join(re.escape(h) for h in DEPLOY_HOSTS) + r")(?![\w-])", re.I
    )
    if DEPLOY_HOSTS
    else None
)
DEPLOY_PROBE_RE = re.compile(
    r"BUILD_SHA|docker\s+(?:inspect|ps|images|run|exec)\b|\bhealth|/version\b|"
    r"rev-parse|\bgit\s+log\b|\bcurl\b|deploy",
    re.I,
)
DEPLOY_RUN_RE = re.compile(r"\bgh\s+run\s+(?:list|view|watch)\b[^\n]*deploy", re.I)
DEPLOY_PENDING_RE = re.compile(
    r"\b(?:verify|check|confirm)\w*\s+(?:the\s+)?(?:deploy|rollout|live\s+rev)|"
    r"\bdeploy\w*\s+(?:is\s+|was\s+)?(?:not\s+(?:yet\s+)?(?:verified|checked|confirmed|live|done)|pending|unverified)|"
    r"\bnot\s+(?:yet\s+)?(?:live|deployed)\b|\b(?:tests?|docs?)[/\s-]+only\b|\bdoes\s+not\s+deploy\b",
    re.I,
)


def said_merged(reply: str) -> Optional[str]:
    text = prose_of(reply)
    for m in MERGED_RE.finditer(text):
        if not NEG_RE.search(text[max(0, m.start() - 40) : m.start()]) and not POST_NEG_RE.match(
            text[m.end() : m.end() + 30]
        ):
            return text[max(0, m.start() - 30) : m.end() + 10].strip()
    return None


def is_deploy_check(cmd: str) -> bool:
    cmd = strip_heredocs(cmd)
    on_host = DEPLOY_HOST_RE is not None and DEPLOY_HOST_RE.search(cmd)
    return bool((on_host and DEPLOY_PROBE_RE.search(cmd)) or DEPLOY_RUN_RE.search(cmd))


def merge_tail(cmd: str) -> Optional[str]:
    """The command text after the first merge command in ``cmd`` ("" when it is last), or None."""
    segs = bash_segments(expand(cmd))
    for k, seg in enumerate(segs):
        if MERGE_SEG_RE.search(seg) and "--help" not in seg and "--dry-run" not in seg:
            return "\n".join(segs[k + 1 :])
    return None


def check_deploy(turn: Turn, ctx: Ctx) -> Optional[str]:
    if not DEPLOY_HOSTS:
        return None
    last, tail = -1, ""
    for i, n, inp in turn.calls:
        if n == "Bash":
            rest = merge_tail(str(inp.get("command") or ""))
            if rest is not None:
                last, tail = i, rest
    if last < 0:
        return None
    phrase = said_merged(turn.reply)
    if not phrase or DEPLOY_PENDING_RE.search(prose_of(turn.reply)):
        return None
    if is_deploy_check(tail):
        return None
    if any(
        i > last and n in ("Bash", "Monitor") and is_deploy_check(str(inp.get("command") or ""))
        for i, n, inp in turn.calls
    ):
        return None
    ctx.notes["deploy"] = phrase
    return (
        'Your reply says "merged" and no deploy check ran after the merge this turn. A merge to '
        f"main deploys to {DEPLOY_HOSTS[0]}. Check the deployed rev on {DEPLOY_HOSTS[0]} against "
        "`git rev-parse origin/main`, or say that the deploy is not verified yet, or that the "
        "change touches only tests/ or docs/."
    )


# ---- WI-15 lesson capture

CORR_LEAD_RE = re.compile(
    r"^\W{0,3}(?:no|nope|incorrect)\s*(?:[,.!;:?—–-]|$)|^\W{0,3}wrong\b", re.I
)
CORR_NO_WORD_RE = re.compile(
    r"^\W{0,3}no\s+(?:that|this|it|you|not|wrong|stop|don'?t|do\s+not|"
    r"never|again|why|i\s+(?:said|told|asked|meant))\b",
    re.I,
)
CORR_ANY_RE = re.compile(
    r"\bi\s+(?:have\s+|'ve\s+)?(?:already\s+)?(?:told|asked)\s+you\b|\bhow\s+many\s+times\b|"
    r"\bdid(?:n'?t|\s+not)\s+i\s+(?:give|tell|ask|say)\b|"
    r"\bstop\s+(?:doing|asking|guessing|ignoring|inventing|claiming|making\s+up|stalling|evad\w*|"
    r"cancel+ing|pausing|stopping|waiting)\b|\bwe\s+stop\s+for\s+nothing\b|"
    r"\bwhy\s+(?:are|did|do)\s+you\s+(?:keep\s+)?(?:stop|ask|paus|wait|ignor|guess)\w*|"
    r"\b(?:you\s+(?:did|do|are\s+doing|made)|doing|did)\s+(?:it|this|that|the\s+same\s+\w+)\s+again\b|"
    r"\bnot\s+again\b|\bagain[?!]|"
    r"\byou\s+(?:ignored|forgot|broke)\b|\byou\s+did\s+not\s+(?:follow|read|listen|do\s+what)\b|"
    r"\b(?:that|this|it)(?:'s|\s+is|\s+was)\s+(?:wrong|incorrect|not\s+what\s+i)\b|"
    r"\byou(?:'re|\s+are|\s+were)\s+(?:\w+ing\s+)?(?:the\s+)?wrong\b|"
    r"\byou(?:'re|\s+are)\s+not\s+(?:communicating|spe\w*king|making\s+sense|listening|following|"
    r"doing\s+what|done)\b|"
    r"\bnot\s+what\s+i\s+(?:asked|said|meant|wanted)\b|\bwasting\s+my\s+time\b|"
    r"\bconsistent\s+error\b|\bwhat\s+the\s+(?:hell|fuck)\s+(?:are|were|did)\s+you\b|"
    r"\b(?:don'?t|do\s+not|never)\s+do\s+(?:that|this)\s+again\b",
    re.I,
)
CORR_CAPS_RE = re.compile(r"\bWRONG\b")
CORR_SCAN = 400


def is_correction(prompt: str) -> Optional[str]:
    """The matched phrase when ``prompt`` is an operator correction, else None."""
    p = (prompt or "").strip()
    if not p or p.startswith(("<", "Stop hook feedback", "[Request interrupted", "Caveat:")):
        return None
    for rx in (CORR_LEAD_RE, CORR_NO_WORD_RE):
        m = rx.search(p)
        if m:
            return m.group(0).strip()
    head = prose_of(p[:CORR_SCAN])
    m = CORR_ANY_RE.search(head) or CORR_CAPS_RE.search(head)
    return m.group(0) if m else None


_FIELDS_MOD = None


def _fields_mod():
    global _FIELDS_MOD
    if _FIELDS_MOD is None:
        _FIELDS_MOD = False
        path = Path(__file__).resolve().parent / "memory_fields.py"
        try:
            spec = importlib.util.spec_from_file_location("memory_fields", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules.setdefault(spec.name, mod)
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    if sys.modules.get(spec.name) is mod:
                        sys.modules.pop(spec.name, None)
                    raise
                _FIELDS_MOD = sys.modules.get(spec.name) or mod
        except Exception:
            _FIELDS_MOD = False
    return _FIELDS_MOD or None


def has_rule_apply(text: str) -> bool:
    """True when the memory text carries non-empty ``rule:`` and ``apply:`` (the WI-1 check)."""
    mod = _fields_mod()
    if mod is not None:
        try:
            fields = mod.read_fields(text)
            probs = mod.rule_apply_problems(fields, "feedback")
            return not any(p in ("rule is missing", "apply is missing") for p in probs)
        except Exception:  # noqa: S110 - a hook fails open
            pass
    fm = re.match(r"^---\n(.*?)\n---", text or "", re.S)
    if not fm:
        return False
    return bool(
        re.search(r"^rule:\s*\S", fm.group(1), re.M)
        and re.search(r"^apply:\s*\S", fm.group(1), re.M)
    )


def memory_writes(turn: Turn, memory_dir: Path) -> List[Tuple[str, str]]:
    """(path, text or "") for memory .md files this turn wrote with a tool call."""
    folder = os.path.normpath(str(memory_dir)) + "/"
    out = []
    for _, name, inp in turn.calls:
        paths: List[Tuple[str, str]] = []
        if name in EDIT_TOOLS:
            raw = str(inp.get("file_path") or "")
            paths.append(
                (
                    _abspath(raw, call_cwd(turn, inp)),
                    str(inp.get("content") or "") if name == "Write" else "",
                )
            )
        elif name == "Bash":
            paths += [
                (p, "")
                for p in bash_write_paths(str(inp.get("command") or ""), call_cwd(turn, inp))
            ]
        for p, text in paths:
            if p.startswith(folder) and p.endswith(".md"):
                out.append((p, text))
    return out


def lesson_captured(turn: Turn, ctx: Ctx) -> Optional[str]:
    """The memory path that captures the lesson, or None."""
    cands = memory_writes(turn, ctx.memory_dir)
    since = float((ctx.marker or {}).get("ts") or 0)
    if ctx.live and since:
        # A file changed since the prompt counts only when this turn's tool calls name it:
        # another session or a mirror job may touch the folder at the same time.
        said = "\n".join(json.dumps(inp, default=str) for _, _, inp in turn.calls)
        try:
            for entry in os.scandir(ctx.memory_dir):
                if (
                    entry.name.endswith(".md")
                    and entry.stat().st_mtime >= since - 1
                    and entry.name[:-3] in said
                ):
                    cands.append((entry.path, ""))
        except OSError:
            pass
    for path, text in cands:
        name = os.path.basename(path)
        if name == "MEMORY.md" or name.startswith(("topic_", "MEMORY_")):
            continue
        body = ""
        if ctx.live or not text:
            try:
                body = Path(path).read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                body = ""
        if has_rule_apply(body) or has_rule_apply(text):
            return path
    return None


def check_lesson(turn: Turn, ctx: Ctx) -> Optional[str]:
    if not ctx.marker:
        return None
    got = lesson_captured(turn, ctx)
    if got:
        ctx.notes["lesson"] = "captured " + os.path.basename(got)
        return None
    snippet = str(ctx.marker.get("phrase") or "")[:60]
    ctx.notes["lesson"] = snippet
    return (
        f'The operator corrected you this turn ("{snippet}"). Save the lesson before you stop: '
        f"write or update a feedback memory in {ctx.memory_dir} with rule:, apply:, scope: and "
        "triggers: fields, and put its pointer in the matching topic_*.md file. If the correction "
        "holds no reusable lesson, say that in one line."
    )


@dataclass(frozen=True)
class Check:
    name: str
    rule: str
    fn: Callable[[Turn, Ctx], Optional[str]]


CHECKS: Tuple[Check, ...] = (
    Check("commit", "commit locally before you stop", check_commit),
    Check("tests", "run the tests after a code change", check_tests),
    Check("notify", "send a push notification when work is not done", check_notify),
    Check("deploy", "verify the deploy after a merge", check_deploy),
    Check("lesson", "save a correction as a memory with rule: and apply:", check_lesson),
)


def enabled_checks() -> Set[str]:
    raw = os.environ.get("NOBLIVION_STOP_CHECKS")
    if raw is None:
        return set(DEFAULT_ON)
    raw = raw.strip().lower()
    if raw in ("all", "*"):
        return {c.name for c in CHECKS}
    if raw in ("", "none", "off"):
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def run_checks(turn: Turn, ctx: Ctx) -> Dict[str, str]:
    """{check name: reason} for every check that fired. A checker fault is skipped."""
    fired: Dict[str, str] = {}
    for c in CHECKS:
        if ctx.deadline and time.monotonic() > ctx.deadline:
            break
        try:
            reason = c.fn(turn, ctx)
        except Timeout:
            raise
        except Exception as exc:  # one broken checker never blocks, never stops the others
            ctx.notes[c.name + "_error"] = type(exc).__name__
            continue
        if reason:
            fired[c.name] = reason
    return fired


# ---------------------------------------------------------------- state and log


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


def log_path() -> Path:
    return _env_path("NOBLIVION_STOP_CHECK_LOG", DEFAULT_LOG)


def state_dir() -> Path:
    return _env_path("NOBLIVION_STOP_CHECK_STATE", DEFAULT_STATE)


def memory_dir() -> Path:
    return _env_path("NOBLIVION_MEMORY_DIR", LIVE_MEMORY)


def _safe_id(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", session or "")[:64] or "nosession"


def marker_path(session: str) -> Path:
    return state_dir() / f"{_safe_id(session)}.correction.json"


def read_marker(session: str) -> Optional[dict]:
    try:
        data = json.loads(marker_path(session).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_marker(session: str) -> None:
    try:
        marker_path(session).unlink()
    except OSError:
        pass


def log(row: dict) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX:
            os.replace(path, str(path) + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}, default=str
                )
                + "\n"
            )
    except OSError:
        pass


# ---------------------------------------------------------------- hooks


def stop_hook(event: dict, t0: float) -> Optional[str]:
    """The block reason, or None. Logs every decision."""
    session = str(event.get("session_id") or "")
    base = {"event": "stop", "session": session[:8]}
    marker = read_marker(session)
    if event.get("stop_hook_active"):
        log({**base, "verdict": "pass-stop-hook-active", "marker": bool(marker)})
        return None
    path = str(event.get("transcript_path") or "")
    if not path or not os.path.isfile(path):
        log({**base, "verdict": "skip-no-transcript"})
        return None
    ctx = Ctx(
        live=True,
        deadline=t0 + TIME_LIMIT - 0.2,
        session=session,
        memory_dir=memory_dir(),
        state_dir=state_dir(),
        marker=marker,
    )
    turn = read_turn(path, deadline=ctx.deadline)
    last = str(event.get("last_assistant_message") or "").strip()
    if last:
        turn.reply = last
    fired = run_checks(turn, ctx)
    on = enabled_checks()
    mode = os.environ.get("NOBLIVION_STOP_CHECK_MODE", "enforce").strip().lower()
    blocking = [n for n in fired if n in on] if mode != "shadow" else []
    shadow = [n for n in fired if n not in blocking]
    if marker and ("lesson" in blocking or "lesson" not in fired):
        clear_marker(session)  # cleared after the block or after the capture
    ms = round((time.monotonic() - t0) * 1000)
    log(
        {
            **base,
            "verdict": "block" if blocking else "pass",
            "blocking": blocking,
            "shadow": shadow,
            "ms": ms,
            "calls": len(turn.calls),
            "truncated": turn.truncated,
            "notes": ctx.notes,
        }
    )
    if not blocking:
        return None
    by_name = {c.name: c for c in CHECKS}
    lines = [f"- {by_name[n].rule}: {fired[n]}" for n in blocking]
    return (
        "Stop checks: meet these rules before you stop, then send the "
        "corrected reply. This block happens once.\n" + "\n".join(lines)
    )


def mark_hook(event: dict) -> None:
    session = str(event.get("session_id") or "")
    phrase = is_correction(str(event.get("prompt") or ""))
    if not phrase:
        if marker_path(session).exists():
            clear_marker(session)
            log({"event": "prompt", "session": session[:8], "verdict": "marker-cleared"})
        return
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{_safe_id(session)}.tmp"
    # the matched phrase only: no prompt text stays on disk (nothing reads it)
    tmp.write_text(json.dumps({"ts": time.time(), "phrase": phrase}), encoding="utf-8")
    os.replace(tmp, marker_path(session))
    log({"event": "prompt", "session": session[:8], "verdict": "marked", "phrase": phrase})


def _alarm(signum, frame):
    raise Timeout("time limit")


def main(argv: List[str]) -> int:
    t0 = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, TIME_LIMIT)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            return 0
        if "--mark-correction" in argv:
            mark_hook(event)
            return 0
        reason = stop_hook(event, t0)
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
        return 0
    except BaseException as exc:  # fail open: never block on a fault
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            log(
                {
                    "event": "error",
                    "verdict": "fail-open",
                    "error": type(exc).__name__,
                    "ms": round((time.monotonic() - t0) * 1000),
                }
            )
        except BaseException:  # noqa: S110 - a hook fails open
            pass
        return 0
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except BaseException:  # noqa: S110 - a hook fails open
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
