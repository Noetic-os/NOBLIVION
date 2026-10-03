# SPDX-License-Identifier: AGPL-3.0-or-later
"""The store under the multi-process load of one machine (design doc 0001,
sections 5.4, 6.1, 6.3 and 17 "Concurrency").

Every load here runs in separate OS processes: a live ``noblivion serve``,
several "hook" processes that search, the ``noblivion index`` CLI and a second
embedding backfill. Fictional data only; the embedding backend is a fake Ollama
server on 127.0.0.1.

Size: ``NOBLIVION_STRESS_FULL=1`` runs the full load (more processes, more
rounds). CI runs the reduced load.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from noblivion import config, db, embedding, indexer, launcher
from noblivion import store as store_module
from store_concurrency_workers import (
    FAKE_MODEL,
    collect,
    ollama_url,
    start_fake_ollama,
    start_workers,
    store_env,
)

FULL = os.environ.get("NOBLIVION_STRESS_FULL", "").strip() == "1"
SEARCHERS = 24 if FULL else 12
ROUNDS = 80 if FULL else 30
RMW_PROCS = 8 if FULL else 4
RMW_COUNT = 400 if FULL else 150
OPENERS = 16 if FULL else 8
BURST = 128 if FULL else 64
FILLERS = 30  # with at most 2 x PAIR_SLOTS pair rows, every row fits one top_k=50 answer
PAIR_SLOTS = 8
WORDS = ("harbour", "lantern", "compass", "anchor", "orchard", "meadow", "copper", "willow")


def wait_for(predicate, timeout: float = 20.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def memory_folder(base: Path) -> Path:
    folder = base / "projects" / "-work-proj-load" / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_note(folder: Path, file_name: str, name: str, description: str, body: str) -> None:
    text = f"---\nname: {name}\ndescription: {description}\ntype: feedback\n---\n{body}\n"
    (folder / file_name).write_text(text, encoding="utf-8")


def write_fillers(folder: Path, n: int) -> None:
    rng = random.Random(7)
    for i in range(n):
        words = " ".join(rng.choice(WORDS) for _ in range(6))
        write_note(
            folder, f"feedback_note{i:03d}.md", f"note {i}", f"Note {i} about {words}.", words
        )


def write_pair(folder: Path, slot: int, gen: int) -> None:
    for side in ("a", "b"):
        write_note(
            folder,
            f"feedback_pair{slot:02d}_{side}.md",
            f"pair {slot} {side}",
            f"kiwi slot {slot} generation {gen}",
            f"Side {side} of pair {slot}, generation {gen}: kiwi {WORDS[slot % len(WORDS)]}.",
        )


def delete_pair(folder: Path, slot: int) -> None:
    for side in ("a", "b"):
        (folder / f"feedback_pair{slot:02d}_{side}.md").unlink()


def run_index_cli(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "noblivion", "index", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class LiveStore:
    """``python -m noblivion serve`` as a child process of the test."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.data_dir = Path(env["NOBLIVION_DATA_DIR"])
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "noblivion", "serve"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert wait_for(lambda: launcher.read_store_json(self.data_dir) is not None, 30)
        info = launcher.read_store_json(self.data_dir)
        assert info["pid"] == self.proc.pid
        self.port = info["port"]
        self.token = launcher.read_token(self.data_dir)

    def health(self) -> dict:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/health", headers={"Authorization": f"Bearer {self.token}"})
            return json.loads(conn.getresponse().read())
        finally:
            conn.close()

    def log_text(self) -> str:
        path = self.data_dir / config.STORE_LOG_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def stop(self) -> int | None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                return self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
                return None
        return self.proc.returncode


def error_lines(log_text: str) -> list[str]:
    return [line for line in log_text.splitlines() if " ERROR " in line]


@pytest.fixture
def fake_ollama():
    server = start_fake_ollama()
    yield server
    server.shutdown()
    server.server_close()


# -- the live store under load ------------------------------------------------------


