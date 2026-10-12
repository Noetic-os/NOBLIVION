<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Use NOBLIVION with Codex

Codex can use the NOBLIVION store and the same memory files as Claude Code.
The Codex adapter is in this repository. Its MCP tools are
`noblivion_recall` for search and fetch, and `noblivion_remember` for an
explicitly saved note. Codex hooks add prompt recall, guards and task
continuity. The adapter translates Codex events into calls to the shared
NOBLIVION code. It does not create another database.

For a direct connection, the store, database and Markdown files must be on the
same host as both clients. The clients must run as the same operating system user. Read
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

For native Codex MCP recall, the PreToolUse hook replaces the reserved
`read_capability` argument with an opaque capability issued by the authenticated
local store. The capability binds this chat, namespace, project root and exact
recall arguments. It expires after 15 seconds, works once and becomes invalid
when the store restarts. The model should omit this reserved argument. Search,
ranked index and fetch all check the chat's current memory-use controls again
when they read. Disabling use in one chat therefore does not block an enabled
chat with a valid native hook route. Contribution remains an independent control.

The Codex installer sets `NOBLIVION_MCP_REQUIRE_CAPABILITY=1`. In this mode a
missing, expired, replayed or altered capability returns a tool error before
reading memory, even if a hook is disabled or fails and the store has not seen
a local opt-out. Process environment session IDs and plain model-supplied IDs
never establish the current chat. Keep the PreToolUse hook enabled and trusted
to get independent per-chat recall. Re-run the Codex installer when upgrading
an older setup so it adds this mode and the recall tool matcher.

Other MCP clients retain the conservative unknown-session guard when they have
no capability. In that case an opted-out chat in the same scope can block recall.

A custom stdio forwarder must run the hook, MCP server and store from compatible
versions on the store host. Forward the native `session_id`, workspace scope,
tool name and complete tool arguments to the adapter. Return its complete hook
output to Codex, including `permissionDecision` and `updatedInput`. Launch the
forwarded MCP server with `NOBLIVION_MCP_REQUIRE_CAPABILITY=1` and the same data
folder, namespace and root used by the hook. The capability issuer and resolver
stay on the store host; the forwarder needs no new signing key. Local use-off
must deny recall, and a failed or absent hook must never disable the required
capability mode. Do not remove an older conservative connection guard until
that mode and complete hook forwarding are verified together. An inherited
session environment variable does not satisfy this contract.

This routing trusts the native hook and the existing local-store authentication
under the same operating system user. It does not attest a session against a
hostile process that controls that user's account. NOBLIVION removes capabilities
from its journal, learning capture and stored text; the client's own transcript
is controlled by Codex.

## Forward to a store on another host

`codex/remote_bridge.py` and `codex/remote_gateway.py` provide the same scoped
hook and stdio MCP paths over an existing authenticated SSH connection. Keep
the same NOBLIVION release on both hosts. The gateway runs as the operating
system user that owns the remote store. It opens no additional network listener.
Use absolute paths and private configuration files.

The client configuration identifies native workspaces and an explicit gateway
argument list. For example, `/absolute/client/remote-client.json`:

```json
{
  "repo_root": "/absolute/client/NOBLIVION",
  "state_dir": "/absolute/client/state",
  "learning": {"contribute": false},
  "project_mappings": [
    {"workspace": "/absolute/client/project", "namespace": "shared", "root": "project"}
  ],
  "gateway_argv": [
    "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=yes", "configured-ssh-alias",
    "python3", "/absolute/server/NOBLIVION/codex/remote_gateway.py",
    "--config", "/absolute/server/remote-server.json"
  ]
}
```

The server configuration maps those exact client workspace paths onto remote
notes. The namespace and root must agree with the client configuration:

```json
{
  "repo_root": "/absolute/server/NOBLIVION",
  "data_dir": "/absolute/server/store-data",
  "memory_dir": "/absolute/server/project/memory",
  "state_dir": "/absolute/server/client-state",
  "project_mappings": [
    {"workspace": "/absolute/client/project", "namespace": "shared", "root": "project"}
  ]
}
```

Register the client MCP server in Codex with this command and argument list:

```toml
[mcp_servers.noblivion]
command = "python3"
args = ["/absolute/client/NOBLIVION/codex/remote_bridge.py", "--config", "/absolute/client/remote-client.json", "mcp"]
```

For native hooks, use the same events and matchers generated by
`scripts/install_codex.py`, replacing the adapter command with
`python3 /absolute/client/NOBLIVION/codex/remote_bridge.py --config /absolute/client/remote-client.json hook`.
The PreToolUse matcher must include `mcp__noblivion__noblivion_recall`. Review and
trust the commands in Codex, then start a new chat. Both scripts accept
`--config`; without it they read the corresponding `remote-client.json` or
`remote-server.json` beside the script.

The gateway always starts MCP with required-capability mode. It forwards native
PreToolUse through the real adapter and returns its complete input rewrite.
Unmapped or mismatched scopes are refused. A local use-off chat denies recall
even when contribution is disabled. A failed remote hook also denies recall.
Other chats can share the connection without inheriting that chat's opt-out.

Local capture remains independent. To enable it, set client
`learning.contribute` to `true` and set `learning.delivery_argv` to the same
gateway argument list followed by `receive`. The client owns native transcript
access and its durable spool. The gateway accepts only mapped capture scopes
and does not create a second remote transcript capture spool. Capability values
are removed before local capture, remote journaling and delivery.

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
