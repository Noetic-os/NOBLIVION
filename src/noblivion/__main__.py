# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``noblivion`` command: ``noblivion <command> [options]``.

Commands in this version: ``index`` (see ``noblivion.indexer``),
``consent embeddings`` (see ``noblivion.embedding``), ``dedup`` (the
opt-in duplicate sweep, see ``noblivion.dedup``), ``serve`` (the store
in the foreground, see ``noblivion.store``), ``ensure-running`` (start
the store in the background unless it runs, see ``noblivion.launcher``),
``mine`` (the transcript miner, see ``noblivion.miner``), ``trust
report`` (print the trust report) and ``trust recompute`` (the trust repair),
see ``noblivion.trust``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

USAGE = (
    "usage: noblivion index [--force] [--allow-shrink] [--memory-dir DIR] [--json]\n"
    "       noblivion consent embeddings [--revoke]\n"
    "       noblivion dedup pairs|plan [--dry-run] [--yes]|apply RUN_ID|undo RUN_ID\n"
    "       noblivion dedup consent [--revoke]|clear-latch\n"
    "       noblivion trust report [--json] [--limit N]\n"
    "       noblivion trust recompute [--json]\n"
    "       noblivion serve [--port PORT] [--lock-wait SECONDS]\n"
    "       noblivion ensure-running [--json]\n"
    "       noblivion mine [--since YYYY-MM-DD] [--max-seconds S] [--json]\n"
    "       noblivion migrate-from-legacy [--apply | --undo] [--json]"
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
    if command == "dedup":
        from noblivion import dedup

        return dedup.main(rest)
    if command == "serve":
        from noblivion import store

        return store.main(rest)
    if command == "ensure-running":
        from noblivion import launcher

        return launcher.main(rest)
    if command == "mine":
        from noblivion import miner

        return miner.main(rest)
    if command == "trust":
        from noblivion import trust

        return trust.main(rest)
    if command == "migrate-from-legacy":
        from noblivion import legacy

        return legacy.main(rest)
    if command == "consent" and rest[:1] == ["embeddings"]:
        from noblivion import embedding

        return embedding.consent_main(rest[1:])
    print(f"noblivion: unknown command {command!r}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
