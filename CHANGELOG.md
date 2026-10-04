<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Changelog

All notable changes to NOBLIVION. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/).

## 0.1.4 - 2026-10-04

Adds the time-split test for trust ranking and two optional trust signals.
Both signals and trust ranking stay off by default. After the plugin update,
type `/noblivion:setup` in the next session: the store venv must be rebuilt
for 0.1.4.

### Added

- `noblivion trust timesplit`: the time-split test for trust ranking
  (NOBLIVION-38). It computes trust from the events before a cut and
  measures on the newer sessions whether the used notes rank higher with
  trust on than off (MRR and hit@k). It runs on your own store, per prompt
  when the hook's trust log exists, else per session. It changes no
  setting. Trust ranking stays off by default.
- A citation in a reply can count as a `use` (NOBLIVION-38): the reply
  names a shown note or repeats 8 words of its rule in a row. Setting
  `NOBLIVION_TRUST_CITATION_USE` or `trust.citation_use`, off by default.
  Its precision on a labelled set of made-up sessions was 0.67 at the first
  run.
- A user correction can count as a `contradict` (NOBLIVION-38): the user
  corrects a reply that cited a note shown in the last 2 prompts, on that
  note's topic. Setting `NOBLIVION_TRUST_CORRECTION_CONTRADICT` or
  `trust.correction_contradict`, off by default. Its precision on the
  labelled set is 0.80, and a correction does not say whether the note was
  wrong.
- With one of these settings on, the prompt hook writes the name and rule
  of each shown note once per session to
  `<data dir>/cache/by-session/<session id>.trust-shown.jsonl`. The trust
  flush removes it with the other session files.

## 0.1.3 - 2026-10-04

Makes the hooks correct for users other than the author: the memory folder,
the per-project guards and the stop checks. After the plugin update, type
`/noblivion:setup` in the next session: the store venv must be rebuilt for
0.1.3.

### Changed

- Behaviour change for readers of the trust report: a guard that only shows
  a rule (guard rows or label rows) no longer counts as a use
  (NOBLIVION-34). Only a guard deny, or a fetch with the MCP tool, is a use.
  Use counts in the report fall, so fewer notes reach "promote" and more
  can reach "retire". `NOBLIVION_RECALL_TRUST_MODE=a` now has no effect.
  Spooled guard-row events from an older version are not sent. Events the
  store already holds still count.
- The docs, the report text and the package description now call trust
  usage evidence for the retire, promote and demote report. Trust does not
  measure whether a note is correct, and trust ranking stays off by
  default until a time-split test shows a ranking gain (NOBLIVION-34).
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
- Behaviour change: three score thresholds are now measured for the shipped
  model `BAAI/bge-small-en-v1.5` (NOBLIVION-33). The search floor
  `recall.min_score` goes from 0.3 to 0.68: at 0.3 it cut nothing, because
  this model gives an unrelated prompt a cosine of about 0.5 to 0.6. The
  hook and the MCP search tool now return fewer, more relevant hits. The
  error recall store floor goes from 0.60 to 0.70. The dedup cosine
  `dedup.min_cosine` goes from 0.82 to 0.75, so fewer duplicate pairs are
  missed. Set the env var or config key to keep an old value. The measured
  numbers are in the design doc, section 18.

### Added

- A new trust event kind, `contradict` (NOBLIVION-34). The guard records it
  when Claude Code overrides a deny with `# guard-ok: <reason>`. It is a
  trial and takes 2 uses off, so a note's trust value can now fall, also
  below its prior. The store keeps it in the existing `contradiction` rows;
  no migration is needed. Report rows carry `contradict_sessions`.
- `tools/eval_thresholds.py` and the made-up eval set
  `tools/eval/thresholds.json` measure precision and recall per threshold
  through a real store. CI job `real-model` installs the `embed` extra,
  runs the real-model tests and fails when a shipped threshold misses its
  floor. Each threshold names its model in `THRESHOLD_MODEL`; a test fails
  when the default model changes.
- `noblivion stop report [--days N] [--json]` counts the stop check log rows
  per check: would block, blocked, and logged only.
- An optional user-wide memory folder: `NOBLIVION_GLOBAL_MEMORY_DIR`, or the
  config key `global_memory_dir` (NOBLIVION-31). Default: none. When it is
  set, the store indexes it and searches it in every session, and recall, the
  guard table and the stop checks read it after the project folder. A file of
  the project folder wins over a file with the same name in the global folder,
  in the store and in the hooks. A file of the session's root now also wins
  over a file with the same name in a `recall.shared_roots` root.
- A Codex adapter (`codex/adapter.py`, `scripts/install_codex.py`). Codex
  uses the same store and the same memory files as Claude Code, with prompt
  recall, guards and task continuity. See `docs/codex.md` and
  `docs/shared-memory.md`.
- The MCP tool `noblivion_remember` saves a verified, redacted Markdown note
  in the memory folder. `NOBLIVION_SOURCE_CLIENT` names the client in the
  note (`claude_code` or `codex`).

### Fixed

- The store starts on macOS (NOBLIVION-35). The stdlib HTTP server looked up
  the host name of 127.0.0.1 when it bound the port. On macOS that lookup can
  block for 20 s or more, so the store wrote no `store.json` within the 10 s
  start wait and `/noblivion:setup` and the SessionStart hook reported "no
  answer from the store". The store now binds without a name lookup.
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
