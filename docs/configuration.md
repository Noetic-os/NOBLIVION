<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Configuration

This page lists every setting. The tables come from the code. A test,
`tests/test_docs_configuration.py`, fails when the code reads a
`NOBLIVION_*` variable that this page does not name.

## Where settings come from

NOBLIVION reads each setting from the first source that has it:

1. An environment variable `NOBLIVION_*`.
2. The config file: `NOBLIVION_CONFIG`, else `<data dir>/config.json`.
3. The built-in default.

Not every setting has both an environment variable and a config key. The
tables below show which ones exist.

The config file is JSON. You can nest keys or write them with dots. These
two files mean the same:

```json
{"index": {"interval_s": 60}}
```

```json
{"index.interval_s": 60}
```

An unknown key is not an error. A bad value falls back to the default.

To set an environment variable for the hooks, put it in the `env` object
of your Claude Code `settings.json`, or export it before you start Claude
Code. The store reads the same variables when the hooks start it.

### Switches

Every on/off switch reads its value the same way. The Default column says
"on (switch)" or "off (switch)".

- `1`, `true`, `yes` or `on` turns the switch on.
- `0`, `false`, `no`, `off` or an empty value turns the switch off.
- Case and spaces around the value do not matter.
- Any other value keeps the default. A switch that is not set keeps the
  default too.
- In the config file, JSON `true` and `false` also work.

## The data dir

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_DATA_DIR` | none | `CLAUDE_PLUGIN_DATA`, else `$XDG_DATA_HOME/noblivion`, else `~/.local/share/noblivion` | The folder for the database, the token, the config, the venv, the logs and the caches. |
| `NOBLIVION_CONFIG` | none | `<data dir>/config.json` | The path of the config file. |

`CLAUDE_PLUGIN_DATA` is set by Claude Code for an installed plugin. When a
`noblivion` command runs from the store venv, it also finds the data dir
from the install stamp in that venv.

### The session's memory folder

Every hook uses the folder Claude Code keeps auto memory in for the session:
recall, error recall, subagent rules, continuity, the guard table and the
guard hook, the memory fields hook, the memory sync hook and the stop checks.
Each hook finds it from the session's project dir, the way Claude Code does.
The project dir is `CLAUDE_PROJECT_DIR` when it is an existing absolute
folder, else the `cwd` of the hook event. Claude Code sets
`CLAUDE_PROJECT_DIR` for every hook to the folder the session started in. It
does not change when a Bash `cd` moves the session into another repository,
and Claude Code keeps its auto memory on the start folder too. So the hooks
keep reading the folder of the project you started in, not the folder of the
repository the session is in now. In a linked worktree `CLAUDE_PROJECT_DIR` is
the worktree, and step 3 gives its main checkout. `--add-dir` does not change
it. Codex does not set it, so there the event `cwd` is used.

1. `NOBLIVION_MEMORY_DIR` (or `NOBLIVION_RECALL_MEMORY_DIR`), when set.

1. `NOBLIVION_MEMORY_DIR` (or `NOBLIVION_RECALL_MEMORY_DIR`), when set.
2. `autoMemoryDirectory` from the first Claude Code settings file that sets
   it: the managed settings, the project's `.claude/settings.local.json` and
   `.claude/settings.json` in the project dir, then the user's
   `settings.json`. The value must be
   absolute or start with `~/`.
3. `<config dir>/projects/<project>/memory`. The config dir is
   `CLAUDE_CONFIG_DIR`, else `~/.claude`. The project is the main checkout of
   the git repository that holds the project dir, so a subfolder and
   every linked worktree share one folder. Outside git it is the project
   dir. Its name has every character that is not a letter or a digit
   replaced by `-`. `CLAUDE_CODE_PROJECT_DIR_NAME` replaces the name when
   `CLAUDE_CONFIG_DIR` is set.

When that folder does not exist, the recall hook writes one line to
`<data dir>/cache/memory-dir.log` for the call, and recall reads no memory
files. There is no home-folder default: a session in a project uses the
folder of that project, never the folder of your home folder.

The stop checks name this folder when they ask Claude to save a lesson, so a
lesson goes where recall finds it.

### The global memory folder

You can keep rules that hold in every project in one more folder. Set
`NOBLIVION_GLOBAL_MEMORY_DIR`, or the config key `global_memory_dir`, to an
absolute path (or a path that starts with `~/`). There is no default: when it
is not set, the hooks read only the project folder.

When it is set:

- The store indexes the folder and searches it in every session. Its root is
  the name of its parent folder, and the store adds that root to
  `recall.shared_roots`.
- Recall, the guard table and the stop checks read the project folder and the
  global folder.
- A file of the project folder wins over a file with the same name in the
  global folder (and in any other shared root).
- A lesson saved in the global folder satisfies the stop checks too. The stop
  checks ask for the project folder.

```json
{
  "global_memory_dir": "~/claude-rules/memory"
}
```

The guard table is rebuilt at the start of each session, after a write of
a memory file, and before a guarded call when a memory file changed in
another way. A change to the global folder reaches the guard table of
another project at that project's next Bash, Edit or Write call.

### Shared Claude Code and Codex settings

Use the same absolute `NOBLIVION_DATA_DIR` for both clients. Use the same
`NOBLIVION_PROJECT` value, or one `namespace` value in their shared
`config.json`. The default namespace is `claude_code`; Codex can use it.
Use one Markdown folder for notes both clients should find. Set
`NOBLIVION_MEMORY_DIR` for each adapter, or list the folder in
`NOBLIVION_MEMORY_DIRS` or `memory_dirs` for the shared store.

```json
{
  "namespace": "my_project",
  "memory_dirs": ["/absolute/shared/memory"]
}
```

`NOBLIVION_MEMORY_DIRS` replaces the default folder list. Include existing
Claude Code folders if you still want to index them. The two clients also
need the same recall root for project scoped searches. See
[shared-memory.md](shared-memory.md) and [codex.md](codex.md).

The MCP save tool records which client wrote a note. This is provenance,
not a database namespace. It does not separate the two clients' searches.

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_SOURCE_CLIENT` | none | `claude_code` | Client name written by `noblivion_remember` into a new note. The Codex installer sets `codex`. Allowed values are `codex` and `claude_code`. |

