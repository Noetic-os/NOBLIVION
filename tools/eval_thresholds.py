# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the score thresholds with the shipped embedding model (NOBLIVION-33, NOBLIVION-48).

The eval set is ``tools/eval/thresholds.json``: made-up developer memories,
labelled queries, near-miss negatives and duplicate pairs. It is small, and it
has no held-out split: each threshold is chosen and scored on the same items.

The script starts a real store in this process (``noblivion.store.Store``),
with the fastembed backend and ``embedding.DEFAULT_MODEL``. The store indexes
the eval memories and embeds them. Then each score is measured on the code
path that compares it with its threshold:

- prompt recall floor (``hooks/recall_hook.py`` ``DEFAULT_MIN_SCORE``): the
  hook's own ``recall()`` over HTTP, with ``NOBLIVION_RECALL_MIN_SCORE`` set
  to each grid value and the hook's prompt ``k``. The score is the store's
  ``scores`` entry: the cosine of the query and the memory, times the row
  weight (1.0 by default), at 4 places (``ranking.rank_pool``). It is not the
  fused rank score. This is the path of a hook with NO config file.
- ranked index floor (``hooks/recall_hook.py`` ``DEFAULT_INDEX_MIN_SCORE``):
  the hook's own ``run()`` for a ``UserPromptSubmit`` event, with the shipped
  config file ``config/config.default.json`` (the ranked index with its
  re-rank, rule rows and the other shipped steps) and
  ``NOBLIVION_RECALL_INDEX_MIN_SCORE`` set to each grid value. Each prompt is
  the first prompt of its own session. The rows counted are the rows in the
  hook output. The score is the ``score`` field of ``/api/memories/index``,
  the same weighted cosine. The path is measured two more times: with the
  floor off, and with the variable unset, which is what a user with the
  shipped config file gets. The label rows (``NOBLIVION_RECALL_LABELS``) are
  not part of this path and are not measured.
- subagent rules floor (``hooks/subagent_rules_hook.py``
  ``DEFAULT_INDEX_MIN_SCORE``): the subagent hook's own ``run()`` for a
  ``SubagentStart`` event that carries the query as its task text, with the
  shipped config file, the hook's own index defaults (``INDEX_DEFAULTS``,
  8 rows) and ``NOBLIVION_RECALL_INDEX_MIN_SCORE`` set to each grid value.
  Each query is the first subagent of its own session. The path is measured
  once more with the variable unset. The eval queries are prompts: a real
  task text is longer (NOBLIVION-76).
- error recall store floor (``hooks/error_recall_hook.py``
  ``STORE_MIN_SCORE``): the hook's own ``search()`` in ``store`` mode, with
  ``NOBLIVION_ERROR_RECALL_MIN_SCORE`` set to each grid value. The score is
  the ``score`` field of ``/api/memories/index``, the same weighted cosine.
  The hook's quote check comes after this gate and is not measured here.
- error recall local floor (``hooks/error_recall_hook.py``
  ``LOCAL_MIN_SCORE``): the hook's own ``search()`` in ``local`` mode, the
  default mode, with ``NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE`` set to each
  grid value. The score is the share of the query terms that the memory holds,
  weighted by idf (``LocalIndex.search``). It needs no store and no model. The
  quote check is not measured here either.
- dedup cosine (``noblivion.dedup.DEFAULT_MIN_COSINE``): ``dedup.select_pairs``
  over ``dedup.load_vectors``, the plain cosine of two stored memory vectors.

For each grid value the script counts true and false hits and prints
precision, recall and F1. ``--check`` fails (exit 1) when precision or recall
at the shipped threshold is below the floor in the ``gate`` section of the
eval file. ``--json PATH`` writes every measured number.

Usage:
    uv run python tools/eval_thresholds.py            # print the curves
    uv run python tools/eval_thresholds.py --check    # and gate the shipped values

The model is downloaded on the first run. fastembed keeps it in
``FASTEMBED_CACHE_PATH`` (default: a folder in the system temp dir).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from noblivion import __version__, config, db, dedup, embedding, store

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = HERE / "eval" / "thresholds.json"
SHIPPED_CONFIG = REPO / "config" / "config.default.json"
NAMESPACE = "claude_code"
RECALL_ROOT = "-eval-recall"
DEDUP_ROOT = "-eval-dedup"
READY_TIMEOUT_S = 600.0


