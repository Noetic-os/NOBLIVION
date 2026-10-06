<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Troubleshooting

In the commands below, `<data dir>` is the NOBLIVION data dir (see
[install.md](install.md#the-data-dir)), and `noblivion` is
`<data dir>/venv/bin/noblivion`.

## Where to look first

| File | What it tells you |
| --- | --- |
| `<data dir>/cache/recall.log` | One line per prompt: hits, characters, time, and `ok`, `fail:<reason>` or `skip:<reason>`. Rotated at 5 MB. |
| `<data dir>/logs/store.log` | Store start, stop, index scans and embedding state. |
| `<data dir>/guard-log.jsonl` | Guard decisions. Rotated at 5 MB. |
| `<data dir>/error-recall-log.jsonl` | Error recall decisions. |
| `<data dir>/cache/trust-flush.log` | Trust flush runs. |

The logs never hold note text or prompt text, unless you set the log level
to `debug`.

To check the store, run:

```sh
noblivion ensure-running --json
```

It prints `{"state": "running"}` when a store runs and passes the
handshake. It prints `started` when it started a new store. `started` does
not prove that the store came up. Run the command again after a few
seconds.

## Recall shows nothing

Read the last lines of `recall.log`. The reason after `fail:` or `skip:`
tells you the cause.

| Reason | Cause | What to do |
| --- | --- | --- |
| `store_down` | No store answers on the port in `store.json`, or the store is not installed. | See [The store is not running](#the-store-is-not-running). |
| `no_token` | The `token` file is missing or cannot be read. | Run `noblivion ensure-running`. The store makes a new token. |
| `foreign_listener` | The handshake failed. | See [Handshake failure](#handshake-failure). |
| `timeout` | The store did not answer within 2 seconds. | See [Recall is slow](#recall-is-slow). |
| `store_hung` | A store call timed out less than 30 seconds ago, so the hook did not call the store. | See [Recall is slow](#recall-is-slow). |
| `busy` | The hook already had 4 store requests in flight, because earlier requests hang. | Check that the store answers: run `noblivion ensure-running --json`. |
| `namespace_mismatch`, `bad_project` | `NOBLIVION_PROJECT` or `NOBLIVION_RECALL_PROJECT` differs between the hook and the store, or has bad characters. | Use lower-case letters, digits and `_`. Set the same value for both. |
| `skip:disabled` | `NOBLIVION_RECALL_DISABLE` is set. | Remove it. |
| `ok` with `hits=0` | The store found no matching note for this root. | See below. |

When the store answers but finds nothing:

- Check that your notes are in `~/.claude/projects/<project>/memory/`.
  Files in sub-folders are not indexed.
- Recall searches only the notes of the session's root: the project folder
  of the session's working directory. A project with no memory folder of
  its own finds nothing. List shared roots in `recall.shared_roots` so that
  every session can find them.
- Run `noblivion index --json` and read the counts and the `skipped` list.
- The hook drops a note whose vector score is below the floor: 0.68 for
  note text and for the ranked index. For the ranked index the log status
  holds `:floorNofM`: N rows passed the floor, of M ranked rows. To see
  the index with no floor, set `NOBLIVION_RECALL_INDEX_MIN_SCORE` to `off`.
- The floor 0.68 was measured for the default embedding model,
  `BAAI/bge-small-en-v1.5`. With another model the ranked index has no
  default floor, and the log status holds `:floor_off:model`. The status
  `:floor_off:no_model` means that the store runs an older version than
  the hook, so the index has no default floor: type `/noblivion:setup` in
  the chat. The note text floor, the subagent rules floor and the error
  recall store floor follow the same rule: with another model they have
  no default. To get a floor with another model, set `recall.min_score`,
  `NOBLIVION_RECALL_INDEX_MIN_SCORE` or `NOBLIVION_ERROR_RECALL_MIN_SCORE`
  to a value that fits its scores.

## The store is not running

When the store cannot start, the session start shows one line:
`NOBLIVION: the memory store could not start, so memory recall is off:
<reason>`. The reason is also in `<data dir>/store.error` and in the log.
`noblivion ensure-running` prints `noblivion: store failed: <reason>` and
exits with code 1.

1. Check that the venv exists: `<data dir>/venv/bin/noblivion`. If it does
   not, type `/noblivion:setup` in the chat, or run `install.sh`. See
   [install.md](install.md). At session start, the plugin prints both when
   the venv is missing.
2. Check that autostart is on. `NOBLIVION_STORE_AUTOSTART=0` stops every
   hook from starting the store.
3. Run `noblivion ensure-running`. It waits up to 10 seconds for the
   store and prints `started`, `running` or `failed` with the reason.
4. Start the store in the foreground to see its errors:

   ```sh
   noblivion serve
   ```

5. Read `<data dir>/logs/store.log`.

| Log line | Cause | What to do |
| --- | --- | --- |
| `bind failed on port N: Address already in use` | Another program uses the port. This happens only with a fixed `port`. | Stop that program, or set `port` (or `NOBLIVION_PORT`) to `0` (the default: the system picks a free port) or to another port. |
| `another store runs; exiting` | A store already holds `store.lock`. | Nothing. The running store serves the hooks. |
| `schema: ...` | The database is from a newer version, or is damaged. | Update the plugin, or move `noblivion.db` away and run `noblivion index` to build a new one. Trust history is in the old file. |
| `store stopping: idle` | The store had no request for 30 minutes. | Nothing. The next prompt starts it again. |
| `not installed: no database at ...` | There is no `noblivion.db`, and `install.sh` has not run for this data dir. The store makes no new database then (NOBLIVION-28). | Type `/noblivion:setup`, or run `install.sh`. |
| `store stopping: data dir removed` | The data dir was deleted, for example by `claude plugin uninstall`. The store stopped and made nothing. | Nothing, or install again. |

The store exits with code 2 when it cannot bind to the port, with code 3
on a schema error, and with code 4 when the data dir is missing, or when
the database is missing and `install.sh` has not run. `noblivion
ensure-running` prints `noblivion: not installed: no data dir at ...` and
exits 1 when the data dir is missing. Neither makes the data dir.

To stop the store, send SIGTERM to the process id in
`<data dir>/store.json`. The uninstall script does the same, and waits
until the store has exited.

## Handshake failure

`fail:foreign_listener` means that a process answers on the port in
`store.json`, but it cannot prove that it knows the token. The hooks then
send nothing to it. This protects your prompts and your notes.

Common causes:

- Another program took the port after the store stopped.
- A store from another data dir, with another token, runs on the same
  port.
- The `token` file changed while the store was running.

To fix it:

1. Find the program on the port in `store.json`, for example with
   `ss -ltnp` or `lsof -i :<port>`.
2. If it is an old NOBLIVION store, stop it. If it is another program,
   set another `port` in `config.json`.
3. Run `noblivion ensure-running --json`.

## Database is locked

The store and the `noblivion` commands share one SQLite file. SQLite runs
in WAL mode. A writer waits up to 5 seconds for another writer. A lock
that lasts longer gives the error `database is locked`.

1. Wait and run the command again. Most locks are short.
2. Check for a stuck `noblivion` process, for example a `noblivion index`
   or `noblivion mine` that still runs.
3. Commands that hold the index lock (`index`, `dedup apply`,
   `dedup undo`) exit with code 4 when another process holds it. Run them
   again later.
4. `noblivion index`, `noblivion trust report` and
   `noblivion trust recompute` exit with code 5 when the database stays
   locked past the 5 seconds, or on another SQLite error. They print one
   line, for example
   `noblivion index: database error (database is locked); try again later`.
5. If the lock stays, stop the store (SIGTERM to the process id in
   `store.json`). Then run the command again.

Do not delete `noblivion.db-wal` or `noblivion.db-shm` while a store or a
command runs. These files hold recent writes.

## Model download

`install.sh` downloads the embedding model to `<data dir>/models/`. When
the download fails, it sets `embedding.backend` to `none`. Recall then
uses keyword search only.

Signs that the store runs without a model:

- `store.log` shows `model load failed (<error type>): <reason>; keyword
  search only, next try in 3600 s`, or `embedding state: failed`. The store
  writes this line once for each failed load. The reason says what went
  wrong, for example a missing model file.
- The health answer has `"status": "degraded"`.

To fix it:

1. Check your network connection and your proxy settings.
2. In `<data dir>/config.json`, set `embedding.backend` to `fastembed`, or
   remove the key.
3. Run `install.sh` again. It restarts the store, so the store loads
   the model at once. Without a restart, the store retries a failed
   model load only once per hour.

On a machine with no network, copy the model cache from another machine
to `<data dir>/models/`. The store does not download a model by itself
unless `embedding.allow_download` is `true`.

## Recall is slow

- The hook has a budget of 2 seconds (`NOBLIVION_RECALL_TIMEOUT_S`).
- When a store call runs out of time, the hooks do not call the store for
  the next 30 seconds. The log then shows `store_hung`, and the prompt does
  not wait. The hooks write the end of this pause to `<data dir>/store.hung`.
  The pause stops only a call with no more time than the call that timed
  out, so a hook with a short budget does not stop the prompt hook.
  When `store_hung` comes back every 30 seconds, the store accepts a
  connection but does not answer. Stop the store (SIGTERM to the process id
  in `store.json`), then run `noblivion ensure-running`.
- The first prompt after a store start can be slow, because the store
  loads the model and the index.
- An embedding of the prompt that takes more than 1 second gives a
  keyword-only answer.
- A large memory folder takes longer to index the first time. Run
  `noblivion index` once by hand.
- A note larger than 256 KB is stored by its first 256 KB only. Split it
  into smaller notes. `noblivion index` names each such file, and the
  store log names it once.

## The index does not delete removed files

The shrink guard stops deletes when a scan would remove more than 10% of
the notes. `noblivion index --json` then shows `index_blocked` and the
reason. When you really removed the files, run:

```sh
noblivion index --allow-shrink
```

The index also deletes nothing in a project folder while a file of that
folder cannot be read or redacted. `noblivion index` names the file, and
`--json` shows the held deletes per folder in `deletes_held`. Fix the
permissions of the file, or remove it. The next scan runs the deletes.

`--json` also shows `files_cut`: the count of files of this scan that are
larger than 256 KB and stored by their head only. `noblivion index` names
them.

## Hooks run twice

You may still have the old hand-installed hooks. Run
`noblivion migrate-from-legacy`. See
[install.md](install.md#migrate-from-hand-installed-hooks).
