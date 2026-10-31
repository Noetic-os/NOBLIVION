#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One hook command per event: run the modules of the event in ONE process (NOBLIVION-162).

``hooks.json`` used to start one Python process per module for an event: a prompt started 3, a
session start 7. Python start and the imports cost about the same for each, and Claude Code waited
for the slowest one. ``python3 dispatch.py EVENT`` reads the event JSON once, runs the modules that
``PLAN[EVENT]`` lists for it, one after the other in this process, and prints one merged answer.

Rules, each pinned by ``tests/test_hook_dispatch.py``:

* A module sees what it saw as its own process: stdin holds the event JSON, ``sys.argv`` holds its
  own flags, ``os.environ`` and the working directory are restored after it. Its stdout and stderr
  are captured. A module that raises, exits non-zero, runs too long or changes the environment does
  not change what the next module sees or prints.
* Exit codes keep their meaning for Claude Code. Exit 0 keeps the stdout of the module. Exit 2 is a
  blocking error: the dispatcher exits 2 with the stderr of that module and prints no stdout, as
  Claude Code ignores stdout then. Any other code is a plain error: the module's stdout is dropped,
  as Claude Code drops it, its stderr is passed on, and the dispatcher exits 0 for the others.
* One module with output: its stdout passes through byte for byte. Several: the JSON objects merge.
  ``systemMessage`` and ``additionalContext`` join with a line break in the table order; text that is
  not JSON counts as ``additionalContext``, as Claude Code reads plain stdout of these events;
  ``decision: "block"`` and ``continue: false`` win; the first value of any other key stays.
* The deny paths are not here. ``guard_hook.py`` (with the credential guard) stays its own command
  hook for ``PreToolUse``, so its deny result and its failure mode are those of a single process.
  Async work (``memory_sync_hook``, ``trust_flush``, ``version_check``) and the ``Stop`` check stay
  their own commands too: a dispatcher would make Claude Code wait for them.
* Which module runs for which ``source`` (SessionStart) or ``trigger`` (PreCompact) is the
  ``matcher`` column of ``PLAN``, with the full-match rule of Claude Code. An event without the
  field runs the modules that have no matcher.

Warm path (NOBLIVION-162, step 2, opt-in). ``run_forked`` runs ``run_event`` and ``combine`` in a
forked child of a long-lived process and returns the same ``(exit code, stdout, stderr)``. The MCP
server of the plugin calls it for the ``mcp_tool`` hook of ``UserPromptSubmit``, so a prompt starts no
Python process. The child loads the modules fresh, so no state passes from one prompt to the next, as
with one process per prompt. The file ``warm-prompt-hooks`` in the data dir switches it on (``warm_on``);
without the file the command hook runs and the tool answers nothing.

``--raw`` prints every part (module, exit code, stdout, stderr, milliseconds) as JSON, for the tests
and for the timing tool. Standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import signal
import sys
import time
import traceback
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

HOOKS_DIR = Path(__file__).resolve().parent


class Step(NamedTuple):
    """One module of an event. ``timeout`` is the second limit the module had as its own hook."""

    matcher: Optional[str]  # full match against the event's source or trigger; None: always
    module: str  # file name in this folder, without .py
    argv: Tuple[str, ...]
    timeout: int
    entry: str = "main"  # the function to call
    takes_argv: bool = False  # main(argv) instead of main()


# The order is the order of the old hooks.json. SessionStart and UserPromptSubmit are the events
# where the old file started several synchronous commands. ``version_check`` (async) is not here.
PLAN: Dict[str, Tuple[Tuple[Step, ...], str]] = {
    "SessionStart": (
        (
            Step(None, "store_client", (), 10),
            Step(None, "guard_table", ("--rebuild",), 20),
            Step("compact", "recall_hook", ("--reset-rows",), 5, entry="main_reset_rows"),
            Step("startup|resume|clear|compact", "continuity_hook", (), 10),
            Step("startup|resume|clear", "trust_session_line", (), 5),
            Step(None, "learning_capture", (), 10),
            Step(None, "learning_summary", (), 5),
        ),
        "source",
    ),
    "UserPromptSubmit": (
        (
            Step(None, "stop_checks", ("--mark-correction",), 5, takes_argv=True),
            Step(None, "learning_context", (), 5),
            Step(None, "recall_hook", (), 5),
        ),
        "",
    ),
    "PreCompact": (
        (
            Step("manual|auto", "continuity_hook", (), 10),
            Step(None, "learning_capture", (), 10),
        ),
        "trigger",
    ),
}

