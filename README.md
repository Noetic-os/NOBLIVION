<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# NOBLIVION

NOBLIVION is a plugin for Claude Code. It gives Claude Code a memory that
it can search. It runs on your own machine.

You keep notes for Claude Code as Markdown files in its memory folders.
NOBLIVION indexes these files in a local database. On each prompt, it finds
the notes that match the prompt and adds them to the context. It also keeps
track of which notes help, and it can stop a tool call that breaks one of
your own rules.

Status: in development. There is no release yet.

## What it does

- **Recall.** On each prompt, a hook asks the local store for the notes
  that match. The hook adds the best notes to the context of the prompt.
- **Recall on request.** An MCP tool, `noblivion_recall`, lets Claude Code
  search the notes when it needs them. (MCP is the Model Context Protocol,
  the way Claude Code talks to tools.)
- **Error recall.** When a shell command fails, a hook looks for notes
  about that error.
- **Guards.** Hooks read rules from your notes. Before a tool call, they
  show the matching rule to Claude Code. A rule can also block a command.
  A separate guard blocks commands that would print a credential.
- **Stop checks.** When Claude Code wants to end a turn, a hook can ask it
  to finish open work first.
- **Trust.** The store records when a note is shown and when it is used. It
  computes a trust score for each note. A report lists notes to promote,
  to demote or to retire.
- **Duplicate sweep.** An optional command finds notes that say the same
  thing. A hosted model judges each pair. This is off until you turn it
  on.
- **Transcript miner.** At the end of a session, a hook turns the session
  transcript into notes with low trust. The prompt hook never adds these
  notes. Claude Code sees them only when it asks the MCP tool for them.

## What it does not do

- It does not run a server on the network. The store listens only on
  `127.0.0.1`, the local machine.
- It does not sync notes between machines. Your Markdown files are the
  source of truth. Sync them with any file tool.
- It does not change how Claude Code loads `CLAUDE.md` or `MEMORY.md`.
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

## Quick install

You need Claude Code, `python3` 3.9 or newer, and
[uv](https://docs.astral.sh/uv/).

1. In Claude Code, add the marketplace. Replace `<owner>` with the GitHub
   account that hosts this repository:

   ```text
   /plugin marketplace add <owner>/NOBLIVION
   ```

2. Install the plugin:

   ```text
   /plugin install noblivion@noblivion
   ```

3. Start a new Claude Code session. The first session prints the command
   that builds the store. Run that command in a terminal. It looks like
   this:

   ```sh
   CLAUDE_PLUGIN_DATA="<data dir>" bash "<plugin dir>/scripts/install.sh"
   ```

4. Start a new session. The store starts by itself.

The full steps are in [docs/install.md](docs/install.md).

## Documentation

- [Install, update and uninstall](docs/install.md)
- [Configuration](docs/configuration.md): every setting, with its default
- [How recall works](docs/how-recall-works.md)
- [Guards and stop checks](docs/guards.md)
- [Trust](docs/trust.md)
- [Duplicate sweep and privacy](docs/dedup-and-privacy.md)
- [Transcript miner](docs/transcript-miner.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture (design document)](docs/design/0001-architecture.md)
- [Contributing](CONTRIBUTING.md)

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
