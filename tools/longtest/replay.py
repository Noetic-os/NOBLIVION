# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arms b and c: the planted lessons through every channel, on each build and on stock.

``run_shard`` sends the frozen queries of one shard through the worker of one build
and returns one row per (query, channel). ``run_stock`` does the same for S0, S1 and
S2. Detection is an exact match on the answer string of the lesson, so no judge is
needed. A row holds counts and ids only: no prompt, no note text.

Channels: ``prompt`` (rows on a prompt), ``pull`` (the recall tool), ``guard`` (the
tool-call hook), ``error`` (error recall), ``subagent``, ``continuity``.
The primary channel of a variant carries its claims; a secondary channel is kept for
diagnosis and carries none.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from . import stats
from .buildrunner import Harness, Worker, suite
from .fixtures import WORK, Fixture
from .stocksim import Stock, project_folders
from .util import summarize_ms

BENIGN_COMMANDS = (
    "ls -la",
    "git status",
    "git diff --stat",
    "pytest -q",
    "cat README.md",
    "pwd",
    "git log --oneline -5",
    "python3 -m json.tool data.json",
    "grep -rn TODO src",
    "npm run build",
    "make test",
    "docker ps",
    "echo done",
    "head -20 notes.txt",
    "cd src && ls",
    "git rebase origin/main",
    "df -h",
    "ps aux",
    "uname -a",
    "which python3",
)
BENIGN_ERRORS = (
    ("npm run build", "Exit code 1\nerror TS2304: Cannot find name 'widget' in src/app.ts"),
    ("pytest -q", "Exit code 1\nFAILED tests/test_basic.py::test_add - assert 3 == 4"),
    ("git push", "Exit code 1\nfatal: unable to access remote: could not resolve host"),
    ("make", "Exit code 2\nmake: *** No rule to make target 'all'.  Stop."),
)
PRIMARY = {
    "V1": "pull",
    "V2": "pull",
    "V3": "prompt",
    "V4": "prompt",
    "V5": "prompt",
    "V6": "prompt",
    "V7": "prompt",
    "CMD": "guard",
    "CRED": "guard",
    "ERR": "error",
    "SUB": "subagent",
    "CONT": "continuity",
}
SECONDARY = {"V1": ("prompt",), "V2": ("prompt",), "V3": ("pull",)}
STOCK_VARIANTS = ("V1", "V2", "V3", "V4", "V6")
NEGATIVE = ("none", "no_rows", "no_label_row")
CLAIM_NOTES = {
    "C11": "arm d (soak)",
    "C12": "dedup pairs: needs the P1-24 build",
    "C13": "arms a, b, c: characters per prompt, from every prompt row",
    "C14": "arm d (soak)",
    "C15": "arm a (live labels)",
    "C20": "needs the P1-18 build",
    "C21": "needs the P1-24 build",
    "C22": "guard-log replay (arm b)",
    "C23": "needs the P1-20 build",
    "C24": "arm d (latency benchmark)",
}


def ask_text(event: str, out: str) -> str:
    return suite().model_text(event, out)


def hit_rank(text: str, answer: str) -> tuple[bool, int | None]:
    """Is the answer in the text, and in which row (a line that starts with ``- ``)."""
    if answer not in text:
        return False, None
    rows = [ln for ln in text.splitlines() if ln.startswith("- ")]
    for rank, line in enumerate(rows, 1):
        if answer in line:
            return True, rank
    return True, None


def pointer_rank(text: str, answer: str, title: str) -> tuple[bool, int | None]:
    """Is the note named in the text, by its title (cut as a row cuts it) or by its
    answer string, and at which row."""
    marks = [m for m in (answer, title[:50].lower()) if m]
    low = text.lower()
    if not any(m.lower() in low for m in marks):
        return False, None
    for rank, line in enumerate((ln.lower() for ln in text.splitlines() if ln.startswith("- ")), 1):
        if any(m.lower() in line for m in marks):
            return True, rank
    return True, None


