# SPDX-License-Identifier: AGPL-3.0-or-later
"""A labelled set of made-up sessions for the citation and correction
signals (``hooks/trust_signals.py``, NOBLIVION-38).

Each case is a short session: turns of (prompt, notes shown at that prompt,
reply). The label is the set of events a careful reader would record. The
set holds positives and near misses, and some hard cases that the rules get
wrong on purpose: they show the limit of matching by words.

``measure`` runs the scan on every case and counts true and false events
per signal. docs/trust.md quotes the result; a test keeps the two equal.
All names and rules here are invented.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

NOTES: dict[int, tuple[str, str]] = {
    101: (
        "feedback_no_git_stash",
        "Never use git stash; commit work in progress as a WIP commit instead.",
    ),
    102: (
        "project_deploy_via_main_merge",
        "A merge to main is the deploy; check the deployed revision after the merge.",
    ),
    103: (
        "feedback_limit_command_output",
        "Limit every command output with tail, head or quiet flags to keep the context small.",
    ),
    104: (
        "user_prefers_short_sentences",
        "Write short sentences in active voice with common words in every reply.",
    ),
    105: (
        "feedback_run_tests_before_push",
        "Run the full test suite before every push and report the pass count.",
    ),
    106: ("deploy", "Deploy with the release script from the tools folder."),
}

T0 = _dt.datetime(2026, 9, 1, 10, 0, 0, tzinfo=_dt.timezone.utc)
TURN_S = 300  # one turn every 5 minutes
SHOW_S = 1  # the prompt hook shows its rows 1 s after the prompt
REPLY_S = 60

# (name, signal, turns, label). A turn is (prompt, shown ids, reply) or
# (prompt, shown ids, reply, tool command). The label is a set of
# (kind, id).
Case = tuple[str, str, list[tuple[Any, ...]], set[tuple[str, int]]]

CASES: list[Case] = [
    # ── citation: positives ──
    (
        "names the note",
        "citation",
        [
            (
                "Save my work and switch branch.",
                [101, 103],
                "Per feedback_no_git_stash I made a WIP commit, then switched.",
            )
        ],
        {("use", 101)},
    ),
    (
        "names the note in backticks",
        "citation",
        [
            (
                "Show the build log.",
                [103],
                "Following `feedback_limit_command_output`, here is the last part of the log.",
            )
        ],
        {("use", 103)},
    ),
    (
        "repeats the rule word for word",
        "citation",
        [
            (
                "Merge the pull request.",
                [102],
                "Merged. A merge to main is the deploy; check the deployed revision "
                "after the merge, so I checked it: it matches.",
            )
        ],
        {("use", 102)},
    ),
    (
        "names the file",
        "citation",
        [
            (
                "Push the branch.",
                [105, 104],
                "I read feedback_run_tests_before_push.md and ran the suite: 210 passed.",
            )
        ],
        {("use", 105)},
    ),
    (
        "cites in two turns, one use",
        "citation",
        [
            ("Save my work.", [101], "Per feedback_no_git_stash, a WIP commit."),
            ("And again for the other tree.", [101], "Same rule, feedback_no_git_stash."),
        ],
        {("use", 101)},
    ),
    (
        "cites a note shown two turns ago",
        "citation",
        [
            ("Start the release work.", [102], "Started."),
            ("Fix the typo.", [], "Fixed."),
            ("Merge it.", [], "Merged; project_deploy_via_main_merge says that is the deploy."),
        ],
        {("use", 102)},
    ),
    # ── citation: near misses ──
    (
        "names a note that was never shown",
        "citation",
        [("Save my work.", [103], "Per feedback_no_git_stash, a WIP commit.")],
        set(),
    ),
    (
        "sets the note aside",
        "citation",
        [
            (
                "Save my work.",
                [101],
                "feedback_no_git_stash does not apply here, because this is a scratch clone.",
            )
        ],
        set(),
    ),
    (
        "paraphrase only",
        "citation",
        [("Save my work.", [101], "I will avoid stashing and commit the work instead.")],
        set(),
    ),
    (
        "short shared phrase",
        "citation",
        [("Save my work.", [101], "I keep the work in progress on the branch.")],
        set(),
    ),
    (
        "named before it was shown",
        "citation",
        [
            ("Save my work.", [], "I know feedback_no_git_stash from the docs."),
            ("Continue.", [101], "Done."),
        ],
        set(),
    ),
    (
        "a short common name",
        "citation",
        [("Ship it.", [106], "I will deploy now.")],
        set(),
    ),
    (
        "quotes the rule and calls it outdated",
        "citation",
        [
            (
                "Save my work.",
                [101],
                "Never use git stash; commit work in progress as a WIP commit instead is "
                "outdated for this repo.",
            )
        ],
        set(),
    ),
    (
        "the name is only in a command",
        "citation",
        [
            (
                "Find the rule.",
                [101],
                "Searching now.",
                "grep -r feedback_no_git_stash memory/",
            )
        ],
        set(),
    ),
    (
        "hard: lists the shown notes",
        "citation",
        [
            (
                "Which rules did you get?",
                [101, 103],
                "The index showed feedback_no_git_stash and feedback_limit_command_output.",
            )
        ],
        set(),
    ),
    (
        "hard: cannot find the note",
        "citation",
        [
            (
                "Open the push rule.",
                [105],
                "I could not open feedback_run_tests_before_push, the file is missing.",
            )
        ],
        set(),
    ),
    # ── correction: positives ──
    (
        "corrects the note the reply acted on",
        "correction",
        [
            ("Save my work.", [101], "Per feedback_no_git_stash I made a WIP commit."),
            (
                "No, in this repo a WIP commit on the shared branch is bad, use git stash here.",
                [101],
                "Understood.",
            ),
        ],
        {("contradict", 101)},
    ),
    (
        "names the note it corrects",
        "correction",
        [
            ("Merge it.", [102], "Merged; per project_deploy_via_main_merge that deploys."),
            (
                "No, project_deploy_via_main_merge is out of date, "
                "deploys now use the release tag.",
                [],
                "Noted.",
            ),
        ],
        {("contradict", 102)},
    ),
    (
        "corrects a verbatim rule",
        "correction",
        [
            (
                "Run the tests.",
                [103],
                "Limit every command output with tail, head or quiet flags to keep the "
                "context small: so I show the tail.",
            ),
            (
                "Wrong, do not limit the command output here, I need the full test output.",
                [],
                "Here is all of it.",
            ),
        ],
        {("contradict", 103)},
    ),
    (
        "corrects one prompt later",
        "correction",
        [
            ("Push it.", [105], "Per feedback_run_tests_before_push I ran the suite first."),
            ("ok, go on", [], "Pushed."),
            (
                "No, do not run the full test suite before every push, CI runs it.",
                [],
                "Understood.",
            ),
        ],
        {("contradict", 105)},
    ),
    # ── correction: near misses ──
    (
        "holds the user to the note",
        "correction",
        [
            ("Save my work.", [101], "Per feedback_no_git_stash I will commit."),
            ("No, you ignored feedback_no_git_stash, you used git stash.", [], "Sorry."),
        ],
        set(),
    ),
    (
        "corrects another topic",
        "correction",
        [
            ("Save my work.", [101], "Per feedback_no_git_stash I made a WIP commit."),
            ("No, the button colour should be blue.", [], "Fixed."),
        ],
        set(),
    ),
    (
        "the note was shown but not cited",
        "correction",
        [
            ("Run the tests.", [103], "All 210 tests passed."),
            ("No, limit the command output with tail.", [], "Done."),
        ],
        set(),
    ),
    (
        "the note is shown only with the correction",
        "correction",
        [
            ("Save my work.", [], "Committed."),
            ("No, never use git stash here, make a WIP commit.", [101], "Done."),
        ],
        set(),
    ),
    (
        "shown and cited too long ago",
        "correction",
        [
            ("Push it.", [105], "Per feedback_run_tests_before_push I ran the suite."),
            ("Next task.", [], "Done."),
            ("Next one.", [], "Done."),
            ("And one more.", [], "Done."),
            ("No, do not run the full test suite before every push.", [], "Understood."),
        ],
        set(),
    ),
    (
        "not a correction",
        "correction",
        [
            ("Run the tests.", [103], "Per feedback_limit_command_output, the tail only."),
            ("Good, also limit the command output for the logs.", [], "Done."),
        ],
        set(),
    ),
    (
        "two notes fit the correction",
        "correction",
        [
            (
                "Push and merge.",
                [102, 105],
                "Per feedback_run_tests_before_push and project_deploy_via_main_merge, done.",
            ),
            ("No, do not run the test suite before the merge to main deploy.", [], "OK."),
        ],
        set(),
    ),
    (
        "a correction with no topic",
        "correction",
        [
            ("Save my work.", [101], "Per feedback_no_git_stash I made a WIP commit."),
            ("No, that's wrong.", [], "Sorry."),
        ],
        set(),
    ),
    (
        "hard: cited, then broke it; the user agrees with the note",
        "correction",
        [
            (
                "Save my work.",
                [101],
                "Per feedback_no_git_stash I should commit, but I ran git stash to be quick.",
            ),
            ("No, never use git stash, make a WIP commit.", [], "Done."),
        ],
        set(),
    ),
]


def _iso(t: _dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def build(turns: list[tuple[Any, ...]]) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
    """Transcript records and the times each note was shown."""
    records: list[dict[str, Any]] = []
    exposures: dict[int, list[str]] = {}
    for i, turn in enumerate(turns):
        prompt, shown, reply = turn[0], turn[1], turn[2]
        tool = turn[3] if len(turn) > 3 else None
        start = T0 + _dt.timedelta(seconds=i * TURN_S)
        records.append(
            {
                "type": "user",
                "timestamp": _iso(start).replace("+00:00", "Z"),
                "message": {"role": "user", "content": prompt},
            }
        )
        for mid in shown:
            exposures.setdefault(mid, []).append(_iso(start + _dt.timedelta(seconds=SHOW_S)))
        content: list[dict[str, Any]] = [{"type": "text", "text": reply}]
        if tool:
            content.append(
                {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": tool}}
            )
        records.append(
            {
                "type": "assistant",
                "timestamp": _iso(start + _dt.timedelta(seconds=REPLY_S)),
                "message": {"role": "assistant", "content": content},
            }
        )
    return records, exposures


def shown_map() -> dict[int, dict[str, str]]:
    return {mid: {"name": name, "rule": rule} for mid, (name, rule) in NOTES.items()}


def measure(signals: Any) -> dict[str, dict[str, Any]]:
    """Per signal: true and false events, missed events, precision and the
    names of the cases with a false or missed event."""
    out: dict[str, dict[str, Any]] = {}
    kinds = {"citation": "use", "correction": "contradict"}
    for name, signal, turns, label in CASES:
        records, exposures = build(turns)
        events, _ = signals.scan(records, shown_map(), exposures, None, True, True)
        kind = kinds[signal]
        got = {(e["kind"], e["mv_id"]) for e in events if e["kind"] == kind}
        want = {x for x in label if x[0] == kind}
        row = out.setdefault(
            signal, {"cases": 0, "tp": 0, "fp": 0, "fn": 0, "false": [], "missed": []}
        )
        row["cases"] += 1
        row["tp"] += len(got & want)
        row["fp"] += len(got - want)
        row["fn"] += len(want - got)
        if got - want:
            row["false"].append(name)
        if want - got:
            row["missed"].append(name)
    for row in out.values():
        found = row["tp"] + row["fp"]
        row["precision"] = row["tp"] / found if found else 0.0
        row["recall"] = row["tp"] / (row["tp"] + row["fn"]) if row["tp"] + row["fn"] else 0.0
    return out
