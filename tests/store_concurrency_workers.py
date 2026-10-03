# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker processes and shared helpers for the store concurrency tests.

Run as ``python tests/store_concurrency_workers.py <role> <json args>``. Each
role is one separate OS process, so the tests load the store the way one
machine does: several Claude Code sessions, the indexer CLI, the embedding
backfill and the store process at once. Every worker writes one JSON report
to ``args["out"]`` and exits 0; the test reads the report. Fictional data
only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from noblivion import db, embedding, launcher, rest  # noqa: E402
from store_helpers import fake_vector  # noqa: E402

PAIR_QUERY = "kiwi"
PAIR_FILE_RE = re.compile(r"^feedback_pair(\d+)_([ab])\.md$")
PAIR_GEN_RE = re.compile(r"kiwi slot (\d+) generation (\d+)")
FAKE_MODEL = "fake"

# -- the fake Ollama server (an embedding backend on loopback) ---------------------


class _OllamaHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        texts = body.get("input") or []
        if isinstance(texts, str):
            texts = [texts]
        time.sleep(self.server.delay_s)  # type: ignore[attr-defined]
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.calls += 1  # type: ignore[attr-defined]
        data = json.dumps({"embeddings": [fake_vector(t) for t in texts]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return None


def start_fake_ollama(delay_s: float = 0.002) -> ThreadingHTTPServer:
    """A loopback server that answers ``POST /api/embed`` with fake vectors."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    server.daemon_threads = True
    server.delay_s = delay_s  # type: ignore[attr-defined]
    server.calls = 0  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def ollama_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


# -- shared helpers ----------------------------------------------------------------


def wait_until(start_at: float) -> None:
    """A start barrier across processes: sleep until the wall clock time."""
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)


def store_env(base: Path, **extra: str) -> dict[str, str]:
    """The environment of a store or CLI process: a temp data dir and HOME, no
    inherited ``NOBLIVION_*`` key, no embedding backend unless ``extra`` sets one."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOBLIVION_")}
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.update(
        NOBLIVION_DATA_DIR=str(base / "data"),
        NOBLIVION_PORT="0",
        NOBLIVION_EMBED_BACKEND="none",
        NOBLIVION_MEMORY_DIRS=str(base / "projects" / "-work-proj-load" / "memory"),
        NOBLIVION_IDLE_EXIT_S="300",
        HOME=str(base),
    )
    env.update(extra)
    return env


