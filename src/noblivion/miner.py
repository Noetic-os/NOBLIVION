# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transcript miner: past Claude Code sessions to low-trust rows (design doc 0001, section 11).

The miner reads the session transcripts (``~/.claude/projects/*/*.jsonl``,
config ``miner.transcript_glob``) and stores three kinds of experience that the
memory files never record:

- ``correction``: a user turn that corrects the assistant (a cue word in the
  first 200 characters), with the assistant text before it;
- ``review_request_changes``: a tool result with a "request changes" review
  verdict and a findings block;
- ``tool_error``: a failed tool call (``is_error``), paired with its call.

At most one row per transcript line, in that order. Every row has
``source_type = 'transcript_mined'``: the prompt hooks never inject it, and the
store returns it only when a request asks (``include_mined=1``, section 11.3).

Rules:

- Local only. It reads files and writes the SQLite store through ``db``.
- API records: Claude Code writes one assistant API message as several lines
  with the same ``message.id`` (one per content block, or repeated while it
  streams). The miner merges each run of lines with the same id into one
  record: the record with the largest ``usage.output_tokens`` is the base and
  the content blocks of all lines are joined, each block once.
- Redaction: secrets (``redaction.redact_at_rest``) and injection patterns
  (``injection.redact_injection``) are masked at extract and again before the
  insert. A candidate that gives the fail token is skipped.
- Duplicates: one tool error shape (tool and first error line) and one
  findings block are stored once. The shape hash is the row's ``hash``, so the
  check holds across runs too.
- Incremental: ``miner_state`` keeps the byte offset per transcript. A run
  reads from the offset (and replays up to ``LOOKBACK_BYTES`` before it to get
  back the tool calls and the last assistant text). A file that shrank or
  whose mtime moved back is read from the start; the unique
  ``(project, root, path)`` key makes a re-read safe. A last line without a
  newline is left for the next run.
- Bounded: a run stops at ``miner.max_run_s`` seconds and keeps its offsets.
- Opt-out: ``miner.enabled = false`` or ``NOBLIVION_MINER=0`` and the CLI does
  nothing.

CLI: ``noblivion mine [--since YYYY-MM-DD]``. Exit codes: 0 done (or the miner
is off); 3 the database schema refuses this tool; 4 another run holds
``mine.lock``.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from noblivion import config, db, indexer, injection, redaction

MINE_LOCK_FILE = "mine.lock"
DEFAULT_TRANSCRIPT_GLOB = "~/.claude/projects/*/*.jsonl"
DEFAULT_MAX_RUN_S = 300.0

EXIT_OK = 0
EXIT_SCHEMA = 3
EXIT_LOCKED = 4
EXIT_NO_DB = 6  # no database: the miner never makes one (NOBLIVION-28)

KIND_CORRECTION = "correction"
KIND_REVIEW = "review_request_changes"
KIND_TOOL_ERROR = "tool_error"
KINDS = (KIND_CORRECTION, KIND_REVIEW, KIND_TOOL_ERROR)

CORRECTION_MAX_CHARS = 600  # the user turn, as stored
CUE_WINDOW_CHARS = 200  # a cue must appear in this head of the turn
CONTEXT_MAX_CHARS = 400  # the assistant text before the correction
ERROR_MAX_CHARS = 400  # the tool error text
COMMAND_MAX_CHARS = 300  # the shell command that failed
FINDINGS_MAX_CHARS = 1500  # the review's findings block
TITLE_COMMAND_CHARS = 60
TOOL_USE_WINDOW = 512  # tool calls kept per file for pairing
LOOKBACK_BYTES = 4 * 1024 * 1024  # replayed before the offset on a resume
DEADLINE_CHECK_LINES = 256

# Precision over recall: the cue must sit in the first CUE_WINDOW_CHARS chars.
CUES = (
    "no",
    "wrong",
    "don't",
    "do not",
    "never",
    "stop",
    "again",
    "i told you",
    "you should have",
    "why did you",
    "that is not",
    "not what i asked",
    "undo",
    "revert",
    "you ignored",
)

# User turns that the harness writes, not the user. Matched on the stripped
# head of the turn.
HARNESS_PREFIXES = (
    "<task-notification>",
    "<cross-session-message",
    "<command-name>",
    "<command-message>",
    "<local-command",
    "<system-reminder>",
    "<ide_selection>",
    "<ide_opened_file>",
    "This session is being continued",
    "Caveat:",
    "Stop hook feedback:",
    "Another Claude session sent a message:",
    "[Request interrupted",
)

_VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*`?REQUEST_CHANGES`?")
FINDINGS_MARKER = "**Findings**"
# The findings block ends where the verdict comment's trailer starts.
FINDINGS_STOPS = ("[GitHub Actions run]", "<!--", "\nreviewed-pr:", "**Verdict:**")
_REVIEWED_PR_RE = re.compile(r"^reviewed-pr:\s*(\S+)", re.M)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _cue_pattern() -> re.Pattern[str]:
    alts = []
    for cue in sorted(CUES, key=len, reverse=True):
        alts.append(re.escape(cue).replace("'", "['’]").replace(r"\ ", r"\s+"))
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)


