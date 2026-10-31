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
  that folder, and with the memory folder of each shared root
  (`recall.shared_roots`; by default the home root, see "Shared roots" in
  [configuration.md](configuration.md)) when it exists. A project file wins
  over a global or shared file with the same name.
  The files `MEMORY.md`, `MEMORY_ARCHIVE.md` and `topic_*.md` are skipped.
  The store indexes the same folders, so recall and the guards read the same
  notes.
- **Entry.** A file becomes an entry when it has a `rule:` field, a
  `violates:` field, a `protects_process:` field or a valid trigger.
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
| `rewrite` | Optional, only with `violates`. `add-flag <flag> after <prefix>`. A note with `rewrite` is a rewrite note, not a deny note. See "Rewrite and ask". |
| `on_violation` | Optional, only with `violates`. `deny` (default) or `ask`. See "Rewrite and ask". |
| `protects_process` | Optional. A regular expression over the command line of a process. A command that would signal a matching process is blocked. At most 300 characters. See "Deny: protect a process". |
| `where` | Optional. One or more path globs, separated by commas. The rule fires only inside them. See "Where a rule fires". |

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

- It adds at most 3 rules per call, under a header of one short line.
- A row is `- Memory <id>: <rule line>` (at most 250 characters). A strong
  match adds an `Apply:` line. A row never holds the text of the note: the
  model opens the file when the rule fits.
- It shows one rule once per agent in a session, whatever the tool. A deny
  is not a row: it repeats at every call.
- One show of rows is at most 1800 characters.
- All rows of a session are capped at 6000 characters.

A weak trigger needs evidence. A trigger is weak when it is one word, when
it has no specific argument, when it is a `glob:` with no fixed path part,
or when 4 or more notes share it. Evidence means words of the call that
also appear in the rule or apply text. With little evidence or none, the
row is not shown: only a deny shows for a weak trigger. One exception: when
the rule line names the trigger ("use X instead of `gh run rerun`"), the
row shows as the rule line only.

An everyday command as a trigger (`pytest`, `kill`, `git worktree`, `git
status`) is graded in the same way, and it is not dropped. The hook shows the
row of such a trigger only when the note fits the call:

- The note's rule names the command (`pytest` in "run pytest quiet"). Or the
  rule shares enough rare words (3 words) with the part of the command that
  fires the trigger.
- Or the rule shares enough rare words (3 words) with the context of the
  call: the command, the call's `description`, and the last prompt of the
  session. The hook reads the prompt from the end of the transcript (the
  last 512 KB). When it finds no prompt, only the command and the
  description count.

A note that does not fit the call shows no row. These rows are the weakest
rows of a call:

- They show the rule line only (no `Apply:` line), after every other row,
  and only in a free place. At most 3 rows come from everyday triggers in one call.
- They have their own budget of 1500 characters per agent in a session, so
  they never use up the budget of the other rows. The limit of 3 times per
  note and agent holds for them too.
- A trigger on text plumbing (`cat`, `head`, `tail`, `grep`, `sed`, `ls`,
  `echo`, `cd` and similar) stays dropped. It names no situation.
- They only add context: they never block a call. The grading above must be
  on (`NOBLIVION_GUARD_WEAK_SHARE` is not `0`), because the grading is what
  keeps these triggers quiet.

A rule for landing work (a note with scope `tool` whose command triggers are mostly `git push`,
`git worktree`, `git merge`, `gh pr`, a merge script or a full test suite run) is acted on here
and not in the prompt (`NOBLIVION_RECALL_GUARD_ONLY_LANDING`, config key
`recall.guard_only_landing`, on by default). The prompt shows such a row only when it names a landing
action. At a landing action (`git push`, `git worktree add`, `git merge`, `gh pr create`, `gh pr
merge`, `gh pr edit`, `gh pr ready`, or a program with `merge` in its name) the landing notes whose
landing trigger fires are strong rows: they need no evidence in the call, because `gh pr` and `git
push` are shared by many notes and the grading above would keep the row away from the one command
where it is needed. The limits stay: 3 rows per call, one show per note and agent in a session,
the session budget. A read (`gh pr view`, `git worktree list`) gives no such row. Replay of the
1,451 landing commands in 33 sessions against the guard table of the home project: a landing note was
shown at 615 of them before, and at 1,126 after.

