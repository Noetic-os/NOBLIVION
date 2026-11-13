#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay the exact-wording prompts of a scale fixture through the prompt hook
when the store does not answer (NOBLIVION-224).

The fixture is the folder that ``tools/eval/scale_fixture.py`` writes:
``lessons.json``, ``queries.jsonl`` and ``scale-NNNNN/shard-NN/projects/*/memory``.
Each V1 query (the exact wording of its lesson) is sent as a ``UserPromptSubmit``
to ``hooks/recall_hook.py``, in this process, with the memory folder of the
query's session project. A prompt is delivered when the answer string of its
lesson is on a row (a line that starts with ``- ``) of the text the model sees.

Modes:

  stopped   ``store.json`` is missing and the hook may not start a store.
  hung      ``store.json`` names a listener that accepts and never answers, and
            a ``store.hung`` stamp for it is in force.
  compare   a local fake store answers every index request with the same rows,
            and the hook of ``--old-hooks`` and the hook of ``--hooks`` must
            print the same bytes for every prompt.

Per pass it gives the time of the whole hook call (``p50_ms``, ``p95_ms``: the
recall leg and the label rows leg) and of the recall leg alone, from the hook's
log line (``recall_leg_p95_ms``). Prints one JSON object. No model call and no
network: every socket is on 127.0.0.1. Standard library only.

    python3 tools/replay_store_silent.py --fixture DIR --size 20000 --mode stopped
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import io
import json
import os
import secrets
import socket
import statistics
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WORK = "/work"


def load_hook(hooks: Path, alias: str) -> Any:
    spec = importlib.util.spec_from_file_location(alias, hooks / "recall_hook.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"no recall hook in {hooks}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def model_text(out: str) -> str:
    """The part of the prompt hook's stdout that the model sees."""
    text = out.strip()
    if text.startswith("{"):
        try:
            doc = json.loads(text.splitlines()[0])
        except ValueError:
            return ""
        return str((doc.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    return text


def delivered(text: str, answer: str) -> bool:
    return any(answer in ln for ln in text.splitlines() if ln.startswith("- "))


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[pos], 1)


class Fixture:
    def __init__(self, root: Path, size: int) -> None:
        self.root = root
        self.size = size
        self.lessons = {
            row["id"]: row
            for row in json.loads((root / "lessons.json").read_text(encoding="utf-8"))
        }
        lines = (root / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        self.queries = [json.loads(ln) for ln in lines if ln.strip()]
        self.scale = json.loads(
            (root / f"scale-{size:05d}" / "scale.json").read_text(encoding="utf-8")
        )

    def exact(self) -> list[dict[str, Any]]:
        return [q for q in self.queries if q.get("variant") == "V1"]

    def project(self, query: dict[str, Any]) -> str:
        return self.scale["lessons"][query["lesson"]]["session_project"]

    def memory(self, query: dict[str, Any]) -> Path:
        shard = int(self.scale["lessons"][query["lesson"]]["shard"])
        name = "-work-" + self.project(query)
        return (
            self.root
            / f"scale-{self.size:05d}"
            / f"shard-{shard:02d}"
            / "projects"
            / name
            / "memory"
        )


def install_config(data: Path) -> None:
    """The shipped default config as the data dir's ``config.json``, as the
    installer writes it."""
    data.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "config" / "config.default.json").read_text(encoding="utf-8")
    (data / "config.json").write_text(text, encoding="utf-8")


def base_env(tmp: Path, data: Path, cache: Path) -> dict[str, str]:
    home = tmp / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_RECALL_CACHE_DIR": str(cache),
        "NOBLIVION_STORE_AUTOSTART": "0",
    }


def write_store(data: Path, port: int, token: str) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "token").write_text(token + "\n", encoding="ascii")
    started = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    doc = {"pid": os.getpid(), "port": port, "version": "0.0.0", "started_at": started}
    (data / "store.json").write_text(json.dumps(doc), encoding="utf-8")


def call(hook: Any, env: dict[str, str], query: dict[str, Any], cwd: str, sid: str):
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": sid,
        "cwd": cwd,
        "prompt": query["prompt"],
    }
    out = io.StringIO()
    t0 = time.perf_counter()
    hook.main(io.StringIO(json.dumps(event)), out, dict(env))
    return out.getvalue(), (time.perf_counter() - t0) * 1000.0


def replay_silent(fx: Fixture, hooks: Path, mode: str, passes: int = 2) -> dict[str, Any]:
    """Each pass sends every exact-wording prompt once, in a new session per
    prompt. The first pass meets an empty corpus cache (every note file is
    read once); the later passes meet the cache that the first one wrote, as a
    prompt of a running install does."""
    hook = load_hook(hooks, "replay_silent_hook")
    out_passes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="nob-silent-") as raw:
        tmp = Path(raw)
        data, cache = tmp / "data", tmp / "cache"
        install_config(data)
        listener: socket.socket | None = None
        if mode == "hung":
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(256)
            write_store(data, listener.getsockname()[1], secrets.token_hex(32))
        env = base_env(tmp, data, cache)
        try:
            for p in range(passes):
                if mode == "hung":
                    # A stamp in force, written by a prompt that timed out just now.
                    denv = {"NOBLIVION_DATA_DIR": str(data)}
                    hook._STORE.mark_hung(denv, 2.0, hook._STORE.store_identity(denv))
                hits, ms = 0, []
                log = cache / "recall.log"
                if log.exists():
                    log.unlink()
                for n, query in enumerate(fx.exact()):
                    project = fx.project(query)
                    run_env = dict(env, NOBLIVION_MEMORY_DIR=str(fx.memory(query)))
                    run_env["CLAUDE_PROJECT_DIR"] = f"{WORK}/{project}"
                    sid = f"replay-{p}-{n}"
                    out, took = call(hook, run_env, query, f"{WORK}/{project}", sid)
                    ms.append(took)
                    answer = fx.lessons[query["lesson"]]["answer"]
                    hits += delivered(model_text(out), answer)
                statuses: dict[str, int] = {}
                leg: list[float] = []  # the recall leg alone, from its log line
                for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
                    key = ":".join(line.rsplit(" ", 1)[-1].split(":")[:3])
                    statuses[key] = statuses.get(key, 0) + 1
                    if " ms=" in line:
                        leg.append(float(line.split(" ms=", 1)[1].split()[0]))
                out_passes.append(
                    {
                        "pass": p + 1,
                        "corpus_cache": "cold" if p == 0 else "warm",
                        "prompts": len(ms),
                        "delivered": hits,
                        "p50_ms": percentile(ms, 0.5),
                        "p95_ms": percentile(ms, 0.95),
                        "max_ms": round(max(ms), 1) if ms else 0.0,
                        "mean_ms": round(statistics.fmean(ms), 1) if ms else 0.0,
                        "recall_leg_p95_ms": percentile(leg, 0.95),
                        "recall_leg_max_ms": max(leg) if leg else 0.0,
                        "statuses": statuses,
                    }
                )
        finally:
            if listener is not None:
                listener.close()
    return {"mode": mode, "size": fx.size, "passes": out_passes}


class _Store:
    """A local fake store: the listener proof, and one fixed index answer per
    prompt text."""

    def __init__(self, data: Path) -> None:
        self.token = secrets.token_hex(32)
        self.answers: dict[str, Any] = {}
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path == "/health":
                    nonce = (query.get("nonce") or [""])[0]
                    proof = hmac.new(
                        store.token.encode(), ("noblivion-health:" + nonce).encode(), hashlib.sha256
                    ).hexdigest()
                    body = {"status": "ok", "service": "noblivion", "proof": proof}
                else:
                    body = store.answers.get((query.get("q") or [""])[0], {"results": []})
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        write_store(data, self.server.server_address[1], self.token)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def compare(fx: Fixture, hooks: Path, old_hooks: Path) -> dict[str, Any]:
    """The same store answer through the old and the new hook: the bytes."""
    new = load_hook(hooks, "replay_compare_new")
    old = load_hook(old_hooks, "replay_compare_old")
    corpus_mod = new._CORPUS
    same, differ, rows = 0, [], 0
    with tempfile.TemporaryDirectory(prefix="nob-compare-") as raw:
        tmp = Path(raw)
        data = tmp / "data"
        install_config(data)
        store = _Store(data)
        try:
            for n, query in enumerate(fx.exact()):
                folder = fx.memory(query)
                corpus = corpus_mod.load_memory_corpora([str(folder)], None)[0]
                order = new.text_search(query["prompt"], corpus) or [(0, 0.0)]
                results = [
                    {
                        "rank": r,
                        "id": 1000 + i,
                        "title": corpus[i].name,
                        "summary": corpus[i].description,
                        "score": round(0.9 - 0.01 * r, 4),
                        "source": corpus[i].rel_path,
                        "source_type": "claude_code_md",
                    }
                    for r, (i, _v) in enumerate(order[:200], 1)
                ]
                rows += len(results)
                answer = {"namespace": "claude_code", "mode": "hybrid", "results": results}
                outs = []
                for name, hook in (("old", old), ("new", new)):
                    env = base_env(tmp / name, data, tmp / name / "cache")
                    env["NOBLIVION_MEMORY_DIR"] = str(folder)
                    env["CLAUDE_PROJECT_DIR"] = f"{WORK}/{fx.project(query)}"
                    store.answers.clear()
                    # The hook may cut the prompt; the answer is keyed by what it sends.
                    store.answers.update({q: answer for q in _query_keys(hook, query, env)})
                    out, _ms = call(hook, env, query, f"{WORK}/{fx.project(query)}", f"cmp-{n}")
                    outs.append(out)
                if outs[0] == outs[1] and outs[0]:
                    same += 1
                else:
                    differ.append(query["id"])
        finally:
            store.stop()
    return {"mode": "compare", "prompts": len(fx.exact()), "identical": same, "differ": differ}


def _query_keys(hook: Any, query: dict[str, Any], env: dict[str, str]) -> list[str]:
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": query["prompt"]}
    keys = {query["prompt"]}
    for cut in (hook.QUERY_MAX_CHARS, hook.index_query_chars(env)):
        got = hook.query_for(payload, cut)
        if got:
            keys.add(got)
    return sorted(keys)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", type=Path, required=True)
    ap.add_argument("--size", type=int, default=20000)
    ap.add_argument("--mode", choices=("stopped", "hung", "compare"), default="stopped")
    ap.add_argument("--hooks", type=Path, default=ROOT / "hooks")
    ap.add_argument("--old-hooks", type=Path)
    ap.add_argument("--passes", type=int, default=2)
    args = ap.parse_args(argv)
    fx = Fixture(args.fixture, args.size)
    if args.mode == "compare":
        if args.old_hooks is None:
            ap.error("--mode compare needs --old-hooks")
        result = compare(fx, args.hooks, args.old_hooks)
    else:
        result = replay_silent(fx, args.hooks, args.mode, max(1, args.passes))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