def base_row(build: str, size: int, shard: int, q: dict[str, Any], channel: str) -> dict[str, Any]:
    primary = PRIMARY.get(q["variant"]) == channel or q["variant"] in ("LABEL",)
    return {
        "build": build,
        "size": size,
        "shard": shard,
        "qid": q["id"],
        "variant": q["variant"],
        "spelling": q.get("spelling", ""),
        "lesson": q.get("lesson") or q.get("near_lesson", ""),
        "channel": channel,
        "claims": list(q.get("claims", [])) if primary else [],
        "expect": q.get("expect", ""),
    }


class Credentials:
    """The canary secret of a CRED lesson, put where its source says (git remote,
    pushurl, netrc or dotenv) in a scratch folder of the harness. The value comes
    from the fixture; the repository holds no secret-shaped string."""

    def __init__(self, harness: Harness) -> None:
        self.home = harness.home
        self.done: set[str] = set()

    def cwd_for(self, project: str) -> Path:
        return self.home / "work" / project

    def plant(self, row: dict[str, Any], project: str) -> Path:
        folder = self.cwd_for(project)
        key = f"{row['id']}:{project}"
        if key in self.done:
            return folder
        folder.mkdir(parents=True, exist_ok=True)
        source, line = row["source"], row["canary_line"]
        if source in ("git_remote", "git_pushurl"):
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
            name = "url" if source == "git_remote" else "pushurl"
            value = line.split("=", 1)[1].strip()
            subprocess.run(
                ["git", "-C", str(folder), "config", f"remote.origin.{name}", value], check=True
            )
            if name == "pushurl":
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(folder),
                        "config",
                        "remote.origin.url",
                        "https://git.example.com/mirror.git",
                    ],
                    check=True,
                )
        elif source == "netrc":
            path = self.home / ".netrc"
            path.write_text(line + "\n", encoding="utf-8")
            path.chmod(0o600)
        else:
            path = folder / ".env"
            path.write_text(line + "\n", encoding="utf-8")
            path.chmod(0o600)
        self.done.add(key)
        return folder


