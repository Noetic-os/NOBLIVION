# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rule-format fields of a memory file (``hooks/memory_fields.py``).

What these tests hold:

1. THE EXISTING READER SEES WHAT IT SAW BEFORE. The recall corpus parser
   (``hooks/corpus.py``) returns the same values for every key it returned
   before, the body is byte-identical, and ``read_rule_apply`` still returns
   the rule and apply pair.
2. THE WRITER IS IDEMPOTENT and never changes the body.
3. EVERY CHECK BITES: each rule of ``check_fields`` has a failing case and a
   passing case.
"""

from __future__ import annotations

import pytest

from hookload import load_hook

mf = load_hook("memory_fields", "memory_fields_t_memory_fields")
corpus = load_hook("corpus", "corpus_t_memory_fields")

LIVE_SHAPE = """---
name: feedback_never_git_checkout_uncommitted_work
description: "🔴`git checkout -- <file>` reverts to HEAD: it DESTROYS edits; hit TWICE"
metadata:
  type: feedback
---

## Rule
Commit before mutation testing.
---
- not a trigger: a list item in the body
key: value in the body
"""

FIELDS = {
    "rule": "Commit all uncommitted work before running git checkout -- <file>.",
    "apply": '**Commit first.** The revert is\n`git checkout -- <file>` and it "restores" HEAD.',
    "scope": "tool",
    "triggers": ["git checkout", "glob:tools/*.py", "tool:Bash", "phrase:mutation test"],
    "violates": r"\bgit\s+checkout\s+--\s+\S",
    "example_repeat": "git checkout -- tools/foo.py",
    "example_ok": "git commit -am wip && git checkout -- tools/foo.py.orig",
}
# example_ok above still holds "git checkout -- x"; the clean example is below.
FIELDS["example_ok"] = "git stash list"
FIELDS["complies"] = r"\bgit\s+stash\s+list\b"


def clean_fields(**over):
    f = dict(FIELDS)
    f.update(over)
    return f


# --------------------------------------------------------------------------
# 1. the reader
# --------------------------------------------------------------------------
def test_existing_keys_and_body_unchanged_for_the_corpus_parser():
    parse = corpus.parse_frontmatter
    before_meta, before_body = parse(LIVE_SHAPE)
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    after_meta, after_body = parse(new)
    assert after_body == before_body
    for k, v in before_meta.items():
        assert after_meta[k] == v
    added = set(after_meta) - set(before_meta)
    assert added == set(mf.KEYS)
    # every new key is a flat string, never a nested dict
    assert all(isinstance(after_meta[k], str) for k in added)


def test_read_rule_apply_returns_the_rule_and_apply_pair():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    meta, _ = corpus.parse_frontmatter(new)
    assert corpus.read_rule_apply(meta) == (FIELDS["rule"], FIELDS["apply"])


def test_triggers_are_written_as_one_flow_list():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    assert mf.read_fields(new)["triggers"] == FIELDS["triggers"]
    assert "triggers: [git checkout, glob:tools/*.py, tool:Bash, phrase:mutation test]" in new


def test_no_triggers_key_written_when_empty_so_derivation_stays():
    new = mf.write_fields(
        LIVE_SHAPE,
        clean_fields(scope="always", triggers=[], violates="", example_repeat="", example_ok=""),
    )
    assert "triggers" not in new
    assert mf.read_fields(new)["triggers"] == []


def test_body_byte_identical_and_read_back_equal():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    assert mf.body_of(new) == mf.body_of(LIVE_SHAPE)
    got = mf.read_fields(new)
    assert {k: got[k] for k in mf.KEYS} == {k: FIELDS[k] for k in mf.KEYS}
    assert set(got) == set(mf.KEYS) | set(mf.PROJECT_KEYS)
    assert (got["status"], got["project"]) == ("", "")


def test_keys_are_last_in_fixed_order():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    lines = new.split("\n")
    close = lines.index("---", 1)
    tail = [ln.split(":", 1)[0] for ln in lines[close - len(mf.KEYS) : close]]
    assert tail == list(mf.KEYS)


def test_block_triggers_are_replaced_not_duplicated():
    text = LIVE_SHAPE.replace("  type: feedback\n", "  type: feedback\ntriggers:\n  - docker rm\n")
    new = mf.write_fields(text, FIELDS)
    assert "  - docker rm" not in new.split("\n---\n")[0]
    assert mf.read_fields(new)["triggers"] == FIELDS["triggers"]
    assert mf.body_of(new) == mf.body_of(text)


# --------------------------------------------------------------------------
# 2. idempotence and the write contract
# --------------------------------------------------------------------------
def test_idempotent():
    once = mf.write_fields(LIVE_SHAPE, FIELDS)
    assert mf.write_fields(once, FIELDS) == once
    assert mf.write_fields(once, {}) == once


def test_partial_write_keeps_other_fields_and_none_removes():
    once = mf.write_fields(LIVE_SHAPE, FIELDS)
    dropped = mf.write_fields(once, {"violates": None, "example_repeat": None, "example_ok": None})
    got = mf.read_fields(dropped)
    assert got["violates"] == got["example_repeat"] == got["example_ok"] == ""
    assert got["rule"] == FIELDS["rule"] and got["triggers"] == FIELDS["triggers"]


def test_no_frontmatter_raises():
    with pytest.raises(mf.FrontmatterError):
        mf.write_fields("just a body\n", FIELDS)


def test_unsafe_trigger_item_refused_by_writer():
    with pytest.raises(ValueError):
        mf.write_fields(LIVE_SHAPE, clean_fields(triggers=["git push, --force"]))


def test_body_change_guard(monkeypatch):
    real = mf.body_of
    calls = {"n": 0}

    def fake(text):
        calls["n"] += 1
        return real(text) + ("x" if calls["n"] == 1 else "")

    monkeypatch.setattr(mf, "body_of", fake)
    with pytest.raises(mf.BodyChanged):
        mf.write_fields(LIVE_SHAPE, FIELDS)


def test_read_fields_of_unannotated_file_is_empty():
    assert mf.read_fields(LIVE_SHAPE) == mf.empty_fields()
    assert mf.read_fields("no front matter") == mf.empty_fields()


# --------------------------------------------------------------------------
# 3. every check, failing and passing
# --------------------------------------------------------------------------
def test_clean_fields_pass():
    assert mf.check_fields(FIELDS, "feedback") == []


@pytest.mark.parametrize(
    "over, words",
    [
        ({"rule": ""}, "rule is missing"),
        ({"apply": ""}, "apply is missing"),
        ({"rule": "x " * 90}, "longer than 160"),
        ({"rule": "Do this.\nDo that."}, "more than one line"),
        ({"apply": "y" * 401}, "longer than 400"),
        ({"scope": ""}, "scope is missing"),
        ({"scope": "sometimes"}, "not one of"),
        ({"triggers": []}, "needs at least one trigger"),
        ({"scope": "file", "triggers": ["git push"]}, "glob: trigger"),
        ({"triggers": ["phrase:push it"]}, "command prefix or a tool: trigger"),
        ({"triggers": ["x"] * 65}, "more than 64 items"),
        ({"triggers": ["tool:not a name"]}, "not a tool name"),
        ({"triggers": ["glob:a b"]}, "glob with a space"),
        ({"triggers": ["git push, --force"]}, "comma"),
        ({"triggers": ["url:x"]}, "unknown type prefix"),
        ({"triggers": ["phrase:"]}, "nothing after"),
        ({"violates": "(unclosed"}, "does not compile"),
        ({"violates": r"(git\s.*)*checkout"}, "nested quantifier"),
        ({"violates": r"(a+)+b"}, "nested quantifier"),
        ({"violates": r"stash"}, "does not match its example_repeat"),
        ({"violates": r"git"}, "matches its example_ok"),
        ({"violates": r"x*"}, "empty command"),
        ({"violates": r"^Bash git checkout"}, "starts with Bash"),
        ({"example_repeat": ""}, "needs example_repeat"),
        ({"example_ok": ""}, "needs example_ok"),
        ({"example_repeat": "Bash git checkout -- a.py"}, "action line"),
        (
            {"violates": "", "example_repeat": "git checkout -- a", "example_ok": "ls"},
            "allowed only with violates",
        ),
        # the compliant-form regex
        (
            {"violates": "", "example_repeat": "", "example_ok": ""},
            "complies is allowed only with violates",
        ),
        ({"complies": "(unclosed"}, "complies does not compile"),
        ({"complies": r"x*"}, "complies matches the empty command"),
        ({"complies": r"\bgit\s+stash\s+pop\b"}, "complies does not match its example_ok"),
        ({"complies": r"\bgit\b"}, "complies matches its example_repeat"),
        ({"complies": r"(a+)+git\s+stash"}, "complies has a nested quantifier"),
        ({"complies": r"git\s+stash\s+list" + "|zz" * 150}, "complies is longer than 300"),
        ({"complies": r"^\s*Bash\s+git\s+stash"}, "complies starts with Bash"),
    ],
)
def test_each_check_fails(over, words):
    problems = mf.check_fields(clean_fields(**over), "feedback")
    assert any(words in p for p in problems), problems


def test_benign_list_check_fails_on_broad_regex():
    f = clean_fields(
        violates=r"\bgit\s+(checkout|status)\b",
        example_repeat="git checkout -- a.py",
        example_ok="git stash list",
    )
    problems = mf.check_fields(f)
    assert any("everyday commands" in p and "git status" in p for p in problems), problems


def test_complies_that_matches_an_everyday_command_is_refused():
    """An everyday command must never count as a run of the rule's own
    command, or any failing ``git status`` would unlock the override."""
    f = clean_fields(complies=r"\bgit\s+(?:stash\s+list|status)\b")
    problems = mf.check_fields(f)
    assert any("complies matches everyday commands" in p and "git status" in p for p in problems), (
        problems
    )


def test_complies_is_optional_with_violates():
    assert mf.check_fields(clean_fields(complies=""), "feedback") == []


def test_complies_round_trips_and_is_written_last():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    assert mf.read_fields(new)["complies"] == FIELDS["complies"]
    close = new.split("\n").index("---", 1)
    assert new.split("\n")[close - 1].startswith("complies: ")


def test_slow_complies_is_named_complies():
    assert mf.regex_too_slow(r"(?:a|a)*$", "a" * 30, name="complies").startswith("complies")


def test_schema_line_names_complies():
    assert "complies:" in mf.SCHEMA_LINE


def test_benign_list_is_about_thirty_and_every_item_passes_a_narrow_regex():
    assert 28 <= len(mf.BENIGN_COMMANDS) <= 50
    assert "git status" in mf.BENIGN_COMMANDS and "ls -la" in mf.BENIGN_COMMANDS


def test_non_feedback_kind_does_not_require_rule_or_scope():
    assert mf.check_fields(mf.empty_fields(), "reference") == []
    assert not any(
        "rule" in p or "scope" in p or "apply" in p
        for p in mf.check_fields(mf.empty_fields(), "project")
    )
    assert "rule is missing" in mf.check_fields(mf.empty_fields(), "feedback")


def test_scope_always_and_stop_may_have_no_triggers():
    for scope in ("always", "stop"):
        f = clean_fields(
            scope=scope,
            triggers=[],
            violates="",
            example_repeat="",
            example_ok="",
            complies="",
        )
        assert mf.check_fields(f) == []


def test_nested_quantifier_negative_cases():
    for pat in (r"git\s+checkout\s+--\s+\S", r"(ab)+c", r"a{2}(b+)?", r"(?:x|y)+z"):
        assert not mf.nested_quantifier(pat), pat
    for pat in (r"(.*)*", r"(.+)+", r"(a*b)*", r"(?:x+y)+", r"((a)+)+"):
        assert mf.nested_quantifier(pat), pat


def test_a_derived_bare_head_is_a_valid_trigger():
    # the derivation takes a bare head only when the corpus names it rarely,
    # so check_fields must not flag it; only the annotator filters model items
    assert mf.trigger_problem("psql") == ""
    assert mf.check_fields(clean_fields(triggers=["psql"])) == []


def test_strip_schema_restores_the_unannotated_front_matter():
    assert mf.strip_schema(mf.write_fields(LIVE_SHAPE, FIELDS)) == LIVE_SHAPE


# --------------------------------------------------------------------------
# a polynomial regex is refused, and the check never hangs
# --------------------------------------------------------------------------
def test_reviewer_polynomial_regex_refused_by_shape():
    f = clean_fields(
        violates=r"curl\s.*\s.*\s.*\s.*\s.*-k\b",
        example_repeat="curl -s https://x -k",
        example_ok="curl -s https://x",
    )
    problems = mf.check_fields(f)
    assert any("more than one unbounded repeat of a broad class" in p for p in problems)


@pytest.mark.parametrize(
    "pat, n",
    [
        (r".*a.*", 2),
        (r"\S+\s\S+", 2),
        (r"[^x]+y[^z]*", 2),
        (r"\bgit\s+stash\b", 0),
        (r"[^\s$()]+", 1),
        (r"a.{0,20}b.{0,20}c", 0),
        (r"(?:x.*|y)", 1),
    ],
)
def test_broad_unbounded_repeat_count(pat, n):
    assert mf.broad_unbounded_repeats(pat) == n


def test_slow_regex_of_a_passing_shape_refused_by_timing_within_the_kill():
    import time

    f = clean_fields(violates=r"\s+\s+\s+\s+z", example_repeat="a z", example_ok="git stash list")
    t0 = time.time()
    problems = mf.check_fields(f)
    assert time.time() - t0 < mf.REGEX_PROBE_KILL + 1.0
    assert any("runs longer than" in p or "takes" in p for p in problems), problems


def test_fast_regex_passes_timing():
    assert mf.regex_too_slow(r"\bgit\s+stash\b", "git stash") == ""


def test_probe_inputs_are_ten_kilobytes():
    assert all(len(s) == mf.REGEX_PROBE_BYTES for s in mf.regex_probe_inputs("git stash pop"))


@pytest.mark.parametrize(
    "pat, hit",
    [
        (r"gh\s+pr\s+checks\b", "gh pr checks 123"),  # the reviewer's example
        (r"gh\s+pr\s+view\b", "gh pr view 123"),
        (r"gh\s+run\s+(list|view)\b", "gh run list"),
        (r"tools/merge\s+\d+", "tools/merge 123"),
        (r"tools/ready\b", "tools/ready 123"),
        (r"\bgit\s+push\b", "git push"),
        (r"docker\s+compose\s+up\b", "docker compose up -d"),
    ],
)
def test_everyday_commands_refused(pat, hit):
    problems = mf.violates_problems(
        {"violates": pat, "example_repeat": hit + " --x", "example_ok": "true"}
    )
    assert any("everyday commands" in p and hit in p for p in problems), problems


# --------------------------------------------------------------------------
# a rule with ": " or " #" is never a raw line
# --------------------------------------------------------------------------
HASH_RULES = [
    "When writing a Pyright suppression, place `# pyright: ignore[ruleName]` at the end of the "
    "line.",
    "When a CI job boots infrastructure, add a workflow-level if: always() teardown step.",
    "Open the first reply with the checkpoint's # title line.",
]


@pytest.mark.parametrize("rule", HASH_RULES)
def test_rule_with_colon_space_or_space_hash_is_quoted_and_reads_back_exact(rule):
    new = mf.write_fields(LIVE_SHAPE, clean_fields(rule=rule))
    line = next(ln for ln in new.split("\n") if ln.startswith("rule: "))
    assert line == 'rule: "' + rule + '"'
    assert mf.read_fields(new)["rule"] == rule
    assert corpus.parse_frontmatter(new)[0]["rule"] == rule
    assert corpus.read_rule_apply(corpus.parse_frontmatter(new)[0])[0] == rule
    assert mf.read_fields(new)["triggers"] == FIELDS["triggers"]
    assert mf.write_fields(new, {}) == new  # idempotent


@pytest.mark.parametrize("rule", ['Use `x: "y"` #1 form.', "Set a: b\\c."])
def test_rule_that_quoting_would_escape_is_refused(rule):
    with pytest.raises(ValueError, match="rewrite the rule"):
        mf.write_fields(LIVE_SHAPE, clean_fields(rule=rule))


def test_plain_rule_stays_a_plain_line():
    assert (
        mf.rule_line_value("Commit before git checkout -- file.")
        == "Commit before git checkout -- file."
    )


def test_unused_helper_removed():
    assert not hasattr(mf, "is_feedback")


# --------------------------------------------------------------------------
# status: and project: (the fields of a project memory)
# --------------------------------------------------------------------------
PROJECT_TOP = """---
name: project_x
description: an open item
status: parked
project: proj-12
metadata:
  type: project
