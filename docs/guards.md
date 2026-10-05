<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Guards and stop checks

Guards turn rules in your memory files into checks on Claude Code's tool
calls. They run as hooks. They do not need the store: they read the memory
files directly. Every guard fails open: on an error, it allows the call.

## The guard table

The guard hook does not parse your memory files on each call. It reads a
table. Each project has its own table,
`<data dir>/guard-tables/<slug of the memory folder>.json`.

- **Source.** Every `*.md` file in the session's memory folder: the folder
  Claude Code keeps for the project of the session's working directory (see
  "The session's memory folder" in [configuration.md](configuration.md)),
  or `NOBLIVION_MEMORY_DIR` when it is set. When
  `NOBLIVION_GLOBAL_MEMORY_DIR` is set, the table also holds the files of
  that folder; a project file wins over a global file with the same name.
  The files `MEMORY.md`, `MEMORY_ARCHIVE.md` and `topic_*.md` are skipped.
  The store indexes the same folders, so recall and the guards read the same
  notes.
- **Entry.** A file becomes an entry when it has a `rule:` field, a
  `violates:` field or a valid trigger.
- **Rebuild.** The table is rebuilt at session start and after Claude Code
  writes a memory file of any kind. The guard hook also rebuilds it before a
  call when a memory file changed in another way: a shell command (`rm`,
  `sed`, `mv`, `git checkout`), an editor or another session. The table
  holds a stamp of the memory files: their names, sizes and change times.
  The hook compares the stamp on each call. It reads no file for that, and
  the check takes less than 1 ms for 200 files. A build measures the speed
  of each `violates` expression once. That takes about 10 ms for each
  expression. The file `<table>.probes` next to the table keeps the
  results, so a rebuild measures only a new or changed expression. In a
  test with 650 rules, the first build took about 7 seconds and a rebuild
  after one change took about 0.2 seconds.

  A table that `NOBLIVION_GUARD_TABLE` names is not checked on each call.
  It is rebuilt only at session start and after a memory write.
- **Skipped rules.** A rule that fails a check is skipped. It does not
  block. At session start, one line names the skipped rules and the reason,
  for example:

  ```text
  NOBLIVION: 1 rule is skipped and does not block: feedback_no_force
  (violates matches the empty command, so it matches every command).
  ```

  The line names at most 3 rules. To see all of them, rebuild by hand:

  ```sh
  cd <your project> && python3 "<plugin dir>/hooks/guard_table.py" --rebuild --report
  ```

  Run it from the project folder: the working directory names the project.

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

The hook has a time limit of 1.5 seconds. When the deny check is not done
in that time, the hook allows the call and prints one line:

```text
NOBLIVION: the guard ran out of time, so the deny rules were not checked for this call.
```

This can occur with a very long command and a very large table, for
example 1000 rules and a 90 KB command. The guard log records `timeout`.
When the time runs out after a deny check that found nothing, only rows
are lost, and the hook prints nothing.

A rebuild of the table has its own limit of 6 seconds. When a rebuild is
not done in that time, the rules of the last build stay in force and the
call is checked against them. The hook prints one line for that change of
the memory folder:

```text
NOBLIVION: a memory file changed, and the deny rules could not be read again in time. The rules from before the change stay in force until a memory file changes again or a new session starts.
```

The hook does not try that rebuild again at each call. It tries again when
a memory file changes again. The session start and a memory write rebuild
the table with no such limit. The rebuild that ran out of time keeps what
it measured, so the next one is faster. The guard log records
`table rebuild timeout`. When there is no table from an earlier build, the
line says that no deny rule is checked.

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
  `git ls-remote` with no remote, `git config --list` and `--get`,
  `git var -l`, reads of a git config file, and a recursive grep over a
  tree that holds one.
