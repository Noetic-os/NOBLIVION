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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

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


def project_slug(path: Any) -> str:
    """The folder name Claude Code gives the project at ``path``: every
    character that is not a letter or a digit becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def default_memory_dir() -> Path:
    """The memory folder of the project that is the home folder:
    ``~/.claude/projects/<slug of the home folder>/memory``."""
    home = Path.home()
    return home / ".claude" / "projects" / project_slug(home) / "memory"
