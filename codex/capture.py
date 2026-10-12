#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable, explicitly scoped conversation capture. Standard library only.

The spool commits records and the transcript cursor in one transaction. Delivery
is at least once: only a matching store receipt deletes a pending batch. A fresh
SessionStart is required; installing this module never scans old conversations.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import functools
import hashlib
import http.client
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CHUNK_CHARS = 16000
# The extractor permits exact evidence quotes up to 8192 characters. Overlap
# preserves every such contiguous quote across a canonical source boundary.
CHUNK_OVERLAP = 8192
BATCH_CHUNKS = 24
# Includes overlap bytes counted as source content; leaves room for the ledger's
# preceding context below the extractor's total source budget.
BATCH_CONTENT_CHARS = 96000


class SpoolFull(OSError):
    """Capture stays unacknowledged until local spool capacity is available."""


def spool_folder(cfg: dict) -> Path:
    return (
        Path(cfg["state_dir"])
        / "learning"
        / digest(str(cfg.get("namespace", "")) + ":" + str(cfg.get("recall_root", "")))[:16]
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=8)
def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def scoped_config(cfg: dict, event: dict) -> dict | None:
    """Longest explicit workspace prefix wins. No inferred home-directory scope."""
    cwd = os.path.normpath(str(event.get("cwd") or ""))
    matches = []
    for mapping in cfg.get("project_mappings", []):
        if not isinstance(mapping, dict):
            continue
        path = os.path.normpath(str(mapping.get("workspace") or ""))
        if not os.path.isabs(path) or not mapping.get("root") or not mapping.get("namespace"):
            continue
        if cwd == path or cwd.startswith(path.rstrip(os.sep) + os.sep):
            matches.append((len(path), mapping))
    if not matches:
        return None
    selected = max(matches, key=lambda item: item[0])[1]
    result = {**cfg, **selected}
    result["recall_root"] = selected["root"]
    result["learning"] = {**cfg.get("learning", {}), **selected.get("learning", {})}
    return result


def allowed(cfg: dict, session: str = "") -> bool:
    opts = cfg.get("learning", {})
    session_opts = opts.get("sessions", {}).get(session, {})
    return (
        opts.get("contribute") is True
        and opts.get("paused") is not True
        and session_opts.get("contribute", True) is True
        and session_opts.get("excluded") is not True
        and session_opts.get("paused") is not True
    )


def memory_use(cfg: dict, session: str = "") -> bool:
    opts = cfg.get("learning", {})
    return (
        opts.get("use", True) is True
        and opts.get("sessions", {}).get(session, {}).get("use", True) is True
    )


def safe_content(cfg: dict, text: str) -> bool:
    mod = load_module(Path(cfg["repo_root"]) / "src/noblivion/redaction.py", "_capture_redaction")
    if hasattr(mod, "contains_secret"):
        return not mod.contains_secret(text)
    clean = mod.redact_at_rest(text)
    return clean == text and clean != mod.REDACTION_FAILED_TOKEN


def _text(value: Any) -> str:
    return (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )


def _scrub(value):
    module = load_module(
        Path(__file__).resolve().parents[1] / "src/noblivion/redaction.py", "_capture_redaction"
    )
    return module.scrub_read_capabilities(value)


def _codex_context_items(payload: dict, count: int) -> set:
    """Use native block provenance; text prefixes cannot prove who authored it."""
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(kinds, list) or len(kinds) != count:
        return set()
    return {
        index
        for index, kind in enumerate(kinds)
        if isinstance(kind, str)
        and kind in {"agents_md.instructions", "environments.environment_context"}
    }


