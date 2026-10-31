# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time the recall path on the scale fixture: hook stages, ``Ranker.search``, store memory.

NOBLIVION-157. The scale fixture (``scale_fixture.py``) gives notes at 1,000, 5,000
and 20,000. This tool builds a scratch store on one scale, then times what a prompt
costs:

- the store process: time to first idle, resident memory (``VmRSS``, peak ``VmHWM``)
  before the first query, after it and after the query set;
- ``Ranker.search``: the cold reload, the first search of each pool (pool build and
  BM25 build), and the warm search (p50, p95), in one process;
- the recall hook (``hooks/recall_hook.py``) against that store, in its shipped
  configuration: the stages ``load_session_corpus``, ``BM25`` build,
  ``rerank_index``, the store call ``recall_index`` and the whole ``main``, and the
  wall time of a hook process.

No model call. The embedder is a bag-of-words hash of 384 dims (the dims of the
default model), so a vector costs the memory and the time of a real one but not
its quality. The scores of such a vector tell nothing about relevance; only the
cost is meant. Run only in a scratch folder (mode 0700) outside the repository and
not on a RAM disk. The tool refuses a live data folder.

Usage:
    python tools/eval/profile_recall_scale.py build   --fixture FX --scale 5000 --work DIR
    python tools/eval/profile_recall_scale.py measure --fixture FX --scale 5000 --work DIR \
        [--json OUT]

``build`` runs a store on the fixture until every chunk has a vector, then stops it.
``measure`` copies nothing: it starts a store on the built data folder.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIM = 384
FAKE_MODEL = "profile-fake"
SAMPLE_KINDS = ("V1", "V2", "V3", "V4", "V5")


# -- the fake embedder ------------------------------------------------------------


class FakeEmbedder:
    """A bag-of-words hash vector of ``DIM`` dims. Deterministic, no model, no network."""

    remote = False
    backend = "fake"
    model = FAKE_MODEL
    dim = DIM

    @property
    def model_id(self) -> str:
        return f"{self.backend}:{self.model}"

    def load(self) -> None:
        return None

    def _vector(self, text: str) -> list[float]:
        from noblivion import bm25

        vec = [0.0] * DIM
        for token in bm25.tokenize(text):
            slot = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")
            vec[slot % DIM] += 1.0
        if not any(vec):
            vec[0] = 1.0
        return vec

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


# -- helpers ----------------------------------------------------------------------


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def refuse_unsafe(work: Path) -> None:
    live = Path.home() / ".claude" / "plugins" / "data"
    resolved = work.resolve()
    if live in resolved.parents or resolved == live:
        sys.exit("refused: the work folder is inside the live data folder")
    if str(resolved).startswith("/tmp/") or str(resolved).startswith("/dev/shm/"):
        sys.exit("refused: the work folder is on a RAM disk")


def proc_kb(pid: int, key: str) -> int:
    with open(f"/proc/{pid}/status", encoding="ascii") as fh:
        for line in fh:
            if line.startswith(key + ":"):
                return int(line.split()[1])
    return 0


def percentile(values: list[float], point: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(round(point * (len(ordered) - 1))))]


def summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.5), 1),
        "p95": round(percentile(values, 0.95), 1),
        "max": round(max(values), 1) if values else 0.0,
        "mean": round(statistics.fmean(values), 1) if values else 0.0,
    }


def shard_dir(fixture: Path, scale: int) -> Path:
    return fixture / f"scale-{scale:05d}" / "shard-00"


def store_env(work: Path, fixture: Path, scale: int) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOBLIVION_", "CLAUDE"))}
    env.update(
        NOBLIVION_DATA_DIR=str(work / "data"),
        NOBLIVION_CONFIG=str(work / "config.json"),
        NOBLIVION_PORT="0",
        NOBLIVION_IDLE_EXIT_S="0",
        NOBLIVION_STORE_AUTOSTART="0",
        CLAUDE_CONFIG_DIR=str(shard_dir(fixture, scale)),
    )
    return env


