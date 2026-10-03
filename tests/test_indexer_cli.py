# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexer CLI, index lock, config and concurrent scans (design doc 0001, sections 5.6, 6.1, 12)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from noblivion import __main__ as cli
from noblivion import config, db, indexer


def make_folder(base: Path, root: str, n: int) -> Path:
    folder = base / ".claude" / "projects" / root / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"user_{i:03}.md").write_text(f"note {i} for {root}\n", encoding="utf-8")
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "NOBLIVION_DATA_DIR",
        "CLAUDE_PLUGIN_DATA",
        "XDG_DATA_HOME",
        "NOBLIVION_CONFIG",
        "NOBLIVION_MEMORY_DIRS",
        "NOBLIVION_MEMORY_DIR",
        "NOBLIVION_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# -- config ------------------------------------------------------------------


def test_data_dir_order(home):
    assert config.data_dir({}) == home / ".local" / "share" / "noblivion"
    assert config.data_dir({"XDG_DATA_HOME": "/srv/xdg"}) == Path("/srv/xdg/noblivion")
    env = {"CLAUDE_PLUGIN_DATA": "/srv/plugin", "XDG_DATA_HOME": "/srv/xdg"}
    assert config.data_dir(env) == Path("/srv/plugin")
    env["NOBLIVION_DATA_DIR"] = "/srv/own"
    assert config.data_dir(env) == Path("/srv/own")


