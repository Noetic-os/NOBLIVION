<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Guards and stop checks

Guards turn rules in your memory files into checks on Claude Code's tool
calls. They run as hooks. They do not need the store: they read the memory
files directly. Every guard fails open: on an error, it allows the call.

## The guard table

The guard hook does not parse your memory files on each call. It reads a
table, `<data dir>/guard-table.json`.

- **Source.** Every `*.md` file in one memory folder: `NOBLIVION_MEMORY_DIR`,
  else `~/.claude/projects/<slug of your home folder>/memory`. The files
  `MEMORY.md`, `MEMORY_ARCHIVE.md` and `topic_*.md` are skipped.
- **Entry.** A file becomes an entry when it has a `rule:` field, a
  `violates:` field or a valid trigger.
- **Rebuild.** The table is rebuilt at session start and after Claude Code
  writes a `feedback_*.md` file. To rebuild it by hand, run:

  ```sh
  python3 "<plugin dir>/hooks/guard_table.py" --rebuild --report
  ```

  `--report` prints the counts and the files that were skipped, with the
  reason.

## Rule fields

Put the rule fields at the end of the front matter. This example is a
complete rule:

```yaml
---
name: Use git -C instead of cd
description: Do not change directory before a git command.
rule: Use git -C <path> instead of cd before git.
apply: "git -C /repo status"
scope: tool
triggers: [cd, glob:tools/*.sh]
violates: "^cd\\s+\\S+\\s*&&\\s*git\\b"
example_repeat: "cd /repo && git status"
example_ok: "git -C /repo status"
---
```

| Field | Meaning |
| --- | --- |
| `rule` | One command sentence. At most 160 characters. |
| `apply` | The exact shape of the right action. At most 400 characters. |
| `scope` | `tool`, `stop`, `file` or `always`. |
| `triggers` | A list of items that make the rule show. See below. |
| `violates` | Optional. A regular expression over a shell command. A match blocks the command. At most 300 characters. |
| `example_repeat` | Needed with `violates`. A command that `violates` must match. |
| `example_ok` | Needed with `violates`. A command that `violates` must not match. |
| `complies` | Optional, only with `violates`. A regular expression that `example_ok` must match. |

A `feedback_*.md` file needs `rule`, `apply` and `scope`. With scope `tool`,
it needs a command trigger or a `tool:` trigger. With scope `file`, it
needs a `glob:` trigger.

The table keeps a `violates` expression only when it passes every check:
it matches `example_repeat`, it does not match `example_ok`, it does not
match an empty command or an everyday command, and it is not a slow
pattern.

### Triggers

| Item | Fires when |
| --- | --- |
| `git push` | a shell command segment is this text, or starts with it and a space |
| `glob:tools/*.sh` | Edit, Write or MultiEdit touches a file whose path matches |
| `tool:<name>` | accepted in the table; the guard hook does not use it |
| `phrase:<text>` | accepted in the table; the guard hook does not use it |

Each item is at most 80 characters. A list has at most 64 items. An item
cannot hold a comma, a quote or a bracket.

## The guard hook

The guard hook runs before each Bash, Read, Grep, Edit, Write and
MultiEdit call.

### Rows: show the rule

When a trigger fires, the hook adds the rule to the context of the call.
It does not block the call.

- It adds at most 3 rules per call.
- It shows one rule at most 3 times per agent in a session.
- It shows the full rule until 12,000 characters per session. After that
  it shows only the rule and apply lines.
- All rows of a session are capped at 6000 characters.

A weak trigger needs evidence. A trigger is weak when it is one word, when
it has no specific argument, when it is a `glob:` with no fixed path part,
or when 4 or more notes share it. Evidence means words of the call that
also appear in the rule or apply text. With no evidence, the row is not
shown. With little evidence, only the rule line is shown.

### Deny: block a command

When a `violates` expression matches a Bash command, the hook blocks the
command. The reason names the rule.

- The hook checks the full command and each part of a pipeline or a
  command list.
- It removes wrappers first, such as `env`, `sudo`, `timeout` and
  `VAR=value` prefixes.
- It ignores heredoc bodies, quoted data and comments.
- It checks the command inside `bash -c`, `eval` and `ssh host '...'` as a
  command of its own.

