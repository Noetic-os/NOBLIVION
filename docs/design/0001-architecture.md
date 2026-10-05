<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 0001 NOBLIVION architecture

Status: proposed. Ticket: E0 of the v0.1 plan.

This document is the design for NOBLIVION v0.1. It fixes the parts that
later work must not change by accident: the REST contract, the SQLite
schema, the process lifecycle and the privacy rules. Code that does not
match this document is a defect in the code or in this document. Fix one
of them in the same pull request.

Words used in this document:

- "Hook": a Claude Code hook script. It runs once per event and exits.
- "Store": the one local NOBLIVION process that holds the database.
- "Memory file": a Markdown file in a Claude Code memory folder.
- "Memory row": one row in the `memories` table.
- "Reference implementation": the earlier client and server code that this
  plugin replaces. Its hooks talk to a remote memory server over REST.

## 1. Goal and non-goals

### Goal

- Give Claude Code recall of the user's own memory files, on the user's
  machine, with no remote server.
- Keep the hooks of the reference implementation and the REST paths and
  JSON shapes they use. The hooks need a short list of changes (section
  4.0), but no change to how they parse answers.
- Keep the trust loop: record when a memory is shown, used or contradicted,
  and use this usage evidence to report memories to retire, promote or
  demote. Trust does not measure whether a memory is correct.
- Keep the guards (tool-call guards that need no store) working with no
  store at all.
- Add an opt-in duplicate sweep with a hosted judge model.
- Add a local transcript miner that turns past sessions into low-trust
  memory rows.
- Ship as one Claude Code plugin under AGPL-3.0-or-later.

### Non-goals

- No multi-user server. One store serves one OS user.
- No network listener other than loopback.
- No Postgres, Redis, row-level security, or container.
- No sync between machines. The memory files are the source of truth. Sync
  them with any file tool if needed.
- No change to how Claude Code itself loads `CLAUDE.md` or `MEMORY.md`.
- No benchmark package and no developer tools in v0.1.
- No Windows support in v0.1 (see section 18).

## 2. Components

```
 Claude Code
   |  hook events (stdin JSON)          MCP (stdio)
   v                                     v
 hooks/*.py  (stdlib python3)      mcp/recall_mcp.py (stdlib python3)
   |   \                                 |
   |    +-- local-only hooks: guards, stop checks (no store needed)
   |
   +-- HTTP on 127.0.0.1:<port>  ------+
                                       v
                         store process (plugin venv: numpy, fastembed)
                           - HTTP server (stdlib ThreadingHTTPServer)
                           - SQLite file (WAL)
                           - in-RAM BM25 index and vector matrix
                           - embedding backend
                           - indexer, trust recompute, embed backfill
                                       ^
 CLI tools (plugin venv) --------------+  same SQLite file, same rules
   noblivion index | mine | dedup | trust | doctor | migrate-from-legacy
```

- Hooks stay stdlib `python3`. They load siblings by file path, as today.
  They must start fast because they run on every prompt.
- The store is the only process that loads numpy and the embedding model.
- The CLI tools use the same library code as the store. They may open the
  SQLite file directly. They never hold a write transaction for more than
  one batch.
- The MCP server is a stdlib script. It calls the store over HTTP.

Python versions:

- Hooks and the MCP server: python3 3.9 or newer, stdlib only. No
  `tomllib`, so the config file is JSON.
- Store and CLI: python 3.11 or newer in a venv that the installer makes
  with `uv` in the data dir.

## 3. Process lifecycle

### 3.1 Files in the data dir

| File | Owner | Purpose |
|---|---|---|
| `noblivion.db` (+ `-wal`, `-shm`) | store, CLI | the SQLite database |
| `store.lock` | store | `flock` held for the store's lifetime |
| `store.json` | store | `{pid, port, version, started_at}`, written atomically after bind |
| `token` | store | 32 random bytes, hex, mode 0600 (section 4.1); the store makes it at start when it is missing or malformed (section 19) |
| `spawn.stamp` | hooks | mtime marks the last start attempt |
| `cache/by-session/` | hooks | per-session recall cache and trust event spool |
| `cache/` (other files) | hooks | continuity state, memory sync stamps and log, trust report and dedup status files |
| `guard-tables/<slug>.json` | hooks | the guard table of one project memory folder, compiled from the memory rule fields (NOBLIVION-31) |
| `guard-log.jsonl`, `guard-state/` | hooks | guard hook log and per-agent state |
| `stop-check-log.jsonl`, `stop-check-state/` | hooks | stop check log and correction markers |
| `index.lock`, `mine.lock` | store, CLI | one indexer scan and one miner run at a time |
| `dedup/` | CLI | dedup plans, write-ahead logs, backups, latch file |
| `migrated/<utc>/` | CLI | old hook files moved by `noblivion migrate-from-legacy --apply`, with `manifest.json` for `--undo` |
| `logs/store.log` | store | rotating log, 5 x 1 MB |
| `config.json` | user | optional config (section 12); `install.sh` writes the shipped default when it is missing |
| `venv/`, `models/` | installer | store venv and embedding model cache; `venv/noblivion-install.json` holds the plugin version and root the venv was built from |
| `backups/` | store | DB copy taken before each migration |
| `store.error` | store, launcher | why the last start failed (`{reason, pid, at}`); a good start removes it |

The data dir is mode 0700. Every file in it is mode 0600. Claude Code can
make the data dir with the user's umask before NOBLIVION runs, so
`install.sh`, the SessionStart hook, the launcher and the store set an
existing data dir to 0700 too (NOBLIVION-27).

Only `install.sh` makes the data dir (NOBLIVION-28). The store, the
launcher, the CLI tools and the hooks never make it: the store and the
launcher stop with "not installed", the CLI tools exit with code 6, and a
hook makes a folder inside the data dir only while the data dir exists
(`hook_config.make_dirs`). A new `noblivion.db` needs the install stamp
`venv/noblivion-install.json` (section 6.6). Reason: Claude Code keeps
running sessions, and an orphaned plugin version, after `claude plugin
uninstall` deleted the data dir. A store that stopped later opened a new
connection for its WAL checkpoint, and `connect` made the folder and an
empty `noblivion.db` again.

### 3.2 Start

1. The `SessionStart` hook reads `store.json`. If the file is missing, go
   to step 3. Else the hook checks the listener with the proof of section
   3.3 (300 ms timeout).
2. Proof good and the same full version as the plugin: the hook stops. The
   store runs. Proof good and a different version: the hook sends `SIGTERM`
   to the pid in `store.json`, because an old store may run an old
   redactor, and goes to step 4 (it skips the stamp check). The new store
   gets `--lock-wait 10`, so it waits up to 10 s for the old store to
   release `store.lock` instead of exiting at step 5. No proof: go to
   step 3.
3. The hook checks `spawn.stamp`. If its mtime is less than 30 s old,
   another hook started the store already. The hook stops. The check and
   the touch of step 4 run under `flock` on `spawn.stamp` (a caller that
   creates the file wins), so callers at the same moment start one store,
   not one each (E3d).
4. Else the hook touches `spawn.stamp` and starts
   `<data dir>/venv/bin/python -m noblivion.store` as a detached child
   (`start_new_session=True`, stdin from `/dev/null`, output to the log,
   working dir the data dir, `PYTHONPATH` set to `${CLAUDE_PLUGIN_ROOT}` so
   the package imports from the installed plugin). It does not wait.
5. The store takes `flock(LOCK_EX | LOCK_NB)` on `store.lock`. If the lock
   is held, another store runs. The new process exits with code 0.
6. The store opens the database (a new one only after `install.sh` ran,
   section 6.6), runs migrations (section 6.6), and binds
   `127.0.0.1:<port>`. It writes `store.json` (temp file plus `rename`).
7. The store answers requests at once, from the rows already in the
   database. The first index scan (section 5.6) and the model load run in
   background threads. `/health` shows `index_state: scanning` until the
   scan ends. Until the model is ready, ranking is keyword-only (section
   8.4).

Any REST hook that finds the store down also runs steps 3 and 4. So a store
that exited on idle comes back on the next prompt. That hook call itself
fails open (section 3.5).

Entry points (E3c):

- `noblivion serve [--port N] [--lock-wait S]` and
  `python -m noblivion.store` run steps 5 to 7 in the foreground.
- `noblivion ensure-running [--json] [--wait S] [--restart]` runs steps 1
  to 4. `--restart` replaces a proven store of its own version too, because
  a store reads the config and loads the model only at its start
  (NOBLIVION-57). When it started a store (or another caller is starting
  one), it then waits up to `S` seconds (default 10) for a proven store of
  its version, and not for the store that it replaced. It prints
  `running`, `starting`, `started` or `failed` with the reason, and exits
  1 on `failed` (NOBLIVION-25). A failure is `store.error`, the child store
  exiting with a code other than 0, or no proven store in time; the
  launcher writes `store.error` when the store did not. It imports only
  the stdlib and `noblivion.config`. The code is `noblivion.launcher`.
- The hooks cannot import the package. `hooks/store_client.py` (E4) holds
  their side: it reads `store.json` and `token`, runs the listener proof of
  section 3.3, and starts the launcher. It does not copy steps 1 to 4. It
  runs `<venv>/bin/noblivion ensure-running` as a detached child
  (`start_new_session`, no output, never waited for), and the launcher runs
  steps 1 to 4, the version check and the atomic `flock` start check
  included. As a script it is the
  `SessionStart` hook: it starts the launcher, waits at most 5 s for the
  launcher's exit code and exits 0. When the launcher exits with code 1,
  the hook prints one line with the reason from `store.error`, as a
  `systemMessage` for the user and as context for the model. A launcher
  still waiting after 5 s is left running in the background. A REST
  hook that finds the store down or unproven also starts the launcher, but
  only when `spawn.stamp` is older than 30 s, so a prompt does not fork a
  launcher while a start is under way. The hook only reads the stamp's
  age; the launcher alone locks and touches it. The entry point is config key
  `store.bin` (env `NOBLIVION_BIN`), default `<data dir>/venv/bin/noblivion`;
  no executable there means no start. `NOBLIVION_STORE_AUTOSTART=0` turns
  every hook start off.
- Exit codes of the store: 0 stopped, or another store holds the lock;
  2 the bind failed; 3 a schema fault (section 6.6); 4 not installed (no
  data dir, or no database and no install stamp). `ensure-running` prints
  `not_installed` and exits 1 when the data dir is missing; it never makes
  it.

### 3.3 Port

- Default port: 0, any free port (since 0.1.1). Config key `port`, env
  `NOBLIVION_PORT`. The fixed default 8894 of 0.1.0 was taken on a test
  host, and the store then could not start (NOBLIVION-25). The hooks find
  the port only through `store.json`, so no caller needs a fixed port.
- The listen backlog is 128, not the stdlib 5. Hooks of several sessions
  connect at once; a full backlog drops the SYN and the client retries
  after 1 s, past the 300 ms proof below (E3d measured 1 s to 8 s).
- Port `0` means "any free port". The store writes the real port to
  `store.json`.
- The hooks find the store only through `store.json`. They never fall back
  to the config port. No `store.json` means no store: the hook starts one
  (section 3.2) and fails open.
- If the bind fails because the port is in use, the store logs the error
  with its reason (`bind failed on port N: Address already in use`),
  writes it to `store.error` and exits with code 2. It does not try another port, because a silent
  port change would hide a foreign listener.

Listener proof. A port in `store.json` can belong to another process: the
store may have crashed, and another local user may have bound the same
port. So a hook proves the listener before it sends the token or any text:

1. The hook sends `GET /health?nonce=<32 random hex characters>`, with no
   token.
2. The store answers with
   `"proof": hex(HMAC-SHA256(token, "noblivion-health:" + nonce))`.
3. The hook computes the same value from the `token` file and compares in
   constant time.
4. No match, no `proof`, or no answer: the hook treats the store as down,
   sends nothing more, and logs `foreign_listener` or `down`.

The nonce is new for each check, so a recorded answer cannot be replayed.
A hook process checks once and keeps the result for its own short life.
The cost is one extra loopback request per hook run.

A Unix socket in the 0700 data dir would remove the port, the token on the
wire and the browser threats. v0.1 keeps TCP loopback because the approved
plan fixes it. Section 18 lists this as an open decision.

### 3.4 Idle exit and stop

- The store exits after `idle_exit_s` seconds with no request (default
  1800, `0` means never). A background job (indexer scan, re-embed) does
  not count as a request, but the store waits for a running job to finish
  its current batch before it exits.
- On `SIGTERM` or `SIGINT` the store stops accepting requests, finishes
  open requests (5 s limit), runs `PRAGMA wal_checkpoint(TRUNCATE)`,
  deletes `store.json` and releases the lock. The checkpoint opens the
  existing database only: after the data dir was deleted it makes nothing.
- The serve loop checks every tick (0.25 s) that the data dir exists. When
  it is gone (an uninstall), the store stops with reason `data dir removed`.
- On a crash the lock is released by the OS. `store.json` may stay. The
  next hook gets no answer from that port and starts a new store. The new
  store overwrites `store.json`.

### 3.5 What the hooks do when the store is down

Every hook fails open. A store problem must never block a prompt, a tool
call or a stop. A listener that fails the proof (section 3.3) counts as
down.

| Hook | Store down, timeout, or error |
|---|---|
| prompt recall (UserPromptSubmit) | inject nothing; exit 0 |
| MCP recall tool | return a tool error text "memory store not running" |
| trust flush (Stop) | keep the send offset; the next Stop sends again; exit 0 |
| subagent rules (SubagentStart) | inject nothing; exit 0 |
| error recall (PostToolUseFailure, PostToolUse for Bash) | default mode is `local`: it uses its own BM25 over the memory files and never calls the store. In store mode it falls back to that local BM25 |
| guards, stop checks, session line | not affected; they do not call the store |

Timeouts: one total budget per call, 2 s by default
(`NOBLIVION_RECALL_TIMEOUT_S`); 0.8 s for the error recall hook; 2 s per
feedback POST; 10 s for the trust report CLI. At most 4 calls run at once
per hook process. A hook never retries in the same call. A response over
1 MiB is a failure, so the store keeps every answer below 512 KB (section
4.2).

Event spool (kept from the reference implementation, moved into the data
dir):

- Hooks append trust events to
  `cache/by-session/<session_id>.trust-events.jsonl`. The Stop hook starts
  one detached flush worker and returns at once.
- The flush worker merges events to one per (memory, kind), keeps the
  earliest `ts`, and sends batches of at most 500 events and 256 KB. It
  keeps the send offset in `<session_id>.trust-flush.json` and moves it only
  when every batch answers 200.
