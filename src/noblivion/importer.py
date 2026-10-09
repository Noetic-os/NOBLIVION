# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import memories from a JSONL file: ``noblivion import FILE.jsonl``.

The input holds one JSON object per line. A blank line is skipped and is
not counted as invalid. A UTF-8 byte order mark at the start of the file is
accepted. The fields:

- ``content`` (required): the memory text, a non-empty string after strip,
  at most ``indexer.MAX_FILE_BYTES`` (256 KB) after redaction;
- ``source_type``: ``transcript_mined`` (or its alias ``mined``) stores the
  row like a row of the transcript miner. Any other value, or none, makes a
  plain imported memory;
- ``category``: a string;
- ``labels``: a list of strings;
- ``created_at``, ``updated_at``: times, kept on the row. Default: now.
  Accepted forms (``parse_time``): ``YYYY-MM-DD``, or ``YYYY-MM-DD`` then
  ``T`` or a space, then ``HH:MM``, ``HH:MM:SS`` or ``HH:MM:SS.f`` (1 to 9
  fraction digits), then an optional zone: ``Z``, ``+HH``, ``+HHMM`` or
  ``+HH:MM`` (or ``-``). No zone means UTC. The parser is our own, so every
  Python version reads a file the same way;
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
  then the rest of the text. A text that already holds a
  ``[claude_code_md: ...]`` line is stored as it is.
- A ``transcript_mined`` row keeps its text as it is. Recall returns it only
  on request, like a miner row.
- The path is the row hash: sha256 of the redacted content. So a second run
  inserts nothing, and the same content twice in one file is stored once.
  A row that ``--remove`` soft-deleted comes back (same id) on a new import.
- ``category``, when it is one of the indexer's file categories (feedback,
  project, reference, user), goes to the row's category column; else the
  column is ``reference``. A given category is also kept as the label
  ``category:<value>``.
- Labels, in this order, each once: the row's own labels, the ``--label``
  values, ``source:<value>`` (a source type the store does not know), then
  ``category:<value>``. A label the redactor would change is dropped.
- ``--archived`` sets ``archived_at``: the row stays out of recall, the
  duplicate sweep and the vector backfill. The archive purge never removes
  it: it removes only rows that a done dedup action archived.
- Redaction: secrets, then injection patterns, as the miner does
  (``miner.scrub``). A line whose text gives the fail token is skipped.

``--remove``: read the file with the same rules and soft-delete the
imported rows with the same hashes (archived ones too). The delete purge
(``db.purge_deleted``) removes them after the grace period.

Vectors: the command does not embed. Each write batch bumps
``content_rev``, and the running store embeds the new live rows on its next
backfill pass, with its own model and its own consent rules. The report
says how many written rows still wait for a vector, plain and mined rows
apart: with a backend that sends text off the machine, the backfill embeds
mined rows only when ``embedding.remote_include_mined`` is true.

Exit codes: 0 done; 1 refused (the file is missing or cannot be read, the
lock file cannot be opened, or ``--apply`` found no line it can import or
remove); 2 usage error; 3 the database schema refuses this tool; 4 another
import holds ``import.lock``; 5 SQLite error; 6 no database.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noblivion import config, db, embedding, indexer, miner, redaction

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
MAX_CONTENT_BYTES = indexer.MAX_FILE_BYTES
BOM = b"\xef\xbb\xbf"

EMBED_MODE = "store_backfill"

_TIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?)?",
    re.ASCII,
)
_MD_MARKER = f"[{db.SOURCE_MD}:"


# -- parse and validate -------------------------------------------------------


class LineError(ValueError):
    """One input line breaks the format. The text is the reason."""


class InputReadError(RuntimeError):
    """The input file cannot be read."""


class LockFileError(RuntimeError):
    """The lock file in the data dir cannot be opened."""


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
        if self.source_type == db.SOURCE_MINED or _has_md_marker(self.text):
            return self.text
        return render_plain(self.path, self.text)


