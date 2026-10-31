# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hand-labelled real set (NOBLIVION-144), through its loader only.

The set lives outside the repository. This module reads it with
``heldout.load_split`` and ``heldout.load_labels`` and with nothing else: it never
opens the key files, the group file or the manifest by name. The default split is
``dev``. The sealed split opens once, on day 7, through ``open_sealed_once``, which
logs the opening; it refuses a second call.

What is built here:

* ``dev_baseline``: delivery, precision and false delivery of the arms in the pool,
  on the dev split, as counts and shares.
* ``pool_additions``: the (prompt, note) pairs that the B1 arm adds to the pool and
  that have no label yet. They are exported for blind labelling by subagents.
* ``merge_labels``: new labels, y or n per item id, merged without overwriting.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .util import append_private, load_tool, write_private


def heldout():
    return load_tool("eval/heldout.py", "longtest_heldout")


def dev_baseline(directory: Path) -> dict[str, Any]:
    """Arm metrics of the dev split. Needs a label for every dev item."""
    h = heldout()
    groups, key = h.load_split(Path(directory), "dev")
    labels = h.load_labels(Path(directory))
    missing = sum(1 for g in groups for it in g["items"] if it["id"] not in labels)
    if missing:
        raise SystemExit(f"{missing} dev items have no label")
    doc = {
        "split": "dev",
        "prompts": len(groups),
        "arms": {arm: h.arm_metrics(groups, key, labels, arm) for arm in h.ARMS},
    }
    return doc


def pool_additions(
    groups: Sequence[Mapping[str, Any]],
    pooled_notes: Mapping[str, Sequence[str]],
    new_arm: Mapping[str, Sequence[str]],
    labels: Mapping[str, str],
) -> list[dict[str, str]]:
    """Notes that the new arm shows for a prompt and that the pool does not hold.
    ``pooled_notes`` maps a group to the notes in its pool; ``new_arm`` maps a group
    to the notes the new arm shows. Returns ``{"group", "note"}`` pairs, without text."""
    out = []
    for g in groups:
        known = set(pooled_notes.get(g["group"], ())) | {it["note"] for it in g["items"]}
        for note in new_arm.get(g["group"], ()):
            if note not in known:
                out.append({"group": g["group"], "note": note})
    return out


def merge_labels(existing: Mapping[str, str], new: Mapping[str, str]) -> dict[str, str]:
    """New labels only; a label already set is never overwritten."""
    bad = {k: v for k, v in new.items() if v not in ("y", "n")}
    if bad:
        raise ValueError(f"labels must be y or n: {len(bad)} are not")
    merged = dict(existing)
    for k, v in new.items():
        merged.setdefault(k, v)
    return merged


def write_pool_export(pairs: Sequence[Mapping[str, str]], path: Path) -> None:
    write_private(Path(path), "".join(json.dumps(p, sort_keys=True) + "\n" for p in pairs))


def open_sealed_once(directory: Path, run_dir: Path, day: int) -> tuple[list, dict]:
    """The sealed split, on day 7 and once. The opening is written to the run folder
    and to the set's own log, so a second call is refused."""
    marker = Path(run_dir) / "sealed-opened.json"
    if day < 7:
        raise SystemExit("the sealed split opens on day 7")
    if marker.exists():
        raise SystemExit("the sealed split was opened already")
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    write_private(marker, json.dumps({"opened": stamp}) + "\n")
    append_private(Path(directory) / "sealed-opened.jsonl", json.dumps({"opened": stamp}))
    return heldout().load_split(Path(directory), "sealed", open_sealed=True)
