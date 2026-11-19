# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared command-line argument helpers."""

from __future__ import annotations


def join_root(argv):
    """``--root -slug`` as ``--root=-slug``. A root is a folder slug and starts
    with a dash, which argparse reads as a flag. A value that starts with
    ``--`` stays an option, so ``--root --json`` is still an error."""
    items = list(argv)
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        if item == "--root" and nxt and nxt.startswith("-") and not nxt.startswith("--"):
            out.append(f"--root={nxt}")
            i += 2
            continue
        out.append(item)
        i += 1
    return out
