# SPDX-License-Identifier: AGPL-3.0-or-later
"""Error recall reads the error body behind a generic tool line (NOBLIVION-136).

1. A generic strong line (``curl: (22) The requested URL returned error: 400``)
   names the tool, not the cause. The weak error lines of a failed call (the
   body the server sent) are read as well. After a specific strong line they
   are still not read.
2. The message of a JSON error body (``{"error":"description is not valid"}``)
   gives query words: the quote check kept no word of a double-quoted value.
3. A call whose output has no error-form line (``gh pr checks`` with exit 8
   prints a table) still shows nothing. That case is out of scope: see
   ``test_a_table_with_no_error_line_still_shows_nothing``.

Fictional data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hookload import load_hook
from test_hook_error_recall import _mem, env, fail_event, run, write_memory  # noqa: F401

er = load_hook("error_recall_hook", "error_recall_hook_t_json_body")

CURL = "curl: (22) The requested URL returned error: 400"
BODY = '{"error":"description is not valid"}'


def test_the_error_body_follows_a_generic_curl_line():
    lines = er.error_lines(f"{CURL}\n{BODY}", failed=True)
    assert lines == [CURL, BODY]


def test_the_body_is_not_read_when_the_call_exited_zero():
    assert er.error_lines(f"{CURL}\n{BODY}", failed=False) == [CURL]


def test_a_specific_strong_line_still_hides_the_weak_lines():
    out = er.error_lines("fatal: Needed a single revision\nthe build failed twice", failed=True)
    assert out == ["fatal: Needed a single revision"]


def test_the_message_of_a_json_error_body_gives_words():
    words = er.message_words(BODY)
    assert "description" in words and "valid" in words


@pytest.mark.parametrize(
    "body",
    [
        '{"message":"description is not valid"}',
        '{"detail": "description is not valid"}',
        '{"errors":{"error":"description is not valid"}}',
    ],
)
def test_other_message_keys_give_words_too(body):
    assert "valid" in er.message_words(body)


def test_a_double_quoted_value_of_another_key_is_still_removed():
    assert er.message_words('{"id":"abc","name":"widget"}') == []


def test_a_memory_that_quotes_the_json_message_passes():
    mem = _mem("A Jira create call answers `description is not valid` when the body is markdown.")
    assert er.quotes_error(mem, f"{CURL} | {BODY}")


def test_a_memory_that_does_not_quote_the_json_message_fails():
    mem = _mem("A Jira create call with a markdown body fails. Use the MCP tool.")
    assert not er.quotes_error(mem, BODY)


def test_the_note_shows_after_a_curl_call_with_a_json_body(env):  # noqa: F811
    folder = Path(env["NOBLIVION_ERROR_RECALL_MEMORY_DIR"])
    write_memory(
        folder,
        "feedback_issue_create_markdown",
        "A Jira create call with a markdown body fails; use the MCP tool.",
        "Call the create tool, not curl.",
        "`curl -sf` on the create call exits 22 with an HTTP 400. The body says\n"
        "`description is not valid` when the description is markdown.\n",
    )
    ev = fail_event(
        f"Exit code 22\n{CURL}\n{BODY}",
        command="curl -sf -X POST https://example.invalid/api/issue -d @body.json",
    )
    out = run(ev, env)
    assert "feedback_issue_create_markdown" in out


def test_a_table_with_no_error_line_still_shows_nothing(env):  # noqa: F811
    ev = fail_event(
        "Exit code 8\nbuild\tfail\t1m2s\thttps://ci.example.test/runs/1\nlint\tpass\t10s\t-",
        command="gh pr checks 12",
    )
    assert er.failure_of(ev) is None
    assert run(ev, env) == ""
