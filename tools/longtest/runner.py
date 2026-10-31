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

from . import config, fixtures, replay, sandbox
from .buildrunner import Harness, build_version, export_build, store_python
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
    """The version of the installed plugin: read from the plugin cache beside the
    data folder, as text only."""
    for p in sorted(Path(data_dir).parent.parent.glob("cache/*/noblivion/*/pyproject.toml")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def freeze(run: Run) -> dict[str, Any]:
    """The pre-registration, its hash, the leakage check and the program hash. After
    this the manifest is marked frozen; a run refuses to start if the hash differs."""
    doc = run.manifest
    text = config.render_prereg()
    write_private(run.dir / "PREREG.md", text)
    fx = run.fixture()
    leak = fixtures.leakage(fx)
    doc.update(
        {
            "prereg_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "leakage": leak,
            "program_sha256": {p.name: sha256_file(p) for p in PROGRAM},
            "frozen": leak["ok"],
        }
    )
    run.save_manifest(doc)
    return doc


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
        wait_s,
        settings=run.manifest.get("settings_env"),
    )
    h.guard_site = guard_site
    h.prepare(fx, size, shard)
    started = time.monotonic()
    health = h.start()
    info = {
        "build": build,
        "size": size,
        "shard": shard,
        "memories": health.get("memories"),
        "index_s": h.timing["index_s"],
        "ready_s": h.timing["ready_s"],
    }
    try:

        def sink(row: dict[str, Any]) -> None:
            row["day"] = day
            run.append("replay", row)

        rows = replay.run_shard(fx, size, shard, h, build, variants, limit, sink)
        info["rows"] = len(rows)
        info["seconds"] = round(time.monotonic() - started, 1)
    finally:
        h.stop()
        if not keep:
            shutil.rmtree(h.root, ignore_errors=True)
    return info


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
    for size in sizes:
        pick = fx.shards(size)[:shards] if shards else fx.shards(size)
        for shard in pick:
            for build in builds:
                jobs.append((build, size, shard))
            for row in replay.run_stock(
                fx, size, shard, fx.shard_dir(size, shard), variants, limit
            ):
                row["day"] = day
                run.append("replay", row)
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
