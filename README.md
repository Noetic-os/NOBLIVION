<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# NOBLIVION

NOBLIVION gives Claude Code and Codex one searchable memory on your own
machine. The Claude Code plugin and the Codex adapter use the same local
store. The store indexes Markdown notes in SQLite. Each client can find
project notes and rules. The store also tracks which notes help, and a hook
can stop a tool call that breaks a rule.

Status: early release (v0.1.x). [CHANGELOG.md](CHANGELOG.md) lists every
release.

## What it does

- **Recall.** On each prompt, a client hook asks the local store for notes
  that match. The hook adds the best notes to the prompt context.
- **Recall on request.** The MCP tool `noblivion_recall` lets either client
  search notes and fetch one full note by ID. Codex can also save a reviewed
  note with `noblivion_remember`.
- **Error recall.** When a shell command fails, a hook looks for notes
  about that error.
- **Guards.** Hooks read rules from your notes. Before a tool call, they
  show the matching rule to Claude Code. A rule can also block a command.
  A separate guard blocks commands that would print a git URL with a
  password or a token in it.
- **Stop checks.** When Claude Code wants to end a turn, a hook can ask it
  to finish open work first. By default the checks only log what they would
  ask (shadow mode); `noblivion stop report` shows the counts.
- **Usage report.** The store records when a note is shown, used or
  contradicted (a guard deny that was overridden). From this usage
  evidence, a report lists notes to retire, to promote or to demote. The
  trust value in the report does not say whether a note is correct, and it
  does not change the order of recall by default.
- **Duplicate sweep.** An optional command finds notes that say the same
  thing. A hosted model judges each pair. This is off until you turn it
  on.
- **Transcript miner.** This is off until you turn it on, because its
  precision is not measured yet. When it is on, a hook turns the session
  transcript into notes with a low starting trust value at the end of a
  session. The prompt hook never adds these notes. Claude Code sees them
  only when it asks the MCP tool for them.

## What it does not do

- It does not run a server on the network. The store listens only on
  `127.0.0.1`, the local machine.
- It does not sync notes between machines. Your Markdown files are the
  source of truth. Sync them with any file tool.
- It does not change how Claude Code loads `CLAUDE.md` or `MEMORY.md`.
  Codex still loads its own project instructions.
- It does not increase either client's compute or usage allowance.
- It does not support Windows in this version.
- It does not serve more than one operating system user from one store.

## Privacy

NOBLIVION is local by default. The notes, the database, the vectors and
the trust data stay on your machine. With the default settings, the store
makes no network connection after the install. Two features can send note
text off the machine, and both are off until you turn them on and confirm
your consent. Remote embeddings send note text and prompts to a hosted
embedding service, after you set the backend and run
`noblivion consent embeddings`. The duplicate sweep sends two notes per
pair to a hosted judge model, after you set a model, an API key and type
your consent. Before text leaves the machine, NOBLIVION removes secrets
and replaces paths, user names and IP addresses. It does not catch every
name in plain prose. See [docs/dedup-and-privacy.md](docs/dedup-and-privacy.md).

## Quick install for Claude Code

You need Claude Code, `python3` 3.9 or newer, and
[uv](https://docs.astral.sh/uv/).

1. Add the marketplace. Use the HTTPS URL:

   ```sh
   claude plugin marketplace add https://github.com/Noetic-os/NOBLIVION.git
   ```

   In a Claude Code session, `/plugin marketplace add <same URL>` does the
   same.

2. Install the plugin:

   ```sh
   claude plugin install noblivion@noblivion
   ```

   In a session: `/plugin install noblivion@noblivion`.

3. Start a new Claude Code session. Its first line says that the memory
   store is not installed.
4. In that session, type this slash command in the chat (in the VS Code
   chat or in a terminal session):

   ```text
   /noblivion:setup
   ```

   Allow the one `install.sh` command it runs. It builds the store and
   starts it. Memory recall works from your next prompt. You do not need
   another session.

The full steps, and the same step as a terminal command, are in
[docs/install.md](docs/install.md).

## Add Codex to the same memory

Use the same computer, operating system user, NOBLIVION data folder and
project namespace as Claude Code. Select an existing memory folder that the
store already indexes. You can use the default Claude Code memory folder
for the project, or add your folder to `memory_dirs` in the shared
`config.json`. From a checkout of this repository, run:

```sh
python3 scripts/install_codex.py \
  --data-dir /absolute/shared/data \
  --memory-dir /absolute/shared/memory \
  --workspace /absolute/project/repo
```

Use the Claude Code plugin's existing data folder when you have one. The
Codex hooks apply to the listed workspace and its Git worktrees. The MCP
server is a user-level Codex tool, so it is available in all Codex
workspaces. Call it only for the intended project. The installer uses the
existing store. Open Codex CLI and use `/hooks` to review the new hooks.
See [Codex setup](docs/codex.md) and [shared memory](docs/shared-memory.md)
for the full steps and limits.

## Documentation

- [Install, update and uninstall](docs/install.md)
- [Codex setup](docs/codex.md)
- [Shared memory for Claude Code and Codex](docs/shared-memory.md)
- [Configuration](docs/configuration.md): every setting, with its default
- [How recall works](docs/how-recall-works.md)
- [Guards and stop checks](docs/guards.md)
- [Trust](docs/trust.md)
- [Duplicate sweep and privacy](docs/dedup-and-privacy.md)
- [Transcript miner](docs/transcript-miner.md)
- [Import memories from a JSONL file](docs/import.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture (design document)](docs/design/0001-architecture.md)
- [Contributing](CONTRIBUTING.md)

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