# The limit of the dispatcher command in hooks.json: the sum of its steps, but never above this
# (tests/test_plugin_package.py allows at most 60 s for a hook).
MAX_TIMEOUT = 60
# What is left of the limit for the work after the last step: print and exit.
RESERVE_S = 0.5


def timeout_of(event: str) -> int:
    """The ``timeout`` that ``hooks.json`` gives the dispatcher command of ``event``."""
    steps, _ = PLAN[event]
    return min(MAX_TIMEOUT, sum(s.timeout for s in steps))


class ModuleTimeout(BaseException):
    """Raised in the main thread when a step has used its time."""


class Part(NamedTuple):
    module: str
    argv: Tuple[str, ...]
    code: int
    stdout: str
    stderr: str
    ms: int


def _alarm(signum: int, frame: Any) -> None:
    raise ModuleTimeout("time limit")


_LOADED: Dict[str, Any] = {}


def _load(name: str) -> Any:
    """The module ``name`` of this folder, loaded once. It registers under its own name, as the
    ``dataclass`` code of some modules reads ``sys.modules[cls.__module__]``."""
    mod = _LOADED.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / (name + ".py"))
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    _LOADED[name] = mod
    return mod


def _selected(steps: Sequence[Step], field: str, event: Dict[str, Any]) -> List[Step]:
    """The steps whose matcher accepts the event."""
    value = event.get(field) if field else None
    value = value if isinstance(value, str) else None
    out = []
    for step in steps:
        if step.matcher is None:
            out.append(step)
        elif value is not None and re.fullmatch(step.matcher, value):
            out.append(step)
    return out


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def _run_step(step: Step, event_text: str, allowed_s: float) -> Part:
    """Run one module as its own process would: own stdio, argv, environment and directory.
    Nothing a module does here may reach the next one or stop the dispatcher."""
    started = time.monotonic()
    saved_io = (sys.stdin, sys.stdout, sys.stderr, sys.argv)
    saved_env = dict(os.environ)
    saved_cwd = None
    try:
        saved_cwd = os.getcwd()
    except OSError:
        pass
    saved_handler = None
    out, err = StringIO(), StringIO()
    code = 0
    try:
        saved_handler = signal.signal(signal.SIGALRM, _alarm)
    except (ValueError, OSError, AttributeError):
        saved_handler = None
    try:
        sys.stdin, sys.stdout, sys.stderr = StringIO(event_text), out, err
        sys.argv = [str(HOOKS_DIR / (step.module + ".py")), *step.argv]
        try:
            signal.setitimer(signal.ITIMER_REAL, max(0.05, allowed_s))
        except (ValueError, OSError, AttributeError):
            pass
        try:
            mod = _load(step.module)
            fn = getattr(mod, step.entry)
            result = fn(list(step.argv)) if step.takes_argv else fn()
            code = result if isinstance(result, int) else 0
        except SystemExit as exc:
            code = _exit_code(exc)
        except ModuleTimeout:
            code = 1
            err.write("dispatch: " + step.module + " ran out of time\n")
        except BaseException:  # a module fault stays in this module
            code = 1
            err.write(traceback.format_exc())
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if saved_handler is not None:
                signal.signal(signal.SIGALRM, saved_handler)
        except (ValueError, OSError, AttributeError):
            pass
        sys.stdin, sys.stdout, sys.stderr, sys.argv = saved_io
        os.environ.clear()
        os.environ.update(saved_env)
        if saved_cwd is not None:
            try:
                if os.getcwd() != saved_cwd:
                    os.chdir(saved_cwd)
            except OSError:
                pass
    return Part(
        step.module,
        step.argv,
        code,
        out.getvalue(),
        err.getvalue(),
        round((time.monotonic() - started) * 1000),
    )