def start_workers(
    role: str, jobs: list[dict], out_dir: Path, env: dict[str, str] | None = None
) -> list[tuple[subprocess.Popen, Path]]:
    """Start one worker process per job. Returns (process, report path) pairs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = []
    for i, job in enumerate(jobs):
        out = out_dir / f"{role}-{i}.json"
        job = dict(job, out=str(out))
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(Path(__file__).resolve()), role, json.dumps(job)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        started.append((proc, out))
    return started


def collect(started: list[tuple[subprocess.Popen, Path]], timeout: float = 90.0) -> list[dict]:
    """Wait for every worker; return the reports. A worker that hangs is killed."""
    deadline = time.monotonic() + timeout
    reports = []
    for proc, out in started:
        try:
            _, err = proc.communicate(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
            reports.append({"ok": False, "error": "worker timed out"})
            continue
        try:
            reports.append(json.loads(out.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            tail = (err or b"").decode("utf-8", "replace")[-1500:]
            reports.append({"ok": False, "error": f"no report, exit {proc.returncode}: {tail}"})
    return reports


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pair_problems(seen: dict[int, dict[str, int]]) -> list[str]:
    """A pair is written in one scan, so a committed state holds both files of a
    slot with one generation, or neither."""
    problems = []
    for slot, sides in sorted(seen.items()):
        if set(sides) != {"a", "b"}:
            problems.append(f"slot {slot}: only side {sorted(sides)}")
        elif sides["a"] != sides["b"]:
            problems.append(f"slot {slot}: generations {sides['a']} and {sides['b']}")
    return problems


def pairs_from_index(results: list[dict]) -> tuple[dict[int, dict[str, int]], list[str]]:
    seen: dict[int, dict[str, int]] = {}
    problems: list[str] = []
    for item in results:
        match = PAIR_FILE_RE.match(item.get("source") or "")
        if not match:
            continue
        gen = PAIR_GEN_RE.search(item.get("summary") or "")
        if gen is None or int(gen.group(1)) != int(match.group(1)):
            problems.append(f"row {item.get('id')}: summary does not match its file")
            continue
        seen.setdefault(int(match.group(1)), {})[match.group(2)] = int(gen.group(2))
    return seen, problems


def pairs_from_search(joined: str) -> dict[int, dict[str, int]]:
    seen: dict[int, dict[str, int]] = {}
    if joined == rest.NO_MEMORIES:
        return seen
    for entry in joined.split(rest.ENTRY_SEPARATOR):
        source = rest.source_of(entry) or ""
        match = PAIR_FILE_RE.match(source)
        gen = PAIR_GEN_RE.search(entry)
        if match and gen:
            seen.setdefault(int(match.group(1)), {})[match.group(2)] = int(gen.group(2))
    return seen


# -- roles -------------------------------------------------------------------------


class _Hook:
    """What a hook does: find the store through ``store.json``, prove it, then
    send bearer requests on a new loopback connection per call."""

    def __init__(self, data_dir: Path) -> None:
        info = launcher.read_store_json(data_dir)
        token = launcher.read_token(data_dir)
        if info is None or token is None:
            raise RuntimeError("no store.json or token")
        if launcher.probe(info["port"], token, timeout=5) is None:
            raise RuntimeError("the store failed the listener proof")
        self.port = int(info["port"])
        self.token = token

    def get(self, path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Host": f"127.0.0.1:{self.port}",
                },
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"null")
        finally:
            conn.close()


def role_search(args: dict) -> dict:
    """A Claude Code session: recall searches, index calls, fetches and health
    checks until the stop file appears. Checks every answer."""
    data_dir = Path(args["data_dir"])
    stop = Path(args["stop"])
    rng = random.Random(args["seed"])
    env = {"NOBLIVION_DATA_DIR": str(data_dir)}
    hook = _Hook(data_dir)
    statuses: Counter[int] = Counter()
    kinds: Counter[str] = Counter()
    problems: list[str] = []
    last_gen: dict[int, int] = {}
    last_revs = (-1, -1)
    known_ids: list[int] = []
    ensure_states: Counter[str] = Counter()
    health_max_s = 0.0

    def check_pairs(seen: dict[int, dict[str, int]], where: str) -> None:
        problems.extend(f"{where}: {p}" for p in pair_problems(seen))
        for slot, sides in seen.items():
            gen = max(sides.values())
            if gen < last_gen.get(slot, -1):
                problems.append(f"{where}: slot {slot} went back to generation {gen}")
            last_gen[slot] = max(gen, last_gen.get(slot, -1))

    def record(status: int, kind: str) -> None:
        statuses[status] += 1
        kinds[kind] += 1
        if status != 200:
            problems.append(f"{kind}: status {status}")

    while not stop.exists():
        pick = rng.random()
        if pick < 0.35:
            status, body = hook.get(f"/api/memories/index?q={PAIR_QUERY}&top_k=200")
            record(status, "index")
            if status == 200:
                if body.get("reason"):
                    problems.append(f"index: reason {body['reason']!r}")
                results = body.get("results") or []
                ids = [item["id"] for item in results]
                if len(ids) != len(set(ids)):
                    problems.append("index: an id shows twice")
                known_ids[:] = ids[:50] or known_ids
                seen, bad = pairs_from_index(results)
                problems.extend(f"index: {p}" for p in bad)
                check_pairs(seen, "index")
        elif pick < 0.6:
            status, body = hook.get(f"/api/memories/search?q={PAIR_QUERY}&top_k=50")
            record(status, "search")
            if status == 200:
                if body.get("reason"):
                    problems.append(f"search: reason {body['reason']!r}")
                check_pairs(pairs_from_search(body["results"][0]), "search")
        elif pick < 0.75:
            word = rng.choice(["harbour", "lantern", "compass", "anchor", "orchard"])
            status, body = hook.get(f"/api/memories/index?q={word}&top_k=35")
            record(status, "index-word")
            if status == 200 and body.get("reason"):
                problems.append(f"index-word: reason {body['reason']!r}")
        elif pick < 0.85 and known_ids:
            status, body = hook.get(f"/api/memories/fetch/{rng.choice(known_ids)}")
            record(status, "fetch")
            if status == 200 and body.get("reason") == rest.INTERNAL_REASON:
                problems.append("fetch: internal failure")
        elif pick < 0.97:
            began = time.monotonic()
            status, body = hook.get("/health")
            health_max_s = max(health_max_s, time.monotonic() - began)
            record(status, "health")
            if status == 200:
                if body.get("status") != "ok":
                    problems.append(f"health: status {body.get('status')!r}")
                if body.get("content_rev") is None or body.get("vector_rev") is None:
                    problems.append("health: no revision counters (the health read failed)")
                    continue
                revs = (int(body["content_rev"]), int(body["vector_rev"]))
                if revs[0] < last_revs[0] or revs[1] < last_revs[1]:
                    problems.append(f"health: counters went back {last_revs} -> {revs}")
                last_revs = (max(revs[0], last_revs[0]), max(revs[1], last_revs[1]))
        else:
            # The SessionStart hook. Its listener proof has a 300 ms limit
            # (section 3.3); a store that is slower under this load counts as
            # down, and the hook would start a store that exits on the lock.
            # The spawn here is a no-op; the test checks that one store serves.
            state = launcher.ensure_running(env, spawn=lambda *a, **k: None)
            ensure_states[state] += 1
    return {
        "statuses": {str(k): v for k, v in statuses.items()},
        "kinds": dict(kinds),
        "problems": problems[:50],
        "problem_count": len(problems),
        "last_revs": list(last_revs),
        "ensure": dict(ensure_states),
        "health_max_ms": round(health_max_s * 1000),
    }


def role_backfill(args: dict) -> dict:
    """A second embedding backfill process (as a re-embed CLI would run)."""
    stop = Path(args["stop"])
    embedder = embedding.OllamaEmbedder(FAKE_MODEL, args["ollama_url"])
    conn = db.connect(args["db"])
    passes = embedded = 0
    try:
        while not stop.exists():
            result = embedding.backfill(conn, embedder, batch_size=args.get("batch", 8))
            passes += 1
            embedded += result.embedded
            time.sleep(0.02)
    finally:
        conn.close()
    return {"passes": passes, "embedded": embedded}


def role_rmw(args: dict) -> dict:
    """Read a value, then write the value plus one, in one ``db.write_tx``.

    This is the read-then-write shape of section 6.1 (the trust recompute of
    section 9.4 has it). ``BEGIN IMMEDIATE`` takes the write lock before the
    read, so no other process can commit between the read and the write.
    """
    wait_until(args["start_at"])
    conn = db.connect(args["db"])
    done = 0
    try:
        for _ in range(args["count"]):
            with db.write_tx(conn):
                value = int(db.get_meta(conn, args["key"], "0") or 0)
                db.set_meta(conn, args["key"], str(value + 1))
            done += 1
    finally:
        conn.close()
    return {"done": done}


def role_open_db(args: dict) -> dict:
    """Open a fresh database: connect and migrate, as the store and the CLI do."""
    wait_until(args["start_at"])
    conn = db.open_db(args["db"], allow_migrate=args.get("allow_migrate", True))
    try:
        return {"version": db.user_version(conn), "revs": list(db.revisions(conn))}
    finally:
        conn.close()


def role_ensure(args: dict) -> dict:
    """``noblivion ensure-running``, all callers released at one moment."""
    wait_until(args["start_at"])
    return {"state": launcher.ensure_running()}


ROLES: dict[str, Callable[[dict], dict]] = {
    "search": role_search,
    "backfill": role_backfill,
    "rmw": role_rmw,
    "open_db": role_open_db,
    "ensure": role_ensure,
}


def main(argv: list[str]) -> int:
    role, args = argv[0], json.loads(argv[1])
    try:
        report = {"ok": True, **ROLES[role](args)}
    except (Exception, sqlite3.Error) as exc:  # noqa: BLE001 - the test reads the report
        report = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-2000:],
        }
    tmp = Path(args["out"] + ".tmp")
    tmp.write_text(json.dumps(report), encoding="utf-8")
    os.replace(tmp, args["out"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