@pytest.mark.slow
def test_live_store_under_multi_process_load(tmp_path, fake_ollama):
    """Hooks of several sessions search while the indexer CLI adds, changes and
    deletes files, the store scans and backfills, and a second backfill runs.

    No "database is locked", no 5xx, no lost or duplicate row, monotonic
    revision counters, and every answer shows one committed state: the two
    files of a pair are always written in one scan, so an answer that shows
    one side, or two generations, read a state that was never committed.
    """
    folder = memory_folder(tmp_path)
    write_fillers(folder, FILLERS)
    rng = random.Random(11)
    gen = 0
    slots: dict[int, int | None] = {s: None for s in range(PAIR_SLOTS)}
    for slot in range(PAIR_SLOTS // 2):
        gen += 1
        write_pair(folder, slot, gen)
        slots[slot] = gen

    env = store_env(
        tmp_path,
        NOBLIVION_EMBED_BACKEND="ollama",
        NOBLIVION_EMBED_MODEL=FAKE_MODEL,
        NOBLIVION_OLLAMA_URL=ollama_url(fake_ollama),
        NOBLIVION_INDEX_INTERVAL_S="1",
    )
    data = Path(env["NOBLIVION_DATA_DIR"])
    settings = config.load_settings(env)
    live = LiveStore(env)
    try:
        assert wait_for(
            lambda: (
                (h := live.health())["index_state"] == "idle"
                and h["embedding"]["state"] == "ready"
                and h["embedding"]["missing_vectors"] == 0
            ),
            30,
        ), live.health()

        stop = tmp_path / "stop"
        out = tmp_path / "reports"
        searchers = start_workers(
            "search",
            [{"data_dir": str(data), "stop": str(stop), "seed": i} for i in range(SEARCHERS)],
            out,
            env,
        )
        backfill = start_workers(
            "backfill",
            [
                {
                    "db": str(settings.db_path),
                    "ollama_url": ollama_url(fake_ollama),
                    "stop": str(stop),
                }
            ],
            out,
            env,
        )

        cli_runs = []
        for _ in range(ROUNDS):
            # Hold index.lock while the files change, so no scan reads half a
            # change: the store scan and the CLI scan then each see a whole round.
            with indexer.index_lock(settings.index_lock_path, timeout_s=60):
                present = [s for s, g in slots.items() if g is not None]
                absent = [s for s, g in slots.items() if g is None]
                for slot in present:  # many rows per scan: a torn write shows more often
                    gen += 1
                    write_pair(folder, slot, gen)
                    slots[slot] = gen
                if len(present) > 2 and rng.random() < 0.5:
                    slot = rng.choice(present)
                    delete_pair(folder, slot)
                    slots[slot] = None
                if absent and rng.random() < 0.6:
                    slot = rng.choice(absent)
                    gen += 1
                    write_pair(folder, slot, gen)
                    slots[slot] = gen
            cli_runs.append(run_index_cli(env))
            time.sleep(rng.uniform(0.0, 0.3))

        stop.touch()
        reports = collect(searchers + backfill)
        for report in reports:
            assert report["ok"], report
        search_reports = reports[:SEARCHERS]
        for report in search_reports:
            assert report["problem_count"] == 0, report["problems"]
            assert set(report["statuses"]) == {"200"}, report["statuses"]
        total = sum(sum(r["statuses"].values()) for r in search_reports)
        assert total > 20 * SEARCHERS
        assert reports[-1]["passes"] > 0
        # One store serves the whole time: the one this test started.
        assert launcher.read_store_json(data)["pid"] == live.proc.pid
        assert live.log_text().count("listening on port") == 1
        for report in search_reports:
            assert set(report["ensure"]) <= {"running", "starting", "started"}
        for run in cli_runs:
            assert run.returncode == 0, run.stderr
            assert "locked" not in run.stderr

        # The end state: the CLI scan makes the rows match the files, the store
        # backfill gives every live row a vector of the current text.
        final = run_index_cli(env)
        assert final.returncode == 0, final.stderr
        assert wait_for(lambda: live.health()["embedding"]["missing_vectors"] == 0, 30)
        health = live.health()
        for report in search_reports:
            content_seen, vector_seen = report["last_revs"]
            assert health["content_rev"] >= content_seen
            assert health["vector_rev"] >= vector_seen

        on_disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob("*.md")}
        with closing(db.connect(settings.db_path, create=True)) as conn, db.read_tx(conn):
            rows = conn.execute(
                "SELECT id, path, hash, content, rev FROM memories "
                "WHERE archived_at IS NULL AND deleted_at IS NULL"
            ).fetchall()
            paths = [r["path"] for r in rows]
            assert len(paths) == len(set(paths)), "a file has two live rows"
            assert {r["path"]: r["hash"] for r in rows} == on_disk
            for slot, slot_gen in slots.items():
                for side in ("a", "b"):
                    name = f"feedback_pair{slot:02d}_{side}.md"
                    if slot_gen is None:
                        assert name not in paths
                        continue
                    content = next(r["content"] for r in rows if r["path"] == name)
                    assert f"generation {slot_gen}" in content, "a change was lost"
            dup = conn.execute(
                "SELECT count(*) FROM (SELECT root, path FROM memories "
                "GROUP BY project, root, path HAVING count(*) > 1)"
            ).fetchone()[0]
            assert dup == 0
            content_rev, vector_rev = db.revisions(conn)
            assert content_rev == health["content_rev"]
            assert conn.execute("SELECT max(rev) FROM memories").fetchone()[0] <= content_rev
            assert conn.execute("SELECT max(rev) FROM vectors").fetchone()[0] <= vector_rev
            model = f"ollama:{FAKE_MODEL}"
            vectors = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT memory_id, content_hash FROM vectors WHERE model = ?", (model,)
                )
            }
            for r in rows:
                want = embedding.text_hash(embedding.embed_text(r["content"]))
                assert vectors.get(r["id"]) == want, f"row {r['id']}: stale or no vector"
            assert (
                conn.execute("SELECT count(*) FROM vectors WHERE model <> ?", (model,)).fetchone()[
                    0
                ]
                == 0
            )
            assert db.foreign_key_problems(conn) == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert error_lines(live.log_text()) == []
    finally:
        code = live.stop()
    assert code == 0
    assert not (data / config.STORE_JSON_FILE).exists()


