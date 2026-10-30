<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Changelog

All notable changes to NOBLIVION. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

- `noblivion explain` now runs on stores over 10,000 notes or chunks. The offline diagnostic refused
  a real store of 10,406 chunks; the time limit bounds its work.
- Add `tools/eval_suite.py`: one command for every zero-token replay and cost check on a scratch
  store, with a before-and-after report that exits 1 when delivery drops. The prompt-row replay now
  also counts the task notifications and queued prompts the hook runs for, and rows whose note name
  holds parentheses.

## 0.1.18 - 2026-10-29

- Token cost: measured on 16 model runs against the previous build (one run per slot). Prompt rows
  fell from 32 to 5 per run, recall calls from 3.0 to 1.9, deferred tool-loading calls from 9 of 14
  runs to 0 of 16. Total calls and input tokens were not shown lower (calls 29.6 against 28.1, input
  tokens 1,314k against 1,268k; the intervals include zero), so the sample is too small to confirm a
  total saving. Delivery, pass rate and errors were equal.
- Upgrade: the new recall defaults (`recall.rules_only=1`, `recall.prompt_rows=5`,
  `recall.hub_days=14`) apply to new installs only, because the installer keeps an existing
  `config.json`. Add the three keys by hand on an existing install.
- Drop "after a tool error you cannot explain" from the recall triggers (NOBLIVION-140). The error
  recall hook already answers a tool error at PostToolUseFailure with no model call, so the text
  asked the model for a second lookup. The server instructions, the tool description, the skill
  description and `docs/recall-reflex.md` now name five moments: cannot verify, merge, restart,
  ticket and remember. Count over the 32 NOBLIVION runs of the recorded test 2 campaign: 94
  `noblivion_recall` calls, 3 of them straight after a tool error, none of them with the lesson
  note in the result, so the recorded deliveries lost through this trigger: 0. The sentence "read
  it in full with `fetch_id`" stays: in 5 of the 13 runs where the lesson came through the recall
  tool, the note appeared only in a `fetch_id` result.
- Load the two NOBLIVION MCP tools eagerly (NOBLIVION-141). Claude Code lists a plugin's MCP tools
  by name only, so the model paid one `ToolSearch` call (0.9 calls and about 30,000 input tokens
  per run in the benchmark) to load `noblivion_recall` before its first use. `.mcp.json` now sets
  `"alwaysLoad": true` for the server. The Claude Code 2.1.261 binary reads that key in the config
  of a stdio server and then keeps each of its tools out of the deferred list; a plugin run with
  the key connected the server with no error. The cost: the two tool definitions, 2,591 characters
  (about 650 tokens), ride on every call, about 17,000 tokens over a 26-call run. Whether the model
  then makes no `ToolSearch` call needs a model run.
- Show guard rows once per memory and tool, and cap one show at 1800 characters (NOBLIVION-137).
  A trigger row was shown up to 3 times per agent in a session, and one show could reach 3600
  characters, so the same rule rode along on later calls. Now a memory's trigger row shows once for
  each tool (Bash, Edit ...) in a session, and `ROWS_MAX_CHARS` is 1800 (was 3600). The deny is
  decided before any row is rendered and does not change; a deny still repeats at every call. Offline
  replay of the 499 Bash calls of the 32 recorded stock-memory runs: rows shown 116 to 108, characters
  51.9k to 46.3k, calls with rows 58 to 57, guard actions unchanged (4 of 32 runs, 12 calls). The
  lowering of `MAX_ROWS` from 3 to 2 was tried and held back: it dropped the third row, the lesson
  row of one reserve scenario. See `docs/guards.md`.
- Cap the session briefing at 5 rows (NOBLIVION-138). The SessionStart briefing printed up to 8
  decision, 5 open and 2 trap rows, and it rides on every later call of the session. Now the
  default quotas are 2, 2 and 1 (`NOBLIVION_CONTINUITY_QUOTA_DECISION`, `..._OPEN`, `..._TRAP` set
  them back). The briefing is a hook that is off unless `NOBLIVION_CONTINUITY` is set. Measured in
  a replay on a large home memory folder (about 190 notes): 15 rows and 1,454 characters to 5 rows
  and 670 characters. The save before a compaction and the restore after it do not change. Gate 0
  verdicts of all 46 primary and reserve checks are identical. See `docs/configuration.md`.
- Shorten the memory format message to the fix (NOBLIVION-139). After a write of a feedback memory
  with a missing or wrong rule field, the PostToolUse hook added a rule schema of 780 characters,
  with the optional fields (`violates`, `complies`, `rewrite`, `protects_process`). One benchmark
  run spent 4 calls on it. Now the message names the problems and the four required fields in one
  line (`SCHEMA_LINE_SHORT`); the optional fields are in `docs/guards.md`. A bare feedback file:
  912 to 371 characters; a feedback file with a rule only: 895 to 354. The message of a project
  memory (323 characters) does not change. The hook still never blocks a write.
- Ship the rules-only prompt path in the default config file (NOBLIVION-121). The prompt hook
  showed up to 30 rows, about 7,100 characters, on a first prompt, and 98 percent of them were not
  used. `config/config.default.json` now sets `recall.rules_only` to 1, `recall.prompt_rows` to 5
  and `recall.hub_days` to 14, so a new install shows at most 5 rows that each carry a rule and an
  apply text. The code defaults are unchanged, and `install.sh` keeps an existing `config.json`, so
  an installed config needs the three keys added by hand. Offline replay of the 25 prompts of the
  frozen test scenarios in the gate 0 box (a copy of the same large home memory folder): rows
  521 to 100, characters 139,465 to 48,027 (-65.6%), and the lesson row keeps its rank in all 25
  (no lesson row left the top 5; one sits at rank 4) and still carries its rule and apply text.
  Gate 0 verdicts of all 46 checks are identical. Replay of the 499 Bash calls of the 32 stock runs
  through the guard (label rows follow the switch): calls with rows 58 to 46, rows 116 to 90,
  characters 51.9k to 43.0k, guard actions unchanged. See `docs/configuration.md`.
- Fire a command trigger when the command runs with a directory path (NOBLIVION-135). A trigger
  such as `tools/worktree_guard.py remove` or `docker compose down` matched the start of the
  command only, so `python3 /srv/proj-a/tools/worktree_guard.py remove x` and
  `/usr/bin/docker compose down` gave no row. Now the command word, and the script after an
  interpreter (`python3`, `bash`, `node`), also count with each path suffix and without the
  interpreter. Offline replay of 2278 recorded Bash calls: 136 calls (6.0%) get a row they did not
  get, 0 extra denies. Rows only. See `docs/guards.md`.
