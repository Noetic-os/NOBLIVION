<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Diagnose memory and explain recall

Use the store environment from the same installed plugin revision as the hooks:

```sh
~/.noblivion/venv/bin/python -m noblivion doctor --json --root YOUR_ROOT
~/.noblivion/venv/bin/python -m noblivion explain 'your prompt' --json --root YOUR_ROOT
```

Set `NOBLIVION_DATA_DIR` or pass `--data-dir` for a non-default store. `--root`
selects the memory root, not an arbitrary filesystem search. In Codex, the
current workspace mapping in `CODEX_HOME/noblivion/config.json` can supply the
root and memory folder. The diagnostic does not guess another project's root.
The namespace follows the configured store or `--namespace`.

## Doctor

`doctor` checks runtime files, private file modes, the installed environment,
model cache presence, authenticated health, schema objects, foreign keys,
filesystem risks, configuration keys and values, environment precedence,
embedding URL, selected memory folders, and Codex hook and MCP entries. A model
cache check verifies presence, not model quality or successful inference.

Each check reports `pass`, `warn`, `fail`, `unavailable`, or `not_configured`.
An unavailable check is not a successful check. Exit status is 0 when there
are no findings, 1 for warnings or failures, and 2 when a required inspection
is unavailable. An unset optional integration is `not_configured`.

Doctor reads bounded files and probes only the configured store at literal
`127.0.0.1`, with a nonce proof of store identity. It does not start, restart,
repair, migrate, download models, or call an embedding provider. It reports
fixed messages instead of settings values, tokens, note contents or private
paths. On an unknown filesystem it reports the check as unavailable rather
than claiming that SQLite WAL is safe on a network mount.

Offline SQLite checks require a checkpointed database. An active WAL or
rollback journal makes them unavailable because immutable inspection could
otherwise report stale data. Doctor does not force a checkpoint or stop the
store. Re-run against a safely obtained checkpointed copy when needed.

## Explain

By default, `explain` ranks the selected root's indexed notes with keyword
scoring and passes the candidates through the actual recall hook filters.
It reports candidate IDs, scores, initial and final ranks, and removal reasons
such as hygiene, relevance floor, missing rule, top-k or character cap. Note
text, paths, titles and the supplied prompt are omitted from the output.
The explanation does not update recall caches, shown sets, logs, feedback or
SQLite. Memory-use controls apply, including conservative handling when the
current session identity is unknown.

This is a bounded keyword explanation, not a prediction of hybrid semantic
ranking. Cosine scores are unavailable in the default mode; the hook correctly
skips a cosine floor for keyword candidates. At most 200 retrieved candidates
are considered, and the hook's configured retrieval depth can narrow that
list. Unindexed notes and another session's shown state are not inspected.
The result includes explicit coverage and scoring limitations. It fails with
exit status 2 when the root, database, schema, installed hook, or time budget
cannot be inspected. Memory use disabled for the scope returns status 1.

For scores from an **already running** store, opt in explicitly:

```sh
~/.noblivion/venv/bin/python -m noblivion explain 'your prompt' --root YOUR_ROOT --live --json
```

`--live` authenticates that store before sending the prompt and may call its
configured embedding provider under the existing consent policy. It never
starts a store or changes consent. An empty store answer can mean no matches
or a store privacy refusal; it does not prove that no matching notes exist.
No session identity is invented to bypass a privacy control. The default
mode makes no provider calls. Both modes use a 3 second deadline by default;
`--timeout` accepts 0.1 through 10 seconds. Large or changing offline databases
are reported as unavailable instead of producing incomplete health claims.
