# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion stop report`` counts the stop check log rows per check.

Every test points the log at a tmp file (``NOBLIVION_STOP_CHECK_LOG``) and the
home folder and the data dir at tmp folders, so no test reads or writes the
live files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from noblivion import __main__ as cli
from noblivion import stop_report

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "stop_checks.py"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name in ("CLAUDE_PLUGIN_DATA", "XDG_DATA_HOME"):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    data = home / ".local" / "share" / "noblivion"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    log = tmp_path / "stop-check-log.jsonl"
    monkeypatch.setenv("NOBLIVION_STOP_CHECK_LOG", str(log))
    return log


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime(stop_report.TS_FORMAT)


def _row(days_ago, mode="shadow", would=(), blocking=(), shadow=None, **extra):
    return {
        "ts": _ts(days_ago),
        "event": "stop",
        "verdict": "x",
        "mode": mode,
        "would_block": list(would),
        "blocking": list(blocking),
        "shadow": list(would) if shadow is None else list(shadow),
        **extra,
    }


def test_counts_per_check_inside_the_window():
    rows = [
        _row(1, would=["commit", "tests"]),
        _row(2, would=["commit"]),
        _row(3, mode="enforce", blocking=["lesson"], shadow=[]),
        _row(4, would=[], shadow=["deploy"]),  # fired, not on: logged only
        _row(5, notes={"notify_skipped": "no PushNotification tool"}),
        _row(30, would=["commit"]),  # outside the window
        {"ts": _ts(1), "event": "prompt", "verdict": "marked"},
        {"ts": "bad", "event": "stop", "would_block": ["commit"]},
    ]
    answer = stop_report.build_report(rows, 7, now=NOW)
    assert answer["stops"] == 5
    assert answer["modes"] == {"shadow": 4, "enforce": 1}
    assert answer["checks"]["commit"] == {"would_block": 2, "blocked": 0, "logged_only": 0}
    assert answer["checks"]["tests"]["would_block"] == 1
    assert answer["checks"]["lesson"]["blocked"] == 1
    assert answer["checks"]["deploy"]["logged_only"] == 1
    assert answer["notify_skipped"] == 1
    assert list(answer["checks"]) == ["commit", "tests", "deploy", "lesson"]


def test_rows_before_the_mode_field_count_as_enforce():
    old = {"ts": _ts(1), "event": "stop", "blocking": ["commit"], "shadow": ["notify"]}
    answer = stop_report.build_report([old], 7, now=NOW)
    assert answer["modes"] == {"enforce": 1}
    assert answer["checks"]["commit"]["blocked"] == 1
    assert answer["checks"]["notify"]["logged_only"] == 1


def test_reads_the_rotated_file_and_skips_broken_lines(env):
    Path(str(env) + ".1").write_text(json.dumps(_row(0, would=["tests"])) + "\n")
    env.write_text("not json\n" + json.dumps(_row(0, would=["commit"])) + "\n")
    rows = list(stop_report.read_rows(env))
    assert [r["would_block"] for r in rows] == [["tests"], ["commit"]]


def test_missing_log_is_an_empty_report(env, capsys):
    assert cli.main(["stop", "report"]) == 0
    out = capsys.readouterr().out
    assert "0 stops" in out and "No check fired." in out


def test_the_cli_counts_rows_the_hook_wrote(env, tmp_path, capsys):
    transcript = tmp_path / "t.jsonl"
    rows = [
        {
            "type": "attachment",
            "attachment": {"type": "deferred_tools_delta", "addedNames": ["PushNotification"]},
        },
        {"type": "user", "cwd": "/home/user", "message": {"role": "user", "content": "x"}},
        {
            "type": "assistant",
            "cwd": "/home/user",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I deferred it."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for n in range(2):
        res = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"session_id": f"s{n}", "transcript_path": str(transcript)}),
            capture_output=True,
            text=True,
            timeout=30,
            env=dict(os.environ),
        )
        assert res.returncode == 0 and res.stdout.strip() == ""  # shadow: never blocks
    assert cli.main(["stop", "report", "--json", "--days", "1"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["stops"] == 2 and answer["modes"] == {"shadow": 2}
    assert answer["checks"]["notify"]["would_block"] == 2
    assert cli.main(["stop", "report"]) == 0
    assert "- notify: would block 2, blocked 0" in capsys.readouterr().out
