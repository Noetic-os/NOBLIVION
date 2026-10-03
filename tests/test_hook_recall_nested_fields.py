# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook reads ``rule:`` and ``apply:`` under ``metadata:``.

The Edit and Write tools of Claude Code sometimes rewrite the front matter of a
memory file and move the rule fields under ``metadata:`` (indented). A hook
that read the two fields at the top level only would drop the rule row and the
APPLY line of such a memory without any message.

What these tests hold:

1. SAME OUTPUT. A memory with its fields under ``metadata:`` gives the same
   rule row and the same APPLY line as the same memory with top-level fields.
   A memory with no such field shows the store summary and no APPLY line.
2. TOP LEVEL WINS, for each field on its own, when both places hold a value.
3. ONLY ``metadata:`` IS READ. A field indented under another key is not a rule.
4. A BROKEN FRONT MATTER gives the old result and no exception.
5. NO OPTIONAL SIBLING IS NEEDED. The hook with only its required siblings
   (``corpus.py``, ``store_client.py``) in a folder reads the fields.

The recall hook parses no ``triggers:`` field (the guard table does), so there
is no trigger test here.

Every test runs on a tmp cache folder, a tmp data dir and a tmp memory folder,
and the store call is replaced.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest

import recall_helpers
from hookload import HOOKS, load_hook

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp cache folder and a tmp memory folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_hook_nested_fields")

N = 6
SID = "nested-fields-test-session"


def _rule(i: int) -> str:
    return f"Do step {i} before the first command."


