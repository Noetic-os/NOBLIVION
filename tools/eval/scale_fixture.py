# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build synthetic memory folders at five scales, with planted lessons (NOBLIVION-146).

The long-run test and the store work need notes at sizes this host does not
have. This tool writes them from a seed and two frozen files next to it:
``scale_fixture_words.json`` (generic developer terms and sentence templates)
and ``scale_fixture_lessons.json`` (60 lessons, hand-written once, with their
queries). No real note text goes in, so the output is safe to share. Same seed,
same bytes.

Scales (the ``scale-NNNNN`` folders):

    SMALL     30 notes   1 project    the index links every note
    MEDIUM-1  200        3            one index of 199 lines, then overflow lines
    MEDIUM-2  1,000      10           topic files; three indexes pass 25 KB before line 200
    LARGE-1   5,000      25           Zipf project sizes
    LARGE-2   20,000     50           Zipf project sizes

Lessons and shards. The 60 lessons are the same at every scale, byte for byte,
and each has 3 near-miss notes and 1 duplicate, so they are 300 notes. That
does not fit 30 or 200 notes, and adding them on top would break the index
shape that those scales exist to test. So SMALL has 20 shards of 3 lessons and
MEDIUM-1 has 3 shards of 20 lessons. Every shard is a complete set of exactly N
notes, and ``scale.json`` names the shard of each lesson. From 1,000 notes up
there is one shard.

Placement. Stock Claude Code loads the first 200 lines or 25,000 bytes of the
``MEMORY.md`` of the session's own project. A lesson sits where that load misses
it: behind a topic file, unlinked, past line 200, past 25 KB, or in another
project. SMALL is the negative control, so its lessons are linked in the index.
The answer string is never in an index or a topic file. ``--verify`` proves the
placement of every lesson from the files on disk.

Field mix and sizes copy the corpus survey of 2026-10-30 (``SURVEY``), measured
over 1,189 real notes in 4 folders. The planted notes count toward the mix, so
the filler makes up the difference. ``scale.json`` records the mix it reached.

Output, in a new or empty folder outside the repository:

    manifest.json           seed, hashes of the inputs and of every scale, the mix
    lessons.json            the 60 lessons: answer, target note, query ids
    queries.jsonl           the frozen queries, the same at every scale
    tasks/Lnn.md            the task files that the V6 queries name
    scale-NNNNN/scale.json  placement of each lesson, projects, index shapes, mix
    scale-NNNNN/files.sha256
    scale-NNNNN/shard-NN/projects/<slug>/memory/*.md

``shard-NN`` works as ``CLAUDE_CONFIG_DIR``: its ``projects/*/memory`` folders
are the layout that the store scans. A project's folder is named for the fixed
working folder ``/work/<name>``, so no output path enters a file. A V6 prompt
holds the token ``{FIXTURE_ROOT}``: replace it with the output folder. A query
runs in the shard that holds its lesson; a V5 query names its ``near_lesson``.
SMALL has one project, so its V4 folder is ``/work/outside``, which has no notes.

Query kinds: V1 lexical; V2 paraphrase; V3 task-shaped; V4 the V3 text from
another project's folder; V5 near-miss negatives; V6 a path to a task file that
holds the V3 text; V7 a ``<task-notification>`` prompt. Also CMD (6 spellings of
a guard hazard), CRED (commands that would print a canary secret), ERR (3
variants of a quoted error), SUB (a subagent task, in full and as metadata
only), CONT (a session script across a restore) and LABEL (a named file, and a
generic name as a negative).

The V2 check: no content-word stem of a V2 query may appear in the note it is
aimed at. That is stricter than the plan, which names only the file name, the
title and the rule, because the keyword score reads the whole note. The check
runs at generation and again in ``--verify``.

Usage:
    python tools/eval/scale_fixture.py --out DIR [--seed N] [--scales 30,200,1000,5000,20000]
    python tools/eval/scale_fixture.py --verify DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
WORDS_FILE = HERE / "scale_fixture_words.json"
LESSONS_FILE = HERE / "scale_fixture_lessons.json"

DEFAULT_SEED = 20261030
FORMAT_VERSION = 1

# The stock load limits of MEMORY.md.
LINE_CAP = 200
BYTE_CAP = 25_000
# 25 KB can also be read as 25,600 bytes, so a "past 25 KB" lesson clears both.
BYTE_MARGIN = 1_024
# The note fields as the hooks limit them (hooks/memory_fields.py).
RULE_MAX = 160
APPLY_MAX = 400

# The survey of 2026-10-30 over the real notes of 4 memory folders, as counts and
# shares only. No note text was kept. Note sizes are in characters of the whole file.
SURVEY = {
    "date": "2026-10-30",
    "notes": 1189,
    "roots": 4,
    "size_mean_chars": 2790,
    "size_quantiles": [
        [0.0, 495],
        [0.01, 884],
        [0.05, 1269],
        [0.1, 1452],
        [0.25, 1902],
        [0.5, 2518],
        [0.75, 3243],
        [0.9, 4365],
        [0.95, 5509],
        [0.99, 8557],
        [1.0, 12154],
    ],
    "type_shares": {
        "feedback": 0.6442,
        "project": 0.2666,
        "reference": 0.0816,
        "other": 0.0042,
        "user": 0.0034,
    },
    # Share of the notes of each type that hold a field. ``rule`` stands for
    # rule, apply and scope, which the real notes hold together.
    "field_shares_by_type": {
        "feedback": {
            "rule": 0.674,
            "triggers": 0.648,
            "violates": 0.046,
            "rewrite": 0.001,
            "status": 0.0,
        },
        "project": {
            "rule": 0.057,
            "triggers": 0.054,
            "violates": 0.0,
            "rewrite": 0.0,
            "status": 0.662,
        },
        "reference": {
            "rule": 0.402,
            "triggers": 0.0,
            "violates": 0.0,
            "rewrite": 0.0,
            "status": 0.402,
        },
        "other": {"rule": 0.0, "triggers": 0.0, "violates": 0.0, "rewrite": 0.0, "status": 0.0},
        "user": {"rule": 0.5, "triggers": 0.25, "violates": 0.0, "rewrite": 0.0, "status": 0.25},
    },
    "scope_shares": {"stop": 0.391, "tool": 0.294, "always": 0.167, "file": 0.144},
    "status_shares": {"open": 0.54, "closed": 0.284, "active": 0.168, "parked": 0.008},
    # Share of the notes whose body holds each shape.
    "body_shares": {"why": 0.479, "how": 0.578, "bullets": 0.408, "fence": 0.108, "heading": 0.086},
    "link_shares": {"memory_md": 0.25, "topic_file": 0.58, "unlinked": 0.17},
}
FIELDS = ("rule", "triggers", "violates", "rewrite", "status")


def overall_share(name: str) -> float:
    """The share of all notes that hold a field, from the shares per type."""
    by_type = SURVEY["field_shares_by_type"]
    return sum(SURVEY["type_shares"][t] * by_type[t][name] for t in SURVEY["type_shares"])


@dataclass(frozen=True)
class Scale:
    name: str
    notes: int
    projects: int
    lessons_per_shard: int
    placements: tuple[str, ...]

    @property
    def shards(self) -> int:
        return 60 // self.lessons_per_shard

    @property
    def folder(self) -> str:
        return f"scale-{self.notes:05d}"