For every plain trigger, `python -m X`, `python3 -m X` and `<path to
python> -m X` also count as `X`. A trigger `pytest` fires on `python3 -m
pytest -v`. Turn both changes off with `NOBLIVION_GUARD_EVERYDAY_TRIGGERS=0`
(config key `guard.everyday_triggers`). They are on by default.

A plain trigger also fires when the command runs with a directory path. The
command word, and the script after an interpreter (`python3`, `bash`, `node`),
count with each path suffix and without the interpreter. A trigger
`docker compose down` fires on `/usr/bin/docker compose down`. A trigger
`tools/worktree_guard.py remove` fires on `python3 tools/worktree_guard.py
remove x` and on `python3 /srv/proj-a/tools/worktree_guard.py remove x`. A
path that is an argument of another command (`cat /srv/tools/x.py`) does not
count. This adds rows only: no new deny.

### Deny: block a command

When a `violates` expression matches a Bash command, the hook blocks the
command. The reason names the rule.

- The hook checks the full command and each part of a pipeline or a
  command list.
- It removes wrappers first, such as `env`, `sudo`, `timeout` and
  `VAR=value` prefixes.
- It ignores quoted data and comments.
- It checks the command inside `bash -c`, `eval` and `ssh host '...'` as a
  command of its own.
- A heredoc body or a here-string (`<<< "..."`) is data, so the hook
  ignores it, unless a shell reads it. Then the body is code, and the hook
  checks it as a command of its own. These readers make a body code:
  - `bash`, `sh`, `zsh`, `dash` and `ksh`, also by path (`/bin/bash`),
    behind a wrapper (`sudo`, `env`, `command`, `exec`, `nohup`,
    `timeout`, `nice`, `VAR=value`) and with `-s`. With `-c` or a script
    file, the shell reads the body as data.
  - `su` without `-c`, `sudo -s` and `sudo -i`.
  - `ssh host` with no remote command, or with a remote shell
    (`ssh host bash -s`).
  - `cat` with no file, when it pipes to one of these readers
    (`cat <<EOF | bash`).
- Every other reader keeps the body as data, for example `cat > file`,
  `tee`, `git commit -F -`, `gh pr create --body-file -` and `python3`.

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

### Deny: protect a process

A `violates` expression guards one spelling of a command. A rule such as "do
not run `pkill -f`, because the pattern also matches the CI runner" does not
stop `ps`, then `kill 4711 4712`. The `protects_process` field guards the
target instead:

```yaml
violates: "\\bpkill\\s+-f\\b"
example_repeat: "pkill -f runner"
example_ok: "kill 4711"
protects_process: "/opt/ci/bin/ci-(runner|worker)\\b"
```

Before a Bash call, the hook finds the processes that the command would
signal. When the command line of one of them matches a `protects_process`
expression, the hook blocks the command. The block is the same as a
`violates` block: the reason names the rule, and the same override works. The
reason also names the process, for example
`This command would signal process 4711 (/opt/ci/bin/ci-runner --listen), which this rule protects.`

The command line of a process is its arguments joined by blanks, as `ps`
shows them. The hook finds the targets of these commands:

| Command | Targets |
| --- | --- |
| `kill [-SIG \| -s SIG \| -n N] PID...` | each PID that exists; a PID that does not exist is ignored |
| `kill -- -PGID`, `kill -9 -PGID` | every process of the process group |
| `kill -9 -1` | every process |
| `kill $(pgrep [-f] PAT)`, `` kill `pgrep PAT` `` | the processes that `pgrep` prints |
| `pgrep [-f] PAT \| xargs kill` | the processes that `pgrep` prints |
| `pkill [-f] PAT` | as `pgrep`: `-f` matches the full command line, else the process name; `-x` the whole text; `-i` any case; `-v` the processes that do not match; `-P`, `-g` and `-s` filter by parent, group and session |
| `killall NAME` | a process whose name, or the base name of its first argument, is `NAME`; `-r` reads `NAME` as an expression, `-I` any case, `-g` adds the group |

