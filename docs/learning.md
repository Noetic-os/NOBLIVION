<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Background learning

The learner turns new, eligible conversations into scoped memories. It is
off by default. Capture, extraction, validation, storage and indexing each
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
  "enabled": true,
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
store locations. Add these fields to that configuration. The most specific
workspace prefix wins. An unmapped workspace cannot contribute. Map
unrelated projects to different roots. A shared namespace alone does not
make project facts global. Preferences and workflows with explicit across-project
evidence are available across roots in the same namespace. Both the destination
root and source root must permit memory use. A general note owned by another root
is read-only during reconciliation: corrections become visible conflicts for an
explicit edit in the owning scope. Project facts never receive this exception.

New-session activation establishes a durable source cursor. Resuming an old
session does not silently import its earlier transcript. Native Codex and
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
Settings, model and budget fingerprints are frozen before calls. A dense
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
