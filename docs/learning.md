<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Background learning

The learner turns new, eligible conversations into scoped memories. It runs
automatically after you configure a scope and a valid local model. Set
`"enabled": false` in the store policy to turn it off. Clients still need
explicit permission to contribute. Capture, extraction, validation, storage and indexing each
have durable state. A delivery receipt means that the store accepted the
source batch. It does not mean that a memory passed validation or can be
recalled yet.

## Enable a store

1. Install the updated package with the normal installer. The database
   migration makes a backup before it changes an existing database.
2. Create `learning.json` in the store data directory. Use mode `0600`.
3. Map each namespace and root to its memory folder explicitly. The root
   must equal the name of the folder above `memory`.
4. Configure an already running local model and finite budgets. The learner
   accepts only an explicitly priced, zero-dollar loopback provider. It
   rejects redirects, network proxies, external endpoints and unknown prices.
5. Restart the store through the normal installer or launcher. Check
   `noblivion learning status` before enabling client contribution.

Example, with fictional folders and an operator-selected local model:

```json
{
  "scopes": [{
    "project": "claude_code",
    "root": "demo-project",
    "memory_dir": "/srv/memories/demo-project/memory"
  }],
  "provider": {
    "endpoint": "http://127.0.0.1:11434/v1",
    "model": "your-resident-model",
    "local": true,
    "cost_per_million_tokens": 0,
    "reasoning_effort": "low"
  },
  "budget": {
    "window_seconds": 86400,
    "max_calls": 20,
    "max_tokens": 100000,
    "max_seconds": 300,
    "max_cost": 0,
    "max_concurrency": 1
  }
}
```

The store namespace must match `project`. A malformed configuration disables
learning with an error in the store log. It never supplies permissive defaults.
Existing accepted memories remain available when contribution is disabled.

For a reasoning model that fills the output limit before returning JSON, configure
both limits together. For example, add this object beside `provider` and `budget`:

```json
"worker": {"max_output_tokens": 8192, "timeout_seconds": 180}
```

The hard limits are 8,192 output tokens and 300 seconds per call. The worker
extends its lease before the call to cover the configured timeout plus 30 seconds.
Check the existing time and token budgets before selecting larger limits: a
180-second reservation still consumes 180 seconds even when the call finishes
sooner. The example daily time budget above therefore permits one such call;
a job needing several calls can wait for the next budget window. The provider
also supports `"reasoning_effort": "none"`. Measure that setting with the selected
local model before using it. Neither example enables a remote or paid provider.

### Parallel model calls

`budget.max_concurrency` sets how many worker loops the store runs, and so how
many model calls run at once. The default is 1. A model server that serves
several requests at once (continuous batching) can then run several jobs at
the same time. Set the value to the number of requests the server can serve
at once, or lower. The store starts at most 16 worker loops; a value of 0 runs
no worker.

- Each worker loop claims a job through the job lease and uses its own
  database connection, so no two loops run the same job.
- One supervisor reads `learning.json` every 5 seconds, prunes, writes the
  accepted notes to the memory folders and indexes them. The worker loops do
  not do this work.
- A change of `max_concurrency` takes effect on the next read, up and down,
  with no restart. After a scale-down, a loop above the new count ends after
  its current job. Until then, the loops take turns for the calls, so a
  started job waits for a free call instead of a `BudgetExceeded` deferral.
- A worker that waits for a free call keeps its job lease alive. When the
  store stops or learning is turned off during the wait, the worker gives the
  job back with `LearningWorkerStopped`. The job keeps its attempt and its
  progress.
- Each call reserves its budget in one database transaction, so parallel calls
  never spend past `max_calls`, `max_tokens`, `max_seconds` or `max_cost`.
  The ledger also refuses a call when `max_concurrency` reservations are open
  in the database, from all processes together.
