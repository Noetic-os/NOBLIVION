# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count the learner rows and bytes that simulated sessions leave in a new database (NOBLIVION-143).

Each simulated session writes a Claude Code style transcript (made-up text from a seeded random
generator, never real data). The tool feeds hook events to the capture spool of the release
under test, delivers every spooled batch to that release's ledger, and counts what the
database holds. No model runs, no worker claims a job, and the live store is never opened: the
database is a new file in a temporary folder.

``--repo`` names the release folder to measure (default: this one), so one tool run per
release gives a before and an after.

``--events`` picks which hook events reach the spool:

* ``every-event``: the old hook setup. Capture ran on session start, each prompt, each tool call
  and each stop, as well as at compaction and at the end of the session.
* ``session-points``: capture at session start, at each compaction and at the end only.

Report fields (JSON, counts only): ``jobs``, ``job_source_rows`` (one row per linked source),
``job_range_rows`` (one row per range-linked job), ``sources``, ``source_chars``, ``db_bytes``
(file size after VACUUM), and the same figures per simulated day with ``--sessions-per-day``.

Usage::

    python tools/replay_learning_growth.py --events every-event --repo /path/to/old/release
    python tools/replay_learning_growth.py --events session-points
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import tempfile
from pathlib import Path

WORDS = (
    "alpha bravo cedar delta ember fjord grove harbor indigo juniper kestrel lantern meadow "
    "nectar orchard pebble quartz ripple saffron timber umber velvet willow xenon yarrow zephyr "
    "build cache config deploy error fix log merge patch query render shell test update"
).split()


def text(rng: random.Random, chars: int) -> str:
    out: list[str] = []
    size = 0
    while size < chars:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)


def line(kind: str, role: str, block: dict, stamp: int) -> str:
    return json.dumps(
        {
            "type": kind,
            "timestamp": f"2026-10-12T09:{stamp // 60 % 60:02d}:{stamp % 60:02d}Z",
            "message": {"role": role, "content": [block]},
        }
    )


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(repo: Path, events: str, sessions: int, tools: int, compactions: int, seed: int) -> dict:
    sys.path.insert(0, str(repo / "src"))
    from noblivion import db
    from noblivion.learning import LearningLedger

    capture = load(repo / "codex/capture.py", "_growth_capture")
    rng = random.Random(seed)
    every = events == "every-event"
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        base.chmod(0o700)
        conn = db.open_db(base / "growth.db", create=True)
        ledger = LearningLedger(conn)
        cfg = {
            "repo_root": str(repo),
            "state_dir": str(base / "state"),
            "namespace": "claude_code",
            "recall_root": "sample",
            "learning": {"contribute": True},
        }
        spool = capture.Spool(cfg)

        def deliver() -> None:
            for row in spool.conn.execute("SELECT id,body FROM batches ORDER BY created,id"):
                ledger.capture_batch(json.loads(row[1]))
                with spool.conn:
                    spool.conn.execute("DELETE FROM batches WHERE id=?", (row[0],))

        def fire(name: str, session: str, path: Path, **extra) -> None:
            spool.capture(
                {
                    "hook_event_name": name,
                    "session_id": session,
                    "transcript_path": str(path),
                    "timestamp": "2026-10-12T09:00:00Z",
                    **extra,
                },
                "claude",
            )
            deliver()

        for number in range(sessions):
            session = f"session-{number}"
            path = base / (session + ".jsonl")
            stamp = 0
            fire("SessionStart", session, path, source="startup")
            compact_at = {tools * (n + 1) // (compactions + 1) for n in range(compactions)}
            for turn in range(tools):
                stamp += 1
                call = f"{session}-call-{turn}"
                with path.open("a", encoding="utf-8") as out:
                    if turn % 5 == 0:
                        block = {"type": "text", "text": text(rng, 300)}
                        out.write(line("user", "user", block, stamp) + "\n")
                    block = {
                        "type": "tool_use",
                        "id": call,
                        "name": "Bash",
                        "input": {"command": text(rng, 60)},
                    }
                    out.write(line("assistant", "assistant", block, stamp) + "\n")
                    block = {"type": "tool_result", "tool_use_id": call, "content": text(rng, 900)}
                    out.write(line("user", "user", block, stamp) + "\n")
                if every:
                    fire(
                        "PostToolUse",
                        session,
                        path,
                        event_id=call,
                        tool_name="Bash",
                        tool_input={"command": "x"},
                        tool_response={"output": "y"},
                    )
                    if turn % 5 == 4:
                        fire("Stop", session, path, last_assistant_message="done")
                if turn in compact_at:
                    fire("PreCompact", session, path)
            fire("SessionEnd", session, path)
        spool.close()
        scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        tables = {
            "jobs": scalar("SELECT count(*) FROM learning_jobs"),
            "job_source_rows": scalar("SELECT count(*) FROM learning_job_sources"),
            "sources": scalar("SELECT count(*) FROM learning_sources"),
            "source_chars": scalar("SELECT coalesce(sum(length(content)),0) FROM learning_sources"),
        }
        try:
            tables["job_range_rows"] = scalar("SELECT count(*) FROM learning_job_ranges")
        except Exception:  # noqa: BLE001 - a release before schema 7 has no range table
            tables["job_range_rows"] = 0
        conn.execute("VACUUM")
        conn.close()
        tables["db_bytes"] = (base / "growth.db").stat().st_size
    return tables


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--events", choices=("every-event", "session-points"), required=True)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--tools", type=int, default=80, help="tool calls per session")
    parser.add_argument("--compactions", type=int, default=1)
    parser.add_argument("--sessions-per-day", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    result = run(
        args.repo.resolve(), args.events, args.sessions, args.tools, args.compactions, args.seed
    )
    scale = args.sessions_per_day / args.sessions
    result["per_day"] = {
        key: round(value * scale, 1) for key, value in result.items() if key != "per_day"
    }
    result["setup"] = vars(args) | {"repo": args.repo.name}
    result["setup"]["repo"] = "release under test"
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