- It then retries up to 5 other sessions with unsent events and stops at
  the first failure.
- Sent files are pruned after 7 days. Unsent files are dropped after 30
  days, well inside the 80-day age limit of the store.
- A replay is safe: the store keeps one event per (session, memory, kind).

### 3.6 Health check

`GET /health` (section 4.7) is cheap. It reads counters from RAM and runs
one `SELECT` on `meta`. The `noblivion doctor` CLI calls it and also checks
the files in section 3.1, the venv, the model cache and the hook entries.

## 4. REST contract

The store serves the same paths, query parameters and JSON shapes as the
reference implementation. Fields marked "new" are additions; the hooks
ignore unknown fields.

### 4.0 Hook changes (E4)

The answers parse as before, but the hooks are not unchanged. E4 makes
these changes and nothing else in the REST client:

- Base URL from `store.json` (section 3.3). The API key variables go away.
  The token comes from the `token` file and is sent only after the
  listener proof.
- Start the store when it is down (section 3.2).
- Keyword mode: when an answer has `"mode": "keyword"`, keep the server
  order and skip score floors (section 8.4).
- Send `root` (new) on search, index, fetch and feedback calls (section
  5.1).
- The transport guard keeps "plain http only to a loopback IP literal" and
  drops its private-network branch.
- The MCP tool is renamed `noblivion_recall`. Its input schema refuses
  unknown properties, so `include_mined` (section 11.3) is added to the
  schema.
- The injected header text names the reference project. It is rewritten.
- The trust report CLI prints `root`.

How E4 built these (the hooks are `hooks/recall_hook.py`,
`hooks/error_recall_hook.py`, `hooks/subagent_rules_hook.py` and
`mcp/recall_mcp.py`; the continuity hook reaches the store through
`corpus.recall_index`):

- One client path: `recall_hook.store_get` proves the listener
  (`store_client.connect`) and then sends the token. The proof request gets
  at most half the call budget (at least 0.3 s); the request gets the rest.
  A proof is kept for 30 s per process (data dir, port, token), so the
  long-lived MCP server does not prove on every call; a request that gets
  no answer drops the kept proof. Reasons in the hook log: `store_down`,
  `no_token`, `foreign_listener`.
- A proof or a request that times out writes `store.hung` in the data dir:
  a "down until" time 30 s ahead (NOBLIVION-69). Until then every caller of
  `store_get` fails at once with `store_hung` and sends nothing, so a store
  that accepts a connection and never answers costs one wait per 30 s, not
  one per prompt and per subagent start. A refused connection and an error
  answer write no stamp. A `store.json` newer than the stamp ends it.
- `root`: the hook entry derives it from the event's `cwd`. The memory
  folder is `NOBLIVION_RECALL_MEMORY_DIR` (or `NOBLIVION_MEMORY_DIR`), else
  `~/.claude/projects/<slug of cwd>/memory` when it exists. The root is
  `NOBLIVION_RECALL_ROOT`, else the parent folder name of the memory folder,
  else the slug of `cwd`. Without either, no `root` is sent. The MCP server
  uses `CLAUDE_PROJECT_DIR`, else its working dir, as `cwd`. The local
  re-rank, the rule rows and the error recall hook read the same folder.
- Keyword mode: the prompt hook and the subagent hook keep the store order.
  They skip the local re-rank (log `:rerank_off:keyword`) instead of fusing
  their BM25 with null scores, and skip the index score floor (log
  `:floor_off:keyword`). The error recall hook in `store` mode (renamed
  from `daemon`) treats a keyword answer as a failure and uses its local
  BM25.
- The MCP tool `include_mined=true` lists candidates from
  `/api/memories/index?include_mined=1` (titles and ids, no bodies);
  `fetch_id` reads one. A store that is down or unproven is the tool error
  `noblivion_recall: memory store not running (<reason>)`.
- The trust report CLI is not part of E4; it moves with E5.

Release gate: the E4 contract tests run the ported hooks against a live
store, keyword mode included. An unported hook is not supported. In
keyword mode it would sort the `null` scores last and fall back to id
order.

General rules for every route:

- Bind address 127.0.0.1 only.
- JSON in and out, UTF-8. `Content-Type: application/json`.
- Errors that refuse a whole request answer `{"detail": "<text>"}` with a
  4xx or 503 status. The text never carries memory text, file paths or
  exception messages. Details go to the store log.
- Search, index and fetch accept `root` (new, optional): the memory folder
  key of the session (section 5.1). With `root`, the pool is the
  `claude_code_md` rows of that root plus the roots in
  `recall.shared_roots`. Without `root`, the pool is every root, as one
  folder was in the reference implementation.
- Read routes that fail inside the ranker answer 200 with an empty result
  and a `reason` string, as the reference implementation does. The fixed
  error reason is `"index or fetch failed; see the store log"`. The store
  keeps this exact text because clients may compare it. (The reference
  implementation said "daemon log"; no NOBLIVION hook compares it.)

### 4.1 Auth and request checks

- Every request except `GET /health` needs
  `Authorization: Bearer <token>`. The token is the content of the `token`
  file. The hooks read it from the file at each call. The user never types
  it. Compare in constant time. Missing or wrong token: 401
  `{"detail": "missing or wrong token"}`.