- The hook reads the commands as the deny check does: in a list or a
  pipeline, after wrappers (`sudo`, `env`, `timeout`, `VAR=value`), and
  inside `bash -c '...'`, `sh -c`, `su -c`, `eval` and `$( )`. Quoted data,
  comments and heredoc bodies are not commands.
- A command inside `ssh host '...'`, `docker exec` or `kubectl exec` runs on
  another machine or in another process table. The hook does not check it.
- The hook reads the process table only when a rule has `protects_process`
  and the command holds the word `kill`, `pkill` or `killall`. It reads
  `/proc/PID` for `kill PID`, and all of `/proc` for a pattern or a group. In
  a test with 900 processes, a full read took about 15 ms.
- The check runs inside the time limit of the deny check. When the time runs
  out, the hook allows the call and prints the line of the time limit, as for
  a `violates` check.
- The check works on Linux only. On another system it finds no target, and
  the hook prints nothing about it.

The check has these limits:

- It does not resolve a PID that the command finds when it runs:
  `kill $PID`, `kill $(cat app.pid)`, `kill %1`, `kill 0`, `pkill -P $$`, and
  `ps aux | grep x | awk '{print $2}' | xargs kill`.
- It does not resolve `pidof`, `fuser -k`, `systemctl stop` or
  `docker stop`.
- `pkill -u`, `-t`, `-n` and `-o` filters are not applied, so a command can
  count more targets than it signals, never fewer. A pipe after `pgrep`
  (`| head -1`) is not followed either.
- `pkill -f PAT` also matches the shell that runs the command, because its
  command line holds `PAT`. That shell does not exist yet when the hook runs,
  so the hook cannot see it.

### Where a rule fires

A rule can be right in one repository and wrong in another. Two things limit it.

**`where:`** is a list of path globs, separated by commas:

```yaml
where: "/srv/app/**, ~/work/tool"
```

The rule (deny, rewrite and rows) fires only when the call works inside one of
the globs. The call works in its working directory, in the target of each
`git -C <path>` and in the directory after each `cd <path>` in the command
(for the commands that follow the `cd`). A glob matches the folder and every
folder below it. `*` matches inside one folder name, `**` matches across
folders, and `*` or `**` alone means everywhere. A glob starts with `/`, `~/` or
`**/`. For a file edit or a file read, the folder of the file counts. A
directory that the hook cannot work out (`cd "$DIR"`, `git -C $(pwd)`) keeps the
rule firing, so the guard is never switched off by a variable.

**The repository the note names.** A note without `where:` that names a git
repository by an absolute path (or `~/path`) in its `violates` regular
expression or its triggers fires only inside that repository. A path in the
rule, the apply text or the examples names the remedy, not the place of the
hazard, so it does not scope the rule. A linked
work tree (`git worktree add`) counts as its main repository. A note that names
more than three repositories, or only the home folder, is general and fires
everywhere. The table build works this out and keeps it in the table entry
(`repos`). Write `where: "**"` to make such a note fire everywhere.

### Quarantine of an overridden rule

A deny that the model overrides most of the time costs one turn and the deny
text, and teaches nothing. When a deny rule has 3 or more overrides and an
override rate of 50 percent or more in the last 14 days, the guard stops
denying it. The rule shows as a row where it would have denied, and the guard
log gets a `quarantined` line. The session-start line names the rule:

```text
Guard: 1 deny rule(s) shown as a row only, overridden too often in 14 days:
feedback_example (overridden 32 of 53). Fix or remove them
```

