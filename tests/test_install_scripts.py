# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/install.sh``, ``scripts/uninstall.sh`` and the SessionStart notice
(design doc 0001, sections 13.2 and 13.3).

Each script runs in a temp HOME with a clean env. A fake ``uv`` on PATH logs
its calls, so a dry run proves that no install command runs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not found")


def _bin(tmp_path: Path, with_uv: bool) -> Path:
    """A PATH folder with python3, the few tools the scripts use and,
    optionally, a fake uv that only logs its arguments."""
    folder = tmp_path / "bin"
    folder.mkdir(parents=True)
    (folder / "python3").symlink_to(sys.executable)
    for tool in (
        "dirname",
        "sed",
        "mkdir",
        "chmod",
        "rm",
        "tr",
        "grep",
        "ps",
        "kill",
        "env",
        "install",
    ):
        found = shutil.which(tool)
        if found:
            (folder / tool).symlink_to(found)
    if with_uv:
        log = tmp_path / "uv.log"
        (folder / "uv").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
        (folder / "uv").chmod(0o755)
    return folder


def _run(script: Path, args: list[str], tmp_path: Path, *, with_uv: bool = True, extra=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "PATH": str(_bin(tmp_path, with_uv)), "LANG": "C"}
    env.update(extra or {})
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_install_dry_run_changes_nothing(tmp_path):
    proc = _run(INSTALL, ["--dry-run"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = tmp_path / "home" / ".local" / "share" / "noblivion"
    assert f"data dir {data}" in proc.stdout
    assert "would run: uv venv" in proc.stdout
    assert "--extra embed" in proc.stdout
    assert "config.default.json" in proc.stdout
    assert "would download the embedding model" in proc.stdout
    assert "migrate-from-legacy" in proc.stdout
    assert not (tmp_path / "uv.log").exists(), "a dry run called uv"
    assert not data.exists()
    assert sorted(p.name for p in (tmp_path / "home").iterdir()) == []


def test_install_data_dir_order(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    proc = _run(INSTALL, ["--dry-run"], tmp_path, extra={"CLAUDE_PLUGIN_DATA": str(plugin_data)})
    assert f"data dir {plugin_data}" in proc.stdout
    proc = _run(
        INSTALL,
        ["--dry-run", "--data-dir", str(tmp_path / "flag"), "--no-embed"],
        tmp_path / "x",
        extra={"CLAUDE_PLUGIN_DATA": str(plugin_data)},
    )
    assert f"data dir {tmp_path / 'flag'}" in proc.stdout
    assert "--extra embed" not in proc.stdout
    assert "download" not in proc.stdout


def test_install_sets_an_existing_data_dir_to_0700(tmp_path):
    """Claude Code makes the data dir with the user's umask before install.sh
    runs; install.sh must still leave it 0700 (NOBLIVION-27)."""
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o775)
    data.chmod(0o775)
    _run(INSTALL, ["--no-embed"], tmp_path, extra={"CLAUDE_PLUGIN_DATA": str(data)})
    assert stat.S_IMODE(data.stat().st_mode) == 0o700


def test_install_without_uv_prints_the_official_command(tmp_path):
    proc = _run(INSTALL, [], tmp_path, with_uv=False)
    assert proc.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in proc.stderr
    assert not (tmp_path / "home" / ".local").exists()


def test_install_refuses_an_unknown_option(tmp_path):
    proc = _run(INSTALL, ["--bogus"], tmp_path)
    assert proc.returncode == 1
    assert "unknown option --bogus" in proc.stderr


def test_uninstall_dry_run_and_run_keep_the_database(tmp_path):
    data = tmp_path / "data"
    (data / "venv" / "bin").mkdir(parents=True)
    (data / "models").mkdir()
    (data / "noblivion.db").write_text("db")
    (data / "token").write_text("t")
    proc = _run(UNINSTALL, ["--data-dir", str(data), "--dry-run"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "would remove" in proc.stdout
    assert (data / "venv").exists() and (data / "models").exists()
    proc = _run(UNINSTALL, ["--data-dir", str(data)], tmp_path / "again")
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in data.iterdir()) == ["noblivion.db", "token"]
    # The next step keeps the data too: plain "claude plugin uninstall"
    # deletes the data dir (NOBLIVION-26).
    assert "claude plugin uninstall noblivion --keep-data" in proc.stdout


def test_uninstall_refuses_a_folder_that_is_not_a_data_dir(tmp_path):
    folder = tmp_path / "other"
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("x")
    proc = _run(UNINSTALL, ["--data-dir", str(folder), "--purge"], tmp_path)
    assert proc.returncode == 1
    assert (folder / "keep.txt").exists()


# ── SessionStart notice (hooks/store_client.py) ─────────────────────────────


def _store_client():
    path = ROOT / "hooks" / "store_client.py"
    spec = importlib.util.spec_from_file_location("_e8_store_client", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_notice_when_the_venv_is_missing(tmp_path):
    sc = _store_client()
    env = {"NOBLIVION_DATA_DIR": str(tmp_path), "CLAUDE_PLUGIN_DATA": "/pd"}
    text = sc.install_notice("no_launcher", env, root=ROOT)
    assert "not installed" in text
    assert f'CLAUDE_PLUGIN_DATA="/pd" bash "{ROOT}/scripts/install.sh"' in text
    assert sc.install_notice("off", env, root=ROOT) is None


def test_notice_when_the_venv_is_for_another_version(tmp_path):
    sc = _store_client()
    env = {"NOBLIVION_DATA_DIR": str(tmp_path)}
    (tmp_path / "venv").mkdir()
    stamp = tmp_path / "venv" / "noblivion-install.json"
    assert sc.install_notice("spawned", env, root=ROOT) is None  # no stamp
    stamp.write_text(json.dumps({"version": sc.plugin_version(ROOT)}))
    assert sc.install_notice("spawned", env, root=ROOT) is None
    stamp.write_text(json.dumps({"version": "0.0.0-old"}))
    text = sc.install_notice("spawned", env, root=ROOT)
    assert "built for plugin version 0.0.0-old" in text


def test_session_start_prints_the_notice_only_under_the_plugin(tmp_path):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    base["NOBLIVION_DATA_DIR"] = str(tmp_path / "data")
    hook = ROOT / "hooks" / "store_client.py"
    for extra, expect in (({}, ""), ({"CLAUDE_PLUGIN_ROOT": str(ROOT)}, "not installed")):
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input="{}",
            capture_output=True,
            text=True,
            env={**base, **extra},
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0
        assert (expect in proc.stdout) if expect else proc.stdout == ""


# ── the venv's install stamp (a terminal command finds the plugin) ──────────


def test_a_venv_command_finds_its_data_dir_and_plugin_root(tmp_path, monkeypatch):
    from noblivion import config, labels

    venv = tmp_path / "data" / "venv"
    venv.mkdir(parents=True)
    for name in ("NOBLIVION_DATA_DIR", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert config.data_dir() == tmp_path / "xdg" / "noblivion"  # no stamp
    assert labels.installed_plugin_root() is None
    (venv / config.INSTALL_STAMP).write_text(
        json.dumps({"version": "0.0.0", "plugin_root": str(ROOT)})
    )
    assert config.data_dir() == tmp_path / "data"
    assert labels.installed_plugin_root() == ROOT
    assert labels.rules_path() == ROOT / "hooks" / "memory_labels.py"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "pd"))
    assert config.data_dir() == tmp_path / "pd"  # the env still wins


def test_an_installed_package_finds_the_plugin_hooks_through_the_stamp(tmp_path, monkeypatch):
    """A non-editable install puts the package in site-packages, away from the
    plugin. ``noblivion index`` (label rules, ``memory_labels.py``) and
    ``noblivion dedup`` (field check, ``memory_fields.py``) then find the
    plugin's ``hooks/`` folder only through the install stamp."""
    from noblivion import config, dedup, labels

    venv = tmp_path / "data" / "venv"
    site = venv / "lib" / "site-packages" / "noblivion"
    site.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(labels, "__file__", str(site / "labels.py"))
    assert labels.rules_path() is None
    with pytest.raises(dedup.DedupError):
        dedup.load_memory_fields()
    (venv / config.INSTALL_STAMP).write_text(json.dumps({"plugin_root": str(ROOT)}))
    dirs = labels.hooks_dirs()
    assert dirs[0] == ROOT / "hooks"
    for name in ("memory_labels.py", "memory_fields.py"):
        assert (dirs[0] / name).is_file()
    assert labels.rules_path() == ROOT / "hooks" / "memory_labels.py"
    fields = dedup.load_memory_fields()
    assert Path(fields.__file__).resolve() == (ROOT / "hooks" / "memory_fields.py").resolve()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_the_plugin_ships_every_hook_file():
    """The plugin is the repo: every hook file must be tracked, none ignored."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "hooks", "mcp", "scripts", "config", ".claude-plugin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = set(proc.stdout.split())
    on_disk = {
        str(p.relative_to(ROOT))
        for folder in ("hooks", "mcp", "scripts", "config", ".claude-plugin")
        for p in (ROOT / folder).iterdir()
        if p.is_file() and p.suffix in (".py", ".json", ".sh")
    }
    assert on_disk <= tracked, sorted(on_disk - tracked)
