# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store lifecycle under concurrency (design doc 0001, sections 3.2 to 3.4).

- Several ``ensure-running`` calls at one moment start exactly one store.
- A store killed with ``SIGKILL`` in the middle of a write transaction: the
  next start recovers the WAL, takes the lock the OS released, and replaces
  the stale ``store.json``.

Real processes only. Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from noblivion import __version__, config, db, launcher, store
from store_concurrency_workers import collect, pid_alive, start_workers, store_env

FULL = os.environ.get("NOBLIVION_STRESS_FULL", "").strip() == "1"
CALLERS = 16 if FULL else 6
KILL_FILES = 3000 if FULL else 1200


def wait_for(predicate, timeout: float = 30.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def proven(data: Path) -> dict | None:
    info = launcher.read_store_json(data)
    token = launcher.read_token(data)
    if info is None or token is None:
        return None
    answer = launcher.probe(info["port"], token, timeout=2)
    return None if answer is None else info


def full_health(info: dict, token: str) -> dict:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=10)
    try:
        conn.request("GET", "/health", headers={"Authorization": f"Bearer {token}"})
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


def stop_detached(data: Path) -> None:
    """SIGTERM the store this test started (its pid is in ``store.json``)."""
    info = launcher.read_store_json(data)
    if info is None:
        return
    os.kill(info["pid"], signal.SIGTERM)
    assert wait_for(lambda: gone(info["pid"]), 30)