- A read of the config file is a read in each of these forms: the file name
  (`cat .git/config`), a glob (`cat .g*/conf*`), a brace list
  (`cat .git/{config,HEAD}`), the file as standard input
  (`cat < .git/config`, `echo "$(< .git/config)"`), and a variable that the
  same command sets (`f=.git/config; cat $f`). The guard also sees the name
  when the command builds it from parts: `cat .gi{t,}/conf{ig,}`,
  `cat .g'i't/con'f'ig` and `d=.gi; cat ${d}t/config`.
- It covers `git fetch`, `git pull`, `git push`, `git ls-remote`,
  `git submodule` and `git remote update` when the command sets a
  `GIT_TRACE*` variable or `GIT_CURL_VERBOSE`. The trace prints the URL.
- It blocks only when it finds such a URL in the git config.
- A filter later in the pipeline (`sed` that removes the secret, `wc`,
  `grep -c`, `grep -l` or `grep -q`) makes the command safe, and the guard
  allows it.
- It has no override. The reason lists safe forms of the command.

Sometimes the guard cannot know the file before the command runs. The path
holds a variable that the command does not set, `$( )` or backticks, for
example `cat "$f"` or `cat $(git rev-parse --git-dir)/config`. The guard
then uses this rule for `cat`, `tac`, `nl`, `head`, `tail`, `less`, `more`,
`bat`, `grep`, `rg`, `sed` and `awk`:

1. If the fixed text at the end of the path is not the name of a git config
   file, the guard allows the command. It cannot show that such a command
   reads the git config. `cat "$f"`, `cat docs/$name`,
   `cat $(git rev-parse --show-toplevel)/README.md` and
   `for f in $(git diff --name-only); do head -5 "$f"; done` pass.
2. If no git config that git reads in that folder holds a credential, the
   guard allows the command.
3. If not, the guard blocks the command, for example `cat "$d/config"`.
   Write the path in the command. Then the guard can see that the file is
   not the git config.

`$(mktemp)` always passes, because it names a new file.

The guard has these limits:

- It covers git URLs only. It does not cover other secrets. `printenv`,
  `cat .env` and `cat ~/.aws/credentials` pass.
- It reads the command text. It does not run the command. It cannot follow
  a path that a program builds, for example `python3 -c` with a path made
  from parts. Such a read is not blocked.
- The rule for a path that the guard cannot know covers only the commands
  in the list above. Another program that reads such a path is not blocked.
- It does not follow a file descriptor that an earlier `exec` opened, a
  positional parameter (`$1`), or a loop that reads lines from a file that
  it cannot resolve.
- It does not follow a file name that the command reads when it runs.
  `find . -name config | while read f; do cat "$f"; done` is not blocked.
- It blocks a `find` that starts in the folder of the clone and gives its
  files to a reading command, with `| xargs` or with `-exec`, when the git
  config holds a credential. It blocks such a command also when the `find`
  cannot name the config file: `find . -name "*.py" | xargs grep -c TODO`
  and `find . -name '*.py' -exec head -3 {} \;`. This limit is not new:
  earlier versions have it too. Start the `find` in a subfolder
  (`find src -name '*.py' | xargs grep -c TODO`), or use `git grep` or
  `git ls-files` for the list of files.
- The check must end inside the time limit of the hook (1.5 seconds). A
  very long command of some forms can still run out of it, and the hook
  then allows the call with the line that the deny rules were not checked.
  In a test this occurred with 60,000 `$( )` substitutions in one command
  (250 KB), with one word of 750 KB that has a backslash in each part, with
  one word of 1.5 MB that has quotes in each part, and with 2 MB of short
  words. One word of letters, also with `-` or `.` between its parts, was
  denied up to 4 MB.

## The memory fields hook

After Claude Code writes a `feedback_*.md` or `project_*.md` file in the
memory folder, this hook checks its fields.

- For a feedback file: the rule fields.
- For a project file: `status` and `project`.

It never blocks. It returns a warning to Claude Code, which can then fix
the file. After a memory file of any kind changes, it rebuilds the guard
table, because a note of any kind can hold a `violates` rule. A change to
`MEMORY.md`, `MEMORY_ARCHIVE.md` or a `topic_*.md` file does not rebuild it.