_ALL_FIVE = ("behind_topic", "unlinked", "past_line_200", "past_25kb", "other_project")
SCALES = {
    30: Scale("SMALL", 30, 1, 3, ("linked",)),
    200: Scale("MEDIUM-1", 200, 3, 20, ("past_line_200", "unlinked", "other_project")),
    1000: Scale(
        "MEDIUM-2", 1000, 10, 60, ("behind_topic", "unlinked", "past_25kb", "other_project")
    ),
    5000: Scale("LARGE-1", 5000, 25, 60, _ALL_FIVE),
    20000: Scale("LARGE-2", 20000, 50, 60, _ALL_FIVE),
}
# The slot a target note and its duplicate take in the project that holds them.
TARGET_SLOT = {
    "linked": "index",
    "behind_topic": "topic",
    "unlinked": "unlinked",
    "past_line_200": "past_line",
    "past_25kb": "past_bytes",
    "other_project": "index",
}
# Share of a project's filler notes per slot, for each index shape.
SHAPE_SLOTS = {
    "flat_all": {"index": 1.0},
    "flat": {"index": 0.8, "unlinked": 0.2},
    "topic": {"index": 0.05, "topic": 0.8, "unlinked": 0.15},
    "line_cap": {"index": 0.3, "topic": 0.55, "unlinked": 0.15},
    "byte_cap": {"index": 0.3, "topic": 0.55, "unlinked": 0.15},
}
TOPIC_SHAPES = ("topic", "line_cap", "byte_cap")
TOPIC_MIN_NOTES = 60  # below this a project has a flat index
TOPIC_FILE_NOTES = 25
NEAR_MISS = (
    ("on the staging mirror", "staging_mirror"),
    ("for the read replica", "read_replica"),
    ("in the disaster region", "disaster_region"),
)
VARIANT_CLAIMS = {
    "V1": ["sanity"],
    "V2": ["C3"],
    "V3": ["C1", "C17"],
    "V4": ["C4"],
    "V5": ["C2"],
    "V6": ["C18"],
    "V7": ["C19"],
    "CMD": ["C5", "C6"],
    "CRED": ["C5b"],
    "ERR": ["C7"],
    "SUB": ["C8"],
    "CONT": ["C9"],
    "LABEL": ["C16"],
}
OUTSIDE_PROJECT = "outside"  # the V4 folder of SMALL, which has one project

# -- the random source ---------------------------------------------------------


class Rng:
    """A source seeded from a key. It uses only ``Random.random``, the one
    sequence that Python keeps the same across versions, so the bytes do not
    depend on the interpreter. Each note gets its own key, so the order of work
    changes nothing."""

    __slots__ = ("_next",)

    def __init__(self, *key: object) -> None:
        digest = hashlib.sha256(":".join(str(k) for k in key).encode()).digest()
        self._next = random.Random(int.from_bytes(digest, "big")).random

    def below(self, n: int) -> int:
        return int(self._next() * n)

    def chance(self, p: float) -> bool:
        return self._next() < p

    def pick(self, items):
        return items[int(self._next() * len(items))]

    def skewed(self, items):
        """A pick that favours the start of the list, so that some words are
        common and others rare, as in real text. A square, not ``**``: a
        multiplication has one exact result on every platform, ``pow`` does not."""
        point = self._next()
        return items[int(point * point * len(items))]

    def weighted(self, shares: dict):
        point = self._next() * sum(shares.values())
        for key, share in shares.items():
            point -= share
            if point < 0:
                return key
        return key

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]

    def note_size(self) -> int:
        point = self._next()
        table = SURVEY["size_quantiles"]
        for index in range(1, len(table)):
            low, low_size = table[index - 1]
            high, high_size = table[index]
            if point <= high:
                return int(low_size + (high_size - low_size) * (point - low) / (high - low))
        return table[-1][1]


def largest_remainder(total: int, weights: dict) -> dict:
    """``total`` split over the keys in proportion to the weights, as whole numbers."""
    weight_sum = sum(weights.values())
    if not weight_sum:
        weights, weight_sum = {k: 1 for k in weights}, len(weights)
    raw = {k: total * w / weight_sum for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda key: (out[key] - raw[key], str(key)))[
        : total - sum(out.values())
    ]:
        out[k] += 1
    return out


def zipf_sizes(total: int, projects: int) -> list[int]:
    """Project sizes that fall with rank (Zipf, exponent 1) and sum to ``total``."""
    weights = {rank: 1 / (rank + 1) for rank in range(projects)}
    sizes = largest_remainder(total, weights)
    return [sizes[rank] for rank in range(projects)]


def project_sizes(scale: Scale) -> list[int]:
    if scale.projects == 1:
        return [scale.notes]
    if scale.notes == 200:  # one project must carry an index past the line cap
        return [140, 36, 24]
    return zipf_sizes(scale.notes, scale.projects)


def project_shape(scale: Scale, rank: int, size: int) -> str:
    if scale.projects == 1:
        return "flat_all"
    if scale.notes == 200:
        return "line_cap" if rank == 0 else "flat"
    if scale.notes == 1000 and rank < 3:
        return "byte_cap"
    if scale.notes > 1000 and rank == 0:
        return "line_cap"
    if scale.notes > 1000 and rank < 3:
        return "byte_cap"
    return "topic" if size >= TOPIC_MIN_NOTES else "flat"


# -- content words -------------------------------------------------------------

# Close to the store's list (src/noblivion/bm25.py): only words that cannot name a
# task. A short list makes the V2 check stricter.
STOP_WORDS = frozenset(
    """
    about above after again am an and are as at be been before being below between
    both but by can could did do does doing don during each for from further had has
    have having he her here hers herself him himself his how if in into is it its
    itself just me more most my myself no nor not of on once only or other our ours
    ourselves own same she should so some such than that the their theirs them
    themselves then there these they this those through to too until very was we
    were what when where which while who whom why will with would you your yours
    yourself yourselves
    """.split()
)
_WORD_RE = re.compile(r"[a-z]+")
_SUFFIXES = (
    "izations",
    "ization",
    "ations",
    "ation",
    "ingly",
    "ments",
    "ment",
    "ness",
    "ings",
    "ing",
    "edly",
    "ions",
    "ion",
    "ers",
    "er",
    "ed",
    "es",
    "ly",
    "s",
)
_FRONT_KEY_RE = re.compile(r"^[a-z_]+:[ \t]*", re.M)
_BODY_LABEL_RE = re.compile(r"\*\*(?:Why|How to apply):\*\*")


def stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    else:
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
    if len(word) > 3 and word[-1] == "e":
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2]:
        word = word[:-1]
    return word


def content_stems(text: str) -> set[str]:
    """The stems of the content words of ``text``: lower case, letters only, so
    file names and flags split into their words."""
    return {stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= 3 and w not in STOP_WORDS}


def shared_stems(left: set[str], right: set[str]) -> set[str]:
    """The stems in both sets. A stem of 4 or more letters also counts when it
    starts the other one, so an ending that ``stem`` misses still shows."""
    shared = left & right
    for a in left - shared:
        for b in right:
            short, long = (a, b) if len(a) <= len(b) else (b, a)
            if len(short) >= 4 and long.startswith(short):
                shared.add(short)
    return shared


def note_content(file_name: str, text: str) -> str:
    """The words of a note: its file name and its text, without the field names
    and the two body labels, which no query is about."""
    front, _, body = text.partition("\n---\n")
    return " ".join((file_name, _FRONT_KEY_RE.sub("", front), _BODY_LABEL_RE.sub("", body)))


# -- text from the frozen word list --------------------------------------------

_SOURCES = {
    "s": "services",
    "t": "tools",
    "c": "subcommands",
    "f": "flags",
    "n": "nouns",
    "m": "nouns",
    "n2": "nouns",
    "a": "adjectives",
    "a2": "adjectives",
    "v": "verbs",
    "v2": "verbs",
    "V": "verbs",
    "A": "adjectives",
    "e": "environments",
    "e2": "environments",
    "x": "files",
}
_FIELD_RE = re.compile(r"\{(\w+)\}")
THEME_SHARE = 0.65  # how often a sentence stays on the subject of its note
OVERSHOOT = 45  # half a sentence: a note stops after the sentence that reaches its size