def normalized(row: dict, client: str) -> list[dict]:
    """Keep messages, function/custom calls, results, IDs and original tool JSON."""
    row = _scrub(row)
    payload = row.get("payload", {}) if row.get("type") == "response_item" else row
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    common = {"observed_at": str(row.get("timestamp") or ""), "kind": str(kind or "")}
    if client == "codex" and kind in ("function_call", "custom_tool_call"):
        return [
            {
                **common,
                "role": "assistant",
                "kind": "tool_call",
                "content": _text(payload.get("arguments", payload.get("input", ""))),
                "call_id": str(payload.get("call_id") or payload.get("id") or ""),
                "tool_name": str(payload.get("name") or ""),
            }
        ]
    if client == "codex" and kind in ("function_call_output", "custom_tool_call_output"):
        return [
            {
                **common,
                "role": "tool",
                "kind": "tool_result",
                "content": _text(payload.get("output", "")),
                "call_id": str(payload.get("call_id") or ""),
            }
        ]
    message = payload.get("message", payload)
    if not isinstance(message, dict):
        return []
    role = message.get("role") or (kind if kind in ("user", "assistant", "system") else None)
    if role not in ("user", "assistant", "tool"):
        # Internal system/developer instructions are not conversation evidence.
        return []
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    injected = _codex_context_items(payload, len(content)) if client == "codex" else set()
    out = []
    for index, block in enumerate(content):
        if role == "user" and index in injected:
            continue
        if not isinstance(block, dict):
            continue
        item = {**common, "role": role, "phase": str(payload.get("phase") or "")}
        if block.get("type") in ("text", "input_text", "output_text"):
            item.update(content=str(block.get("text") or ""), kind="message")
        elif block.get("type") == "tool_use":
            item.update(
                role="assistant",
                kind="tool_call",
                content=_text(block.get("input", {})),
                call_id=str(block.get("id") or ""),
                tool_name=str(block.get("name") or ""),
            )
        elif block.get("type") == "tool_result":
            item.update(
                role="tool",
                kind="tool_result",
                content=_text(block.get("content", "")),
                call_id=str(block.get("tool_use_id") or ""),
                is_error=bool(block.get("is_error")),
            )
        else:
            # Nontext content is not model evidence. Its type stays visible, with no bitmap bytes.
            continue
        if item["content"]:
            out.append(item)
    return out


def observation(content: str) -> dict:
    result = {}
    with contextlib.suppress(ValueError):
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            code = parsed.get("exit_code", parsed.get("exitCode"))
            if isinstance(code, int) and not isinstance(code, bool):
                result["exit_code"] = code
            if parsed.get("isError") is True or parsed.get("is_error") is True:
                result["is_error"] = True
    match = re.search(r"Process exited with code (-?\d+)", content)
    if match:
        result["exit_code"] = int(match.group(1))
    return result


def hook_records(event: dict) -> list[dict]:
    event = _scrub(event)
    name = event.get("hook_event_name")
    common = {"observed_at": str(event.get("timestamp") or ""), "kind": "message"}
    if name == "UserPromptSubmit":
        return [{**common, "role": "user", "content": str(event.get("prompt") or "")}]
    if name == "Stop":
        return [
            {
                **common,
                "role": "assistant",
                "content": str(event.get("last_assistant_message") or ""),
            }
        ]
    if name in ("PostToolUse", "PostToolUseFailure"):
        call = str(
            event.get("tool_use_id")
            or event.get("call_id")
            or event.get("event_id")
            or uuid.uuid4().hex
        )
        tool = str(event.get("tool_name") or "")
        return [
            {
                **common,
                "role": "assistant",
                "kind": "tool_call",
                "call_id": call,
                "tool_name": tool,
                "content": _text(event.get("tool_input", {})),
            },
            {
                **common,
                "role": "tool",
                "kind": "tool_result",
                "call_id": call,
                "tool_name": tool,
                "content": _text(event.get("tool_response", event.get("error", ""))),
                "metadata": {
                    **observation(_text(event.get("tool_response", event.get("error", "")))),
                    "command": _text(event.get("tool_input", {})),
                },
            },
        ]
    return []


