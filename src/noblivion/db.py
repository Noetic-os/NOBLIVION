# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite access for the store and the CLI tools (design doc 0001, section 6).

Connection rules (section 6.1): every connection sets WAL, ``synchronous =
NORMAL``, ``foreign_keys = ON``, ``busy_timeout = 5000`` and ``trusted_schema =
OFF``. Connections run in autocommit mode and open transactions explicitly:
``write_tx`` uses ``BEGIN IMMEDIATE``, ``read_tx`` uses a deferred ``BEGIN``.
One connection per thread.

Migrations (section 6.6): numbered SQL scripts in ``noblivion/migrations``,
applied in order, each in one ``BEGIN IMMEDIATE`` transaction together with its
``PRAGMA user_version`` change. A backup copy is taken before a migration of an
existing database. A database newer than the code is never opened for writes.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

BATCH_ROWS = 200
BUSY_TIMEOUT_MS = 5000
KEEP_BACKUPS = 3
SOURCE_MD = "claude_code_md"
SOURCE_MINED = "transcript_mined"

WAL_PRAGMA = "PRAGMA journal_mode = WAL"
_CONNECTION_PRAGMAS = (
    WAL_PRAGMA,
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
    "PRAGMA trusted_schema = OFF",
)

_MIGRATION_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SchemaError(RuntimeError):
    """The schema version does not allow this caller to use the database."""


class SchemaTooNewError(SchemaError):
    pass


class SchemaTooOldError(SchemaError):
    pass


class DatabaseMissing(FileNotFoundError):
    """The database file does not exist and the caller may not create it."""


def utc_now() -> str:
    return format_ts(datetime.now(timezone.utc))


def format_ts(moment: datetime) -> str:
    """UTC ISO-8601 with microseconds and a ``Z``. One format, so text compares."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


# -- connection ---------------------------------------------------------------


def connect(db_path: Path | str, *, create: bool = False) -> sqlite3.Connection:
    """Open a connection with the section 6.1 rules.

    ``create=False`` (the default) opens an existing database only: it never
    makes the folder or the file, and raises ``DatabaseMissing`` when the file
    is missing. SQLite opens it with ``mode=rw``, so a file deleted between the
    check and the open is not made again either. Only a caller that knows
    ``install.sh`` has run passes ``create=True`` (NOBLIVION-28): then the
    folder is made as 0700 and the file as 0600.
    """
    path = Path(db_path)
    if str(path) == ":memory:":
        conn = sqlite3.connect(
            ":memory:", timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True
        )
    elif create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create the file before SQLite does, so it is 0600 from the start. The
        # WAL and SHM files take the mode of the database file.
        #
        # Never open and close an existing database file here: closing any fd
        # of a file drops every POSIX lock this process holds on it, also the
        # locks of other open SQLite connections (the store opens one per
        # request). Another process could then write under a reader, which
        # gave "database disk image is malformed". O_EXCL opens nothing when
        # the file exists.
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        conn = sqlite3.connect(
            str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True
        )
    else:
        if not path.is_file():
            raise DatabaseMissing(f"no database at {path}; run install.sh")
        uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=rw"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                check_same_thread=True,
            )
        except sqlite3.OperationalError as exc:
            if not path.is_file():
                raise DatabaseMissing(f"no database at {path}; run install.sh") from exc
            raise
    conn.row_factory = sqlite3.Row
    for pragma in _CONNECTION_PRAGMAS:
        if pragma == WAL_PRAGMA:
            _set_wal(conn)
        else:
            conn.execute(pragma)
    return conn


def _set_wal(conn: sqlite3.Connection) -> None:
    """``PRAGMA journal_mode = WAL`` with a retry up to the busy timeout.

    The switch of a new database to WAL takes an exclusive lock, and SQLite
    does not run the busy handler for it. When the store and a CLI tool open a
    new database at the same moment, one of them got "database is locked" at
    once. Retry until ``BUSY_TIMEOUT_MS``, as for any other lock.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute(WAL_PRAGMA).fetchall()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(0.01)


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One ``BEGIN IMMEDIATE`` transaction: the write lock is taken at the start."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One deferred read transaction: one snapshot, never blocks a writer."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


# -- migrations ---------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    found: list[Migration] = []
    for entry in resources.files("noblivion").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME_RE.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise RuntimeError(f"migration numbers have a gap at {migration.name}")
    return found


