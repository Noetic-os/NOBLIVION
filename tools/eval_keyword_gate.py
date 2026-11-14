# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the keyword-only relevance gate of the prompt index (NOBLIVION-153).

The prompt index (``/api/memories/index``) of a store with no embedder ranks by
keyword and then runs ``ranking.keyword_gate``. This script calls the same rank
(``RankIndex.rank`` with ``gate=True``, as ``Ranker.search`` does) on a pool of
thousands of units and counts:

- off-topic: the 20 prompts of ``tools/eval/offtopic_prompts.json``. A prompt
  passes when it gets 0 rows;
- on-topic: prompts with a known target note. A prompt passes when the target
  is in the top 5 rows. Each set is also run with the gate off, for reference;
- rows: the mean number of rows per on-topic prompt (gate on).

Two pools, and both can run in one call:

``--fixture DIR`` the scale fixture of ``tools/eval/scale_fixture.py`` at
    ``--size`` notes (default 5000), indexed into a new database under
    ``--work``. The on-topic sets are the V1 (exact wording), V2 (paraphrase)
    and V3 (vague) queries of the lessons whose note is in the session project,
    the label prompts, the topic of each lesson alone (``topic``), and a short
    prompt on the flag of the lesson (``what does --flag do?``, the one-rare-word
    rule) alone and with one or two words that no note holds (``flag_unknown1``,
    ``flag_unknown2``).
    The pool is the session project. Synthetic text: safe to share.

``--db FILE`` a copy of a store database (never the live one: copy it first,
    the script opens it read only). The pool is ``--root`` plus ``--shared``,
    as the hook asks for it. The on-topic sets are made from the notes of
    ``--root`` with a fixed seed: ``desc`` (the description line of the note)
    and ``title`` (the words of the file name). The script prints counts, never
    note text.

``--thresholds a,b,c`` repeats the run with ``ranking.GATE_MIN_SCORE_SHARE`` set to
each value (a sweep, for choosing the constant). ``--dump FILE`` writes one JSON
line per prompt with the prompt and the gate inputs, for analysis. With ``--db``
the prompts are note text: write the dump next to the copy, not in the repo.

No model call and no network. Usage:

    uv run python tools/eval_keyword_gate.py --fixture DIR --work DIR \\
        [--db COPY --root -home-user --shared -home-user] [--thresholds 0,0.3]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from noblivion import bm25, config, db, indexer, inverted, labels, ranking  # noqa: E402

PROJECT = "claude_code"
TOP_K = 30  # recall.index_k of the shipped config
OFFTOPIC = ROOT / "tools" / "eval" / "offtopic_prompts.json"
_UNKNOWN = ("qzorbl", "vexmuth")  # words in no note of the fixture
_NOTE_RE = re.compile(r"^(feedback|project|reference|user)_.+\.md$")


def offtopic_prompts() -> list[str]:
    return list(json.loads(OFFTOPIC.read_text(encoding="utf-8"))["prompts"])


class Pool:
    """One pool of one store: the rank of the prompt index, gate on or off."""

    def __init__(
        self, conn: sqlite3.Connection, root: str, shared: Sequence[str], mined: bool = False
    ) -> None:
        self.index = ranking.RankIndex(None)
        self.index.refresh(conn)
        self.root = root
        self.shared = tuple(shared)
        self.mined = mined
        key = self.index.pool_key(PROJECT, root, self.shared, mined)
        units, self.view, self.n_files = self.index._pool(self.index._snap, key)
        self.units = len(units)

    def rank(self, query: str, gate: bool) -> ranking.RankResult:
        return self.index.rank(
            query,
            project=PROJECT,
            top_k=TOP_K,
            root=self.root,
            shared_roots=self.shared,
            include_mined=self.mined,
            gate=gate,
        )

    def features(self, query: str, ranked: ranking.RankResult) -> dict[str, Any]:
        """The inputs of the gate for the top row of the ungated answer."""
        view = self.view
        if not ranked.hits or not isinstance(view, inverted.PoolView):
            return {}
        tokens = bm25.tokenize(query)
        terms = view.real_terms(tokens)
        terms = {w for w in terms if w not in bm25.STOP_WORDS} or terms
        top = ranked.hits[0].row
        cap = view.n // 2
        informative = min(view.df_cut(ranking.GATE_INFORMATIVE_MASS), cap)
        rare = min(view.df_cut(ranking.GATE_RARE_MASS), cap)
        named = set(tokens)
        return {
            "label_path": bool(top.labels) and ranking.label_boost(top.labels, named) > 0,
            "rare_path": all(view.unit_has(top.slot, w) and view.df[w] <= rare for w in terms),
            "terms": len(terms),
            "shared": sum(1 for w in terms if view.unit_has(top.slot, w)),
            "informative": sum(
                1 for w in terms if view.unit_has(top.slot, w) and view.df[w] <= informative
            ),
            "unknown": len({w for w in tokens if w not in bm25.STOP_WORDS and not view.df[w]}),
            "bm25": ranked.hits[0].bm25,
            "share": round(ranking.score_share(view, top, terms, tokens), 3),
        }


