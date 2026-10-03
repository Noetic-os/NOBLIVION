# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart line about memory upkeep.

What these tests hold:

1. It reads the two cache files only (the trust report cache and the dedup
   status file) and prints at most two lines: the trust counts and the dedup
   counts. No line for a missing or broken file, none for all-zero counts,
   none at source ``compact``. A dry run says so; an old run names its date.
   It takes under 5 ms.
2. Without the file variables it reads both files from ``<data dir>/cache``.
3. A refused nightly dedup sweep prints one short, clean line, except for the
   "no gate configured" refusal, which is silent. A failed run outranks a
   refusal.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hookload import HOOKS, load_hook

sl = load_hook("trust_session_line", "trust_session_line_t_trust_session_line")

NOW = dt.datetime(2026, 10, 2, 7, 0, tzinfo=dt.timezone.utc)
TRUST_ENV = "NOBLIVION_TRUST_REPORT_FILE"
DEDUP_ENV = "NOBLIVION_DEDUP_STATUS_FILE"


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited settings."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    return {"home": home, "data": data}


def row(mid: int, path: str, **kw: Any) -> dict[str, Any]:
    d = {
        "mv_id": mid,
        "path": path,
        "trust": 0.2,
        "trials": 0,
        "shown_sessions": 25,
        "use_sessions": 0,
        "reason": "shown in 25 sessions, never used",
    }
    d.update(kw)
    return d


REPORT = {
    "persona": "claude_code",
    "generated_at": "2026-10-02T03:00:00+00:00",
    "trust_prior": 0.5,
    "retire": [row(1, "feedback_a.md"), row(2, "feedback_b.md")],
    "promote": [row(3, "feedback_c.md", trust=0.8, trials=9, use_sessions=9)],
    "demote": [],
}
DEDUP = {
    "stamp": "s",
    "finished_at": "2026-10-02T03:10:00+00:00",
    "mode": "apply",
    "merged": 4,
    "proposals": 2,
    "refused": 1,
}
TRUST_LINE = "Memory trust: 2 to retire, 1 to promote, 0 to demote. Run noblivion trust report"
DEDUP_LINE = "Memory dedup: 4 merged on 2026-10-02, 2 proposals"


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    trust = tmp_path / "trust-report.json"
    dedup = tmp_path / "dedup-last-run.json"
    trust.write_text(json.dumps(dict(REPORT, counts={"retire": 2, "promote": 1, "demote": 0})))
    dedup.write_text(json.dumps(DEDUP))
    return {TRUST_ENV: str(trust), DEDUP_ENV: str(dedup)}


def test_both_lines_from_the_cache_files(files):
    assert sl.lines(files, NOW) == [TRUST_LINE, DEDUP_LINE]


def test_the_default_files_are_in_the_data_dir_cache(hook_env):
    """Without the file variables both files come from ``<data dir>/cache``."""
    cache = hook_env["data"] / "cache"
    cache.mkdir()
    env = {"NOBLIVION_DATA_DIR": str(hook_env["data"])}
    assert sl.lines(env, NOW) == []
    (cache / "trust-report.json").write_text(json.dumps(REPORT))
    (cache / "dedup-last-run.json").write_text(json.dumps(DEDUP))
    assert sl.lines(env, NOW) == [TRUST_LINE, DEDUP_LINE]


def test_no_line_for_a_missing_or_broken_file(tmp_path, files):
    missing = {TRUST_ENV: str(tmp_path / "x"), DEDUP_ENV: str(tmp_path / "y")}
    assert sl.lines(missing, NOW) == []
    Path(files[TRUST_ENV]).write_text("{not json")
    Path(files[DEDUP_ENV]).write_text(json.dumps({"merged": "4", "proposals": 1}))
    assert sl.lines(files, NOW) == []


def test_counts_come_from_the_lists_when_the_counts_are_absent_and_zero_is_silent(files):
    p = Path(files[TRUST_ENV])
    p.write_text(json.dumps(REPORT))
    line = sl.trust_line(json.loads(p.read_text()))
    assert line.startswith("Memory trust: 2 to retire, 1 to promote")
    assert sl.trust_line(dict(REPORT, retire=[], promote=[], demote=[])) == ""
    zero = {"merged": 0, "proposals": 0, "finished_at": "2026-10-02T03:00:00Z"}
    assert sl.dedup_line(zero, NOW) == ""


