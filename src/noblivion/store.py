# SPDX-License-Identifier: AGPL-3.0-or-later
"""The store process: ``python -m noblivion.store`` or ``noblivion serve``.

Design doc 0001, section 3. One process per data dir:

- holds ``flock`` on ``store.lock`` for its lifetime; a second store exits 0;
- makes the ``token`` file (mode 0600) when it is missing;
- opens the database and runs migrations (exit 3 on a schema fault);
- binds ``127.0.0.1:<port>`` and nothing else (exit 2 when the port is taken);
- writes ``store.json`` (temp file plus rename) after the bind;
- answers at once; the index scan, the model load and the embed backfill run
  in background threads and never block a request;
- exits after ``idle_exit_s`` without a request, or on ``SIGTERM``/``SIGINT``:
  it finishes open requests (5 s), checkpoints the WAL, deletes ``store.json``
  and releases the lock.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socketserver
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from noblivion import __version__, config, db, embedding, indexer, ranking, trust
from noblivion.launcher import TOKEN_RE, clear_start_error, health_proof, write_start_error
from noblivion.rest import Handler, read_trust

log = logging.getLogger("noblivion.store")

LOOPBACK = "127.0.0.1"  # fixed in code, not in config (section 15.3)
SERVICE = "noblivion"
DRAIN_TIMEOUT_S = 5.0
JOB_JOIN_TIMEOUT_S = 60.0
JOB_TICK_S = 0.5

EXIT_OK = 0
EXIT_BIND = 2
EXIT_SCHEMA = 3
EXIT_NOT_INSTALLED = 4  # no data dir, or no database and install.sh has not run

INDEX_SCANNING = "scanning"
INDEX_IDLE = "idle"

_NO_LABELLER = object()


class StoreLockedError(RuntimeError):
    """Another store holds ``store.lock``."""


class BindRefusedError(ValueError):
    """The store binds the loopback address only."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The stdlib listen backlog is 5. Hooks of several sessions connect at the
    # same moment (up to 4 calls per hook process); a full backlog drops the
    # SYN and the client retries after 1 s, past the 300 ms listener proof.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], store: Store) -> None:
        self.store = store
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(host), a reverse name
        # lookup of 127.0.0.1. On macOS that lookup can block for 20 s or more,
        # so the store wrote no store.json in time and the start failed. The
        # store needs no host name: bind, and use the address as the name.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: object, client_address: object) -> None:
        # The stdlib prints a traceback to stderr; log the class name only.
        log.warning("request error: %s", type(sys.exc_info()[1]).__name__)


def make_server(host: str, port: int, store: Store) -> _Server:
    """Bind the REST server. Any address but 127.0.0.1 is refused."""
    if host != LOOPBACK:
        raise BindRefusedError(f"the store binds {LOOPBACK} only")
    return _Server((LOOPBACK, port), store)


# -- files in the data dir --------------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` as mode 0600: temp file, fsync, rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_token(data_dir: Path) -> str:
    """The token of section 4.1. Made (32 random bytes, hex, mode 0600) when
    missing or malformed; an existing good token is kept."""
    path = data_dir / config.TOKEN_FILE
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if TOKEN_RE.fullmatch(text):
        os.chmod(path, 0o600)
        return text
    token = secrets.token_hex(32)
    _write_private(path, (token + "\n").encode("ascii"))
    return token


