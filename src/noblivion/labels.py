# SPDX-License-Identifier: AGPL-3.0-or-later
"""The indexer's labeller: the label rules of the hooks (design doc 0001, section 5.2).

The rules live in one place, ``hooks/memory_labels.py``. The hooks are stdlib
and run on the user's ``python3`` without this package, so the rules stay
there and this module loads that file by path. No hook imports this package:
there is no import cycle.

The hooks folder is found in this order:

1. ``${CLAUDE_PLUGIN_ROOT}/hooks`` (the installed plugin; the memory sync
   hook passes the variable to the indexer command), then the plugin root
   in the venv's install stamp (``install.sh``; a command run from a
   terminal);
2. the ``hooks`` folder next to this package (the plugin layout, where the
   package is ``<plugin root>/noblivion``);
3. the ``hooks`` folder of the source tree (``src/noblivion`` -> ``hooks``).

The ticket and service prefixes come from the store's config
(``labels.ticket_prefixes`` and ``labels.service_prefixes``), so a row gets the
labels that the guard table (``hooks/guard_table.py``) computes for the same
file. With no prefix set, no ticket key or service label is made; file and tool
labels need no config.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import ModuleType

from noblivion import config

Labeller = Callable[[str, str], Iterable[str]]  # the same shape as indexer.Labeller

RULES_FILE = "memory_labels.py"
_MODULE_NAME = "_noblivion_hook_memory_labels"
_CACHE: dict[str, ModuleType] = {}


def hooks_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    """The folders that may hold ``memory_labels.py``, in search order."""
    env = os.environ if env is None else env
    out: list[Path] = []
    root = env.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if root:
        out.append(Path(os.path.expanduser(root)) / "hooks")
    stamp = installed_plugin_root()
    if stamp is not None:
        out.append(stamp / "hooks")
    here = Path(__file__).resolve().parent
    out.append(here.parent / "hooks")
    out.append(here.parent.parent / "hooks")
    return out


def installed_plugin_root() -> Path | None:
    """The plugin root that ``install.sh`` built this venv from (its stamp
    file in the venv), or ``None``."""
    try:
        doc = json.loads((Path(sys.prefix) / config.INSTALL_STAMP).read_text(encoding="utf-8"))
        root = doc.get("plugin_root")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(root) if isinstance(root, str) and root else None


def rules_path(env: Mapping[str, str] | None = None) -> Path | None:
    """The first ``memory_labels.py`` found, or ``None``."""
    for folder in hooks_dirs(env):
        candidate = folder / RULES_FILE
        if candidate.is_file():
            return candidate
    return None


def load_rules(path: Path) -> ModuleType:
    """Load ``memory_labels.py`` from ``path`` (once per path and process)."""
    key = str(path.resolve())
    mod = _CACHE.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CACHE[key] = mod
    return mod


def make_labeller(
    settings: config.Settings, env: Mapping[str, str] | None = None
) -> Labeller | None:
    """A labeller for ``indexer.scan``: ``(text, file stem) -> labels``.
    ``None`` when the rules file is not found or does not load; the scan then
    stores no labels."""
    path = rules_path(env)
    if path is None:
        return None
    try:
        mod = load_rules(path)
        rules = mod.rules(settings.ticket_prefixes, settings.service_prefixes)
    except Exception:  # noqa: BLE001 - labels fail open, the scan still runs
        return None

    def labeller(text: str, stem: str) -> list[str]:
        return list(mod.memory_labels(text, stem, rules))

    return labeller
