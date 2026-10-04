# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion migrate-from-legacy`` (design doc 0001, section 14).

A fake home holds old hook files, a link to one, an unrelated hook, an
unknown ``claude_code_*.py``, a ``settings.json`` with old and unrelated hook
entries (one matcher group mixes both) and a ``.mcp.json`` with an old and an
unrelated server. The dry run changes nothing; ``--apply`` removes only the
old entries and writes backups; ``--undo`` restores the files byte for byte.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from noblivion import legacy
from noblivion.__main__ import main as cli_main

UNRELATED_STYLE = {
    "type": "command",
    "command": "python3 ~/.claude/hooks/my-style.py",
    "timeout": 5,
}
UNRELATED_GUARD = {"type": "command", "command": "bash $HOME/.claude/hooks/protect.sh"}


def _settings(hooks_dir: Path) -> dict:
    return {
        "model": "opus",
        "env": {"OLDMEM_RECALL_INDEX": "1", "OLDMEM_RECALL_URL": "x", "EDITOR": "vi"},
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [UNRELATED_STYLE]},
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "env OLDMEM_RECALL_INDEX_K=30 "
                            "OLDMEM_TRUST_EVENTS_KEY_FILE=/k "
                            f"python3 {hooks_dir}/my-recall.py",
                            "timeout": 5,
                        }
                    ]
                },
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        UNRELATED_GUARD,
                        {
                            "type": "command",
                            "command": "sh -c 'OLDMEM_GUARD_LOG=1 python "
                            "~/.claude/hooks/claude_code_guard_hook.py; exit 0'",
                        },
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${HOME}/.claude/hooks/claude_code_recall_hook.py "
                            "--reset-rows",
                        }
                    ],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "python3 /opt/x/claude_code.py"}]}],
        },
    }


def _mcp() -> dict:
    return {
        "mcpServers": {
            "old-recall": {
                "command": "python3",
                "args": ["~/.claude/hooks/claude_code_recall_mcp.py"],
                "env": {"OLDMEM_RECALL_PROJECT": "claude_code"},
            },
            "other": {"command": "npx", "args": ["some-server"]},
        }
    }


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("guard_hook", "recall_hook", "recall_mcp", "memory_text"):
        (hooks / f"claude_code_{name}.py").write_text(f"# old {name}\n")
    (hooks / "my-recall.py").symlink_to("claude_code_recall_hook.py")
    (hooks / "my-style.py").write_text("# unrelated\n")
    (hooks / "protect.sh").write_text("# unrelated\n")
    (hooks / "claude_code_someone_else.py").write_text("# not ours\n")
    (hooks / "claude_code_guard_hook.py.bak-1").write_text("# a backup copy\n")
    (home / ".claude" / "settings.json").write_text(json.dumps(_settings(hooks), indent=4))
    (home / ".mcp.json").write_text(json.dumps(_mcp(), indent=4))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    return home


def _snapshot(home: Path) -> dict[str, bytes | str]:
    out: dict[str, bytes | str] = {}
    for path in sorted(home.rglob("*")):
        key = str(path.relative_to(home))
        if path.is_symlink():
            out[key] = "link:" + str(path.readlink())
        elif path.is_file():
            out[key] = path.read_bytes()
    return out


