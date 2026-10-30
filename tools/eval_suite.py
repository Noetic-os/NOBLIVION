# SPDX-License-Identifier: AGPL-3.0-or-later
"""One command for every zero-token replay and cost check (NOBLIVION-145).

``run`` measures one build of the plugin on a scratch store and writes
``result.json`` and ``summary.txt``. ``compare`` reads two results, prints the
before-and-after report and exits 1 when delivery dropped. No step calls a model:
the store embeds with its local model, or ranks by keyword when it has none.

    python tools/eval_suite.py run --out DIR --bench FROZEN_DIR --freeze-inputs
    python tools/eval_suite.py run --out DIR2 --bench FROZEN_DIR --inputs DIR/inputs
    python tools/eval_suite.py compare DIR/result.json DIR2/result.json

The sections of ``run``:

* ``prompt_rows``: every prompt of the chosen transcripts through
  ``hooks/recall_hook.py``, with the release settings (``shipped``) and with the
  three settings of NOBLIVION-121 off (``off``). Characters, rows, hub share.
  ``acceptance_121`` holds the numbers that ticket asks for, less the labelled
  precision.
* ``guard_log``: the lines of ``guard-log.jsonl`` through ``hooks/guard_hook.py``.
  ``guard_calls``: every tool call of the transcripts through the same hook, so a
  row on a call that showed nothing before is seen too.
* ``error_recall``: every Bash result of the transcripts through
  ``hooks/error_recall_hook.py``.
* ``subagent``: every Agent call through ``hooks/subagent_rules_hook.py``.
* ``offtopic``: prompts on no project subject, ``tools/eval/offtopic_prompts.json``.
* ``keyword_mode``: the store again without its embedder, then the real prompts,
  the off-topic prompts and the test 4 prompts.
* ``gate0`` and ``test4``: the frozen delivery checks. Their inputs stay outside
  the repository, in the folder of ``--bench``. Without that folder both sections
  are skipped with a message, and ``compare`` gives no verdict on them.
* ``mcp_listing``: ``tools/list`` and the server instructions of two starts.
* ``cost``: characters and re-sent tokens by hook, recorded in the transcripts and
  replayed, and the plugin share of the session input tokens. Re-sent tokens are
  characters / 4 times the later API calls of the same transcript (an upper bound:
  compaction is ignored).
* ``hook_time``: milliseconds per run, recorded, in this process, and cold.

The replays run the hooks in this process, as ``tools/replay_prompt_rows.py`` does,
so they cannot drift from the hook code. Only the cold timing starts processes,
because the start of a process is part of the wait it measures.

Everything lives in a scratch folder: ``HOME``, the Claude Code config dir, the
memory folders, the store, the caches and the guard tables. The live data folder
(``--live-data``) is read for ``guard-log.jsonl``, ``models`` and ``venv/bin/python``
only, and is never written. The result holds counts and note names, never a
prompt, a command or a note text, but note names are private: ``--out`` is mode
0700 and its files are mode 0600. ``--freeze-inputs`` copies the transcripts and
the guard log under ``--out/inputs``, so a later run replays the same input.

``run`` exits 3 when a section failed: a hook that fails open prints nothing, and
nothing must not read as a gain. ``compare`` exits 1 when a gate 0 case or a test 4
slot that passed no longer passes, and 2 when it cannot give a verdict: a section
that ran before is missing now, or the inputs differ.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as _dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import types
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
SCHEMA = 1
DATA_NAME = "noblivion-noblivion"
DB_NAME = "noblivion.db"
OFFTOPIC_FILE = TOOLS / "eval" / "offtopic_prompts.json"
OFF_ARM = {
    "NOBLIVION_RECALL_RULES_ONLY": "0",
    "NOBLIVION_RECALL_PROMPT_ROWS": "0",
    "NOBLIVION_RECALL_HUB_DAYS": "0",
}
ACCEPT_CHARS = 1500  # NOBLIVION-121: characters per prompt
ACCEPT_HUB_SHARE = 0.20  # NOBLIVION-121: no hub note above this share of sessions
SECTIONS = (
    "prompt_rows",
    "guard_log",
    "guard_calls",
    "error_recall",
    "subagent",
    "offtopic",
    "gate0",
    "test4",
    "mcp_listing",
    "hook_time",
    "cost",
    "keyword_mode",
)
# The variables that name a folder of the user. A hook that reads one of them
# would reach the live data, so they never pass from the caller's env.
PATH_VARS = (
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
    "CLAUDE_PROJECT_DIR",
)
GUARD_TOOLS = ("Bash", "Read", "Grep", "Edit", "Write", "MultiEdit")
AGENT_TOOLS = ("Agent", "Task")
PLUGIN_MARKS = ("CLAUDE_PLUGIN_ROOT}/hooks/", "/plugins/cache/noblivion/")
SCRIPT_RE = re.compile(r"hooks/([a-z_]+\.py)(\s+--[a-z-]+)?")
LOG_STATE_RE = re.compile(r" ms=\d+ (\S+)")
TEXT_MAX = 200_000  # the longest tool result a replay keeps


USER_PATH_ARGS = (
    "bench",
    "config",
    "corpus",
    "guard_log",
    "inputs",
    "live_data",
    "models",
    "out",
    "plugin_root",
    "seed_db",
    "store_python",
    "work_dir",
)


def load_module(path: Path, alias: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def percentile(values: Sequence[float], share: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def ms_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "runs": len(values),
        "ms_p50": round(percentile(values, 0.5), 1),
        "ms_p95": round(percentile(values, 0.95), 1),
    }


def file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def tree_digest(paths: Iterable[tuple[str, Path]]) -> str:
    """One hash over ``(name, content hash)`` pairs, in name order."""
    sha = hashlib.sha256()
    for name, path in sorted(paths):
        sha.update(f"{name}\0{file_digest(path)}\n".encode())
    return sha.hexdigest()[:16]


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def slugify(folder: str) -> str:
    """The Claude Code project folder name of an absolute path."""
    return re.sub(r"[^A-Za-z0-9-]", "-", folder)


def epoch(value: Any) -> float | None:
    try:
        moment = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.timestamp()


def fail_states(lines: Iterable[str]) -> Counter:
    """The states of ``recall.log`` lines: ``ok``, ``keyword`` for an answer with no
    cosine, and ``fail`` for a hook that failed open and printed nothing."""
    states: Counter = Counter()
    for line in lines:
        match = LOG_STATE_RE.search(line)
        if match:
            state = match.group(1)
            states["keyword" if ":keyword" in state else state.split(":")[0]] += 1
    return states


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def model_text(event: str, stdout: str) -> str:
    """The part of a hook's stdout the model sees (method of R2): the additional
    context, a deny or ask reason, a block reason, and plain text only on the two
    events where Claude Code adds it to the context."""
    text = (stdout or "").strip()
    if text.startswith("{"):
        try:
            doc = json.loads(text.splitlines()[0])
        except ValueError:
            return ""
        spec = doc.get("hookSpecificOutput") or {}
        parts = [str(spec.get("additionalContext") or "")]
        if spec.get("permissionDecision") in ("deny", "ask"):
            parts.append(str(spec.get("permissionDecisionReason") or ""))
        if doc.get("decision") == "block":
            parts.append(str(doc.get("reason") or ""))
        return "".join(parts)
    return text if event in ("SessionStart", "UserPromptSubmit") else ""


def decision_of(stdout: str) -> str:
    """``deny``, ``ask``, ``rewrite``, ``rows`` or ``silent`` for one PreToolUse output."""
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return "silent"
    try:
        spec = json.loads(text.splitlines()[0]).get("hookSpecificOutput") or {}
    except ValueError:
        return "silent"
    decision = spec.get("permissionDecision")
    if decision in ("deny", "ask"):
        return str(decision)
    if spec.get("updatedInput"):
        return "rewrite"
    return "rows" if spec.get("additionalContext") else "silent"


def row_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("- "))


# -- the transcripts -------------------------------------------------------------


@dataclass
class Call:
    """One tool call of a transcript, with what the replays need."""

    session: str
    agent: str
    slug: str
    tool: str
    input: dict
    tool_use_id: str
    moment: float
    cwd: str
    prompt: str
    transcript: str
    error: bool = False
    interrupted: bool = False
    text: str = ""
    response: Any = None


@dataclass
class HookCount:
    runs: int = 0
    shown: int = 0
    chars: int = 0
    resent_tokens: float = 0.0
    ms: list = field(default_factory=list)


def _tool_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def script_of(command: str) -> str | None:
    if any(mark in command for mark in PLUGIN_MARKS):
        found = SCRIPT_RE.search(command)
        if found:
            return found.group(1) + (found.group(2) or "")
    return None


class Scan:
    """One pass over the transcripts: the tool calls for the replays, the API call
    times for the re-sent tokens, and the counts of the R2 cost scripts."""

    def __init__(
        self, rt: types.ModuleType, signals: types.ModuleType, prompt_event: Callable[..., Any]
    ):
        self.rt = rt
        self.signals = signals
        self.prompt_event = prompt_event
        self.call_times: dict[tuple[str, str], list[float]] = {}
        self.tool_calls: list[Call] = []
        self.slugs: dict[str, str] = {}
        self.hooks: dict[tuple[str, str], HookCount] = defaultdict(HookCount)
        self.stop_ms: dict[str, list] = defaultdict(list)
        self.count: Counter = Counter()

    def later(self, session: str, agent: str, moment: float) -> int | None:
        """API calls of the transcript after ``moment``, None for a transcript
        outside the sample."""
        times = self.call_times.get((session, agent))
        return None if times is None else len(times) - bisect.bisect_right(times, moment)

    def add(self, path: str, session: str, agent: str, slug: str) -> None:
        self.slugs.setdefault(session, slug)
        best: dict[str, dict] = {}
        calls: list[float] = []
        pending: dict[str, Call] = {}
        shown: list[tuple[HookCount, int, int]] = []
        seen: set[str] = set()
        prompt = ""
        cwd = ""
        for rec in self.rt.read_jsonl(path):
            kind = rec.get("type")
            cwd = str(rec.get("cwd") or cwd)
            moment = epoch(rec.get("timestamp")) or (calls[-1] if calls else 0.0)
            event = self.prompt_event(self.rt, rec, self.signals)
            if event is not None and agent == "main":
                if event[1] == "typed" or event[0][:200] not in seen:
                    self.count["prompt_events"] += 1
                    self.count["prompts"] += event[1] == "typed"
                seen.add(event[0][:200])
            if kind == "assistant":
                message = rec.get("message") or {}
                mid = message.get("id") or rec.get("uuid")
                usage = message.get("usage") or {}
                if mid not in best:
                    best[mid] = usage
                    calls.append(moment)
                elif (usage.get("output_tokens") or 0) > (best[mid].get("output_tokens") or 0):
                    best[mid] = usage
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        pending_call = self._tool_use(
                            block, session, agent, slug, moment, cwd, prompt, path
                        )
                        pending[pending_call.tool_use_id] = pending_call
            elif kind == "user":
                text = self.rt.prompt_text(rec, self.signals)
                if text is not None:
                    prompt = text
                self._results(rec, pending)
            elif kind == "attachment":
                self._attachment(rec.get("attachment") or {}, len(calls), shown)
            elif kind == "system" and rec.get("subtype") == "stop_hook_summary":
                self.count["stop_events"] += agent == "main"
                for info in rec.get("hookInfos") or []:
                    name = script_of(str(info.get("command") or ""))
                    if name and info.get("durationMs") is not None:
                        self.stop_ms[name].append(info["durationMs"])
        calls.sort()
        self.call_times[(session, agent)] = calls
        self.count["api_calls"] += len(calls)
        self.count["api_calls_subagent" if agent != "main" else "api_calls_main"] += len(calls)
        for usage in best.values():
            self.count["input_tokens"] += sum(
                usage.get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
        for hook, chars, position in shown:
            hook.resent_tokens += chars / 4 * (len(calls) - position)

    def _tool_use(
        self,
        block: dict,
        session: str,
        agent: str,
        slug: str,
        moment: float,
        cwd: str,
        prompt: str,
        path: str,
    ) -> Call:
        name = str(block.get("name") or "")
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        call = Call(
            session,
            agent,
            slug,
            name,
            tool_input,
            str(block.get("id") or ""),
            moment,
            cwd,
            prompt,
            path,
        )
        self.tool_calls.append(call)
        self.count["tool_calls"] += 1
        if name == "Bash":
            self.count["bash_calls"] += 1
        if name.endswith("noblivion_recall"):
            self.count["recall_calls"] += 1
        elif name.endswith("noblivion_remember"):
            self.count["remember_calls"] += 1
        elif name == "ToolSearch" and "nobliv" in json.dumps(tool_input).lower():
            self.count["toolsearch_noblivion"] += 1
        return call

    def _results(self, rec: dict, pending: dict[str, Call]) -> None:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            call = pending.pop(str(block.get("tool_use_id") or ""), None)
            if call is None:
                continue
            call.error = bool(block.get("is_error"))
            call.text = _tool_text(block.get("content"))[:TEXT_MAX]
            result = rec.get("toolUseResult")
            call.interrupted = isinstance(result, dict) and bool(result.get("interrupted"))
            call.response = result if isinstance(result, (dict, str)) else None

    def _attachment(
        self, att: dict, position: int, shown: list[tuple[HookCount, int, int]]
    ) -> None:
        kind = att.get("type")
        if kind not in ("hook_success", "hook_blocking_error"):
            return
        blocked = att.get("blockingError") if kind == "hook_blocking_error" else None
        blocked = blocked if isinstance(blocked, dict) else {}
        name = script_of(str(att.get("command") or blocked.get("command") or ""))
        if not name:
            return
        event = str(att.get("hookEvent") or "")
        hook = self.hooks[(event, name)]
        hook.runs += 1
        stdout = str(att.get("stdout") or "")
        if kind == "hook_blocking_error":
            chars = len(str(blocked.get("blockingError") or att.get("stderr") or ""))
            self.count["stop_blocks"] += event == "Stop"
        else:
            chars = len(model_text(event, stdout))
            self.count["stop_blocks"] += event == "Stop" and '"block"' in stdout
        if chars:
            hook.shown += 1
            hook.chars += chars
            shown.append((hook, chars, position))
        if att.get("durationMs") is not None:
            hook.ms.append(att["durationMs"])


def subagent_files(main: str) -> list[Path]:
    return sorted(Path(main[: -len(".jsonl")], "subagents").glob("agent-*.jsonl"))


def agent_of(path: Path) -> str:
    return path.stem.removeprefix("agent-")


# -- the scratch store -----------------------------------------------------------


class Store:
    """A store process of its own, on the scratch data folder."""

    def __init__(self, data: Path, python: str, src: Path, wait_s: float):
        self.data = data
        self.python = python
        self.src = src
        self.wait_s = wait_s
        self.proc: subprocess.Popen | None = None

    def _env(self, env: Mapping[str, str]) -> dict[str, str]:
        return dict(env, PYTHONPATH=str(self.src))

    def index(self, env: Mapping[str, str]) -> None:
        run = subprocess.run(
            # An explicit --db: the index makes the database of a run with no seed.
            [self.python, "-m", "noblivion", "index", "--json", "--db", str(self.data / DB_NAME)],
            env=self._env(env),
            capture_output=True,
            text=True,
            timeout=self.wait_s,
        )
        if run.returncode not in (0, 1):
            raise RuntimeError(f"noblivion index exited {run.returncode}")

    def start(self, env: Mapping[str, str]) -> dict:
        (self.data / "store.json").unlink(missing_ok=True)
        log = open(self.data / "suite-store.out", "ab")  # noqa: SIM115 - the child keeps it
        self.proc = subprocess.Popen(
            [self.python, "-m", "noblivion", "serve"],
            env=self._env(env),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        log.close()
        health = self.wait_ready()
        self.warm()
        return health

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.proc = None

    def _get(self, route: str, timeout: float) -> dict:
        info = json.loads((self.data / "store.json").read_text(encoding="utf-8"))
        token = (self.data / "token").read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{route}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310
            return json.loads(answer.read())

    def health(self) -> dict | None:
        try:
            return self._get("/api/health", 5)
        except (OSError, ValueError, KeyError):
            return None

    def warm(self) -> None:
        """Two queries first. A cold embedder can miss the hook's time limit, and
        the prompt then gets a keyword answer (found in the first gate 0 run)."""
        for text in ("warm up the embedding model", "a second query to warm up the model"):
            query = urllib.parse.urlencode({"q": text, "project": "claude_code", "top_k": 1})
            self._get("/api/memories/index?" + query, 60)

    def wait_ready(self) -> dict:
        """The health answer once the index is idle and every vector is there.
        Two equal answers in a row, so a scan that starts late is not missed."""
        end = time.monotonic() + self.wait_s
        last = None
        while time.monotonic() < end:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"store exited {self.proc.returncode}")
            health = self.health()
            if health and health.get("index_state") == "idle":
                embedding = health.get("embedding") or {}
                state = (
                    health.get("content_rev"),
                    health.get("vector_rev"),
                    health.get("memories"),
                )
                busy = embedding.get("state") not in ("ready", "off", "failed") or (
                    embedding.get("state") == "ready" and embedding.get("missing_vectors")
                )
                if not busy and state == last:
                    return health
                last = state
            time.sleep(0.5)
        raise RuntimeError(f"store not ready after {self.wait_s:.0f} s")


class Hooks:
    """The hooks of the build under test, each loaded once, run in this process."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.mods: dict[str, types.ModuleType] = {}

    def module(self, script: str) -> types.ModuleType:
        if script not in self.mods:
            self.mods[script] = load_module(self.folder / script, "suite_" + script[:-3])
        return self.mods[script]

    def call(self, script: str, event: Mapping[str, Any], env: Mapping[str, str]) -> tuple:
        """``(stdout, milliseconds)`` of one event."""
        out = io.StringIO()
        started = time.perf_counter()
        self.module(script).main(io.StringIO(json.dumps(event)), out, dict(env))
        return out.getvalue(), (time.perf_counter() - started) * 1000