## Store

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_PORT` | `port` | `0` | The TCP port on `127.0.0.1`. `0` lets the system pick a free port; the store writes the real port to `store.json`, and the hooks read it from there. A fixed port fails when another program uses it. The store never moves to another port by itself. |
| `NOBLIVION_IDLE_EXIT_S` | `idle_exit_s` | `1800` | The store stops after this many seconds with no request. `0` means never. |
| `NOBLIVION_INDEX_INTERVAL_S` | `index.interval_s` | `30` | Seconds between two scans of the memory folders. `0` turns the periodic scan off. |
| none | `index.delete_grace_days` | `14` | Days a deleted note stays in the database before the store removes it. |
| `NOBLIVION_LOG_LEVEL` | `log_level` | `info` | `debug`, `info`, `warning` or `error`. Only `debug` writes query text to the log. |
| `NOBLIVION_PROJECT` | `namespace` | `claude_code` | The namespace of the notes. Lower-case letters, digits and `_`. |
| `NOBLIVION_MEMORY_DIRS` | `memory_dirs` | every `<config dir>/projects/*/memory` folder, plus the `autoMemoryDirectory` of the user and managed settings | The folders the store indexes. The env var separates folders with `:`. The config key is a JSON list. The store also indexes `NOBLIVION_MEMORY_DIR` (see Guards) and `NOBLIVION_GLOBAL_MEMORY_DIR` when they are set, so the hooks and the store always use the same folders. |
| `NOBLIVION_GLOBAL_MEMORY_DIR` | `global_memory_dir` | none | The user-wide memory folder (see "The global memory folder"). Every session reads it after its project folder. |
| `NOBLIVION_BIN` | `store.bin` | `<data dir>/venv/bin/noblivion` | The `noblivion` command the hooks run to start the store. |
| `NOBLIVION_STORE_AUTOSTART` | none | on (switch) | When off, no hook starts the store. |

## Embeddings

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_EMBED_BACKEND` | `embedding.backend` | `fastembed` | `fastembed` (local), `ollama`, `openrouter` (hosted) or `none` (keyword search only). |
| `NOBLIVION_EMBED_MODEL` | `embedding.model` | `BAAI/bge-small-en-v1.5` | The embedding model. A change re-embeds all notes. |
| `NOBLIVION_OLLAMA_URL` | `embedding.ollama_url` | `http://127.0.0.1:11434` | The Ollama server for the `ollama` backend. |
| none | `embedding.allow_download` | `false` | When `true`, the store may download the model itself. `install.sh` downloads the model, so you do not need this. |
| none | `embedding.allow_remote` | `false` | Must be `true` before the `ollama` backend may use a server that is not on this machine. |
| none | `embedding.remote_include_mined` | `false` | When `true`, a hosted backend also gets the transcript-mined notes. |

A hosted backend sends text off the machine. It needs your consent first:
run `noblivion consent embeddings`. The `openrouter` backend also needs
`OPENROUTER_API_KEY` in the environment. See
[dedup-and-privacy.md](dedup-and-privacy.md).

## Recall hook (each prompt)

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_RECALL_DISABLE` | none | off (switch) | Turns the recall hook off. It also turns off the label rows at prompt time and the subagent rules hook. |
| `NOBLIVION_RECALL_TIMEOUT_S` | `recall.timeout_s` | `2.0` | The time budget of one recall, in seconds. |
| `NOBLIVION_RECALL_MIN_SCORE` | `recall.min_score` | `0.68` | The lowest score a hit needs. The store sends a cosine score with each hit. A hit with no score (keyword-only mode) always passes. The default was measured for the embedding model `BAAI/bge-small-en-v1.5`, and the hook uses it with every model. With another model, set a value that fits its scores. |
| `NOBLIVION_RECALL_PROJECT` | `namespace` | `claude_code` | The namespace to search. `NOBLIVION_PROJECT` is read when this is not set. |
| `NOBLIVION_RECALL_MEMORY_DIR` | none | the session's memory folder (see "The session's memory folder") | The memory folder of the session. It sets the root (see below) and the folder for local re-ranking and rule rows. `NOBLIVION_MEMORY_DIR` is read when this is not set. |
| `NOBLIVION_RECALL_ROOT` | none | the name of the project folder that holds the memory folder | The root. Recall searches only the notes of this root, plus the shared roots. |
| none | `recall.shared_roots` | `[]` | Roots whose notes every session can find. |
| `NOBLIVION_RECALL_CACHE_DIR` | none | `<data dir>/cache` | Hook state: session files, logs and the trust event spool. |
| `NOBLIVION_RECALL_SESSION_KEEP_DAYS` | none | `7` | Days a session file stays in the cache. |
| `NOBLIVION_RECALL_SESSION_KEEP_MAX` | none | `200` | The most session files the cache keeps. |
| `NOBLIVION_RECALL_LABELS` | none | off (switch) | Adds label rows (notes that name a file, a tool, a ticket or a service in the prompt). The shipped config file turns it on under `recall.env`. |
| `NOBLIVION_RECALL_MD_ONLY` | none | off (switch) | Makes the MCP tool return memory files only. |

### Ranked index

With `NOBLIVION_RECALL_INDEX` set, the hook shows a ranked list of note
titles instead of note text. Claude Code then opens the notes it needs
with the MCP tool. The other settings in this table work only with the
ranked index. All switches are off by default. The floor
`NOBLIVION_RECALL_INDEX_MIN_SCORE` is on by default when the embedding
model is the default model, `BAAI/bge-small-en-v1.5`.

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_RECALL_INDEX` | none | off (switch) | Shows the ranked index. |
| `NOBLIVION_RECALL_INDEX_K` | `recall.index_k` | `35` | How many candidates the hook asks for, 1 to 100. |
| `NOBLIVION_RECALL_INDEX_MAX_CHARS` | none | `7600` | The most characters the index adds to the prompt. |
| `NOBLIVION_RECALL_INDEX_QUERY_CHARS` | none | `300` | The most characters of the prompt sent as the query, up to 8000. |
| `NOBLIVION_RECALL_INDEX_MIN_SCORE` | none | `0.68` with the embedding model `BAAI/bge-small-en-v1.5`, else no floor | A cosine floor from -1 to 1. A row below it is not shown. `off`, `none`, `false` or `no` turns the floor off. A value that is not a number from -1 to 1 gives the default. The default `0.68` was measured for the embedding model `BAAI/bge-small-en-v1.5`. The store names its model in each index answer, and the hook uses the default only for that model. With another model, or with a store of an older version that names no model, the default is no floor. A value that you set applies to every model. Not used in keyword-only mode. |
| `NOBLIVION_RECALL_INDEX_HYGIENE` | none | off (switch) | Asks for more candidates. Drops index files, topic files and notes with a closed status. |
| `NOBLIVION_RECALL_INDEX_RERANK` | none | off (switch) | Re-ranks the rows: a local keyword score over the memory files, fused with the store's vector order. |
| `NOBLIVION_RECALL_INDEX_RULE_ROWS` | none | off (switch) | Shows each row as its id, the `rule:` line of the file and its name. |
| `NOBLIVION_RECALL_INDEX_APPLY` | none | off (switch) | Adds the `apply:` text under the top rule rows. Needs `NOBLIVION_RECALL_INDEX_RULE_ROWS`. |
| `NOBLIVION_RECALL_SHOWN_SET` | none | off (switch) | With apply lines: a note that the session already opened, or already saw with its apply text, keeps its row but loses the apply text. |
| `NOBLIVION_RECALL_INDEX_ROW_DEDUPE` | none | off (switch) | Leaves out the rows that an earlier prompt of the same session already showed. |
| `NOBLIVION_RECALL_INDEX_CHECK_LINE` | none | off (switch) | Adds one more header line to the rule rows. |
| `NOBLIVION_RECALL_INDEX_DROP_NO_RULE` | none | off (switch) | Leaves out a row with no rule and no summary. |
| `NOBLIVION_RECALL_INDEX_TRUST` | none | off | Stays off by default until a time-split test shows a ranking gain. `1`, `on`, `true` or `yes` multiplies each score by the trust factor. `shadow` computes the factor and logs it, but keeps the order. See [trust.md](trust.md). |
| `NOBLIVION_RECALL_TRUST_FILE` | none | unset | A fixed trust snapshot file to use instead of the store's trust values. For tests. |
| `NOBLIVION_RECALL_TRUST_MODE` | none | unset | `b` or `c`: other ways to compute the trust factor, for evaluation. `a` has no effect now: guard rows are never a use. Unset uses the normal factor. |
| `NOBLIVION_RECALL_TRUST_MIN_SCORE` | none | `0.58` | The score a row needs in trust mode `c`. |

### The shipped config file

`install.sh` copies `config/config.default.json` to
`<data dir>/config.json` when that file does not exist. The shipped file
sets `recall.index_k` to `30`. Under the key `recall.env` it also sets
these hook variables:

| Variable | Value |
| --- | --- |
| `NOBLIVION_RECALL_INDEX` | `1` |
| `NOBLIVION_RECALL_INDEX_APPLY` | `1` |
| `NOBLIVION_RECALL_INDEX_DROP_NO_RULE` | `1` |
| `NOBLIVION_RECALL_INDEX_HYGIENE` | `1` |
| `NOBLIVION_RECALL_INDEX_MAX_CHARS` | `9000` |
| `NOBLIVION_RECALL_INDEX_QUERY_CHARS` | `2000` |
| `NOBLIVION_RECALL_INDEX_RERANK` | `1` |
| `NOBLIVION_RECALL_INDEX_ROW_DEDUPE` | `1` |
| `NOBLIVION_RECALL_INDEX_RULE_ROWS` | `1` |
| `NOBLIVION_RECALL_LABELS` | `1` |
| `NOBLIVION_RECALL_SHOWN_SET` | `1` |

The `recall.env` key accepts only `NOBLIVION_RECALL_*` names. A variable
that is set in the environment wins over `recall.env`.

The shipped file does not set `NOBLIVION_RECALL_INDEX_MIN_SCORE`. The
default floor `0.68` applies when the embedding model is
`BAAI/bge-small-en-v1.5`, also to a config file that an older release
installed. With another embedding model the index has no floor. To set a
floor, add the variable under `recall.env`.

## Error recall hook

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_ERROR_RECALL_MODE` | none | `local` | `local` searches the memory files with a keyword score and needs no store. `store` asks the store. `fused` uses both. |
| `NOBLIVION_ERROR_RECALL_LOCAL_MIN_SCORE` | none | `0.39` | The lowest score of a local hit, from 0 to 1: the share of the words of the error that the note holds, where a rare word counts more. |
| `NOBLIVION_ERROR_RECALL_MIN_SCORE` | none | `0.70` | The lowest score of a store hit: a cosine score. The default was measured for the embedding model `BAAI/bge-small-en-v1.5`, and the hook uses it with every model. With another model, set a value that fits its scores. |
| `NOBLIVION_ERROR_RECALL_TIMEOUT_S` | none | `0.8` | The time budget of the store call, in seconds. |
| `NOBLIVION_ERROR_RECALL_SESSION_CHARS` | none | `6000` | The most characters the hook adds per agent in one session. |
| `NOBLIVION_ERROR_RECALL_MEMORY_DIR` | none | `NOBLIVION_RECALL_MEMORY_DIR`, else `NOBLIVION_MEMORY_DIR`, else the session's memory folder | The memory folder to search. |
| `NOBLIVION_ERROR_RECALL_LOG` | none | `<data dir>/error-recall-log.jsonl` | The log file. |
| `NOBLIVION_ERROR_RECALL_STATE_DIR` | none | `NOBLIVION_GUARD_STATE_DIR`, else `<data dir>/guard-state` | The state folder. |

## Subagent rules hook

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_SUBAGENT_RULES_OFF` | none | off (switch) | Turns the hook off. |
| `NOBLIVION_SUBAGENT_RULES_K` | none | `8` | The most rows for one subagent, up to 30. |
| `NOBLIVION_SUBAGENT_RULES_MAX_CHARS` | none | `2500` | The most characters for one subagent. |
| `NOBLIVION_SUBAGENT_RULES_QUERY_CHARS` | none | `2000` | The most characters of the task text sent as the query. |
| `NOBLIVION_SUBAGENT_RULES_MODE` | none | `context` | `context` adds the rows to the subagent's context. `rewrite` changes the task prompt instead. |

The subagent rules hook also reads the recall hook settings. When a ranked
index setting is not set, this hook uses its own defaults: rule rows,
apply lines, hygiene, re-ranking, row dedupe, the shown set and a cosine
floor of `0.52`.

## Continuity hook

The continuity hook keeps the shown rules across a context compaction and
prints a short briefing at session start. It is off by default.

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_CONTINUITY` | none | off (switch) | Turns the hook on. |
| `NOBLIVION_CONTINUITY_BRIEFING` | none | on (switch) | The briefing at session start. |
| `NOBLIVION_CONTINUITY_PRECOMPACT_PRINT` | none | on (switch) | Before a compaction, print the saved rules as well as save them. |
| `NOBLIVION_CONTINUITY_BUDGET_S` | none | `4.0` | The time budget, in seconds. |
| `NOBLIVION_CONTINUITY_MAX_CHARS` | none | `1500` | The most characters of the briefing, 500 to 2000. |
| `NOBLIVION_CONTINUITY_QUOTA_DECISION` | none | `8` | The most rows in the decisions section. |
| `NOBLIVION_CONTINUITY_QUOTA_OPEN` | none | `5` | The most rows in the open items section. |
| `NOBLIVION_CONTINUITY_QUOTA_TRAP` | none | `2` | The most rows in the traps section. |
| `NOBLIVION_CONTINUITY_REQUIRE_STATUS` | none | off (switch) | A note needs a `status:` field to count as an open item. |
| `NOBLIVION_CONTINUITY_CURATED` | none | on (switch) | The notes that the curated index files link come first in their sections. Off uses the ranked order only. |
| none | `continuity.decision_files` | `["topic_decisions.md"]` | Index files in the memory folder. The notes they link are the first rows of the decisions section, in file order. |
| none | `continuity.open_files` | `["topic_open_work.md"]` | Index files in the memory folder. The notes they link are the first rows of the open items section, in file order. |

## Guards

See [guards.md](guards.md) for what each guard does.

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_MEMORY_DIR` | none | the session's memory folder (see "The session's memory folder") | One fixed memory folder for every hook and every session. It wins over the folder of the session's `cwd`. When it is set, the store indexes this folder too, in addition to `NOBLIVION_MEMORY_DIRS`. |
| `NOBLIVION_GUARD_TABLE` | none | `<data dir>/guard-tables/<slug of the memory folder>.json` | The guard table file. By default each project has its own table. When this is set, every session uses this one file, and the guard hook does not rebuild it when a memory file changes outside a session start or a memory write. |
| `NOBLIVION_GUARD_LOG` | none | `<data dir>/guard-log.jsonl` | The guard log. |
| `NOBLIVION_GUARD_STATE_DIR` | none | `<data dir>/guard-state` | State per agent and session. |
| `NOBLIVION_GUARD_ROWS_SESSION_CHARS` | none | `6000` | The most characters of guard rows per agent in one session. |
| `NOBLIVION_GUARD_ROWS_FULL_SESSION_CHARS` | none | `12000` | After this many characters, a row shows only its rule and apply text. |
| `NOBLIVION_GUARD_WEAK_SHARE` | none | `4` | A trigger that this many notes share is weak. `0` turns the weak-trigger check off. |
| `NOBLIVION_GUARD_CREDENTIAL` | none | on (switch) | The credential guard. |
| `NOBLIVION_GUARD_LABELS` | none | on (switch) | Label rows at tool time. |
| `NOBLIVION_GUARD_LABEL_SESSION_CHARS` | none | `4000` | The most characters of label rows in one session. |
| `NOBLIVION_GUARD_LABELS_FILE_TOOLS` | none | off (switch) | Label rows for Read, Grep and Glob, and for the text of an edit. |
| none | `guard.generic_args_extra` | `[]` | More words that do not make a trigger specific. They add to the built-in list. |
| none | `guard.evidence_stop_extra` | `[]` | More words that do not count as evidence. They add to the built-in list. |
| none | `labels.ticket_prefixes` | `[]` | Ticket key prefixes, for example `PROJ`. With none, no ticket labels are made. |
| none | `labels.service_prefixes` | `[]` | Service name prefixes. With none, no service labels are made. |

## Memory sync hook

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_MEMORY_SYNC_CMD` | `sync.index_command` | `<data dir>/venv/bin/noblivion index` when it exists | The command that re-indexes the notes after a write. |
| `NOBLIVION_MEMORY_SYNC_OFF` | none | off (switch) | Turns the hook off. |
| `NOBLIVION_MEMORY_SYNC_BASH_OFF` | none | off (switch) | Turns off only the check after shell commands. |
| `NOBLIVION_MEMORY_SYNC_STATE_DIR` | none | `<data dir>/cache` | Stamps and the log of the hook. |
| `NOBLIVION_MEMORY_SYNC_DEBOUNCE_S` | none | `3.0` | Seconds the folder must be quiet before a run. |
| `NOBLIVION_MEMORY_SYNC_TIMEOUT_S` | none | `120.0` | The time limit of one index run. |
| `NOBLIVION_MEMORY_SYNC_RETRY_WAIT_S` | none | `5.0` | Seconds between two attempts. |
| `NOBLIVION_MEMORY_SYNC_LOCK_WAIT_S` | none | `400.0` | Seconds to wait for another sync worker. |

## Stop checks

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_STOP_CHECK_MODE` | `stop.mode` | `shadow` | `shadow` runs the checks, logs what would have blocked and never blocks. `enforce` lets the checks block. Any other value means `shadow`. |
| `NOBLIVION_STOP_CHECKS` | `stop.checks` | `commit,tests,notify,deploy,lesson` | The checks that block in `enforce` mode: a comma list, or `all`, or `none`. A set but empty value means none. The config key takes a list or a comma text. |
| `NOBLIVION_STOP_CHECK_LOG` | none | `<data dir>/stop-check-log.jsonl` | The decision log. |
| `NOBLIVION_STOP_CHECK_STATE` | none | `<data dir>/stop-check-state` | Marker files per session. |
| none | `stop.shared_checkouts` | `[]` | Checkouts where an edit is reported instead of asked to be committed. |
| none | `stop.worktree_prefixes` | `[]` | Path prefixes of your git worktrees. Each starts with `/` or `~`. |
| none | `stop.test_command` | `python3 -m pytest -q <absolute test path>` | The test command that the tests check names in its message. |
| none | `stop.deploy_hosts` | `[]` | Hosts of the deploy check. With none, the deploy check is off. |

The env var wins over the config key. To let only the commit and tests
checks block, set this in `<data dir>/config.json`:

```json
{"stop": {"mode": "enforce", "checks": ["commit", "tests"]}}
```

`noblivion stop report [--days N] [--json]` counts the log rows of the last
N days (default 7) per check: would block (shadow mode), blocked (enforce
mode) and logged only (the check is not in the blocking list).

## Trust

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_TRUST_EVENTS` | `trust.events` | on (switch) | Records which notes were shown, used and contradicted. This is the usage evidence for the retire, promote and demote report. Off stops the spool and the flush. |
| `NOBLIVION_TRUST_RANKING` | `trust.ranking` | `off` | Stays `off` by default until a time-split test (fit on older events, test on newer events) shows a ranking gain. Run `noblivion trust timesplit` to test it on your own store. `off`, `shadow` or `on`. With `shadow` or `on`, the store adds trust values to each index row. The store itself never reorders rows. With `shadow`, the prompt hook computes and logs the trust factor but never applies it. With `on`, the hook applies it when `NOBLIVION_RECALL_INDEX_TRUST` is on. |
| `NOBLIVION_TRUST_CITATION_USE` | `trust.citation_use` | off (switch) | A reply that cites a shown note (its name, or 8 words of its rule in a row) records a `use`. Off by default: its precision on the labelled set was not high at the first run. See [trust.md](trust.md). |
| `NOBLIVION_TRUST_CORRECTION_CONTRADICT` | `trust.correction_contradict` | off (switch) | A user correction of a reply that cited a note records a `contradict` for that note. Off by default: a correction does not say whether the note was wrong, and a wrong `contradict` lowers a good note. See [trust.md](trust.md). |
| none | `trust.prior_mined` | `0.3` | The starting trust of a transcript-mined note, more than 0 and at most 1. A memory file starts at `0.5`. |
| `NOBLIVION_TRUST_REPORT_FILE` | none | `<data dir>/cache/trust-report.json` | The cached trust report. |
| `NOBLIVION_DEDUP_STATUS_FILE` | none | `<data dir>/cache/dedup-last-run.json` | The duplicate sweep status. Every `noblivion dedup plan`, `apply` and `undo` writes it. The session start line reads it. |

## Duplicate sweep

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_DEDUP_JUDGE` | `dedup.judge` | `openrouter` | `openrouter`, or `off`. Any other value counts as `off`. |
| `NOBLIVION_DEDUP_MODEL` | `dedup.model` | none | The judge model on OpenRouter. With no model, `noblivion dedup plan` refuses. |
| none | `dedup.min_cosine` | `0.75` | The lowest vector similarity of a candidate pair. |
| none | `dedup.max_pairs` | `50` | The most judge calls in one `plan` run. |
| none | `dedup.timeout_s` | `60` | The time limit of one judge call, in seconds. |
| none | `dedup.min_interval_s` | `1.0` | The shortest time between two judge calls, in seconds. |
| none | `dedup.price_in_per_mtok` | none | Price per million input tokens, for the cost estimate. |
| none | `dedup.price_out_per_mtok` | none | Price per million output tokens, for the cost estimate. |
| none | `dedup.archive_retention_days` | `90` | Days an archived note stays in the database. The archived file is kept. |

`OPENROUTER_API_KEY` holds the API key. It is read only from the
environment, never from the config file.

## Transcript miner

| Env var | Config key | Default | Meaning |
| --- | --- | --- | --- |
| `NOBLIVION_MINER` | `miner.enabled` | off (switch) | The miner, at session end and from the command line. Off by default: its precision is not measured yet. See [transcript-miner.md](transcript-miner.md). |
| `NOBLIVION_MINE_CMD` | none | `<data dir>/venv/bin/noblivion mine` | The command the session end hook runs. |
| `NOBLIVION_MINER_STAMP_DIR` | none | `<data dir>/cache` | Folder for the miner start throttle stamp. The Codex adapter uses its own state folder so a Claude Code session does not suppress a Codex miner start. |
| none | `miner.transcript_glob` | `~/.claude/projects/*/*.jsonl` | The transcripts to read. |
| none | `miner.max_run_s` | `300` | The time budget of one run, in seconds. The next run goes on from where this one stopped. |