class ShardRun:
    """The queries of one shard through one build."""

    def __init__(
        self, fx: Fixture, size: int, shard: int, harness: Harness, worker: Worker, name: str
    ) -> None:
        self.fx, self.size, self.shard = fx, size, shard
        self.h, self.w, self.name = harness, worker, name
        self.creds = Credentials(harness)
        self.n = 0

    def sid(self, tag: str) -> str:
        self.n += 1
        return f"lt-{self.name}-{self.size}-{self.shard}-{tag}-{self.n}"

    def row(
        self, q: dict[str, Any], channel: str, text: str, ms: float, ok: bool, decision: str = ""
    ) -> dict[str, Any]:
        lesson = self.fx.lessons.get(self.fx.lesson_of(q), {})
        answer = lesson.get("answer", "")
        hit, rank = hit_rank(text, answer) if answer else (False, None)
        named, named_rank = pointer_rank(text, answer, lesson.get("title", ""))
        row = base_row(self.name, self.size, self.shard, q, channel)
        if channel == "pull":
            # The recall tool returns the leading hit in full and the others as
            # snippets, so a lower row can name the note without holding the answer.
            # Recall at k and MRR are retrieval metrics: a pointer counts.
            hit, rank = named, named_rank
        row.update(
            {
                "ok": ok,
                "hit": hit,
                "named": named,
                "rank": rank,
                "chars": len(text),
                "rows": suite().row_count(text),
                "ms": round(ms, 1),
                "decision": decision,
            }
        )
        if q.get("expect") in NEGATIVE:
            # a negative: any row on V5 or V7, the lesson row on a generic name. The
            # claim rate counts a correct silence as a hit; the fired flag is the
            # false delivery.
            fired = bool(text.strip()) if q["variant"] in ("V5", "V7") else row["hit"]
            row["fired"] = fired
            row["false_delivery"] = fired
            row["hit"] = not fired
        return row

    def prompt(self, q: dict[str, Any], cwd: str, tag: str = "p") -> dict[str, Any]:
        r = self.w.call("prompt", sid=self.sid(tag), cwd=cwd, prompt=self.fx.prompt_of(q))
        return self.row(q, "prompt", ask_text("UserPromptSubmit", r["out"]), r["ms"], r["ok"])

    def pull(self, q: dict[str, Any], cwd: str) -> dict[str, Any]:
        r = self.w.call("pull", sid=self.sid("r"), cwd=cwd, query=self.fx.prompt_of(q), k=5)
        return self.row(q, "pull", r["out"], r["ms"], r["ok"])

    def guard(
        self, q: dict[str, Any], cwd: str, command: str, tag: str = "g", project: str = ""
    ) -> tuple[dict[str, Any], str]:
        extra = {"project": project} if project else {}
        r = self.w.call(
            "guard",
            sid=self.sid(tag),
            cwd=cwd,
            tool="Bash",
            tool_input={"command": command},
            prompts=[q.get("prompt") or "x"],
            **extra,
        )
        st = suite()
        return (
            self.row(
                q,
                "guard",
                ask_text("PreToolUse", r["out"]),
                r["ms"],
                r["ok"],
                st.decision_of(r["out"]),
            ),
            r["out"],
        )

    def one(self, q: dict[str, Any]) -> list[dict[str, Any]]:
        fx, size, variant = self.fx, self.size, q["variant"]
        cwd = fx.cwd_of(size, q)
        out: list[dict[str, Any]] = []
        if variant in ("V1", "V2", "V3"):
            out += [self.pull(q, cwd), self.prompt(q, cwd)]
        elif variant in ("V4", "V5", "V6", "V7"):
            out.append(self.prompt(q, cwd))
        elif variant == "CMD":
            row, _ = self.guard(q, cwd, q["command"])
            row["hit"] = row["decision"] == "deny"
            out.append(row)
        elif variant == "CRED":
            lesson = fx.lessons[q["lesson"]]
            project = fx.scale(size)["lessons"][q["lesson"]]["session_project"]
            folder = self.creds.plant(lesson, project)
            row, raw = self.guard(
                q, str(folder), q["command"], project=f"{WORK}/{project}".replace("/", "-")
            )
            canary = lesson["canary"]
            row["hit"] = row["decision"] == "deny" and canary not in raw
            row["canary_leak"] = canary in raw
            out.append(row)
        elif variant == "ERR":
            r = self.w.call(
                "error",
                sid=self.sid("e"),
                cwd=cwd,
                command=q["command"],
                error=q["error"],
                prompts=["x"],
            )
            out.append(
                self.row(q, "error", ask_text("PostToolUseFailure", r["out"]), r["ms"], r["ok"])
            )
        elif variant == "SUB":
            r = self.w.call(
                "subagent",
                sid=self.sid("s"),
                cwd=cwd,
                tool_input=q["tool_input"],
                parent_prompt=q["tool_input"].get("prompt", "x"),
            )
            out.append(
                self.row(q, "subagent", ask_text("SubagentStart", r["out"]), r["ms"], r["ok"])
            )
        elif variant == "CONT":
            r = self.w.call("continuity", sid=self.sid("c"), cwd=cwd, steps=q["steps"])
            steps = json.loads(r["out"]) if r["ok"] and r["out"] else []
            last = ask_text("SessionStart", steps[-1]["out"]) if steps else ""
            out.append(self.row(q, "continuity", last, r["ms"], r["ok"]))
        elif variant == "LABEL":
            if q.get("command"):
                row, _ = self.guard(q, cwd, q["command"], tag="l")
                row["claims"] = list(q.get("claims", []))
                out.append(row)
            else:
                row = self.prompt(q, cwd, tag="l")
                row["claims"] = list(q.get("claims", []))
                out.append(row)
        return out

    def benign(self) -> list[dict[str, Any]]:
        """False deny: commands that no lesson forbids, in the first project."""
        cwd = f"{WORK}/" + next(iter(project_folders(self.h.config_dir))).replace("-work-", "", 1)
        rows = []
        for i, command in enumerate(BENIGN_COMMANDS):
            q = {"id": f"BENIGN-{i}", "variant": "BENIGN", "lesson": "", "claims": ["C5"]}
            r = self.w.call(
                "guard",
                sid=self.sid("b"),
                cwd=cwd,
                tool="Bash",
                tool_input={"command": command},
                prompts=["x"],
            )
            row = base_row(self.name, self.size, self.shard, q, "guard")
            row.update(
                {
                    "ok": r["ok"],
                    "decision": suite().decision_of(r["out"]),
                    "ms": r["ms"],
                    "chars": len(ask_text("PreToolUse", r["out"])),
                    "claims": [],
                }
            )
            row["false_deny"] = row["decision"] == "deny"
            rows.append(row)
        return rows


