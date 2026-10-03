<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# How recall works

This page tells you how a note in a memory folder gets into the context of
a Claude Code prompt.

## The parts

- **Memory files.** Markdown files in `~/.claude/projects/<project>/memory/`.
  You or Claude Code write them. They are the source of truth.
- **The store.** One local process. It reads the memory files into a
  SQLite database, keeps a search index in memory, and answers search
  requests over HTTP on `127.0.0.1`.
- **The hooks.** Small Python scripts that Claude Code runs on events. They
  ask the store for notes and print the notes into the context.
- **The MCP tool** `noblivion_recall`. Claude Code calls it to search or
  to open one note.

## The store

### Start and stop

1. At session start, a hook runs `noblivion ensure-running`.
2. When no store runs, this command starts one in the background.
3. The store takes a lock file, `store.lock`. A second store sees the lock
   and exits.
4. The store binds to `127.0.0.1` on port `8894`. It writes the port and
   its process id to `store.json` in the data dir.
5. When no request comes for 30 minutes, the store stops.

A hook that finds no store also asks for a start, at most once every 30
seconds. The hook does not wait for the start. It prints nothing for this
prompt. The next prompt finds the store.

The store never listens on another address than `127.0.0.1`. When it
cannot bind to the port, it exits. It does not move to another port by
itself.

### The handshake

The hooks and the store share a secret: the `token` file in the data dir,
mode 0600. Before a hook sends a prompt, it checks that the process on the
port is really the store:

1. The hook sends a random number (a nonce) to `/health`, with no token.
2. The store answers with a proof: an HMAC-SHA256 of the nonce, keyed with
   the token.
3. The hook computes the same value and compares the two.
4. Only when they match does the hook send the token and the prompt.

A foreign process on the port cannot make the proof. So it never gets the
token or a prompt. The hook remembers a good proof for 30 seconds.

### The indexer

The store scans the memory folders at start and then every 30 seconds.

- **Folders.** Every `~/.claude/projects/*/memory` folder, or the folders
  in `memory_dirs`.
- **Files.** Each non-empty `*.md` file at the top of a folder. Files in
  sub-folders and files whose names start with `.` are skipped.
- **Root.** The name of the project folder that holds the memory folder.
  Each note belongs to one root.
- **Fields.** The `name` and `description` fields of the front matter, and
  the body.
- **Category.** `MEMORY.md` is an index file. Other files get their
  category from the start of the file name: `feedback_`, `project_`,
  `reference_`, `topic_` or `user_`. Else the `type` field decides. Else
  the category is `reference`.
- **Changes.** The indexer compares a hash of each file. It writes only
  the files that changed. A renamed file keeps its id and its trust
  history.
- **Deletes.** A deleted file is marked as deleted. The store removes the
  row after 14 days.
- **Shrink guard.** When a scan would delete more than 10% of the notes,
  the indexer deletes none of them. It also deletes nothing when it cannot
  read or redact a file. Run `noblivion index --allow-shrink` when you
  really removed many files.
- **Redaction.** The indexer removes secrets (keys, tokens, passwords)
  from the text before it stores the text. A file that cannot be redacted
  is not stored.

After Claude Code writes a memory file, the memory sync hook runs
`noblivion index` at once. You do not have to wait for the next scan.

### Ranking

The store ranks the notes of the session's root and of the shared roots.

1. **Keyword score.** BM25, a standard keyword score, over the note text.
2. **Vector score.** The cosine similarity between the vector of the
   prompt and the vector of each note.
3. **Fusion.** Reciprocal rank fusion joins the two ranked lists into one.

Without an embedding model, the store uses the keyword score only. This
is keyword-only mode. Recall still works, but it finds fewer notes that
use other words than the prompt.

### Embedding backends

| Backend | Where it runs | Text leaves the machine |
| --- | --- | --- |
| `fastembed` (default) | in the store, on the CPU | no |
| `ollama` on `127.0.0.1` | a local Ollama server | no |
| `ollama` on another host | a remote Ollama server | yes, needs consent |
| `openrouter` | a hosted service | yes, needs consent |
| `none` | no vectors: keyword-only mode | no |

The default model is `BAAI/bge-small-en-v1.5`. When the model changes, the
store computes the vectors again in the background. A slow embedding of
the prompt (more than 1 second) gives a keyword-only answer for that
prompt.

## What the prompt hook adds

On each prompt, the recall hook sends the prompt (cut to 300 characters)
and the root of the session to the store. It has a total budget of 2
seconds. It always exits with code 0, so it never blocks a prompt.

The hook has two output shapes.

### Note text (default without a config file)

The hook asks for the 5 best hits. It prints their text under a header
that starts with `GROUNDED MEMORY`. The output is at most 4000
characters. A note shown earlier in the same session is not shown again.

### Ranked index (shipped default config)

With `NOBLIVION_RECALL_INDEX` set, the hook asks for up to 35 rows. It
prints one short line per note: its id, its rule or its title, and a
summary. The output is at most 7600 characters. Claude Code opens the
notes it needs with the MCP tool. The shipped config file turns this
shape on, with rule rows, apply lines and a local re-rank. See
[configuration.md](configuration.md#ranked-index).

### Safety of injected text

- The hook adds only notes from your memory files. It never adds a
  transcript-mined note.
- It frames the notes as memory notes, not as instructions.
- It removes control characters and replaces `<` and `>`.
- It cuts each title, summary and row to a fixed length.

A memory file is still text that the model reads. Review new memory files,
the same way you review code.

## The MCP tool

Claude Code can call `noblivion_recall` with these arguments:

| Argument | Meaning |
| --- | --- |
| `query` | What to find, free text, cut to 300 characters. |
| `k` | The most hits, 1 to 20. Default 5. |
| `fetch_id` | The id of one note to open in full. |
| `include_mined` | `true` lists the ranked index with the transcript-mined notes. Default `false`. |

Give `query` or `fetch_id`, not both. An opened note starts with
`GROUNDED MEMORY <id>:`. The trust flush reads this line to record that the
note was used.

## Other recall hooks

- **Error recall.** When a shell command fails, the hook searches the
  memory files for the error. By default it searches the files directly,
  with no store. It shows a note only when the note quotes three words in
  a row from the error message. It shows at most 2 notes.
- **Subagent rules.** When Claude Code starts a subagent, the hook sends
  the task text to the store and adds up to 8 matching rules to the
  subagent's context.
- **Continuity.** Off by default. Before a context compaction, it saves
  the rules that were shown. After the compaction, it prints them again.
  At session start, it prints a short briefing.

## When the store is down

- The prompt hook and the subagent hook print nothing. They exit with code
  0 and ask for a store start.
- The MCP tool returns an error: `memory store not running`.
- Error recall in its default mode does not need the store.
- The guards do not need the store. They read the memory files directly.

See [troubleshooting.md](troubleshooting.md).
