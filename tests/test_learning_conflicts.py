# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conflict discovery and history through the real ledger and public CLI."""

import hashlib
import json

import pytest

from noblivion import db
from noblivion.learning import LearningError, LearningLedger
from noblivion.learning_cli import main


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "conflicts.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def submit(ledger, number, *, root="sample-project", count=1, **changes):
    text = f"Use short progress updates for task {number}."
    source_id = f"source-{number}"
    digest = hashlib.sha256(text.encode()).hexdigest()
    ledger.capture_batch(
        {
            "batch_id": f"batch-{number}",
            "namespace": "test",
            "root": root,
            "source_client": "codex",
            "session_id": "session-a",
            "records": [
                {
                    "source_id": source_id,
                    "record_id": source_id,
                    "role": "user",
                    "content": text,
                    "source_hash": digest,
                    "observed_at": "2026-10-12T09:00:00Z",
                    "metadata": {"authority": "direct_user", "direct_user": True},
                }
            ],
            "cursor": {"path_id": "transcript-a", "offset": number * 100},
        }
    )
    evidence = [
        {"source_id": source_id, "source_hash": digest, "start": 0, "end": len(text), "quote": text}
    ]
    decision = {
        "action": "add",
        "logical_key": "progress-style",
        "category": "preference",
        "title": "Progress format",
        "statement": text,
        "observed_at": "2026-10-12",
        "effective_at": None,
        "scope": "project",
        "evidence": evidence,
        **changes,
    }
    job = ledger.claim("worker")
    results = ledger.finish(job["id"], job["lease_token"], [decision] * count)
    return results[0], decision


def conflict_for(ledger, note, number=2, **changes):
    return submit(
        ledger,
        number,
        action="conflict",
        note_id=note["note_id"],
        expected_revision=note["revision"],
        reason="semantic contradiction",
        **changes,
    )


def test_status_discovers_exact_proposal_and_note_history(ledger):
    note, _ = submit(ledger, 1)
    _, decision = conflict_for(ledger, note)
    status = ledger.status("test", "sample-project")["conflicts"]
    assert status["open_count"] == 1
    compact = status["items"][0]
    assert compact["target_revision"] == 1
    assert "evidence" not in compact and "proposed_statement" not in compact
    detail = ledger.inspect(compact["conflict_id"], project="test", root="sample-project")
    assert detail["reason"] == "semantic contradiction"
    assert detail["proposed_statement"] == decision["statement"]
    assert detail["evidence"] == decision["evidence"]
    assert detail["state"] == "open"
    assert not detail["evidence_unavailable"]
    assert ledger.inspect(note["note_id"])["conflicts"] == [compact]
    assert ledger.inspect(note["note_id"])["statement"] != decision["statement"]


def test_explicit_edit_clears_open_conflict_without_losing_history(ledger):
    note, _ = submit(ledger, 1)
    conflict_for(ledger, note)
    conflict_id = ledger.status()["conflicts"]["items"][0]["conflict_id"]
    ledger.edit(note["note_id"], 1, title="Progress wording")
    assert ledger.status()["conflicts"]["open_count"] == 0
    history = ledger.inspect(note["note_id"])["conflicts"][0]
    assert history["state"] == "cleared"
    assert history["cleared_reason"] == "target revision changed"
    assert history["target_revision"] == 1 and history["current_revision"] == 2
    assert ledger.inspect(conflict_id, project="test", root="sample-project")["proposed_statement"]


@pytest.mark.parametrize("exclude_target", [False, True])
def test_exclusion_clears_conflict_but_retains_audit_evidence(ledger, exclude_target):
    note, _ = submit(ledger, 1)
    _, proposal = conflict_for(ledger, note)
    conflict_id = ledger.status()["conflicts"]["items"][0]["conflict_id"]
    if exclude_target:
        ledger.exclude(note["note_id"])
    else:
        ledger.exclude_source("source-2")
    assert ledger.status()["conflicts"]["open_count"] == 0
    detail = ledger.inspect(conflict_id, project="test", root="sample-project")
    assert detail["state"] == "cleared"
    assert detail["cleared_reason"] == ("target excluded" if exclude_target else "source excluded")
    assert detail["evidence"] == proposal["evidence"]


def test_cross_root_conflict_is_visible_to_both_scopes_only(ledger):
    note, _ = submit(ledger, 1, root="owner-project")
    conflict_for(ledger, note, root="source-project")
    owner = ledger.status("test", "owner-project")["conflicts"]
    source = ledger.status("test", "source-project")["conflicts"]
    assert owner == source and owner["open_count"] == 1
    assert ledger.status("other", "owner-project")["conflicts"]["open_count"] == 0
    assert ledger.status("test", "unrelated-project")["conflicts"]["open_count"] == 0
    detail = ledger.inspect(owner["items"][0]["conflict_id"], project="test", root="owner-project")
    assert detail["target_scope"]["root"] == "owner-project"
    assert detail["source_scope"]["root"] == "source-project"
    assert detail["evidence"][0]["source_id"] == "source-2"


