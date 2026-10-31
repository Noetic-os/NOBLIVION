# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scale fixture of ``tools/eval/scale_fixture.py`` (NOBLIVION-146), loaded.

This module does not generate anything of its own: the generator, its frozen word
list and its 60 lessons are the P1-03 work. Here are the readers the harness
needs: which shard holds a lesson, which working folder a query runs in, and the
leakage check that the plan asks for before day 1.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .util import load_tool, sha256_file

WORK = "/work"  # the fixture's fixed working folder; no output path enters a note
ANSWER_FREE = ("V5", "V7")  # the notification repeats the V3 text; V5 names no answer


def generator():
    return load_tool("eval/scale_fixture.py", "longtest_scale_fixture")


def slug_of(cwd: str) -> str:
    """The project folder name Claude Code gives a working folder."""
    return cwd.replace("/", "-")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        rows = json.loads((self.root / "lessons.json").read_text(encoding="utf-8"))
        self.lessons = {row["id"]: row for row in rows}
        text = (self.root / "queries.jsonl").read_text(encoding="utf-8")
        self.queries = [json.loads(line) for line in text.splitlines() if line.strip()]
        self._scale: dict[int, dict[str, Any]] = {}

    @classmethod
    def ensure(cls, root: Path, seed: int, sizes: Iterable[int]) -> Fixture:
        """Open the fixture at ``root``, or generate it there. A folder that holds
        a fixture of another seed, or lacks a size, is refused: nothing is mixed."""
        root = Path(root)
        sizes = sorted(set(sizes))
        if (root / "manifest.json").is_file():
            fx = cls(root)
            have = {int(k) for k in fx.manifest["scales"]}
            if fx.manifest["seed"] != seed or not set(sizes) <= have:
                raise SystemExit(
                    f"{root}: fixture holds seed {fx.manifest['seed']}, sizes {sorted(have)}"
                )
            return fx
        generator().generate(root, seed, sizes)
        return cls(root)

    def sizes(self) -> list[int]:
        return sorted(int(k) for k in self.manifest["scales"])

    def scale(self, size: int) -> dict[str, Any]:
        if size not in self._scale:
            path = self.root / f"scale-{size:05d}" / "scale.json"
            self._scale[size] = json.loads(path.read_text(encoding="utf-8"))
        return self._scale[size]

    def shard_dir(self, size: int, shard: int) -> Path:
        return self.root / f"scale-{size:05d}" / f"shard-{shard:02d}"

    def shards(self, size: int) -> list[int]:
        return sorted({row["shard"] for row in self.scale(size)["lessons"].values()})

    def lesson_shard(self, size: int, lesson: str) -> int:
        return int(self.scale(size)["lessons"][lesson]["shard"])

    def lesson_of(self, query: dict[str, Any]) -> str:
        """The lesson that fixes the shard of a query: V5 names its near lesson."""
        return query.get("lesson") or query.get("near_lesson") or ""

    def cwd_of(self, size: int, query: dict[str, Any]) -> str:
        """The working folder of a query: the project of the session, or for V4 the
        other project. SMALL has one project, so its other folder has no notes."""
        lesson = self.lesson_of(query)
        row = self.scale(size)["lessons"][lesson]
        name = row["session_project"] if query.get("cwd") != "other" else row["other_project"]
        return f"{WORK}/{name}"

    def prompt_of(self, query: dict[str, Any]) -> str:
        return str(query.get("prompt", "")).replace("{FIXTURE_ROOT}", str(self.root))

    def queries_for(
        self, size: int, variants: Iterable[str] | None = None, shard: int | None = None
    ) -> Iterator[dict[str, Any]]:
        wanted = set(variants) if variants else None
        for query in self.queries:
            if wanted and query["variant"] not in wanted:
                continue
            if shard is not None and self.lesson_shard(size, self.lesson_of(query)) != shard:
                continue
            yield query

    def memory_folders(self, size: int, shard: int) -> list[Path]:
        return sorted((self.shard_dir(size, shard) / "projects").glob("*/memory"))

    def distractors(self, size: int, shard: int, count: int) -> list[Path]:
        """Filler notes of one shard, in a stable order, to add to a real corpus
        (arm b). A planted note is never taken."""
        planted = {n for row in self.lessons.values() for n in self._planted_names(row)}
        picked: list[Path] = []
        for folder in self.memory_folders(size, shard):
            for path in sorted(folder.glob("*.md")):
                if path.name.startswith(("MEMORY", "topic_")) or path.name in planted:
                    continue
                picked.append(path)
                if len(picked) >= count:
                    return picked
        return picked

    @staticmethod
    def _planted_names(row: dict[str, Any]) -> list[str]:
        return [row["target_note"], row["duplicate_note"], *row["near_miss_notes"]]

    def fingerprint(self) -> dict[str, str]:
        """Hashes that go into the run manifest."""
        keys = ("generator_sha256", "words_sha256", "lessons_sha256", "queries_sha256")
        doc = {k: self.manifest[k] for k in keys}
        doc["manifest_sha256"] = sha256_file(self.root / "manifest.json")
        return doc


def copy_shard(fx: Fixture, size: int, shard: int, config_dir: Path) -> list[Path]:
    """The notes of one shard as the ``projects`` folder of a harness config dir."""
    target = config_dir / "projects"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fx.shard_dir(size, shard) / "projects", target, dirs_exist_ok=True)
    return sorted(target.glob("*/memory"))


def leakage(fx: Fixture) -> dict[str, Any]:
    """The pre-day-1 check of the plan: no query holds an answer string, and a V2
    query shares no content-word stem with the note it is aimed at."""
    gen = generator()
    leaked: list[str] = []
    for query in fx.queries:
        text = " ".join(str(query.get(k, "")) for k in ("prompt", "command", "error"))
        for step in query.get("steps", []) or []:
            text += " " + str(step.get("prompt", ""))
        tool_input = query.get("tool_input") or {}
        text += " " + json.dumps(tool_input)
        for row in fx.lessons.values():
            if row["answer"] in text:
                leaked.append(query["id"])
                break
    overlap: list[str] = []
    for query in fx.queries:
        if query["variant"] != "V2":
            continue
        row = fx.lessons[query["lesson"]]
        note = None
        for size in fx.sizes():
            for folder in fx.memory_folders(size, fx.lesson_shard(size, row["id"])):
                if (folder / row["target_note"]).is_file():
                    note = folder / row["target_note"]
                    break
            if note:
                break
        if note is None:
            continue
        shared = gen.shared_stems(
            gen.content_stems(query["prompt"]),
            gen.content_stems(gen.note_content(note.name, note.read_text(encoding="utf-8"))),
        )
        if shared:
            overlap.append(query["id"])
    return {"answer_in_query": leaked, "v2_overlap": overlap, "ok": not (leaked or overlap)}
