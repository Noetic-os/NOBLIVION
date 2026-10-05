<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Changelog

All notable changes to NOBLIVION. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- Behaviour change for the ranked index, which the shipped config file
  turns on: the index now has a relevance floor by default (NOBLIVION-48).
  A row whose vector score is below 0.68 is not shown, so the index can be
  short, and a prompt that no note fits gets no index. Before, the index
  had no floor unless `NOBLIVION_RECALL_INDEX_MIN_SCORE` was set: on the
  eval set every prompt got 30 rows, also the 20 prompts that fit no note
  (precision 0.023, recall 1.000). With the floor 1 of these 20 prompts
  gets rows (precision 0.857, recall 0.750). The floor is a default of the
  hook, so a config file of an older release gets it too. Set the variable
  to `off` for the index with no floor. A value that is not a number from
  -1 to 1 now gives the default, not "no floor".
- Behaviour change for error recall in its default `local` mode: the floor
  `NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE` is now 0.39, not 0.75
  (NOBLIVION-48). The old value was not measured. On the eval set it let 1
  of 14 expected notes pass (recall 0.071). At 0.39, 9 of 14 pass (recall
  0.643) and no wrong note passes (precision 1.000). The quote check still
  runs after this floor.
- `tools/eval_thresholds.py` now also measures the two paths of a default
  install: the ranked index with the shipped config file, and error recall
  in `local` mode. CI gates both. The docs name the path that each
  published number describes. The eval set is small and has no held-out
  split, so the numbers are too good as a forecast for real notes
  (NOBLIVION-48).
- Behaviour change: the transcript miner is off by default (NOBLIVION-53).
  Before, it ran at the end of every session. No precision figure is
  measured for its notes yet, and it stored false notes (see Fixed). To
  turn it on, set `miner.enabled` to `true` in `config.json` or set
  `NOBLIVION_MINER=1`. A config file or an environment that already turns
  the miner on keeps it on. While the miner is off, `noblivion mine` reads
  no transcript and says how to turn it on. Notes mined before stay in the
  database. The update does not delete or change them.

### Fixed

- The citation trust signal (`NOBLIVION_TRUST_CITATION_USE`) no longer
  counts a reply that saves or edits the note it names, or a reply that
  lists the notes: a turn whose prompt asks to list, show or print the
  rules or memories, or a reply that names 5 or more shown notes at once.
  On a fresh real-session sample its precision rose to 15 of 29 (Wilson
  95% lower bound 0.344), still below the 0.70 rule, so the signal stays
  off by default. docs/trust.md has the numbers (NOBLIVION-43).
- The secret redactor of the store now removes a private key block
  (`-----BEGIN ... PRIVATE KEY-----`, also one with no END line), a secret
  whose key is in quotes (JSON: `"password": "value"`), the value of a key
  that ends in `pass` (`DB_PASS=value`) and the token prefixes `glpat-`,
  `AIza`, `sk_live_`, `hf_` and `npm_` (NOBLIVION-46). Before, the indexer,
  the transcript miner and the MCP tool `noblivion_remember` stored these
  as written. The redactor of the hooks now removes the same forms: the
  quoted key and the last 4 prefixes were missing there. One list of
  secret forms is now tested against both redactors. The redactor version
  is 2, so the first index scan after the update writes every Markdown
  note again with the new rules. Notes that the transcript miner stored
  before the update are not written again.
- The credential guard now also blocks these reads of a git config that
  holds a credential URL: the file as standard input (`cat < .git/config`),
  a glob on a folder name (`cat .g*/conf*`), a brace list
  (`cat .git/{config,HEAD}`), a variable or `$( )` as the path
  (`f=.git/config; cat $f`), `git var -l`, and a `GIT_TRACE*` variable on a
  git command that talks to the remote. A path that the guard cannot
  resolve is blocked only when the git config holds a credential and the
  path can be that file. The README now says what the guard covers: git
  URLs, not every credential. docs/guards.md lists the limits
  (NOBLIVION-47).
- The credential guard no longer needs seconds for a Bash command with one
  very long word (NOBLIVION-47). The search for a `.git/config` path inside
  a word read the word again from each of its characters: one word of
  100 KB took about 10 seconds. The guard hook allows a call that it cannot
  decide in 1.5 seconds, so such a word let a read of a git config pass.
  Now a word is read once, and the same command is decided in under 0.1
  seconds. The paths that the search finds are the same.
- Hybrid ranking no longer gives a keyword rank to a note that matches no
  word of the query (NOBLIVION-49). Before, the keyword list held the whole
  pool, and the notes with a keyword score of 0 were ordered by id. So the
  oldest notes got a fused score for a query they do not match. A note with
  no vector yet and no matching word was returned with no score, and the
  hook floors keep a row with no score: during a first backfill or a model
  change, old notes that had nothing to do with the prompt reached the
  context. Now the keyword list holds only the notes that match, as in
  keyword-only mode, and a note with no vector and no match is not returned.
- A rule that the guard table build skips no longer goes unnoticed
  (NOBLIVION-50). A rule whose `violates` matches the empty command or an
  everyday command, or is too slow, does not block. Before, only
  `guard_table.py --rebuild --report` listed it. Now the session start
  prints one line that names the skipped rules and the reason.