def test_status_is_bounded_and_cli_can_inspect_discovered_id(ledger, tmp_path, capsys):
    note, _ = submit(ledger, 1)
    conflict_for(ledger, note, count=25)
    args = ["--database", str(tmp_path / "conflicts.db")]
    assert main([*args, "status", "--project", "test"]) == 0
    conflicts = json.loads(capsys.readouterr().out)["conflicts"]
    assert conflicts["open_count"] == 25 and len(conflicts["items"]) == conflicts["limit"] == 20
    conflict_id = conflicts["items"][0]["conflict_id"]
    assert (
        main([*args, "inspect", conflict_id, "--project", "test", "--root", "sample-project"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["evidence"][0]["source_id"] == "source-2"
    assert len(ledger.inspect(note["note_id"])["conflicts"]) == 25


def test_unknown_target_stays_visible_and_invalid_evidence_is_withheld(ledger):
    _, proposal = submit(
        ledger,
        1,
        action="conflict",
        note_id="missing-note",
        expected_revision=1,
        reason="target note is unavailable",
    )
    summary = ledger.status()["conflicts"]
    assert summary["open_count"] == 1
    conflict_id = summary["items"][0]["conflict_id"]
    # Simulate corrupt legacy evidence: inspection must not echo an unverified quote.
    ledger.conn.execute("UPDATE learning_sources SET content='' WHERE id='source-1'")
    detail = ledger.inspect(conflict_id, project="test", root="sample-project")
    assert detail["evidence_unavailable"]
    assert detail["evidence"] == [] and detail["proposed_statement"] is None
    assert proposal["statement"] not in json.dumps(detail)
    with pytest.raises(LearningError, match="unknown conflict"):
        ledger.inspect("conflict:99999", project="test", root="sample-project")


def test_already_stale_revision_refusal_remains_open_until_later_edit(ledger):
    note, _ = submit(ledger, 1)
    ledger.edit(note["note_id"], 1, title="Revised format")
    conflict_for(ledger, note)
    conflict = ledger.status()["conflicts"]["items"][0]
    assert conflict["target_revision"] == 1
    assert conflict["recorded_target_revision"] == conflict["current_revision"] == 2
    assert conflict["state"] == "open"
    ledger.edit(note["note_id"], 2, title="Reviewed format")
    assert ledger.status()["conflicts"]["open_count"] == 0
    assert (
        ledger.inspect(conflict["conflict_id"], project="test", root="sample-project")["state"]
        == "cleared"
    )


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"project": "test"},
        {"root": "sample-project"},
        {"project": "other", "root": "sample-project"},
        {"project": "test", "root": "unrelated-project"},
    ],
)
def test_conflict_inspection_rejects_missing_or_unrelated_scope(ledger, scope):
    note, _ = submit(ledger, 1)
    conflict_for(ledger, note)
    conflict_id = ledger.status()["conflicts"]["items"][0]["conflict_id"]
    with pytest.raises(LearningError):
        ledger.inspect(conflict_id, **scope)


@pytest.mark.parametrize(
    "scope_args",
    [
        [],
        ["--project", "test"],
        ["--root", "sample-project"],
        ["--project", "other", "--root", "sample-project"],
        ["--project", "test", "--root", "unrelated-project"],
    ],
)
def test_cli_conflict_rejects_missing_or_unrelated_scope(ledger, tmp_path, capsys, scope_args):
    note, _ = submit(ledger, 1)
    _, proposal = conflict_for(ledger, note)
    conflict_id = ledger.status()["conflicts"]["items"][0]["conflict_id"]
    assert (
        main(["--database", str(tmp_path / "conflicts.db"), "inspect", conflict_id, *scope_args])
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert proposal["statement"] not in output.err


@pytest.mark.parametrize("root", ["owner-project", "source-project"])
def test_cli_cross_root_conflict_authorizes_origin_and_owner(ledger, tmp_path, capsys, root):
    note, _ = submit(ledger, 1, root="owner-project")
    _, proposal = conflict_for(ledger, note, root="source-project")
    conflict_id = ledger.status()["conflicts"]["items"][0]["conflict_id"]
    assert (
        main(
            [
                "--database",
                str(tmp_path / "conflicts.db"),
                "inspect",
                conflict_id,
                "--project",
                "test",
                "--root",
                root,
            ]
        )
        == 0
    )
    detail = json.loads(capsys.readouterr().out)
    assert detail["proposed_statement"] == proposal["statement"]
    assert detail["evidence"] == proposal["evidence"]