- `GET /health` without a token answers the short form, plus `proof` when
  the request has a `nonce` (sections 3.3 and 4.7).
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`. Else
  421 `{"detail": "bad host"}`. This blocks DNS-rebinding pages.
- `POST` requests need `Content-Type: application/json`. Else 415. A web
  page cannot send that type to another origin without a CORS preflight,
  and the store never answers a preflight with allow headers.
- No CORS headers on any answer.
- Request body cap: 256 KB (262144 bytes). A `POST` must send
  `Content-Length`; without it the answer is 411, because the stdlib server
  does not decode a chunked body. A larger `Content-Length`: 413 before any
  read.
- Query string cap: 8 KB; a longer one: 414
  `{"detail": "query string too long"}`. Query text `q` is cut to 2000
  characters. (The hooks already cut it to 300.)
- A known path with the wrong method: 405 `{"detail": "method not
  allowed"}`. A body that is not JSON: 400. An internal fault outside a
  read route: 503 `{"detail": "store error"}`.
- The base URL is `http://127.0.0.1:<port>`. The hooks keep their
  plaintext guard: plain http only to a loopback IP literal. The guard
  refuses the name `localhost`, so the hooks never use it.
- `namespace` in every answer is the `project` the client sent, or the
  configured default namespace (`claude_code`) when it sent none. The hooks
  fail a call with `namespace_mismatch` when the two differ, so the store
  must never answer for a different namespace.

Why a token at all on loopback: any local process and any other OS user on
the same machine can reach 127.0.0.1. The token file is readable only by
the owner. The reference implementation used a user-managed API key. The
token replaces it and needs no user action.

### 4.2 `GET /api/memories/search`

Query: `q` (required), `project` (optional), `top_k` (int, default 5,
clamp 1..50, a non-number reads as 5). A missing `q` reads as empty.

The answer is one joined text, as in the reference implementation. Entries
are joined with `"\n---\n"`. Each entry is redacted on its own before the
join.

```json
{"results": ["<entry 1>\n---\n<entry 2>\n---\n<entry 3>"], "scores": [0.71, 0.66, null], "namespace": "claude_code"}
```

No hit, empty query, or empty pool:

```json
{"results": ["No memories available."], "scores": [], "namespace": "claude_code"}
```

Notes:

- `results` is a list with exactly one string. The hooks split it on
  `"\n---\n"`. They treat an entry as a memory file entry only when it
  holds the source marker `[claude_code_md: <path>]` (section 5.3). So the
  store must keep the content format of section 5.3 byte for byte.
- `scores` (NOBLIVION-22): one score per entry, in entry order, with the
  `score` rules of section 4.3 (`null` in keyword-only mode). The hook
  pairs the scores with the entries only when the counts match, and drops
  a hit below `recall.min_score`. A hit with no score passes.
- Each entry goes through the secret redactor and then the
  injection-pattern redactor (section 15.4).
- The hooks treat these strings as "no hits": `No memories available.`,
  `No valid embeddings for search.`, `No valid vectors for search.`,
  `Embedding generation failed.` The store uses only the first one.
- The reference implementation dropped `namespace` in this shape. The store
  always sends it, with the value the client expects.
- Rows with `source_type = 'transcript_mined'` and archived rows are never
  in this answer.
- The answer stays below 512 KB. An entry that does not fit is left out,
  and a smaller entry after it still goes in. An entry whose stored text
  alone does not fit is left out before it is redacted, so it costs no
  redaction time (NOBLIVION-62).
- A fault inside the ranker gives the "no hits" answer plus
  `"reason": "index or fetch failed; see the store log"` (section 4).

### 4.3 `GET /api/memories/index`

Query: `q` (required), `project` (optional), `top_k` (int, default 35,
clamp 1..200, a non-number reads as 35), `include_mined` (new; `0` or `1`,
default `0`).

```json
{
  "namespace": "claude_code",
  "reason": null,
  "mode": "hybrid",
  "model": "BAAI/bge-small-en-v1.5",
  "results": [
    {
      "rank": 1,
      "id": 412,
      "title": "Run the tests with the venv python",
      "summary": "The system python has no pytest; use the project venv.",
      "score": 0.7134,
      "fusion_score": 0.032787,
      "source": "feedback_tests_use_venv.md",
      "source_type": "claude_code_md",
      "trust": 0.6211,
      "trials": 9,
      "trust_prior": 0.5
    }
  ]
}
```

Field rules:

- `rank`: 1-based position.
- `id`: integer memory row id. Durable (section 6.2), unlike the reference
  implementation, where it was a handle into a capped RAM pool.
- `title`: the first non-empty line that is not a source marker, with a
  leading `# ` removed. Redacted, whitespace collapsed, cut to 120
  characters with `…` at the cut. Empty title: `"memory <id>"`. For a
  memory file this is the frontmatter `name`, because the hooks join index
  rows to local files by this title.
- `summary`: the next non-empty line, same rules, cut to 180 characters.
  May be `""`.
- `source`: the memory file path relative to its memory folder, from the
  source marker (section 7.3). `null` when the row has none.
- `score`: weight times cosine, rounded to 4 places, range -1..1. In
  keyword-only mode it is `null` (section 8.4). The hooks use `score` for
  floors (0.68 for search hits, 0.70 for error recall) and as the cosine
  order they fuse with their own BM25.
- `fusion_score`: the reciprocal rank fusion score, rounded to 6 places.
  It compares rows inside one answer only. No hook reads it today.
- `source_type` (new): `claude_code_md` or `transcript_mined`.
- `mode` (new): `hybrid` or `keyword` (section 8.4).
- `model` (top level, NOBLIVION-48): the embedding model of the cosine
  leg, by its plain name (the value of `embedding.model`, with no backend
  in front). `null` while the store has no usable model, and in every
  answer whose `mode` is `keyword`: also when only this query was ranked
  by keyword, because its embedding ran out of time. The prompt hook
  applies its default index floor only when this is the model that the
  floor was measured for (section 18). An answer with no `model` field
  comes from a store of an older version: the hook then applies no default
  floor.
- `trust_ranking` (top level, NOBLIVION-22): `shadow` or `on`, present
  only when the config key `trust.ranking` has that value. The hook reads
  it to keep the order in `shadow` (section 8.6).
- `trust`, `trials`, `trust_prior`: present only when the config key
  `trust.ranking` is `shadow` or `on`. `trust_prior` is the row's own
  prior: 0.5 for `claude_code_md`, 0.3 for `transcript_mined` (section
  9.2). A row with no rollup row has `trust: null` and `trials: null`. The
  hook reads a null trust as factor 1.0.
- `reason`: `null` on a normal answer. Otherwise one sentence, and
  `results` is `[]`: `"No memories available."` for an empty query or
  pool; the fixed error reason from section 4 on an internal error.

`include_mined=1` adds rows with `source_type = 'transcript_mined'`. The
prompt recall hook never sends it (section 11).

### 4.4 `GET /api/memories/fetch/{id}`

Path: `id` (the decimal row id). Query: `project` (optional).

Found:

```json
{"namespace": "claude_code", "id": 412, "title": "Run the tests with the venv python",
 "source": "feedback_tests_use_venv.md", "text": "<full redacted content>", "reason": null}
```

Not found or archived:

```json
{"namespace": "claude_code", "id": 412, "title": "", "source": null, "text": "",
 "reason": "no memory with that id in this namespace"}
```

Id not a number: same shape with `"id": "<as sent>"` and
`"reason": "id is not a number"`.

- The fetch is limited to the namespace, as in the reference
  implementation. An id from another namespace is "not found".
- The answer stays below 512 KB. A longer `text` is cut, with `…` at the
  cut.
- A fetch is the point where a memory counts as read. The store does not
  write a trust event for it; the trust hooks do that (section 9).
- A fetch can return a `transcript_mined` row. That is the "on request"
  path for mined rows.

### 4.5 `POST /api/memory/feedback/batch`

Body (max 256 KB, max 500 events):

```json
{
  "session_id": "6f1c2a8e-0b7d-4c55-9a51-3e2f0d9c7b10",
  "events": [
    {"kind": "recall", "mv_id": 412, "ts": "2026-10-03T09:15:02Z"},
    {"kind": "use", "path": "feedback_tests_use_venv.md", "ts": "2026-10-03T09:16:40Z"}
  ]
}
```

Validation, the same as the reference implementation:

- The body must be a JSON object. Else 400 `body is not valid JSON` or
  `body must be a JSON object`.
- `session_id`: 1-128 characters, full match of
  `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Else 400.
- `events`: a list. Else 400. More than 500: 413.
- A `persona` or `project` key in the body or query is ignored. (The
  reference implementation refused a benchmark persona with 403. v0.1 has
  no benchmark, so the rule is dropped.)
- `root` (new, optional, string): the memory folder key used to resolve
  `path` events (section 9.2).
- Per event, a bad event is counted in `rejected` and skipped:
  - `kind` is `recall` or `use`;
  - exactly one of `mv_id` and `path`;
  - `mv_id`: an int or an ASCII digit string, 1..2147483647, not a bool;
  - `path`: relative, ends in `.md`, max 512 characters, no `\`, no NUL,
    no empty, `.` or `..` part; leading `./` removed;
  - `ts`: ISO-8601, optional (missing means server time). No offset means
    UTC. Refused when more than 1 day in the future or more than 80 days
    in the past. A future time within 1 day is clamped to now.

Answer 200:

```json
{"inserted": 2, "duplicate": 0, "unknown": 0, "rejected": 0}
```

- `unknown`: the id or path names no live row.
- `duplicate`: a second event for the same (session, memory, kind), in the
  batch or already stored.
- Errors: 400, 403, 413 with `{"detail": ...}`. A store failure: 503
  `{"detail": "memory feedback store failed"}`. Nothing is half written.
- `sources` (optional, on an event): the hooks send the local source of an
  event (`index`, `fetch`, `guard_deny`, `guard_override`). v0.1 stores no
  sources, so the store ignores the key and never rejects an event for it.
- E5 implements this route in `noblivion/trust.py` (`parse_batch`,
  `store_batch`).

### 4.6 `GET /api/memory/trust/report`

Query: `persona` (default `claude_code`). Any other value: 400
`{"detail": "the trust report serves persona claude_code only"}`.

```json
{
  "persona": "claude_code",
  "generated_at": "2026-10-03T09:20:00Z",
  "trust_prior": 0.5,
  "retire":  [{"mv_id": 1048653, "root": "-work-proj-demo", "path": "old_note.md",
               "trust": 0.5, "trials": 0, "shown_sessions": 20, "use_sessions": 0,
               "reason": "shown in 20 sessions, used in none; first indexed 41 days ago"}],
  "promote": [],
  "demote":  [],
  "truncated": false
}
```

Rules (unchanged from the reference implementation):

- Skip index files: category `index` or `topic`, or a file name that
  starts with `MEMORY` or `topic_`.
- demote: a file that `MEMORY.md` links to, with no use in the last 30
  days. The list stays empty until the event history spans 30 days.
- retire: not linked from `MEMORY.md`, shown in at least 20 sessions, used
  in none, first indexed at least 30 days ago.
- promote: not linked, trust at least 0.7, trials at least 5, used in at
  least 20% of the sessions since its first event, first event at least 14
  days ago.
- Sort: retire by shown sessions (desc) then path; promote by trust (desc),
  use sessions (desc), path; demote by path.
- Trust is computed on read from the events (section 9.3), so the report
  never waits for a recompute.
- Only live `claude_code_md` rows are in the report: not mined, not
  archived, not deleted (section 5.4).
- Each row also carries `root` (new): the memory folder key (section 5.1),
  because `path` alone is not unique across folders.
- "Linked from `MEMORY.md`" means linked from the `MEMORY.md` of the same
  root.
- Each list holds at most 500 rows. `truncated` (new) is true when a list
  was cut. This keeps the answer far below the 1 MiB client cap.
- A `recall` event is not a trial (it is not citation-capable). A
  `contradict` event is a trial and lowers trust, also below trust_0
  (NOBLIVION-34). Retire and demote rest on the session counts, not on
  trust. Each row also carries `contradict_sessions`.
- Store failure: 503 `{"detail": "trust report unavailable"}`.
- `generated_at` is UTC with whole seconds and a `Z`.

### 4.7 `GET /health` (also `/api/health`)

Without a token:

```json
{"status": "ok", "service": "noblivion", "version": "0.1.0", "proof": "<64 hex>"}
```

`proof` is present only when the request has `nonce` (section 3.3) of 32
to 128 hex characters. A wrong token gets the short form, not 401.

With a valid token:

```json
{
  "status": "ok",
  "service": "noblivion",
  "version": "0.1.0",
  "uptime_s": 312,
  "memories": 214,
  "schema_version": 1,
  "content_rev": 1093,
  "vector_rev": 1088,
  "embedding": {"backend": "fastembed", "model": "BAAI/bge-small-en-v1.5",
                "dim": 384, "state": "ready", "missing_vectors": 0,
                "consent": "not_needed"},
  "mode": "hybrid",
  "trust_ranking": "shadow",
  "index_state": "idle",
  "index_blocked": false
}
```

- `status` is `ok`, or `degraded` when the model failed to load. A degraded store still answers every route.
- `embedding.state`: `loading`, `ready`, `reembedding`, `failed`, `off`.
- `embedding.consent`: `not_needed` for a backend that keeps text on the
  machine, else `given` or `missing` (section 7.1). The store reads it from
  `meta` for each health answer.
- The answer never holds a path, a user name or memory text.

### 4.8 Routes the store does not serve

Every other path answers 404 `{"detail": "not found"}`. The store has no
write route for memory content. Memory rows come only from the indexer and
the miner. This keeps the HTTP surface read-mostly.

## 5. Indexer

The indexer copies memory files into `memories` rows. The memory files stay
the source of truth. The indexer never writes a memory file.

### 5.1 What it scans

- Config `memory_dirs`: a list of folders. Default: every folder that
  matches `~/.claude/projects/*/memory`.
- In each folder: only top-level `*.md` files, sorted by name. It skips
  `.archive/`, backup files, and files that are empty or only whitespace.
- A file larger than 256 KB is stored by its head: up to its last line end
  inside 256 KB, or up to its last space when the second half of the head
  holds no line end. So a row fits in a search answer, and the redactor
  time stays bounded. The hash stays the hash of the whole file.
- `root` is the key of the folder: the name of its parent folder (the
  Claude Code project folder name). `path` is the file name relative to the
  folder.
- All folders go into one namespace, the default `claude_code`, but recall
  is scoped by root. The hooks derive the root of the session from its
  working dir the same way Claude Code names its project folders, and send
  it as `root` (section 4). So one project's rules are not injected into
  another project, and a file name that exists in two folders cannot pull
  the wrong file's rule. `recall.shared_roots` (default empty) lists roots
  that every session sees.
- A `path` trust event resolves in the `root` of its batch. Without a
  `root`, it resolves only when exactly one live row has that path; else it
  counts as `unknown`. An `mv_id` event is never ambiguous.

### 5.2 Kind and frontmatter

- `MEMORY.md` and `MEMORY_ARCHIVE.md` get category `index`.
- Else the file name prefix before the first `_` gives the category when
  it is one of `feedback`, `project`, `reference`, `topic`, `user`.
- Else the frontmatter `type` (or `metadata.type`) gives it when it is one
  of those words. Else the category is `reference`.
- Frontmatter is parsed by a small hand-written parser, not a YAML library:
  top-level `key: value` lines and one level of nesting, quotes stripped.
  A file with no closing `---` is all body.
- The indexer reads `name` (default: the file stem), `description` and
  `type`. Other keys (`rule`, `apply`, `scope`, `triggers`) are for the
  guard hooks, which read the files directly.
- Labels come from the label rules of the reference hooks (ported in E2a
  without the host alias map). A label that the redactor would change is
  dropped. Index and topic files get no labels.
- The rules stay in one file, `hooks/memory_labels.py`, because the hooks
  are stdlib and run without the package. `noblivion.labels` loads that
  file by path (`${CLAUDE_PLUGIN_ROOT}/hooks`, else the `hooks` folder next
  to the package or the source tree) and passes the store config's
  `labels.ticket_prefixes` and `labels.service_prefixes`. So a row holds
  the labels that the guard table holds for the same file. No hook imports
  the package. When the file is not found, `noblivion index` warns and
  stores no labels.

### 5.3 Content format

The stored `content` is these parts joined by one blank line:

```
# <name>
<description>
[claude_code_md: <path>]
<body>
```

The hooks parse this format (section 4.2), so it is fixed. An empty
description is left out with its blank line.

### 5.4 Change detection and writes

- `hash` is the sha256 of the raw file bytes. Redaction does not change the
  hash. So `meta` records the redactor version that wrote the rows. A scan
  reads the rows of one namespace, so the mark is per namespace (key
  `redactor_version:<namespace>`). When a scan finds a different redactor
  version, it writes every live row of the namespace again as if `--force`
  were given, and sets the mark.
- A row that this scan cannot write again keeps its old text: its file is
  not readable or not redactable, or its folder is gone. The id of such a
  row goes on the redo list of the namespace (key
  `redactor_redo:<namespace>`). A later scan writes only the rows on the
  list again, each when its file can be read. So one bad file does not make
  every scan write all rows again.
- A database of an older release holds one mark for all namespaces (key
  `redactor_version`). A namespace with no mark of its own reads that mark.
  The indexer does not write it.
- A store process that started before an update still runs the older
  release. It finds its own version in the single mark, so it indexes a new
  or changed note with its old rules, and the hash of that row is then the
  hash of the file. So each scan keeps the content revision of its last
  write (key `redactor_rev:<namespace>`), and the next scan writes a live
  row with a later revision again. Only such rows: the indexer leaves the
  single mark as it is, because an older release that finds another version
  there writes all its rows again with its old rules on each scan.
- New file: insert. Changed hash, or `--force`: update the row in place
  (same id). Same hash: skip.
- A new file whose hash equals the hash of a row deleted in the grace
  period (a rename, or a project folder that moved and changed its root):
  the indexer moves that row to the new root and path. The id and the
  trust history stay. A live row in a root whose folder is gone counts as
  a candidate too, because the empty-scan rule (section 5.5) never
  soft-deletes it. A live row whose file is gone in the same scan counts
  as well, so a rename inside one folder keeps its id in one scan.
- File gone: soft delete. The row gets `deleted_at` and leaves every pool
  at once. The maintenance pass (section 9.3) hard-deletes it after
  `index.delete_grace_days` (default 14). Only then does the cascade remove
  its vectors, events and rollup row.
- A file that comes back at the path of a soft-deleted row revives that
  row.
- A file moved by the dedup sweep (section 10) sets `archived_at`, not
  `deleted_at`, so an undo keeps the id and the trust history. A file that
  comes back at the path of an archived row un-archives it.
- All writes of one scan run in batches of 200 rows, each in one
  `BEGIN IMMEDIATE` transaction that also bumps `meta.content_rev` and
  stamps the changed rows (section 6.3).
- The indexer writes no vectors. The embed backfill of the store fills
  them (section 7.3).

### 5.5 Guards

- Shrink guard, per root and in total: if one scan would delete more than
  10% of the live `claude_code_md` rows of a root, or of all roots, it
  deletes nothing and logs the count. A root that holds fewer than 10 rows
  uses a fixed limit of 1 delete per scan. The store
  reports `"index_blocked": true` in `/health`. The user runs
  `noblivion index --allow-shrink` to accept. The guard is off when there
  are 0 live rows.
- Empty scan: if a folder is missing or empty while its root has live
  rows, the scan deletes nothing in that root. A missing folder is far more often a
  mount or a config fault than a user who deleted every memory.
- Skipped file: if a file cannot be read, or the redactor returns its fail
  token for it, that file is skipped and logged, and the scan deletes
  nothing in the root of that file in that run. The deletes of the other
  roots run. The CLI and the store log name the file that holds back the
  deletes of its root. A file whose text cannot be redacted safely is never
  stored.

### 5.6 When it runs

- At store start, in a background thread. The store answers from the
  rows already in the database while the scan runs (section 3.2).
- Every `index.interval_s` seconds (default 30) while the store runs. The
  scan is stat-only (size and mtime) until a file differs, so it is cheap.
  A file whose mtime is less than 2 s before the scan is not cached: a
  rewrite with the same size in one tick of a coarse file system clock
  keeps the mtime, and the cache would hide it (E3d).
- On demand: `noblivion index [--force] [--allow-shrink]`, also
  `python -m noblivion.indexer`. Exit codes: 0 done; 1 a file was skipped
  (not readable or not redactable), so nothing was deleted in its root;
  2 the shrink guard blocked the deletes; 3 the schema refuses the CLI
  (section 6.6); 4 another scan holds `index.lock`.
- One scan at a time: the store holds an in-process lock, and the CLI holds
  `flock` on `index.lock` in the data dir. The store takes the same flock.

### 5.7 Redaction

The redactor is a port of the reference at-rest redactor, stdlib only. It
covers private key blocks (also the PGP form), auth headers (Basic, Bearer,
JWT), API key shapes (`sk-`, GitHub token prefixes, Slack `xox`, chat bot
tokens, cloud access key ids, and the token prefixes `glpat-`, `AIza`,
`sk_live_`, `rk_live_`, `hf_` and `npm_`), the text of an XML `<password>`
element, CLI `key=value` and `--password=` forms, secret field names (also
with the key in quotes, as in JSON), the password in a connection URL,
cluster secret data and email addresses. It runs up to 5 passes until the
text is stable. If it does not settle or raises, it returns its fail
token. It runs before the content is stored, embedded, or sent anywhere.
The connection URL rule starts at each `://` and the email rule at each
`@`, so a long run of letters or digits is read once, not once for each of
its characters (NOBLIVION-62).

Three rules keep the text of a note that is not a secret:

- A line that only names the BEGIN line of a key block is prose. A block is
  masked when it has an END line, or when key text follows the BEGIN line.
  A block with no END line is masked up to the end of its key text, not up
  to the end of the note.
- `pass` and `pwd` are also words of prose and of a test report
  (`first_pass: complete`, `PASS: test_name`). They count as a key in front
  of `=` (`DB_PASS=value`) and in the JSON form (`"db_pass": "value"`). In
  front of `:` a key that ends in `pass` counts when its value looks like a
  secret: one word of 8 or more characters with a digit, a symbol inside
  it, or a capital letter after a small letter
  (`smtp-pass: hunter2hunter2`). A plain `pass` or `Pass` in front of `:`
  counts by the same rule at the start of a line, also after an indent
  (`  pass: hunter2hunter2`). `PASS` in capitals does not count there.
- After `:` the words `true`, `false` and `null` are not a secret. A number
  is a secret for a password key (`password: 12345678`) and a count for a
  key that ends in `token` (`"input_token": 12345678`).

The hooks have a redactor of their own for the text that they show to the
model (`hooks/memory_text.py`), because they run without the package. The
rules for a private key block, for the token prefixes and for the
`<password>` element are one table (`SHARED_SHAPES`). Each of the two files
holds a copy, and a test fails when the copies differ. A second test feeds
one list of secret forms and one list of note text (`tests/secret_forms.py`)
to both redactors, so a form that one of them misses, or note text that one
of them changes, fails the suite (NOBLIVION-46).

## 6. SQLite schema

### 6.1 Connection rules

Every connection, in the store and in every CLI tool, runs these first:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;      -- off by default in SQLite; cascades need it
PRAGMA busy_timeout = 5000;
PRAGMA trusted_schema = OFF;
```

- One connection per thread. No connection crosses a thread.
- Every transaction that reads a value and then writes based on it starts
  with `BEGIN IMMEDIATE`. That takes the write lock at the start, so no
  other writer can change the rows between the read and the write.
- Plain reads use `BEGIN` (deferred). In WAL mode they see one snapshot and
  never block a writer.
- A write transaction holds at most one batch (default 200 rows). Long
  jobs commit per batch, so a hook write never waits more than one batch.
- The switch of a new database to WAL takes an exclusive lock, and SQLite
  does not run the busy handler for it. `connect` retries
  `PRAGMA journal_mode = WAL` until `busy_timeout`, so the store and a CLI
  tool can open a new database at the same moment (E3d).
- No code opens and closes the database file outside SQLite. POSIX locks
  belong to the process, so closing any fd of the file drops the locks of
  every open connection; another process could then checkpoint and delete
  the WAL under them. `connect` creates a new file with `O_EXCL` only (E3d).
- `connect` opens an existing database only (SQLite `mode=rw`), unless the
  caller passes `create=True`. It never makes the folder then. The store
  connections per request, the jobs and the stop checkpoint never create
  (NOBLIVION-28).
- The data dir must be on a local file system. WAL needs shared memory
  that network file systems do not give. `doctor` warns on NFS and SMB.

### 6.2 DDL, schema version 1

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
-- keys: content_rev, vector_rev (counters, section 6.3), db_id (random,
--       set at create), embed_backend, embed_model, embed_dim,
--       redactor_version (section 5.4), created_at, last_maintenance_at,
--       dedup_consent and embed_consent (section 10.2)

CREATE TABLE memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused
    project     TEXT NOT NULL,                       -- namespace, default 'claude_code'
    root        TEXT NOT NULL,                       -- memory folder key, section 5.1
    path        TEXT NOT NULL,                       -- relative to root, section 5
    source_type TEXT NOT NULL
                CHECK (source_type IN ('claude_code_md', 'transcript_mined')),
    category    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,                       -- redacted text, with source marker
    hash        TEXT NOT NULL,                       -- sha256 hex of the raw file bytes
    weight      REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 4),
    pinned      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    labels      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels)),
    archived_at TEXT,                                -- set by dedup
    deleted_at  TEXT,                                -- file gone; purged after grace
    rev         INTEGER NOT NULL,                    -- content_rev of the last change
    created_at  TEXT NOT NULL,                       -- UTC ISO-8601
    updated_at  TEXT NOT NULL,
    UNIQUE (project, root, path)
) STRICT;
CREATE INDEX memories_live ON memories (project, root, source_type)
    WHERE archived_at IS NULL AND deleted_at IS NULL;
CREATE INDEX memories_rev ON memories (rev);
CREATE INDEX memories_hash ON memories (hash);

CREATE TABLE vectors (
    memory_id    INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL CHECK (dim > 0),
    content_hash TEXT NOT NULL,          -- sha256 of the text that was embedded
    blob         BLOB NOT NULL,          -- float32 little-endian, L2-normalised
    rev          INTEGER NOT NULL,       -- vector_rev of the last change
    CHECK (length(blob) = dim * 4),
    PRIMARY KEY (memory_id, model)
) STRICT;
CREATE INDEX vectors_rev ON vectors (rev);

CREATE TABLE feedback_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- ingest order
    session_id       TEXT NOT NULL,
    memory_id        INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL
                     CHECK (kind IN ('recall', 'use', 'load_bearing', 'contradiction')),
    citation_capable INTEGER NOT NULL CHECK (citation_capable IN (0, 1)),
    ts               TEXT NOT NULL,      -- event time from the client, UTC
    received_at      TEXT NOT NULL,      -- store time
    UNIQUE (session_id, memory_id, kind)
) STRICT;
CREATE INDEX feedback_events_memory ON feedback_events (memory_id, kind);
CREATE INDEX feedback_events_ts ON feedback_events (ts);

CREATE TABLE feedback (
    memory_id           INTEGER PRIMARY KEY REFERENCES memories (id) ON DELETE CASCADE,
    trust_0             REAL NOT NULL,
    trials              INTEGER NOT NULL DEFAULT 0,
    use_pos             REAL NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    trust_score         REAL NOT NULL,
    last_recalled_at    TEXT,
    last_used_at        TEXT,
    folded_event_id     INTEGER NOT NULL DEFAULT 0   -- highest feedback_events.id counted
) STRICT;

-- No foreign keys on purpose: the undo record must outlive the rows.
CREATE TABLE dedup_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    root         TEXT NOT NULL,
    kept_id      INTEGER NOT NULL,
    archived_id  INTEGER NOT NULL,
    kept_path    TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    archive_to   TEXT NOT NULL,          -- archive file path, relative to the memory folder
    status       TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'undone')),
    judge_model  TEXT NOT NULL,
    verdict      TEXT NOT NULL CHECK (json_valid(verdict)),
    created_at   TEXT NOT NULL,
    undone_at    TEXT
) STRICT;

CREATE TABLE dedup_vetoes (
    root       TEXT NOT NULL,
    path_a     TEXT NOT NULL,            -- path_a < path_b
    path_b     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (root, path_a, path_b)
) STRICT;

CREATE TABLE miner_state (
    transcript TEXT PRIMARY KEY,         -- path relative to the projects folder
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    offset     INTEGER NOT NULL,         -- bytes already mined
    mined_at   TEXT NOT NULL
) STRICT;

-- Ids start at a random base, so an id from an older database (a reset,
-- a reinstall) almost never names a row in a new one.
INSERT INTO sqlite_sequence (name, seq) VALUES ('memories', :random_base);
-- :random_base is a random integer in 1,000,000 .. 1,000,000,000.

PRAGMA user_version = 1;
```

Why each choice:

- `AUTOINCREMENT` on `memories.id` and `feedback_events.id`: an id is never
  reused after a delete in the same database. The random base covers a new
  database: a spooled event, or a `GROUNDED MEMORY <id>` line that the
  trust flush reads from an old transcript, then almost always finds
  "unknown", not a different memory. The ids stay below 2,147,483,647, the
  limit of the feedback route.
- `ON DELETE CASCADE` on vectors, events and the rollup row: a purged
  memory takes them with it. Dedup records have no foreign key, so an undo
  record survives. This designs out the orphan
  defect (section 9.4).
- `memories.id` stays the same when a file changes. The indexer updates the
  row in place. Trust follows the file, not one version of its text.
- Labels are a JSON column, not a table. Nothing joins on labels in v0.1.
  The indexer writes them; ranking does not read them.
- `STRICT` tables refuse wrong column types (SQLite 3.37 or newer, which
  the store venv provides).

### 6.3 Revision counters and the RAM index

The store keeps the BM25 index and the vector matrix in RAM. It must see
every change that another process (a CLI tool) commits. A clock is not
safe for this: commit order is not timestamp order. So the store uses
counters, as section 9.4 does for trust.

- `meta.content_rev` counts changes to `memories`. `meta.vector_rev`
  counts changes to `vectors`.
- Every write transaction to `memories` runs, in one `BEGIN IMMEDIATE`
  transaction: increment `content_rev`, read it, set `rev` to that value on
  every row it inserts, updates, soft-deletes, archives or revives. The
  same rule holds for `vectors` and `vector_rev`. A hard delete happens
  only to rows that are already soft-deleted or archived, so it changes no
  pool and needs no stamp.
- Before a search, the store reads both counters (one primary-key read).
  If one is higher than the value of its RAM index, one reload runs. The
  reload reads, in one read transaction (one snapshot), the counters and
  the rows `WHERE rev > <loaded value>`. It applies them to a copy of the
  index and swaps the copy in. A lock lets only one reload run at a time.
  Other requests keep using the old index during the reload.
- A `vector_rev` change patches the vector matrix only. The BM25 index is
  rebuilt only on a `content_rev` change. So a re-embed does not rebuild
  BM25 on every search.
- This replaces the content fingerprint of the reference implementation.

### 6.4 Data volumes

Planned for up to 50,000 memory rows. At 384 dims float32 that is 77 MB of
vectors in RAM. A typical user has under 2,000 rows (3 MB).

### 6.5 File permissions

The store creates the database with umask 077. `doctor` checks mode 0600 on
the database, WAL, SHM and token files, and mode 0700 on the data dir.

### 6.6 Migrations

- `PRAGMA user_version` holds the schema version. Migrations are a list of
  numbered SQL scripts in the package, applied in order.
- Before a migration the store copies the database with the SQLite backup
  API to `backups/noblivion-v<old>-<utc>.db`. It keeps the last 3 copies.
- Each migration runs in one `BEGIN IMMEDIATE` transaction together with
  its `PRAGMA user_version` change. A failed migration rolls back, and the
  store exits with code 3 and logs the step. The hooks fail open.
- A database with a `user_version` higher than the code knows is never
  opened for writes. The store exits with code 3 and logs "database is
  newer than this NOBLIVION". This protects against a downgrade.
- Only the store runs migrations. A CLI tool that finds an old schema
  refuses to run and asks the user to start the store once.
- A new, empty database at the latest version is made only by the store
  at start and by `noblivion index`, and only when the install stamp
  `venv/noblivion-install.json` exists (`install.sh` writes it before its
  first `noblivion index`), or for `index --db PATH`. So the indexer works
  before the store first runs. That is not a migration: no user data exists
  yet. Every other CLI tool (`mine`, `trust`, `dedup`, `consent`) opens an
  existing database only and exits with code 6 without one. A run that a
  hook starts after an uninstall therefore makes nothing (NOBLIVION-28).

## 7. Embedding backends

### 7.1 Backends

| Backend | Default | Model | Dim | Leaves the machine |
|---|---|---|---|---|
| `fastembed` | yes | `BAAI/bge-small-en-v1.5` (ONNX, CPU, about 130 MB) | 384 | model download only |
| `ollama` | no | config, e.g. `nomic-embed-text` | from model | no, if Ollama is local |
| `openrouter` | no | config | from model | yes: memory text and query text |
| `none` | no | none | none | no |

- `fastembed` is in the store venv. The installer downloads the model into
  `<data dir>/models/` once. The store never downloads at run time unless
  the config allows it (`embedding.allow_download`, default true only
  during install).
- `ollama`: `POST <ollama_url>/api/embed`. The URL must be loopback unless
  the user sets `embedding.allow_remote = true` and gives the same
  embeddings consent.
- `openrouter`: `POST https://openrouter.ai/api/v1/embeddings` with
  `Authorization: Bearer $OPENROUTER_API_KEY`. It sends every memory text
  and every query out, so it needs its own consent, `meta.embed_consent`,
  set only by the CLI command `noblivion consent embeddings` (the store
  runs detached and cannot ask). Without that consent the store does not
  start the backend: `embedding.state = failed`, keyword mode. The consent
  text says what leaves (section 10.2 rules apply).
- A backend that sends text out (`openrouter`, or `ollama` with a URL that
  is not loopback) reads the consent again before each remote call: each
  query embed, each backfill batch and each single-row retry. The jobs
  loop reads it on each tick too. This is one read of `meta`, about 2
  microseconds. A local backend does not read it. After
  `noblivion consent embeddings --revoke`, a running store sends nothing
  more and takes the state of a start without consent: `failed`, keyword
  mode. A call that is already on its way is not stopped. A query embed
  that still waits in the pool when its time limit ends is cancelled. The
  check fails closed: without a database connection the remote query call
  is not made.
  A consent read that fails with a database error (for example
  `database is locked`) is not a revoke. The store sends nothing for that
  call and keeps the state of the backend: the query gets a keyword-only
  answer, the jobs loop runs no backfill and starts no model on that tick,
  and the next call reads again. The log gets one line for each run of
  failed reads.
  While the backend is `failed`, the jobs loop asks the service on each
  tick if a new start can change the state: the retry time is over, or the
  consent is back. Only then it starts a model thread. So a store with no
  consent writes one log line for the revoke and then no more, and a new
  consent starts the backend within a tick.
- `none`: keyword-only ranking forever.

### 7.2 What is embedded

The redacted `content` of a row, without the source marker line, cut to
the model's input limit (512 tokens for the default model). The query is
embedded with the model's query prefix if the model defines one.

For a backend that leaves the machine, two more rules hold:

- The query is the user's prompt. It goes through the secret redactor and
  the outbound scrub (section 10.2) before it is sent. Memory text goes
  through the outbound scrub too.
- `transcript_mined` rows are not sent unless
  `embedding.remote_include_mined = true`. Without it they rank on BM25
  only. They hold tool output and third-party text, which the user never
  chose to share.

### 7.3 Model change and re-embed

- `meta` holds `embed_backend`, `embed_model` and `embed_dim`.
- At start, if the configured model differs from `meta`, the store sets
  `embedding.state = reembedding`. A background job embeds every live row
  with the new model in batches of 64 and writes `vectors` rows with the
  new `model` value. Rows keep their old vectors until the job ends.
- While re-embedding, the cosine leg uses only rows that have a vector for
  the new model. The other rows rank on BM25 only, with `score: null`. The
  answer `mode` is `hybrid` once the query itself is embedded and at least
  one pool row has a vector of the new model; with no such row the answer
  is keyword-only (section 8.4), because a cosine leg with no rows has
  nothing to fuse.
- At the end of a backfill pass, one transaction deletes vectors of other
  models and writes the new model to `meta`. A row that failed in the pass
  has no vector of either model and is retried by the next pass; its old
  vector is in another vector space and cannot be used anyway.
- `vectors.model` holds the model id `<backend>:<model>`, for example
  `fastembed:BAAI/bge-small-en-v1.5`, so two backends that use one model
  name never share vectors.
- A row whose `vectors.content_hash` differs from the hash of its current
  text is re-embedded by the same backfill job. The backfill also fills
  rows that a CLI tool inserted without a vector.

### 7.4 Failures

- Model load fails: `embedding.state = failed`, `status = degraded`,
  keyword-only ranking. The store retries the load at most once per hour.
  One exception: when the failure is a missing consent, the store starts
  the backend as soon as the consent is in `meta`, without that wait and
  without a restart.
  Each failed load writes one log line with the error type and the reason
  (secrets redacted, cut to 300 characters).
- A single embed call fails: that row stays without a vector and is
  retried by the next backfill pass.
- Query embed fails or takes more than 1 s: this answer is keyword-only
  (`mode: keyword`).

### 7.5 Packaging of the backends (E3b)

- numpy and fastembed are the optional extra `noblivion[embed]`. The store
  venv installs it. Without numpy, or without a usable model, every answer
  is keyword-only. BM25 and the backfill job need only the stdlib.
- The consent in `meta.embed_consent` is
  `{"version": 1, "provider": "<openrouter|ollama>", "model": "<model>",
  "at": "<utc>"}`. A change of provider, model or consent text version
  needs a new consent, as in section 10.2. `noblivion consent embeddings`
  prints the text and writes the record only on the answer `yes`;
  `--revoke` removes it.

## 8. Ranking

### 8.1 Pool

The pool for a query is the live rows (`archived_at IS NULL AND
deleted_at IS NULL`) of the namespace, limited to the request `root` plus
`recall.shared_roots` when the request has a `root`. `/search` and `/index` without `include_mined=1` also drop
`transcript_mined` rows. The pool filter runs before scoring, so a filtered
row cannot change the ranks of the others.

### 8.2 Legs

- BM25: the hand-written BM25 of the reference hooks, over the tokens of
  the redacted content: BM25 Okapi with `k1 = 1.5`, `b = 0.75`,
  `epsilon = 0.25` (a negative idf becomes `epsilon` times the mean idf).
  Built in RAM at load; rebuilt on a `content_rev` change (section 6.3).
  One BM25 index per pool, so the document frequencies are those of the
  pool. The tokenizer is the reference store tokenizer: lower case, split
  on every character outside `a-z 0-9 _ . - /`, keep tokens of 2 or more
  characters. The sub-token split of the reference hook re-rank stays in
  the hook (E4).
- Cosine: the query vector times the L2-normalised matrix of the pool
  (numpy, float32), times the row `weight`, quantized to 4 places.

### 8.3 Fusion

Reciprocal rank fusion with `k = 60`, as in the reference implementation:

1. Rank the rows that have a vector by weighted cosine (ties by id).
2. Rank the rows that match a query term by BM25 (ties by id). A row
   matches when it holds at least one token of the query. Skip this list
   when no row matches. A row that matches no query term is not in this
   list, as in keyword-only mode (section 8.4). The BM25 score cannot
   decide the match: a term in exactly half of the pool has an idf of 0,
   so a row that holds only this term has a score of 0. The reference
   implementation ranked the whole pool here, so the tie of zeros broke by
   id and the oldest rows got a fused score for a query they do not match
   (NOBLIVION-49). When the best BM25 score of the matched rows is below
   0, every matched term has a negative idf, as in a pool of two rows that
   both hold the term. The row with more uses of the term then has the
   lower score. So this list then holds only the matched rows that have no
   vector, and the cosine order of step 1 decides the rest.
3. Fused score per row: the sum over lists of `1 / (60 + rank)`. A row
   that is in no list is not returned: it has no vector and it matches no
   query term.
4. Walk the fused order. Drop a row when its cosine is below 0.2 AND its
   fused score is below `1 / (60 + n)` (n = pool size). Stop at `top_k`.
5. If nothing is left and the pool has rows, return the single best cosine
   row (empty fallback), with its cosine as `fusion_score`.

Note on step 4: every row with a fused score is in a list at a rank of at
most n, so its fused score is at least `1 / (60 + n)` and the floor never
drops a row. The reference implementation behaves the same way. The store
keeps the step for parity. Tests must not claim the floor filters anything.

A row with no vector (not embedded yet, or its embed failed) has a `null`
`score` in a hybrid answer. The hook floors keep a row with no score. After
step 2 such a row is in the answer only when it matches a query term, which
is the evidence that keyword-only mode accepts.

`score` in the answer is the weighted cosine. `fusion_score` is the fused
score (or the cosine in the empty fallback).

### 8.4 Keyword-only mode

Used when the backend is `none`, the model is not loaded yet, the model
failed, or the query embed failed.

- Rank by BM25 only. Drop the rows that match no query term: the rows
  that hold no token of the query. A matched row can have a BM25 score of
  0, because a term in exactly half of the pool has an idf of 0. In a pool
  of one or two rows a matched term can have a negative idf, so the score
  can be below 0 too. Such a row stays, after the rows with a higher score
  (NOBLIVION-49).
- `score` is `null`, because no cosine exists. A `0.0` would read as "no
  match" to a hook floor and drop every row. `fusion_score` is
  `1 / (60 + rank)`.
- `mode` is `keyword`. `/health` reports it.
- Hook rule (built in E4): when `mode` is `keyword`, the hooks skip their
  score floors and keep the rank order. The error recall hook uses its own
  local BM25 instead, as it does when the store is down.

### 8.5 Interface for the REST service (E3c)

Module `noblivion.ranking`, built in E3b:

- `Ranker(embedding_service).search(conn, query, *, project, top_k,
  root=None, shared_roots=(), include_mined=False) -> RankResult`. It
  refreshes the RAM index by the counters, embeds the query (limit 1 s)
  and ranks.
- `RankResult.mode` is `hybrid` or `keyword`. `RankResult.hits` is a list
  of `Hit(rank, row, score, fusion_score, bm25)`; `row` is a `PoolRow` with
  `id, project, root, path, source_type, content, weight`. `score` is
  `None` when the row has no cosine.
- `noblivion.embedding.EmbeddingService(load_embedding_settings())`: the
  store calls `start(conn)` at start and again for the hourly retry,
  `backfill(conn)` from its background job, and reads `state` and `error`
  for `/health`.

### 8.6 Trust in ranking

Config `trust.ranking`: `off` (default in v0.1), `shadow`, `on`. In
`shadow` and `on` the store adds the trust fields to `/index` rows and
names the mode in the answer (`trust_ranking`). The hook decides what to
do with them. With `NOBLIVION_RECALL_INDEX_TRUST` on, it multiplies its own
fused score by `clamp(trust / trust_prior, 0.8, 1.25)`, and uses factor 1.0
below 5 trials. In `shadow` (the store's mode, or the hook switch set to
`shadow`) it computes the factor and logs it but does not reorder: the
trust values are computed and reported, never applied. The store itself
never reorders by trust in v0.1.

Trust ranking stays off by default. Trust is usage evidence for the
report, not a measure of relevance. It turns on by default only after a
time-split test (fit on older events, test on newer events) shows a
ranking gain. That test does not exist yet.

## 9. Trust

### 9.1 Events

- `recall`: the memory was shown to the model (injected or listed).
  `citation_capable = 0`.
- `use`: the model opened the memory with the MCP tool, or the guard
  denied a command by the memory's rule. `citation_capable = 1`. A guard
  that only shows a rule (rows, labels) sends no event (NOBLIVION-34).
- `contradict`: the model overrode a guard deny of the memory's rule with
  the `# guard-ok:` marker. The store keeps it as kind `contradiction`.
  `citation_capable = 1`, so it is a trial. The guard cannot know at deny
  time whether a deny will be overridden, so it sends the deny as `use`
  and the override as `contradict`.
- A user correction on the topic of a recalled memory is not a
  `contradict` event in v0.1: the correction detector does not know which
  memory a correction is about.
- `load_bearing` exists in the schema and the formula but no v0.1 client
  sends it. The REST route accepts `recall`, `use` and `contradict`.
- The event id is the session id. One session counts at most once per
  memory and kind.

### 9.2 Ingest

One `BEGIN IMMEDIATE` transaction per batch:

1. Resolve each `mv_id` to a live row (`archived_at IS NULL AND
   deleted_at IS NULL`) and each `path` to a live `claude_code_md` row by
   the root rule of section 5.1. Unresolved: `unknown`.
2. Keep the earliest `ts` per (memory, kind) inside the batch. Repeats in
   the batch: `duplicate`.
3. `INSERT ... ON CONFLICT (session_id, memory_id, kind) DO NOTHING
   RETURNING id`. Conflicts: `duplicate`.
4. For every memory that got a new event, recompute its rollup row from its
   events (section 9.3) in the same transaction.
5. `COMMIT`.

The trust_0 of a new rollup row: 0.5 for `claude_code_md`, 0.3 for
`transcript_mined` (config `trust.prior_mined`).

### 9.3 Formula and recompute

```
trials   = count of distinct sessions with any event that is citation_capable
           or of kind use or load_bearing
use_pos  = count of distinct sessions with kind use or load_bearing
contra   = count of distinct sessions with kind contradiction
u_eff    = max(0, use_pos - 2 * contra)
trust    = clamp((10 * trust_0 + u_eff) / (10 + trials), 0, 1)
```

With no events, trust equals trust_0. A contradiction session is a trial
and takes 2 off `u_eff`, so it lowers trust, also below trust_0. The
ranking multiplier a hook may use
is `0.5 + 0.5 * trust`.

Recompute is one function, `recompute(conn, memory_ids)`. It reads the
event counts and writes the rollup row, and it records
`folded_event_id = max(feedback_events.id)` of the events it counted. It is
called:

- by ingest, in the ingest transaction, for the touched rows;
- by the store maintenance pass, for every row where an event with
  `id > folded_event_id` exists (repair after a crash or a CLI import),
  in batches of 200, each batch in one `BEGIN IMMEDIATE` transaction;
- by `noblivion trust recompute`, same as at start (E5: the CLI runs the
  repair only; the store runs the full maintenance pass).

There is no nightly trust job in v0.1. Ingest keeps the rollup current,
and the start pass repairs anything left.

The store runs one maintenance pass at start and then every 24 hours while
it runs (`meta.last_maintenance_at`). It does, each step in batches of 200
under `BEGIN IMMEDIATE`:

- the trust repair above;
- hard delete of rows soft-deleted more than `index.delete_grace_days` ago;
- hard delete of rows archived more than `dedup.archive_retention_days`
  ago, when their `dedup_actions` row is `done` and not undone;
- `PRAGMA foreign_key_check` (section 9.4);
- pruning of `backups/` to the last 3 copies.

E5 runs the pass in the store's background job thread: once at start,
before the first index scan, then every 24 hours of store uptime. A failed
step is logged and the next step still runs. The pass ends by writing
`meta.last_maintenance_at`.

### 9.4 The two defects of the reference implementation, designed out

Defect 1, recompute and ingest race. In the reference implementation the
nightly recompute read the events, then wrote `updated_at = NOW()` in a
separate step. An ingest that committed in between was hidden: its event
was older than the new `updated_at`, so the stale scan never saw it.

The store removes the race in three ways:

- Read and write happen in one `BEGIN IMMEDIATE` transaction. SQLite has
  one writer, so no ingest can commit between the read and the write.
- The stale mark is the event id watermark `folded_event_id`, not a clock.
  A late or backdated event always has a higher id than any event already
  counted, so it is always found.
- Ingest recomputes in its own transaction, so a separate pass is only a
  repair.

Defect 2, orphan feedback rows. In the reference implementation the trust
key was a text `"MV-<id>"` with no foreign key. A deleted memory left its
trust rows, and the report counted them.

The store removes it in two ways:

- `feedback_events.memory_id` and `feedback.memory_id` are integer foreign
  keys with `ON DELETE CASCADE`, and every connection sets
  `PRAGMA foreign_keys = ON`.
- `doctor` and the store maintenance pass run `PRAGMA foreign_key_check`
  and log any row it finds. A test asserts zero orphans after delete paths.

### 9.5 Report

`GET /api/memory/trust/report` (section 4.6) reads in one deferred read
transaction, so it sees one snapshot. It computes trust on read with the
same formula.

## 10. Dedup sweep

The sweep finds memory files that say the same thing and merges them. It
is off by default. It is the only v0.1 feature that sends memory text to a
third party, so it needs consent.

### 10.1 Flow

The dedup CLI is the one writer of every dedup step. It uses the same
library and connection rules as the store, and it holds `index.lock` so
the indexer never sees a half-done merge.

1. `noblivion dedup plan`: the CLI reads the vectors from the database
   and lists candidate pairs in one root. A pair needs
   cosine of at least `dedup.min_cosine` (default 0.75, see section 18) and
   the same category prefix (`feedback`, `project`, `reference`, `user`).
   Index and topic files are never candidates. Files changed in the last 30
   minutes are skipped. Pairs in `dedup_vetoes` are skipped.
2. For each pair the judge returns one verdict (section 10.3). The plan
   writes `dedup/plan-<run_id>.json` in the data dir with every pair, the
   verdict and the reason. It changes nothing. At most `dedup.max_pairs`
   (default 50) pairs, best cosine first, are judged per run. Before the
   first call the CLI prints the number of calls and an estimate of the
   tokens (and of the cost in USD when `dedup.price_in_per_mtok` and
   `dedup.price_out_per_mtok` are set); after the run it prints the tokens
   used and the cost OpenRouter reports. After the estimate the CLI asks the
   user to type `yes` before the first billed call; `plan --yes` skips the
   question. `plan --dry-run` (NOBLIVION-22) prints the pairs and the
   estimate only: it builds no judge, makes no network call, needs no
   consent and writes no plan file.
   Every `plan`, `apply` and `undo` writes `cache/dedup-last-run.json` in
   the data dir (`NOBLIVION_DEDUP_STATUS_FILE`) for the session start line.
3. The user reads the plan. `noblivion dedup apply <run_id>` applies only
   the `MERGE` pairs in that plan file, and only when both files still have
   the hash recorded in the plan.
4. At most 30 merged-away files per run.

Commands (E6, `src/noblivion/dedup.py`): `noblivion dedup pairs` (list the
candidates; sends nothing, needs no consent), `plan [--dry-run] [--yes]
[--max-pairs N]`, `apply <run_id>`, `undo <run_id> [--pair A,B]`,
`consent [--revoke]` and `clear-latch`. Only `plan` calls the judge, so
only `plan` needs the model, the key and the consent. The candidate search
uses the vectors of the configured embedding model and numpy (the store
venv has both).

There is no unattended apply mode in v0.1. The reference implementation
gated unattended apply on a precision test against a private gold set. No
gold set ships with this plugin, so a human reads every plan.

### 10.2 Consent and what leaves the machine

- `dedup.judge` is `openrouter` (default) or `off`. Dedup is still off by
  default: the plugin ships no model (section 19), and `plan` refuses and
  sends nothing until `dedup.model`, `OPENROUTER_API_KEY` and the consent
  are all set. `off` is a switch that refuses even then. An Ollama judge is
  not in v0.1 (any other value counts as `off`). Example model, for the docs
  only: `google/gemini-2.5-flash`.
- The first `plan` run prints the consent text and needs
  the user to type `yes` (or the user runs `noblivion dedup consent`
  first). The CLI writes
  `meta.dedup_consent = {"version": 2, "provider": "openrouter",
  "model": "<model>", "at": "<utc>"}`. A change of provider, model or
  consent text version asks again.
- Sent per pair: the frozen judge prompt, the kind of the pair
  (`feedback`, `project`, `reference` or `user`) and the two file texts.
  A text longer than 20,000 characters after the scrub is not cut and sent:
  the pair is `UNSURE` and nothing is sent for it. A text the redactor
  cannot settle is not sent either. File names are replaced by `A` and
  `B`, but the file text includes its front matter, so `name` and
  `description` are sent as written; consent text version 2 says so. No
  trust data and no other memory is sent.
- Before sending, each text goes through the secret redactor and then an
  outbound scrub: the home folder path becomes `~`, the user name becomes
  `<user>`, IPv4 and IPv6 literals become `<ip>`, and email addresses are
  already redacted. Host names and project names inside the prose cannot
  be found reliably and are sent as written. The consent text says so in
  these words.
- Receiver: OpenRouter and the model provider it routes to. Their data
  policies apply. The request sets OpenRouter provider preferences
  `{"data_collection": "deny"}` so it routes only to providers that do not
  keep or train on prompts.
- The key comes only from the env var `OPENROUTER_API_KEY`. NOBLIVION never
  writes it to disk and never logs it.

### 10.3 Judge contract

- Request: OpenRouter chat completions over `urllib`, Bearer key,
  temperature 0, JSON output (`response_format: json_object`), at most 400
  output tokens, `usage.include` so the answer carries the cost. Timeout
  `dedup.timeout_s` (default 60). Rate limit: at least
  `dedup.min_interval_s` (default 1.0) between calls; HTTP 429 waits for
  `Retry-After` (at most 60 s) and retries twice.
- The judge is an interface (`model` plus `judge(system, user) -> Verdict`);
  the tests use a fake judge and never reach the network.
- Answer schema:
  `{"verdict": "MERGE|RELATED|CONTRADICT|SUPERSEDE|UNSURE", "survivor": "A|B|none", "reason": "<text>"}`.
- A pair whose input was cut, an answer that does not parse, a timeout or
  an HTTP error all count as `UNSURE`. Only `MERGE` merges.
- The prompt is a versioned file in the package
  (`src/noblivion/dedup_prompt_v1.txt`, adapted from the reference prompt
  v3), checked by sha256 at load.
- The judge answer is data. It is parsed against the schema and never run
  or followed as an instruction.

### 10.4 Apply, archive and undo

- Merge: append to the survivor file a block "Merged from <loser> on
  <date>:", then the loser description, rule and apply lines as quotes,
  then the loser body. `triggers` become an ordered union. Links to the
  loser in other files point to the survivor. Index lines that point only
  to the loser are removed when the index already points to the survivor.
- Survivor: the judge's `survivor`; for `none`, the file with more inbound
  links, then the first name.
- The merge refuses a pair (and skips it) when `scope`, `status` or
  `project` differ, when both files hold different `violates` groups, when
  the trigger union has more than 12 items, or when the merged file fails
  the field check with a new problem. It uses `hooks/memory_fields.py`.
- Before any write: a write-ahead log entry in
  `dedup/<run_id>/wal.jsonl` and a `tar.gz` backup of every file the step
  touches.
- Order of one merge step, so a crash leaves a state the next run can
  finish or undo:
  1. One transaction: insert the `dedup_actions` row with status
     `pending`, set `archived_at` on the loser row.
  2. Write the survivor file, then move the loser file to
     `<memory folder>/.archive/dedup/<run_id>/`.
  3. One transaction: set the action to `done`.
  If step 2 fails, the CLI restores the survivor from the backup, clears
  `archived_at`, and sets the action to `failed`. A `pending` action found
  at the next `apply` or `undo` is rolled back by the same rule, using the
  write-ahead log and the backup.
- `apply` also writes `dedup/<run_id>/undo.sh`, which runs
  `noblivion dedup undo <run_id>`. The write-ahead log holds names and
  hashes only, never file text or the judge reason. The indexer never deletes a row whose loser has a
  `pending` or `done` action; it only keeps it archived.
- The row and its trust history stay while archived.
- `noblivion dedup undo <run_id> [--pair A,B]` undoes newest step first. It
  restores a file only if its sha256 equals the hash logged after the
  step; else it stops and names the file. An undone pair is written to
  `dedup_vetoes` and is never proposed again. The action becomes
  `undone`.
- A failed apply sets a latch file. Later runs refuse until the user runs
  `undo` or `noblivion dedup clear-latch`.
- Archived rows older than `dedup.archive_retention_days` (default 90) are
  hard-deleted by the store maintenance pass (section 9.3). The cascade
  removes their trust rows. The `dedup_actions` row stays for the record.

## 11. Transcript miner

The miner turns past Claude Code sessions into low-trust memory rows. It
runs only on the local machine.

### 11.1 Input and output

- Reads `~/.claude/projects/*/*.jsonl` (config `miner.transcript_glob`).
- JSON fields read: `type`, `message.content` (text, tool use and tool
  result blocks), `sessionId`, `timestamp`, `isMeta`, `isSidechain`,
  `tool_use_id`, `is_error`.
- Kinds it extracts, at most one per transcript line, in this order:
  - `correction`: a user turn (not meta, not a sidechain, not harness
    text) that the correction test accepts. The test is `is_correction` in
    `hooks/stop_checks.py`: the stop checks, the trust signals and the
    miner share it, so one rule says what a correction is. The miner loads
    the file from the plugin's `hooks/` folder (section 13.2). When the
    file does not load, the miner stores no correction. Stored: the turn
    (max 600 characters) and the assistant text before it (max 400).
  - `review_request_changes`: the result of a reviewer agent call (the
    `Agent` or `Task` tool) that holds a review verdict of "request
    changes" and a findings block. Max 1500 characters. A result of any
    other tool (a web fetch, a file read, a shell command) gives no
    review, whatever its text. A result with no known tool call gives
    none either.
  - `tool_error`: a tool result with `is_error`, paired with its tool call.
    Stored: tool name, command (max 300), error (max 400).
- Row: `source_type = 'transcript_mined'`, `category = <kind>`,
  `root = <transcript project folder name>`,
  `path = <session_id>#<line>`, no source marker, trust prior 0.3.
  `<session_id>` is the transcript file name without `.jsonl` (Claude Code
  names the file after the session; a resumed session can carry an older
  `sessionId` in its lines, the file name is unique). `<line>` is the
  1-based line number in the file.
- Content: `# <title>` (for example `# Tool error: Bash` and the command), an
  empty line, then one paragraph with the date, the first 8 characters of
  the session id and the extracted text.
- `hash` is the sha256 of the kind and the dedupe shape: the tool name and
  the first error line for a tool error, the findings block for a review,
  the stored text for a correction. All are taken after redaction.
- The same tool error shape and the same findings block are stored once.
  The check reads `hash`, so it holds across runs, not only within one.
- API records: Claude Code writes one assistant API message as several
  lines with the same `message.id` (one content block per line, or a
  repeat while it streams). The miner merges each run of such lines into
  one record: the line with the largest `usage.output_tokens` is the base,
  and the content blocks of all the lines are joined, each block once.
  Keeping only one line would lose the tool calls in the other lines.

### 11.2 Redaction and idempotence

- Redact at extract and again before insert: secrets
  (`redact_at_rest`, section 5.7) and then injection patterns
  (`redact_injection`, section 15.4). A fail token skips the candidate.
  Nothing unredacted reaches the database or the embedder.
- `miner_state` keeps the byte offset per transcript. A run reads from the
  offset. If a file shrank or its mtime moved back, the miner reads it from
  the start; the unique `(project, root, path)` key makes a re-read safe.
  A file with the same size and mtime as its stored offset is not opened.
- On a resume the miner replays the assistant lines of the last 4 MiB
  before the offset, without output, so a tool error after the offset is
  still paired with its call and a correction keeps its context. It counts
  the newlines before the offset to get the line number.
- A last line without a newline is a line still being written. It is left
  for the next run; the offset stops before it.

### 11.3 Who sees mined rows

- `/api/memories/search` never returns them.
- `/api/memories/index` returns them only with `include_mined=1`. The
  prompt recall hook and the subagent hook never send it.
- The MCP tool gets an `include_mined` argument (default false) and can
  fetch a mined row by id.
- They are not in the trust report.

This is a hard rule because transcripts hold text from tool outputs, web
pages and files, which may hold prompt injection (section 15).

### 11.4 When it runs

- `miner.enabled` (default `false`). The precision of the miner is not
  measured yet, so the user turns it on. When it is on, the `SessionEnd`
  hook starts `noblivion mine` detached, at most once per 10 minutes
  (stamp file).
- On demand: `noblivion mine [--since <date>]`. `--since YYYY-MM-DD`
  skips files with an older mtime.
- One run at a time: `flock` on `mine.lock`. A second run exits 4 at once.
- Bounded: a run stops after `miner.max_run_s` seconds (default 300) at a
  line end and keeps the offsets, so the next run goes on from there.
- The hook is `hooks/mine_session_end.py`. It runs
  `NOBLIVION_MINE_CMD` (a shell command line) when set, else
  `<data dir>/venv/bin/noblivion mine` when that file exists, else
  nothing. Its stamp file is `<data dir>/cache/mine.stamp`.
- `miner.enabled = false` (the default, or `NOBLIVION_MINER=0`): the hook
  starts nothing, and `noblivion mine` reads no transcript and opens no
  database.

## 12. Configuration

### 12.1 Sources and order

1. Env vars `NOBLIVION_*` (highest).
2. The config file: `NOBLIVION_CONFIG`, else `<data dir>/config.json`.
3. Built-in defaults.

The config file is JSON because the hooks must read it with stdlib
python 3.9. Unknown keys are a warning, not an error. A bad value is an
error in `doctor` and falls back to the default in a hook.

### 12.2 Data dir

`NOBLIVION_DATA_DIR`, else `${CLAUDE_PLUGIN_DATA}`, else
`${XDG_DATA_HOME:-~/.local/share}/noblivion`. No code holds a literal home
path. Paths are built with `Path.home()` and `os.path.expanduser`.

### 12.3 Keys

| Key | Env var | Default |
|---|---|---|
| `port` | `NOBLIVION_PORT` | `0` (any free port) |
| `idle_exit_s` | `NOBLIVION_IDLE_EXIT_S` | `1800` |
| `namespace` | `NOBLIVION_PROJECT` | `claude_code` |
| `memory_dirs` | `NOBLIVION_MEMORY_DIRS` (`:`-separated) | all `~/.claude/projects/*/memory`; plus the hooks' folder `NOBLIVION_MEMORY_DIR` when it is set |
| `index.interval_s` | `NOBLIVION_INDEX_INTERVAL_S` | `30` |
| `index.delete_grace_days` | none | `14` |
| `recall.shared_roots` | none | `[]` |
| `recall.timeout_s` | `NOBLIVION_RECALL_TIMEOUT_S` | `2.0` |
| `recall.min_score` | `NOBLIVION_RECALL_MIN_SCORE` | `0.68` |
| `recall.index_k` | `NOBLIVION_RECALL_INDEX_K` | `35` (shipped config: `30`) |
| `recall.env` | the `NOBLIVION_RECALL_*` env vars it names | `{}`; the shipped config turns on the ranked index and its steps (section 13.2) |
| `trust.events` | `NOBLIVION_TRUST_EVENTS` | `1` (on; `0`, `off`, `false` or `no` turns it off) |
| `trust.ranking` | `NOBLIVION_TRUST_RANKING` | `off` |
| `trust.prior_mined` | none | `0.3` |
| `embedding.backend` | `NOBLIVION_EMBED_BACKEND` | `fastembed` |
| `embedding.model` | `NOBLIVION_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` |
| `embedding.ollama_url` | `NOBLIVION_OLLAMA_URL` | `http://127.0.0.1:11434` |
| `embedding.allow_remote` | none | `false` |
| `embedding.remote_include_mined` | none | `false` |
| `dedup.judge` | `NOBLIVION_DEDUP_JUDGE` | `openrouter`; `off` refuses (section 10.2) |
| `dedup.model` | `NOBLIVION_DEDUP_MODEL` | unset; `plan` refuses without it |
| `dedup.min_cosine` | none | `0.75` |
| `dedup.max_pairs` | none | `50` judge calls per `plan` |
| `dedup.timeout_s` | none | `60` |
| `dedup.min_interval_s` | none | `1.0` |
| `dedup.price_in_per_mtok`, `dedup.price_out_per_mtok` | none | unset (no USD estimate) |
| `dedup.archive_retention_days` | none | `90` |
| `miner.enabled` | `NOBLIVION_MINER` | `false` |
| `miner.transcript_glob` | none | `~/.claude/projects/*/*.jsonl` |
| `miner.max_run_s` | none | `300` |
| `guard.generic_args_extra` | none | `[]` |
| `guard.evidence_stop_extra` | none | `[]` |
| `labels.ticket_prefixes` | none | `[]` (no ticket key labels) |
| `labels.service_prefixes` | none | `[]` (no service labels) |
| `stop.shared_checkouts` | none | `[]` |
| `stop.worktree_prefixes` | none | `[]` |
| `stop.test_command` | none | `python3 -m pytest -q <absolute test path>` |
| `stop.deploy_hosts` | none | `[]` (the deploy stop check is off) |
| `sync.index_command` | `NOBLIVION_MEMORY_SYNC_CMD` | `<data dir>/venv/bin/noblivion index` when installed, else none |
| `log_level` | `NOBLIVION_LOG_LEVEL` | `info` |
| `store.bin` | `NOBLIVION_BIN` | `<data dir>/venv/bin/noblivion` (the launcher the hooks start) |
| none | `NOBLIVION_STORE_AUTOSTART` | on; `0` stops every hook from starting the store |
| none | `NOBLIVION_RECALL_MEMORY_DIR` | the memory folder of the session's `cwd` (section 4.0) |
| none | `NOBLIVION_RECALL_ROOT` | derived from the memory folder or `cwd` (section 4.0) |

`OPENROUTER_API_KEY` keeps its common name. It is never in the config file.

Every on/off switch, in the hooks and in the store, parses its value with
one rule (`hooks/hook_config.py` `switch`, `noblivion.config.switch`):
`1`, `true`, `yes`, `on` are on; `0`, `false`, `no`, `off` and the empty
value are off; not set or any other value keeps the default.

The continuity briefing takes its curated index files from
`continuity.decision_files` (default `["topic_decisions.md"]`) and
`continuity.open_files` (default `["topic_open_work.md"]`).

### 12.4 Guard word lists

The guard hook has two word lists that were hard-coded in the reference
implementation:

- `GENERIC_ARGS`: command arguments too generic to make a trigger
  "specific". The shipped default is the generic git words only:
  `localhost origin github upstream main master head`. The reference list
  also held three host names; they are removed. A user adds their own host
  names with `guard.generic_args_extra`.
- `EVIDENCE_STOP`: words removed from a command before the evidence score.
  The shipped default is the reference list without the two entries that
  name the reference project. A user adds words with
  `guard.evidence_stop_extra`.
- The lists are extended, never replaced, by the config. The defaults live
  in one Python module so the leak scanner checks them.
- The host alias map of the label rules is removed. It has no generic form.
- The label rules made ticket key labels and service labels for the
  reference project's own prefixes. The prefixes now come from
  `labels.ticket_prefixes` and `labels.service_prefixes`. With none set,
  no such labels are made.
- The stop checks named one private checkout, worktree folder, test
  command and deploy host. These are now the `stop.*` keys. With
  `stop.deploy_hosts` empty, the deploy check never fires.
- The hooks read the config file and the data dir through one stdlib
  module, `hooks/hook_config.py`. The default word lists live there.

### 12.5 Env var rename

E2a renames every env var of the reference hooks to the `NOBLIVION_`
prefix with the same suffix, except these, which are removed:

- the API key and API key file variables (the token file replaces them);
- the plaintext override and the private-network address branch of the
  transport guard (loopback only now);
- the remote base URL variable (the URL comes from `store.json`).

The full old-to-new table is a data file in the migration tool (E8), not in
this document.

## 13. Packaging and install

### 13.1 Plugin layout

```
.claude-plugin/plugin.json      name, version, description, author, license
.claude-plugin/marketplace.json the repo is also a one-plugin marketplace (source "./")
hooks/hooks.json                hook entries, commands use ${CLAUDE_PLUGIN_ROOT}
hooks/*.py                      stdlib hooks (hook_config.py: settings; corpus.py: local corpus helpers;
                                store_client.py: store discovery, listener proof, launcher start,
                                and the SessionStart hook)
.mcp.json                       the recall MCP server (stdio)
mcp/recall_mcp.py
config/config.default.json      the config install.sh writes when none exists
src/noblivion/                  store and CLI package (installed into the venv)
skills/setup/SKILL.md           the slash command /noblivion:setup: runs install.sh (NOBLIVION-29)
scripts/install.sh, scripts/uninstall.sh
docs/
```

- Hook commands run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/<name>.py"`.
  Claude Code runs them through the shell, so they use the system
  `python3`. Plugins get no bundled runtime and no install-time hook.
- The hook bindings are those of the reference install, under the new file
  names: SessionStart (`store_client.py`, `guard_table.py --rebuild`;
  `compact`: `recall_hook.py --reset-rows`; `startup|resume|clear|compact`:
  `continuity_hook.py`; `startup|resume|clear`: `trust_session_line.py`),
  UserPromptSubmit (`stop_checks.py --mark-correction`, `recall_hook.py`),
  PreToolUse (`Bash`, `Read|Grep`, `Edit|Write|MultiEdit`: `guard_hook.py`;
  `Agent|Task`: `subagent_rules_hook.py`), PostToolUse
  (`Write|Edit|MultiEdit`: `memory_fields_hook.py`, `memory_sync_hook.py`;
  `Bash`: `error_recall_hook.py`, `memory_sync_hook.py`), PostToolUseFailure
  (`Bash`: `error_recall_hook.py`, `guard_hook.py`), PreCompact
  (`manual|auto`: `continuity_hook.py`), SubagentStart
  (`subagent_rules_hook.py`), Stop (`stop_checks.py`, `trust_flush.py`),
  and the new SessionEnd (`mine_session_end.py`, the transcript miner).
  `trust_report.py` has no binding: the trust flush refreshes the report.
  The reference install also set a trust A/B mode on some commands; that
  experiment flag is not shipped. `tests/test_plugin_package.py` holds the table.
- The store and CLI run from `<data dir>/venv/bin/python`. The package is
  installed into the venv (not editable), because `${CLAUDE_PLUGIN_ROOT}`
  changes on every plugin update.
- `.mcp.json` has no `env` block. Claude Code sets `CLAUDE_PLUGIN_ROOT`
  and `CLAUDE_PLUGIN_DATA` for the MCP server by itself. In 0.1.0 an `env`
  entry `"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"` reached the server
  as the literal text (measured on Claude Code 2.1.92 and 2.1.261), so the
  tool found no store (NOBLIVION-24). The data dir rule treats a value
  that still holds `${` as unset. `tests/test_plugin_package.py` fails on
  any variable in the plugin JSON files that Claude Code does not expand
  in that field.
- The MCP tool is renamed to `noblivion_recall`. The `GROUNDED MEMORY <id>:`
  prefix of its answer stays, because the trust flush reads it. Under the
  plugin its full name is `mcp__plugin_noblivion_noblivion__noblivion_recall`.
- Users add the repo with
  `claude plugin marketplace add https://github.com/Noetic-os/NOBLIVION.git`
  (the short form `Noetic-os/NOBLIVION` can clone over SSH) and install with
  `claude plugin install noblivion@noblivion` (or the `/plugin` slash
  commands).

### 13.2 Install

`install.sh [--data-dir DIR] [--no-embed] [--no-model] [--no-start] [--dry-run]`:

1. Check `python3` >= 3.9 for hooks and `uv` on `PATH`. Without `uv`, print
   the official install command and stop. The script never runs it.
2. `uv venv` in `<data dir>/venv` with python 3.11+, then install the locked
   store dependencies (`uv export --frozen --extra embed` from `uv.lock`),
   then the package itself with `--no-deps`. `--no-embed` leaves out numpy
   and fastembed: keyword search only. It sets `embedding.backend = none`
   in the config, else the store reports `degraded` (NOBLIVION-57).
3. Copy `config/config.default.json` to `<data dir>/config.json` when that
   file is missing. It ships the reference recall tuning in `recall.env`
   (the ranked index, apply lines, hygiene, re-rank, rule rows, row dedupe,
   drop rows with no rule, the label leg and the shown set) and
   `recall.index_k = 30`. It does not ship the reference index score floor
   (0.52), because that number was measured with another embedding model.
   It sets no floor at all: the hook default `DEFAULT_INDEX_MIN_SCORE`
   (0.68, measured for the default model, section 18) applies when the
   store answers with that model.
   Trust flags stay with E5.
4. Download the embedding model to `<data dir>/models/` (`--no-model`
   skips it). On failure, set `embedding.backend = none` in the config and
   say so. Keyword search still works.
5. Write the `token` file (mode 0600) if missing.
6. Write `venv/noblivion-install.json` (plugin version and root).
7. Run `noblivion index` once and `noblivion migrate-from-legacy` as a dry
   run, so the user sees any old hooks to remove.
8. Run `noblivion ensure-running --restart` (section 3.2), unless
   `--no-start`. A store that runs is replaced, so a config or model
   change takes effect. The store then runs in the session that ran the
   script, and the next prompt's hooks find it through `store.json`. A
   failed start prints the reason and is not fatal.

The data dir is `--data-dir`, else `NOBLIVION_DATA_DIR`, else
`CLAUDE_PLUGIN_DATA`, else `${XDG_DATA_HOME:-~/.local/share}/noblivion`. A
terminal has no `CLAUDE_PLUGIN_DATA`, so the `SessionStart` hook prints the
exact command, with the plugin's data dir, when the venv is missing or was
built for another plugin version (the stamp of step 6). A `noblivion`
command run from the venv finds its data dir and the plugin's `hooks/`
folder (label rules for `index`, `memory_fields.py` for `dedup`,
`stop_checks.py` for `mine`) through the same stamp.

The plugin ships the slash command `/noblivion:setup`
(`skills/setup/SKILL.md`, `disable-model-invocation: true`). Claude Code
fills in `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` in its body, so
the command runs `bash "<plugin root>/scripts/install.sh" --data-dir "<data
dir>"` from the chat, also in the VS Code chat where the user has no
terminal (NOBLIVION-29). Its `allowed-tools` entry covers that one command.
The `SessionStart` line names the slash command first and the terminal
command second. From `claude plugin install` to working recall is then at
most two sessions: the session (or terminal) of the plugin install, and
the next session, where the user runs `/noblivion:setup`. An empty
`--data-dir` (a Claude Code that does not fill in the variable) is refused.

Plugin install through the Claude Code plugin system places the files.
`install.sh` only builds the venv, the model cache, the config and the
token, and starts the store. Run it again after a plugin update.

### 13.3 Uninstall

`uninstall.sh [--data-dir DIR] [--purge] [--dry-run]` stops the store,
waits until it has exited (90 s, then `SIGKILL`; NOBLIVION-28), and
removes the venv and the model cache. It keeps `noblivion.db`, the config
and the token unless `--purge` is given. It refuses a folder that holds no
venv, store file or database. It never touches memory files. Then the
plugin is removed with `claude plugin uninstall noblivion --keep-data`.
Without `--keep-data`, `claude plugin uninstall` (and `/plugin uninstall`)
deletes the whole data dir, the database and the token included
(NOBLIVION-26). The script prints this next step.

## 14. Migration from hand-installed hooks

Some users run the reference hooks by hand: copies in
`~/.claude/hooks/claude_code_*.py`, hand-written entries in
`~/.claude/settings.json`, and an MCP entry in `~/.mcp.json`. With the
plugin as well, every hook runs twice.

`noblivion migrate-from-legacy [--apply | --undo] [--home DIR] [--json]` (E8):

1. Lists the old hook files (the known reference file names only; another
   `claude_code_*.py` is reported and left alone), links in the hooks
   folder that point to one of them, the settings entries whose command
   runs one of them, the MCP entries that run one, and the old env vars set
   for them (in the settings `env`, inline in the old commands, in the old
   MCP `env`).
2. Without `--apply` it only prints the plan. A dry run is the default.
3. `--apply` backs up `settings.json` and `.mcp.json` to
   `<file>.noblivion-backup-<utc>`.
4. It removes only the entries whose command runs one of the old hook
   files, and the old MCP entry. A matcher group or event left empty is
   removed. Every other key and entry keeps its value and its order (the
   file is written again as JSON, so whitespace may change).
5. Moves the old hook files to `<data dir>/migrated/<utc>/`. It does not
   delete them.
6. Prints the env var rename table (section 12.5, data file
   `src/noblivion/legacy_env.json`, by suffix) for the vars it found.
7. `--undo` restores the backups and the files of the last `--apply`.

The command is `migrate-from-legacy`, not `migrate`, so it is not mistaken
for the schema migrations of section 6.6.

What does not carry over:

- Trust history. It lives on the old server. Trust starts at the prior.
- Memory ids. The store assigns new ids. Spooled events with old ids count
  as `unknown`. Path events still resolve.
- Old spool files. The migration deletes nothing in the old cache dir; the
  new hooks do not read it.

The memory files themselves need no migration. The indexer reads them in
place.

## 15. Privacy and threat model

### 15.1 Assets

- Memory text: rules, project facts, sometimes personal facts.
- Transcripts: everything said and every tool output of past sessions.
- Trust data: what the user worked on and when.
- The token and any `OPENROUTER_API_KEY`.

### 15.2 Default data flow

With the default config nothing leaves the machine after install. The
install downloads Python packages and the model. At run time the store
makes no outbound connection. A test asserts this (section 17).

Outbound flows exist only when the user turns them on:

| Flow | Sends | Gate |
|---|---|---|
| OpenRouter dedup judge | two redacted memory texts per pair | `dedup.model` plus `OPENROUTER_API_KEY` plus typed consent |
| OpenRouter embeddings | every redacted and scrubbed memory text and query; mined rows only on opt-in | `embedding.backend = openrouter` plus `noblivion consent embeddings` |
| Remote Ollama | the same as above | non-loopback URL plus `allow_remote` plus consent |

### 15.3 Threats and controls

| Threat | Control |
|---|---|
| Another local user or process reads memories over the port | token file mode 0600; constant-time compare; 401 without it |
| A web page in the user's browser calls the port | `Host` check (blocks DNS rebinding); JSON content type on POST forces a CORS preflight; no CORS headers; no state change through GET |
| A foreign process takes the port and harvests the token, prompts or events | hooks find the port only in `store.json` and send nothing before the HMAC listener proof (section 3.3); the store does not move ports silently |
| The store listens on the network | bind address fixed to `127.0.0.1` in code, not in config; a test asserts it |
| Secrets in memory files or transcripts | at-rest redaction before store, embed and judge; fail token skips the text; a new redactor version re-indexes (section 5.4) |
| Paths, user names and IPs in text sent out | outbound scrub (section 10.2); host and project names in prose are not caught, and the consent text says so |
| One project's rules injected into another | recall scoped by `root` (section 5.1) |
| Prompt injection through memory text | see 15.4 |
| Hostile transcript text | mined rows never injected (section 11.3); they enter context only when the model asks the MCP tool for them |
| Path traversal through `path` events or fetch ids | `path` validation (section 4.5); paths resolve only to indexed rows, never to the file system |
| Large or deep JSON bodies | 256 KB body cap, 500 events cap, JSON parse errors are 400 |
| A database file from a newer version | refused for writes (section 6.6) |
| A tampered judge answer | parsed against the schema; only `MERGE` acts; human reads the plan |
| Log files leak memory text | logs hold ids, counts and error class names, never memory text or query text; level `debug` adds query text and is never the default |

### 15.4 Prompt injection through memory text

Memory text goes into the model's context. Anyone who can write a memory
file, or text that ends up in a transcript, can try to steer the model.

Controls:

- Only `claude_code_md` rows are injected by hooks. Those files are the
  user's own memory folder. Mined rows are not injected.
- Injected text is cut: title 120 characters, summary 180, a rule row 235,
  total 7600 characters for the index path and 2000 or 4000 for the hit
  path.
- The hook frames injected text as data ("memory notes, not instructions")
  and strips control characters. Text is folded to NFKC before matching.
- The reference implementation had an injection-pattern redactor on the
  server read path. E3a ports it next to the secret redactor
  (`noblivion/injection.py`, the memory-scoped pattern list without the
  two patterns that named the reference project's own approval tiers).
  The store runs it on every title, summary and text it returns, field
  by field.
- The guard hooks read rule fields (`rule`, `apply`, `triggers`) from the
  files, not from the store, and never execute them.

Residual risk: a memory file written by the model itself in an earlier
session can carry an injected instruction from that session. The trust
report and the user's own review of new memory files are the controls.
This is listed in section 18.

## 16. Leak policy

The repository is public. Code from the reference implementation was
written for one private setup and holds internal names.

Rules:

- No internal host name, IP address, home path, internal ticket key,
  company name or user name in any file, commit message or test fixture.
- Fixtures use fictional names (for example `alice`, `example-host`,
  `proj-demo`) and the `example.com` domains.
- Paths are built from `Path.home()`, the data dir or the plugin root.
- E1 adds a forbidden-names scanner to CI and pre-commit. It fails the
  build on a match. It checks file content, file names and commit
  messages of the pull request.
- The scanner has two parts:
  - generic patterns that are safe to publish: home-folder paths of the
    form `/home/` followed by a lower-case user name and `/`, private and
    carrier-grade NAT address ranges, and secret shapes (gitleaks). The
    placeholder `/home/<name>/` in docs does not match, because `<` is not
    a user-name character;
  - exact internal names, including the prefix of the reference ticket
    keys. A generic ticket-key regex would flag words such as `SHA-256` or
    `UTF-8`, and a specific one would publish the prefix. So the
    repository holds only salted SHA-256 hashes of each lower-case token.
    The scanner splits every file into tokens (and a ticket-shaped token
    into its prefix), hashes them and compares. The salt and the plain
    list stay with the maintainers.
- An allowlist file names fixture strings that look like a hit but are
  fictional. Each entry has a reason.
- The release gate (E9) runs both parts over the full git history.

## 17. Test strategy

All tests run with `pytest -q` and no network. A fixture blocks
`socket.connect` to any address except 127.0.0.1; a test that needs the
judge or OpenRouter uses a fake server on loopback.

| Area | Tests |
|---|---|
| REST contract | golden JSON for every route and every error status in section 4; field types; the `namespace` echo rule; the 512 KB cap |
| Client compatibility | run the ported hooks against a live store on a free port; assert the hooks parse every answer (E4) |
| Auth and transport | token missing or wrong; bad `Host`; POST without JSON type; bind address is loopback; no CORS headers |
| Schema | migrations from every older `user_version`; refuse a newer one; backup created; `foreign_key_check` empty after every delete path |
| Defect 1 reproducer | two connections: recompute reads, ingest commits, recompute writes; assert the event is counted (by design ingest blocks on `BEGIN IMMEDIATE`; the test proves it) |
| Defect 2 reproducer | insert a row, store an event, delete through the indexer path; assert 0 rows in `feedback` and `feedback_events` |
| Concurrency | 8 threads of ingest plus the indexer plus a CLI process writing; no `database is locked` error past `busy_timeout`; counter reload seen by search. E3d (`tests/test_store_concurrency*.py`): a live `noblivion serve` with hook processes searching while the indexer CLI, the store scan and two backfills write; read-then-write transactions in several processes; first open of a new database by several processes; `ensure-running` races; `SIGKILL` in a write transaction. `NOBLIVION_STRESS_FULL=1` runs the full load |
| Lifecycle | two starts race (one wins the lock); stale `store.json`; port held by a foreign server that fakes `service` (hook sends no token); proof with a wrong token; version mismatch restarts; idle exit; `SIGTERM` checkpoint |
| RAM reload | a CLI commit with an older clock is still seen; one reload at a time; a re-embed does not rebuild BM25 |
| Root scoping | a rule in root A never in the pool for root B; same file name in two roots; `path` event resolution with and without `root` |
| Soft delete | rename keeps id and trust; folder move keeps id; grace purge cascades; shrink guard per root |
| Hooks fail open | store down, slow, 500, bad JSON: each hook exits 0 and injects nothing |
| Ranking | BM25 values against hand-computed cases; fusion order and floor; empty fallback; keyword-only mode; re-embed mixed pool |
| Indexer | frontmatter cases; content format byte for byte; hash skip; shrink guard; empty scan; redaction fail token |
| Trust | formula properties (no events gives trust_0; bounded; monotone); report rules at each threshold; `path` and `mv_id` resolution |
| Dedup | fake judge; plan does not write; apply checks hashes; crash at each merge step then rerun; undo restores; vetoed pair not proposed; archive keeps trust; consent required; outbound scrub |
| Miner | fictional transcripts; each kind; offset resume; redaction before insert; mined rows absent from search and default index |
| Leak | forbidden-names scanner on the tree in CI |

Coverage target for the store package: 85% lines. Mutation testing is a
later goal, not a v0.1 gate.

## 18. Open risks

- Recall quality. The reference used a 1024-dim multilingual model. The
  default here is a 384-dim English model. Non-English memories will
  recall worse. Users can switch to a multilingual model through Ollama.
- Thresholds. The old values (search floor 0.3, error recall floor 0.60,
  dedup cosine 0.82) were tuned for the old 1024-dim model. NOBLIVION-33
  measured three of them for `BAAI/bge-small-en-v1.5` with
  `tools/eval_thresholds.py` on the made-up eval set
  `tools/eval/thresholds.json`: 36 memories, 57 prompts (37 with an
  expected memory, 20 without), 24 error lines (14 with, 10 without), and
  24 dedup memories with 8 duplicate pairs among 276 pairs. The script runs
  a real store and measures each score on the code path that compares it.
  The search, index and store error scores are the store's weighted cosine
  (weight 1.0 by default), not the fused rank score. Each value is the grid
  point (step 0.01) with the best F1, the middle one when several share it.

  NOBLIVION-48 added the two paths that a default install runs and that
  NOBLIVION-33 did not measure: the ranked index of the shipped config file,
  and error recall in `local` mode. All numbers below are from the ranking
  of NOBLIVION-49 (section 8.3). That ranking changed one number of
  NOBLIVION-33: the store error precision at the old floor 0.60 was 0.255.

  | Threshold | Path it describes | Old | New | Precision old / new | Recall old / new | Prompts or error lines without a memory that still get rows, old / new |
  |---|---|---|---|---|---|---|
  | note text floor `recall.min_score` | prompt hook with no config file: `recall()`, k = 5 | 0.3 | 0.68 | 0.137 / 0.857 | 0.975 / 0.750 | 20 of 20 / 1 of 20 |
  | index floor `NOBLIVION_RECALL_INDEX_MIN_SCORE` | prompt hook with the shipped config file: the ranked index (`_serve_index`), 30 rows, with re-rank and rule rows | none | 0.68 | 0.023 / 0.857 | 1.000 / 0.750 | 20 of 20 / 1 of 20 |
  | error recall local floor `LOCAL_MIN_SCORE` | error hook in `local` mode (the default): `search()` over the memory files | 0.75 | 0.39 | 1.000 / 1.000 | 0.071 / 0.643 | 0 of 10 / 0 of 10 |
  | error recall store floor `STORE_MIN_SCORE` | error hook in `store` mode (off by default): `search()` over the store index | 0.60 | 0.70 | 0.283 / 1.000 | 0.929 / 0.857 | 3 of 10 / 0 of 10 |
  | dedup cosine `dedup.min_cosine` | `noblivion dedup`: pairs of stored vectors | 0.82 | 0.75 | 1.000 / 1.000 | 0.875 / 1.000 | not applicable |

  This model packs cosines into a narrow band: an unrelated prompt still
  scores about 0.5 to 0.6. At 0.3 the note text floor cut nothing, and all
  20 prompts without a memory got hits. At 0.68 one of them does.

  The ranked index had no floor. With the shipped config file each of the
  57 prompts got 30 rows: 40 right rows and 1670 wrong rows. The floor
  is now a default of the hook (`DEFAULT_INDEX_MIN_SCORE`), so a config
  file of an older release gets it too; the value `off` gives the index
  with no floor back. The default is for the measured model only: the
  index answer names the model of its scores (section 4.3), and for
  another model, or for an answer that names none, the hook applies no
  default floor and writes `floor_off:model` or `floor_off:no_model` to
  the recall log. The note text floor and the error recall store floor do
  not read the model yet: they apply to every model. At 0.68 the index
  shows 30 right rows and 5 wrong
  rows, and 10 of the 40 expected rows are no longer shown. The eval runs
  the path a third time with the variable unset and fails when the counts
  differ from the counts of the shipped floor. The label rows
  (`NOBLIVION_RECALL_LABELS`) are a separate output and are not measured.

  The local error score is not a cosine. It is the share of the error's
  words that the memory holds, weighted by idf, so it needs no model. At
  0.75 only 1 of the 14 expected memories passed the gate. The grid values
  0.37 to 0.42 share the best F1; under 0.37 the first wrong rows pass (the
  best wrong row has a share of 0.36). Both error recall numbers are before
  the quote check, which comes after the gate and removes more wrong hits.
  The dedup duplicates scored 0.776 to 0.899, the best non-duplicate 0.726.

  Limits of these numbers. The eval set is small and written by hand. It
  has no held-out split: each threshold is chosen and scored on the same
  items, so the precision and recall above are too good as a forecast for
  other memories, and each number can move by several points on real
  memories. The recall of the local floor moves by 0.071 for each error
  line. NOBLIVION-64 tracks a larger set with a held-out split.

  CI job `real-model` runs the eval and fails when precision or recall at a
  shipped value falls under its floor in the eval file. A test in the
  normal suite does the same for the local error floor. Each threshold sits
  next to `THRESHOLD_MODEL`; a test fails when `embedding.DEFAULT_MODEL`
  changes, so a new model needs a new measurement.
- First-run cost: about 130 MB model download and a numpy plus
  onnxruntime venv. Offline machines get keyword-only search.
- The `localhost` name is refused by the hook transport guard. A user who
  sets a base URL by hand with `localhost` gets silent fail-open. `doctor`
  checks this.
- Root derivation copies how Claude Code names project folders. If Claude
  Code changes that rule, the hooks send a root that matches nothing, and
  recall falls back to `recall.shared_roots` only. `doctor` checks that
  the root of the current dir exists.
- Self-written memory files can carry injected text from an earlier
  session (section 15.4). No automatic control exists.
- WAL on a network file system is unsafe. `doctor` warns; the store does
  not refuse.
- No Windows support: `flock`, detached spawn and file modes are POSIX.
- The OpenRouter `data_collection: deny` preference depends on the
  provider's own statements. It is not a guarantee.
- One store per OS user. Two Claude Code installs under one user with
  different data dirs and the same port: the second store fails to bind
  and logs it. `port: 0` avoids this.

- After an idle exit (default 30 minutes) the first prompt gets no recall,
  because the hook only starts the store and fails open. The
  `SessionStart` hook starts it too, so this hits long sessions with long
  pauses. A longer `idle_exit_s` trades RAM for recall.
- Keyword mode with an unported hook gives id order (section 4.0). Only
  the ported hooks are supported.

Decisions this document could not settle (for the reviewer or the
maintainer):

- Transport: TCP loopback with a token and a listener proof (this
  document, as the approved plan says) or a Unix socket in the data dir.
  The socket is simpler and safer; it changes the plan.

- The default `dedup.model`. This document ships none, so the user must
  pick one. A default would name a vendor model.
- Whether a loopback token is acceptable as "no API key" under the plan.
  This document keeps it because loopback is shared by every local user.
  The user never sees or types it.

## 19. Decisions taken after review

- Transport: TCP on 127.0.0.1 stays. A Unix socket is safer on Linux and macOS, but
  Windows support for it in Python is partial. The token handshake (section 15)
  closes the gap that a socket would close.
- Token: the store writes a token file in the data dir with mode 0600. The user
  never sees or manages it. "No API key" in the plan means no key the user must
  set, not no secret at all.
- Dedup judge model: the plugin ships no default model. Dedup stays off until the
  user sets both a model and an OpenRouter key. The docs show one example model.

## Review log

### Round 1, 2026-10-03: adversarial review before code

A critical-reviewer agent read this document against the reference server
and client code. Verdict: not ready. 1 blocker, 7 major, 12 minor, all
marked "fix now", and 4 notes. Every finding is handled in this version.

| Id | Finding | Handling |
|---|---|---|
| B1 | The hooks could send the token and the prompt to any process on the port (a crash or idle exit left the port free; the hooks fell back to the config port; `"service"` could be faked) | Fixed: discovery only through `store.json`, no config-port fallback, HMAC listener proof before any token or text (3.3). Unix socket recorded as an open decision (18) |
| M1 | "Only the base URL changes" was false | Fixed: claim removed; full hook change list and release gate (4.0) |
| M2 | Keyword mode `score: null` gives id order in an unported hook | Fixed: E4 hook rule plus contract test as a release gate (4.0, 8.4); risk listed (18) |
| M3 | One namespace for every project folder lets rules cross projects and lets a same-named file inject the wrong rule | Fixed: `root` parameter, pool scoped by root plus `recall.shared_roots`, `path` events resolved in the batch root (4, 5.1, 8.1, 9.2) |
| M4 | RAM reload used a clock watermark; vectors had no stamp; BM25 rebuilt during re-embed; parallel reloads | Fixed: `content_rev` and `vector_rev` counters, per-row `rev`, snapshot read, single reload with swap, BM25 only on content change (6.3) |
| M5 | Hard delete on a missing file lost trust on rename or folder move; shrink guard was corpus-wide | Fixed: soft delete with grace period, hash re-link on rename and move, guard per root (5.4, 5.5) |
| M6 | No consent design for remote embeddings; query not redacted; mined rows would leave | Fixed: `embed_consent` set by a CLI command, query redacted and scrubbed, mined rows local unless opted in (7.1, 7.2) |
| M7 | "No path, user name, host name is sent" was false | Fixed: outbound scrub for home path, user name and IPs; claim corrected; consent text states the residue (10.2) |
| m1 | Error recall listed twice with conflicting rows; its default mode is local | Fixed (3.5) |
| m2 | "Answers at once" vs "index before answering" | Fixed: first scan runs in the background, `index_state` in health (3.2, 5.6) |
| m3 | Report example impossible: recall is not a trial | Fixed example; doc stated trust never falls below trust_0 in v0.1 (4.6). NOBLIVION-34 added `contradict`, which lowers it |
| m4 | `trust_prior` 0.5 for every row, but mined prior is 0.3 | Fixed: the row's own prior (4.3) |
| m5 | Cascade on `dedup_actions` erased undo records; no veto table | Fixed: no foreign keys on `dedup_actions`, new `dedup_vetoes` (6.2) |
| m6 | No job ran retention; merge step order undefined; "the store" vs the CLI | Fixed: maintenance pass (9.3), three-step merge order with `pending` status, CLI named as the one writer (10.1, 10.4) |
| m7 | New ids restart at 1 after a reset, so old ids credit the wrong row | Fixed: random id base at create (6.2) |
| m8 | A new redaction rule needed a manual `--force` | Fixed: `meta.redactor_version` triggers a re-index (5.4) |
| m9 | Report: which `MEMORY.md`, archived rows, size cap, `root` | Fixed: same-root link set, live rows only, 500-row cap with `truncated`, `root` field (4.6) |
| m10 | Scanner: a generic ticket regex flags `SHA-256`; a specific one leaks the prefix; the doc placeholder could match | Fixed: hashed prefix, allowlist, placeholder rule (16) |
| m11 | Benchmark persona 403 is a leftover | Fixed: dropped (4.5) |
| m12 | Major-version check let an old store run an old redactor; import path unstated | Fixed: full version compare with restart, `PYTHONPATH` set to the plugin root (3.2) |
| n1 | The fusion floor never drops a row | Noted in 8.3 |
| n2 | The stdlib server does not decode chunked bodies | Adopted: `Content-Length` required, 411 (4.1) |
| n3 | First prompt after an idle exit gets no recall | Listed as a risk (18) |
| n4 | Degraded mode over an old schema is extra code | Adopted: a failed migration exits with code 3 (6.6) |