def prepare_work(work: Path) -> None:
    private_dir(work)
    private_dir(work / "data")
    shutil.copyfile(ROOT / "config" / "config.default.json", work / "config.json")
    os.chmod(work / "config.json", 0o600)


# -- the store process ---------------------------------------------------------------


def serve() -> int:
    """Run a store with the fake embedder in this process (internal)."""
    sys.path.insert(0, str(ROOT / "src"))
    from noblivion import config, embedding, store

    settings = config.load_settings()
    store_settings = config.load_store_settings()
    from dataclasses import replace

    store_settings = replace(store_settings, index_interval_s=0, idle_exit_s=0)
    service = embedding.EmbeddingService(
        embedding.EmbeddingSettings(backend="fastembed", model=FAKE_MODEL),
        factory=lambda *a: FakeEmbedder(),
    )
    st = store.Store(settings, store_settings, embedding_service=service, labeller=None)
    config.install_stamp(settings.data_dir).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.install_stamp(settings.data_dir).write_text(
        json.dumps({"version": "profile"}), encoding="utf-8"
    )
    st.open()
    st.start_background()
    signal.signal(signal.SIGTERM, lambda *_: st.request_stop("term"))
    return st.serve()


class Running:
    """A store process on the work folder."""

    def __init__(self, work: Path, fixture: Path, scale: int) -> None:
        self.work = work
        env = store_env(work, fixture, scale)
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(work / "store.err", "wb"),
        )
        self.started = time.monotonic()
        info = work / "data" / "store.json"
        deadline = time.monotonic() + 60
        while not info.is_file():
            if time.monotonic() > deadline or self.proc.poll() is not None:
                sys.exit("the store did not start; see store.err")
            time.sleep(0.05)
        time.sleep(0.2)
        self.port = int(json.loads(info.read_text())["port"])
        self.token = (work / "data" / "token").read_text().strip()

    def get(self, path: str):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    def wait_idle(self, limit_s: float = 7200) -> float:
        """Seconds from start until the scan is done and no vector is missing."""
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            try:
                h = self.get("/api/health")
            except Exception:  # noqa: BLE001 - the store is busy; ask again
                time.sleep(0.5)
                continue
            emb = h.get("embedding") or {}
            if (
                h.get("index_state") == "idle"
                and emb.get("state") == "ready"
                and emb.get("missing_vectors") == 0
                and h.get("memories")
            ):
                return time.monotonic() - self.started
            time.sleep(1.0)
        sys.exit("timeout while the store settled")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def build(args) -> int:
    work = Path(args.work)
    refuse_unsafe(work)
    prepare_work(work)
    run = Running(work, Path(args.fixture), args.scale)
    try:
        seconds = run.wait_idle()
        h = run.get("/api/health")
        print(
            json.dumps(
                {
                    "scale": args.scale,
                    "build_seconds": round(seconds, 1),
                    "memories": h.get("memories"),
                    "rss_mb": proc_kb(run.proc.pid, "VmRSS") // 1024,
                    "db_mb": round((work / "data" / "noblivion.db").stat().st_size / 1e6, 1),
                }
            )
        )
    finally:
        run.stop()
    return 0


# -- queries ---------------------------------------------------------------------------


