# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count the learner rows and bytes that simulated sessions leave in a new database (NOBLIVION-143).

Each simulated session writes a Claude Code style transcript (made-up text from a seeded random
generator, never real data). The tool feeds hook events to the capture spool of the release
under test, delivers every spooled batch to that release's ledger, and counts what the
database holds. No model runs, and the live store is never opened: the database is a new file
in a temporary folder. With retention enabled, one scripted accepted note is indexed before
the replay to verify that pruning preserves its recall rows and evidence. All other jobs stay
pending to exercise bounded backlog retention, not to measure extraction yield.

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
    python tools/replay_learning_growth.py --events session-points --days 22 \
        --pending-days 7 --retention-days 7

Retention reports daily file size after checkpointing, without VACUUM. Its steady-state growth
starts after the pending and retention windows. Batch receipts remain for replay protection;
accepted evidence remains for provenance. These small records are not deleted to force a flat
number. The policy is opt-in and this tool changes no installed configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import random
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

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


def seed_retained_note(ledger, base):
    """Accept and index one scripted fixture, without a model or real data."""
    from noblivion import indexer

    statement = "Keep progress updates brief and include the validation result."
    sha = hashlib.sha256(statement.encode()).hexdigest()
    ledger.capture_batch(
        {
            "batch_id": "retained-batch",
            "namespace": "claude_code",
            "root": "sample",
            "source_client": "claude",
            "session_id": "retained-session",
            "records": [
                {
                    "source_id": "retained-source",
                    "record_id": "retained-record",
                    "role": "user",
                    "content": statement,
                    "source_hash": sha,
                    "observed_at": "2026-10-12T09:00:00Z",
                    "metadata": {"authority": "direct_user", "direct_user": True},
                }
            ],
            "cursor": {"path_id": "retained", "offset": 1},
        }
    )
    job = ledger.claim("scripted-fixture")
    ledger.finish(
        job["id"],
        job["lease_token"],
        [
            {
                "action": "add",
                "logical_key": "progress-style",
                "category": "preference",
                "title": "Progress format",
                "statement": statement,
                "scope": "general",
                "observed_at": "2026-10-12",
                "effective_at": None,
                "evidence": [
                    {
                        "source_id": "retained-source",
                        "source_hash": sha,
                        "start": 0,
                        "end": len(statement),
                        "quote": statement,
                    }
                ],
            }
        ],
    )
    folder = base / "sample" / "memory"
    ledger.project_pending("claude_code", "sample", folder)
    indexer.scan(ledger.conn, [folder], project="claude_code")


def run(
    repo: Path,
    events: str,
    sessions: int,
    tools: int,
    compactions: int,
    seed: int,
    *,
    days: int = 1,
    pending_days: float | None = None,
    retention_days: float = 14,
) -> dict:
    if sessions < 1 or days < 1 or tools < 0 or compactions < 0:
        raise ValueError("sessions and days must be positive; tools and compactions nonnegative")
    if pending_days is not None and not (0 < pending_days <= 3650 and 0 < retention_days <= 3650):
        raise ValueError("retention days must be in (0, 3650]")
    sys.path.insert(0, str(repo / "src"))
    from noblivion import db
    from noblivion.learning import LearningLedger

    capture = load(repo / "codex/capture.py", "_growth_capture")
    rng = random.Random(seed)
    every = events == "every-event"
    clock = [time.time()]
    start = clock[0]
    daily = []
    pruning = pending_days is not None
    if pruning:
        from noblivion import learning_ranges
    with tempfile.TemporaryDirectory() as folder, patch("time.time", lambda: clock[0]):
        base = Path(folder)
        base.chmod(0o700)
        conn = db.open_db(base / "growth.db", create=True)
        ledger = LearningLedger(conn)
        if pruning:
            seed_retained_note(ledger, base)
            preserved_tables = (
                "memories",
                "vectors",
                "memory_chunks",
                "chunk_vectors",
                "learning_notes",
                "learning_evidence",
            )
            retained = {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in preserved_tables
            }
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

        for number in range(sessions * days):
            clock[0] = start + (number // sessions) * 86400
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
            if pruning and (number + 1) % sessions == 0:
                with db.write_tx(conn):
                    counts = learning_ranges.prune(
                        conn, pending_days=pending_days, retention_days=retention_days, now=clock[0]
                    )
                # Checkpoint only: no daily VACUUM that could hide file growth.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                pages = conn.execute("PRAGMA page_count").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
                size = conn.execute("PRAGMA page_size").fetchone()[0]
                daily.append(
                    {
                        "day": number // sessions + 1,
                        "db_bytes": (base / "growth.db").stat().st_size,
                        "used_bytes": (pages - free) * size,
                        "jobs": dict(
                            conn.execute(
                                "SELECT status,count(*) FROM learning_jobs GROUP BY status"
                            )
                        ),
                        "sources": conn.execute("SELECT count(*) FROM learning_sources").fetchone()[
                            0
                        ],
                        "pruned": counts,
                    }
                )
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
        if pruning:
            from noblivion.ranking import Ranker

            tables["retention"] = {
                "pending_days": pending_days,
                "retention_days": retention_days,
                "days": daily,
                "recall_and_evidence_unchanged": all(
                    retained[table]
                    == [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                    for table in preserved_tables
                ),
                "retained_source": conn.execute(
                    "SELECT count(*) FROM learning_sources WHERE id='retained-source'"
                ).fetchone()[0]
                == 1,
                "recall_hits": len(
                    Ranker()
                    .search(
                        conn, "progress validation", project="claude_code", root="sample", top_k=5
                    )
                    .hits
                ),
                "pruned_totals": {
                    key: sum(day["pruned"][key] for day in daily) for key in daily[0]["pruned"]
                },
            }
            warmup = math.ceil(pending_days + retention_days) + 1
            growth = [
                day["db_bytes"] - daily[i - 1]["db_bytes"]
                for i, day in enumerate(daily)
                if i >= warmup
            ]
            tables["retention"]["steady_state"] = {
                "after_day": warmup,
                "measured_days": len(growth),
                "max_daily_growth_bytes": max(growth) if growth else None,
                "mean_daily_growth_bytes": round(sum(growth) / len(growth)) if growth else None,
            }
        else:
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
    parser.add_argument(
        "--days", type=int, default=1, help="replay days; sessions repeats each day"
    )
    parser.add_argument(
        "--pending-days", type=float, help="enable synthetic retention with this pending age"
    )
    parser.add_argument("--retention-days", type=float, default=14)
    args = parser.parse_args()
    result = run(
        args.repo.resolve(),
        args.events,
        args.sessions,
        args.tools,
        args.compactions,
        args.seed,
        days=args.days,
        pending_days=args.pending_days,
        retention_days=args.retention_days,
    )
    scale = args.sessions_per_day / args.sessions
    if args.pending_days is None:
        result["per_day"] = {
            key: round(value * scale / args.days, 1)
            for key, value in result.items()
            if isinstance(value, (int, float))
        }
    result["setup"] = vars(args) | {"repo": args.repo.name}
    result["setup"]["repo"] = "release under test"
    if args.pending_days is not None:
        result["setup"]["simulated_sessions_per_day"] = args.sessions
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