def _has_md_marker(text: str) -> bool:
    """True when a line is a ``[claude_code_md: ...]`` marker. Only that
    marker makes the recall hook read the entry as a memory, so any other
    text (a ``[transcript_mined: ...]`` line too) gets the plain layout."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(_MD_MARKER) and line.endswith("]"):
            return True
    return False


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


def _zone(text: str | None) -> timezone:
    if not text or text in ("Z", "z"):
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:] or 0)
    if hours > 23 or minutes > 59:
        raise ValueError("zone out of range")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_time(value: object, name: str, default: str) -> str:
    """A time in the store format (UTC, ``db.format_ts``). See the module
    docstring for the accepted forms. A time the store cannot write and read
    back (out of range) is refused too."""
    if value is None:
        return default
    match = _TIME_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise LineError(f"{name} is not an ISO-8601 time")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            int((fraction or "0")[:6].ljust(6, "0")),
            tzinfo=_zone(zone),
        )
        utc = moment.astimezone(timezone.utc)
        # The store format needs a four-digit year. strftime pads a year
        # below 1000 on some platforms and not on others, so check it here.
        if not 1000 <= utc.year <= 9999:
            raise ValueError("year out of range")
        text = db.format_ts(utc)
    except (ValueError, OverflowError) as exc:
        raise LineError(f"{name} is out of range") from exc
    return text


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
    archived: bool = False
    remove: bool = False
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
    to_insert: int = 0  # new rows
    to_revive: int = 0  # rows that --remove soft-deleted, written again
    inserted: int = 0  # new rows only
    revived: int = 0
    to_remove: int = 0
    removed: int = 0
    not_present: int = 0
    already_removed: int = 0
    by_source_type: Counter = field(default_factory=Counter)
    content_rev: int | None = None
    embed_backend: str = ""  # the configured backend; "" when not known
    mined_embedded: bool = True  # the backfill embeds mined rows
    vectors_waiting: int | None = None  # live plain rows written without a vector
    mined_vectors_waiting: int | None = None  # live mined rows written without a vector
    archived_without_vector: int | None = None
    refused: str = ""

    @property
    def to_write(self) -> int:
        """New rows plus revived rows."""
        return self.to_insert + self.to_revive

    @property
    def usable(self) -> int:
        """Valid lines that redaction did not skip."""
        return self.valid - self.redaction_skipped

    def note_invalid(self, line_no: int, reason: str) -> None:
        self.invalid += 1
        if len(self.invalid_lines) < INVALID_LIST_MAX:
            self.invalid_lines.append({"line": line_no, "reason": reason})

    def to_dict(self) -> dict:
        out = {
            "file": self.file,
            "action": "remove" if self.remove else "import",
            "mode": "apply" if self.apply else "dry_run",
            "lines": self.lines,
            "blank": self.blank,
            "valid": self.valid,
            "invalid": self.invalid,
            "invalid_lines": self.invalid_lines,
            "invalid_lines_shown_max": INVALID_LIST_MAX,
            "redacted": self.redacted,
            "redaction_skipped": self.redaction_skipped,
            "duplicates_in_file": self.duplicates_in_file,
            "root": db.IMPORT_ROOT,
            "content_rev": self.content_rev,
            "refused": self.refused or None,
        }
        if self.remove:
            out.update(
                {
                    "would_remove": self.to_remove,
                    "removed": self.removed,
                    "not_present": self.not_present,
                    "already_removed": self.already_removed,
                }
            )
            return out
        out.update(
            {
                "labels_dropped": self.labels_dropped,
                "already_present": self.already_present,
                "would_insert": self.to_insert,
                "would_revive": self.to_revive,
                "inserted": self.inserted,
                "revived": self.revived,
                "by_source_type": {
                    db.SOURCE_MD: self.by_source_type.get(db.SOURCE_MD, 0),
                    db.SOURCE_MINED: self.by_source_type.get(db.SOURCE_MINED, 0),
                },
                "by_archived": {
                    "archived": self.to_write if self.archived else 0,
                    "live": 0 if self.archived else self.to_write,
                },
                "embedding": {
                    "mode": EMBED_MODE,
                    "backend": self.embed_backend or None,
                    "mined_embedded": self.mined_embedded,
                    "vectors_waiting": self.vectors_waiting,
                    "mined_vectors_waiting": self.mined_vectors_waiting,
                    "archived_without_vector": self.archived_without_vector,
                },
            }
        )
        return out

    def summary(self) -> list[str]:
        d = self.to_dict()
        action = "remove" if self.remove else "import"
        if self.apply:
            mode = f"{action}, apply"
        else:
            mode = f"{action}, dry run (nothing written; use --apply to write)"
        out = [
            f"mode: {mode}",
            f"lines={self.lines} blank={self.blank} valid={self.valid} invalid={self.invalid}",
            f"redacted={self.redacted} redaction_skipped={self.redaction_skipped}",
        ]
        if self.remove:
            out.append(
                f"duplicates_in_file={self.duplicates_in_file} would_remove={self.to_remove} "
                f"not_present={self.not_present} already_removed={self.already_removed}"
            )
        else:
            src = d["by_source_type"]
            out += [
                f"labels_dropped={self.labels_dropped} duplicates_in_file="
                f"{self.duplicates_in_file} already_present={self.already_present} "
                f"would_insert={self.to_insert} would_revive={self.to_revive}",
                f"by_source_type: {db.SOURCE_MD}={src[db.SOURCE_MD]} "
                f"{db.SOURCE_MINED}={src[db.SOURCE_MINED]}",
                f"by_archived: archived={d['by_archived']['archived']} "
                f"live={d['by_archived']['live']}",
            ]
        for item in self.invalid_lines:
            out.append(f"invalid line {item['line']}: {item['reason']}")
        if self.invalid > len(self.invalid_lines):
            out.append(f"... and {self.invalid - len(self.invalid_lines)} more invalid lines")
        if self.refused:
            return out
        if self.remove:
            if self.apply:
                out.append(
                    f"removed={self.removed}; the delete purge removes the rows after the "
                    "grace period"
                )
            return out
        if self.apply:
            out.append(f"new={self.inserted} revived={self.revived}")
        out.append(self.vector_line())
        return out

    def vector_line(self) -> str:
        if self.embed_backend == "none":
            return (
                "vectors: the embedding backend is none, so no row gets a vector; "
                "recall finds the rows by keyword"
            )
        head = "vectors: this command does not embed; the running store embeds"
        if not self.apply:
            mined = "" if self.mined_embedded else " (plain rows only: " + _MINED_NOTE + ")"
            return f"{head} the new live rows on its next backfill pass{mined}"
        parts = [f"{self.vectors_waiting} plain row(s) wait for a vector"]
        if self.mined_embedded:
            parts.append(f"{self.mined_vectors_waiting} mined row(s) wait for a vector")
        else:
            parts.append(f"{self.mined_vectors_waiting} mined row(s) get no vector: {_MINED_NOTE}")
        if self.archived_without_vector:
            parts.append(f"{self.archived_without_vector} archived row(s) get no vector")
        return f"{head} live rows on its next backfill pass. " + "; ".join(parts)


_MINED_NOTE = (
    "a backend that sends text off the machine skips mined rows unless "
    "embedding.remote_include_mined is true"
)


def parse_line(
    line_no: int,
    raw: bytes,
    *,
    extra_labels: Sequence[str],
    now: str,
    report: Report,
) -> Entry | None:
    """One line to an ``Entry``. ``None`` for a blank, invalid or unredactable line."""
    if line_no == 1 and raw.startswith(BOM):
        raw = raw[len(BOM) :]
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
    except (ValueError, RecursionError):
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


def _check_size(text: str) -> None:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_CONTENT_BYTES:
        raise LineError(f"content larger than {MAX_CONTENT_BYTES // 1024} KB")


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

    # The cap twice: before the redactor, so a huge line costs no scrub,
    # and after it, because a mask can be longer than the text it hides.
    _check_size(content.strip())
    clean = miner.scrub(content.strip())
    if clean == redaction.REDACTION_FAILED_TOKEN:
        return None, 0
    clean = clean.strip()
    if not clean:
        return None, 0
    _check_size(clean)
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
    try:
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
    except OSError as exc:
        raise InputReadError(f"cannot read {path} ({exc.strerror or type(exc).__name__})") from exc
    return entries


# -- the store ----------------------------------------------------------------


def present_rows(conn: sqlite3.Connection, project: str) -> dict[str, bool]:
    """``{path: soft-deleted}`` for every imported row of a namespace."""
    with db.read_tx(conn):
        cur = conn.execute(
            "SELECT path, deleted_at FROM memories WHERE project = ? AND root = ?",
            (project, db.IMPORT_ROOT),
        )
        return {str(r[0]): r[1] is not None for r in cur.fetchall()}


def plan(entries: Sequence[Entry], present: dict[str, bool], report: Report) -> list[Entry]:
    """The entries to write: new hashes, and hashes whose row is soft-deleted."""
    todo = [e for e in entries if present.get(e.path) is not False]  # new, or soft-deleted
    report.already_present = len(entries) - len(todo)
    report.to_revive = sum(1 for e in todo if e.path in present)
    report.to_insert = len(todo) - report.to_revive
    report.by_source_type = Counter(e.source_type for e in todo)
    return todo


def _row_values(e: Entry, archived_at: str | None, rev: int) -> tuple:
    return (
        e.source_type,
        e.category,
        e.content(),
        e.hash,
        1 if e.pinned else 0,
        json.dumps(e.labels),
        archived_at,
        rev,
        e.created_at,
        e.updated_at,
    )


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
    A live row that appeared since the plan counts as already present; a
    soft-deleted row at the key is revived in place (same id)."""
    ids: list[int] = []
    for start in range(0, len(entries), db.BATCH_ROWS):
        batch = entries[start : start + db.BATCH_ROWS]
        with db.write_tx(conn):
            current = {e.path: db.row_at(conn, project, db.IMPORT_ROOT, e.path) for e in batch}
            todo = [e for e in batch if current[e.path] is None or current[e.path].deleted_at]
            report.already_present += len(batch) - len(todo)
            if not todo:
                continue
            rev = db.bump_rev(conn, "content_rev")
            now = db.utc_now()
            archived_at = now if archived else None
            for e in todo:
                row = current[e.path]
                if row is not None:
                    conn.execute(
                        "UPDATE memories SET source_type = ?, category = ?, content = ?, "
                        "hash = ?, pinned = ?, labels = ?, archived_at = ?, deleted_at = NULL, "
                        "rev = ?, created_at = ?, updated_at = ? WHERE id = ?",
                        (*_row_values(e, archived_at, rev), row.id),
                    )
                    ids.append(row.id)
                    report.revived += 1
                    continue
                cur = conn.execute(
                    "INSERT INTO memories (project, root, path, source_type, category, "
                    "content, hash, pinned, labels, archived_at, rev, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project, db.IMPORT_ROOT, e.path, *_row_values(e, archived_at, rev)),
                )
                ids.append(int(cur.lastrowid))
            report.inserted += len(todo) - sum(1 for e in todo if current[e.path] is not None)
            report.content_rev = rev
    return ids


