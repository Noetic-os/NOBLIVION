# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helper for the hook tests: load ``hooks/<name>.py`` by path.

The hooks are stdlib scripts, not a package, so each test loads the module
it needs from its file under a unique alias. ``HOOKS`` is the hooks folder.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def load_hook(name: str, alias: str | None = None) -> ModuleType:
    """Load ``hooks/<name>.py`` as a fresh module named ``alias``.

    The module is put in ``sys.modules`` before it runs, because a dataclass
    resolves its annotations through ``sys.modules``.
    """
    mod_name = alias or f"hooktest_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def load_hook_with_config(name: str, config: dict, alias: str | None = None) -> ModuleType:
    """Load ``hooks/<name>.py`` while ``NOBLIVION_CONFIG`` points at ``config``.

    Some hooks read the config file once, at import. The config file is a
    temporary file; the env var is restored after the import.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        old = os.environ.get("NOBLIVION_CONFIG")
        os.environ["NOBLIVION_CONFIG"] = str(path)
        try:
            return load_hook(name, alias)
        finally:
            if old is None:
                os.environ.pop("NOBLIVION_CONFIG", None)
            else:
                os.environ["NOBLIVION_CONFIG"] = old
