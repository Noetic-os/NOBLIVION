<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Changelog

All notable changes to NOBLIVION. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- Behaviour change: the stop checks run in shadow mode by default
  (NOBLIVION-32). They run and log what would have blocked, but never block.
  Before, the commit, tests, notify and lesson checks blocked for every new
  user. Set `NOBLIVION_STOP_CHECK_MODE=enforce`, or the new config key
  `stop.mode`, to let them block. The new config key `stop.checks` chooses
  the blocking checks, like `NOBLIVION_STOP_CHECKS`. The log rows now hold
  `mode` and `would_block`.
- The `notify` check fires only when the session has a `PushNotification`
  tool, read from the transcript's tool list. Without one, it does not fire
  and the log row notes `notify_skipped`.

### Added

- `noblivion stop report [--days N] [--json]` counts the stop check log rows
  per check: would block, blocked, and logged only.

### Fixed

- The hooks find the memory folder from the session's project dir
  (`CLAUDE_PROJECT_DIR`), not from the `cwd` of the hook event
  (NOBLIVION-37). Before, a Bash `cd` into another git repository moved
  recall, the guards and the stop checks to that repository's memory folder,
  while Claude Code kept its auto memory on the project the session started
  in. The event `cwd` is still used when `CLAUDE_PROJECT_DIR` is not set or
  is not an existing folder (Codex, a hook run by hand). The Codex adapter
  drops a `CLAUDE_PROJECT_DIR` it inherits from an outer Claude Code session.
