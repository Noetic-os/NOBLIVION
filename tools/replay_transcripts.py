# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay past Claude Code sessions through the trust signals, offline (NOBLIVION-39).

The trust signals (docs/trust.md) need sessions in which the plugin showed
notes. A user who has used Claude Code for a while has many past sessions but
no trust data. This script makes the data from those transcripts, with the
plugin's own code:

1. PROMPT. For each session (oldest first) and each prompt the user typed,
   the script runs the prompt hook (``hooks/recall_hook.py`` ``main``) in
   this process, with the hook's own defaults, against a running store. The
   hook writes what it would write live: the ``recall`` events of the notes
   it showed and, with a signal on, their names and rules
   (``<session>.trust-shown.jsonl``). The hook's clock is set to the time of
   the prompt in the transcript, so every event has the original time.
2. STOP. After the last prompt of the session, the Stop flush's own scan
   (``hooks/trust_flush.py`` ``scan_signals``) reads the transcript: a reply
   that cites a shown note is a ``use``, a correction of a cited note is a
   ``contradict``.
3. STORE. The session's events, merged as the flush merges them
   (``trust_flush.read_unsent``), go into the store database through the
   store's own checks (``noblivion.trust.parse_batch`` and ``store_batch``),
   with the time of the session's last event as "now". So a transcript older
   than the route's 80-day window is kept too.

Then ``noblivion trust timesplit`` and ``noblivion trust report`` read the
store as usual. With ``--shadow`` (the default) the hook also writes its
trust log (``NOBLIVION_RECALL_INDEX_TRUST=shadow``), so the time-split test
has per-prompt units. Shadow mode serves the order unchanged.

A prompt here is a user record that ``trust_signals.user_prompt`` accepts,
less a compaction summary and the "[Request interrupted" marker: Claude Code
runs no prompt hook for those. Subagent transcripts are not replayed.

``--birth-from-tool-calls``: the memory folder is read as it is today, so a
note written in a later session could be shown at an earlier prompt. With
this option a note exists only from the first tool call, in any transcript
(subagents too), whose input names its file. A note that no tool call names
exists from the start. The hook gets the store's answer without the notes
that did not exist yet.

The replay keeps its place in ``<data dir>/replay-state.json``: a run that
stops (``--max-seconds``) goes on from the next session. ``--trace PATH``
writes one line per signal event with the reply sentence or the prompt that
caused it, for labelling by hand. That file holds transcript text: keep it
private.