## The memory sync hook

After Claude Code writes a `*.md` file in the memory folder, this hook
re-indexes the notes.

1. It starts a background worker.
2. The worker waits until the folder is quiet for 3 seconds.
3. The worker runs `noblivion index` once. It tries at most 3 times.

After a shell command, the hook also checks whether the memory folder
changed. This check does not see changes in sub-folders.

## Stop checks

When Claude Code wants to end a turn, the stop hook runs these checks. In
`enforce` mode, a blocking check sends Claude Code back to work with a
reason. A check blocks at most once per stop.

The default mode is `shadow`. The checks run and the log records what would
have blocked, but no check blocks. The checks were tuned on the sessions of
one user, so you see first what they would do in your sessions.

| Check | Blocks when |
| --- | --- |
| `commit` | A git worktree that Claude Code edited in this turn has changes that are not committed. |
| `tests` | Code files changed and no test command ran after the last change. |
| `notify` | The reply says that work was deferred, skipped or blocked, and no push notification was sent in this turn. |
| `deploy` | A pull request was merged, the reply says "merged", and no check of a deploy host ran. Off while `stop.deploy_hosts` is empty. |
| `lesson` | The user corrected Claude Code and no memory file with `rule:` and `apply:` was written in this turn. |

In `enforce` mode, all five checks may block unless you choose fewer. The
`deploy` check stays off until you list deploy hosts. The `notify` check runs
only when the session has the `PushNotification` tool. The hook reads the
tool list from the transcript (the deferred tool rows that Claude Code
writes). When the transcript has no tool list, or the list does not name the
tool, the check does not fire and the log row notes `notify_skipped`.

- Run `noblivion stop report` to count, per check, the stops that would
  have blocked in the last 7 days (`--days N` for another window).
- Set `NOBLIVION_STOP_CHECK_MODE=enforce`, or the config key `stop.mode`,
  to let the checks block.
- Choose the blocking checks with `NOBLIVION_STOP_CHECKS` or the config key
  `stop.checks`, for example `commit,tests`. Use `none` to turn all off.
- The `stop.*` config keys tell the checks about your setup. See
  [configuration.md](configuration.md#stop-checks).

## Logs

| File | What it holds |
| --- | --- |
| `<data dir>/guard-log.jsonl` | One line per guard decision: rows, deny, override, label rows, credential deny, error. It holds command text. Mode 0600. |
| `<data dir>/guard-state/` | State per agent and session. Removed after 7 days. |
| `<data dir>/guard-tables/<table>.probes` | The measured speed of each `violates` expression of that table. You can delete it: the next build measures again. |
| `<data dir>/guard-tables/<table>.late` | The state of the memory folder whose rebuild ran out of time. While the folder has this state, the guard hook does not try the rebuild again. The guard hook removes the file when a rebuild that it starts itself, before a tool call, is done in time. The rebuild at the session start and the rebuild after a write of a memory file do not remove it. You can delete it: the next tool call then tries the rebuild again. |
| `<data dir>/stop-check-log.jsonl` | One line per stop check decision. Rotated at 5 MB. |
| `<data dir>/cache/memory_sync.log` | The memory sync runs. |

## Turn a guard off

| Guard | How |
| --- | --- |
| Credential guard | `NOBLIVION_GUARD_CREDENTIAL=0` |
| Label rows | `NOBLIVION_GUARD_LABELS=0` |
| Weak-trigger check | `NOBLIVION_GUARD_WEAK_SHARE=0` |
| Stop checks | They do not block by default (`shadow` mode). In `enforce` mode: `NOBLIVION_STOP_CHECKS=none`, or `NOBLIVION_STOP_CHECK_MODE=shadow` |
| Memory sync | `NOBLIVION_MEMORY_SYNC_OFF=1` |
| Guard hook, memory fields hook | Remove the rule fields from the note, or turn off the plugin. These hooks have no switch. |
