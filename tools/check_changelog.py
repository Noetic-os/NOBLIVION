# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a change to the shipped code has no CHANGELOG entry, or when a
tracked Markdown file still holds the ``<owner>`` placeholder.

1. With ``--base REF``: when a file under ``src/``, ``hooks/``, ``mcp/`` or
   ``codex/`` changed between REF and HEAD, ``CHANGELOG.md`` must change too.
   A commit in the range with the line ``Changelog: none`` waives this, for
   a change that a user cannot see.
2. Always: no tracked ``*.md`` file holds ``<owner>``. CHANGELOG.md is not
   checked, because a past entry may quote the placeholder.

Usage:
    python tools/check_changelog.py                     # check 2 only
    python tools/check_changelog.py --base origin/main  # checks 1 and 2

Exit status: 0 when both checks pass, 1 when one fails.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CHANGELOG = "CHANGELOG.md"
PRODUCT_DIRS = ("src/", "hooks/", "mcp/", "codex/")
PLACEHOLDER = "<owner>"
WAIVER = re.compile(r"^Changelog: none\s*$", re.M)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changelog_problem(base: str) -> str | None:
    """The reason the range ``base...HEAD`` needs a CHANGELOG entry, or None."""
    changed = _git("diff", "--name-only", f"{base}...HEAD").split()
    product = [p for p in changed if p.startswith(PRODUCT_DIRS)]
    if not product or CHANGELOG in changed:
        return None
    if WAIVER.search(_git("log", "--format=%B", f"{base}..HEAD")):
        return None
    shown = ", ".join(product[:5]) + (" ..." if len(product) > 5 else "")
    return (
        f"{shown} changed, but {CHANGELOG} did not. Add an entry under"
        " '## Unreleased', or add the line 'Changelog: none' to a commit"
        " message when a user cannot see the change."
    )


def placeholder_hits() -> list[str]:
    """``path:line`` of every ``<owner>`` in a tracked Markdown file."""
    hits = []
    for name in _git("ls-files", "*.md").split():
        if name == CHANGELOG:
            continue
        try:
            text = Path(name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if PLACEHOLDER in line:
                hits.append(f"{name}:{n}")
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="compare HEAD with this ref (a pull request base)")
    args = parser.parse_args(argv)
    failed = False
    if args.base:
        problem = changelog_problem(args.base)
        if problem:
            print(f"check_changelog: {problem}")
            failed = True
    hits = placeholder_hits()
    if hits:
        print(f"check_changelog: {PLACEHOLDER} placeholder in {len(hits)} place(s):")
        for hit in hits:
            print(f"  {hit}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
