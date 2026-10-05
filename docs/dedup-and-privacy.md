<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Duplicate sweep and privacy

## Privacy

NOBLIVION is local by default.

- The memory files, the database, the vectors, the trust data, the logs
  and the token stay on your machine.
- The store listens only on `127.0.0.1`. A token protects it from other
  local users.
- With the default settings, the store makes no network connection. The
  install downloads Python packages and the embedding model once.
- The transcript miner is off by default. When you turn it on, it reads
  your transcripts and writes to the local database only.

Only two features send text off the machine. Both are off until you turn
them on and type your consent.

| Feature | What it sends | What you must do |
| --- | --- | --- |
| Remote embeddings | every memory file and every prompt | set a hosted backend and run `noblivion consent embeddings` |
| Duplicate sweep | the two notes of each candidate pair, front matter included | set `dedup.model` and `OPENROUTER_API_KEY`, type `yes` to the consent, and type `yes` after each cost estimate |

Before text leaves the machine, NOBLIVION does this to it:

1. The secret redactor removes keys, tokens and passwords.
2. The outbound scrub replaces your home folder path with `~`, your user
   name with `<user>`, and IP addresses with `<ip>`.

The scrub cannot find host names, company names or project names in plain
prose. They are sent as written. Do not turn on a remote feature for notes
that hold names you must keep private.

## Remote embeddings

The default embedding backend, `fastembed`, runs on your machine. A local
Ollama server on `127.0.0.1` also keeps text on your machine. Two backends
send text off the machine:

- `openrouter`. It also needs `OPENROUTER_API_KEY`.
- `ollama` with a server that is not on `127.0.0.1`. It also needs
  `embedding.allow_remote` set to `true`.

To turn on a remote backend:

1. Set `embedding.backend` (and `embedding.model`, or
   `embedding.ollama_url`) in `<data dir>/config.json`.
2. Run the consent command. It prints the text below and waits for your
   answer:

   ```sh
   <data dir>/venv/bin/noblivion consent embeddings
   ```

3. Type `yes`.
4. Start a new Claude Code session.

The consent text is:

```text
NOBLIVION embeddings consent (version 1)

Backend: <provider>, model: <model>

With this backend NOBLIVION sends text off this machine:
- the text of every memory file in the store (not transcript-mined rows,
  unless embedding.remote_include_mined is true);
- the text of every prompt you send to Claude Code, as the search query.

Before sending, each text goes through the secret redactor and an outbound
scrub: your home folder path becomes ~, your user name becomes <user>, and
IP addresses become <ip>. Host names and project names inside the prose
cannot be found reliably and are sent as written.

The receiver is <receiver>. Their data policies apply.
Type yes to agree. Any other answer keeps the backend off.
```

The receiver is "OpenRouter and the model provider it routes to" for
`openrouter`, or "the Ollama server at the configured URL" for `ollama`.
Each memory text is cut to 8000 characters before it is sent.

To withdraw your consent, run:

```sh
<data dir>/venv/bin/noblivion consent embeddings --revoke
```

The withdrawal applies at once. You do not need to restart the store:

- A running store reads the consent before each request to the remote
  service. Without the consent it sends nothing: no prompt and no note.
- When the store cannot read the consent (a database error), it sends
  nothing for that request and answers by keyword search. It reads the
  consent again for the next request.
- Recall then uses keyword search only. The health answer shows
  `"consent": "missing"` and the embedding state `failed`.
- A request that is already on its way when you run the command is not
  stopped.
- The vectors that the store already has stay in the database. The store
  does not use them while the consent is missing.

To turn the remote backend on again, run `noblivion consent embeddings`
again. A running store uses the new consent within a few seconds.

## The duplicate sweep

Over time, you can get several notes that say the same thing. The
duplicate sweep finds such pairs, asks a hosted model whether they are
duplicates, and merges the ones you approve. It never runs by itself. You
run each step.

### Set up