def run_event(event_name: str, event_text: str) -> List[Part]:
    """Run the steps of ``event_name`` that match the event, in table order."""
    steps, field = PLAN[event_name]
    try:
        event = json.loads(event_text or "{}")
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    chosen = _selected(steps, field, event)
    deadline = time.monotonic() + timeout_of(event_name) - RESERVE_S
    parts: List[Part] = []
    for step in chosen:
        left = deadline - time.monotonic()
        if left <= 0.05:
            parts.append(Part(step.module, step.argv, 1, "", "dispatch: no time left\n", 0))
            continue
        parts.append(_run_step(step, event_text, min(float(step.timeout), left)))
    return parts


def _merge_docs(items: Sequence[Any], event_name: str) -> Dict[str, Any]:
    """One answer from several, in table order. An item is a dict (the JSON a module printed) or a
    str (text it printed that is not JSON). See the module docstring for the rules."""
    merged: Dict[str, Any] = {}
    messages: List[str] = []
    contexts: List[str] = []
    spec: Dict[str, Any] = {}
    reasons: List[str] = []
    stop_reasons: List[str] = []
    for doc in items:
        if isinstance(doc, str):
            contexts.append(doc)
            continue
        for key, value in doc.items():
            if key == "systemMessage":
                if isinstance(value, str) and value:
                    messages.append(value)
            elif key == "hookSpecificOutput" and isinstance(value, dict):
                for k2, v2 in value.items():
                    if k2 == "additionalContext":
                        if isinstance(v2, str) and v2:
                            contexts.append(v2)
                    else:
                        spec.setdefault(k2, v2)
            elif key == "decision":
                if value == "block" or "decision" not in merged:
                    merged["decision"] = value
            elif key == "reason":
                if isinstance(value, str) and value:
                    reasons.append(value)
            elif key == "continue":
                if value is False or "continue" not in merged:
                    merged["continue"] = value
            elif key == "stopReason":
                if isinstance(value, str) and value:
                    stop_reasons.append(value)
            elif key == "suppressOutput":
                merged["suppressOutput"] = bool(merged.get("suppressOutput")) or bool(value)
            else:
                merged.setdefault(key, value)
    if messages:
        merged["systemMessage"] = "\n".join(messages)
    if reasons:
        merged["reason"] = "\n".join(reasons)
    if stop_reasons:
        merged["stopReason"] = "\n".join(stop_reasons)
    if contexts or spec:
        spec.setdefault("hookEventName", event_name)
        if contexts:
            spec["additionalContext"] = "\n".join(contexts)
        merged["hookSpecificOutput"] = spec
    return merged


def combine(parts: Sequence[Part], event_name: str) -> Tuple[int, str, str]:
    """``(exit code, stdout, stderr)`` of the dispatcher for ``parts``."""
    stderr = "".join(p.stderr for p in parts if p.stderr)
    blocking = [p for p in parts if p.code == 2]
    if blocking:
        return 2, "", "".join(p.stderr for p in blocking)
    kept = [p.stdout for p in parts if p.code == 0 and p.stdout.strip()]
    if not kept:
        return 0, "", stderr
    if len(kept) == 1:
        return 0, kept[0], stderr
    items: List[Any] = []
    for raw in kept:
        text = raw.strip()
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        items.append(doc if isinstance(doc, dict) else text)
    return 0, json.dumps(_merge_docs(items, event_name)) + "\n", stderr


WARM_FLAG = "warm-prompt-hooks"
WARM_EVENT = "UserPromptSubmit"
# The event fields the warm hook passes (``input`` of the ``mcp_tool`` hook in hooks.json). The
# modules of the prompt event read these and no others.
WARM_FIELDS = (
    "hook_event_name",
    "session_id",
    "transcript_path",
    "cwd",
    "prompt",
    "permission_mode",
)