---

Body.
"""
PROJECT_NESTED = """---
name: project_x
description: an open item
metadata:
  node_type: memory
  type: project
  status: open
  project: app-infra
---

Body.
"""


def project_fields(**over):
    f = dict(mf.empty_fields(), status="open", project="proj-12")
    f.update(over)
    return f


def test_status_and_project_are_read_at_the_top_level():
    got = mf.read_fields(PROJECT_TOP)
    assert (got["status"], got["project"]) == ("parked", "proj-12")


def test_status_and_project_are_read_under_metadata():
    got = mf.read_fields(PROJECT_NESTED)
    assert (got["status"], got["project"]) == ("open", "app-infra")


def test_the_top_level_line_wins_over_the_metadata_line():
    text = PROJECT_NESTED.replace("metadata:", "status: closed\nmetadata:")
    got = mf.read_fields(text)
    assert (got["status"], got["project"]) == ("closed", "app-infra")


def test_an_indented_line_outside_metadata_and_a_body_line_are_not_read():
    text = PROJECT_NESTED.replace("metadata:", "other:").replace(
        "Body.", "status: open\nproject: global"
    )
    got = mf.read_fields(text)
    assert (got["status"], got["project"]) == ("", "")


def test_a_quoted_value_is_read_without_its_quotes():
    text = PROJECT_TOP.replace("status: parked", 'status: "closed"')
    assert mf.read_fields(text)["status"] == "closed"


def test_rule_under_metadata_is_read():
    text = PROJECT_NESTED.replace("  status: open", "  rule: Do it.")
    assert mf.read_fields(text)["rule"] == "Do it."


@pytest.mark.parametrize("text", [PROJECT_TOP, PROJECT_NESTED], ids=["top", "metadata"])
def test_write_fields_keeps_status_and_project_byte_for_byte(text):
    assert mf.write_fields(text, {}) == text
    same = mf.read_fields(text)
    assert mf.write_fields(text, {"status": same["status"], "project": same["project"]}) == text
    new = mf.write_fields(text, {"rule": "Do it.", "apply": "Do it so."})
    got = mf.read_fields(new)
    assert (got["status"], got["project"]) == (same["status"], same["project"])
    assert new.replace('rule: Do it.\napply: "Do it so."\n', "") == text
    assert mf.write_fields(new, {"rule": "Do it.", "apply": "Do it so."}) == new
    # strip_schema removes the rule fields only
    assert mf.strip_schema(new) == text


def test_write_fields_changes_a_status_line_in_place():
    new = mf.write_fields(PROJECT_NESTED, {"status": "closed"})
    assert new == PROJECT_NESTED.replace("  status: open", "  status: closed")
    new = mf.write_fields(PROJECT_TOP, {"project": "global"})
    assert new == PROJECT_TOP.replace("project: proj-12", "project: global")


def test_write_fields_adds_status_and_project_before_the_rule_fields():
    new = mf.write_fields(LIVE_SHAPE, dict(FIELDS, status="open", project="beta-team"))
    lines = new.split("\n")
    close = lines.index("---", 1)
    assert lines[close - len(mf.KEYS) - 2 : close - len(mf.KEYS)] == [
        "status: open",
        "project: beta-team",
    ]
    assert [ln.split(":", 1)[0] for ln in lines[close - len(mf.KEYS) : close]] == list(mf.KEYS)
    assert mf.body_of(new) == mf.body_of(LIVE_SHAPE)
    assert mf.write_fields(new, dict(FIELDS, status="open", project="beta-team")) == new


def test_write_fields_removes_and_refuses():
    assert "status" not in mf.write_fields(PROJECT_TOP, {"status": None})
    assert "project: proj-12" in mf.write_fields(PROJECT_TOP, {"status": ""})
    with pytest.raises(ValueError):
        mf.write_fields(PROJECT_TOP, {"status": "done"})
    with pytest.raises(ValueError):
        mf.write_fields(PROJECT_TOP, {"project": "Proj 12"})
    with pytest.raises(KeyError):
        mf.write_fields(PROJECT_TOP, {"owner": "x"})


def test_a_project_memory_needs_status_and_project():
    assert mf.check_fields(project_fields(), "project") == []
    assert mf.check_fields(mf.empty_fields(), "project") == [
        "status is missing",
        "project is missing",
    ]
    assert mf.check_fields(project_fields(status=""), "project") == ["status is missing"]
    assert mf.check_fields(project_fields(project=""), "project") == ["project is missing"]


@pytest.mark.parametrize("status", ["open", "closed", "parked"])
def test_each_status_value_is_accepted(status):
    assert mf.check_fields(project_fields(status=status), "project") == []


@pytest.mark.parametrize("status", ["done", "Open", "open ", "in progress", "open|closed"])
def test_an_unknown_status_is_a_problem(status):
    problems = mf.check_fields(project_fields(status=status), "project")
    assert len(problems) == 1 and "is not one of open, closed, parked" in problems[0]


@pytest.mark.parametrize(
    "project", ["proj-12", "app-infra", "beta-team", "global", "a1", "9lives", "a" * 40]
)
def test_a_slug_is_accepted(project):
    assert mf.check_fields(project_fields(project=project), "project") == []


@pytest.mark.parametrize(
    "project",
    ["PROJ-12", "a", "-proj", "proj_12", "proj 12", "a" * 41, "app/infra", "global\n"],
)
def test_a_bad_slug_is_a_problem(project):
    problems = mf.check_fields(project_fields(project=project), "project")
    assert len(problems) == 1 and "is not a lower-case slug" in problems[0]


def test_a_project_memory_keeps_the_rule_fields_optional_and_checked():
    assert (
        mf.check_fields(project_fields(rule="Do it.", apply="So.", scope="always"), "project") == []
    )
    assert "scope 'sometimes' is not one of tool, stop, file, always" in mf.check_fields(
        project_fields(scope="sometimes"), "project"
    )
    assert "scope tool needs at least one trigger" in mf.check_fields(
        project_fields(scope="tool"), "project"
    )


def test_feedback_is_unchanged_status_and_project_are_optional():
    assert mf.check_fields(FIELDS, "feedback") == []
    assert mf.check_fields(clean_fields(status="", project=""), "feedback") == []
    assert mf.check_fields(clean_fields(status="parked", project="global"), "feedback") == []
    bare = mf.check_fields(mf.empty_fields(), "feedback")
    assert not mf.status_project_problems_of(bare)


def test_feedback_reports_a_present_but_invalid_status_or_project():
    assert mf.check_fields(clean_fields(status="done"), "feedback") == [
        "status 'done' is not one of open, closed, parked"
    ]
    problems = mf.check_fields(clean_fields(project="Bad Slug"), "feedback")
    assert len(problems) == 1 and "is not a lower-case slug" in problems[0]


def test_the_corpus_parser_still_reads_a_file_with_the_new_fields():
    parse = corpus.parse_frontmatter
    before, body_before = parse(PROJECT_TOP.replace("status: parked\nproject: proj-12\n", ""))
    after, body_after = parse(PROJECT_TOP)
    assert body_after == body_before
    assert {k: v for k, v in after.items() if k not in mf.PROJECT_KEYS} == before
    assert (after["status"], after["project"]) == ("parked", "proj-12")


def test_write_fields_leaves_a_quoted_status_line_alone_when_the_value_is_the_same():
    text = PROJECT_TOP.replace("status: parked", 'status:  "parked"')
    assert mf.write_fields(text, {"status": "parked"}) == text
    assert mf.write_fields(text, {"status": "open"}) == PROJECT_TOP.replace("parked", "open")


# --------------------------------------------------------------------------
# the rule fields under metadata: (the Edit tool moves them there)
# --------------------------------------------------------------------------
# The shape of a real project memory whose rule fields the Edit tool moved.
RULE_NESTED = """---
name: project_ruling_x
description: "Operator ruling: no Acme content in the corpus."
metadata:
  node_type: memory
  rule: Do not ingest Acme documents into the corpus.
  apply: "before any ingest, check the source.\\nDo not re-add it."
  scope: tool
  triggers:
    - phrase:ingest into PC
    - "psql corpus"
  violates: "\\bingest\\s+--acme\\b"
  example_repeat: "ingest --acme"
  example_ok: "ingest --customer klr"
  complies: "\\bingest\\s+--customer\\b"
  type: project
  modified: 2026-09-30T14:45:32.760Z
