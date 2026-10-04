# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings for the stdlib hooks: the data dir, the config file and the
built-in word lists.

Design doc 0001, section 12. A setting comes from, in this order:

1. an env var ``NOBLIVION_*``;
2. the JSON config file: ``NOBLIVION_CONFIG``, else ``<data dir>/config.json``;
3. the built-in default.

The data dir is ``NOBLIVION_DATA_DIR``, else ``CLAUDE_PLUGIN_DATA``, else
``${XDG_DATA_HOME:-~/.local/share}/noblivion``. No code holds a literal home
path: every path is built from ``Path.home()`` or the data dir.

The config file is JSON because the hooks run on python 3.9 with no
``tomllib``. A missing or broken file reads as ``{}``: a hook falls back to
the defaults and never fails on a bad config.

The word lists of the guard hook (section 12.4) live here, so the leak
scanner checks one place. The config extends them and never replaces them.

Standard library only. Hooks load this file by path, as a sibling.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

DATA_DIR_ENV = "NOBLIVION_DATA_DIR"
CONFIG_ENV = "NOBLIVION_CONFIG"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
CONFIG_NAME = "config.json"
CACHE_SUBDIR = "cache"

# Command arguments too generic to make a guard trigger "specific"
# (guard hook ``GENERIC_ARGS``). Generic git and loopback words only. Users
# add their own host names with ``guard.generic_args_extra``.
GENERIC_ARGS_DEFAULT: FrozenSet[str] = frozenset(
    "localhost origin github upstream main master head".split()
)

# Words removed from a command before the guard hook's evidence score
# (guard hook ``EVIDENCE_STOP``). Users add words with
# ``guard.evidence_stop_extra``.
EVIDENCE_STOP_DEFAULT: FrozenSet[str] = frozenset(
    (
        "tmp home claude scratchpad dev null echo date python py src tail head rm exec jq format "
        "true false json md txt log sh bash usr bin opt var etc lib venv print import sys os re open read "
        "write path main origin github git gh docker cmd dir tree ref refs rev parse short oneline sort "
        "count len str int status list show exit return stdin stdout stderr err out cut xargs awk sed grep "
        "tee cat ls wc find test for while done do if then fi else in set local "
        "command commands rule rules both exists name where equal guard ci source key result "
        "results tools tool up pin deploy host new old first last each every one two after before same "
        "without never always check checks using used value values need needs want output input data "
        "text type time names note notes work"
    ).split()
)

_CACHE: Dict[str, Tuple[Optional[float], Dict[str, Any]]] = {}

# The one rule for every on/off switch (docs/configuration.md, "Switches").
SWITCH_ON: Tuple[str, ...] = ("1", "true", "yes", "on")
SWITCH_OFF: Tuple[str, ...] = ("0", "false", "no", "off", "")


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """The plugin data dir (design doc section 12.2)."""
    e = _env(env)
    for name in (DATA_DIR_ENV, PLUGIN_DATA_ENV):
        raw = str(e.get(name) or "").strip()
        if raw and "${" not in raw:  # "${...}": a config passed it unexpanded
            return Path(os.path.expanduser(raw))
    xdg = str(e.get("XDG_DATA_HOME") or "").strip()
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "share"
    return base / "noblivion"


def private_dir(path: Path) -> Path:
    """Make ``path`` (and its parents) and set it to mode 0700, also when it
    exists: Claude Code can make the data dir with the user's umask before
    any NOBLIVION code runs (NOBLIVION-27)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o700)
    except OSError:
        pass
    return path


class DataDirMissing(FileNotFoundError):
    """The data dir does not exist. Only ``install.sh`` makes it (NOBLIVION-28)."""


def make_dirs(path: Any, env: Optional[Mapping[str, str]] = None) -> Path:
    """Make the folder ``path`` (and its parents) with mode 0700, but never
    the data dir itself (NOBLIVION-28).

    A folder inside a data dir that does not exist raises ``DataDirMissing``
    (an ``OSError``). After ``claude plugin uninstall`` deleted the data dir,
    a hook of a session that still runs must not make it again. A folder
    outside the data dir (an override) is made as before."""
    folder = Path(path)
    root = data_dir(env)
    if not root.is_dir():
        try:
            Path(os.path.abspath(folder)).relative_to(os.path.abspath(root))
        except ValueError:
            pass
        else:
            raise DataDirMissing("the data dir %s does not exist" % root)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def cache_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """``<data dir>/cache``: the per-session recall cache and the trust spool."""
    return data_dir(env) / CACHE_SUBDIR


def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    raw = str(_env(env).get(CONFIG_ENV) or "").strip()
    return Path(os.path.expanduser(raw)) if raw else data_dir(env) / CONFIG_NAME


def load_config(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The config file as a dict. ``{}`` when it is missing, unreadable or not
    a JSON object. Read once per process and file version. Never raises."""
    path = config_path(env)
    key = str(path)
    try:
        mtime: Optional[float] = path.stat().st_mtime
    except OSError:
        mtime = None
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    doc: Dict[str, Any] = {}
    if mtime is not None:
        try:
            with path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                doc = loaded
        except (OSError, ValueError):
            doc = {}
    _CACHE[key] = (mtime, doc)
    return doc