def test_settings_from_file_and_env(home, monkeypatch):
    data = home / "data"
    data.mkdir()
    (data / "config.json").write_text(
        json.dumps(
            {
                "namespace": "demo-ns",
                "memory_dirs": ["~/notes/memory"],
                "index": {"delete_grace_days": 3},
                "dedup.archive_retention_days": 7,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    s = config.load_settings()
    assert (s.namespace, s.delete_grace_days, s.archive_retention_days) == ("demo-ns", 3, 7)
    assert s.resolved_memory_dirs() == [home / "notes" / "memory"]
    assert s.db_path == data / "noblivion.db"
    monkeypatch.setenv("NOBLIVION_PROJECT", "env-ns")
    monkeypatch.setenv("NOBLIVION_MEMORY_DIRS", "/srv/a/memory:/srv/b/memory")
    s = config.load_settings()
    assert s.namespace == "env-ns"
    assert s.resolved_memory_dirs() == [Path("/srv/a/memory"), Path("/srv/b/memory")]


def test_bad_config_falls_back_to_defaults(home, monkeypatch):
    data = home / "data"
    data.mkdir()
    (data / "config.json").write_text('{"index": {"delete_grace_days": "soon"}', encoding="utf-8")
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    s = config.load_settings()
    assert (s.namespace, s.delete_grace_days, s.memory_dirs) == ("claude_code", 14, None)


def test_default_memory_dirs_glob(home):
    a = make_folder(home, "proj-a", 1)
    b = make_folder(home, "proj-b", 1)
    (home / ".claude" / "projects" / "proj-c").mkdir()
    assert config.default_memory_dirs() == [a, b]


# -- CLI ---------------------------------------------------------------------


def test_cli_index_default_folders(home, capsys):
    make_folder(home, "proj-a", 3)
    make_folder(home, "proj-b", 2)
    assert cli.main(["index", "--json"]) == indexer.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["inserted"] == 5 and out["index_blocked"] is False
    db_path = home / ".local" / "share" / "noblivion" / "noblivion.db"
    conn = db.open_db(db_path)
    roots = {r[0] for r in conn.execute("SELECT DISTINCT root FROM memories")}
    assert roots == {"proj-a", "proj-b"}


def test_cli_exit_code_when_the_shrink_guard_blocks(home, capsys):
    folder = make_folder(home, "proj-a", 20)
    assert indexer.main([]) == indexer.EXIT_OK
    for i in range(5):
        (folder / f"user_{i:03}.md").unlink()
    assert indexer.main([]) == indexer.EXIT_BLOCKED
    assert "--allow-shrink" in capsys.readouterr().err
    assert indexer.main(["--allow-shrink"]) == indexer.EXIT_OK


def test_cli_refuses_an_old_schema(home, capsys):
    data = home / ".local" / "share" / "noblivion"
    data.mkdir(parents=True)
    import sqlite3

    raw = sqlite3.connect(data / "noblivion.db")
    raw.execute("CREATE TABLE legacy (x INTEGER)")
    raw.commit()
    raw.close()
    assert indexer.main([]) == indexer.EXIT_SCHEMA
    assert "start the store" in capsys.readouterr().err


def test_cli_waits_for_the_index_lock(home, capsys):
    make_folder(home, "proj-a", 1)
    lock = home / ".local" / "share" / "noblivion" / "index.lock"
    with indexer.index_lock(lock):
        assert indexer.main(["--lock-timeout", "0.2"]) == indexer.EXIT_LOCKED
    assert "another scan" in capsys.readouterr().err
    assert indexer.main(["--lock-timeout", "0.2"]) == indexer.EXIT_OK


def test_cli_database_locked_is_one_line_and_exit_5(home, capsys, monkeypatch):
    """A SQLite lock past busy_timeout: exit 5, one line, no traceback."""
    import sqlite3

    make_folder(home, "proj-a", 1)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(indexer, "scan", locked)
    assert indexer.main([]) == indexer.EXIT_DB_BUSY == 5
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "database is locked" in err and "Traceback" not in err


def test_the_hook_memory_dir_is_always_indexed(home, monkeypatch, tmp_path):
    """NOBLIVION_MEMORY_DIR (the hooks' folder) joins NOBLIVION_MEMORY_DIRS, once."""
    hook_dir = make_folder(tmp_path / "elsewhere", "proj-h", 1)
    other = make_folder(tmp_path / "x", "proj-x", 1)
    monkeypatch.setenv("NOBLIVION_MEMORY_DIRS", str(other))
    assert config.load_settings().resolved_memory_dirs() == [other]
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(hook_dir))
    assert config.load_settings().resolved_memory_dirs() == [other, hook_dir]
    monkeypatch.setenv("NOBLIVION_MEMORY_DIRS", f"{other}:{hook_dir}")
    assert config.load_settings().resolved_memory_dirs() == [other, hook_dir]
    monkeypatch.delenv("NOBLIVION_MEMORY_DIRS")
    assert hook_dir in config.load_settings().resolved_memory_dirs()


def test_cli_explicit_memory_dir_and_db(home, tmp_path):
    folder = make_folder(tmp_path / "x", "proj-x", 2)
    db_path = tmp_path / "other" / "store.db"
    assert indexer.main(["--memory-dir", str(folder), "--db", str(db_path)]) == 0
    assert (tmp_path / "other" / "index.lock").exists()
    conn = db.open_db(db_path)
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 2


def test_cli_usage(capsys):
    assert cli.main([]) == 2
    assert cli.main(["--help"]) == 0
    assert cli.main(["nope"]) == 2
    assert "unknown command" in capsys.readouterr().err


# -- concurrency -------------------------------------------------------------


def _assert_consistent(db_path: Path, expected: int) -> None:
    conn = db.open_db(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.foreign_key_problems(conn) == []
    total, distinct = conn.execute(
        "SELECT count(*), count(DISTINCT root || '/' || path) FROM memories "
        "WHERE deleted_at IS NULL"
    ).fetchone()
    assert total == distinct == expected
    rev = db.revisions(conn)[0]
    assert conn.execute("SELECT max(rev) FROM memories").fetchone()[0] <= rev
    conn.close()


def test_two_threads_scan_at_once_without_the_lock(tmp_path):
    folders = [make_folder(tmp_path, f"proj-{c}", 250) for c in "ab"]
    db_path = tmp_path / "data" / "noblivion.db"
    db.open_db(db_path).close()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        conn = db.open_db(db_path)
        try:
            barrier.wait()
            for _ in range(3):
                indexer.scan(conn, folders, force=True)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _assert_consistent(db_path, 500)


def test_two_cli_processes_at_once(tmp_path):
    make_folder(tmp_path, "proj-a", 300)
    make_folder(tmp_path, "proj-b", 300)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOBLIVION_", "XDG_"))}
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env["HOME"] = str(tmp_path)
    cmd = [sys.executable, "-m", "noblivion.indexer", "--json"]
    procs = [
        subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    inserted = sorted(json.loads(out)["inserted"] for out, _err in outs)
    assert inserted == [0, 600]  # the index lock lets one scan run at a time
    _assert_consistent(tmp_path / ".local" / "share" / "noblivion" / "noblivion.db", 600)