Usage, with the store of a scratch data dir running::

    export NOBLIVION_DATA_DIR=/tmp/replay-data
    noblivion index --db "$NOBLIVION_DATA_DIR/noblivion.db" --memory-dir DIR
    noblivion ensure-running
    python tools/replay_transcripts.py --memory-dir DIR \\
        --birth-from-tool-calls ~/.claude/projects/<project>/*.jsonl
    noblivion trust timesplit

Never point it at the data dir of a live install: it writes trust events.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import importlib.util
import io
import json
import os
import re
import sys
import time
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
STATE_NAME = "replay-state.json"
INTERRUPT_PREFIX = "[Request interrupted"
TRACE_TEXT_MAX = 1200
_MD_MARK_RE = re.compile(r"\[[a-z_]+:\s*([\w.-]{1,200}\.md)\]")
_MD_NAME_RE = re.compile(r"[\w.-]{1,200}\.md")


# -- the transcripts -------------------------------------------------------------


def read_jsonl(path: str) -> Iterator[dict]:
    """The JSON objects of a transcript, one per line; a bad line is skipped."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def first_ts(path: str) -> str:
    """The first record time of a transcript, or "" when it has none."""
    for rec in read_jsonl(path):
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return ""


def main_transcripts(patterns: Iterable[str]) -> list[str]:
    """The ``.jsonl`` files the patterns name, oldest session first. A file
    under a ``subagents`` folder is not a main transcript."""
    found: set[str] = set()
    for pattern in patterns:
        for path in glob.glob(os.path.expanduser(pattern)) or []:
            if path.endswith(".jsonl") and "subagents" not in Path(path).parts:
                found.add(os.path.abspath(path))
    stamped = [(first_ts(p), p) for p in sorted(found)]
    return [p for ts, p in sorted(stamped) if ts]


def prompt_text(rec: Mapping[str, Any], signals: Any) -> str | None:
    """The full text of a prompt the user typed, or None. The same test as
    the Stop scan (``trust_signals.user_prompt``), less the records for which
    Claude Code runs no prompt hook."""
    if signals.user_prompt(rec) is None or rec.get("isCompactSummary"):
        return None
    text = signals._text_blocks((rec.get("message") or {}).get("content"))
    if not isinstance(text, str) or text.lstrip().startswith(INTERRUPT_PREFIX):
        return None
    return text


def _iso(value: str) -> _dt.datetime | None:
    try:
        moment = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)


# -- note births -----------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def note_births(paths: Iterable[str], names: set[str]) -> dict[str, _dt.datetime]:
    """``{file name: first time a tool call names it}`` over ``paths``. Only
    the inputs of ``tool_use`` blocks count: a tool result can list a note
    that another tool shows."""
    out: dict[str, _dt.datetime] = {}
    for path in paths:
        for rec in read_jsonl(path):
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            moment = None
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                for text in _strings(block.get("input")):
                    for name in _MD_NAME_RE.findall(text):
                        if name not in names:
                            continue
                        moment = moment or _iso(str(rec.get("timestamp") or ""))
                        if moment is not None and (name not in out or moment < out[name]):
                            out[name] = moment
    return out


def all_transcripts(main_paths: Iterable[str]) -> list[str]:
    """The main transcripts and the subagent transcripts of their sessions."""
    out: list[str] = []
    for path in main_paths:
        out.append(path)
        out.extend(
            sorted(glob.glob(os.path.join(path[: -len(".jsonl")], "**", "*.jsonl"), recursive=True))
        )
    return out


def filter_payload(payload: Any, exists: Callable[[str], bool]) -> Any:
    """The store's search answer without the notes that do not exist yet. A
    result names its file as ``[<source>: <name>.md]``; every list of the
    answer as long as ``results`` loses the same places."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return payload
    results = payload["results"]
    keep = []
    for i, item in enumerate(results):
        m = _MD_MARK_RE.search(item) if isinstance(item, str) else None
        if m is None or exists(m.group(1)):
            keep.append(i)
    if len(keep) == len(results):
        return payload
    out = dict(payload)
    for key, value in payload.items():
        if isinstance(value, list) and len(value) == len(results):
            out[key] = [value[i] for i in keep]
    return out


# -- the plugin's code -------------------------------------------------------------


def _load(name: str, alias: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(alias, HOOKS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


class Clock:
    """The replay's time. The hook modules read it through ``_dt``."""

    def __init__(self) -> None:
        self.now = _dt.datetime.now(_dt.timezone.utc)
        clock = self

        class _Datetime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return clock.now if tz is None else clock.now.astimezone(tz)

        self.module = types.SimpleNamespace(
            datetime=_Datetime, timezone=_dt.timezone, timedelta=_dt.timedelta
        )


class Plugin:
    """The prompt hook, the Stop scan and the event helpers, loaded once,
    sharing one ``trust_events`` module whose clock is the replay's."""

    def __init__(self, clock: Clock) -> None:
        self.hook = _load("recall_hook", "replay_recall_hook")
        self.flush = _load("trust_flush", "replay_trust_flush")
        self.te = self.hook._sibling_module("trust_events")
        self.signals = self.hook._sibling_module("trust_signals")
        rank = self.hook._sibling_module("trust_rank")
        for mod in (self.te, rank):
            mod._dt = clock.module
        self.flush._MODS["trust_events"] = self.te
        self.flush._MODS["trust_signals"] = self.signals
        self.signals._MODS["trust_events"] = self.te
        self.exists: Callable[[str], bool] = lambda name: True
        store_get = self.hook.store_get

        def _store_get(build: Any, environ: Any, timeout_s: float) -> Any:
            return filter_payload(store_get(build, environ, timeout_s), self.exists)

        self.hook.store_get = _store_get

    def prompt(self, sid: str, text: str, env: Mapping[str, str]) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": sid,
            "prompt": text,
        }
        self.hook.main(io.StringIO(json.dumps(payload)), io.StringIO(), dict(env))


def replay_env(base: Mapping[str, str], memory_dir: str, shadow: bool) -> dict[str, str]:
    env = dict(base)
    env["NOBLIVION_MEMORY_DIR"] = memory_dir
    env["NOBLIVION_TRUST_CITATION_USE"] = "1"
    env["NOBLIVION_TRUST_CORRECTION_CONTRADICT"] = "1"
    env.pop("CLAUDE_PROJECT_DIR", None)
    if shadow:
        env["NOBLIVION_RECALL_INDEX_TRUST"] = "shadow"
    return env


# -- one session -----------------------------------------------------------------


def _cited_sentences(text: str, note: Mapping[str, str], signals: Any) -> str:
    out = [
        s.strip()
        for s in signals.sentences(text)
        if signals.cites(s, note.get("name", ""), note.get("rule", ""))
    ]
    return " | ".join(out)[:TRACE_TEXT_MAX]


def trace_lines(
    sid: str,
    records: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    shown: Mapping[int, Mapping[str, str]],
    signals: Any,
    te: Any,
) -> list[dict]:
    """One line per citation or correction event of the session's spool
    (``events`` before the merge, which drops ``src``), with the text that
    caused it."""
    by_ts: dict[str, list[Mapping[str, Any]]] = {}
    for rec in records:
        ts = te.norm_ts(rec.get("timestamp"))
        if ts is not None:
            by_ts.setdefault(ts, []).append(rec)
    out: list[dict] = []
    for ev in events:
        if ev.get("src") not in (signals.SRC_CITATION, signals.SRC_CORRECTION):
            continue
        note = shown.get(int(ev["mv_id"]), {})
        line = {
            "session": sid,
            "ts": ev["ts"],
            "kind": ev["kind"],
            "src": ev["src"],
            "mv_id": ev["mv_id"],
            "name": note.get("name", ""),
            "rule": note.get("rule", ""),
        }
        if ev["src"] == signals.SRC_CITATION:
            texts = [signals.reply_text(r) or "" for r in by_ts.get(ev["ts"], [])]
            line["text"] = " | ".join(
                t for t in (_cited_sentences(x, note, signals) for x in texts) if t
            )[:TRACE_TEXT_MAX]
        else:
            prompts = [signals.user_prompt(r) or "" for r in by_ts.get(ev["ts"], [])]
            line["text"] = " | ".join(p for p in prompts if p)[:TRACE_TEXT_MAX]
            cited = ""
            for rec in records:
                ts = te.norm_ts(rec.get("timestamp"))
                if ts is None or ts >= ev["ts"]:
                    continue
                reply = signals.reply_text(rec)
                if reply and signals.cites(reply, note.get("name", ""), note.get("rule", "")):
                    cited = _cited_sentences(reply, note, signals)
            line["cited_text"] = cited
        out.append(line)
    return out


def replay_session(
    path: str,
    plugin: Plugin,
    clock: Clock,
    env: Mapping[str, str],
    conn: Any,
    births: Mapping[str, _dt.datetime] | None = None,
) -> dict[str, Any]:
    """Replay one main transcript. Returns its counts and its trace lines."""
    from noblivion import trust

    te, signals = plugin.te, plugin.signals
    sid = te.valid_sid(Path(path).stem)
    counts: dict[str, Any] = {"prompts": 0, "shown": 0, "events": {}, "trace": []}
    if sid is None:
        return counts
    records = list(read_jsonl(path))
    for rec in records:
        text = prompt_text(rec, signals)
        moment = _iso(str(rec.get("timestamp") or "")) if text is not None else None
        if text is None or moment is None:
            continue
        clock.now = moment
        if births is not None:
            plugin.exists = lambda name, m=moment: name not in births or births[name] <= m
        plugin.prompt(sid, text, env)
        counts["prompts"] += 1
    plugin.exists = lambda name: True
    cache = te.cache_dir(env)
    state: dict[str, Any] = {}
    plugin.flush.scan_signals(cache, sid, path, state, env)
    events, _end, _bad = plugin.flush.read_unsent(cache, sid, 0)
    shown = signals.load_shown(cache, sid)
    counts["shown"] = len(shown)
    for ev in events:
        key = f"{ev['kind']}:{ev.get('src', '')}"
        counts["events"][key] = counts["events"].get(key, 0) + 1
    spool = te.events_file(cache, sid)
    raw = (
        [rec for _, rec in te.read_records(spool, 0)[0]] if spool and os.path.isfile(spool) else []
    )
    counts["trace"] = trace_lines(sid, records, raw, shown, signals, te)
    if events:
        last = max(_iso(str(e["ts"])) or clock.now for e in events)
        now = last + _dt.timedelta(seconds=1)
        for start in range(0, len(events), trust.MAX_EVENTS):
            body = {"session_id": sid, "events": events[start : start + trust.MAX_EVENTS]}
            batch = trust.parse_batch(body, now=now)
            stored = trust.store_batch(conn, batch, now=now)
            for k, v in stored.items():
                counts["events"][f"store:{k}"] = counts["events"].get(f"store:{k}", 0) + v
    return counts


# -- the run -----------------------------------------------------------------------


def _add(total: dict[str, int], part: Mapping[str, int]) -> None:
    for k, v in part.items():
        total[k] = total.get(k, 0) + v


def run(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    from noblivion import config, db

    settings = config.load_settings()
    data_dir = Path(settings.data_dir)
    state_path = data_dir / STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    done: list[str] = list(state.get("done") or [])
    totals: dict[str, Any] = dict(state.get("totals") or {})
    memory_dir = os.path.abspath(os.path.expanduser(args.memory_dir))
    paths = main_transcripts(args.transcripts)
    births = None
    if args.birth_from_tool_calls:
        names = {p.name for p in Path(memory_dir).glob("*.md")}
        births = note_births(all_transcripts(paths), names)
    clock = Clock()
    plugin = Plugin(clock)
    env = replay_env(environ, memory_dir, args.shadow)
    conn = db.open_db(settings.db_path, allow_migrate=False)
    trace = open(args.trace, "a", encoding="utf-8") if args.trace else None  # noqa: SIM115
    t0 = time.monotonic()
    try:
        for path in paths:
            if path in done:
                continue
            if args.max_seconds and time.monotonic() - t0 > args.max_seconds:
                break
            got = replay_session(path, plugin, clock, env, conn, births)
            for line in got.pop("trace"):
                if trace is not None:
                    trace.write(json.dumps(line, sort_keys=True) + "\n")
            totals["sessions"] = totals.get("sessions", 0) + 1
            totals["sessions_with_prompts"] = totals.get("sessions_with_prompts", 0) + (
                1 if got["prompts"] else 0
            )
            totals["prompts"] = totals.get("prompts", 0) + got["prompts"]
            totals["shown_notes"] = totals.get("shown_notes", 0) + got["shown"]
            totals.setdefault("events", {})
            _add(totals["events"], got["events"])
            done.append(path)
            state_path.write_text(json.dumps({"done": done, "totals": totals}), encoding="utf-8")
    finally:
        conn.close()
        if trace is not None:
            trace.close()
    return {
        "transcripts": len(paths),
        "replayed": len(done),
        "left": len([p for p in paths if p not in done]),
        "births_known": len(births) if births is not None else None,
        **totals,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay_transcripts", description=__doc__.split("\n")[0])
    parser.add_argument("transcripts", nargs="+", help="main transcript files or glob patterns")
    parser.add_argument("--memory-dir", required=True, help="the memory folder the store indexed")
    parser.add_argument("--birth-from-tool-calls", action="store_true")
    parser.add_argument(
        "--no-shadow", dest="shadow", action="store_false", help="do not write the hook's trust log"
    )
    parser.add_argument("--max-seconds", type=float, default=0.0, help="stop after this long")
    parser.add_argument("--trace", help="write the signal events with their text here")
    args = parser.parse_args(argv)
    if not os.environ.get("NOBLIVION_DATA_DIR"):
        parser.error("set NOBLIVION_DATA_DIR to a scratch data dir")
    result = run(args, os.environ)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
