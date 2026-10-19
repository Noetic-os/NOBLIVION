# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tools/replay_recall_nudge.py (NOBLIVION-123), on made-up transcripts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
RECALL = "mcp__plugin_noblivion_noblivion__noblivion_recall"
TICKET = "mcp__tickets__createJiraIssue"


@pytest.fixture()
def rn(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NOBLIVION_CONFIG", str(cfg))
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location(
        "replay_recall_nudge", TOOLS / "replay_recall_nudge.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["replay_recall_nudge"] = mod
    spec.loader.exec_module(mod)
    yield mod
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def rec(ident, name, tin=None, ts="2026-10-01T10:00:00.000Z", sidechain=False):
    return json.dumps(
        {
            "type": "assistant",
            "isSidechain": sidechain,
            "timestamp": ts,
            "message": {
                "content": [{"type": "tool_use", "id": ident, "name": name, "input": tin or {}}]
            },
        }
    )


def write(tmp_path, name, records):
    path = tmp_path / name
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def test_counts_fires_cooldowns_and_recalls(rn, tmp_path):
    records = (
        [rec("a1", "Read"), rec("t1", TICKET, {"summary": "Fix it"})]  # fires
        + [rec(f"r{i}", "Read") for i in range(3)]
        + [rec("t2", TICKET, {"summary": "Again"})]  # cooldown
        + [rec("q1", RECALL, {"query": "x"})]  # follows the first line
        + [rec("t3", TICKET)]  # recent recall
        + [rec("m1", "Bash", {"command": "cd w && gh pr merge 5 --squash"})]  # recent recall
        + [rec(f"x{i}", "Read") for i in range(11)]
        + [rec("m2", "Bash", {"command": "gh pr merge 6"})]  # fires
        + [rec("m3", "Bash", {"command": 'echo "gh pr merge 7"'})]  # not a match
    )
    path = write(tmp_path, "s1.jsonl", records)
    env = {}
    nudge = rn._load_nudge()
    c = rn.replay_session(nudge, str(path), "", env)
    assert c["tool.matches"] == 3 and c["tool.fire"] == 1
    assert c["tool.cooldown"] == 1 and c["tool.recent_recall"] == 1
    assert c["tool.followed"] == 1
    assert c["command.matches"] == 2 and c["command.recent_recall"] == 0 + 1
    assert c["command.fire"] == 1
    assert c["recall_calls"] == 1 and c["fetch_calls"] == 0
    assert c["fires"] == 2 and c["chars"] > 0


def test_report_has_counts_only(rn, tmp_path, capsys):
    write(tmp_path, "s1.jsonl", [rec("a", "Read"), rec("t", TICKET, {"summary": "PRIVATE-WORDS"})])
    write(tmp_path, "s2.jsonl", [rec("b", "Read")])
    sub = tmp_path / "s1" / "subagents"
    sub.mkdir(parents=True)
    write(sub, "agent-1.jsonl", [rec("t", TICKET)])
    assert (
        rn.main(
            [str(tmp_path / "*.jsonl"), str(tmp_path / "*" / "subagents" / "*.jsonl"), "--json"]
        )
        == 0
    )
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["sessions"] == 2  # the subagent transcript is left out
    assert doc["sessions_with_match"] == 1 == doc["sessions_with_fire"]
    assert doc["moments"]["tool"]["fire"] == 1
    assert doc["nudge_tokens_est"] == round(doc["nudge_chars"] / 4)
    assert "PRIVATE-WORDS" not in out


def test_sidechain_calls_and_old_calls(rn, tmp_path):
    nudge = rn._load_nudge()
    old = "2026-01-01T00:00:00.000Z"
    path = write(
        tmp_path,
        "s.jsonl",
        [
            rec("o1", TICKET, ts=old),  # before the cutoff: not counted, but it fills the window
            rec("s1", RECALL, sidechain=True),  # a subagent call: not in the window
            rec("t2", TICKET, ts="2026-10-02T00:00:00.000Z"),
        ],
    )
    c = rn.replay_session(nudge, str(path), "2026-06-01T00:00:00", {})
    assert c["tool.matches"] == 1
    assert c["tool.cooldown"] == 1  # the old call t1 was a line, still inside the window
    assert c.get("recall_calls", 0) == 0


def test_script_runs_as_a_program(tmp_path):
    write(tmp_path, "s.jsonl", [rec("t", TICKET)])
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "replay_recall_nudge.py"), str(tmp_path / "*.jsonl")],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NOBLIVION_CONFIG": str(tmp_path / "none.json"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "sessions: 1" in proc.stdout and "fires: 1" in proc.stdout
