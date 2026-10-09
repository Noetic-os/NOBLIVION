# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import memories from a JSONL file: ``noblivion import FILE.jsonl``.

The input holds one JSON object per line. A blank line is skipped and is
not counted as invalid. The fields:

- ``content`` (required): the memory text, a non-empty string after strip;
- ``source_type``: ``transcript_mined`` (or its alias ``mined``) stores the
  row like a row of the transcript miner. Any other value, or none, makes a
  plain imported memory;
- ``category``: a string;
- ``labels``: a list of strings;
- ``created_at``, ``updated_at``: ISO-8601 times, kept on the row (a time
  without a zone is UTC). Default: now;
- ``pinned``: true or false.

Unknown fields are ignored. A line that is not a JSON object, or that fails
these rules, is counted and reported; it never stops the run.

Rows (see ``docs/import.md``):

- Every imported row has root ``db.IMPORT_ROOT``. No memory folder can have
  that root, and the indexer leaves the root alone, so a file scan never
  deletes, moves or rewrites an imported row.
- A plain imported memory is stored as ``claude_code_md``: recall treats it
  like a memory file in a shared root. Its content gets the layout of an
  indexed file: ``# <title>``, the ``[claude_code_md: <path>]`` marker line,
  then the rest of the text.
- A ``transcript_mined`` row keeps its text as it is. Recall returns it only
  on request, like a miner row.
- The path is the row hash: sha256 of the redacted content. So a second run
  inserts nothing, and the same content twice in one file is stored once.
- ``category``, when it is one of the indexer's file categories (feedback,
  project, reference, user), goes to the row's category column; else the
  column is ``reference``. A given category is also kept as the label
  ``category:<value>``.
- Labels, in this order, each once: the row's own labels, the ``--label``
  values, ``source:<value>`` (a source type the store does not know), then
  ``category:<value>``. A label the redactor would change is dropped.
- ``--archived`` sets ``archived_at``: the row stays out of recall, the
  duplicate sweep and the vector backfill. No purge removes it: the archive
  purge removes only rows that a done dedup action archived.
- Redaction: secrets, then injection patterns, as the miner does
  (``miner.scrub``). A line whose text gives the fail token is skipped.

Vectors: the command does not embed. Each write batch bumps
``content_rev``, and the running store embeds the new live rows on its next
backfill pass, with its own model and its own consent rules. The report
says how many inserted rows still wait for a vector.

Exit codes: 0 done; 1 refused (the file is missing or cannot be read, or
``--apply`` found no valid line); 2 usage error; 3 the database schema
refuses this tool; 4 another import holds ``import.lock``; 5 SQLite error;
6 no database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from noblivion import config, db, indexer, miner, redaction

IMPORT_LOCK_FILE = "import.lock"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_SCHEMA = 3
EXIT_LOCKED = 4
EXIT_DB_ERROR = 5
EXIT_NO_DB = 6

MINED_NAMES = frozenset({"transcript_mined", "mined"})
KNOWN_SOURCES = frozenset({db.SOURCE_MD, *MINED_NAMES})
COLUMN_CATEGORIES = frozenset({"feedback", "project", "reference", "user"})
CATEGORY_FALLBACK = "reference"
INVALID_LIST_MAX = 20
TITLE_MAX_CHARS = 120

EMBED_MODE = "store_backfill"


# -- parse and validate -------------------------------------------------------


class LineError(ValueError):
    """One input line breaks the format. The text is the reason."""


@dataclass
class Entry:
    """One valid input line, redacted and ready to store."""

    line_no: int
    source_type: str
    text: str  # the redacted content, stripped; its sha256 is the row hash
    category: str
    labels: list[str]
    created_at: str
    updated_at: str
    pinned: bool
    redacted: bool = False  # the redactor changed the text

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def path(self) -> str:
        return self.hash

    def content(self) -> str:
        """The stored text. A plain row gets the layout of an indexed file,
        so the recall hook reads it as one memory; a mined row stays as is."""
        if self.source_type == db.SOURCE_MINED or _has_marker(self.text):
            return self.text
        return render_plain(self.path, self.text)


