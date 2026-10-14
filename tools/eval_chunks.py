# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the section-level chunks on a held-out query set (NOBLIVION-103).

The eval set is ``tools/eval/chunks.json``: made-up long memory files that each
cover several topics, and queries whose answer sits past the first 2 KB of its
file. The first 2 KB is what the default embedding model reads (512 tokens), so
a file-level vector cannot hold the answer. The queries were written once, before
any measurement, and no chunk parameter was tuned on them.

The script starts a real store in this process (``noblivion.store.Store``) with
the fastembed backend, indexes and embeds the files, and then measures:

- file hit at 1 and at 5: the right file is the first, or in the first five,
  of the ranked answer (``Store.rank``, the call behind ``/api/memories/index``
  and ``/api/memories/search``);
- the chunk in the top result: the top file is right and its best chunk holds
  the answer phrase (needs the chunk fields, so it is empty on a store from
  before NOBLIVION-103);
- the answer in one fetch: the right file is the first and the text that one
  ``fetch`` call gives (``rest.fetch_answer``, cut as the hook cuts it) holds
  the answer phrase. With chunks the call names the query as ``query``;
- the characters that the prompt hook injects per query, with the shipped
  config file (the ranked index) and with no config file (the search entries),
  as the median and the mean;
- the index time: from the start of the store to the last vector.

The script uses only calls that also exist before NOBLIVION-103, so the same
file measures the baseline when it runs with the ``src`` folder of an older
release on ``PYTHONPATH``.

