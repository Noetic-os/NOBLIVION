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
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DB_FILE = "noblivion.db"
INDEX_LOCK_FILE = "index.lock"
STORE_LOCK_FILE = "store.lock"
STORE_JSON_FILE = "store.json"
TOKEN_FILE = "token"
SPAWN_STAMP_FILE = "spawn.stamp"
STORE_LOG_FILE = "logs/store.log"
STORE_ERROR_FILE = "store.error"  # why the last start failed; removed on a good start

DEFAULT_NAMESPACE = "claude_code"
DEFAULT_DELETE_GRACE_DAYS = 14
DEFAULT_ARCHIVE_RETENTION_DAYS = 90
DEFAULT_PORT = 0  # any free port; the hooks read the real port from store.json
DEFAULT_IDLE_EXIT_S = 1800
DEFAULT_INDEX_INTERVAL_S = 30
DEFAULT_PRIOR_MINED = 0.3
TRUST_RANKING_MODES = ("off", "shadow", "on")
LOG_LEVELS = ("debug", "info", "warning", "error")

# The one rule for every on/off switch (docs/configuration.md, "Switches").
# ``hooks/hook_config.py`` holds the same rule for the stdlib hooks.
SWITCH_ON = ("1", "true", "yes", "on")
SWITCH_OFF = ("0", "false", "no", "off", "")


INSTALL_STAMP = "noblivion-install.json"  # in the store venv, written by install.sh


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """``NOBLIVION_DATA_DIR``, else ``CLAUDE_PLUGIN_DATA``, else the folder of
    the store venv this runs in (``install.sh`` makes ``<data dir>/venv``, so
    a ``noblivion`` command run from a terminal finds the plugin's data dir),
    else the XDG data dir."""
    env = os.environ if env is None else env
    for key in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        value = env.get(key, "").strip()
        if value and "${" not in value:  # "${...}": a config passed it unexpanded
            return Path(os.path.expanduser(value))
    venv = Path(sys.prefix)
    if (venv / INSTALL_STAMP).is_file():
        return venv.parent
    xdg = env.get("XDG_DATA_HOME", "").strip()
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "share"
    return base / "noblivion"


class DataDirMissing(FileNotFoundError):
    """The data dir does not exist. Only ``install.sh`` makes it (NOBLIVION-28)."""


def install_stamp(data_dir: Path) -> Path:
    """``<data dir>/venv/noblivion-install.json``, written by ``install.sh``."""
    return Path(data_dir) / "venv" / INSTALL_STAMP


def is_installed(data_dir: Path) -> bool:
    """True when ``install.sh`` has run for this data dir: its install stamp
    exists. Only then may a process create a new database (NOBLIVION-28)."""
    return install_stamp(data_dir).is_file()


def private_dir(path: Path, *, create: bool = True) -> Path:
    """Make ``path`` (and its parents) and set it to mode 0700. ``mkdir -m``
    does not change a folder that exists, and Claude Code may make the data
    dir before the store does, with the user's umask (NOBLIVION-27).

    ``create=False``: never make the folder; raise ``DataDirMissing`` when it
    is missing. The store and the launcher use it for the data dir, so a
    store that stops after an uninstall deleted the folder does not make it
    again (NOBLIVION-28)."""
    if not create and not path.is_dir():
        raise DataDirMissing(f"the data dir {path} does not exist; run install.sh")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o700)
    except OSError:
        pass
    return path


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


def parse_switch(value: object, default: bool) -> bool:
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
    env: Mapping[str, str], env_name: str, cfg: Mapping, key: str | None, default: bool
) -> bool:
    """The switch ``env_name``: the env var when it is set (an empty value is
    off), else the config ``key`` when one is given, else ``default``."""
    if env_name in env:
        return parse_switch(env.get(env_name), default)
    if key:
        return parse_switch(lookup(cfg, key), default)
    return default


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """``CLAUDE_CONFIG_DIR``, else ``~/.claude``, as Claude Code reads it (no
    ``~`` expansion). ``hooks/hook_config.py`` holds the same rule."""
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _auto_memory_directory(path: Path) -> Path | None:
    """The ``autoMemoryDirectory`` of one Claude Code settings file, when it
    is absolute or ``~/...`` (the only forms Claude Code accepts)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = doc.get("autoMemoryDirectory") if isinstance(doc, dict) else None
    if not isinstance(raw, str) or not (raw.startswith("~/") or os.path.isabs(raw)):
        return None
    return Path(os.path.expanduser(raw))


def default_memory_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    """Every folder Claude Code may keep auto memory in (NOBLIVION-30):
    ``<config dir>/projects/*/memory`` (``CLAUDE_CONFIG_DIR``, else
    ``~/.claude``), the same under ``CLAUDE_CODE_REMOTE_MEMORY_DIR``, and the
    ``autoMemoryDirectory`` of the user and managed settings files. The
    store has no working dir, so a project's own settings files are not
    read; ``NOBLIVION_MEMORY_DIR`` covers that case."""
    env = os.environ if env is None else env
    config = claude_config_dir(env)
    bases = [config]
    if env.get("CLAUDE_CODE_REMOTE_MEMORY_DIR"):
        bases.append(Path(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"]))
    found: set[Path] = set()
    for base in bases:
        try:
            found.update(p for p in (base / "projects").glob("*/memory") if p.is_dir())
        except OSError:
            pass
    managed = (
        Path("/Library/Application Support/ClaudeCode/managed-settings.json")
        if sys.platform == "darwin"
        else Path("/etc/claude-code/managed-settings.json")
    )
    for settings in (managed, config / "settings.json"):
        custom = _auto_memory_directory(settings)
        if custom is not None and custom.is_dir():
            found.add(custom)
    return sorted(found)


