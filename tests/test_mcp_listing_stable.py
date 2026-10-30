# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tools/list`` and the server instructions are the same bytes on every start.

Claude Code puts both in the cached part of a request. A count, a date or a
version in them would change on some start, and that start would rewrite the
whole prompt cache (finding R2-F14 of the review of 2026-10-30, NOBLIVION-145).
Each start here is a new process with another folder, another data folder and
another store state, so a text built from any of them fails the test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from noblivion import config, db
from store_helpers import add_memory

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "mcp" / "recall_mcp.py"
REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
    )
    + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    + "\n"
)


def start(cwd: Path, data: Path, **extra: str) -> tuple[bytes, bytes]:
    """The raw ``tools/list`` line and the instruction bytes of one server start."""
    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, NOBLIVION_DATA_DIR=str(data), **extra)
    run = subprocess.run(
        [sys.executable, str(SERVER)],
        input=REQUEST.encode(),
        capture_output=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr[-500:]
    lines = [line for line in run.stdout.split(b"\n") if line.strip()]
    answers = {json.loads(line)["id"]: line for line in lines}
    instructions = json.loads(answers[1])["result"].get("instructions", "")
    return answers[2], instructions.encode()


def test_two_starts_give_the_same_bytes(tmp_path: Path) -> None:
    empty = tmp_path / "empty-data"
    empty.mkdir()
    full = tmp_path / "full-data"
    conn = db.open_db(full / config.DB_FILE, create=True)
    add_memory(conn, "one-note", "a note, so the two stores differ")
    conn.close()
    first = start(tmp_path / "a", empty)
    second = start(tmp_path / "other" / "folder", full, CLAUDE_PROJECT_DIR=str(tmp_path))
    assert first[0] == second[0], "tools/list changed between two starts"
    assert first[1] == second[1], "the server instructions changed between two starts"
    assert first[1], "the server gave no instructions: this test compared nothing"


def test_a_changed_text_fails_the_comparison(tmp_path: Path) -> None:
    """The negative control: the switch below changes the instructions, and the
    comparison above must see it."""
    data = tmp_path / "data"
    data.mkdir()
    plain = start(tmp_path / "a", data)
    changed = start(tmp_path / "a", data, NOBLIVION_MCP_INSTRUCTIONS="0")
    assert plain[1] != changed[1]


def test_the_texts_hold_no_date_and_no_version(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    tools, instructions = start(tmp_path / "a", data)
    text = tools.decode() + instructions.decode()
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] not in text
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)
    assert not re.search(r"\b\d+\.\d+\.\d+\b", text)