def run_sets(
    pool: Pool, offtopic: Sequence[str], ontopic: dict[str, list[tuple[str, Any]]], match, dump
) -> dict[str, Any]:
    off_zero = 0
    for prompt in offtopic:
        gated = pool.rank(prompt, True)
        off_zero += not gated.hits
        if dump is not None:
            dump.append(
                {
                    "set": "offtopic",
                    "prompt": prompt,
                    "rows": len(gated.hits),
                    **pool.features(prompt, pool.rank(prompt, False)),
                }
            )
    out: dict[str, Any] = {"offtopic_zero_rows": f"{off_zero}/{len(offtopic)}"}
    for name, items in ontopic.items():
        top5 = top5_off = rows = 0
        for prompt, target in items:
            gated = pool.rank(prompt, True)
            ungated = pool.rank(prompt, False)
            hit_on = any(match(h.row, target) for h in gated.hits[:5])
            top5 += hit_on
            top5_off += any(match(h.row, target) for h in ungated.hits[:5])
            rows += len(gated.hits)
            if dump is not None:
                dump.append(
                    {
                        "set": name,
                        "prompt": prompt,
                        "rows": len(gated.hits),
                        "top5": hit_on,
                        "top5_off": any(match(h.row, target) for h in ungated.hits[:5]),
                        **pool.features(prompt, ungated),
                    }
                )
        n = len(items)
        out[name] = {
            "top5": f"{top5}/{n}",
            "top5_gate_off": f"{top5_off}/{n}",
            "rows_per_prompt": round(rows / n, 1) if n else 0.0,
        }
    return out


# -- the scale fixture ----------------------------------------------------------------------


def fixture_pools(fixture: Path, size: int, work: Path) -> tuple[sqlite3.Connection, dict]:
    scale = fixture / f"scale-{size:05d}"
    meta = json.loads((scale / "scale.json").read_text(encoding="utf-8"))
    dbfile = work / f"fixture-{size}-labels.db"
    fresh = not dbfile.exists()
    conn = db.open_db(dbfile, create=True)
    if fresh:
        dirs = sorted(scale.glob("shard-00/projects/*/memory"))
        # The labels of the store (file names, tickets), so the label rule of the gate runs.
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        labeller = labels.make_labeller(config.load_settings(env), env)
        indexer.scan(conn, dirs, project=PROJECT, labeller=labeller)
    return conn, meta