def run_shard(
    fx: Fixture,
    size: int,
    shard: int,
    harness: Harness,
    name: str,
    variants: Iterable[str] | None = None,
    limit_per_variant: int | None = None,
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """All the queries of one shard through one started harness."""
    queries = list(fx.queries_for(size, variants, shard))
    if limit_per_variant:
        seen: dict[str, int] = defaultdict(int)
        kept = []
        for q in queries:
            if seen[q["variant"]] < limit_per_variant:
                seen[q["variant"]] += 1
                kept.append(q)
        queries = kept
    rows: list[dict[str, Any]] = []
    with Worker(harness) as worker:
        run = ShardRun(fx, size, shard, harness, worker, name)
        for q in queries:
            for row in run.one(q):
                rows.append(row)
                if on_row:
                    on_row(row)
        for row in run.benign():
            rows.append(row)
            if on_row:
                on_row(row)
    return rows


def run_stock(
    fx: Fixture,
    size: int,
    shard: int,
    config_dir: Path,
    variants: Iterable[str] | None = None,
    limit_per_variant: int | None = None,
) -> list[dict[str, Any]]:
    """S0, S1 and S2 on the same queries. Stock reads the folder of the session's
    own project only; a V4 query runs in another project, so stock cannot find it."""
    folders = project_folders(config_dir)
    stocks: dict[str, Stock] = {}
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = defaultdict(int)
    wanted = set(variants) if variants else set(STOCK_VARIANTS)
    for q in fx.queries_for(size, wanted & set(STOCK_VARIANTS), shard):
        if limit_per_variant and seen[q["variant"]] >= limit_per_variant:
            continue
        seen[q["variant"]] += 1
        slug = fx.cwd_of(size, q).replace("/", "-")
        if slug not in stocks:
            stocks[slug] = Stock(folders[slug]) if slug in folders else None  # type: ignore[assignment]
        stock = stocks[slug]
        answer = fx.lessons[q["lesson"]]["answer"]
        prompt = fx.prompt_of(q)
        if q.get("task_file"):
            prompt += "\n" + (fx.root / q["task_file"]).read_text(encoding="utf-8")
        got = stock.delivered(prompt, answer) if stock else {"S0": False, "S1": False, "S2": False}
        for level, hit in got.items():
            row = base_row(level, size, shard, q, "stock")
            row.update(
                {
                    "ok": True,
                    "hit": hit,
                    "claims": list(q.get("claims", [])),
                    "index_tokens": stock.index_tokens() if stock else 0,
                }
            )
            rows.append(row)
    return rows


# -- analysis ---------------------------------------------------------------------


def rate_table(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``size|claim|build -> rate`` over the rows that carry a claim."""
    cells: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r.get("hit") is None or not r.get("claims"):
            continue
        for claim in r["claims"]:
            cells[f"{r['size']}|{claim}|{r['build']}"].append(bool(r["hit"]))
    return {k: stats.rate(sum(v), len(v)) for k, v in sorted(cells.items())}


def pair_compare(
    rows: Sequence[dict[str, Any]], first: str, second: str, resamples: int = 2000
) -> dict[str, dict[str, Any]]:
    """``size|claim -> paired difference first minus second`` with McNemar and the
    cluster bootstrap (cluster = lesson). Pairs are the same query in both arms."""
    index: dict[tuple, dict[str, bool]] = {}
    lesson_of: dict[tuple, str] = {}
    for r in rows:
        if r["build"] not in (first, second) or not r.get("claims") or r.get("hit") is None:
            continue
        for claim in r["claims"]:
            key = (r["size"], claim, r["qid"])
            index.setdefault(key, {})[r["build"]] = bool(r["hit"])
            lesson_of[key] = r["lesson"]
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for key, got in index.items():
        if first in got and second in got:
            groups[(key[0], key[1])].append((key, got[first], got[second]))
    out: dict[str, dict[str, Any]] = {}
    for (size, claim), items in sorted(groups.items()):
        a = [x[1] for x in items]
        b = [x[2] for x in items]
        counts = stats.paired_counts(a, b)
        clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for key, x, y in items:
            clusters[lesson_of[key]].append((float(x), float(y)))
        boot = stats.cluster_bootstrap(clusters, resamples=resamples)
        out[f"{size}|{claim}"] = {
            "n": len(items),
            **counts,
            "p": stats.mcnemar_exact(counts["only_first"], counts["only_second"]),
            **boot,
        }
    return out


def false_delivery(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """V5 and V7: the share of prompts that should get no row and got one."""
    cells: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r["variant"] in ("V5", "V7") and "false_delivery" in r:
            cells[f"{r['size']}|{r['variant']}|{r['build']}"].append(r["false_delivery"])
    return {k: stats.rate(sum(v), len(v)) for k, v in sorted(cells.items())}


def cost_table(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Characters and rows per prompt row, p50 and p95, per size and build."""
    cells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["channel"] == "prompt" and r["build"] in ("B0", "B1"):
            cells[f"{r['size']}|{r['build']}"].append(r)
    return {
        k: {
            "chars": summarize_ms(r["chars"] for r in v),
            "rows": summarize_ms(r["rows"] for r in v),
            "hook_ms": summarize_ms(r["ms"] for r in v),
        }
        for k, v in sorted(cells.items())
    }


def false_deny(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cells: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r["variant"] == "BENIGN":
            cells[f"{r['size']}|{r['build']}"].append(bool(r["false_deny"]))
    return {k: stats.rate(sum(v), len(v)) for k, v in sorted(cells.items())}


def rank_table(rows: Sequence[dict[str, Any]], variant: str, channel: str) -> dict[str, Any]:
    """Recall at 1 and 5 and MRR for one variant and channel, per size and build."""
    cells: dict[str, list[int | None]] = defaultdict(list)
    for r in rows:
        if r["variant"] == variant and r["channel"] == channel and r["build"] in ("B0", "B1"):
            cells[f"{r['size']}|{r['build']}"].append(r["rank"] if r["hit"] else None)
    out = {}
    for k, ranks in sorted(cells.items()):
        n = len(ranks)
        out[k] = {
            "n": n,
            "recall_at_1": round(sum(1 for x in ranks if x == 1) / n, 4) if n else 0.0,
            "recall_at_5": round(sum(1 for x in ranks if x and x <= 5) / n, 4) if n else 0.0,
            "mrr": round(sum(1 / x for x in ranks if x) / n, 4) if n else 0.0,
        }
    return out


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rates": rate_table(rows),
        "b1_vs_s2": pair_compare(rows, "B1", "S2"),
        "b1_vs_b0": pair_compare(rows, "B1", "B0"),
        "false_delivery_v5": false_delivery(rows),
        "false_deny": false_deny(rows),
        "cost": cost_table(rows),
        "mrr_v2_pull": rank_table(rows, "V2", "pull"),
        "claim_notes": CLAIM_NOTES,
    }
