# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded recall diagnostics, using the actual ranked-index hook stages."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import signal
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from noblivion import config, db, doctor, launcher, ranking, rest
from noblivion.learning_runtime import use_allowed

MAX_QUERY = 4096
# A sanity cap, not the working limit. The time limit bounds the work and the
# answer holds MAX_CANDIDATES rows, so a store of 20,000 notes must pass: 300,000
# chunks rank in under 3 seconds. Measured 10,406 chunks on a real store at 0.3 s.
MAX_ROWS = 1_000_000
MAX_CANDIDATES = 200


class Unavailable(Exception):
    pass


class DiagnosticTimeout(BaseException):
    pass


@contextlib.contextmanager
def deadline(seconds):
    def expired(*_args):
        raise DiagnosticTimeout()

    previous = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )


def hook_module(folder=None):
    root = Path(__file__).resolve().parents[2]
    if not (root / "hooks/recall_hook.py").is_file():
        stamp = doctor._json_file(Path(folder) / "venv" / config.INSTALL_STAMP)
        value = stamp.get("plugin_root")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise Unavailable(
                "Installed recall hook source is unavailable; update plugin and store together."
            )
        root = Path(value)
    path = root / "hooks/recall_hook.py"
    spec = importlib.util.spec_from_file_location("_noblivion_explain_hook", path)
    if spec is None or spec.loader is None:
        raise Unavailable("Recall hook source is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if "diagnostic" not in __import__("inspect").signature(module._serve_index).parameters:
        raise Unavailable(
            "Recall hook version cannot explain candidates; update plugin and store together."
        )
    return module


@contextlib.contextmanager
def readonly_database(folder, seconds):
    path = folder / config.DB_FILE
    if not path.is_file() or path.is_symlink():
        raise Unavailable("Database is unavailable.")
    for suffix in ("-wal", "-journal"):
        companion = path.with_name(path.name + suffix)
        if companion.exists() and companion.stat().st_size:
            raise Unavailable(
                "Offline inspection requires a checkpointed database; "
                "live scoring is an explicit opt-in."
            )
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    conn = sqlite3.connect(
        "file:" + quote(str(path.resolve())) + "?mode=ro&immutable=1", uri=True, timeout=seconds
    )
    conn.row_factory = sqlite3.Row
    end = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() >= end), 100)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] != db.latest_version():
            raise Unavailable("Database schema does not match this diagnostic version.")
        if conn.execute("SELECT count(*) FROM memories").fetchone()[0] > MAX_ROWS:
            raise Unavailable("Database exceeds diagnostic row limit.")
        if conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0] > MAX_ROWS:
            raise Unavailable("Database exceeds diagnostic chunk limit.")
        yield conn
        if before != (path.stat().st_size, path.stat().st_mtime_ns) or any(
            path.with_name(path.name + s).exists() and path.with_name(path.name + s).stat().st_size
            for s in ("-wal", "-journal")
        ):
            raise Unavailable("Database changed during offline inspection.")
    finally:
        conn.close()


def live_index(folder, query, namespace, root, seconds):
    """Explicit request to an already-running, nonce-proven local store."""
    info = doctor._json_file(folder / config.STORE_JSON_FILE)
    port = info.get("port")
    token = doctor.read_bytes(folder / config.TOKEN_FILE, 128).decode("ascii").strip()
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 0 < port < 65536
        or not launcher.TOKEN_RE.fullmatch(token)
    ):
        raise Unavailable("Running store metadata is unavailable.")
    if launcher.probe(port, token, timeout=seconds) is None:
        raise Unavailable("Running store identity could not be verified.")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds)
    try:
        params = urlencode(
            {"q": query, "project": namespace, "root": root, "top_k": MAX_CANDIDATES}
        )
        connection.request(
            "GET", "/api/memories/index?" + params, headers={"Authorization": "Bearer " + token}
        )
        response = connection.getresponse()
        body = response.read(doctor.MAX_FILE_BYTES + 1)
        if response.status != 200 or len(body) > doctor.MAX_FILE_BYTES:
            raise Unavailable("Running store did not return a bounded index.")
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("namespace") != namespace:
            raise Unavailable("Running store returned an invalid scope.")
        return result
    finally:
        connection.close()


