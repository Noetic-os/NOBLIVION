# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``noblivion`` command: ``noblivion <command> [options]``.

Commands in this version: ``index`` (see ``noblivion.indexer``),
``consent embeddings`` (see ``noblivion.embedding``), ``serve`` (the store
in the foreground, see ``noblivion.store``) and ``ensure-running`` (start
the store in the background unless it runs, see ``noblivion.launcher``).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

USAGE = (
    "usage: noblivion index [--force] [--allow-shrink] [--memory-dir DIR] [--json]\n"
    "       noblivion consent embeddings [--revoke]\n"
    "       noblivion serve [--port PORT] [--lock-wait SECONDS]\n"
    "       noblivion ensure-running [--json]"
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "index":
        from noblivion import indexer

        return indexer.main(rest)
    if command == "serve":
        from noblivion import store

        return store.main(rest)
    if command == "ensure-running":
        from noblivion import launcher

        return launcher.main(rest)
    if command == "consent" and rest[:1] == ["embeddings"]:
        from noblivion import embedding

        return embedding.consent_main(rest[1:])
    print(f"noblivion: unknown command {command!r}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
