# SPDX-License-Identifier: AGPL-3.0-or-later
"""The labels, rule and apply lines of a mined row (NOBLIVION-105). Fictional text only."""

from __future__ import annotations

import json

import pytest

from noblivion import mined_fields as mf

ERROR_TEXT = (
    "# Tool error: Bash `make water`\n\nTool error on 2026-01-02 in session abc12345: "
    "Bash `make water` failed with: make: *** No rule to make target 'water'.  Stop."
)
CORRECTION_TEXT = (
    "# Correction (wrong) on 2026-01-02\n\nUser correction on 2026-01-02 in session abc12345 "
    "(cue: wrong). The user wrote: The beds are in zone B. Do not water zone A! "
    "The assistant had just said: Done, the beds are watered."
)
REVIEW_TEXT = (
    "# Review asked for changes\n\nIndependent review on 2026-01-02 in session abc12345 "
    "returned REQUEST_CHANGES. Findings: 1. The retry loop never stops. 2. No test."
)


def test_the_rule_of_a_tool_error_is_the_first_sentence_of_the_error():
    assert mf.rule_of(mf.KIND_TOOL_ERROR, ERROR_TEXT) == "make: *** No rule to make target 'water'."


def test_the_rule_of_a_correction_is_the_first_sentence_of_the_user_turn():
    assert mf.rule_of(mf.KIND_CORRECTION, CORRECTION_TEXT) == "The beds are in zone B."


def test_a_list_number_is_not_the_end_of_the_rule():
    # "1." is shorter than a sentence: the rule runs to the next stop.
    assert mf.rule_of(mf.KIND_REVIEW, REVIEW_TEXT) == "1. The retry loop never stops."


def test_a_text_with_no_marker_gives_its_first_sentence():
    assert mf.rule_of(mf.KIND_TOOL_ERROR, "One line only") == "One line only"


def test_a_long_rule_is_cut_at_the_limit():
    rule = mf.rule_of(mf.KIND_TOOL_ERROR, " failed with: " + "word " * 100)
    assert len(rule) == mf.RULE_MAX_CHARS and rule.endswith("…")


def test_the_apply_line_names_the_tool_of_a_tool_error_and_nothing_else_mined():
    apply = mf.apply_of(mf.KIND_TOOL_ERROR, ERROR_TEXT)
    assert apply.startswith("Bash failed with this error before.")
    assert "water" not in apply
    assert mf.apply_of(mf.KIND_CORRECTION, CORRECTION_TEXT) == mf.apply_of(mf.KIND_CORRECTION, "")
    assert mf.apply_of("other", "x") == ""


def test_index_text_is_the_rule_line_then_the_apply_line():
    lines = mf.index_text(mf.KIND_TOOL_ERROR, ERROR_TEXT).split("\n")
    assert lines[0] == "make: *** No rule to make target 'water'."
    assert lines[1].startswith("Bash failed")
    assert mf.split_index_text("\n".join(lines)) == (lines[0], lines[1])


def test_fields_of_prefers_the_stored_lines_and_derives_for_an_old_row():
    stored = "stored rule.\nstored apply."
    assert mf.fields_of(mf.KIND_TOOL_ERROR, stored, ERROR_TEXT) == ("stored rule.", "stored apply.")
    rule, apply = mf.fields_of(mf.KIND_TOOL_ERROR, "", ERROR_TEXT)
    assert rule.startswith("make: *** No rule") and apply.startswith("Bash failed")
    # a stored rule with no apply line takes the derived apply line
    assert mf.fields_of(mf.KIND_TOOL_ERROR, "only a rule.", ERROR_TEXT)[1].startswith("Bash")


@pytest.mark.parametrize(
    ("tool", "command", "error", "want"),
    [
        ("Bash", "make water", "make: *** No rule", ["bash", "make"]),
        (
            "Bash",
            "FOO=1 sudo /usr/bin/python3 x.py",
            "KeyError: 'a'",
            ["bash", "python3", "keyerror"],
        ),
        ("Read", "", "<tool_use_error>File does not exist.", ["read", "tool_use_error"]),
        ("Bash", "git status", "fatal: not a repository", ["bash", "git"]),
        ("Bash", "git push", "GitError: x", ["bash", "git", "giterror"]),
        ("mcp__demo__lookup", "", "TimeoutError", ["mcp__demo__lookup", "timeouterror"]),
    ],
)
def test_labels_of_an_error_are_the_tool_the_command_head_and_the_error_class(
    tool, command, error, want
):
    assert mf.labels_of_error(tool, command, error) == want


def test_a_value_that_is_not_a_plain_word_is_not_a_label():
    assert mf.labels_of_error("Bash", "'$(rm x)' y", "boom") == ["bash"]


def test_join_labels_is_the_json_list_of_the_labels_column():
    assert json.loads(mf.join_labels(["bash", "git"])) == ["bash", "git"]
    assert mf.join_labels([]) == "[]"
