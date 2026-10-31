# SPDX-License-Identifier: AGPL-3.0-or-later
"""One build of the plugin on a harness store, with its hooks run by a worker.

A build is a frozen, detached copy of a commit (``export_build``). A ``Harness``
gives it its own home, Claude Code config dir, data folder and store, filled from
one shard of the fixture, never from a live folder. A ``Worker`` is a process of
its own with that build on ``PYTHONPATH``; it runs the hooks in-process against the
harness store and answers one JSON line per job.

The store and the embedding model come from ``tools/eval_suite.py``: the same
``Store`` class, so the harness cannot start a store the suite does not.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from .fixtures import Fixture, copy_shard
from .sandbox import clean_environment
from .util import DATA_NAME, TOOLS, load_tool, private_dir, write_private

WORKER = TOOLS / "longtest" / "worker.py"


def suite():
    return load_tool("eval_suite.py", "longtest_eval_suite")


def export_build(repo: Path, commit: str, dest: Path) -> dict[str, str]:
    """A detached, frozen copy of ``commit``: ``git archive`` into ``dest``. The
    run manifest records the commit; the copy cannot move with a branch."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", commit + "^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    data = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        check=True,
        capture_output=True,
    ).stdout
    private_dir(dest)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest.resolve() not in (target, *target.parents):
                raise SystemExit(f"archive member outside the target: {member.name}")
        # Members are checked above and the source is git: keep the tar semantics.
        kwargs = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
        tar.extractall(dest, **kwargs)  # noqa: S202
    return {"commit": sha, "path": str(dest)}


def build_version(build: Path) -> str:
    text = (Path(build) / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


class Harness:
    """A store of one build on one shard of one scale."""

    def __init__(
        self,
        name: str,
        build: Path,
        root: Path,
        python: str,
        models: Path | None,
        wait_s: float = 900.0,
        settings: dict[str, str] | None = None,
    ) -> None:
        self.name, self.build, self.python = name, Path(build), python
        self.root = private_dir(Path(root))
        self.home = private_dir(self.root / "home")
        self.config_dir = private_dir(self.home / ".claude")
        self.data = private_dir(self.config_dir / "plugins" / "data" / DATA_NAME)
        self.models = models
        # The NOBLIVION_* switches of the install under test (``run.json`` holds them):
        # continuity and error recall are off without them.
        self.settings = dict(settings or {})
        self.guard_site: Path | None = None  # a folder with sitecustomize.py (sandbox)
        self.wait_s = wait_s
        self.store: Any = None
        self.timing: dict[str, float] = {}

    def environ(self, base: dict[str, str] | None = None) -> dict[str, str]:
        base = dict(os.environ) if base is None else base
        return clean_environment(
            base,
            HOME=str(self.home),
            XDG_DATA_HOME=str(self.root / "xdg"),
            CLAUDE_CONFIG_DIR=str(self.config_dir),
            CLAUDE_PLUGIN_DATA=str(self.data),
            CLAUDE_PLUGIN_ROOT=str(self.build),
            NOBLIVION_DATA_DIR=str(self.data),
            NOBLIVION_IDLE_EXIT_S="0",
            NOBLIVION_RECALL_CACHE_DIR=str(self.root / "recall-cache"),
            NOBLIVION_GUARD_STATE_DIR=str(self.root / "guard-state"),
            NOBLIVION_GUARD_LOG=str(self.root / "guard-log.jsonl"),
            NOBLIVION_GUARD_TABLE=str(self.root / "guard-table.json"),
            OMP_NUM_THREADS="4",  # a model batch must not take the whole host
            OPENBLAS_NUM_THREADS="2",
            **self.settings,
            PYTHONPATH=self.pythonpath(),
            PYTHONPYCACHEPREFIX=str(self.root / "pycache"),
        )

    def pythonpath(self, *more: Path) -> str:
        parts = [str(self.build / "src"), *map(str, more)]
        if self.guard_site:
            parts.insert(0, str(self.guard_site))
        return os.pathsep.join(parts)

    def prepare(self, fx: Fixture, size: int, shard: int) -> list[Path]:
        folders = copy_shard(fx, size, shard, self.config_dir)
        config = json.loads(
            (self.build / "config" / "config.default.json").read_text(encoding="utf-8")
        )
        write_private(self.data / "config.json", json.dumps(config, indent=1) + "\n")
        if self.models and Path(self.models).is_dir() and not (self.data / "models").exists():
            shutil.copytree(self.models, self.data / "models", symlinks=True)
        return folders

    def start(self, index: bool = True) -> dict[str, Any]:
        import time

        st = suite()
        self.store = st.Store(self.data, self.python, self.build / "src", self.wait_s)
        # The suite store sets PYTHONPATH to the build alone; add the socket guard.
        self.store._env = lambda env: dict(env, PYTHONPATH=self.pythonpath())
        env = self.environ()
        t0 = time.monotonic()
        if index:
            self.store.index(env)
        self.timing["index_s"] = round(time.monotonic() - t0, 2)
        t1 = time.monotonic()
        health = self.store.start(env)
        self.timing["ready_s"] = round(time.monotonic() - t1, 2)
        return health

    def health(self) -> dict | None:
        return self.store.health() if self.store else None

    def stop(self) -> None:
        if self.store is not None:
            self.store.stop()
            self.store = None


class Worker:
    """The process that runs the hooks of one build against one harness."""

    def __init__(self, harness: Harness, timeout_s: float = 120.0) -> None:
        self.harness = harness
        self.timeout_s = timeout_s
        env = harness.environ()
        env["PYTHONPATH"] = harness.pythonpath(TOOLS)
        self.proc = subprocess.Popen(
            [harness.python, str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
            cwd=str(harness.root),
        )
        self.call("init", build=str(harness.build), config_dir=str(harness.config_dir))

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"op": op, **args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"worker of {self.harness.name} exited {self.proc.poll()}")
        return json.loads(line)

    def close(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def store_python(live_data: Path | None) -> str:
    """The python with numpy and fastembed. The live venv is read to run it and
    nothing is written there: ``PYTHONDONTWRITEBYTECODE`` stays on."""
    import sys

    if live_data:
        candidate = Path(live_data) / "venv" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable
