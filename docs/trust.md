<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Trust

Trust in NOBLIVION is usage evidence for one report. NOBLIVION records when
a note is shown, when it is used and when it is contradicted. From these
records it computes a trust value for each note. The report then lists the
notes to retire, to promote or to demote.

Trust does not say whether a note is correct. By default it does not change
the order of recall either (see "Trust in ranking" below).

Trust data stays on your machine. It is never sent anywhere.

## What is recorded

There are three kinds of events:

| Event | When |
| --- | --- |
| `recall` | The prompt hook showed the note in a ranked index row. This counts as exposure only. |
| `use` | Claude Code opened the note with the MCP tool, or the guard denied a command because of the note's rule. |
| `contradict` | Claude Code overrode a guard deny of the note's rule with `# guard-ok: <reason>`. |

A guard that only shows a rule next to a command (guard rows or label rows)
records no event. Showing a rule is not a use.

A deny is a use only when it is not overridden. The guard cannot know that
when it denies, because the override comes later, as a retry of the
command. So the guard records the deny as `use` and the override as
`contradict`. A deny and its override in one session lower the score below
the prior.

NOBLIVION does not record a `contradict` when the user corrects Claude Code
on the topic of a recalled note. The stop checks detect a correction prompt
by its words, but they do not know which note the correction is about. That
signal is not reliable enough to lower a note's score.

The trust flush finds MCP use in the session transcript. It looks for tool
results of `noblivion_recall` that start with `GROUNDED MEMORY <id>:`.

Each note gets at most one event of each kind per session.

The store keeps a `contradict` event as a row of kind `contradiction`. The
database schema has had that kind from the start, so no migration is needed.
Events stored by an older version have no `contradiction` rows, and their
scores stay as they were. An older version also recorded guard rows as
`use`. The store cannot tell those old rows apart, so they still count. A
spool line with such an event that was not sent yet is dropped.

## How events reach the store

1. The hooks append events to a spool file:
   `<data dir>/cache/by-session/<session id>.trust-events.jsonl`. This takes
   less than a millisecond.
2. At the end of each turn, the trust flush hook (a Stop hook) starts a
   background worker and exits at once.
3. The worker reads the transcript, adds the `use` events it finds, and
   sends the new events to the store in batches.
4. The worker also retries up to 5 older sessions whose events were not
   sent.
5. Spool files are removed 7 days after they were sent, or after 30 days
   when they were never sent.

To stop all of this, set `NOBLIVION_TRUST_EVENTS=0` or `trust.events` to
`false`.

## The score

Each note starts at a prior value:

- `0.5` for a note from a memory file;
- `trust.prior_mined` (default `0.3`) for a transcript-mined note.

The score is:

```text
u_eff = max(0, used_sessions - 2 * contradicted_sessions)
trust = (10 * prior + u_eff) / (10 + trial_sessions)
```

The result is kept between 0 and 1. A trial is a session in which the
note was used or contradicted. A note that is used often moves towards 1.
Each session with a contradiction is a trial and takes 2 uses off, so it
lowers the score, also below the prior. A `recall` is not a trial, so a
note that is shown and not used keeps its score.

The store updates the score:

- at once, for the notes in each batch of events;
- at start and every 24 hours, for any note whose score is out of date;
- when you run `noblivion trust recompute`.

`noblivion trust recompute` is a repair command. It prints how many scores
it computed again. Add `--json` for JSON output.

`noblivion trust report` and `noblivion trust recompute` exit with code 3
on a database schema error, with code 5 when the database stays locked
or another database error occurs, and with code 6 when there is no
database. They then print one line and no
traceback.

## Trust in ranking

Trust does not change the order of recall by default, and it stays off by
default. It turns on by default only after a time-split test shows a
ranking gain: fit on older events, then test on newer events. NOBLIVION
does not have that test yet.

Two settings change the order for you:

- `trust.ranking` (or `NOBLIVION_TRUST_RANKING`): with `shadow` or `on`,
  the store adds the trust score, the number of trials and the prior to
  each row of the ranked index. The store itself does not reorder rows.
  - `shadow`: the trust values are computed and reported, but never
    applied. The prompt hook computes the factor and logs it to
    `<data dir>/cache/trust-rank.jsonl`, but keeps the order, even when
    `NOBLIVION_RECALL_INDEX_TRUST` is on.
  - `on`: the prompt hook applies the factor when
    `NOBLIVION_RECALL_INDEX_TRUST` is on.
- `NOBLIVION_RECALL_INDEX_TRUST`: with `1`, `on`, `true` or `yes`, the
  prompt hook multiplies each row's score by a trust factor. With
  `shadow`, the hook computes the factor and logs it to
  `<data dir>/cache/trust-rank.jsonl`, but keeps the order.

The trust factor is `trust / prior`, kept between `0.8` and `1.25`. A note
with fewer than 5 trials, or with no trust values, gets a factor of 1. The
hook factor needs the trust values, so set `trust.ranking` to `on` as well.

## The trust report

The report has three lists. It covers memory-file notes only, never mined
notes.

| List | A note is listed when |
| --- | --- |
| retire | It was shown in at least 20 sessions, never used, first indexed at least 30 days ago, and `MEMORY.md` does not link it. |
| promote | Its trust is at least 0.7, it has at least 5 trials, it was used in at least 20% of its sessions over at least 14 days, and `MEMORY.md` does not link it. |
| demote | `MEMORY.md` links it, and it was not used in 30 days. |

Each row shows the trust value, the trials, the shown and used sessions,
and the contradicted sessions when there are any. A contradiction lowers
the trust value, so a contradicted note is less likely to be promoted.

"Promote" means: consider a link from `MEMORY.md`, so that Claude Code
loads it in every session. "Demote" means: consider removing that link.
"Retire" means: consider deleting or merging the note. NOBLIVION never
changes your files based on the report. You decide.

To print the report, run:

```sh
<data dir>/venv/bin/noblivion trust report
```

It reads the database directly, so the store does not need to run. Add
`--json` for JSON. Add `--limit N` to show at most N notes per list
(default 50).

The hook script `python3 "<plugin dir>/hooks/trust_report.py"` prints the
same report through the store. Its `--cache-only` option prints the cached
report without a call to the store. The report is cached in
`<data dir>/cache/trust-report.json`. The trust flush refreshes the cache
about once a day.

## The session start line

At session start, a hook prints one line when the cached report has
entries:

```text
Memory trust: 2 to retire, 1 to promote, 0 to demote. Run noblivion trust report
```

The line does not appear when all counts are 0, or after a context
compaction.
