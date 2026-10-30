# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scale fixture through the store's own parser and redactor (NOBLIVION-146).

Delivery is scored by an exact match on the answer string. The store redacts
text at rest, so an answer that looks like a secret would never be delivered,
and every lesson would count as a miss for the wrong reason.

This file needs the package, so it is apart from ``test_scale_fixture.py``, which
also runs on the 3.9 lane.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from noblivion import indexer, redaction

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "eval" / "scale_fixture.py"

_spec = importlib.util.spec_from_file_location("scale_fixture_store_t", TOOL)
assert _spec is not None and _spec.loader is not None
sf = importlib.util.module_from_spec(_spec)
sys.modules["scale_fixture_store_t"] = sf
_spec.loader.exec_module(sf)


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("fixture") / "out"
    sf.generate(folder, sf.DEFAULT_SEED, [1000])
    return folder


@pytest.fixture(scope="module")
def memory(out) -> list[Path]:
    return sorted((out / "scale-01000" / "shard-00" / "projects").glob("*/memory"))


@pytest.fixture(scope="module")
def lessons(out) -> list[dict]:
    return json.loads((out / "lessons.json").read_text(encoding="utf-8"))


def test_no_note_is_changed_by_the_redactor(memory):
    checked = 0
    for folder in memory:
        for path in folder.glob("*.md"):
            data = path.read_bytes()
            assert not redaction.holds_secret_kind(data), path.name
            assert not redaction.would_change(data.decode()), path.name
            checked += 1
    assert checked >= 1000


def test_the_parser_reads_each_target(memory, lessons):
    for row in lessons:
        name = row["target_note"]
        path = next(f / name for f in memory if (f / name).exists())
        parsed = indexer.parse_file(name, path.read_text(encoding="utf-8"))
        assert parsed.name == row["title"]
        assert parsed.active
        assert parsed.category in ("feedback", "project")
        assert row["answer"] in parsed.body or row["answer"] in parsed.description


def test_rule_text_reaches_the_keyword_index(memory, lessons):
    for row in lessons:
        name = row["target_note"]
        path = next(f / name for f in memory if (f / name).exists())
        parsed = indexer.parse_file(name, path.read_text(encoding="utf-8"))
        if "rule: " in path.read_text(encoding="utf-8").split("\n---\n", 1)[0]:
            assert row["answer"] in parsed.index_text


def test_the_triggers_parse_as_a_list(memory):
    folder = memory[0]
    with_triggers = 0
    for path in sorted(folder.glob("feedback_*.md"))[:60]:
        text = path.read_text(encoding="utf-8")
        if "\ntriggers: " in text:
            with_triggers += 1
            assert len(indexer.trigger_items(text)) >= 3
    assert with_triggers
