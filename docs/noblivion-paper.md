<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# The NOBLIVION paper

Features, differences from other Claude Code memory, and the path from the first prototype.

Written 2026-11-02 against NOBLIVION 0.1.21.

## Abstract

NOBLIVION is a local plugin that gives Claude Code and Codex one searchable memory. It reads the same Markdown notes that Claude Code already keeps. It adds ranked recall on every prompt, rules that can block a tool call, recall after a failed command, and safe clean-up with undo.

Every one of these features started in an earlier prototype that ran on a server database. The prototype measured each feature on real sessions. NOBLIVION is the part of that work that survived, rebuilt so that it stands alone.

Main finding: the mechanism works and is safe. The value on real work is not yet proven. Section 5 gives the numbers.

## 1. The problem

Claude Code starts every session with an empty context. Its built-in memory is a folder of Markdown notes. Claude loads the first 200 lines or 25 KB of the index file `MEMORY.md` at session start. Claude opens a topic note only if it decides to. Rules in `CLAUDE.md` are advice, and nothing enforces them. After `/clear` or `/compact`, anything said only in chat is lost. A subagent gets none of the main session's notes.

A note that is not read at the right moment has no effect. NOBLIVION moves the right note to the right moment and keeps the notes as plain files.

## 2. The most significant features

1. **Ranked recall on every prompt.** A hook searches all notes with keyword and meaning-based search. It adds the best matches, each with a short "apply" line, under a score floor and a size cap.
2. **Guards that can block.** A note can carry a `violates` pattern. Before a tool call, a hook shows the matching rule. If the command matches the pattern, the hook blocks it. A written reason (`# guard-ok: reason`) overrides the block.
3. **Error recall.** When a shell command fails, a hook finds notes that quote the error text and shows them.
4. **Continuity and subagent rules.** The rules shown before a compaction come back after it. An optional briefing opens each session. A subagent receives the matching rules when it starts.
5. **Files stay the truth, and every write has an undo.** The SQLite index can be rebuilt from the notes at any time. Duplicate merge, archive and import can all be reverted byte for byte.
6. **Measure first, enforce later.** Trust ranking and stop checks ship in shadow mode. They log what they would do and change nothing, until a measured gate passes. That is the shipped default. The maintainers run stop checks in enforce mode (section 5).
7. **Local by default, consent for anything else.** The store listens on the loopback address only. Two features can send text to a hosted service, and both stay off until the user types consent. A credential guard blocks commands that would print a git URL with a token.
8. **One store, several projects, two clients.** One store indexes every project folder. Claude Code and Codex share the same notes.
9. **Opt-in learning.** A background loop turns conversations into scoped notes, with a zero-cost local model only.

## 3. What sets it apart

### Against the built-in Claude Code memory

The repository's own comparison ([NOBLIVION against Claude Code memory](noblivion-against-claude-code-memory.md)) scores 13 dimensions from 0 to 5. NOBLIVION scores 42 of 65 and the built-in memory 22 of 65. NOBLIVION leads on 10 rows and ties on privacy. It loses on operation burden (it needs an install, a model download and manual updates) and on context cost (it adds text to every prompt). The authors of NOBLIVION wrote this scoring, so read it as a self-assessment.

### Against other memory modules

The other modules were not installed or tested. The descriptions below come from third-party blog and directory pages, which disagree on details such as star counts and licences.

| Module | How sources describe it | What NOBLIVION does differently |
| --- | --- | --- |
| claude-mem | Local plugin. Hooks record tool use. Observations are compressed into SQLite and a Chroma vector store and injected in later sessions. Four MCP search tools. | NOBLIVION indexes notes that people write and read; it does not compress a recording of tool use. It adds rule enforcement and error recall. |
| Mem0 MCP server | Hosted cloud memory with nine tools. Needs an account and API key. | NOBLIVION stays on the machine by default and needs no account. |
| OpenMemory MCP | Mem0's self-hosted, local-first option. | Same locality. NOBLIVION also pushes notes through hooks, so the model does not have to ask. |
| mempalace | MCP only. Mines projects and conversations into a vector store. No hooks, so retrieval is query-driven. | NOBLIVION pushes by hook and also offers pull by MCP. |

The common pattern in the sources is hooks for capture and MCP tools for retrieval. The difference NOBLIVION claims is on the control side. Its notes act as rules, and a rule can stop a tool call. Its writes are reversible. Its risky features run in shadow first. No source describes these three properties in the other modules, but their absence was not tested.

## 4. How the features came from the prototype

In September 2026 the maintainers asked how Claude Code could get a memory that grows. The answer was: keep Claude Code as the harness, and put the memory behind hook scripts. Three loops followed: capture, recall and reinforcement.

The prototype found a weak step. About 550 correction-like turns and 1,051 tool errors sat unused in 293 transcripts, while only 419 feedback notes existed. Capture relied on the model choosing to write a file.

| | Prototype (2026-09 to 2026-10) | NOBLIVION (from 2026-10-03) |
| --- | --- | --- |
| Store | Server database with row-level security | SQLite file, one local process |
| Embeddings | bge-m3, 1024 dimensions | bge-small, 384 dimensions, no network |
| Transport | REST over the network | Loopback with a token handshake |
| Client code | Hooks, guard, dedup, trust | Moved as it was |