# -- read-then-write transactions across processes ------------------------------------


@pytest.mark.slow
def test_read_then_write_transactions_lose_no_update(tmp_path, fake_ollama):
    """Section 6.1: a transaction that reads a value and writes based on it
    starts with ``BEGIN IMMEDIATE``. Several processes run such a transaction
    while the indexer CLI and an embedding backfill write too. Every increment
    must land, and no process may see "database is locked".

    With a deferred ``BEGIN`` in ``db.write_tx`` this test fails: a process
    that read under an old snapshot cannot write, and SQLite answers
    "database is locked" at once, without the busy timeout.
    """
    folder = memory_folder(tmp_path)
    write_fillers(folder, FILLERS)
    env = store_env(tmp_path)
    settings = config.load_settings(env)
    db.open_db(settings.db_path, create=True).close()
    assert run_index_cli(env).returncode == 0

    stop = tmp_path / "stop"
    out = tmp_path / "reports"
    start_at = time.time() + 1.5
    rmw = start_workers(
        "rmw",
        [
            {
                "db": str(settings.db_path),
                "key": "stress_counter",
                "count": RMW_COUNT,
                "start_at": start_at,
            }
            for _ in range(RMW_PROCS)
        ],
        out,
        env,
    )
    backfill = start_workers(
        "backfill",
        [
            {
                "db": str(settings.db_path),
                "ollama_url": ollama_url(fake_ollama),
                "stop": str(stop),
                "batch": 4,
            }
        ],
        out,
        env,
    )
    cli_runs = []
    rng = random.Random(3)
    try:
        time.sleep(max(0.0, start_at - time.time()))
        while any(proc.poll() is None for proc, _ in rmw):
            note = rng.randrange(FILLERS)
            write_note(
                folder,
                f"feedback_note{note:03d}.md",
                f"note {note}",
                f"Note {note} changed {rng.random()}.",
                " ".join(rng.choice(WORDS) for _ in range(5)),
            )
            cli_runs.append(run_index_cli(env))
    finally:
        stop.touch()
    reports = collect(rmw + backfill)
    for report in reports:
        assert report["ok"], report
    assert [r["done"] for r in reports[:RMW_PROCS]] == [RMW_COUNT] * RMW_PROCS
    assert cli_runs
    for run in cli_runs:
        assert run.returncode == 0, run.stderr
    with closing(db.connect(settings.db_path, create=True)) as conn:
        assert db.get_meta(conn, "stress_counter") == str(RMW_PROCS * RMW_COUNT)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_first_open_migrates_once(tmp_path):
    """The store and the CLI may both open a new database at the same moment.
    Every process gets the latest schema; the seed rows are written once."""
    path = tmp_path / "data" / config.DB_FILE
    start_at = time.time() + 1.5
    reports = collect(
        start_workers(
            "open_db",
            [{"db": str(path), "start_at": start_at} for _ in range(OPENERS)],
            tmp_path / "reports",
        )
    )
    for report in reports:
        assert report["ok"], report
        assert report["version"] == db.latest_version()
        assert report["revs"] == [0, 0]
    with closing(db.connect(path, create=True)) as conn:
        assert conn.execute("SELECT count(*) FROM meta WHERE key = 'db_id'").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# -- the store scan and a file that changes inside one clock tick ----------------------