def remove_plan(entries: Sequence[Entry], present: dict[str, bool], report: Report) -> list[Entry]:
    todo = [e for e in entries if present.get(e.path) is False]
    report.not_present = sum(1 for e in entries if e.path not in present)
    report.already_removed = sum(1 for e in entries if present.get(e.path) is True)
    report.to_remove = len(todo)
    return todo


def remove_entries(
    conn: sqlite3.Connection, project: str, entries: Sequence[Entry], report: Report
) -> None:
    """Soft-delete the imported rows of ``entries``, archived ones too, in
    batches; each batch bumps ``content_rev`` once. ``db.soft_delete`` skips
    archived rows, so this is its own statement."""
    for start in range(0, len(entries), db.BATCH_ROWS):
        batch = entries[start : start + db.BATCH_ROWS]
        with db.write_tx(conn):
            marks = ",".join("?" * len(batch))
            live = [
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM memories WHERE project = ? AND root = ? AND path IN ({marks}) "
                    "AND deleted_at IS NULL",
                    (project, db.IMPORT_ROOT, *(e.path for e in batch)),
                )
            ]
            report.already_removed += len(batch) - len(live)
            if not live:
                continue
            rev = db.bump_rev(conn, "content_rev")
            now = db.utc_now()
            conn.executemany(
                "UPDATE memories SET deleted_at = ?, rev = ?, updated_at = ? WHERE id = ?",
                [(now, rev, now, memory_id) for memory_id in live],
            )
            report.removed += len(live)
            report.content_rev = rev


