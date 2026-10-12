# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native clients without session identity cannot bypass session use controls."""

import json
from urllib.parse import urlencode

import pytest

from noblivion import db, rest
from noblivion.learning import LearningLedger
from noblivion.learning_runtime import use_allowed
from test_learning import batch, learn
from test_store import Running, make_store, wait_for


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "read-controls.db", create=True)
    yield LearningLedger(conn)
    conn.close()


@pytest.mark.parametrize("unknown_id", ["", None, " ", "bad/id", "x" * 257, "session\n"])
def test_unknown_identity_respects_any_scoped_session_opt_out(ledger, unknown_id):
    assert ledger.read_allowed("test", "demo", unknown_id)
    ledger.set_controls("test", "demo", "session-a", use=False)
    assert not ledger.read_allowed("test", "demo", unknown_id)
    assert not use_allowed(ledger.conn, "test", "demo", unknown_id)
    assert not ledger.read_allowed("test", "demo", "session-a")
    assert ledger.read_allowed("test", "demo", "session-b")
    assert ledger.read_allowed("test", "other", unknown_id)
    assert ledger.read_allowed("other", "demo", unknown_id)
    # No change to contribution, worker contexts or controls inspection semantics.
    assert ledger.effective_controls("test", "demo")["use"]
    assert ledger.effective_controls("test", "demo")["contribute"]
    ledger.set_controls("test", "demo", use=True)
    assert not ledger.read_allowed("test", "demo", unknown_id)
    ledger.set_controls("test", "demo", "session-a", use=True)
    assert ledger.read_allowed("test", "demo", unknown_id)
    ledger.set_controls("test", "demo", use=False)
    assert not ledger.read_allowed("test", "demo", unknown_id)
    assert not ledger.read_allowed("test", "demo", "session-b")


def test_valid_session_routing_matches_bridge_ids(ledger):
    ledger.set_controls("test", "demo", "another", use=False)
    for session_id in ("native:session-1", "00000000-0000-4000-8000-000000000001", "x" * 256):
        assert ledger.read_allowed("test", "demo", session_id)
        ledger.set_controls("test", "demo", session_id, use=False)
        assert not ledger.read_allowed("test", "demo", session_id)


@pytest.fixture
def memory_service(tmp_path):
    st = make_store(tmp_path)
    run = Running(st)
    folder = st.settings.memory_dirs[0]
    root = folder.parent.name
    try:
        with st.connection() as conn:
            ledger = LearningLedger(conn)
            payload = batch(1, "For every project, I prefer brief replies.")
            payload.update(namespace=st.namespace, root=root)
            note = learn(ledger, payload)
            ledger.project_pending(st.namespace, root, folder)
            assert wait_for(lambda: st.scan_once(conn) is not None, timeout=5)
            memory_id = str(
                conn.execute(
                    "SELECT id FROM memories WHERE path=?", (f"learned-{note['note_id']}.md",)
                ).fetchone()[0]
            )
            yield st, run, ledger, memory_id, root
    finally:
        run.stop()


def assert_reads(service, destination, session_id, allowed):
    st, run, ledger, memory_id, _ = service
    params = {"project": st.namespace, "root": destination, "q": "brief replies"}
    if session_id is not None:
        params["session_id"] = session_id
    for path in (
        rest.SEARCH_PATH,
        rest.INDEX_PATH,
        rest.FETCH_PREFIX + memory_id,
        "/learning/summary",
    ):
        status, body = run.get(path + "?" + urlencode(params))
        assert status == 200
        assert ("brief replies" in json.dumps(body)) is allowed, (path, body)
    assert (
        bool(
            st.rank(
                "brief replies",
                st.namespace,
                root=destination,
                session_id=session_id or "",
                top_k=5,
            ).hits
        )
        is allowed
    )
    assert bool(ledger.summary(st.namespace, destination, session_id=session_id or "")) is allowed


@pytest.mark.parametrize(
    "destination_is_owner,block_owner", [(True, True), (False, True), (False, False)]
)
def test_unknown_mcp_reads_and_general_roots_obey_live_controls(
    memory_service, destination_is_owner, block_owner
):
    st, _, ledger, _, owner = memory_service
    destination = owner if destination_is_owner else "receiving-project"
    blocked_root = owner if block_owner else destination
    assert_reads(memory_service, destination, None, True)
    ledger.set_controls(st.namespace, blocked_root, "session-a", use=False)
    # Calls without identity must be protected even after the index is warm.
    assert_reads(memory_service, destination, None, False)
    assert_reads(memory_service, destination, "", False)
    assert_reads(memory_service, destination, "bad/id", False)
    assert_reads(memory_service, destination, "session-a", False)
    assert_reads(memory_service, destination, "session-b", True)
    # The unknown-session fallback must not silently narrow worker semantic context.
    assert ledger.semantic_notes(st.namespace, destination)
    ledger.set_controls(st.namespace, blocked_root, "session-a", use=True)
    assert_reads(memory_service, destination, None, True)
    ledger.set_controls(st.namespace, blocked_root, use=False)
    assert_reads(memory_service, destination, None, False)
    assert_reads(memory_service, destination, "session-b", False)


def test_missing_root_cannot_bypass_source_controls(memory_service):
    st, run, ledger, memory_id, owner = memory_service
    ledger.set_controls(st.namespace, owner, "session-a", use=False)
    for path in (rest.SEARCH_PATH, rest.INDEX_PATH, rest.FETCH_PREFIX + memory_id):
        status, body = run.get(
            path + "?" + urlencode({"project": st.namespace, "q": "brief replies"})
        )
        assert status == 200 and "brief replies" not in json.dumps(body)