def test_dry_run_lists_the_plan_and_changes_nothing(home, capsys):
    before = _snapshot(home)
    assert cli_main(["migrate-from-legacy", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert _snapshot(home) == before
    assert "Dry run: nothing changed" in out
    plan = legacy.build_plan(home)
    hooks = home / ".claude" / "hooks"
    assert sorted(p.name for p in plan.files) == [
        "claude_code_guard_hook.py",
        "claude_code_memory_text.py",
        "claude_code_recall_hook.py",
        "claude_code_recall_mcp.py",
        "my-recall.py",
    ]
    assert plan.unknown == [hooks / "claude_code_someone_else.py"]
    assert [(e["event"], e["matcher"]) for e in plan.settings_entries] == [
        ("UserPromptSubmit", ""),
        ("PreToolUse", "Bash"),
        ("SessionStart", "compact"),
    ]
    assert plan.mcp_servers == ["old-recall"]
    assert plan.env_vars == {
        "OLDMEM_GUARD_LOG": "NOBLIVION_GUARD_LOG",
        "OLDMEM_RECALL_INDEX": "NOBLIVION_RECALL_INDEX",
        "OLDMEM_RECALL_INDEX_K": "NOBLIVION_RECALL_INDEX_K",
        "OLDMEM_RECALL_PROJECT": "NOBLIVION_RECALL_PROJECT",
        "OLDMEM_RECALL_URL": None,
        "OLDMEM_TRUST_EVENTS_KEY_FILE": None,
    }


def test_apply_removes_only_old_entries_and_writes_backups(home, tmp_path, capsys):
    settings_file = home / ".claude" / "settings.json"
    mcp_file = home / ".mcp.json"
    old_settings, old_mcp = settings_file.read_bytes(), mcp_file.read_bytes()
    hooks = home / ".claude" / "hooks"
    assert cli_main(["migrate-from-legacy", "--home", str(home), "--apply"]) == 0
    assert "Applied." in capsys.readouterr().out

    new = json.loads(settings_file.read_text())
    original = json.loads(old_settings)
    # Every key other than "hooks" is unchanged, in the same order.
    assert [k for k in new] == [k for k in original]
    assert {k: v for k, v in new.items() if k != "hooks"} == {
        k: v for k, v in original.items() if k != "hooks"
    }
    assert new["hooks"] == {
        "UserPromptSubmit": [{"hooks": [UNRELATED_STYLE]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [UNRELATED_GUARD]}],
        # claude_code.py is not an old hook file name: kept.
        "Stop": [{"hooks": [{"type": "command", "command": "python3 /opt/x/claude_code.py"}]}],
    }
    assert json.loads(mcp_file.read_text())["mcpServers"] == {
        "other": {"command": "npx", "args": ["some-server"]}
    }

    backups = sorted(home.rglob("*.noblivion-backup-*"))
    assert {b.name.split(".noblivion-backup-")[0] for b in backups} == {
        "settings.json",
        ".mcp.json",
    }
    by_name = {b.name.split(".noblivion-backup-")[0]: b for b in backups}
    assert by_name["settings.json"].read_bytes() == old_settings
    assert by_name[".mcp.json"].read_bytes() == old_mcp

    left = sorted(p.name for p in hooks.iterdir())
    assert left == [
        "claude_code_guard_hook.py.bak-1",
        "claude_code_someone_else.py",
        "my-style.py",
        "protect.sh",
    ]
    (run,) = (tmp_path / "data" / "migrated").iterdir()
    assert (run / "my-recall.py").is_symlink()
    assert (run / "claude_code_guard_hook.py").read_text() == "# old guard_hook\n"
    manifest = json.loads((run / "manifest.json").read_text())
    assert len(manifest["moved"]) == 5 and len(manifest["backups"]) == 2


def test_undo_restores_the_files(home, capsys):
    before = _snapshot(home)
    assert cli_main(["migrate-from-legacy", "--home", str(home), "--apply"]) == 0
    assert cli_main(["migrate-from-legacy", "--home", str(home), "--undo"]) == 0
    after = {k: v for k, v in _snapshot(home).items() if ".noblivion-backup-" not in k}
    assert after == before
    assert cli_main(["migrate-from-legacy", "--undo"]) == 0
    assert "Nothing to undo." in capsys.readouterr().out


def test_nothing_to_do_in_a_clean_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / ".claude").mkdir()
    assert cli_main(["migrate-from-legacy", "--home", str(tmp_path), "--apply"]) == 0
    assert "Nothing to do." in capsys.readouterr().out
    assert not (tmp_path / "data").exists()


def test_rename_env_table():
    assert legacy.rename_env("OLD_RECALL_INDEX_RERANK") == (True, "NOBLIVION_RECALL_INDEX_RERANK")
    assert legacy.rename_env("OLD_RECALL_API_KEY") == (True, None)
    assert legacy.rename_env("NOBLIVION_RECALL_INDEX") == (False, None)
    assert legacy.rename_env("EDITOR") == (False, None)
    assert legacy.rename_env("XDG_DATA_HOME") == (False, None)


def test_runs_legacy_needs_a_known_file_name(tmp_path):
    assert legacy.runs_legacy("python3 /x/claude_code_stop_checks.py --mark-correction", tmp_path)
    assert not legacy.runs_legacy("python3 /x/claude_code_stop_checks.py.bak", tmp_path)
    assert not legacy.runs_legacy("python3 /x/claude_code_custom.py", tmp_path)
    assert not legacy.runs_legacy(None, tmp_path)


def test_another_profile_leaves_the_default_profile_alone(home, tmp_path, monkeypatch, capsys):
    """NOBLIVION-40: with CLAUDE_CONFIG_DIR set, the default profile's
    settings, hooks and ``~/.mcp.json`` are not listed and not changed."""
    before = _snapshot(home)
    profile = tmp_path / "profile"
    (profile / "hooks").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    assert cli_main(["migrate-from-legacy", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["claude_dir"] == str(profile)
    assert plan["mcp_file"] is None
    assert plan["files"] == [] and plan["settings_entries"] == [] and plan["mcp_servers"] == []
    assert cli_main(["migrate-from-legacy", "--home", str(home), "--apply"]) == 0
    assert "Nothing to do." in capsys.readouterr().out
    assert _snapshot(home) == before
    assert not (tmp_path / "data" / "migrated").exists()


def test_another_profile_with_old_hooks_is_migrated(home, tmp_path, monkeypatch, capsys):
    """The profile that CLAUDE_CONFIG_DIR names is the one --apply changes."""
    before = _snapshot(home)
    profile = tmp_path / "profile"
    shutil.copytree(home / ".claude", profile, symlinks=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    assert cli_main(["migrate-from-legacy", "--apply", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)["applied"]
    assert {Path(b["file"]) for b in applied["backups"]} == {profile / "settings.json"}
    assert all(Path(m["from"]).parent == profile / "hooks" for m in applied["moved"])
    assert _snapshot(home) == before


def test_config_dir_option_wins_over_the_env(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    args = ["migrate-from-legacy", "--home", str(home), "--json"]
    assert cli_main([*args, "--config-dir", str(home / ".claude")]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["mcp_file"] == str(home / ".mcp.json") and plan["settings_entries"]