def warm_flag(environ: Optional[Dict[str, str]] = None) -> Path:
    """The flag file of the warm path: in the data dir, as ``hook_config.data_dir`` names it."""
    return _load("hook_config").data_dir(environ) / WARM_FLAG


def warm_on(environ: Optional[Dict[str, str]] = None) -> bool:
    try:
        return warm_flag(environ).is_file()
    except Exception:  # noqa: BLE001 - a fault reads as off: the command hook runs
        return False


def warm_event(args: Dict[str, Any]) -> str:
    """The event JSON from the ``input`` fields of the ``mcp_tool`` hook. A field the event did not
    have arrives as an empty text (Claude Code puts "" for a missing ``${path}``) and is left out."""
    event = {k: v for k, v in args.items() if k in WARM_FIELDS and isinstance(v, str) and v != ""}
    event.setdefault("hook_event_name", WARM_EVENT)
    return json.dumps(event)


def _forget_modules() -> None:
    """Drop the hook modules this process loaded, so the child execs them fresh, as a new process."""
    _LOADED.clear()
    for path in HOOKS_DIR.glob("*.py"):
        if path.stem != Path(__file__).stem:
            sys.modules.pop(path.stem, None)


def run_forked(
    event_name: str, event_text: str, environ: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str]:
    """``run_event`` and ``combine`` in a forked child; the child's answer comes back over a pipe.
    ``environ``, when given, is the environment of the child. The modules never change the parent. A
    child that dies or overruns gives exit 1 and the reason on stderr, so the caller fails open. No
    ``fork`` on this platform: the same."""
    if not hasattr(os, "fork"):
        return 1, "", "dispatch: no fork on this platform\n"
    limit = timeout_of(event_name)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never return into the caller
        status = 1
        try:
            os.close(read_fd)
            # The server speaks JSON-RPC on fd 0 and 1: nothing a module starts may read or write there.
            null = os.open(os.devnull, os.O_RDWR)
            os.dup2(null, 0)
            os.dup2(null, 1)
            _forget_modules()
            if environ is not None:
                os.environ.clear()
                os.environ.update(environ)
            code, stdout, stderr = combine(run_event(event_name, event_text), event_name)
            data = json.dumps({"code": code, "stdout": stdout, "stderr": stderr}).encode("utf-8")
            with os.fdopen(write_fd, "wb") as pipe:
                pipe.write(data)
            status = 0
        finally:
            os._exit(status)
    os.close(write_fd)
    chunks: List[bytes] = []
    end = time.monotonic() + limit
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                return 1, "", "dispatch: child ran out of time\n"
            ready, _, _ = select.select([read_fd], [], [], left)
            if not ready:
                continue
            block = os.read(read_fd, 65536)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(read_fd)
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    try:
        doc = json.loads(b"".join(chunks).decode("utf-8"))
        return int(doc["code"]), str(doc["stdout"]), str(doc["stderr"])
    except (ValueError, KeyError, TypeError):
        return 1, "", "dispatch: child gave no answer\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    raw = "--raw" in args
    names = [a for a in args if not a.startswith("--")]
    if len(names) != 1 or names[0] not in PLAN:
        sys.stderr.write("usage: dispatch.py [--raw] {" + "|".join(sorted(PLAN)) + "}\n")
        return 1
    event_name = names[0]
    out, err = sys.stdout, sys.stderr
    try:
        event_text = sys.stdin.read()
    except Exception:  # noqa: BLE001 - no stdin: run the steps on an empty event
        event_text = ""
    parts = run_event(event_name, event_text)
    code, stdout, stderr = combine(parts, event_name)
    if raw:
        doc = {
            "code": code,
            "stdout": stdout,
            "parts": [p._asdict() for p in parts],
        }
        out.write(json.dumps(doc) + "\n")
        return 0
    if stderr:
        err.write(stderr)
    if stdout:
        out.write(stdout)
    out.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
