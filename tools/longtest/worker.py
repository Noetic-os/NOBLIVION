# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worker of one build: its hooks, run in this process, on request.

The parent (``buildrunner.Worker``) starts one worker per build, with that build
on ``PYTHONPATH`` and the harness environment, and sends one JSON line per job. The
worker prints one JSON line per answer. It reads no live path: the environment it
gets names the harness folders only.

Jobs (``op``): ``init``, ``prompt``, ``guard``, ``error``, ``subagent``,
``continuity``, ``pull``, ``ping``. Each answer holds ``ok``, ``out`` (the hook's
stdout) and ``ms``. An exception becomes ``ok: false`` with its type name; the hook
itself fails open, so a failure to read is the hook's, not the worker's.

This file uses the standard library only, because it runs on the python of the
store venv.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

STATE: dict = {"hooks": {}, "mods": {}, "build": None, "config_dir": None}


def load(script: str):
    mods = STATE["mods"]
    if script not in mods:
        path = Path(STATE["build"]) / script
        spec = importlib.util.spec_from_file_location("lt_" + path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mods[script] = mod
    return mods[script]


def call_hook(script: str, event: dict, env: dict):
    out = io.StringIO()
    started = time.perf_counter()
    load("hooks/" + script).main(io.StringIO(json.dumps(event)), out, dict(env))
    return out.getvalue(), (time.perf_counter() - started) * 1000


def slug(cwd: str) -> str:
    return cwd.replace("/", "-")


def scope_env(job: dict) -> dict:
    """The env of a job. ``project`` names the project folder whose memory the
    session uses; it defaults to the folder of ``cwd``."""
    cwd, extra = job["cwd"], job.get("env")
    env = dict(os.environ)
    memory = Path(STATE["config_dir"]) / "projects" / job.get("project", slug(cwd)) / "memory"
    env.update({"CLAUDE_PROJECT_DIR": cwd, "NOBLIVION_MEMORY_DIR": str(memory)})
    env.update(extra or {})
    return env


def transcript(sid: str, cwd: str, prompts: list, project: str = "") -> str:
    path = Path(STATE["config_dir"]) / "projects" / (project or slug(cwd)) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "sessionId": sid,
                "cwd": cwd,
                "timestamp": f"2026-01-01T10:00:{i % 60:02d}Z",
            }
        )
        for i, text in enumerate(prompts)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def tool_id(sid: str) -> str:
    return "toolu_" + hashlib.sha256(sid.encode()).hexdigest()[:12]


def common(job: dict, prompts: list) -> dict:
    return {
        "session_id": job["sid"],
        "cwd": job["cwd"],
        "transcript_path": transcript(job["sid"], job["cwd"], prompts, job.get("project", "")),
    }


def do_prompt(job: dict):
    env = scope_env(job)
    event = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": job["prompt"],
        **common(job, [job["prompt"]]),
    }
    return call_hook("recall_hook.py", event, env)


def do_guard(job: dict):
    env = scope_env(job)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": job.get("tool", "Bash"),
        "tool_input": job["tool_input"],
        "tool_use_id": tool_id(job["sid"]),
        **common(job, job.get("prompts") or ["x"]),
    }
    return call_hook("guard_hook.py", event, env)


def do_error(job: dict):
    env = scope_env(job)
    event = {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": job["command"]},
        "error": job["error"],
        "tool_use_id": tool_id(job["sid"]),
        **common(job, job.get("prompts") or ["x"]),
    }
    return call_hook("error_recall_hook.py", event, env)


def do_subagent(job: dict):
    env = scope_env(job)
    base = common(job, [job.get("parent_prompt") or "x"])
    digest = hashlib.sha256(job["sid"].encode()).hexdigest()
    ms = 0.0
    if not job.get("meta_only"):
        pre = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": job["tool_input"],
            "tool_use_id": "toolu_" + digest[:12],
            **base,
        }
        _, ms = call_hook("subagent_rules_hook.py", pre, env)
    start = {
        "hook_event_name": "SubagentStart",
        "agent_id": digest[12:28],
        "agent_type": job["tool_input"].get("subagent_type", "general-purpose"),
        **base,
    }
    out, ms2 = call_hook("subagent_rules_hook.py", start, env)
    return out, ms + ms2


def do_continuity(job: dict):
    """A scripted session: each step is an event; returns the output of each."""
    env = scope_env(job)
    outs, total, seen = [], 0.0, []
    for step in job["steps"]:
        kind = step["event"]
        if kind == "UserPromptSubmit":
            seen.append(step["prompt"])
            out, ms = call_hook(
                "recall_hook.py",
                {"hook_event_name": kind, "prompt": step["prompt"], **common(job, seen)},
                env,
            )
        else:
            event = {"hook_event_name": kind, **common(job, seen or ["x"])}
            event.update({k: v for k, v in step.items() if k != "event"})
            out, ms = call_hook("continuity_hook.py", event, env)
        outs.append({"event": kind, "out": out})
        total += ms
    return json.dumps(outs), total


def do_pull(job: dict):
    env = scope_env(job)
    mod = load("mcp/recall_mcp.py")
    started = time.perf_counter()
    answer = mod.noblivion_recall(query=job["query"], k=job.get("k", 5), environ=env)
    text = "\n".join(str(b.get("text") or "") for b in answer.get("content") or [])
    return text, (time.perf_counter() - started) * 1000


def do_sync(job: dict):
    """The memory-sync hook after a write: it is a script on stdin, so it runs as a
    process, as Claude Code runs it. It starts the indexer and returns."""
    import subprocess

    env = scope_env(job)
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": job["file_path"]},
        "tool_use_id": tool_id(job["sid"]),
        **common(job, ["x"]),
    }
    started = time.perf_counter()
    done = subprocess.run(
        [sys.executable, str(Path(STATE["build"]) / "hooks" / "memory_sync_hook.py")],
        input=json.dumps(event),
        text=True,
        env=env,
        capture_output=True,
        timeout=60,
    )
    return done.stdout, (time.perf_counter() - started) * 1000


OPS = {
    "sync": do_sync,
    "prompt": do_prompt,
    "guard": do_guard,
    "error": do_error,
    "subagent": do_subagent,
    "continuity": do_continuity,
    "pull": do_pull,
}


def handle(job: dict) -> dict:
    op = job.get("op")
    if op == "init":
        STATE["build"], STATE["config_dir"] = job["build"], job["config_dir"]
        sys.path.insert(0, str(Path(job["build"]) / "hooks"))
        return {"ok": True, "out": "", "ms": 0.0}
    if op == "ping":
        return {"ok": True, "out": "pong", "ms": 0.0}
    try:
        out, ms = OPS[op](job)
        return {"ok": True, "out": out, "ms": round(ms, 2)}
    except Exception as exc:  # noqa: BLE001 - the job fails, the worker stays up
        return {
            "ok": False,
            "out": "",
            "ms": 0.0,
            "error": type(exc).__name__,
            "trace": traceback.format_exc()[-400:],
        }


def main() -> int:
    try:
        os.nice(5)
    except OSError:
        pass
    for line in sys.stdin:
        if not line.strip():
            continue
        job = json.loads(line)
        sys.stdout.write(json.dumps(handle(job)) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
