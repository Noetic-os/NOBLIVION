# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held-out real set of NOBLIVION recall (NOBLIVION-144).

Planted lessons say what the recall can do. This tool says what it does on the
prompts of one real install. It samples, with a fixed seed, from the local
transcripts and the plugin logs:

* 150 prompts: 30 each of home folder, project folders, file-path prompts, task
  notifications and subagent tasks;
* 40 error-recall shows (``error-recall-log.jsonl``);
* 50 label rows (``guard-log.jsonl``);
* 50 prompts for the correction detector: half that the detector or a wider phrase
  probe marks, half at random, because a random sample holds almost no correction.

For each of the 150 prompts it pools two arms:

* NOBLIVION: the rows of the build's own hook, ``recall_hook`` or, for a subagent
  task, ``subagent_rules_hook``, on a scratch store made from a read-only copy
  (the core of ``tools/replay_prompt_rows.py``). Each prompt runs alone in its own
  session, with a copy of the install's hub counts;
* stock S2: the most generous simulation of stock memory (``StockFolder``).

A note that did not exist yet at the time of the prompt is in neither arm
(``replay_transcripts.note_births``). The labels are blind. ``labels.md`` and
``groups.jsonl`` hold no arm, a note of both arms is one item, and the order comes
from a hash of the seed and the note, never from an arm. The arms are in the key
files, which only ``report`` reads.

A pool holds up to 10 notes, so the 150 prompts hold over 1,000 items, not 150. To
keep the labelling near 2 hours, ``labels.md`` has one block per prompt, error show,
label row or correction prompt, with one field. The labeller reads the text once,
scans its notes and types the letters of the notes that help, or ``n``. ``import``
turns each block into one y or n per item.

Commands::

    heldout.py build  --out DIR   sample, pool and write the set (refuses to overwrite)
    heldout.py status --out DIR   blocks done and left, per class
    heldout.py import --out DIR   read labels.md into labels.jsonl
    heldout.py report --out DIR   delivery, precision and false delivery on the dev split

The 150 prompts split into 50 dev and 100 sealed; one session never sits on both
sides. Tuning code reads ``load_split(out)``: the dev split only. ``report`` refuses
the sealed split unless it gets ``--split sealed --open-sealed``, and it logs that
opening in ``sealed-opened.jsonl``.

``build`` runs on a Python that has the ``noblivion`` package and the embedding
model files, because it starts a store. The other commands need the standard
library only. ``--build-dir`` names a checkout of the frozen build: its ``hooks``
and ``src`` serve the NOBLIVION arm, so a later change on the branch cannot move
the pool. All text goes through ``noblivion.redaction`` before it is written. The
output folder holds real text: mode 0700, files 0600, and it must not be in a git
work tree. The live store is read, never written; its ``token`` is not read.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import types
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent.parent
REPO = TOOLS.parent

SEED = 144
STRATA = ("home", "project", "file_path", "task_notification", "subagent_task")
PER_STRATUM = 30  # 150 prompts
DEV_PER_STRATUM = 10  # 50 dev, 100 sealed
ERROR_SHOWS = 40
LABEL_ROWS = 50
CORRECTIONS = 50
DETECTOR_MAX = 10  # of the marked half; the phrase probe fills the rest
TOP = 5
ARMS = ("noblivion", "stock_s2")

PROMPT_CHARS = 500
NOTE_CHARS = 300
BEFORE_CHARS = 200
NAMED_FILE_CHARS = 2000  # the same reach as the planned prompt-file recall
PROMPT_KEEP_CHARS = 20000

# The stock simulation. These values are frozen: they are the published stock
# limits and the rules of the test plan, and they are not tuned on this set.
INDEX_LINES = 200
INDEX_BYTES = 25000
LINKED_FILES = 3
SEARCH_FILES = 5

FIELD = "@@"
LETTERS = "abcdefghij"  # one per note of a pool: 2 x TOP at most
GROUPS = "groups.jsonl"
LABEL_FILE = "labels.md"
LABELS = "labels.jsonl"
MANIFEST = "manifest.json"
KEYS = {"dev": "key-dev.json", "sealed": "key-sealed.json"}
TASK_KEY = "key-tasks.json"
SEALED_LOG = "sealed-opened.jsonl"
DB_FILE = "noblivion.db"
COPY_FILES = ("config.json",)  # an allowlist: never the token, a log or a lock
HUB_FILE = Path("cache") / "hub-stats.json"
STORE_WAIT_S = 120

PARTS = (
    ("prompt", "Part 1: prompts. A note helps if it is useful for the work this prompt asks for."),
    ("error", "Part 2: tool errors. A note helps if it is useful for this error."),
    ("label", "Part 3: tool calls. y = this note fits this tool call."),
    ("correction", "Part 4: y = this line corrects what the assistant did."),
)

STOP = frozenset(
    "about above after again also been before being both could does doing done down each "
    "every from have having here into just like made make many more most much must need "
    "only other over same should since some such than that their them then there these "
    "they this those through under until upon very want were what when where which while "
    "will with would your".split()
)
WORD_RE = re.compile(r"[^\W\d_]{4,}")
LINK_RE = re.compile(r"\]\(([^()\s#]+\.md)\)")
PATH_RE = re.compile(r"^\s*[\"'`]?((?:~|/)[^\s\"'`]+\.(?:md|txt))(?![\w.])")
PRIVATE_NAME_RE = re.compile(r"credential|secret|token", re.I)
ROW_ID_RE = re.compile(r"^- \[id (\d+)\]")
FIELD_RE = re.compile(r"^Answer:\s*(?P<value>.*?)\s*#(?P<id>[0-9a-f]{8})\s*$")
MARK_RE = re.compile(r"\[[a-z_]+:\s*[^\]\s]+\]")
NOTIFICATION_RE = re.compile(r"<(summary|result)>(.*?)</\1>", re.S)
PROBE_RE = re.compile(
    r"\b(?:i said|i told|i asked|do not|don't|never|why did you|wrong|not what|stop|"
    r"you (?:missed|forgot|ignored))\b|^\s*no\b",
    re.I,
)


