# SPDX-License-Identifier: AGPL-3.0-or-later
"""A note shown once in a session is not shown again in that session (NOBLIVION-160).

Fictional data only. The test file names noblivion here so the Python 3.9 job skips it.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import noblivion  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "learning_context_shown_test", ROOT / "hooks/learning_context.py"
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

A = "- [observation fact demo 2026-01-01] Otter notes. [claim:note:a1 revision:1 supports:s1]"
B = "- [observation fact demo 2026-01-02] Heron notes. [claim:note:b2 revision:1 supports:s2]"
A2 = A.replace("revision:1", "revision:2")
CONFLICT = "- [unresolved/not admitted 2026-01-03] Fox. [conflict:c9 affected:a1 revision:1]"


def test_drop_shown_keeps_new_notes_and_new_revisions():
    text, new = hook.drop_shown("\n".join([A, B, CONFLICT]), {"note:a1@1"})
    assert text == "\n".join([B, CONFLICT])
    assert new == ["note:b2@1"]
    text, new = hook.drop_shown(A2, {"note:a1@1"})
    assert text == A2 and new == ["note:a1@2"]


def test_drop_shown_keeps_a_continuation_line_with_its_row():
    row = A + "\nsecond line of the same note"
    text, _ = hook.drop_shown(row, set())
    assert text == row
    text, _ = hook.drop_shown(row, {"note:a1@1"})
    assert text == ""


def run_prompt(monkeypatch, tmp_path, session, context):
    recall = SimpleNamespace(
        store_get=lambda *args: {"context": context, "reason": None},
        session_env=lambda env, event: env,
    )
    monkeypatch.setattr(hook, "load", lambda *args: recall)
    monkeypatch.setattr(hook, "no_context_items", lambda *args: False)
    monkeypatch.setattr(hook, "shown_path", lambda root, env, sid: tmp_path / sid)
    event = dict(hook_event_name="UserPromptSubmit", session_id=session, prompt="otter heron")
    env = {"NOBLIVION_RECALL_PROJECT": "demo", "NOBLIVION_RECALL_ROOT": "r"}
    return hook.run(event, env, codex=True)


def test_second_prompt_of_a_session_gets_no_repeat(monkeypatch, tmp_path):
    first = run_prompt(monkeypatch, tmp_path, "s-1", A + "\n" + B)
    assert A in first["hookSpecificOutput"]["additionalContext"]
    assert run_prompt(monkeypatch, tmp_path, "s-1", A + "\n" + B) == {}
    third = run_prompt(monkeypatch, tmp_path, "s-1", A2)
    assert A2 in third["hookSpecificOutput"]["additionalContext"]


def test_another_session_still_gets_the_note(monkeypatch, tmp_path):
    run_prompt(monkeypatch, tmp_path, "s-1", A)
    other = run_prompt(monkeypatch, tmp_path, "s-2", A)
    assert A in other["hookSpecificOutput"]["additionalContext"]


def test_an_unwritable_state_file_shows_everything(monkeypatch, tmp_path):
    (tmp_path / "s-1").mkdir()  # a directory where the file should be
    assert run_prompt(monkeypatch, tmp_path, "s-1", A) != {}
    assert run_prompt(monkeypatch, tmp_path, "s-1", A) != {}
