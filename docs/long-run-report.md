<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Long-run test report (run-a)

## Verdict

The run does not support a verdict for or against NOBLIVION. It ended after
3.7 days of the planned 7. The pre-registered rules score every scale LOSE, but
the run has two faults that make that score unreliable (see "Limits"). Do not
quote it as a one-week result. A valid run is the next step.

## What was tested

- Build under test (B1): NOBLIVION v0.1.21. Reference build (B0): v0.1.18.
  A stock memory arm ran beside both.
- Five store sizes: 30, 200, 1000, 5000 and 20000 notes.
- Planned length: 7 days from 2026-11-02 07:50 CET. The run was stopped on
  2026-11-06 at 00:35 CET.
- Per scale: nightly replay of a fixed question set, an hourly soak load
  (writes, edits, deletes and queries, with a burst of 20 parallel requests),
  an hourly drift check, a resource watch, and a nightly determinism hash.
- The run was sealed: no network, no Claude process, no read of files outside
  its folder. Pre-registered rules (DR1 to DR8) were frozen before the start.

## Results

### Sealed-run checks

- External connects: 0 on every day.
- Claude processes: 0 on every day.
- Refused opens and refused writes: 0.
- Heartbeats: complete for every full day.
- Plugin version: unchanged from start to stop.

### Soak load (rule DR8)

| Day | Operations | Errors | Error rate | Query p95 |
| --- | ---: | ---: | ---: | ---: |
| 2026-11-02 | 917 | 20 | 2.2 % | 1.21 s |
| 2026-11-03 | 1362 | 47 | 3.5 % | 1.30 s |
| 2026-11-04 | 1629 | 2 | 0.1 % | 1.76 s |
| 2026-11-05 | 2029 | 64 | 3.2 % | 1.19 s |
| 2026-11-06 (part day) | 56 | 0 | 0 % | 1.39 s |

- DR8 needs an error-rate upper bound under 0.5 %. Three of four full days are
  far above it. DR8 fails.
- Query p95 stayed under the 2 s recall budget on every day. This part holds.
- Write-to-searchable lag p95 was 32.67 s. DR8 allows 10 s. This part fails.
  Ticket NOBLIVION-192 tracks it.
- Drift: 4 of 89 hourly checks found drift. DR8 needs 0.

### Scale verdicts (rule DR5)

- All five scales (30, 200, 1000, 5000, 20000) score LOSE. DR5 is false at each.
- Of 75 claim cells, none meets DR1 (B1 beats the stock arm). Six cells carry
  the lose flag. Eight cells have no test because too few tasks ran.
- B1 against B0 at 1000 notes: claim C19 favours B1 in 6 of 6 tasks
  (p = 0.031). Claim C7 favours B0 in 4 of 6 (p = 0.125, not significant).
  The other cells show no difference.
- Determinism (same input, same hash) held at 30 and 200 notes: two nightly
  hashes matched at each size. It was not measured at 1000 or more.

### Job outcomes

- Nightly replay: 5 runs, 3 failed.
- Latency bench: 3 runs, 2 failed.
- Soak load: 522 runs, 80 failed.
- The verdict file holds no pass value for DR4 and no value for DR7 or DR8. Those rules are not scored.

## Misses

- Write-to-searchable lag (NOBLIVION-192): p95 32.67 s against a 10 s rule.
- Claims C12 and C20 to C23 have no scenario in the program (NOBLIVION-193).
  They were never tested.
- Run validity (NOBLIVION-196): the unit allowed only 64 tasks. The embedding
  runtime of the larger stores could not start threads, and the nightly
  replay and bench stores crashed. The limit was raised to 1024 on 2026-11-04.
  Errors continued on 2026-11-05 (64), so the limit may not be the only cause.
  That is not yet established.

## Limits

- The run covers 3.7 days, not 7. Trend rules (slope of latency and memory
  over days) need the full length.
- Store sizes 1000 and larger lost most replay data to the crashes above, so
  their verdicts rest on very few tasks (6 per cell).
- One run on one host. No repeat, so no estimate of run-to-run spread.
- The pre-registered LOSE score follows from DR5 being false. It says the
  evidence is insufficient to pass, not that NOBLIVION is worse than stock.

## Next step

Run the full 7 days again on the fixed program, with the task limit set from
the start, and check that DR4 and DR7 get a value. Fix the lag first
(NOBLIVION-192) so DR8 can pass.
