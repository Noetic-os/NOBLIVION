# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data dir rules found by the v0.1.0 fresh-install test.

- NOBLIVION-24: a data dir value that still holds ``${`` was passed
  unexpanded by a config file; it counts as unset.
- NOBLIVION-27: the data dir is 0700 also when it existed before.
"""

from __future__ import annotations

import json
import runpy
import stat
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import config

ROOT = Path(__file__).resolve().parent.parent
hc = load_hook("hook_config", "hook_config_data_dir_rules")
sc = load_hook("store_client", "store_client_data_dir_rules")
UNEXPANDED = "${CLAUDE_PLUGIN_DATA}"


def test_an_unexpanded_plugin_data_value_counts_as_unset(tmp_path):
    env = {"CLAUDE_PLUGIN_DATA": UNEXPANDED, "XDG_DATA_HOME": str(tmp_path)}
    assert config.data_dir(env) != Path(UNEXPANDED)
    assert hc.data_dir(env) == tmp_path / "noblivion"
    good = {"CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin")}
    assert config.data_dir(good) == hc.data_dir(good) == tmp_path / "plugin"


def test_the_trust_flush_fallback_ignores_the_v010_env(tmp_path):
    """trust_flush reads the data dir with its own stdlib rule; it skips the
    literal text too."""
    trust_flush = load_hook("trust_flush", "trust_flush_data_dir_rules")
    env = {"CLAUDE_PLUGIN_DATA": UNEXPANDED, "XDG_DATA_HOME": str(tmp_path)}
    assert trust_flush._fallback_cache(env) == str(tmp_path / "noblivion" / "cache")


@pytest.mark.parametrize("module", [config, hc])
def test_private_dir_sets_an_existing_folder_to_0700(tmp_path, module):
    folder = tmp_path / "data"
    folder.mkdir(mode=0o775)
    folder.chmod(0o775)
    module.private_dir(folder)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    module.private_dir(tmp_path / "new" / "deep")
    assert stat.S_IMODE((tmp_path / "new" / "deep").stat().st_mode) == 0o700


def test_session_start_sets_the_data_dir_to_0700(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    folder.chmod(0o775)
    sc.secure_data_dir({"NOBLIVION_DATA_DIR": str(folder)})
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    sc.secure_data_dir({"NOBLIVION_DATA_DIR": str(tmp_path / "absent")})
    assert not (tmp_path / "absent").exists()  # it never makes the folder


def test_the_mcp_server_reports_the_plugin_version():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mod = runpy.run_path(str(ROOT / "mcp" / "recall_mcp.py"), run_name="recall_mcp_version")
    assert mod["SERVER_VERSION"] == plugin["version"]
