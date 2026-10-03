# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration and data dir (design doc 0001, section 12).

Order: env vars ``NOBLIVION_*`` first, then the JSON config file
(``NOBLIVION_CONFIG``, else ``<data dir>/config.json``), then the defaults.

The config file may use nested objects (``{"index": {"interval_s": 30}}``) or
dotted keys (``{"index.interval_s": 30}``). An unknown key is not an error.
A bad value falls back to the default. No code here holds a literal home path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DB_FILE = "noblivion.db"
INDEX_LOCK_FILE = "index.lock"

DEFAULT_NAMESPACE = "claude_code"
DEFAULT_DELETE_GRACE_DAYS = 14
DEFAULT_ARCHIVE_RETENTION_DAYS = 90


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """``NOBLIVION_DATA_DIR``, else ``CLAUDE_PLUGIN_DATA``, else the XDG data dir."""
    env = os.environ if env is None else env
    for key in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        value = env.get(key, "").strip()
        if value:
            return Path(os.path.expanduser(value))
    xdg = env.get("XDG_DATA_HOME", "").strip()
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "share"
    return base / "noblivion"


def load_file(env: Mapping[str, str] | None = None) -> dict:
    """Read the config file. A missing or broken file gives ``{}``."""
    env = os.environ if env is None else env
    explicit = env.get("NOBLIVION_CONFIG", "").strip()
    path = Path(os.path.expanduser(explicit)) if explicit else data_dir(env) / "config.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def lookup(cfg: Mapping, dotted: str, default: object = None) -> object:
    """Find ``dotted`` as a flat key or as a nested path in ``cfg``."""
    if dotted in cfg:
        return cfg[dotted]
    node: object = cfg
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def string_list(cfg: Mapping, dotted: str) -> tuple[str, ...]:
    """A config list of strings, stripped, empty items left out. ``()`` for a
    missing key or a value that is not a list."""
    value = lookup(cfg, dotted)
    if not isinstance(value, list):
        return ()
    return tuple(s.strip() for s in value if isinstance(s, str) and s.strip())


def default_memory_dirs() -> list[Path]:
    """Every folder that matches ``~/.claude/projects/*/memory``."""
    projects = Path.home() / ".claude" / "projects"
    try:
        return sorted(p for p in projects.glob("*/memory") if p.is_dir())
    except OSError:
        return []


def _non_negative_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    namespace: str
    memory_dirs: tuple[Path, ...] | None  # None: the default glob, read at scan time
    delete_grace_days: int
    archive_retention_days: int
    ticket_prefixes: tuple[str, ...] = ()  # labels.ticket_prefixes
    service_prefixes: tuple[str, ...] = ()  # labels.service_prefixes

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def index_lock_path(self) -> Path:
        return self.data_dir / INDEX_LOCK_FILE

    def resolved_memory_dirs(self) -> list[Path]:
        if self.memory_dirs is None:
            return default_memory_dirs()
        return list(self.memory_dirs)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    cfg = load_file(env)

    namespace = env.get("NOBLIVION_PROJECT", "").strip()
    if not namespace:
        value = lookup(cfg, "namespace")
        namespace = value.strip() if isinstance(value, str) and value.strip() else ""
    namespace = namespace or DEFAULT_NAMESPACE

    dirs: tuple[Path, ...] | None = None
    raw_env_dirs = env.get("NOBLIVION_MEMORY_DIRS", "").strip()
    if raw_env_dirs:
        dirs = tuple(Path(os.path.expanduser(p)) for p in raw_env_dirs.split(":") if p.strip())
    else:
        value = lookup(cfg, "memory_dirs")
        if isinstance(value, list) and all(isinstance(p, str) for p in value):
            dirs = tuple(Path(os.path.expanduser(p)) for p in value if p.strip())

    return Settings(
        data_dir=data_dir(env),
        namespace=namespace,
        memory_dirs=dirs,
        delete_grace_days=_non_negative_int(
            lookup(cfg, "index.delete_grace_days"), DEFAULT_DELETE_GRACE_DAYS
        ),
        archive_retention_days=_non_negative_int(
            lookup(cfg, "dedup.archive_retention_days"), DEFAULT_ARCHIVE_RETENTION_DAYS
        ),
        ticket_prefixes=string_list(cfg, "labels.ticket_prefixes"),
        service_prefixes=string_list(cfg, "labels.service_prefixes"),
    )