class SealedError(RuntimeError):
    """A read of the sealed split without the explicit flag."""


# -- plain helpers ------------------------------------------------------------------


def _load(path: Path, alias: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _digest(seed: int, *parts: str) -> str:
    return hashlib.sha256("\0".join((str(seed), *parts)).encode("utf-8")).hexdigest()


def _rng(seed: int, purpose: str) -> random.Random:
    """One stream per purpose, so that one more class leaves the others as they are."""
    return random.Random(_digest(seed, purpose))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def safe(text: str) -> str:
    """``text`` on one line with every covered secret masked, "" when redaction
    fails. Cut only after this: a cut can hide a secret from the patterns."""
    from noblivion.redaction import REDACTION_FAILED_TOKEN, redact_at_rest

    out = redact_at_rest(text)
    if out == REDACTION_FAILED_TOKEN:
        return ""
    return re.sub(r"@{2,}", "@", " ".join(out.split()))


def norm(text: str) -> str:
    """The form two prompts share when they are the same prompt."""
    return " ".join(re.sub(r"\W+", " ", text.lower()).split())[:200]


def content_words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOP}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def append_private(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def private_dir(path: Path) -> Path:
    """The output folder, mode 0700. Real text must never land in a repository."""
    path = path.expanduser().resolve()
    if any((p / ".git").exists() for p in (path, *path.parents)):
        raise SystemExit(f"{path} is in a git work tree: use a folder outside the repository")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def harden(out: Path) -> None:
    for root, dirs, files in os.walk(out):
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o700)
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):  # a model file links a blob in this tree
                os.chmod(path, 0o600)


def wilson(hits: int, n: int, z: float = 1.96) -> list[float]:
    if not n:
        return [0.0, 0.0]
    p = hits / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(mid - half, 3), round(mid + half, 3)]


def share(hits: int, n: int) -> dict[str, Any]:
    value = round(hits / n, 3) if n else None
    return {"hits": hits, "n": n, "share": value, "ci95": wilson(hits, n)}


def alive_at(
    births: Mapping[str, _dt.datetime] | None, moment: _dt.datetime
) -> Callable[[str], bool]:
    """The test for "this note file existed at ``moment``"."""
    return lambda name: not births or name not in births or births[name] <= moment


# -- the stock simulation -----------------------------------------------------------


def index_cut(text: str) -> str:
    """What stock loads of ``MEMORY.md``: the first 200 lines or 25 KB."""
    cut = "\n".join(text.split("\n")[:INDEX_LINES])
    return cut.encode("utf-8")[:INDEX_BYTES].decode("utf-8", errors="ignore")


def is_link_page(text: str) -> bool:
    """A topic page: at least half of its lines link a note. Stock reads it for its
    links. It is no note, and its size would win every word search."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and 2 * sum(1 for ln in lines if LINK_RE.search(ln)) >= len(lines)


class StockFolder:
    """Stock S2 on one project's memory folder, generous to stock.

    1. The index: ``MEMORY.md`` cut at 200 lines or 25 KB.
    2. Links: a note linked on a loaded line, or from a topic page a loaded line
       links, opens when its line shares a content word with the prompt. At most 3.
    3. Word search: each content word is searched as a substring in every file. The
       5 files that match the most distinct words open.
    4. A note the prompt names opens first.

    The result is what opened, by distinct words matched, cut to the top 5, so a
    weak link never pushes out a better search hit.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        raw = {p.name: read_text(p) for p in sorted(folder.glob("*.md")) if p.name != "MEMORY.md"}
        self.pages = {name for name, text in raw.items() if is_link_page(text)}
        self.texts = {n: t.lower() for n, t in raw.items() if not n.startswith("MEMORY")}
        self.texts = {n: t for n, t in self.texts.items() if n not in self.pages}
        self.lines: list[tuple[str, str]] = []
        for line in index_cut(read_text(folder / "MEMORY.md")).splitlines():
            for link in LINK_RE.findall(line):
                name = Path(link).name
                if name not in self.pages:
                    self.lines.append((name, line))
                    continue
                for sub in raw[name].splitlines():
                    self.lines.extend((Path(n).name, sub) for n in LINK_RE.findall(sub))

    def top(self, query: str, exists: Callable[[str], bool] = lambda name: True) -> list[str]:
        words = content_words(query)
        if not words:
            return []
        best: dict[str, int] = {}
        for name, line in self.lines:
            shared = len(words & content_words(line))
            if shared and name in self.texts and exists(name):
                best[name] = max(best.get(name, 0), shared)
        linked = sorted(best, key=lambda n: (-best[n], n))[:LINKED_FILES]
        hits = {
            name: n
            for name, text in self.texts.items()
            if exists(name) and (n := sum(1 for w in words if w in text))
        }
        found = sorted(hits, key=lambda n: (-hits[n], n))[:SEARCH_FILES]
        named = [n for n in self.texts if exists(n) and n.lower() in query.lower()]
        opened = set(linked) | set(found)
        ranked = sorted(opened - set(named), key=lambda n: (-hits.get(n, 0), n))
        return (sorted(named) + ranked)[:TOP]