def test_same_size_rewrite_in_one_clock_tick_is_indexed(tmp_path):
    """The store scan keeps a stat cache (size, mtime). On a file system with a
    coarse clock, a rewrite with the same size in the same tick keeps the
    mtime. The cache must not hide that change from every later scan."""
    folder = memory_folder(tmp_path)
    path = folder / "feedback_tick.md"
    write_note(folder, path.name, "tick", "Version one of the tick rule.", "alpha")
    first = path.stat()
    with closing(db.open_db(tmp_path / "data" / config.DB_FILE, create=True)) as conn:
        cache: indexer.StatCache = {}
        indexer.scan(conn, [folder], stat_cache=cache)
        write_note(folder, path.name, "tick", "Version two of the tick rule.", "alpha")
        assert path.stat().st_size == first.st_size
        os.utime(path, ns=(first.st_atime_ns, first.st_mtime_ns))  # the same clock tick
        result = indexer.scan(conn, [folder], stat_cache=cache)
        assert result.updated == 1
        content = conn.execute("SELECT content FROM memories").fetchone()[0]
        assert "Version two" in content


def test_stat_cache_still_skips_old_unchanged_files(tmp_path):
    folder = memory_folder(tmp_path)
    write_note(folder, "feedback_old.md", "old", "An old rule.", "beta")
    hour_ago = time.time() - 3600
    os.utime(folder / "feedback_old.md", (hour_ago, hour_ago))
    with closing(db.open_db(tmp_path / "data" / config.DB_FILE, create=True)) as conn:
        cache: indexer.StatCache = {}
        indexer.scan(conn, [folder], stat_cache=cache)
        assert str(folder / "feedback_old.md") in cache
        assert indexer.scan(conn, [folder], stat_cache=cache).unchanged == 1


# -- many hook connections at the same moment -------------------------------------------


@pytest.mark.slow
def test_a_burst_of_hook_connections_is_answered_fast(tmp_path):
    """Hooks of several sessions connect at once. A small listen backlog drops
    SYNs, and the client retries after 1 s: past the 300 ms listener proof
    (section 3.3), so the hook would count a running store as down."""
    import http.client
    import threading

    assert store_module._Server.request_queue_size >= 64
    live = LiveStore(store_env(tmp_path))
    times: list[float] = []
    errors: list[str] = []
    gate = threading.Barrier(BURST)

    def one() -> None:
        gate.wait()
        began = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=10)
        try:
            conn.request("GET", "/health")
            if conn.getresponse().status != 200:
                errors.append("status")
        except OSError as exc:
            errors.append(type(exc).__name__)
        finally:
            conn.close()
        times.append(time.monotonic() - began)

    try:
        for _ in range(3):
            threads = [threading.Thread(target=one) for _ in range(BURST)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
    finally:
        assert live.stop() == 0
    assert errors == []
    assert max(times) < 0.9, f"slowest connection {max(times):.2f} s (a dropped SYN waits 1 s)"


# -- POSIX locks of other connections in the same process ---------------------------------


def test_a_new_connection_keeps_the_locks_of_open_connections(tmp_path):
    """POSIX locks belong to the process: closing any fd of the database file
    drops the locks of every SQLite connection of the process. The store opens
    a connection per request. If ``connect`` opened and closed the file, another
    process could take the lock, checkpoint and delete the WAL under an open
    connection ("disk I/O error", "database disk image is malformed")."""
    path = tmp_path / "data" / config.DB_FILE
    db.open_db(path, create=True).close()
    with closing(db.connect(path, create=True)) as held:
        held.execute("SELECT count(*) FROM meta").fetchone()  # a WAL reader: SHARED lock
        db.connect(path, create=True).close()  # a second connection of this process, as per request
        other = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sqlite3, sys\n"
                "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
                "c.execute(\"INSERT INTO meta (key, value) VALUES ('probe', '1')\")\n"
                "c.close()\n",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert other.returncode == 0, other.stderr
        # The other process closed its last connection. With the locks of this
        # process intact, it may not checkpoint and delete the WAL.
        assert Path(f"{path}-wal").exists()
        assert held.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "1"
