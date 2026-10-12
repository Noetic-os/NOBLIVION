# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stable MCP launcher (NOBLIVION-99).

What these tests hold:

1. It finds ``mcp/recall_mcp.py`` of the installed version from
   ``installed_plugins.json``, and follows a new install with no change to
   the launcher.
2. It starts that server as a real process, with the arguments it was given.
3. A missing registry, an unknown key or a missing server file gives exit 1
   and one line on stderr, never a traceback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "noblivion_mcp_launcher.py"
spec = importlib.util.spec_from_file_location("launcher_t", SCRIPT)
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def install(config: Path, version: str, key: str = "noblivion@noblivion") -> Path:
    """Add a fake install whose server prints its version and arguments."""
    root = config / "plugins" / "cache" / "mk" / "noblivion" / version
    (root / "mcp").mkdir(parents=True)
    (root / "mcp" / "recall_mcp.py").write_text(
        "import sys\nprint('server " + version + " ' + ' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    registry = config / "plugins" / "installed_plugins.json"
    doc = {"version": 2, "plugins": {key: [{"scope": "user", "installPath": str(root)}]}}
    registry.write_text(json.dumps(doc), encoding="utf-8")
    return root


def run(config: Path, *args: str, key: str | None = None):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config)}
    if key:
        env["NOBLIVION_PLUGIN_KEY"] = key
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=20
    )


def test_runs_the_installed_server_with_its_arguments(tmp_path):
    install(tmp_path, "0.1.9")
    done = run(tmp_path, "--flag", "x")
    assert done.returncode == 0
    assert done.stdout.strip() == "server 0.1.9 --flag x"


def test_follows_a_new_install_without_a_change(tmp_path):
    install(tmp_path, "0.1.9")
    assert run(tmp_path).stdout.strip() == "server 0.1.9"
    install(tmp_path, "0.1.11")
    assert run(tmp_path).stdout.strip() == "server 0.1.11"


def test_plugin_key_can_be_set(tmp_path):
    install(tmp_path, "0.2.0", key="noblivion@other")
    assert run(tmp_path).returncode == 1
    assert run(tmp_path, key="noblivion@other").stdout.strip() == "server 0.2.0"


def test_missing_registry_is_one_line_not_a_traceback(tmp_path):
    done = run(tmp_path)
    assert done.returncode == 1
    assert "Traceback" not in done.stderr
    assert done.stderr.startswith("noblivion-mcp: ")
    assert len(done.stderr.strip().splitlines()) == 1


BAD_REGISTRIES = [
    "{not json",
    "[]",
    '{"plugins": []}',
    '{"plugins": {"noblivion@noblivion": 5}}',
]


@pytest.mark.parametrize("text", BAD_REGISTRIES)
def test_bad_registry_is_exit_one(tmp_path, text):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text(text, encoding="utf-8")
    done = run(tmp_path)
    assert done.returncode == 1 and "Traceback" not in done.stderr


def test_missing_server_file_is_exit_one(tmp_path):
    root = install(tmp_path, "0.1.9")
    (root / "mcp" / "recall_mcp.py").unlink()
    done = run(tmp_path)
    assert done.returncode == 1 and "recall_mcp.py" in done.stderr
