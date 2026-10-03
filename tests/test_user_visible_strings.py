# SPDX-License-Identifier: AGPL-3.0-or-later
"""No user-visible string carries the wording of the private reference project.

The scan reads every string literal in ``hooks/``, ``src/noblivion/`` and
``mcp/`` (messages, log lines, briefings, answers), plus the shipped text and
JSON files. Docstrings and comments are not user-visible and are not scanned.
A regular expression that only detects text (the first argument of a
``re.compile``) is not output either, so it is skipped. The few literals that
must name a word on purpose are in ``ALLOWED``, each with its reason.

This is not the forbidden-names gate (``tools/check_forbidden_names.py``):
these words are common English and may appear in comments and patterns.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("hooks", "src/noblivion", "mcp")
TEXT_FILES = ("src/noblivion/*.txt", "src/noblivion/*.json", "config/*.json", "hooks/*.json")
WORDS = re.compile(
    r"\boperator|\bruling|\bdaemon|\bestate\b|\bfleet\b|stop for nothing", re.IGNORECASE
)

# (file, substring of the literal): why the literal may name the word.
ALLOWED = (
    # The injection redactor must match forged approval claims by their words.
    ("src/noblivion/injection.py", "operator"),
    # Docker's own error text, matched in tool output.
    ("hooks/error_recall_hook.py", "Error response from daemon"),
    ("hooks/error_recall_hook.py", "daemon container"),
)


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _patterns(tree: ast.AST) -> set[int]:
    """String literals that are the pattern argument of a ``re`` call."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            out.add(id(node.args[0]))
    return out


def hits_in_source(rel: str, text: str) -> list[str]:
    tree = ast.parse(text)
    skip = _docstrings(tree) | _patterns(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip or not WORDS.search(node.value):
            continue
        if any(rel == f and sub in node.value for f, sub in ALLOWED):
            continue
        found.append(f"{rel}:{node.lineno}: {node.value[:80]!r}")
    return found


def _sources() -> list[Path]:
    return sorted(p for d in CODE_DIRS for p in (ROOT / d).glob("*.py"))


def test_the_scan_sees_the_code():
    assert len(_sources()) > 30


def test_the_scan_finds_a_planted_word_and_skips_docstrings_and_patterns():
    src = (
        '"""The operator docstring."""\n'
        "import re\n"
        'P = re.compile(r"ruling|approved")\n'
        'MSG = f"The operator corrected you {x}"\n'
    )
    assert hits_in_source("x.py", src) == ["x.py:4: 'The operator corrected you '"]


def test_no_user_visible_string_names_the_reference_project():
    found: list[str] = []
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        found += hits_in_source(rel, path.read_text(encoding="utf-8"))
    assert not found, "reword these user-visible strings:\n" + "\n".join(found)


@pytest.mark.parametrize("pattern", TEXT_FILES)
def test_no_shipped_text_file_names_the_reference_project(pattern):
    for path in sorted(ROOT.glob(pattern)):
        text = path.read_text(encoding="utf-8")
        assert not WORDS.search(text), f"{path.relative_to(ROOT)}: {WORDS.search(text)}"