def get(key: str, default: Any = None, env: Optional[Mapping[str, str]] = None) -> Any:
    """The config value of a dotted ``key`` (``guard.generic_args_extra``), or
    ``default``. A flat key with the dots in it is also accepted."""
    doc = load_config(env)
    if key in doc:
        return doc[key]
    node: Any = doc
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def string_list(key: str, env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """A config list of strings, stripped, empty items left out. ``()`` for a
    missing key or a value that is not a list."""
    value = get(key, None, env)
    if not isinstance(value, list):
        return ()
    return tuple(s.strip() for s in value if isinstance(s, str) and s.strip())


def setting(
    env_name: str, key: str, default: Any = None, env: Optional[Mapping[str, str]] = None
) -> Any:
    """An env var when set and not empty, else the config value, else ``default``."""
    raw = str(_env(env).get(env_name) or "").strip()
    if raw:
        return raw
    return get(key, default, env)


def parse_switch(value: Any, default: bool) -> bool:
    """One switch value as a bool. ``1``, ``true``, ``yes``, ``on`` (any case,
    JSON ``true``, a non-zero number) are on. ``0``, ``false``, ``no``,
    ``off``, the empty text (JSON ``false``, ``0``) are off. ``None`` (not
    set) and any other value give ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if not isinstance(value, str):
        return default
    raw = value.strip().lower()
    if raw in SWITCH_ON:
        return True
    if raw in SWITCH_OFF:
        return False
    return default


def switch(
    env_name: str,
    default: bool = False,
    env: Optional[Mapping[str, str]] = None,
    key: Optional[str] = None,
) -> bool:
    """The switch ``env_name``: the env var when it is set (an empty value is
    off), else the config ``key`` when one is given, else ``default``."""
    e = _env(env)
    if env_name in e:
        return parse_switch(e.get(env_name), default)
    if key:
        return parse_switch(get(key, None, env), default)
    return default


def generic_args(env: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """``GENERIC_ARGS_DEFAULT`` plus ``guard.generic_args_extra``, lower case."""
    extra = {s.lower() for s in string_list("guard.generic_args_extra", env)}
    return GENERIC_ARGS_DEFAULT | frozenset(extra)


def evidence_stop(env: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """``EVIDENCE_STOP_DEFAULT`` plus ``guard.evidence_stop_extra``, lower case."""
    extra = {s.lower() for s in string_list("guard.evidence_stop_extra", env)}
    return EVIDENCE_STOP_DEFAULT | frozenset(extra)


# Claude Code cuts a project folder name at 200 characters and appends a
# hash of the full path, so the name stays within file system limits.
SLUG_MAX = 200


def _java_hash(text: str) -> int:
    """Claude Code's 32-bit string hash: ``h = h * 31 + unit``, over UTF-16
    code units, kept to a signed 32-bit integer."""
    h = 0
    data = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i : i + 2], "little")) & 0xFFFFFFFF
    return h - 0x100000000 if h & 0x80000000 else h


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out


def project_slug(path: Any) -> str:
    """The folder name Claude Code gives the project at ``path``: every
    character that is not an ASCII letter or digit becomes ``-`` (one ``-``
    per UTF-16 code unit, as in JavaScript). A name longer than 200
    characters is cut to 200 and gets ``-<base 36 hash of the path>``."""
    text = str(path)
    slug = "".join(
        c if c.isascii() and c.isalnum() else ("--" if ord(c) > 0xFFFF else "-") for c in text
    )
    if len(slug) <= SLUG_MAX:
        return slug
    return "%s-%s" % (slug[:SLUG_MAX], _base36(abs(_java_hash(text))))


# ── the memory folder of a session (NOBLIVION-30) ─────────────────────────
#
# How Claude Code picks the auto-memory folder. Read from the Claude Code
# 2.1.261 bundle (the ``defaultPath`` and ``resolveEntry`` code next to the
# ``autoMemoryDirectory`` setting) and checked with a live run on Linux: a
# session in ``repo/sub/deep`` and a session in a linked worktree of ``repo``
# both reported ``<config dir>/projects/<slug of repo>/memory/``, while their
# transcripts went to the slug of their own working directory. The docs say
# the same ("derived from the git repository, so all worktrees and
# subdirectories within the same repo share one auto memory directory"):
# https://code.claude.com/docs/en/memory#storage-location
#
# 1. ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE``, when it is a valid absolute path.
# 2. ``autoMemoryDirectory`` from the first settings source that has the key:
#    managed (policy), ``--settings`` (a hook cannot see it), the project's
#    ``.claude/settings.local.json`` and ``.claude/settings.json``, then the
#    user's ``<config dir>/settings.json``. The value must be absolute or
#    start with ``~/``. An invalid value does not fall through to the next
#    source: Claude Code then uses the default folder (3).
# 3. ``<base>/projects/<name>/memory``. The base is
#    ``CLAUDE_CODE_REMOTE_MEMORY_DIR``, else the config dir
#    (``CLAUDE_CONFIG_DIR``, else ``~/.claude``). The name is
#    ``CLAUDE_CODE_PROJECT_DIR_NAME`` when the base is the config dir,
#    ``CLAUDE_CONFIG_DIR`` is set and the name is valid; else the slug of the
#    project folder. The project folder is the main checkout of the git
#    repository that holds the working dir, else the working dir itself.
#
# Claude Code finds the repository with no ``git`` call: it walks up from the
# working dir to the first folder with a ``.git`` entry. When ``.git`` is a
# file (a linked worktree), it follows ``gitdir:`` and ``commondir`` and
# checks that the worktree's ``gitdir`` file points back. Any check that fails
# keeps the folder that holds ``.git``. This code does the same.

COWORK_MEMORY_ENV = "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"
REMOTE_MEMORY_ENV = "CLAUDE_CODE_REMOTE_MEMORY_DIR"
CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
PROJECT_DIR_NAME_ENV = "CLAUDE_CODE_PROJECT_DIR_NAME"
MEMORY_DIR_OVERRIDE_ENV = "NOBLIVION_MEMORY_DIR"
AUTO_MEMORY_KEY = "autoMemoryDirectory"

_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEVICE_NAME_RE = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)


def managed_settings_path() -> Path:
    """The managed settings file (the policy scope) of this platform."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if os.name == "nt":
        return Path(r"C:\Program Files\ClaudeCode\managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def claude_config_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """``CLAUDE_CONFIG_DIR``, else ``~/.claude``. Claude Code uses the value
    as it is: no ``~`` expansion."""
    raw = str(_env(env).get(CLAUDE_CONFIG_ENV) or "")
    return Path(unicodedata.normalize("NFC", raw or str(Path.home() / ".claude")))


def _check_dir(raw: Any, expand_home: bool) -> Optional[Path]:
    """Claude Code's check of a memory folder setting: absolute, or ``~/...``
    when ``expand_home``; no ``..`` above home; not a bare drive; no NUL."""
    if not isinstance(raw, str) or not raw:
        return None
    path = raw
    if expand_home and (path.startswith("~/") or path.startswith("~\\")):
        rest = path[2:]
        norm = os.path.normpath(rest or ".")
        if norm in (".", "..") or norm.startswith(".." + os.sep) or norm.startswith("../"):
            return None
        path = os.path.join(str(Path.home()), rest)
    path = os.path.normpath(path).rstrip("/\\")
    if (
        not os.path.isabs(path)
        or len(path) < 3
        or re.fullmatch(r"[A-Za-z]:", path)
        or "\x00" in path
    ):
        return None
    return Path(unicodedata.normalize("NFC", path))


def _settings_value(path: Path, key: str) -> Any:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc.get(key) if isinstance(doc, dict) else None


def auto_memory_setting(
    cwd: Optional[str], env: Optional[Mapping[str, str]] = None
) -> Tuple[Optional[Any], Optional[Path]]:
    """``(value, file)`` of the first settings file that sets
    ``autoMemoryDirectory``, in Claude Code's order; ``(None, None)`` when
    none does. The project files are read from ``cwd``."""
    files = [managed_settings_path()]
    if cwd:
        files += [
            Path(cwd) / ".claude" / "settings.local.json",
            Path(cwd) / ".claude" / "settings.json",
        ]
    files.append(claude_config_dir(env) / "settings.json")
    for path in files:
        value = _settings_value(path, AUTO_MEMORY_KEY)
        if value is not None:
            return value, path
    return None, None


def _is_entry(path: str) -> bool:
    return os.path.isdir(path) or os.path.isfile(path)


def git_root(cwd: str) -> Optional[str]:
    """The first folder at or above ``cwd`` that holds a ``.git`` entry (a
    folder or a file), or None outside git."""
    here = os.path.abspath(cwd)
    while True:
        try:
            if _is_entry(os.path.join(here, ".git")):
                return here
        except (OSError, ValueError):
            return None
        up = os.path.dirname(here)
        if up == here:
            return None
        here = up


def _read_line(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def canonical_git_root(root: str) -> str:
    """The main checkout of the repository whose ``.git`` entry is in
    ``root``. A linked worktree gives its main checkout (or the bare
    repository folder); anything else, or any failed check, gives ``root``."""
    try:
        text = _read_line(os.path.join(root, ".git"))
    except (OSError, ValueError):
        return root  # a .git folder: root is the main checkout
    try:
        if not text.startswith("gitdir:"):
            return root
        gitdir = os.path.normpath(os.path.join(root, text[len("gitdir:") :].strip()))
        common = os.path.normpath(
            os.path.join(gitdir, _read_line(os.path.join(gitdir, "commondir")))
        )
        if os.path.normpath(os.path.dirname(gitdir)) != os.path.join(common, "worktrees"):
            return root
        back = os.path.normpath(os.path.join(gitdir, _read_line(os.path.join(gitdir, "gitdir"))))
        if os.path.realpath(back) != os.path.join(os.path.realpath(root), ".git"):
            return root
        if os.path.basename(common) != ".git":
            return common  # a bare repository
        return os.path.dirname(common)
    except (OSError, ValueError):
        return root


def project_folder(cwd: str) -> str:
    """The folder Claude Code names the project after: the main checkout of
    the git repository at ``cwd``, else ``cwd``."""
    cwd = os.path.abspath(cwd)
    root = git_root(cwd)
    return canonical_git_root(root) if root else cwd


def resolve_memory_dir(
    cwd: Optional[str], env: Optional[Mapping[str, str]] = None
) -> Optional[Path]:
    """The memory folder of a session at ``cwd``: ``NOBLIVION_MEMORY_DIR``,
    else the folder Claude Code uses (the rule above). None when there is no
    override and ``cwd`` is not an absolute path. The folder may not exist."""
    e = _env(env)
    raw = str(e.get(MEMORY_DIR_OVERRIDE_ENV) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    cowork = _check_dir(e.get(COWORK_MEMORY_ENV), expand_home=False)
    if cowork:
        return cowork
    if not (isinstance(cwd, str) and os.path.isabs(cwd)):
        return None
    value, _ = auto_memory_setting(cwd, e)
    custom = _check_dir(value, expand_home=True)
    if custom:
        return custom
    config = claude_config_dir(e)
    remote = str(e.get(REMOTE_MEMORY_ENV) or "")
    base = Path(remote) if remote else config
    name = None
    if base == config and e.get(CLAUDE_CONFIG_ENV):
        pinned = str(e.get(PROJECT_DIR_NAME_ENV) or "")
        if _DIR_NAME_RE.match(pinned) and not _DEVICE_NAME_RE.match(pinned):
            name = pinned
    if name is None:
        name = project_slug(project_folder(cwd))
    return Path(unicodedata.normalize("NFC", str(base / "projects" / name / "memory")))


# ── the user-wide memory folder (NOBLIVION-31) ────────────────────────────
#
# Claude Code keeps one memory folder per project. A user may keep rules that
# hold in every project in one more folder: ``NOBLIVION_GLOBAL_MEMORY_DIR``,
# else the config key ``global_memory_dir``. Default: none. When it is set,
# recall, the guards and the stop checks read both folders. A file of the
# project folder wins over a file with the same name in the global folder.

GLOBAL_MEMORY_DIR_ENV = "NOBLIVION_GLOBAL_MEMORY_DIR"
GLOBAL_MEMORY_DIR_KEY = "global_memory_dir"


def global_memory_dir(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The user-wide memory folder, or None when none is set. ``~`` is
    expanded. The folder may not exist."""
    raw: Any = str(_env(env).get(GLOBAL_MEMORY_DIR_ENV) or "").strip()
    if not raw:
        raw = get(GLOBAL_MEMORY_DIR_KEY, None, env)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(os.path.expanduser(raw.strip()))


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except (OSError, ValueError):
        return str(a) == str(b)


def memory_dirs(cwd: Optional[str], env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """The folders a session at ``cwd`` reads rules from, in order: the
    project folder (``resolve_memory_dir``), then the global folder when it
    is set and is not the project folder. A folder may not exist."""
    out: List[Path] = []
    project = resolve_memory_dir(cwd, env)
    if project is not None:
        out.append(project)
    extra = global_memory_dir(env)
    if extra is not None and not any(_same_dir(extra, p) for p in out):
        out.append(extra)
    return out


def memory_files(folders: Any, pattern: str = "*.md") -> List[Path]:
    """The top-level files that match ``pattern`` in ``folders``, sorted by
    name. On a name that two folders hold, the first folder wins, so the
    project folder shadows the global folder. A missing folder is skipped."""
    seen: Dict[str, Path] = {}
    for folder in folders:
        try:
            found = sorted(Path(folder).glob(pattern))
        except OSError:
            continue
        for path in found:
            seen.setdefault(path.name, path)
    return [seen[name] for name in sorted(seen)]