1. Get an API key from [OpenRouter](https://openrouter.ai/). Put it in the
   environment as `OPENROUTER_API_KEY`. NOBLIVION never reads the key from
   the config file.
2. Choose a judge model. Set it in `<data dir>/config.json`, for example:

   ```json
   {"dedup": {"model": "<provider>/<model>"}}
   ```

   There is no default model. You choose who receives the text.

3. Optional: set `dedup.price_in_per_mtok` and `dedup.price_out_per_mtok`
   to get a cost estimate in USD.
4. Give your consent:

   ```sh
   <data dir>/venv/bin/noblivion dedup consent
   ```

   Type `yes`. A change of model asks for consent again.

The consent text is:

```text
NOBLIVION dedup consent (version 2)

Judge: OpenRouter, model: <model>

`noblivion dedup plan` sends text off this machine, once per candidate pair:
- a fixed judge prompt;
- the text of the two memory files of the pair, each at most 20,000
  characters. The file names are replaced by A and B, but the text
  includes the front matter, so its name and description fields are sent
  as written. No trust data and no other memory is sent.

Before sending, each text goes through the secret redactor and an outbound
scrub: your home folder path becomes ~, your user name becomes <user>, and
IP addresses become <ip>. Host names and project names inside the prose
cannot be found reliably and are sent as written.

The receiver is OpenRouter and the model provider it routes to (the request
asks for providers with data_collection "deny"). Their data policies apply.
Each judge call is billed to your OpenRouter account.
Type yes to agree. Any other answer keeps dedup off.
```

The text of a memory file includes its front matter. So the `name` and
`description` fields are sent as written, even though the file names are
replaced. The consent text says so since version 2. A consent that you
gave to version 1 is not valid any more: `noblivion dedup consent` (or the
next `plan`) asks again.

To withdraw your consent, run `noblivion dedup consent --revoke`.

### What is sent

For each candidate pair, one request to OpenRouter holds:

- the fixed judge prompt;
- the kind of the two notes (for example `feedback`);
- the full text of the two files, after redaction and scrub.

The request asks for providers that do not collect data. A text longer
than 20,000 characters after the scrub, or a text that fails redaction, is
not sent. That pair gets the verdict "unsure".

Transcript-mined notes, index files and topic files are never candidates.

### Run a sweep

Run the steps in this order. All commands are
`<data dir>/venv/bin/noblivion dedup ...`.

1. **List the candidates.** This uses the local vectors only. It sends
   nothing.

   ```sh
   noblivion dedup pairs
   ```

   A pair needs the same root, the same category and a cosine similarity
   of at least `dedup.min_cosine` (0.75). Files changed in the last 30
   minutes are skipped. A pair you undid before is skipped.

2. **Check the cost first (optional).** This prints the pairs that a
   plan would judge and the cost estimate. It makes no network call, sends
   nothing and needs no consent, model or API key.

   ```sh
   noblivion dedup plan --dry-run
   ```

3. **Judge the pairs.** This sends the best pairs (at most
   `dedup.max_pairs`, default 50) to the judge. It prints the cost
   estimate and asks you to type `yes`. Any other answer sends nothing.
   Add `--yes` to skip the question, for example in a script.

   ```sh
   noblivion dedup plan
   ```

   It writes `<data dir>/dedup/plan-<run id>.json` and prints the run id.

4. **Read the plan.** Open the plan file. Check each pair with the
   verdict `MERGE`.
5. **Apply the plan.**

   ```sh
   noblivion dedup apply <run id>
   ```

   It merges only the `MERGE` pairs whose files did not change since the
   plan. It merges at most 30 pairs per run. It refuses a pair when the
   two notes differ in scope, status or project, or when the merged rule
   would be invalid.

### Archive and undo

For each merged pair, `apply`:

1. writes a backup of both files to `<data dir>/dedup/<run id>/`;
2. writes the merged text to the note that stays;
3. moves the other note to `<memory folder>/.archive/dedup/<run id>/`;
4. marks its database row as archived.

It also writes an undo script, `<data dir>/dedup/<run id>/undo.sh`.

To undo a run, run:

```sh
noblivion dedup undo <run id>
```

Add `--pair A.md,B.md` to undo one pair. Undo restores the files newest
first. It stops at a file that changed after the merge. An undone pair is
never proposed again.

The store removes an archived row from the database 90 days after the
merge (`dedup.archive_retention_days`). The archived file stays in
`.archive/dedup/`. NOBLIVION never deletes it.

### When an apply fails

When a step of `apply` fails, NOBLIVION writes a latch file,
`<data dir>/dedup/apply-failed.json`. While the latch exists, `apply`
refuses to run.

1. Undo the failed run: `noblivion dedup undo <run id>`. This also removes
   the latch.
2. Or, when you have fixed the files by hand, remove the latch:
   `noblivion dedup clear-latch`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done |
| 1 | refused, for example no consent, no model, no API key, or no `yes` after the cost estimate; nothing was sent |
| 2 | usage error |
| 3 | database schema error |
| 4 | another process holds the index lock |
| 5 | the latch blocks `apply` |
| 6 | no database: run `/noblivion:setup` (or `install.sh`) first; dedup never makes one |

### The status line

Every `plan` (also `--dry-run`), `apply` and `undo` writes its result to
`<data dir>/cache/dedup-last-run.json` (`NOBLIVION_DEDUP_STATUS_FILE`). At
session start, a hook prints one line from it, for example:

```text
Memory dedup plan <run id> on 2026-10-03: 2 merge proposals. Read the plan, then run noblivion dedup apply <run id>
```

The line names the date of the run. It does not appear when the run has
nothing to report, for example after an `undo`. After a failed `apply`,
the line names the run to undo.

To turn the sweep off for good, set `dedup.judge` to `off`.