- The hooks find the session's memory folder the way Claude Code does
  (NOBLIVION-30). A session started in a subfolder of a git repository, or in
  a linked worktree, now uses the memory folder of the main checkout, so it
  gets recall. The hooks also follow `autoMemoryDirectory` in the Claude Code
  settings files, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_PROJECT_DIR_NAME`. One
  rule, `resolve_memory_dir` in `hooks/hook_config.py`, serves the recall,
  error recall, subagent rules and trust flush hooks. When the folder does
  not exist, the recall hook writes one line to
  `<data dir>/cache/memory-dir.log`; the error recall hook writes one
  `memory_dir_missing` record to its log.
- The store's default folder list follows `CLAUDE_CONFIG_DIR` and the
  `autoMemoryDirectory` of the user and managed settings files.
  `scripts/install_codex.py` checks the same list.
- Every hook now uses the project memory folder of the session
  (NOBLIVION-31). Before, the guard table, the guard hook, the stop checks,
  continuity, the memory fields hook and the memory sync hook used the memory
  folder of the home folder unless `NOBLIVION_MEMORY_DIR` was set. For a user
  who did not start Claude Code in the home folder, the guards did nothing,
  and the lesson stop check asked Claude to save rules in a folder that recall
  did not search. Each hook now finds the folder from the `cwd` of its hook
  event (`resolve_memory_dir`). `NOBLIVION_MEMORY_DIR` still wins. The
  home-folder default (`default_memory_dir`) is removed.
- The guard table is kept per project, in
  `<data dir>/guard-tables/<slug of the memory folder>.json`, so two sessions
  in two projects do not overwrite each other's table. The SessionStart
  rebuild reads the `cwd` from its hook event. The guard hook builds a
  missing table once. `NOBLIVION_GUARD_TABLE` still names one fixed file.
- The memory sync hook keeps its Bash stamp per project folder, and gives the
  indexer the project folder.

### Added

- An optional user-wide memory folder: `NOBLIVION_GLOBAL_MEMORY_DIR`, or the
  config key `global_memory_dir` (NOBLIVION-31). Default: none. When it is
  set, the store indexes it and searches it in every session, and recall, the
  guard table and the stop checks read it after the project folder. A file of
  the project folder wins over a file with the same name in the global folder,
  in the store and in the hooks. A file of the session's root now also wins
  over a file with the same name in a `recall.shared_roots` root.

## 0.1.2 - 2026-10-04

Fixes from the first real install on a second host. After the plugin
update, type `/noblivion:setup` in the next session: the store venv must be
rebuilt for 0.1.2.

### Added

- The slash command `/noblivion:setup` (`skills/setup/SKILL.md`). It runs
  `install.sh` with the plugin data dir that Claude Code fills in, so the
  setup works from the chat, also in the VS Code chat with no terminal. The
  SessionStart line now names it first and the terminal command second.
  (NOBLIVION-29)
- `install.sh` starts the store at the end (`noblivion ensure-running`).
  Memory recall works from the next prompt of the session that ran it. So
  the path from `claude plugin install` to working recall is at most two
  sessions, not three. `--no-start` skips the start. An empty `--data-dir`
  is refused. (NOBLIVION-29)

### Fixed

- After `claude plugin uninstall`, the data dir came back at once with an
  empty `noblivion.db`. Cause: `uninstall.sh` sent SIGTERM to the store and
  did not wait. The store was still stopping when Claude Code deleted the
  data dir, and its stop opened a new connection for the WAL checkpoint;
  `db.connect` made the folder and the file again. Now only `install.sh`
  makes the data dir. `db.connect` opens an existing database only unless
  the caller asks to create one. Only the store and `noblivion index` make
  a new database, and only after `install.sh` has run (the install stamp in
  `venv/`). `mine`, `trust`, `dedup` and `consent` exit with code 6 without
  a database. The launcher, the store and every hook never make a missing
  data dir. A running store stops when its data dir is deleted.
  `uninstall.sh` waits until the store has exited. (NOBLIVION-28)

## 0.1.1 - 2026-10-04

Fixes from the fresh-install test of 0.1.0. After the plugin update, run
the install command that the first session prints, because the store venv
must be rebuilt for 0.1.1.

### Fixed

- The MCP tool `noblivion_recall` answered `store_down` in every session.
  `.mcp.json` passed the literal text `${CLAUDE_PLUGIN_DATA}` to the MCP
  server. The `env` block is gone: Claude Code sets the variable by itself.
  A data dir value that still holds `${` now counts as unset. A test fails
  on any variable in the plugin JSON files that Claude Code does not expand
  in that field. The MCP server reports the plugin version. (NOBLIVION-24)
- A store that could not start was silent. The store now logs the bind
  error with its reason and writes it to `<data dir>/store.error`.
  `noblivion ensure-running` waits up to 10 seconds for the store, prints
  `failed` with the reason and exits with code 1. The SessionStart hook
  shows one line with the reason. (NOBLIVION-25)
- The data dir could be mode 0775 when Claude Code made it before the
  install. `install.sh`, the SessionStart hook, the launcher and the store
  now set it to 0700, and the hooks make their folders with mode 0700.
  (NOBLIVION-27)
- The first session of a fresh install printed
  `guard table: rebuild failed: memory folder ... does not exist`. No
  memory folder now means no guards, with no error line. (NOBLIVION-27)

### Changed

- The default store port is `0`: the system picks a free port, and the
  hooks read it from `store.json`. The fixed port 8894 was taken on a test
  host. Set `port` in `config.json` to use a fixed port. (NOBLIVION-25)
- The uninstall steps keep the database by default. `uninstall.sh` and
  `docs/install.md` now say to run
  `claude plugin uninstall noblivion --keep-data`, and say that without
  `--keep-data` Claude Code deletes the whole data dir, the database
  included. (NOBLIVION-26)
- The install docs use the HTTPS URL for `marketplace add` (the short form
  `<owner>/NOBLIVION` can clone over SSH) and show the terminal commands
  `claude plugin marketplace add`, `claude plugin install` and
  `claude plugin list`. (NOBLIVION-27)

## 0.1.0 - 2026-10-03

First release.

- The Claude Code plugin: hooks for recall, guards, stop checks,
  continuity, memory sync, the trust flush, the store start and the
  transcript miner; the MCP tool `noblivion_recall`.
- The local store: SQLite, keyword and optional vector search, served on
  `127.0.0.1` with a token handshake.
- Trust scoring, the transcript miner and opt-in dedup.
- `scripts/install.sh`, `scripts/uninstall.sh` and
  `noblivion migrate-from-legacy`.
