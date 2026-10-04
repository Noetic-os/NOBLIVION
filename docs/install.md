<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Install, update and uninstall

NOBLIVION has two parts:

- **The plugin files.** These are the hooks, the MCP server and the
  scripts. Claude Code installs them through its plugin system. The hooks
  and the MCP server need only `python3` 3.9 or newer. They use no extra
  Python packages.
- **The store.** This is a small local server with a SQLite database and,
  optionally, an embedding model. It runs from a Python virtual
  environment (a "venv") in the data dir. The script `scripts/install.sh`
  builds this venv.

The plugin system places the files. It does not build the venv. You run
the slash command `/noblivion:setup` once after the first install and once
after each plugin update. It runs `install.sh` for you and starts the store
in the same session.

To add Codex to this same store, follow [Codex setup](codex.md) after the
store install. [Shared memory](shared-memory.md) explains the data folder,
project namespace and Markdown folder that both clients must use.

## Requirements

- Claude Code with plugin support.
- `python3` 3.9 or newer on `PATH`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`.
  `uv` builds the venv. It also fetches Python 3.11 or newer for the venv
  when your system does not have it.
- Linux or macOS. Windows is not supported in this version.

## The data dir

All state lives in one folder, the data dir. NOBLIVION picks the first of
these:

1. `NOBLIVION_DATA_DIR`, when it is set.
2. `CLAUDE_PLUGIN_DATA`. Claude Code sets this for the hooks and the MCP
   server of an installed plugin. A value that still holds `${` (a config
   file passed it unexpanded) counts as unset.
3. `$XDG_DATA_HOME/noblivion`, else `~/.local/share/noblivion`.

When the plugin is installed, the data dir is the folder that Claude Code
gives the plugin in `CLAUDE_PLUGIN_DATA`. The slash command
`/noblivion:setup` gets this folder from Claude Code. A terminal does not
have this variable. For this reason the first session also prints the full
terminal command with the folder in it.

For Claude Code and Codex on one computer, use the same absolute data dir.
Set `NOBLIVION_DATA_DIR` for both clients, or point Codex at the existing
Claude Code plugin data dir. A different folder makes a different SQLite
database. See [Codex setup](codex.md).

Only `install.sh` makes the data dir. The store, the command line tools and
the hooks never make it. They also never make a new `noblivion.db`, except
the store and `noblivion index` after `install.sh` has run (they check the
install stamp in `venv/`). So after an uninstall deletes the data dir, a
late process cannot make an empty data dir again (NOBLIVION-28).

The data dir holds these files:

| File or folder | What it is |
| --- | --- |
| `noblivion.db` | the SQLite database |
| `config.json` | your settings (see [configuration.md](configuration.md)) |
| `token` | the shared secret between the hooks and the store, mode 0600 |
| `store.json` | the port and process id of the running store |
| `store.error` | why the last store start failed; removed by a good start |
| `venv/` | the store venv |
| `models/` | the embedding model cache |
| `cache/` | hook state: recall logs, trust events, sync state |
| `logs/store.log` | the store log |
| `migrated/` | old hook files moved by the migration, if you ran it |

The data dir has mode 0700. Claude Code can make the folder with your
umask (for example 0775) before NOBLIVION runs. So `install.sh`, the
SessionStart hook and the store set it to 0700 also when it exists.

## Install with the plugin marketplace

Each step shows the slash command in a Claude Code session and the same
step as a terminal command. Use either one.

1. Add this repository as a marketplace. Use the HTTPS URL:

   ```text
   /plugin marketplace add https://github.com/Noetic-os/NOBLIVION.git
   ```

   ```sh
   claude plugin marketplace add https://github.com/Noetic-os/NOBLIVION.git
   ```

   The repository is public, so the HTTPS URL needs no credentials. The
   short form `Noetic-os/NOBLIVION` can clone over SSH. On a machine with no GitHub SSH key it then fails with
   `Permission denied (publickey)`.

   To install from a local clone, give the path of the clone instead:

   ```sh
   claude plugin marketplace add /path/to/NOBLIVION
   ```

2. Install the plugin:

   ```text
   /plugin install noblivion@noblivion
   ```

   ```sh
   claude plugin install noblivion@noblivion
   ```

   `claude plugin list` shows the installed plugins.

3. Start a new Claude Code session. The SessionStart hook sees that the
   venv is missing. It prints one line, for example:

   ```text
   NOBLIVION: the memory store is not installed, so memory recall is off. Tell the user to type /noblivion:setup in this chat (in a terminal: CLAUDE_PLUGIN_DATA="<data dir>" bash "<plugin dir>/scripts/install.sh")
   ```

4. In the same session, type the slash command in the chat. This works in
   the VS Code chat and in a terminal session:

   ```text
   /noblivion:setup
   ```

   The command runs `install.sh` with your data dir. Claude Code asks you
   to allow that one command. The first run takes one to a few minutes: it
   builds the venv and downloads the embedding model. At the end,
   `install.sh` starts the store. Memory recall works from your next
   prompt in this session. You do not need another session.

   Without the slash command, run the terminal command from the line of
   step 3 instead. It also starts the store. Your open session then has
   memory recall from its next prompt.

5. When the store cannot start, `install.sh` prints the reason, and the
   next session shows one line with the reason, for example:

   ```text
   NOBLIVION: the memory store could not start, so memory recall is off: bind failed on port 8894: Address already in use. ...
   ```

   See [The store is not running](troubleshooting.md#the-store-is-not-running).

So the plugin install needs at most two sessions: the one where you ran
`claude plugin install` (or a terminal), and the next one, where you run
`/noblivion:setup`.

## What install.sh does

`install.sh` does these steps in this order:

1. It checks for `python3` 3.9 or newer and for `uv`. Without `uv`, it
   prints the official install command for `uv` and stops. It does not
   install `uv` for you.
2. It makes the data dir and sets it to mode 0700, also when the folder
   exists.
3. It makes the venv in `<data dir>/venv` with Python 3.11 or newer. It
   installs the locked store dependencies (`numpy` and `fastembed`) and
   the `noblivion` package.
4. It copies the default config to `<data dir>/config.json`. It never
   overwrites a `config.json` that exists.
5. It downloads the default embedding model to `<data dir>/models/`.
   When the download fails, it sets `embedding.backend` to `none` in
   `config.json`. Recall then works with keyword search only.
6. It writes the `token` file with mode 0600, unless one exists.
7. It writes an install stamp in the venv with the plugin version.
8. It indexes your memory files once with `noblivion index`.
9. It checks for old hand-installed hooks. This is a dry run. It only
   prints what the migration would remove. See
   [Migrate from hand-installed hooks](#migrate-from-hand-installed-hooks).
10. It starts the store with `noblivion ensure-running`, unless you give
    `--no-start`. A store of an older version is stopped and replaced. When
    the start fails, the script prints the reason and still exits 0; the
    next session tries again.

### Options

| Option | Effect |
| --- | --- |
| `--data-dir DIR` | Use `DIR` as the data dir. |
| `--no-embed` | Do not install `numpy` and `fastembed`. Recall uses keyword search only. No model is downloaded. |
| `--no-model` | Install the packages, but do not download the model now. |
| `--no-start` | Do not start the store now. The next session starts it. |
| `--dry-run` | Print the steps. Change nothing. |
| `--help` | Print the usage. |

## The embedding model

The default backend is `fastembed` with the model
`BAAI/bge-small-en-v1.5`. The model runs on the CPU. With the model,
recall combines keyword search and vector search. Without it, recall uses
keyword search only. Both modes work.

To download the model later:

1. Open `<data dir>/config.json`.
2. If `embedding.backend` is `none`, set it to `fastembed`, or remove the
   key.
3. Run `/noblivion:setup` again, or `install.sh` without `--no-model` and
   without `--no-embed`. It restarts the store. The store computes the
   vectors for your notes in the background.

Other backends (a local Ollama server, or a hosted service) are in
[configuration.md](configuration.md). A hosted backend sends note text off
the machine. Read [dedup-and-privacy.md](dedup-and-privacy.md) first.

## Update

1. Update the plugin in Claude Code with the `/plugin` menu.
2. Start a new session. When the venv was built for another plugin
   version, the SessionStart hook prints one line that names
   `/noblivion:setup`.
3. Type `/noblivion:setup` in that session. It builds the venv again and
   replaces the old store with the new one. Your database, config and
   token stay.

## Uninstall

The steps below keep your database (the memory index and the trust
history), `config.json` and `token`, so a later install starts where you
stopped.

1. Stop the store, wait for it to exit, and remove the venv and the model
   cache:

   ```sh
   CLAUDE_PLUGIN_DATA="<data dir>" bash "<plugin dir>/scripts/uninstall.sh"
   ```

   The script waits until the store has exited (at most 90 s; then it
   sends `SIGKILL`), so no store process writes in the data dir after the
   next step. This keeps `noblivion.db`, `config.json` and `token`. Add
   `--dry-run` to see the steps first. The script refuses a folder that does not look
   like a NOBLIVION data dir.

2. Remove the plugin and keep the data dir:

   ```sh
   claude plugin uninstall noblivion --keep-data
   ```

**Warning.** `claude plugin uninstall noblivion` without `--keep-data`, and
`/plugin uninstall noblivion` in a session, delete the whole data dir:
`noblivion.db`, `config.json`, `token`, the logs and the cache. The
database cannot be recovered after that.

To remove everything on purpose, run `uninstall.sh --purge` (it deletes
the data dir), then `claude plugin uninstall noblivion`.

The uninstall never touches your memory files.

## Migrate from hand-installed hooks

An earlier, private version of these hooks was installed by hand. If you
used it, you have files named `claude_code_*.py` in `~/.claude/hooks/`,
entries in `~/.claude/settings.json` that run them, and an MCP server entry
in `~/.mcp.json`. With the plugin installed as well, each hook runs twice.
The command `noblivion migrate-from-legacy` removes the old setup.

Use the `noblivion` command from the venv:
`<data dir>/venv/bin/noblivion`.

1. Print the plan. This is a dry run and changes nothing:

   ```sh
   <data dir>/venv/bin/noblivion migrate-from-legacy
   ```

   The plan lists the old hook files, the settings entries that run them,
   the old MCP entry, and the old env vars that are set, with their new
   `NOBLIVION_*` names.

2. Read the plan. Then apply it:

   ```sh
   <data dir>/venv/bin/noblivion migrate-from-legacy --apply
   ```

   The command copies `settings.json` and `.mcp.json` to
   `<file>.noblivion-backup-<time>`. It removes only the entries that run
   an old hook file. Every other entry keeps its value and its place. It
   moves the old hook files to `<data dir>/migrated/<time>/`. It deletes
   no file. An unknown `claude_code_*.py` file stays where it is.

3. Rename the old env vars that the plan lists. The command does not
   change your shell profile.
4. Start a new Claude Code session.

To undo the last `--apply`:

```sh
<data dir>/venv/bin/noblivion migrate-from-legacy --undo
```

These things do not carry over:

- **Trust history.** It lived on the old server. Trust starts again from
  the default value.
- **Memory ids.** The store gives each note a new id.

Your memory files need no migration. The store reads them where they are.
Settings files are written back as JSON: the values and the order stay,
but the whitespace can change.
