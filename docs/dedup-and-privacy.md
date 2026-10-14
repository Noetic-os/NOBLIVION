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
| Duplicate sweep | the two notes of each candidate pair, front matter included | set `dedup.model` and `OPENROUTER_API_KEY`, type `yes` to the consent, and type `yes` after each cost estimate of a `plan`. `noblivion dedup sweep` asks no question: it uses the consent you gave once |

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
run each step, or you set up a timer that runs `noblivion dedup sweep` (see
[Run the sweep on a timer](#run-the-sweep-on-a-timer)).

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

When the answer of the judge cannot be read, NOBLIVION asks the same
question once more, with the same text, and with the instruction to answer
with the JSON object only. So a pair can be sent twice. Both calls are
billed and counted in the plan.

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
   The last line of the verdicts looks like this:

   ```text
   noblivion dedup: verdicts MERGE=4, RELATED=26, SUPERSEDE=1, UNSURE=3; parse_failed=16; used ...
   ```

   `parse_failed` counts the pairs where the answer of the judge could not
   be read, also after the retry (see
   [Judge answers that cannot be read](#judge-answers-that-cannot-be-read)).
   It is not a judgement, so it is not counted as `UNSURE`. Such a pair
   has `"error": "parse"` in the plan.

4. **Read the plan.** Open the plan file. Check each pair with the
   verdict `MERGE` or `SUPERSEDE`.
5. **Apply the plan.**

   ```sh
   noblivion dedup apply <run id>
   ```

   It acts on the `MERGE` and `SUPERSEDE` pairs whose files did not change
   since the plan, at most 30 per run. It refuses a pair when the two notes
   differ in scope, status or project, or when the merged rule would be
   invalid. It prints one line for each step with the cosine similarity and
   the reason of the judge, and one line for each trigger item that did not
   fit (see [Merge and supersede](#merge-and-supersede)).

### Judge answers that cannot be read

The judge must answer with one JSON object. A reasoning model often adds
thinking text. NOBLIVION reads the answer like this:

1. It drops `<think>` blocks.
2. It takes a JSON object from anywhere in the text. With several objects it
   takes the last one that has a `verdict`.
3. When there is no readable object, it asks once more. The second request
   asks for the JSON object only and allows a longer answer
   (1,200 output tokens instead of 400), because a reasoning model can use
   the whole first allowance for thinking.
4. When the second answer cannot be read either, the pair gets the verdict
   `UNSURE` and the error `parse`. The summary counts it as `parse_failed`.

A `parse_failed` pair says nothing about the two notes. Run the plan again
with a different model when the share is high.

### Merge and supersede

The judge answers `MERGE` when two notes teach the same lesson. It answers
`SUPERSEDE` when one note says that a claim of the other is wrong or
replaced, and it names the note that is current.

- **MERGE.** The note that stays gets the text of the other. The notes must
  agree on scope, status and project.
- **SUPERSEDE.** The note that is current stays as it is. The other note
  moves to the archive with one more line in its front matter,
  `superseded_by: <name of the current note>`. Links to it in other notes
  point at the current note. A SUPERSEDE with no named current note is
  skipped.

**Trigger limit.** A note keeps at most 12 trigger items (the limit of the
memory field check). In a merge, the new list is the items of the surviving
note, then the new items of the other note, without duplicates, cut at 12.
The surviving note's own items are never cut. The items that did not fit
are printed and written to the plan file, under `applied`, in
`dropped_triggers`. The archived note still has them, and `undo` brings it
back.

### Archive and undo

For each merged pair, `apply`:

1. writes a backup of both files to `<data dir>/dedup/<run id>/`;
2. writes the merged text to the note that stays (a SUPERSEDE leaves it
   as it is);
3. moves the other note to `<memory folder>/.archive/dedup/<run id>/` (a
   SUPERSEDE adds the `superseded_by` line to the archived copy);
4. marks its database row as archived.

It also writes an undo script, `<data dir>/dedup/<run id>/undo.sh`.

To undo a run, run:

```sh
noblivion dedup undo <run id>
```

Add `--pair A.md,B.md` to undo one pair. Undo restores the files newest
first, also for a SUPERSEDE: the note comes back without the
`superseded_by` line, byte for byte. It stops at a file that changed after
the merge. An undone pair is never proposed again.

The store removes an archived row from the database 90 days after the
merge (`dedup.archive_retention_days`). The archived file stays in
`.archive/dedup/`. NOBLIVION never deletes it.

### Run the sweep on a timer

`noblivion dedup sweep` does the steps `pairs`, `plan` and `apply` in one
run, for a timer.

```sh
noblivion dedup sweep [--max-pairs N] [--daily-cost-cap USD]
```

- It asks no question. It needs the consent from `noblivion dedup consent`,
  `dedup.model` and `OPENROUTER_API_KEY`. Without the consent it sends
  nothing and exits with code 1.
- It judges the best `--max-pairs` pairs (default `dedup.max_pairs`, 50).
  A memory folder with more candidate pairs needs several runs: a pair that
  was judged and merged is gone, and the next run takes the next best pairs.
  A pair that was judged and not merged comes up again in the next run.
- **Cost cap.** The sweep stops before a pair when the money of this UTC day
  (all plans written today, also by hand) plus the dearest pair so far would
  pass the cap. The cap is `--daily-cost-cap`, or `dedup.daily_cost_cap_usd`
  (default `0.5` USD). With no pair judged yet today, the first pair is
  always judged, so the spend can pass the cap by at most one pair. When a
  judge call reports no cost, the sweep stops, because it cannot keep the
  cap. The plan names the reason in `stopped`.
- It writes the plan, as `plan` does, and then applies it. The undo
  script and the run id are the same.
- It prints one line for each step, so you can check the first sweeps and
  label the pairs it merged:

  ```text
  merged beta.md into alpha.md (<root>) cosine=0.953 reason: <reason of the judge>
  superseded old.md by new.md (<root>) cosine=0.91 reason: <reason of the judge>
  ```

  At the end it prints the run id and the undo command:
  `noblivion dedup undo <run id>`.
- It does nothing while the latch exists (see below), and it spends
  nothing then.

Read the output of the first runs before you leave a timer on. For example,
a cron entry for a daily run (the key comes from the environment of the
job):

```text
17 3 * * * <data dir>/venv/bin/noblivion dedup sweep --max-pairs 50
```

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
| 1 | refused, for example no consent, no model, no API key, or no `yes` after the cost estimate; nothing was sent. A `sweep` with no consent also exits with 1 |
| 2 | usage error |
| 3 | database schema error |
| 4 | another process holds the index lock (a `sweep` has then written its plan; run `apply` later) |
| 5 | the latch blocks `apply` |
| 6 | no database: run `/noblivion:setup` (or `install.sh`) first; dedup never makes one |

### The status line

Every `plan` (also `--dry-run`), `sweep`, `apply` and `undo` writes its result to
`<data dir>/cache/dedup-last-run.json` (`NOBLIVION_DEDUP_STATUS_FILE`). At
session start, a hook prints one line from it, for example:

```text
Memory dedup plan <run id> on 2026-10-03: 2 merge proposals. Read the plan, then run noblivion dedup apply <run id>
```

The line names the date of the run. It does not appear when the run has
nothing to report, for example after an `undo`. After a failed `apply`,
the line names the run to undo.

To turn the sweep off for good, set `dedup.judge` to `off`.
