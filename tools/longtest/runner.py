# SPDX-License-Identifier: AGPL-3.0-or-later
"""The run folder, the freeze, the replay over all scales, the pilot and the job loop.

The run folder (mode 0700) holds ``run.json`` (the manifest), ``builds/b0`` and
``builds/b1`` (frozen copies), ``fixtures/``, ``stores/<build>/<scale>/shard-NN/``,
``out/*.jsonl``, ``daily/``, ``private/``, ``state.json``, ``ALERT`` and ``stop``.
Nothing here writes to a live folder: the live folders are named in ``run.json``
only so that the open guard can refuse a write under them.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from . import config, fixtures, realreplay, replay, sandbox
from .buildrunner import Harness, build_version, export_build, index_wait_s, store_python
from .config import SCALES
from .fixtures import Fixture
from .util import (
    TOOLS,
    append_jsonl,
    json_text,
    private_dir,
    read_jsonl,
    sha256_file,
    write_private,
)

SIZE_NAMES = {v: k for k, v in SCALES.items()}
PROGRAM = sorted((TOOLS / "longtest").glob("*.py"))
KEEP_FROM = 1000  # stores of this size and above are kept: their index takes hours
VOLATILE = ("guard-state", "recall-cache", "guard-log.jsonl", "guard-table.json")
_lock = threading.Lock()


class Run:
    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.out = self.dir / "out"

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads((self.dir / "run.json").read_text(encoding="utf-8"))

    def save_manifest(self, doc: dict[str, Any]) -> None:
        write_private(self.dir / "run.json", json_text(doc))

    def build_path(self, build: str) -> Path:
        return self.dir / "builds" / build.lower()

    def fixture(self) -> Fixture:
        return Fixture(self.dir / "fixtures")

    def append(self, name: str, row: dict[str, Any]) -> None:
        with _lock:
            append_jsonl(self.out / f"{name}.jsonl", row)

    def append_many(self, name: str, rows: Iterable[dict[str, Any]]) -> None:
        with _lock:
            for row in rows:
                append_jsonl(self.out / f"{name}.jsonl", row)

    def done(self, day: int) -> set[tuple[str, int, int]]:
        return {
            (r["build"], r["size"], r["shard"])
            for r in read_jsonl(self.out / "replay-done.jsonl")
            if r["day"] == day
        }


def init_run(
    run_dir: Path,
    repo: Path,
    b0: str,
    b1: str,
    sizes: Iterable[int],
    live_config: Path,
    live_data: Path,
    seed: int = config.SEED,
) -> dict[str, Any]:
    """Make the run folder, freeze both builds, generate the fixtures. Installs
    nothing and starts nothing."""
    run = Run(run_dir)
    for sub in ("out", "daily", "private", "stores", "builds"):
        private_dir(run.dir / sub)
    builds = {}
    for name, commit in (("B0", b0), ("B1", b1)):
        info = export_build(repo, commit, run.build_path(name))
        info["version"] = build_version(run.build_path(name))
        builds[name] = info
    sandbox.make_bin(run.dir / "bin")
    sandbox.write_guard_site(
        run.dir / "private" / "guard-site", run.dir / "private" / "external_connects.log"
    )
    fx = Fixture.ensure(run.dir / "fixtures", seed, sizes)
    doc = {
        "run_id": run.dir.name,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "builds": builds,
        "seed": seed,
        "sizes": sorted(sizes),
        "fixture": fx.fingerprint(),
        "live": {"config_dir": str(live_config), "data_dir": str(live_data)},
        "plugin_version": installed_version(live_data),
        "settings_env": host_settings(live_config),
        "frozen": False,
    }
    run.save_manifest(doc)
    return doc


def host_settings(config_dir: Path) -> dict[str, str]:
    """The NOBLIVION_* switches that the install under test sets in the ``env`` block
    of the Claude Code settings: names and values read once, never written. A switch
    whose value is a path is left out (it would point at live data)."""
    try:
        doc = json.loads((Path(config_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = doc.get("env") if isinstance(doc, dict) else None
    return {
        k: str(v)
        for k, v in sorted((env or {}).items())
        if k.startswith("NOBLIVION_") and "/" not in str(v) and k != "NOBLIVION_MINER"
    } | ({"NOBLIVION_MINER": "0"} if "NOBLIVION_MINER" in (env or {}) else {})


def installed_version(data_dir: Path) -> str:
    """The version of the installed plugin, from ``installed_plugins.json`` (read
    only). Falls back to the highest version folder of the plugin cache."""
    plugins = Path(data_dir).parent.parent
    try:
        doc = json.loads((plugins / "installed_plugins.json").read_text(encoding="utf-8"))
        for name, entries in (doc.get("plugins") or {}).items():
            if name.split("@")[0] == "noblivion" and entries:
                return str(entries[0].get("version", ""))
    except (OSError, ValueError, AttributeError):
        pass
    found = [p.name for p in plugins.glob("cache/*/noblivion/*") if p.is_dir()]
    return max(
        found, key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")], default=""
    )


def program_files(tools: Path) -> list[Path]:
    return sorted(p for p in Path(tools).rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def program_hashes(tools: Path) -> dict[str, str]:
    return {str(p.relative_to(tools)): sha256_file(p) for p in program_files(tools)}


def freeze_program(run: Run) -> Path:
    """The program of the run is a frozen copy of ``tools/`` in the run folder. The
    units and the commands of the run use that copy, so a later change of the
    repository cannot change a run that is under way."""
    target = run.dir / "program" / "tools"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(TOOLS, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def freeze(run: Run) -> dict[str, Any]:
    """The pre-registration, its hash, the leakage check and the program hash. After
    this the manifest is marked frozen; a run refuses to start if the hash differs.
    A second freeze (after a fix to a failed pilot gate) is logged in ``freezes``."""
    doc = run.manifest
    text = config.render_prereg()
    write_private(run.dir / "PREREG.md", text)
    fx = run.fixture()
    leak = fixtures.leakage(fx)
    copy = freeze_program(run)
    doc.setdefault("freezes", []).append(
        {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prereg_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )
    doc.update(
        {
            "prereg_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "leakage": leak,
            "program_sha256": program_hashes(copy),
            "frozen": leak["ok"],
        }
    )
    run.save_manifest(doc)
    return doc


def verify_frozen(run: Run, tools: Path = TOOLS) -> list[str]:
    """What differs from the freeze: the pre-registration file, its rendered text, and
    the program. Empty when the run may start. ``tools`` is the program that runs now;
    it must be the frozen copy."""
    doc = run.manifest
    problems: list[str] = []
    if not doc.get("frozen"):
        problems.append("not frozen")
        return problems
    path = run.dir / "PREREG.md"
    try:
        config.check_prereg(path, doc["prereg_sha256"])
    except (SystemExit, OSError) as exc:
        problems.append(f"PREREG.md: {exc}")
    if config.prereg_hash() != doc["prereg_sha256"]:
        problems.append("the parameters in config.py differ from PREREG.md")
    copy = run.dir / "program" / "tools"
    if Path(tools).resolve() != copy.resolve():
        problems.append(f"the program runs from {tools}, not from the frozen copy")
    elif program_hashes(copy) != doc.get("program_sha256"):
        problems.append("the frozen program changed")
    return problems


# Files that belong to the pilot loop and not to the 7 days.
PILOT_LOOP_FILES = (
    "out/heartbeat.jsonl",
    "out/soak.jsonl",
    "out/sandbox.jsonl",
    "out/live.jsonl",
    "out/status.jsonl",
    "out/latency.jsonl",
    "out/determinism.jsonl",
    "state.json",
    "ALERT",
    "finished",
)


def archive_pilot_loop(run: Run) -> list[str]:
    """Move the files of the pilot loop into ``pilot-loop/``, so that day 1 starts
    with empty series. The stores and the day-0 replay rows stay: they are reused."""
    target = private_dir(run.dir / "pilot-loop")
    moved = []
    for name in (*PILOT_LOOP_FILES, "daily"):
        src = run.dir / name
        if src.exists():
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(dest.name + dt.datetime.now().strftime(".%H%M%S"))
            shutil.move(str(src), str(dest))
            moved.append(name)
    private_dir(run.dir / "daily")
    return moved


def reset_volatile(root: Path) -> None:
    """The state a hook keeps between calls (one show per session, the recall cache,
    the guard log). A kept store starts every use without it."""
    for name in VOLATILE:
        path = Path(root) / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


# -- the replay over scales ---------------------------------------------------------


def shard_root(run: Run, build: str, size: int, shard: int) -> Path:
    return run.dir / "stores" / build.lower() / SIZE_NAMES[size].lower() / f"shard-{shard:02d}"


def replay_task(
    run: Run,
    fx: Fixture,
    build: str,
    size: int,
    shard: int,
    python: str,
    models: Path | None,
    limit: int | None,
    variants: Sequence[str] | None,
    day: int,
    guard_site: Path | None,
    keep: bool,
    wait_s: float,
) -> dict[str, Any]:
    h = Harness(
        build,
        run.build_path(build),
        shard_root(run, build, size, shard),
        python,
        models,
        index_wait_s(size, wait_s),
        settings=run.manifest.get("settings_env"),
    )
    h.guard_site = guard_site
    keep = keep or size >= KEEP_FROM
    reset_volatile(h.root)
    h.prepare(fx, size, shard)
    started = time.monotonic()
    info: dict[str, Any] = {"build": build, "size": size, "shard": shard}
    try:
        # the start is inside the try: a store that is not ready in time must be
        # stopped, or it keeps embedding for hours and holds the lock of the next try
        health = h.start()
        info.update(
            memories=health.get("memories"),
            index_s=h.timing["index_s"],
            ready_s=h.timing["ready_s"],
        )
        rows = replay.run_shard(fx, size, shard, h, build, variants, limit)
        # The rows of a task go in together, with a marker, so a restart never meets
        # half a task: a task without a marker is run again.
        for row in rows:
            row["day"] = day
        run.append_many("replay", rows)
        run.append("replay-done", {"day": day, "build": build, "size": size, "shard": shard})
        info["rows"] = len(rows)
        info["seconds"] = round(time.monotonic() - started, 1)
    finally:
        h.stop()
        if keep:
            reset_volatile(h.root)
        else:
            shutil.rmtree(h.root, ignore_errors=True)
    return info


def bench_kept_store(run: Run, build: str, size: int, budget_s: float) -> dict[str, Any] | None:
    """The prompt hook of one build on the store that the replay keeps at this size:
    p50 and p95 over up to ``budget_s`` seconds. ``None`` when the store is not there
    yet (the first night builds it)."""
    from . import soak
    from .buildrunner import Worker

    fx = run.fixture()
    shard = fx.shards(size)[0]
    root = shard_root(run, build, size, shard)
    if not (root / "home").is_dir():
        return None
    live_data = Path(run.manifest["live"]["data_dir"])
    h = Harness(
        build,
        run.build_path(build),
        root,
        store_python(live_data),
        live_data / "models",
        settings=run.manifest.get("settings_env"),
    )
    h.guard_site = run.dir / "private" / "guard-site"
    reset_volatile(root)
    texts = [fx.prompt_of(q) for q in fx.queries_for(size, ["V3", "V1"], shard)]
    first = min(fx.scale(size)["lessons"].values(), key=lambda r: r["shard"])
    cwd = f"{fixtures.WORK}/{first['session_project']}"
    try:
        health = h.start()
        pid = h.store.proc.pid if h.store.proc else None
        count = {"i": 0}

        def call(prompt: str, w: Any = None) -> float:
            count["i"] += 1
            return w.call("prompt", sid=f"bench-{count['i']}", cwd=cwd, prompt=prompt)["ms"]

        with Worker(h) as w:
            lat = soak.latency_benchmark(lambda p: call(p, w), texts, n=100, budget_s=budget_s)
        res = soak.resources(h.data, pid)
    finally:
        h.stop()
        reset_volatile(root)
    return {
        "build": build,
        "size": size,
        "memories": health.get("memories"),
        "rss_mb": res["rss_mb"],
        "db_mb": res["db_mb"],
        "wal_mb": res["wal_mb"],
        "quick_check": res["quick_check"],
        **lat,
    }


def replay_all(
    run: Run,
    sizes: Sequence[int],
    shards: int | None,
    limit: int | None,
    variants: Sequence[str] | None = None,
    builds: Sequence[str] = ("B0", "B1"),
    parallel: int = 2,
    day: int = 1,
    keep: bool = False,
    wait_s: float = 900.0,
    python: str | None = None,
    models: Path | None = None,
    guard_site: Path | None = None,
) -> list[dict[str, Any]]:
    """Arms b and c over the synthetic lessons: every build and every shard, plus the
    stock arms. Rows go to ``out/replay.jsonl``; returns one info row per task."""
    fx = run.fixture()
    live_data = Path(run.manifest["live"]["data_dir"])
    python = python or store_python(live_data)
    models = models or (live_data / "models")
    jobs = []
    finished = run.done(day)
    for size in sizes:
        pick = fx.shards(size)[:shards] if shards else fx.shards(size)
        for shard in pick:
            for build in builds:
                if (build, size, shard) not in finished:
                    jobs.append((build, size, shard))
            if ("stock", size, shard) not in finished:
                rows = replay.run_stock(fx, size, shard, fx.shard_dir(size, shard), variants, limit)
                for row in rows:
                    row["day"] = day
                run.append_many("replay", rows)
                run.append(
                    "replay-done", {"day": day, "build": "stock", "size": size, "shard": shard}
                )
    infos: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = [
            pool.submit(
                replay_task,
                run,
                fx,
                b,
                s,
                sh,
                python,
                models,
                limit,
                variants,
                day,
                guard_site,
                keep,
                wait_s,
            )
            for b, s, sh in jobs
        ]
        for f in futures:
            infos.append(f.result())
    return infos


def counts_hint(config_dir: Path) -> list[int]:
    """Notes per project folder of the real corpus, to size the wait of a store."""
    return [len(list(f.glob("*.md"))) for f in realreplay.real_memory_folders(config_dir)]


def seed_soak_store(run: Run, root: Path, size: int) -> bool:
    """Start the soak store from a copy of the kept B1 replay store of the same size.
    The notes and their vectors are the same, and a store keeps its paths relative to
    the project folder, so a copy needs a scan, not three hours of embedding. Only when
    the soak folder is not there yet."""
    root = Path(root)
    fx = run.fixture()
    source = shard_root(run, "B1", size, fx.shards(size)[0])
    if root.exists() and any(root.iterdir()) or not (source / "home").is_dir():
        return False
    reset_volatile(source)
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, root, symlinks=True, dirs_exist_ok=True)
    (root / "home/.claude/plugins/data/noblivion-noblivion/store.json").unlink(missing_ok=True)
    return True


def real_replay(
    run: Run,
    heldout_dir: Path,
    levels: Sequence[int],
    day: int,
    guard_site: Path | None = None,
    python: str | None = None,
    wait_s: float = 900.0,
) -> dict[str, Any]:
    """Arm b: the dev prompts of the real set through B0 and B1, on a snapshot of the
    real corpus plus ``level`` filler notes. Rows go to ``out/real.jsonl``; the row
    lines of B1 that the pool lacks go to ``private/`` for the labels."""
    from .buildrunner import Worker

    fx = run.fixture()
    live = Path(run.manifest["live"]["config_dir"])
    live_data = Path(run.manifest["live"]["data_dir"])
    python = python or store_python(live_data)
    prompts = realreplay.dev_prompts(heldout_dir)
    salt = run.manifest["run_id"]
    done: dict[str, Any] = {"prompts": len(prompts), "levels": {}}
    for level in levels:
        if level and level not in fx.sizes():
            continue
        per_build: dict[str, list[dict[str, Any]]] = {}
        shown_b1: dict[str, list[str]] = {}
        for build in ("B0", "B1"):
            root = run.dir / "stores" / "real" / build.lower() / str(level)
            h = Harness(
                build,
                run.build_path(build),
                root,
                python,
                live_data / "models",
                wait_s=index_wait_s(sum(counts_hint(live)) + level, wait_s),
                settings=run.manifest.get("settings_env"),
            )
            h.guard_site = guard_site
            reset_volatile(root)
            h.prepare_empty()
            counts = realreplay.copy_real_corpus(live, h.config_dir)
            if level:
                realreplay.add_distractors(
                    fx.distractors(level, fx.shards(level)[0], level),
                    h.config_dir,
                    realreplay.session_project(counts),
                )
            try:
                h.start()
                with Worker(h) as w:

                    def call(sid: str, cwd: str, text: str, w: Any = w) -> dict[str, Any]:
                        return w.call("prompt", sid=sid, cwd=cwd, prompt=text)

                    rows, shown = realreplay.replay_prompts(
                        call, prompts, realreplay.session_cwd(counts), replay.ask_text, salt, build
                    )
            finally:
                h.stop()
                reset_volatile(root)
            for r in rows:
                run.append("real", {**r, "build": build, "level": level, "day": day})
            per_build[build] = rows
            if build == "B1":
                shown_b1 = shown
        done["levels"][str(level)] = realreplay.summary(per_build)
        if level == levels[0]:
            done["pool_additions"] = realreplay.export_pool_additions(
                prompts, shown_b1, run.dir / "private" / "pool-additions.jsonl"
            )
    return done


def replay_rows(run: Run, day: int | None = None) -> list[dict[str, Any]]:
    rows = read_jsonl(run.out / "replay.jsonl")
    return [r for r in rows if day is None or r.get("day") == day]


def determinism_hash(rows: Iterable[dict[str, Any]], size: int) -> str:
    """The hash of the outcomes (query, build, channel, hit, rank, rows, chars) of one
    scale: equal on a re-run when the hooks are deterministic. Time is left out."""
    keep = sorted(
        (
            r["qid"],
            r["build"],
            r["channel"],
            bool(r.get("hit")),
            r.get("rank"),
            r.get("rows"),
            r.get("chars"),
        )
        for r in rows
        if r["size"] == size
    )
    return hashlib.sha256(json.dumps(keep, default=str).encode()).hexdigest()[:16]


# -- the pilot ----------------------------------------------------------------------


def snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Name, size and mtime of every file under a live folder: file stat only, no
    read. Two snapshots that are equal prove the program wrote nothing there."""
    snap: dict[str, tuple[int, int]] = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            snap[str(p)] = (st.st_size, int(st.st_mtime_ns))
    return snap


def changed_files(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    ignore: Callable[[str], bool] = lambda name: False,
) -> list[str]:
    names = set(before) | set(after)
    return sorted(n for n in names if before.get(n) != after.get(n) and not ignore(n))


def zero_token_gate(run: Run, python: str) -> dict[str, Any]:
    """Pilot gate 2: the start check passes, a connect to a public address fails
    through the guard of a child interpreter, and nothing is counted as a leak."""
    import subprocess

    log = run.dir / "private" / "external_connects.log"
    site = sandbox.write_guard_site(run.dir / "private" / "guard-site", log)
    env = sandbox.clean_environment(os.environ, PYTHONPATH=str(site))
    probe = (
        "import socket\ntry:\n socket.create_connection(('192.0.2.1', 80), timeout=1)\n"
        "except ConnectionRefusedError:\n print('refused')\nexcept OSError as e:\n"
        " print('other', type(e).__name__)\n"
    )
    done = subprocess.run(
        [python, "-c", probe], env=env, capture_output=True, text=True, timeout=30
    )
    check = sandbox.start_check(os.environ, PROGRAM)
    check["public_connect_refused"] = done.stdout.strip() == "refused"
    check["refused_logged"] = sandbox.count_refused(log)
    return check