- A call that only `max_concurrency` refuses is retried after
  `worker.poll_seconds`, and the job keeps its attempt. This happens after a
  restart during a model call: the stopped process leaves the call reserved
  until its job lease ends, at most `timeout_seconds` + 30 seconds later.
- When the store stops, a worker that waits for a call slot takes no slot, so
  no new call starts. A call that has started runs to its end or dies with the
  process; its reservation stays and counts against the budget.
- The store does not stop for idle while one of its workers holds a job, or
  while a pending job can run now: it is due, it has attempts left, its scope
  is enabled and not paused, and the budget window has room for one more call.
  A job that no worker can take does not keep the store up. At a clean stop
  (SIGTERM or idle) the store waits up to 5 seconds for its workers, then gives
  back each job they still hold: the job is pending again with
  `LearningWorkerStopped`, keeps its progress, and gets back the attempt that
  its claim counted. Before 0.1.30 the lease expired instead, and each stop
  cost each leased job one attempt (NOBLIVION-248).
- A worker loop claims its next job as soon as the last one is done. It waits
  `worker.poll_seconds` only when no job was done. So jobs run back to back,
  with no 5 second pause, also at `max_concurrency` 1.
- At N worker loops, `max_calls` and `max_seconds` run out about N times
  faster in wall time. `max_seconds` charges at least `worker.timeout_seconds`
  for each call, because each call reserves that time and a call that ends
  sooner gives none of it back.
- Jobs that add notes to one root at the same time see the notes of the other
  jobs appear. Such a job keeps its finished semantic comparisons. It compares
  each candidate again only with the notes that are new or have a new
  revision. A removed note needs no new comparison. A candidate that a removed
  or revised note blocked gets a fresh comparison with all current notes. Each
  new comparison is one more model call. The job never repeats its extraction
  calls. The refresh builds the comparisons for every current note. The write
  transaction stores an add only when its snapshot equals the current note
  set, as before. The job progress and learning
  status count the changed note sets in `semantic_restarts` and the kept
  comparisons in `semantic_reused` (NOBLIVION-250).

### Context and output deferrals

Existing notes are checked separately. A safe statement stays available when an
unsafe title or evidence quote can be removed. Safe evidence references remain
in the diagnostic metadata. If the statement itself is unsafe or exceeds the
context budget, the worker records its identity, revision and exclusion reason
without storing or submitting its body. A claim that could depend on that hidden
statement waits for the dependency to be resolved. A title or keyword mismatch
alone does not prove that hidden material is unrelated. Corrections whose old
evidence is withheld become visible conflicts; they never replace the old note.
All provider inputs still pass the complete secret check.

A `Provider:length` result means the model output reached the output token
cap. The cut-off output is never kept. For an extraction window, the worker
splits that window in two halves and goes on with each half. A half that also
ends on `length` splits again. A window of several parts splits between parts. A
single part splits in the middle, but not below 2048 characters. Nothing splits
past three levels (eight pieces). At that floor the piece gets a `rejected`
page receipt, its unit index goes into `rejected_windows`, and the job goes on.
So one window makes at most 15 calls that end on `length`. The frozen window
plan and the job fingerprint do not change: the split is recorded per window in
the job progress as `length_splits`, and a piece stores its page receipts at
continuation `64 * piece + continuation`. A job that an older release deferred
on `length` resumes with the split and does not send the held request again.
Extraction contract version 2 keeps the same three levels. So its smallest piece
is twice as large: about 3,000 characters of a 24,000-character window. Version
1 windows of 12,000 characters had no `length` result in 32 measured calls.

For a semantic or derivation call, a `Provider:length` result holds the exact
request. Automatic attempts with the same request remain deferred and make no
model call. A deferral of this kind, and a `ContextDependencyDeferred`
deferral, gives the attempt back at most 24 times per job (about one day at one
hour each). The job counts them in `returned_deferrals` in its progress. After
24, each such deferral counts an attempt, so the job ends after at most
`max_attempts` more runs. An explicit retry renews the count. Changing only the timeout
does not release this hold. After reviewing the cause, change the output limit,
model or reasoning mode in the explicit worker configuration, then let the
pending job resume. The worker retains completed extraction pages and all budget
charges. Source, window, coverage and scope changes cannot use this recovery
path. Do not reset exhausted historical jobs as part of this step.

