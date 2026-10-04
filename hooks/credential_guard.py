#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Credential guard (candidate b): deny a tool call that would put
a git URL holding a credential into the model context.

Called by ``guard_hook.decide`` for PreToolUse events of the tools
Bash, Read and Grep, before the guard table is read. Pure functions plus local
reads of git config; no network, no daemon.

What counts as a credential URL (``credential_url``): a ``scheme://userinfo@``
URL whose userinfo holds a password (``user:secret@``) or is a token on its
own (a known token prefix, or 20+ characters with letters AND digits). A plain
user name (``git@``, ``https://alice@``) and the scp form ``git@host:o/r`` are
not credentials.

What the guard denies, and only when the probe finds a credential URL:

* Bash ``git remote -v`` / ``--verbose``, ``git remote show <name>``,
  ``git remote get-url``, ``git ls-remote`` with no remote named (it prints
  ``From <url>``): a credential in a ``remote.*.url`` / ``pushurl`` value or in
  a ``url.<base>.insteadOf`` base. The probe is
  ``git [-C dir] [--git-dir d] config --list --null``, run quietly; its output
  never leaves this module.
* Bash ``git config --list`` / ``list``, ``--get`` / ``--get-all`` / ``get`` /
  the implicit ``git config <key>``, ``--get-regexp`` / ``get --regexp``: the
  entries the command would print (key match, regex match on the key), with the
  same scope flags (``--global``, ``--file F`` ...). Bash ``git var -l``: it
  prints every config value.
* Bash ``git fetch`` / ``pull`` / ``push`` / ``ls-remote`` / ``submodule`` /
  ``remote update`` with a ``GIT_TRACE*`` or ``GIT_CURL_VERBOSE`` variable set
  (in the command or by an earlier ``export``): the trace prints the remote URL
  on stderr.
* Bash reads of a git config file (``cat``, ``head``, ``grep``, ``awk``, an
  interpreter one-liner that names ``.git/config`` ...), globs that match one,
  recursive ``grep -r`` / ``rg --hidden`` over a tree that holds one. The file is
  read here and scanned line by line. A ``grep`` / ``rg`` whose output cannot
  hold a credential line (``-c``, ``-l``, ``-q``, or a pattern that matches no
  credential line, context lines included) is allowed.
* The same file by another name: as stdin (``cat < .git/config``), a glob on
  any part of the path (``.g*/conf*``), a brace list (``.git/{config,HEAD}``),
  a variable that the command set before (``f=.git/config; cat $f``, a ``for``
  variable).
* A print command (``cat``, ``head``, ``tail``, ``less``, ``grep``, ``sed``,
  ``awk`` ...) whose file is set when the command runs: a variable with no
  known value, ``$( )``, backticks. The path cannot be resolved here, so the
  call is denied when a config file that git reads in that folder holds a
  credential URL and the fixed text around the variable fits its path
  (``$d/config`` fits, ``$HOME/notes.txt`` and ``docs/$name`` do not).
  ``$(mktemp)`` is a new file and never fits.
* Read of a git config file; Grep in ``content`` mode over a git config file or
  a tree that holds one, when the pattern can print a credential line.

Allowed: names-only ``git remote``; any revealing stage followed in the same
pipeline by a scrub ``sed`` (``sed 's#://[^@]*@#://#'``: an ``s`` command whose
regex consumes a run up to ``@``, or starts with ``.*``), ``wc``, or a counting
``grep -c|-q|-l``; ``sed`` with such a script reading the config file itself.
A clone with no credential URL: everything is allowed (the probe finds none).

Not covered (design limits): a path that a program builds (``python3 -c`` with
``os.path.join``), a file descriptor opened by an earlier ``exec``, a loop that
reads lines from an unresolved file, positional parameters (``$1``). The guard
reads the command text; it does not run it.

The deny reason never holds the URL, the user name or the secret, and there is
no override marker: a scrubbed form always exists. Every error fails open
(allow).
"""

from __future__ import annotations

import fnmatch
import itertools
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable, List, Optional, Tuple

ENV_SWITCH = "NOBLIVION_GUARD_CREDENTIAL"  # on unless off (hook_config.switch)
PROBE_TIMEOUT_S = 0.6
WALK_MAX_DIRS = (
    3000  # a recursive grep deeper or wider than this is a design limit (RESULT-V2-FU3 L6)
)
WALK_MAX_DEPTH = 6
GLOB_MAX = 200
EXPAND_MAX = 4096  # a word longer than this gets no brace list and no variable value put in
EXEC_DEPTH = 3
SCRUB = "sed 's#://[^@]*@#://#'"

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://([^/?#@\s\"'<>]+)@")
_TOKEN_PREFIX = re.compile(
    r"(?i)^(?:gh[pousr]_|github_pat_|glpat-|gldt-|x-access-token|x-token-auth|oauth2|"
    r"xox[abprs]-|sk-|ya29\.|AKIA)"
)
_TOKENISH = re.compile(r"(?=[^:]*\d)(?=[^:]*[A-Za-z])^[A-Za-z0-9_.~%+\-]{20,}$")


_HOOK_CONFIG: Any = None


def _hook_config() -> Any:
    """``hook_config.py`` from this file's folder, loaded once."""
    global _HOOK_CONFIG
    if _HOOK_CONFIG is None:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hook_config.py")
        spec = importlib.util.spec_from_file_location("hook_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _HOOK_CONFIG = mod
    return _HOOK_CONFIG


def guard_on(env: Mapping[str, str]) -> bool:
    """On unless ``NOBLIVION_GUARD_CREDENTIAL`` is off (0, false, no, off or empty)."""
    return bool(_hook_config().switch(ENV_SWITCH, True, env))


def credential_url(text: object) -> bool:
    """True when ``text`` holds a URL whose userinfo is a credential."""
    for m in _URL.finditer(str(text or "")):
        info = m.group(1)
        user, sep, secret = info.partition(":")
        if sep and secret:
            return True
        if _TOKEN_PREFIX.match(user) or _TOKENISH.match(user):
            return True
    return False


def scrub(text: str) -> str:
    """``text`` with every URL userinfo removed (for a log line)."""
    return re.sub(
        r"(?i)(\b[a-z][a-z0-9+.\-]*://)[^/?#@\s\"'<>]+@", r"\1[redacted]@", str(text or "")
    )


# --------------------------------------------------------------------------
# probes (their output never leaves this module)
# --------------------------------------------------------------------------
class Probe:
    """Cached probes for one decision."""

    def __init__(self) -> None:
        self._cfg: dict = {}
        self._files: dict = {}
        self.copies: dict = {}  # a copy made earlier in the same command -> the credential file it copies

    def config_entries(
        self, cwd: str, git_dir: Optional[str], scope: Sequence[str]
    ) -> List[Tuple[str, str]]:
        key = (cwd, git_dir, tuple(scope))
        if key in self._cfg:
            return self._cfg[key]
        argv = ["git"]
        if git_dir:
            argv += ["--git-dir", git_dir]
        argv += ["config", *scope, "--list", "--null"]
        out: List[Tuple[str, str]] = []
        try:
            r = subprocess.run(
                argv,
                cwd=cwd if os.path.isdir(cwd) else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_S,
                env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"),
            )
            if r.returncode == 0:
                for rec in r.stdout.decode("utf-8", "replace").split("\0"):
                    if rec:
                        k, _, v = rec.partition("\n")
                        out.append((k.lower(), v))
        except Exception:  # noqa: BLE001 - a probe fails open
            out = []
        self._cfg[key] = out
        return out

    def cred_configs(self, cwd: str) -> List[str]:
        """The config files git reads in ``cwd`` (``--show-origin``) that hold a credential URL."""
        key = ("origins", cwd)
        if key in self._cfg:
            return self._cfg[key]
        out: List[str] = []
        try:
            r = subprocess.run(
                ["git", "config", "--list", "--null", "--show-origin"],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_S,
                env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"),
            )
            recs = r.stdout.decode("utf-8", "replace").split("\0") if r.returncode == 0 else []
            for origin in dict.fromkeys(x[5:] for x in recs[0::2] if x.startswith("file:")):
                d = cwd  # git names the config of the clone relative to its top folder
                while not os.path.isfile(os.path.join(d, origin)) and os.path.dirname(d) != d:
                    d = os.path.dirname(d)
                path = os.path.normpath(os.path.join(d, origin))
                if is_git_config(path) and self.cred_lines(path):
                    out.append(path)
        except Exception:  # noqa: BLE001 - a probe fails open
            out = []
        self._cfg[key] = out
        return out

    def file_lines(self, path: str) -> List[str]:
        if path not in self._files:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    self._files[path] = fh.read(1 << 20).splitlines()
            except Exception:  # noqa: BLE001
                self._files[path] = []
        return self._files[path]

    def cred_lines(self, path: str) -> List[int]:
        return [i for i, line in enumerate(self.file_lines(path)) if credential_url(line)]


def _remote_samples(entries: Iterable[Tuple[str, str]]) -> List[str]:
    """The lines ``git remote -v`` / ``get-url`` would print for the credential URLs (in memory only)."""
    out: List[str] = []
    for k, v in entries:
        if k.startswith("remote.") and k.endswith((".url", ".pushurl")) and credential_url(v):
            out += [f"{k.split('.')[1]}\t{v} (fetch)", v]
        if (
            k.startswith("url.")
            and k.endswith((".insteadof", ".pushinsteadof"))
            and credential_url(k)
        ):
            base = k[4:].rsplit(".", 1)[0]
            out += [f"origin\t{base}owner/repo.git (fetch)", f"{base}owner/repo.git"]
    return out


def _remote_cred(entries: Iterable[Tuple[str, str]]) -> bool:
    for k, v in entries:
        if k.startswith("remote.") and k.endswith((".url", ".pushurl")) and credential_url(v):
            return True
        if (
            k.startswith("url.")
            and k.endswith((".insteadof", ".pushinsteadof"))
            and credential_url(k)
        ):
            return True
    return False


def is_git_dir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "HEAD")) and os.path.isdir(
        os.path.join(path, "objects")
    )


