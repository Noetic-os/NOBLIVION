# SPDX-License-Identifier: AGPL-3.0-or-later
"""The old-version notice (NOBLIVION-99).

What these tests hold:

1. A session that runs an older version than the installed one gets one
   ``systemMessage`` line that names ``/reload-plugins``. Nothing goes to the
   model context.
2. The line prints once for each session and installed version, and again for a
   newer install or another session.
3. Silence: same version, a newer running version, an install entry that names
   the running version, a missing or broken registry, and any folder that is
   not the plugin cache layout (a checkout, a Codex release).
4. The hook runs from a copy in a cache layout as a real process, and exits 0
   on every input.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

vc = load_hook("version_check", "version_check_t_version_check")
REPO = HOOKS.parent


def build(tmp_path: Path, running: str, installed: list, layout: str = "cache") -> Path:
    """A plugin folder for ``running`` and a registry that lists ``installed``.

    Returns the plugin folder (the ``root`` the hook reads)."""
    plugins = tmp_path / "plugins"
    parent = plugins / "cache" / "mk" / "noblivion" if layout == "cache" else tmp_path / "other"
    root = parent / running
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "noblivion", "version": running}), encoding="utf-8"
    )
    plugins.mkdir(parents=True, exist_ok=True)
    rows = [{"scope": "user", "version": v, "installPath": str(parent / v)} for v in installed]
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"noblivion@mk": rows}}), encoding="utf-8"
    )
    return root


@pytest.mark.parametrize(
    ("running", "installed", "expected"),
    [
        ("0.1.9", ["0.1.11"], ("0.1.11", "0.1.9")),
        ("0.1.9", ["0.1.10", "0.1.11"], ("0.1.11", "0.1.9")),
        ("0.1.9", ["0.1.11", "0.1.10"], ("0.1.11", "0.1.9")),
        ("0.1.9", ["0.1.10"], ("0.1.10", "0.1.9")),  # numeric order, not text order
        ("0.1.2", ["0.1.10"], ("0.1.10", "0.1.2")),
        ("0.1.11", ["0.1.11"], None),
        ("0.1.11", ["0.1.9"], None),  # the session is newer than the registry
        ("0.1.9", ["0.1.9", "0.1.11"], None),  # an install entry still names it
        ("0.1.9", [], None),
    ],
)
def test_newer_installed(tmp_path, running, installed, expected):
    assert vc.newer_installed(build(tmp_path, running, installed)) == expected


def test_other_layout_is_silent(tmp_path):
    assert vc.newer_installed(build(tmp_path, "0.1.9", ["0.1.11"], layout="other")) is None


def test_broken_registry_is_silent(tmp_path):
    root = build(tmp_path, "0.1.9", ["0.1.11"])
    (tmp_path / "plugins" / "installed_plugins.json").write_text("{not json", encoding="utf-8")
    assert vc.newer_installed(root) is None
    (tmp_path / "plugins" / "installed_plugins.json").unlink()
    assert vc.newer_installed(root) is None


def test_broken_plugin_json_is_silent(tmp_path):
    root = build(tmp_path, "0.1.9", ["0.1.11"])
    (root / ".claude-plugin" / "plugin.json").write_text("[]", encoding="utf-8")
    assert vc.newer_installed(root) is None


def test_not_a_numeric_version_does_not_raise(tmp_path):
    root = build(tmp_path, "0.1.9", ["0.2.0-rc1"])
    assert vc.newer_installed(root) is None or vc.newer_installed(root)[1] == "0.1.9"


def test_message_names_the_command():
    text = vc.message("0.1.11", "0.1.9")
    assert "0.1.11" in text and "0.1.9" in text and "/reload-plugins" in text


def test_notice_prints_once_per_session_and_version(tmp_path):
    state = tmp_path / "cache" / "version-notice.json"
    assert vc.first_notice("s1", "0.1.11", state) is True
    assert vc.first_notice("s1", "0.1.11", state) is False
    assert vc.first_notice("s2", "0.1.11", state) is True
    assert vc.first_notice("s1", "0.1.12", state) is True
    assert vc.first_notice("s1", "0.1.12", state) is False


def test_state_keeps_only_the_newest_sessions(tmp_path):
    state = tmp_path / "version-notice.json"
    for n in range(vc.STATE_KEEP + 5):
        vc.first_notice(f"s{n}", "0.1.11", state)
    assert len(json.loads(state.read_text())) == vc.STATE_KEEP
    assert vc.first_notice(f"s{vc.STATE_KEEP + 4}", "0.1.11", state) is False


def test_unwritable_state_still_gives_the_notice(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert vc.first_notice("s1", "0.1.11", blocker / "sub" / "state.json") is True


def run_copy(tmp_path: Path, running: str, installed: list, stdin: str, data: Path):
    """Run a copy of the hook from a cache layout, as Claude Code does."""
    root = build(tmp_path, running, installed)
    for name in ("version_check.py", "hook_config.py"):
        shutil.copy(HOOKS / name, root / "hooks" / name)
    env = {**os.environ, "NOBLIVION_DATA_DIR": str(data)}
    return subprocess.run(
        [sys.executable, str(root / "hooks" / "version_check.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def test_real_process_prints_one_system_message(tmp_path):
    data = tmp_path / "data"
    event = json.dumps({"session_id": "abc", "hook_event_name": "UserPromptSubmit"})
    first = run_copy(tmp_path, "0.1.9", ["0.1.11"], event, data)
    assert first.returncode == 0
    out = json.loads(first.stdout)
    assert list(out) == ["systemMessage"]
    assert "/reload-plugins" in out["systemMessage"]
    again = subprocess.run(
        [sys.executable, str(tmp_path / "plugins/cache/mk/noblivion/0.1.9/hooks/version_check.py")],
        input=event,
        capture_output=True,
        text=True,
        env={**os.environ, "NOBLIVION_DATA_DIR": str(data)},
        timeout=20,
    )
    assert again.returncode == 0 and again.stdout == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", '{"session_id": 5}'])
def test_real_process_exits_zero_on_any_input(tmp_path, stdin):
    done = run_copy(tmp_path, "0.1.11", ["0.1.11"], stdin, tmp_path / "data")
    assert done.returncode == 0 and done.stdout == ""


def test_main_in_this_checkout_is_silent():
    out = io.StringIO()
    assert vc.main(stdin=io.StringIO("{}"), stdout=out, environ={}) == 0
    assert out.getvalue() == ""


def test_hook_is_registered_for_both_events():
    doc = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event in ("SessionStart", "UserPromptSubmit"):
        commands = [h["command"] for g in doc[event] for h in g["hooks"]]
        assert any("hooks/version_check.py" in c for c in commands), event
