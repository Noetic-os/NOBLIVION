# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook's ``split_entries`` keeps transcript-miner rows apart from
the memory before them.

If ``split_entries`` started a new entry only at a ``[claude_code_md: ...]``
marker, a mined row (which carries no marker) that followed a mirror row
became part of that row's hit. The same memory then had a different text,
and so a different dedupe key, per query, and the per-session seen-set showed
it again.

The rows here are built with a copy of the miner's render functions (the
fixed opening phrases the hook's ``_MINED_START_RE`` knows), so a test here
breaks when the hook stops recognizing that wording.
"""

from __future__ import annotations

from hookload import load_hook

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

hook = load_hook("recall_hook", "hooktest_recall_hook_split_mined")


def _short(session_id: str) -> str:
    return session_id[:8] if session_id else "unknown"


def render_correction(session_id: str, date: str, cue: str, turn: str, context: str) -> str:
    text = (
        f"Operator correction on {date} in Claude Code session {_short(session_id)} "
        f"(cue: {cue}). The operator wrote: {turn}"
    )
    if context:
        text += f" The assistant had just said: {context}"
    return text


def render_tool_error(session_id: str, date: str, tool: str, command: str, error: str) -> str:
    what = f"{tool} `{command}`" if command else tool
    return (
        f"Tool error on {date} in Claude Code session {_short(session_id)}: "
        f"{what} failed with: {error}"
    )


def render_review(session_id: str, date: str, reviewed_pr: str, findings: str) -> str:
    pr = f" of PR #{reviewed_pr}" if reviewed_pr else ""
    return (
        f"Independent review on {date} in Claude Code session {_short(session_id)} "
        f"returned REQUEST_CHANGES{pr}. Findings: {findings}"
    )


SID = "64ecfdb0-1111-2222-3333-444455556666"
MD_ENTRY = (
    "# feedback_always_merge\n\n[claude_code_md: feedback_always_merge.md]\n\n"
    "Always merge your own finished pull requests."
)
MINED = [
    render_correction(SID, "2026-09-22", "wrong", "Wrong you have the credentials.", "Context"),
    render_tool_error(SID, "2026-09-16", "Bash", "cat settings.json", "exit 1"),
    render_review(SID, "2026-09-20", "2100", "memory.py:1095 misses RLS"),
]


def _hits(*entries: str):
    return hook.parse_hits({"results": [hook.ENTRY_SEPARATOR.join(entries)]})


def test_each_mined_row_after_a_mirror_row_is_its_own_hit():
    for row in MINED:
        hits = _hits(MD_ENTRY, row)
        assert len(hits) == 2, row[:40]
        assert hits[0].title == "feedback_always_merge"
        assert row[:20] not in hits[0].body
        assert hits[1].body.startswith(row[:20])


def test_a_mirror_row_has_one_key_whatever_follows_it():
    alone = _hits(MD_ENTRY)[0].key
    for row in MINED:
        assert _hits(MD_ENTRY, row)[0].key == alone, row[:40]
    assert _hits(MD_ENTRY, *MINED)[0].key == alone


def test_mined_rows_in_a_row_stay_apart():
    hits = _hits(*MINED)
    assert len(hits) == 3
    assert len({h.key for h in hits}) == 3


def test_a_rule_line_inside_a_mirror_body_still_continues_the_entry():
    body_with_rule = MD_ENTRY + hook.ENTRY_SEPARATOR + "More text of the same memory."
    hits = _hits(body_with_rule)
    assert len(hits) == 1
    assert "More text of the same memory." in hits[0].body


def test_a_body_that_only_mentions_a_tool_error_mid_line_does_not_split():
    tail = "Note: a Tool error on 2026-09-16 in Claude Code session abc is not a new row."
    hits = _hits(MD_ENTRY + hook.ENTRY_SEPARATOR + tail)
    assert len(hits) == 1