def is_git_config(path: str) -> bool:
    """A git config file, or a git credential store (``.git-credentials``,
    ``$XDG_CONFIG_HOME/git/credentials``): the files whose credential URLs
    the guard keeps out of the context."""
    if not os.path.isfile(path):
        return False
    path = os.path.realpath(path)  # a link to a config is classified by its target
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if base == ".git-credentials" or (base == "credentials" and parent == "git"):
        return True
    if base in (".gitconfig", "gitconfig") or (base == "config" and parent == "git"):
        return True  # global and system git config ($XDG_CONFIG_HOME/git/config, /etc/gitconfig)
    return base in ("config", "config.worktree") and is_git_dir(os.path.dirname(path))


def configs_under(root: str) -> List[str]:
    """Git config files and git credential stores in ``root`` (a file, a git dir
    or a tree), bounded walk."""
    if os.path.isfile(root):
        return [root] if is_git_config(root) else []
    if not os.path.isdir(root):
        return []
    found: List[str] = []
    seen = 0
    base = root.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(root):
        seen += 1
        if seen > WALK_MAX_DIRS:
            break
        for name in (".git-credentials", ".gitconfig", "gitconfig"):
            if name in files:
                found.append(os.path.join(d, name))
        if os.path.basename(d) == "git":
            found += [
                os.path.join(d, x)
                for x in ("credentials", "config")
                if x in files and not is_git_dir(d)
            ]
        if "config" in files and is_git_dir(d):
            found.append(os.path.join(d, "config"))
            dirs[:] = []  # a git dir holds no other config we read
            continue
        if d.count(os.sep) - base >= WALK_MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [
            x
            for x in dirs
            if x
            not in (
                "node_modules",
                "objects",
                "__pycache__",
                ".venv",
                "venv",
                ".cache",
                ".npm",
                ".mypy_cache",
                ".pytest_cache",
                ".tox",
            )
        ]
    return found


# --------------------------------------------------------------------------
# shell text
# --------------------------------------------------------------------------
_OPS = (
    "&&",
    "||",
    ";;",
    "|&",
    ">>",
    "<<<",
    "<<",
    ">&",
    "&>",
    ">|",
    "<&",
    ";",
    "&",
    "|",
    "(",
    ")",
    "<",
    ">",
)
_SEPS = frozenset(("&&", "||", ";;", ";", "&", "(", ")"))
_PIPES = frozenset(("|", "|&"))
_REDIR = frozenset((">>", "<<<", "<<", ">&", "&>", ">|", "<&", "<", ">"))
_PUNCT = set(";&|()<>")