def fixture_ontopic(fixture: Path, meta: dict) -> dict[str, dict[str, list[tuple[str, Any]]]]:
    """Per session root: {set name: [(prompt, target file name)]}."""
    lessons = meta["lessons"]
    topics = {
        x["id"]: x["topic"]
        for x in json.loads((fixture / "lessons.json").read_text(encoding="utf-8"))
    }
    flags = {
        x["id"]: x.get("flag", "")
        for x in json.loads((ROOT / "tools/eval/scale_fixture_lessons.json").read_text())
    }
    slug = {p["name"]: p["slug"] for s in meta["shards_detail"] for p in s["projects"]}
    by_root: dict[str, dict[str, list[tuple[str, Any]]]] = {}
    for line in (fixture / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        lesson = lessons.get(q.get("lesson", ""))
        if q["variant"] not in ("V1", "V2", "V3", "LABEL") or lesson is None:
            continue
        if q["variant"] == "LABEL" and (q["expect"] != "label_row" or "prompt" not in q):
            continue
        if lesson["note_project"] != lesson["session_project"]:
            continue  # the note is in another project: not in this pool
        root = slug[lesson["session_project"]]
        sets = by_root.setdefault(root, {})
        sets.setdefault(q["variant"], []).append((q["prompt"], lesson["target"]))
        if q["variant"] == "V1":
            # The topic of the lesson alone ("queue worker drain"): a short prompt.
            sets.setdefault("topic", []).append((topics[q["lesson"]], lesson["target"]))
            if flags.get(q["lesson"]):
                # One rare word and stop words: the short prompt of the rare-word rule.
                prompt = f"what does {flags[q['lesson']]} do?"
                sets.setdefault("flag", []).append((prompt, lesson["target"]))
                # The same with one and two words that no note holds (a name, a typo).
                for k in (1, 2):
                    more = f"{prompt[:-1]} {' '.join(_UNKNOWN[:k])}?"
                    sets.setdefault(f"flag_unknown{k}", []).append((more, lesson["target"]))
    return by_root


def run_fixture(args, dump) -> dict[str, Any]:
    conn, meta = fixture_pools(args.fixture, args.size, args.work)
    by_root = fixture_ontopic(args.fixture, meta)
    totals: dict[str, list[int]] = {}
    off_zero = off_n = units = 0
    by_root_off: dict[str, str] = {}
    for root, sets in sorted(by_root.items()):
        pool = Pool(conn, root, ())
        units = max(units, pool.units)
        res = run_sets(
            pool,
            offtopic_prompts(),
            sets,
            lambda row, t, r=root: row.root == r and row.path.endswith(t),
            dump,
        )
        by_root_off[root] = res.pop("offtopic_zero_rows")
        a, b = by_root_off[root].split("/")
        off_zero, off_n = off_zero + int(a), off_n + int(b)
        for name, v in res.items():
            t = totals.setdefault(name, [0, 0, 0, 0.0])
            hit, n = map(int, v["top5"].split("/"))
            t[0] += hit
            t[1] += n
            t[2] += int(v["top5_gate_off"].split("/")[0])
            t[3] += v["rows_per_prompt"] * n
    conn.close()
    return {
        "pool": f"scale fixture {args.size} notes, {len(by_root)} session roots, largest pool"
        f" {units} units",
        "offtopic_zero_rows": f"{off_zero}/{off_n}",
        "offtopic_zero_rows_by_root": by_root_off,
        **{
            name: {
                "top5": f"{t[0]}/{t[1]}",
                "top5_gate_off": f"{t[2]}/{t[1]}",
                "rows_per_prompt": round(t[3] / t[1], 1),
            }
            for name, t in sorted(totals.items())
        },
    }


# -- a copy of a real store -----------------------------------------------------------------


def store_ontopic(conn: sqlite3.Connection, root: str, n: int, seed: int) -> dict:
    rows = conn.execute(
        "SELECT path, content FROM memories WHERE project = ? AND root = ? AND source_type = ?"
        " AND deleted_at IS NULL AND archived_at IS NULL ORDER BY path",
        (PROJECT, root, db.SOURCE_MD),
    ).fetchall()
    notes = []
    for path, content in rows:
        name = Path(path).name
        if not _NOTE_RE.match(name):
            continue
        parts = content.split("\n\n")
        desc = parts[1].strip() if len(parts) > 2 and not parts[1].startswith("[") else ""
        if len(desc.split()) < 4:
            continue
        title = re.sub(r"_\d{4}_\d{2}_\d{2}$|_\d{8}$", "", name[:-3].split("_", 1)[1])
        notes.append((name, desc, title.replace("_", " ")))
    picked = random.Random(seed).sample(notes, min(n, len(notes)))
    return {
        "desc": [(d, name) for name, d, _ in picked],
        "title": [(t, name) for name, _, t in picked],
    }


def run_store(args, dump) -> dict[str, Any]:
    uri = "file:" + str(args.db.resolve()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    pool = Pool(conn, args.root, args.shared, args.mined)
    sets = store_ontopic(conn, args.root, args.n, args.seed)
    res = run_sets(
        pool,
        offtopic_prompts(),
        sets,
        lambda row, t: row.root == args.root and row.path.endswith(t),
        dump,
    )
    conn.close()
    return {"pool": f"store copy, {pool.n_files} files, {pool.units} units", **res}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--size", type=int, default=5000)
    ap.add_argument("--work", type=Path, help="a folder for the fixture database")
    ap.add_argument("--db", type=Path, help="a copy of a store database")
    ap.add_argument("--root", default="")
    ap.add_argument("--shared", action="append", default=[])
    ap.add_argument("--mined", action="store_true", help="the pool with mined rows")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=153)
    ap.add_argument("--thresholds", default="", help="comma-separated GATE_MIN_SCORE_SHARE values")
    ap.add_argument("--dump", type=Path)
    args = ap.parse_args(argv)
    if args.fixture and not args.work:
        ap.error("--fixture needs --work")
    values: Iterable[float | None] = (
        [float(v) for v in args.thresholds.split(",")] if args.thresholds else [None]
    )
    dump: list[dict] | None = [] if args.dump else None
    report = []
    for value in values:
        if value is not None:
            ranking.GATE_MIN_SCORE_SHARE = value
        entry: dict[str, Any] = {"threshold": getattr(ranking, "GATE_MIN_SCORE_SHARE", None)}
        if args.fixture:
            entry["fixture"] = run_fixture(args, dump)
        if args.db:
            entry["store"] = run_store(args, dump)
        report.append(entry)
        print(json.dumps(entry), flush=True)
    if args.dump and dump is not None:
        args.dump.write_text("".join(json.dumps(d) + "\n" for d in dump), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
