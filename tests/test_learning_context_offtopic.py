# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prompt context holds no unresolved conflict row and no row from another scope (NOBLIVION-276).

Fixture at store scale: two scopes with many notes that share the query words, and open
conflicts of both kinds (a conflicted claim and a conflict proposal). The default context
is what the prompt hook asks for. A caller that asks for conflicts on purpose still gets them.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from noblivion import learning_runtime
from test_learning_consolidation import learn, ledgers, proposal, verdict  # noqa: F401

QUERY = "cobalt service port"
OWN, FOREIGN = "demo", "elsewhere"


def conflict(ledger, note, n, root, text):
    """An open conflict proposal against ``note``, made by a session of scope ``root``."""
    payload = {
        "batch_id": f"conflict-batch-{n}",
        "project": "test",
        "root": root,
        "session_id": f"conflict-session-{n}",
        "source_client": "codex",
        "records": [
            {
                "source_id": f"conflict-source-{n}",
                "role": "user",
                "content": text,
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "observed_at": "2026-10-14T12:00:00Z",
                "metadata": {"direct_user": True},
            }
        ],
    }
    ledger.capture_batch(payload)
    job = ledger.claim("conflict")
    ledger.finish(
        job["id"],
        job["lease_token"],
        [
            {
                "action": "conflict",
                "note_id": note["note_id"],
                "expected_revision": note["revision"],
                "statement": text,
                "observed_at": "2026-10-14",
                "evidence": [
                    {
                        "source_id": f"conflict-source-{n}",
                        "source_hash": payload["records"][0]["source_hash"],
                        "start": 0,
                        "end": len(text),
                        "quote": text,
                    }
                ],
            }
        ],
    )


@pytest.fixture
def store_at_scale(ledgers):  # noqa: F811
    ledger, store = ledgers
    own = [
        learn(ledger, n, f"The cobalt service {n} listens on port {4000 + n}.", root=OWN)
        for n in range(1, 13)
    ]
    for n in range(13, 25):
        learn(ledger, n, f"The cobalt service {n} listens on port {5000 + n}.", root=FOREIGN)
    for n in range(0, 6):
        conflict(ledger, own[n], 100 + n, OWN, f"The cobalt service {n + 1} uses port {9000 + n}.")
    # A proposal from another scope against a note of this scope.
    conflict(ledger, own[6], 200, FOREIGN, "The cobalt service 7 uses port 7777.")
    # A conflicted claim of this scope.
    claim = store.stage(
        proposal(store, own[:2], statement="The cobalt service port 4317."), "test", OWN
    )
    store.admit(claim["claim_id"], verdict(claim, relation="contradictory"))
    return ledger, store, own, claim


def test_default_context_has_no_unresolved_row(store_at_scale):
    _, store, _, claim = store_at_scale
    result = store.context("test", OWN, QUERY)
    assert "unresolved" not in result["context"]
    assert "not admitted" not in result["context"]
    assert "[conflict:" not in result["context"]
    assert claim["claim_id"] not in result["context"]
    assert result["conflicts"] == []
    # What stays is the admitted notes of this scope.
    assert result["context"].count("[claim:note:") == 12


def test_explicit_call_still_returns_the_conflicts_of_this_scope(store_at_scale):
    _, store, _, claim = store_at_scale
    result = store.context("test", OWN, QUERY, include_conflicts=True)
    assert result["context"].count("[unresolved/not admitted") == 7
    assert claim["claim_id"] in result["context"]
    assert result["conflicts"]


def test_conflict_from_another_scope_is_not_shown_even_on_request(store_at_scale):
    _, store, _, _ = store_at_scale
    result = store.context("test", OWN, QUERY, include_conflicts=True)
    assert "port 7777" not in result["context"]
    assert all("port 7777" not in str(c) for c in result["conflicts"])


def test_context_has_no_row_of_another_scope(store_at_scale):
    _, store, _, _ = store_at_scale
    for flag in (False, True):
        result = store.context("test", OWN, QUERY, include_conflicts=flag)
        assert f" {FOREIGN} " not in result["context"]
        assert all(c["root"] == OWN for c in result["claims"])
    other = store.context("test", FOREIGN, QUERY)
    assert other["context"].count("[claim:note:") == 12
    assert all(c["root"] == FOREIGN for c in other["claims"])


def test_store_route_asks_for_conflicts_only_on_request(store_at_scale):
    ledger, _, _, _ = store_at_scale

    @contextmanager
    def connection():
        yield ledger.conn

    fake = SimpleNamespace(namespace="test", connection=connection)
    body = {"project": "test", "root": OWN, "query": QUERY, "session_id": ""}
    assert "unresolved" not in learning_runtime.context(fake, body)["context"]
    # Only the boolean true counts: a string or a number is not a request.
    for value in ("1", 1, "true"):
        assert (
            "unresolved"
            not in learning_runtime.context(fake, {**body, "include_conflicts": value})["context"]
        )
    assert (
        "unresolved"
        in learning_runtime.context(fake, {**body, "include_conflicts": True})["context"]
    )