# -- the sample ---------------------------------------------------------------------


@dataclass
class Prompt:
    """One candidate. ``text`` is raw: it reaches the hook and is never stored."""

    stratum: str
    text: str
    session: str
    project: str
    moment: _dt.datetime
    reply: str = ""


def strip_notification(text: str) -> str:
    parts = [body for _, body in NOTIFICATION_RE.findall(text)]
    return " ".join(parts) if parts else re.sub(r"<[^>]+>", " ", text)


def _notification(rec: Mapping[str, Any], signals: Any) -> str | None:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    text = signals._text_blocks((rec.get("message") or {}).get("content"))
    return (
        text if isinstance(text, str) and text.lstrip().startswith("<task-notification") else None
    )


def _agent_tasks(rec: Mapping[str, Any]) -> Iterator[str]:
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return
    content = (rec.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else ():
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") in ("Agent", "Task")
            and isinstance((block.get("input") or {}).get("prompt"), str)
        ):
            yield block["input"]["prompt"]


def scan(
    paths: Iterable[str],
    home_slug: str,
    rt: types.ModuleType,
    signals: Any,
    before: _dt.datetime | None,
) -> Iterator[Prompt]:
    """Every candidate of the main transcripts, oldest session first."""
    for path in paths:
        project = Path(path).parent.name
        session = Path(path).stem
        reply = ""
        for rec in rt.read_jsonl(path):
            moment = rt._iso(str(rec.get("timestamp") or ""))
            found: list[tuple[str, str]] = []
            if rec.get("type") == "assistant":
                reply = signals.reply_text(rec) or reply
                found = [("subagent_task", task) for task in _agent_tasks(rec)]
            elif (note := _notification(rec, signals)) is not None:
                found = [("task_notification", note)]
            elif (text := rt.prompt_text(rec, signals)) is not None:
                kind = "home" if project == home_slug else "project"
                found = [("file_path" if PATH_RE.match(text) else kind, text)]
            if moment is None or (before is not None and moment >= before):
                continue
            for stratum, text in found:
                keep = text[:PROMPT_KEEP_CHARS]
                yield Prompt(stratum, keep, session, project, moment, reply)


def pick_prompts(cands: Sequence[Prompt], seed: int) -> list[Prompt]:
    """``PER_STRATUM`` distinct prompts of each stratum. A short stratum stops the
    build: a smaller set would quietly change what the targets mean."""
    seen: set[str] = set()
    out: list[Prompt] = []
    for stratum in STRATA:
        pool = [c for c in cands if c.stratum == stratum]
        _rng(seed, f"pick:{stratum}").shuffle(pool)
        got = 0
        for c in pool:
            key = norm(c.text)
            if key in seen or not safe(c.text):
                continue
            seen.add(key)
            out.append(c)
            got += 1
            if got == PER_STRATUM:
                break
        if got < PER_STRATUM:
            raise SystemExit(f"stratum {stratum}: {got} prompts, {PER_STRATUM} needed")
    return out


def pick_corrections(
    cands: Sequence[Prompt], taken: Sequence[Prompt], seed: int, signals: Any
) -> list[tuple[Prompt, str, bool]]:
    """``(prompt, why, the detector marks it)`` for the correction class, disjoint
    from the 150 prompts."""
    seen = {norm(p.text) for p in taken}
    typed: list[Prompt] = []
    for c in cands:
        key = norm(c.text)
        if c.stratum in ("home", "project") and key not in seen and safe(c.text):
            seen.add(key)
            typed.append(c)
    _rng(seed, "corrections").shuffle(typed)
    marks = {id(c): bool(signals.is_correction(c.text)) for c in typed}
    hit = [c for c in typed if marks[id(c)]][:DETECTOR_MAX]
    half = CORRECTIONS // 2
    probe = [c for c in typed if not marks[id(c)] and PROBE_RE.search(c.text)][: half - len(hit)]
    used = {id(c) for c in (*hit, *probe)}
    rest = [c for c in typed if id(c) not in used][: CORRECTIONS - len(hit) - len(probe)]
    if len(hit) + len(probe) + len(rest) < CORRECTIONS:
        raise SystemExit(f"{len(typed)} typed prompts, {CORRECTIONS} needed for corrections")
    return (
        [(c, "detector", True) for c in hit]
        + [(c, "probe", False) for c in probe]
        + [(c, "random", marks[id(c)]) for c in rest]
    )


def split_prompts(prompts: Sequence[Prompt], seed: int) -> dict[int, str]:
    """``{id(prompt): "dev" | "sealed"}``: 10 dev per stratum, 50 in all. The whole
    of a session goes to one side, so a near copy of a dev prompt is not sealed."""
    by_session: dict[str, list[Prompt]] = defaultdict(list)
    for p in prompts:
        by_session[p.session].append(p)
    sessions = sorted(by_session)
    _rng(seed, "split").shuffle(sessions)
    room = Counter({s: DEV_PER_STRATUM for s in STRATA})
    dev: set[str] = set()
    for session in sessions:
        need = Counter(p.stratum for p in by_session[session])
        if all(room[s] >= n for s, n in need.items()):
            room.subtract(need)
            dev.add(session)
    left = sum(room.values())
    for session in sessions:
        size = len(by_session[session])
        if left and session not in dev and size <= left:
            dev.add(session)
            left -= size
    if left:
        raise SystemExit(f"cannot split by session: {left} dev places stay empty")
    return {id(p): "dev" if p.session in dev else "sealed" for p in prompts}


def read_log(path: Path, rt: types.ModuleType) -> list[dict[str, Any]]:
    return list(rt.read_jsonl(str(path))) if path.is_file() else []


