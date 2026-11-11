# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen builds and the worker protocol, with a stub build (no store)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import buildrunner, runner

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "mcp").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    (repo / "hooks" / "recall_hook.py").write_text(
        "import json\n\ndef main(stdin, stdout, env):\n"
        "    event = json.loads(stdin.read())\n"
        "    stdout.write('- [id 1] note: ' + event['prompt'] + ' '\n"
        "                 + env['NOBLIVION_MEMORY_DIR'])\n"
    )
    (repo / "hooks" / "guard_hook.py").write_text(
        "import json\n\ndef main(stdin, stdout, env):\n"
        "    event = json.loads(stdin.read())\n"
        "    stdout.write(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny',\n"
        "        'permissionDecisionReason': event['tool_input']['command']}}))\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", "x"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_export_build_freezes_a_commit(tmp_path):
    repo, sha = make_repo(tmp_path)
    info = buildrunner.export_build(repo, "HEAD", tmp_path / "b1")
    assert info["commit"] == sha
    assert (tmp_path / "b1" / "hooks" / "recall_hook.py").is_file()
    assert buildrunner.build_version(tmp_path / "b1") == "9.9.9"
    assert oct((tmp_path / "b1").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(SystemExit):
        buildrunner.export_build(repo, "HEAD", tmp_path / "b1")  # not empty


def harness(tmp_path):
    repo, _ = make_repo(tmp_path)
    buildrunner.export_build(repo, "HEAD", tmp_path / "b1")
    h = buildrunner.Harness("B1", tmp_path / "b1", tmp_path / "root", sys.executable, None)
    h.config_dir.joinpath("projects", "-work-a", "memory").mkdir(parents=True)
    return h


def test_harness_environment_is_built_from_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("SOMETHING_ELSE", "v")
    env = harness(tmp_path).environ()
    assert "ANTHROPIC_API_KEY" not in env and "SOMETHING_ELSE" not in env
    assert env["NOBLIVION_DATA_DIR"].startswith(str(tmp_path / "root"))
    assert env["HOME"] == str(tmp_path / "root" / "home")
    assert env["PYTHONPATH"].endswith("src")


def test_worker_runs_the_hooks_of_its_build(tmp_path):
    h = harness(tmp_path)
    with buildrunner.Worker(h) as w:
        assert w.call("ping")["out"] == "pong"
        r = w.call("prompt", sid="s1", cwd="/work/a", prompt="hello there")
        assert r["ok"] and "hello there" in r["out"]
        assert str(h.config_dir / "projects" / "-work-a" / "memory") in r["out"]
        # a job may name a project folder apart from the cwd
        r = w.call("prompt", sid="s2", cwd="/tmp/x", prompt="p", project="-work-a")
        assert str(h.config_dir / "projects" / "-work-a" / "memory") in r["out"]
        g = w.call("guard", sid="s3", cwd="/work/a", tool="Bash", tool_input={"command": "rm x"})
        assert json.loads(g["out"])["hookSpecificOutput"]["permissionDecision"] == "deny"
        bad = w.call("error", sid="s4", cwd="/work/a", command="c", error="e")
        assert bad["ok"] is False and bad["error"]  # a missing hook fails the job, not the worker
        assert w.call("ping")["out"] == "pong"
    assert (h.config_dir / "projects" / "-work-a" / "s1.jsonl").is_file()  # the transcript


def test_worker_writes_nothing_outside_the_harness(tmp_path):
    h = harness(tmp_path)
    before = {p for p in Path(tmp_path).rglob("*")}
    with buildrunner.Worker(h) as w:
        w.call("prompt", sid="s1", cwd="/work/a", prompt="x")
    new = {p for p in Path(tmp_path).rglob("*")} - before
    assert all(str(tmp_path / "root") in str(p) for p in new)


def test_a_dead_worker_names_its_exit_code_and_its_log(tmp_path, monkeypatch):
    dying = tmp_path / "dying.py"
    dying.write_text(
        "import sys\nsys.stderr.write('PosixThread\\nResource temporarily unavailable\\n')\n"
        "sys.exit(134)\n"
    )
    monkeypatch.setattr(buildrunner, "WORKER", dying)
    h = buildrunner.Harness("B1", tmp_path / "b1", tmp_path / "root", sys.executable, None)
    with pytest.raises(RuntimeError, match=r"exited 134: PosixThread \| Resource temporarily"):
        buildrunner.Worker(h)
    assert oct((h.root / "worker.err").stat().st_mode & 0o777) == "0o600"


def test_a_store_that_exits_names_the_end_of_its_log(tmp_path):
    st = buildrunner.suite()
    log = tmp_path / "suite-store.out"
    log.write_text("".join(f"line {i}\n" for i in range(5000)) + "terminate called\n")
    store = st.Store(tmp_path, sys.executable, tmp_path, 5.0)
    store.proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    store.proc.wait()
    with pytest.raises(RuntimeError, match=r"store exited 3: line 4991 \|.*\| terminate called$"):
        store.wait_ready()
    assert st.file_tail(tmp_path / "absent") == "(no log)"


def test_a_failed_index_names_the_end_of_its_stderr(tmp_path):
    src = tmp_path / "src" / "noblivion"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "__main__.py").write_text("import sys\nsys.stderr.write('disk full\\n')\nsys.exit(2)\n")
    store = buildrunner.suite().Store(tmp_path, sys.executable, tmp_path / "src", 30.0)
    with pytest.raises(RuntimeError, match="noblivion index exited 2: disk full"):
        store.index({})


def test_snapshot_and_changed_files(tmp_path):
    (tmp_path / "a").write_text("1")
    before = runner.snapshot_tree(tmp_path)
    assert runner.changed_files(before, runner.snapshot_tree(tmp_path)) == []
    (tmp_path / "a").write_text("22")
    (tmp_path / "b").write_text("x")
    assert len(runner.changed_files(before, runner.snapshot_tree(tmp_path))) == 2
    assert runner.changed_files(before, runner.snapshot_tree(tmp_path), lambda n: True) == []


def test_determinism_hash_ignores_time_and_order():
    rows = [
        {
            "qid": "a",
            "build": "B1",
            "channel": "prompt",
            "hit": True,
            "rank": 1,
            "rows": 2,
            "chars": 10,
            "size": 30,
            "ms": 5.0,
        },
        {
            "qid": "b",
            "build": "B1",
            "channel": "prompt",
            "hit": False,
            "rank": None,
            "rows": 0,
            "chars": 0,
            "size": 30,
            "ms": 7.0,
        },
    ]
    again = [dict(r, ms=r["ms"] + 100) for r in reversed(rows)]
    assert runner.determinism_hash(rows, 30) == runner.determinism_hash(again, 30)
    again[0]["hit"] = not again[0]["hit"]
    assert runner.determinism_hash(rows, 30) != runner.determinism_hash(again, 30)


# -- a store is ready only when no chunk lacks a vector ---------------------------------


def chunk_db(folder: Path, chunks: int, with_vector: int = 0) -> sqlite3.Connection:
    """A database with the columns that the backlog count reads: ``chunks`` chunks of
    live notes, the first ``with_vector`` of them embedded; one archived note, one
    deleted note and one chunk with no text, none of which the store embeds."""
    conn = sqlite3.connect(folder / buildrunner.suite().DB_NAME, isolation_level=None)
    conn.executescript(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, archived_at TEXT, deleted_at TEXT);"
        "CREATE TABLE memory_chunks (id INTEGER PRIMARY KEY, parent_id INTEGER, search_text TEXT);"
        "CREATE TABLE chunk_vectors (chunk_id INTEGER, model TEXT);"
        "INSERT INTO memories VALUES (1, NULL, NULL), (2, 't', NULL), (3, NULL, 't');"
        "INSERT INTO memory_chunks VALUES (901, 2, 'archived'), (902, 3, 'deleted'), "
        "(903, 1, ' \n\t');"
    )
    for i in range(1, chunks + 1):
        conn.execute("INSERT INTO memory_chunks VALUES (?, 1, 'text')", (i,))
        if i <= with_vector:
            conn.execute("INSERT INTO chunk_vectors VALUES (?, 'm')", (i,))
    return conn


