# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm b: real prompts through the hooks of both builds, on a snapshot of the real corpus.

The memory folders of the install are copied (read only) into a harness; filler notes of
the scale fixture are added at 0, 1,000, 5,000 and 20,000. The prompts are the dev split
of the hand-labelled real set, read through its loader; the sealed split is never read.
A row holds counts and salted hashes. The text of a row line goes to ``private/`` only,
for the labels the plan asks for in the pilot (gate 7).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .labels import heldout, pool_additions, write_pool_export
from .util import write_private

LEVELS = (0, 1000, 5000, 20000)
DISTRACTOR_PROJECT = "-work-distractors"
SKIP = ("credential", "secret", "token", ".env")


def real_memory_folders(config_dir: Path) -> list[Path]:
    return sorted(Path(config_dir).glob("projects/*/memory"))


def copy_real_corpus(config_dir: Path, target_config: Path) -> dict[str, int]:
    """Copy the ``*.md`` notes of every project memory folder. Nothing in the source
    is written; a file whose name looks like a credential is left out."""
    counts: dict[str, int] = {}
    for folder in real_memory_folders(config_dir):
        dest = target_config / "projects" / folder.parent.name / "memory"
        dest.mkdir(parents=True, exist_ok=True)
        for path in sorted(folder.glob("*.md")):
            if path.is_symlink() or any(w in path.name.lower() for w in SKIP):
                continue
            shutil.copyfile(path, dest / path.name)
            counts[folder.parent.name] = counts.get(folder.parent.name, 0) + 1
    return counts


def add_distractors(paths: Iterable[Path], target_config: Path, project: str) -> int:
    """Filler notes go into the project folder of the session: a prompt is ranked against
    its own project and the shared roots, so fillers in another project would change
    nothing (the first pilot showed levels 0 and 1,000 with identical rows)."""
    stale = target_config / "projects" / DISTRACTOR_PROJECT
    if stale.is_dir():
        shutil.rmtree(stale)
    dest = target_config / "projects" / project / "memory"
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in paths:
        shutil.copyfile(path, dest / path.name)
        n += 1
    return n


def session_project(counts: Mapping[str, int]) -> str:
    return max(counts, key=lambda k: (counts[k], k)) if counts else "-work-none"


def session_cwd(counts: Mapping[str, int]) -> str:
    """The folder the prompts run in: the project with the most notes. A project folder
    name is the path with ``/`` written as ``-``, so the path comes back from the name."""
    return session_project(counts).replace("-", "/")


def dev_prompts(directory: Path) -> list[dict[str, Any]]:
    """The prompt groups of the dev split: id, class and the pooled row lines."""
    groups, key = heldout().load_split(Path(directory), "dev")
    return [
        {
            "group": g["group"],
            "text": g["text"],
            "stratum": key[g["group"]].get("stratum", ""),
            "lines": [it["line"] for it in g["items"]],
        }
        for g in groups
        if g.get("cls") == "prompt"
    ]


def row_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("- ")]


def digest(salt: str, text: str) -> str:
    return hashlib.sha256((salt + text).encode()).hexdigest()[:12]


def replay_prompts(
    call: Any, prompts: Sequence[Mapping[str, Any]], cwd: str, ask: Any, salt: str, tag: str
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """``call(sid, cwd, prompt)`` returns the hook answer ``{"out", "ms", "ok"}``.
    Returns the count rows and, per group, the row lines (for ``private/`` only)."""
    rows, shown = [], {}
    for i, p in enumerate(prompts):
        r = call(f"real-{tag}-{i}", cwd, p["text"])
        text = ask("UserPromptSubmit", r["out"]) if r["ok"] else ""
        lines = row_lines(text)
        shown[p["group"]] = lines
        rows.append(
            {
                "group": p["group"],
                "stratum": p["stratum"],
                "rows": len(lines),
                "chars": len(text),
                "ms": r["ms"],
                "ok": r["ok"],
                "row_hashes": [digest(salt, ln) for ln in lines],
            }
        )
    return rows, shown


def export_pool_additions(
    prompts: Sequence[Mapping[str, Any]], shown: Mapping[str, Sequence[str]], path: Path
) -> int:
    """Row lines that the new build shows and that the pool of the real set does not
    hold, as pairs for blind labelling. The file is private."""
    groups = [{"group": p["group"], "items": [{"note": ln} for ln in p["lines"]]} for p in prompts]
    pairs = pool_additions(groups, {}, shown, {})
    write_pool_export(pairs, path)
    return len(pairs)


def summary(rows_by_build: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Rows and characters per prompt, and the paired character ratio B1 over B0."""
    out: dict[str, Any] = {}
    for build, rows in rows_by_build.items():
        chars = sorted(r["chars"] for r in rows)
        out[build] = {
            "n": len(rows),
            "rows_mean": round(sum(r["rows"] for r in rows) / max(1, len(rows)), 2),
            "chars_p50": chars[len(chars) // 2] if chars else 0,
            "chars_total": sum(chars),
            "with_rows": sum(1 for r in rows if r["rows"]),
        }
    b0, b1 = out.get("B0"), out.get("B1")
    if b0 and b1 and b0["chars_total"]:
        out["char_ratio_b1_over_b0"] = round(b1["chars_total"] / b0["chars_total"], 3)
    return out


def dump(path: Path, doc: Any) -> None:
    write_private(Path(path), json.dumps(doc, sort_keys=True) + "\n")