def acquire_lock(path: Path, wait_s: float = 0.0) -> int:
    """``flock(LOCK_EX | LOCK_NB)`` on ``store.lock``; returns the fd to keep open."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise StoreLockedError("another store holds the lock") from None
            time.sleep(0.1)


# -- the store --------------------------------------------------------------------


class Store:
    """One store process. ``open``, then ``serve`` (blocks), or ``stop`` from a test."""

    def __init__(
        self,
        settings: config.Settings,
        store_settings: config.StoreSettings,
        *,
        embedding_service: embedding.EmbeddingService | None = None,
        labeller: object = _NO_LABELLER,
        host: str = LOOPBACK,
        lock_wait_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store_settings = store_settings
        self.namespace = settings.namespace
        self.data_dir = settings.data_dir
        self.host = host
        self.lock_wait_s = lock_wait_s
        self.clock = clock
        self.embedding = embedding_service or embedding.EmbeddingService(
            embedding.load_embedding_settings()
        )
        self.ranker = ranking.Ranker(self.embedding)
        self._labeller = labeller
        self.token = ""
        self.port = 0
        self.server: _Server | None = None
        self.started_at = 0.0
        self.index_state = INDEX_SCANNING
        self.index_blocked = False
        self._lock_fd: int | None = None
        self._stop = threading.Event()
        self._jobs_stop = threading.Event()
        self._activity = threading.Lock()
        self._active = 0
        self._last_request = clock()
        self._threads: list[threading.Thread] = []
        self._model_thread: threading.Thread | None = None
        self._stat_cache: indexer.StatCache = {}
        self.stop_reason = ""

    # -- paths

    @property
    def store_json_path(self) -> Path:
        return self.data_dir / config.STORE_JSON_FILE

    @property
    def lock_path(self) -> Path:
        return self.data_dir / config.STORE_LOCK_FILE

    # -- start

    def open(self) -> None:
        """Steps 5 and 6 of section 3.2: lock, token, database, bind, ``store.json``.

        Raises ``StoreLockedError``, ``db.SchemaError``, ``BindRefusedError`` or
        ``OSError`` (bind). Nothing is left held on a failure.
        """
        if self.host != LOOPBACK:
            raise BindRefusedError(f"the store binds {LOOPBACK} only")
        config.private_dir(self.data_dir, create=False)
        self._lock_fd = acquire_lock(self.lock_path, self.lock_wait_s)
        try:
            self.token = ensure_token(self.data_dir)
            # A new database only after install.sh has run (NOBLIVION-28).
            create = config.is_installed(self.data_dir)
            db.open_db(self.settings.db_path, allow_migrate=True, create=create).close()
            self.server = make_server(self.host, self.store_settings.port, self)
            self.port = int(self.server.server_address[1])
            self.started_at = time.time()
            self._last_request = self.clock()
            self._write_store_json()
        except BaseException:
            if self.server is not None:
                self.server.server_close()
                self.server = None
            self._release_lock()
            raise
        log.info("store %s listening on port %d", __version__, self.port)

    def _write_store_json(self) -> None:
        info = {
            "pid": os.getpid(),
            "port": self.port,
            "version": __version__,
            "started_at": db.format_ts(datetime.fromtimestamp(self.started_at, tz=timezone.utc)),
        }
        _write_private(self.store_json_path, (json.dumps(info) + "\n").encode("utf-8"))

    def start_background(self) -> None:
        """The first index scan and the model load, then the periodic jobs."""
        self._model_thread = self._spawn(self._load_model, "noblivion-model")
        self._spawn(self._jobs, "noblivion-jobs")

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()
        return thread

    # -- requests

    def request_started(self) -> None:
        with self._activity:
            self._active += 1
            self._last_request = self.clock()

    def request_finished(self) -> None:
        with self._activity:
            self._active -= 1
            self._last_request = self.clock()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """A connection for this thread only (section 6.1); closed after use.
        It opens the existing database only: a request or the stop after an
        uninstall deleted the data dir does not make it again (NOBLIVION-28)."""
        conn = db.connect(self.settings.db_path, create=False)
        try:
            yield conn
        finally:
            conn.close()

    def rank(
        self,
        query: str,
        namespace: str,
        *,
        top_k: int,
        root: str | None = None,
        include_mined: bool = False,
    ) -> ranking.RankResult | None:
        """Rank for a read route; None on an internal fault (logged, no text)."""
        try:
            with self.connection() as conn:
                return self.ranker.search(
                    conn,
                    query,
                    project=namespace,
                    top_k=top_k,
                    root=root,
                    shared_roots=self.store_settings.shared_roots,
                    include_mined=include_mined,
                )
        except Exception as exc:  # noqa: BLE001 - section 4: 200 with the fixed reason
            log.error("rank failed: %s", type(exc).__name__)
            return None

    def read_trust(self, ids: Sequence[int]) -> dict[int, tuple[float, int]] | None:
        try:
            with self.connection() as conn:
                return read_trust(conn, ids)
        except (sqlite3.Error, OSError) as exc:
            log.error("trust read failed: %s", type(exc).__name__)
            return {}

    def ingest_feedback(self, batch: trust.Batch) -> dict[str, int] | None:
        """Section 9.2: one transaction per batch. None on a store failure."""
        try:
            with self.connection() as conn:
                counts = trust.store_batch(
                    conn,
                    batch,
                    project=self.namespace,
                    prior_mined=self.store_settings.prior_mined,
                )
        except (sqlite3.Error, OSError) as exc:
            log.error("feedback store failed: %s", type(exc).__name__)
            return None
        if counts["inserted"] or counts["unknown"] or counts["rejected"]:
            log.info("feedback batch: %s", " ".join(f"{k}={v}" for k, v in counts.items()))
        return counts

    def trust_report(self) -> dict | None:
        """Section 4.6, in one read transaction. None on a store failure."""
        try:
            with self.connection() as conn:
                return trust.report(conn, self.namespace)
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.error("trust report failed: %s", type(exc).__name__)
            return None

    def maintenance_once(self, conn: sqlite3.Connection) -> trust.MaintenanceResult | None:
        """Section 9.3: the pass at start and every 24 hours."""
        try:
            result = trust.maintenance_pass(
                conn,
                delete_grace_days=self.settings.delete_grace_days,
                archive_retention_days=self.settings.archive_retention_days,
                prior_mined=self.store_settings.prior_mined,
                backups_dir=self.data_dir / "backups",
            )
        except Exception as exc:  # noqa: BLE001 - the next pass retries
            log.error("maintenance failed: %s", type(exc).__name__)
            return None
        log.info("maintenance: %s", result.summary())
        return result

    def mode(self) -> str:
        if self.embedding.model_id is not None and ranking.np is not None:
            return ranking.MODE_HYBRID
        return ranking.MODE_KEYWORD

    def model(self) -> str | None:
        """The embedding model of the cosine leg by its plain name, or None in
        keyword mode. The index answer names it (section 4.3)."""
        embedder = self.embedding.embedder  # one read: a revoked consent sets it to None
        if embedder is None or self.mode() != ranking.MODE_HYBRID:
            return None
        return embedder.model

    def health(self, *, nonce: str | None, full: bool) -> dict:
        """Section 4.7. The short form without a token; never a path or memory text."""
        state = self.embedding.state
        answer: dict[str, object] = {
            "status": "degraded" if state == embedding.STATE_FAILED else "ok",
            "service": SERVICE,
            "version": __version__,
        }
        if nonce is not None:
            answer["proof"] = health_proof(self.token, nonce)
        if not full:
            return answer
        answer.update(self._health_details())
        return answer

    def _health_details(self) -> dict[str, object]:
        emb = self.embedding
        model_id = emb.model_id
        memories = missing = schema_version = content_rev = vector_rev = consent = None
        dim = getattr(emb.embedder, "dim", None) if emb.embedder is not None else None
        try:
            with self.connection() as conn, db.read_tx(conn):
                schema_version = db.user_version(conn)
                content_rev, vector_rev = db.revisions(conn)
                consent = emb.consent_state(conn)
                memories = conn.execute(
                    "SELECT count(*) FROM memories WHERE project = ? "
                    "AND archived_at IS NULL AND deleted_at IS NULL",
                    (self.namespace,),
                ).fetchone()[0]
                missing = 0
                if model_id is not None:
                    missing = conn.execute(
                        "SELECT count(*) FROM memories m WHERE m.project = ? "
                        "AND m.archived_at IS NULL AND m.deleted_at IS NULL AND NOT EXISTS "
                        "(SELECT 1 FROM vectors v WHERE v.memory_id = m.id AND v.model = ?)",
                        (self.namespace, model_id),
                    ).fetchone()[0]
                    if dim is None:
                        row = conn.execute(
                            "SELECT dim FROM vectors WHERE model = ? LIMIT 1", (model_id,)
                        ).fetchone()
                        dim = int(row[0]) if row else None
        except (sqlite3.Error, OSError) as exc:
            log.error("health read failed: %s", type(exc).__name__)
        settings = emb.settings
        return {
            "uptime_s": int(max(0.0, time.time() - self.started_at)),
            "memories": memories,
            "schema_version": schema_version,
            "content_rev": content_rev,
            "vector_rev": vector_rev,
            "embedding": {
                "backend": settings.backend,
                "model": None if settings.backend == "none" else settings.model,
                "dim": dim,
                "state": "off" if emb.state == embedding.STATE_NONE else emb.state,
                "missing_vectors": missing,
                "consent": consent,
            },
            "mode": self.mode(),
            "trust_ranking": self.store_settings.trust_ranking,
            "index_state": self.index_state,
            "index_blocked": self.index_blocked,
        }

    # -- background jobs

    def _load_model(self) -> None:
        try:
            with self.connection() as conn:
                state = self.embedding.start(conn)
            log.info("embedding state: %s", state)
        except Exception as exc:  # noqa: BLE001 - keyword mode on any fault
            log.error("model load failed: %s", type(exc).__name__)

    def _labeller_now(self):
        if self._labeller is _NO_LABELLER:
            from noblivion import labels

            self._labeller = labels.make_labeller(self.settings)
        return self._labeller

    def scan_once(self, conn: sqlite3.Connection) -> indexer.ScanResult | None:
        """One index scan under ``index.lock``. None when the CLI holds the lock."""
        self.index_state = INDEX_SCANNING
        try:
            with indexer.index_lock(self.settings.index_lock_path, timeout_s=0.0):
                result = indexer.scan(
                    conn,
                    self.settings.resolved_memory_dirs(),
                    project=self.namespace,
                    delete_grace_days=self.settings.delete_grace_days,
                    labeller=self._labeller_now(),  # type: ignore[arg-type]
                    stat_cache=self._stat_cache,
                )
        except indexer.LockTimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - the next scan retries
            log.error("index scan failed: %s", type(exc).__name__)
            return None
        finally:
            self.index_state = INDEX_IDLE
        self.index_blocked = result.blocked
        if result.changed or result.skipped or result.blocked:
            log.info("index scan: %s", result.summary())
        return result

    def backfill_once(self, conn: sqlite3.Connection) -> None:
        try:
            result = self.embedding.backfill(conn)
        except Exception as exc:  # noqa: BLE001 - rows stay without a vector; next pass
            log.error("embed backfill failed: %s", type(exc).__name__)
            return
        if result is not None and (result.embedded or result.failed):
            log.info("embed backfill: embedded=%d failed=%d", result.embedded, result.failed)

    def _jobs(self) -> None:
        interval = self.store_settings.index_interval_s
        try:
            with self.connection() as conn:
                self.maintenance_once(conn)
                next_maintenance = self.clock() + trust.MAINTENANCE_INTERVAL_S
                self.scan_once(conn)
                next_scan = self.clock() + interval
                backfilled_state = None
                backfilled_rev = -1
                while not self._jobs_stop.wait(JOB_TICK_S):
                    # A revoked consent turns a remote backend off, also on a
                    # store that gets no query (NOBLIVION-51).
                    self.embedding.check_consent(conn)
                    state = self.embedding.state
                    if self.clock() >= next_maintenance:
                        self.maintenance_once(conn)
                        next_maintenance = self.clock() + trust.MAINTENANCE_INTERVAL_S
                    due_scan = interval > 0 and self.clock() >= next_scan
                    if due_scan:
                        self.scan_once(conn)
                        next_scan = self.clock() + interval
                    usable = state in (embedding.STATE_READY, embedding.STATE_REEMBEDDING)
                    # A content change by `noblivion index` also needs a pass,
                    # with periodic scans off too (NOBLIVION-42).
                    content_rev = db.revisions(conn)[0] if usable else 0
                    if usable and (
                        due_scan or backfilled_state != state or content_rev > backfilled_rev
                    ):
                        self.backfill_once(conn)
                        backfilled_state = self.embedding.state
                        backfilled_rev = content_rev
                    if (
                        state == embedding.STATE_FAILED
                        and not self._model_alive()
                        and self.embedding.retry_due(conn)
                    ):
                        # The service keeps the retry to once per hour, and a
                        # new consent ends the wait. No thread and no log line
                        # on a tick where a start would change nothing.
                        self._model_thread = self._spawn(self._load_model, "noblivion-model")
        except Exception as exc:  # noqa: BLE001 - the store keeps answering
            log.error("background jobs stopped: %s", type(exc).__name__)

    def _model_alive(self) -> bool:
        return self._model_thread is not None and self._model_thread.is_alive()

    # -- serve and stop

    def idle_for(self) -> float:
        with self._activity:
            if self._active:
                return 0.0
            return self.clock() - self._last_request

    def request_stop(self, reason: str = "stop") -> None:
        if not self._stop.is_set():
            self.stop_reason = reason
            self._stop.set()

    def serve(self) -> int:
        """Answer requests until a stop or the idle limit; then shut down cleanly."""
        assert self.server is not None, "open() first"
        server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="noblivion-http",
            daemon=True,
        )
        server_thread.start()
        idle_limit = self.store_settings.idle_exit_s
        tick = min(0.25, idle_limit / 4) if idle_limit else 0.25
        while not self._stop.wait(tick):
            if idle_limit and self.idle_for() >= idle_limit:
                self.request_stop("idle")
            elif not self.data_dir.is_dir():
                # An uninstall deleted the data dir: stop and make nothing
                # (NOBLIVION-28). Checked here, not in the jobs thread, which
                # ends on its first fault.
                self.request_stop("data dir removed")
        log.info("store stopping: %s", self.stop_reason)
        self.server.shutdown()
        server_thread.join(DRAIN_TIMEOUT_S)
        self.close()
        return EXIT_OK

    def close(self) -> None:
        """Finish open requests (5 s), stop the jobs after their current batch,
        checkpoint the WAL, delete ``store.json``, release the lock."""
        deadline = time.monotonic() + DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._activity:
                if self._active <= 0:
                    break
            time.sleep(0.05)
        if self.server is not None:
            self.server.server_close()
            self.server = None
        self._jobs_stop.set()
        for thread in self._threads:
            # The jobs thread ends after its current batch. A model load cannot
            # be cut short; its thread is a daemon and dies with the process.
            thread.join(JOB_JOIN_TIMEOUT_S if thread.name == "noblivion-jobs" else 1.0)
        try:
            with self.connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (sqlite3.Error, OSError) as exc:
            log.error("checkpoint failed: %s", type(exc).__name__)
        self.embedding.close()
        self._remove_store_json()
        self._release_lock()

    def _remove_store_json(self) -> None:
        """Delete ``store.json`` only when it names this process."""
        try:
            info = json.loads(self.store_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            try:
                self.store_json_path.unlink()
            except OSError:
                pass

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None


# -- process entry ----------------------------------------------------------------


def _setup_logging(data_dir: Path, level: str) -> None:
    path = data_dir / config.STORE_LOG_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1024 * 1024, backupCount=4, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("noblivion")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion serve", description="Run the local memory store in the foreground."
    )
    parser.add_argument("--port", type=int, default=None, help="port; 0 = any free port")
    parser.add_argument(
        "--lock-wait", type=float, default=0.0, help="seconds to wait for the store lock"
    )
    return parser


def bind_error_text(port: int, exc: OSError) -> str:
    """One line with the reason, for the log and ``store.error``. The text
    starts with ``bind failed on port N`` (docs/troubleshooting.md)."""
    detail = exc.strerror or str(exc) or type(exc).__name__
    text = f"bind failed on port {port}: {detail}"
    if exc.errno == errno.EADDRINUSE:
        text += (
            '. Another program uses the port: set "port" in config.json to 0'
            " (any free port) or to a free port"
        )
    return text


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(0o077)
    settings = config.load_settings()
    store_settings = config.load_store_settings()
    if args.port is not None and 0 <= args.port <= 65535:
        from dataclasses import replace

        store_settings = replace(store_settings, port=args.port)
    try:
        config.private_dir(settings.data_dir, create=False)
    except config.DataDirMissing as exc:
        print(f"noblivion serve: {exc}", file=sys.stderr)
        return EXIT_NOT_INSTALLED
    _setup_logging(settings.data_dir, store_settings.log_level)

    store = Store(settings, store_settings, lock_wait_s=args.lock_wait)

    def on_signal(signum: int, _frame: object) -> None:
        store.request_stop(signal.Signals(signum).name)

    # Install the handlers before open() writes store.json (NOBLIVION-20): a
    # caller that sees store.json may send SIGTERM at once. With the default
    # action the process dies and leaves a stale store.json; with the handler
    # serve() stops at once and close() deletes store.json.
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        store.open()
    except StoreLockedError:
        log.info("another store runs; exiting")
        return EXIT_OK
    except db.SchemaError as exc:
        log.error("schema: %s", exc)
        write_start_error(settings.data_dir, f"database schema fault: {exc}")
        return EXIT_SCHEMA
    except (db.DatabaseMissing, config.DataDirMissing) as exc:
        log.error("not installed: %s", exc)
        write_start_error(settings.data_dir, f"not installed: {exc}")
        return EXIT_NOT_INSTALLED
    except OSError as exc:
        reason = bind_error_text(store_settings.port, exc)
        log.error("%s", reason)
        write_start_error(settings.data_dir, reason)
        return EXIT_BIND
    clear_start_error(settings.data_dir)

    store.start_background()
    return store.serve()


if __name__ == "__main__":
    sys.exit(main())