### Configuration changes during a job

A started job freezes only the request part of the configuration: the window
and overlap sizes, the window count, the context size, the output limit, the
coverage limits (pages, continuations, decisions), the provider type, endpoint,
model and reasoning mode, and the version of the learning prompts and schemas.
A change to any of these fails a started job with `window configuration changed
during a job`, so outputs of two configurations never mix in one job. A new
release that adds a contract version does not fail a started job: a job with
partial work keeps its own version, and a job with no work yet freezes again
under the newest version (see Extraction contract versions below).

Budget limits, `enable_consolidation`, `max_attempts`, `lease_seconds`,
`retry_delay`, `poll_seconds` and `timeout_seconds` are policy. A started job
uses their current values on its next call. So you can raise the budget while
jobs wait with `BudgetExceeded`.

Extraction jobs that releases up to 0.1.24 started also continue: their stored
configuration proves the frozen value, and only its request part is compared
(NOBLIVION-236). Such a job keeps its original freeze, so 0.1.24 still accepts
it under its original `learning.json`.

Derivation (consolidate and draft) jobs differ. A derivation job that 0.1.24
started and that made no model call stored only a hash. It continues only while
the configuration is unchanged, so a budget change still fails it. A derivation
job that resumes on this release is frozen again in the request-only shape.

### Extraction contract versions

Each extraction job freezes the version of the extraction contract when it
starts. A started job with partial work always resumes under its own version.
A new job starts under the newest version. The worker supports versions 1 and 2. Version 2
(NOBLIVION-241) needs fewer model calls per job:

- A window holds two times `window_chars`. With the default 12,000 characters
  a window holds 24,000 characters, and the overlap stays at `overlap_chars`.
  On recorded jobs of a live store the median job needs 11 windows instead of
  25. Three times `window_chars` was measured first, on the local model: 3
  `length` results and 4 rejected replies in 30 window calls. Two times gave 1
  `length` result and 3 rejected replies in 30 calls; version 1 windows gave 0
  and 3 in 32 calls.
- A window, or a piece of a split window, that holds no tool text and no direct
  user text gets a `no_lesson`
  receipt with the reason `no tool or direct user text in the window; no model
  call`. The model is not called, because the evidence checks cannot accept a
  candidate from such a window. A window with only tool output is still sent:
  a fact or a fix proof can come from tool output.
- One semantic call compares up to 8 candidates and up to 64 (candidate,
  target) pairs. Each pair is the same pair that version 1 compares, and each
  pair still needs one explicit relation. A reply with a missing, extra,
  repeated or unknown candidate or target is rejected, as in version 1.

Version 2 keeps every version 1 prompt and schema unchanged and adds only the
batched semantic prompt and schema. Consolidation and draft jobs use only
version 1 text, so they freeze version 1. A job that 0.1.28 or an older release
started resumes as version 1 when it has partial work: same windows, one
semantic call per comparison task. Partial work is a completed window, a page
receipt, a semantic stage, a decision, a pending format retry, an output-limit
hold, a length split (`length_splits`) or a rejected window. A version 1 job with none of these freezes again under the newest version
when a worker leases it, so it also needs fewer calls; its progress records
`refrozen_from: 1`. A version 1 job whose freeze no longer matches the
configuration still fails with `window configuration changed during a job`.

The version 2 semantic stage stores a digest of its batches when it starts. A
resume builds the batches again; if the digest differs, the job fails with
`semantic batches changed during a job` instead of skipping or repeating
comparisons. A semantic stage without a digest resumes and gets one.