- Read the notes of the shared roots in the local steps (NOBLIVION-133, NOBLIVION-134). The store
  ranked the notes of `recall.shared_roots` (by default the home root) in every session, but the
  prompt row's rule join, the guard table and the continuity print read the session's own folder
  and `global_memory_dir` only. So a note from another project folder showed its summary and no
  rule, its `violates` rule and triggers did nothing, and the print and restore after a compaction
  were empty although the saved list held the lesson. Now these steps also read
  `<config dir>/projects/<root>/memory` for each shared root that exists. The project folder wins
  on a file name that two folders hold. `recall.shared_roots: []` turns it off. This adds rows and
  denies in the folders of other projects: an offline replay of 1206 recorded Bash calls of a
  second project went from 0.17 to 1.62 rows per call (the rate of the home folder), with no
  session budget in the replay. See `docs/configuration.md`.
- Grade everyday triggers by relevance instead of dropping them (NOBLIVION-129). A note whose
  trigger is `pytest`, `kill` or `git worktree` gave no row on the commands that matter. Now
  the trigger goes through the weak-trigger grading: the row shows when the note's rule names
  the command, or shares enough rare words with the command or with the call's description and
  the last prompt of the session transcript, and stays quiet otherwise. These rows show the
  rule line only, after every other row, at most 3 per call, with a budget of their own (1500
  characters per agent and session); triggers on text plumbing (`cat`, `tail`, `grep`, `cd`)
  stay dropped. `python -m X`, `python3 -m X` and `<path to python> -m X` now also fire a
  trigger `X`. Rows only: no new deny. On by default; `NOBLIVION_GUARD_EVERYDAY_TRIGGERS=0`
  (config key `guard.everyday_triggers`) restores the old behaviour. See `docs/guards.md`.
- Keep a high-ranked row under the relevance floor (NOBLIVION-130). A row that stands in the top
  30 of the re-ranked order stays in the prompt index although its cosine is under the floor
  (0.68), when its cosine is at most 0.08 under the floor
  (`NOBLIVION_RECALL_FLOOR_EXEMPT_MARGIN`, config key `recall.floor_exempt_margin`), its note has a
  `rule:` or `apply:` line and the row shares a word with the prompt (its local keyword score is
  above 0; a word in more than half of the notes does not count). A row with no rule, a pure
  vector near-miss, a row past place 30 and a row far under the floor are still cut. The row count
  and the character cap run after it and are unchanged. Off by default
  (`NOBLIVION_RECALL_FLOOR_EXEMPT=1`, config key `recall.floor_exempt`, turns it on) because it adds
  rows. The `recall.log` note `floor_exemptN` counts the kept rows. See `docs/how-recall-works.md`.
- Let an exact file name match count above the 8-note label limit (NOBLIVION-131). A file label
  that equals the target of the tool call (`file_path` of Edit, Write, MultiEdit and Read, or a
  path word of a Bash command) by full file name or full relative path now matches although more
  than 8 notes hold it. At most 3 such notes show, best first, and the guard log lists them under
  `label_exact`. A partial or substring match, a name only in quoted text or edit content, a prompt,
  and a weak label (a tool or a `.md` file) keep the old limit. See "Label rows" in `docs/guards.md`.
- Make the guard rewrite act on the commands a model types (NOBLIVION-126). The rewrite now
  reads a command as the deny check does: it rewrites each simple command of a list or a
  pipeline that starts with the prefix (`cd x && pytest`, `make; pytest`, `pytest | tail`),
  after the wrappers `env`, `timeout`, `VAR=value`, `sudo` and `nice`. `python -m <prog>`,
  `python3 -m <prog>` and a path to a python binary with `-m <prog>` count as `<prog>`. Every
  other byte of the command stays. Quoted data, heredocs and comments are never rewritten, and a
  command whose output goes to a pipe other than `| tail`, to a file, or out of a group or a loop
  is left alone. Shadow and ask act on the new shapes as before. Off by default
  (`NOBLIVION_GUARD_REWRITE`).
- Accept a root that starts with a dash in `noblivion explain` (NOBLIVION-132). A root is a folder
  slug such as `-home-alice`, and argparse read the value after `--root` as a flag, so
  `noblivion explain 'x' --root -home-alice` failed with `Invalid diagnostic arguments`. Now
  `--root -slug` works like `--root=-slug`. A missing value names its option (`--root needs a value`).
  The reason never echoes the prompt or an unknown option.
- Add the note field `protects_process` (NOBLIVION-127): a regex over the command line of a
  process. Before a Bash call, the guard finds the processes the command would signal (`kill
  PID`, `kill -PGID`, `kill $(pgrep ...)`, `pgrep ... | xargs kill`, `pkill`, `killall`) and
  denies the call when one of them matches, with the same reason and override as a `violates`
  deny. So `ps`, then `kill 4711`, is blocked when 4711 is a protected CI runner. Linux only
  (`/proc`); on other systems the check finds nothing. The memory fields check validates the
  field, the guard table carries it, and a dedup merge keeps it. See "Deny: protect a process" in
  `docs/guards.md`.
- Share the home root by default (NOBLIVION-125). When `recall.shared_roots` is not set, it now
  holds the home root: the root of the Claude Code project folder of the user's home folder, named
  by the hooks' own rule (`project_slug(project_folder(home))` in `hooks/hook_config.py`). A note
  saved in a home-folder session, such as a ruling, a convention or a tool trap, now reaches
  sessions in other project folders through prompt recall, the ranked index (subagent rules, error
  recall), the MCP recall tool, fetch and `noblivion explain`. Before, the default was `[]` and
  such a note never left the home project. This can add home-project notes to other projects'
  prompts; they pass the same relevance floor and caps as any other note, and a file of the
  session's own root still wins over a file with the same name. An explicit list is used as
  given; `"recall": {"shared_roots": []}` keeps the old behaviour. A session in the home folder
  sees no change. The guards, label rows and stop checks read memory folders and do not change.
  See `docs/configuration.md`, "Shared roots".
- Read the error body behind a generic `curl` line in error recall (NOBLIVION-136). After a failed
  call that printed `curl: (22) ...`, the search stopped at that line, which names the tool and
  not the cause, and the quote check removed every double-quoted message, so a JSON body
  `{"error":"..."}` gave no words. Now the weak error lines of a failed call are read after a
  `curl: (N)` line, and the value of a JSON `error`, `message`, `detail`, `error_description`,
  `msg` or `reason` key counts as the error message. A specific strong line still hides the weak
  lines. A call with no error-form line (`gh pr checks` with exit 8) still shows nothing: a
  search from the command text would give up the 0 false shows of the local mode. Offline replay
  of 229 recorded failed Bash calls (fused mode, the same rule as an offline patch): shows 24 to 24,
  0 extra characters.
  See `docs/how-recall-works.md`.

## 0.1.17 - 2026-10-19