def test_a_dry_run_and_a_plan_say_so_and_every_line_names_its_date():
    old = {"mode": "apply", "merged": 3, "proposals": 0, "finished_at": "2026-09-28T03:00:00Z"}
    assert sl.dedup_line(old, NOW) == "Memory dedup: 3 merged on 2026-09-28, 0 proposals"
    dry = {"mode": "dry-run", "pairs": 5, "finished_at": "2026-10-02T03:00:00Z"}
    assert sl.dedup_line(dry, NOW) == (
        "Memory dedup dry run on 2026-10-02: 5 candidate pairs, nothing sent. "
        "Run noblivion dedup plan to judge them"
    )
    assert sl.dedup_line(dict(dry, pairs=0), NOW) == ""
    plan = {"mode": "plan", "run_id": "R1", "merged": 0, "proposals": 2}
    assert sl.dedup_line(plan, NOW) == (
        "Memory dedup plan R1 in the last run: 2 merge proposals. "
        "Read the plan, then run noblivion dedup apply R1"
    )
    assert sl.dedup_line(dict(plan, proposals=0), NOW) == ""
    undo = {"mode": "undo", "run_id": "R1", "merged": 0, "proposals": 0}
    assert sl.dedup_line(undo, NOW) == ""


def test_compact_prints_nothing_and_startup_prints_the_lines(files):
    out = io.StringIO()
    compact = json.dumps({"hook_event_name": "SessionStart", "source": "compact"})
    assert sl.main(io.StringIO(compact), out, files) == 0
    assert out.getvalue() == ""
    startup = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    assert sl.main(io.StringIO(startup), out, files) == 0
    assert out.getvalue().count("\n") == 2


def test_the_hook_takes_under_5_ms(files):
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        sl.main(io.StringIO('{"source": "startup"}'), io.StringIO(), files)
        times.append(time.perf_counter() - t0)
    assert statistics.median(times) < 0.005, statistics.median(times)


def test_the_hook_as_a_subprocess_exits_0_and_prints_the_lines(files):
    r = subprocess.run(
        [sys.executable, str(HOOKS / "trust_session_line.py")],
        input='{"source": "resume"}',
        capture_output=True,
        text=True,
        env={**os.environ, **files},
        timeout=30,
    )
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.splitlines() == [TRUST_LINE, DEDUP_LINE]


# a refused nightly sweep (status file "outcome": "refused") ---------------
def _refused(
    code: Any,
    reason: str = "the gate result /g.json names no chosen judge",
    at: str = "2026-10-02T04:20:05Z",
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "stamp": "20261002T042005Z",
        "finished_at": at,
        "mode": "apply",
        "merged": 0,
        "proposals": 0,
        "refused": 0,
        "outcome": "refused",
        "refused_reason": reason,
    }
    if code is not None:
        doc["refused_code"] = code
    return doc


def test_a_refusal_for_no_gate_configured_prints_nothing():
    """The gate is empty by design until a prompt passes it: no daily noise line."""
    assert sl.dedup_line(_refused("no_gate", "no gate result at /x/gate-result.json"), NOW) == ""


@pytest.mark.parametrize("code", ["gate", None, "something-new"])
def test_any_other_refusal_prints_one_short_line(code):
    line = sl.dedup_line(_refused(code), NOW)
    assert line == (
        "Memory dedup REFUSED on 2026-10-02, nothing merged: "
        "the gate result /g.json names no chosen judge"
    )
    old = sl.dedup_line(_refused(code, at="2026-09-28T04:20:05Z"), NOW)
    assert old.startswith("Memory dedup REFUSED on 2026-09-28, nothing merged: ")


def test_a_refusal_line_is_short_and_has_no_control_characters():
    line = sl.dedup_line(_refused("gate", "x\n\x1b[31m" + "y" * 500), NOW)
    assert "\n" not in line and "\x1b" not in line and len(line) <= 200


def test_a_failed_latch_outranks_a_refusal():
    line = sl.dedup_line(dict(_refused("no_gate"), failed="20261001T042000Z"), NOW)
    assert line.startswith("Memory dedup FAILED in run 20261001T042000Z")
    assert "noblivion dedup undo 20261001T042000Z" in line