def _has_marker(text: str) -> bool:
    return any(
        line.strip().startswith(("[claude_code_md:", "[transcript_mined:"))
        and line.strip().endswith("]")
        for line in text.splitlines()
    )


def render_plain(path: str, text: str) -> str:
    """``# <title>``, the marker line, the rest. The title is the first line
    without its ``#`` marks; a first line too long for a title stays in the body."""
    lines = text.split("\n")
    first = lines[0].strip()
    title = first.lstrip("#").strip()
    if title and len(title) <= TITLE_MAX_CHARS:
        body = "\n".join(lines[1:]).strip()
    else:
        cut = (title or first)[:TITLE_MAX_CHARS].rsplit(" ", 1)[0].rstrip()
        title = (cut or "imported memory") + "…"
        body = text
    parts = [f"# {title}", f"[{db.SOURCE_MD}: {path}]"]
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def parse_time(value: object, name: str, default: str) -> str:
    """An ISO-8601 time in the store format. A time without a zone is UTC."""
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise LineError(f"{name} is not an ISO-8601 time")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LineError(f"{name} is not an ISO-8601 time") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return db.format_ts(moment)


def _optional_str(doc: dict, name: str) -> str:
    value = doc.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LineError(f"{name} must be a string")
    return value.strip()


def merge_labels(*groups: Iterable[str]) -> tuple[list[str], int]:
    """The labels in order, each once. A label the redactor would change is
    dropped (the indexer's rule). Returns the labels and the dropped count."""
    out: list[str] = []
    dropped = 0
    for group in groups:
        for raw in group:
            label = raw.strip()
            if not label or label in out:
                continue
            if redaction.would_change(label):
                dropped += 1
                continue
            out.append(label)
    return out, dropped


@dataclass
class Report:
    file: str
    apply: bool
    archived: bool
    lines: int = 0
    blank: int = 0
    valid: int = 0
    invalid: int = 0
    invalid_lines: list[dict] = field(default_factory=list)
    redacted: int = 0  # valid lines whose text the redactor changed
    redaction_skipped: int = 0
    labels_dropped: int = 0
    duplicates_in_file: int = 0
    already_present: int = 0
    to_insert: int = 0
    inserted: int = 0
    by_source_type: Counter = field(default_factory=Counter)
    content_rev: int | None = None
    vectors_waiting: int | None = None
    archived_without_vector: int | None = None

    def note_invalid(self, line_no: int, reason: str) -> None:
        self.invalid += 1
        if len(self.invalid_lines) < INVALID_LIST_MAX:
            self.invalid_lines.append({"line": line_no, "reason": reason})

    def to_dict(self) -> dict:
        state = "archived" if self.archived else "live"
        return {
            "file": self.file,
            "mode": "apply" if self.apply else "dry_run",
            "lines": self.lines,
            "blank": self.blank,
            "valid": self.valid,
            "invalid": self.invalid,
            "invalid_lines": self.invalid_lines,
            "invalid_lines_shown_max": INVALID_LIST_MAX,
            "redacted": self.redacted,
            "redaction_skipped": self.redaction_skipped,
            "labels_dropped": self.labels_dropped,
            "duplicates_in_file": self.duplicates_in_file,
            "already_present": self.already_present,
            "would_insert": self.to_insert,
            "inserted": self.inserted,
            "by_source_type": {
                db.SOURCE_MD: self.by_source_type.get(db.SOURCE_MD, 0),
                db.SOURCE_MINED: self.by_source_type.get(db.SOURCE_MINED, 0),
            },
            "by_archived": {
                "archived": self.to_insert if self.archived else 0,
                "live": 0 if self.archived else self.to_insert,
            },
            "state": state,
            "root": db.IMPORT_ROOT,
            "content_rev": self.content_rev,
            "embedding": {
                "mode": EMBED_MODE,
                "vectors_waiting": self.vectors_waiting,
                "archived_without_vector": self.archived_without_vector,
            },
        }

    def summary(self) -> list[str]:
        d = self.to_dict()
        src = d["by_source_type"]
        mode = "apply" if self.apply else "dry run (nothing written; use --apply to write)"
        out = [
            f"mode: {mode}",
            f"lines={self.lines} blank={self.blank} valid={self.valid} invalid={self.invalid}",
            f"redacted={self.redacted} redaction_skipped={self.redaction_skipped} "
            f"labels_dropped={self.labels_dropped}",
            f"duplicates_in_file={self.duplicates_in_file} "
            f"already_present={self.already_present} would_insert={self.to_insert}",
            f"by_source_type: {db.SOURCE_MD}={src[db.SOURCE_MD]} "
            f"{db.SOURCE_MINED}={src[db.SOURCE_MINED]}",
            f"by_archived: archived={d['by_archived']['archived']} live={d['by_archived']['live']}",
        ]
        for item in self.invalid_lines:
            out.append(f"invalid line {item['line']}: {item['reason']}")
        if self.invalid > len(self.invalid_lines):
            out.append(f"... and {self.invalid - len(self.invalid_lines)} more invalid lines")
        if self.apply:
            out.append(f"inserted={self.inserted}")
            out.append(
                "vectors: this command does not embed; the running store embeds new live rows "
                f"on its next backfill pass. {self.vectors_waiting} inserted row(s) wait for "
                "a vector"
                + (
                    f"; {self.archived_without_vector} archived row(s) get no vector"
                    if self.archived_without_vector
                    else ""
                )
            )
        else:
            out.append(
                "vectors: this command does not embed; after --apply the running store "
                "embeds the new live rows on its next backfill pass"
            )
        return out