def _apply(i: int) -> str:
    return f"run the tool number {i} with --flag\nthen check: the result {i}"


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    """No test may reach the live data dir, memory folder, recall cache or
    home folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(tmp_path / "no-memory"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NOBLIVION_STORE_AUTOSTART", "0")


@pytest.fixture(autouse=True)
def daemon(monkeypatch):
    def fake(query, k, environ=None, root=None, include_mined=False):
        return [
            hook.IndexLine(
                rank=i,
                mid=19000 + i,
                title=f"feedback_m{i:02d}",
                summary=f"summary {i}",
                score=round(0.90 - i / 100, 2),
            )
            for i in range(1, N + 1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)


def _front(i: int, shape: str) -> list[str]:
    rule, apply_ = f"rule: {_rule(i)}", "apply: " + json.dumps(_apply(i))
    head = ["---", f"name: feedback_m{i:02d}", f"description: about m{i}"]
    if shape == "top":
        return head + ["metadata:", "  type: feedback", rule, apply_, "---"]
    if shape == "nested":
        return head + ["metadata:", "  type: feedback", "  " + rule, "  " + apply_, "---"]
    if shape == "nested_tab":
        return head + ["metadata:", "\ttype: feedback", "\t" + rule, "\t" + apply_, "---"]
    if shape == "none":
        return head + ["metadata:", "  type: feedback", "---"]
    raise AssertionError(shape)


def _corpus(folder: Path, shape: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, N + 1):
        (folder / f"feedback_m{i:02d}.md").write_text(
            "\n".join(_front(i, shape) + ["", f"BODY-TEXT-OF-m{i}.", ""])
        )
    return folder


def _serve(tmp_path: Path, folder: Path, cache: str, **extra: str) -> str:
    env: dict[str, str] = recall_helpers.hook_env(
        tmp_path,
        NOBLIVION_RECALL_CACHE_DIR=str(tmp_path / cache),
        NOBLIVION_RECALL_INDEX="1",
        HOME=str(tmp_path / "home"),
    )
    env.update(
        {
            hook.MEMORY_DIR_ENV: str(folder),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_APPLY_ENV: "1",
            hook.INDEX_DROP_NO_RULE_ENV: "1",
        }
    )
    env.update(extra)
    payload = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt="a prompt about steps")
    )
    out = io.StringIO()
    hook.run(payload, out, env)
    return out.getvalue()


def _meta(*lines: str) -> dict:
    return hook.parse_frontmatter("\n".join(("---",) + lines + ("---", "", "body")))[0]


# ── 1. same output ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["nested", "nested_tab"])
def test_nested_fields_give_the_same_hook_output_as_top_level_fields(tmp_path, shape):
    top = _serve(tmp_path, _corpus(tmp_path / "top", "top"), "cache-top")
    nested = _serve(tmp_path, _corpus(tmp_path / shape, shape), "cache-nested")
    assert nested == top
    for i in range(1, N + 1):
        assert _rule(i) in nested  # every rule row
    assert "run the tool number 1 with --flag" in nested  # an APPLY line
    assert "APPLY" in nested


def test_a_memory_with_no_rule_field_has_no_rule_row_and_no_apply_line(tmp_path):
    """The control for the test above: without the fields a row shows the
    store summary, so the rule rows and the APPLY lines of the nested corpus
    come from the nested fields."""
    out = _serve(tmp_path, _corpus(tmp_path / "none", "none"), "cache-none")
    assert "summary 1 (feedback_m01)" in out
    assert "Do step" not in out
    assert "run the tool number" not in out
    assert "APPLY" not in out


def test_corpus_reader_sets_rule_and_apply_from_nested_fields(tmp_path):
    top = hook.load_memory_corpus(str(_corpus(tmp_path / "top", "top")))
    nested = hook.load_memory_corpus(str(_corpus(tmp_path / "nested", "nested")))
    assert [(m.name, m.rule, m.apply_block) for m in nested] == [
        (m.name, m.rule, m.apply_block) for m in top
    ]
    assert nested[0].rule == _rule(1)
    assert nested[0].apply_block == _apply(1)  # the JSON string is decoded
    assert nested[0].kind == "feedback"


def test_nested_only_one_field_each():
    assert hook.read_rule_apply(_meta("metadata:", "  rule: R nested")) == ("R nested", "")
    assert hook.read_rule_apply(_meta("metadata:", '  apply: "A nested"')) == ("", "A nested")


# ── 2. top level wins ───────────────────────────────────────────────────────


def test_top_level_wins_when_both_places_hold_a_value():
    meta = _meta(
        "metadata:", "  rule: R nested", '  apply: "A nested"', "rule: R top", 'apply: "A top"'
    )
    assert hook.read_rule_apply(meta) == ("R top", "A top")
    meta = _meta(
        "rule: R top", 'apply: "A top"', "metadata:", "  rule: R nested", '  apply: "A nested"'
    )
    assert hook.read_rule_apply(meta) == ("R top", "A top")


def test_each_field_is_chosen_on_its_own():
    meta = _meta("rule: R top", "metadata:", "  rule: R nested", '  apply: "A nested"')
    assert hook.read_rule_apply(meta) == ("R top", "A nested")
    meta = _meta('apply: "A top"', "metadata:", "  rule: R nested", '  apply: "A nested"')
    assert hook.read_rule_apply(meta) == ("R nested", "A top")


def test_an_empty_top_level_value_does_not_hide_the_nested_value():
    meta = _meta('rule: ""', "apply: '  '", "metadata:", "  rule: R nested", '  apply: "A nested"')
    assert hook.read_rule_apply(meta) == ("R nested", "A nested")
    meta = _meta("metadata:", "  rule: R nested", '  apply: "A nested"', "rule:", "apply:")
    assert hook.read_rule_apply(meta) == ("R nested", "A nested")


def test_an_empty_nested_value_does_not_hide_the_top_level_value():
    meta = _meta("rule: R top", 'apply: "A top"', "metadata:", '  rule: ""', "  apply: '  '")
    assert hook.read_rule_apply(meta) == ("R top", "A top")


def test_top_level_only_reads_as_before():
    assert hook.read_rule_apply(_meta("rule:  R top ", 'apply: "A\\ntop"')) == ("R top", "A\ntop")
    assert hook.read_rule_apply(_meta("name: x")) == ("", "")
    # a top-level value of spaces only, with nothing nested, stays as it was read
    assert hook.read_rule_apply({"rule": "  ", "apply": "  "}) == ("", "  ")
    assert hook.read_rule_apply(
        {"rule": "  ", "apply": "  ", "metadata": {"type": "feedback"}}
    ) == ("", "  ")
    assert hook.read_rule_apply(
        {"rule": "", "apply": "  ", "metadata": {"rule": "", "apply": ""}}
    ) == ("", "  ")


# ── 3. only ``metadata:`` is read ───────────────────────────────────────────


def test_a_field_under_another_key_is_not_read():
    meta = _meta("other:", "  rule: R other", '  apply: "A other"', "metadata:", "  type: feedback")
    assert hook.read_rule_apply(meta) == ("", "")


def test_an_indented_line_after_a_scalar_key_is_not_read():
    meta = _meta(
        "metadata:", "  type: feedback", "name: x", "  rule: R stray", '  apply: "A stray"'
    )
    assert hook.read_rule_apply(meta) == ("", "")


# ── 4. a broken front matter ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"metadata": "a scalar"},
        {"metadata": None},
        {"metadata": ["rule", "apply"]},
        {"metadata": {"rule": 7, "apply": ["x"]}},
        {"metadata": {"rule": {}, "apply": None}},
        {"rule": {}, "apply": {}, "metadata": {}},
        {"rule": 3, "apply": None},
    ],
)
def test_a_strange_front_matter_reads_as_no_fields(meta):
    assert hook.read_rule_apply(meta) == ("", "")


def test_a_nested_apply_that_is_not_json_degrades_to_text():
    assert hook.read_rule_apply(_meta("metadata:", "  rule: R", "  apply: not \\q json")) == (
        "R",
        "not \\q json",
    )


@pytest.mark.parametrize(
    "text",
    [
        '---\nname: feedback_m01\nmetadata:\n  rule: R never closed\n  apply: "A"\n\nbody',
        "---\nmetadata: scalar\n  rule: R stray\n---\nbody",
        "---\n  rule: R indented first\n: no key\nmetadata:\n\t\n  : x\n  - rule: item\n---\nbody",
        "---\nmetadata:\n---\nbody",
        "﻿---\nmetadata:\n  rule: R after a byte order mark\n---\nbody",
        "",
    ],
)
def test_a_broken_file_gives_no_rule_and_no_exception(tmp_path, text):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "feedback_m01.md").write_bytes(text.encode("utf-8") + b"\xff\xfe tail")
    (folder / "feedback_m02.md").write_text("\n".join(_front(2, "nested") + ["", "body", ""]))
    by_name = {m.rel_path: m for m in hook.load_memory_corpus(str(folder))}
    assert (by_name["feedback_m01.md"].rule, by_name["feedback_m01.md"].apply_block) == ("", "")
    assert by_name["feedback_m02.md"].rule == _rule(2)
    out = _serve(tmp_path, folder, "cache-broken")  # the hook still answers
    assert _rule(2) in out
    assert "R never closed" not in out and "R stray" not in out


# ── 5. no optional sibling is needed ────────────────────────────────────────


def test_the_hook_with_only_its_required_siblings_reads_nested_fields(tmp_path):
    """The field reader imports nothing optional: with no ``memory_fields.py``
    (and no other hook file) next to the hook and its two required siblings,
    the nested fields are read."""
    alone = tmp_path / "alone"
    alone.mkdir()
    required = ["corpus.py", "recall_hook.py", "store_client.py"]
    for name in required:
        shutil.copy(HOOKS / name, alone / name)
    assert sorted(p.name for p in alone.iterdir()) == required
    spec = importlib.util.spec_from_file_location("recall_hook_alone", alone / "recall_hook.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert str(alone) in str(mod._CORPUS.__file__)
    assert "memory_fields" not in mod._SIBLING_MODULES
    corpus = mod.load_memory_corpus(str(_corpus(tmp_path / "nested", "nested")))
    assert [(m.rule, m.apply_block) for m in corpus] == [
        (_rule(i), _apply(i)) for i in range(1, N + 1)
    ]
    top = mod.load_memory_corpus(str(_corpus(tmp_path / "top", "top")))
    assert [(m.rule, m.apply_block) for m in corpus] == [(m.rule, m.apply_block) for m in top]