A batch sends only the tasks whose candidate is still an add. A batch answer
that parses but does not match the batch is asked again up to 2 times. Past
that bound, each task of the batch is asked alone with the version 1 prompt,
with its own 2 re-asks. So one bad item of a batch answer drops only its own
candidate, as in version 1. The progress row lists the tasks answered alone in
`fallback_done`, so a resume does not ask them again.

A window of 24,000 characters with 24,000 characters of existing notes needs
about 16,000 input tokens. Keep `window_chars` at a value whose double, plus the
existing notes and the output limit, fits the model context.

The number of semantic comparisons grows with the number of notes in the
scope: each new candidate is compared with every managed note. Batching cuts
the calls for these comparisons by up to 8 times, but the count still grows
with the note count.

A downgrade to 0.1.28 or older fails each extraction job that started or froze again
under contract version 2 with `window configuration changed during a job`, before any
provider call. A job that names a contract version this release does not know
fails with `unsupported extraction contract`. To recover, upgrade again,
then run `noblivion learning retry <id>` for each failed job.

A downgrade to 0.1.24 fails each job that this release froze: every job it
started, every resumed derivation job and every job frozen again by an
output-limit recovery. The failure happens before any provider call, and the
job keeps its progress and page receipts. To recover, upgrade again, then run
`noblivion learning retry <id>` for each failed job.

Learning status includes `failure_reasons`, `context_rejections`, `outcomes`, and
`provider_actual`. Provider reasons are `length`, `incomplete`, `empty`, `timeout`,
`transport`, and `invalid`. Schema and budget failures have separate counts.
`provider_actual` totals measured calls, seconds and reported tokens from retained
jobs, including failed calls. Unknown usage is not reported as a measurement.
The existing `budget` totals remain conservative reservations, including unknown
usage after a crash or transport failure. `outcomes` separates committed decisions,
deferred jobs, exhausted failures and corrections deferred as visible conflicts.
`candidate_mismatches` counts dropped and rejected extraction candidates by class,
without text. A candidate that fails a check of its own values is dropped, and
the valid rest of the response is kept. These are the text length, logical key,
scope, evidence, date, reconciliation target, correction and fix proof checks.
These failures reject the whole response: a secret, invalid JSON, the response
envelope, the candidate schema (fields, field types, enum values), an authority
claim, quoted or injected evidence and an unknown source. A dropped candidate is
checked for them too, in all its fields and references. No candidate of a
rejected response is stored. When a candidate fails one of the five response
failure classes (an authority claim, quoted or injected evidence, an unknown
source, a secret in a candidate field or an evidence span mismatch), the job
does not fail. The window gets a `rejected` page receipt with no candidate, its
unit index goes into `rejected_windows` in the job progress, and the job goes
on with the next window. The worker does not call the model again for that
window. When this happens on a continuation, the candidates of the earlier
pages of the window stay: each page is a separate response, validated on its
own. Learning status counts these windows in `rejected_windows`, and
`noblivion learning retry` keeps their receipts. The other response failures
(the candidate schema, an unclassified failure, a secret in the raw response,
invalid JSON after the format retry, the envelope and the version) still fail
the job, as before. When a continuation page proposes twelve candidates and
all twelve are dropped, the window makes no more progress. That page gets an
`exhausted` receipt with no candidate, its unit index goes into
`exhausted_windows` in the job progress, and the job goes on with the next
window. The candidates of the earlier pages of the window stay. Learning
status counts these windows in `exhausted_windows`. The hard decision limit
(`max_decisions`) still ends the job. The
records of one job use at most 256 KiB. An exact-quote failure has a detail: `repeated`,
`whitespace`, `case`, `typography` or `absent`. The progress row of each job keeps
one record per page (`unit`, `continuation`) with the cited source IDs, so a
measurement can join it to the page spans. `context_rejections` also counts
`ContextLegacyMarkerReplaced`: a read-only legacy note whose only finding was an
old redaction marker, sent with the neutral mark `[...]` instead.

