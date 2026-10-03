---
# SPDX-License-Identifier: AGPL-3.0-or-later
name: setup
description: Install the NOBLIVION memory store and start it in this session. Run it once after the plugin install and once after each plugin update.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/install.sh" --data-dir "${CLAUDE_PLUGIN_DATA}":*)
---

# NOBLIVION setup

This command builds the NOBLIVION memory store and starts it. Memory recall
then works from the next prompt in this session. No new session is needed.

1. Run this one command with the Bash tool. Set the timeout to 600000 ms:
   the first run builds a Python venv and downloads the embedding model
   (about 70 MB).

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/install.sh" --data-dir "${CLAUDE_PLUGIN_DATA}" $ARGUMENTS
   ```

   Run the command exactly as written. Run no other command before it. If
   the command still holds a `${` placeholder, do not run it: tell the user
   that this Claude Code version does not fill in plugin paths, and to run
   the terminal command from the NOBLIVION line at the session start instead.

2. Tell the user the result in two or three short lines:
   - The last line says `The store runs`: setup is done, and memory recall
     works from the next prompt.
   - The script stopped with an error, or the last line says the store did
     not start: give the reason line from the output. Point the user to
     `docs/install.md` and `docs/troubleshooting.md` in the plugin folder.
   - The script printed `uv not found`: give the user the install command it
     printed for `uv`, then ask them to run `/noblivion:setup` again.
