#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Start the MCP server of the NOBLIVION version that is installed now.

The plugin cache puts each version in its own folder, for example
``~/.claude/plugins/cache/noblivion/noblivion/0.1.11/mcp/recall_mcp.py``. A
client outside Claude Code, such as an MCP client that connects over SSH, must
not name such a path: it would run that one version for ever, and Claude Code
deletes the folder 14 days after an update.

Copy this file to a fixed path once, for example
``~/.local/bin/noblivion-mcp``, and make the client run that path. Each start
reads ``installed_plugins.json`` and runs the server of the installed version.
The copy needs no update, because it holds no version.

Environment:

- ``CLAUDE_CONFIG_DIR``: the Claude config folder; default ``~/.claude``.
- ``NOBLIVION_PLUGIN_KEY``: the plugin key; default ``noblivion@noblivion``.

Exit 1 with one line on stderr when no installed server is found.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

DEFAULT_KEY = "noblivion@noblivion"


def server_path(environ: Mapping[str, str]) -> Path:
    """The ``recall_mcp.py`` of the installed version. Raises ``LookupError``."""
    config = Path(os.path.expanduser(environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    registry = config / "plugins" / "installed_plugins.json"
    key = environ.get("NOBLIVION_PLUGIN_KEY") or DEFAULT_KEY
    try:
        with open(registry, encoding="utf-8") as fh:
            rows = json.load(fh)["plugins"][key]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LookupError(f"{key} is not listed in {registry}") from exc
    for row in rows if isinstance(rows, list) else []:
        install = row.get("installPath") if isinstance(row, dict) else None
        if isinstance(install, str):
            server = Path(install) / "mcp" / "recall_mcp.py"
            if server.is_file():
                return server
    raise LookupError(f"no installed {key} has an mcp/recall_mcp.py in {registry}")


def main(argv: list | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        server = server_path(os.environ if environ is None else environ)
    except LookupError as exc:
        sys.stderr.write(f"noblivion-mcp: {exc}\n")
        return 1
    os.execv(sys.executable, [sys.executable, str(server), *args])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
