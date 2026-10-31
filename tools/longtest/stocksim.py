# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stock Claude Code memory, simulated with no model.

Stock loads the first 200 lines or 25,000 bytes of the ``MEMORY.md`` of the
session's own project, and CLAUDE.md. The model then opens files. Three levels say
how many it opens (all read only the session's own project folder):

* S0: the index only. The lower bound.
* S1: S0 plus up to 3 linked files whose index line shares a content word with the
  prompt.
* S2: S1 plus a word search of the folder; the 5 files that match the most distinct
  words open. The upper bound, generous to stock.
* S-cal: S0 + (S2 - S0) x p, where p is the share of recorded stock runs that opened
  the lesson note (8 of 32), with the Wilson interval carried through.

A note that the prompt names counts as read, so stock gets the same reach as the
task-file rule of NOBLIVION-158. The cost of stock is the index in tokens (bytes
divided by 4) on every call of the session, because the index rides on every
request.

The word rules are the ones of ``tools/eval/heldout.py`` (``StockFolder``), so the
two simulations cannot drift apart. This module returns the opened text, so a
delivery check is an exact match on the answer string.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import PARAMS
from .stats import wilson
from .util import load_tool


def heldout():
    return load_tool("eval/heldout.py", "longtest_heldout")


@dataclass
class Opened:
    index: str  # the loaded part of MEMORY.md
    linked: list[str]  # S1 opens
    searched: list[str]  # S2 opens, besides the linked ones
    named: list[str]  # opened because the prompt names them
    texts: dict[str, str]  # file name -> text, for every opened file

    def context(self, level: str) -> str:
        """The text that stock puts in front of the model at level S0, S1 or S2."""
        parts = [self.index]
        if level in ("S1", "S2"):
            parts += [self.texts[n] for n in self.named + self.linked if n in self.texts]
        if level == "S2":
            parts += [self.texts[n] for n in self.searched if n in self.texts]
        return "\n".join(parts)


class Stock:
    """Stock memory on one project's memory folder."""

    def __init__(self, folder: Path) -> None:
        self.h = heldout()
        self.folder = Path(folder)
        self.sim = self.h.StockFolder(self.folder)
        index_file = self.folder / "MEMORY.md"
        raw = (
            index_file.read_text(encoding="utf-8", errors="replace") if index_file.is_file() else ""
        )
        self.index = self.h.index_cut(raw)

    def index_tokens(self) -> int:
        return len(self.index.encode("utf-8")) // 4

    def open_files(self, prompt: str) -> Opened:
        h, sim = self.h, self.sim
        words = h.content_words(prompt)
        best: dict[str, int] = {}
        for name, line in sim.lines:
            shared = len(words & h.content_words(line))
            if shared and name in sim.texts:
                best[name] = max(best.get(name, 0), shared)
        linked = sorted(best, key=lambda n: (-best[n], n))[: h.LINKED_FILES]
        hits = {
            name: n for name, text in sim.texts.items() if (n := sum(1 for w in words if w in text))
        }
        found = sorted(hits, key=lambda n: (-hits[n], n))[: h.SEARCH_FILES]
        named = sorted(n for n in sim.texts if n.lower() in prompt.lower())
        searched = [n for n in found if n not in linked and n not in named]
        texts = {}
        for name in set(linked) | set(found) | set(named):
            texts[name] = (self.folder / name).read_text(encoding="utf-8", errors="replace")
        return Opened(self.index, [n for n in linked if n not in named], searched, named, texts)

    def delivered(self, prompt: str, needle: str) -> dict[str, bool]:
        """Does the context of each level hold ``needle`` (the answer string)?"""
        opened = self.open_files(prompt)
        return {level: needle in opened.context(level) for level in ("S0", "S1", "S2")}


def s_cal(s0: float, s2: float, hits: int | None = None, runs: int | None = None) -> dict:
    """S0 + (S2 - S0) x p with the Wilson interval of p carried through. The
    result is a rate; its interval is the interval of p scaled by (S2 - S0)."""
    hits = PARAMS.calibration_hits if hits is None else hits
    runs = PARAMS.calibration_runs if runs is None else runs
    low, high = wilson(hits, runs)
    gap = s2 - s0
    point = s0 + gap * hits / runs
    bounds = sorted((s0 + gap * low, s0 + gap * high))
    return {
        "p": round(hits / runs, 4),
        "rate": round(point, 4),
        "low": round(bounds[0], 4),
        "high": round(bounds[1], 4),
    }


def stock_cost(index_tokens: int, calls: int) -> int:
    """Re-sent tokens of stock in one session: the index on every call."""
    return index_tokens * calls


def project_folders(config_dir: Path) -> dict[str, Path]:
    """``slug -> memory folder`` of a config dir."""
    return {p.parent.name: p for p in sorted((Path(config_dir) / "projects").glob("*/memory"))}


def sanity(rows: Iterable[tuple[bool, bool, bool]], params=PARAMS) -> dict[str, float | bool]:
    """Pilot gate 3 for stock: S2 delivery on SMALL V1. ``rows`` hold (S0, S1, S2)."""
    rows = list(rows)
    n = len(rows) or 1
    s2 = sum(1 for r in rows if r[2]) / n
    return {"n": len(rows), "s2": round(s2, 3), "ok": s2 >= params.sanity_s2_delivery}


Check = Callable[[str, str], dict[str, bool]]
