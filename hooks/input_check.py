#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A sign when the Claude Code hook input changes shape (NOBLIVION-118).

The hooks fail open: a hook that does not find ``tool_name`` or ``prompt`` in its input does
nothing and exits 0, so the prompt is never blocked. But when Claude Code renames a field, recall
and the guards stop with no sign. This module checks the input of each hook entry point against
the fields that Claude Code documents for the event (``EXPECTED``).

When a field is missing:

* the log ``input-drift.log`` in the data dir gets one JSON line for each session (the field
  names, never the values);
* the user gets one ``systemMessage`` for each session, from the first hook that can show it. An
  async hook (``notify=False``) only logs, because Claude Code does not show its output.

Input with every field gives no change: the hook output stays byte for byte the same. A fault in
this module never changes what the hook does. Standard library only.

The session transcript (``transcript_path``) is a second input, and Claude Code does not document
its shape. The hooks read it by hand and read nothing when a line changes shape. So on
PreToolUse, PostToolUse and Stop the module also checks the transcript once for each session
(``transcript_drift``): when the last ``TRANSCRIPT_TAIL`` bytes hold ``TRANSCRIPT_MIN_LINES`` or
more JSON lines and no line has the message shape that the hooks read, ``input-drift.log`` gets
one line (``"kind": "transcript"``, the line count, the ``type`` names and the Claude Code
version) and the user gets one ``systemMessage`` for each session. A healthy session pays one
read: a marker then stops the check. Not covered: the subagent transcripts that SubagentStart
reads, and the PostToolUseFailure event.

``TESTED_CLAUDE_CODE`` records the newest Claude Code version whose transcript shape the made-up
transcripts of ``tests/test_replay_transcripts.py`` and ``tests/test_hook_input_check.py`` model.

A hook calls ``run(hook, fn)`` in place of ``fn()`` in its ``__main__`` block. The dispatcher
calls ``apply`` on its combined answer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent

LOG_NAME = "input-drift.log"
STATE_DIR = "input-drift"
# Markers older than this are removed when a new one is made.
KEEP_S = 7 * 24 * 3600

# The fields Claude Code sends to every hook, and the fields of each event that the hooks read.
# Source: the Claude Code hooks reference (hook input). A field that Claude Code sends only some
# of the time (``permission_mode``, ``tool_use_id``) is not here: its absence is not drift.
COMMON = ("session_id", "transcript_path", "cwd", "hook_event_name")
EXPECTED: Dict[str, Tuple[str, ...]] = {
    "SessionStart": ("source",),
    "UserPromptSubmit": ("prompt",),
    "PreToolUse": ("tool_name", "tool_input"),
    "PostToolUse": ("tool_name", "tool_input", "tool_response"),
    "PostToolUseFailure": ("tool_name", "tool_input"),
    "PreCompact": ("trigger",),
    "SubagentStart": ("agent_type",),
    "Stop": ("stop_hook_active",),
    "SessionEnd": ("reason",),
}
# Plain stdout of these events is context for the model, so a notice keeps it as context.
TEXT_IS_CONTEXT = ("SessionStart", "UserPromptSubmit")
# The transcript check (NOBLIVION-118). Measured on 400 real transcripts: a 4 MB tail with no
# message line held at most 15 JSON lines (the start of a session), and a 512 KB tail held up to
# 298, so the window is 4 MB and a verdict needs 30 lines.
TESTED_CLAUDE_CODE = "2.1.325"
TRANSCRIPT_EVENTS = ("PreToolUse", "PostToolUse", "Stop")
TRANSCRIPT_TAIL = 4 * 1024 * 1024
TRANSCRIPT_MIN_LINES = 30
# The line shape the hooks read: guard_hook.last_prompt (message.role), stop_checks.read_turn,
# recall_nudge.recent_calls and trust_events (content blocks).
_ROLES = ("user", "assistant")
_BLOCKS = ("text", "tool_use", "tool_result", "thinking")
# Shells and wrappers between Claude Code and the hook process.
_WRAPPERS = {"sh", "bash", "dash", "zsh", "python", "python3", "env", "timeout", "uv"}


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def missing_fields(text: str) -> Optional[Tuple[str, List[str], List[str]]]:
    """``(event name, missing fields, present fields)`` when the input lacks an expected field;
    None when every field is there.

    Only input that Claude Code made is checked: a JSON object that names its event in
    ``hook_event_name``, or in a field of the same name in another case or spelling
    (``hookEventName``). Empty input, input that is not an object and a hand-made event without
    an event name (a hook run by hand, a test) are not checked."""
    if not text or not text.strip():
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    if _norm("hook_event_name") not in {_norm(k) for k in event}:
        return None
    name = event.get("hook_event_name")
    name = name if isinstance(name, str) and name else "?"
    want = COMMON + EXPECTED.get(name, ())
    missing = [k for k in want if k not in event]
    if not missing:
        return None
    return name, missing, sorted(str(k) for k in event)[:40]


