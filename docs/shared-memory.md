<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# One memory for Claude Code and Codex

NOBLIVION can serve Claude Code and Codex from one local store. Both clients
use the same Markdown notes, the same SQLite database and the same search
index. Each client has its own event adapter. The adapters send memory work
to the shared NOBLIVION code.

```text
Claude Code hooks ─┐
Claude Code MCP ────┼──► local NOBLIVION store ──► one SQLite database
Codex hooks ───────┤             ▲
Codex MCP ─────────┘             │
                         shared Markdown notes
```

The store listens on `127.0.0.1`. It uses a token from the data folder. It
does not accept connections from another computer. The database uses SQLite
with WAL so both clients can search it while the indexer writes new notes.

## What must match

Both clients must run as the same operating system user on the same computer.
They must point to the same absolute data folder. Set `NOBLIVION_DATA_DIR`
for both clients. That folder holds `noblivion.db`, the store token, the
configuration, the index and the trust events. A second folder means a
second database.

Both clients must use the same project namespace. Set `NOBLIVION_PROJECT`
to the same value, or set the same `namespace` in the shared `config.json`.
The default namespace is `claude_code`. The name is historical; Codex can
use it too. Different namespaces keep rows apart even within one database.

Both clients must use the same project memory folder. A practical choice is
the existing Claude Code folder for that project. Set `NOBLIVION_MEMORY_DIR`
to that folder for the two adapters. The store adds it to its index. If you
set `NOBLIVION_MEMORY_DIRS`, include every folder you want indexed. This
variable replaces the default folder list. Use `:` between paths on Linux
and macOS. The `memory_dirs` list in the shared `config.json` is another
way to set the index folders.

For automatic project recall, the clients must also select the same memory
root. The Codex installer uses the memory folder's parent name as its root.
This works when both clients use the same folder, even if Codex runs in a
Git worktree elsewhere. If Claude Code already uses an explicit
`NOBLIVION_RECALL_ROOT`, pass that root to the Codex installer with
`--root name`. A memory from a shared root can be used across projects when
you add that root to `recall.shared_roots` in `config.json`.

See [configuration.md](configuration.md) for these settings.

## How a note moves between clients

1. A user saves a lesson as a Markdown note in the shared memory folder.
2. The NOBLIVION indexer adds or updates one row in the SQLite database.
3. A new Claude Code or Codex prompt can find that row through recall.
4. Either client can fetch the full note by its memory ID.
5. Usage events from either client can update the note's trust history
   (its usage evidence) in the same database.

The Markdown file is the source of truth. The database is a local index and
history store. If a client writes outside the indexed folders, the other
client cannot find that note through NOBLIVION.

A note saved with `noblivion_remember` records `source_client: codex` or
`source_client: claude_code`. The source label says who saved it. It does
not create a separate database or project namespace.

The tool saves the note in `NOBLIVION_MEMORY_DIR` when it is set. Else it
saves the note in the project's memory folder, the folder the hooks read
(see "The session's memory folder" in
[configuration.md](configuration.md)). The folder must exist; the tool
does not make it. The Codex installer sets `NOBLIVION_MEMORY_DIR`.

Session files stay separate. They hold short lived context for one task,
such as which rules that task has seen. They are not shared as live chat
history between Claude Code and Codex.

## Expected advantages

For a Codex user, the shared store gives later tasks a way to find earlier
project lessons. A prompt can receive a short list of relevant rules. Codex
can fetch a full note only when it needs it, and it can save a new lesson
for later work. Guard hooks can check supported tool calls against project
rules before they run. These paths can help when a large project spans many
tasks.

When you use both clients, a lesson saved during Claude Code work can help
a later Codex task. A lesson saved during Codex work can help a later Claude
Code task. Both clients can use the same rule files and search results.
You can fix an outdated lesson in one Markdown file. The shared usage
history shows which notes are used across clients.

These are expected gains, not measured Codex results. NOBLIVION may add
text to a prompt and may take time to search or index a note. It does not
increase either product's compute or usage allowance. It does not merge
Claude Code and Codex conversations or replace their own instruction files.
The two clients have different tool events, so a guard can only inspect an
action that its client reports to the adapter. Codex registers the MCP tools
for the user, so they remain available in unrelated Codex workspaces. Use
them only for the intended project. See [Codex support](codex.md) for the
Codex coverage and limits.

## Computer boundary

One shared database works only when both clients use the same local store.
NOBLIVION does not sync its data folder or Markdown notes between computers.
For example, Codex on a Mac and Claude Code on a Linux host have separate
stores unless you deliberately run their memory commands on one host. Do not
copy a live SQLite database file while the store writes it. If you move to
another host, stop the store and use a safe backup or sync the Markdown files
and rebuild the index there.

The optional remote embedding service and duplicate judge can send note text
off the computer after you turn them on and give consent. The default local
store does not need either service. See [privacy](dedup-and-privacy.md).