# -- gate 0 ----------------------------------------------------------------------


def markers(lesson: str) -> list[str]:
    """The forms of a note name in a row: the file stem, and the stem cut the way
    a row or a store title cuts it."""
    stem = lesson.removesuffix(".md")
    dashed = stem.replace("_", "-")
    return list(dict.fromkeys([stem, stem[:55], dashed[:50], dashed[:55]]))


def found(text: str, lesson: str, rule: str, title: str = "") -> tuple[bool, int | None]:
    """Is the lesson in the hook output (its file name, its ``name:`` field, or the
    first 50 characters of its rule), and at which row. The criterion of gate 0,
    written before its run. The ``name:`` field is a marker since its amendment A3."""
    low = text.lower()
    names = [m.lower() for m in markers(lesson)]
    names += [t.lower() for t in (title, title[:55]) if t]
    rule_head = rule[:50].lower()
    seen = any(n in low for n in names) or bool(rule) and rule_head in low
    rows = [line.lower() for line in text.splitlines() if line.startswith("- ")]
    for rank, line in enumerate(rows, 1):
        if any(n in line for n in names) or (rule and rule_head in line):
            return seen, rank
    return seen, None


def check(name: str, passed: bool, rank: int | None = None) -> dict[str, Any]:
    return {"check": name, "pass": bool(passed), "rank": rank}


def verdict(case: Mapping[str, Any], checks: Sequence[Mapping[str, Any]]) -> bool:
    """The pass rule of one case, per scenario (W1 to W10)."""

    def any_of(prefix: str) -> bool:
        return any(c["pass"] for c in checks if c["check"].startswith(prefix))

    scenario = case["W"]
    if scenario == "W1":
        prompt = [c for c in checks if c["check"].startswith("prompt_rows")][:1]
        return any(c["pass"] for c in prompt) or any_of("guard")
    if scenario == "W2":
        return any_of("pull")
    if scenario in ("W3", "W7"):
        return any_of("guard_bash")
    if scenario == "W4":
        return any_of("precompact_print")
    if scenario == "W5":
        return any_of("subagent")
    if scenario == "W6":
        return any_of("prompt_rows")
    if scenario == "W8":
        return any_of("error_recall")
    counted = [c for c in checks if "(info)" not in c["check"]]
    return bool(counted) and all(c["pass"] for c in counted)