def ready_store(tmp_path, monkeypatch, embedding="ready", on_look=lambda n: None):
    """A suite store with no process: the health answer is idle with every file vector
    there, as a store with a chunk backlog gives it. The clock is the test's own."""
    st = buildrunner.suite()
    clock, looks = {"t": 0.0}, []
    fake = SimpleNamespace(
        monotonic=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s)
    )
    monkeypatch.setattr(st, "time", fake)
    store = st.Store(tmp_path, sys.executable, tmp_path, 30.0)

    def health():
        looks.append(clock["t"])
        rev = on_look(len(looks)) or 9  # a store that embeds moves its vector revision
        state = {"state": embedding, "missing_vectors": 0}
        return {"index_state": "idle", "content_rev": 4, "vector_rev": rev, "embedding": state}

    store.health = health
    return store, looks


def test_the_backlog_count_is_the_chunks_that_the_store_still_embeds(tmp_path):
    store = buildrunner.suite().Store(tmp_path, sys.executable, tmp_path, 30.0)
    assert store.chunk_backlog() == 0  # no database yet
    conn = chunk_db(tmp_path, chunks=5, with_vector=2)
    assert store.chunk_backlog() == 3  # not the archived, the deleted or the empty one
    conn.execute("DROP TABLE chunk_vectors")
    conn.execute("DROP TABLE memory_chunks")
    assert store.chunk_backlog() == 0  # a build from before the chunks


