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
| `use` | Claude Code opened the note with the MCP tool, or the guard denied a command because of the note's rule. With `trust.citation_use` on, also: a reply cited the note (see "Citations and corrections"). |
| `contradict` | Claude Code overrode a guard deny of the note's rule with `# guard-ok: <reason>`. With `trust.correction_contradict` on, also: the user corrected a reply that cited the note (see "Citations and corrections"). |

A guard that only shows a rule next to a command (guard rows or label rows)
records no event. Showing a rule is not a use.

A deny is a use only when it is not overridden. The guard cannot know that
when it denies, because the override comes later, as a retry of the
command. So the guard records the deny as `use` and the override as
`contradict`. A deny and its override in one session lower the score below
the prior.

A user correction and a citation in a reply are two more signals. Both
are off by default. See "Citations and corrections" below.

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

## Citations and corrections

Two settings add events from the session transcript. Both are off by
default, because their precision on a small labelled set was not high.

| Setting | Event | Default |
| --- | --- | --- |
| `NOBLIVION_TRUST_CITATION_USE` or `trust.citation_use` | `use` when a reply cites a shown note | off |
| `NOBLIVION_TRUST_CORRECTION_CONTRADICT` or `trust.correction_contradict` | `contradict` when the user corrects a reply that cited a note | off |

Both also need the trust events (`trust.events`).

When one of them is on, the prompt hook writes the name and the rule text of
each note it shows to
`<data dir>/cache/by-session/<session id>.trust-shown.jsonl`, once per note
and session. The trust flush worker reads the main transcript in the
background, so the hooks stay fast. It does not read subagent transcripts.

### Citation as `use`

A reply cites a note when all of these hold:

- the prompt hook showed the note earlier in the session;
- one sentence of the reply names the note (its file name, at least 10
  characters and 2 parts, such as `feedback_no_git_stash`), or repeats 8
  words of its rule in a row, with at least 4 content words among them;
- that sentence does not set the note aside. Words such as "does not
  apply", "outdated", "ignore", "override", "could not", "missing",
  "index" or "showed" exclude it.

A tool call is not a reply: a note name in a command does not count.

### Correction as `contradict`

A user prompt contradicts a note when all of these hold:

- the stop checks read the prompt as a correction ("no, ...", "wrong",
  ...);
- the prompt does not hold Claude Code to a note. Words such as "you
  ignored", "you forgot", "again", "I told you" or "remember" exclude it,
  because then the note is right;
- the note was shown in the last 2 prompts, and not only with the
  correction itself;
- a reply cited the note after it was shown and before the correction;
- the prompt names the note, or it shares at least 3 content words with
  the note's rule and name, and these are at least a quarter of the
  prompt's content words;
- no other note meets the same tests.

The words of a correction do not say whether the note led Claude Code wrong
or whether Claude Code broke a right note. A wrong `contradict` takes 2
uses off a good note. That is the reason this signal is off by default.

### Measured precision

The labelled set is in `tests/trust_signal_cases.py`: made-up sessions
with positives, near misses and hard cases. Precision is the share of the
recorded events that the label holds.

| Signal | Cases | Events found | True | Precision | Missed |
| --- | --- | --- | --- | --- | --- |
| citation, first run | 16 | 9 | 6 | 0.67 | 0 |
| citation, now | 16 | 6 | 6 | 1.00 | 0 |
| correction | 13 | 5 | 4 | 0.80 | 0 |

<!-- citation-precision: 6 of 6 -->
<!-- correction-precision: 4 of 5 -->

The citation rule first counted a reply that listed the shown notes and a
reply that could not open a note. Two groups of exclusion words were added
after that run. The second number is on the same set, so it is not an
independent test. The correction rule counts a correction that agrees with
a note that Claude Code cited and then broke. A test keeps this table equal
to the measured numbers.

To try a signal, turn it on for a while and read the trust report. Turn it
off again when the report shows notes that you know are wrong.

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
default. Turn it on only after a time-split test on your own store shows a
ranking gain: fit on older events, then test on newer events. See "The
time-split test" below.

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

## The time-split test

The time-split test tells you whether trust ranking would help you. It
runs on your own store. NOBLIVION ships no event data, so there is no
result for all users.

```sh
<data dir>/venv/bin/noblivion trust timesplit
```

What it does:

1. It sorts the sessions by their first event and cuts at the start of the
   first session after 70% of them. `--cut YYYY-MM-DD` sets the cut, and
   `--train-share F` sets the share.
2. It computes trust per note from the events before the cut only, with
   the store's formula and the prompt hook's factor.
3. In each session after the cut, the notes that were used and not
   contradicted are the relevant ones. It compares two orders, trust off
   and trust on:
   - per prompt, when the hook's trust log
     `<data dir>/cache/trust-rank.jsonl` exists. Off is the logged order of
     the index. On sorts it again with the trust factor, as the hook does.
     `--rank-log PATH` names another log, and `--no-rank-log` skips it;
   - per session, from the store alone. The store keeps no rank, so the
     notes shown in the session are put in a random order (the same 50
     orders for both arms), and on sorts them by the factor. This only asks
     whether older trust predicts newer use.
4. It prints the mean reciprocal rank of the first relevant note (MRR) and
   the share with a relevant note in the top 5 (hit@5, `--k N`) for both
   orders, and how many units got better or worse. Add `--json` for JSON.

The verdict is `gain` only with at least 30 test units, a higher MRR with
trust on, and more units better than worse. It uses the per-prompt result
when that has 30 units, else the per-session result.

The command reads the database and the log. It changes no setting. To
collect the per-prompt log first, set `trust.ranking` and
`NOBLIVION_RECALL_INDEX_TRUST` to `shadow` for some weeks: the hook then
logs the order it would have used and keeps the order it shows. Turn trust
ranking on only when your own run says `gain`.

It exits with the same codes as `noblivion trust report`.
