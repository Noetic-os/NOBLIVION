<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Recall as a reflex

The `noblivion_recall` tool is called about once in 5,000 tool calls. The prompt hook shows rows
before the model has judged anything, so the model rarely decides to ask. Three changes name the
moments where a lookup pays. The moments are:

- before you write "cannot verify";
- before a merge;
- before a restart of a shared service;
- before you file or comment on a ticket;
- before you save a memory (recall first, so you do not save a second note).

## What ships

| Part | State | What it does |
| --- | --- | --- |
| Tool descriptions of `noblivion_recall` and `noblivion_remember` | on | Name the moments. The tool list is 1,815 characters (NOBLIVION-161). |
| MCP server instructions | on | One text of about 70 tokens with the same moments. Claude Code lists a plugin's MCP tools by name only when it defers them, so the descriptions are not in the model's view then; the server instructions are. Set `NOBLIVION_MCP_INSTRUCTIONS=0` to leave them out. |
| Skill `recall-first` | on | The plugin skill `noblivion:recall-first`. Its name and description (about 50 tokens) are listed each turn. Its steps load only when the model uses it. Remove the folder `skills/recall-first` to undo. |
| The nudge (`hooks/recall_nudge.py`) | off | One line before a ticket tool or a merge command. See below. |

Together the static text adds about 150 tokens to each turn (estimate: characters over 4). The
nudge adds a line only when it fires.

## The nudge

Before a matching call the hook adds one line of context, under 120 characters:

```
Recall first: call noblivion_recall with query "<topic>", then act.
```

The topic is the ticket key or summary of a ticket call. For a merge command it is the branch of the
working folder when the argument is only a PR number, else the argument. The hook adds the line only
when the main session made no recall call and received no earlier line in its last 10 tool calls. It
reads the tail of the session transcript for that. When the tail does not show 10 calls and does not
reach the start of the file, the hook stays silent. A call from a subagent gets no line.

| Env var | Config key | Default |
| --- | --- | --- |
| `NOBLIVION_RECALL_NUDGE` | `recall.nudge` | off. On: the hook prints the line. |
| `NOBLIVION_RECALL_NUDGE_SHADOW` | `recall.nudge_shadow` | off. On, with the first off: the hook logs what it would do and prints nothing. |
| none | `recall.nudge_tools` | list of regular expressions over the MCP tool name (case-blind search). |
| none | `recall.nudge_commands` | list of regular expressions over each simple command of a Bash call, at its start. |

The two lists decide which calls count as the moments. The hook has no tool name of any one
tracker or organisation in its logic; the defaults are generic patterns:

- `recall.nudge_tools`: `^mcp__.+__create_?(jira_?)?(issue|ticket)$` and
  `^mcp__.+__add_?(issue_?)?comment(_?to_?(jira_?)?issue)?$`. They match `create_issue`,
  `createJiraIssue`, `add_issue_comment`, `addCommentToJiraIssue` and the same shapes on other
  servers. They do not match `getJiraIssue`, `editJiraIssue`, `createIssueLink` or
  `addWorklogToJiraIssue`.
- `recall.nudge_commands`: `^gh\s+pr\s+merge\b` and `^glab\s+mr\s+merge\b`.

A list that is set replaces the default list. A bad expression is skipped. A command pattern is
matched against each live simple command: quoted text, comments and here-document bodies are
masked first, as in the guard, so `grep "gh pr merge" notes.md` does not match. `^` is the start of
the simple command, after variable assignments and wrappers such as `env` and `cd dir &&`.

Example, in the config file: nudge before an in-house merge script and a tracker tool of a
made-up server.

```json
{
  "recall": {
    "nudge": true,
    "nudge_tools": ["^mcp__tracker__(open_case|comment_case)$"],
    "nudge_commands": ["^gh\\s+pr\\s+merge\\b", "^tools/merge-pr\\b"]
  }
}
```

To try it first, set `NOBLIVION_RECALL_NUDGE_SHADOW=1` and leave the other switch off. Undo: unset
both switches. The hook is then a few lines of code that return at once.

Cost. The hook is bound to `Bash` and to every `mcp__.*` tool, because a plugin matcher is fixed
text and the patterns come from the config. With both switches off it starts one Python process
for such a call and returns at once. A line costs about 20 tokens and stays in the context for the
rest of the session.

## Logs

`<cache>/nudge.log` (the cache dir of the recall hook) has one line per matching call and one per
recall call, with no query, topic or command text. It is a separate file because tools read
`recall.log` as one line per prompt.

```
2026-10-20T08:00:00+00:00 session=S event=nudge moment=tool result=fired chars=77
2026-10-20T08:00:09+00:00 session=S event=recall_call fetch=0 after=tool
```

- `moment` is `tool` for an MCP tool match and `command` for a command match.
- `result` is `fired`, `would_fire` (shadow), `skip_recent_recall` (the session recalled within
  its last 10 calls), `skip_cooldown` (a line was given for an earlier call within the window) or
  `skip_no_window` (no transcript, or too long to read).
- `after` on a recall call is the moment of the last nudge when that nudged call is still within
  the last 10 tool calls, else `-`. It tells a recall call that followed a line from one that did
  not. A recall call at one of the other moments of the list above has no line to follow; count it
  with the replay tool.

Count per moment:

```bash
awk '$3=="event=nudge"{print $4, $5}' nudge.log | sort | uniq -c
awk '$3=="event=recall_call"{print $4, $5}' nudge.log | sort | uniq -c
```

## Measuring

`tools/replay_recall_nudge.py` applies the hook's own matching and window rule to past
transcripts. It reports recall calls and sessions with a recall call, the calls that match, how
many lines the nudge would have printed, how many were skipped and why, and the characters of the
lines. It reads counts only.

```bash
python tools/replay_recall_nudge.py "~/.claude/projects/*/*.jsonl" --since-days 30 --json
```

A replay of the past cannot say whether a line changes what the model does, because the model
never saw a line. That needs one live week with `NOBLIVION_RECALL_NUDGE` on. Read it as follows:

1. Run the replay over the week before the switch (the base rate) and over the live week. Compare
   `sessions_with_recall` over `sessions`, and `recall_calls` per session. The target of
   NOBLIVION-119 is at least one recall call per session at the named moments.
2. Read `nudge.log` for the live week. Of the lines that fired, the share of `recall_call` lines
   with `after=tool` or `after=command` is the follow rate. The replay field `followed` gives the
   same share for the base rate, with no line.
3. Label a sample of 20 recall calls (by hand, not by script): a call is useful when the reply
   used a row that the call returned. The target is at least 14 of 20 (0.70).
   `tools/replay_transcripts.py` reports uses by source, with `fetch` as one source, for the same
   weeks.
4. NOBLIVION-124 holds the baseline week and the verdict that these numbers go into.