def _hook_config() -> Any:
    mod = sys.modules.get("hook_config")
    if mod is not None and hasattr(mod, "data_dir"):
        return mod
    spec = importlib.util.spec_from_file_location(
        "_input_check_hook_config", HOOKS_DIR / "hook_config.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("hook_config")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _comm(pid: int) -> Tuple[str, int]:
    """``(command name, parent pid)`` of ``pid`` from /proc; ``("", 0)`` when not readable."""
    try:
        raw = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    head, _, tail = raw.rpartition(")")
    name = head.partition("(")[2]
    fields = tail.split()
    try:
        return name, int(fields[1])
    except (IndexError, ValueError):
        return name, 0


def _owner_pid() -> int:
    """The Claude Code process: the first parent that is not a shell or a wrapper."""
    pid = os.getppid()
    for _ in range(6):
        name, parent = _comm(pid)
        if not name or name not in _WRAPPERS or parent <= 1:
            return pid
        pid = parent
    return pid


def session_key(text: str) -> str:
    """A file-safe key of the session: from ``session_id``, else ``transcript_path``, else the
    Claude Code process (a renamed ``session_id`` must still give one notice, not one per call)."""
    try:
        event = json.loads(text)
    except ValueError:
        event = None
    raw = ""
    if isinstance(event, dict):
        for key in ("session_id", "transcript_path"):
            value = event.get(key)
            if isinstance(value, str) and value:
                raw = key + ":" + value
                break
    if not raw:
        raw = "pid:%d" % _owner_pid()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def claude_version(text: str) -> str:
    """The Claude Code version from the ``version`` field of the transcript, or ``unknown``."""
    try:
        event = json.loads(text)
        path = event.get("transcript_path") if isinstance(event, dict) else None
        if not isinstance(path, str) or not path:
            return "unknown"
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
        found = re.findall(r'"version"\s*:\s*"([0-9][0-9A-Za-z.+-]{0,40})"', tail)
        return found[-1] if found else "unknown"
    except Exception:  # noqa: BLE001 - best effort
        return "unknown"


def _claim(folder: Path, name: str) -> bool:
    """Make the marker ``name`` once. True for the one call that made it."""
    try:
        fd = os.open(str(folder / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True


def _prune(folder: Path) -> None:
    cut = time.time() - KEEP_S
    try:
        for path in folder.iterdir():
            try:
                if path.stat().st_mtime < cut:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _state(env: Optional[Mapping[str, str]]) -> Optional[Tuple[Path, Path]]:
    """``(marker folder, log file)``, or None when the data dir is gone (after an uninstall a hook
    must not make it again, NOBLIVION-28)."""
    cfg = _hook_config()
    root = cfg.data_dir(env)
    if not root.is_dir():
        return None
    folder = cfg.make_dirs(root / STATE_DIR, env)
    return folder, root / LOG_NAME


def notice_text(hook: str, event: str, missing: List[str], version: str, log: Path) -> str:
    return (
        "NOBLIVION: the Claude Code hook input changed (Claude Code %s). The %s hook got no %s "
        "for %s. Recall and the guards can fail without a sign in this session. Update NOBLIVION. "
        "Details: %s" % (version, hook, ", ".join(missing), event, log)
    )


def check(
    text: str, hook: str, notify: bool = True, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Log drift once for each session and return the notice when this call must show it (once
    for each session). None when the input is complete, or the notice was shown already."""
    found = missing_fields(text)
    if found is None:
        return None
    event, missing, present = found
    state = _state(env)
    if state is None:
        return None
    folder, log = state
    key = session_key(text)
    version = claude_version(text)
    if _claim(folder, key + ".log"):
        _prune(folder)
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": hook,
            "event": event,
            "missing": missing,
            "fields": present,
            "claude_code": version,
            "session": key,
        }
        fd = os.open(str(log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    if notify and _claim(folder, key + ".notice"):
        return notice_text(hook, event, missing, version, log)
    return None


# ── the transcript shape ──


def known_line(row: Mapping[str, Any]) -> bool:
    """True for a transcript line in the shape the hooks read: ``type`` user or assistant, a
    ``message`` with the same ``role``, and ``content`` that is text or holds a known block."""
    kind = row.get("type")
    msg = row.get("message")
    if kind not in _ROLES or not isinstance(msg, dict) or msg.get("role") != kind:
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in _BLOCKS for b in content
    )


def transcript_shape(path: str) -> Tuple[int, int, List[str]]:
    """``(JSON lines, known lines, type names)`` of the last ``TRANSCRIPT_TAIL`` bytes of the
    transcript. The count stops at the first known line (newest first): one is enough."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - TRANSCRIPT_TAIL)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start:
        lines = lines[1:]  # the first line is cut
    seen = known = 0
    types = set()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        seen += 1
        if known_line(row):
            known += 1
            break
        types.add(str(row.get("type"))[:40])
    return seen, known, sorted(types)[:20]


def transcript_notice_text(hook: str, rec: Mapping[str, Any], log: Path) -> str:
    return (
        "NOBLIVION: the Claude Code session transcript changed shape (Claude Code %s, NOBLIVION "
        "was tested with %s). The %s hook found no message line it can read in the last %s "
        "transcript lines. Guard context, the recall nudge and the stop checks can fail without "
        "a sign in this session. Update NOBLIVION. Details: %s"
        % (rec.get("claude_code"), TESTED_CLAUDE_CODE, hook, rec.get("lines"), log)
    )


def transcript_drift(
    text: str, hook: str, env: Optional[Mapping[str, str]] = None
) -> Optional[Tuple[Path, str, Dict[str, Any]]]:
    """``(marker folder, session key, log line)`` when the session transcript has drifted; None
    when it is healthy, too short for a verdict, or not checked.

    The verdict is made once for each session. The first call that makes it claims the marker
    ``<session>.transcript`` (empty when healthy, the log line on drift) and logs drift once.
    Later calls read only the marker."""
    try:
        event = json.loads(text)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("hook_event_name") not in TRANSCRIPT_EVENTS:
        return None
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    state = _state(env)
    if state is None:
        return None
    folder, log = state
    key = session_key(text)
    marker = folder / (key + ".transcript")
    try:
        saved: Optional[str] = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        saved = None
    if saved is not None:
        try:
            rec = json.loads(saved) if saved.strip() else None
        except ValueError:
            rec = None
        return (folder, key, rec) if isinstance(rec, dict) else None
    try:
        seen, known, types = transcript_shape(path)
    except OSError:  # no transcript yet
        return None
    if not known and seen < TRANSCRIPT_MIN_LINES:
        return None  # too early for a verdict: check again on the next call
    if not _claim(folder, key + ".transcript"):
        return None  # another hook made the verdict
    _prune(folder)
    if known:
        return None
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "transcript",
        "hook": hook,
        "event": event.get("hook_event_name"),
        "lines": seen,
        "known": 0,
        "types": types,
        "claude_code": claude_version(text),
        "tested": TESTED_CLAUDE_CODE,
        "session": key,
    }
    line = json.dumps(rec, sort_keys=True)
    marker.write_text(line, encoding="utf-8")
    fd = os.open(str(log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return folder, key, rec


def transcript_due(text: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when this call may still make the transcript verdict or show its notice. A healthy
    session (empty marker) and a session with its notice shown are not due: ``run`` then does not
    capture the hook's stdout."""
    try:
        event = json.loads(text)
        if not isinstance(event, dict) or event.get("hook_event_name") not in TRANSCRIPT_EVENTS:
            return False
        path = event.get("transcript_path")
        if not isinstance(path, str) or not path:
            return False
        state = _state(env)
        if state is None:
            return False
        folder = state[0]
        key = session_key(text)
        try:
            saved = (folder / (key + ".transcript")).read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        return bool(saved.strip()) and not (folder / (key + ".tnotice")).exists()
    except Exception:  # noqa: BLE001 - the check never changes what the hook does
        return False


def _with_transcript_notice(
    stdout: str, text: str, hook: str, event: str, env: Optional[Mapping[str, str]]
) -> str:
    """``stdout`` with the one transcript notice of the session, when it is due and fits."""
    found = transcript_drift(text, hook, env)
    if found is None:
        return stdout
    folder, key, rec = found
    name = event or _event_name(text)
    if merge(stdout, "x", name) is None or not _claim(folder, key + ".tnotice"):
        return stdout
    log = folder.parent / LOG_NAME
    return merge(stdout, transcript_notice_text(hook, rec, log), name) or stdout


def merge(stdout: str, notice: str, event: str) -> Optional[str]:
    """``stdout`` with ``notice`` as ``systemMessage``. None when the notice cannot be added
    without a change to what the hook says (plain text of an event that does not read it)."""
    text = stdout.strip()
    if not text:
        return json.dumps({"systemMessage": notice}) + "\n"
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        old = doc.get("systemMessage")
        doc["systemMessage"] = (old + "\n" + notice) if isinstance(old, str) and old else notice
        return json.dumps(doc) + "\n"
    if doc is None and event in TEXT_IS_CONTEXT:
        out = {
            "systemMessage": notice,
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
        }
        return json.dumps(out) + "\n"
    return None


def _event_name(text: str, default: str = "?") -> str:
    try:
        name = json.loads(text).get("hook_event_name")
    except Exception:  # noqa: BLE001
        return default
    return name if isinstance(name, str) and name else default


def apply(
    code: int,
    stdout: str,
    text: str,
    hook: str,
    event: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """The stdout to print for a hook answer ``(code, stdout)`` on input ``text``. Only exit 0
    shows stdout, so only exit 0 takes the notice. Complete input and a healthy transcript:
    ``stdout`` unchanged."""
    if code != 0:
        try:
            transcript_drift(text, hook, env)  # log only: the notice waits for a later hook
        except Exception:  # noqa: BLE001
            pass
        return stdout
    out = stdout
    try:
        if missing_fields(text) is not None:
            name = event or _event_name(text)
            # Check first that the notice fits, so the one notice of the session is not lost.
            if merge(out, "x", name) is None:
                check(text, hook, notify=False, env=env)
            else:
                notice = check(text, hook, env=env)
                if notice is not None:
                    out = merge(out, notice, name) or out
    except Exception:  # noqa: BLE001 - the check never changes what the hook does
        out = stdout
    try:
        return _with_transcript_notice(out, text, hook, event, env)
    except Exception:  # noqa: BLE001
        return out


def run(hook: str, fn: Callable[[], Any], notify: bool = True) -> Any:
    """Call ``fn`` (the hook's ``main``) with the hook's stdin, and add the notice to its stdout
    when the input lacks a field. ``fn``'s return value and exceptions pass through."""
    try:
        if sys.stdin is None or sys.stdin.isatty():  # run by hand: nothing to check
            return fn()
        text = sys.stdin.read()
    except (OSError, ValueError):  # no stdin: the hook reads an empty input
        text = ""
    sys.stdin = io.StringIO(text)
    try:
        drift = missing_fields(text) is not None
    except Exception:  # noqa: BLE001
        drift = False
    due = transcript_due(text)
    if not drift and not due:
        return fn()
    if not notify:
        try:
            if drift:
                check(text, hook, notify=False)
            transcript_drift(text, hook)  # log only: a hook that shows no output
        except Exception:  # noqa: BLE001
            pass
        return fn()
    real = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    code: Any = 0
    try:
        code = fn()
    except SystemExit as exc:
        code = exc.code
        raise
    finally:
        sys.stdout = real
        out = buf.getvalue()
        status = 0 if code is None else code
        if not isinstance(status, int):
            status = 1
        real.write(apply(status, out, text, hook))
        try:
            real.flush()
        except Exception:  # noqa: BLE001
            pass
    return code