The counts come from the `deny` and `override` lines of the guard log
(`guard-log.jsonl` and `.1`). The rate is overrides over the larger of denies and
overrides. The rule leaves the quarantine by itself: when it is not overridden
for 14 days, or when its overrides leave the window, it denies again. Fix the
rule or remove it. The credential guard is never quarantined. Turn the
quarantine off with `NOBLIVION_GUARD_QUARANTINE=0` (config key
`guard.quarantine`).

To measure the rule on your own log, run
`python tools/replay_guard_quarantine.py <data dir>/guard-log.jsonl`. It prints
counts only.

### Override

To run a blocked command on purpose, end the command with a comment:

```sh
some-command  # guard-ok: <reason>
```

The reason must hold a letter or a digit. When the rule has `complies`,
the override works only after the right command (the `example_ok` shape)
failed in this session.

### Rewrite and ask

Two switches, both off by default, let a rule act on the call instead of only
denying it. With both off, the `on_violation` field does nothing, and a
`rewrite` field makes its note a rewrite note (see "A rewrite note is not a deny
note" below). The guard writes no extra log line.

**Rewrite** (`NOBLIVION_GUARD_REWRITE=1`, config key `guard.rewrite`). When the
`violates` expression of one note hits and the note has a valid `rewrite`
field, the hook runs the command in the shape of the rule. The hook sends the
new command back as `updatedInput`, with `ask` as the permission decision (the
user sees the new command) or `allow` (`NOBLIVION_GUARD_REWRITE_DECISION=allow`:
the hook approves the command itself, so the settings prompt for it is
skipped; deny rules in the settings still win). The model gets one line that
names the new command and the `apply` text. The reason names the note id only.

The `rewrite` field has one form, `add-flag <flag> after <prefix>`. The pair
must be in a closed table in `hooks/guard_rewrite.py`: `-q` after `pytest`,
`python -m pytest`, `python3 -m pytest` or `uv run pytest`; `--oneline` after
`git log`; `-sb` after `git status`. These flags change the form of the
output and keep the exit status. The table holds no flag that cuts data (`-20`,
`-n`) and no pipe (`| head`): a cut hides data, and the matcher splits a command
at every pipe, so a `violates` expression cannot tell `git log` from the first
segment of `git log | head`.

`python -m <prog>`, `python3 -m <prog>` and a path to a python binary with
`-m <prog>` (`.venv/bin/python -m pytest`) count as `<prog>`. So
`add-flag -q after pytest` also rewrites `python3 -m pytest tests` into
`python3 -m pytest -q tests`.

The hook reads the command as the deny check does. It rewrites each simple
command of a list or a pipeline that starts with the prefix, after the
wrappers (`env`, `timeout <n>`, `VAR=value`, `sudo`, `nice`). Every other byte
of the command stays as it was:

| Command | Rewritten command |
| --- | --- |
| `cd ~/proj && pytest tests/unit` | `cd ~/proj && pytest -q tests/unit` |
| `make; pytest tests \| tail -5` | `make; pytest -q tests \| tail -5` |
| `env CI=1 timeout 600 pytest tests` | `env CI=1 timeout 600 pytest -q tests` |
| `.venv/bin/python -m pytest -x` | `.venv/bin/python -m pytest -q -x` |
| `git log && git status` (two notes) | not rewritten: two notes hit (`multi_hit`) |

Claude Code applies `updatedInput` only together with an `allow` or `ask`
decision, which is why the hook never sends it with nothing else. Do not run
a second hook that also rewrites Bash commands next to this one: two hooks that
change the same call may not both apply.

The hook rewrites only when nothing is in doubt. It falls back to the
`on_violation` decision of the note (see "A rewrite note is not a deny note"; a
hit that holds a note without `rewrite` keeps the deny) and logs
`rewrite-skipped` with a `why` when:

- more than one note hit (`multi_hit`);
- no simple command starts with the prefix, or one that does is not plain
  (`not_applicable`): its output goes to a pipe other than one `| tail` after
  a test runner, it has a redirect to a file, a variable or a substitution, or
  it is inside `( )`, `{ }`, `if`, `for`, `while` or `case`. A command with a
  heredoc or a line continuation is not rewritten, and neither is a command
  inside `bash -c '...'`, `eval` or `ssh host '...'`: that text is quoted. A
  simple command that already holds the flag, or a flag of the same kind
  (`-v` for `-q`), is kept as it is; when no other command changes, the
  result is `not_applicable` too;
- the new command still hits a `violates` expression (`still_violates`);
- the note has `complies` and the new command does not match it
  (`no_compliant_form`).

A rewrite is idempotent: the new command holds the flag, so it is not
rewritten again. A `# guard-ok` marker is an override, never a rewrite. When
the table is built, a bad `rewrite` is dropped alone and the deny stays. The
check runs the rewrite on `example_repeat`: the result must not match
`violates`, must match `complies` (if set), and the rewrite must not apply to
the result or to `example_ok`.

**Ask** (`NOBLIVION_GUARD_ASK=1`, config key `guard.ask`). A note with
`on_violation: ask` makes the hook ask the user, with the rule, the apply text
and the note id as the reason, instead of denying. If any note in the hit is
not marked `ask`, the deny stays (`ask-skipped`). A rewrite is tried before an
ask.

**Shadow** (`NOBLIVION_GUARD_ACT_SHADOW=1`, config key `guard.act_shadow`). The
hook only logs `would-rewrite` and `would-ask`. A note without `rewrite` denies
as before; a rewrite note runs unchanged. A switch
that is on acts for real. Use shadow first, and read the log.

The log lines are `rewrote`, `asked`, `would-rewrite`, `would-ask`,
`rewrite-skipped`, `ask-skipped` and `act-error`. They hold the note id, the
command text as every guard line does, and `kind`, `perm` or `why`; they hold no
rule text. A rewrite and an ask are trust events of kind `use` with the sources
`rewrite` and `guard_ask`; shadow and skip lines are not.

To measure on real commands before you turn a switch on:
`python tools/replay_guard_rewrite.py "<transcripts glob>"`. It prints counts
only.

**A rewrite note is not a deny note.** A note with a valid `rewrite` field never
adds a deny, so writing one cannot block the plain command while the switches
are off. The states, for a hit set made only of rewrite notes:

| `guard.rewrite` | `guard.ask` | `guard.act_shadow` | Rewrite possible | Result |
|---|---|---|---|---|
| off | off | off | - | The command runs unchanged. Nothing is logged. |
| off | off | on | yes | The command runs unchanged. The log has `would-rewrite`. |
| off | on | any | - | The hook asks, with the rule as the reason (`asked`). |
| on | any | any | yes | The command is rewritten (`rewrote`). |
| on | on | any | no | The hook asks (`asked`, with `rewrite-skipped`). |
| on | off | any | no | The command runs unchanged. The log has `rewrite-skipped`. |

`on_violation` of a rewrite note is `ask` when the field is absent. A rewrite
note that has `on_violation: deny` is a deny note: it denies in every state and
the rewrite acts only when `guard.rewrite` is on. The table build and the
memory write hook warn about that clash, because it is almost always a mistake.
If one hit is a note without `rewrite`, the deny stands as before. An error in
the act step keeps the deny (`act-error`).

Do not put `rewrite` on a note that guards a dangerous command. Such a note
would stop denying while the rewrite settings are off. Write a deny note
(no `rewrite`) for a command that must never run, and a rewrite note only for
a command whose plain form is harmless and whose rewritten form is the same
command with a flag that changes the form of the output.

Undo: unset the switches. The fields stay in the notes and do nothing.

### Label rows

Label rows show notes that name the file, the tool, the ticket or the
service in the call. They never block. They fill only the free row places
and have their own budget of 700 characters per session. A label row is the
rule line of the note and nothing else (at most 250 characters). Ticket and
service labels need `labels.ticket_prefixes` and
`labels.service_prefixes` in the config.

With `NOBLIVION_RECALL_RULES_ONLY` on (config key `recall.rules_only`), a
label match shows only when its note has both a `rule:` and an `apply:`
field. A note without them stays reachable with the `noblivion_recall` tool.
The guard log line counts the matches that the rule left out in
`label_no_apply`. The switch is on by default.

A label that more than 8 notes hold never matches alone. One exception: when
the label is exactly the target file of the call, it counts. The target is the
`file_path` of Edit, Write, MultiEdit and Read, or a word of a Bash command
that is the path (a word in quotes with spaces, a comment and a prompt name
no target). The label must equal the full file name or the full relative path:
`preferences.yaml` is not `role_preferences.yaml`, and a file named only in the
content of an edit is no target. At most 3 such notes show, the best by label
score first, and the guard log line lists them in `label_exact`. A tool name
and a `.md` file name never match alone, at any count.

A file name is not a subject by itself. Many projects have a `pyproject.toml`,
a `store.py` or a `db.py`. A name on the stop list (`pyproject.toml`,
`pytest.ini`, `db.py`, `store.py`, `config.json` and a few more), or a name
that more than 3 memory folders hold, counts for a note only in two cases:

- the call names the file with a relative path (`src/app/store.py`) and the
  note names the same path, or
- the note is in the project folder of the session. When the project folder
  is itself a shared root, it holds the notes of many projects, so none of
  its notes is "in the project" for this rule.

The exact-name exception above needs one of the two as well. A name with no
folder, and a note that names only the bare name in another folder, no longer
match. The index in the guard table keeps the folder count of each label and,
for these names, the paths of the notes outside the project folder.

## The credential guard

The credential guard runs first, before the rule checks. It blocks a
command that would print a git URL with a password or a token in it. It
also blocks a call that would print a file from a closed list of secret
sources (see "Secret sources" below).

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

### Secret sources

The guard also blocks a Bash, Read or Grep call that would print one of
these files. A file counts only when it exists and holds something.

| Source | What counts |
| --- | --- |
| `.env` files | `.env` and `.env.<name>` in any folder. Not a template: `.env.example`, `.env.sample`, `.env.template`, `.env.dist`. |
| Cloud and registry files | `~/.aws/credentials`, `~/.netrc`, `~/.docker/config.json`, `~/.kube/config` |
| Private keys | `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, any `*.pem` and `*.key` file whose first 4 KB hold `PRIVATE KEY-----`. A public key or a certificate passes. |
| Mounted secrets | Any file in `/run/secrets` |
| The environment | A bare `env` or `printenv`, with no command and no name. `printenv HOME` and `env FOO=1 cmd` pass. |

- A print command that names such a file is blocked: `cat`, `head`, `tail`,
  `less`, `sed`, `awk`, `cut`, `sort`, `base64`, `xxd`, `diff`, `jq`, and an
  interpreter one-liner that names the file. The name can be a glob, a brace
  list, standard input or a variable that the command sets.
- A `grep` is blocked when it can print a line of the file. `grep -c`,
  `grep -q`, `grep -l`, `wc` and a pattern that matches no line pass. So does
  `sed -i`. A counting `grep` or `wc` later in the pipe passes too:
  `env | grep -c '^NAME='`.
- The output of a `$( )` that goes into a variable or an argument does not
  reach the model, so `TOKEN=$(grep ^TOKEN= .env | cut -d= -f2)` passes. A
  `$( )` inside `echo` or `printf` is blocked.
- The deny text is about 300 characters. It gives one safe form,
  `grep -c '^NAME=' FILE` (or `env | grep -c '^NAME='`), and the override.
- Override. End a Bash command with `# guard-ok: <reason>` to read a secret
  source on purpose. The guard logs `credential-override`. The Read and Grep
  tools cannot carry the marker: run the read in Bash. The git URL part of the
  guard has no override, also in a command that holds both: the marker then
  lets the secret source pass and the git URL still blocks.
- Measure before you rely on it: `python tools/replay_credential_sources.py
  "<transcripts glob>"` runs the Bash, Read and Grep calls of local
  transcripts through this check and prints counts only (no command, no path,
  no value). On 226,107 calls from one user it gave 0.15 % would-deny calls.
- Turn this part off with `NOBLIVION_GUARD_CREDENTIAL_SOURCES=0`. The git URL
  part stays on.
- Limits. The guard does not follow a copy (`cp .env x; cat x`), a variable
  that holds the value (`x=$(cat .env); echo $x`), a path in a variable it
  does not know, a backtick substitution, `set`, `export -p`, `declare -p`,
  or `find -exec cat`. A command whose output goes to a file (`env > out`)
  passes, so a later `cat out` is not seen either.

The guard has these limits:

- The git URL part covers git URLs only. Other secrets are covered only
  through the closed list of secret sources below. A secret in a file or
  a command that is not on the list passes.
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
  denied up to 4 MB. These sizes are from one machine. The time of the
  check grows with the length of the command, so a slower machine reaches
  the limit at a smaller size: on a slow CI machine with Python 3.9 the
  check of a command of 500 KB took more than 1.5 seconds.

## The memory fields hook

After Claude Code writes a `feedback_*.md` or `project_*.md` file in the
memory folder, this hook checks its fields.

- For a feedback file: the rule fields.
- For a project file: `status` and `project`.

It never blocks. It returns a warning to Claude Code, which can then fix
the file. After a memory file of any kind changes, it rebuilds the guard
table, because a note of any kind can hold a `violates` rule. A change to
`MEMORY.md`, `MEMORY_ARCHIVE.md` or a `topic_*.md` file does not rebuild it.

It also warns (and keeps the rule) when a `violates` expression ends in a fixed
path, such as `docker cp .*:/app/`. That rule catches one spelling of the
target: the same command with `/tmp/` is not caught. End the expression on the
command and its flag, or match the path with a pattern such as `[^ ]*`. The
table build lists the same warnings in `guard_table.py --rebuild --report`.

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
| `<data dir>/guard-log.jsonl` | One line per guard decision: rows, deny, override, rewrite, ask, label rows, credential deny, error. It holds command text. Mode 0600. Rotated at 5 MB. |
| `<data dir>/guard-state/` | State per agent and session. Removed after 7 days. |
| `<data dir>/guard-tables/<table>.probes` | The measured speed of each `violates` expression of that table. You can delete it: the next build measures again. |
| `<data dir>/guard-tables/<table>.late` | The state of the memory folder whose rebuild ran out of time. While the folder has this state, the guard hook does not try the rebuild again. The guard hook removes the file when a rebuild that it starts itself, before a tool call, is done in time. The rebuild at the session start and the rebuild after a write of a memory file do not remove it. You can delete it: the next tool call then tries the rebuild again. |
| `<data dir>/stop-check-log.jsonl` | One line per stop check decision. Rotated at 5 MB. |
| `<data dir>/cache/memory_sync.log` | The memory sync runs. Rotated at 5 MB. |

## Turn a guard off

| Guard | How |
| --- | --- |
| Credential guard | `NOBLIVION_GUARD_CREDENTIAL=0`. The secret sources only: `NOBLIVION_GUARD_CREDENTIAL_SOURCES=0` |
| Label rows | `NOBLIVION_GUARD_LABELS=0` |
| Rewrite, ask, shadow | They are off by default. Unset `NOBLIVION_GUARD_REWRITE`, `NOBLIVION_GUARD_ASK` and `NOBLIVION_GUARD_ACT_SHADOW`. |
| Weak-trigger check | `NOBLIVION_GUARD_WEAK_SHARE=0` |
| Everyday triggers, `python -m X` | `NOBLIVION_GUARD_EVERYDAY_TRIGGERS=0` |
| Quarantine of overridden deny rules | `NOBLIVION_GUARD_QUARANTINE=0` |
| Stop checks | They do not block by default (`shadow` mode). In `enforce` mode: `NOBLIVION_STOP_CHECKS=none`, or `NOBLIVION_STOP_CHECK_MODE=shadow` |
| Memory sync | `NOBLIVION_MEMORY_SYNC_OFF=1` |
| Guard hook, memory fields hook | Remove the rule fields from the note, or turn off the plugin. These hooks have no switch. |
