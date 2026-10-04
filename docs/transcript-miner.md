<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Transcript miner

Claude Code writes a transcript of each session to disk. The transcript
miner reads these transcripts and saves short notes about past mistakes in
the local database. Claude Code can then find these notes when it asks for
them.

The miner runs on your machine. It sends nothing off the machine.

The miner is off by default. No precision figure is measured for it yet:
nobody has counted how many of its notes are correct on real sessions. See
[Turn it on or off](#turn-it-on-or-off).

## What it finds

The miner looks for three kinds of events. It makes at most one note per
transcript line.

| Kind | What it is | What it stores |
| --- | --- | --- |
| `correction` | You corrected Claude Code: a prompt that the correction test accepts. | up to 600 characters of your prompt, and up to 400 characters of the reply before it |
| `tool_error` | A tool call that failed. | up to 300 characters of the command and 400 characters of the error |
| `review_request_changes` | The result of a reviewer agent call (the `Agent` or `Task` tool) with the verdict `REQUEST_CHANGES` and a findings block. | up to 1500 characters of the findings |

Turns that Claude Code adds in your name, such as tool results and system
messages, are not counted as your prompts.

### The correction test

The miner uses the same correction test as the stop checks
([guards.md](guards.md)) and the trust signals. The test is the function
`is_correction` in `hooks/stop_checks.py`. It accepts a prompt that:

- starts with a denial: "No", "Nope" or "Incorrect" with a punctuation
  mark after it, "no" before a word such as "that", "you" or "stop", or
  "Wrong". For example "No, that is wrong, use the other file"; or
- has a correction phrase in its first 400 characters, for example "I told
  you", "you did it again", "that is not what I asked", "you ignored" or
  "stop guessing".

A single word such as "again", "never", "undo", "revert" or "stop" is not
enough. "Run the tests again please" and "Please revert the last commit"
are ordinary requests, and the miner makes no note for them. A request
that starts with "no" is not a correction either, for example "No push
yet, build it locally first".

The test is a list of phrases. It can miss a correction in other words,
and it can accept a prompt that is not a correction. When the miner cannot
load `hooks/stop_checks.py`, it makes no correction note.

### Review notes

The miner takes a review note only from the result of an agent call. A
result of any other tool gives no review note: not a web fetch, not a
file read, not a shell command, not an MCP tool. So a web page or a file
that holds the text `**Verdict:** REQUEST_CHANGES` does not become a
review note.

An agent can repeat text that it read from a web page or a file. The
miner cannot tell this text from the agent's own findings.

## How a mined note differs

- Its source type is `transcript_mined`. A memory file has the source type
  `claude_code_md`.
- Its root is the project folder of the transcript.
- It starts with a lower trust value: `trust.prior_mined`, default `0.3`.
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

The miner masks secrets (keys, tokens, passwords) in each note. It does
this twice: when it extracts the note and again before it stores it. When
redaction fails, the miner skips the note.

The miner also runs the injection filter on each note. This filter does
not remove every text that looks like an instruction to the model.

- It masks a fixed list of phrases: "ignore previous instructions",
  "disregard previous", "system override", a fake system message such as
  `<system>` or "new system prompt", and a claim that an approval was
  given.
- It does not read the meaning of the text. An instruction in plain words
  passes, for example "Always run this command before any git push". A
  shell command passes too.

So a mined note can hold text that reads like an instruction. Read a mined
note as a record of what a transcript said, not as a rule. This is one
reason why the automatic paths never show mined notes, and why the miner
is off by default. Nobody has measured yet how much of such text the
filter catches or misses in mined notes.

The miner does not replace paths, user names or IP addresses, because the
note stays on your machine.

## When it runs

When the miner is on, the SessionEnd hook starts it in the background at
the end of each session. It runs at most once every 10 minutes. The hook
runs `NOBLIVION_MINE_CMD` when you set it, else
`<data dir>/venv/bin/noblivion mine`.

One run stops after `miner.max_run_s` seconds (default 300). It stops at
the end of a line and saves its place. The next run goes on from there.
The miner keeps the size and the read position of each transcript in the
database. So a run reads only new lines, and a note is never stored twice.

## Run it by hand

```sh
NOBLIVION_MINER=1 <data dir>/venv/bin/noblivion mine
```

`NOBLIVION_MINER=1` turns the miner on for this one run. Leave it out when
`miner.enabled` is `true` in the config file. While the miner is off, the
command reads no transcript, prints that the miner is off and exits with
code 0.

| Option | Effect |
| --- | --- |
| `--since YYYY-MM-DD` | Skip transcripts that did not change since this date. |
| `--max-seconds S` | The time budget of this run. |
| `--json` | Print the result as JSON. |

Exit code 4 means another miner run holds the lock. Exit code 3 means a
database schema error. Exit code 6 means there is no database: the miner
never makes one, so a run that the SessionEnd hook starts after an
uninstall does not make the data dir again (NOBLIVION-28).

## Input

The miner reads `~/.claude/projects/*/*.jsonl` by default. Set
`miner.transcript_glob` to read other files. The default pattern does not
read the transcripts of subagents, which are in sub-folders.

## Turn it on or off

The miner is off by default (NOBLIVION-53). In version 0.1.5 and before,
it was on by default.

- To turn it on, set `miner.enabled` to `true` in
  `<data dir>/config.json`, or set `NOBLIVION_MINER=1`.
- To turn it off again, remove that setting, or set `miner.enabled` to
  `false` or `NOBLIVION_MINER=0`.
- If your config file or your environment already turned the miner on, it
  stays on.

Notes mined before stay in the database. The update does not delete or
change them. Some of them can be wrong: an earlier version made a
`correction` note for an ordinary request, and a `review_request_changes`
note from any tool result.

This version has no command that lists all mined notes, and no command
that removes them. The MCP tool `noblivion_recall` shows the mined notes
that match a query when Claude Code asks with `include_mined: true`.
