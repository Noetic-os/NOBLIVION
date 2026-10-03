<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Transcript miner

Claude Code writes a transcript of each session to disk. The transcript
miner reads these transcripts and saves short notes about past mistakes in
the local database. Claude Code can then find these notes when it asks for
them.

The miner runs on your machine. It sends nothing off the machine.

## What it finds

The miner looks for three kinds of events. It makes at most one note per
transcript line.

| Kind | What it is | What it stores |
| --- | --- | --- |
| `correction` | You corrected Claude Code: a prompt with a correction word near its start. | up to 600 characters of your prompt, and up to 400 characters of the reply before it |
| `tool_error` | A tool call that failed. | up to 300 characters of the command and 400 characters of the error |
| `review_request_changes` | A review result with the verdict `REQUEST_CHANGES` and a findings block. | up to 1500 characters of the findings |

Turns that Claude Code adds in your name, such as tool results and system
messages, are not counted as your prompts.

## How a mined note differs

- Its source type is `transcript_mined`. A memory file has the source type
  `claude_code_md`.
- Its root is the project folder of the transcript.
- It starts with a lower trust score: `trust.prior_mined`, default `0.3`.
  A memory file starts at `0.5`.
- It has no file. It lives only in the database.

## Who sees mined notes

Transcripts can hold text from web pages, tool output and other sources
you did not write. So mined notes are kept out of the automatic paths.

- The prompt hook never adds a mined note.
- The subagent rules hook never adds a mined note.
- The search endpoint never returns a mined note.
- The MCP tool `noblivion_recall` returns mined notes only when Claude
  Code asks with `include_mined: true`. It can also open a mined note by
  its id.
- The trust report and the duplicate sweep skip mined notes.
- A hosted embedding backend gets mined notes only when
  `embedding.remote_include_mined` is `true`.

## Redaction

The miner removes secrets (keys, tokens, passwords) from each note. It
also removes text that looks like an instruction to the model. It does
this twice: when it extracts the note and again before it stores it. When
redaction fails, the miner skips the note.

The miner does not replace paths, user names or IP addresses, because the
note stays on your machine.

## When it runs

At the end of each session, the SessionEnd hook starts the miner in the
background. It runs at most once every 10 minutes. The hook runs
`NOBLIVION_MINE_CMD` when you set it, else
`<data dir>/venv/bin/noblivion mine`.

One run stops after `miner.max_run_s` seconds (default 300). It stops at
the end of a line and saves its place. The next run goes on from there.
The miner keeps the size and the read position of each transcript in the
database. So a run reads only new lines, and a note is never stored twice.

## Run it by hand

```sh
<data dir>/venv/bin/noblivion mine
```

| Option | Effect |
| --- | --- |
| `--since YYYY-MM-DD` | Skip transcripts that did not change since this date. |
| `--max-seconds S` | The time budget of this run. |
| `--json` | Print the result as JSON. |

Exit code 4 means another miner run holds the lock. Exit code 3 means a
database schema error.

## Input

The miner reads `~/.claude/projects/*/*.jsonl` by default. Set
`miner.transcript_glob` to read other files. The default pattern does not
read the transcripts of subagents, which are in sub-folders.

## Turn it off

Set `NOBLIVION_MINER=0`, or set `miner.enabled` to `false` in
`<data dir>/config.json`. Notes mined before stay in the database.