Before enabling paused learning, verify provider readiness and budgets, capture
cadence and retention, and one bounded capture-to-accepted-note-to-recall path.
Use normal existing context for that check. Clean fixture yield does not prove
useful production yield when existing notes still require dependency deferral.

## Enable clients

Codex uses its adapter configuration. Claude uses `learning-client.json` in
the store data directory. Both clients need explicit `project_mappings`:

```json
{
  "learning": {"contribute": true, "use": true},
  "project_mappings": [{
    "workspace": "/srv/work/demo",
    "namespace": "claude_code",
    "root": "demo-project",
    "memory_dir": "/srv/memories/demo-project/memory"
  }]
}
```

Keep the existing adapter settings, including the repository, state and
store locations. The Claude file needs no `repo_root`. The hooks use the
installed release they run from, so the file cannot point at an old folder. Add these fields to that configuration. The most specific
workspace prefix wins. An unmapped workspace cannot contribute. Map
unrelated projects to different roots. A shared namespace alone does not
make project facts global. Preferences and workflows with explicit across-project
evidence are available across roots in the same namespace. Both the destination
root and source root must permit memory use. A general note owned by another root
is read-only during reconciliation: corrections become visible conflicts for an
explicit edit in the owning scope. Project facts never receive this exception.

New-session activation establishes a durable source cursor. Resuming an old
session does not silently import its earlier transcript. A new session counts
wherever it starts, in a mapped folder or not. If it later runs in a mapped
workspace, that scope captures it from its first event there. The earlier
part of the transcript, from the unmapped folder, is not imported. Native Codex and
Claude records preserve roles, tool call IDs, source hashes and complete
text through bounded chunks. The client rejects detected secrets before
spooling. An oversized or malformed source remains a visible failure; it
must not be accepted as a shortened successful input.

For a remote store, set `learning.delivery_argv` to an explicit command
argument list that runs `codex/capture.py --receive --config <remote-config>`
on the store host. An SSH command is one option. Do not put credentials in
the argument list. The remote configuration must authorize the same scope.
The local spool retains each batch until it receives a matching receipt.
A later hook or flush retries disconnected delivery with bounded backoff.

Install hooks through the supported client installer. Review changed Codex
hook definitions through its normal trust interface. Never edit trust hashes.

## What the learner accepts

The versioned extractor contract covers preferences, workflows, project
facts, decisions and proven fixes. Each candidate names exact source evidence.
Validation checks source hashes, spans, roles, dates and the candidate text.
User preferences require direct human evidence. A proven fix requires the
failure and the successful verification. Quoted instructions and claims of
authority are data; they do not grant permission.

The learner reads existing scoped memories before reconciliation. Learned
notes have stable IDs independent of titles and optimistic revisions. A
correction preserves permitted history, withdraws the old indexed content,
and queues the new file projection. Legacy manual notes are included as
read-only reconciliation context. A conflict with one is surfaced for an
explicit edit instead of silently replacing its evidence.

Session-start hooks deliver compact summaries derived from current accepted
statements and evidence references. Both the client and store enforce use controls. Historical correction text is not included in current recall.
Inspect the note for its detailed evidence and revision history.

## Controls and recovery

Run `noblivion learning --help` for inspection, controls, edit, exclusion,
forget, summary and job retry commands. Use and contribution are independent.
Session settings can restrict a project setting; they cannot override a
project opt-out. Pausing stops work without deleting accepted notes. Excluding
a source withdraws its dependent notes. Forgetting removes retained content
and derived text while keeping an empty identity tombstone so stale files
cannot restore the forgotten fact. This does not delete the native client transcript
or older external backups; manage those through their own retention controls.

`noblivion learning status` also lists pending extraction jobs under `pending`.
Each entry gives the job, its age in days and the reason it waits: a paused
session or scope, contribution off, a provider error, the budget, or a worker
that has not claimed it yet. A job held by a pause for more than 7 days has
`"stale": true` and counts in `pending.stale`. Status never drops or cancels
a job. Resume the session or scope, or retry it, to continue.

