<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Frozen evaluation and paired replay

`tools/eval/thresholds.json` is a tuning fixture. Its threshold curves and
same-fixture gates do not measure generalisation. Keep its old results labelled
as tuning results.

`tools/eval/replay_manifest.json` records SHA-256 hashes of that tuning fixture,
a separate held-out threshold fixture, synthetic tasks and a synthetic runner.
Both fixtures use fictional content. Held-out queries, query IDs, memory IDs
and normalised memory fields must be disjoint from tuning. Normalisation folds
Unicode compatibility characters, letter case and whitespace. Expected memory
IDs and duplicate labels must refer to their own fixture.

The manifest makes changes detectable. It is a reviewable freeze record, not
a signature: a person who can edit both data and manifest can change the freeze.
An independent custodian should reserve a qualification corpus for claims beyond
this fictional fixture. After a
held-out result has influenced tuning, retire that split and freeze a new one.
The public synthetic held-out fixture verifies the evaluation pipeline only.

## Thresholds

1. Use `uv run python tools/eval_thresholds.py --mode tune` for tuning curves.
2. Freeze the selected product settings through normal code review.
3. Use `uv run python tools/eval_thresholds.py --mode evaluate --json heldout.json`
   to measure only the shipped settings on the separate held-out fixture.

Evaluation verifies the frozen hashes and leakage checks before starting the
store. It evaluates one value per threshold, does not choose a best value, and
does not emit curves or dedup pair scores. The JSON report records the manifest
hash, fixture hash and sample sizes. `--check` compares the held-out results to
the held-out fixture's frozen floors. Public synthetic floors are zero because
this fixture does not qualify product accuracy.

The threshold script still uses its existing real embedding backend. It can
download the model if absent. Run it only when the model and download policy
are approved. Split validation and the replay below need no model or download.

## Paired task replay

Run the public protocol demonstration from the repository root:

```sh
python3 tools/eval_replay.py --json replay.json -- \
  /absolute/path/to/python3 /absolute/path/to/repo/tools/eval/replay_task.py
```

Each task runs once with injected memory and once with empty memory. Each arm
starts in a new temporary directory with a separate home and temporary folder.
Arm order alternates. The prompt and initial files are the same in both arms.
The runner receives one JSON object on stdin, with `version`, `task_id`,
`prompt`, `memory_context` and `limits`. It receives no expected output labels.
The runner writes task files in its working directory and returns one JSON
object on stdout:

```json
{"usage": {"iterations": 1, "input_tokens": 0, "output_tokens": 0}}
```

The file oracle compares actual bytes to frozen expected bytes after execution.
It rejects missing, oversized and symlink outputs and checks frozen absent-file labels. Completion is never inferred
from the runner's text, citations, counters or usage. The report records the runner executable hash and includes each
arm's status, file matches, measured duration and pipe bytes. It publishes the
paired pass-rate difference and sample size. Invalid pairs remain in the total
sample and are listed separately. The difference uses valid pairs only; it is
null if there are none. The report explicitly calls these synthetic
harness results; a favourable difference does not show real-user benefit.

Commands are argument lists with an absolute executable, with no shell.
Inherited environment variables, including provider credentials, are removed.
The harness caps task count, invocations (one per arm), input bytes, combined
stdout/stderr bytes, observed artifact bytes and wall time. POSIX runners also receive per-file
size and CPU resource limits. These do not cap total workspace disk use. It kills the process
group on POSIX, including remaining descendants. Usage must be non-negative
integers within the frozen iteration and token budgets. Tokens and internal
iterations are runner-reported, not independently measured or enforced. A
trusted real runner must enforce its own model loop budgets and expose reliable
usage metering. Zero tokens in the supplied runner means it uses no model.

Temporary directories and environment filtering are process hygiene, not a
security sandbox. A trusted runner can still read the host filesystem, use the
network or write outside its working directory. Do not use this tool to bypass tool isolation or native memory eligibility
controls. No provider, key, download,
collection setting or production corpus is configured by this tool.