class Gate0:
    """The frozen delivery cases, through the hooks of the build, in this process.
    A case names a folder of the frozen home: ``cwd`` is that folder in the scratch
    home, and the memory folder is the one Claude Code would use for the frozen
    path, so the root of a row is the same as in the frozen run."""

    def __init__(self, suite: Suite, frozen_home: str):
        self.suite = suite
        self.hooks = suite.hooks
        self.frozen_home = frozen_home
        self.mcp: types.ModuleType | None = None

    def env_of(self, cwd: str) -> tuple[dict[str, str], str]:
        inside = cwd == self.frozen_home or cwd.startswith(self.frozen_home + "/")
        mapped = str(self.suite.home) + cwd[len(self.frozen_home) :] if inside else cwd
        memory = self.suite.projects / slugify(cwd) / "memory"
        return dict(self.suite.env, CLAUDE_PROJECT_DIR=mapped, NOBLIVION_MEMORY_DIR=str(memory)), (
            mapped
        )

    def transcript(self, sid: str, cwd: str, prompts: Sequence[str]) -> str:
        path = self.suite.projects / slugify(cwd) / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": text},
                    "sessionId": sid,
                    "cwd": cwd,
                    "timestamp": f"2026-01-01T10:00:{i:02d}Z",
                }
            )
            for i, text in enumerate(prompts)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def note_fields(self, lesson: str, cwd: str) -> tuple[str, str]:
        """``(rule, name)`` of a note. Two project folders can hold a file of the
        same name: the folder of the case first, then the home folder."""
        folders = [slugify(cwd), slugify(self.frozen_home)]
        for slug in folders + [p.parent.name for p in sorted(self.suite.projects.glob("*/memory"))]:
            path = self.suite.projects / slug / "memory" / lesson
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                fields = [re.search(rf"^{k}:\s*(.*)$", text, re.M) for k in ("rule", "name")]
                rule, name = (m.group(1).strip().strip('"') if m else "" for m in fields)
                return rule, name
        return "", ""

    def prompt_rows(self, sid: str, prompt: str, cwd: str, cache: Path | None = None) -> str:
        env, mapped = self.env_of(cwd)
        if cache is not None:
            env["NOBLIVION_RECALL_CACHE_DIR"] = str(cache)
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": sid,
            "prompt": prompt,
            "cwd": mapped,
            "transcript_path": self.transcript(sid, cwd, [prompt]),
        }
        return self.hooks.call("recall_hook.py", event, env)[0]

    def guard(self, sid: str, tool: str, tool_input: dict, prompts: Sequence[str], cwd: str) -> str:
        env, mapped = self.env_of(cwd)
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "session_id": sid,
            "cwd": mapped,
            "transcript_path": self.transcript(sid, cwd, prompts),
            "tool_use_id": "toolu_" + hashlib.sha256(sid.encode()).hexdigest()[:12],
        }
        return self.hooks.call("guard_hook.py", event, env)[0]

    def pull(self, query: str, cwd: str, k: int = 5) -> str:
        if self.mcp is None:
            self.mcp = load_module(self.suite.plugin / "mcp" / "recall_mcp.py", "suite_recall_mcp")
        env, _ = self.env_of(cwd)
        answer = self.mcp.noblivion_recall(query=query, k=k, environ=env)
        return "\n".join(str(block.get("text") or "") for block in answer.get("content") or [])

    def error_recall(self, sid: str, failing: Mapping[str, str], prompts: list, cwd: str) -> str:
        env, mapped = self.env_of(cwd)
        event = {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": failing["command"]},
            "error": failing["output"],
            "session_id": sid,
            "cwd": mapped,
            "transcript_path": self.transcript(sid, cwd, prompts),
            "tool_use_id": "toolu_" + hashlib.sha256(sid.encode()).hexdigest()[:12],
        }
        return self.hooks.call("error_recall_hook.py", event, env)[0]

    def subagent(self, sid: str, case: Mapping[str, Any]) -> str:
        cwd = case["cwd"]
        env, mapped = self.env_of(cwd)
        path = self.transcript(sid, cwd, [case["prompt"]])
        digest = hashlib.sha256(sid.encode()).hexdigest()
        common = {"session_id": sid, "cwd": mapped, "transcript_path": path}
        pre = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": case["subagent_prompt"],
                "subagent_type": case["subagent_type"],
                "description": "task",
            },
            "tool_use_id": "toolu_" + digest[:12],
            **common,
        }
        self.hooks.call("subagent_rules_hook.py", pre, env)
        start = {
            "hook_event_name": "SubagentStart",
            "agent_id": digest[12:28],
            "agent_type": case["subagent_type"],
            **common,
        }
        return self.hooks.call("subagent_rules_hook.py", start, env)[0]

    def compaction(self, case: Mapping[str, Any], sid: str) -> list[dict[str, Any]]:
        """W4: a prompt row, the PreCompact save, and the restore after compact."""
        lesson, cwd = case["lesson"], case["cwd"]
        keys = self.note_fields(lesson, cwd)
        env, mapped = self.env_of(cwd)
        first = case["session"][0]
        out = []
        seen, rank = found(self.prompt_rows(sid, first, cwd), lesson, *keys)
        out.append(check("step1_prompt_rows", seen, rank))
        event = {
            "hook_event_name": "PreCompact",
            "trigger": "manual",
            "session_id": sid,
            "cwd": mapped,
            "transcript_path": self.transcript(sid, cwd, [first]),
        }
        text = self.hooks.call("continuity_hook.py", event, env)[0]
        seen, rank = found(text, lesson, *keys)
        out.append(check("precompact_print", seen, rank))
        event = {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": sid,
            "cwd": mapped,
        }
        text = self.hooks.call("continuity_hook.py", event, env)[0]
        seen, rank = found(text, lesson, *keys)
        out.append(check("restore_after_compact", seen, rank))
        return out

    def credential(self, case: Mapping[str, Any], sid: str, prompts: list) -> list[dict[str, Any]]:
        """W9: the credential guard denies every frozen form, and the deny text
        does not hold the canary."""
        cwd, canary = case["cwd"], case["canary"]
        _, mapped = self.env_of(cwd)
        repo = Path(mapped)
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            url = f"https://x-access-token:{canary}@example.invalid/org/repo.git"
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
        out = []
        for i, command in enumerate(case["hazard_commands"]):
            text = self.guard(f"{sid}C{i}", "Bash", {"command": command}, prompts, cwd)
            out.append(
                check(f"credential_guard[{i}]", decision_of(text) == "deny" and canary not in text)
            )
        return out

    def case(self, case: Mapping[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        lesson, cwd, scenario = case["lesson"], case["cwd"], case["W"]
        sid = "g0-" + case["id"]
        keys = self.note_fields(lesson, cwd) if lesson else ("", "")
        prompts = [case["prompt"]] if case.get("prompt") else []
        prompts = prompts or [case.get("late_prompt_context") or (case.get("session") or ["x"])[0]]
        if scenario in ("W1", "W2", "W3", "W5", "W6") and case.get("prompt") and lesson:
            seen, rank = found(self.prompt_rows(sid, case["prompt"], cwd), lesson, *keys)
            out.append(check("prompt_rows", seen, rank))
        if case.get("late_prompt_context") and lesson:
            text = self.prompt_rows(sid + "L", case["late_prompt_context"], cwd)
            seen, rank = found(text, lesson, *keys)
            out.append(check("late_prompt_rows(info)", seen, rank))
        for i, query in enumerate(case.get("recall_queries") or []):
            seen, rank = found(self.pull(query, cwd), lesson, *keys)
            out.append(check(f"pull_recall[{i}]", seen, rank))
        if scenario == "W9":
            return out + self.credential(case, sid, prompts)
        for i, command in enumerate(case.get("hazard_commands") or []):
            text = self.guard(f"{sid}G{i}", "Bash", {"command": command}, prompts, cwd)
            seen, rank = found(text, lesson, *keys) if lesson else (False, None)
            out.append(check(f"guard_bash[{i}]", seen, rank))
        for i, call in enumerate(case.get("guard_tool_calls") or []):
            tool_input = {"file_path": call["file_path"], "new_string": "x", "old_string": "y"}
            text = self.guard(f"{sid}E{i}", call["tool"], tool_input, prompts, cwd)
            seen, rank = found(text, lesson, *keys) if lesson else (False, None)
            out.append(check(f"guard_edit[{i}]", seen, rank))
        if case.get("failing"):
            text = self.error_recall(sid, case["failing"], prompts, cwd)
            seen, rank = found(text, lesson, *keys)
            out.append(check("error_recall", seen, rank))
        if scenario == "W5":
            seen, rank = found(self.subagent(sid + "S", case), lesson, *keys)
            out.append(check("subagent_start_rules", seen, rank))
        if scenario == "W4":
            out.extend(self.compaction(case, sid + "P"))
        return out

    def remember(self, case: Mapping[str, Any], store: Store) -> list[dict[str, Any]]:
        """W10: the remember tool leaves MEMORY.md as it is, and a new session
        finds the saved fact, in its prompt rows or by pull recall."""
        cwd = case["cwd"]
        env, _ = self.env_of(cwd)
        if self.mcp is None:
            self.mcp = load_module(self.suite.plugin / "mcp" / "recall_mcp.py", "suite_recall_mcp")
        index = Path(env["NOBLIVION_MEMORY_DIR"]) / "MEMORY.md"
        before = file_digest(index) if index.is_file() else ""
        fact = case["session"][0].split(":", 1)[1].strip()
        args = {
            "title": "Saved lesson " + case["id"],
            "rule": fact[:150],
            "apply": fact[:390],
            "body": fact,
            "evidence": "The operator said so in session 1 of test " + case["id"],
        }
        self.mcp.noblivion_remember(args, env)
        unchanged = before == (file_digest(index) if index.is_file() else "")
        store.index(self.suite.env)
        store.wait_ready()
        needle = fact[:50].lower()
        rows = self.prompt_rows(
            "g0-" + case["id"] + "-s2", case["session"][1].split(">", 1)[1].strip(), cwd
        )
        in_rows = needle in rows.lower()
        pulls = [needle in self.pull(q, cwd).lower() for q in case.get("recall_queries") or []]
        return [
            check("memory_md_unchanged", unchanged),
            check("new_session_rows(info)", in_rows),
            check("new_session_found", in_rows or any(pulls)),
        ]


# -- the suite -------------------------------------------------------------------


def skipped(reason: str) -> dict[str, Any]:
    print(f"skipped: {reason}", file=sys.stderr)
    return {"status": "skipped", "reason": reason}


class Suite:
    """One run: the scratch folder, the store and the sections."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.user_home = str(Path.home())
        self.user_config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        for name in USER_PATH_ARGS:
            value = getattr(args, name)
            if value:
                setattr(args, name, os.path.expanduser(value))
        args.transcripts = [os.path.expanduser(p) for p in args.transcripts]
        self.plugin = Path(args.plugin_root).resolve()
        self.out = private_dir(Path(args.out).resolve())
        self.live = Path(args.live_data).resolve()
        self.root = private_dir(
            Path(args.work_dir).resolve()
            if args.work_dir
            else Path(tempfile.mkdtemp(prefix="noblivion-suite-"))
        )
        if self.root == self.live or self.live in self.root.parents:
            raise SystemExit("--work-dir is inside the live data folder: refused")
        self.home = private_dir(self.root / "home")
        self.config_dir = private_dir(self.home / ".claude")
        self.projects = private_dir(self.config_dir / "projects")
        self.data = private_dir(self.config_dir / "plugins" / "data" / DATA_NAME)
        self.work = private_dir(self.root / "work")
        self.settings: dict[str, str] = {}
        self.env = self._env(os.environ)
        # Before a hook module loads: a module may read its settings at import,
        # and a hook that resolves a folder may read this process's env.
        os.environ.clear()
        os.environ.update(self.env)
        # The bytecode goes to the scratch folder: the build under test may be a
        # read-only tree, and a hook that recompiles its siblings on every call
        # would make the replay 8 times slower and the cold timing wrong.
        sys.pycache_prefix = str(self.root / "pycache")
        self.hooks = Hooks(self.plugin / "hooks")
        self.rows = load_module(TOOLS / "replay_prompt_rows.py", "suite_rows")
        self.rt = load_module(TOOLS / "replay_transcripts.py", "suite_transcripts")
        self.rows.HOOKS = self.rt.HOOKS = self.plugin / "hooks"
        self.gt = load_module(self.plugin / "hooks" / "guard_table.py", "suite_guard_table")
        self.scan = Scan(self.rt, self.hooks.module("trust_signals.py"), self.rows.prompt_event)
        self.store: Store | None = None
        self.health: dict[str, Any] = {}
        self.sections: dict[str, dict[str, Any]] = {}
        self.inputs: dict[str, Any] = {}
        self.mains: list[str] = []
        self.guard_lines: list[dict] = []
        self.tables: dict[str, Path] = {}
        self.bench, self.bench_note = self._bench()
        self.gate0_runner: Gate0 | None = None

    def _env(self, source: Mapping[str, str]) -> dict[str, str]:
        env = {
            k: v
            for k, v in source.items()
            if k not in PATH_VARS
            and not (k.startswith("NOBLIVION_") and "/" in v)
            and k != "PYTHONPATH"
        }
        for item in self.args.env:
            name, _, value = item.partition("=")
            env[name] = value
        env.pop("NOBLIVION_CONFIG", None)
        self.settings = {k: v for k, v in sorted(env.items()) if k.startswith("NOBLIVION_")}
        env.update(
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.root / "xdg"),
                "CLAUDE_CONFIG_DIR": str(self.config_dir),
                "CLAUDE_PLUGIN_DATA": str(self.data),
                "CLAUDE_PLUGIN_ROOT": str(self.plugin),
                "NOBLIVION_DATA_DIR": str(self.data),
                "NOBLIVION_IDLE_EXIT_S": "0",
                "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
            }
        )
        return env

    def _bench(self) -> tuple[Path | None, str]:
        if not self.args.bench:
            return (
                None,
                "no --bench folder given: the gate 0 and test 4 inputs are outside the repo",
            )
        bench = Path(self.args.bench)
        if not (bench / "gate0" / "candidates.json").is_file():
            return None, f"frozen inputs absent: no gate0/candidates.json under {bench}"
        return bench, ""

    # -- set-up ----------------------------------------------------------------

    def select_inputs(self) -> None:
        """The transcripts and the guard log of this run, and a hash of them."""
        rt = self.rt
        if self.args.inputs:
            frozen = Path(self.args.inputs).resolve()
            self.mains = rt.main_transcripts([str(frozen / "transcripts" / "*" / "*.jsonl")])
            guard_log = frozen / "guard-log.jsonl"
            since = 0.0
        else:
            default = str(self.user_config / "projects" / "*" / "*.jsonl")
            self.mains = rt.main_transcripts(self.args.transcripts or [default])
            since = time.time() - self.args.since_days * 86400 if self.args.since_days else 0.0
            self.mains = [p for p in self.mains if (epoch(rt.first_ts(p)) or 0.0) >= since]
            guard_log = Path(self.args.guard_log or self.live / "guard-log.jsonl")
        self.guard_lines = []
        if guard_log.is_file():
            for line in read_lines(guard_log):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (epoch(rec.get("ts")) or 0.0) >= since:
                    self.guard_lines.append(rec)
        if self.args.freeze_inputs:
            self.freeze()
        files = [(Path(p).parent.name + "/" + Path(p).name, Path(p)) for p in self.mains]
        for main in self.mains:
            files += [(f"{Path(main).stem}/{p.name}", p) for p in subagent_files(main)]
        sha = hashlib.sha256(tree_digest(files).encode())
        for rec in self.guard_lines:
            sha.update(json.dumps(rec, sort_keys=True).encode())
        self.inputs.update(
            {
                "transcripts": len(self.mains),
                "subagent_transcripts": len(files) - len(self.mains),
                "guard_log_lines": len(self.guard_lines),
                "transcripts_digest": sha.hexdigest()[:16],
            }
        )

    def freeze(self) -> None:
        """A private byte copy of the input, under ``--out/inputs``."""
        frozen = private_dir(self.out / "inputs")
        copies = []
        for main in self.mains:
            source = Path(main)
            folder = private_dir(frozen / "transcripts" / source.parent.name)
            target = folder / source.name
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            copies.append(str(target))
            for sub in sorted(source.with_suffix("").glob("subagents/*")):
                dest = private_dir(folder / source.stem / "subagents") / sub.name
                shutil.copyfile(sub, dest)
                os.chmod(dest, 0o600)
        write_private(
            frozen / "guard-log.jsonl",
            "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in self.guard_lines),
        )
        self.mains = self.rt.main_transcripts(copies)

    def prepare_home(self) -> None:
        """Copy the corpus and, when there is one, the seed database; then the
        config, the embedding model and the frozen folders."""
        seed = None
        if self.bench:
            fixture = self.bench / "gate0" / "fx" / "base"
            source = fixture / ".claude" / "projects"
            candidate = fixture / ".claude" / "plugins" / "data" / DATA_NAME / DB_NAME
            seed = candidate if candidate.is_file() else None
            for item in fixture.iterdir():
                if item.name in (".claude", ".cache", ".local"):
                    continue
                target = self.home / item.name
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=True)
                else:
                    shutil.copy2(item, target)
        elif self.args.corpus:
            source = Path(self.args.corpus)
        else:
            source = self.user_config / "projects"
        self.copy_corpus(source, self.projects)
        self.seed_store(seed or self.args.seed_db, self.data)
        self.write_config()
        if self.bench:
            names = ["gate0/candidates.json", "gate0/candidates_reserve.json"]
            names.append("test4/results/test4.jsonl")
            files = [(n, self.bench / n) for n in names if (self.bench / n).is_file()]
            self.inputs["bench_digest"] = tree_digest(files)
        models = Path(self.args.models or self.live / "models")
        if models.is_dir():
            shutil.copytree(models, self.data / "models")

    def write_config(self) -> None:
        """The config of the build, with one key filled in. The default shared root
        is the project folder of ``Path.home()``, which is the scratch home here,
        so the key names the root of the real home, or a session in a project folder
        would lose the home notes. ``HOME`` stays on the scratch folder because the
        credential guard reads the git credential files under it."""
        path = Path(self.args.config or self.plugin / "config" / "config.default.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        recall = doc.setdefault("recall", {})
        if not isinstance(recall.get("shared_roots"), list):
            recall["shared_roots"] = [slugify(self.real_home())]
            self.inputs["scratch_shared_root"] = recall["shared_roots"][0]
        write_private(self.data / "config.json", json.dumps(doc, indent=1) + "\n")

    def real_home(self) -> str:
        return min((c["cwd"] for c in self.cases()), key=len) if self.bench else self.user_home

    def copy_corpus(self, source: Path, target: Path) -> None:
        files = []
        for folder in sorted(source.glob("*/memory")):
            notes = sorted(folder.glob("*.md"))
            if notes:
                shutil.copytree(folder, target / folder.parent.name / "memory")
                files += [(f"{folder.parent.name}/{p.name}", p) for p in notes]
        self.inputs.update({"notes": len(files), "corpus_digest": tree_digest(files)})

    @staticmethod
    def seed_store(seed: Any, data: Path) -> None:
        if not seed:
            return
        seed = Path(seed)
        shutil.copyfile(seed, data / DB_NAME)
        wal = seed.with_name(seed.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, data / "noblivion.db-wal")

    def store_python(self) -> str:
        if self.args.store_python:
            return self.args.store_python
        live = self.live / "venv" / "bin" / "python"
        return str(live) if live.exists() else sys.executable

    def start_store(self) -> None:
        self.store = Store(
            self.data, self.store_python(), self.plugin / "src", self.args.store_wait
        )
        self.store.index(self.env)
        self.health = self.store.start(self.env)

    def memory_of(self, slug: str) -> Path:
        return self.projects / slug / "memory"

    def table_of(self, slug: str) -> Path:
        """The guard table of one memory folder, built as SessionStart builds it."""
        if slug not in self.tables:
            path = self.work / "tables" / slug / "guard-table.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            env = dict(self.env, NOBLIVION_MEMORY_DIR=str(self.memory_of(slug)))
            folders = self.gt.source_dirs(None, env)
            self.gt.rebuild(folders, path)
            self.tables[slug] = path
        return self.tables[slug]

    def scan_transcripts(self) -> None:
        for main in self.mains:
            path = Path(main)
            self.scan.add(main, path.stem, "main", path.parent.name)
            for sub in subagent_files(main):
                self.scan.add(str(sub), path.stem, agent_of(sub), path.parent.name)

    def main_slug(self) -> str:
        """The project folder of most transcripts. A guard log line of a session
        outside the sample is replayed against this folder."""
        count = Counter(Path(p).parent.name for p in self.mains)
        count = Counter({k: v for k, v in count.items() if self.memory_of(k).is_dir()})
        return count.most_common(1)[0][0] if count else ""

    def calls_of(self, tools: Sequence[str]) -> tuple[list[Call], int]:
        """The tool calls of these tools in a project with a memory folder, and how
        many others there are. A session has no notes to show without that folder."""
        calls = [c for c in self.scan.tool_calls if c.tool in tools]
        kept = [c for c in calls if self.memory_of(c.slug).is_dir()]
        return kept, len(calls) - len(kept)

    # -- the replays -----------------------------------------------------------

    def run_section(self, name: str, fn: Callable[[], dict[str, Any]]) -> None:
        if name not in self.args.sections:
            return
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - one section must not stop the others
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        result.setdefault("status", "ok")
        result["seconds"] = round(time.monotonic() - started, 1)
        self.sections[name] = result
        print(f"{name}: {result['status']} ({result['seconds']} s)", file=sys.stderr, flush=True)

    def replay_prompts(
        self, arms: Mapping[str, Mapping[str, str]], label: str, typed_only: Sequence[str] = ()
    ) -> dict[str, Any]:
        """Every prompt event of the sample through the prompt hook, per arm, per
        project folder. An arm in ``typed_only`` skips the other events."""
        groups: dict[str, list[str]] = defaultdict(list)
        for main in self.mains:
            groups[Path(main).parent.name].append(main)
        prompts: dict[str, list[dict]] = {name: [] for name in arms}
        states: dict[str, Counter] = {name: Counter() for name in arms}
        left_out = 0
        for slug, paths in sorted(groups.items()):
            memory = self.memory_of(slug)
            if not memory.is_dir():
                left_out += len(paths)
                continue
            names = {p.name for p in memory.glob("*.md")}
            births = self.rt.note_births(self.rt.all_transcripts(paths), names)
            work = private_dir(self.work / label / slug)
            shutil.copyfile(self.table_of(slug), work / "guard-table.json")
            for arm, settings in arms.items():
                every = arm not in typed_only
                prompts[arm] += self.rows.run_arm(
                    self.rt, paths, self.env, settings, work, arm, str(memory), births, every
                )
                states[arm].update(fail_states(read_lines(work / arm / "cache" / "recall.log")))
        return {"prompts": prompts, "states": states, "left_out": left_out}

    def arm_report(self, prompts: Sequence[dict], states: Counter, hubs: set[str]) -> dict:
        """The numbers of NOBLIVION-121 count typed prompts, as that ticket does.
        The cost counts every event the hook ran for."""
        typed = [p for p in prompts if p["kind"] == "typed"]
        report = self.rows.summarize(typed, hubs)
        resent = 0.0
        for prompt in prompts:
            later = self.scan.later(prompt["session"], "main", prompt["moment"].timestamp())
            resent += prompt["chars"] / 4 * (later or 0)
        total_chars = sum(p["chars"] for p in prompts)
        report.update(
            {
                "rows_max": max((len(p["names"]) for p in typed), default=0),
                "prompt_events": len(prompts),
                "prompt_events_by_kind": dict(Counter(p["kind"] for p in prompts)),
                "chars_per_event": round(total_chars / len(prompts), 1) if prompts else 0.0,
                "resent_tokens": int(resent),
                "hook_states": dict(states),
                **ms_summary([p["ms"] for p in prompts]),
            }
        )
        return report

    def section_prompt_rows(self) -> dict[str, Any]:
        if not self.mains:
            return skipped("no transcripts in the window")
        arms = {"off": OFF_ARM, "shipped": {}}
        run = self.replay_prompts(arms, "prompts", typed_only=("off",))
        hubs = self.rows.hub_notes(run["prompts"]["off"])
        report = {
            "arms": {
                name: self.arm_report(run["prompts"][name], run["states"][name], hubs)
                for name in arms
            },
            "hub_notes": sorted(hubs),
            "transcripts_without_memory_folder": run["left_out"],
        }
        shipped = report["arms"]["shipped"]
        report["status"] = self.replay_state(shipped, run["states"]["shipped"])
        report["acceptance_121"] = {
            "prompts": shipped["prompts"],
            "sessions": shipped["sessions"],
            "chars_per_prompt": shipped["chars_per_prompt"],
            "chars_target": ACCEPT_CHARS,
            "chars_met": shipped["chars_per_prompt"] <= ACCEPT_CHARS,
            "rows_per_prompt": shipped["rows_per_prompt"],
            "rows_max": shipped["rows_max"],
            "hub_notes": len(hubs),
            "hub_share": shipped["hub_share"],
            "hub_notes_max_session_share": shipped["hub_notes_max_session_share"],
            "hub_target": ACCEPT_HUB_SHARE,
            "hub_met": shipped["hub_notes_max_session_share"] < ACCEPT_HUB_SHARE,
            "chars_per_prompt_off": report["arms"]["off"]["chars_per_prompt"],
            "labelled_precision": "not measured: the hand labels come from P1-01",
        }
        return report

    def states_verdict(self, states: Mapping[str, int]) -> str:
        """``degraded`` when a hook failed open: a store that is down prints nothing,
        and that must not read as a gain. A keyword answer from a store with an
        embedder counts too: it is a timed-out query. The first frozen gate 0 run
        lost case 1a that way."""
        allowed = ("ok",) if self.health.get("mode") == "hybrid" else ("ok", "keyword")
        return "degraded" if any(s not in allowed for s in states) else "ok"

    def replay_state(self, report: Mapping[str, Any], states: Mapping[str, int]) -> str:
        if report["prompts"] and not report["prompts_with_rows"]:
            return "degraded"
        return self.states_verdict(states)

    def guard_event(self, call: Call, work: Path, tool_input: dict) -> tuple[dict, dict]:
        """One PreToolUse event. The session transcript holds only the prompt that
        came before this call, so the hook reads what it read then."""
        slug = call.slug
        path = work / "prompts" / f"{call.session}-{call.agent}.jsonl"
        text = json.dumps({"type": "user", "message": {"role": "user", "content": call.prompt}})
        if not path.is_file() or path.read_text(encoding="utf-8").strip() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": call.tool,
            "tool_input": tool_input,
            "session_id": call.session,
            "cwd": call.cwd or str(self.home),
            "transcript_path": str(path),
            "tool_use_id": call.tool_use_id,
        }
        if call.agent != "main":
            event["agent_id"] = call.agent
        env = dict(
            self.env,
            NOBLIVION_MEMORY_DIR=str(self.memory_of(slug)),
            NOBLIVION_GUARD_TABLE=str(self.table_of(slug)),
            NOBLIVION_GUARD_STATE_DIR=str(work / "state"),
            NOBLIVION_GUARD_LOG=str(work / "guard-log.jsonl"),
            NOBLIVION_RECALL_CACHE_DIR=str(work / "cache"),
        )
        return event, env

    def replay_guard(self, label: str, calls: Sequence[tuple[Call, dict, str]]) -> dict[str, Any]:
        work = private_dir(self.work / label)
        decisions: Counter = Counter()
        pairs: Counter = Counter()
        chars = rows = 0
        resent = 0.0
        ms: list[float] = []
        for call, tool_input, logged in calls:
            event, env = self.guard_event(call, work, tool_input)
            out, spent = self.hooks.call("guard_hook.py", event, env)
            ms.append(spent)
            decision = decision_of(out)
            decisions[decision] += 1
            if logged:
                pairs[f"{logged}>{decision}"] += 1
            text = model_text("PreToolUse", out)
            chars += len(text)
            rows += row_count(text)
            later = self.scan.later(call.session, call.agent, call.moment)
            resent += len(text) / 4 * (later or 0)
        logged_errors = sum(
            1
            for line in read_lines(work / "guard-log.jsonl")
            if '"decision": "error"' in line or '"decision":"error"' in line
        )
        total = len(calls)
        return {
            "calls": total,
            "decisions": dict(decisions),
            "chars": chars,
            "chars_per_call": round(chars / total, 1) if total else 0.0,
            "rows": rows,
            "rows_per_call": round(rows / total, 3) if total else 0.0,
            "resent_tokens": int(resent),
            "hook_errors": logged_errors,
            "logged_to_replayed": dict(pairs),
            "status": "degraded" if logged_errors else "ok",
            **ms_summary(ms),
        }

    def section_guard_log(self) -> dict[str, Any]:
        if not self.guard_lines:
            return skipped(f"no guard log lines at {self.live / 'guard-log.jsonl'}")
        linked = 0
        by_head: dict[tuple[str, str, str], Call] = {}
        for call in self.scan.tool_calls:
            if call.tool in GUARD_TOOLS:
                text = self.tool_text(call)
                by_head.setdefault((call.session, call.tool, text[:200]), call)
        calls: list[tuple[Call, dict, str]] = []
        for rec in self.guard_lines:
            tool, text = str(rec.get("tool") or ""), str(rec.get("text") or "")
            if tool not in GUARD_TOOLS or not text:
                continue
            session = str(rec.get("session_id") or "")
            full = by_head.get((session, tool, text[:200]))
            if full is not None and self.memory_of(full.slug).is_dir():
                linked += 1
                calls.append((full, full.input, str(rec.get("decision") or "")))
                continue
            key = "command" if tool == "Bash" else "path" if tool == "Grep" else "file_path"
            slug = self.scan.slugs.get(session, self.main_slug())
            if not self.memory_of(slug).is_dir():
                continue
            call = Call(
                session,
                str(rec.get("agent_id") or "main"),
                slug,
                tool,
                {key: text},
                "toolu_" + hashlib.sha256(text.encode()).hexdigest()[:12],
                epoch(rec.get("ts")) or 0.0,
                "",
                "",
                "",
            )
            calls.append((call, call.input, str(rec.get("decision") or "")))
        report = self.replay_guard("guard_log", calls)
        pairs = report["logged_to_replayed"]
        report["lines"] = len(self.guard_lines)
        report["linked_to_a_transcript"] = linked
        report["deny_logged"] = sum(c for k, c in pairs.items() if k.startswith("deny>"))
        report["deny_kept"] = pairs.get("deny>deny", 0)
        return report

    @staticmethod
    def tool_text(call: Call) -> str:
        if call.tool == "Bash":
            return str(call.input.get("command") or "")
        return str(call.input.get("file_path") or call.input.get("path") or "")

    def section_guard_calls(self) -> dict[str, Any]:
        kept, left_out = self.calls_of(GUARD_TOOLS)
        if not kept:
            return skipped("no tool calls in a project with a memory folder")
        report = self.replay_guard("guard_calls", [(c, c.input, "") for c in kept])
        report.pop("logged_to_replayed", None)
        report["calls_without_memory_folder"] = left_out
        report["share_with_text"] = round(
            1 - report["decisions"].get("silent", 0) / report["calls"], 3
        )
        return report

    def section_error_recall(self) -> dict[str, Any]:
        kept, left_out = self.calls_of(("Bash",))
        calls = [c for c in kept if c.text or c.response]
        if not calls:
            return skipped("no Bash results in a project with a memory folder")
        work = private_dir(self.work / "error_recall")
        log = work / "error-recall-log.jsonl"
        chars = shows = 0
        resent = 0.0
        ms: list[float] = []
        for call in calls:
            slug = call.slug
            common = {
                "tool_name": "Bash",
                "tool_input": call.input,
                "session_id": call.session,
                "cwd": call.cwd or str(self.home),
                "tool_use_id": call.tool_use_id,
            }
            if call.agent != "main":
                common["agent_id"] = call.agent
            if call.error:
                event = {
                    "hook_event_name": "PostToolUseFailure",
                    "error": call.text,
                    "is_interrupt": call.interrupted,
                    **common,
                }
            else:
                event = {
                    "hook_event_name": "PostToolUse",
                    "tool_response": call.response,
                    **common,
                }
            env = dict(
                self.env,
                NOBLIVION_MEMORY_DIR=str(self.memory_of(slug)),
                NOBLIVION_ERROR_RECALL_LOG=str(log),
                NOBLIVION_ERROR_RECALL_STATE_DIR=str(work / "state"),
                NOBLIVION_RECALL_CACHE_DIR=str(work / "cache"),
            )
            out, spent = self.hooks.call("error_recall_hook.py", event, env)
            ms.append(spent)
            text = model_text("PostToolUse", out)
            if text:
                shows += 1
                chars += len(text)
                resent += (
                    len(text) / 4 * (self.scan.later(call.session, call.agent, call.moment) or 0)
                )
        decisions: Counter = Counter()
        for line in read_lines(log):
            try:
                decisions[str(json.loads(line).get("decision"))] += 1
            except ValueError:
                continue
        return {
            "bash_results": len(calls),
            "calls_without_memory_folder": left_out,
            "failures": sum(1 for c in calls if c.error),
            "decisions": dict(decisions),
            "shows": shows,
            "chars": chars,
            "chars_per_show": round(chars / shows, 1) if shows else 0.0,
            "resent_tokens": int(resent),
            "status": "degraded" if decisions.get("error") else "ok",
            **ms_summary(ms),
        }

    def agent_ids(self, main: str) -> dict[str, str]:
        """``tool_use_id`` to agent id, from the ``.meta.json`` files of a session."""
        out: dict[str, str] = {}
        for path in Path(main[: -len(".jsonl")], "subagents").glob("agent-*.meta.json"):
            try:
                out[json.loads(path.read_text(encoding="utf-8"))["toolUseId"]] = (
                    path.name.removeprefix("agent-").removesuffix(".meta.json")
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return out

    def section_subagent(self) -> dict[str, Any]:
        kept, left_out = self.calls_of(AGENT_TOOLS)
        calls = [c for c in kept if c.agent == "main"]
        if not calls:
            return skipped("no Agent calls in a project with a memory folder")
        work = private_dir(self.work / "subagent")
        links = {main: self.agent_ids(main) for main in self.mains}
        chars = shows = rows = 0
        resent = 0.0
        ms: list[float] = []
        for call in calls:
            slug = call.slug
            env = dict(
                self.env,
                NOBLIVION_MEMORY_DIR=str(self.memory_of(slug)),
                NOBLIVION_RECALL_CACHE_DIR=str(work / "cache"),
            )
            common = {
                "session_id": call.session,
                "cwd": call.cwd or str(self.home),
                "transcript_path": call.transcript,
            }
            pre = {
                "hook_event_name": "PreToolUse",
                "tool_name": call.tool,
                "tool_input": call.input,
                "tool_use_id": call.tool_use_id,
                **common,
            }
            self.hooks.call("subagent_rules_hook.py", pre, env)
            digest = hashlib.sha256(call.tool_use_id.encode()).hexdigest()[:16]
            agent = links.get(call.transcript, {}).get(call.tool_use_id, digest)
            start = {
                "hook_event_name": "SubagentStart",
                "agent_id": agent,
                "agent_type": str(call.input.get("subagent_type") or "general-purpose"),
                **common,
            }
            out, spent = self.hooks.call("subagent_rules_hook.py", start, env)
            ms.append(spent)
            text = model_text("SubagentStart", out)
            if text:
                shows += 1
                chars += len(text)
                rows += row_count(text)
                resent += len(text) / 4 * len(self.scan.call_times.get((call.session, agent), []))
        states = fail_states(read_lines(work / "cache" / "subagent" / "recall.log"))
        return {
            "agent_calls": len(calls),
            "calls_without_memory_folder": left_out,
            "shows": shows,
            "rows": rows,
            "chars": chars,
            "chars_per_show": round(chars / shows, 1) if shows else 0.0,
            "resent_tokens": int(resent),
            "hook_states": dict(states),
            "status": "degraded" if states.get("fail") else "ok",
            **ms_summary(ms),
        }

    def offtopic_run(self, label: str) -> dict[str, Any]:
        doc = json.loads(OFFTOPIC_FILE.read_text(encoding="utf-8"))
        memory = self.memory_of(self.main_slug()) if self.mains else None
        if memory is None or not memory.is_dir():
            folders = sorted(self.projects.glob("*/memory"))
            memory = max(folders, key=lambda p: len(list(p.glob("*.md"))))
        env = dict(
            self.env,
            NOBLIVION_MEMORY_DIR=str(memory),
            NOBLIVION_RECALL_CACHE_DIR=str(self.work / label / "cache"),
        )
        with_rows = rows = chars = 0
        for i, prompt in enumerate(doc["prompts"]):
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": f"offtopic-{label}-{i}",
                "prompt": prompt,
            }
            out = self.hooks.call("recall_hook.py", event, env)[0].rstrip("\n")
            names = self.rows.shown_names(out)
            with_rows += 1 if names else 0
            rows += len(names)
            chars += len(out)
        total = len(doc["prompts"])
        return {
            "prompts": total,
            "prompts_with_rows": with_rows,
            "false_delivery": round(with_rows / total, 3),
            "rows_per_prompt": round(rows / total, 2),
            "chars_per_prompt": round(chars / total, 1),
        }

    # -- the frozen delivery checks --------------------------------------------

    def cases(self) -> list[dict[str, Any]]:
        out = []
        for name, reserve in (("candidates.json", False), ("candidates_reserve.json", True)):
            path = self.bench / "gate0" / name  # type: ignore[operator]
            if path.is_file():
                for case in json.loads(path.read_text(encoding="utf-8")):
                    out.append({**case, "reserve": reserve})
        return out

    def gate(self) -> Gate0:
        if self.gate0_runner is None:
            self.gate0_runner = Gate0(self, self.real_home())
        return self.gate0_runner

    def section_gate0(self) -> dict[str, Any]:
        if self.bench is None:
            return skipped("gate 0 skipped: " + self.bench_note)
        for folder in sorted(self.projects.glob("*/memory")):
            self.table_for_gate(folder)
        gate = self.gate()
        results: dict[str, Any] = {}
        for case in self.cases():
            if case["W"] == "W10":
                continue
            checks = gate.case(case)
            results[case["id"]] = {
                "W": case["W"],
                "reserve": case["reserve"],
                "pass": verdict(case, checks),
                "checks": checks,
            }
        report = self.gate_report(results)
        states = fail_states(read_lines(self.data / "cache" / "recall.log"))
        report["hook_states"] = dict(states)
        report["status"] = self.states_verdict(states)
        return report

    def table_for_gate(self, folder: Path) -> None:
        env = dict(self.env, NOBLIVION_MEMORY_DIR=str(folder))
        folders = self.gt.source_dirs(None, env)
        self.gt.rebuild(folders, self.gt.table_path(folders[0], env))

    @staticmethod
    def gate_report(results: Mapping[str, Any]) -> dict[str, Any]:
        main = [c for c in results.values() if not c["reserve"]]
        reserve = [c for c in results.values() if c["reserve"]]
        checks = [
            c for case in results.values() for c in case["checks"] if "(info)" not in c["check"]
        ]
        return {
            "cases": dict(results),
            "passed": sum(1 for c in main if c["pass"]),
            "total": len(main),
            "reserve_passed": sum(1 for c in reserve if c["pass"]),
            "reserve_total": len(reserve),
            "checks_passed": sum(1 for c in checks if c["pass"]),
            "checks_total": len(checks),
        }

    def slots(self) -> list[dict[str, str]]:
        """The test 4 slots with a lesson: id, lesson, folder, first prompt."""
        root = self.bench / "test4"  # type: ignore[operator]
        out: dict[str, dict[str, str]] = {}
        for line in read_lines(root / "results" / "test4.jsonl"):
            rec = json.loads(line)
            if not rec.get("lesson") or rec["task"] in out:
                continue
            path = root / "raw" / "test4" / rec["runid"] / "cands40.json"
            if not path.is_file():
                continue
            init = rec.get("init") or [{}]
            out[rec["task"]] = {
                "slot": rec["task"],
                "lesson": rec["lesson"],
                "cwd": str(init[0].get("cwd") or ""),
                "prompt": json.loads(path.read_text(encoding="utf-8"))["prompt"],
            }
        return list(out.values())

    def run_slots(self, label: str) -> dict[str, Any]:
        gate = self.gate()
        results = {}
        states: Counter = Counter()
        for slot in self.slots():
            lesson = slot["lesson"]
            sid = f"t4-{label}-{slot['slot']}"
            # A slot of its own: each real run had a fresh data folder, and rows
            # of one slot would count as hub shows for the next.
            cache = self.work / "test4" / sid
            cwd = slot["cwd"] or gate.frozen_home
            out = gate.prompt_rows(sid, slot["prompt"], cwd, cache).rstrip("\n")
            seen, rank = found(out, lesson, *gate.note_fields(lesson, slot["cwd"]))
            results[slot["slot"]] = {
                "delivered": seen,
                "rank": rank,
                "rows": len(self.rows.shown_names(out)),
                "chars": len(out),
            }
            states.update(fail_states(read_lines(cache / "recall.log")))
        total = len(results)
        return {
            "hook_states": dict(states),
            "slots": results,
            "delivered": sum(1 for s in results.values() if s["delivered"]),
            "total": total,
            "rows_per_slot": round(sum(s["rows"] for s in results.values()) / total, 2)
            if total
            else 0.0,
            "chars_per_slot": round(sum(s["chars"] for s in results.values()) / total, 1)
            if total
            else 0.0,
        }

    def section_test4(self) -> dict[str, Any]:
        if self.bench is None:
            return skipped("test 4 skipped: " + self.bench_note)
        if not self.slots():
            return skipped("test 4 skipped: no slots under test4/results and test4/raw")
        report = self.run_slots("shipped")
        report["status"] = self.states_verdict(report["hook_states"])
        return report

    def section_keyword(self) -> dict[str, Any]:
        """The store again with no embedder: real keyword mode, not a stub."""
        assert self.store is not None
        self.store.stop()
        env = dict(self.env, NOBLIVION_EMBED_BACKEND="none")
        health = self.store.start(env)
        report: dict[str, Any] = {"store_mode": health.get("mode")}
        if self.mains:
            run = self.replay_prompts({"keyword": {}}, "keyword")
            report["real_prompts"] = self.arm_report(
                run["prompts"]["keyword"], run["states"]["keyword"], set()
            )
        report["offtopic"] = self.offtopic_run("keyword-offtopic")
        if self.bench is not None and self.slots():
            slots = self.run_slots("keyword")
            report["test4"] = {k: v for k, v in slots.items() if k != "slots"}
        if health.get("mode") != "keyword":
            report["status"] = "degraded"
        return report

    def run_w10(self) -> None:
        """W10 changes the memory folder, so it runs last, on a fixture of its
        own: the notes it saves must not be in the corpus yet."""
        if self.bench is None or "gate0" not in self.sections:
            return
        cases = [c for c in self.cases() if c["W"] == "W10"]
        fixture = self.bench / "gate0" / "fx" / "w10"
        if not cases or not (fixture / ".claude" / "projects").is_dir():
            return
        assert self.store is not None
        self.store.stop()
        root = private_dir(self.root / "w10")
        self.projects = private_dir(root / "projects")
        data = private_dir(root / "data")
        saved = dict(self.env)
        self.env = dict(
            self.env,
            CLAUDE_CONFIG_DIR=str(root),
            CLAUDE_PLUGIN_DATA=str(data),
            NOBLIVION_DATA_DIR=str(data),
        )
        keep = dict(self.inputs)
        self.copy_corpus(fixture / ".claude" / "projects", self.projects)
        self.inputs = keep
        seed = fixture / ".claude" / "plugins" / "data" / DATA_NAME / DB_NAME
        self.seed_store(seed if seed.is_file() else None, data)
        shutil.copyfile(self.data / "config.json", data / "config.json")
        if (self.data / "models").is_dir():
            shutil.copytree(self.data / "models", data / "models")
        store = Store(data, self.store_python(), self.plugin / "src", self.args.store_wait)
        self.store = store
        store.index(self.env)
        store.start(self.env)
        self.gate0_runner = None
        gate = self.gate()
        results = dict(self.sections["gate0"].get("cases") or {})
        try:
            self.table_for_gate(self.projects / slugify(cases[0]["cwd"]) / "memory")
            for case in cases:
                checks = gate.remember(case, store)
                results[case["id"]] = {
                    "W": "W10",
                    "reserve": False,
                    "pass": verdict(case, checks),
                    "checks": checks,
                }
        finally:
            self.env = saved
        status = self.sections["gate0"].get("status", "ok")
        seconds = self.sections["gate0"].get("seconds", 0.0)
        self.sections["gate0"] = {**self.gate_report(results), "status": status, "seconds": seconds}

    # -- cost ------------------------------------------------------------------

    def section_mcp(self) -> dict[str, Any]:
        """``tools/list`` and the server instructions of two starts of the server,
        as Claude Code starts it."""
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        answers = []
        for start in range(2):
            run = subprocess.run(
                ["python3", str(self.plugin / "mcp" / "recall_mcp.py")],
                input=request,
                env=self.env,
                cwd=str(self.work if start else self.home),
                capture_output=True,
                text=True,
                timeout=30,
            )
            lines = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
            init = next(m for m in lines if m.get("id") == 1)["result"]
            tools = next(m for m in lines if m.get("id") == 2)["result"]["tools"]
            answers.append((json.dumps(tools, sort_keys=True), str(init.get("instructions") or "")))
        tools_text, instructions = answers[0]
        return {
            "stable": answers[0] == answers[1],
            "tools_sha256": hashlib.sha256(tools_text.encode()).hexdigest()[:16],
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
            "schema_chars": len(tools_text),
            "instructions_chars": len(instructions),
            "status": "ok" if answers[0] == answers[1] else "degraded",
        }

    def skill_chars(self) -> int:
        """The skill lines the model sees: the name and description of each skill."""
        total = 0
        for path in sorted((self.plugin / "skills").glob("*/SKILL.md")):
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
            for key in ("name", "description"):
                match = re.search(rf"^{key}:\s*(.*)$", head, re.M)
                total += len(match.group(1)) if match else 0
        return total

    def section_cost(self) -> dict[str, Any]:
        count = self.scan.count
        if not count["api_calls"]:
            return skipped("no API calls in the transcripts")
        hooks = {}
        for (event, script), hook in sorted(self.scan.hooks.items()):
            hooks[f"{event} {script}"] = {
                "runs": hook.runs,
                "shown": hook.shown,
                "chars": hook.chars,
                "chars_per_show": round(hook.chars / hook.shown, 1) if hook.shown else 0.0,
                "resent_tokens": int(hook.resent_tokens),
                **ms_summary(hook.ms),
            }
        recorded_chars = sum(h["chars"] for h in hooks.values())
        recorded_resent = sum(h["resent_tokens"] for h in hooks.values())
        replayed = self.replayed_resent()
        replay_resent = self.replay_total(replayed)
        listing = self.sections.get("mcp_listing") or {}
        fixed_chars = (
            listing.get("schema_chars", 0)
            + listing.get("instructions_chars", 0)
            + self.skill_chars()
        )
        fixed_tokens = fixed_chars / 4 * count["api_calls"]
        caused = (
            count["recall_calls"]
            + count["remember_calls"]
            + count["toolsearch_noblivion"]
            + count["stop_blocks"]
        )
        mean_input = count["input_tokens"] / count["api_calls"]

        def share(resent: float) -> float:
            total = resent + fixed_tokens + caused * mean_input
            return round(total / max(1, count["input_tokens"]), 4)

        sessions = max(1, len(self.mains))
        return {
            "sessions": len(self.mains),
            "api_calls": count["api_calls"],
            "api_calls_per_session": round(count["api_calls"] / sessions, 1),
            "input_tokens": count["input_tokens"],
            "input_tokens_per_session": count["input_tokens"] // sessions,
            "mean_input_per_call": int(mean_input),
            "prompts": count["prompts"],
            "prompt_events": count["prompt_events"],
            "turns": count["stop_events"],
            "tool_calls": count["tool_calls"],
            "bash_calls": count["bash_calls"],
            "caused_calls": caused,
            "caused_detail": {
                "recall": count["recall_calls"],
                "remember": count["remember_calls"],
                "toolsearch": count["toolsearch_noblivion"],
                "stop_blocks": count["stop_blocks"],
            },
            "fixed_chars_per_call": fixed_chars,
            "recorded_chars": recorded_chars,
            "recorded_resent_tokens": recorded_resent,
            "recorded_share": share(recorded_resent),
            "replay_resent_by_hook": replayed,
            "replay_resent_tokens": int(replay_resent),
            "replay_share": share(replay_resent),
            "by_hook": hooks,
        }

    def replayed_resent(self) -> dict[str, int]:
        """The re-sent tokens of this build, by replay. The hooks with no replay
        (session start) keep their recorded number in ``replay_total``."""
        out = {}
        sources = {
            "prompt_rows": lambda s: s["arms"]["shipped"]["resent_tokens"],
            "guard_calls": lambda s: s["resent_tokens"],
            "error_recall": lambda s: s["resent_tokens"],
            "subagent": lambda s: s["resent_tokens"],
        }
        for name, read in sources.items():
            section = self.sections.get(name) or {}
            if section.get("status") in ("ok", "degraded"):
                out[name] = int(read(section))
        return out

    def replay_total(self, replayed: Mapping[str, int]) -> float:
        replaced = {
            "prompt_rows": "recall_hook.py",
            "guard_calls": "guard_hook.py",
            "error_recall": "error_recall_hook.py",
            "subagent": "subagent_rules_hook.py",
        }
        gone = {replaced[name] for name in replayed}
        kept = sum(
            hook.resent_tokens
            for (_, script), hook in self.scan.hooks.items()
            if script.split()[0] not in gone
        )
        return kept + sum(replayed.values())

    def cold_groups(self) -> dict[str, tuple[str, str | None, dict[str, Any]]]:
        """Sample events, one per way a hook group fires: the hook event, the tool
        name its matcher sees, and the event fields."""
        bash = {"tool_input": {"command": "true"}}
        read = {"tool_input": {"file_path": str(self.home / "file.txt")}}
        edit = {"tool_input": {"file_path": str(self.home / "file.txt"), "old_string": "a"}}
        return {
            "UserPromptSubmit": ("UserPromptSubmit", None, {"prompt": "merge the pull request"}),
            "PreToolUse Bash": ("PreToolUse", "Bash", bash),
            "PreToolUse Read": ("PreToolUse", "Read", read),
            "PreToolUse Edit": ("PreToolUse", "Edit", edit),
            "PostToolUse Bash": ("PostToolUse", "Bash", {**bash, "tool_response": {"stdout": ""}}),
            "PostToolUse Read": ("PostToolUse", "Read", {**read, "tool_response": {}}),
            "PostToolUse Edit": ("PostToolUse", "Edit", {**edit, "tool_response": {}}),
            "SubagentStart": ("SubagentStart", None, {"agent_id": "a1", "agent_type": "x"}),
            "Stop": ("Stop", None, {"stop_hook_active": False}),
        }

    def section_hook_time(self) -> dict[str, Any]:
        """Cold runs of each hook of ``hooks/hooks.json``, as Claude Code starts
        it. All hooks of one event run in parallel, so the slowest one sets the
        wait (R2). The turn figure weighs the waits by the live mix of the sample."""
        spec = json.loads((self.plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(self.home))
        memory = self.memory_of(self.main_slug()) if self.mains else None
        if memory is not None and memory.is_dir():
            env["NOBLIVION_MEMORY_DIR"] = str(memory)
            self.table_for_gate(memory)
        waits: dict[str, float] = {}
        per_hook: dict[str, Any] = {}
        for group, (event, tool, fields) in self.cold_groups().items():
            payload = {
                "hook_event_name": event,
                "session_id": "coldtimer",
                "cwd": str(self.home),
                **fields,
            }
            if tool:
                payload["tool_name"] = tool
            commands = []
            for block in spec["hooks"].get(event, []):
                matcher = block.get("matcher")
                if tool and matcher and not re.fullmatch(matcher, tool):
                    continue
                commands += [h["command"] for h in block.get("hooks", []) if h.get("command")]
            slowest = 0.0
            for command in commands:
                times = []
                for _ in range(self.args.cold_runs):
                    started = time.perf_counter()
                    subprocess.run(
                        command,
                        shell=True,  # noqa: S602 - the command line of hooks.json, as Claude Code runs it
                        input=json.dumps(payload),
                        env=env,
                        cwd=str(self.home),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    times.append((time.perf_counter() - started) * 1000)
                median = statistics.median(times)
                slowest = max(slowest, median)
                name = script_of(command.replace('"', "")) or command
                per_hook[f"{group} {name}"] = round(median, 1)
            waits[group] = round(slowest, 1)
        return {
            "cold_ms_by_hook": per_hook,
            "wait_ms_by_event": waits,
            "wait_per_turn_s": self.wait_per_turn(waits),
            "recorded_stop_ms": {k: ms_summary(v) for k, v in sorted(self.scan.stop_ms.items())},
        }

    def wait_per_turn(self, waits: Mapping[str, float]) -> float | None:
        """Hook wait per turn: the events of the sample times the wait of each."""
        turns = self.scan.count["stop_events"]
        if not turns:
            return None
        tools: Counter = Counter()
        for call in self.scan.tool_calls:
            kind = (
                "Bash"
                if call.tool == "Bash"
                else "Read"
                if call.tool in ("Read", "Grep")
                else "Edit"
                if call.tool in ("Edit", "Write", "MultiEdit")
                else None
            )
            if kind:
                tools[kind] += 1
            else:
                tools["other"] += 1
        total = self.scan.count["prompt_events"] * waits.get("UserPromptSubmit", 0.0)
        for kind in ("Bash", "Read", "Edit"):
            total += tools[kind] * waits.get(f"PreToolUse {kind}", 0.0)
            total += tools[kind] * waits.get(f"PostToolUse {kind}", 0.0)
        total += tools["other"] * waits.get("PostToolUse Read", 0.0)
        starts = sum(1 for c in self.scan.tool_calls if c.tool in AGENT_TOOLS)
        total += starts * waits.get("SubagentStart", 0.0)
        total += turns * waits.get("Stop", 0.0)
        return round(total / turns / 1000, 3)

    # -- the result ------------------------------------------------------------

    def build_info(self) -> dict[str, Any]:
        def git(*argv: str) -> str:
            command = ["git", "--no-optional-locks", "-C", str(self.plugin), *argv]
            run = subprocess.run(command, capture_output=True, text=True)
            return run.stdout.strip() if run.returncode == 0 else ""

        try:
            plugin_json = self.plugin / ".claude-plugin" / "plugin.json"
            version = json.loads(plugin_json.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            version = None
        code = [
            (str(p.relative_to(self.plugin)), p)
            for folder in ("hooks", "mcp", "src", "config")
            for p in (self.plugin / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ]
        inside = git("rev-parse", "--show-toplevel") == str(self.plugin)
        return {
            "label": self.args.label or (git("rev-parse", "--short", "HEAD") if inside else ""),
            "version": version,
            "commit": git("rev-parse", "HEAD") if inside else "",
            "dirty": bool(git("status", "--porcelain", "--", "hooks", "mcp", "src", "config"))
            if inside
            else False,
            "code_digest": tree_digest(code),
        }

    def run(self) -> dict[str, Any]:
        started = time.time()
        self.select_inputs()
        self.prepare_home()
        self.start_store()
        self.scan_transcripts()
        self.run_section("mcp_listing", self.section_mcp)
        self.run_section("prompt_rows", self.section_prompt_rows)
        self.run_section("guard_log", self.section_guard_log)
        self.run_section("guard_calls", self.section_guard_calls)
        self.run_section("error_recall", self.section_error_recall)
        self.run_section("subagent", self.section_subagent)
        self.run_section("offtopic", lambda: self.offtopic_run("offtopic"))
        self.run_section("gate0", self.section_gate0)
        self.run_section("test4", self.section_test4)
        self.run_section("hook_time", self.section_hook_time)
        self.run_section("cost", self.section_cost)
        self.run_section("keyword_mode", self.section_keyword)
        try:
            self.run_w10()
        except Exception as exc:  # noqa: BLE001 - reported as a failed section
            gate = self.sections.setdefault("gate0", {})
            gate["status"] = "error"
            gate["reason"] = f"W10: {type(exc).__name__}: {exc}"[:300]
        return {
            "schema": SCHEMA,
            "command": sys.argv[1:],
            "started_at": _dt.datetime.fromtimestamp(started, _dt.timezone.utc).isoformat(),
            "seconds": round(time.time() - started, 1),
            "build": self.build_info(),
            "settings": self.settings,
            "store": {
                k: self.health.get(k) for k in ("version", "memories", "mode", "schema_version")
            },
            "embedding_model": (self.health.get("embedding") or {}).get("model"),
            "inputs": self.inputs,
            "bench": str(self.bench) if self.bench else "",
            "sections": self.sections,
        }

    def close(self) -> None:
        if self.store is not None:
            self.store.stop()
        if not self.args.keep_work and not self.args.work_dir:
            shutil.rmtree(self.root, ignore_errors=True)


# -- the report ------------------------------------------------------------------

# ``(label, dotted path, better direction)``. ``up`` means more is better.
METRICS: tuple[tuple[str, str, str], ...] = (
    ("gate 0 cases passed", "gate0.passed", "up"),
    ("gate 0 reserve cases passed", "gate0.reserve_passed", "up"),
    ("gate 0 checks passed", "gate0.checks_passed", "up"),
    ("test 4 slots, lesson in prompt rows", "test4.delivered", "up"),
    ("test 4 rows per slot", "test4.rows_per_slot", "down"),
    ("test 4 chars per slot", "test4.chars_per_slot", "down"),
    ("prompt chars per typed prompt", "prompt_rows.arms.shipped.chars_per_prompt", "down"),
    ("prompt chars per hook event", "prompt_rows.arms.shipped.chars_per_event", "down"),
    ("prompt chars p95", "prompt_rows.arms.shipped.chars_p95", "down"),
    ("prompt rows per prompt", "prompt_rows.arms.shipped.rows_per_prompt", "down"),
    ("prompt hub share", "prompt_rows.arms.shipped.hub_share", "down"),
    ("hub note, top session share", "prompt_rows.arms.shipped.hub_notes_max_session_share", "down"),
    ("prompt hook ms p50", "prompt_rows.arms.shipped.ms_p50", "down"),
    ("prompt hook ms p95", "prompt_rows.arms.shipped.ms_p95", "down"),
    ("off-topic false delivery", "offtopic.false_delivery", "down"),
    ("keyword off-topic false delivery", "keyword_mode.offtopic.false_delivery", "down"),
    ("keyword chars per prompt", "keyword_mode.real_prompts.chars_per_prompt", "down"),
    ("keyword test 4 slots, lesson in rows", "keyword_mode.test4.delivered", "up"),
    ("guard log denies kept", "guard_log.deny_kept", "none"),
    ("guard log chars per call", "guard_log.chars_per_call", "down"),
    ("guard calls with text", "guard_calls.share_with_text", "down"),
    ("guard chars per call", "guard_calls.chars_per_call", "down"),
    ("guard hook ms p50", "guard_calls.ms_p50", "down"),
    ("error recall shows", "error_recall.shows", "none"),
    ("error recall chars per show", "error_recall.chars_per_show", "down"),
    ("subagent shows", "subagent.shows", "none"),
    ("subagent chars per show", "subagent.chars_per_show", "down"),
    ("re-sent tokens, replay", "cost.replay_resent_tokens", "down"),
    ("plugin share of input, replay", "cost.replay_share", "down"),
    ("fixed chars per API call", "cost.fixed_chars_per_call", "down"),
    ("hook wait per turn, s", "hook_time.wait_per_turn_s", "down"),
)


def lookup(sections: Mapping[str, Any], dotted: str) -> Any:
    value: Any = sections
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.3f}" if abs(value) < 10 else f"{value:,.1f}"
    return f"{int(value):,}"


def delta_text(before: Any, after: Any, better: str) -> str:
    if before is None or after is None:
        return ""
    change = after - before
    if not change:
        return "same"
    mark = ""
    if better != "none":
        mark = " better" if (change > 0) == (better == "up") else " worse"
    percent = f" ({change / before:+.0%})" if before else ""
    return f"{change:+,.3f}".rstrip("0").rstrip(".") + percent + mark


def summary(doc: Mapping[str, Any]) -> str:
    """The short text report of one run."""
    build = doc["build"]
    store = doc["store"]
    inputs = doc["inputs"]
    sections = doc["sections"]
    lines = [
        f"NOBLIVION eval suite: build {build['label'] or '?'} ({build['version']}), "
        f"{doc['seconds']:.0f} s",
        f"store: {store['mode']}, {store['memories']} notes. Input: {inputs.get('transcripts')} "
        f"sessions, {inputs.get('subagent_transcripts')} subagent files, "
        f"{inputs.get('guard_log_lines')} guard log lines, digest "
        f"{inputs.get('transcripts_digest')}",
    ]
    for label, path, _ in METRICS:
        value = lookup(sections, path)
        if value is not None:
            lines.append(f"  {label:<36} {number(value):>14}")
    accept = lookup(sections, "prompt_rows.acceptance_121")
    if accept:
        lines.append(
            f"NOBLIVION-121: {accept['chars_per_prompt']} chars per prompt, target "
            f"{accept['chars_target']}: {'met' if accept['chars_met'] else 'not met'}. "
            f"{accept['rows_per_prompt']} rows per prompt, at most {accept['rows_max']}. "
            f"Hub notes {accept['hub_notes']}, top session share "
            f"{accept['hub_notes_max_session_share']}, target under {accept['hub_target']}: "
            f"{'met' if accept['hub_met'] else 'not met'}. Hub share of shown notes "
            f"{accept['hub_share']}. Labelled precision not measured."
        )
    for name in SECTIONS:
        section = sections.get(name)
        if section is None:
            lines.append(f"{name}: not run")
        elif section.get("status") != "ok":
            lines.append(
                f"{name}: {section.get('status')}: {section.get('reason', '')}".rstrip(": ")
            )
    return "\n".join(lines) + "\n"


def compare(before: Mapping[str, Any], after: Mapping[str, Any], allow: bool) -> tuple[str, int]:
    """The before-and-after report and its exit code."""
    lines = [
        f"before: {before['build']['label'] or '?'} ({before['build']['code_digest']})",
        f"after:  {after['build']['label'] or '?'} ({after['build']['code_digest']})",
    ]
    code = 0
    if before["build"]["code_digest"] == after["build"]["code_digest"]:
        lines.append("note: the two runs measured the same code")
    input_keys = ("transcripts_digest", "corpus_digest", "bench_digest")
    changed = [k for k in input_keys if before["inputs"].get(k) != after["inputs"].get(k)]
    if (before["store"].get("mode"), before.get("embedding_model")) != (
        after["store"].get("mode"),
        after.get("embedding_model"),
    ):
        changed.append("store mode")
    if before.get("settings") != after.get("settings"):
        changed.append("NOBLIVION_* settings")
    if changed:
        lines.append("inputs differ: " + ", ".join(changed))
        if not allow:
            code = 2
    width = 36
    lines.append(f"{'':<{width}} {'before':>14} {'after':>14}  change")
    for label, path, better in METRICS:
        old, new = lookup(before["sections"], path), lookup(after["sections"], path)
        if old is None and new is None:
            continue
        lines.append(
            f"{label:<{width}} {number(old):>14} {number(new):>14}  {delta_text(old, new, better)}"
        )
    dropped: list[str] = []
    check_drops: list[str] = []
    no_verdict: list[str] = []
    for name, cases_key, passed in (("gate0", "cases", "pass"), ("test4", "slots", "delivered")):
        old_section, new_section = before["sections"].get(name), after["sections"].get(name)
        old_ran = bool(old_section) and old_section.get("status") not in ("skipped", "error")
        new_ran = bool(new_section) and new_section.get("status") not in ("skipped", "error")
        if not old_ran and not new_ran:
            lines.append(f"{name}: not run in either build, no delivery verdict")
            continue
        if not new_ran or not old_ran:
            no_verdict.append(name)
            continue
        old_cases, new_cases = old_section[cases_key], new_section[cases_key]
        for case, old in old_cases.items():
            new = new_cases.get(case)
            if new is None:
                no_verdict.append(f"{name} {case}")
            elif old[passed] and not new[passed]:
                dropped.append(f"{name} {case}")
            elif name == "gate0":
                gone = {c["check"] for c in old["checks"] if c["pass"]} - {
                    c["check"] for c in new["checks"] if c["pass"]
                }
                check_drops += [f"{case}/{c}" for c in sorted(gone) if "(info)" not in c]
    if check_drops:
        lines.append("checks that passed before, case still passes: " + ", ".join(check_drops))
    if no_verdict:
        lines.append("no delivery verdict for: " + ", ".join(no_verdict))
        code = code or 2
    if dropped:
        lines.append("DELIVERY DROPPED: " + ", ".join(dropped))
        code = 1
    elif not no_verdict:
        lines.append("delivery: not lower")
    for name in SECTIONS:
        status = (after["sections"].get(name) or {}).get("status")
        if status in ("error", "degraded"):
            lines.append(f"after: {name} is {status}")
            code = code or 2
    return "\n".join(lines) + "\n", code


# -- the command -----------------------------------------------------------------


def default_live_data() -> str:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return str(Path(config) / "plugins" / "data" / DATA_NAME)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="measure one build on a scratch store")
    run.add_argument("--out", required=True, help="folder for result.json and summary.txt")
    run.add_argument("--bench", help="folder of the frozen gate 0 and test 4 inputs")
    run.add_argument("--corpus", help="folder of <project>/memory/*.md, when --bench is absent")
    run.add_argument("--seed-db", help="a database of the corpus, to skip the first embedding")
    run.add_argument("--transcripts", action="append", default=[], help="glob, repeatable")
    run.add_argument("--since-days", type=float, default=7.0, help="0 for every session")
    run.add_argument("--guard-log", help="default: guard-log.jsonl of --live-data")
    run.add_argument("--inputs", help="the inputs folder of an earlier --freeze-inputs run")
    run.add_argument("--freeze-inputs", action="store_true", help="copy the input under --out")
    run.add_argument("--plugin-root", default=str(REPO), help="the build to measure")
    run.add_argument("--label", default="", help="name of the build, default its short commit")
    run.add_argument("--config", help="default: config/config.default.json of the build")
    run.add_argument("--env", action="append", default=[], help="NAME=VALUE for every hook")
    run.add_argument("--live-data", default=default_live_data(), help="read only")
    run.add_argument("--store-python", help="python with numpy and fastembed")
    run.add_argument("--models", help="embedding model folder, default in --live-data")
    run.add_argument("--work-dir", help="scratch folder, kept after the run")
    run.add_argument("--keep-work", action="store_true", help="keep the temporary scratch folder")
    run.add_argument("--store-wait", type=float, default=900.0, help="seconds to wait for vectors")
    run.add_argument("--cold-runs", type=int, default=7, help="cold starts per hook")
    run.add_argument(
        "--sections", type=lambda s: s.split(","), default=list(SECTIONS), help="comma list"
    )
    diff = commands.add_parser("compare", help="report two results, exit 1 on a delivery drop")
    diff.add_argument("before", help="result.json of the earlier build")
    diff.add_argument("after", help="result.json of the later build")
    diff.add_argument("--out", help="write the report here too")
    diff.add_argument(
        "--allow-input-change", action="store_true", help="give a verdict on different inputs"
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    unknown = set(args.sections) - set(SECTIONS)
    if unknown:
        raise SystemExit(f"unknown sections: {', '.join(sorted(unknown))}")
    if args.bench and args.corpus and Path(args.bench).expanduser().is_dir():
        raise SystemExit("--bench brings its corpus: give --bench or --corpus, not both")
    if args.inputs and args.freeze_inputs:
        raise SystemExit("--inputs reads a frozen input: --freeze-inputs has nothing to do")
    # A stopped run must still stop its store: the store has no idle exit here.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    suite = Suite(args)
    try:
        doc = suite.run()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"suite failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        suite.close()
    write_private(suite.out / "result.json", json.dumps(doc, indent=1, sort_keys=True) + "\n")
    text = summary(doc)
    write_private(suite.out / "summary.txt", text)
    print(text, end="")
    bad = [n for n, s in doc["sections"].items() if s.get("status") in ("error", "degraded")]
    if bad:
        print(f"failed sections: {', '.join(bad)}", file=sys.stderr)
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run_command(args)
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    text, code = compare(before, after, args.allow_input_change)
    if args.out:
        write_private(Path(args.out), text)
    print(text, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