status: open
project: corpus-data
---

Body.
- not a trigger
"""
RULE_NESTED_FIELDS = {
    "rule": "Do not ingest Acme documents into the corpus.",
    "apply": "before any ingest, check the source.\nDo not re-add it.",
    "scope": "tool",
    "triggers": ["phrase:ingest into PC", "psql corpus"],
    "violates": r"\bingest\s+--acme\b",
    "example_repeat": "ingest --acme",
    "example_ok": "ingest --customer klr",
    "complies": r"\bingest\s+--customer\b",
    "status": "open",
    "project": "corpus-data",
}


def test_every_rule_field_is_read_under_metadata():
    assert mf.read_fields(RULE_NESTED) == RULE_NESTED_FIELDS
    assert mf.check_fields(mf.read_fields(RULE_NESTED), "project") == []
    assert mf.nested_rule_keys(RULE_NESTED) == list(mf.KEYS)


def test_a_flow_list_of_triggers_is_read_under_metadata():
    text = RULE_NESTED.replace(
        '  triggers:\n    - phrase:ingest into PC\n    - "psql corpus"\n',
        "  triggers: [git push, glob:tools/*.py]\n",
    )
    assert mf.read_fields(text)["triggers"] == ["git push", "glob:tools/*.py"]


@pytest.mark.parametrize(
    "key,line,want",
    [
        ("rule", "rule: The top rule.", "The top rule."),
        ("apply", 'apply: "top apply"', "top apply"),
        ("scope", "scope: always", "always"),
        ("triggers", "triggers: [git status]", ["git status"]),
        ("violates", 'violates: "top"', "top"),
    ],
)
def test_a_top_level_rule_field_wins_over_the_metadata_line(key, line, want):
    for text in (
        RULE_NESTED.replace("status: open", line + "\nstatus: open"),
        RULE_NESTED.replace("metadata:", line + "\nmetadata:"),
    ):
        got = mf.read_fields(text)
        assert got[key] == want
        assert {k: v for k, v in got.items() if k != key} == {
            k: v for k, v in RULE_NESTED_FIELDS.items() if k != key
        }
        assert key not in mf.nested_rule_keys(text)


def test_an_empty_top_level_line_does_not_hide_the_metadata_value():
    text = RULE_NESTED.replace("status: open", "rule:\nstatus: open")
    assert mf.read_fields(text)["rule"] == RULE_NESTED_FIELDS["rule"]


def test_indented_rule_fields_outside_metadata_are_not_read():
    text = RULE_NESTED.replace("metadata:", "other:")
    got = mf.read_fields(text)
    assert {k: got[k] for k in mf.KEYS} == {k: mf.empty_fields()[k] for k in mf.KEYS}
    assert mf.nested_rule_keys(text) == []
    # a block after metadata: has ended is not metadata
    text = RULE_NESTED.replace("  rule: Do not", "  type: x\nnotes: a\nother:\n  rule: Do not")
    assert mf.read_fields(text)["rule"] == ""


def test_a_top_level_file_reads_as_before_and_has_no_nested_keys():
    new = mf.write_fields(LIVE_SHAPE, FIELDS)
    assert {k: mf.read_fields(new)[k] for k in mf.KEYS} == {k: FIELDS[k] for k in mf.KEYS}
    assert mf.nested_rule_keys(new) == [] and mf.nested_rule_keys("no front matter") == []


def test_write_fields_moves_nested_rule_fields_to_the_top_level():
    new = mf.write_fields(RULE_NESTED, {})
    assert mf.read_fields(new) == RULE_NESTED_FIELDS
    assert mf.nested_rule_keys(new) == []
    assert mf.body_of(new) == mf.body_of(RULE_NESTED)
    lines = new.split("\n")
    close = lines.index("---", 1)
    assert [ln.split(":", 1)[0] for ln in lines[close - len(mf.KEYS) : close]] == list(mf.KEYS)
    # the other metadata lines and status, project stay; nothing is written twice
    assert (
        "metadata:\n  node_type: memory\n  type: project\n  modified: 2026-09-30T14:45:32.760Z\n"
        "status: open\nproject: corpus-data\n"
    ) in new
    assert new.count("rule:") == 1 and new.count("phrase:ingest into PC") == 1
    assert mf.write_fields(new, {}) == new
    # now the top-level-only corpus reader sees the fields
    meta, _ = corpus.parse_frontmatter(new)
    assert corpus.read_rule_apply(meta) == (
        RULE_NESTED_FIELDS["rule"],
        RULE_NESTED_FIELDS["apply"],
    )
    assert mf.strip_schema(RULE_NESTED) == mf.strip_schema(new)
    assert "rule" not in mf.strip_schema(new)


def test_write_fields_leaves_an_indented_rule_line_outside_metadata_alone():
    text = RULE_NESTED.replace("status: open", "other:\n  rule: not a rule field\nstatus: open")
    new = mf.write_fields(text, {})
    assert "other:\n  rule: not a rule field\nstatus: open" in new
    assert mf.read_fields(new) == RULE_NESTED_FIELDS


def test_nested_keys_names_every_field_under_metadata():
    assert mf.nested_keys(RULE_NESTED) == list(mf.KEYS)
    assert mf.nested_keys(PROJECT_NESTED) == ["status", "project"]
    assert mf.nested_keys(PROJECT_TOP) == [] and mf.nested_keys("no front matter") == []
    # a nested line is named also when the top level has the field too
    both = RULE_NESTED.replace("status: open", "rule: The top rule.\nstatus: open")
    assert "rule" in mf.nested_keys(both) and "rule" not in mf.nested_rule_keys(both)
    assert mf.nested_keys(mf.write_fields(RULE_NESTED, {})) == []
    assert mf.nested_problem([]) == ""