def gone(pid: int) -> bool:
    """True when the process ended. Reaps it when it is a child of this process."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except ChildProcessError:
        pass  # not our child: its parent reaps it
    return not pid_alive(pid)


def store_log(data: Path) -> str:
    path = data / config.STORE_LOG_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


# -- ensure-running races --------------------------------------------------------------


@pytest.mark.slow
def test_concurrent_ensure_running_starts_exactly_one_store(tmp_path):
    env = store_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    start_at = time.time() + 1.5
    reports = collect(
        start_workers(
            "ensure", [{"start_at": start_at} for _ in range(CALLERS)], tmp_path / "r", env
        )
    )
    try:
        for report in reports:
            assert report["ok"], report
        states = sorted(r["state"] for r in reports)
        assert states.count(launcher.STATE_STARTED) == 1, states
        assert set(states) <= {launcher.STATE_STARTED, launcher.STATE_STARTING}
        assert wait_for(lambda: proven(data) is not None)
        # Give a second store, had one been spawned, the time to start.
        time.sleep(1.0)
        log = store_log(data)
        assert log.count("listening on port") == 1, log
        assert "another store runs" not in log
        # A later call finds the proven store and starts nothing.
        again = collect(start_workers("ensure", [{"start_at": 0}], tmp_path / "r2", env))
        assert again[0]["state"] == launcher.STATE_RUNNING
    finally:
        stop_detached(data)
    assert not (data / config.STORE_JSON_FILE).exists()


@pytest.mark.slow
def test_two_stores_started_at_once_one_serves_one_exits(tmp_path):
    """Two hooks that both passed the stamp check: the store lock decides."""
    env = store_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "noblivion", "serve"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    try:
        assert wait_for(lambda: any(p.poll() is not None for p in procs))
        exited = [p for p in procs if p.poll() is not None]
        serving = [p for p in procs if p.poll() is None]
        assert len(exited) == 1 and len(serving) == 1
        assert exited[0].returncode == store.EXIT_OK
        assert wait_for(lambda: proven(data) is not None)
        assert launcher.read_store_json(data)["pid"] == serving[0].pid
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                p.wait(30)
    assert [p.returncode for p in procs].count(0) == 2


# -- SIGKILL in the middle of a write -----------------------------------------------------


def write_files(folder: Path, n: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        text = (
            f"---\nname: crash note {i}\ndescription: Crash note {i} about the harbour.\n"
            f"type: feedback\n---\nBody of note {i}: lantern compass anchor.\n"
        )
        (folder / f"feedback_crash{i:05d}.md").write_text(text, encoding="utf-8")


def write_lock_held(db_path: Path) -> bool:
    """True when another connection holds the write lock right now."""
    with closing(sqlite3.connect(str(db_path), timeout=0, isolation_level=None)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                return True
            raise
        conn.execute("ROLLBACK")
        return False


@pytest.mark.slow
def test_sigkill_mid_write_then_the_next_start_recovers(tmp_path, monkeypatch):
    env = store_env(tmp_path)
    for key in [k for k in os.environ if k.startswith("NOBLIVION_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)  # ensure_running spawns with this environment
    data = Path(env["NOBLIVION_DATA_DIR"])
    folder = Path(env["NOBLIVION_MEMORY_DIRS"])
    write_files(folder, KILL_FILES)
    db_path = data / config.DB_FILE

    child = subprocess.Popen(
        [sys.executable, "-m", "noblivion", "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    killed_mid_write = False
    try:
        assert wait_for(lambda: launcher.read_store_json(data) is not None)
        # The store's first scan writes the rows in batches of 200, each in one
        # BEGIN IMMEDIATE transaction. Freeze the store while it holds the
        # write lock, check it still holds it, then kill it.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and child.poll() is None:
            if not write_lock_held(db_path):
                time.sleep(0.0005)
                continue
            child.send_signal(signal.SIGSTOP)
            if write_lock_held(db_path):
                child.kill()
                killed_mid_write = True
                break
            child.send_signal(signal.SIGCONT)
    finally:
        if child.poll() is None and not killed_mid_write:
            child.kill()
        child.wait(30)
    assert killed_mid_write, "the store never held the write lock while the test looked"
    assert child.returncode == -signal.SIGKILL

    # What a crash leaves (section 3.4): a stale store.json, the lock file
    # (the OS released the flock), the WAL.
    stale = launcher.read_store_json(data)
    assert stale is not None and stale["pid"] == child.pid
    assert (data / config.STORE_LOCK_FILE).exists()
    fd = store.acquire_lock(data / config.STORE_LOCK_FILE, 0.0)
    os.close(fd)  # closing the fd releases the flock
    assert (data / f"{config.DB_FILE}-wal").exists()

    # The killed batch is rolled back whole: only full batches are committed.
    with closing(db.connect(db_path, create=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        content_rev = db.revisions(conn)[0]
        groups = conn.execute(
            "SELECT rev, count(*) FROM memories GROUP BY rev ORDER BY rev"
        ).fetchall()
        sizes = [int(n) for _, n in groups]
        total = sum(sizes)
        assert total < KILL_FILES, "the scan ended before the kill"
        assert all(n == db.BATCH_ROWS for n in sizes), sizes
        assert [int(r) for r, _ in groups] == list(range(1, content_rev + 1))

    # The next hook call starts a new store (section 3.2): the stale port does
    # not prove, the stamp is old, the new store takes the lock and overwrites
    # store.json.
    assert launcher.ensure_running(env) == launcher.STATE_STARTED
    try:
        assert wait_for(lambda: (p := proven(data)) is not None and p["pid"] != child.pid)
        info = launcher.read_store_json(data)
        token = launcher.read_token(data)
        assert full_health(info, token)["version"] == __version__
        assert wait_for(
            lambda: (
                (h := full_health(info, token))["index_state"] == "idle"
                and h["memories"] == KILL_FILES
            ),
            60,
        )
        with closing(db.connect(db_path, create=True)) as conn:
            paths = conn.execute(
                "SELECT count(*), count(DISTINCT path) FROM memories "
                "WHERE deleted_at IS NULL AND archived_at IS NULL"
            ).fetchone()
            assert tuple(paths) == (KILL_FILES, KILL_FILES)
            assert db.foreign_key_problems(conn) == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert " ERROR " not in store_log(data)
    finally:
        stop_detached(data)
    assert not (data / config.STORE_JSON_FILE).exists()
