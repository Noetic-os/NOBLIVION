# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inspect and control the background learner through its transactional service."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from noblivion import config, db
from noblivion.learning import LearningError, LearningLedger


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noblivion learning")
    parser.add_argument(
        "--database", type=Path, help="existing database (default: installed store)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "status",
        "list",
        "summary",
        "context",
        "claim",
        "drafts",
        "consolidate",
        "controls",
        "capture-json",
        "inspect",
        "edit",
        "exclude",
        "forget",
        "exclude-source",
        "retry",
        "prune",
    ):
        command = commands.add_parser(name)
        command.add_argument(
            "--project", default=None if name == "inspect" else config.DEFAULT_NAMESPACE
        )
        command.add_argument("--root", default="", help="explicit project root")
        command.add_argument("--session", default="")
        if name == "context":
            command.add_argument("--query", default="")
            command.add_argument("--limit-chars", type=int, default=6000)
        if name == "claim":
            command.add_argument("claim_id")
        if name in {"inspect", "edit", "exclude", "forget"}:
            command.add_argument("note_id")
        if name == "edit":
            command.add_argument("--expected-revision", type=int, required=True)
            command.add_argument("--title")
            command.add_argument("--statement")
            command.add_argument(
                "--evidence-json",
                type=Path,
                help="exact source evidence array for a changed statement",
            )
        if name == "controls":
            for flag in ("use", "contribute", "paused"):
                command.add_argument("--" + flag, choices=("on", "off"))
        if name == "exclude-source":
            command.add_argument("source_id")
        if name == "retry":
            command.add_argument("job_id")
        if name == "prune":
            command.add_argument("--pending-days", type=float, default=14.0)
            command.add_argument("--retention-days", type=float, default=14.0)
            command.add_argument(
                "--apply", action="store_true", help="delete; without it only count"
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if (
        args.command in {"list", "summary", "context", "drafts", "consolidate", "controls"}
        and not args.root
    ):
        parser.error("--root is required for scoped operations")
    path = args.database or config.data_dir() / config.DB_FILE
    try:
        conn = db.open_db(path, allow_migrate=False)
        try:
            ledger = LearningLedger(conn)
            result = _run(ledger, args)
        finally:
            conn.close()
    except (LearningError, db.SchemaError, sqlite3.Error, OSError, ValueError) as exc:
        # Provider/source text is never echoed from a parser failure.
        diagnostic = (
            str(exc) if isinstance(exc, (LearningError, db.SchemaError)) else type(exc).__name__
        )
        print("noblivion learning: " + diagnostic, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run(ledger: LearningLedger, args: argparse.Namespace):
    if args.command == "status":
        return ledger.status(args.project, args.root or None)
    if args.command == "list":
        return ledger.current_notes(args.project, args.root)
    if args.command in {"context", "claim", "drafts", "consolidate"}:
        from noblivion.learning_consolidation import ConsolidationLedger

        consolidation = ConsolidationLedger(ledger)
        if args.command == "context":
            return consolidation.context(
                args.project,
                args.root,
                args.query,
                session_id=args.session,
                limit_chars=args.limit_chars,
            )
        if args.command == "claim":
            return consolidation.inspect(args.claim_id)
        if args.command == "drafts":
            return consolidation.drafts(args.project, args.root)
        return {"job_id": ledger.enqueue_derivation(args.project, args.root, args.session)}
    if args.command == "summary":
        return {"summary": ledger.summary(args.project, args.root, session_id=args.session)}
    if args.command == "controls":
        switches = {
            name: None if getattr(args, name) is None else getattr(args, name) == "on"
            for name in ("use", "contribute", "paused")
        }
        if all(value is None for value in switches.values()):
            return ledger.effective_controls(args.project, args.root, args.session)
        return ledger.set_controls(args.project, args.root, args.session, **switches)
    if args.command == "capture-json":
        content = sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise LearningError("capture input too large")
        return ledger.capture_batch(json.loads(content))
    if args.command == "inspect":
        return ledger.inspect(args.note_id, project=args.project, root=args.root or None)
    if args.command == "edit":
        evidence = None
        if args.evidence_json:
            evidence = json.loads(args.evidence_json.read_text(encoding="utf-8"))
        return ledger.edit(
            args.note_id,
            args.expected_revision,
            title=args.title,
            statement=args.statement,
            evidence=evidence,
        )
    if args.command == "exclude":
        return ledger.exclude(args.note_id)
    if args.command == "forget":
        return ledger.forget(args.note_id)
    if args.command == "exclude-source":
        return ledger.exclude_source(args.source_id)
    if args.command == "retry":
        ledger.retry(args.job_id)
        return {"job_id": args.job_id, "status": "pending"}
    if args.command == "prune":
        return ledger.prune(
            pending_days=args.pending_days,
            retention_days=args.retention_days,
            dry_run=not args.apply,
        )
    raise LearningError("unknown operation")


if __name__ == "__main__":
    raise SystemExit(main())
