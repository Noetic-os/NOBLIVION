# SPDX-License-Identifier: AGPL-3.0-or-later
"""The indexer stores the hooks' rule labels (NOBLIVION-19, design doc 0001, section 5.2)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from noblivion import __main__ as cli
from noblivion import config, db, indexer, labels

HOOKS = Path(__file__).resolve().parents[1] / "hooks"

NOTE = """---
name: demo backup note
description: The backup job of the demo shop.
type: project
---
The job runs restic from tools/backup/nightly_backup.sh.
It writes to acme-vault and the follow-up is DEMO-42.
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "NOBLIVION_DATA_DIR",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_PLUGIN_ROOT",
        "XDG_DATA_HOME",
        "NOBLIVION_CONFIG",
        "NOBLIVION_MEMORY_DIRS",
        "NOBLIVION_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    return tmp_path


def write_config(home: Path, doc: dict) -> None:
    (home / "data" / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def memory_folder(home: Path, files: dict[str, str]) -> Path:
    folder = home / ".claude" / "projects" / "demo-proj" / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def stored_labels(home: Path) -> dict[str, list[str]]:
    conn = db.open_db(home / "data" / "noblivion.db")
    try:
        cur = conn.execute("SELECT path, labels FROM memories")
        return {path: json.loads(raw) for path, raw in cur.fetchall()}
    finally:
        conn.close()


def run_index(capsys) -> str:
    assert cli.main(["index", "--json"]) == indexer.EXIT_OK
    return capsys.readouterr().err


def hook_rules():
    """``hooks/memory_labels.py`` loaded fresh, the way the guard table loads it."""
    spec = importlib.util.spec_from_file_location("_test_memory_labels", HOOKS / "memory_labels.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rule_labels_reach_the_db_without_config(home, capsys):
    memory_folder(home, {"project_backup.md": NOTE, "topic_backup.md": NOTE})
    assert "not found" not in run_index(capsys)
    got = stored_labels(home)
    # File and tool labels need no config.
    assert got["project_backup.md"] == ["nightly_backup.sh", "restic"]
    # Index and topic files get no labels.
    assert got["topic_backup.md"] == []


def test_no_config_gives_no_ticket_or_service_labels(home, capsys):
    memory_folder(home, {"project_keys.md": "Tickets DEMO-7 and DEMO-8 touch acme-db.\n"})
    run_index(capsys)
    assert stored_labels(home) == {"project_keys.md": []}
    assert config.load_settings().ticket_prefixes == ()
    assert config.load_settings().service_prefixes == ()


def test_config_prefixes_give_key_and_service_labels(home, capsys):
    write_config(home, {"labels": {"ticket_prefixes": ["demo"], "service_prefixes": ["ACME"]}})
    memory_folder(home, {"project_backup.md": NOTE})
    run_index(capsys)
    assert stored_labels(home)["project_backup.md"] == [
        "DEMO-42",
        "acme-vault",
        "nightly_backup.sh",
        "restic",
    ]


def test_dotted_config_keys_and_bad_prefixes(home, capsys):
    write_config(
        home,
        {"labels.ticket_prefixes": ["DEMO", "bad-prefix", "", 7], "labels.service_prefixes": "x"},
    )
    memory_folder(home, {"user_note.md": "See DEMO-3 about acme-db.\n"})
    run_index(capsys)
    assert stored_labels(home) == {"user_note.md": ["DEMO-3"]}


def test_rows_hold_the_labels_the_hooks_compute(home, capsys):
    write_config(home, {"labels": {"ticket_prefixes": ["DEMO"], "service_prefixes": ["acme"]}})
    memory_folder(home, {"project_backup.md": NOTE})
    run_index(capsys)
    # The hook reads the same config file through hook_config (NOBLIVION_DATA_DIR).
    hook_side = hook_rules().memory_labels(NOTE, "project_backup")
    assert stored_labels(home)["project_backup.md"] == sorted(hook_side)


def test_the_file_stem_is_passed_not_the_front_matter_name(home, capsys):
    text = "---\nname: Other name\n---\nThe runbook deploy_steps.md and run_all_checks.sh.\n"
    memory_folder(home, {"deploy_steps.md": text})
    run_index(capsys)
    # The file's own name is not a label of it.
    assert stored_labels(home) == {"deploy_steps.md": ["run_all_checks.sh"]}


def test_missing_rules_file_warns_and_stores_no_labels(home, capsys, monkeypatch):
    monkeypatch.setattr(labels, "rules_path", lambda env=None: None)
    memory_folder(home, {"project_backup.md": NOTE})
    assert "memory_labels.py not found or broken" in run_index(capsys)
    assert stored_labels(home) == {"project_backup.md": []}


def test_broken_rules_file_gives_no_labeller(tmp_path):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "memory_labels.py").write_text("raise RuntimeError('broken')\n")
    settings = config.load_settings({"NOBLIVION_DATA_DIR": str(tmp_path / "d")})
    assert labels.make_labeller(settings, {"CLAUDE_PLUGIN_ROOT": str(root)}) is None


def test_plugin_root_hooks_come_first(tmp_path):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    for name in ("memory_labels.py", "hook_config.py"):
        shutil.copy(HOOKS / name, root / "hooks" / name)
    env = {"CLAUDE_PLUGIN_ROOT": str(root)}
    assert labels.rules_path(env) == root / "hooks" / "memory_labels.py"
    assert labels.rules_path({}) == HOOKS / "memory_labels.py"


def test_labeller_for_scan(tmp_path):
    settings = config.load_settings({"NOBLIVION_DATA_DIR": str(tmp_path / "d")})
    settings = config.Settings(
        data_dir=settings.data_dir,
        namespace=settings.namespace,
        memory_dirs=settings.memory_dirs,
        delete_grace_days=settings.delete_grace_days,
        archive_retention_days=settings.archive_retention_days,
        ticket_prefixes=("DEMO",),
    )
    labeller = labels.make_labeller(settings, {})
    assert labeller is not None
    folder = tmp_path / "projects" / "demo-proj" / "memory"
    folder.mkdir(parents=True)
    (folder / "feedback_keys.md").write_text("Fix DEMO-5 with ruff.\n", encoding="utf-8")
    conn = db.open_db(tmp_path / "data" / "noblivion.db")
    try:
        assert indexer.scan(conn, [folder], labeller=labeller).inserted == 1
        conn.row_factory = sqlite3.Row
        raw = conn.execute("SELECT labels FROM memories").fetchone()["labels"]
    finally:
        conn.close()
    assert json.loads(raw) == ["DEMO-5", "ruff"]


def test_sync_hook_passes_the_plugin_root_to_the_indexer():
    spec = importlib.util.spec_from_file_location(
        "_test_memory_sync_hook", HOOKS / "memory_sync_hook.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = mod.cron_like_env({"CLAUDE_PLUGIN_ROOT": "/srv/plugin", "SECRET_TOKEN": "x"})
    assert env["CLAUDE_PLUGIN_ROOT"] == "/srv/plugin"
    assert "SECRET_TOKEN" not in env