Edit, Write and MultiEdit calls get rows only. They are never blocked.

### Override

To run a blocked command on purpose, end the command with a comment:

```sh
some-command  # guard-ok: <reason>
```

The reason must hold a letter or a digit. When the rule has `complies`,
the override works only after the right command (the `example_ok` shape)
failed in this session.

### Label rows

Label rows show notes that name the file, the tool, the ticket or the
service in the call. They never block. They fill only the free row places
and have their own budget of 4000 characters per session. Ticket and
service labels need `labels.ticket_prefixes` and
`labels.service_prefixes` in the config.

## The credential guard

The credential guard runs first, before the rule checks. It blocks a
command that would print a git URL with a password or a token in it.

- It covers `git remote -v`, `git remote show`, `git remote get-url`,
  `git ls-remote` with no remote, `git config --list` and `--get`, reads of
  a git config file, and a recursive grep over a tree that holds one.
- It blocks only when it finds such a URL in the git config.
- A filter later in the pipeline (`sed` that removes the secret, `wc`,
  `grep -c`, `grep -l` or `grep -q`) makes the command safe, and the guard
  allows it.
- It has no override. The reason lists safe forms of the command.

## The memory fields hook

After Claude Code writes a `feedback_*.md` or `project_*.md` file in the
memory folder, this hook checks its fields.

- For a feedback file: the rule fields.
- For a project file: `status` and `project`.

It never blocks. It returns a warning to Claude Code, which can then fix
the file. After a feedback file changes, it rebuilds the guard table.

## The memory sync hook

After Claude Code writes a `*.md` file in the memory folder, this hook
re-indexes the notes.

1. It starts a background worker.
2. The worker waits until the folder is quiet for 3 seconds.
3. The worker runs `noblivion index` once. It tries at most 3 times.

After a shell command, the hook also checks whether the memory folder
changed. This check does not see changes in sub-folders.

## Stop checks

When Claude Code wants to end a turn, the stop hook runs these checks. A
blocking check sends Claude Code back to work with a reason. A check
blocks at most once per stop.

| Check | Blocks when |
| --- | --- |
| `commit` | A git worktree that Claude Code edited in this turn has changes that are not committed. |
| `tests` | Code files changed and no test command ran after the last change. |
| `notify` | The reply says that work was deferred, skipped or blocked, and no push notification was sent in this turn. |
| `deploy` | A pull request was merged, the reply says "merged", and no check of a deploy host ran. Off while `stop.deploy_hosts` is empty. |
| `lesson` | The user corrected Claude Code and no memory file with `rule:` and `apply:` was written in this turn. |

All five checks may block by default. The `deploy` check stays off until
you list deploy hosts. The `notify` check expects a push notification tool;
turn it off when you do not have one.

- Choose the blocking checks with `NOBLIVION_STOP_CHECKS`, for example
  `commit,tests`. Use `none` to turn all off.
- Set `NOBLIVION_STOP_CHECK_MODE=shadow` to log each decision and never
  block.
- The `stop.*` config keys tell the checks about your setup. See
  [configuration.md](configuration.md#stop-checks).

## Logs

| File | What it holds |
| --- | --- |
| `<data dir>/guard-log.jsonl` | One line per guard decision: rows, deny, override, label rows, credential deny, error. It holds command text. Mode 0600. |
| `<data dir>/guard-state/` | State per agent and session. Removed after 7 days. |
| `<data dir>/stop-check-log.jsonl` | One line per stop check decision. Rotated at 5 MB. |
| `<data dir>/cache/memory_sync.log` | The memory sync runs. |

## Turn a guard off

| Guard | How |
| --- | --- |
| Credential guard | `NOBLIVION_GUARD_CREDENTIAL=0` |
| Label rows | `NOBLIVION_GUARD_LABELS=0` |
| Weak-trigger check | `NOBLIVION_GUARD_WEAK_SHARE=0` |
| Stop checks | `NOBLIVION_STOP_CHECKS=none`, or `NOBLIVION_STOP_CHECK_MODE=shadow` |
| Memory sync | `NOBLIVION_MEMORY_SYNC_OFF=1` |
| Guard hook, memory fields hook | Remove the rule fields from the note, or turn off the plugin. These hooks have no switch. |
