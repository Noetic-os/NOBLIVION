<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Trust

NOBLIVION records when a note is shown and when it is used. From these
records it computes a trust score for each note. A report then lists the
notes to promote, to demote or to retire.

Trust data stays on your machine. It is never sent anywhere.

## What is recorded

There are two kinds of events:

| Event | When |
| --- | --- |
| `recall` | The prompt hook showed the note in a ranked index row. This counts as exposure only. |
| `use` | Claude Code opened the note with the MCP tool, or a guard showed or enforced the note's rule. |

The trust flush finds MCP use in the session transcript. It looks for tool
results of `noblivion_recall` that start with `GROUNDED MEMORY <id>:`.

Each note gets at most one event of each kind per session.

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
trust = (10 * prior + used_sessions) / (10 + trial_sessions)
```

The result is kept between 0 and 1. A trial is a session in which the
note was used. Because a trial is always a use, a note's score never falls
below its prior. A note that is used often moves towards 1.

The store updates the score:

- at once, for the notes in each batch of events;
- at start and every 24 hours, for any note whose score is out of date;
- when you run `noblivion trust recompute`.

`noblivion trust recompute` is a repair command. It prints how many scores
it computed again. Add `--json` for JSON output.

## Trust in ranking

Trust does not change the order of recall by default. Two settings change
this:

- `trust.ranking` (or `NOBLIVION_TRUST_RANKING`): with `shadow` or `on`,
  the store adds the trust score, the number of trials and the prior to
  each row of the ranked index. The store itself does not reorder rows.
  In this version `shadow` and `on` do the same thing.
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

"Promote" means: consider a link from `MEMORY.md`, so that Claude Code
loads it in every session. "Demote" means: consider removing that link.
"Retire" means: consider deleting or merging the note. NOBLIVION never
changes your files based on the report. You decide.

To print the report, run:

```sh
python3 "<plugin dir>/hooks/trust_report.py"
```

Add `--json` for JSON. Add `--cache-only` to print the cached report
without a call to the store. The report is cached in
`<data dir>/cache/trust-report.json`. The trust flush refreshes the cache
about once a day.

## The session start line

At session start, a hook prints one line when the cached report has
entries:

```text
Memory trust: 2 to retire, 1 to promote, 0 to demote. Run noblivion trust
```

The line does not appear when all counts are 0, or after a context
compaction. The command it names does not print the report in this
version. Use `trust_report.py`, as shown above.