def distinct_error_shows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct query and notes, with the number of shows."""
    shows: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for r in rows:
        ids = tuple(str(i) for i in r.get("ids") or ())
        if r.get("decision") != "show" or not ids or not isinstance(r.get("query"), str):
            continue
        key = (norm(r["query"]), ids)
        if key in shows:
            shows[key]["shows"] += 1
        else:
            shows[key] = {"query": r["query"], "ids": ids, "shows": 1}
    return list(shows.values())


def distinct_label_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One row per distinct tool call text and note."""
    pairs: dict[tuple[str, str], dict[str, str]] = {}
    for r in rows:
        labels = r.get("label_rows")
        if not isinstance(labels, dict) or not isinstance(r.get("text"), str):
            continue
        for name in labels:
            pairs.setdefault((norm(r["text"]), name), {"text": r["text"], "name": name})
    return list(pairs.values())


# -- the notes ----------------------------------------------------------------------


def plain_title(name: str) -> str:
    """``feedback_run_the_checks`` -> ``run the checks``: fewer words to read."""
    return re.sub(r"^(?:feedback|project|reference|user)\s+", "", re.sub(r"[_-]+", " ", name))


class Notes:
    """One text per note for both arms, so the shape of an item names no arm."""

    def __init__(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, root, path, source_type, content, COALESCE(index_text, '') "
                "FROM memories WHERE deleted_at IS NULL"
            ).fetchall()
        finally:
            conn.close()
        self.by_id: dict[int, dict[str, str]] = {}
        self.by_file: dict[tuple[str, str], dict[str, str]] = {}
        self.by_name: dict[str, dict[str, str]] = {}
        for mid, root, path, source, content, index_text in rows:
            head, _, body = content.partition("\n")
            head = head.lstrip("# ").strip()
            mined = source == "transcript_mined"
            card = {
                "key": f"{root}/{path}" if mined else Path(path).stem,
                "title": head if mined else plain_title(head),
                "text": index_text.partition("\n")[0] or MARK_RE.sub(" ", body),
            }
            self.by_id[mid] = card
            self.by_file[(root, path)] = card
            if not mined:
                self.by_name.setdefault(Path(path).stem, card)
                self.by_name.setdefault(head, card)

    def named(self, name: str) -> dict[str, str] | None:
        return self.by_name.get(name)

    def of_file(self, root: str, folder: Path, name: str) -> dict[str, str]:
        """A file of the memory folder. A note the store has not indexed is read
        from the file, with the rule or description line as its text."""
        card = self.by_file.get((root, name)) or self.by_name.get(Path(name).stem)
        if card is not None:
            return card
        text = read_text(folder / name)
        m = re.search(r"^(?:rule|description):\s*(.+)$", text, re.M)
        return {
            "key": Path(name).stem,
            "title": plain_title(Path(name).stem),
            "text": m.group(1) if m else text[:NOTE_CHARS],
        }


def note_line(card: Mapping[str, str]) -> str:
    return clip(safe(f"{card['title']}: {card['text']}"), NOTE_CHARS)


# -- the replay ---------------------------------------------------------------------


def clean_env(base: Mapping[str, str]) -> dict[str, str]:
    """The process env without what points a hook at the live install."""
    drop = ("CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR")
    return {k: v for k, v in base.items() if k not in drop}


def copy_store_inputs(data_dir: Path, target: Path) -> None:
    """The scratch data dir from an allowlist. The database comes through SQLite's
    backup from a read-only connection, so the live store is neither stopped nor
    written; a plain file copy could tear a database in use."""
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    src = sqlite3.connect((data_dir / DB_FILE).resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target / DB_FILE)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for name in COPY_FILES:
        if (data_dir / name).is_file():
            shutil.copyfile(data_dir / name, target / name)
    if (data_dir / "models").is_dir():
        shutil.copytree(data_dir / "models", target / "models", symlinks=True)
    harden(target)