def parse_line(
    line_no: int,
    raw: bytes,
    *,
    extra_labels: Sequence[str],
    now: str,
    report: Report,
) -> Entry | None:
    """One line to an ``Entry``. ``None`` for a blank, invalid or unredactable line."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        report.note_invalid(line_no, "not UTF-8")
        return None
    if not text.strip():
        report.blank += 1
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        report.note_invalid(line_no, "not JSON")
        return None
    if not isinstance(doc, dict):
        report.note_invalid(line_no, "not a JSON object")
        return None
    try:
        entry, dropped = _entry(line_no, doc, extra_labels, now)
    except LineError as exc:
        report.note_invalid(line_no, str(exc))
        return None
    report.valid += 1
    if entry is None:
        report.redaction_skipped += 1
        return None
    report.labels_dropped += dropped
    return entry


def _entry(
    line_no: int, doc: dict, extra_labels: Sequence[str], now: str
) -> tuple[Entry | None, int]:
    content = doc.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LineError("content is missing or empty")
    source = _optional_str(doc, "source_type")
    category = _optional_str(doc, "category")
    raw_labels = doc.get("labels")
    if raw_labels is None:
        raw_labels = []
    if not isinstance(raw_labels, list) or not all(isinstance(x, str) for x in raw_labels):
        raise LineError("labels must be a list of strings")
    pinned = doc.get("pinned", False)
    if pinned is None:
        pinned = False
    if not isinstance(pinned, bool):
        raise LineError("pinned must be true or false")
    created_at = parse_time(doc.get("created_at"), "created_at", now)
    updated_at = parse_time(doc.get("updated_at"), "updated_at", now)

    clean = miner.scrub(content.strip())
    if clean == redaction.REDACTION_FAILED_TOKEN:
        return None, 0
    clean = clean.strip()
    if not clean:
        return None, 0
    mined = source.lower() in MINED_NAMES
    tail = []
    if source and source.lower() not in KNOWN_SOURCES:
        tail.append(f"source:{source}")
    if category:
        tail.append(f"category:{category}")
    labels, dropped = merge_labels(raw_labels, extra_labels, tail)
    entry = Entry(
        line_no=line_no,
        source_type=db.SOURCE_MINED if mined else db.SOURCE_MD,
        text=clean,
        category=category.lower() if category.lower() in COLUMN_CATEGORIES else CATEGORY_FALLBACK,
        labels=labels,
        created_at=created_at,
        updated_at=updated_at,
        pinned=pinned,
        redacted=clean != content.strip(),
    )
    return entry, dropped


def read_entries(
    path: Path, *, extra_labels: Sequence[str], report: Report, now: str
) -> list[Entry]:
    """Every valid line, in file order, the first of each hash only."""
    entries: list[Entry] = []
    seen: set[str] = set()
    with path.open("rb") as fh:
        for line_no, raw in enumerate(fh, start=1):
            report.lines += 1
            entry = parse_line(line_no, raw, extra_labels=extra_labels, now=now, report=report)
            if entry is None:
                continue
            if entry.redacted:
                report.redacted += 1
            if entry.hash in seen:
                report.duplicates_in_file += 1
                continue
            seen.add(entry.hash)
            entries.append(entry)
    return entries


# -- the store ----------------------------------------------------------------


def present_paths(conn: sqlite3.Connection, project: str) -> set[str]:
    """The paths (row hashes) of every imported row of a namespace."""
    with db.read_tx(conn):
        cur = conn.execute(
            "SELECT path FROM memories WHERE project = ? AND root = ?", (project, db.IMPORT_ROOT)
        )
        return {str(r[0]) for r in cur.fetchall()}


def plan(entries: Sequence[Entry], present: set[str], report: Report) -> list[Entry]:
    todo = [e for e in entries if e.path not in present]
    report.already_present = len(entries) - len(todo)
    report.to_insert = len(todo)
    report.by_source_type = Counter(e.source_type for e in todo)
    return todo


def store_entries(
    conn: sqlite3.Connection,
    project: str,
    entries: Sequence[Entry],
    report: Report,
    *,
    archived: bool,
) -> list[int]:
    """Insert in batches of ``db.BATCH_ROWS``; each batch is one ``BEGIN
    IMMEDIATE`` transaction that bumps ``content_rev`` once, like the miner.
    A row that appeared since the plan counts as already present."""
    ids: list[int] = []
    for start in range(0, len(entries), db.BATCH_ROWS):
        batch = entries[start : start + db.BATCH_ROWS]
        with db.write_tx(conn):
            todo = [e for e in batch if db.row_at(conn, project, db.IMPORT_ROOT, e.path) is None]
            report.already_present += len(batch) - len(todo)
            if not todo:
                continue
            rev = db.bump_rev(conn, "content_rev")
            now = db.utc_now()
            for e in todo:
                cur = conn.execute(
                    "INSERT INTO memories (project, root, path, source_type, category, "
                    "content, hash, pinned, labels, archived_at, rev, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project,
                        db.IMPORT_ROOT,
                        e.path,
                        e.source_type,
                        e.category,
                        e.content(),
                        e.hash,
                        1 if e.pinned else 0,
                        json.dumps(e.labels),
                        now if archived else None,
                        rev,
                        e.created_at,
                        e.updated_at,
                    ),
                )
                ids.append(int(cur.lastrowid))
            report.inserted += len(todo)
            report.content_rev = rev
    return ids


def count_without_vector(conn: sqlite3.Connection, ids: Sequence[int]) -> tuple[int, int]:
    """``(live, archived)``: the rows of ``ids`` that have no vector yet."""
    live = archived = 0
    with db.read_tx(conn):
        for start in range(0, len(ids), db.BATCH_ROWS):
            chunk = ids[start : start + db.BATCH_ROWS]
            marks = ",".join("?" * len(chunk))
            for (archived_at,) in conn.execute(
                f"SELECT m.archived_at FROM memories m WHERE m.id IN ({marks}) "
                "AND NOT EXISTS (SELECT 1 FROM vectors v WHERE v.memory_id = m.id)",
                chunk,
            ):
                if archived_at is None:
                    live += 1
                else:
                    archived += 1
    return live, archived


def _check_schema(conn: sqlite3.Connection) -> None:
    """A dry run reads only: it never creates or migrates a schema."""
    current, latest = db.user_version(conn), db.latest_version()
    if current > latest:
        raise db.SchemaTooNewError(
            f"database is newer than this NOBLIVION (schema {current}, code knows {latest})"
        )
    if current < latest:
        raise db.SchemaTooOldError(
            f"database schema {current} is older than {latest}; start the store once to migrate"
        )


def run(
    conn: sqlite3.Connection,
    path: Path,
    *,
    project: str,
    apply: bool,
    archived: bool = False,
    extra_labels: Sequence[str] = (),
) -> Report:
    """One import. ``apply=False`` reads the store and writes nothing."""
    report = Report(file=str(path), apply=apply, archived=archived)
    entries = read_entries(path, extra_labels=extra_labels, report=report, now=db.utc_now())
    todo = plan(entries, present_paths(conn, project), report)
    if apply and todo:
        ids = store_entries(conn, project, todo, report, archived=archived)
        report.vectors_waiting, report.archived_without_vector = count_without_vector(conn, ids)
    elif apply:
        report.vectors_waiting, report.archived_without_vector = 0, 0
    return report


# -- CLI ----------------------------------------------------------------------


def _label(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("a label must not be empty")
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion import",
        description="Import memories from a JSONL file. A dry run unless --apply.",
    )
    parser.add_argument("file", type=Path, help="the JSONL file, one JSON object per line")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print what would happen (default)")
    mode.add_argument("--apply", action="store_true", help="write the rows")
    parser.add_argument(
        "--label",
        action="append",
        type=_label,
        default=[],
        help="add this label to every row (repeat for more)",
    )
    parser.add_argument(
        "--archived", action="store_true", help="store the rows archived (out of recall)"
    )
    parser.add_argument("--db", type=Path, default=None, help="database file; default: data dir")
    parser.add_argument("--lock-timeout", type=float, default=0.0, help="seconds to wait")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _err(text: str) -> None:
    print(f"noblivion import: {text}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = config.load_settings()
    db_path = args.db or settings.db_path
    path: Path = args.file.expanduser()
    if not path.is_file():
        _err(f"no file at {path}")
        return EXIT_REFUSED
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        _err(f"cannot read {path} ({exc.strerror or type(exc).__name__})")
        return EXIT_REFUSED
    # Like the miner: this command never makes a database (NOBLIVION-28).
    if not db_path.is_file():
        _err(f"no database at {db_path}; run install.sh")
        return EXIT_NO_DB
    try:
        if args.apply:
            with indexer.index_lock(db_path.parent / IMPORT_LOCK_FILE, args.lock_timeout):
                report = _run_cli(db_path, path, settings.namespace, args)
        else:
            report = _run_cli(db_path, path, settings.namespace, args)
    except db.SchemaError as exc:
        _err(str(exc))
        return EXIT_SCHEMA
    except indexer.LockTimeoutError:
        _err(f"another import holds {IMPORT_LOCK_FILE}")
        return EXIT_LOCKED
    except db.DatabaseMissing:
        _err(f"no database at {db_path}; run install.sh")
        return EXIT_NO_DB
    except OSError as exc:
        _err(f"cannot read {path} ({exc.strerror or type(exc).__name__})")
        return EXIT_REFUSED
    except sqlite3.Error as exc:
        _err(f"database error ({exc}); try again later")
        return EXIT_DB_ERROR
    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        for line in report.summary():
            print(f"noblivion import: {line}")
    if args.apply and report.valid == 0:
        _err("the file has no valid line; nothing was written")
        return EXIT_REFUSED
    return EXIT_OK


def _run_cli(db_path: Path, path: Path, project: str, args: argparse.Namespace) -> Report:
    conn = db.connect(db_path)
    try:
        if args.apply:
            db.migrate(conn, db_path, allow_migrate=False)
        else:
            _check_schema(conn)
        return run(
            conn,
            path,
            project=project,
            apply=args.apply,
            archived=args.archived,
            extra_labels=args.label,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