def latest_version() -> int:
    return len(load_migrations())


def split_statements(script: str) -> list[str]:
    """Split a SQL script into statements. Comments may hold ``;``."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if _strip_comments(statement):
                statements.append(statement)
    if _strip_comments(buffer):
        raise ValueError("migration script ends inside a statement")
    return statements


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).strip()


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
    return int(row.fetchone()[0]) == 0


def _seed_version_1(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("content_rev", "0"),
            ("vector_rev", "0"),
            ("db_id", secrets.token_hex(16)),
            ("created_at", now),
        ],
    )


_SEEDS = {1: _seed_version_1}


def _backup(conn: sqlite3.Connection, db_path: Path, old_version: int) -> Path:
    folder = db_path.parent / "backups"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"noblivion-v{old_version}-{stamp}.db"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    prune_backups(folder)
    return target


def prune_backups(folder: Path, keep: int = KEEP_BACKUPS) -> None:
    copies = sorted(folder.glob("noblivion-v*-*.db"), key=lambda p: p.stat().st_mtime)
    for old in copies[:-keep] if keep > 0 else copies:
        old.unlink(missing_ok=True)


def migrate(
    conn: sqlite3.Connection,
    db_path: Path | None = None,
    *,
    allow_migrate: bool = True,
    random_base: int | None = None,
) -> int:
    """Bring the schema to the latest version and return it.

    ``allow_migrate=False`` is the CLI mode: it creates a new, empty database,
    but refuses to change an existing older schema (only the store migrates).
    """
    migrations = load_migrations()
    latest = len(migrations)
    current = user_version(conn)
    if current > latest:
        raise SchemaTooNewError(
            f"database is newer than this NOBLIVION (schema {current}, code knows {latest})"
        )
    if current == latest:
        return current
    fresh = current == 0 and _is_empty(conn)
    if not fresh and not allow_migrate:
        raise SchemaTooOldError(
            f"database schema {current} is older than {latest}; start the store once to migrate"
        )
    if not fresh and db_path is not None:
        _backup(conn, db_path, current)
    for migration in migrations[current:]:
        with write_tx(conn):
            # Re-check under the write lock: another process may have migrated.
            if user_version(conn) >= migration.version:
                continue
            params = {
                "random_base": random_base
                if random_base is not None
                else secrets.randbelow(999_000_001) + 1_000_000
            }
            for statement in split_statements(migration.sql):
                conn.execute(statement, params if ":random_base" in statement else ())
            seed = _SEEDS.get(migration.version)
            if seed is not None:
                seed(conn)
            conn.execute(f"PRAGMA user_version = {migration.version:d}")
    return user_version(conn)


def open_db(
    db_path: Path | str, *, allow_migrate: bool = True, create: bool = False
) -> sqlite3.Connection:
    """Connect and bring the schema up to date. Closes the connection on error.
    ``create`` as in ``connect``: by default a missing database is an error."""
    path = Path(db_path)
    conn = connect(path, create=create)
    try:
        migrate(conn, path, allow_migrate=allow_migrate)
    except BaseException:
        conn.close()
        raise
    return conn


# -- meta and revision counters ------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bump_rev(conn: sqlite3.Connection, key: str = "content_rev") -> int:
    """Increment a revision counter and return the new value.

    Call inside ``write_tx``; stamp every changed row with the result.
    """
    if key not in ("content_rev", "vector_rev"):
        raise ValueError(f"not a revision counter: {key}")
    row = conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = ? RETURNING value",
        (key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"meta.{key} is missing")
    return int(row[0])


def revisions(conn: sqlite3.Connection) -> tuple[int, int]:
    """``(content_rev, vector_rev)`` in one read."""
    rows = dict(
        conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('content_rev', 'vector_rev')"
        ).fetchall()
    )
    return int(rows.get("content_rev", 0)), int(rows.get("vector_rev", 0))


# -- memories -----------------------------------------------------------------


@dataclass(frozen=True)
class MemoryRow:
    id: int
    root: str
    path: str
    hash: str
    category: str
    archived_at: str | None
    deleted_at: str | None

    @property
    def live(self) -> bool:
        return self.archived_at is None and self.deleted_at is None


def md_rows(conn: sqlite3.Connection, project: str) -> list[MemoryRow]:
    """Every ``claude_code_md`` row of a namespace, live or not."""
    cur = conn.execute(
        "SELECT id, root, path, hash, category, archived_at, deleted_at FROM memories "
        "WHERE project = ? AND source_type = ? ORDER BY root, path",
        (project, SOURCE_MD),
    )
    return [MemoryRow(*tuple(r)) for r in cur.fetchall()]


def row_at(conn: sqlite3.Connection, project: str, root: str, path: str) -> MemoryRow | None:
    r = conn.execute(
        "SELECT id, root, path, hash, category, archived_at, deleted_at FROM memories "
        "WHERE project = ? AND root = ? AND path = ?",
        (project, root, path),
    ).fetchone()
    return None if r is None else MemoryRow(*tuple(r))


def insert_memory(
    conn: sqlite3.Connection,
    *,
    project: str,
    root: str,
    path: str,
    source_type: str,
    category: str,
    content: str,
    content_hash: str,
    labels: str,
    rev: int,
    now: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO memories (project, root, path, source_type, category, content, hash, "
        "labels, rev, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, root, path, source_type, category, content, content_hash, labels, rev, now, now),
    )
    return int(cur.lastrowid)


def rewrite_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    *,
    root: str,
    path: str,
    category: str,
    content: str,
    content_hash: str,
    labels: str,
    rev: int,
    now: str,
) -> bool:
    """Update a row in place (same id) and make it live again.

    Covers a changed file, a forced re-index, a revive of a soft-deleted row,
    an un-archive and a move to a new root or path.
    """
    cur = conn.execute(
        "UPDATE memories SET root = ?, path = ?, category = ?, content = ?, hash = ?, "
        "labels = ?, archived_at = NULL, deleted_at = NULL, rev = ?, updated_at = ? "
        "WHERE id = ?",
        (root, path, category, content, content_hash, labels, rev, now, memory_id),
    )
    return cur.rowcount == 1


def soft_delete(conn: sqlite3.Connection, memory_id: int, *, rev: int, now: str) -> bool:
    """Mark a live row deleted. The row leaves every pool; the purge removes it later."""
    cur = conn.execute(
        "UPDATE memories SET deleted_at = ?, rev = ?, updated_at = ? "
        "WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL",
        (now, rev, now, memory_id),
    )
    return cur.rowcount == 1


def purge_deleted(conn: sqlite3.Connection, grace_days: int, *, now: datetime | None = None) -> int:
    """Hard-delete rows soft-deleted more than ``grace_days`` ago.

    The cascade removes their vectors, events and rollup row. Runs in batches
    of ``BATCH_ROWS``, each in its own ``BEGIN IMMEDIATE`` transaction. Needs
    no revision stamp: the rows already left every pool (section 6.3).
    """
    cutoff = format_ts((now or datetime.now(timezone.utc)) - timedelta(days=grace_days))
    return _purge_batches(
        conn, "SELECT id FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
    )


def purge_archived(
    conn: sqlite3.Connection, retention_days: int, *, now: datetime | None = None
) -> int:
    """Hard-delete rows archived more than ``retention_days`` ago by a done dedup."""
    cutoff = format_ts((now or datetime.now(timezone.utc)) - timedelta(days=retention_days))
    return _purge_batches(
        conn,
        "SELECT m.id FROM memories m WHERE m.archived_at IS NOT NULL AND m.archived_at < ? "
        "AND m.deleted_at IS NULL AND EXISTS (SELECT 1 FROM dedup_actions d "
        "WHERE d.archived_id = m.id AND d.status = 'done' AND d.undone_at IS NULL)",
        (cutoff,),
    )


def _purge_batches(conn: sqlite3.Connection, select_sql: str, params: Sequence[object]) -> int:
    total = 0
    while True:
        with write_tx(conn):
            ids = [r[0] for r in conn.execute(f"{select_sql} LIMIT {BATCH_ROWS}", params)]
            if ids:
                conn.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in ids])
        total += len(ids)
        if len(ids) < BATCH_ROWS:
            return total


def foreign_key_problems(conn: sqlite3.Connection) -> list[tuple]:
    """Rows of ``PRAGMA foreign_key_check``. Empty means no orphan rows."""
    return [tuple(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]
