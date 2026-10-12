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
metadata, not authenticated identities. Contribution settings are unchanged.
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