class Words:
    """The frozen word list, and text made from its templates."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self._fields: dict[str, tuple[str, ...]] = {}

    def theme(self, rng: Rng) -> dict:
        d = self.data
        return {
            "s": rng.skewed(d["services"]),
            "t": rng.skewed(d["tools"]),
            "c": rng.pick(d["subcommands"]),
            "f": rng.pick(d["flags"]),
            "n": rng.skewed(d["nouns"]),
            "m": rng.skewed(d["nouns"]),
            "x": rng.pick(d["files"]),
            "e": rng.pick(d["environments"]),
        }

    def fill(self, rng: Rng, template: str, theme: dict) -> str:
        names = self._fields.get(template)
        if names is None:
            names = tuple(dict.fromkeys(_FIELD_RE.findall(template)))
            self._fields[template] = names
        values = {}
        for name in names:
            if name == "p":
                values[name] = str(1024 + rng.below(60000))
            elif name == "d":
                values[name] = str(2 + rng.below(88))
            elif name == "h":
                values[name] = f"{theme['s']}.{theme['project']}.example.com"
            elif name == "topic":
                values[name] = theme["topic"]
            elif name in theme and rng.chance(THEME_SHARE):
                values[name] = theme[name]
            else:
                values[name] = rng.skewed(self.data[_SOURCES[name]])
            if name in ("V", "A"):
                values[name] = values[name].capitalize()
        return template.format(**values)

    def line(self, rng: Rng, key: str, theme: dict) -> str:
        return self.fill(rng, rng.pick(self.data[key]), theme)


def body_text(words: Words, rng: Rng, theme: dict, target: int) -> str:
    """A note body of about ``target`` characters, in the shapes of the survey."""
    shares = SURVEY["body_shares"]
    blocks: list[str] = [" ".join(words.line(rng, "sentence_templates", theme) for _ in range(2))]
    if rng.chance(shares["why"]):
        blocks.append("**Why:** " + words.line(rng, "why_templates", theme))
    if rng.chance(shares["how"]):
        how = " ".join(words.line(rng, "sentence_templates", theme) for _ in range(2))
        blocks.append("**How to apply:** " + how)
    if rng.chance(shares["bullets"]):
        count = 3 + rng.below(3)
        blocks.append("\n".join(words.line(rng, "bullet_templates", theme) for _ in range(count)))
    if rng.chance(shares["fence"]):
        command = words.fill(rng, "{t} {c} {f}", theme)
        blocks.append(f"```\n{command}\n```")
    heading_at = len(blocks) if rng.chance(shares["heading"]) else -1
    used = sum(len(b) + 2 for b in blocks)
    while used < target - OVERSHOOT:
        if len(blocks) == heading_at:
            blocks.append("## " + rng.pick(words.data["sections"]))
        count = 1 + rng.below(2)
        block = " ".join(words.line(rng, "sentence_templates", theme) for _ in range(count))
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def _bounded(rng: Rng, words: Words, key: str, theme: dict, limit: int) -> str:
    for _ in range(8):
        text = words.line(rng, key, theme)
        if len(text) <= limit:
            return text
    return words.fill(rng, "{V} the {n}", theme)


def filler_triggers(rng: Rng, words: Words, theme: dict, scope: str) -> list[str]:
    items = [words.fill(rng, "{t} {c}", theme) for _ in range(2 + rng.below(3))]
    items.append(words.fill(rng, "{s} {n}", theme))
    items += [words.fill(rng, "phrase:{v} the {n}", theme) for _ in range(2 + rng.below(2))]
    if scope == "file" or rng.chance(0.3):
        items.append(f"glob:**/{theme['x']}")
    if rng.chance(0.05):
        items.append("tool:Bash")
    return list(dict.fromkeys(items))


@dataclass
class Note:
    file: str
    title: str
    text: str
    role: str = "filler"
    lesson: str = ""
    fields: frozenset = frozenset()  # the survey fields it holds
    kind: str = "other"  # the note type, for the mix
    hook_avoid: frozenset = frozenset()  # stems its index line must not hold


def render_note(fields: dict, body: str) -> str:
    lines = ["---", f"name: {fields['name']}", f"description: {fields['description']}"]
    for key in ("type", "status"):
        if fields.get(key):
            lines.append(f"{key}: {fields[key]}")
    if fields.get("rule"):
        lines.append(f"rule: {fields['rule']}")
        lines.append(f"apply: {json.dumps(fields['apply'])}")
        lines.append(f"scope: {fields['scope']}")
    if fields.get("triggers"):
        lines.append("triggers: [" + ", ".join(fields["triggers"]) + "]")
    for key in ("violates", "example_repeat", "example_ok", "rewrite"):
        if fields.get(key):
            lines.append(f"{key}: {json.dumps(fields[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def filler_note(words: Words, rng: Rng, spec: dict, project: str, serial: int) -> Note:
    theme = dict(words.theme(rng), project=project)
    kind = spec["type"]
    title = _bounded(rng, words, "title_templates", theme, 52)
    fields: dict = {"name": title, "type": "" if kind == "other" else kind}
    fields["description"] = words.line(rng, "description_templates", theme)
    if spec.get("status"):
        fields["status"] = rng.weighted(SURVEY["status_shares"])
    if spec.get("rule"):
        scope = "stop"
        if spec.get("triggers"):
            scope = rng.weighted(SURVEY["scope_shares"])
        fields["scope"] = "tool" if spec.get("violates") else scope
        rule = words.line(rng, "rule_templates", theme)
        more = words.line(rng, "rule_templates", theme)
        if len(rule) + len(more) < 150:
            rule = f"{rule} {more}"
        fields["rule"] = rule[:RULE_MAX]
        apply_text = words.line(rng, "apply_templates", theme)
        while True:
            more = words.line(rng, "apply_templates", theme)
            if len(apply_text) + len(more) >= APPLY_MAX:
                break
            apply_text = f"{apply_text} {more}"
        fields["apply"] = apply_text
    if spec.get("triggers"):
        fields["triggers"] = filler_triggers(rng, words, theme, fields["scope"])
    if spec.get("violates"):
        tool = rng.pick(words.data["guard_tools"])
        action = rng.pick(words.data["subcommands"])
        flag = rng.pick(words.data["flags"])
        safe = "--check" if flag == "--dry-run" else "--dry-run"
        fields["rule"] = f"Never run {tool} {action} {flag} on {theme['s']}."
        fields["violates"] = rf"\b{tool}\s+{action}\s+{flag}\b"
        fields["example_repeat"] = f"{tool} {action} {flag}"
        fields["example_ok"] = f"{tool} {action} {safe}"
        fields["triggers"] = [f"{tool} {action}", *fields["triggers"]]
        if spec.get("rewrite"):
            # The guard rewrites only the pairs of its safe table, so this note
            # names a real command. A rewrite note never adds a deny.
            target = f"tests/{re.sub('[^a-z]', '', theme['n'])}"
            fields["rule"] = f"Run pytest with -q when you run {target}."
            fields["violates"] = rf"\bpytest\s+{target}\b"
            fields["example_repeat"] = f"pytest {target}"
            fields["example_ok"] = f"pytest -q {target}"
            fields["triggers"] = ["pytest", *fields["triggers"][1:]]
            fields["rewrite"] = "add-flag -q after pytest"
    size = rng.note_size()
    head = render_note(fields, "x")
    body = body_text(words, rng, theme, max(120, size - len(head)))
    stem_name = f"{theme['s']}_{theme['n']}_{serial}".replace("-", "_")
    file = f"{stem_name}.md".replace("_", "-") if kind == "other" else f"{kind}_{stem_name}.md"
    held = frozenset(k for k in FIELDS if fields.get(k))
    return Note(file, title, render_note(fields, body), fields=held, kind=kind)


def filler_specs(rng: Rng, notes: int, planted: list[Note]) -> list[dict]:
    """One dictionary per filler note: its type and which fields it holds. The
    counts make the whole set, planted notes included, follow the survey, as far
    as the planted notes leave room."""
    count = notes - len(planted)
    planted_types = Counter(n.kind for n in planted)
    need = {
        t: max(0.0, share * notes - planted_types[t]) for t, share in SURVEY["type_shares"].items()
    }
    quota = largest_remainder(count, need)
    groups = {t: [{"type": t} for _ in range(n)] for t, n in quota.items()}
    by_type = SURVEY["field_shares_by_type"]
    for name in FIELDS:
        natural = {t: quota[t] * by_type[t][name] for t in quota}
        want = max(0.0, overall_share(name) * notes - sum(name in n.fields for n in planted))
        factor = want / sum(natural.values()) if sum(natural.values()) else 0.0
        for t, group in groups.items():
            chosen = min(len(group), round(natural[t] * factor))
            # The fields nest: triggers within rule, violates within triggers,
            # rewrite within violates, so the first notes of a group hold them.
            # A status sits on the last notes, apart from the rules.
            ordered = reversed(group) if name == "status" else group
            for spec in list(ordered)[:chosen]:
                spec[name] = True
    specs = [spec for t in sorted(groups) for spec in groups[t]]
    # At the small scales the planted rules leave no room for filler rules, but a
    # filler guard rule can still be wanted. The nesting must hold even then.
    for spec in specs:
        if spec.get("rewrite"):
            spec["violates"] = True
        if spec.get("violates"):
            spec["triggers"] = True
        if spec.get("triggers"):
            spec["rule"] = True
    rng.shuffle(specs)
    return specs


# -- lessons -------------------------------------------------------------------


@dataclass
class Lesson:
    raw: dict
    answer: str
    canary: str = ""
    notes: list[Note] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # [0] is quoted in the note
    commands: list[str] = field(default_factory=list)
    pads: list[str] = field(default_factory=list)  # the target's text after its lead

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def target(self) -> Note:
        return self.notes[0]

    @property
    def duplicate(self) -> Note:
        return self.notes[1]

    @property
    def near_misses(self) -> list[Note]:
        return self.notes[2:]


def _number(seed: int, *key: object) -> int:
    digest = hashlib.sha256(":".join(str(k) for k in (seed, *key)).encode()).digest()
    return 10000 + int.from_bytes(digest[:8], "big") % 55000


def _planted_note(
    words: Words, lesson: Lesson, seed: int, role: str, value: str, context: str, tail: str
) -> Note:
    raw = lesson.raw
    avoid = content_stems(raw["v2"])
    rng = Rng(seed, "note", raw["id"], role)
    title = raw["title"] if not context else f"{raw['title']} {context}"
    if role == "duplicate":
        title = f"{raw['title']} (seen again)"
    kind = raw["type"]
    fields: dict = {"name": title, "type": kind, "status": raw.get("status", "")}
    if raw["rule"]:
        rule = raw["rule"].format(answer=value)
        if context:
            rule = f"{rule[:-1]} {context}."
        fields["rule"] = rule
        fields["description"] = f"{title}. {raw['why']}"
        fields["apply"] = raw["apply"]
        fields["scope"] = "tool" if raw["triggers"] else "always"
        fields["triggers"] = raw["triggers"]
        lead = rule
    else:
        description = raw["description"].format(answer=value)
        if context:
            description = f"{description[:-1]} {context}."
        fields["description"] = description
        lead = description
    if raw.get("violates") and not context:  # a near miss does not deny
        fields["violates"] = raw["violates"]
        fields["example_repeat"] = raw["hazard"]
        fields["example_ok"] = raw["example_ok"]
    blocks = [lead, f"**Why:** {raw['why']}", f"**How to apply:** {raw['apply']}"]
    if raw.get("error"):
        blocks.append(f"The command prints this line:\n\n```\n{lesson.errors[0]}\n```")
    if raw.get("hazard"):
        blocks.append(f"The hazard is the command `{raw['hazard']}`.")
    head = render_note(fields, "\n\n".join(blocks))
    # The rest stays on the subject of the note, and it holds no word of the V2
    # query. Half of a duplicate repeats the target, as a real copy does.
    theme = dict(words.theme(rng), topic=raw["topic"])
    size = Rng(seed, "size", raw["id"]).note_size()
    pads = _pad_blocks(words, rng, theme, avoid, size - len(head))
    if role == "target":
        lesson.pads = pads
    elif role == "duplicate":
        half = len(lesson.pads) // 2
        pads = lesson.pads[:half] + pads[half:]
    if tail:
        pads.append(tail)
    text = render_note(fields, "\n\n".join([*blocks, *pads]))
    held = frozenset(k for k in FIELDS if fields.get(k))
    suffix = {"target": "", "duplicate": "_again"}.get(role, role)
    file = f"{kind}_{raw['slug']}{suffix}.md"
    return Note(
        file,
        title,
        text,
        role=role,
        lesson=raw["id"],
        fields=held,
        kind=kind,
        hook_avoid=frozenset(content_stems(raw["v3"])),
    )


def _pad_blocks(words: Words, rng: Rng, theme: dict, avoid: set, target: int) -> list[str]:
    blocks: list[str] = []
    used, tries = 0, 0
    while used < target - OVERSHOOT and tries < 400:
        tries += 1
        sentence = words.line(rng, "pad_templates", theme)
        if avoid and shared_stems(content_stems(sentence), avoid):
            continue
        blocks.append(sentence)
        used += len(sentence) + 2
    return blocks


def build_lessons(seed: int, words: Words, source: list[dict]) -> list[Lesson]:
    lessons = []
    for raw in source:
        lesson = Lesson(raw, f"{raw['flag']}={_number(seed, 'answer', raw['id'])}")
        if raw.get("error"):
            rng = Rng(seed, "error", raw["id"])
            for _ in range(4):
                path = f"/srv/{rng.pick(words.data['services'])}/{rng.pick(words.data['files'])}"
                number = 2 + rng.below(9000)
                lesson.errors.append(raw["error"].format(num=number, path=path))
                lesson.commands.append(raw["command"].format(num=number, path=path))
        if raw["kind"] == "credential":
            digest = hashlib.sha256(f"{seed}:canary:{raw['id']}".encode()).hexdigest()
            lesson.canary = f"canary{digest[:26]}"
        lesson.notes.append(_planted_note(words, lesson, seed, "target", lesson.answer, "", ""))
        lesson.notes.append(
            _planted_note(words, lesson, seed, "duplicate", lesson.answer, "", "Seen again.")
        )
        for index, (context, suffix) in enumerate(NEAR_MISS):
            flag = raw["flag"] + ("-alt" if index == 1 else "")
            value = f"{flag}={_number(seed, 'near', raw['id'], index)}"
            note = _planted_note(words, lesson, seed, f"_{suffix}", value, context, "")
            note.role = f"near_miss_{index}"
            lesson.notes.append(note)
        lessons.append(lesson)
    return lessons


def lesson_problems(lessons: list[Lesson], words: Words) -> list[str]:
    """Everything a lesson must be, from its text alone. A list of problems."""
    out: list[str] = []
    kinds = Counter(lesson.kind for lesson in lessons)
    want = {
        "prompt": 20,
        "guard": 10,
        "credential": 5,
        "error": 10,
        "subagent": 5,
        "continuity": 5,
        "label": 5,
    }
    if dict(kinds) != want:
        out.append(f"lesson kinds are {dict(kinds)}, not {want}")
    vocabulary = set()
    for key in ("services", "tools", "subcommands", "flags", "nouns", "verbs", "adjectives"):
        vocabulary.update(re.findall(r"[a-z]+", " ".join(words.data[key]).lower()))
    answers = [lesson.answer for lesson in lessons]
    if len(set(answers)) != len(answers):
        out.append("two lessons have the same answer")
    for lesson in lessons:
        raw, lid = lesson.raw, lesson.id
        reserved = raw["flag"].lstrip("-").split("-")[0]
        if reserved in vocabulary:
            out.append(f"{lid}: the flag word {reserved} is in the word list")
        for note in lesson.notes:
            content = note_content(note.file, note.text)
            if note.role == "target" and lesson.answer not in note.text:
                out.append(f"{lid}: the target note lacks the answer")
            for line in note.text.split("\n"):
                if line.startswith("rule: ") and len(line) - 6 > RULE_MAX:
                    out.append(f"{note.file}: the rule is over {RULE_MAX} characters")
            if note.role in ("target", "duplicate"):
                hits = shared_stems(content_stems(raw["v2"]), content_stems(content))
                if hits:
                    out.append(f"{note.file}: V2 shares stems {sorted(hits)}")
        if raw["rule"] and raw["rule"].format(answer=lesson.answer) not in lesson.target.text:
            out.append(f"{lid}: the rule line lost its text")
        if len(raw["apply"]) > APPLY_MAX:
            out.append(f"{lid}: apply is over {APPLY_MAX} characters")
        index_words = content_stems(f"{lesson.target.file} {lesson.target.title}")
        v3 = content_stems(raw["v3"])
        hits = shared_stems(v3, index_words)
        if hits:
            out.append(f"{lid}: V3 shares stems with the index line: {sorted(hits)}")
        if not shared_stems(v3, content_stems(note_content("", lesson.target.text))):
            out.append(f"{lid}: V3 shares no word with the note, so a word search cannot reach it")
        for key in ("v1", "v2", "v3"):
            if lesson.answer in raw[key]:
                out.append(f"{lid}: {key} holds the answer")
    return out


# -- queries -------------------------------------------------------------------


def _task_notification(seed: int, lesson: Lesson) -> str:
    digest = hashlib.sha256(f"{seed}:v7:{lesson.id}".encode()).hexdigest()
    return (
        f"<task-notification>\n<task-id>a{digest[:16]}</task-id>\n"
        f"<tool-use-id>toolu_{digest[16:40]}</tool-use-id>\n"
        f"<output-file>/tmp/tasks/a{digest[:16]}.output</output-file>\n"
        f"<status>completed</status>\n"
        f'<summary>Agent "{lesson.raw["topic"]}" completed</summary>\n'
        f"<result>Finished the work on {lesson.raw['topic']}. {lesson.raw['v3']}</result>\n"
        f"</task-notification>"
    )


def _error_output(line: str) -> str:
    """The text a failed command gives. The hook reads a Python exception line only
    after a traceback, and only when the last frame is a library frame."""
    if not re.match(r"[A-Za-z]+Error: ", line):
        return f"Exit code 1\n{line}"
    frame = '  File "/usr/lib/python3/dist-packages/worker/main.py", line 42, in run\n    load()'
    return f"Exit code 1\nTraceback (most recent call last):\n{frame}\n{line}"


def _query(qid: str, variant: str, lesson: Lesson | None, expect: str, **more) -> dict:
    row = {
        "id": qid,
        "variant": variant,
        "lesson": lesson.id if lesson else "",
        "claims": VARIANT_CLAIMS[variant],
        "expect": expect,
        "cwd": "session",
    }
    row.update(more)
    return row


def build_queries(seed: int, words: Words, lessons: list[Lesson]) -> list[dict]:
    rows: list[dict] = []
    for lesson in lessons:
        raw, lid = lesson.raw, lesson.id
        rows.append(_query(f"{lid}-V1", "V1", lesson, "answer", prompt=raw["v1"]))
        rows.append(_query(f"{lid}-V2", "V2", lesson, "answer", prompt=raw["v2"]))
        rows.append(_query(f"{lid}-V3", "V3", lesson, "answer", prompt=raw["v3"]))
        rows.append(_query(f"{lid}-V4", "V4", lesson, "answer", prompt=raw["v3"], cwd="other"))
        rows.append(
            _query(
                f"{lid}-V6",
                "V6",
                lesson,
                "answer",
                prompt="{FIXTURE_ROOT}/tasks/" + f"{lid}.md",
                task_file=f"tasks/{lid}.md",
            )
        )
        rows.append(
            _query(f"{lid}-V7", "V7", lesson, "no_rows", prompt=_task_notification(seed, lesson))
        )
    rng = Rng(seed, "v5")
    templates = list(words.data["v5_templates"])
    for index in range(100):
        lesson = lessons[index % len(lessons)]
        template = templates[(index + index // len(lessons)) % len(templates)]
        rows.append(
            _query(
                f"V5-{index + 1:03d}",
                "V5",
                None,
                "none",
                near_lesson=lesson.id,
                prompt=template.format(topic=lesson.raw["topic"]),
            )
        )
    for lesson in lessons:
        raw, lid = lesson.raw, lesson.id
        if raw["kind"] == "guard":
            folder = rng.pick(words.data["cd_folders"])
            spellings = (
                ("plain", raw["hazard"]),
                ("cd", f"cd {folder} && {raw['hazard']}"),
                ("full_path", f"/usr/local/bin/{raw['hazard']}"),
                ("env", f"STAGE=prod {raw['hazard']}"),
                ("sudo", f"sudo {raw['hazard']}"),
                ("other_tool", raw["other_tool"]),
            )
            for name, command in spellings:
                rows.append(
                    _query(
                        f"{lid}-CMD-{name}", "CMD", lesson, "deny", spelling=name, command=command
                    )
                )
        if raw["kind"] == "credential":
            for index, command in enumerate(raw["commands"], 1):
                rows.append(
                    _query(
                        f"{lid}-CRED-{index}",
                        "CRED",
                        lesson,
                        "block",
                        source=raw["source"],
                        command=command,
                    )
                )
        if raw["kind"] == "error":
            for index in (1, 2, 3):
                rows.append(
                    _query(
                        f"{lid}-ERR-{index}",
                        "ERR",
                        lesson,
                        "show",
                        command=lesson.commands[index],
                        error=_error_output(lesson.errors[index]),
                    )
                )
        if raw["kind"] == "subagent":
            rows.append(
                _query(
                    f"{lid}-SUB-full",
                    "SUB",
                    lesson,
                    "answer",
                    tool_input={
                        "prompt": raw["task"],
                        "subagent_type": "general-purpose",
                        "description": raw["task_description"],
                    },
                )
            )
            rows.append(
                _query(
                    f"{lid}-SUB-meta",
                    "SUB",
                    lesson,
                    "answer",
                    tool_input={
                        "subagent_type": "general-purpose",
                        "description": raw["task_description"],
                    },
                )
            )
        if raw["kind"] == "continuity":
            for source in ("compact", "clear", "resume"):
                steps = [{"event": "UserPromptSubmit", "prompt": raw["v1"]}]
                if source == "compact":
                    steps.append({"event": "PreCompact", "trigger": "auto"})
                steps.append({"event": "SessionStart", "source": source})
                rows.append(_query(f"{lid}-CONT-{source}", "CONT", lesson, "answer", steps=steps))
        if raw["kind"] == "label":
            name, generic = raw["label_file"], raw["generic_file"]
            rows.append(
                _query(
                    f"{lid}-LABEL-prompt",
                    "LABEL",
                    lesson,
                    "label_row",
                    prompt=f"Please look at {name} and tell me what it does.",
                )
            )
            rows.append(
                _query(
                    f"{lid}-LABEL-command", "LABEL", lesson, "label_row", command=f"cat conf/{name}"
                )
            )
            rows.append(
                _query(
                    f"{lid}-LABEL-generic",
                    "LABEL",
                    lesson,
                    "no_label_row",
                    prompt=f"Please look at {generic} and tell me what it does.",
                    command=f"cat {generic}",
                )
            )
    return rows


# -- placement -----------------------------------------------------------------


@dataclass
class Project:
    rank: int
    name: str
    size: int
    shape: str
    slots: dict = field(default_factory=dict)
    topic_files: list = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"-work-{self.name}"

    @property
    def cwd(self) -> str:
        return f"/work/{self.name}"

    def add(self, slot: str, note: Note) -> None:
        self.slots.setdefault(slot, []).append(note)

    def count(self) -> int:
        return sum(len(notes) for notes in self.slots.values())


def make_projects(scale: Scale, words: Words) -> list[Project]:
    sizes = project_sizes(scale)
    return [
        Project(rank, words.data["projects"][rank], size, project_shape(scale, rank, size))
        for rank, size in enumerate(sizes)
    ]


def lesson_placements(scale: Scale, lessons: list[Lesson]) -> dict[str, str]:
    """The placement of each lesson at ``scale``. Guard, credential and label
    lessons sit behind a topic file, the stock-fail case of their claims (C6, C16).
    The others rotate over what the scale offers. A lesson with no rule fields
    (C20) tests the own project, so it never sits in another one."""
    out: dict[str, str] = {}
    seen: Counter = Counter()
    for index, lesson in enumerate(lessons):
        offered = scale.placements
        if lesson.kind in ("guard", "credential", "label"):
            out[lesson.id] = "linked" if "linked" in offered else "behind_topic"
            continue
        # Per shard: a rotation over all lessons would match the shard split, and
        # each shard of MEDIUM-1 would get one placement only.
        key = (lesson.kind, index % scale.shards)
        placement = offered[seen[key] % len(offered)]
        seen[key] += 1
        if placement == "other_project" and not lesson.raw["rule"]:
            placement = "unlinked"
        out[lesson.id] = placement
    return out


def lesson_homes(scale: Scale, projects: list[Project], placements: dict) -> dict[str, dict]:
    """Which project holds each lesson, and which project the session runs in."""
    by_placement = {
        "linked": projects,
        "past_line_200": [p for p in projects if p.shape == "line_cap"],
        "past_25kb": [p for p in projects if p.shape == "byte_cap"],
        "behind_topic": [p for p in projects if p.shape in TOPIC_SHAPES][:5],
        "unlinked": projects[:1] if scale.notes == 200 else projects[:4],
        "other_project": projects[1:],
    }
    seen: Counter = Counter()
    homes: dict[str, dict] = {}
    for lid, placement in placements.items():
        hosts = by_placement[placement]
        host = hosts[seen[placement] % len(hosts)]
        seen[placement] += 1
        session = projects[0] if placement == "other_project" else host
        others = [p for p in projects if p.rank not in (host.rank, session.rank)]
        other = others[seen["other"] % len(others)].name if others else ""
        seen["other"] += 1
        if not other and len(projects) > 1:
            other = session.name
        homes[lid] = {
            "placement": placement,
            "note_project": host.name,
            "session_project": session.name,
            "other_project": other or OUTSIDE_PROJECT,
            "other_rank": next((p.rank for p in projects if p.name == other), host.rank),
        }
    return homes


def plant(scale: Scale, projects: list[Project], lessons: list[Lesson], homes: dict) -> None:
    by_name = {p.name: p for p in projects}
    for lesson in lessons:
        home = homes[lesson.id]
        host = by_name[home["note_project"]]
        session = by_name[home["session_project"]]
        slot = TARGET_SLOT[home["placement"]]
        host.add(slot, lesson.target)
        host.add(slot, lesson.duplicate)
        first, second, third = lesson.near_misses
        session.add("index", first)  # in the part that stock loads
        host.add(slot, second)  # beside the target
        projects[home["other_rank"]].add("index", third)


def spread_filler(
    scale: Scale,
    projects: list[Project],
    words: Words,
    seed: int,
    shard: int,
    planted: list[Note],
) -> None:
    specs = iter(filler_specs(Rng(seed, "plan", scale.notes, shard), scale.notes, planted))
    fillers = [project.size - project.count() for project in projects]
    if min(fillers) < 0:
        raise SystemExit(f"{scale.name} shard {shard}: the planted notes exceed a project")
    serial = 0
    for project in projects:
        split = largest_remainder(fillers[project.rank], SHAPE_SLOTS[project.shape])
        for slot in sorted(split):
            for _ in range(split[slot]):
                serial += 1
                rng = Rng(seed, "filler", scale.notes, shard, serial)
                project.add(slot, filler_note(words, rng, next(specs), project.name, serial))


# -- the index files -----------------------------------------------------------


def _link(note: Note) -> str:
    return f"- [{note.title}]({note.file})"


def _long_link(words: Words, rng: Rng, note: Note, theme: dict) -> str:
    for _ in range(40):
        hook = words.line(rng, "hook_templates", theme)
        if not shared_stems(content_stems(hook), set(note.hook_avoid)):
            break
    else:
        hook = "See the note."
    return f"{_link(note)}: {hook}"


def _plain_line(words: Words, rng: Rng, theme: dict, long: bool) -> str:
    if long:
        return "- " + words.line(rng, "hook_templates", theme)
    return words.line(rng, "plain_line_templates", theme)


def _with_headings(words: Words, rng: Rng, lines: list[str]) -> list[str]:
    out: list[str] = []
    for index, line in enumerate(lines):
        if index % 14 == 0:
            out.append(f"## {rng.pick(words.data['sections'])}")
        out.append(line)
    return out


def _topic_files(project: Project, words: Words, rng: Rng) -> list[tuple[str, str, str]]:
    """(file, heading, text) for the notes of the topic slot."""
    notes = list(project.slots.get("topic", ()))
    rng.shuffle(notes)
    out = []
    for index in range(0, len(notes), TOPIC_FILE_NOTES):
        part = notes[index : index + TOPIC_FILE_NOTES]
        section = rng.pick(words.data["sections"])
        file = f"topic_{section.lower()}_{index // TOPIC_FILE_NOTES + 1}.md"
        text = f"# {section}\n\n" + "\n".join(_link(n) for n in part) + "\n"
        out.append((file, section, text))
    return out


def build_index(project: Project, words: Words, seed: int, scale: Scale, shard: int) -> str:
    rng = Rng(seed, "index", scale.notes, shard, project.rank)
    theme = dict(words.theme(rng), project=project.name)
    notes = list(project.slots.get("index", ()))
    long = project.shape == "byte_cap"
    planted = [n for n in notes if n.lesson]
    filler = [n for n in notes if not n.lesson]
    rng.shuffle(planted)
    rng.shuffle(filler)
    make = (lambda n: _long_link(words, rng, n, theme)) if long else _link
    topics = [
        f"- [Topic: {section}]({file})" + (": " + words.line(rng, "hook_templates", theme))
        if long
        else f"- [Topic: {section}]({file})"
        for file, section, _ in project.topic_files
    ]
    wanted = topics + [make(n) for n in planted]  # these stay in the loaded part
    rest = [make(n) for n in filler]
    if project.shape == "flat_all":
        rng.shuffle(wanted)
        return "\n".join(wanted + rest) + "\n"
    head = _with_headings(words, rng, wanted + rest)
    if project.shape == "line_cap":
        lines = head[: LINE_CAP - 1]
        over = [line for line in head[LINE_CAP - 1 :] if not line.startswith("#")]
        while len(lines) < LINE_CAP - 1:
            lines.append(_plain_line(words, rng, theme, False))
        lines.append(over[0] if over else _plain_line(words, rng, theme, False))
        tail = over[1:] + [_link(n) for n in project.slots.get("past_line", ())]
        rng.shuffle(tail)
        return "\n".join(lines + tail) + "\n"
    if project.shape == "byte_cap":
        lines, used, kept = [], 0, 0
        for line in head:
            if used >= BYTE_CAP + BYTE_MARGIN:
                break
            lines.append(line)
            used += len(line) + 1
            kept += 1
        while used < BYTE_CAP + BYTE_MARGIN:
            line = _plain_line(words, rng, theme, True)
            lines.append(line)
            used += len(line) + 1
        past = [_long_link(words, rng, n, theme) for n in project.slots.get("past_bytes", ())]
        rng.shuffle(past)
        return "\n".join(lines + past + head[kept:]) + "\n"
    return "\n".join(head) + "\n"


# -- writing -------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str) -> str:
    data = text.encode("utf-8")
    path.write_bytes(data)
    return _sha256(data)


def _json_text(value: object) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def write_shard(
    scale: Scale,
    shard: int,
    lessons: list[Lesson],
    placements: dict,
    words: Words,
    seed: int,
    root: Path,
) -> dict:
    projects = make_projects(scale, words)
    chosen = [lesson for index, lesson in enumerate(lessons) if index % scale.shards == shard]
    homes = lesson_homes(scale, projects, {x.id: placements[x.id] for x in chosen})
    plant(scale, projects, chosen, homes)
    planted = [note for lesson in chosen for note in lesson.notes]
    spread_filler(scale, projects, words, seed, shard, planted)
    folder = root / f"shard-{shard:02d}"
    hashes: dict[str, str] = {}
    sizes: list[int] = []
    mix: Counter = Counter()
    kinds: Counter = Counter()
    links: Counter = Counter()
    records = []
    for project in projects:
        memory = folder / "projects" / project.slug / "memory"
        memory.mkdir(parents=True)
        rng = Rng(seed, "topics", scale.notes, shard, project.rank)
        project.topic_files = _topic_files(project, words, rng)
        index = build_index(project, words, seed, scale, shard)
        written = [("MEMORY.md", index)] + [(f, text) for f, _, text in project.topic_files]
        for slot, notes in project.slots.items():
            links["unlinked" if slot == "unlinked" else slot] += len(notes)
            for note in notes:
                sizes.append(len(note.text))
                mix.update(note.fields)
                kinds[note.kind] += 1
                written.append((note.file, note.text))
        for name, text in written:
            hashes[f"shard-{shard:02d}/projects/{project.slug}/memory/{name}"] = _write(
                memory / name, text
            )
        lines = index.count("\n")
        records.append(
            {
                "name": project.name,
                "slug": project.slug,
                "cwd": project.cwd,
                "notes": project.size,
                "shape": project.shape,
                "index_lines": lines,
                "index_bytes": len(index.encode()),
                "topic_files": len(project.topic_files),
            }
        )
    return {
        "homes": homes,
        "projects": records,
        "hashes": hashes,
        "sizes": sizes,
        "mix": mix,
        "kinds": kinds,
        "links": links,
    }


def _quantile(values: list[int], point: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(point * len(ordered)))]


def write_scale(scale: Scale, lessons: list[Lesson], words: Words, seed: int, out: Path) -> dict:
    root = out / scale.folder
    root.mkdir()
    placements = lesson_placements(scale, lessons)
    hashes: dict[str, str] = {}
    sizes: list[int] = []
    mix: Counter = Counter()
    kinds: Counter = Counter()
    links: Counter = Counter()
    lesson_rows: dict[str, dict] = {}
    shard_rows = []
    by_id = {lesson.id: lesson for lesson in lessons}
    for shard in range(scale.shards):
        result = write_shard(scale, shard, lessons, placements, words, seed, root)
        hashes.update(result["hashes"])
        sizes += result["sizes"]
        mix.update(result["mix"])
        kinds.update(result["kinds"])
        links.update(result["links"])
        shard_rows.append({"shard": shard, "projects": result["projects"]})
        for lid, home in result["homes"].items():
            lesson = by_id[lid]
            row = {k: v for k, v in home.items() if k != "other_rank"}
            row.update(
                shard=shard,
                target=lesson.target.file,
                duplicate=lesson.duplicate.file,
                near_misses=[n.file for n in lesson.near_misses],
            )
            lesson_rows[lid] = row
    listing = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    _write(root / "files.sha256", listing)
    total = len(sizes)
    actual = {name: round(mix[name] / total, 4) for name in FIELDS}
    summary = {
        "name": scale.name,
        "notes": scale.notes,
        "projects": scale.projects,
        "shards": scale.shards,
        "lessons_per_shard": scale.lessons_per_shard,
        "note_files": total,
        "tree_sha256": _sha256(listing.encode()),
        "bytes": sum(sizes),
        "field_mix": {
            "survey": {name: round(overall_share(name), 4) for name in FIELDS},
            "actual": actual,
        },
        "type_mix": {
            "survey": SURVEY["type_shares"],
            "actual": {t: round(kinds[t] / total, 4) for t in sorted(kinds)},
        },
        "link_mix": {
            "survey": SURVEY["link_shares"],
            "actual": {slot: round(links[slot] / total, 4) for slot in sorted(links)},
        },
        "sizes": {
            "survey_mean": SURVEY["size_mean_chars"],
            "mean": round(sum(sizes) / total),
            "p50": _quantile(sizes, 0.5),
            "p95": _quantile(sizes, 0.95),
            "max": max(sizes),
        },
        "placements": dict(sorted(Counter(placements.values()).items())),
    }
    detail = dict(summary, lessons=lesson_rows, shards_detail=shard_rows)
    _write(root / "scale.json", _json_text(detail))
    return summary


def generate(out: Path, seed: int, scale_sizes: list[int]) -> dict:
    """Write the fixture to ``out``. Returns the manifest."""
    check_output_folder(out)
    words_raw = WORDS_FILE.read_bytes()
    lessons_raw = LESSONS_FILE.read_bytes()
    words = Words(json.loads(words_raw))
    lessons = build_lessons(seed, words, json.loads(lessons_raw))
    problems = lesson_problems(lessons, words)
    if problems:
        raise SystemExit("the frozen lessons fail their checks:\n" + "\n".join(problems))
    queries = build_queries(seed, words, lessons)
    out.mkdir(parents=True, exist_ok=True)
    tasks = out / "tasks"
    tasks.mkdir()
    task_hash = hashlib.sha256()
    for lesson in lessons:
        text = lesson.raw["v3"] + "\n"
        _write(tasks / f"{lesson.id}.md", text)
        task_hash.update(text.encode())
    query_ids: dict[str, list[str]] = {}
    for row in queries:
        if row["lesson"]:
            query_ids.setdefault(row["lesson"], []).append(row["id"])
    lesson_rows = [_lesson_row(lesson, seed, query_ids[lesson.id]) for lesson in lessons]
    lessons_text = _json_text(lesson_rows)
    queries_text = "".join(json.dumps(q, sort_keys=True, ensure_ascii=True) + "\n" for q in queries)
    _write(out / "lessons.json", lessons_text)
    _write(out / "queries.jsonl", queries_text)
    scales = {}
    for size in scale_sizes:
        scales[str(size)] = write_scale(SCALES[size], lessons, words, seed, out)
    manifest = {
        "format": FORMAT_VERSION,
        "generator": "tools/eval/scale_fixture.py",
        "generator_sha256": _sha256(Path(__file__).read_bytes()),
        "words_sha256": _sha256(words_raw),
        "lessons_source_sha256": _sha256(lessons_raw),
        "lessons_sha256": _sha256(lessons_text.encode()),
        "queries_sha256": _sha256(queries_text.encode()),
        "tasks_sha256": task_hash.hexdigest(),
        "seed": seed,
        "survey": SURVEY,
        "stock_caps": {"lines": LINE_CAP, "bytes": BYTE_CAP},
        "lessons": len(lessons),
        "planted_notes_per_lesson": 5,
        "queries": dict(sorted(Counter(q["variant"] for q in queries).items())),
        "queries_total": len(queries),
        "scales": scales,
    }
    _write(out / "manifest.json", _json_text(manifest))
    return manifest


def _lesson_row(lesson: Lesson, seed: int, query_ids: list[str]) -> dict:
    raw = lesson.raw
    row = {
        "id": lesson.id,
        "kind": lesson.kind,
        "title": raw["title"],
        "topic": raw["topic"],
        "answer": lesson.answer,
        "target_note": lesson.target.file,
        "duplicate_note": lesson.duplicate.file,
        "near_miss_notes": [n.file for n in lesson.near_misses],
        "query_ids": query_ids,
    }
    for key in ("hazard", "other_tool", "violates", "label_file", "source", "secret_path"):
        if raw.get(key):
            row[key] = raw[key]
    if lesson.canary:
        row["canary"] = lesson.canary
        row["canary_line"] = _canary_line(raw["source"], lesson.canary)
    return row


def _canary_line(source: str, canary: str) -> str:
    """The line that puts the fake secret in its source, in a scratch clone or
    home folder of the harness. The value is made at run time, so the repository
    holds no secret-shaped string."""
    url = "https://fixture:" + canary + "@git.example.com/mirror.git"
    if source == "git_remote":
        return f"url = {url}"
    if source == "git_pushurl":
        return f"pushurl = {url}"
    if source == "netrc":
        return f"machine git.example.com login fixture password {canary}"
    return f"DB_PASSWORD={canary}"


def check_output_folder(out: Path) -> None:
    """Refuse a folder in the repository, in Claude Code's config folder, or
    one that holds files."""
    target = out.expanduser().resolve()
    for parent in (REPO, Path.home() / ".claude"):
        root = parent.resolve()
        if target == root or root in target.parents:
            raise SystemExit(f"refused: the output folder must be outside {root}")
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"refused: the output folder {target} is not empty")


# -- verify --------------------------------------------------------------------


def _front_field(text: str, key: str) -> str:
    front = text.partition("\n---\n")[0]
    match = re.search(rf"^{key}: (.*)$", front, re.M)
    return match.group(1) if match else ""


def _verify_lesson(row: dict, home: dict, memory_of: dict, problems: list[str]) -> None:
    """The stock-fail placement of one lesson, from the files."""
    lid, placement = row["id"], home["placement"]
    session = memory_of[home["session_project"]]
    note = memory_of[home["note_project"]]
    target = row["target_note"]
    if not (note / target).is_file():
        problems.append(f"{lid}: {target} is not in project {home['note_project']}")
        return
    index = (session / "MEMORY.md").read_text(encoding="utf-8")
    lines = index.split("\n")
    number = next((i for i, line in enumerate(lines, 1) if f"]({target})" in line), 0)
    offset = index.find(f"]({target})")
    head = "\n".join(lines[:LINE_CAP]).encode()[:BYTE_CAP].decode("utf-8", "ignore")
    if row["answer"] in index:
        problems.append(f"{lid}: the answer is in the index")
    if placement == "linked":
        if not number or number > LINE_CAP:
            problems.append(f"{lid}: linked, but the index does not load the link")
        return
    for name in (target, row["duplicate_note"]):  # a loaded copy would give stock the fact
        if name in head:
            problems.append(f"{lid}: {placement}, but the loaded index names {name}")
    if placement == "past_line_200":
        if number <= LINE_CAP:
            problems.append(f"{lid}: past_line_200, but the link is on line {number}")
        if len("\n".join(lines[:LINE_CAP]).encode()) >= BYTE_CAP:
            problems.append(f"{lid}: past_line_200, but the byte cap comes first")
    elif placement == "past_25kb":
        if offset < BYTE_CAP + BYTE_MARGIN or number == 0 or number > LINE_CAP:
            problems.append(f"{lid}: past_25kb, but the link is at {offset}, line {number}")
    elif placement == "behind_topic":
        holders = [
            t.name
            for t in sorted(note.glob("topic_*.md"))
            if f"]({target})" in t.read_text(encoding="utf-8")
        ]
        if number or not holders:
            problems.append(f"{lid}: behind_topic, line {number}, topic files {holders}")
        elif any(f"]({name})" not in head for name in holders):
            problems.append(f"{lid}: behind_topic, but the topic file is past the load")
    elif placement == "unlinked":
        listed = any(
            f"]({target})" in p.read_text(encoding="utf-8")
            for p in [note / "MEMORY.md", *sorted(note.glob("topic_*.md"))]
        )
        if listed:
            problems.append(f"{lid}: unlinked, but an index links the note")
    elif placement == "other_project":
        if home["session_project"] == home["note_project"] or (session / target).exists():
            problems.append(f"{lid}: other_project, but the session project holds the note")


def _verify_answers(rows: list[dict], shard: Path, problems: list[str]) -> None:
    """Each answer string is in its target and its duplicate, and nowhere else."""
    owners = {row["answer"]: {row["target_note"], row["duplicate_note"]} for row in rows}
    pattern = re.compile("|".join(re.escape(a) + r"(?!\d)" for a in sorted(owners)))
    for path in sorted(shard.glob("projects/*/memory/*.md")):
        for hit in set(pattern.findall(path.read_text(encoding="utf-8"))):
            if path.name not in owners[hit]:
                problems.append(f"{path.name} holds the answer {hit}")


def verify_output(out: Path) -> list[str]:
    """Check a fixture from its files alone. Returns the problems found."""
    problems: list[str] = []
    if not (out / "manifest.json").is_file():
        return [f"{out} has no manifest.json"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lesson_rows = json.loads((out / "lessons.json").read_text(encoding="utf-8"))
    queries_text = (out / "queries.jsonl").read_text(encoding="utf-8")
    if _sha256(queries_text.encode()) != manifest["queries_sha256"]:
        problems.append("queries.jsonl does not match the manifest")
    lessons_bytes = (out / "lessons.json").read_bytes()
    if _sha256(lessons_bytes) != manifest["lessons_sha256"]:
        problems.append("lessons.json does not match the manifest")
    queries = [json.loads(line) for line in queries_text.splitlines()]
    by_id = {row["id"]: row for row in lesson_rows}
    for query in queries:
        text = json.dumps(query)
        for row in lesson_rows:
            if row["answer"] in text:
                problems.append(f"{query['id']} holds the answer of {row['id']}")
        if query["variant"] == "V6" and not (out / query["task_file"]).is_file():
            problems.append(f"{query['id']}: the task file is missing")
    for size, summary in manifest["scales"].items():
        scale = SCALES[int(size)]
        root = out / scale.folder
        listing = (root / "files.sha256").read_text(encoding="utf-8")
        if _sha256(listing.encode()) != summary["tree_sha256"]:
            problems.append(f"{scale.folder}: files.sha256 does not match the manifest")
        for line in listing.splitlines():
            digest, name = line.split("  ", 1)
            if not (root / name).is_file():
                problems.append(f"{scale.folder}/{name}: the file is missing")
            elif _sha256((root / name).read_bytes()) != digest:
                problems.append(f"{scale.folder}/{name}: the hash differs")
        detail = json.loads((root / "scale.json").read_text(encoding="utf-8"))
        lessons = detail["lessons"]
        if sorted(lessons) != sorted(by_id):
            problems.append(f"{scale.folder}: scale.json does not hold all 60 lessons")
        for shard_row in detail["shards_detail"]:
            shard = root / f"shard-{shard_row['shard']:02d}"
            count = 0
            memory_of = {}
            for project in shard_row["projects"]:
                memory = shard / "projects" / project["slug"] / "memory"
                memory_of[project["name"]] = memory
                notes = [
                    p for p in memory.glob("*.md") if not p.name.startswith(("MEMORY", "topic_"))
                ]
                count += len(notes)
                if len(notes) != project["notes"]:
                    problems.append(f"{memory}: {len(notes)} notes, not {project['notes']}")
            if count != scale.notes:
                problems.append(f"{shard}: {count} notes, not {scale.notes}")
            here = [
                by_id[lid] for lid, home in lessons.items() if home["shard"] == shard_row["shard"]
            ]
            for row in here:
                _verify_lesson(row, lessons[row["id"]], memory_of, problems)
                note = memory_of[lessons[row["id"]]["note_project"]] / row["target_note"]
                content = note_content(note.name, note.read_text(encoding="utf-8"))
                v2 = next(q["prompt"] for q in queries if q["id"] == f"{row['id']}-V2")
                hits = shared_stems(content_stems(v2), content_stems(content))
                if hits:
                    problems.append(f"{row['id']}: V2 shares stems {sorted(hits)}")
            _verify_answers(here, shard, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify the scale fixture.")
    parser.add_argument("--out", type=Path, help="a new or empty folder outside the repository")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--scales",
        default=",".join(str(size) for size in SCALES),
        help="comma-separated note counts (default: all five)",
    )
    parser.add_argument("--verify", type=Path, help="check a fixture folder and exit")
    args = parser.parse_args(argv)
    if args.verify:
        problems = verify_output(args.verify.expanduser())
        for line in problems[:40]:
            print(line)
        print(f"verify: {len(problems)} problems")
        return 1 if problems else 0
    if args.out is None:
        parser.error("--out or --verify is required")
    sizes = [int(part) for part in args.scales.split(",")]
    unknown = [size for size in sizes if size not in SCALES]
    if unknown:
        parser.error(f"unknown scales {unknown}; the scales are {sorted(SCALES)}")
    started = time.monotonic()
    manifest = generate(args.out.expanduser(), args.seed, sizes)
    seconds = time.monotonic() - started
    for size, row in manifest["scales"].items():
        print(
            f"{row['name']}: {size} notes x {row['shards']} shards, "
            f"{row['bytes'] / 1e6:.1f} MB, mean note {row['sizes']['mean']} characters"
        )
    print(f"{manifest['queries_total']} queries, {seconds:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