def explain(query, *, folder=None, namespace=None, root=None, live=False, seconds=3.0, env=None):
    env = dict(os.environ if env is None else env)
    if folder is not None:
        env["NOBLIVION_DATA_DIR"] = str(folder)
    folder = config.data_dir(env)
    cfg_path = Path(env.get("NOBLIVION_CONFIG") or folder / "config.json").expanduser()
    cfg = doctor._json_file(cfg_path) if cfg_path.exists() else {}
    hook = hook_module(folder)
    namespace = (
        namespace
        or env.get("NOBLIVION_RECALL_PROJECT")
        or env.get("NOBLIVION_PROJECT")
        or config.lookup(cfg, "namespace")
        or config.DEFAULT_NAMESPACE
    )
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", namespace):
        raise Unavailable("Namespace is invalid.")
    mapped_root, mapped_folder, mapping_status = doctor._codex_scope(env)
    if mapping_status == "fail":
        raise Unavailable("Codex workspace mapping is invalid.")
    root = root or hook.session_root(env) or mapped_root
    if not isinstance(root, str) or not root or len(root) > 256 or "\0" in root:
        raise Unavailable("Memory root is not configured; select --root explicitly.")
    env.update(
        NOBLIVION_RECALL_PROJECT=namespace,
        NOBLIVION_RECALL_ROOT=root,
        NOBLIVION_DATA_DIR=str(folder),
    )
    if mapped_folder and not env.get("NOBLIVION_MEMORY_DIR"):
        env["NOBLIVION_MEMORY_DIR"] = str(mapped_folder)
    if not query.strip() or len(query) > MAX_QUERY:
        raise Unavailable("Prompt must contain 1 to 4096 characters.")
    if not 0.1 <= seconds <= 10:
        raise Unavailable("Timeout must be between 0.1 and 10 seconds.")
    with deadline(seconds):
        if live:
            payload = live_index(folder, query, namespace, root, min(seconds, 2))
            source = "live_store"
            lexical = {}
        else:
            with readonly_database(folder, seconds) as conn:
                # CLI identity is unknown: never trust a caller-supplied session ID.
                if not use_allowed(conn, namespace, root, ""):
                    return {
                        "status": "disabled",
                        "candidates": [],
                        "reason": "Memory use is disabled for this scope.",
                    }
                ranker = ranking.Ranker(None)  # never constructs or calls an embedding service
                result = ranker.search(
                    conn,
                    query,
                    project=namespace,
                    root=root,
                    top_k=MAX_CANDIDATES,
                    shared_roots=config._shared_roots(env, cfg),
                )
                hits = [
                    hit for hit in result.hits if use_allowed(conn, namespace, hit.row.root, "")
                ]
                result = ranking.RankResult(result.mode, hits, result.pool_size)
                lexical = {hit.row.id: hit.bm25 for hit in hits}
                payload = rest.index_answer(result, namespace, query=query, mode="keyword")
            source = "offline_keyword"
        try:
            lines = hook.parse_index(payload)
        except hook.RecallError:
            raise Unavailable("Running store returned an invalid index.") from None
        trace = {}
        hook._serve_index(
            query,
            "UserPromptSubmit",
            None,
            hook.cache_dir(env),
            time.monotonic(),
            io.StringIO(),
            env,
            diagnostic=trace,
            candidates_override=lines,
        )
        for candidate in trace.get("candidates", []):
            candidate["keyword_score"] = lexical.get(candidate["id"])
        trace.update(
            status="ok",
            source=source,
            semantic_scoring="requested_from_store" if live else "unavailable_no_provider_calls",
            session_state="not_replayed_unknown_session",
            candidate_limit=MAX_CANDIDATES,
            coverage="retrieved_candidates_only; no unindexed notes or session replay",
            note_text_in_output=False,
        )
        return trace


def hub_notes(*, folder=None, env=None, now=None):
    """The hub notes of the prompt hook: notes shown in more than half of the sessions of the
    window (NOBLIVION-114). A hub row costs its characters on every prompt; in ``MEMORY.md`` it
    costs them once per session, so a hub is a candidate for that index. This reports and never
    edits the index. It reads ``hub-stats.json`` only and writes nothing."""
    env = dict(os.environ if env is None else env)
    if folder is not None:
        env["NOBLIVION_DATA_DIR"] = str(folder)
    folder = config.data_dir(env)
    hook = hook_module(folder)
    stats = hook._sibling_module("hub_stats")
    span = stats.window_days(env)
    base = {
        "status": "ok",
        "source": "hub_stats",
        "window_days": span,
        "min_sessions": stats.MIN_SESSIONS,
        "hub_share": stats.HUB_SHARE,
        "note_names_in_output": True,
        "action": "Review these notes by hand. This command never edits MEMORY.md or any index.",
        "candidates": [],
    }
    if span <= 0:
        return {**base, "reason": "Hub counting is off (recall.hub_days is 0).", "sessions": 0}
    cache = hook.cache_dir(env)
    sessions, counts = stats.window(stats.load_days(cache), span, now)
    base["sessions"] = sessions
    if sessions < stats.MIN_SESSIONS:
        return {**base, "reason": f"Fewer than {stats.MIN_SESSIONS} sessions in the window."}
    names, _ = stats.hubs(cache, span, now)
    base["candidates"] = [
        {"note": name, "sessions": counts[name], "share": round(counts[name] / sessions, 3)}
        for name in sorted(names, key=lambda n: (-counts[n], n))
    ]
    return base