- Raise the precision of the behavioural use signal (NOBLIVION-120), still off by default
  (`NOBLIVION_TRUST_BEHAVIOUR_USE`). A `complies` regex now runs on the masked view of the Bash
  command, so heredoc bodies, quoted text and comments are no run. A command trigger counts only
  when the note's own `rule` or `apply` says to run that command, the trigger is held by this note
  alone, and it is not the name of a host or container. `glob:` triggers and file tool calls are
  dropped. Each tier has a code switch (`TIER_COMPLIES`, `TIER_TRIGGER`) that stays off until a
  hand-labelled sample reaches a Wilson 95 percent lower bound of 0.5. See `docs/trust.md`.
- Add recall as a reflex (NOBLIVION-123). The descriptions of `noblivion_recall` and
  `noblivion_remember` name the moments to call them (before "cannot verify", a merge, a restart
  of a shared service, filing or commenting on a ticket, after an unexplained tool error, before
  saving a memory). The MCP server sends the same moments as short `instructions`, because Claude
  Code shows a deferred MCP tool by name only (`NOBLIVION_MCP_INSTRUCTIONS=0` leaves them out). The
  plugin ships the skill `recall-first`. New hook `hooks/recall_nudge.py`, off by default:
  `NOBLIVION_RECALL_NUDGE` (config key `recall.nudge`) adds one line of context under 120
  characters before a ticket create or comment tool or a merge command, only when the session made
  no recall call and got no line in its last 10 tool calls. `NOBLIVION_RECALL_NUDGE_SHADOW`
  (`recall.nudge_shadow`) logs only. The matching tools and commands come from the config lists
  `recall.nudge_tools` and `recall.nudge_commands`, with generic defaults. `nudge.log` counts the
  lines and the recall calls per moment. The new tool `tools/replay_recall_nudge.py` counts the
  lines on past transcripts. See `docs/recall-reflex.md`.
- Add rules that act at the tool call, off by default. `NOBLIVION_GUARD_REWRITE` (config key
  `guard.rewrite`): when one note's `violates` expression hits and the note has a `rewrite:`
  field (`add-flag <flag> after <prefix>`, from a closed table: `-q` for the test runners,
  `--oneline` for `git log`, `-sb` for `git status`), the guard sends the command back in the
  shape of the rule as `updatedInput`. The user sees it first unless
  `NOBLIVION_GUARD_REWRITE_DECISION=allow`. `NOBLIVION_GUARD_ASK` (`guard.ask`): a note with
  `on_violation: ask` makes the guard ask instead of deny. `NOBLIVION_GUARD_ACT_SHADOW`
  (`guard.act_shadow`) logs `would-rewrite` and `would-ask` only. Every decision is a line in
  `guard-log.jsonl`. The memory fields check validates both fields on write. The new tool
  `tools/replay_guard_rewrite.py` measures the matcher on past commands.
- Fix a deny that the rewrite settings could not remove (NOBLIVION-122). A note with a valid
  `rewrite:` field is a rewrite note, not a deny note. It never adds a deny: with `guard.rewrite`
  and `guard.ask` off, or with only `guard.act_shadow` on, the plain command runs unchanged (the
  shadow switch logs `would-rewrite`). With `guard.rewrite` on, the guard rewrites the command;
  when it cannot (a compound command, `cd x &&`, `git -C`), it falls back to the note's
  `on_violation` decision, which is `ask` by default and needs `guard.ask` (with `guard.ask` off
  the command runs unchanged and the log says why). A note without `rewrite`, and a note with
  `on_violation: deny`, deny as before. The guard table build and the write hook warn about a
  note that has `rewrite` and `on_violation: deny`.
- Add a rules only prompt path, off by default. `NOBLIVION_RECALL_RULES_ONLY` (config key
  `recall.rules_only`) shows only rows whose note has both a `rule:` and an `apply:`, with a
  short apply text under each row, and applies the same rule to the label rows at the prompt
  and at the tool call. `NOBLIVION_RECALL_PROMPT_ROWS` (`recall.prompt_rows`) caps the rows of
  one prompt. `NOBLIVION_RECALL_HUB_DAYS` (`recall.hub_days`) moves the notes that the hook
  would show in more than half of the sessions of the window behind the other rows and keeps
  `NOBLIVION_RECALL_HUB_ROWS` (`recall.hub_rows`, default 1) of them. `recall.log` carries
  `rules_dropN`, `hubN`, `hubcutM`, `rowcapN` and `wasC`. The new tool
  `tools/replay_prompt_rows.py` measures characters, rows and hub share per prompt on past
  sessions.
- Add a behavioural `use` signal for the trust events, off by default
  (`NOBLIVION_TRUST_BEHAVIOUR_USE`, config key `trust.behaviour_use`). A tool call
  after the showing that runs a shown rule's `complies` command, fires one of its strong
  triggers or edits a file that its `glob:` trigger names is a `use` with the source
  `behaviour`, unless the rule's `violates` regex matches. The replay tool reports the uses
  by source and the use rate.

## 0.1.16 - 2026-10-18

- Add frozen held-out evaluation and paired injection replay with external task-state checks.

- Add bounded, read-only `doctor` checks and secret-safe `explain` recall diagnostics for Codex and other clients.

- Add safe Codex integration removal and respect `CODEX_HOME` during installation and removal.
- Keep deny rules active during concurrent guard rebuilds, resume interrupted regex checks, and show skipped-rule warnings in Codex.

## 0.1.15 - 2026-10-14

### Added

- Error recall reaches the mined `tool_error` notes. In `store` and `fused` mode the hook
  asks the store for mined notes, and a mined `tool_error` note is shown when it quotes
  three words in a row from the error, as for a memory file (the quote check is
  unchanged). The index answer of a mined note now holds `category`, `rule` and `apply`.
  The miner writes labels for a tool error (the tool name, the command head and the error
  class) and a derived rule line (the first sentence of the lesson) and apply line for
  every new note. A note from an older miner gets the same two lines at answer time. The
  prompt index does not show mined notes: in a read-only sample of 30 shows from the
  `correction` notes, under 0.70 were useful for the prompt, so that part is not shipped
  (NOBLIVION-105).
- The keyword score reads the `rule:`, `apply:` and `triggers:` lines of a memory file, in
  the store and in the hook re-rank, with double weight. The prompt index shows `rule`
  and the APPLY excerpt shows `apply`, but a word that only they held could not find the
  note. The `labels` column now adds a bounded boost (at most 0.8 of a BM25 score) when
  the prompt, or the new optional `target` parameter of `/api/memories/index`, names a
  label of the note. A label alone never admits a note. The stored text, `fetch_id` output
  and the embeddings do not change. Schema 6 adds the `index_text` column, and the next
  index scan writes every row once to fill it (NOBLIVION-104).