A review on 2026-10-03 showed why the port was cheap. Ranking never ran in the database. The prototype loaded about 1,300 rows into memory and ranked them with a vector library. A SQLite store could do the same job, so the team rebuilt the store on SQLite and did not copy the server. NOBLIVION replaced the prototype hooks on the maintainers' main machine on 2026-10-08.

| NOBLIVION feature | Measured fact that shaped it |
| --- | --- |
| Ranked recall | In the prototype's recall test the target memory arrived in 10 of 13 runs, against 2 of 13 for stock memory (p = 0.0107). |
| Guards, deny, override | 0 false denies on 4,899 Bash commands, after three `violates` patterns were fixed. |
| Error recall | The 1,051 unused tool errors in the transcripts. |
| Subagent rules | The built-in memory gives a subagent no notes. |
| Continuity, briefing | The briefing is capped at 1,500 characters. No compact event appeared in the prototype's test logs, so that path is less proven. |
| Label rows | 555 label decisions logged in the live week. |
| Transcript miner | About 550 corrections in 293 transcripts. |
| Learning loop | The learning tables grew about 24 MB a day, which led to a daily budget. |
| Duplicate sweep with undo | The hosted judge missed its accuracy floor (10 of 12 and 7 of 12 on two models), so the sweep needs consent and has undo. |
| Trust and shadow gates | Citation precision was 15 of 29 (lower bound 0.344, bar 0.70), so trust ranking stays in shadow. |
| Stop checks | 11 of 31 stop blocks were false in the live week, so they ship in shadow. |
| Credential guard | In a memory-trap test the token URL reached the model in 10 of 10 cells. |
| Note fields (rule, apply, scope, triggers) | Guards need machine-readable fields. |
| Remember tool; import and migrate | New in NOBLIVION. The prototype had a write-back story that was deferred. |

### What the prototype's live week taught the design

The prototype's live week (2026-10-01 to 2026-10-08) did not meet its target. 30 of 60 guard denies and 11 of 31 stop blocks were false, mostly during work on NOBLIVION itself. No complaint was filed. The result changed NOBLIVION in one clear way: any feature that blocks or reorders must pass a measured gate first. That rule is why feature 6 exists.

## 5. Evidence and limits

- **Use rate.** About 0.7 percent of recalls were marked as used (55 of 7,985 in the repository's measurement of 2026-10-15; 70 of 10,348 in the prototype data).
- **Fixtures.** Recall precision of 0.857 comes from a made-up set of 36 notes. It is not held out, and the documents call it too good as a forecast.
- **Stop checks, today.** The shipped default is shadow. On the maintainers' main machine they are in enforce mode, with four checks on (commit, tests, notify, lesson). In the last 7 days there were 1,242 stops. 28 were blocked (24 notify, 3 tests, 1 lesson) and 10 got a reminder. No count of false blocks exists for this period.
- **Trust ranking, today.** The shipped default is shadow. On the maintainers' main machine it is on, in the config file and in the environment settings, by a decision of 2026-11-02. Stop checks stay in enforce mode by the same decision. The earlier test showed no gain: mean reciprocal rank was 0.447 with trust off and with trust on. Today's trust report lists no note to retire, promote or demote.
- **Learning, today.** Learning is on for three project folders. It uses a local model at zero cost. Of its jobs, 2 committed a note, 40 are deferred and 163 failed (131 pruned, 92 failed schema checks, 70 provider errors, 40 over budget; one job can fail for several reasons). The yield is still very low. The transcript miner is off, and mined notes stay out of the prompt.
- **Cost.** Each prompt can carry up to 9,000 characters of ranked notes. The text is re-sent on every later turn. It is bounded but not yet measured on real prompts (ticket NOBLIVION-116).
- **Platform.** No Windows. One operating-system user per store. English-only embeddings. No sync between machines.

The scores and counts above were measured at different times and on different versions. The current release is 0.1.21.

## 6. Conclusion

**Target.** A coding agent that recalls the right note at the right moment, enforces rules at the tool call, shares memory across projects and clients, and keeps itself clean, while the notes stay plain files and nothing leaves the machine without consent.

**Distance covered.** The mechanism is built, on and guarded. It is a direct port of features that were measured on real sessions, and the prototype's failures set its safety rules.

**Distance left.** Proof of value on real work. That needs a measured use rate on real prompts, a held-out evaluation, and a measure of context cost. Trust ranking and stop checks ship in shadow. The maintainers' main machine has already moved past that default, so the gate evidence for those two settings must be shown, not assumed.

## Sources

1. NOBLIVION repository documents: [executive summary](executive-summary.md), [comparison with Claude Code memory](noblivion-against-claude-code-memory.md), [changelog](../CHANGELOG.md).
2. [emelia.io on claude-mem](https://emelia.io/hub/persistent-memory-claude-code-claude-mem)
3. [mem0.ai, persistent memory for Claude Code](https://mem0.ai/blog/persistent-memory-for-claude-code)
4. [claudepluginhub.com, memory plugins that persist](https://claudepluginhub.com/blog/claude-code-memory-plugins-that-persist)
5. [augmentcode.com on claude-mem](https://www.augmentcode.com/learn/claude-mem-persistent-memory-claude-code)