A genuine foreground client replay can use frozen fictional tasks. Keep its
results separate from the supplied deterministic runner. Record client version,
model settings, actual usage, failures and external task-state evidence. A
failed request without usage is unavailable, not zero cost or task failure.
The public deterministic runner does not establish how a real client responds
to injection. Population-level quality and native memory parity need separate
studies; neither is an acceptance claim of this harness. Keep raw real prompts,
memories and transcripts out of this public repository.

## Recorded synthetic held-out check

On 18 October 2026, the cached `BAAI/bge-small-en-v1.5` embedding model ran
with downloads disabled and the real isolated store and hooks. The sample held
three recall queries (two positive, one negative), two error queries (one
positive, one negative), and three dedup memories (one duplicate pair among
three pairs). All six shipped threshold paths returned the expected labels,
with no false hit or missed positive. Only the shipped values were measured.
This small fictional sample verifies the held-out pipeline, not population
accuracy. The manifest SHA-256 was
`7fe9666718f0e542e288a797210cfcfa4cc0146d7cf087ceefff8992021357a2`.

The measured counts and hashes are in
[the held-out threshold report](evidence/heldout-thresholds-2026-10-18.json).

## Recorded foreground injection replay

On 18 October 2026, six genuine foreground calls used CLI 0.154.0 with
`gpt-5.6-sol` and low reasoning. Three frozen fictional task pairs completed
three of three tasks with injection and one of three without injection. The
paired pass-rate difference was +0.6667. This measures the injection component
on these cases; it does not measure native memory parity or population quality.

Model tools and native memory were disabled. The model selected a structured
action. A separate executor validated the finite action and wrote only the
allowed task file. The independent file oracle checked actual bytes and
required file absence. The model could not access expected labels or the oracle.

The six completed calls reported 48,361 input tokens, including 6,528 cached
input tokens, and 128 output tokens, including zero reasoning output tokens.
One earlier unsupported-model request returned HTTP 400 with no usage. Its
actual usage remains unavailable, and a 25,000-token reservation stayed in the
budget. The total attempt count was seven. The missing code-mode host produced
the same fail-closed startup warning in each completed call. No model tool
event executed. No retry or campaign expansion followed the completed pairs.

The [foreground replay report](evidence/foreground-injection-2026-10-18.json)
records limits, hashes, each arm, actual task files and usage. The frozen
private runner and sanitized event traces support independent review. The
supplied public deterministic runner verifies mechanics separately.

## Scale fixture

`tools/eval/scale_fixture.py` builds synthetic memory folders at 30, 200, 1,000,
5,000 and 20,000 notes, so the behaviour at scale can be measured without paid
calls. It reads a seed and two frozen files: a word list of generic developer
terms and 60 hand-written lessons with their queries. No real note text goes in,
and the same seed gives the same bytes.

```bash
python3 tools/eval/scale_fixture.py --out /a/folder/outside/the/repo
python3 tools/eval/scale_fixture.py --verify /a/folder/outside/the/repo
```

Field mix and note sizes copy a survey of real notes. Each lesson has one unique
answer string, 3 near-miss notes and 1 duplicate, and sits where the stock load of
`MEMORY.md` misses it: behind a topic file, unlinked, past line 200, past 25 KB or
in another project. At 30 notes the lessons are linked, as a negative control.
`--verify` proves each placement from the files, and `manifest.json` holds the
hashes. Synthetic text is easier than real text, so this measures size and
latency. It does not measure quality on real notes.

## CI scope

The existing CI threshold job still runs the tuning fixture and its tuning
regression gate. It does not qualify generalisation. Standard CI tests verify
the frozen split, reject leakage, prevent held-out tuning and alternative-floor
measurement, and exercise paired subprocess and external-file mechanics.
No extra embedding download or heavy evaluation job is added. The zero floors
in the public synthetic held-out fixture do not qualify product accuracy.
