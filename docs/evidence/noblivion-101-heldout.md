<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# NOBLIVION-101 held-out consolidation experiment

This is a fictional, fixed-provider contract experiment. It extends the real-store
and hook evaluation used by `tools/eval_thresholds.py` and
`tools/replay_transcripts.py` without tuning or scoring against their threshold
corpus. The fixtures were authored during implementation. Here, held out means
separate from the existing threshold tuning set. No independent external corpus
was used. This experiment does not establish productive real-user outcome causality
or model synthesis accuracy. Those measurements remain unavailable.

## Frozen inputs and source binding

- Baseline: Noblivion 0.1.13, commit
  `d911ddb136da8c1386082c3aad9ba56ee60fb39c`. The harness verified all 199 tracked
  baseline files against that immutable Git tree before execution.
- Corpus: `tools/eval/consolidation_heldout.json`, SHA-256
  `8f3c1a641c2840d55cf12ff24df4b9486522836443f54afbe2a50cfbc0a1ada0`.
- Labels: 40 particular fictional configuration settings, one dated correction
  from another session, eight external configuration tasks, and two queries with
  no relevant memory. None occurs in `tools/eval/thresholds.json`.
- Provider: `fixed-extractive-fixture-v1`, deterministic exact extraction and
  scripted distinctness responses, temperature zero, 50 declared fixture tokens
  per response, known zero price, no network or model download. The same provider
  and settings run against both product versions.
- Worker settings: product defaults. Budgets: 256 calls, 5,000,000 accounted
  tokens, 10,000 reserved seconds, known zero cost. Automatic synthesis is disabled
  in this extraction-and-context outcome experiment. Separate synthesis admission
  and workflow qualification tests cover the new controlled stages.
- `tools/eval/consolidation_manifest.json` freezes the corpus, executor and
  settings. The result JSON binds the harness and fifteen changed runtime files with
  SHA-256 hashes. Its Git revision can name the worktree base; file hashes bind the
  code actually executed before commit.

## Measured fixture results

| Actual product source | Memory use | Retained particulars | External file tasks | Useful recall | Stale guidance | Empty full-session context for unrelated queries |
| --- | --- | --- | --- | --- | --- | --- |
| 0.1.13 baseline | On | 12/40 | 4/8 | 4/8 | 0/8 | 0/2 |
| 0.1.13 baseline | Off | 12/40 | 0/8 | 0/8 | 0/8 | 2/2 |
| Changed code | On | 40/40 | 8/8 | 8/8 | 0/8 | 0/2 |
| Changed code | Off | 40/40 | 0/8 | 0/8 | 0/8 | 2/2 |

Both versions applied the dated correction without stale guidance. No stored
extractive note lacked a labelled source statement. This is not a broader
unsupported-inference rate: synthesis accuracy is explicitly unavailable here.

Both versions run their actual `learning_summary.py` SessionStart hook and
existing `recall_hook.py` UserPromptSubmit hook. Changed code additionally runs
`learning_context.py` on UserPromptSubmit. The external task oracle receives the
combined startup and prompt memory. Both versions project accepted notes to
files, index them, and use the same deterministic bag-of-words fixture embedder
through the real embedding service. They use a real isolated SQLite store and
its authenticated loopback listener proof. No retrieval-quality conclusion
follows from that fixture embedder.

The new query hook returns empty context for both unrelated queries (2/2).
That is a per-hook result. The complete memory context remains nonempty in both
memory-on versions because their startup summaries remain present; the existing
prompt recall also returned context for these negative fixture queries. Thus
full-session empty context is 0/2 for both memory-on versions. Both memory-off
versions deliver no context (2/2). The JSON reports these components separately.

`tools/eval/consolidation_task.py` receives the delivered context and requested
key. It never receives the expected value. It writes an external JSON
configuration only when memory supplies one unambiguous value. A separate oracle
reads the file and compares it with the held-out label. An assistant reply,
citation or execution counter cannot pass this oracle.

## Recovery and accounting

Each version also receives an injected provider failure on its second request,
then resumes through a fresh worker and a reopened SQLite connection. Both resume
with no duplicate logical keys. Baseline recovery retains 12/40 labelled
particulars; changed-code recovery retains 40/40. The baseline acknowledges the
saturated extraction window. Changed code retains three saturated page receipts
and one processed continuation receipt before atomic admission.

Main extraction and correction use 13 accounted calls in the baseline and 74 in
the changed code. This workload is larger because it processes and reconciles all
40 lessons. The work factor is `13/74 = 0.175676`, defined as baseline accounted
calls divided by changed accounted calls. It is not a measured speedup. Declared
fixture output token totals are 650 and 3,700. Durable reservation totals are
61,858 and 366,520 tokens, and 780 and 4,440 seconds; those are conservative budget
charges, not tokenizer counts or measured inference latency. Total replay wall
time is recorded separately in the JSON and includes setup, hooks, external file
tasks and the recovery experiment.

Spend is zero because the explicitly free provider is in process. Price factor
is `null` because both prices are zero. Real-user outcome effect and synthesis
accuracy are `null` with explicit reasons. No population benefit, paid-provider
saving or model-quality improvement follows from these small fixtures.

## Reproduce

From an isolated checkout containing the baseline Git object:

```sh
mkdir -p work/heldout-baseline
git archive d911ddb136da8c1386082c3aad9ba56ee60fb39c | tar -x -C work/heldout-baseline
.venv/bin/python tools/eval_consolidation.py \
  --baseline-source work/heldout-baseline \
  --output docs/evidence/noblivion-101-heldout.json
.venv/bin/python -m pytest -q tests/test_learning_pages.py tests/test_eval_consolidation.py \
  tests/test_hook_learning_context.py
```

The held-out fixtures stay frozen when implementation changes. Regenerate the
result after changes to bound runtime files; do not tune labels or expected values
to a failing implementation. The committed JSON contains exact task outcomes,
coverage receipts, source hashes, conservative accounting and limitations.
