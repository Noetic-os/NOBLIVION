<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Import memories from a JSONL file

`noblivion import` adds memories to the local database from a JSONL file.
Use it to move memories from another tool into NOBLIVION.

The command runs on your machine. It sends nothing off the machine.

## Quick start

1. Check the file. This is a dry run: it prints what the import would do
   and writes nothing.

   ```sh
   noblivion import memories.jsonl
   ```

2. Read the report. Fix the invalid lines if you need them.
3. Write the rows:

   ```sh
   noblivion import memories.jsonl --apply
   ```

You can run the import again. It inserts only the rows that are not in the
database yet.

## Options

| Option | What it does |
| --- | --- |
| `--dry-run` | Print the report and write nothing. This is the default. |
| `--apply` | Write the rows. You cannot use it together with `--dry-run`. |
| `--label L` | Add the label `L` to every row. Repeat it for more labels. |
| `--archived` | Store the rows as archived. Recall does not return them. |
| `--json` | Print the report as JSON. |
| `--db FILE` | Use this database file. Default: the database in the data dir. |
| `--lock-timeout S` | Wait up to `S` seconds for another import to end. Default: 0. |

## The file format

The file holds one JSON object per line. The command skips a blank line
and does not count it as invalid.

| Field | Required | What it holds |
| --- | --- | --- |
| `content` | yes | The memory text. It must not be empty. |
| `source_type` | no | `transcript_mined` (or `mined`) for a note mined from a transcript. Any other value, or none, makes a plain memory. |
| `category` | no | A category name. |
| `labels` | no | A list of strings. |
| `created_at` | no | An ISO-8601 time, for example `2030-01-02T03:04:05Z`. A time without a zone is UTC. Default: now. |
| `updated_at` | no | An ISO-8601 time. Default: now. |
| `pinned` | no | `true` or `false`. |

The command ignores other fields.

A line is invalid when it is not a JSON object, when `content` is missing
or empty, or when a field has the wrong type. A time that the command
cannot read also makes the line invalid. The command counts each invalid
line and shows the first 20 line numbers with the reason. An invalid line
does not stop the import.

Example:

```json
{"content": "Water the tomatoes at dawn.", "category": "feedback", "labels": ["garden"]}
{"content": "Tool error: the sprinkler valve sticks.", "source_type": "mined"}
```

## What the import stores

### Redaction

The command removes secrets from the text first. Then it masks text that
tries to give the model new instructions. These are the same two steps as
for the transcript miner. When the text cannot be redacted, the command
skips the line and counts it as `redaction_skipped`.

### Duplicates

The key of a row is the SHA-256 hash of its redacted text.

- The same text twice in one file is stored once. The report counts the
  copies as `duplicates_in_file`.
- A text that an earlier import stored is not stored again. The report
  counts it as `already_present`.
- An import never changes or deletes a row that is in the database. It
  only adds rows. So a second import of the same text with other labels,
  or with `--archived`, does not change the first row.
- The first import of a text decides its source type. A later line with
  the same text and another source type is a duplicate.
- A memory file with the same text is not a match. The import compares
  only with rows that an earlier import stored.

### Labels and category

The labels of a row come in this order. Each label appears once.

1. The labels of the line.
2. The `--label` values.
3. `source:<value>`, when the line has a `source_type` that the store does
   not know. The known values are `transcript_mined`, `mined` and
   `claude_code_md`.
4. `category:<value>`, when the line has a `category`.

The command drops a label that the redactor would change.

The database row also has a category column. It gets the category of the
line when the category is `feedback`, `project`, `reference` or `user`.
Else it gets `reference`.

### Where the rows go

Every imported row is in the namespace of the config (`namespace`, or
`NOBLIVION_PROJECT`). Its root is `noblivion/import` and its path is its
hash. A root is the name of the folder above a memory folder. A folder name
cannot contain `/`, so no memory folder has this root.

The index scan reads memory files. It skips the imported rows, because no
file backs them. So a scan never deletes, moves or rewrites an imported
row.

## What recall does with the rows

- **Plain memory.** It is stored like a memory file (`claude_code_md`).
  The text gets the layout of an indexed file: `# <title>` from the first
  line, a source marker line, then the rest of the text. Recall treats the
  row like a note in a shared root: every session searches it, and the
  prompt hooks can show it. It goes through the same redaction as every
  other note before the store returns it.
- **Mined note** (`transcript_mined`). It is stored like a miner note. The
  prompt hooks never show it. The store returns it only when a request
  asks for mined notes, for example the MCP tool with `include_mined`.
- **Archived row** (`--archived`). Recall does not return it. The duplicate
  sweep does not pair it. The vector backfill does not embed it. No purge
  deletes it: the archive purge deletes only rows that a duplicate sweep
  archived.

The duplicate sweep merges memory files. It never pairs an imported row,
because no file backs it.

The redactor of the index scan does not run again on imported rows. When a
new release changes the redaction rules, the imported rows keep the text
of the rules at import time. The store still redacts every text it returns.

## Vectors

The command does not compute vectors. Each write batch raises the content
revision of the database. The running store then embeds the new live rows
on its next backfill pass, with its own model and its own consent rules
(see `noblivion consent embeddings`). Until then, recall finds the rows by
keyword only.

After `--apply`, the report shows how many inserted rows still wait for a
vector (`vectors_waiting`). If no store runs, start it:

```sh
noblivion ensure-running
```

## Locks and exit codes

`--apply` holds the lock file `import.lock` in the data dir, so two imports
do not run at the same time. Each write batch is one database transaction,
so the import does not conflict with the store or with the index scan.

| Exit code | Meaning |
| --- | --- |
| 0 | Done. |
| 1 | Refused: the file is missing or cannot be read, or `--apply` found no valid line. |
| 2 | Usage error, for example `--dry-run` together with `--apply`. |
| 3 | The database schema does not match this version. Start the store once. |
| 4 | Another import holds `import.lock`. |
| 5 | A database error. Try again later. |
| 6 | No database. Run `install.sh` first. |
