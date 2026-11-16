# SPDX-License-Identifier: AGPL-3.0-or-later
"""Historical fetch: the older notes that ONE current note replaces (NOBLIVION-189).

A note that says ``supersedes: <stem>`` withdraws the older note of that stem in the same
folder; the indexer archives its row (``indexer._archive_superseded``), so an ordinary fetch
does not find it. ``history_answer`` reads those archived rows, and only these, from one
current successor. It is narrow on purpose:

- The caller names the successor only. The older notes come from the link the indexer
  stored, never from the caller.
- The successor passes the ordinary fetch checks, with an exact root: no shared root, no
  import root and no general-note exception. A request without a root is refused.
- Each older note must be in the same project and root, archived and not deleted, still on
  disk with the same bytes the store holds, still ``active`` by its own ``status:``, and, for a
  learning note, still ``active`` in the ledger. So a note that was later forgotten,
  excluded, edited, removed or never stored is not returned.
- One link, no chains. Every refusal gives the same reason, so the answer does not tell
  whether a refused note exists.

Ordinary search and fetch do not call this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from noblivion import db, indexer

NO_HISTORY_REASON = "no readable older version for that id in this scope"


def _folder_of(folders: Sequence[Path], root: str) -> Path | None:
    """The folder of ``root``, chosen as ``indexer.discover`` chooses: the first one wins."""
    for d in folders:
        folder = Path(os.path.expanduser(str(d)))
        if folder.parent.name == root:
            return folder
    return None


def _current_file(folder: Path, path: str, stored_hash: str) -> indexer.ParsedFile | None:
    """The parsed file, when its bytes on disk are still the bytes the store holds."""
    target = folder / path
    if target.parent != folder or not indexer._wanted(target):
        return None
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != stored_hash:
        return None
    text = indexer._head(raw).decode("utf-8", errors="replace")
    return indexer.parse_file(path, text)


def _ledger_allows(conn: sqlite3.Connection, project: str, row: sqlite3.Row) -> bool:
    """A learning note must be ``active`` in the ledger and match the stored bytes."""
    managed = conn.execute(
        "SELECT status, file_hash FROM learning_notes WHERE project=? AND root=? AND path=?",
        (project, row["root"], row["path"]),
    ).fetchone()
    return managed is None or (
        managed["status"] == "active" and managed["file_hash"] == row["hash"]
    )


def _linked_stems(conn: sqlite3.Connection, project: str, root: str, path: str) -> list[str]:
    """The stems the indexer recorded for this note in its ``supersedes`` map."""
    try:
        mapping = json.loads(db.get_meta(conn, f"supersedes:{project}") or "{}")
    except ValueError:
        return []
    stems = mapping.get(f"{root}/{path}") if isinstance(mapping, dict) else None
    if not isinstance(stems, list):
        return []
    own = path.removesuffix(".md")
    return [s for s in stems if isinstance(s, str) and s != own][: indexer.SUPERSEDES_MAX]


def history_answer(
    conn: sqlite3.Connection | None,
    raw_id: str,
    namespace: str,
    root: str | None,
    session_id: str,
    folders: Sequence[Path],
) -> dict:
    """The older notes that note ``raw_id`` replaces, as ``history``: a list of
    ``{id, title, source, text}``. ``history`` is always present, so a caller can tell this
    answer from an ordinary fetch of an older store that ignored the request."""
    from noblivion import rest
    from noblivion.learning_runtime import use_allowed

    answer: dict = {"namespace": namespace, "id": raw_id, "history": [], "reason": None}
    if not rest._ID_RE.fullmatch(raw_id) or int(raw_id) > rest.MAX_ROW_ID:
        return dict(answer, reason=rest.BAD_ID_REASON)
    answer["id"] = memory_id = int(raw_id)
    denied = dict(answer, reason=NO_HISTORY_REASON)
    if conn is None or not root:
        return denied
    row = conn.execute(
        "SELECT id, root, path, hash FROM memories WHERE id=? AND project=? AND source_type=? "
        "AND archived_at IS NULL AND deleted_at IS NULL",
        (memory_id, namespace, db.SOURCE_MD),
    ).fetchone()
    if row is None or row["root"] != root:
        return denied
    if not use_allowed(conn, namespace, root, session_id) or not _ledger_allows(
        conn, namespace, row
    ):
        return denied
    folder = _folder_of(folders, root)
    successor = _current_file(folder, row["path"], row["hash"]) if folder else None
    if successor is None or not successor.active:
        return denied
    named = [
        s for s in _linked_stems(conn, namespace, root, row["path"]) if s in successor.supersedes
    ]
    found = []
    for stem in named:
        old = conn.execute(
            "SELECT id, root, path, hash, content FROM memories WHERE project=? AND source_type=? "
            "AND root=? AND path=? AND archived_at IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (namespace, db.SOURCE_MD, root, f"{stem}.md"),
        ).fetchone()
        if old is None or not _ledger_allows(conn, namespace, old):
            continue
        parsed = _current_file(folder, old["path"], old["hash"])  # type: ignore[arg-type]
        if parsed is None or not parsed.active:
            continue
        found.append(old)
    if not found:
        return denied
    share = max(
        0, (rest.ANSWER_CAP_BYTES - 1 - len(json.dumps(answer).encode("utf-8"))) // len(found) - 256
    )
    for old in found:
        content = str(old["content"])
        title, _ = rest.title_and_summary(content, int(old["id"]))
        item = {"id": int(old["id"]), "title": title, "source": rest.source_of(content), "text": ""}
        room = share - len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        item["text"] = rest._cut_to_json_len(rest.safe_text(content), room)
        answer["history"].append(item)
    return answer
