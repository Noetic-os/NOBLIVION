<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Use NOBLIVION with Codex

Codex can use the NOBLIVION store and the same memory files as Claude Code.
The Codex adapter is in this repository. Its MCP tools are
`noblivion_recall` for search and fetch, and `noblivion_remember` for an
explicitly saved note. Codex hooks add prompt recall, guards and task
continuity. The adapter translates Codex events into calls to the shared
NOBLIVION code. It does not create another database.

The store, database and Markdown files must be on the same host as both
clients. The clients must run as the same operating system user. Read
[shared memory](shared-memory.md) before you set up two clients.

## Requirements

- Linux or macOS.
- Codex CLI with MCP and hook support.
- Python 3.9 or newer for the hooks and MCP server.
- `uv` and Python 3.11 or newer for the store install. See
  [install.md](install.md).
- A NOBLIVION checkout and an installed store. The Codex installer points
  at this checkout and the existing data folder.

## Add Codex to a working Claude Code store

1. Find the absolute data folder used by the Claude Code plugin. It holds
   `noblivion.db`, `config.json`, `token` and `venv/`. Use that folder in the
   Codex command below. Do not select a new empty folder.
2. Choose an existing absolute memory folder for the project. It must hold
   the Markdown notes that both clients will use. Use the default Claude
   Code memory folder, or add this folder as an exact entry in `memory_dirs`
   in the shared `<data dir>/config.json`. The installer checks these two
   cases. A runtime only `NOBLIVION_MEMORY_DIR` or `NOBLIVION_MEMORY_DIRS`
   setting does not satisfy its install check.
3. From a NOBLIVION checkout at the same version as the installed store,
   run:

   ```sh
   python3 scripts/install_codex.py \
     --data-dir /absolute/shared/data \
     --memory-dir /absolute/shared/memory \
     --workspace /absolute/project/repo
   ```

4. Open Codex in the project folder. In Codex CLI, use `/hooks` to review
   and trust the installed NOBLIVION hooks. Codex checks each hook command
   before it runs it. If a later update changes a hook, review it again.
5. Start a new Codex task. Ask Codex to use `noblivion_recall` for a known
   note. Save a test lesson with `noblivion_remember`. Give it a title, rule,
   apply text, body and evidence. The tool needs `NOBLIVION_MEMORY_DIR` to
   name the existing absolute folder from step 2. It writes one Markdown
   note, then schedules indexing. If that short step fails, the next store
   scan indexes the note. The default scan interval is 30 seconds. Check
   that Claude Code can then find the note.

The installer adds hooks for the selected workspace and its Git worktrees.
Give `--workspace` again for each separate repository you want. A later
install replaces the installer's workspace list, so pass the full list
each time. Use the same absolute paths when you update or reinstall it.

The MCP server is registered at the user level. Its tools are available in
all Codex workspaces, including unrelated projects. They use the data
folder, memory folder and namespace from this install. Call these tools
only for the intended project. The workspace filter applies to the hooks.

The installer reads the store's `namespace` from its shared `config.json`
by default. If Claude Code sets `NOBLIVION_PROJECT` in its environment to
a different value, give that value to the Codex installer with
`--namespace name`. `NOBLIVION_RECALL_PROJECT` can also override Claude
Code's recall namespace. Both clients must search the same namespace. A
different namespace hides the other client's notes even when they share
the SQLite file.

The installer uses the memory folder's parent name as the Codex recall
root. It sets that root for Codex hooks and MCP. If Claude Code sets
`NOBLIVION_RECALL_ROOT` to another name, pass the same name with
`--root name` when you install Codex. A different root can hide project
notes even when the namespace and database match.

If you use Codex without Claude Code, create the memory folder and
install the NOBLIVION store first:

```sh
bash scripts/install.sh --data-dir /absolute/shared/data
```

Add `/absolute/shared/memory` to `memory_dirs` in the new
`/absolute/shared/data/config.json`. Then install the Codex adapter:

```sh
python3 scripts/install_codex.py \
  --data-dir /absolute/shared/data \
  --memory-dir /absolute/shared/memory \
  --workspace /absolute/project/repo
```

The installer checks that the folder is indexed. The first index scan can
take longer. For a custom namespace, set it in `config.json` before the
Codex install, or pass `--namespace name` to match the other client.

## What Codex can do

- Search the indexed notes and fetch one full note with
  `noblivion_recall`.
- Save a reviewed lesson to the indexed Markdown corpus with
  `noblivion_remember`. The note records `source_client: codex`, and both
  clients can retrieve it after indexing.
- Receive relevant rules at prompt time and before supported tool calls.
- Use the local guard checks for supported Codex tool events.
- Keep short task state through compaction and use lesson feedback in the
  shared store.

The adapter writes a private, redacted journal of reported prompts, tool
results and final replies under `~/.codex/noblivion/state/transcripts/`.
Stop checks read this journal. When the transcript miner is on (it is off
by default, see [transcript-miner.md](transcript-miner.md)), it scans the
journal at session end for candidate lessons and writes those candidates
to the shared store.
This journal is separate from the Codex chat transcript. See
[privacy](dedup-and-privacy.md) for how NOBLIVION handles stored text.

A Codex hook sees the tool name and input that Codex sends to it. It cannot
check an action that Codex does not report. Claude Code and Codex have
different tool events, so their guard coverage is not identical. Review
[guards.md](guards.md) for the rules, and test important project commands in
your own environment.

NOBLIVION does not copy chat history from one client to the other. Codex
still loads its own project instructions. Claude Code still loads
`CLAUDE.md` and `MEMORY.md` in its usual way. NOBLIVION's prompt recall can
add text and use tokens. No Codex token saving or accuracy gain is promised
by the Claude Code tests, and this integration does not increase a Codex
plan's compute allowance.

## Update and check the connection

Update the NOBLIVION checkout and the store as described in
[install.md](install.md). Run `scripts/install_codex.py` again with the same
paths. Review changed hooks in Codex CLI with `/hooks`. Start a new Codex
task to load the new MCP server and hooks.

For a store failure, run the NOBLIVION `doctor` command from the store venv
and read [troubleshooting.md](troubleshooting.md). Check the data folder
first: two data folders mean two databases. Check the namespace and memory
folder next. The store accepts only loopback connections on its host.
