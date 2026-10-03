# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexer: copy memory files into ``memories`` rows (design doc 0001, section 5).

The memory files stay the source of truth. The indexer never writes a file.

One scan:

1. Find the memory folders (config ``memory_dirs``, default every
   ``~/.claude/projects/*/memory``). ``root`` is the name of the folder's parent.
2. List the top-level, non-empty ``*.md`` files of each folder and hash their
   raw bytes (sha256). A stat cache (size, mtime) skips the read of a file that
   did not change since the last scan of the same process.
3. Plan against the rows: same hash is a skip; a changed hash or ``force`` is an
   update in place (same id); a soft-deleted or archived row at the path comes
   back; a new file whose hash matches a row that is being deleted, is
   soft-deleted, or sits in a root whose folder is gone moves that row (id and
   trust history stay); else an insert. A live row whose file is gone is
   soft-deleted.
4. Guards (section 5.5) may cancel every delete of the scan: the shrink guard,
   the empty-folder rule, and a file that could not be read or redacted.
5. Write in batches of 200 rows, each in one ``BEGIN IMMEDIATE`` transaction
   that bumps ``meta.content_rev`` and stamps the changed rows.

Content is redacted (``noblivion.redaction``) before it is stored. A file whose
text cannot be redacted is never stored.

CLI: ``python -m noblivion.indexer [--force] [--allow-shrink]`` (also
``noblivion index``). Exit codes: 0 done; 1 some files were skipped (not
readable or not redactable) so nothing was deleted; 2 the shrink guard blocked
the deletes; 3 the database schema refuses this tool; 4 another scan holds the
index lock; 5 SQLite stayed locked past its busy timeout, or another SQLite
error (one line on stderr, no traceback).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noblivion import config, db, redaction

CATEGORIES = frozenset({"feedback", "project", "reference", "topic", "user"})
INDEX_FILES = frozenset({"MEMORY.md", "MEMORY_ARCHIVE.md"})
CATEGORY_FALLBACK = "reference"
NO_LABEL_CATEGORIES = frozenset({"index", "topic"})

SHRINK_FRACTION = 0.10
SMALL_ROOT_ROWS = 10
SMALL_ROOT_MAX_DELETES = 1

EXIT_OK = 0
EXIT_SKIPPED = 1
EXIT_BLOCKED = 2
EXIT_SCHEMA = 3
EXIT_LOCKED = 4
EXIT_DB_BUSY = 5  # SQLite stayed locked past busy_timeout, or another SQLite error
EXIT_NO_DB = 6  # no database, and install.sh has not run for this data dir (NOBLIVION-28)

Labeller = Callable[[str, str], Iterable[str]]  # (file text, file stem) -> labels
StatCache = dict[str, tuple[int, int, "str | None"]]
# A file whose mtime is this close to the scan is not put in the stat cache. A
# rewrite with the same size inside one tick of the file system clock keeps
# the old mtime, so a cached entry would hide the new text until the next
# change ("racy" entries, as in git). Two seconds covers coarse clocks too.
STAT_CACHE_MIN_AGE_NS = 2_000_000_000

# -- frontmatter and content ---------------------------------------------------

_FM_KEY_RE = re.compile(r"^([A-Za-z_][\w\-]*):\s*(.*)$")


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split ``text`` into ``(frontmatter, body)``.

    Hand-written, not YAML: top-level ``key: value`` lines and one level of
    indented nesting, quotes stripped. A file that does not start with a
    ``---`` line has no frontmatter. A file with no closing ``---`` is all body.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, object] = {}
    parent: str | None = None
    end = None
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            end = i
            break
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            m = _FM_KEY_RE.match(line.strip())
            if parent is not None and m:
                sub = meta.get(parent)
                if not isinstance(sub, dict):
                    sub = {}
                    meta[parent] = sub
                sub[m.group(1)] = _unquote(m.group(2))
            continue
        m = _FM_KEY_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value.strip() == "":
            parent = key
            meta.setdefault(key, {})
        else:
            parent = None
            meta[key] = _unquote(value)
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def frontmatter_type(meta: dict[str, object]) -> str:
    for source in (meta, meta.get("metadata")):
        if isinstance(source, dict):
            value = source.get("type")
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


def category_for(file_name: str, meta: dict[str, object]) -> str:
    """Index files, then the file name prefix, then the frontmatter type."""
    if file_name in INDEX_FILES:
        return "index"
    prefix = file_name.split("_", 1)[0].lower() if "_" in file_name else ""
    if prefix in CATEGORIES:
        return prefix
    fm_type = frontmatter_type(meta)
    if fm_type in CATEGORIES:
        return fm_type
    return CATEGORY_FALLBACK


@dataclass(frozen=True)
class ParsedFile:
    name: str
    description: str
    category: str
    body: str


def parse_file(file_name: str, text: str) -> ParsedFile:
    meta, body = parse_frontmatter(text)
    name = meta.get("name")
    desc = meta.get("description")
    stem = file_name[:-3] if file_name.endswith(".md") else file_name
    return ParsedFile(
        name=(name.strip() if isinstance(name, str) else "") or stem,
        description=desc.strip() if isinstance(desc, str) else "",
        category=category_for(file_name, meta),
        body=body,
    )


def build_content(path: str, parsed: ParsedFile) -> str:
    """The stored text (section 5.3). The hooks parse it, so it is fixed."""
    parts = [f"# {parsed.name}"]
    if parsed.description:
        parts.append(parsed.description)
    parts.append(f"[{db.SOURCE_MD}: {path}]")
    if parsed.body.strip():
        parts.append(parsed.body.rstrip())
    return "\n\n".join(parts)


def labels_json(labels: Iterable[str] | None, category: str) -> str:
    """Labels as a JSON list. A label the redactor would change is dropped."""
    if labels is None or category in NO_LABEL_CATEGORIES:
        return "[]"
    kept = sorted({lbl for lbl in labels if isinstance(lbl, str) and lbl.strip()})
    return json.dumps([lbl for lbl in kept if not redaction.would_change(lbl)])


# -- folders -------------------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    root: str
    path: str  # file name relative to the memory folder
    abspath: Path
    hash: str


@dataclass
class Folder:
    root: str
    path: Path
    exists: bool
    files: list[FileEntry] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _wanted(p: Path) -> bool:
    return p.suffix == ".md" and not p.name.startswith(".") and p.is_file()


def read_folder(root: str, folder: Path, stat_cache: StatCache | None = None) -> Folder:
    """List and hash the top-level ``*.md`` files. Skips empty files."""
    if not folder.is_dir():
        return Folder(root, folder, exists=False)
    result = Folder(root, folder, exists=True)
    cache_before_ns = time.time_ns() - STAT_CACHE_MIN_AGE_NS
    for p in sorted(folder.iterdir(), key=lambda q: q.name):
        try:
            if not _wanted(p):
                continue
            st = p.stat()
            key = str(p)
            cached = stat_cache.get(key) if stat_cache is not None else None
            if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                digest = cached[2]
            else:
                raw = p.read_bytes()
                digest = hashlib.sha256(raw).hexdigest() if raw.strip() else None
                if stat_cache is not None:
                    if st.st_mtime_ns < cache_before_ns:
                        stat_cache[key] = (st.st_size, st.st_mtime_ns, digest)
                    else:
                        stat_cache.pop(key, None)  # too new to trust: hash it again next scan
        except OSError:
            result.unreadable.append(p.name)
            continue
        if digest is not None:
            result.files.append(FileEntry(root, p.name, p, digest))
    return result


def discover(dirs: Sequence[Path], stat_cache: StatCache | None = None) -> list[Folder]:
    """One ``Folder`` per root. The first folder wins when two share a root."""
    folders: list[Folder] = []
    seen: set[str] = set()
    for d in dirs:
        folder = Path(os.path.expanduser(str(d)))
        root = folder.parent.name
        if not root or root in seen:
            print(f"noblivion index: skip {folder}: root {root!r} is used twice", file=sys.stderr)
            continue
        seen.add(root)
        folders.append(read_folder(root, folder, stat_cache))
    return folders


# -- plan ----------------------------------------------------------------------


@dataclass(frozen=True)
class Op:
    kind: str  # insert | update | revive | unarchive | move | delete
    entry: FileEntry | None = None
    row_id: int | None = None


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    unchanged: int = 0
    blocked: bool = False
    block_reason: str = ""
    deletes_cancelled: int = 0

    def count(self, kind: str) -> int:
        return sum(1 for op in self.ops if op.kind == kind)


def shrink_limit_exceeded(deletes: int, live: int) -> bool:
    """True when ``deletes`` of ``live`` rows is too many for one scan."""
    if live == 0 or deletes == 0:
        return False
    if live < SMALL_ROOT_ROWS:
        return deletes > SMALL_ROOT_MAX_DELETES
    return deletes > SHRINK_FRACTION * live


def compute_plan(
    folders: Sequence[Folder],
    rows: Sequence[db.MemoryRow],
    *,
    force: bool = False,
    allow_shrink: bool = False,
    grace_cutoff: str | None = None,
) -> Plan:
    plan = Plan()
    by_key = {(r.root, r.path): r for r in rows}
    present_roots = {f.root for f in folders if f.exists and f.files}
    missing_roots = {f.root for f in folders if not f.exists} | (
        {r.root for r in rows} - {f.root for f in folders}
    )

    # Live rows whose file is gone, in roots with a non-empty folder.
    delete_candidates: list[db.MemoryRow] = []
    wanted = {(e.root, e.path) for f in folders for e in f.files}
    for r in rows:
        if r.live and r.root in present_roots and (r.root, r.path) not in wanted:
            delete_candidates.append(r)

    # Rows a new file with the same bytes may take over, best first.
    move_pool: dict[str, list[db.MemoryRow]] = {}
    for r in delete_candidates:
        move_pool.setdefault(r.hash, []).append(r)
    for r in rows:
        if r.deleted_at is not None and r.archived_at is None:
            if grace_cutoff is None or r.deleted_at >= grace_cutoff:
                move_pool.setdefault(r.hash, []).append(r)
    for r in rows:
        if r.live and r.root in missing_roots:
            move_pool.setdefault(r.hash, []).append(r)
    claimed: set[int] = set()

    for folder in folders:
        for entry in folder.files:
            row = by_key.get((entry.root, entry.path))
            if row is not None:
                if row.deleted_at is not None:
                    plan.ops.append(Op("revive", entry, row.id))
                elif row.archived_at is not None:
                    plan.ops.append(Op("unarchive", entry, row.id))
                elif force or row.hash != entry.hash:
                    plan.ops.append(Op("update", entry, row.id))
                else:
                    plan.unchanged += 1
                continue
            candidate = next(
                (c for c in move_pool.get(entry.hash, ()) if c.id not in claimed), None
            )
            if candidate is not None:
                claimed.add(candidate.id)
                plan.ops.append(Op("move", entry, candidate.id))
            else:
                plan.ops.append(Op("insert", entry))

    deletes = [r for r in delete_candidates if r.id not in claimed]
    if deletes and not allow_shrink:
        live_by_root = Counter(r.root for r in rows if r.live)
        del_by_root = Counter(r.root for r in deletes)
        over = [
            root for root, n in sorted(del_by_root.items())
            if shrink_limit_exceeded(n, live_by_root[root])
        ]  # fmt: skip
        total_live = sum(live_by_root.values())
        if over or shrink_limit_exceeded(len(deletes), total_live):
            plan.blocked = True
            where = f"roots {', '.join(over)}" if over else "all roots"
            plan.block_reason = (
                f"shrink guard: the scan would delete {len(deletes)} of {total_live} live rows "
                f"({where}); run `noblivion index --allow-shrink` to accept"
            )
            plan.deletes_cancelled = len(deletes)
            deletes = []
    plan.ops.extend(Op("delete", row_id=r.id) for r in deletes)
    return plan


# -- apply ---------------------------------------------------------------------


@dataclass
class ScanResult:
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    unarchived: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""
    deletes_cancelled: int = 0
    content_rev: int = 0

    @property
    def changed(self) -> int:
        return (
            self.inserted + self.updated + self.revived + self.unarchived + self.moved
        ) + self.deleted

    def to_dict(self) -> dict[str, object]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "revived": self.revived,
            "unarchived": self.unarchived,
            "moved": self.moved,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "skipped": list(self.skipped),
            "index_blocked": self.blocked,
            "block_reason": self.block_reason,
            "deletes_cancelled": self.deletes_cancelled,
            "content_rev": self.content_rev,
        }

    def summary(self) -> str:
        line = (
            f"inserted={self.inserted} updated={self.updated} moved={self.moved} "
            f"revived={self.revived + self.unarchived} deleted={self.deleted} "
            f"unchanged={self.unchanged} skipped={len(self.skipped)} "
            f"content_rev={self.content_rev}"
        )
        return line + (" index_blocked=true" if self.blocked else "")


@dataclass(frozen=True)
class _Prepared:
    entry: FileEntry
    category: str
    content: str
    labels: str


def _prepare(entry: FileEntry, labeller: Labeller | None) -> _Prepared | None:
    """Read, parse and redact one file. ``None`` when it must not be stored."""
    try:
        raw = entry.abspath.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    # The file may have changed since it was listed; store what is on disk now.
    entry = FileEntry(entry.root, entry.path, entry.abspath, hashlib.sha256(raw).hexdigest())
    text = raw.decode("utf-8", errors="replace")
    parsed = parse_file(entry.path, text)
    content = redaction.redact_at_rest(build_content(entry.path, parsed))
    if content == redaction.REDACTION_FAILED_TOKEN:
        return None
    labels = None
    if labeller is not None:
        try:
            labels = list(labeller(text, Path(entry.path).stem))
        except Exception:  # noqa: BLE001 - one labeller fault never stops the scan
            labels = None
    return _Prepared(entry, parsed.category, content, labels_json(labels, parsed.category))


def _apply_batch(
    conn: sqlite3.Connection,
    project: str,
    batch: Sequence[tuple[Op, _Prepared | None]],
    result: ScanResult,
    *,
    force: bool,
) -> None:
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "content_rev")
        now = db.utc_now()
        for op, prep in batch:
            if op.kind == "delete":
                if op.row_id is not None and db.soft_delete(conn, op.row_id, rev=rev, now=now):
                    result.deleted += 1
                continue
            assert prep is not None
            entry = prep.entry
            fields = {
                "root": entry.root,
                "path": entry.path,
                "category": prep.category,
                "content": prep.content,
                "content_hash": entry.hash,
                "labels": prep.labels,
                "rev": rev,
                "now": now,
            }
            # Re-check under the write lock: another writer may have changed
            # the row since the plan was made.
            current = db.row_at(conn, project, entry.root, entry.path)
            target = op.row_id
            kind = op.kind
            if current is not None and current.id != target:
                if current.live and current.hash == entry.hash and not force:
                    result.unchanged += 1
                    continue
                target = current.id
                kind = "update" if current.live else "revive"
            if target is not None and db.rewrite_memory(conn, target, **fields):
                attr = {"update": "updated", "revive": "revived", "unarchive": "unarchived"}
                name = attr.get(kind, "moved")
                setattr(result, name, getattr(result, name) + 1)
                continue
            db.insert_memory(conn, project=project, source_type=db.SOURCE_MD, **fields)
            result.inserted += 1
        result.content_rev = rev


def scan(
    conn: sqlite3.Connection,
    memory_dirs: Sequence[Path],
    *,
    project: str = config.DEFAULT_NAMESPACE,
    force: bool = False,
    allow_shrink: bool = False,
    delete_grace_days: int = config.DEFAULT_DELETE_GRACE_DAYS,
    labeller: Labeller | None = None,
    stat_cache: StatCache | None = None,
) -> ScanResult:
    """Run one index scan and return what it did."""
    folders = discover(memory_dirs, stat_cache)
    with db.read_tx(conn):
        rows = db.md_rows(conn, project)
        stored_version = db.get_meta(conn, "redactor_version")
        content_rev = db.revisions(conn)[0]
    if stored_version != redaction.REDACTOR_VERSION and rows:
        force = True  # rows written by another redactor version: redo them all
    cutoff = db.format_ts(datetime.now(timezone.utc) - timedelta(days=delete_grace_days))
    plan = compute_plan(folders, rows, force=force, allow_shrink=allow_shrink, grace_cutoff=cutoff)

    result = ScanResult(
        unchanged=plan.unchanged,
        blocked=plan.blocked,
        block_reason=plan.block_reason,
        deletes_cancelled=plan.deletes_cancelled,
        content_rev=content_rev,
    )
    for folder in folders:
        result.skipped.extend(f"{folder.root}/{name}" for name in folder.unreadable)

    work: list[tuple[Op, _Prepared | None]] = []
    for op in plan.ops:
        if op.kind == "delete":
            work.append((op, None))
            continue
        assert op.entry is not None
        prep = _prepare(op.entry, labeller)
        if prep is None:
            result.skipped.append(f"{op.entry.root}/{op.entry.path}")
            if stat_cache is not None:
                stat_cache.pop(str(op.entry.abspath), None)  # retry it next scan
            continue
        work.append((op, prep))

    if result.skipped:
        # A file that cannot be read or redacted: delete nothing this run.
        cancelled = sum(1 for op, _ in work if op.kind == "delete")
        result.deletes_cancelled += cancelled
        work = [(op, prep) for op, prep in work if op.kind != "delete"]

    for start in range(0, len(work), db.BATCH_ROWS):
        _apply_batch(conn, project, work[start : start + db.BATCH_ROWS], result, force=force)

    if stored_version != redaction.REDACTOR_VERSION and not result.skipped:
        with db.write_tx(conn):
            db.set_meta(conn, "redactor_version", redaction.REDACTOR_VERSION)
    return result


# -- lock and CLI -------------------------------------------------------------


class LockTimeoutError(RuntimeError):
    pass


@contextmanager
def index_lock(path: Path, timeout_s: float = 60.0) -> Iterator[None]:
    """Hold ``flock`` on ``index.lock``: one scan at a time across processes.

    It never makes the folder (NOBLIVION-28): a missing data dir raises
    ``FileNotFoundError``."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"another scan holds {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion index", description="Index Claude Code memory files into the store."
    )
    parser.add_argument("--force", action="store_true", help="rewrite every row")
    parser.add_argument(
        "--allow-shrink", action="store_true", help="accept a scan that deletes many rows"
    )
    parser.add_argument(
        "--memory-dir",
        action="append",
        type=Path,
        default=None,
        help="memory folder to scan (repeat for more); default: the config",
    )
    parser.add_argument("--db", type=Path, default=None, help="database file; default: data dir")
    parser.add_argument("--lock-timeout", type=float, default=60.0, help="seconds to wait")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = config.load_settings()
    db_path = args.db or settings.db_path
    lock_path = db_path.parent / config.INDEX_LOCK_FILE
    dirs = args.memory_dir if args.memory_dir else settings.resolved_memory_dirs()
    from noblivion import labels  # labels loads the hook rules by path

    labeller = labels.make_labeller(settings)
    if labeller is None:
        msg = f"label rules {labels.RULES_FILE} not found or broken; rows get no labels"
        print(f"noblivion index: {msg}", file=sys.stderr)
    # Only the first index of a new install makes the database: install.sh
    # writes its stamp before it runs this command. A later run (the memory
    # sync hook) never makes a data dir that an uninstall deleted
    # (NOBLIVION-28). An explicit --db is the caller's own choice.
    create = args.db is not None or config.is_installed(settings.data_dir)
    if not create and not db_path.is_file():
        print(f"noblivion index: no database at {db_path}; run install.sh", file=sys.stderr)
        return EXIT_NO_DB
    try:
        if create:
            db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with index_lock(lock_path, args.lock_timeout):
            conn = db.open_db(db_path, allow_migrate=False, create=create)
            try:
                result = scan(
                    conn,
                    dirs,
                    project=settings.namespace,
                    force=args.force,
                    allow_shrink=args.allow_shrink,
                    delete_grace_days=settings.delete_grace_days,
                    labeller=labeller,
                )
            finally:
                conn.close()
    except db.SchemaError as exc:
        print(f"noblivion index: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except LockTimeoutError as exc:
        print(f"noblivion index: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except FileNotFoundError:
        print(f"noblivion index: no database at {db_path}; run install.sh", file=sys.stderr)
        return EXIT_NO_DB
    except sqlite3.OperationalError as exc:
        # One line, no traceback: "database is locked" after busy_timeout.
        print(f"noblivion index: database error ({exc}); try again later", file=sys.stderr)
        return EXIT_DB_BUSY

    print(json.dumps(result.to_dict()) if args.json else f"noblivion index: {result.summary()}")
    for name in result.skipped:
        print(f"noblivion index: skipped {name} (not readable or not redactable)", file=sys.stderr)
    if result.blocked:
        print(f"noblivion index: {result.block_reason}", file=sys.stderr)
        return EXIT_BLOCKED
    return EXIT_SKIPPED if result.skipped else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