def count_without_vector(conn: sqlite3.Connection, ids: Sequence[int]) -> tuple[int, int, int]:
    """``(live plain, live mined, archived)``: the rows of ``ids`` that have
    no vector yet."""
    plain = mined = archived = 0
    with db.read_tx(conn):
        for start in range(0, len(ids), db.BATCH_ROWS):
            chunk = ids[start : start + db.BATCH_ROWS]
            marks = ",".join("?" * len(chunk))
            for archived_at, source_type in conn.execute(
                f"SELECT m.archived_at, m.source_type FROM memories m WHERE m.id IN ({marks}) "
                "AND NOT EXISTS (SELECT 1 FROM vectors v WHERE v.memory_id = m.id)",
                chunk,
            ):
                if archived_at is not None:
                    archived += 1
                elif source_type == db.SOURCE_MINED:
                    mined += 1
                else:
                    plain += 1
    return plain, mined, archived


def mined_get_vectors(settings: embedding.EmbeddingSettings) -> bool:
    """False when the backfill skips mined rows: a backend that sends text
    off the machine embeds them only with ``embedding.remote_include_mined``
    (``EmbeddingService.include_mined``)."""
    return not embedding.needs_consent(settings) or settings.remote_include_mined


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
    remove: bool = False,
    embed_settings: embedding.EmbeddingSettings | None = None,
) -> Report:
    """One import (or removal). ``apply=False`` reads the store and writes
    nothing. ``apply=True`` with no usable line writes nothing and sets
    ``refused``."""
    report = Report(file=str(path), apply=apply, archived=archived, remove=remove)
    if embed_settings is not None:
        report.embed_backend = embed_settings.backend
        report.mined_embedded = mined_get_vectors(embed_settings)
    entries = read_entries(path, extra_labels=extra_labels, report=report, now=db.utc_now())
    present = present_rows(conn, project)
    if remove:
        todo = remove_plan(entries, present, report)
    else:
        todo = plan(entries, present, report)
    if not apply:
        return report
    if report.usable == 0:
        what = "remove" if remove else "import"
        report.refused = f"the file has no line it can {what}; nothing was written"
        return report
    if remove:
        remove_entries(conn, project, todo, report)
        return report
    ids = store_entries(conn, project, todo, report, archived=archived)
    (
        report.vectors_waiting,
        report.mined_vectors_waiting,
        report.archived_without_vector,
    ) = count_without_vector(conn, ids)
    return report


