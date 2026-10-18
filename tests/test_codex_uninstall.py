# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex integration removal preserves independent configuration and backups."""

import json
import os
import subprocess
import sys

import pytest

from test_codex_adapter import ROOT, _installer


def configured(home):
    home.mkdir(parents=True)
    installer = _installer()
    foreign = {"type": "command", "command": "python3 /other/guard.py"}
    mixed = {
        "matcher": ".*",
        "hooks": [
            {"type": "command", "command": "python3 /release/codex/adapter.py --config /config"},
            foreign,
        ],
    }
    doc = {"description": "keep", "hooks": {event: [mixed] for event in installer.EVENTS}}
    (home / "hooks.json").write_text(json.dumps(doc))
    preserved = "[hooks.state]\nfixture = { approved = true, value = 'opaque' }\n"
    (home / "config.toml").write_text(
        "model = 'fixture-model'\n" + installer.BEGIN + "\n"
        "[mcp_servers.noblivion]\ncommand='python3'\n"
        "[mcp_servers.noblivion.env]\nOLD='value'\n"
        + preserved
        + installer.END
        + "\n[mcp_servers.other]\ncommand='other'\n"
    )
    (home / "noblivion").mkdir()
    (home / "noblivion/config.json").write_text('{"preserve": true}')
    return preserved, foreign


def invoke(home, *args, env=None):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/install_codex.py"),
            "--uninstall",
            "--codex-home",
            str(home),
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def test_uninstall_keeps_foreign_hooks_trust_servers_and_private_state(tmp_path):
    home = tmp_path / "codex"
    preserved, foreign = configured(home)
    before = {name: (home / name).read_bytes() for name in ("hooks.json", "config.toml")}
    result = invoke(home)
    assert result.returncode == 0, result.stderr
    hooks = json.loads((home / "hooks.json").read_text())
    for groups in hooks["hooks"].values():
        assert groups == [{"matcher": ".*", "hooks": [foreign]}]
    toml = (home / "config.toml").read_text()
    assert preserved in toml and "[mcp_servers.other]" in toml
    assert "mcp_servers.noblivion" not in toml and _installer().BEGIN not in toml
    assert (home / "noblivion/config.json").read_text() == '{"preserve": true}'
    backups = list((home / "noblivion/backups").glob("*/*"))
    assert len(backups) == 2
    for path in backups:
        assert path.read_bytes() == before[path.name]
        assert path.stat().st_mode & 0o777 == 0o600
    assert invoke(home).returncode == 0
    assert len(list((home / "noblivion/backups").glob("*/*"))) == 2


def test_uninstall_dry_run_and_absent_home_do_not_write(tmp_path):
    home = tmp_path / "codex"
    configured(home)
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    result = invoke(home, "--dry-run")
    assert result.returncode == 0 and json.loads(result.stdout)["dry_run"]
    assert before == {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    missing = tmp_path / "missing"
    assert invoke(missing).returncode == 0
    assert not missing.exists()


def test_invalid_toml_markers_leave_both_files_intact(tmp_path):
    home = tmp_path / "codex"
    configured(home)
    (home / "config.toml").write_text(_installer().BEGIN)
    before = (home / "hooks.json").read_bytes()
    assert invoke(home).returncode != 0
    assert (home / "hooks.json").read_bytes() == before
    assert not (home / "noblivion/backups").exists()


def test_unmanaged_toml_and_literal_marker_are_preserved(tmp_path):
    installer = _installer()
    text = "[mcp_servers.noblivion]\ncommand='custom'\ntext='''\n" + installer.BEGIN + "\n'''\n"
    assert installer._remove_toml(text) == text


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_install_respects_codex_home_and_explicit_precedence(tmp_path, explicit):
    data, memory, workspace = (tmp_path / name for name in ("data", "memory", "project"))
    for folder in (data, memory, workspace):
        folder.mkdir()
    executable = data / "venv/bin/noblivion"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (data / "config.json").write_text(json.dumps({"memory_dirs": [str(memory)]}))
    env_home, explicit_home = tmp_path / "env-codex", tmp_path / "explicit-codex"
    args = [
        sys.executable,
        str(ROOT / "scripts/install_codex.py"),
        "--data-dir",
        str(data),
        "--memory-dir",
        str(memory),
        "--workspace",
        str(workspace),
    ]
    if explicit:
        args.extend(["--codex-home", str(explicit_home)])
    result = subprocess.run(
        args, env={**os.environ, "CODEX_HOME": str(env_home)}, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    selected = explicit_home if explicit else env_home
    assert (selected / "hooks.json").is_file()
    if explicit:
        assert not env_home.exists()
    args = [sys.executable, str(ROOT / "scripts/install_codex.py"), "--uninstall"]
    if explicit:
        args.extend(["--codex-home", str(explicit_home)])
    result = subprocess.run(
        args, env={**os.environ, "CODEX_HOME": str(env_home)}, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert not any(
        _installer()._is_ours(g)
        for groups in json.loads((selected / "hooks.json").read_text())["hooks"].values()
        for g in groups
    )


@pytest.mark.parametrize("missing_data", [False, True])
def test_store_uninstall_detaches_codex_with_and_without_runtime(tmp_path, missing_data):
    home = tmp_path / "codex"
    configured(home)
    data = tmp_path / "store"
    if not missing_data:
        (data / "venv").mkdir(parents=True)
        (data / "noblivion.db").write_text("fixture")
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/uninstall.sh"),
            "--data-dir",
            str(data),
            "--codex-home",
            str(home),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "adapter.py" not in (home / "hooks.json").read_text()
    assert "[mcp_servers.noblivion]" not in (home / "config.toml").read_text()
    if not missing_data:
        assert not (data / "venv").exists()
        assert (data / "noblivion.db").read_text() == "fixture"


def test_compound_adapter_command_refuses_without_removing_other_guard(tmp_path):
    home = tmp_path / "codex"
    configured(home)
    path = home / "hooks.json"
    doc = json.loads(path.read_text())
    doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += (
        " " + chr(38) * 2 + " python3 /other/guard.py"
    )
    path.write_text(json.dumps(doc))
    before = {name: (home / name).read_bytes() for name in ("hooks.json", "config.toml")}
    result = invoke(home)
    assert result.returncode != 0 and "compound" in result.stderr
    assert before == {name: (home / name).read_bytes() for name in before}
    assert not (home / "noblivion/backups").exists()