@contextlib.contextmanager
def scratch_store(
    data_dir: Path, work: Path, build: Path, memory_dirs: Sequence[Path]
) -> Iterator[dict[str, str]]:
    """A store of the frozen build on a copy of the database. It has no
    ``learning.json``, so no learning worker runs and nothing writes to a memory
    folder. It stops with this block: the handle is ours, nothing else is signalled."""
    scratch = work / "data"
    copy_store_inputs(data_dir, scratch)
    env = clean_env(os.environ)
    env.update(
        {
            "NOBLIVION_DATA_DIR": str(scratch),
            "NOBLIVION_MEMORY_DIRS": ":".join(str(d) for d in memory_dirs),
            "NOBLIVION_INDEX_INTERVAL_S": "86400",
            "NOBLIVION_IDLE_EXIT_S": "86400",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if (build / "src").is_dir():
        env["PYTHONPATH"] = str(build / "src")
    with open(work / "store.out", "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "noblivion", "serve"],
            env=env,
            stdout=log,
            stderr=log,
            cwd=work,
        )
    try:
        wait_for_store(scratch, proc)
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)


def wait_for_store(data_dir: Path, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + STORE_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the scratch store exited with {proc.returncode}")
        with contextlib.suppress(OSError, ValueError, KeyError):
            port = json.loads(read_text(data_dir / "store.json"))["port"]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return
        time.sleep(0.2)
    raise SystemExit("the scratch store did not answer in time")


class Replay:
    """The build's own hooks, one prompt at a time. The hook fails open and prints
    nothing when the store fails, so ``check`` reads its log: an empty arm from a
    failed call would look like a clean miss and bias the pool."""

    def __init__(
        self,
        hooks: Path,
        env: Mapping[str, str],
        work: Path,
        projects: Path,
        births: Mapping[str, _dt.datetime] | None,
        rt: types.ModuleType,
        hub_file: Path | None = None,
    ) -> None:
        rp = _load(TOOLS / "replay_prompt_rows.py", "heldout_replay_rows")
        self.work = work
        self.projects = projects
        self.births = births
        self.hub_file = hub_file
        self.env = rp.arm_env(env, work, work / "arm", "")
        self.env.pop("NOBLIVION_MEMORY_DIR", None)
        self.env.update({"NOBLIVION_RECALL_LABELS": "0", "NOBLIVION_RECALL_TIMEOUT_S": "30"})
        self.cache = Path(self.env["NOBLIVION_RECALL_CACHE_DIR"])
        self.cache.mkdir(parents=True, exist_ok=True)
        saved = sys.modules.get("recall_hook")
        try:
            # The subagent hook takes this copy from sys.modules, with its filter.
            self.hook, self.exists = rp.filtered_hook(rt, hooks, "recall_hook")
            self.sub = _load(hooks / "subagent_rules_hook.py", "heldout_subagent_hook")
        finally:
            sys.modules.pop("recall_hook", None)
            if saved is not None:
                sys.modules["recall_hook"] = saved

    def _scope(self, project: str) -> dict[str, str]:
        env = dict(self.env)
        folder = self.projects / project / "memory"
        if folder.is_dir():
            env["NOBLIVION_MEMORY_DIR"] = str(folder)
        else:
            env["NOBLIVION_RECALL_ROOT"] = project
        return env

    def rows(self, prompt: Prompt, sid: str) -> list[int]:
        """The memory ids of the rows, best first, at most ``TOP``."""
        if self.hub_file is not None:
            shutil.copyfile(self.hub_file, self.cache / "hub-stats.json")
        self.exists["fn"] = alive_at(self.births, prompt.moment)
        env = self._scope(prompt.project)
        buf = io.StringIO()
        if prompt.stratum == "subagent_task":
            scoped = self.hook.session_env(env, {"session_id": sid})
            buf.write(
                self.sub._rules_for(prompt.text, "SubagentStart", sid, time.monotonic(), scoped)
            )
        else:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": sid,
                "prompt": prompt.text,
            }
            self.hook.main(io.StringIO(json.dumps(payload)), buf, env)
        ids = [int(m.group(1)) for m in map(ROW_ID_RE.match, buf.getvalue().splitlines()) if m]
        return ids[:TOP]

    def check(self) -> None:
        failed = 0
        for log in (self.cache / "recall.log", self.cache / "subagent" / "recall.log"):
            failed += read_text(log).count(" fail:")
        if failed:
            raise SystemExit(f"{failed} hook calls failed open: the pool is not valid")


# -- the set ------------------------------------------------------------------------


@dataclass
class Inputs:
    """What ``make_set`` works from."""

    prompts: Sequence[Prompt]
    errors: Sequence[Mapping[str, Any]]
    guards: Sequence[Mapping[str, Any]]
    projects: Path
    notes: Notes
    signals: Any
    seed: int = SEED
    home: Path = field(default_factory=Path.home)
    births: Mapping[str, _dt.datetime] | None = None


def named_file_head(prompt: str, home: Path) -> str:
    """The head of the file a prompt starts with. Stock opens a named file with its
    file tool, and a label needs to see what the bare path stood for."""
    m = PATH_RE.match(prompt)
    if m is None:
        return ""
    path = Path(m.group(1)).expanduser()
    with contextlib.suppress(OSError, ValueError):
        path = path.resolve()
        if path.is_relative_to(home.resolve()) and not PRIVATE_NAME_RE.search(path.name):
            return read_text(path)[:NAMED_FILE_CHARS]
    return ""


def shown_text(p: Prompt, home: Path) -> str:
    text = strip_notification(p.text) if p.stratum == "task_notification" else p.text
    head = named_file_head(p.text, home) if p.stratum == "file_path" else ""
    return clip(safe(f"{text} | file: {head}" if head else text), PROMPT_CHARS)


def stock_query(p: Prompt, home: Path) -> str:
    text = strip_notification(p.text) if p.stratum == "task_notification" else p.text
    return f"{text} {named_file_head(p.text, home)}"


def _items(seed: int, group: str, cards: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """One item per note. The order comes from the hash alone, so it names no arm."""
    items = [
        {"id": _digest(seed, group, c["key"])[:8], "note": c["key"], "line": note_line(c)}
        for c in cards
    ]
    return sorted(items, key=lambda it: _digest(seed, "order", group, it["note"]))


def pool_prompt(
    p: Prompt, group: str, rows: Sequence[int], stock: Sequence[str], inp: Inputs
) -> tuple[dict[str, Any], dict[str, list[str]], list[int]]:
    """The group of a prompt, its arms and the row ids no note answers for."""
    cards: dict[str, dict[str, str]] = {}
    arms: dict[str, list[str]] = {arm: [] for arm in ARMS}
    unknown: list[int] = []
    for mid in rows:
        card = inp.notes.by_id.get(mid)
        if card is None:
            unknown.append(mid)
            continue
        cards.setdefault(card["key"], card)
        if card["key"] not in arms["noblivion"]:
            arms["noblivion"].append(card["key"])
    folder = inp.projects / p.project / "memory"
    for name in stock:
        card = inp.notes.of_file(p.project, folder, name)
        cards.setdefault(card["key"], card)
        arms["stock_s2"].append(card["key"])
    text = shown_text(p, inp.home)
    doc = {
        "group": group,
        "cls": "prompt",
        "text": text,
        "items": _items(inp.seed, group, list(cards.values())),
    }
    return doc, arms, unknown


def make_set(
    inp: Inputs, rows_of: Callable[[Prompt, str], list[int]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """``(groups, keys per split, task key, pool stats)``."""
    seed = inp.seed
    prompts = pick_prompts(inp.prompts, seed)
    sides = split_prompts(prompts, seed)
    groups: list[dict[str, Any]] = []
    keys: dict[str, dict[str, Any]] = {"dev": {}, "sealed": {}}
    stats: dict[str, Any] = {"strata": {}, "unknown_rows": 0}
    folders: dict[str, StockFolder] = {}
    for p in prompts:
        group = _digest(seed, "group", p.stratum, norm(p.text))[:12]
        if p.project not in folders:
            folders[p.project] = StockFolder(inp.projects / p.project / "memory")
        stock = folders[p.project].top(stock_query(p, inp.home), alive_at(inp.births, p.moment))
        doc, arms, unknown = pool_prompt(p, group, rows_of(p, f"heldout-{group}"), stock, inp)
        groups.append(doc)
        keys[sides[id(p)]][group] = {"stratum": p.stratum, "arms": arms}
        one = stats["strata"].setdefault(
            p.stratum,
            {"prompts": 0, "items": 0, "noblivion": 0, "stock_s2": 0, "both": 0, "empty": 0},
        )
        one["prompts"] += 1
        one["items"] += len(doc["items"])
        one["noblivion"] += len(arms["noblivion"])
        one["stock_s2"] += len(arms["stock_s2"])
        one["both"] += len(set(arms["noblivion"]) & set(arms["stock_s2"]))
        one["empty"] += 0 if doc["items"] else 1
        stats["unknown_rows"] += len(unknown)
    task_key = {}
    shows = distinct_error_shows(inp.errors)
    _rng(seed, "errors").shuffle(shows)
    got = 0
    for show in shows:
        cards = [c for c in map(inp.notes.named, show["ids"]) if c is not None]
        text = clip(safe(show["query"]), PROMPT_CHARS)
        if got == ERROR_SHOWS or not cards or not text:
            continue
        group = _digest(seed, "error", norm(show["query"]), *show["ids"])[:12]
        groups.append(
            {"group": group, "cls": "error", "text": text, "items": _items(seed, group, cards)}
        )
        task_key[group] = {"shows": show["shows"]}
        got += 1
    stats["error_groups"] = got
    rows = distinct_label_rows(inp.guards)
    _rng(seed, "labels").shuffle(rows)
    got = 0
    for row in rows:
        card = inp.notes.named(row["name"])
        text = clip(safe(row["text"]), PROMPT_CHARS)
        if got == LABEL_ROWS or card is None or not text:
            continue
        group = _digest(seed, "label", norm(row["text"]), row["name"])[:12]
        groups.append(
            {"group": group, "cls": "label", "text": text, "items": _items(seed, group, [card])}
        )
        task_key[group] = {}
        got += 1
    stats["label_groups"] = got
    for c, why, marked in pick_corrections(inp.prompts, prompts, seed, inp.signals):
        group = _digest(seed, "correction", norm(c.text))[:12]
        line = clip(safe(c.text), PROMPT_CHARS)
        before = safe(c.reply)[-BEFORE_CHARS:]
        item = {"id": _digest(seed, group)[:8], "note": "", "line": line}
        groups.append(
            {"group": group, "cls": "correction", "text": f"Before: {before}", "items": [item]}
        )
        task_key[group] = {"why": why, "detector": marked}
    for g in groups:
        g["field"] = _digest(seed, "field", g["group"])[:8]
    ids = Counter(it["id"] for g in groups for it in g["items"])
    ids.update(g["field"] for g in groups)
    if ids and max(ids.values()) > 1:
        raise SystemExit("two items share an id: use another seed")
    return groups, keys, task_key, stats


def render(groups: Sequence[Mapping[str, Any]], out: Path, seed: int) -> str:
    """``labels.md``: one block per group with notes. Markdown, because VS Code wraps
    its lines by default and shows no word suggestions there. The field is one
    token that the text cannot hold, so Find Next walks from field to field, and a
    typed key replaces the selected field."""
    tool = Path(__file__).resolve()
    lines = [
        "# Held-out labels",
        "",
        "1. Press Ctrl+F, type two @ signs, press Esc. On a Mac use Cmd+G for F3.",
        "2. Press F3 to select the next empty field. Type your answer. Press F3 again.",
        "3. Several notes: type the letters of those that help, like ac, or n if none helps."
        " One note: type y or n.",
        f"4. Press Ctrl+S. Run: python3 {tool} status --out {out}",
        f"5. When 0 are left, run: python3 {tool} import --out {out}",
    ]
    for cls, heading in PARTS:
        part = [g for g in groups if g["cls"] == cls and g["items"]]
        part.sort(key=lambda g: _digest(seed, "order", g["group"]))
        lines += ["", f"## {heading}"]
        for n, g in enumerate(part, 1):
            lines += ["", f"### {n} of {len(part)}", "", f"> {g['text'] or '(no text)'}", ""]
            for letter, item in zip(LETTERS, g["items"], strict=False):
                lines.append(f"{letter}. {item['line']}")
            lines += ["", f"Answer: {FIELD} #{g['field']}"]
    return "\n".join(lines) + "\n"


def write_set(
    out: Path,
    groups: Sequence[Mapping[str, Any]],
    keys: Mapping[str, Mapping[str, Any]],
    task_key: Mapping[str, Any],
    manifest: Mapping[str, Any],
    seed: int,
) -> None:
    write_private(out / GROUPS, "".join(json.dumps(g) + "\n" for g in groups))
    for side, name in KEYS.items():
        write_private(out / name, json.dumps(keys[side]))
    write_private(out / TASK_KEY, json.dumps(task_key))
    write_private(out / MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
    write_private(out / LABEL_FILE, render(groups, out, seed))
    harden(out)


def counts_of(groups: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    out = {cls: {"groups": 0, "items": 0} for cls, _ in PARTS}
    for g in groups:
        out[g["cls"]]["groups"] += 1
        out[g["cls"]]["items"] += len(g["items"])
    return out


def build(args: argparse.Namespace) -> int:
    out = private_dir(Path(args.out))
    if (out / GROUPS).exists():
        raise SystemExit(f"{out} already holds a set: the set is built once")
    os.umask(0o077)
    projects = Path(args.projects).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()
    build_dir = Path(args.build_dir).expanduser().resolve() if args.build_dir else REPO
    hooks = build_dir / "hooks"
    rt = _load(TOOLS / "replay_transcripts.py", "heldout_transcripts")
    signals = _load(hooks / "trust_signals.py", "heldout_trust_signals")
    before = None
    if args.before:
        before = _dt.datetime.fromisoformat(args.before).replace(tzinfo=_dt.timezone.utc)
    paths = rt.main_transcripts([str(projects / "*" / "*.jsonl")])
    home_slug = str(Path.home()).replace("/", "-").replace(".", "-")
    prompts = list(scan(paths, home_slug, rt, signals, before))
    memory_dirs = sorted(p for p in projects.glob("*/memory") if p.is_dir())
    names = {p.name for folder in memory_dirs for p in folder.glob("*.md")}
    births = rt.note_births(rt.all_transcripts(paths), names)
    work = out / "scratch"
    work.mkdir(mode=0o700, exist_ok=True)
    try:
        with scratch_store(data_dir, work, build_dir, memory_dirs) as env:
            hub = data_dir / HUB_FILE
            replay = Replay(hooks, env, work, projects, births, rt, hub if hub.is_file() else None)
            inputs = Inputs(
                prompts=prompts,
                errors=read_log(data_dir / "error-recall-log.jsonl", rt),
                guards=read_log(data_dir / "guard-log.jsonl", rt),
                projects=projects,
                notes=Notes(work / "data" / DB_FILE),
                signals=signals,
                seed=args.seed,
                births=births,
            )
            groups, keys, task_key, stats = make_set(inputs, replay.rows)
            replay.check()
    finally:
        if not args.keep_scratch:
            shutil.rmtree(work, ignore_errors=True)
    manifest = {
        "seed": args.seed,
        "before": args.before or "",
        "build": args.build_id or "",
        "made": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "candidates": dict(Counter(p.stratum for p in prompts)),
        "counts": counts_of(groups),
        "pool": stats,
    }
    write_set(out, groups, keys, task_key, manifest, args.seed)
    print(json.dumps({"label_file": str(out / LABEL_FILE), **manifest}, indent=1, sort_keys=True))
    return 0


# -- labels -------------------------------------------------------------------------


def load_groups(out: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in read_text(out / GROUPS).splitlines() if ln.strip()]


def parse_answer(value: str, size: int) -> list[str] | None:
    """One y or n per note of a block of ``size`` notes, or None for a bad answer."""
    if value == "n":
        return ["n"] * size
    if value == "y" and size == 1:
        return ["y"]
    letters = LETTERS[:size]
    if value and set(value) <= set(letters) and len(set(value)) == len(value):
        return ["y" if c in value else "n" for c in letters]
    return None


def read_fields(
    path: Path, groups: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, str], set[str], list[int]]:
    """``(label per item, fields done, numbers of the lines with a bad field)``. An
    empty field is neither done nor bad: the block is left."""
    by_field = {g["field"]: g for g in groups if g["items"]}
    labels: dict[str, str] = {}
    done: set[str] = set()
    bad: list[int] = []
    seen: set[str] = set()
    for no, line in enumerate(read_text(path).splitlines(), 1):
        m = FIELD_RE.match(line)
        if m is None or m.group("id") not in by_field:
            continue
        field_id = m.group("id")
        items = by_field[field_id]["items"]
        value = re.sub(r"[\s,]+", "", m.group("value")).lower()
        if field_id in seen:
            bad.append(no)
            done.discard(field_id)
            for item in items:
                labels.pop(item["id"], None)
            continue
        seen.add(field_id)
        if value in ("", FIELD):
            continue
        answer = parse_answer(value, len(items))
        if answer is None:
            bad.append(no)
            continue
        done.add(field_id)
        labels.update((item["id"], one) for item, one in zip(items, answer, strict=True))
    return labels, done, bad


BAD_FIELDS = "bad fields on lines: {} (use letters of the block, or y or n)"


def status(args: argparse.Namespace) -> int:
    """Counts only: no text, no metric, no split."""
    out = Path(args.out).expanduser()
    groups = load_groups(out)
    _labels, done, bad = read_fields(out / LABEL_FILE, groups)
    total = filled = 0
    for cls, _ in PARTS:
        fields = [g["field"] for g in groups if g["cls"] == cls and g["items"]]
        hit = sum(1 for f in fields if f in done)
        total += len(fields)
        filled += hit
        print(f"{cls:<11} {len(fields):>4} blocks {hit:>4} done {len(fields) - hit:>4} left")
    print(f"{'all':<11} {total:>4} blocks {filled:>4} done {total - filled:>4} left")
    if bad:
        print(BAD_FIELDS.format(", ".join(map(str, bad[:20]))))
    return 1 if bad else 0


def import_labels(args: argparse.Namespace) -> int:
    """Each item keeps one label. A changed label needs ``--relabel``."""
    out = Path(args.out).expanduser()
    groups = load_groups(out)
    known = {it["id"] for g in groups for it in g["items"]}
    labels, _done, bad = read_fields(out / LABEL_FILE, groups)
    old = load_labels(out)
    changed = sorted(i for i, v in labels.items() if i in old and old[i] != v)
    if changed and not args.relabel:
        print(f"{len(changed)} labels differ from the import before. Use --relabel to change them.")
        return 1
    merged = {**old, **labels}
    write_private(
        out / LABELS,
        "".join(json.dumps({"id": i, "label": merged[i]}) + "\n" for i in sorted(merged)),
    )
    print(f"imported {len(labels)} labels, {len(known) - len(merged)} items left")
    if bad:
        print(BAD_FIELDS.format(", ".join(map(str, bad[:20]))))
    return 1 if bad else 0


def load_labels(out: Path) -> dict[str, str]:
    rows = (json.loads(ln) for ln in read_text(out / LABELS).splitlines() if ln.strip())
    return {r["id"]: r["label"] for r in rows}


def load_split(
    out: Path, split: str = "dev", open_sealed: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(groups, key)`` of one split. Tuning code calls this with the default."""
    if split == "sealed" and not open_sealed:
        raise SealedError("the sealed split stays closed until the long-run test")
    key = json.loads(read_text(Path(out) / KEYS[split]))
    return [g for g in load_groups(Path(out)) if g["group"] in key], key


def arm_metrics(
    groups: Sequence[Mapping[str, Any]], key: Mapping[str, Any], labels: Mapping[str, str], arm: str
) -> dict[str, Any]:
    """Delivery: of the prompts with a relevant note in the pool, the share where
    the arm holds one. Precision: the share of the arm's rows that are relevant.
    False delivery: of the prompts with no relevant note, the share where the arm
    shows a row."""
    shown = good = pos = hit = neg = false = 0
    for g in groups:
        relevant = {it["note"] for it in g["items"] if labels[it["id"]] == "y"}
        rows = key[g["group"]]["arms"][arm]
        shown += len(rows)
        good += sum(1 for n in rows if n in relevant)
        if relevant:
            pos += 1
            hit += 1 if relevant & set(rows) else 0
        else:
            neg += 1
            false += 1 if rows else 0
    return {
        "delivery": share(hit, pos),
        "precision": share(good, shown),
        "false_delivery": share(false, neg),
    }


def task_metrics(
    groups: Sequence[Mapping[str, Any]], task_key: Mapping[str, Any], labels: Mapping[str, str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cls in ("error", "label"):
        ids = [
            it["id"] for g in groups if g["cls"] == cls for it in g["items"] if it["id"] in labels
        ]
        out[f"{cls}_precision"] = share(sum(1 for i in ids if labels[i] == "y"), len(ids))
    marked = yes = both = 0
    for g in groups:
        if g["cls"] != "correction" or g["items"][0]["id"] not in labels:
            continue
        is_yes = labels[g["items"][0]["id"]] == "y"
        detector = bool(task_key[g["group"]].get("detector"))
        marked += detector
        yes += is_yes
        both += detector and is_yes
    out["detector_precision"] = share(both, marked)
    out["detector_recall_enriched_sample"] = share(both, yes)
    return out


def report(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    try:
        groups, key = load_split(out, args.split, args.open_sealed)
    except SealedError as exc:
        print(f"refused: {exc}. To open it give --split sealed --open-sealed.", file=sys.stderr)
        return 2
    labels = load_labels(out)
    left = sum(1 for g in groups for it in g["items"] if it["id"] not in labels)
    if left:
        print(f"refused: {left} items of the {args.split} split have no label", file=sys.stderr)
        return 2
    if args.split == "sealed":
        stamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        append_private(out / SEALED_LOG, json.dumps({"opened": stamp}))
    doc: dict[str, Any] = {
        "split": args.split,
        "prompts": len(groups),
        "arms": {arm: arm_metrics(groups, key, labels, arm) for arm in ARMS},
    }
    if args.split == "dev":
        task_key = json.loads(read_text(out / TASK_KEY))
        doc["tasks"] = task_metrics(load_groups(out), task_key, labels)
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("build", help="sample, pool and write the set")
    one.add_argument("--projects", default="~/.claude/projects")
    one.add_argument("--data-dir", default="~/.claude/plugins/data/noblivion-noblivion")
    one.add_argument("--build-dir", help="checkout of the frozen build (default: this repository)")
    one.add_argument("--build-id", help="the commit of that build, for the manifest")
    one.add_argument("--seed", type=int, default=SEED)
    one.add_argument("--before", help="only prompts before this date, YYYY-MM-DD")
    one.add_argument("--keep-scratch", action="store_true", help="keep the scratch store files")
    one.set_defaults(run=build)
    two = sub.add_parser("status", help="blocks done and left")
    two.set_defaults(run=status)
    three = sub.add_parser("import", help="read labels.md into labels.jsonl")
    three.add_argument("--relabel", action="store_true", help="allow a changed label")
    three.set_defaults(run=import_labels)
    four = sub.add_parser("report", help="delivery, precision and false delivery")
    four.add_argument("--split", choices=tuple(KEYS), default="dev")
    four.add_argument("--open-sealed", action="store_true", help="really open the sealed split")
    four.set_defaults(run=report)
    for cmd in (one, two, three, four):
        cmd.add_argument("--out", required=True, help="the private folder of the set")
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
