# SPDX-License-Identifier: AGPL-3.0-or-later
"""PreToolUse: tell the MCP recall tool which session is calling (NOBLIVION-110).

The MCP server is one process for the whole Claude Code window and reads no session id from
its environment. Without an id, ``fetch_id`` read a rank through the newest ``last_index.json``
of any session and marked the newest shown-set, which is the wrong one when several sessions
are open. This hook writes the id of the calling session into the arguments of each
``noblivion_recall`` call, as ``session_key``, and the id of the calling subagent as
``agent_key``. The server uses them only to choose which of its own cache files to read.

The hook overwrites a value the model supplied, and removes it when the hook payload has no
valid id, so the model cannot pick another session's files through the arguments. It does not
touch any other argument. It prints nothing for any other tool, and nothing when it fails.

Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, Optional

# The shape ``corpus._SESSION_ID_RE`` accepts. ``tests/test_mcp_session.py`` keeps the two equal.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TOOL_SUFFIX = "__noblivion_recall"


def _valid(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and SESSION_ID_RE.match(value) else None


def decide(payload: Any) -> Optional[Dict[str, Any]]:
    """The hook output for ``payload``, or None when the call is not a recall call."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("tool_name")
    raw = payload.get("tool_input")
    if not (
        isinstance(name, str)
        and name.startswith("mcp__")
        and name.endswith(TOOL_SUFFIX)
        and isinstance(raw, dict)
    ):
        return None
    updated = dict(raw)
    updated.pop("session_key", None)
    updated.pop("agent_key", None)
    session = _valid(payload.get("session_id"))
    if session:
        updated["session_key"] = session
        agent = _valid(payload.get("agent_id"))
        if agent:
            updated["agent_key"] = agent
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "session id added to the recall call",
            "updatedInput": updated,
        }
    }


def main(stdin=None, stdout=None) -> int:
    """Always returns 0: a failure here must never block a tool call."""
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        out = decide(payload)
        if out is not None:
            (stdout or sys.stdout).write(json.dumps(out) + "\n")
    except Exception:  # noqa: BLE001 - fail open: print nothing, exit 0
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