CUE_RE = _cue_pattern()


def find_cue(text: str) -> str:
    """The leftmost cue in the head of ``text``, in its canonical spelling, or ""."""
    m = CUE_RE.search(text[:CUE_WINDOW_CHARS])
    if m is None:
        return ""
    return re.sub(r"\s+", " ", m.group(0).lower()).replace("’", "'")


def is_harness_turn(text: str) -> bool:
    return text.lstrip().startswith(HARNESS_PREFIXES)


# -- settings -----------------------------------------------------------------


@dataclass(frozen=True)
class MinerSettings:
    enabled: bool = True
    transcript_glob: str = DEFAULT_TRANSCRIPT_GLOB
    max_run_s: float = DEFAULT_MAX_RUN_S


def _positive_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def load_miner_settings(env: Mapping[str, str] | None = None) -> MinerSettings:
    """``miner.*`` keys (section 12.3). ``NOBLIVION_MINER`` wins over the file."""
    env = os.environ if env is None else env
    cfg = config.load_file(env)
    enabled = config.switch(env, "NOBLIVION_MINER", cfg, "miner.enabled", True)
    pattern = config.lookup(cfg, "miner.transcript_glob")
    return MinerSettings(
        enabled=enabled,
        transcript_glob=pattern.strip()
        if isinstance(pattern, str) and pattern.strip()
        else DEFAULT_TRANSCRIPT_GLOB,
        max_run_s=_positive_float(config.lookup(cfg, "miner.max_run_s"), DEFAULT_MAX_RUN_S),
    )


# -- text helpers -------------------------------------------------------------


