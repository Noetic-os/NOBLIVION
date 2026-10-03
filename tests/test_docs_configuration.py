# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/configuration.md must name every NOBLIVION_* env var the code reads.

The scan covers the shipped code: ``src/``, ``hooks/``, ``mcp/``,
``scripts/`` and ``config/``. A name that ends in ``_`` is a prefix (for
example ``NOBLIVION_CONTINUITY_QUOTA_`` + a section name); the doc names the
full forms instead. The reverse check keeps the doc honest: a name in the
doc must exist in the code, or start with a prefix the code builds names
from.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "configuration.md"
CODE_DIRS = ("src", "hooks", "mcp", "scripts", "config")
CODE_SUFFIXES = {".py", ".sh", ".json", ".txt"}
NAME = re.compile(r"NOBLIVION_[A-Z0-9_]+")


def _code_names() -> set[str]:
    names: set[str] = set()
    for folder in CODE_DIRS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in CODE_SUFFIXES:
                names.update(NAME.findall(path.read_text(encoding="utf-8", errors="replace")))
    return names


def _doc_names() -> set[str]:
    return set(NAME.findall(DOC.read_text(encoding="utf-8")))


def test_scan_finds_names() -> None:
    names = _code_names()
    assert "NOBLIVION_DATA_DIR" in names
    assert len(names) > 50


def _missing(code: set[str], doc_text: str) -> list[str]:
    documented = set(NAME.findall(doc_text))
    return sorted(n for n in code if not n.endswith("_") and n not in documented)


def test_missing_reports_an_undocumented_name() -> None:
    code = {"NOBLIVION_A", "NOBLIVION_B", "NOBLIVION_PREFIX_"}
    assert _missing(code, "`NOBLIVION_A` only") == ["NOBLIVION_B"]
    assert _missing(code, "NOBLIVION_A and NOBLIVION_B") == []


def test_every_code_name_is_documented() -> None:
    missing = _missing(_code_names(), DOC.read_text(encoding="utf-8"))
    assert not missing, f"add these env vars to docs/configuration.md: {missing}"


def test_every_documented_name_exists_in_code() -> None:
    code = _code_names()
    prefixes = tuple(n for n in code if n.endswith("_"))
    stale = sorted(
        n
        for n in _doc_names()
        if not n.endswith("_") and n not in code and not n.startswith(prefixes)
    )
    assert not stale, f"docs/configuration.md names env vars the code does not read: {stale}"