def grid(lo: float, hi: float, step: float = 0.01) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 2) for i in range(n + 1)]


RECALL_GRID = grid(0.30, 0.90)
LOCAL_GRID = grid(0.30, 1.00)  # a share of the query terms: 1.0 is a full match
DEDUP_GRID = grid(0.60, 0.98)
INDEX_FLOOR_OFF = "off"  # the value of NOBLIVION_RECALL_INDEX_MIN_SCORE for no floor


# -- metrics ----------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    threshold: float
    tp: int
    fp: int
    fn: int
    negatives_hit: int = 0  # queries with no expected memory that still got a hit

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "negatives_hit": self.negatives_hit,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def score_queries(
    threshold: float,
    queries: Sequence[Mapping[str, object]],
    returned: Callable[[str], Iterable[str]],
) -> Point:
    """Count hits for one threshold. ``returned(query)`` gives the memory ids
    the code path returns at that threshold."""
    tp = fp = fn = negatives_hit = 0
    for item in queries:
        expect = set(item["expect"])  # type: ignore[arg-type]
        got = set(returned(str(item["q"])))
        tp += len(got & expect)
        fp += len(got - expect)
        fn += len(expect - got)
        if not expect and got:
            negatives_hit += 1
    return Point(threshold, tp, fp, fn, negatives_hit)


def score_pairs(
    threshold: float, cosines: Mapping[tuple[str, str], float], duplicates: set[tuple[str, str]]
) -> Point:
    tp = fp = fn = 0
    for pair, cos in cosines.items():
        hit = cos >= threshold
        dup = pair in duplicates
        tp += hit and dup
        fp += hit and not dup
        fn += dup and not hit
    return Point(threshold, tp, fp, fn)