- The guard table no longer goes stale (NOBLIVION-50). Before, a rule that
  a shell command deleted, edited or moved (`rm`, `sed`, `mv`, `git
  checkout`) kept its old effect until the next session, and a write of a
  `project_*.md` file did not rebuild the table. Now the table holds a
  stamp of the memory files (names, sizes, change times). The guard hook
  rebuilds the table before a call when the stamp changed, and a write of
  a memory file of any kind rebuilds it. The check adds about 0.3 ms to a
  call with 200 notes. A table that `NOBLIVION_GUARD_TABLE` names is not
  checked on each call.
- A deny check that runs out of time is no longer silent (NOBLIVION-50).
  The call is still allowed after 1.5 seconds, but one line now says that
  the deny rules were not checked for this call. Before, only the guard
  log recorded the timeout.
- `noblivion consent embeddings --revoke` now stops a running store at
  once. Before, the store read the consent only when it started, so every
  prompt and every backfill text still went to the remote embedding
  service until the store exited. The store now reads the consent before
  each remote call: each prompt embedding, each backfill batch and each
  single-row retry. Without the consent it sends nothing, and recall uses
  keyword search only. The same rule applies to an Ollama server that is
  not on this machine. A local backend does not pay for the check. A
  prompt embedding that still waits when its 1 second limit ends is no
  longer sent later. A new consent takes effect within seconds, without a
  restart. The health answer has a new field `embedding.consent`:
  `not_needed`, `given` or `missing` (NOBLIVION-51).
- A store whose embedding backend is `failed` no longer starts a thread
  and writes the line `embedding state: failed` to `store.log` every 0.5
  seconds (NOBLIVION-51). A failed model load and a start without the
  consent did this before, and with the entry above a consent revoke did it
  too: about 170,000 log lines a day. Now the store starts the backend
  again only when a start can change the state: when the hourly retry time
  is over, or when the consent is back. A new consent still takes effect
  within seconds, without a restart.
- The transcript miner no longer stores an ordinary request as a
  `correction` note (NOBLIVION-53). Its own word list read "Run the tests
  again please", "Is there no index on this table?" and "Please revert the
  last commit" as corrections. The miner now uses the correction test of
  the stop checks and the trust signals (`is_correction` in
  `hooks/stop_checks.py`), so one rule says what a correction is. When that
  file does not load, the miner stores no correction.
- The transcript miner takes a `review_request_changes` note only from the
  result of a reviewer agent call, the `Agent` or `Task` tool
  (NOBLIVION-53). Before, the text `**Verdict:** REQUEST_CHANGES` in any
  tool result made a note that said "Independent review": a web page, a
  file or shell output could put its own text there, a shell command
  included.
- docs/transcript-miner.md no longer says that the miner removes text that
  looks like an instruction to the model. The injection filter masks a
  fixed list of phrases. An instruction in plain words passes
  (NOBLIVION-53).

## 0.1.5 - 2026-10-04

Adds the real-session trust results and the replay tool that produced
them. On 521 real sessions the time-split test found no gain, and the
citation signal had low precision, so trust ranking and both trust signals
stay off by default. Also fixes three store and migration bugs. After the
plugin update, type `/noblivion:setup` in the next session: the store venv
must be rebuilt for 0.1.5.

### Added

- `tools/replay_transcripts.py` replays past Claude Code sessions offline
  through the prompt hook and the Stop flush's citation and correction
  scan, with the original times, into a scratch store. Then `noblivion
  trust timesplit` and the trust report run on real history
  (NOBLIVION-39).
- docs/trust.md has a new section "Real-session results (0.1.5)": counts,
  time-split results and the measured precision of the two transcript
  signals on one developer's real sessions, with the rules that set the
  defaults. Citation precision was 7 of 40 (Wilson 95% lower bound
  0.087), the correction signal had no event, and the time-split verdict
  was `no gain`, so the citation signal, the correction signal and trust
  ranking all stay off by default (NOBLIVION-39).

### Fixed

- `noblivion migrate-from-legacy` and the migration check in the setup
  script follow `CLAUDE_CONFIG_DIR` (NOBLIVION-40). Before, they read
  `~/.claude` also when another profile was in use, so `--apply` could
  change the settings and hooks of a profile without the plugin. With
  another profile, `~/.mcp.json` is left alone. New option `--config-dir`.
- The embed backfill no longer writes a vector of an older text
  (NOBLIVION-41). A pass that read a row before it changed could overwrite
  the vector of the new text, so recall ranked the row by its old text until
  the next pass. The write now checks the current text of the row in the
  same transaction.
- The `ensure-running` concurrency test accepts `running` from a caller that
  starts late and finds the store already up. It still asserts one `started`
  and one store process.
- With periodic indexing off (`NOBLIVION_INDEX_INTERVAL_S=0`), the store
  now runs an embed backfill pass when `content_rev` moves. Before, rows
  that `noblivion index` added or changed kept an old vector, or had none,
  until the store restarted (NOBLIVION-42).

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