def test_the_backlog_count_reads_the_schema_of_the_build(tmp_path):
    from noblivion import db

    db.open_db(tmp_path / buildrunner.suite().DB_NAME, create=True).close()
    store = buildrunner.suite().Store(tmp_path, sys.executable, tmp_path, 30.0)
    assert store.chunk_backlog() == 0


def test_a_store_with_an_embedding_backlog_is_not_ready(tmp_path, monkeypatch):
    """The fault of the long runs: the health answer counts file vectors only, so a
    store that still embedded 948 chunks was called ready, answered late and latched
    the hooks. Ready now waits until no chunk lacks a vector."""
    conn = chunk_db(tmp_path, chunks=3)

    def embed_one(look):  # the store embeds one chunk between two looks
        if look <= 3:
            conn.execute("INSERT INTO chunk_vectors VALUES (?, 'm')", (look,))
        return 9 + min(look, 3)

    store, looks = ready_store(tmp_path, monkeypatch, on_look=embed_one)
    health = store.wait_ready()
    assert health["index_state"] == "idle"
    # looks 1 and 2 see 2 and 1 chunks left, look 3 sees none, look 4 is the equal answer
    assert len(looks) == 4 and store.backlog == 2 and store.drain_s == 1.0


def test_a_backlog_that_does_not_drain_ends_the_wait_and_says_what_was_left(tmp_path, monkeypatch):
    chunk_db(tmp_path, chunks=7, with_vector=2)
    store, looks = ready_store(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match=r"^store not ready after 30 s: 5 chunks without"):
        store.wait_ready()
    assert len(looks) == 60 and store.backlog == 5 and store.drain_s is None


def test_a_store_in_keyword_mode_has_no_backlog_to_wait_for(tmp_path, monkeypatch):
    chunk_db(tmp_path, chunks=7)
    store, looks = ready_store(tmp_path, monkeypatch, embedding="off")
    store.wait_ready()
    assert len(looks) == 2 and store.backlog is None and store.drain_s is None


def test_harness_start_records_the_backlog_and_the_drain_time(tmp_path, monkeypatch):
    class Store:
        backlog, drain_s = 948, 41.5

        def __init__(self, data, python, src, wait_s):
            self.wait_s = wait_s

        def index(self, env):
            return True

        def start(self, env):
            self.env = env
            return {"memories": 7261}

    monkeypatch.setattr(buildrunner, "suite", lambda: SimpleNamespace(Store=Store))
    h = buildrunner.Harness("B1", tmp_path / "b1", tmp_path / "root", sys.executable, None, 77.0)
    assert h.start() == {"memories": 7261} and h.store.wait_s == 77.0
    assert h.timing["backlog_chunks"] == 948 and h.timing["drain_s"] == 41.5
    assert set(h.timing) == {"index_s", "ready_s", "backlog_chunks", "drain_s"}


def test_the_harness_binds_the_embedder_by_the_name_its_runtime_reads(tmp_path):
    """OMP_NUM_THREADS does not bind the embedder: its model runtime reads a name of its
    own. The store and the worker both get it."""
    h = buildrunner.Harness("B1", tmp_path / "b1", tmp_path / "root", sys.executable, None)
    env = h.environ({"PATH": "/usr/bin"})
    assert buildrunner.EMBED_THREADS == "4"
    assert env["ORT_INTRA_OP_NUM_THREADS"] == "4" and env["OMP_NUM_THREADS"] == "4"