def _flat(text: str) -> str:
    """One paragraph: every whitespace run is one space."""
    return re.sub(r"\s+", " ", text).strip()


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` chars; a cut is marked with '...' inside the limit."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_text(content: Any) -> str:
    """The text of a message content: a string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _blocks(content: Any, block_type: str) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == block_type]


def _date_of(timestamp: str) -> str:
    return timestamp[:10] if _DATE_RE.match(timestamp) else "an unknown date"


def scrub(text: str) -> str:
    """Secrets, then injection patterns. The fail token when either fails."""
    out = redaction.redact_at_rest(text)
    if out == redaction.REDACTION_FAILED_TOKEN:
        return out
    out, _hits = injection.redact_injection(out)
    return out


# -- API record dedupe --------------------------------------------------------


def message_id(record: Mapping[str, Any]) -> str | None:
    message = record.get("message")
    mid = message.get("id") if isinstance(message, dict) else None
    return mid if isinstance(mid, str) and mid else None


def output_tokens(record: Mapping[str, Any]) -> int:
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    value = usage.get("output_tokens") if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def merge_api_records(records: Sequence[dict]) -> dict:
    """One record for lines that share a ``message.id``.

    The record with the largest ``output_tokens`` is the base (the first one on
    a tie). Its content is the blocks of every line, in order, each block once:
    a line may hold one block of the message or the whole message so far.
    """
    if len(records) == 1:
        return records[0]
    base = max(records, key=output_tokens)
    blocks: list[Any] = []
    seen: set[str] = set()
    for record in records:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        for block in content:
            key = json.dumps(block, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                blocks.append(block)
    merged = dict(base)
    merged["message"] = {**base["message"], "content": blocks}
    return merged


# -- candidates ---------------------------------------------------------------


@dataclass
class Candidate:
    """One mined experience, already redacted."""

    kind: str
    session_id: str
    line_no: int
    title: str
    text: str
    shape: str  # dedupe key text; its sha256 is the row hash

    @property
    def path(self) -> str:
        return f"{self.session_id}#{self.line_no}"

    @property
    def content(self) -> str:
        return f"# {self.title}\n\n{self.text}"

    @property
    def hash(self) -> str:
        return sha256_text(f"{self.kind}\n{self.shape}")


@dataclass
class MineStats:
    files: int = 0
    files_skipped: int = 0  # unchanged since the last run
    unreadable_files: int = 0
    lines: int = 0
    bytes_read: int = 0
    bad_lines: int = 0
    api_records_deduped: int = 0
    unpaired_errors: int = 0
    redaction_hits: int = 0
    redaction_failed: int = 0
    candidates: Counter = field(default_factory=Counter)
    deduped: Counter = field(default_factory=Counter)
    inserted: int = 0
    existing: int = 0
    partial: bool = False  # the time budget ran out
    content_rev: int = 0

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "files_skipped": self.files_skipped,
            "unreadable_files": self.unreadable_files,
            "lines": self.lines,
            "bytes_read": self.bytes_read,
            "bad_lines": self.bad_lines,
            "api_records_deduped": self.api_records_deduped,
            "unpaired_errors": self.unpaired_errors,
            "redaction_hits": self.redaction_hits,
            "redaction_failed": self.redaction_failed,
            "candidates": {k: self.candidates.get(k, 0) for k in KINDS},
            "deduped": {k: self.deduped.get(k, 0) for k in KINDS},
            "inserted": self.inserted,
            "existing": self.existing,
            "partial": self.partial,
            "content_rev": self.content_rev,
        }

    def summary(self) -> str:
        per_kind = " ".join(f"{k}={self.candidates.get(k, 0)}" for k in KINDS)
        line = (
            f"files={self.files} skipped={self.files_skipped} lines={self.lines} "
            f"{per_kind} inserted={self.inserted} deduped={sum(self.deduped.values())} "
            f"existing={self.existing} redaction_failed={self.redaction_failed}"
        )
        return line + (" partial=true" if self.partial else "")


class SessionScan:
    """Per-file state: the last assistant text and a window of tool calls."""

    def __init__(self) -> None:
        self.last_assistant_text = ""
        self.tool_uses: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def note_assistant(self, content: Any) -> None:
        text = _flat(_content_text(content))
        if text:
            self.last_assistant_text = _clip(text, CONTEXT_MAX_CHARS)
        for b in _blocks(content, "tool_use"):
            tid, name = b.get("id"), b.get("name")
            if not isinstance(tid, str) or not isinstance(name, str):
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            cmd = inp.get("command") if name == "Bash" else None
            command = _clip(_flat(cmd), COMMAND_MAX_CHARS) if isinstance(cmd, str) else ""
            self.tool_uses[tid] = (name, command)
            self.tool_uses.move_to_end(tid)
            while len(self.tool_uses) > TOOL_USE_WINDOW:
                self.tool_uses.popitem(last=False)


def _scrubbed(text: str, stats: MineStats) -> str | None:
    out = scrub(text)
    if out == redaction.REDACTION_FAILED_TOKEN:
        return None
    if out != text:
        stats.redaction_hits += 1
    return out


def correction_from(content: Any) -> tuple[str, str] | None:
    """``(turn, cue)`` for a user turn that corrects the assistant, else None."""
    raw = _content_text(content)
    if not raw.strip() or is_harness_turn(raw):
        return None
    turn = _flat(raw)
    cue = find_cue(turn)
    if not cue:
        return None
    return _clip(turn, CORRECTION_MAX_CHARS), cue


def review_from(result_text: str) -> tuple[str, str] | None:
    """``(findings, reviewed_pr)`` for a "request changes" verdict, else None."""
    if not _VERDICT_RE.search(result_text):
        return None
    pos = result_text.find(FINDINGS_MARKER)
    if pos < 0:
        return None
    body = result_text[pos + len(FINDINGS_MARKER) :]
    end = len(body)
    for stop in FINDINGS_STOPS:
        i = body.find(stop)
        if 0 <= i < end:
            end = i
    findings = _clip(_flat(body[:end]), FINDINGS_MAX_CHARS)
    if not findings:
        return None
    m = _REVIEWED_PR_RE.search(result_text)
    return findings, (m.group(1) if m else "")


class Miner:
    """Turns records into candidates. ``seen`` answers "is this hash stored?"."""

    def __init__(self, stats: MineStats, seen: Callable[[str], bool] | None = None) -> None:
        self.stats = stats
        self.seen = seen or (lambda _h: False)
        self.run_hashes: set[str] = set()

    def _first(self, cand: Candidate) -> bool:
        h = cand.hash
        if h in self.run_hashes or self.seen(h):
            self.stats.deduped[cand.kind] += 1
            return False
        self.run_hashes.add(h)
        return True

    def _fail(self) -> None:
        self.stats.redaction_failed += 1

    def mine_record(
        self, record: dict, line_no: int, scan: SessionScan, session_id: str
    ) -> Candidate | None:
        """At most one candidate for one line. Updates ``scan`` for assistant lines."""
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        rtype = record.get("type")
        if rtype == "assistant":
            scan.note_assistant(content)
            return None
        if rtype != "user" or content is None:
            return None
        ts = record.get("timestamp")
        date = _date_of(ts if isinstance(ts, str) else "")
        short = session_id[:8] or "unknown"

        if record.get("isMeta") is not True and record.get("isSidechain") is not True:
            found = correction_from(content)
            if found is not None:
                return self._correction(found, scan, session_id, line_no, date, short)

        results = _blocks(content, "tool_result")
        for block in results:
            review = review_from(_content_text(block.get("content")))
            if review is not None:
                return self._review(review, session_id, line_no, date, short)

        for block in results:
            if block.get("is_error") is not True:
                continue
            raw_error = _content_text(block.get("content"))
            error = _flat(raw_error)
            if not error:
                continue  # an empty result says nothing
            tid = block.get("tool_use_id")
            pair = scan.tool_uses.get(tid) if isinstance(tid, str) else None
            if pair is None:
                self.stats.unpaired_errors += 1
                continue
            return self._tool_error(pair, raw_error, error, session_id, line_no, date, short)
        return None

    def _correction(self, found, scan, session_id, line_no, date, short) -> Candidate | None:
        turn, cue = found
        turn = _scrubbed(turn, self.stats)
        context = _scrubbed(scan.last_assistant_text, self.stats)
        if turn is None or context is None:
            self._fail()
            return None
        text = f"User correction on {date} in session {short} (cue: {cue}). The user wrote: {turn}"
        if context:
            text += f" The assistant had just said: {context}"
        cand = Candidate(
            KIND_CORRECTION, session_id, line_no, f"Correction ({cue}) on {date}", text, text
        )
        return cand if self._first(cand) else None

    def _review(self, review, session_id, line_no, date, short) -> Candidate | None:
        findings, pr = review
        findings = _scrubbed(findings, self.stats)
        pr = _scrubbed(pr, self.stats) if pr else ""
        if findings is None or pr is None:
            self._fail()
            return None
        of_pr = f" of pull request #{pr}" if pr else ""
        text = (
            f"Independent review on {date} in session {short} returned "
            f"REQUEST_CHANGES{of_pr}. Findings: {findings}"
        )
        cand = Candidate(
            KIND_REVIEW, session_id, line_no, f"Review asked for changes{of_pr}", text, findings
        )
        return cand if self._first(cand) else None

    def _tool_error(
        self, pair, raw_error, error, session_id, line_no, date, short
    ) -> Candidate | None:
        tool, command = pair
        command = _scrubbed(command, self.stats) if command else ""
        error = _scrubbed(_clip(error, ERROR_MAX_CHARS), self.stats)
        first_line = raw_error.strip().splitlines()[0] if raw_error.strip() else ""
        first_line = _scrubbed(first_line, self.stats)
        if command is None or error is None or first_line is None:
            self._fail()
            return None
        what = f"{tool} `{command}`" if command else tool
        title = f"Tool error: {tool}"
        if command:
            title += f" `{_clip(command, TITLE_COMMAND_CHARS)}`"
        text = f"Tool error on {date} in session {short}: {what} failed with: {error}"
        cand = Candidate(KIND_TOOL_ERROR, session_id, line_no, title, text, f"{tool}\n{first_line}")
        return cand if self._first(cand) else None


# -- reading one transcript ---------------------------------------------------


@dataclass
class FileScan:
    candidates: list[Candidate]
    offset: int  # bytes consumed, always at a line end
    finished: bool  # False: the time budget stopped the read


def _parse(line: bytes) -> dict | None:
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _count_lines(fh, end: int) -> int:
    fh.seek(0)
    count = 0
    left = end
    while left > 0:
        chunk = fh.read(min(1 << 20, left))
        if not chunk:
            break
        count += chunk.count(b"\n")
        left -= len(chunk)
    return count


def _replay(fh, offset: int, scan: SessionScan) -> None:
    """Rebuild ``scan`` from the lines just before ``offset`` (no candidates)."""
    start = max(0, offset - LOOKBACK_BYTES)
    fh.seek(start)
    data = fh.read(offset - start)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first piece is the tail of a line
    group: list[dict] = []
    for line in lines:
        record = _parse(line) if line.strip() else None
        if record is None or record.get("type") != "assistant":
            continue
        mid = message_id(record)
        if group and (mid is None or mid != message_id(group[0])):
            scan.note_assistant(merge_api_records(group)["message"].get("content"))
            group = []
        group.append(record)
    if group:
        scan.note_assistant(merge_api_records(group)["message"].get("content"))


def mine_stream(
    fh,
    *,
    session_id: str,
    offset: int,
    miner: Miner,
    stats: MineStats,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FileScan:
    """Mine a binary file object from ``offset``. Lines without a newline wait."""
    scan = SessionScan()
    line_no = 0
    if offset > 0:
        line_no = _count_lines(fh, offset)
        _replay(fh, offset, scan)
    fh.seek(offset)
    pos = offset
    out: list[Candidate] = []
    group: list[tuple[dict, int]] = []  # assistant lines that share a message id

    def flush() -> None:
        if group:
            stats.api_records_deduped += len(group) - 1
            merged = merge_api_records([r for r, _ in group])
            miner.mine_record(merged, group[0][1], scan, session_id)
            group.clear()

    counter = 0
    while True:
        if deadline is not None and not group:
            counter += 1
            if counter % DEADLINE_CHECK_LINES == 0 and clock() >= deadline:
                return FileScan(out, pos, False)
        line = fh.readline()
        if not line or not line.endswith(b"\n"):
            break  # end of file, or a line still being written
        pos += len(line)
        line_no += 1
        stats.lines += 1
        stats.bytes_read += len(line)
        if not line.strip():
            continue
        record = _parse(line)
        if record is None:
            stats.bad_lines += 1
            continue
        mid = message_id(record) if record.get("type") == "assistant" else None
        if group and (mid is None or mid != message_id(group[0][0])):
            flush()
        if mid is not None:
            group.append((record, line_no))
            continue
        cand = miner.mine_record(record, line_no, scan, session_id)
        if cand is not None:
            out.append(cand)
    flush()
    return FileScan(out, pos, True)


# -- store writes -------------------------------------------------------------


@dataclass(frozen=True)
class Transcript:
    path: Path
    root: str  # the project folder name
    key: str  # miner_state.transcript: <root>/<file name>


def list_transcripts(pattern: str) -> list[Transcript]:
    """Every file that matches ``pattern`` (``~`` expanded), in name order."""
    found = []
    for name in sorted(glob.glob(os.path.expanduser(pattern))):
        p = Path(name)
        if p.is_file():
            found.append(Transcript(p, p.parent.name, f"{p.parent.name}/{p.name}"))
    return found


def _state(conn: sqlite3.Connection, key: str) -> tuple[int, int, int] | None:
    row = conn.execute(
        "SELECT size, mtime_ns, offset FROM miner_state WHERE transcript = ?", (key,)
    ).fetchone()
    return None if row is None else (int(row[0]), int(row[1]), int(row[2]))


def _hash_seen(conn: sqlite3.Connection, project: str) -> Callable[[str], bool]:
    def seen(h: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM memories WHERE project = ? AND source_type = ? AND hash = ? LIMIT 1",
            (project, db.SOURCE_MINED, h),
        ).fetchone()
        return row is not None

    return seen


def store_candidates(
    conn: sqlite3.Connection,
    project: str,
    root: str,
    cands: Sequence[Candidate],
    stats: MineStats,
) -> None:
    """Insert new rows in batches; each batch bumps ``content_rev`` once."""
    for start in range(0, len(cands), db.BATCH_ROWS):
        batch = []
        for cand in cands[start : start + db.BATCH_ROWS]:
            content = scrub(cand.content)  # second pass, right before the insert
            if content == redaction.REDACTION_FAILED_TOKEN:
                stats.redaction_failed += 1
                continue
            batch.append((cand, content))
        if not batch:
            continue
        with db.write_tx(conn):
            todo = [(c, t) for c, t in batch if db.row_at(conn, project, root, c.path) is None]
            stats.existing += len(batch) - len(todo)
            if not todo:
                continue
            rev = db.bump_rev(conn, "content_rev")
            now = db.utc_now()
            for cand, content in todo:
                db.insert_memory(
                    conn,
                    project=project,
                    root=root,
                    path=cand.path,
                    source_type=db.SOURCE_MINED,
                    category=cand.kind,
                    content=content,
                    content_hash=cand.hash,
                    labels="[]",
                    rev=rev,
                    now=now,
                )
                stats.inserted += 1
            stats.content_rev = rev


def _save_state(conn, key: str, size: int, mtime_ns: int, offset: int) -> None:
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO miner_state (transcript, size, mtime_ns, offset, mined_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (transcript) DO UPDATE SET "
            "size = excluded.size, mtime_ns = excluded.mtime_ns, offset = excluded.offset, "
            "mined_at = excluded.mined_at",
            (key, size, mtime_ns, offset, db.utc_now()),
        )


def run(
    conn: sqlite3.Connection,
    transcripts: Iterable[Transcript],
    *,
    project: str = config.DEFAULT_NAMESPACE,
    max_run_s: float | None = DEFAULT_MAX_RUN_S,
    since: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> MineStats:
    """One miner run over ``transcripts``. Returns what it did."""
    stats = MineStats()
    deadline = None if max_run_s is None else clock() + max_run_s
    miner = Miner(stats, _hash_seen(conn, project))
    since_ns = int(since.timestamp() * 1e9) if since is not None else None
    for t in transcripts:
        if deadline is not None and clock() >= deadline:
            stats.partial = True
            break
        try:
            st = t.path.stat()
        except OSError:
            stats.unreadable_files += 1
            continue
        if since_ns is not None and st.st_mtime_ns < since_ns:
            continue
        prev = _state(conn, t.key)
        offset = 0
        if prev is not None:
            size, mtime_ns, prev_offset = prev
            if st.st_size == prev_offset and st.st_mtime_ns == mtime_ns:
                stats.files_skipped += 1
                continue
            if st.st_size >= prev_offset and st.st_mtime_ns >= mtime_ns:
                offset = prev_offset
        stats.files += 1
        try:
            with t.path.open("rb") as fh:
                scan = mine_stream(
                    fh,
                    session_id=t.path.stem,
                    offset=offset,
                    miner=miner,
                    stats=stats,
                    deadline=deadline,
                    clock=clock,
                )
        except OSError:
            stats.unreadable_files += 1
            continue
        for cand in scan.candidates:
            stats.candidates[cand.kind] += 1
        store_candidates(conn, project, t.root, scan.candidates, stats)
        _save_state(conn, t.key, st.st_size, st.st_mtime_ns, scan.offset)
        if not scan.finished:
            stats.partial = True
            break
    return stats


# -- CLI ----------------------------------------------------------------------


def _date(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion mine", description="Mine Claude Code transcripts into the store."
    )
    parser.add_argument("--since", type=_date, default=None, help="skip files older (YYYY-MM-DD)")
    parser.add_argument("--transcripts", default=None, help="glob; default: the config")
    parser.add_argument("--db", type=Path, default=None, help="database file; default: data dir")
    parser.add_argument("--max-seconds", type=float, default=None, help="run time budget")
    parser.add_argument("--lock-timeout", type=float, default=0.0, help="seconds to wait")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    miner_settings = load_miner_settings()
    if not miner_settings.enabled:
        print("noblivion mine: the miner is off (miner.enabled)")
        return EXIT_OK
    settings = config.load_settings()
    db_path = args.db or settings.db_path
    pattern = args.transcripts or miner_settings.transcript_glob
    max_s = args.max_seconds if args.max_seconds is not None else miner_settings.max_run_s
    # The SessionEnd hook starts this run. It opens an existing database
    # only: after an uninstall deleted the data dir it must not make it again.
    if not db_path.is_file():
        print(f"noblivion mine: no database at {db_path}; run install.sh", file=sys.stderr)
        return EXIT_NO_DB
    try:
        with indexer.index_lock(db_path.parent / MINE_LOCK_FILE, args.lock_timeout):
            conn = db.open_db(db_path, allow_migrate=False)
            try:
                stats = run(
                    conn,
                    list_transcripts(pattern),
                    project=settings.namespace,
                    max_run_s=max_s,
                    since=args.since,
                )
            finally:
                conn.close()
    except db.SchemaError as exc:
        print(f"noblivion mine: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except indexer.LockTimeoutError:
        print("noblivion mine: another miner run holds mine.lock", file=sys.stderr)
        return EXIT_LOCKED
    except FileNotFoundError:
        print(f"noblivion mine: no database at {db_path}; run install.sh", file=sys.stderr)
        return EXIT_NO_DB
    print(json.dumps(stats.to_dict()) if args.json else f"noblivion mine: {stats.summary()}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