# -- CLI ----------------------------------------------------------------------


def _label(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("a label must not be empty")
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion import",
        description="Import memories from a JSONL file, or remove an earlier import. "
        "A dry run unless --apply.",
    )
    parser.add_argument("file", type=Path, help="the JSONL file, one JSON object per line")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print what would happen (default)")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="soft-delete the imported rows of this file instead of importing",
    )
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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.remove and (args.label or args.archived):
        parser.error("--remove does not take --label or --archived")
    settings = config.load_settings()
    db_path = args.db or settings.db_path
    path: Path = args.file.expanduser()
    if not path.is_file():
        _err(f"no file at {path}")
        return EXIT_REFUSED
    # Like the miner: this command never makes a database (NOBLIVION-28).
    if not db_path.is_file():
        _err(f"no database at {db_path}; run install.sh")
        return EXIT_NO_DB
    lock_path = db_path.parent / IMPORT_LOCK_FILE
    try:
        with contextlib.ExitStack() as stack:
            if args.apply:
                try:
                    stack.enter_context(indexer.index_lock(lock_path, args.lock_timeout))
                except OSError as exc:
                    raise LockFileError(
                        f"cannot open the lock file {lock_path} in the data dir "
                        f"({exc.strerror or type(exc).__name__})"
                    ) from exc
            report = _run_cli(db_path, path, settings.namespace, args)
    except db.SchemaError as exc:
        _err(str(exc))
        return EXIT_SCHEMA
    except indexer.LockTimeoutError:
        _err(f"another import holds {lock_path}")
        return EXIT_LOCKED
    except db.DatabaseMissing:
        _err(f"no database at {db_path}; run install.sh")
        return EXIT_NO_DB
    except (InputReadError, LockFileError) as exc:
        _err(str(exc))
        return EXIT_REFUSED
    except sqlite3.Error as exc:
        _err(f"database error ({exc}); try again later")
        return EXIT_DB_ERROR
    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        for line in report.summary():
            print(f"noblivion import: {line}")
    if report.refused:
        _err(report.refused)
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
            remove=args.remove,
            embed_settings=embedding.load_embedding_settings(),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
