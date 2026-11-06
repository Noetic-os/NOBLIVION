# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily growth of a kept replay store (the large-project profile).

Before every night about 500 filler notes go into the memory folders of the kept
B1 store of each scale, spread over its projects as the fixture spreads notes
(the largest project grows the most). A note is linked from ``MEMORY.md``, from a
topic file of the day, or not at all, in the shares of the fixture survey. The
notes come from the filler generator of the fixture with their own seed key, and
none holds an answer string of a lesson. The store embeds them at its next start;
the stock simulation reads the same grown folders.

``out/growth.jsonl`` holds one row per (size, day), so a restart never adds the
notes of a day twice.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import fixtures
from .runner import Run, shard_root
from .stocksim import project_folders
from .util import read_jsonl

LINK_SHARES = {"index": 0.25, "topic": 0.58, "unlinked": 0.17}  # the survey link shares


def done(run: Run) -> set[tuple[int, int]]:
    return {(int(r["size"]), int(r["day"])) for r in read_jsonl(run.out / "growth.jsonl")}


def kept_config_dir(run: Run, size: int, shard: int) -> Path:
    """The Claude Code config dir of the kept B1 store at ``size``: its ``projects``
    folders hold the notes the store indexes."""
    return shard_root(run, "B1", size, shard) / "home" / ".claude"


def stock_dir(run: Run, size: int, shard: int) -> Path:
    """Where the stock simulation reads: the kept (grown) store when it exists, else
    the fixture shard. Both arms then see the same notes."""
    config_dir = kept_config_dir(run, size, shard)
    if (config_dir / "projects").is_dir():
        return config_dir
    return run.fixture().shard_dir(size, shard)


def _append(path: Path, text: str, once: str = "") -> None:
    """Append ``text``; with ``once`` (a file name), skip when the file already links
    it, so a growth step that is run again after a kill adds no second link."""
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if once and f"({once})" in old:
        return
    if old and not old.endswith("\n"):
        old += "\n"
    path.write_text(old + text, encoding="utf-8")


def grow(
    run: Run, size: int, day: int, count: int, now: dt.datetime | None = None
) -> dict[str, Any]:
    """Add ``count`` filler notes to the kept B1 store at ``size`` for ``day``. Returns
    the row written to ``out/growth.jsonl``; a day already grown is skipped."""
    fx = run.fixture()
    shard = fx.shards(size)[0]
    config_dir = kept_config_dir(run, size, shard)
    folders = project_folders(config_dir)
    if not folders:
        raise RuntimeError(f"no kept B1 store at {size} notes under {config_dir}: nothing to grow")
    if (size, day) in done(run):
        return {"size": size, "day": day, "shard": shard, "added": 0, "skipped": True}
    gen = fixtures.generator()
    words = gen.Words(json.loads(gen.WORDS_FILE.read_bytes()))
    answers = [str(row["answer"]) for row in fx.lessons.values() if row.get("answer")]
    seed = int(run.manifest["seed"])
    rng = gen.Rng(seed, "growth", size, day)
    slugs = sorted(folders)
    quota = gen.largest_remainder(count, {s: 1.0 / (rank + 1) for rank, s in enumerate(slugs)})
    specs = gen.filler_specs(rng, count, [])
    by_slot = {"index": 0, "topic": 0, "unlinked": 0}
    serial = 0
    for slug in slugs:
        folder = folders[slug]
        project = slug.replace("-work-", "", 1)
        topic_file = f"topic_growth_d{day:02d}.md"
        for _ in range(int(quota.get(slug, 0))):
            spec = specs[serial % len(specs)] if specs else {"type": "other"}
            note = None
            for attempt in range(20):
                candidate = gen.filler_note(
                    words,
                    gen.Rng(seed, "growth", size, day, serial, attempt),
                    spec,
                    project,
                    serial,
                )
                if not any(a in candidate.text for a in answers):
                    note = candidate
                    break
            if note is None:
                raise RuntimeError(f"growth note {serial} at {size} holds an answer string")
            note = replace(note, file=f"growth-d{day:02d}-{serial:05d}-{note.file}")
            (folder / note.file).write_text(note.text, encoding="utf-8")
            slot = rng.weighted(LINK_SHARES)
            link = f"- [{note.title}]({note.file})\n"
            if slot == "index":
                _append(folder / "MEMORY.md", link, once=note.file)
            elif slot == "topic":
                if not (folder / topic_file).is_file():
                    _append(folder / topic_file, f"# Growth day {day}\n\n")
                _append(
                    folder / "MEMORY.md",
                    f"- [Topic: Growth day {day}]({topic_file})\n",
                    once=topic_file,
                )
                _append(folder / topic_file, link, once=note.file)
            by_slot[slot] += 1
            serial += 1
    stamp = (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    on_disk = sum(
        1
        for f in folders.values()
        for p in f.glob("*.md")
        if not p.name.startswith(("MEMORY", "topic_"))
    )
    row = {
        "ts": stamp,
        "day": day,
        "size": size,
        "shard": shard,
        "added": serial,
        "by_slot": by_slot,
        "projects": len(slugs),
        "notes_on_disk": on_disk,
    }
    run.append("growth", row)
    return row
