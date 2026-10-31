# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm a: what the installed build did in real sessions, as counts.

The program reads three kinds of live file, always read-only and never past a
complete line: the transcripts (``~/.claude/projects/*/*.jsonl``), the plugin logs
(``recall.log``, ``guard-log.jsonl``, ``error-recall-log.jsonl``) and the token-free
``/health`` route of the live store. It never reads the ``token`` file and never
calls the store with it. Each file is followed by inode and byte offset in a state
file, so a restart resumes and a rotation is noticed.

Output rows hold counts, ids, times and salted hashes: no prompt, command or note
text. A guard-log line has a ``text`` field (a command or a path): ``parse_guard_line``
drops it before anything is stored.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .util import salted_hash, summarize_ms

RECALL_RE = re.compile(
    r"^(?P<ts>\S+) event=(?P<event>\S+) session=(?P<session>\S+) "
    r"hits=(?P<hits>\d+) chars=(?P<chars>\d+) ms=(?P<ms>\d+)(?P<rest>.*)$"
)


def read_new(path: Path, state: dict[str, Any], limit: int = 8 << 20) -> list[str]:
    """The complete lines added to ``path`` since the last call. ``state`` holds the
    inode and the offset of the file and is updated in place. A changed inode or a
    shorter file means a rotation: the file is read from its start."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path)
    entry = state.setdefault(key, {"inode": stat.st_ino, "offset": 0})
    if entry["inode"] != stat.st_ino or stat.st_size < entry["offset"]:
        entry.update(inode=stat.st_ino, offset=0)
    if stat.st_size == entry["offset"]:
        return []
    with open(path, "rb") as fh:
        fh.seek(entry["offset"])
        data = fh.read(limit)
    end = data.rfind(b"\n")
    if end < 0:
        return []
    entry["offset"] += end + 1
    return data[: end + 1].decode("utf-8", errors="replace").splitlines()


def parse_recall_line(line: str) -> dict[str, Any] | None:
    m = RECALL_RE.match(line)
    if not m:
        return None
    d = m.groupdict()
    tags = d["rest"].strip().split(":")
    return {
        "ts": d["ts"],
        "event": d["event"],
        "session": d["session"],
        "hits": int(d["hits"]),
        "chars": int(d["chars"]),
        "ms": int(d["ms"]),
        "fail": " fail:" in line,
        "state": tags[0] if tags and tags[0] else "",
    }


def parse_guard_line(line: str, salt: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    return {
        "ts": doc.get("ts", ""),
        "session": doc.get("session_id", ""),
        "tool": doc.get("tool", ""),
        "decision": doc.get("decision", ""),
        "ids": len(doc.get("ids") or []),
        "who": salted_hash(salt, str(doc.get("agent_id", ""))),
    }  # the text field is dropped here


def parse_error_line(line: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    return {
        "ts": doc.get("ts", ""),
        "session": doc.get("session_id", ""),
        "shown": bool(doc.get("shown", doc.get("ids"))),
        "ids": len(doc.get("ids") or []),
    }


def transcript_counts(lines: list[str]) -> dict[str, Any]:
    """Counts from transcript lines: API calls, input tokens, tool calls, user
    prompts, and the time of every API call (for the re-sent token estimate)."""
    calls: list[str] = []
    tokens = tools = prompts = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") or {}
        if rec.get("type") == "assistant":
            usage = msg.get("usage") or {}
            tokens += sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            calls.append(str(rec.get("timestamp", "")))
            content = msg.get("content")
            if isinstance(content, list):
                tools += sum(
                    1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                )
        elif rec.get("type") == "user" and isinstance(msg.get("content"), str):
            prompts += 1
    return {
        "calls": len(calls),
        "call_times": calls,
        "input_tokens": tokens,
        "tool_calls": tools,
        "prompts": prompts,
    }


def resent_tokens(inject_times: list[tuple[str, int]], call_times: list[str]) -> int:
    """Characters / 4 times the later API calls of the same session (an upper bound:
    compaction is ignored). ``inject_times`` holds (timestamp, characters)."""
    total = 0
    for ts, chars in inject_times:
        later = sum(1 for t in call_times if t > ts)
        total += (chars // 4) * later
    return total


def transcripts(root: Path) -> Iterator[Path]:
    yield from sorted(Path(root).glob("*/*.jsonl"))


def probe_health(data_dir: Path, timeout: float = 3.0) -> dict[str, Any] | None:
    """The token-free ``/health`` route of a store, on loopback only. It reads
    ``store.json`` for the port and nothing else in the folder."""
    try:
        info = json.loads((Path(data_dir) / "store.json").read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/health"
        with urllib.request.urlopen(url, timeout=timeout) as answer:  # noqa: S310
            return json.loads(answer.read())
    except (OSError, ValueError, KeyError):
        return None


class Live:
    """One pass over the live files; the state decides what is new."""

    def __init__(self, config_dir: Path, data_dir: Path, state: dict[str, Any], salt: str) -> None:
        self.config_dir, self.data_dir = Path(config_dir), Path(data_dir)
        self.state, self.salt = state, salt

    def pass_once(self) -> dict[str, Any]:
        recall = [
            r
            for ln in read_new(self.data_dir / "cache" / "recall.log", self.state)
            if (r := parse_recall_line(ln))
        ]
        guard = [
            r
            for ln in read_new(self.data_dir / "guard-log.jsonl", self.state)
            if (r := parse_guard_line(ln, self.salt))
        ]
        errors = [
            r
            for ln in read_new(self.data_dir / "error-recall-log.jsonl", self.state)
            if (r := parse_error_line(ln))
        ]
        sessions: dict[str, dict[str, Any]] = {}
        for path in transcripts(self.config_dir / "projects"):
            lines = read_new(path, self.state)
            if lines:
                sessions[path.stem] = transcript_counts(lines)
        return {"recall": recall, "guard": guard, "errors": errors, "sessions": sessions}


def daily(pass_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts over the passes of one day."""
    recall = [r for p in pass_rows for r in p["recall"]]
    guard = [r for p in pass_rows for r in p["guard"]]
    errors = [r for p in pass_rows for r in p["errors"]]
    sessions: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "call_times": [], "input_tokens": 0, "tool_calls": 0, "prompts": 0}
    )
    for p in pass_rows:
        for sid, c in p["sessions"].items():
            s = sessions[sid]
            for k in ("calls", "input_tokens", "tool_calls", "prompts"):
                s[k] += c[k]
            s["call_times"] += c["call_times"]
    injected: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for r in recall:
        injected[r["session"]].append((r["ts"].replace("+00:00", "Z"), r["chars"]))
    resent = sum(
        resent_tokens(v, sessions[sid]["call_times"])
        for sid, v in injected.items()
        if sid in sessions
    )
    total_in = sum(s["input_tokens"] for s in sessions.values())
    by_decision: dict[str, int] = defaultdict(int)
    for g in guard:
        by_decision[g["decision"]] += 1
    return {
        "prompt_hooks": len(recall),
        "prompt_rows": sum(r["hits"] for r in recall),
        "prompt_chars": summarize_ms(r["chars"] for r in recall),
        "hook_ms": summarize_ms(r["ms"] for r in recall),
        "fail_open": sum(1 for r in recall if r["fail"]),
        "guard_events": dict(by_decision),
        "error_recall_shows": sum(1 for e in errors if e["shown"]),
        "sessions": len(sessions),
        "api_calls": sum(s["calls"] for s in sessions.values()),
        "input_tokens": total_in,
        "resent_tokens_estimate": resent,
        "plugin_share_of_input": round(resent / total_in, 5) if total_in else 0.0,
    }


def live_paths_guard(config_dir: Path, data_dir: Path) -> list[Path]:
    """The folders that the open guard treats as live: nothing here is written."""
    return [Path(config_dir).expanduser(), Path(data_dir).expanduser()]


def private_mode_ok(path: Path) -> bool:
    return (os.stat(path).st_mode & 0o077) == 0