- `noblivion learning status` lists pending extraction jobs under `pending`, with
  the age and the reason each one waits: a paused session or scope,
  contribution off, a provider error, the budget, or no worker yet. A job held
  by a pause for more than 7 days is flagged `stale` and counted. Nothing is
  dropped (NOBLIVION-107).
- Learned notes carry a `description:` and a `rule:` line, so the prompt index
  can show them. An existing learned note is rewritten with both on the next
  projection (NOBLIVION-107).
- `noblivion dedup sweep` runs pairs, plan and apply in one command for a
  timer. It asks no question and needs the dedup consent. It stops before a
  pair that could pass the daily cost cap (`--daily-cost-cap`,
  `dedup.daily_cost_cap_usd`, default 0.5 USD, all plans of the UTC day
  counted) and prints one line for each merge. See
  [Duplicate sweep](docs/dedup-and-privacy.md#run-the-sweep-on-a-timer)
  (NOBLIVION-108).
- `noblivion dedup apply` now acts on a `SUPERSEDE` verdict: the current note
  stays, the other note moves to the archive with a `superseded_by` line, and
  `undo` restores it byte for byte (NOBLIVION-108).
- The index cuts each memory file into sections (a summary chunk, then one chunk
  per heading, at most about 1,200 characters, with one sentence of overlap when
  a section is split). Each chunk has its own vector and keyword score, so a topic
  far down a long file is found; the file still has one row, one hit and the same
  `[claude_code_md: <path>]` marker. The best chunk gives the file its place and its
  score, and `/api/memories/index` rows name it (`chunk`, `heading`). A short file is
  one chunk with the text and the vector it had before. Schema 5 adds the chunk tables
  and cuts the existing rows when the store starts, with a backup first (NOBLIVION-103).
- `noblivion_recall` with `fetch_id` takes `chunk` (a section number) or `section`
  (words) and returns that section with its heading. A note over 6,000 characters now
  opens as its summary and the list of its sections, so the text past 6,000 characters
  can be reached. A search entry for such a file shows its header and the best section.
  `tools/eval_chunks.py` measures this on a held-out query set (NOBLIVION-103).

### Fixed

- Learning capture decides eligibility per mapped scope, on the first event in
  that scope. A session that starts in an unmapped folder and later runs in a
  mapped one is captured for that scope from there on. Its earlier part is not
  imported, and a resumed session is still never swept (NOBLIVION-107).
- A learning worker whose lease was lost to another worker, a pause or an
  exclusion stops quietly. It no longer ends the pass with a
  `learning pass failed: LeaseLost` error (NOBLIVION-107).
- The delivery drain uses the release it runs from, not a `repo_root` left in
  `learning-client.json` by an older install (NOBLIVION-107).
- The prompt index splits its character cap between the rows and the APPLY
  text. The rows keep a guaranteed share of 60 percent. An APPLY block that does
  not fit is left out and its row stays, so long APPLY text no longer pushes the
  lowest rows out. The log status says how many rows the cap cut
  (`:capcutNofM`) (NOBLIVION-109).
- The prompt hook without an index switch now drops `MEMORY.md`, `topic_*` and
  closed memories, as the index does, before it takes its top hits. A hit also
  carries its file's `rule:` and `apply:` lines (NOBLIVION-109).
- `noblivion_recall` in query mode shows the part of each memory that holds the
  query words, not the first 200 characters of its body (NOBLIVION-109).
- A store row finds its local memory file by the file path the store returns,
  then by name. A name over 120 characters, which the store cuts, and two files
  of one name no longer lose their rule, APPLY text, hygiene and re-rank
  (NOBLIVION-109).
- `noblivion_recall` with `fetch_id` now reads a rank through the calling session's last
  index and marks the calling session's shown-set. With several sessions open it used the
  newest file of any session, so rank 3 could be another session's memory. A new
  PreToolUse hook, `hooks/mcp_session_hook.py`, writes the session id (and the subagent id
  inside a subagent) into the arguments of each recall call, and the server reads a
  subagent's files under `cache/subagent`. The id only chooses a cache file. Without an
  id the newest file is used as before, and the tool result now says so (NOBLIVION-110).

## 0.1.14 - 2026-10-13

### Added

- Evidence-linked generated claims, complete support/equivalence checks and
  canonical revisions preserve exact passages, dates, correction history and
  transitive withdrawal. Query context is available through HTTP, CLI and native
  prompt hooks. Per-session opt-outs preserve other callers' context (NOBLIVION-101).
- Durable extraction continuation receipts expose saturated, deferred and
  exhausted coverage instead of silently dropping lessons after twelve proposals.
  Extraction, synthesis and downstream draft jobs share existing leases and
  persistent request/token/time/concurrency/spend budgets (NOBLIVION-101).
- Repeated independently verified native action/outcome evidence can produce
  validated inert procedure drafts requiring review. No execution, activation or
  permission path is added. Held-out real-store fixtures measure external task
  state and disclose causal limits. Phoenix patterns are adapted as separately
  maintained Noblivion code with no shared runtime or release (NOBLIVION-101).

### Fixed

- One Codex chat's memory opt-out no longer blocks recall in another chat when
  the native PreToolUse hook is active. The hook binds each MCP recall to its
  own session with a short-lived, one-use store capability. The Codex installer
  adds the recall tool matcher and requires a valid capability before any MCP
  memory read. Other clients keep the conservative unknown-session privacy
  guard. Every read checks current project and session controls (NOBLIVION-100).
- Ephemeral recall capabilities are removed from NOBLIVION journals, learning
  capture, spools and at-rest text without dropping the rest of a contribution.
- Portable Codex stdio forwarding now carries native PreToolUse through the
  remote adapter and requires a per-call capability at the remote MCP server.
  Explicit workspace mappings and independent local contribution controls also
  apply to this path. See [Codex setup](docs/codex.md) (NOBLIVION-100).

## 0.1.13 - 2026-10-12

### Added

- A session that runs an older NOBLIVION than the installed one now says so. The
  new `version_check` hook prints one line, once for each session and installed
  version: `NOBLIVION X is installed, but this session runs Y. Run /reload-plugins
  to switch.` It stays silent outside the plugin cache layout (NOBLIVION-99).
- `scripts/noblivion_mcp_launcher.py` starts the MCP server of the installed
  version. An MCP client outside Claude Code, such as one that connects over SSH,
  runs a copy of it at a fixed path and follows each update without a config
  change. See [Update](docs/install.md#update).

## 0.1.12 - 2026-10-12

### Fixed

- Codex-generated project instructions and environment context no longer enter
  background learning as direct user evidence (NOBLIVION-98).
- A deny rule now also checks a heredoc body or a here-string that a shell
  reads (NOBLIVION-52). Before, the guard removed every heredoc body as
  data, so `bash <<'EOF'` with `git push --force` in the body was not
  denied. The body is code when its reader is `bash`, `sh`, `zsh`, `dash`
  or `ksh` (also by path, with `-s`, and behind `sudo`, `env`, `exec`,
  `nohup`, `timeout` and the like), `su`, `sudo -s`, `sudo -i`, or `ssh`
  with no remote command or with a remote shell. `cat <<EOF | bash` and
  `bash <<< "..."` count too. A body for any other reader, such as
  `cat > file`, `tee`, `git commit -F -` or `python3`, stays data.

## 0.1.11 - 2026-10-12

### Added

- Opt-in background learning for Codex and Claude, with durable capture,
  validated source evidence, bounded local extraction, revision history,
  correction reconciliation, independent memory controls and index receipts
  (NOBLIVION-98). See [background learning](docs/learning.md).

### Fixed

- Signal-triggered store shutdown no longer reacquires the main thread’s stop-event
  lock, preventing a deadlock when SIGTERM or SIGINT interrupts a wait.
- Concurrent database upgrades use distinct backup files and publish each backup
  only after the copy completes, preventing startup collisions and partial backups.
- Superseded and excluded memories stay out of recall across repeated index
  scans and cached searches. Root-scoped fetches enforce project boundaries.
- Codex adapter upgrades preserve existing command enforcement prefixes and
  unrelated TOML settings, including hook trust state inside an older managed block.

## 0.1.10 - 2026-10-12

Fixes lost recall after an idle stop: the first prompt after the store has
stopped for idleness now waits for the store to start, so it gets its
memories. After the plugin update, type `/noblivion:setup` in the next
session, so that the store restarts with 0.1.10.

### Fixed

- The first prompt after the store stops for idleness no longer loses its
  recall (NOBLIVION-97). The store stops itself after about 30 minutes
  without use. The next prompt asked for a start, but the recall hook did
  not wait for the new store, so that prompt got no memories (11 of 164
  prompts in one measured week). Now the hook waits up to 2 seconds for the
  store it just asked to start, then asks again. With autostart off, or when
  the store never comes up, the hook still fails open at once.

## 0.1.9 - 2026-10-11

Fixes `noblivion_remember` in Claude Code: it now saves a note with no
extra setting. After the plugin update, type `/noblivion:setup` in the
next session, so that the store restarts with 0.1.9.

### Fixed

- `noblivion_remember` works in Claude Code with no extra setting
  (NOBLIVION-94). Before, the tool needed `NOBLIVION_MEMORY_DIR`, but the
  plugin does not set it, so every save failed with "NOBLIVION_MEMORY_DIR
  must name the shared memory folder". Now, when the variable is not set
  and the client is Claude Code, the tool saves the note in the session's
  memory folder: the same folder that the hooks and the store use. The
  tool creates that folder (mode 0700) when it does not exist yet, as
  Claude Code does. `NOBLIVION_MEMORY_DIR` still wins. Codex still needs
  the variable, which its installer sets.

## 0.1.8 - 2026-10-09

Adds the command `noblivion import`, which imports memories from a JSONL
file. It is a dry run by default, and `--apply` writes the rows. A plain
imported memory enters recall in every session. `noblivion import --remove`
takes the rows of a file out again. The trust report names an imported
row by a short hash. See [docs/import.md](docs/import.md). After the plugin
update, type `/noblivion:setup` in the next session, so that the store
restarts with 0.1.8.

### Added

- New command `noblivion import FILE.jsonl` imports memories from a JSONL
  file, one JSON object per line (NOBLIVION-93). It is a dry run by
  default: it prints what it would do and writes nothing. `--apply`
  writes the rows. A second run inserts nothing. The rows are redacted
  like the miner's rows, and the running store embeds them. A plain
  imported memory enters recall in every session; a row with the source
  type `transcript_mined` comes back only on request. `--archived`
  stores the rows out of recall. The index scan never deletes an imported
  row. `noblivion import --remove FILE.jsonl --apply` marks the rows of
  that file as deleted; the store deletes them after the grace period.
  The trust report names an imported row by a short hash and counts its
  age from its first trust event in the store. The report counts the rows
  that wait for a vector, plain and mined rows apart: a backend that sends
  text off the machine embeds mined rows only with
  `embedding.remote_include_mined`. See [docs/import.md](docs/import.md).

## 0.1.7 - 2026-10-09

Changes defaults and the redactor version. The label rows at prompt time
are on with the shipped config file: up to 3 rows, at most 2400
characters per prompt. The relevance floor of the subagent rules hook is
0.68, not 0.52. It was measured on short prompts, so the longer text of a
real subagent task can score below it. A store that accepts a connection
but does not answer pauses its callers for 30 seconds. `install.sh`
restarts a store that runs. The redactor version is 3, so the first index
scan after the update writes the notes again. A memory file over 256 KB
is stored by its head. Also fixes the switches of the label rows, the
deletes of a folder that holds an unreadable note, keyword matches on
common words, and the time that the store redactor needs for large notes.

### Fixed

- The label rows at prompt time now read their switches from the
  `recall.env` block of the config file, like the rest of the recall hook
  (NOBLIVION-55). Before, they read the environment only, so the shipped
  config file, which sets `NOBLIVION_RECALL_LABELS=1`, did not turn them
  on, and `NOBLIVION_RECALL_DISABLE` in the config file did not stop them.
  Behaviour change: with the shipped config file, a prompt that names a
  file, a tool, a ticket or a service of a memory now gets up to 3 label
  rows (at most 2400 characters per prompt and 4000 per session). To turn
  them off, set `NOBLIVION_RECALL_LABELS` to `0` in `recall.env` or in the
  environment.
- A note file that the indexer cannot read or redact now holds back only
  the deletes of its own project folder (NOBLIVION-54). Before, one such
  file stopped the deletes in every folder, so deleted notes of all
  projects stayed in recall until the file was fixed. `noblivion index`
  and the store log name the file that holds back the deletes of its
  folder, and `noblivion index --json` shows the count per folder in the
  new key `deletes_held`. The other half of the report, a full rewrite on
  every scan after a redactor version change, was already fixed in 0.1.6.
- `install.sh` (and `/noblivion:setup`) now restarts a store that runs
  (NOBLIVION-57). Before, it kept a store of the same version, and the
  store reads `config.json` and loads the model only at its start. So a
  user who turned the embedding model on later, as docs/install.md says,
  kept keyword search only, with no sign. New option
  `noblivion ensure-running --restart`. The new store waits up to 90 s
  for the old one to stop, because a busy store needs up to about 71 s
  after `SIGTERM`. A wait of 10 s gave up while the old store still
  stopped, and then no store ran. The script prints one line before this
  step, which can take up to 100 s with no other output.
- `install.sh --no-embed` sets `embedding.backend` to `none` in
  `config.json` (NOBLIVION-57). Before, the default backend `fastembed`
  stayed, but the package was not installed. The store then reported
  `degraded` for good and logged a warning every hour. It does this also
  when `config.json` names another backend, and it prints the value it
  replaced. A `config.json` that is not valid JSON, whose `embedding` is
  not an object, or that cannot be read or written, stays as it is: one
  line names the file and the problem, and the install goes on, with no
  Python traceback. With a backend other than
  `fastembed`, the script no longer prints "embedding model ready".
- A relevance floor that is not a cosine (`nan`, `inf`, or a value outside
  -1..1) in `NOBLIVION_RECALL_MIN_SCORE`, `NOBLIVION_ERROR_RECALL_MIN_SCORE`,
  `NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE` or
  `NOBLIVION_RECALL_TRUST_MIN_SCORE` now counts as unset. Before, `nan`
  dropped every scored hit, `nan` or `inf` in the local error recall floor
  dropped every local hit, and `inf` in the trust floor gave no row its
  trust in mode `c`.
- A store that accepts a connection but does not answer no longer costs
  every prompt and every subagent start a wait (NOBLIVION-69). Before,
  each call waited for the listener proof (1 s) or for the search (2 s)
  to time out. Now a call that times out writes `<data dir>/store.hung`,
  and for the next 30 seconds the recall hook, the subagent rules hook,
  error recall and the MCP tool do not call the store. The log reason is
  `store_hung`. Measured with a store that never answers a search: the
  prompt hook took 2.1 s on each prompt before; now it takes 2.0 s on the
  first prompt and 0.04 s on each later prompt in the pause. A refused
  connection does not start the pause, and a new store ends it. The pause
  stops only a call with no more time than the call that timed out: error
  recall, which waits 0.8 s, does not stop the prompt hook, which waits 2 s.
- `recall.log`, `memory-dir.log`, `guard-log.jsonl` and `memory_sync.log`
  now move to `<name>.1` past 5 MB, as the stop check log does
  (NOBLIVION-69). Before, they grew without a limit. `memory-dir.log` gets
  one line per prompt in a project with no memory folder, and the guard
  log holds command text.
- One large or odd note no longer turns recall off (NOBLIVION-62):
  - `/api/memories/search` left out every entry after the first one that
    did not fit in the 512 KB answer. A top hit larger than that gave
    "No memories available." with smaller hits in the pool. Now only the
    entry that does not fit is left out, and a stored text too large to
    fit is left out before it is redacted.
  - The indexer stores the first 256 KB of a larger memory file. Before,
    it stored the whole file. The cut is at the last line end. When the
    last 128 KB in front of the limit hold no line end, the cut is at the
    last space; when they hold no space either, the cut is hard at the
    limit and drops the token characters there. A cut that is not at a
    line end also drops the text after the last quote mark. The head can
    still end inside a secret whose rule needs the part after the cut: the
    END line of a key block, the end tag of a `<password>` element, the
    rest of a token. So the indexer also redacts the first 320 KB of the
    file (the limit and the next 64 KB), and stores only the text on which
    the two redactions agree. The 64 KB are counted from the limit, not from
    the end of the head, so they also cover a head that is cut back to a
    line end near 128 KB. A secret that runs on for more than 64 KB after the
    limit is not covered. The data lines of a cluster Secret are masked when
    `kind: Secret` is anywhere in the file, also in the part that is cut
    off: `kubectl get secret -o yaml` puts `data:` before `kind:`.
    `noblivion index` names each file that it stored by its head,
    `noblivion index --json` counts them in the new key `files_cut`, and
    the store log names each one once.
  - `/api/memories/fetch/{id}` cuts a text that would make the answer
    larger than 512 KB, with `…` at the cut. Before, it had no limit.
  - The connection URL rule and the email rule of the store redactor now
    start at each `://` and `@`. Before, they read a long run of letters,
    digits or dashes again from each character, so 40 KB cost 9.7 s on
    each search that hit the note, and 100 KB of letters cost minutes. Now
    100 KB costs less than 0.1 s. The rules mask the same text as before.
  - The JWT rule and the cluster Secret data rule of the store redactor
    now read each run of characters once. Before, 256 KB of `eyJ` cost
    18.5 s, and `kind: Secret` with 64 K blank lines after it cost 34 s.
    Now the two rules take about 5 ms together on 128 KB of such text, and
    the time grows in line with the length. A differential run of
    500000 texts per rule gave the same output as the old rules.
- The data lines of a cluster Secret are now masked also when the
  manifest has CRLF line ends, or when its kind is in quotes
  (`kind: "Secret"`). Before, the store kept both. The redactor version is
  now 3, so the first index scan after the update writes the notes again.
  A differential run of 300000 texts per rule found no other change.
- The store redactor now reads the `kind: Secret` line as ASCII only, as
  the indexer reads it from the bytes of a large file. Before, the text
  rule also took a no-break space, an em space or U+001C after `kind:`,
  the long s in `secret` and the Kelvin sign in `kind`, and the byte test
  did not, so the stored head of a large file kept data lines that the
  redaction of the whole file masked. So a small file with such a kind
  line now keeps its data lines, and `kind: Secreté` now masks them. The
  redactor version 3 above covers this change.
- A common word no longer counts as a keyword match (NOBLIVION-76). Before,
  a word such as "the" matched nearly every note, so while the store was
  still computing vectors, the query "how do I deploy the service" brought
  back unrelated notes with no vector and no score, and the hook floors
  keep a row with no score. The store now skips a list of stop words
  (articles, pronouns, auxiliary verbs, prepositions, conjunctions and
  question words) when it decides which notes match. The scores do not
  change. In keyword-only mode, a query of stop words alone finds no note.
- The local re-rank of the ranked index (`NOBLIVION_RECALL_INDEX_RERANK`)
  no longer ranks the rows whose note holds no word of the query in its
  keyword list (NOBLIVION-76). Before, it ranked them by their id, so a row
  with a low id got a better place than a row with a better vector score.

### Changed

- Behaviour change for the subagent rules hook: its default relevance floor
  is now 0.68, not 0.52 (NOBLIVION-76). 0.52 was the reference index floor,
  measured with another embedding model. `tools/eval_thresholds.py` now
  measures the subagent path too. On the eval set the old floor let rows
  through for 16 of the 20 prompts that fit no note (precision 0.112,
  recall 1.000). At 0.68, 1 of these 20 prompts gets rows (precision 0.857,
  recall 0.750). The eval queries are prompts; the longer text of a real
  subagent task was not measured.
- Behaviour change for a store with another embedding model than
  `BAAI/bge-small-en-v1.5`: the default floors now apply only to the model
  they were measured for, as the ranked index floor already did
  (NOBLIVION-76). This holds for the note text floor `recall.min_score`
  (0.68), the subagent rules floor (0.68) and the error recall store floor
  `NOBLIVION_ERROR_RECALL_MIN_SCORE` (0.70). With another model, or with a
  store of an older version that names no model, these floors have no
  default. Before, they applied to every model, and the scores of another
  model are on another scale, so recall could show no note at all. A floor
  that you set applies to every model. The search answer of the store has
  a new field `model`, as the index answer has. It is `null` when the rows
  were ranked by keyword only.

## 0.1.6 - 2026-10-05

Changes three defaults and the redactor version. The transcript miner is
off. The ranked index has a relevance floor of 0.68 for the default
embedding model. The floor of error recall in `local` mode is 0.39. The
redactor version is 2, so the first index scan after the update writes
the Markdown notes again with the new secret rules. Also fixes the
citation trust signal, the secret redactor, the credential guard, the
guard table, keyword matches in recall, the embeddings consent of a
running store, and the transcript miner. After the plugin update, type
`/noblivion:setup` in the next session: the store venv must be rebuilt for
0.1.6.

### Changed

- Behaviour change: the transcript miner is off by default (NOBLIVION-53).
  Before, it ran at the end of every session. No precision figure is
  measured for its notes yet, and it stored false notes (see Fixed). To
  turn it on, set `miner.enabled` to `true` in `config.json` or set
  `NOBLIVION_MINER=1`. A config file or an environment that already turns
  the miner on keeps it on. While the miner is off, `noblivion mine` reads
  no transcript and says how to turn it on. Notes mined before stay in the
  database. The update does not delete or change them.
- Behaviour change for the ranked index, which the shipped config file
  turns on: the index now has a relevance floor by default (NOBLIVION-48).
  A row whose vector score is below 0.68 is not shown, so the index can be
  short, and a prompt that no note fits gets no index. Before, the index
  had no floor unless `NOBLIVION_RECALL_INDEX_MIN_SCORE` was set: on the
  eval set every prompt got 30 rows, also the 20 prompts that fit no note
  (precision 0.023, recall 1.000). With the floor 1 of these 20 prompts
  gets rows (precision 0.857, recall 0.750). The floor is a default of the
  hook, so a config file of an older release gets it too. The number 0.68
  was measured for the embedding model `BAAI/bge-small-en-v1.5`, so it is
  the default only for that model. The index answer of the store has a new
  field `model`, and the hook reads it. The field is `null` in every answer
  whose `mode` is `keyword`. With another embedding model the
  default is no floor, because the scores of another model are on another
  scale and 0.68 could hide every row. The recall log then has the note
  `floor_off:model`. A store of an older version names no model: the hook
  then applies no default floor (`floor_off:no_model`) until the store runs
  the new version. A floor that you set applies to every model. Set the
  variable to `off`, `none`, `false` or `no` for the index with no floor. A
  value that is not a number from -1 to 1 now gives the default, not "no
  floor".
- Behaviour change for error recall in its default `local` mode: the floor
  `NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE` is now 0.39, not 0.75
  (NOBLIVION-48). The old value was not measured. On the eval set it let 1
  of 14 expected notes pass (recall 0.071). At 0.39, 9 of 14 pass (recall
  0.643) and no wrong note passes (precision 1.000). The quote check still
  runs after this floor.
- The redactor version is 2, so the first index scan after the update
  writes every Markdown note whose file it can read again with the new
  rules (NOBLIVION-46). The indexer now keeps the version mark per
  namespace. Before, the mark was one value for the database, so with two
  namespaces the second kept its old rows. One file that the scan cannot
  read no longer makes every later scan write all rows again: the scan sets
  the mark, and it writes the row of that file again when the file can be
  read. The row of a note whose file is deleted is not written again: it
  keeps its old text until the store removes the row at the end of the
  delete grace time (14 days by default). Notes that the transcript miner
  stored before the update are not written again. A store process that
  started before the update runs the old code until it stops. A note that
  it indexes in that time gets the old rules. The next scan of the new
  code writes that row again: a scan keeps the revision of its last write
  for each namespace and redoes a row with a later revision.
- `tools/eval_thresholds.py` now also measures the two paths of a default
  install: the ranked index with the shipped config file, and error recall
  in `local` mode. CI gates both. The docs name the path that each
  published number describes. The eval set is small and has no held-out
  split, so the numbers are too good as a forecast for real notes
  (NOBLIVION-48).

### Fixed

- The citation trust signal (`NOBLIVION_TRUST_CITATION_USE`) no longer
  counts a reply that saves or edits the note it names, or a reply that
  lists the notes: a turn whose prompt asks to list, show or print the
  rules or memories, or a reply that names 5 or more shown notes at once.
  On a fresh real-session sample its precision rose to 15 of 29 (Wilson
  95% lower bound 0.344), still below the 0.70 rule, so the signal stays
  off by default. docs/trust.md has the numbers (NOBLIVION-43).
- The secret redactor of the store now removes these forms too
  (NOBLIVION-46): a private key block (`-----BEGIN ... PRIVATE KEY-----`,
  also the PGP form `PRIVATE KEY BLOCK`), a secret whose key is in quotes
  (JSON: `"password": "value"`), a value in quotes with a space in it
  (`password="two words"`), the value of a key that ends in `pass` or
  `pwd` in front of `=` and in the JSON form (`DB_PASS=value`,
  `"db_pass": "value"`), the text of an XML `<password>` element, and the
  token prefixes `glpat-`, `AIza`, `sk_live_`, `rk_live_`, `hf_` and
  `npm_`. Before, the indexer, the transcript miner and the MCP tool
  `noblivion_remember` stored these as written. A key block with no END
  line is removed up to the end of its key text. The redactor of the hooks
  removes every form in this entry too. The two redactors are still two
  sets of rules: one list of secret forms (`tests/secret_forms.py`) is
  tested against both, and a form that is not on the list can still pass
  one of them.
- The new rules keep the text of a note that is not a secret
  (NOBLIVION-46). A line that only names the BEGIN line of a key block
  stays, and so does the text after a key block with no END line. A key
  that ends in `pass` with a colon counts only when its value looks like a
  secret: one word of 8 or more characters with a digit, a symbol inside
  it, or a capital letter after a small letter
  (`smtp-pass: hunter2hunter2`). So `first_pass: complete` stays. A plain
  `pass` or `Pass` key with a colon counts by the same rule, but only at
  the start of a line, also after an indent as in YAML
  (`  pass: hunter2hunter2`). So `pass: complete` stays, and a sentence
  such as "The first pass: read the file" stays. `PASS` in capitals is the
  word of a test report, so `PASS: test_name` stays. After a colon,
  `true`, `false` and `null` stay. A number stays for a key that ends in
  `token` (`"input_token": 12345678`) and is removed for a password key
  (`password: 12345678`). A list of 90 texts is tested against the store
  redactor and against the redactor that the hooks use for memory text.
- The credential guard now also blocks these reads of a git config that
  holds a credential URL: the file as standard input (`cat < .git/config`,
  `echo "$(< .git/config)"`), a glob on a folder name (`cat .g*/conf*`), a
  brace list (`cat .git/{config,HEAD}`), a variable or `$( )` as the path
  (`f=.git/config; cat $f`), `git var -l`, and a `GIT_TRACE*` variable on a
  git command that talks to the remote. A command that builds the name
  from parts is blocked too: `cat .gi{t,}/conf{ig,}`,
  `cat .g'i't/con'f'ig`, `d=.gi; cat ${d}t/config`. A path that the guard
  cannot resolve is blocked only when the git config holds a credential
  and the fixed end of the path is the name of that file
  (`cat "$d/config"`, `cat $(git rev-parse --git-dir)/config`). A read
  through a loop variable, a `read` variable or `$(git ls-files)` passes,
  as before: `for f in $(git diff --name-only); do head -5 "$f"; done`. A
  `find` that starts in the folder of the clone and gives its files to a
  reading command, with `| xargs` or with `-exec`, is denied in a clone
  whose config holds a credential, also when it names no config file:
  `find . -name "*.py" | xargs grep -c TODO`,
  `find . -name '*.py' -exec head -3 {} \;`. That is an older limit, not a
  change of this release. The README
  now says what the guard covers: git URLs, not every credential.
  docs/guards.md lists the limits (NOBLIVION-47).
- A Bash command with one very long word no longer passes the credential
  guard (NOBLIVION-47). The guard hook allows a call that it cannot decide
  in 1.5 seconds, and three steps needed more time than that for one long
  word. The search for a `.git/config` path inside a word read the word
  again from each of its characters: one word of 100 KB took about 10
  seconds. The hook made the text of its log line before it printed the
  deny: that text took 2 seconds for a word of 30 KB. The lexer added one
  character at a time to a word: one word of 500 KB took about 1 second.
  Now the path search reads a word once. The hook prints the deny first
  and makes the log text after that, from the first 4 KB of the command.
  A long run of plain characters is taken out of the text before the lexer
  reads it. `cat <word> .git/config` with a word of 500 KB is denied in 0.2
  seconds. Before, it passed from 30 KB. The paths that the search finds
  are the same.
- A deny of a rule from the guard table is no longer lost for a long
  command (NOBLIVION-47). A refused override (`# guard-ok:`) with a reason
  of 30 KB passed, because the hook read the reason for its log before it
  printed the deny. A command with one word of 500 KB that broke a rule
  passed, because the credential guard reads each command first and its
  lexer needed over 1 second for that word. A deny of a shorter long
  command was printed, but its log line was lost. Now the reason and the
  command are read for the log after the print, and a denied call does not
  search the rows before it. The log text of a command or a reason over
  4 KB is made from its first 4 KB, with a note of the full length. The
  time limit stays: docs/guards.md names the commands that can still run
  out of it.
- Hybrid ranking no longer gives a keyword rank to a note that matches no
  word of the query (NOBLIVION-49). Before, the keyword list held the whole
  pool, and the notes with a keyword score of 0 were ordered by id. So the
  oldest notes got a fused score for a query they do not match. A note with
  no vector yet and no matching word was returned with no score, and the
  hook floors keep a row with no score: during a first backfill or a model
  change, old notes that had nothing to do with the prompt reached the
  context. Now the keyword list holds only the notes that match, as in
  keyword-only mode, and a note with no vector and no match is not returned.
  A note matches when it holds any word of the query, also a common word
  such as "the": the store has no stop-word list. So a note with no vector
  yet still comes back, with no score, when it shares only such a word with
  the prompt. During a backfill fewer unrelated notes reach the context
  than before, not none.
- Recall no longer misses a note whose only matching word is in exactly
  half of the notes of its pool (NOBLIVION-49). Such a word has a keyword
  weight of 0, so the note had a keyword score of 0, and the store read a
  score of 0 as "matches no word". With 4 notes and `deploy` in 2 of them,
  the prompt `how do I deploy` found nothing in keyword-only mode. In
  hybrid mode, with the entry above, a matching note with no vector yet
  was not returned. The store now compares the words: a note matches when
  it holds a word of the query. The keyword scores are unchanged. When
  every matching note has a keyword score below 0, the keyword order is not
  used in hybrid mode, as before: with two notes that both hold a word, the
  note with more uses of it has the lower score. The vector order then
  decides, and a matching note with no vector yet is still returned.
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
  checked on each call. A build measures the speed of each `violates`
  expression once and keeps the result in the file `<table>.probes`, so a
  rebuild measures only a new or changed expression: in a test with 650
  rules, a rebuild after one change took about 0.2 seconds, and the first
  build about 7 seconds. A rebuild in the guard hook has a limit of 6 seconds.
  When it is not done in that time, the rules of the last build stay in
  force, and one line tells the user so for that change of the folder.
- A deny check that runs out of time is no longer silent (NOBLIVION-50).
  The call is still allowed after 1.5 seconds, but one line now says that
  the deny rules were not checked for this call. Before, only the guard
  log recorded the timeout.
- `noblivion consent embeddings --revoke` now takes effect on a running
  store at once. Before, the store read the consent only when it started, so
  every prompt and every backfill text still went to the remote embedding
  service until the store exited. The store now reads the consent before
  each remote call: each prompt embedding, each backfill batch and each
  single-row retry. Without the consent it sends nothing, and recall uses
  keyword search only. The same rule applies to an Ollama server that is not
  on this machine. A local backend does not pay for the check. A prompt
  embedding that still waits when its 1 second limit ends is no longer sent
  later. A new consent takes effect within seconds, without a restart. The
  health answer has a new field `embedding.consent`: `not_needed`, `given`
  or `missing` (NOBLIVION-51).
- A store whose embedding backend is `failed` no longer starts a thread
  and writes the line `embedding state: failed` to `store.log` every 0.5
  seconds (NOBLIVION-51). A failed model load and a start without the
  consent did this before, and with the entry above a consent revoke did it
  too: about 170,000 log lines a day. Now the store starts the backend
  again only when a start can change the state: when the hourly retry time
  is over, or when the consent is back. A new consent still takes effect
  within seconds, without a restart.
- A consent read that fails no longer ends the background jobs of the
  store (NOBLIVION-51). With the consent check above, the store reads the
  embeddings consent of a remote backend on each tick of its jobs loop and
  before each remote call. When that read failed with a database error (for
  example `database is locked`), the jobs loop ended: no index scan, no
  backfill and no maintenance until a restart. A search at that moment
  returned an error and no rows. Now the store sends nothing for that
  call. The search answers by keyword, the jobs loop goes on and reads
  again on the next tick, and `store.log` gets one line for each run of
  failed reads. A read that fails is not a revoke: the backend keeps its
  state. A local backend reads no consent and was not affected.
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