### Capture, jobs and growth

Capture is cheap to start and bounded in size (NOBLIVION-143):

- Claude Code and Codex sessions are captured at session start, at each compaction and at session
  end. Other Codex events only track scope entry, so entering a different scope cannot capture
  earlier work from another scope. They do not read the transcript or start delivery.
- Later batches extend the session's unstarted extraction job until it holds about 200,000
  characters or a worker has started it. Then a new bounded job starts, with the newest earlier
  sources as context. This preserves late evidence; it is not a lifetime one-job limit.
- A job names its sources as a range of per-session sequence numbers (`learning_sources.seq`, the
  `learning_job_ranges` table), not as one row per source. A source stored by an older release
  keeps its `learning_job_sources` rows.
- The session-start hook stores how many notes and active claims a prompt context can use. When it
  is 0, the prompt hook of that session makes no store call. A note accepted later in the same
  session shows from the next session on.

Pruning deletes data, so it is off until you ask. Run `noblivion learning prune` to count what it
would drop (nothing is changed), and add `--apply` to drop it. It cancels pending extraction jobs
whose creation and newest capture are older than `--pending-days` (14), drops expired source links,
budget rows and extraction pages of terminal jobs after `--retention-days` (14), and drops old
eligible sources that no note, claim, draft, conflict or live job uses. An exhausted job within
`--retention-days` keeps its sources and pages, so `noblivion learning retry` can still run it.
Retry does not reset the age of the job: when its creation and its newest capture are older than
`--pending-days`, the next prune cancels the retried job while it is still pending. Audit-linked job rows and
batch receipts remain.
Excluded and forgotten sources stay, because their hash blocks the same text. To run it hourly
in the store worker, add
`"prune": {"enabled": true, "pending_days": 14, "retention_days": 14}` to `learning.json`.
A job pruned this way cannot be retried.

To measure the rows and bytes a release leaves for a number of simulated sessions, run
`python tools/replay_learning_growth.py --events session-points`. It makes made-up transcripts
in a temporary folder and never opens a live store.

Add `--days 22 --pending-days 7 --retention-days 7` for a multi-day retention replay.
The report separates initial growth from steady-state growth, logs pruning counts and checks
retained accepted evidence and recall. It uses no model and cannot establish production learning
yield. Receipt metadata continues to grow; retention does not promise a fixed database file size.

`noblivion learning status --project <namespace> --root <root>` lists up to
20 open conflicts and their total count. Run `noblivion learning inspect
conflict:<id> --project <namespace> --root <root>` with an ID from that list to
read the reason, proposed statement, target revision, owning scope and exact
source evidence. Both scope arguments are required. Only the proposal's source
scope or target owner scope can inspect its contents. A cross-root proposal
appears in both its source and target scopes. Inspecting a note also lists its
conflict history. A target revision change or note/source exclusion marks a
conflict cleared from the open list; its retained history still explains why.
This does not apply a proposal or establish that the correction was accepted.
Use the existing explicit edit or exclusion commands in the owning scope.

The authenticated local API exposes `/learning/capture`, `/learning/control`,
`/learning/status` and `/learning/summary`. Recall routes accept `session_id`
and enforce use controls on both search and fetch. Requests without a valid session ID use a conservative fallback:
any explicit session use-off setting in the requested root blocks those reads
until it is cleared. Requests with a valid session ID retain their own session
controls. The same rule applies to each source root of shared general memories,
and to search, fetch and summary. This protects clients whose native memory-tool
process does not receive a session ID; it can also suppress recall for other
unidentified sessions in that root. Session IDs are client-supplied routing
metadata, not authenticated identities. Public MCP reads always use this
unknown-session fallback: the MCP process removes inherited session IDs because
they may be static or belong to another conversation. Native hooks continue to
use the session ID in their event payload. Contribution settings are unchanged.
Root-scoped fetches cannot read another project's note. Superseded, excluded and forgotten facts remain
out of current recall after repeated scans and warm-cache requests.