def pick_queries(fixture: Path, scale: int, count: int) -> tuple[list[tuple[str, str]], str, list]:
    """``(prompt, cwd)`` pairs spread over the projects, and the biggest project's cwd."""
    rows = [json.loads(x) for x in (fixture / "queries.jsonl").read_text().splitlines()]
    detail = json.loads((fixture / f"scale-{scale:05d}" / "scale.json").read_text())
    projects = detail["shards_detail"][0]["projects"]
    biggest = max(projects, key=lambda p: p["notes"])
    homes = detail["lessons"]
    by_name = {p["name"]: p["cwd"] for p in projects}
    chosen = [r for r in rows if r["variant"] in SAMPLE_KINDS]
    step = max(1, len(chosen) // count)
    mixed = []
    for r in chosen[::step][:count]:
        home = homes.get(r["lesson"], {})
        name = home.get("other_project") if r["cwd"] == "other" else home.get("note_project")
        mixed.append((r["prompt"], by_name.get(name, biggest["cwd"])))
    worst = [(p, biggest["cwd"]) for p, _ in mixed]
    return mixed, biggest["cwd"], worst


# -- the Ranker, in this process ----------------------------------------------------------


def measure_ranker(work: Path, fixture: Path, scale: int, queries) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from noblivion import db, embedding, ranking

    service = embedding.EmbeddingService(
        embedding.EmbeddingSettings(backend="fastembed", model=FAKE_MODEL),
        factory=lambda *a: FakeEmbedder(),
    )
    service.embedder = FakeEmbedder()
    service.state = embedding.STATE_READY
    ranker = ranking.Ranker(service)
    conn = db.connect(work / "data" / "noblivion.db", create=False)
    out: dict = {"rss_start_mb": proc_kb(os.getpid(), "VmRSS") // 1024}
    ranker.index.set_model(service.model_id)
    t = time.perf_counter()
    ranker.index.refresh(conn)
    out["reload_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["rss_after_reload_mb"] = proc_kb(os.getpid(), "VmRSS") // 1024
    first, warm = [], []
    seen: set = set()
    for prompt, cwd in queries:
        slug = "-" + cwd.strip("/").replace("/", "-")
        kwargs = dict(project="claude_code", top_k=30, root=slug, gate=True)
        t = time.perf_counter()
        ranker.search(conn, prompt, **kwargs)
        ms = (time.perf_counter() - t) * 1000
        (warm if slug in seen else first).append(ms)
        seen.add(slug)
    for prompt, cwd in queries * 2:
        slug = "-" + cwd.strip("/").replace("/", "-")
        t = time.perf_counter()
        ranker.search(conn, prompt, project="claude_code", top_k=30, root=slug, gate=True)
        warm.append((time.perf_counter() - t) * 1000)
    out["first_search_per_pool_ms"] = summary(first)
    out["warm_search_ms"] = summary(warm)
    out["bm25_builds"] = ranker.index.bm25_builds
    out["rss_end_mb"] = proc_kb(os.getpid(), "VmRSS") // 1024
    out["rss_peak_mb"] = proc_kb(os.getpid(), "VmHWM") // 1024
    conn.close()
    return out


# -- the hook ------------------------------------------------------------------------------


def hook_env(work: Path, fixture: Path, scale: int, run: Running) -> dict:
    env = store_env(work, fixture, scale)
    env.update(
        NOBLIVION_RECALL_CACHE_DIR=str(work / "cache"),
        NOBLIVION_CONFIG=str(work / "config.json"),
        PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        HOME=str(work / "home"),
    )
    private_dir(work / "home")
    return env


def payload(prompt: str, cwd: str, sid: str) -> str:
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": prompt, "cwd": cwd}
    )


def measure_hook(work: Path, fixture: Path, scale: int, run: Running, mixed, worst) -> dict:
    sys.path.insert(0, str(ROOT / "tests"))
    from hookload import load_hook

    rh = load_hook("recall_hook", "profile_recall_hook")
    stages: dict[str, list[float]] = {}

    def timed(name: str, fn):
        def wrapper(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                stages.setdefault(name, []).append((time.perf_counter() - t) * 1000)

        return wrapper

    for name in ("load_session_corpus", "rerank_index", "recall_index", "hygiene_index"):
        setattr(rh, name, timed(name, getattr(rh, name)))
    real_bm25 = rh.BM25

    class TimedBM25(real_bm25):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            t = time.perf_counter()
            super().__init__(*a, **k)
            stages.setdefault("bm25_build", []).append((time.perf_counter() - t) * 1000)

    rh.BM25 = TimedBM25
    env = hook_env(work, fixture, scale, run)
    result: dict = {}
    for label, queries in (("mixed", mixed), ("biggest_project", worst)):
        stages.clear()
        totals: list[float] = []
        statuses: dict[str, int] = {}
        for n, (prompt, cwd) in enumerate(queries):
            out = io.StringIO()
            t = time.perf_counter()
            rh.main(
                stdin=io.StringIO(payload(prompt, cwd, f"s-{label}-{n}")),
                stdout=out,
                environ=dict(env),
            )
            totals.append((time.perf_counter() - t) * 1000)
        log = (work / "cache" / "recall.log").read_text().splitlines()[-len(queries) :]
        for line in log:
            for part in line.split():
                if part.startswith(("fail:", "joined", "rerank_off")) or "fail:" in part:
                    statuses[part[:40]] = statuses.get(part[:40], 0) + 1
        # The first call of each folder parses the files; the rest read the cache.
        result[label] = {
            "main_ms": summary(totals),
            "main_ms_without_first": summary(totals[1:]),
            "stages_ms": {k: summary(v) for k, v in sorted(stages.items())},
            "log_tail_sample": log[-1][:200] if log else "",
            "failures": sum(1 for line in log if "fail:" in line),
        }
    # Wall time of a hook process: Python start, imports, one prompt.
    walls: list[float] = []
    for n, (prompt, cwd) in enumerate(worst[:15]):
        t = time.perf_counter()
        subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "recall_hook.py")],
            input=payload(prompt, cwd, f"s-wall-{n}").encode(),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        walls.append((time.perf_counter() - t) * 1000)
    result["process_wall_ms_biggest_project"] = summary(walls)
    return result


def measure(args) -> int:
    work = Path(args.work)
    fixture = Path(args.fixture)
    refuse_unsafe(work)
    prepare_work(work)
    shutil.rmtree(work / "cache", ignore_errors=True)
    mixed, _biggest, worst = pick_queries(fixture, args.scale, args.queries)
    run = Running(work, fixture, args.scale)
    report: dict = {
        "scale": args.scale,
        "loadavg_start": [round(x, 1) for x in os.getloadavg()],
        "cpus": os.cpu_count(),
    }
    try:
        report["store_settle_s"] = round(run.wait_idle(), 1)
        report["store_rss_idle_mb"] = proc_kb(run.proc.pid, "VmRSS") // 1024
        prompt, cwd = mixed[0]
        slug = "-" + cwd.strip("/").replace("/", "-")
        import urllib.parse

        def search(q: str, root: str) -> float:
            qs = urllib.parse.urlencode(
                {"q": q, "project": "claude_code", "top_k": 30, "root": root}
            )
            t0 = time.perf_counter()
            run.get(f"/api/memories/search?{qs}")
            return (time.perf_counter() - t0) * 1000

        report["store_first_query_ms"] = round(search(prompt, slug), 1)
        report["store_rss_after_first_mb"] = proc_kb(run.proc.pid, "VmRSS") // 1024
        http = [search(p, "-" + c.strip("/").replace("/", "-")) for p, c in mixed * 2]
        report["store_http_search_ms"] = summary(http)
        report["hook"] = measure_hook(work, fixture, args.scale, run, mixed, worst)
        report["store_rss_end_mb"] = proc_kb(run.proc.pid, "VmRSS") // 1024
        report["store_rss_peak_mb"] = proc_kb(run.proc.pid, "VmHWM") // 1024
    finally:
        run.stop()
    report["ranker_in_process"] = measure_ranker(work, fixture, args.scale, mixed)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "measure"):
        p = sub.add_parser(name)
        p.add_argument("--fixture", required=True)
        p.add_argument("--scale", type=int, required=True)
        p.add_argument("--work", required=True)
        if name == "measure":
            p.add_argument("--queries", type=int, default=40)
            p.add_argument("--json")
    sub.add_parser("serve")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return serve()
    return build(args) if args.cmd == "build" else measure(args)


if __name__ == "__main__":
    sys.exit(main())