def print_hubs(result, as_json):
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return 0
    lines = [
        "Hub notes, candidates for MEMORY.md: "
        f"{len(result['candidates'])} in {result['sessions']} sessions "
        f"of the last {result['window_days']} days",
        result["action"],
    ]
    if result.get("reason"):
        lines.append(result["reason"])
    lines += [
        f"{row['note']}: shown in {row['sessions']} sessions ({row['share']})"
        for row in result["candidates"]
    ]
    print("\n".join(lines))
    return 0


_MISSING_VALUE = re.compile(r"^argument (--[a-z-]+): expected one argument$")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        """Refuse with a reason that never echoes a value: the prompt and an
        unknown option may be private. Only a fixed option name is named."""
        reason = "Invalid diagnostic arguments; use --help."
        found = _MISSING_VALUE.match(message)
        if found:
            reason = f"Invalid diagnostic arguments: {found.group(1)} needs a value; use --help."
        elif message.startswith("the following arguments are required"):
            reason = "Invalid diagnostic arguments: the prompt is missing; use --help."
        raise Unavailable(reason)


def join_root(argv):
    """``--root -slug`` as ``--root=-slug``. A root is a folder slug and starts
    with a dash, which argparse reads as a flag. A value that starts with
    ``--`` stays an option, so ``--root --json`` is still an error."""
    items = list(argv)
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        if item == "--root" and nxt and nxt.startswith("-") and not nxt.startswith("--"):
            out.append(f"--root={nxt}")
            i += 2
            continue
        out.append(item)
        i += 1
    return out


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--root")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "explicitly request existing-store scoring; may send the prompt to "
            "its configured embedding provider; never starts a store"
        ),
    )
    parser.add_argument(
        "--hubs",
        action="store_true",
        help=(
            "list the notes the prompt hook shows in most sessions, as candidates for "
            "MEMORY.md; reads the hub counts only and edits no index (no prompt needed)"
        ),
    )
    try:
        args = parser.parse_args(join_root(sys.argv[1:] if argv is None else argv))
        if args.hubs:
            return print_hubs(hub_notes(folder=args.data_dir), args.json)
        if args.prompt is None:
            raise Unavailable("Invalid diagnostic arguments: the prompt is missing; use --help.")
        result = explain(
            args.prompt,
            folder=args.data_dir,
            namespace=args.namespace,
            root=args.root,
            live=args.live,
            seconds=args.timeout,
        )
    except Unavailable as exc:
        result = {"status": "unavailable", "reason": str(exc), "candidates": []}
    except DiagnosticTimeout:
        result = {
            "status": "unavailable",
            "reason": "Diagnostic time limit reached.",
            "candidates": [],
        }
    except (OSError, ValueError, sqlite3.Error, http.client.HTTPException, RecursionError):
        result = {
            "status": "unavailable",
            "reason": (
                "Cannot inspect the configured scope within diagnostic limits; "
                "run doctor and check --root."
            ),
            "candidates": [],
        }
    print(
        json.dumps(result, sort_keys=True)
        if "args" not in locals() or args.json
        else "\n".join(
            [
                "Recall explanation: " + result["status"],
                "Source: " + result.get("source", "unavailable"),
                result.get(
                    "reason",
                    "Offline keyword scoring does not predict hybrid cosine scores."
                    if not args.live
                    else "Existing-store scores; no recall state was written.",
                ),
                *[
                    f"id {row['id']}: {row['decision']} "
                    f"(score={row['score']}, keyword={row['keyword_score']})"
                    for row in result["candidates"]
                ],
            ]
        )
    )
    return 2 if result["status"] == "unavailable" else 1 if result["status"] == "disabled" else 0