Jobs have leases, attempts, retry times and visible exhaustion. Every model
call reserves persistent request, token, time and cost capacity first.
Uncertain failures retain their reservation. Extraction progress is saved
between windows so recovery does not process a completed prefix again.
An accounting error prevents the provider call.

File projection uses a durable outbox. Index acknowledgement checks the
actual file hash against the indexed row. Indexing or embedding failure does
not undo accepted source input. Keyword recall can work without embeddings;
an accepted note waiting for its file or index remains visibly pending.

## Verification boundaries

Test with isolated data and memory folders. Do not put synthetic canaries in
the shared corpus. A full verification includes a new eligible client
conversation, an automatic accepted write, a later independent session's
automatic recall, and another authorized client's access. A direct remember
call or a successful database connection does not establish that result.

Native Codex memory is a separate system. Its documented eligibility delays
and controls must be recorded when comparing results. A documentation table
does not establish equal extraction accuracy or failure recovery.

## Evidence-linked consolidation

Exact accepted notes and immutable source passages remain the authority for
background learning. Generated claims live in separate `learning_claims`,
revision and exact-support tables. An edge names the note revision, source ID,
SHA-256, start/end offsets and complete quoted passage. Batch membership alone
never establishes a claim's support.

Set `"worker": {"enable_consolidation": true}` in the existing `learning.json`
to enqueue consolidation atomically with a completed extraction job. The default
is false. Capture permission, project mapping, contribution controls and local
provider restrictions still apply. An operator can also enqueue existing accepted
notes with `noblivion learning consolidate --root ROOT --session SESSION`.
This creates durable work; it makes no synchronous provider request.

Consolidation uses distinct proposal, complete semantic support and equivalence
stages. Each model request, including format repair and retry, uses the existing
persistent budget reservation and settlement path. A valid citation is only a
reference-integrity result. It does not establish semantic support. A claim stays
staged or conflicted when any premise is unsupported, incomplete, contradictory
or uncertain. An inference can establish only a project fact. It cannot create
preferences, instructions, approvals, permissions or memory controls. Explicit
user guidance requires an exact direct user statement and existing scope evidence.
Dates come from the exact support, rather than the generation time.

A separate complete equivalence verdict can combine supported paraphrases.
The canonical claim receives a new revision and the union of all exact support.
Merged predecessors retain their revisions and an equivalence lineage edge.
Current note and claim snapshots are checked inside the merge transaction.
Different applicability, contradictions and uncertain equivalence do not merge.
Correction, supersession, exclusion, forgetting and root use-off synchronously
withdraw transitive descendants before returning. Reads independently validate
current note revisions, source eligibility, hashes and exact spans. A session
use-off only removes that caller's context caches and serving permission. It does
not withdraw another session's claims. Both source and destination roots must
permit cross-root general guidance.

Query context is available through:

```console
noblivion learning context --root ROOT --session SESSION --query 'service port'
noblivion learning claim CLAIM_ID
noblivion learning drafts --root ROOT
noblivion learning status --root ROOT
```

The authenticated `POST /learning/context` accepts
`{project, root, session_id, query, limit_chars}`. `GET /learning/context` accepts
the same query parameters. The response contains `context`, `claims`, `conflicts`,
`deferred` and `reason`. A disabled caller receives empty context. Nonempty queries
require meaningful term overlap; unrelated queries abstain. An empty query keeps
the startup-summary behavior. The new prompt hook supplies this bounded context
to Codex and Claude. Existing `/learning/summary` clients remain compatible.

Delivered bullets label explicit statements, observations and inferences. They
include scope, support dates, claim/revision IDs and exact support IDs. Unresolved
conflicts include the actual proposed value, date and affected revision, labelled
`unresolved/not admitted`. If a complete conflict cannot fit, affected guidance
is omitted and the response records that disposition. Content is never cut into
a superficially complete claim to meet a size limit.