``--corpus DIR`` adds a second run over memory folders that you copied to
``DIR`` (``DIR/<root>/memory/*.md``, the layout of ``~/.claude/projects``).
Copy them first: the run writes a database and nothing else, but it must not
touch live data. It reports the index time of the corpus and the same hit
measures on queries made from the corpus: for up to ``--corpus-queries`` long
files the query is the first 12 distinctive words of a sentence in the second
half of the file. Those queries are lexical, so they are an easier test than
the held-out set. The script prints counts and times, never memory text.

Usage:
    PYTHONPATH=src python tools/eval_chunks.py [--json PATH] [--corpus DIR]

The model is downloaded on the first run (``FASTEMBED_CACHE_PATH``).
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
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from types import ModuleType

from noblivion import bm25, config, db, embedding, rest, store

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = HERE / "eval" / "chunks.json"
SHIPPED_CONFIG = REPO / "config" / "config.default.json"
NAMESPACE = "claude_code"
ROOT = "-eval-chunks"
READY_TIMEOUT_S = 1800.0
FETCH_CHARS = 6000  # what the hook shows of a fetched memory (FETCH_MAX_CHARS)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def load_hook(name: str) -> ModuleType:
    mod_name = f"noblivion_eval_chunks_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, REPO / "hooks" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class EvalStore:
    """A real store, serving in a thread of this process, over memory folders."""

    def __init__(self, tmp: Path, folders: Sequence[Path]) -> None:
        self.tmp = tmp
        self.folders = list(folders)
        self.n_files = sum(len(list(f.glob("*.md"))) for f in self.folders)
        data_dir = tmp / "data"
        stamp = config.install_stamp(data_dir)
        stamp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"version": "eval"}), encoding="utf-8")
        self.settings = config.Settings(
            data_dir=data_dir,
            namespace=NAMESPACE,
            memory_dirs=tuple(self.folders),
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
        self.index_seconds = 0.0

    def __enter__(self) -> EvalStore:
        started = time.monotonic()
        self.store.open()
        self.store.start_background()
        self.thread = threading.Thread(target=self.store.serve, daemon=True)
        self.thread.start()
        self._wait_ready()
        self.index_seconds = time.monotonic() - started
        return self

    def __exit__(self, *exc: object) -> None:
        self.store.request_stop("eval")
        if self.thread is not None:
            self.thread.join(15)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path, create=False)

    def counts(self) -> dict[str, int]:
        """Live rows, file vectors, chunks and chunk vectors. No chunks before schema 5."""
        model = self.embedding_settings.model_id
        out = {"rows": 0, "vectors": 0, "chunks": 0, "chunk_vectors": 0}
        with closing(self.connect()) as conn:
            out["rows"] = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE archived_at IS NULL AND deleted_at IS NULL"
                " AND source_type = 'claude_code_md'"
            ).fetchone()[0]
            out["vectors"] = conn.execute(
                "SELECT COUNT(*) FROM vectors v JOIN memories m ON m.id = v.memory_id "
                "WHERE v.model = ? AND m.source_type = 'claude_code_md'",
                (model,),
            ).fetchone()[0]
            try:
                out["chunks"] = conn.execute(
                    "SELECT COUNT(*) FROM memory_chunks c JOIN memories m ON m.id = c.parent_id "
                    "WHERE m.archived_at IS NULL AND m.deleted_at IS NULL "
                    "AND m.source_type = 'claude_code_md'"
                ).fetchone()[0]
                out["chunk_vectors"] = conn.execute(
                    "SELECT COUNT(*) FROM chunk_vectors v "
                    "JOIN memory_chunks c ON c.id = v.chunk_id "
                    "JOIN memories m ON m.id = c.parent_id WHERE v.model = ? "
                    "AND m.source_type = 'claude_code_md'",
                    (model,),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass  # a store from before the chunks
        return out

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            emb = self.store.embedding
            if emb.state == embedding.STATE_FAILED:
                raise SystemExit(f"model load failed: {emb.error}")
            n = self.counts()
            if (
                self.store.index_state == store.INDEX_IDLE
                and emb.state == embedding.STATE_READY
                and n["rows"] >= self.n_files
                and n["vectors"] >= n["rows"]
                and n["chunk_vectors"] >= n["chunks"]
            ):
                return
            time.sleep(0.05)
        raise SystemExit("the store did not index and embed the eval set in time")

    def hook_env(self, folder: Path, **extra: str) -> dict[str, str]:
        env = {
            "NOBLIVION_DATA_DIR": str(self.settings.data_dir),
            "NOBLIVION_RECALL_CACHE_DIR": str(self.tmp / "cache"),
            "NOBLIVION_CONFIG": str(self.tmp / "no-config.json"),
            "NOBLIVION_STORE_AUTOSTART": "0",
            "NOBLIVION_RECALL_MEMORY_DIR": str(folder),
            "NOBLIVION_RECALL_TIMEOUT_S": "30",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        env.update(extra)
        return env


def write_fixture(folder: Path, files: Sequence[Mapping[str, str]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        text = (
            f"---\nname: {f['name']}\ndescription: {f['description']}\n"
            f"type: reference\n---\n{f['body']}\n"
        )
        (folder / f"reference_{f['id']}.md").write_text(text, encoding="utf-8")


def ranked(es: EvalStore, query: str, top_k: int = 5) -> list:
    result = es.store.rank(query, NAMESPACE, top_k=top_k)
    if result is None:
        raise SystemExit("the store could not rank the query")
    return list(result.hits)


def fetch_text(es: EvalStore, memory_id: int, query: str, chunked: bool) -> str:
    """What one fetch call gives for the top file, cut as the hook cuts it."""
    with closing(es.connect()) as conn:
        if chunked:
            # As the fetch route does: the query vector, when the store has a model.
            ranker = es.store.ranker
            answer = rest.fetch_answer(
                conn,
                str(memory_id),
                NAMESPACE,
                query=query,
                query_vector=ranker.query_vector(conn, query),
                model_id=ranker.embedding.model_id if ranker.embedding else None,
            )
        else:
            answer = rest.fetch_answer(conn, str(memory_id), NAMESPACE)
    return str(answer.get("text") or "")[:FETCH_CHARS]


def measure_hits(
    es: EvalStore,
    queries: Sequence[Mapping[str, str]],
    prefix: str,
    negatives: Sequence[Mapping[str, str]] = (),
    floor: float = 0.68,
) -> dict:
    """``floor`` is the score floor of the prompt hook (``DEFAULT_MIN_SCORE``). A
    positive query is "kept" when its right file is the first and has a score at
    or above the floor. A negative query (no right file) is a "false hit" when its
    first file has a score at or above the floor."""
    chunked = es.counts()["chunks"] > 0
    at1 = at5 = chunk_top = fetched = kept = 0
    for q in queries:
        want = f"{prefix}{q['file']}.md"
        hits = ranked(es, q["q"], 5)
        paths = [h.row.path for h in hits]
        at1 += bool(paths) and paths[0] == want
        at5 += want in paths
        if hits and paths[0] == want:
            kept += (hits[0].score or 0.0) >= floor
            if chunked and q["phrase"] in getattr(hits[0].row, "chunk_text", ""):
                chunk_top += 1
            if q["phrase"] in fetch_text(es, hits[0].row.id, q["q"], chunked):
                fetched += 1
    false_hits = 0
    for q in negatives:
        hits = ranked(es, q["q"], 1)
        false_hits += bool(hits) and (hits[0].score or 0.0) >= floor
    n = len(queries)
    return {
        "floor": floor,
        "positives_kept_at_floor": kept,
        "negatives": len(negatives),
        "negatives_hit_at_floor": false_hits,
        "queries": n,
        "file_hit_at_1": at1,
        "file_hit_at_5": at5,
        "chunk_in_top_result": chunk_top if chunked else None,
        "answer_in_one_fetch": fetched,
        "rates": {
            "file_hit_at_1": round(at1 / n, 4),
            "file_hit_at_5": round(at5 / n, 4),
            "answer_in_one_fetch": round(fetched / n, 4),
        },
    }


def injected_chars(
    es: EvalStore, rh: ModuleType, queries: Sequence[Mapping[str, str]], folder: Path | None = None
) -> dict:
    """The characters the prompt hook prints per query: the shipped config (ranked
    index) and no config (search entries). One session per prompt."""
    folder = folder or es.folders[0]
    out: dict[str, dict] = {}
    for tag, cfg in (("shipped_config", str(SHIPPED_CONFIG)), ("no_config", None)):
        sizes: list[int] = []
        cache = es.tmp / f"cache-{tag}"
        for n, q in enumerate(queries):
            extra = {"NOBLIVION_RECALL_CACHE_DIR": str(cache)}
            if cfg:
                extra["NOBLIVION_CONFIG"] = cfg
            env = es.hook_env(folder, **extra)
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": f"eval-{tag}-{n}",
                "prompt": q["q"],
            }
            buf = io.StringIO()
            rh.run(json.dumps(event), buf, env)
            sizes.append(len(buf.getvalue()))
        shutil.rmtree(cache, ignore_errors=True)
        out[tag] = {
            "median": statistics.median(sizes),
            "mean": round(statistics.fmean(sizes), 1),
            "max": max(sizes),
        }
    return out


def corpus_queries(es: EvalStore, limit: int) -> list[dict[str, str]]:
    """Lexical queries from long files: 12 distinctive words of a sentence in the
    second half of the file, and a phrase of that sentence as the answer."""
    queries: list[dict[str, str]] = []
    with closing(es.connect()) as conn:
        rows = conn.execute(
            "SELECT path, content FROM memories WHERE source_type = 'claude_code_md' "
            "AND archived_at IS NULL AND deleted_at IS NULL AND length(content) > 4500 "
            "ORDER BY path"
        ).fetchall()
    for path, content in rows:
        half = content[len(content) // 2 :]
        for sentence in SENTENCE_RE.split(half)[1:]:
            words = [w for w in bm25.tokenize(sentence) if w not in bm25.STOP_WORDS and len(w) > 3]
            if 8 <= len(words) <= 40 and 60 <= len(sentence) <= 300:
                queries.append(
                    {"q": " ".join(words[:12]), "file": path[:-3], "phrase": sentence.strip()}
                )
                break
        if len(queries) >= limit:
            break
    return queries


def run_fixture(data: Mapping, tmp: Path, rh: ModuleType) -> dict:
    folder = tmp / "projects" / ROOT / "memory"
    write_fixture(folder, data["files"])
    with EvalStore(tmp / "fixture", [folder]) as es:
        result = {
            "files": len(data["files"]),
            "index_seconds": round(es.index_seconds, 1),
            "counts": es.counts(),
            "hits": measure_hits(
                es,
                data["queries"],
                "reference_",
                data.get("negatives", ()),
                float(rh.DEFAULT_MIN_SCORE),
            ),
            "injected": injected_chars(es, rh, [*data["queries"], *data.get("negatives", ())]),
        }
    return result


def run_corpus(corpus: Path, tmp: Path, rh: ModuleType, limit: int) -> dict:
    folders = sorted(p for p in corpus.glob("*/memory") if p.is_dir())
    if not folders:
        raise SystemExit(f"no <root>/memory folder in {corpus}")
    with EvalStore(tmp / "corpus", folders) as es:
        queries = corpus_queries(es, limit)
        hits = measure_corpus_hits(es, queries)
        biggest = max(folders, key=lambda f: len(list(f.glob("*.md"))))
        result = {
            "injected": injected_chars(es, rh, queries[:60], biggest),
            "folders": len(folders),
            "files": es.n_files,
            "index_seconds": round(es.index_seconds, 1),
            "counts": es.counts(),
            "self_queries": len(queries),
            "hits": hits,
        }
    return result


def measure_corpus_hits(es: EvalStore, queries: Sequence[Mapping[str, str]]) -> dict:
    chunked = es.counts()["chunks"] > 0
    at1 = at5 = chunk_top = 0
    for q in queries:
        hits = ranked(es, q["q"], 5)
        paths = [Path(h.row.path).stem for h in hits]
        at1 += bool(paths) and paths[0] == q["file"]
        at5 += q["file"] in paths
        if chunked and hits and paths[0] == q["file"]:
            chunk_top += q["phrase"][:40] in getattr(hits[0].row, "chunk_text", "")
    n = max(1, len(queries))
    return {
        "file_hit_at_1": at1,
        "file_hit_at_5": at5,
        "chunk_in_top_result": chunk_top if chunked else None,
        "rates": {"file_hit_at_1": round(at1 / n, 4), "file_hit_at_5": round(at5 / n, 4)},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA, help="the eval set")
    parser.add_argument("--json", type=Path, help="write every measured number to this file")
    parser.add_argument("--corpus", type=Path, help="a COPY of memory folders: DIR/<root>/memory")
    parser.add_argument("--corpus-queries", type=int, default=100)
    args = parser.parse_args(argv)
    data = json.loads(args.data.read_text(encoding="utf-8"))
    rh = load_hook("recall_hook")
    out: dict[str, object] = {"model": embedding.DEFAULT_MODEL}
    with tempfile.TemporaryDirectory(prefix="noblivion-eval-chunks-") as raw:
        tmp = Path(raw)
        out["fixture"] = run_fixture(data, tmp, rh)
        if args.corpus:
            out["corpus"] = run_corpus(args.corpus, tmp, rh, args.corpus_queries)
    text = json.dumps(out, indent=1)
    print(text)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
