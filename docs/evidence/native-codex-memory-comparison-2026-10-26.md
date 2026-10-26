<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Native Codex memory versus NOBLIVION 0.1.17: preliminary comparison

Measured 2026-10-26. This is a paired recall benchmark on a historical corpus.
It is not a measurement of production error reduction.

## Result

Native Codex recovered more required details. NOBLIVION used fewer tokens and
answered faster. The result does not establish equal quality or a general winner.

| Measure | Native Codex | NOBLIVION 0.1.17 |
| --- | ---: | ---: |
| Clean paired questions | 19 | 19 |
| Required-detail recovery, equal weight per question | 100.0% | 90.4% |
| Complete answers without a confirmed false claim | 19/19 | 15/19 |
| Answers with a confirmed false claim | 0/19 | 0/19 |
| Answers with additions unsupported by the supplied reference evidence | 19/19 | 17/19 |
| Honest abstentions | 0/19 | 4/19 |

A confirmed false claim needs contradictory reference evidence. An unsupported
addition is a separate outcome. No confirmed false claims does not establish that
every assertion was correct. Complete answers satisfy all required propositions,
all completion criteria, the evidence rules, and the 180-word limit.

The required-detail difference was 9.6 percentage points in favor of native Codex.
A descriptive bootstrap that resampled the 19 whole questions gave a 95% interval
of 1.3 to 20.2 percentage points in favor of native Codex. This interval describes
this selected challenge set. It is not a population guarantee.

## Tokens and time

These figures use all 24 complete first-round pairs, with one answer per system
for each question. They include usage from child contexts.

| Measure | Native Codex | NOBLIVION 0.1.17 | NOBLIVION reduction |
| --- | ---: | ---: | ---: |
| Input tokens, including cached inputs | 20,936,060 | 7,545,827 | 64.0% |
| Uncached input tokens | 1,803,132 | 941,795 | 47.8% |
| Output tokens | 150,426 | 41,634 | 72.3% |
| Median answer time | 143.6 s | 69.0 s | — |

Cache warmth was not equalized. Preparation costs are incomplete, including
background native processing and historical corpus construction. The interrupted
answer has no final usage total. Missing usage is not counted as zero. These
figures are not a complete study-cost total or a currency-saving estimate.

## Method

- Both systems received the same 2,339 active historical records.
- Answer runs used the same `gpt-6-astra` model and `ultra` reasoning setting,
  with Codex CLI `0.162.0-alpha.17.2`. Memory generation was disabled
  during answers. No completed answer was rerun.
- Native memory used real background extraction and consolidation. All 100
  original source jobs completed: 98 retained nonempty outputs and two ended
  without retained output. The 106 generated native memory files were frozen.
- Original source versions were checked against the recorded ingestion and
  extraction evidence. Two source chats were later reopened. Current freshness
  flags remained false; the later turns were not claimed as extracted.
- Assigned memory could expose source references for follow-through. Five cases
  failed the frozen source-access rule: one listed external filenames, and four
  additional native answers scanned later versions of a reopened source chat.
  The scans returned no later evaluator snippets. Access deviation alone does not
  prove factual contamination. Each affected case was excluded from both systems
  in the primary result before grades were inspected.
- The plan was 24 questions, two repetitions, and two systems: 96 answers in
  NOBLIVION, native, native, NOBLIVION order. The user requested an early stop
  before grading. The saved set is 67 answers: 43 native and 24 NOBLIVION.
- The primary result uses 19 clean first-round pairs. The secondary result uses
  all 24 first-round pairs. The 19 additional native answers remain supplementary;
  they are not pooled into an unequal comparison with NOBLIVION.
- Two fresh graders independently assessed all 67 answers with provider labels
  hidden. They agreed on all 247 required-proposition labels and all 134
  completion labels. A third fresh reviewer adjudicated 51 selected answers,
  covering every substantive disagreement and every alleged false claim.
  Citation styles could still reveal the provider.

## Secondary and supplementary results

Across all 24 first-round pairs, native Codex recovered 100.0% of required details
and NOBLIVION recovered 90.3%. Complete answers without a confirmed false claim
were 24/24 and 19/24, respectively. Neither had a confirmed false claim.

The 19 additional native answers recovered 85.5% of required details and had no
confirmed false claim. These are partial repetitions on a different case subset.
They do not establish a paired second-round result. The lower recovery also
cautions against treating the first-round 100% as a stable general success rate.

## Scope and limits

- Questions were selected as difficult recall challenges. They are not a random
  sample of coding work. The benchmark measures historical import and usable
  recall, not automatic learning from equivalent coding sessions.
- Native memory also retained three genuine prior-history sources. There was no
  no-memory baseline. Order and cache effects were not fully balanced after the
  early stop.
- NOBLIVION trust ranking was in shadow mode. This study does not establish gains
  from active trust ranking, live invalidation, shared capture across clients,
  or automatic prevention by error hooks.
- Native consolidation retained tool names without complete arguments. Its full
  background access trace was unavailable. A marker scan does not prove complete
  isolation.
- The private test service was stopped and global native memory was verified off.
  The isolated evidence directory was retained. Production services were untouched.

## Evidence

[Aggregate results and token totals](native-codex-memory-comparison-2026-10-26.json) accompany this report.
The public files omit private source records, questions, answer text, internal
names, host details, and user paths. This limits independent reproduction from
this repository alone. The complete private evidence package retains both
independent grades, adjudications, frozen inputs, access decisions and hashes.

SHA-256 of the complete private results JSON:
`056dcdcd7b309bc3a95e8356e0265ae8a4305ea40f5691745526987faffa9631`.

SHA-256 of the complete private evidence archive:
`c12a824d8d558bf06be14dafe7c9160068841fba3eaaf133b0b4ad7eea00190a`.