def best(points: Sequence[Point]) -> Point:
    """The point with the highest F1. When several grid values share it, the
    middle one (the lower of two middles), so the pick keeps a margin on both
    sides of the plateau."""
    top = max(round(p.f1, 6) for p in points)
    plateau = [p for p in points if round(p.f1, 6) == top]
    return plateau[(len(plateau) - 1) // 2]


def at(points: Sequence[Point], threshold: float) -> Point:
    for p in points:
        if abs(p.threshold - threshold) < 1e-9:
            return p
    raise KeyError(f"threshold {threshold} is not on the grid")


# -- setup ------------------------------------------------------------------------


def load_hook(name: str) -> ModuleType:
    mod_name = f"noblivion_eval_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, REPO / "hooks" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def memory_file_name(mem_id: str) -> str:
    return f"feedback_{mem_id}.md"


def write_memories(folder: Path, memories: Iterable[Mapping[str, str]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for m in memories:
        text = (
            f"---\nname: {m['name']}\ndescription: {m['description']}\n"
            f"type: feedback\n---\n{m['body']}\n"
        )
        (folder / memory_file_name(m["id"])).write_text(text, encoding="utf-8")


def mem_id(file_name: str) -> str:
    stem = Path(file_name).name
    stem = stem[: -len(".md")] if stem.endswith(".md") else stem
    return stem[len("feedback_") :] if stem.startswith("feedback_") else stem


class EvalStore:
    """A real store, serving in a thread of this process, over the eval folders."""

    def __init__(self, tmp: Path, data: Mapping[str, object]) -> None:
        self.tmp = tmp
        self.folders = {
            root: tmp / "projects" / root / "memory" for root in (RECALL_ROOT, DEDUP_ROOT)
        }
        write_memories(self.folders[RECALL_ROOT], data["recall"]["memories"])  # type: ignore[index]
        write_memories(self.folders[DEDUP_ROOT], data["dedup"]["memories"])  # type: ignore[index]
        self.n_memories = sum(len(list(f.glob("*.md"))) for f in self.folders.values())
        data_dir = tmp / "data"
        stamp = config.install_stamp(data_dir)
        stamp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"version": __version__}), encoding="utf-8")
        self.settings = config.Settings(
            data_dir=data_dir,
            namespace=NAMESPACE,
            memory_dirs=tuple(self.folders.values()),
            delete_grace_days=14,
            archive_retention_days=90,
        )
        cache = os.environ.get("FASTEMBED_CACHE_PATH", "").strip()
        self.embedding_settings = embedding.EmbeddingSettings(
            backend="fastembed",
            model=embedding.DEFAULT_MODEL,
            allow_download=True,
            models_dir=Path(cache) if cache else None,
        )
        self.store = store.Store(
            self.settings,
            config.StoreSettings(port=0, idle_exit_s=0, index_interval_s=0),
            embedding_service=embedding.EmbeddingService(self.embedding_settings),
            labeller=None,
        )
        self.thread: threading.Thread | None = None

    def __enter__(self) -> EvalStore:
        self.store.open()
        self.store.start_background()
        self.thread = threading.Thread(target=self.store.serve, daemon=True)
        self.thread.start()
        self._wait_ready()
        return self

    def __exit__(self, *exc: object) -> None:
        self.store.request_stop("eval")
        if self.thread is not None:
            self.thread.join(15)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path, create=False)

    def memory_ids(self, root: str) -> dict[int, str]:
        """Store id -> eval id of the memories of one root."""
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT id, path FROM memories WHERE root = ?", (root,)).fetchall()
        return {int(r[0]): mem_id(str(r[1])) for r in rows}

    def _vectors(self) -> int:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM vectors WHERE model = ?", (self.embedding_settings.model_id,)
            ).fetchone()
        return int(row[0])

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            emb = self.store.embedding
            if emb.state == embedding.STATE_FAILED:
                raise SystemExit(f"model load failed: {emb.error}")
            if (
                self.store.index_state == store.INDEX_IDLE
                and emb.state == embedding.STATE_READY
                and self._vectors() >= self.n_memories
            ):
                return
            time.sleep(0.1)
        raise SystemExit("the store did not index and embed the eval set in time")

    def hook_env(self, root: str, **extra: str) -> dict[str, str]:
        env = {
            "NOBLIVION_DATA_DIR": str(self.settings.data_dir),
            "NOBLIVION_RECALL_CACHE_DIR": str(self.tmp / "cache"),
            "NOBLIVION_CONFIG": str(self.tmp / "no-config.json"),
            "NOBLIVION_STORE_AUTOSTART": "0",
            "NOBLIVION_RECALL_MEMORY_DIR": str(self.folders[root]),
            "NOBLIVION_RECALL_TIMEOUT_S": "30",
            "NOBLIVION_ERROR_RECALL_TIMEOUT_S": "30",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        env.update(extra)
        return env


# -- the measurements -------------------------------------------------------------


def measure_recall(es: EvalStore, rh: ModuleType, queries: Sequence[Mapping]) -> list[Point]:
    """The prompt hook path: ``recall(query, K_PROMPT, md_only=True)``."""

    def at_threshold(t: float) -> Point:
        env = es.hook_env(RECALL_ROOT, NOBLIVION_RECALL_MIN_SCORE=str(t))
        return score_queries(
            t, queries, lambda q: [mem_id(h.mid) for h in rh.recall(q, rh.K_PROMPT, env, True)]
        )

    return [at_threshold(t) for t in RECALL_GRID]


_INDEX_ROW_RE = re.compile(r"^- \[id (\d+)\] ", re.MULTILINE)  # a rule row of the index


def index_status(cache: Path, sid: str) -> str:
    """The status field of the hook's log line for one session."""
    try:
        lines = (cache / "recall.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        fields = line.split()
        if f"session={sid}" in fields:
            return fields[-1]
    return ""


def measure_index(
    es: EvalStore, rh: ModuleType, queries: Sequence[Mapping]
) -> tuple[list[Point], Point, Point]:
    """The prompt hook path of the shipped config file: ``run()`` serves the
    ranked index. Returns the curve, the point with the floor off, and the
    point with the variable unset (the floor of the shipped defaults).

    The hook fails open, so a failed call prints nothing and would count as "no
    rows". The log line of each call must say ``ok``, and no shipped step may
    be off (a keyword-mode answer skips the floor, a failed local read skips
    the re-rank)."""
    ids = es.memory_ids(RECALL_ROOT)

    def at_floor(t: float, value: str | None, tag: str) -> Point:
        cache = es.tmp / f"cache-index-{tag}"
        extra = {"NOBLIVION_CONFIG": str(SHIPPED_CONFIG), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
        if value is not None:
            extra["NOBLIVION_RECALL_INDEX_MIN_SCORE"] = value
        env = es.hook_env(RECALL_ROOT, **extra)
        calls = iter(range(len(queries)))

        def returned(q: str) -> list[str]:
            # One session per prompt: the shipped row dedupe leaves out the
            # rows that an earlier prompt of the same session showed.
            sid = f"eval-{next(calls)}"
            event = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": q}
            out = io.StringIO()
            rh.run(json.dumps(event), out, env)
            status = index_status(cache, sid)
            if not status.startswith("ok") or "_off:" in status:
                raise SystemExit(f"the index path did not run in full: {status or 'no log line'}")
            return [ids[int(n)] for n in _INDEX_ROW_RE.findall(out.getvalue())]

        point = score_queries(t, queries, returned)
        shutil.rmtree(cache, ignore_errors=True)
        return point

    curve = [at_floor(t, str(t), f"{t:.2f}") for t in RECALL_GRID]
    floor = float(rh.DEFAULT_INDEX_MIN_SCORE)
    return curve, at_floor(-1.0, INDEX_FLOOR_OFF, "off"), at_floor(floor, None, "unset")


def measure_subagent(
    es: EvalStore, srh: ModuleType, queries: Sequence[Mapping]
) -> tuple[list[Point], Point]:
    """The subagent rules path: ``run()`` of the subagent hook for a
    ``SubagentStart`` event with the task text in it. Returns the curve and the
    point with the floor variable unset (the floor of the shipped defaults).
    Like ``measure_index``, every call must log ``ok`` with no step off."""
    ids = es.memory_ids(RECALL_ROOT)
    agent = "a0000000000000001"

    def at_floor(t: float, value: str | None, tag: str) -> Point:
        cache = es.tmp / f"cache-subagent-{tag}"
        extra = {"NOBLIVION_CONFIG": str(SHIPPED_CONFIG), "NOBLIVION_RECALL_CACHE_DIR": str(cache)}
        if value is not None:
            extra["NOBLIVION_RECALL_INDEX_MIN_SCORE"] = value
        env = es.hook_env(RECALL_ROOT, **extra)
        calls = iter(range(len(queries)))

        def returned(q: str) -> list[str]:
            sid = f"eval-{next(calls)}"
            event = {
                "hook_event_name": "SubagentStart",
                "session_id": sid,
                "agent_id": agent,
                "agent_type": "general-purpose",
                "prompt": q,
            }
            out = io.StringIO()
            srh.run(json.dumps(event), out, env)
            status = index_status(cache / srh.SUBDIR, f"{sid}.{agent}")
            if not status.startswith("ok") or "_off:" in status:
                raise SystemExit(
                    f"the subagent path did not run in full: {status or 'no log line'}"
                )
            text = out.getvalue()
            context = json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""
            return [ids[int(n)] for n in _INDEX_ROW_RE.findall(context)]

        point = score_queries(t, queries, returned)
        shutil.rmtree(cache, ignore_errors=True)
        return point

    curve = [at_floor(t, str(t), f"{t:.2f}") for t in RECALL_GRID]
    floor = float(srh.DEFAULT_INDEX_MIN_SCORE)
    return curve, at_floor(floor, None, "unset")


def measure_error_recall(es: EvalStore, erh: ModuleType, queries: Sequence[Mapping]) -> list[Point]:
    """The error hook path: ``search()`` in store mode, every row that passes the gate."""
    folder = es.folders[RECALL_ROOT]

    def at_threshold(t: float) -> Point:
        env = es.hook_env(
            RECALL_ROOT,
            NOBLIVION_ERROR_RECALL_MODE="store",
            NOBLIVION_ERROR_RECALL_MIN_SCORE=str(t),
        )

        def returned(q: str) -> list[str]:
            source, hits, error = erh.search(q, env, folder)
            if source != "store":
                raise SystemExit(f"error recall fell back to {source}: {error}")
            return [mem_id(str(m["id"])) for _, m in hits]

        return score_queries(t, queries, returned)

    return [at_threshold(t) for t in RECALL_GRID]


def measure_error_local(folder: Path, erh: ModuleType, queries: Sequence[Mapping]) -> list[Point]:
    """The error hook path in its default mode: ``search()`` in local mode over
    the memory files of ``folder``, every row that passes the gate. It uses no
    store and no model."""

    def at_threshold(t: float) -> Point:
        env = {"NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE": str(t)}

        def returned(q: str) -> list[str]:
            source, hits, _error = erh.search(q, env, folder)
            if source != "local":
                raise SystemExit(f"error recall ran in {source} mode, not in its default mode")
            return [mem_id(str(m["id"])) for _, m in hits]

        return score_queries(t, queries, returned)

    return [at_threshold(t) for t in LOCAL_GRID]


def dedup_cosines(es: EvalStore) -> dict[tuple[str, str], float]:
    """Every pair of the dedup folder with its cosine, from the dedup code."""
    with closing(es.connect()) as conn:
        rows = dedup.load_vectors(conn, NAMESPACE, es.embedding_settings.model_id)
    rows = [r for r in rows if r[1] == DEDUP_ROOT]
    pairs = dedup.select_pairs(rows, min_cosine=-1.0)
    out = {tuple(sorted((mem_id(c.path_a), mem_id(c.path_b)))): c.cosine for c in pairs}
    n = len(rows)
    if len(out) != n * (n - 1) // 2:
        raise SystemExit(f"dedup gave {len(out)} pairs for {n} memories")
    return out  # type: ignore[return-value]


def measure_dedup(cosines: Mapping, duplicates: Iterable[Sequence[str]]) -> list[Point]:
    dups = {tuple(sorted(p)) for p in duplicates}
    missing = dups - set(cosines)
    if missing:
        raise SystemExit(f"duplicate pairs not in the dedup folder: {sorted(missing)}")
    return [score_pairs(t, cosines, dups) for t in DEDUP_GRID]  # type: ignore[arg-type]


# -- report -----------------------------------------------------------------------


def shipped(rh: ModuleType, erh: ModuleType, srh: ModuleType) -> dict[str, float]:
    return {
        "recall": float(rh.DEFAULT_MIN_SCORE),
        "index": float(rh.DEFAULT_INDEX_MIN_SCORE),
        "subagent": float(srh.DEFAULT_INDEX_MIN_SCORE),
        "error_recall": float(erh.STORE_MIN_SCORE),
        "error_local": float(erh.LOCAL_MIN_SCORE),
        "dedup": float(dedup.DEFAULT_MIN_COSINE),
    }


def point_row(label: str, p: Point, mark: str = "") -> str:
    return (
        f"  {label:>9}  {p.precision:9.3f}  {p.recall:6.3f}  {p.f1:5.3f}"
        f"  {p.tp:3d} {p.fp:4d} {p.fn:3d}  {p.negatives_hit:3d}{mark}"
    )


def print_curve(title: str, points: Sequence[Point], current: float, step: int = 5) -> None:
    pick = best(points)
    print(f"\n{title}")
    print("  threshold  precision  recall   f1     tp   fp  fn  neg_hit")
    for i, p in enumerate(points):
        mark = ""
        if p is pick:
            mark += "  best F1"
        if abs(p.threshold - current) < 1e-9:
            mark += "  shipped"
        if i % step and not mark:
            continue
        print(point_row(f"{p.threshold:.2f}", p, mark))


def check(
    results: Mapping[str, list[Point]], current: Mapping[str, float], gate: Mapping
) -> list[str]:
    failures = []
    for name, points in results.items():
        p = at(points, current[name])
        floor = gate[name]
        for metric in ("precision", "recall"):
            value = getattr(p, metric)
            if value < float(floor[metric]):
                failures.append(
                    f"{name}: {metric} {value:.3f} at {current[name]:.2f}"
                    f" is below the floor {floor[metric]}"
                )
    return failures


def check_shipped_config(
    points: Sequence[Point], current: float, unset: Point, name: str = "index"
) -> list[str]:
    """The index path with ``NOBLIVION_RECALL_INDEX_MIN_SCORE`` unset must give
    the counts of the shipped floor: a user with the shipped config file, and
    no setting of their own, then gets the floor that was measured. The same
    holds for the subagent path (``name`` ``subagent``)."""
    p = at(points, current)
    want = (p.tp, p.fp, p.fn, p.negatives_hit)
    got = (unset.tp, unset.fp, unset.fn, unset.negatives_hit)
    if got == want:
        return []
    return [
        f"{name}: the shipped config with the floor variable unset gives tp, fp, fn, neg_hit"
        f" {got}, not {want} of the shipped floor {current:.2f}"
    ]


def run(data: Mapping) -> tuple[dict[str, list[Point]], dict[str, float], dict, dict[str, Point]]:
    """Every curve, the shipped values, extra numbers for ``--json``, and the
    two more points of the index path (``off`` and ``unset``) and the one of
    the subagent path (``subagent_unset``)."""
    rh = load_hook("recall_hook")
    erh = load_hook("error_recall_hook")
    srh = load_hook("subagent_rules_hook")
    with (
        tempfile.TemporaryDirectory(prefix="noblivion-eval-") as tmp,
        EvalStore(Path(tmp), data) as es,
    ):
        recall_points = measure_recall(es, rh, data["recall"]["queries"])
        index_points, index_off, index_unset = measure_index(es, rh, data["recall"]["queries"])
        subagent_points, subagent_unset = measure_subagent(es, srh, data["recall"]["queries"])
        error_points = measure_error_recall(es, erh, data["recall"]["error_queries"])
        local_points = measure_error_local(
            es.folders[RECALL_ROOT], erh, data["recall"]["error_queries"]
        )
        cosines = dedup_cosines(es)
        dedup_points = measure_dedup(cosines, data["dedup"]["duplicates"])
    extra = {
        "dedup_pairs": {
            f"{a} {b}": round(c, 4) for (a, b), c in sorted(cosines.items(), key=lambda kv: -kv[1])
        },
    }
    results = {
        "recall": recall_points,
        "index": index_points,
        "subagent": subagent_points,
        "error_recall": error_points,
        "error_local": local_points,
        "dedup": dedup_points,
    }
    paths = {"off": index_off, "unset": index_unset, "subagent_unset": subagent_unset}
    return results, shipped(rh, erh, srh), extra, paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="fail when a shipped value misses its floor"
    )
    parser.add_argument("--json", type=Path, help="write every measured number to this file")
    parser.add_argument("--data", type=Path, default=DATA, help="the eval set")
    args = parser.parse_args(argv)
    data = json.loads(args.data.read_text(encoding="utf-8"))
    t0 = time.monotonic()
    results, current, extra, index_paths = run(data)
    print(
        f"model {embedding.DEFAULT_MODEL}, eval set {args.data.name}, {time.monotonic() - t0:.0f} s"
    )
    titles = {
        "recall": "prompt recall floor, note text, no config file (recall_hook DEFAULT_MIN_SCORE),"
        " store 'scores' value",
        "index": "ranked index floor, shipped config file (recall_hook DEFAULT_INDEX_MIN_SCORE),"
        " index 'score'",
        "subagent": "subagent rules floor, shipped config file"
        " (subagent_rules_hook DEFAULT_INDEX_MIN_SCORE), index 'score'",
        "error_recall": "error recall store floor, store mode (error_recall_hook STORE_MIN_SCORE),"
        " index 'score'",
        "error_local": "error recall local floor, local mode, the default"
        " (error_recall_hook LOCAL_MIN_SCORE), share of the query terms",
        "dedup": "dedup cosine (dedup.DEFAULT_MIN_COSINE), pair cosine",
    }
    for name, points in results.items():
        print_curve(titles[name], points, current[name])
        if name == "index":
            print(point_row("off", index_paths["off"], "  no floor"))
            print(point_row("unset", index_paths["unset"], "  the shipped config file as it is"))
        if name == "subagent":
            print(point_row("unset", index_paths["subagent_unset"], "  the shipped defaults"))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "model": embedding.DEFAULT_MODEL,
                    "shipped": current,
                    "best": {k: best(v).as_dict() for k, v in results.items()},
                    "curves": {k: [p.as_dict() for p in v] for k, v in results.items()},
                    "index_paths": {
                        "off": {**index_paths["off"].as_dict(), "threshold": None},
                        "unset": index_paths["unset"].as_dict(),
                    },
                    "subagent_unset": index_paths["subagent_unset"].as_dict(),
                    **extra,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    if args.check:
        failures = check(results, current, data["gate"])
        failures += check_shipped_config(results["index"], current["index"], index_paths["unset"])
        failures += check_shipped_config(
            results["subagent"], current["subagent"], index_paths["subagent_unset"], "subagent"
        )
        for line in failures:
            print(f"FAIL {line}")
        if failures:
            return 1
        print("\ngate: every shipped threshold meets its precision and recall floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