Default hard limits are twelve proposals per call, 32 exact supports per claim
(configured internal ceiling 64), 512 notes per derivation, 24,000 input characters,
2,000 characters per generated statement, 32 context claims, 12,000 context
characters and eight drafts per pass. Query input is limited to 8,000 characters.
Request settings and the model are frozen before calls; budget and scheduling
policy are not (see [Configuration changes during a job](#configuration-changes-during-a-job)). A dense
extraction response with twelve candidates is saturated. Durable continuation
receipts must establish remaining coverage before atomic admission. The default
maximum is sixteen continuations per unit, 256 pages and 512 decisions per job;
hard ceilings are 32 continuations and 512 pages. A repeated saturated response,
unsupported limit or omitted unit produces visible deferred/exhausted work.
Successful JSON alone does not acknowledge coverage. Status reports include unit
coverage, claim/draft states and deferred derivation work. Explicit retries retain
settled and uncertain budget charges and valid completed page receipts.

Context caching keeps at most one entry per caller/root, expires entries after
ten minutes and keeps at most 256 entries globally. Every serve rebuilds against
current evidence; a cache is never used to bypass validation. Forgetting erases
claim text and revision snapshots, draft text and qualification snapshots,
quoted extraction-page candidates, provider checkpoints and context caches.
Only safe eligibility tombstones remain under the existing logical erasure policy.

## Inert procedure drafts

Draft qualification requires repeated independently sourced successful native
action/outcome records, including the exact input, native tool-call ID, tool name,
observed exit status and applicability. At least two distinct sessions, source
families and outcome hashes are required. Overlapping chunks or copied identical
outcomes cannot qualify by repetition. An assistant's claim of success, a missing
native input, a one-off run or an observed failure cannot qualify. Conservative
false negatives are intentional.

Noblivion renders its own `noblivion-procedure-v1` JSON template and validates its
parser, exact fields, declared tools, read-only command/option grammar and support.
Only a small explicit `git`/`rg` inspection grammar qualifies as read-only;
executable project tests such as `pytest` do not receive that classification.
All generated drafts have `review_required` and `active=false`. Their native input
and exact note/source support are checked again when inspected. This release adds
no execution, automatic activation or permission path. Support changes withdraw
the draft. A reviewable draft is evidence for human review, never operator approval.

## Independent adaptation and evaluation

The receiving-product implementation is owned and maintained entirely by
Noblivion. Design references were copied for inspection from PhoenixDaemon commit
`d7b231283a1ae0bd685e0e846ffedc6d6db77114`:

| Reference | Noblivion-owned adaptation |
| --- | --- |
| `provenance.py` | Transactional exact-support withdrawal and scoped cache eviction in `learning_consolidation.py` |
| `memory_versioning.py`, `sleep_cycle.py` | Claim revisions, canonical merge history and lineage in `learning_consolidation.py` |
| `recon_mm_candidates.py`, `skill_evolution.py` | Independently verified native actions and inert linted review templates in `learning_consolidation.py` |
| `compounding_metrics.py` | Separate work/price accounting in `tools/eval_consolidation.py` |

These are independently implemented adaptations of the inspected design patterns.
No Phoenix import, package, checkout, shared source, runtime service, data store or
synchronized release is required. Coarse batch ancestry is used only for cleanup,
never semantic proof. The implementation does not copy silent text truncation,
stale insight reinforcement, fire-and-forget work or assistant-reply success scores.

The frozen held-out real-store/hook/external-state evaluation extends the existing
recall work. Its results and limits are recorded in
[`docs/evidence/noblivion-101-heldout.md`](evidence/noblivion-101-heldout.md).
Contract and scripted fixture outcomes are reported separately. Productive
real-user outcome causality remains unavailable without an authorized labelled
corpus. Unknown prices, unavailable metrics and zero-denominator factors remain
unavailable; they never become favorable defaults.