LIVE = "\x01"  # stands for a ``$`` that starts ``$name``, ``${`` or ``$(`` outside single quotes
TICK = "\x02"  # stands for a backtick outside single quotes
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_{(")


def _prepare(command: str) -> str:
    """Unquoted newlines become ``;``; unquoted ``#`` comments are dropped. Outside single quotes, a ``$``
    that starts ``$name``, ``${`` or ``$(`` becomes LIVE and a backtick becomes TICK (``_expand`` reads them)."""
    out: List[str] = []
    q: Optional[str] = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if q != "'" and (c == "`" or (c == "$" and command[i + 1 : i + 2] in _NAME_START)):
            c = TICK if c == "`" else LIVE
        if q:
            out.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                out.append(command[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c == "\\" and i + 1 < n:
            out.append(c + command[i + 1])
            i += 1
        elif c in "'\"":
            q = c
            out.append(c)
        elif c == "\n":
            out.append(" ; ")
        elif c == "#" and (i == 0 or command[i - 1] in " \t;&|()"):
            while i < n and command[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _split_punct(tok: str) -> List[str]:
    out: List[str] = []
    while tok:
        for op in _OPS:
            if tok.startswith(op):
                out.append(op)
                tok = tok[len(op) :]
                break
        else:
            out.append(tok[0])
            tok = tok[1:]
    return out


def tokens(command: str) -> List[str]:
    text = _prepare(command)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        raw = list(lex)
    except ValueError:
        raw = text.replace(";", " ; ").replace("|", " | ").split()
    out: List[str] = []
    for t in raw:
        if t and set(t) <= _PUNCT:
            out.extend(_split_punct(t))
        else:
            out.append(t)
    return out


TO_STDERR = "\x00stdout-to-stderr"  # marker word: this stage copies stdout to stderr (``>&2``)
FROM_FILE = "\x00stdin-from:"  # marker word, the file name follows: this stage reads its stdin from a file (``<``)


def pipelines(toks: List[str]) -> List[List[List[str]]]:
    """``[[stage words, ...], ...]``: pipelines split on separators, stages on pipes,
    redirections (and a bare fd number before them) removed. A stage whose stdout
    goes to stderr (``>&2``, ``1>&2``, ``>/dev/stderr``) gets the word TO_STDERR:
    stderr reaches the model even when a scrub follows in the pipe. A stage that
    reads a file as stdin (``< file``) gets the word FROM_FILE plus the file name."""
    res: List[List[List[str]]] = []
    pipe: List[List[str]] = []
    stage: List[str] = []
    skip = False
    for k, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t in _REDIR:
            fd = stage.pop() if stage and stage[-1].isdigit() else ""
            target = toks[k + 1] if k + 1 < len(toks) else ""
            if fd in ("", "1") and (
                (t == ">&" and target == "2")
                or (t in (">", ">>", ">|") and target == "/dev/stderr")
            ):
                stage.append(TO_STDERR)
            if t == "<" and target and not set(target) <= _PUNCT:
                stage.append(FROM_FILE + target)
            skip = True
            continue
        if t in _PIPES:
            pipe.append(stage)
            stage = []
        elif t in _SEPS:
            pipe.append(stage)
            res.append([s for s in pipe if s])
            pipe, stage = [], []
        else:
            stage.append(t)
    pipe.append(stage)
    res.append([s for s in pipe if s])
    return [p for p in res if p]


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAP_ARGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": set(),
    "nohup": set(),
    "setsid": set(),
    "time": set(),
    "command": set(),
    "exec": set(),
    "ionice": {"-c", "-n"},
}


def unwrap(words: List[str]) -> Tuple[List[str], dict]:
    """Words without leading assignments and wrappers; the assignments seen."""
    assigns: dict = {}
    w = list(words)
    changed = True
    while w and changed:
        changed = False
        while w and _ASSIGN.match(w[0]):
            k, _, v = w[0].partition("=")
            assigns[k] = v
            w.pop(0)
            changed = True
        if w and os.path.basename(w[0]) in _WRAP_ARGS:
            name = os.path.basename(w.pop(0))
            takes = _WRAP_ARGS[name]
            while w and (w[0].startswith("-") or (name == "env" and _ASSIGN.match(w[0]))):
                if name == "env" and _ASSIGN.match(w[0]):
                    k, _, v = w[0].partition("=")
                    assigns[k] = v
                    w.pop(0)
                    continue
                opt = w.pop(0)
                if opt in takes and w:
                    w.pop(0)
            if name == "timeout" and w and re.match(r"^\d", w[0]):
                w.pop(0)
            changed = True
    return w, assigns


_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
_SAFE_FILE_CMDS = frozenset(
    (
        "ls",
        "stat",
        "test",
        "[",
        "[[",
        "file",
        "wc",
        "cp",
        "mv",
        "rm",
        "chmod",
        "chown",
        "touch",
        "du",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "realpath",
        "readlink",
        "dirname",
        "basename",
        "echo",
        "printf",
        "mkdir",
        "rmdir",
        "ln",
        "cd",
        "pushd",
        "find",
        "git",
        "true",
        "false",
        "diff3",
        "cmp",
    )
)
_GREPS = frozenset(("grep", "egrep", "fgrep", "rg", "zgrep"))


def _resolve(cwd: str, p: str) -> str:
    p = os.path.expanduser(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def _display(cwd: str, p: str) -> str:
    try:
        rel = os.path.relpath(p, cwd)
    except ValueError:
        return p
    return p if rel.startswith("..") else rel


_VAR = re.compile(LIVE + r"(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_NAME_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
_BRACE = re.compile(r"\{([^{},]*(?:,[^{},]*)+)\}")
#: ``$(mktemp ...)`` names a new file, so it is never a git config: it is read as NEW_FILE, a path that holds no file
_MKTEMP = re.compile(r"\$\(\s*mktemp(?:\s[^()`$;|&<>]*)?\)")
NEW_FILE = "/dev/null/mktemp"


def _open_parts(word: str) -> Tuple[str, str]:
    """``(head, tail)`` of a marked word: its fixed text before the first and after the last part that is
    set when the command runs. A ``$(``, ``${`` or backtick that the word does not close: no tail is known."""
    first = min(i for i in (word.find(LIVE), word.find(TICK)) if i >= 0)
    last = max(word.rfind(c) for c in (LIVE, TICK, ")", "}", "*", "?", "]"))
    tail = word[last + 1 :]
    if word[last] == LIVE:
        tail = "" if tail[:1] in ("(", "{") else tail.lstrip(_NAME_CHARS)
    if word.count(TICK) % 2:
        tail = ""
    head = word[:first]
    for ch in "*?[{":
        head = head.split(ch, 1)[0]
    return head, tail


class _Shell(dict):
    """The variables a command has set so far: their values, None when not known. ``room``: the characters
    their values may still add to the words (the bound for a hostile command)."""

    room = 4 * EXPAND_MAX


def _expand(words: Sequence[str], shell: _Shell) -> Tuple[List[str], List[str], dict, List[str]]:
    """One stage without the marks: ``(words, stdin files, open, alternatives)``. A variable that the command
    set before is replaced by its value. ``open`` maps each word that still holds a variable, ``$( )`` or
    backticks to its ``_open_parts``. A word with a ``for`` variable stays as it is, and ``alternatives``
    holds it once per value of the variable."""
    out: List[str] = []
    files: List[str] = []
    opened: dict = {}
    alts: List[str] = []
    for word in words:
        dest = out
        if word.startswith(FROM_FILE):
            word, dest = word[len(FROM_FILE) :], files
        parts: List[List[str]] = []
        pos = 0
        for m in _VAR.finditer(word) if LIVE in word and shell.room > 0 else ():
            vals = shell.get(m.group(1) or m.group(2))
            if vals:
                parts += [[word[pos : m.start()]], vals]
                pos = m.end()
        parts.append([word[pos:]])
        longest, count = sum(max(len(v) for v in p) for p in parts), 1
        for p in parts:
            count = min(count * len(p), GLOB_MAX + 1)
        cost = max(longest * count - len(word), 0)
        if len(parts) > 1 and (longest > EXPAND_MAX or cost > shell.room):
            parts = [[word]]  # too long: the word stays open
            if longest <= EXPAND_MAX:
                shell.room = 0  # the room is used up: no later word gets a value
        else:
            shell.room -= cost
        forms = ["".join(x) for x in itertools.islice(itertools.product(*parts), GLOB_MAX + 1)]
        plain = (forms[0] if len(forms) == 1 else word).replace(LIVE, "$").replace(TICK, "`")
        if LIVE in forms[0] or TICK in forms[0] or len(forms) > GLOB_MAX:
            opened[plain] = _open_parts(forms[0] if len(forms) == 1 else word)
        elif len(forms) > 1:
            alts += forms
        dest.append(plain)
    return out, files, opened, alts


def _braces(word: str) -> List[str]:
    """The words a brace list stands for (``a/{b,c}``: ``a/b`` and ``a/c``). At most GLOB_MAX words; a word
    longer than EXPAND_MAX stays as it is."""
    if "{" not in word or len(word) > EXPAND_MAX:
        return [word]
    done: List[str] = []
    todo = [word]
    while todo and len(done) + len(todo) <= GLOB_MAX:
        cur = todo.pop()
        m = _BRACE.search(cur)
        if m is None:
            done.append(cur)
        else:
            todo += [
                cur[: m.start()] + x + cur[m.end() :] for x in m.group(1).split(",")[:GLOB_MAX]
            ]
    return done + todo


# --------------------------------------------------------------------------
# scrub and safe sinks
# --------------------------------------------------------------------------
#: Lines a scrub must clean before a pipe stage counts as a scrub: credential URLs in the shapes git prints them
#: (``remote -v``, ``config --list``, a config file, a credential store), several hosts, ports, token as user.
SCRUB_SAMPLES = (
    "origin\thttps://git-user:zzMARKzz@github.com/o/r.git (fetch)",  # gitleaks:allow - a fake marker, not a credential
    "origin\thttps://zzMARKzzlongtokenpart000000000@gitlab.example.org:8443/g/sub/r.git (push)",  # gitleaks:allow - a fake marker, not a credential
    "\turl = https://x-access-token:zzMARKzz@h0st-9.example.invalid/o/r",  # gitleaks:allow - a fake marker, not a credential
    "remote.origin.url=http://u:zzMARKzz@10.0.0.5/r.git",  # gitleaks:allow - a fake marker, not a credential
    "https://u:zzMARKzz@github.com",  # gitleaks:allow - a fake marker, not a credential
)
SCRUB_MARK = "zzMARKzz"  # gitleaks:allow - a fake marker, not a credential
_POSIX_CLASSES = {
    "[:alnum:]": "a-zA-Z0-9",
    "[:alpha:]": "a-zA-Z",
    "[:digit:]": "0-9",
    "[:upper:]": "A-Z",
    "[:lower:]": "a-z",
    "[:space:]": r"\s",
    "[:blank:]": r" \t",
    "[:xdigit:]": "0-9A-Fa-f",
    "[:punct:]": r"!-/:-@\[-`{-~",
}


def _sed_s_commands(script: str) -> Optional[List[Tuple[str, str, str]]]:
    """``[(regex, replacement, flags), ...]`` when the script is only ``s`` commands (separated by ``;``, a newline
    or blanks), else None (any other command, or the flags ``w``/``e``/``m`` or a number, cannot be proven safe)."""
    out: List[Tuple[str, str, str]] = []
    i, n = 0, len(script)
    while i < n:
        if script[i] in " \t\n;":
            i += 1
            continue
        if script[i] != "s" or i + 1 >= n:
            return None
        delim = script[i + 1]
        if delim in "\\\n" or delim.isalnum():
            return None
        parts: List[str] = []
        j = i + 2
        cur: List[str] = []
        while j < n and len(parts) < 2:
            c = script[j]
            if c == "\\" and j + 1 < n:
                cur.append(script[j + 1] if script[j + 1] == delim else c + script[j + 1])
                j += 2
                continue
            if c == delim:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(c)
            j += 1
        if len(parts) < 2:
            return None
        k = j
        while k < n and script[k] not in " \t\n;}":
            k += 1
        flags = script[j:k]
        if not re.fullmatch(r"[gpiI]*", flags):
            return None
        out.append((parts[0], parts[1], flags))
        i = k
    return out or None


def _py_template(rep: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(rep):
        c = rep[i]
        if c == "\\" and i + 1 < len(rep):
            d = rep[i + 1]
            out.append(
                f"\\g<{d}>"
                if d.isdigit()
                else ("\n" if d == "n" else ("\t" if d == "t" else d.replace("\\", "\\\\")))
            )
            i += 2
            continue
        out.append("\\g<0>" if c == "&" else c.replace("\\", "\\\\"))
        i += 1
    return "".join(out)


def _sed_scripts(words: List[str]) -> List[str]:
    scripts: List[str] = []
    args = words[1:]
    explicit = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1])
            explicit = True
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
            explicit = True
        elif a.startswith("-e") and len(a) > 2:
            scripts.append(a[2:])
            explicit = True
        elif not a.startswith("-") and not explicit and not scripts:
            scripts.append(a)
        i += 1
    return scripts


def _secrets_of(line: str) -> List[str]:
    out = []
    for m in _URL.finditer(line):
        user, sep, secret = m.group(1).partition(":")
        out.append(secret if sep and secret else user)
    return out


def scrubs(words: List[str], samples: Optional[Sequence[str]] = None) -> bool:
    """True when ``sed`` with these arguments removes the secret from every sample line: the real credential
    lines of the call when known, else SCRUB_SAMPLES (several hosts and shapes). The ``s`` commands run in Python
    (BRE converted, POSIX classes mapped); anything that cannot be emulated is not a scrub."""
    real = [x for x in (samples or []) if _secrets_of(x)]
    lines = real or list(SCRUB_SAMPLES)
    secrets = {x: (_secrets_of(x) if real else [SCRUB_MARK]) for x in lines}
    if not words or os.path.basename(words[0]) != "sed":
        return False
    args = words[1:]
    if any(a.startswith(("-i", "--in-place", "--file=")) or a in ("-f", "--file") for a in args):
        return False
    ere = any(
        a in ("--regexp-extended",) or (re.fullmatch(r"-[a-zA-Z]+", a) and ("E" in a or "r" in a))
        for a in args
    )
    quiet = any(
        a in ("--quiet", "--silent")
        or (re.fullmatch(r"-[a-zA-Z]+", a) and "n" in a and not a.startswith("-e"))
        for a in args
    )
    cmds: List[Tuple[str, str, str]] = []
    for sc in _sed_scripts(words):
        got = _sed_s_commands(sc)
        if got is None:
            return False
        cmds += got
    if not cmds:
        return False
    compiled = []
    for rx, rep, flags in cmds:
        src = rx if ere else _bre_to_py(rx)
        for k, v in _POSIX_CLASSES.items():
            src = src.replace(k, v)
        try:
            compiled.append(
                (
                    re.compile(src, re.I if "i" in flags.lower() else 0),
                    _py_template(rep),
                    0 if "g" in flags else 1,
                    "p" in flags,
                )
            )
        except (re.error, IndexError):
            return False
    for line in lines:
        want_gone = secrets[line]
        printed = not quiet
        for rx, tmpl, count, pflag in compiled:
            try:
                line, hits = rx.subn(lambda m, t=tmpl: m.expand(t), line, count=count)
            except (re.error, IndexError):
                return False
            if hits and pflag:
                printed = True
        if printed and any(x in line for x in want_gone):
            return False
    return True


_PASS_THROUGH = frozenset(
    ("grep", "egrep", "fgrep", "head", "tail", "sort", "uniq", "cat")
)  # no transforms


def _scrubbed_after(pipe: List[List[str]], si: int, samples: Sequence[str] = ()) -> bool:
    """True when a safe sink follows stage ``si`` and nothing between them can show the raw stream: the stages
    in between are plain filters (no ``tee``, no copy to stderr), and stage ``si`` itself copies nothing to stderr."""
    if TO_STDERR in pipe[si]:
        return False
    for words in pipe[si + 1 :]:
        if _safe_sink([x for x in words if x != TO_STDERR], samples):
            return True
        w, _ = unwrap(words)
        if TO_STDERR in words or not w or os.path.basename(w[0]) not in _PASS_THROUGH:
            return False
        if os.path.basename(w[0]) in _GREPS and any(
            a == "--only-matching" or re.fullmatch(r"-[A-Za-z]*o[A-Za-z]*", a) for a in w[1:]
        ):
            return False  # grep -o cuts the line: the scrub no longer sees a whole URL
    return False


def _safe_sink(words: List[str], samples: Sequence[str] = ()) -> bool:
    w, _ = unwrap(words)
    if not w:
        return False
    name = os.path.basename(w[0])
    if scrubs(w, samples) or name == "wc":
        return True
    if name in ("grep", "egrep", "fgrep"):
        return any(
            re.match(r"^-[A-Za-z]*[cqlL]", a)
            or a
            in ("--count", "--quiet", "--silent", "--files-with-matches", "--files-without-match")
            for a in w[1:]
            if a.startswith("-")
        )
    return False


# --------------------------------------------------------------------------
# grep emulation over a config file
# --------------------------------------------------------------------------
def _bre_to_py(p: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            d = p[i + 1]
            out.append(d if d in "|(){}+?" else c + d)
            i += 2
            continue
        out.append("\\" + c if c in "|(){}+?" else c)
        i += 1
    return "".join(out)


def would_emit(
    lines: Sequence[str],
    cred: Sequence[int],
    patterns: Sequence[str],
    *,
    fixed: bool = False,
    basic: bool = False,
    icase: bool = False,
    invert: bool = False,
    word: bool = False,
    whole: bool = False,
    before: int = 0,
    after: int = 0,
    only: bool = False,
) -> bool:
    """True when a grep with these options can print a credential line. With
    ``only`` (``-o``) only the matched parts print: True when a match on a
    credential line overlaps the userinfo of its URL."""
    if not cred:
        return False
    if not patterns:
        return True
    rxs = []
    for p in patterns:
        src = re.escape(p) if fixed else (_bre_to_py(p) if basic else p)
        if word:
            src = r"\b(?:" + src + r")\b"
        if whole:
            src = r"^(?:" + src + r")$"
        try:
            rxs.append(re.compile(src, re.I if icase else 0))
        except re.error:
            return True  # unknown syntax: assume it can print
    if only and not invert:
        for c in cred:
            spans = [m.span(1) for m in _URL.finditer(lines[c])]
            for r in rxs:
                for m in r.finditer(lines[c]):
                    if any(m.start() < b and a < m.end() for a, b in spans):
                        return True
        return False
    hit = [i for i, line in enumerate(lines) if any(r.search(line) for r in rxs) != invert]
    return any(i - before <= c <= i + after for i in hit for c in cred)


_GREP_ARG_OPTS = frozenset(
    (
        "-e",
        "-f",
        "-m",
        "-A",
        "-B",
        "-C",
        "-d",
        "-D",
        "--regexp",
        "--file",
        "--max-count",
        "--after-context",
        "--before-context",
        "--context",
        "--include",
        "--exclude",
        "--exclude-dir",
        "-g",
        "--glob",
        "-t",
        "--type",
        "-T",
        "--type-not",
        "--color",
        "--colour",
        "-j",
        "--threads",
        "-M",
        "--max-columns",
        "--label",
        "--binary-files",
        "--devices",
        "--directories",
    )
)


def _grep_parse(words: List[str]) -> dict:
    name = os.path.basename(words[0])
    o = {
        "rg": name == "rg",
        "patterns": [],
        "paths": [],
        "fixed": name == "fgrep",
        "basic": name in ("grep", "zgrep"),
        "icase": False,
        "invert": False,
        "word": False,
        "whole": False,
        "only": False,
        "before": 0,
        "after": 0,
        "recursive": name == "rg",
        "hidden": False,
        "quiet": False,
        "pfile": False,
    }
    args = words[1:]
    i = 0
    endopts = False
    while i < len(args):
        a = args[i]
        if endopts or not a.startswith("-") or a == "-":
            (o["paths"] if (o["patterns"] or o["pfile"]) else o["patterns"]).append(a)
            i += 1
            continue
        if a == "--":
            endopts = True
            i += 1
            continue
        name_v = a.split("=", 1)
        if a.startswith("--"):
            opt, val = name_v[0], (name_v[1] if len(name_v) > 1 else None)
            if opt in _GREP_ARG_OPTS and val is None and i + 1 < len(args):
                val = args[i + 1]
                i += 1
            if opt == "--regexp":
                o["patterns"].append(val or "")
            elif opt == "--file":
                o["pfile"] = True
            elif opt in (
                "--count",
                "--quiet",
                "--silent",
                "--files-with-matches",
                "--files-without-match",
                "--count-matches",
            ):
                o["quiet"] = True
            elif opt == "--fixed-strings":
                o["fixed"] = True
            elif opt in ("--extended-regexp", "--perl-regexp"):
                o["basic"] = False
            elif opt == "--ignore-case":
                o["icase"] = True
            elif opt == "--invert-match":
                o["invert"] = True
            elif opt == "--word-regexp":
                o["word"] = True
            elif opt == "--line-regexp":
                o["whole"] = True
            elif opt == "--only-matching":
                o["only"] = True
            elif opt in ("--recursive", "--dereference-recursive"):
                o["recursive"] = True
            elif opt in ("--hidden", "--no-ignore", "--unrestricted"):
                o["hidden"] = True
            elif opt in ("--after-context", "--context"):
                o["after"] = max(o["after"], int(val) if val is not None and val.isdigit() else 99)
            if opt in ("--before-context", "--context"):
                o["before"] = max(
                    o["before"], int(val) if val is not None and val.isdigit() else 99
                )
            i += 1
            continue
        # short options, possibly bundled; an option with an argument takes the rest or the next word
        j = 1
        while j < len(a):
            c = a[j]
            if c in "efmABCdDgtTMj":
                val = a[j + 1 :] or (args[i + 1] if i + 1 < len(args) else "")
                if not a[j + 1 :]:
                    i += 1
                if c == "e":
                    o["patterns"].append(val)
                elif c == "f":
                    o["pfile"] = True
                elif c in "AC":
                    o["after"] = max(o["after"], int(val) if val.isdigit() else 99)
                if c in "BC":
                    o["before"] = max(o["before"], int(val) if val.isdigit() else 99)
                if c == "d" and val == "recurse":
                    o["recursive"] = True
                break
            if c in "clLq":
                o["quiet"] = True
            elif c == "F":
                o["fixed"] = True
            elif c in "EP":
                o["basic"] = False
            elif c == "i":
                o["icase"] = True
            elif c == "v":
                o["invert"] = True
            elif c == "w":
                o["word"] = True
            elif c == "x":
                o["whole"] = True
            elif c == "o":
                o["only"] = True
            elif c in "rR":
                o["recursive"] = True
            elif c == "u" and o["rg"]:
                o["hidden"] = o["hidden"] or a.count("u") >= 2
            elif c == "." and o["rg"]:
                o["hidden"] = True
            j += 1
        i += 1
    if o["rg"]:
        o["basic"] = False
    return o


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------
class Finding:
    """What a call would reveal. ``samples``: the credential lines in the shape the call prints them, used only
    in memory to test a later scrub stage (never printed, never logged)."""

    open_path = False  # the file is named by a variable, ``$( )`` or backticks: it is a guess

    def __init__(
        self, what: str, repo: str, cfg: str = "", samples: Sequence[str] = (), stderr: bool = False
    ) -> None:
        self.what, self.repo, self.cfg, self.samples = what, repo, cfg, list(samples)
        self.stderr = stderr  # the URL goes to stderr: no later pipe stage can scrub it


def _file_finding(probe: Probe, cwd: str, cfg: str, what: str) -> Finding:
    if not cfg.endswith(("config", "config.worktree")):
        return Finding(
            what.replace("git config file", "git credential store"),
            ".",
            _display(cwd, cfg),
            [probe.file_lines(cfg)[i] for i in probe.cred_lines(cfg)],
        )
    if not is_git_dir(os.path.dirname(cfg)):  # a global or system git config
        return Finding(
            what, ".", _display(cwd, cfg), [probe.file_lines(cfg)[i] for i in probe.cred_lines(cfg)]
        )
    git_dir = os.path.dirname(cfg)
    repo = os.path.dirname(git_dir) if os.path.basename(git_dir) == ".git" else git_dir
    return Finding(
        what,
        _display(cwd, repo),
        _display(cwd, cfg),
        [probe.file_lines(cfg)[i] for i in probe.cred_lines(cfg)],
    )


_HELPER_FILE = re.compile(r"--file(?:=|\s+)(\S+)")


def _credential_store_hit(
    probe: Probe,
    d: str,
    git_dir: Optional[str],
    entries: Sequence[Tuple[str, str]],
    store_args: Sequence[str],
) -> Optional[str]:
    """The credential store a ``git credential fill`` / ``credential-store get`` would read, when it holds a
    credential URL; ``"credential cache"`` when a cache helper is set (its content cannot be read here)."""
    files: List[str] = []
    for i, a in enumerate(store_args):
        if a == "--file" and i + 1 < len(store_args):
            files.append(_resolve(d, store_args[i + 1]))
        elif a.startswith("--file="):
            files.append(_resolve(d, a.split("=", 1)[1]))
    helpers = [
        v
        for k, v in entries
        if k == "credential.helper" or (k.startswith("credential.") and k.endswith(".helper"))
    ]
    for h in helpers:
        m = _HELPER_FILE.search(h)
        if m:
            files.append(_resolve(d, m.group(1)))
        if h.split()[:1] == ["cache"] or "credential-cache" in h:
            return "credential cache"
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    files += [os.path.expanduser("~/.git-credentials"), os.path.join(xdg, "git", "credentials")]
    for f in files:
        if os.path.isfile(f) and probe.cred_lines(f):
            return f
    return None


#: git builtins: git never lets an alias replace one, so any other word may be an alias.
_GIT_BUILTINS = frozenset(
    """
add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore checkout cherry cherry-pick
citool clean clone column commit commit-graph commit-tree config count-objects credential credential-cache
credential-store describe diff diff-files diff-index diff-tree difftool fast-export fast-import fetch fetch-pack
filter-branch for-each-ref format-patch fsck gc get-tar-commit-id grep gui hash-object help init instaweb
interpret-trailers log ls-files ls-remote ls-tree maintenance merge merge-base merge-file merge-tree mergetool mktag
mktree mv name-rev notes pack-objects prune pull push range-diff read-tree rebase reflog remote repack replace
request-pull rerere reset restore rev-list rev-parse revert rm send-email shortlog show show-branch show-ref
sparse-checkout stash status stripspace submodule switch symbolic-ref tag update-index update-ref var verify-commit
verify-pack verify-tag version whatchanged worktree write-tree lfs
""".split()
)


def _git_stage(
    w: List[str], cwd: str, assigns: dict, probe: Probe, depth: int = 0
) -> Optional[Finding]:
    cli_alias: dict = {}
    i = 1
    d = cwd
    git_dir = assigns.get("GIT_DIR")
    takes = {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--exec-path",
    }
    while i < len(w) and w[i].startswith("-"):
        a = w[i]
        if a == "-C" and i + 1 < len(w):
            d = _resolve(d, w[i + 1])
            i += 2
            continue
        if a.startswith("--git-dir="):
            git_dir = a.split("=", 1)[1]
        elif a in takes and i + 1 < len(w):
            if a == "--git-dir":
                git_dir = w[i + 1]
            if a == "-c" and w[i + 1].lower().startswith("alias."):
                k, _, val = w[i + 1].partition("=")
                cli_alias[k[6:].lower()] = val
            i += 2
            continue
        i += 1
    if i >= len(w):
        return None
    sub, rest = w[i], w[i + 1 :]
    if git_dir:
        git_dir = _resolve(d, git_dir)
    repo_disp = _display(cwd, d)
    if sub not in _GIT_BUILTINS and depth < EXEC_DEPTH:
        alias = cli_alias.get(sub.lower())
        if alias is None:
            alias = next(
                (v for k, v in probe.config_entries(d, git_dir, ()) if k == f"alias.{sub.lower()}"),
                None,
            )
        if alias:
            if alias.startswith("!"):
                return check_bash(
                    " ".join([alias[1:], *(shlex.quote(x) for x in rest)]), d, probe, depth + 1
                )
            try:
                words = shlex.split(alias)
            except ValueError:
                words = alias.split()
            return _git_stage([*w[:i], *words, *rest], cwd, assigns, probe, depth + 1)

    def entries(scope: Sequence[str] = ()) -> List[Tuple[str, str]]:
        return probe.config_entries(d, git_dir, scope)

    trace = any(
        (k.startswith("GIT_TRACE") or k == "GIT_CURL_VERBOSE")
        and v.lower() not in ("", "0", "false", "no", "off")
        for k, v in assigns.items()
    )
    talks = sub in ("fetch", "ls-remote", "pull", "push", "submodule") or (
        sub == "remote" and any(a in ("update", "prune") for a in rest)
    )
    if trace and talks and _remote_cred(entries()):
        # a trace variable makes git print the commands it runs, with the remote URL, on stderr
        return Finding(
            f"git {sub} with a trace variable",
            repo_disp,
            samples=_remote_samples(entries()),
            stderr=True,
        )
    if sub == "remote":
        verbose = any(re.fullmatch(r"-v+", a) or a == "--verbose" for a in rest)
        pos = [a for a in rest if not a.startswith("-")]
        reveal = verbose or (pos[:1] == ["get-url"]) or (pos[:1] == ["show"] and len(pos) > 1)
        if reveal and _remote_cred(entries()):
            return Finding("git remote", repo_disp, samples=_remote_samples(entries()))
        return None
    if sub == "ls-remote":
        pos = [a for a in rest if not a.startswith("-")]
        get_url = "--get-url" in rest
        if (
            get_url or (not pos and not any(a in ("-q", "--quiet") for a in rest))
        ) and _remote_cred(entries()):
            # without a remote named, ls-remote prints "From <url>" on stderr, which bypasses the pipe
            return Finding(
                "git ls-remote", repo_disp, samples=_remote_samples(entries()), stderr=not get_url
            )
        return None
    if sub in ("credential", "credential-store", "credential-cache"):
        pos = [a for a in rest if not a.startswith("-")]
        if (sub == "credential" and pos[:1] == ["fill"]) or (sub != "credential" and "get" in pos):
            store = _credential_store_hit(
                probe, d, git_dir, entries(), rest if sub == "credential-store" else ()
            )
            if store is not None:
                return Finding(
                    f"git {sub}",
                    repo_disp,
                    "" if store == "credential cache" else _display(cwd, store),
                )
        return None
    if sub == "var":
        # ``git var -l`` prints every config value, as ``git config --list`` does
        shown = [f"{k}={v}" for k, v in entries() if credential_url(k) or credential_url(v)]
        if "-l" in rest and shown:
            return Finding("git var", repo_disp, samples=shown)
        return None
    if sub != "config":
        return None
    scope: List[str] = []
    mode, arg, name_only = "", "", False
    pos: List[str] = []
    j = 0
    new_sub = (
        rest[0]
        if rest
        and rest[0] in ("list", "get", "set", "unset", "rename-section", "remove-section", "edit")
        else ""
    )
    if new_sub:
        j = 1
        mode = {"list": "all", "get": "key"}.get(new_sub, "none")
    while j < len(rest):
        a = rest[j]
        if a in ("--global", "--system", "--local", "--worktree"):
            scope.append(a)
        elif a in ("--file", "-f", "--blob") and j + 1 < len(rest):
            scope += [a, _resolve(d, rest[j + 1]) if a != "--blob" else rest[j + 1]]
            j += 1
        elif a.startswith("--file="):
            scope += ["--file", _resolve(d, a.split("=", 1)[1])]
        elif a in ("-l", "--list") or (re.fullmatch(r"-[A-Za-z]{2,}", a) is not None and "l" in a):
            mode = "all"
        elif a in ("--get", "--get-all") and not mode:
            mode = "key"
        elif a == "--get-regexp" or (new_sub == "get" and a == "--regexp"):
            mode = "regexp"
        elif a == "--get-urlmatch":
            mode = "all"
        elif a == "--name-only":
            name_only = True
        elif a in ("--default", "--type", "--value", "--comment") and j + 1 < len(rest):
            j += 1
        elif not a.startswith("-"):
            pos.append(a)
        j += 1
    if not mode and len(pos) == 1:
        mode = "key"  # ``git config <key>`` reads the key
    if mode in ("", "none"):
        return None
    arg = pos[0] if pos else ""
    shown_lines = [f"{k}={v}" for k, v in entries(scope) if credential_url(k) or credential_url(v)]
    shown_lines += [v for k, v in entries(scope) if credential_url(v)]
    for k, v in entries(scope):
        shown = credential_url(k) or (not name_only and credential_url(v))
        if not shown:
            continue
        if mode == "all":
            return Finding("git config", repo_disp, samples=shown_lines)
        if mode == "key" and arg and k == arg.lower():
            return Finding("git config", repo_disp, samples=shown_lines)
        if mode == "regexp":
            try:
                if not arg or re.search(arg, k, re.I):
                    return Finding("git config", repo_disp, samples=shown_lines)
            except re.error:
                return Finding("git config", repo_disp, samples=shown_lines)
    return None


# The folder part starts only where a path can start (the ``(?<!...)``). So a long word is read once,
# not once from each of its characters. The matches are the same.
_CFG_IN_WORD = re.compile(
    r"((?:(?<![^\s'\"()=,;])[^\s'\"()=,;]*/)?\.git/config(?:\.worktree)?)(?![\w.\-])"
)
_CFG_NAMES = (
    "config",
    "config.worktree",
    ".git-credentials",
    "credentials",
    ".gitconfig",
    "gitconfig",
)


def _glob_configs(pattern: str) -> List[str]:
    """The files with a git config name (_CFG_NAMES) that the glob ``pattern`` matches. Only the folder part
    is expanded on disk, so the cost does not grow with the number of files in a folder."""
    import glob as _glob

    head, last = os.path.split(pattern)
    if len(last) > 255:  # longer than a file name
        return []
    out: List[str] = []
    for name in _CFG_NAMES:
        # as in the shell, a pattern matches a leading dot only with a dot of its own
        if fnmatch.fnmatchcase(name, last) and (last[:1] == "." or name[:1] != "."):
            out += _glob.glob(os.path.join(head, name))[:GLOB_MAX]
    return out


def _find_parts(w: List[str]) -> Tuple[List[str], List[List[str]]]:
    """``find``: the start paths and the commands of ``-exec``/``-execdir``/``-ok``."""
    roots: List[str] = []
    execs: List[List[str]] = []
    i = 1
    while i < len(w) and not w[i].startswith("-") and w[i] not in ("(", "!"):
        roots.append(w[i])
        i += 1
    while i < len(w):
        if w[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            cmd: List[str] = []
            while j < len(w) and w[j] not in (";", "+", "\\;"):
                cmd.append(w[j])
                j += 1
            execs.append(cmd)
            i = j
        i += 1
    return roots or ["."], execs


def _reader(words: List[str]) -> bool:
    w, _ = unwrap(words)
    if not w:
        return False
    name = os.path.basename(w[0])
    return name not in _SAFE_FILE_CMDS and not (name == "sed" and scrubs(w)) and name != "xargs"


def _cred_files_under(probe: Probe, cwd: str, roots: Sequence[str]) -> List[str]:
    out: List[str] = []
    for r in roots:
        out.extend(c for c in configs_under(_resolve(cwd, r)) if probe.cred_lines(c))
    return out


def _file_stage(
    w: List[str],
    cwd: str,
    probe: Probe,
    before: Sequence[List[str]] = (),
    extra: Sequence[str] = (),
) -> Optional[Finding]:
    """``extra``: more words that name a file the stage reads (its stdin file, a word with each value of a
    ``for`` variable)."""
    name = os.path.basename(w[0])
    if name == "find":
        roots, execs = _find_parts(w)
        if any(_reader(e) for e in execs):
            hit = _cred_files_under(probe, cwd, roots)
            if hit:
                return _file_finding(probe, cwd, hit[0], "find -exec on a git config file")
        return None
    if name == "xargs":
        rest = [a for a in w[1:] if not a.startswith("-")]
        if rest and _reader(rest):
            for prev in before:
                pw, _ = unwrap(prev)
                if pw and os.path.basename(pw[0]) == "find":
                    hit = _cred_files_under(probe, cwd, _find_parts(pw)[0])
                    if hit:
                        return _file_finding(probe, cwd, hit[0], "xargs on a git config file")
                for word in pw[1:]:
                    c = _resolve(cwd, word)
                    if is_git_config(c) and probe.cred_lines(c):
                        return _file_finding(probe, cwd, c, "xargs on a git config file")
        return None
    if name in ("cp", "mv", "install", "ln"):
        args = [a for a in w[1:] if not a.startswith("-")]
        if len(args) >= 2:
            dest = _resolve(cwd, args[-1])
            for src in args[:-1]:
                sp = _resolve(cwd, src)
                if is_git_config(sp) and probe.cred_lines(sp):
                    probe.copies[os.path.join(dest, os.path.basename(sp))] = (
                        sp  # dest is (or will be) a folder
                    )
                    if not os.path.isdir(dest):
                        probe.copies[dest] = sp
        return None
    if name in _SAFE_FILE_CMDS:
        return None
    for word in [*w[1:], *extra]:
        if not word.startswith("-") and _resolve(cwd, word) in probe.copies:
            return _file_finding(
                probe,
                cwd,
                probe.copies[_resolve(cwd, word)],
                f"{name} of a copy of a git config file",
            )
    cands: List[str] = []
    for word in [*w[1:], *extra]:
        for m in _CFG_IN_WORD.finditer(word):
            cands.append(_resolve(cwd, m.group(1)))
        if word.startswith("-"):
            continue
        for part in _braces(word):
            p = _resolve(cwd, part)
            if os.path.basename(p) in _CFG_NAMES:
                cands.append(p)
            if any(ch in part for ch in "*?["):
                cands.extend(_glob_configs(p))
                if ".git" in part or os.path.basename(cwd) == ".git" or is_git_dir(cwd):
                    import glob as _glob

                    cands.extend(_glob.glob(p)[:GLOB_MAX])
    grep = _grep_parse(w) if name in _GREPS else None
    if grep is not None and grep["recursive"] and (not grep["rg"] or grep["hidden"]):
        roots = grep["paths"] or ["."]
        for r in roots:
            cands.extend(configs_under(_resolve(cwd, r)))
    seen = set()
    for c in cands:
        if c in seen or not is_git_config(c):
            continue
        seen.add(c)
        cred = probe.cred_lines(c)
        if not cred:
            continue
        if name == "sed" and scrubs(w, [probe.file_lines(c)[i] for i in cred]):
            continue
        if grep is not None:
            if grep["quiet"]:
                continue
            if not grep["pfile"] and not would_emit(
                probe.file_lines(c),
                cred,
                grep["patterns"],
                fixed=grep["fixed"],
                basic=grep["basic"],
                icase=grep["icase"],
                invert=grep["invert"],
                word=grep["word"],
                whole=grep["whole"],
                before=grep["before"],
                after=grep["after"],
                only=grep["only"],
            ):
                continue
        return _file_finding(probe, cwd, c, f"{name} of a git config file")
    return None


_KEYWORDS = frozenset(("do", "then", "else", "elif", "if", "while", "until", "!", "{", "time"))
#: commands that print the files they name, with their options that take the next word
_PRINT_OPTS = {
    "cat": (),
    "tac": (),
    "nl": (),
    "less": (),
    "more": (),
    "bat": (),
    "head": ("-n", "-c"),
    "tail": ("-n", "-c"),
    "sed": ("-e", "-f"),
    "awk": ("-v", "-F", "-f"),
}


def _open_read(
    w: List[str],
    cwd: str,
    probe: Probe,
    files: Sequence[str],
    opened: Mapping[str, Tuple[str, str]],
) -> Optional[Finding]:
    """A print command whose file is set when the command runs (a variable, ``$( )``, backticks): a finding
    when that word can stand for a config file that git reads in ``cwd`` and that holds a credential URL."""
    while len(w) > 1 and w[0] in _KEYWORDS:
        w = w[1:]
    name = os.path.basename(w[0])
    blind = False
    if name in _GREPS:
        grep = _grep_parse(w)
        words = grep["paths"]
        blind = not grep["quiet"] and any(p in opened for p in grep["patterns"])
    elif name in _PRINT_OPTS:
        takes = _PRINT_OPTS[name]
        words = [a for k, a in enumerate(w[1:]) if not a.startswith("-") and w[k] not in takes]
        if name == "sed":
            words = [a for a in words if a not in _sed_scripts(w)]
        elif name == "awk":
            words = words[1:]  # the first one is the program
    else:
        return None
    parts = [opened[a] for a in [*words, *files] if a in opened]
    for cfg in probe.cred_configs(cwd) if parts else ():
        if not any(
            cfg.endswith(tail) and (not head or cfg.startswith(_resolve(cwd, head)))
            for head, tail in parts
        ):
            continue
        # an unknown grep pattern can match the credential line
        f = _file_finding(probe, cwd, cfg, "") if blind else _file_stage(w, cwd, probe, extra=[cfg])
        if f is not None:
            f.what, f.open_path = f"{name} of a path that is set when the command runs", True
            return f
    return None


def check_bash(
    command: str,
    cwd: str,
    probe: Optional[Probe] = None,
    depth: int = 0,
    strip: Optional[Callable[[str], str]] = None,
) -> Optional[Finding]:
    probe = probe or Probe()
    if strip is not None:
        try:
            command = strip(command)
        except Exception:  # noqa: BLE001, S110 - no strip: the heredoc body is read as commands (deny side only)
            pass
    if depth > EXEC_DEPTH or not command.strip():
        return None
    here = cwd
    exported: dict = {}
    shell = _Shell()
    for m in _SUBST.finditer(command):  # $( ) and backticks, also inside double quotes
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        f = check_bash(inner or "", cwd, probe, depth + 1)
        if f is not None:
            return f
    for marked in pipelines(tokens(_MKTEMP.sub(NEW_FILE, command))):
        stages = [_expand(words, shell) for words in marked]
        pipe = [st[0] for st in stages]
        for words in pipe:
            for (
                x
            ) in words:  # a word that names a known variable may set it again (read, local, f+=...)
                if x.partition("=")[0].rstrip("+") in shell:
                    shell[x.partition("=")[0].rstrip("+")] = None
        first: Optional[Tuple[int, Finding]] = None
        for si, words in enumerate(pipe):
            w, assigns = unwrap([x for x in words if x != TO_STDERR])
            if not w:
                continue
            files, opened, alts = stages[si][1:]
            name = os.path.basename(w[0])
            f = None
            if name in _SHELLS:
                for k in range(1, len(w) - 1):
                    if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", w[k]):
                        f = check_bash(w[k + 1], here, probe, depth + 1)
                        break
            elif name == "eval":
                f = check_bash(" ".join(w[1:]), here, probe, depth + 1)
            elif name == "git":
                f = _git_stage(w, here, {**exported, **assigns}, probe)
            else:
                f = _file_stage(w, here, probe, pipe[:si], [*files, *alts])
                if f is None and opened:
                    f = _open_read(w, here, probe, files, opened)
            if f is not None and first is None:
                first = (si, f)
        if first is not None and (
            first[1].stderr or not _scrubbed_after(pipe, first[0], first[1].samples)
        ):
            return first[1]
        if len(pipe) == 1:
            w, assigns = unwrap(pipe[0])
            if w and w[0] in ("export", "declare", "typeset"):
                for a in w[1:]:
                    if _ASSIGN.match(a):
                        k, _, val = a.partition("=")
                        exported[k] = assigns[k] = val
            if not w or w[0] in ("export", "declare", "typeset"):
                for k, val in assigns.items():
                    shell[k] = [val] if val and f"{k}={val}" not in stages[0][2] else None
            elif w[0] == "for" and w[2:3] == ["in"]:
                shell[w[1]] = None if any(x in stages[0][2] for x in w[3:]) else w[3:]
            if w and w[0] in ("cd", "pushd"):
                tgt = next((a for a in w[1:] if not a.startswith("-")), "~")
                here = _resolve(here, tgt)
    return None


def check_read(
    ti: Mapping[str, object], cwd: str, probe: Optional[Probe] = None
) -> Optional[Finding]:
    raw = ti.get("file_path")
    if not isinstance(raw, str) or not raw:
        return None
    probe = probe or Probe()
    p = _resolve(cwd, raw)
    if is_git_config(p) and probe.cred_lines(p):
        return _file_finding(probe, cwd, p, "Read of a git config file")
    return None


def check_grep(
    ti: Mapping[str, object], cwd: str, probe: Optional[Probe] = None
) -> Optional[Finding]:
    if str(ti.get("output_mode") or "files_with_matches") != "content":
        return None
    if ti.get("type"):
        return None  # a git config file has no file type
    probe = probe or Probe()
    root = _resolve(cwd, str(ti.get("path") or "."))
    glob = str(ti.get("glob") or "")
    pat = str(ti.get("pattern") or "")

    def num(k: str) -> int:
        raw = ti.get(k) or 0
        try:
            return int(raw) if isinstance(raw, (int, float, str)) else 99
        except ValueError:
            return 99

    ctx = num("-C") or num("context")
    for c in configs_under(root):
        if glob and not (
            fnmatch.fnmatch(os.path.basename(c), glob)
            or fnmatch.fnmatch(c, glob)
            or fnmatch.fnmatch(os.path.relpath(c, root), glob)
        ):
            continue
        cred = probe.cred_lines(c)
        if not cred:
            continue
        if ti.get("multiline") or would_emit(
            probe.file_lines(c),
            cred,
            [pat],
            icase=bool(ti.get("-i")),
            before=max(num("-B"), ctx),
            after=max(num("-A"), ctx),
        ):
            return _file_finding(probe, cwd, c, "Grep of a git config file")
    return None


def reason(f: Finding, tool: str) -> str:
    repo = f.repo or "."
    lines = [
        f"Credential guard: this {tool} call ({f.what}) would put a git URL that holds a "
        "credential (a token or a password) into the conversation. The URL is not shown here.",
        "Use a form that hides the credential:",
        f"- remote names only: git -C {repo} remote",
        f"- URLs without the credential: git -C {repo} remote -v | {SCRUB}",
        f"- owner/name of a remote: git -C {repo} remote get-url origin | {SCRUB}",
    ]
    if f.cfg:
        lines.append(f"- the config file: {SCRUB} {f.cfg}")
    if f.open_path:
        lines.append(
            "- a file other than the git config: write the path in the command, not a variable or $( )"
        )
    lines += [
        f"- access check: git -C {repo} ls-remote -q origin >/dev/null 2>&1; echo $?",
        "This rule has no override marker: the forms above always work.",
    ]
    return "\n".join(lines)


def decide(
    tool: str, ti: Mapping[str, object], cwd: str, strip: Optional[Callable[[str], str]] = None
) -> Optional[Tuple[str, str]]:
    """``(deny reason, what)`` when the call would print a credential URL, else None.
    Every error is an allow."""
    try:
        probe = Probe()
        f: Optional[Finding] = None
        if tool == "Bash":
            cmd = ti.get("command")
            if isinstance(cmd, str):
                f = check_bash(cmd, cwd, probe, strip=strip)
        elif tool == "Read":
            f = check_read(ti, cwd, probe)
        elif tool == "Grep":
            f = check_grep(ti, cwd, probe)
        if f is None:
            return None
        return reason(f, tool), f.what
    except Exception:  # noqa: BLE001 - the guard fails open
        return None
