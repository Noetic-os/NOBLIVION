# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep the test session away from the user's real NOBLIVION data.

At start the session drops every setting that points at real data (the
``NOBLIVION_*`` variables except ``NOBLIVION_TEST_*``, and the Claude Code
folder variables) and points ``HOME`` and ``XDG_DATA_HOME`` at a temporary
folder. A test that loses its own settings (for example through
``monkeypatch.undo()``) then still writes under the temporary folder.

At the end the session compares the real data dir with a snapshot taken at
start. A test that changed a file there fails the session (NOBLIVION-36).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Claude Code sets these for hooks and for its Bash tool; a test that
# inherits them resolves the real memory folder or the real data dir.
CLAUDE_VARS = (
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
)

_real_dirs: list[Path] = []
_before: dict[Path, dict[str, tuple[int, int]]] = {}
_tmp_root: str | None = None


def _real_data_dirs(env: os._Environ[str]) -> list[Path]:
    """The data dirs a hook would use with the user's own settings: the
    ``NOBLIVION_DATA_DIR`` / ``CLAUDE_PLUGIN_DATA`` values and the default
    ``${XDG_DATA_HOME:-~/.local/share}/noblivion``."""
    dirs = []
    for name in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        raw = (env.get(name) or "").strip()
        if raw and "${" not in raw:
            dirs.append(Path(os.path.expanduser(raw)))
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "share"
    dirs.append(base / "noblivion")
    return list(dict.fromkeys(d.resolve() for d in dirs))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` of every file under ``root``."""
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def pytest_configure(config: pytest.Config) -> None:
    global _tmp_root
    _real_dirs[:] = _real_data_dirs(os.environ)
    for d in _real_dirs:
        _before[d] = _snapshot(d)
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") and not name.startswith("NOBLIVION_TEST_"):
            del os.environ[name]
    for name in CLAUDE_VARS:
        os.environ.pop(name, None)
    _tmp_root = tempfile.mkdtemp(prefix="noblivion-test-data-")
    home = Path(_tmp_root) / "home"
    home.mkdir()
    # HOME too: a hook given an explicit env mapping without XDG_DATA_HOME
    # falls back to Path.home(), which reads HOME from os.environ
    os.environ["HOME"] = str(home)
    os.environ["XDG_DATA_HOME"] = str(Path(_tmp_root) / "data")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changed = []
    for d in _real_dirs:
        after = _snapshot(d)
        before = _before.get(d, {})
        for rel in sorted(set(before) | set(after)):
            if before.get(rel) != after.get(rel):
                changed.append(str(d / rel))
    if _tmp_root:
        shutil.rmtree(_tmp_root, ignore_errors=True)
    if changed:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        lines = ["tests changed files in the real NOBLIVION data dir:"] + [
            f"  {p}" for p in changed[:20]
        ]
        if reporter is not None:
            reporter.write_line("\n".join(lines), red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