class Spool:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        folder = spool_folder(cfg)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        self.conn = sqlite3.connect(str(folder / "capture.sqlite3"), timeout=2)
        os.chmod(folder / "capture.sqlite3", 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA secure_delete=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, eligible INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (
                session_id TEXT NOT NULL, path_id TEXT NOT NULL, offset INTEGER NOT NULL,
                PRIMARY KEY(session_id,path_id));
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS calls (session_id TEXT, call_id TEXT,
                name TEXT, command TEXT,
                PRIMARY KEY(session_id,call_id));
            CREATE TABLE IF NOT EXISTS rejected (
                source_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS hook_receipts (
                event_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, created REAL NOT NULL);
        """)

    def close(self):
        self.conn.close()

    def status(self) -> dict:
        return {
            "pending": self.conn.execute("SELECT count(*) FROM batches").fetchone()[0],
            "rejected": self.conn.execute("SELECT count(*) FROM rejected").fetchone()[0],
            "failed_delivery": self.conn.execute(
                "SELECT count(*) FROM batches WHERE attempts>0"
            ).fetchone()[0],
        }

    def exclude_session(self, session: str) -> None:
        with self.conn:
            rows = self.conn.execute("SELECT id,body FROM batches").fetchall()
            for row in rows:
                if json.loads(row["body"])["session_id"] == session:
                    self.conn.execute("DELETE FROM batches WHERE id=?", (row["id"],))
            self.conn.execute("DELETE FROM calls WHERE session_id=?", (session,))
            self.conn.execute("DELETE FROM cursors WHERE session_id=?", (session,))
            self.conn.execute("DELETE FROM hook_receipts WHERE session_id=?", (session,))
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,0,?)", (session, time.time())
            )
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _capacity(self) -> None:
        options = self.cfg.get("learning", {})
        maximum = options.get("max_spool_bytes", 256 * 1024 * 1024)
        events = options.get("max_spool_events", 100000)
        if type(maximum) is not int or maximum < 1 or type(events) is not int or events < 1:
            raise ValueError("invalid spool capacity")
        used = (
            self.conn.execute(
                "SELECT coalesce(sum(length(CAST(body AS BLOB))),0) FROM batches"
            ).fetchone()[0]
            + self.conn.execute(
                "SELECT coalesce(sum(length(CAST(command AS BLOB))),0) FROM calls"
            ).fetchone()[0]
        )
        count = sum(
            self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("batches", "calls", "sessions", "rejected", "cursors", "hook_receipts")
        )
        if used > maximum or count > events:
            raise SpoolFull("capture remains at its previous committed cursor")

    def _queue(self, session: str, client: str, path_id: str, offset: int, records: list[dict]):
        if not records:
            return
        groups: list[list[dict]] = []
        parts: list[dict] = []
        size = 0
        for record in records:
            length = len(record["content"])
            if length > BATCH_CONTENT_CHARS:
                raise ValueError("source exceeds capture batch limit")
            if parts and (len(parts) >= BATCH_CHUNKS or size + length > BATCH_CONTENT_CHARS):
                groups.append(parts)
                parts, size = [], 0
            parts.append(record)
            size += length
        if parts:
            groups.append(parts)
        for parts in groups:
            identity = digest(
                json.dumps(
                    [
                        self.cfg["namespace"],
                        self.cfg["recall_root"],
                        client,
                        session,
                        [r["source_id"] for r in parts],
                    ]
                )
            )
            body = {
                "batch_id": identity,
                "source_client": client,
                "session_id": session,
                "namespace": self.cfg["namespace"],
                "root": self.cfg["recall_root"],
                "records": parts,
                "cursor": {"path_id": path_id, "offset": offset},
            }
            self.conn.execute(
                "INSERT OR IGNORE INTO batches(id,body,created) VALUES (?,?,?)",
                (identity, json.dumps(body, ensure_ascii=False), time.time()),
            )

    def _parts(self, identity: str, items: list[dict]) -> list[dict]:
        records = []
        for index, item in enumerate(items):
            item = _scrub(item)
            content = item["content"]
            item = {
                **item,
                "observed_at": item.get("observed_at")
                or dt.datetime.now(dt.timezone.utc).isoformat(),
                "metadata": {
                    "authority": "direct_user" if item["role"] == "user" else "untrusted",
                    "direct_user": item["role"] == "user",
                    "kind": item.get("kind", "message"),
                    "phase": item.get("phase", ""),
                    **item.get("metadata", {}),
                },
            }
            record = digest(
                self.cfg["namespace"]
                + ":"
                + self.cfg["recall_root"]
                + ":"
                + identity
                + ":"
                + str(index)
            )
            if not content:
                continue
            if not safe_content(self.cfg, content) or not safe_content(
                self.cfg, json.dumps(item, ensure_ascii=False)
            ):
                self.conn.execute(
                    "INSERT OR IGNORE INTO rejected VALUES (?,?,?)",
                    (record, "secret_or_redaction", time.time()),
                )
                continue
            record_hash = digest(content)
            step = CHUNK_CHARS - CHUNK_OVERLAP
            count = 1 + (max(0, len(content) - CHUNK_CHARS) + step - 1) // step
            for part in range(count):
                start = part * step
                end = min(len(content), start + CHUNK_CHARS)
                text = content[start:end]
                records.append(
                    {
                        **item,
                        "content": text,
                        "source_id": digest(record + ":" + str(part)),
                        "record_id": record,
                        "part_index": part,
                        "part_count": count,
                        "source_hash": digest(text),
                        "record_hash": record_hash,
                        "record_start": start,
                        "record_end": end,
                    }
                )
        return records

    def capture(self, event: dict, client: str = "codex") -> dict:
        session = str(event.get("session_id") or "")
        if not session:
            return {"status": "disabled"}
        if not allowed(self.cfg, session):
            with self.conn:
                self.conn.execute("UPDATE sessions SET eligible=0 WHERE id=?", (session,))
            return {"status": "disabled"}
        transcript = event.get("transcript_path")
        path = Path(transcript).expanduser() if isinstance(transcript, str) and transcript else None
        path_id = digest(str(path.resolve())) if path else "hook"
        count = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT eligible FROM sessions WHERE id=?", (session,)
            ).fetchone()
            if row is None:
                fresh = event.get("hook_event_name") == "SessionStart" and event.get("source") in (
                    "startup",
                    "clear",
                    "new",
                )
                self.conn.execute(
                    "INSERT INTO sessions VALUES (?,?,?)", (session, int(fresh), time.time())
                )
                row = {"eligible": fresh}
            if not row["eligible"]:
                self.conn.commit()
                return {"status": "not_new_session"}
            if path is not None and path.is_file():
                cursor = self.conn.execute(
                    "SELECT offset FROM cursors WHERE session_id=? AND path_id=?",
                    (session, path_id),
                ).fetchone()
                offset = int(cursor[0]) if cursor else 0
                if path.stat().st_size < offset:
                    # Do not reinterpret replaced/truncated history as new evidence.
                    raise ValueError("transcript moved backwards")
                captured = []
                with path.open("rb") as source:
                    source.seek(offset)
                    while True:
                        start = source.tell()
                        line = source.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        end = source.tell()
                        try:
                            record = json.loads(line)
                            items = normalized(record, client) if isinstance(record, dict) else []
                        except (ValueError, UnicodeError):
                            raise ValueError("invalid transcript record") from None
                        identity = ":".join(
                            (client, session, path_id, str(start), digest(line.decode("utf-8")))
                        )
                        for item in items:
                            call_id = item.get("call_id")
                            if call_id and item.get("tool_name"):
                                command = item["content"]
                                with contextlib.suppress(ValueError):
                                    arguments = json.loads(command)
                                    if isinstance(arguments, dict):
                                        command = str(
                                            arguments.get("cmd")
                                            or arguments.get("command")
                                            or command
                                        )
                                if safe_content(self.cfg, command):
                                    self.conn.execute(
                                        "INSERT OR REPLACE INTO calls VALUES (?,?,?,?)",
                                        (session, call_id, item["tool_name"], command),
                                    )
                            elif call_id:
                                call = self.conn.execute(
                                    "SELECT name,command FROM calls "
                                    "WHERE session_id=? AND call_id=?",
                                    (session, call_id),
                                ).fetchone()
                                if call:
                                    item["tool_name"] = call[0]
                                    item["metadata"] = {"command": call[1]}
                            if item.get("kind") == "tool_result":
                                item.setdefault("metadata", {}).update(observation(item["content"]))
                        parts = self._parts(identity, items)
                        captured.extend(parts)
                        count += len(parts)
                        offset = end
                self._queue(session, client, path_id, offset, captured)
                self.conn.execute(
                    "INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (session, path_id, offset)
                )
            else:
                # The supported hook fallback is used only without a readable native transcript.
                event_id = str(
                    event.get("event_id")
                    or event.get("tool_use_id")
                    or event.get("call_id")
                    or uuid.uuid4().hex
                )
                identity = ":".join(
                    (client, session, "hook", str(event.get("hook_event_name")), event_id)
                )
                # Keep a hashed receipt after delivery deletes the batch. A hook
                # retry must not regenerate timestamps or create a second job.
                items = hook_records(event)
                if items:
                    key = digest(identity)
                    payload_hash = digest(json.dumps(items, ensure_ascii=False, sort_keys=True))
                    receipt = self.conn.execute(
                        "SELECT payload_hash FROM hook_receipts WHERE event_key=?", (key,)
                    ).fetchone()
                    if receipt and receipt[0] != payload_hash:
                        raise ValueError("hook event identity collision")
                    if receipt is None:
                        parts = self._parts(identity, items)
                        self._queue(session, client, "hook", 0, parts)
                        self.conn.execute(
                            "INSERT INTO hook_receipts VALUES (?,?,?,?)",
                            (key, session, payload_hash, time.time()),
                        )
                        count += len(parts)
            if event.get("hook_event_name") == "SessionEnd":
                self.conn.execute("DELETE FROM calls WHERE session_id=?", (session,))
            self._capacity()
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return {"status": "captured", "chunks": count}

    def drain(self, send=None, limit: int = 16) -> dict:
        """A failed/ambiguous receipt retains the batch. Replays use identical IDs."""
        send = send or (lambda payload: deliver(self.cfg, payload))
        sent = 0
        after = None
        while sent < limit:
            params: list[Any] = [time.time()]
            condition = ""
            if after is not None:
                condition = " AND (created,id)>(?,?)"
                params.extend(after)
            rows = self.conn.execute(
                "SELECT * FROM batches WHERE next_attempt<=?"
                + condition
                + " ORDER BY created,id LIMIT 64",
                params,
            ).fetchall()
            if not rows:
                break
            for row in rows:
                after = (row["created"], row["id"])
                payload = json.loads(row["body"])
                # Paused batches stay durable. Do not let them block eligible
                # work from another session, even across many pages of batches.
                if not allowed(self.cfg, payload["session_id"]):
                    continue
                try:
                    result = send(payload)
                    if (
                        not isinstance(result, dict)
                        or result.get("accepted") is not True
                        or result.get("batch_id") != row["id"]
                    ):
                        raise ValueError("missing matching receipt")
                except Exception as exc:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE batches SET attempts=attempts+1,"
                            "next_attempt=?,error=? WHERE id=?",
                            (
                                time.time() + min(3600, 2 ** min(row["attempts"] + 1, 12)),
                                type(exc).__name__,
                                row["id"],
                            ),
                        )
                    return {"sent": sent, "pending": self.status()["pending"]}
                with self.conn:
                    self.conn.execute("DELETE FROM batches WHERE id=?", (row["id"],))
                sent += 1
                if sent >= limit:
                    break
        return {
            "sent": sent,
            "pending": self.conn.execute("SELECT count(*) FROM batches").fetchone()[0],
        }


def deliver(cfg: dict, payload: dict) -> dict:
    argv = cfg.get("learning", {}).get("delivery_argv")
    if argv:
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("delivery_argv must be a list")
        proc = subprocess.run(
            argv, input=json.dumps(payload), text=True, capture_output=True, timeout=15, check=False
        )
        if proc.returncode:
            raise OSError("delivery process failed")
        return json.loads(proc.stdout)
    module = load_module(Path(cfg["repo_root"]) / "hooks/store_client.py", "_capture_store_client")
    env = {**os.environ, "NOBLIVION_DATA_DIR": str(cfg["data_dir"])}

    def get_json(url, timeout):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout)
        try:
            conn.request("GET", parsed.path + "?" + parsed.query)
            response = conn.getresponse()
            if response.status != 200:
                raise OSError("listener proof failed")
            return json.loads(response.read(65536))
        finally:
            conn.close()

    base, token = module.connect(env, get_json, 1)
    from urllib.parse import urlsplit

    conn = http.client.HTTPConnection("127.0.0.1", urlsplit(base).port, timeout=5)
    try:
        conn.request(
            "POST",
            "/learning/capture",
            json.dumps(payload).encode("utf-8"),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("capture not acknowledged")
        return json.loads(response.read(65536))
    finally:
        conn.close()


def wake(cfg: dict) -> None:
    """Delivery runs detached; capture is already durable when the hook returns."""
    # Persist only the private scoped settings needed to retry this spool.
    folder = spool_folder(cfg)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (
        "delivery-" + digest(cfg["namespace"] + ":" + cfg["recall_root"])[:16] + ".json"
    )
    temp = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(cfg, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--config", str(path), "--drain"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def capture_hook(cfg: dict, event: dict, client: str = "codex") -> dict:
    scoped = scoped_config(cfg, event)
    if scoped is None:
        return {"status": "disabled"}
    enabled = allowed(scoped, str(event.get("session_id") or ""))
    if not enabled and not (spool_folder(scoped) / "capture.sqlite3").exists():
        return {"status": "disabled"}
    spool = Spool(scoped)
    try:
        result = spool.capture(event, client)
    finally:
        spool.close()
    if enabled:
        wake(scoped)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--receive", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--exclude-session")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.drain and cfg.get("source_config"):
        current = json.loads(Path(cfg["source_config"]).read_text(encoding="utf-8"))
        refreshed = scoped_config(current, {"cwd": cfg.get("workspace", "")})
        if refreshed is None:
            return 0
        cfg = refreshed
    if args.status or args.exclude_session:
        spool = Spool(cfg)
        try:
            if args.exclude_session:
                spool.exclude_session(args.exclude_session)
            print(json.dumps(spool.status()))
        finally:
            spool.close()
    elif args.receive:
        payload = json.load(sys.stdin)
        # Remote gateways must explicitly authorize the namespace/root pair too.
        scopes = [(m.get("namespace"), m.get("root")) for m in cfg.get("project_mappings", [])]
        if (payload.get("namespace"), payload.get("root")) not in scopes:
            raise ValueError("capture scope is not authorized")
        cfg = {**cfg, "learning": {**cfg.get("learning", {}), "delivery_argv": None}}
        print(json.dumps(deliver(cfg, payload)))
    elif args.drain:
        import fcntl

        lock = spool_folder(cfg) / "delivery.lock"
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            spool = Spool(cfg)
            try:
                print(json.dumps(spool.drain()))
            finally:
                spool.close()
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())