def _non_negative_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _same(path: Path) -> str:
    """A path key that ignores ``~``, ``..`` and symlinks."""
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    namespace: str
    memory_dirs: tuple[Path, ...] | None  # None: the default glob, read at scan time
    delete_grace_days: int
    archive_retention_days: int
    ticket_prefixes: tuple[str, ...] = ()  # labels.ticket_prefixes
    service_prefixes: tuple[str, ...] = ()  # labels.service_prefixes
    # NOBLIVION_MEMORY_DIR: the one folder the hooks guard and sync. The store
    # always indexes it too, so the hooks and the store agree on the folders.
    hook_memory_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def index_lock_path(self) -> Path:
        return self.data_dir / INDEX_LOCK_FILE

    def resolved_memory_dirs(self) -> list[Path]:
        """``memory_dirs`` (or the default glob), plus ``NOBLIVION_MEMORY_DIR``
        when it is set and not in the list yet."""
        dirs = default_memory_dirs() if self.memory_dirs is None else list(self.memory_dirs)
        extra = self.hook_memory_dir
        if extra is not None and extra.is_dir():
            known = {_same(p) for p in dirs}
            if _same(extra) not in known:
                dirs.append(extra)
        return dirs


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

    raw_hook_dir = env.get("NOBLIVION_MEMORY_DIR", "").strip()
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
        hook_memory_dir=Path(os.path.expanduser(raw_hook_dir)) if raw_hook_dir else None,
    )


def _env_or_cfg(env: Mapping[str, str], cfg: Mapping, env_key: str, dotted: str) -> object:
    raw = env.get(env_key, "").strip()
    return raw if raw else lookup(cfg, dotted)


def _choice(value: object, choices: tuple[str, ...], default: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in choices else default


def _unit_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if 0.0 < number <= 1.0 else default


@dataclass(frozen=True)
class StoreSettings:
    """The keys the store process reads (section 12.3)."""

    port: int = DEFAULT_PORT  # 0: any free port
    idle_exit_s: int = DEFAULT_IDLE_EXIT_S  # 0: never
    index_interval_s: int = DEFAULT_INDEX_INTERVAL_S  # 0: no periodic scan
    shared_roots: tuple[str, ...] = ()
    trust_ranking: str = "off"
    prior_mined: float = DEFAULT_PRIOR_MINED
    log_level: str = "info"


def load_store_settings(env: Mapping[str, str] | None = None) -> StoreSettings:
    env = os.environ if env is None else env
    cfg = load_file(env)
    port = _non_negative_int(_env_or_cfg(env, cfg, "NOBLIVION_PORT", "port"), DEFAULT_PORT)
    return StoreSettings(
        port=port if port <= 65535 else DEFAULT_PORT,
        idle_exit_s=_non_negative_int(
            _env_or_cfg(env, cfg, "NOBLIVION_IDLE_EXIT_S", "idle_exit_s"), DEFAULT_IDLE_EXIT_S
        ),
        index_interval_s=_non_negative_int(
            _env_or_cfg(env, cfg, "NOBLIVION_INDEX_INTERVAL_S", "index.interval_s"),
            DEFAULT_INDEX_INTERVAL_S,
        ),
        shared_roots=string_list(cfg, "recall.shared_roots"),
        trust_ranking=_choice(
            _env_or_cfg(env, cfg, "NOBLIVION_TRUST_RANKING", "trust.ranking"),
            TRUST_RANKING_MODES,
            "off",
        ),
        prior_mined=_unit_float(lookup(cfg, "trust.prior_mined"), DEFAULT_PRIOR_MINED),
        log_level=_choice(
            _env_or_cfg(env, cfg, "NOBLIVION_LOG_LEVEL", "log_level"), LOG_LEVELS, "info"
        ),
    )
