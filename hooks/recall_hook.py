#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
recall_hook — Claude Code hook that recalls memory from the local store
=======================================================================
The store (``noblivion serve``, design doc 0001) indexes the Claude Code
markdown memory files into a local SQLite database. This hook is the read
side: on every prompt it asks the store for the memories that match what is
about to happen and hands them to the model as DATA. It is registered for
``UserPromptSubmit``. The ``PreToolUse`` path below still works, but the
plugin does not route tool calls to it.

It runs as a Claude Code hook on the user's ``python3`` (3.9 or newer), so it
is one file and imports only the standard library. ``mcp/recall_mcp.py``,
``error_recall_hook.py`` and ``subagent_rules_hook.py`` import it for the
shared HTTP and rendering code. The pure memory-folder helpers live in the
sibling ``corpus.py``; the store discovery and the listener proof live in the
sibling ``store_client.py``.

Events (``hook_event_name`` in the stdin JSON)
  UserPromptSubmit   query = ``prompt``; k = 5; output = plain stdout, which
                     Claude Code adds to the model's context for this event.
  PreToolUse         only for ``tool_name`` in Bash | Edit | Write; query =
                     the Bash ``command``, or the Edit/Write ``file_path`` plus
                     the first 200 chars of ``new_string`` / ``content``; k = 3;
                     output = ``{"hookSpecificOutput": {"hookEventName":
                     "PreToolUse", "additionalContext": ...}}``. No
                     ``permissionDecision`` is set.

Store call (design doc sections 3.3 and 4)
  The base URL is ``http://127.0.0.1:<port>``, with the port from
  ``<data dir>/store.json``. Before the first request the hook proves the
  listener: ``GET /health?nonce=<32 hex>`` with no token, and the answer must
  carry ``proof = hex(HMAC-SHA256(token, "noblivion-health:" + nonce))`` for
  the token in ``<data dir>/token``. Only then does it send
  ``Authorization: Bearer <token>``. No ``store.json``, no token, no answer
  or a wrong proof: the store counts as down (``fail:store_down``,
  ``fail:foreign_listener``), the hook asks the launcher to start it
  (``store_client.request_start``) and fails open.

  ``GET /api/memories/search?q=<query>&project=claude_code&top_k=<n>&root=<root>``
  returns ONE string of entries joined by ``\\n---\\n``: ``{"results":
  ["<blob>"], "namespace": ...}``. An empty pool returns the sentinel
  ``"No memories available."``. ``root`` is the memory folder key of the
  session (section 5.1): the parent folder name of the memory folder.
  The answer also carries ``scores``: one score per entry, in entry order
  (design doc section 4.2). ``NOBLIVION_RECALL_MIN_SCORE`` applies only to a
  hit that carries a numeric ``score``. A body can hold ``---`` lines of its own: only a separator
  followed by a ``[claude_code_md: ...]`` marker starts a new entry.

Source filter (hook only)
  The hook injects ONLY hits from the memory files: an entry that carries a
  ``[claude_code_md: <path>]`` marker line (``Hit.md``). Every other entry
  (transcript-mined rows quote OTHER sessions) is dropped before k, the
  dedupe and the output cap. The hook asks for ``k * MD_ONLY_OVERFETCH``
  candidates, at most K_MAX. The MCP tool (``mcp/recall_mcp.py``) is the
  on-demand path and may return every row.

Fail open
  Total HTTP budget 2 s (``NOBLIVION_RECALL_TIMEOUT_S``), the listener proof
  included. On any exception, timeout, non-200, bad JSON or missing field:
  exit 0 with no stdout. The exit code is 0 on EVERY path. An exception the
  hook did not expect still leaves its one log line, ``fail:internal:<type>``.
  A 30x is a failure too: the client never follows a redirect. At the
  deadline the socket is shut down, so a peer that drips one byte per socket
  operation cannot hold the worker. A body over RESPONSE_MAX_BYTES (1 MiB) is
  ``fail:too_large``. At most WORKER_MAX (4) requests are in flight; past
  that a call is ``fail:busy`` at once.

Transport
  Plain ``http://`` only to a loopback IP literal (``localhost`` is a name,
  and a name is never trusted). ``https://`` is allowed and verified. Any
  other plain host is ``fail:plaintext_url``, decided before a connection
  is opened. The store URL is always ``http://127.0.0.1:<port>``.

Keyword mode (design doc section 8.4)
  An index answer with ``"mode": "keyword"`` has ``score: null`` on every
  row. The hook then keeps the store's rank order: it skips the local
  re-rank (``:rerank_off:keyword``) and the score floor (``:floor_off:keyword``).

Dedupe and cap
  Hits already surfaced in this session are skipped; the seen-set is
  ``<cache dir>/<session_id>.json``, and its load-decide-save runs under a
  flock, waited for at most LOCK_WAIT_S (0.5 s). The rendered output is
  capped: a hit whose text does not fit is left out, is not marked as seen,
  and is not counted. Nothing is printed for zero hits.

Log
  One line per call appended to ``<cache dir>/recall.log``:
  ``<iso ts> event=<name> session=<id> hits=<n> chars=<n> ms=<n> <ok|fail:<reason>|skip:<reason>>``
  No query text and no memory body ever reach the log.

Environment (config file keys in design doc section 12.3)
  NOBLIVION_DATA_DIR            the data dir (store.json, token, cache)
  NOBLIVION_RECALL_MIN_SCORE    default 0.3, see above
  NOBLIVION_RECALL_TIMEOUT_S    default 2.0
  NOBLIVION_RECALL_CACHE_DIR    default <data dir>/cache
  NOBLIVION_RECALL_DISABLE      any non-empty value: exit 0, no call, no output
  NOBLIVION_RECALL_MEMORY_DIR   the memory folder of the local re-rank, the rule
                                rows and the session root. Unset: the memory
                                folder of the session's working dir,
                                ``~/.claude/projects/<slug of cwd>/memory``,
                                when it exists.
  NOBLIVION_RECALL_INDEX        any non-empty value: serve a RANKED INDEX instead
                                of hits (see "Ranked index" below)
  NOBLIVION_RECALL_INDEX_K      candidates the index asks for, default 35, 1..100
  NOBLIVION_RECALL_INDEX_MAX_CHARS  output cap for the index shape, default 7600
  NOBLIVION_RECALL_INDEX_ROW_DEDUPE  with the rule shape: a later index of one
                                session leaves out the rows it was already shown
  NOBLIVION_RECALL_INDEX_MIN_SCORE  a cosine, -1..1: an index row under it is
                                not shown. Skipped in keyword mode.
  NOBLIVION_RECALL_INDEX_CHECK_LINE  with the rule shape: one more header line
  NOBLIVION_RECALL_INDEX_DROP_NO_RULE  with the rule shape: a row with no rule
                                and no summary is not shown
  NOBLIVION_RECALL_PROJECT      the namespace to search, default ``claude_code``.
                                Lower-case letters, digits and ``_`` only. An
                                answer whose ``namespace`` names another
                                namespace is ``fail:namespace_mismatch``.

Ranked index (opt in with NOBLIVION_RECALL_INDEX)
  Instead of the text of up to five memories, the hook asks
  ``GET /api/memories/index`` for up to 35 candidates and prints one line
  each. The model reads the one it wants by calling the ``noblivion_recall``
  MCP tool with ``fetch_id=<n>`` (``GET /api/memories/fetch/{id}``). The index
  is NOT deduped against the session (a menu is re-ranked every turn) and no
  ``NOBLIVION_RECALL_MIN_SCORE`` floor is applied. Optional steps, each behind
  its own variable: P3 hygiene (drop index, topic and closed rows), P1h local
  re-rank (BM25 over the memory folder fused with the store's cosine order),
  F1 rule rows, F2 APPLY blocks, the shown-set and the trust factor.

Neutral rendering
  Memory text is untrusted. Each field is made neutral first (``_neutral``):
  NFKC; every whitespace character becomes one space; control, format,
  surrogate, private-use and unassigned characters and the variation
  selectors are removed; ``<`` and ``>`` become ``‹`` and ``›``, so a memory
  cannot close the tag Claude Code wraps hook output in. So memory text
  reaches the model only inside a rendered line under the header.

Full text on the hook path
  The hit path shows each memory's own text in the ``memory_text.render_hit``
  shape (``Rule:`` and ``Text:`` field lines, redacted and neutral), under
  ``FULL_OUTPUT_MAX_CHARS``. When ``memory_text.py`` cannot be loaded from
  this file's folder, the hook falls back to the one-line shape.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as _dt
import hashlib
import http.client
import importlib
import ipaddress
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PERSONA = "claude_code"  # the default namespace
PROJECT_ENV = "NOBLIVION_RECALL_PROJECT"
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESPONSE_MAX_BYTES = 1_048_576  # the store answers a few KB per hit; K_MAX is 20
# Name resolution has no timeout and cannot be cancelled, so a worker stuck in
# it outlives the deadline. This many may be in flight; past it a call is
# "busy" at once and starts no thread. See http_get_json().
WORKER_MAX = 4
_WORKERS = threading.BoundedSemaphore(WORKER_MAX)
LOCK_WAIT_S = 0.5  # at most this long for the session lock, then unlocked
LOCK_POLL_S = 0.01
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_MIN_SCORE = 0.3
DEFAULT_CACHE_DIR = ""  # "": <data dir>/cache (hook_config)
QUERY_MAX_CHARS = 300
BODY_MAX_CHARS = 200
TITLE_MAX_CHARS = 120  # a title is one line of the file, and can be long
ENTITY_MAX_CHARS = 32
OUTPUT_MAX_CHARS = 2000
FULL_OUTPUT_MAX_CHARS = 4000  # the full-text hit path (WI-3f)
# WI-16 (gain v3 c08 diagnosis, P/s1gain4/DIAG.md): the old note ended at "no need to open the memory
# files". When the 5 hits miss the task's memory (t01, t07, c08: 0 of 30), the model then skips its own
# memory-folder search: stock arm A3 searched the folder in 34 of 70 runs, the live stack L3 in 8 of 70
# (c08: 7 of 10 vs 0 of 10). The second sentence keeps that search open when no hit fits.
FULL_HEADER_NOTE = (
    "This is the memories' own text, so act on it; no need to open these files. "
    "A search can miss: if none of them fits the task, still search the memory folder."
)
K_PROMPT = 5
K_TOOL = 3
K_MAX = 20  # the most candidates one hook search asks for
MD_ONLY_OVERFETCH = 3  # candidates per wanted hit when mined rows are dropped
TOOL_INPUT_SNIPPET = 200
RECALL_TOOLS = frozenset({"Bash", "Edit", "Write"})
HEADER = "GROUNDED MEMORY (local memory store, namespace {persona}, {n} hits, {ms} ms)"

# ── the ranked index (R1) ─────────────────────────────────────
# A second, OPT-IN shape for the same hook: instead of the text of up to five
# memories, a ranked list of many candidate titles with one line of summary
# each, and the full text fetched on demand by id. Off unless
# NOBLIVION_RECALL_INDEX is set, so a live install is untouched by importing this.
INDEX_ENV = "NOBLIVION_RECALL_INDEX"
INDEX_K_ENV = "NOBLIVION_RECALL_INDEX_K"
INDEX_CAP_ENV = "NOBLIVION_RECALL_INDEX_MAX_CHARS"
INDEX_K_DEFAULT = 35  # R1 pre-registered recall@35
INDEX_K_MAX = 100
# The design note measured the corpus: a frontmatter description is 177
# characters on average, so 35 of them cost about 1,900 tokens — roughly 7,600
# characters. That is the note's own honest budget for this shape, and it is
# nearly four times the 2,000-character cap of the hit shape. The experiment
# pays it on purpose; the number is the thing being measured, so it is a
# constant here and an env override, never a silent default.
INDEX_OUTPUT_MAX_CHARS = 7600
INDEX_SUMMARY_MAX_CHARS = 200
# Fetch on demand returns a BODY, which the hit shape never did. It is bounded
# on three axes, because one memory file is 2,690 bytes on average and a hostile
# one can be anything: characters per line, lines, and total characters.
FETCH_LINE_MAX_CHARS = 400
FETCH_MAX_LINES = 200
FETCH_MAX_CHARS = 6000
LINE_CUT_MARK = " [cut]"  # a line that was shortened says so
BLOCK_CUT_MARK = "[... cut]"  # a block that lost whole lines says so
INDEX_HEADER = (
    "GROUNDED MEMORY INDEX (local memory store, namespace {persona}, {n} candidates ranked "
    "for this turn, {ms} ms). Titles only. To read one, call the "
    "noblivion_recall tool with fetch_id=<the id on that line>."
)

# ── Phase 4 (W4): the three arm E index changes ─────────
# One environment variable each, all off unless the variable is set, so arm D
# and a live install produce byte-identical output without them.
#
#   P2  NOBLIVION_RECALL_INDEX_QUERY_CHARS  how much of the prompt reaches the query
#   P3  NOBLIVION_RECALL_INDEX_HYGIENE      overfetch, then drop index and closed rows
#   P1h NOBLIVION_RECALL_INDEX_RERANK       re-rank the store's candidates locally
#
# P1h runs in the hook, over the local memory folder: the store keeps one
# ranking for every client, and the hook adds the sub-token BM25 of the files.
#
# P3 and P1h both decide from the memory's OWN frontmatter and body, not from the
# row the store sent, so both need the memory folder: NOBLIVION_RECALL_MEMORY_DIR,
# else the memory folder of the session's working dir (``memory_dir``). Never a
# search: a hook that walked out to another folder would rank one corpus against
# another.
INDEX_QUERY_CHARS_ENV = "NOBLIVION_RECALL_INDEX_QUERY_CHARS"
INDEX_HYGIENE_ENV = "NOBLIVION_RECALL_INDEX_HYGIENE"
INDEX_RERANK_ENV = "NOBLIVION_RECALL_INDEX_RERANK"
MEMORY_DIR_ENV = "NOBLIVION_RECALL_MEMORY_DIR"
# The query is a URL parameter, so its length is bounded HERE and not by the
# store's request line. Arm E asks for 2,000 characters.
INDEX_QUERY_CHARS_MAX = 8000
# P3 and P1h ask the store for this many candidates before either one drops or
# re-ranks anything, so that a dropped row does not cost the list its depth and a
# promoted row has somewhere to come from. It bounds the FETCH and never the
# render: index_k still decides how many rows are shown.
#
# 200 is the store route's own cap (top_k is clamped to 1..200), so it is the
# deepest list this hook can reach. In the reference measurement one target
# memory sat at rank 124, out of reach of any shallower re-rank, and a 200-row
# answer cost no measurable time over a 100-row one.
INDEX_CANDIDATE_TOP_K = 200
# P1h reproduces the offline replay exactly: BM25Okapi as rank_bm25 implements it
# (k1, b, epsilon), fused with the store's cosine order by reciprocal rank
# fusion at k=60 with one weight each, which is what retrieval_engine's joint_rrf
# does. Gate G-P1h measured these five numbers. Changing one of them invalidates
# that measurement, so they are constants and not settings.
RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
BM25_EPSILON = 0.25

# ── Phase 4 (W6): rule-first rows, rank-as-id fetch ────
# One variable, off unless set, so arm D and a live install render the arm D row
# byte for byte. F1 changes the ROW, F3 changes the FETCH, and they are one
# variable because a row that shows no rank is only safe to fetch from once the
# fetch also accepts a rank.
INDEX_RULE_ROWS_ENV = "NOBLIVION_RECALL_INDEX_RULE_ROWS"
# The row is "- [id 18993] <rule> (<name>)". The id leads, because it is the
# only thing on the row the model can act on, and because a gate can then read
# it as "the first integer on the row" with no parsing of memory text.
INDEX_ROW_PREFIX = "- [id "
# Measured on the W5 annotated corpus, 2026-09-29: 672 rule lines, mean 123.4
# characters, p95 152, max 160 (the annotator's own cap). 160 keeps every rule
# whole.
INDEX_RULE_MAX_CHARS = 160
# Measured on the same corpus: names are mean 45.6 characters, p95 61, max 110.
# The name is the least load-bearing field on the row (the rule carries the
# meaning, the id does the fetching), so it is the field that gives way to keep
# the row inside INDEX_ROW_MAX_CHARS with the rule intact.
INDEX_ROW_NAME_MAX_CHARS = 55
# Gate G-6a's bound for one row. Enforced HERE and not only measured, so no
# corpus can ever produce a longer row: the rule is allotted whatever is left of
# it after the id and the name, and cut at a word boundary.
INDEX_ROW_MAX_CHARS = 235
# W8: the prefix of a row with no id (arm ET's trigger leg only, see _render_rule).
INDEX_ROW_NO_ID = "- [no id] "
# The header of the rule shape. W6 fixes this wording. The tiers
# sentence is a promise about the APPLY block, which W7 renders and W6 does not,
# so it is emitted only when a block is actually there (k > 0). It names no
# range of ranks, because DP-6 can drop rank 1's block and keep rank 10's
# (review (a) #5), and no "whole" memory, because a fetch cuts a long one
# (review (b) #2: 109 of 661 files).
INDEX_RULE_HEADER = (
    "MEMORY RULES ranked for this task. {tiers}Follow a rule where it fits the "
    "task before your first command. noblivion_recall fetch_id=<id> returns the "
    "memory."
)
# WI-17. The header of a REDUCED rule index (rows were left out because this
# session already has them). It must say that the list is not the whole ranking
# and that the earlier rows still bind, or the model reads a short list as "only
# these rules fit".
INDEX_RULE_HEADER_REDUCED = (
    "MEMORY RULES new for this task. Rules shown earlier in this session still "
    "apply and are not repeated. {tiers}Follow a rule where it fits the task "
    "before your first command. noblivion_recall fetch_id=<id> returns the memory."
)
INDEX_RULE_HEADER_TIERS = "An indented APPLY line under a row says how to apply it. "
# F3. The rows the model was last shown, written by the hook and read by the MCP
# server, which is a separate process and knows no session id. One file per
# recall-cache folder, and the bench gives every run its own folder, so "the
# session's last index" and "this folder's last index" are the same thing. A
# live install with two sessions in one folder needs the session id in the
# path: WI-9 below (SESSION_DIR_NAME) adds it for a caller that has the id.
LAST_INDEX_NAME = "last_index.json"
# F3. A fetch_id at most this large, that is not an id in the last index, is
# read as a RANK. 40 is above the 30 to 32 rows an index shows and far below the
# 18,947 the smallest recorded memory id has, so the two spaces cannot overlap.
FETCH_RANK_MAX = 40

# ── Phase 4 (W8): one session shown-set (DP-6) ─────────
# One variable, off unless set. With it, the index, the MCP fetch and arm ET's
# Bash trigger leg keep one file of what the session was already given, so no
# memory's text is injected twice:
#   - the index drops the APPLY block of a memory that was fetched, or that
#     already carried a block this session. The ROW stays, because the index is
#     a menu (see ``_serve_index``), and a row without its block is one line;
#   - the trigger leg emits no row for a memory that carried a block or was
#     fetched (gate G-8b);
#   - a fetch is never refused, because the model asked for that id. It only
#     records what it returned.
# Same folder rule as LAST_INDEX_NAME: one file per recall-cache folder, and the
# bench gives each run its own. See ``load_shown_set`` for the session rule.
SHOWN_SET_ENV = "NOBLIVION_RECALL_SHOWN_SET"

# ── WI-17: do not repeat an index row inside one session ───────
# One variable, off unless set, so a run without it renders byte for byte what
# it rendered before. With it, the first index of a session is whole, and a
# later index of the SAME session shows only the rows whose memory was not an
# index row earlier in that session. The rank order of the rows that stay is
# unchanged. A memory whose APPLY block was shown (the shown-set's "apply"
# list) counts as shown.
#
# WHY: measured 2026-09-30 on the live install, the index is 30 rows on every
# prompt, and in one session of 6 prompts 82 of the 150 rows on prompts 2 to 6
# were rows the session had already been shown. Every repeated row is re-sent
# on every later API call of the session.
#
# This reverses, for the run that sets it, the "an index is a menu" rule in
# ``_serve_index``. The reduced index says so in its header
# (INDEX_RULE_HEADER_REDUCED), so the model does not read it as the whole list.
# It needs the rule shape (F1): the arm D row prints its rank, and a reduced
# arm D list would show ranks with holes. Set without F1 it is ignored and the
# log says so.
#
# The state is the session shown-set FILE (SHOWN_SET_NAME), not a second file:
# one more list in it, "row". That list is OPTIONAL: it is written only by a
# run that sets this variable, so a run without it writes the same file as
# before. It does not need NOBLIVION_RECALL_SHOWN_SET, which governs the APPLY
# block dedupe only.
INDEX_ROW_DEDUPE_ENV = "NOBLIVION_RECALL_INDEX_ROW_DEDUPE"

# ── WI-9: the shown-set and the last index, one file per session ─
SESSION_KEEP_DAYS_ENV = "NOBLIVION_RECALL_SESSION_KEEP_DAYS"
SESSION_KEEP_MAX_ENV = "NOBLIVION_RECALL_SESSION_KEEP_MAX"
SESSION_KEEP_DAYS_DEFAULT = 7.0
SESSION_KEEP_MAX_DEFAULT = 200

# ── WI-6: index shrink (a relevance floor, a check line) ───────
# Two variables, each off unless set, so a run without them renders byte for
# byte what it rendered before. The third part of the shrink, fewer rows, is
# NOBLIVION_RECALL_INDEX_K, which already exists.
#
# WHY: measured 2026-09-30 on the live install, the index is about 6,600
# characters on every prompt, and the prompt "continue" drew 29 rows (8,018
# characters) of rules with no relation to the task. The index had no floor: it
# always filled K rows.
#
# THE FLOOR IS ON THE DAEMON'S COSINE (the row's ``score`` field). It is the
# only number on a row that means the same thing on two prompts:
#   - the fused order of P1h is reciprocal rank fusion. Its value depends only
#     on a row's two RANKS, and some row is rank 1 on every prompt, so it cannot
#     say "nothing here is relevant";
#   - the local BM25 value grows with the length of the query (measured: 7 for
#     "continue", 377 for a 1,560-character task), so one floor cannot fit both.
# Measured 2026-09-30 (wi6/calibrate.py): the best row for "continue" has cosine
# 0.51; the 12 S1 trap memories sit between 0.537 and 0.714.
#
# The floor runs on the ranked list BEFORE the cut to K. So K is a cap: a row
# under the floor is left out, the next row at or above the floor takes its
# place, and the index has fewer than K rows only when fewer than K candidates
# pass. With none the hook prints nothing, like every other empty answer here.
# A row the store sent with NO score is kept: the floor can only judge a
# number, and a store that stops sending scores must not silently empty the
# index. The log note ``:floorNofM`` counts the N rows at or above the floor of
# the M ranked, and ``:unscoredU`` the rows kept without a score.
INDEX_MIN_SCORE_ENV = "NOBLIVION_RECALL_INDEX_MIN_SCORE"
# The verbalised check (LEVERS.md lever 5, PLAN-LEVERS.md WI-6). One line after
# the rule header, in the plan's own wording. Rule shape only: the arm D header
# names titles, not rules. Set without F1 it is ignored and the log says so.
INDEX_CHECK_LINE_ENV = "NOBLIVION_RECALL_INDEX_CHECK_LINE"
INDEX_CHECK_LINE = "Before a command that matches a shown trigger, write one line naming the rule."

# ── WI-18: a row with no rule text is not shown ────────────────
# One variable, off unless set, so a run without it renders byte for byte what
# it rendered before.
#
# WHY: measured 2026-09-30 on the live install, the store pool holds memories
# the store captured itself ("Tool error on <date> in Claude Code session
# <id>", "Operator correction on ...", "Independent review on ..."). They have
# no local file, so no ``rule:``, and the store sends no summary for them. The
# rule shape renders such a row as INDEX_ROW_NO_RULE and the first characters of
# its title: a row that states no rule. These rows pass the relevance floor and
# take places: 12 of the 30 rows on one live prompt, and 159 of the 675 rows of
# 40 replayed prompts (wi18/replay.py), every one of them a capture of those
# three kinds.
#
# WHAT IS DROPPED is exactly the row that would render INDEX_ROW_NO_RULE: no
# ``rule:`` from a local file AND no usable summary (``IndexLine.has_rule_text``
# asks the renderer itself, so the two cannot disagree). A row with a summary
# and no rule is KEPT: it renders its summary, which is a description the model
# can judge, and a local file without a ``rule:`` (a project or reference
# memory) is such a row.
#
# The drop runs on the ranked list, after the floor and BEFORE the cut to K, so
# a dropped row gives its place to the next row that has text. Like the floor
# it asks the store for INDEX_CANDIDATE_TOP_K candidates, and with no row left
# the hook prints nothing. It needs the rule shape (F1): the arm D row prints
# the title first and is a usable row without a rule. Set without F1 it is
# ignored and the log says so. The log note ``:norule_dropN`` counts the N rows
# dropped from the ranked list (not only from the first K).
INDEX_DROP_NO_RULE_ENV = "NOBLIVION_RECALL_INDEX_DROP_NO_RULE"
INDEX_ROW_NO_RULE = "no rule on this memory; fetch it to read it"

# ── Phase 4 (W7): the tiered APPLY block (F2) ──────────
# One variable, off unless set. With it, the top rows of the rule-shaped index
# carry the file's ``apply:`` text under the row, so the "how" reaches the model
# at prompt time with no fetch call. It needs the rule shape (F1): the block is a
# promise the rule header makes ("Rows 1-K carry how to apply"), and arm D's
# header makes no such promise. Set without F1, it is ignored and the log says so.
INDEX_APPLY_ENV = "NOBLIVION_RECALL_INDEX_APPLY"
# The tiers: (last rank, characters of apply text) in rank order. A row at rank r
# carries at most the characters of the first tier whose last rank is >= r; a row
# below the last tier carries none. K is the last tier's rank.
#
# SET BY GATE G-7a, not by design (W7). Gate G-7a renders the real block
# through this hook on the 13 E2-ordered lists with the W5 fields, 30 rows under
# a 9,000-character cap, and walks the PLAN's cut order. Measured 2026-09-29:
#   design K = 10, 1,200 / 400 / 250   mean 8,439  max 8,857   FAILS the 8,400 mean
#   K = 10, 900 / 300 / 200            mean 8,089  max 8,452   fits: CHOSEN
# The first setting that fits is the one below, and W11a freezes it. Changing it
# invalidates that measurement. The rank-1 tier does not bind on this corpus:
# W5 caps every ``apply:`` at 400 characters, so rank 1 always carries its whole
# field.
APPLY_TIERS: Tuple[Tuple[int, int], ...] = ((1, 900), (5, 300), (10, 200))
# The apply text sits UNDER its row, indented, never on a line of its own at the
# left margin. So no apply line can begin with "- [id", and a memory cannot
# forge a row the model would read as a ranked candidate. The id stays the first
# integer on every ROW, which gate G-6a measures.
APPLY_FIRST_PREFIX = "  APPLY: "
APPLY_NEXT_PREFIX = "  "
# A line that does not fit whole is cut at a word boundary only if at least this
# much of it survives; a shorter stub is noise, and the cut marker on the line
# before it says the text goes on.
APPLY_MIN_PIECE = 24
# A code fence is layout, not content. A tier cut can leave one open, and an open
# fence tells the model that every row after it is code. Fence lines are dropped.
_FENCE_LINE = re.compile(r"^\s*(```|~~~)[\w.+-]*\s*$")

# Every string a search answer may hold instead of hits
# (design doc section 4.2). Each one means "nothing to show".
EMPTY_SENTINELS = frozenset(
    {
        "No memories available.",
        "No valid embeddings for search.",
        "No valid vectors for search.",
        "Embedding generation failed.",  # L3_EMBED_FAILURE_SENTINEL
    }
)
ENTRY_SEPARATOR = "\n---\n"

# The indexed row content is "# <name>\n\n<description>\n\n[claude_code_md: <path>]\n\n<body>".
_MARKER_RE = re.compile(r"^\[claude_code_md:\s*(?P<path>[^\]]+)\]\s*$", re.M)
# Where an indexed row starts, for splitting only: the store's read redaction
# can rewrite part of a marker line (a path that matches an injection
# pattern), and such a row must still be its own entry, not the tail of the
# hit before it (G4 review). Its hit is not ``md`` (no full marker).
_MARKER_START_RE = re.compile(r"^\[claude_code_md:", re.M)
# Rows from transcript_miner.py carry no marker line. Each
# one opens with a fixed phrase (render_correction, render_tool_error,
# render_review), so a part that opens that way starts a new entry. Without
# this, a mined row was glued onto the entry before it: one memory then hashed
# to a different key per query, and the per-session dedupe showed it again.
_MINED_START_RE = re.compile(
    r"\A\s*(?:User correction|Operator correction|Tool error|Independent review) on "
    r"\d{4}-\d{2}-\d{2} in (?:Claude Code )?session \S+"
)


# ── siblings ────────────────────────────────────────────────────────────────

_SIBLING_MODULES: Dict[str, Any] = {}


def _sibling_module(name: str) -> Any:
    """A module from this file's folder, loaded once: ``corpus``,
    ``store_client``, ``hook_config``, and the optional ``trust_events``,
    ``trust_rank`` and ``label_rows``. Raises when it cannot be loaded; a
    caller of an optional module fails open."""
    mod = _SIBLING_MODULES.get(name)
    if mod is None:
        import importlib.util as _ilu

        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), f"{name}.py")
        spec = _ilu.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = _ilu.module_from_spec(spec)
        sys.modules.setdefault(name, mod)
        spec.loader.exec_module(mod)
        _SIBLING_MODULES[name] = mod
    return mod


def switch_on(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    """The switch ``name`` by the one rule of ``hook_config.switch``: 1, true,
    yes or on is on; 0, false, no, off or empty is off; unset gives
    ``default``."""
    return bool(_sibling_module("hook_config").switch(name, default, env))


def recall_disabled(env: Mapping[str, str]) -> bool:
    """``NOBLIVION_RECALL_DISABLE``: off unless on."""
    return switch_on(env, "NOBLIVION_RECALL_DISABLE")


def md_only_on(env: Mapping[str, str]) -> bool:
    """``NOBLIVION_RECALL_MD_ONLY`` (the MCP tool): off unless on."""
    return switch_on(env, "NOBLIVION_RECALL_MD_ONLY")


# The pure memory-folder helpers live in ``corpus.py`` (shared with the
# continuity hook); this hook re-exports them under their old names.
_CORPUS = _sibling_module("corpus")
SHOWN_SET_NAME = _CORPUS.SHOWN_SET_NAME
SHOWN_KINDS = _CORPUS.SHOWN_KINDS
SHOWN_ROW_KIND = _CORPUS.SHOWN_ROW_KIND
SHOWN_OPTIONAL_KINDS = _CORPUS.SHOWN_OPTIONAL_KINDS
SESSION_DIR_NAME = _CORPUS.SESSION_DIR_NAME
_CLOSED_STATUS_RE = _CORPUS._CLOSED_STATUS_RE
_DROPPED_KINDS = _CORPUS._DROPPED_KINDS
_FRONTMATTER_KEY_RE = _CORPUS._FRONTMATTER_KEY_RE
_SUB_TOKEN_SPLIT_RE = _CORPUS._SUB_TOKEN_SPLIT_RE
_DAEMON_TOKEN_SPLIT_RE = _CORPUS._DAEMON_TOKEN_SPLIT_RE
_KNOWN_KINDS = _CORPUS._KNOWN_KINDS
_INDEX_FILES = _CORPUS._INDEX_FILES
_SESSION_ID_RE = _CORPUS._SESSION_ID_RE
_HIDDEN_CATEGORIES = _CORPUS._HIDDEN_CATEGORIES
_VARIATION_SELECTORS = _CORPUS._VARIATION_SELECTORS
_ANGLE = _CORPUS._ANGLE
_BRACKET = _CORPUS._BRACKET
_NEUTRAL_READ_FACTOR = _CORPUS._NEUTRAL_READ_FACTOR
_hidden = _CORPUS._hidden
_neutral = _CORPUS._neutral
_HOST_PATH = _CORPUS._HOST_PATH
_HOST_PATH_MARK = _CORPUS._HOST_PATH_MARK
strip_host_paths = _CORPUS.strip_host_paths
session_state_file = _CORPUS.session_state_file
_newest_state_file = _CORPUS._newest_state_file
session_read_file = _CORPUS.session_read_file
_valid_sid = _CORPUS._valid_sid
_empty_shown = _CORPUS._empty_shown
load_shown_set = _CORPUS.load_shown_set
_unquote_frontmatter = _CORPUS._unquote_frontmatter
parse_frontmatter = _CORPUS.parse_frontmatter
_rule_field = _CORPUS._rule_field
read_rule_apply = _CORPUS.read_rule_apply
frontmatter_kind = _CORPUS.frontmatter_kind
tokens_daemon = _CORPUS.tokens_daemon
tokens_sub = _CORPUS.tokens_sub
build_memory_content = _CORPUS.build_memory_content
memory_name = _CORPUS.memory_name
load_memory_corpus = _CORPUS.load_memory_corpus
corpus_by_name = _CORPUS.corpus_by_name
MemoryFile = _CORPUS.MemoryFile
_STORE = _sibling_module("store_client")


# ── environment ─────────────────────────────────────────────────────────────

ROOT_ENV = "NOBLIVION_RECALL_ROOT"
SHARED_MEMORY_DIR_ENV = "NOBLIVION_MEMORY_DIR"
# Settings that may also come from the config file (design doc section
# 12.3): (env var the hook reads, other env names, config key). An env var
# that is set wins.
CONFIG_SETTINGS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("NOBLIVION_RECALL_TIMEOUT_S", (), "recall.timeout_s"),
    ("NOBLIVION_RECALL_MIN_SCORE", (), "recall.min_score"),
    ("NOBLIVION_RECALL_INDEX_K", (), "recall.index_k"),
    ("NOBLIVION_RECALL_PROJECT", ("NOBLIVION_PROJECT",), "namespace"),
)
# Config key ``recall.env``: an object of ``NOBLIVION_RECALL_*`` env defaults
# (the index flags, which are env-only). The installer ships the reference
# tuning in ``config/config.default.json``. An env var that is set wins.
CONFIG_ENV_KEY = "recall.env"
CONFIG_ENV_NAME = re.compile(r"NOBLIVION_RECALL_[A-Z0-9_]+")


def base_url(environ: Optional[Mapping[str, str]] = None) -> str:
    """``http://127.0.0.1:<port>`` from ``store.json``, or "" when there is
    none. Not proven: ``store_get`` proves the listener before it sends."""
    try:
        port, _token = _STORE.locate(environ)
    except _STORE.StoreUnavailable:
        return ""
    return _STORE.base_url(port)


def transport_reason(url: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why the token must NOT be sent to ``url``; None when it may be.

    ``https://`` is allowed: http.client verifies the certificate and the host
    name. Plain ``http://`` carries the token, the prompt and the memories in
    clear, so it is allowed only to a loopback IP literal. ``localhost`` is a
    name, and a name is never trusted here: the hosts file could resolve it to
    any address. Decided before any connection is opened; nothing is sent.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        return "bad_url"
    if parts.scheme not in ("http", "https") or not host:
        return "bad_url"
    if parts.scheme == "https":
        return None
    try:
        ip: Any = ipaddress.ip_address(host)
    except ValueError:
        return "plaintext_url"
    return None if ip.is_loopback else "plaintext_url"


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return default


def cache_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    """``NOBLIVION_RECALL_CACHE_DIR``, else ``<data dir>/cache``."""
    env = os.environ if environ is None else environ
    raw = env.get("NOBLIVION_RECALL_CACHE_DIR") or DEFAULT_CACHE_DIR
    if raw:
        return os.path.expanduser(raw)
    return str(_sibling_module("hook_config").cache_dir(env))


def _memory_dir_env(env: Mapping[str, str]) -> str:
    return (env.get(MEMORY_DIR_ENV) or env.get(SHARED_MEMORY_DIR_ENV) or "").strip()


def session_env(environ: Mapping[str, str], payload: Mapping[str, Any]) -> Dict[str, str]:
    """A copy of ``environ`` with the config settings, the session's memory
    folder and its root filled in.

    ``CONFIG_SETTINGS`` names the settings that may come from the config file
    when their env var is not set. ``recall.env`` in the config file gives
    defaults for the other ``NOBLIVION_RECALL_*`` env vars.

    The memory folder is ``NOBLIVION_RECALL_MEMORY_DIR`` (or
    ``NOBLIVION_MEMORY_DIR``), else the folder Claude Code keeps for the
    session's working dir, ``~/.claude/projects/<slug of cwd>/memory``, when
    it exists. The root (design doc section 5.1) is ``NOBLIVION_RECALL_ROOT``,
    else the name of the memory folder's parent, else the slug of ``cwd``.
    """
    env = dict(environ)
    cfg = _sibling_module("hook_config")
    for name, aliases, key in CONFIG_SETTINGS:
        if (env.get(name) or "").strip():
            continue
        value: Any = next((env[a] for a in aliases if (env.get(a) or "").strip()), None)
        if value is None:
            value = cfg.get(key, None, env)
        if (
            isinstance(value, (int, float, str))
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            env[name] = str(value).strip()
    defaults = cfg.get(CONFIG_ENV_KEY, None, env)
    for name, value in defaults.items() if isinstance(defaults, Mapping) else ():
        if (
            isinstance(name, str)
            and CONFIG_ENV_NAME.fullmatch(name)
            and not (env.get(name) or "").strip()
            and isinstance(value, (int, float, str))
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            env[name] = str(value).strip()
    cwd = payload.get("cwd") if isinstance(payload, Mapping) else None
    cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
    folder = _memory_dir_env(env)
    if not folder and cwd:
        candidate = os.path.join(
            str(Path.home()),
            ".claude",
            "projects",
            cfg.project_slug(cwd.rstrip("/") or "/"),
            "memory",
        )
        if os.path.isdir(candidate):
            env[MEMORY_DIR_ENV] = folder = candidate
    if not (env.get(ROOT_ENV) or "").strip():
        if folder:
            root = os.path.basename(os.path.dirname(os.path.normpath(os.path.expanduser(folder))))
        elif cwd:
            root = cfg.project_slug(cwd.rstrip("/") or "/")
        else:
            root = ""
        if root:
            env[ROOT_ENV] = root
    return env


def session_root(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The ``root`` to send: ``NOBLIVION_RECALL_ROOT``, else the parent name of
    the memory folder from the env, else None (the store then searches every
    root)."""
    env = os.environ if environ is None else environ
    raw = (env.get(ROOT_ENV) or "").strip()
    if raw:
        return raw
    folder = _memory_dir_env(env)
    if folder:
        return (
            os.path.basename(os.path.dirname(os.path.normpath(os.path.expanduser(folder)))) or None
        )
    return None


# ── HTTP ────────────────────────────────────────────────────────────────────


class RecallError(Exception):
    """Carries a short machine-readable reason for the log line."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_timeout(name: str, exc: Any) -> bool:
    return "timeout" in name.lower() or "timed out" in str(exc).lower()


def recall_project(environ: Optional[Mapping[str, str]] = None) -> str:
    """The namespace to search: ``NOBLIVION_RECALL_PROJECT`` or ``claude_code``.
    Raises RecallError("bad_project") for a value that is not a plain name."""
    env = os.environ if environ is None else environ
    raw = (env.get(PROJECT_ENV) or "").strip()
    if not raw:
        return PERSONA
    if not _PROJECT_RE.fullmatch(raw):
        raise RecallError("bad_project")
    return raw


def _qs(params: Mapping[str, Any], root: Optional[str]) -> str:
    items = dict(params)
    if root:
        items["root"] = root
    return urllib.parse.urlencode(items)


def search_url(
    base: str, query: str, k: int, project: str = PERSONA, root: Optional[str] = None
) -> str:
    qs = _qs({"q": query, "project": project, "top_k": int(k)}, root)
    return f"{base}/api/memories/search?{qs}"


def index_url(
    base: str,
    query: str,
    k: int,
    project: str = PERSONA,
    root: Optional[str] = None,
    include_mined: bool = False,
) -> str:
    """(R1) — the ranked-index route. A different path, not a flag
    on the old one: the two answers have different shapes. ``include_mined``
    is sent only when true (the MCP tool; design doc section 11.3)."""
    params: Dict[str, Any] = {"q": query, "project": project, "top_k": int(k)}
    if include_mined:
        params["include_mined"] = 1
    qs = _qs(params, root)
    return f"{base}/api/memories/index?{qs}"


def fetch_url(base: str, memory_id: Any, project: str = PERSONA, root: Optional[str] = None) -> str:
    """(R1) — fetch on demand, by id. The id is sent as a plain
    integer; anything else is refused here rather than at the store, so a
    crafted id cannot reach a URL path."""
    mid = int(memory_id)
    qs = _qs({"project": project}, root)
    return f"{base}/api/memories/fetch/{mid}?{qs}"


def store_get(build: Any, environ: Optional[Mapping[str, str]], timeout_s: float) -> Any:
    """GET one store route and decode its JSON, within ``timeout_s`` TOTAL.

    ``build(base)`` returns the URL for the store's base URL. The store is
    found through ``store.json`` and proved first (``store_client.connect``:
    the health nonce and its HMAC proof, with no token sent); only a proven
    listener gets ``Authorization: Bearer <token>``. A store that is down or
    unproven raises ``RecallError("store_down")``, ``("no_token")`` or
    ``("foreign_listener")`` and asks the launcher to start a store
    (``store_client.request_start``, detached, never waited for).
    """
    env = os.environ if environ is None else environ
    t0 = time.monotonic()
    probe_s = min(timeout_s, max(_STORE.PROBE_TIMEOUT_S, timeout_s / 2.0))
    try:
        base, token = _STORE.connect(
            env, lambda url, budget: http_get_json(url, "", budget), probe_s
        )
    except _STORE.StoreUnavailable as exc:
        _STORE.request_start(env)
        raise RecallError(exc.reason) from None
    url = build(base)
    reason = transport_reason(url, env)
    if reason:
        raise RecallError(reason)
    left = timeout_s - (time.monotonic() - t0)
    if left <= 0:
        raise RecallError("timeout")
    try:
        return http_get_json(url, token, left)
    except RecallError as exc:
        if not exc.reason.startswith("http_"):
            _STORE.forget(env)  # no answer: prove again next time
        raise


def _connection(url: str, timeout_s: float) -> Tuple[http.client.HTTPConnection, str]:
    """An unopened connection for ``url`` and the request target. ``https``
    uses http.client's default context, which verifies the certificate and
    the host name against the system CA store."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, parts.port, timeout=timeout_s)
    else:
        conn = http.client.HTTPConnection(host, parts.port, timeout=timeout_s)
    return conn, target


def _abort(sock: Optional[socket.socket]) -> None:
    """Shut the socket down from the caller's thread, so a worker blocked in
    recv() returns at once. Only shutdown() wakes a blocked recv(); close()
    alone would let a peer that drips bytes keep the worker alive. The socket
    object is held directly: http.client hands the socket to the response
    when the server closes after one request (HTTP/1.0, ``will_close``), and
    ``conn.sock`` is None from then on."""
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def http_get_json(url: str, key: str, timeout_s: float) -> Any:
    """GET ``url`` and decode JSON within ``timeout_s`` TOTAL, reading at most
    RESPONSE_MAX_BYTES of body.

    http.client's timeout is per socket operation, so a slow server could take
    connect + read > budget, and a peer that sends one byte per operation never
    times out at all. The request runs in a daemon thread; the caller waits at
    most ``timeout_s``, then shuts the socket down (see _abort) so the worker's
    read fails and the thread ends. http.client never follows a redirect, so a
    30x is a failure like any other non-200 and the bearer key cannot be
    forwarded to another origin.

    Name resolution (getaddrinfo) has no timeout and cannot be cancelled, so
    a worker stuck in it has no socket to shut down and outlives the deadline.
    At most WORKER_MAX workers are in flight: past that the call is ``busy``
    at once and no thread starts, so stuck workers cannot accumulate in the
    long-lived MCP server. Plain http is limited to IP literals by
    transport_reason(), so resolution only happens for an https host name.
    """
    box: Dict[str, Any] = {}

    def _worker() -> None:
        conn: Optional[http.client.HTTPConnection] = None
        resp: Optional[http.client.HTTPResponse] = None
        try:
            conn, target = _connection(url, timeout_s)
            headers = {"Accept": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            conn.connect()
            box["sock"] = conn.sock
            if box.get("aborted"):  # the deadline passed during connect()
                return
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(RESPONSE_MAX_BYTES + 1)
            if resp.status != 200:
                box["error"] = RecallError(f"http_{resp.status}")
            elif len(raw) > RESPONSE_MAX_BYTES:
                box["error"] = RecallError("too_large")
            else:
                try:
                    box["value"] = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    box["error"] = RecallError("bad_json")
        except OSError as exc:  # refused, no route, DNS, reset, socket timeout
            name = type(exc).__name__
            box["error"] = RecallError(
                "timeout" if _is_timeout(name, exc) else f"unreachable:{name}"
            )
        except Exception as exc:  # noqa: BLE001 — every failure is a reason string, never a traceback
            name = type(exc).__name__
            box["error"] = RecallError("timeout" if _is_timeout(name, exc) else f"error:{name}")
        finally:
            for closer in (resp, conn):
                if closer is not None:
                    with contextlib.suppress(OSError):
                        closer.close()
            _WORKERS.release()

    if not _WORKERS.acquire(blocking=False):
        raise RecallError("busy")
    t = threading.Thread(target=_worker, name="noblivion-recall-http", daemon=True)
    try:
        t.start()
    except RuntimeError:
        _WORKERS.release()
        raise RecallError("error:RuntimeError") from None
    t.join(timeout_s)
    if t.is_alive():
        box["aborted"] = True
        _abort(box.get("sock"))
        raise RecallError("timeout")
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise RecallError("no_response")
    return box["value"]


# ── hits ────────────────────────────────────────────────────────────────────


class Hit:
    """One memory. ``md`` is True only for a mirrored markdown memory file:
    the entry carries a ``[claude_code_md: <path>]`` marker line and does not
    open like a transcript-miner row. The hook injects only these."""

    __slots__ = ("entity", "title", "body", "score", "key", "md", "mid", "description", "text")

    def __init__(
        self,
        entity: str,
        title: str,
        body: str,
        score: Optional[float],
        key: str,
        md: bool = False,
        mid: str = "",
        description: str = "",
        text: str = "",
    ):
        self.entity = entity
        self.title = title
        self.body = body
        self.score = score
        self.key = key
        self.md = md
        # WI-3f: the memory id (the marker path's file name), the entry's
        # description line and the body with its line breaks, for the full
        # text shape (``render_full_capped``).
        self.mid = mid
        self.description = description
        self.text = text

    def render(self) -> str:
        score = "n/a" if self.score is None else f"{self.score:.2f}"
        # Square brackets would let an entity close the ``[...]`` early.
        entity = _neutral(self.entity, ENTITY_MAX_CHARS).translate(_BRACKET) or "memory"
        title = _neutral(self.title, TITLE_MAX_CHARS) or "memory"
        body = _neutral(self.body, BODY_MAX_CHARS)
        return f"- [{entity}] {title}: {body} (score {score})"


class IndexLine:
    """One row of the ranked index (R1): a candidate the model may
    choose to read, not a memory it has been handed.

    ``mid`` is the memory id, and it is the whole point of the row: without it
    the model is offered a title it cannot fetch. It is rendered as a plain
    integer, never as text from the memory, so nothing in a memory can forge it.
    """

    __slots__ = (
        "rank",
        "mid",
        "title",
        "summary",
        "score",
        "rule",
        "apply_block",
        "fused",
        "trust",
        "trials",
        "trust_prior",
        "keyword",
        "trust_ranking",
    )

    def __init__(
        self,
        rank: int,
        mid: int,
        title: str,
        summary: str,
        score: Optional[float],
        rule: str = "",
        apply_block: str = "",
        fused: Optional[float] = None,
        trust: Optional[float] = None,
        trials: Optional[int] = None,
        trust_prior: Optional[float] = None,
        keyword: bool = False,
        trust_ranking: str = "",
    ):
        self.rank = rank
        self.mid = mid
        self.title = title
        self.summary = summary
        self.score = score
        # (G). ``fused`` is the local fusion score ``rerank_index``
        # ordered by (None when it did not run); ``trust``, ``trials`` and
        # ``trust_prior`` are the store's per-row fields (contract C3), None
        # when the store sent none. Read by trust_rank only.
        self.fused = fused
        self.trust = trust
        self.trials = trials
        self.trust_prior = trust_prior
        # True when the store answered in keyword-only mode (design doc
        # section 8.4): ``score`` is None and the store order is the ranking.
        self.keyword = keyword
        # The store's ``trust.ranking`` (``shadow`` or ``on``; ``""`` when the
        # answer names none). In ``shadow`` the trust factor is computed and
        # logged, never applied (design doc section 8.6).
        self.trust_ranking = trust_ranking
        # W6 (F1) and W7 (F2). The two W5 fields of the LOCAL file this row
        # names, filled by ``annotate_index`` before rendering. Both are "" for
        # a row whose title names no local file and for the un-annotated corpus
        # arms A and D read, so those arms are not a different object here.
        # W6 renders ``rule``; W7 renders ``apply_block`` under the top rows.
        self.rule = rule
        self.apply_block = apply_block

    def render(self, rule_rows: bool = False) -> str:
        """The row, inert and with no host path in it.

        ``strip_host_paths`` runs AFTER ``_neutral``, because ``_neutral``
        normalises the text first (NFKC), and a full-width solidus is a "/" only
        once it has. In the arm D shape it runs after the truncation too, which
        can make a field a few characters longer than its cap; that shape is
        frozen. The rule shape folds the paths BEFORE its cut (``_render_rule``).

        ``rule_rows`` is W6's F1 shape: the id first, then the file's ``rule:``
        line, then the name. The RANK is not rendered, so that "the first
        integer on the row" is always the id; the order of the lines is the
        ranking. The SCORE is not rendered either: it is not the ordering key
        once P1h has fused two lists, so printing it would name a number that
        did not decide the row's place.
        """
        if rule_rows:
            return self._render_rule()
        score = "n/a" if self.score is None else f"{self.score:.2f}"
        title = strip_host_paths(_neutral(self.title, TITLE_MAX_CHARS)) or "memory"
        summary = strip_host_paths(_neutral(self.summary, INDEX_SUMMARY_MAX_CHARS))
        return f"- {int(self.rank)}. {title} — {summary} (id {int(self.mid)}, score {score})"

    def _render_rule(self) -> str:
        """W6 F1: ``- [id 18993] <rule> (<name>)``, at most INDEX_ROW_MAX_CHARS.

        The cap is enforced, not hoped for. The id and the name are laid out
        first and the rule is allotted what is left, so no rule, however long,
        can push the row past the bound gate G-6a measures.

        A row whose title names no local file, or whose file carries no
        ``rule:``, falls back to the store's own summary. It is still a real
        candidate the model may fetch, and a row with no text at all would be a
        worse answer than a row with a description on it. The two files of the
        frozen corpus with no frontmatter (MEMORY.md, MEMORY_ARCHIVE.md) are the
        only ones in it without a rule, and P3 drops both before this runs.
        """
        prefix, rule, suffix, limit = self._rule_parts()
        if not rule:
            rule = INDEX_ROW_NO_RULE[:limit]
        return f"{prefix}{rule}{suffix}"

    def has_rule_text(self) -> bool:
        """WI-18. False when the rule shape would render INDEX_ROW_NO_RULE for
        this row: no ``rule:`` and no usable summary. It asks ``_rule_parts``,
        the renderer's own code, so it cannot disagree with what is printed.
        Call it after ``annotate_index``: before that no row has its rule."""
        return bool(self._rule_parts()[1])

    def _rule_parts(self) -> Tuple[str, str, str, int]:
        """The rule row in parts: its prefix, its rule text ("" when the row
        has neither a rule nor a usable summary), its suffix, and the number of
        characters the rule text may take."""
        # Paths are folded BEFORE the cut, never after: the fold can lengthen
        # text ("~/a" is 3 characters, its marker 6), so a fold after the cut
        # could push the row past INDEX_ROW_MAX_CHARS (review (a) #3).
        name = (
            strip_host_paths(_neutral(self.title, 2 * INDEX_ROW_NAME_MAX_CHARS))[
                :INDEX_ROW_NAME_MAX_CHARS
            ].rstrip()
            or "memory"
        )
        # W8: arm ET's trigger leg renders its rows here too, and a memory the
        # index did not show has no id that leg can know. Id 0 renders
        # INDEX_ROW_NO_ID, never a false id, and never a bare "- " either: then
        # the rule would be the first text on the row, and a rule that begins
        # "[id 5]" would forge an id. An index row's id is always 1 or more.
        prefix = f"{INDEX_ROW_PREFIX}{int(self.mid)}] " if int(self.mid) > 0 else INDEX_ROW_NO_ID
        suffix = f" ({name})"
        room = INDEX_ROW_MAX_CHARS - len(prefix) - len(suffix)
        limit = max(1, min(INDEX_RULE_MAX_CHARS, room))
        text = self.rule or self.summary
        rule = _at_word_boundary(strip_host_paths(_neutral(text, 2 * limit)), limit)
        return prefix, rule, suffix, limit

    def render_apply(self, limit: int) -> str:
        """W7 F2: the lines that go UNDER this row, or "" for none.

        At most ``limit`` characters of apply text, not counting the indent and
        the label; see ``apply_excerpt`` for how it is cut. Every line is
        indented, so none of them can be read as a row.
        """
        if limit <= 0 or not self.apply_block:
            return ""
        lines = apply_excerpt(self.apply_block, limit)
        if not lines:
            return ""
        return "\n".join(
            [APPLY_FIRST_PREFIX + lines[0]] + [APPLY_NEXT_PREFIX + ln for ln in lines[1:]]
        )


def neutral_block(
    text: str,
    line_max: int = FETCH_LINE_MAX_CHARS,
    max_lines: int = FETCH_MAX_LINES,
    total_max: int = FETCH_MAX_CHARS,
) -> str:
    """A MULTI-LINE memory body, made as inert as a one-line hit.

    ``_neutral`` exists for one line and collapses every newline into a space,
    which would turn a fetched memory file into one unreadable paragraph. This
    runs the same rules per line and keeps the line breaks, so the text stays
    readable while ``<`` and ``>`` are still folded (a memory cannot close the
    block Claude Code wraps this in) and control, format and hidden characters
    are still removed.

    Bounded on three axes, because the body is the first thing on this path that
    is not already a short field: characters per line, number of lines, and total
    characters. A cut only ever drops text, so it cannot make the block less
    inert. A truncated block ends with a marker, so the reader knows it was cut.
    """
    raw_lines = str(text or "").splitlines()
    out: List[str] = []
    used = 0
    # Review round 2 on #2222: the markers used to be appended AFTER the limits
    # were applied, so each of the three bounds could be passed by the length of
    # its own marker. Room is reserved for them instead, so a hostile body cannot
    # exceed any of the three, marker included.
    body_line_max = max(1, line_max - len(LINE_CUT_MARK))
    body_max_lines = max(1, max_lines - 1)  # one line kept for BLOCK_CUT_MARK
    body_total_max = max(1, total_max - len(BLOCK_CUT_MARK) - 1)
    cut = len(raw_lines) > body_max_lines
    for raw in raw_lines[:body_max_lines]:
        line = _neutral(raw, body_line_max)
        # A cut that leaves no trace is the dangerous kind: one 50,000-character
        # line would come back as 400 characters that read like the whole memory.
        if len(line) >= body_line_max and len(_collapse(raw)) > len(line):
            line += LINE_CUT_MARK
        if used + len(line) + 1 > body_total_max:
            cut = True
            break
        out.append(line)
        used += len(line) + 1
    if cut:
        out.append(BLOCK_CUT_MARK)
    return "\n".join(out)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _at_word_boundary(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, at the last whole word.

    ``_neutral`` cuts mid-word, which is right for a title and wrong for a rule:
    a rule is an instruction, and half an instruction can say the opposite of
    the whole one ("never merge before" / "never merge"). A cut only ever drops
    text. The back-off is bounded: if the last space is in the first quarter of
    the allowance, the hard cut is kept, because one very long word must not
    shrink the row to nothing.
    """
    s = _collapse(text)
    if len(s) <= limit:
        return s
    head = s[:limit]
    space = head.rfind(" ")
    return head[:space].rstrip() if space > limit // 4 else head


def apply_limit(rank: int, tiers: Sequence[Tuple[int, int]]) -> int:
    """W7 F2: the characters of apply text a row at ``rank`` may carry, 0 = none.

    ``tiers`` is ``APPLY_TIERS``'s shape: (last rank, chars) in rank order.
    """
    for last, chars in tiers:
        if rank <= last:
            return max(0, int(chars))
    return 0


def apply_k(tiers: Sequence[Tuple[int, int]]) -> int:
    """K: the last rank that carries an apply block, 0 when there are no tiers."""
    return max((int(last) for last, _ in tiers), default=0)


def apply_excerpt(text: str, limit: int) -> List[str]:
    """W7 F2: the ``apply:`` text as inert lines, at most ``limit`` characters.

    The length is ``len("\\n".join(lines))``, cut marker included, so a tier
    is a hard bound. Each line goes through the same rules as a row: ``_neutral``
    (hidden characters out, angle brackets folded), then ``strip_host_paths``,
    because this text is injected at prompt time exactly like a row. Unlike
    ``neutral_block`` it keeps the line breaks AND cuts inside a line at a word
    boundary: ``neutral_block`` drops a whole line that does not fit, and the
    typical apply text is one or two long lines, so a tier would often keep
    nothing at all. Blank lines and code fences are dropped (see _FENCE_LINE).
    A cut text ends with LINE_CUT_MARK, so the reader knows to fetch the rest.
    """
    if limit <= 0:
        return []
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        if _FENCE_LINE.match(raw):
            continue
        line = strip_host_paths(_neutral(raw, FETCH_LINE_MAX_CHARS))
        if line:
            lines.append(line)
    if not lines:
        return []
    if len("\n".join(lines)) <= limit:
        return lines
    budget = limit - len(LINE_CUT_MARK)
    if budget < 1:
        return []
    out: List[str] = []
    used = 0
    for line in lines:
        sep = 1 if out else 0
        if used + sep + len(line) <= budget:
            out.append(line)
            used += sep + len(line)
            continue
        room = budget - used - sep
        if room >= APPLY_MIN_PIECE or not out:
            piece = _at_word_boundary(line, room)
            if piece:
                out.append(piece)
        break
    if not out:
        return []
    out[-1] += LINE_CUT_MARK
    return out


def entity_for_path(rel_path: str) -> str:
    """The indexer kind rule, from the path alone: index files, then
    the filename prefix, else ``reference``."""
    base = os.path.basename(rel_path.strip())
    if base in _INDEX_FILES:
        return "index"
    prefix = base.split("_", 1)[0].lower() if "_" in base else ""
    return prefix if prefix in _KNOWN_KINDS else "reference"


def hit_from_text(
    text: str,
    score: Optional[float] = None,
    entity: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[Hit]:
    """One entry of the search blob -> Hit, or None for a sentinel/blank."""
    text = (text or "").strip()
    if not text or text in EMPTY_SENTINELS:
        return None
    lines = text.split("\n")
    first = lines[0].strip()
    derived_title = first[2:].strip() if first.startswith("# ") else ""
    rest = "\n".join(lines[1:]) if derived_title else text
    m = _MARKER_RE.search(text)
    mid = description = ""
    if m:
        path = m.group("path").strip()
        entity = entity or entity_for_path(path)
        title = title or derived_title or os.path.splitext(os.path.basename(path))[0]
        mid = os.path.splitext(os.path.basename(path))[0]
        # The indexer layout: "# <name>", the description, the marker, the body.
        if derived_title:
            description = text[len(lines[0]) : m.start()].strip()
        rest = text[m.end() :]
    body = _collapse(rest)
    return Hit(
        entity=(entity or "memory").strip() or "memory",
        title=(title or derived_title or "memory").strip(),
        body=body,
        score=score,
        key=hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[
            :16
        ],  # a lone surrogate is data, not a crash
        md=m is not None and not _MINED_START_RE.match(text),
        mid=mid,
        description=description,
        text=rest.strip(),
    )


def _score_of(item: Dict[str, Any]) -> Optional[float]:
    for name in ("score", "similarity", "relevance"):
        v = item.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def split_entries(blob: str) -> List[str]:
    """The store's joined string -> its entries. Every indexed row carries
    exactly one ``[claude_code_md: <path>]`` marker line, and the body after
    it can hold ``---`` lines of its own (YAML front matter, horizontal
    rules). So a separator followed by marker-less text continues the entry
    before it instead of starting a fabricated one. A transcript-miner row has
    no marker but opens with a fixed phrase, so it starts its own entry.
    Blank parts are dropped."""
    entries: List[str] = []
    for part in blob.split(ENTRY_SEPARATOR):
        if not part.strip():
            continue
        if entries and not _MARKER_START_RE.search(part) and not _MINED_START_RE.match(part):
            entries[-1] += ENTRY_SEPARATOR + part
        else:
            entries.append(part)
    return entries


def parse_hits(payload: Any) -> List[Hit]:
    """``{"results": [...]}`` -> hits. Each element is either the joined string
    the store returns today, or a dict with ``content``/``text`` and optional
    ``score``/``entity``/``title`` for a future shaped response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RecallError("bad_shape")
    hits: List[Hit] = []
    # ``scores`` (design doc section 4.2): one score per entry of the joined
    # string, in entry order. Used only when it lines up with the entries.
    raw_scores = payload.get("scores")
    for item in payload["results"]:
        if isinstance(item, str):
            parts = split_entries(item)
            scores: List[Optional[float]] = [None] * len(parts)
            if (
                isinstance(raw_scores, list)
                and len(payload["results"]) == 1
                and len(raw_scores) == len(parts)
            ):
                scores = [_real_of(v) for v in raw_scores]
            for part, score in zip(parts, scores):
                h = hit_from_text(part, score=score)
                if h:
                    hits.append(h)
        elif isinstance(item, dict):
            text = item.get("content") or item.get("text") or ""
            if not isinstance(text, str):
                continue
            h = hit_from_text(
                text,
                score=_score_of(item),
                entity=item.get("entity") if isinstance(item.get("entity"), str) else None,
                title=(item.get("title") or item.get("name"))
                if isinstance(item.get("title") or item.get("name"), str)
                else None,
            )
            if h:
                hits.append(h)
    return hits


def parse_index(payload: Any) -> List[IndexLine]:
    """``{"results": [{rank, id, title, summary, score, ...}]}`` -> index lines.

    Stricter than ``parse_hits`` on purpose. A hit is text, and text that does
    not parse can still be shown. An index row is a PROMISE that id N can be
    fetched, so a row without a usable integer id is dropped rather than
    rendered: a line the model cannot act on is pure cost. ``rank`` falls back
    to the position, because the order the store sent is the ranking.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RecallError("bad_shape")
    lines: List[IndexLine] = []
    keyword = payload.get("mode") == "keyword"
    ranking = payload.get("trust_ranking")
    trust_ranking = ranking if ranking in ("shadow", "on") else ""
    for position, item in enumerate(payload["results"], start=1):
        if not isinstance(item, dict):
            continue
        # int() of an arbitrary JSON value is typed Unknown | None, and int(1.5)
        # would silently become 1. Parsing the value's own text form is one rule
        # for both: it refuses a float, a bool and None, and accepts the integer
        # and the string a transport may hand over.
        try:
            mid = int(str(item.get("id")).strip())
        except (TypeError, ValueError):
            continue
        # The MCP fetch path refuses an id below 1, so a row carrying one is a
        # candidate the model cannot act on. Review round 2 on #2222.
        if mid < 1:
            continue
        title = item.get("title")
        summary = item.get("summary")
        try:
            rank = int(str(item.get("rank")).strip())
        except (TypeError, ValueError):
            rank = position
        trials = item.get("trials")
        lines.append(
            IndexLine(
                rank=rank,
                mid=mid,
                title=title if isinstance(title, str) else "",
                summary=summary if isinstance(summary, str) else "",
                score=_score_of(item),
                trust=_real_of(item.get("trust")),
                trials=trials if isinstance(trials, int) and not isinstance(trials, bool) else None,
                trust_prior=_real_of(item.get("trust_prior")),
                keyword=keyword,
                trust_ranking=trust_ranking,
            )
        )
    return lines


def _real_of(value: Any) -> Optional[float]:
    """(G): a JSON number as a float; None for null, a bool or
    anything else, which the trust factor reads as "no trust" (factor 1.0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _header(n: int, ms: int, project: str = PERSONA) -> str:
    return HEADER.format(persona=project, n=n, ms=int(ms))


def render(
    hits: Sequence[Hit], ms: int, cap: Optional[int] = OUTPUT_MAX_CHARS, project: str = PERSONA
) -> str:
    """The context text: header, then one line per hit. See render_capped."""
    return render_capped(hits, ms, cap, project)[0]


def render_capped(
    hits: Sequence[Hit], ms: int, cap: Optional[int], project: str = PERSONA
) -> Tuple[str, List[Hit]]:
    """The context text and the hits it surfaces. Empty for zero hits. With
    ``cap`` the text never exceeds that many characters: hits are taken in
    rank order, one whose whole line no longer fits is left out, and the
    header counts the surfaced hits only. A hit that is not in the returned
    list was never shown, so the caller must not mark it as seen."""
    if not hits:
        return "", []
    if cap is None:
        return "\n".join([_header(len(hits), ms, project)] + [h.render() for h in hits]), list(hits)
    used = len(_header(len(hits), ms, project))  # the widest the header can be
    shown: List[Hit] = []
    lines: List[str] = []
    for h in hits:
        line = h.render()
        if used + 1 + len(line) <= cap:
            shown.append(h)
            lines.append(line)
            used += 1 + len(line)
    if not shown:
        return "", []
    return "\n".join([_header(len(shown), ms, project)] + lines), shown


_MEMORY_TEXT: List[Any] = []


def _memory_text():
    """The shared formatter ``memory_text`` from this file's
    folder (WI-3f), loaded once. Raises when it cannot be loaded."""
    if not _MEMORY_TEXT:
        import importlib.util as _ilu

        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "memory_text.py")
        spec = _ilu.spec_from_file_location("memory_text", path)
        if spec is None or spec.loader is None:
            raise ImportError("memory_text")
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MEMORY_TEXT.append(mod)
    return _MEMORY_TEXT[0]


def _formatter_loads() -> bool:
    """True when ``memory_text`` loads. Only a failure to load it
    selects the one-line shape; any other error goes to ``main``'s handler."""
    try:
        _memory_text()
        return True
    except Exception:  # noqa: BLE001
        return False


def _full_header(n: int, ms: int, project: str) -> str:
    return f"{_header(n, ms, project)}. {FULL_HEADER_NOTE}"


def render_full_capped(
    hits: Sequence[Hit],
    ms: int,
    query: str,
    cap: int = FULL_OUTPUT_MAX_CHARS,
    project: str = PERSONA,
) -> Tuple[str, List[Hit]]:
    """The hook's full-text shape (WI-3f): the context text and the hits it
    surfaces, never over ``cap``. Each hit is a memory in the shape of
    ``memory_text.render_hit`` (id head, then the description as
    the rule, then the body whole or its part on the prompt). When the hits
    break ``cap``, the later ones get a shorter text, then the description
    only; a hit that still does not fit is left out from the end, so it is
    not marked seen and not counted in the header. Raises when the shared
    formatter cannot be loaded (``run`` then uses ``render_capped``)."""
    if not hits:
        return "", []
    mt = _memory_text()
    mems = [
        {
            "id": h.mid or h.title,
            "rule": "",
            "apply": "",
            "description": h.description or h.title,
            "body": h.text or h.body,
        }
        for h in hits
    ]

    def one(
        mem: Mapping[str, Any], q: str, compact: bool = False, text_chars: int = mt.TEXT_CHARS
    ) -> str:
        return mt.render_hit(
            mem,
            q,
            compact,
            text_chars,
            subject="prompt",
            compact_needs_apply=False,
            head_fallback=True,
            neutral=_neutral,
        )

    for n in range(len(mems), 0, -1):
        text = mt.render(
            mems[:n], query, header=_full_header(n, ms, project), max_chars=cap, render_one=one
        )
        if len(text) <= cap:
            return text, list(hits[:n])
    return "", []


# ── session dedupe ──────────────────────────────────────────────────────────


def session_file(cache: str, session_id: Any) -> Optional[str]:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return os.path.join(cache, f"{session_id}.json")


def load_seen(path: Optional[str]) -> set:
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return set(k for k in data if isinstance(k, str)) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_seen(path: Optional[str], seen: set) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(seen), fh)
        os.replace(tmp, path)
    except OSError:
        pass


def prune_session_files(
    cache: str,
    keep_session: Any = None,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> int:
    """WI-9. Remove per-session files older than SESSION_KEEP_DAYS, then the
    oldest sessions above SESSION_KEEP_MAX. Never the files of
    ``keep_session``. Returns the number of files removed. Never raises."""
    env = os.environ if environ is None else environ
    days = _float_env(env, SESSION_KEEP_DAYS_ENV, SESSION_KEEP_DAYS_DEFAULT)
    try:
        keep_max = int(env.get(SESSION_KEEP_MAX_ENV) or SESSION_KEEP_MAX_DEFAULT)
    except (TypeError, ValueError):
        keep_max = SESSION_KEEP_MAX_DEFAULT
    if days <= 0:
        days = SESSION_KEEP_DAYS_DEFAULT
    keep_max = max(1, keep_max)
    folder = os.path.join(cache, SESSION_DIR_NAME)
    try:
        entries = os.listdir(folder)
    except OSError:
        return 0
    keep = _valid_sid(keep_session)
    suffixes = (
        "." + SHOWN_SET_NAME,
        "." + LAST_INDEX_NAME,
        "." + SHOWN_SET_NAME + ".lock",
        "." + LAST_INDEX_NAME + ".lock",
    )
    sessions: Dict[str, List[str]] = {}
    newest: Dict[str, float] = {}
    for entry in entries:
        sid = next((entry[: -len(sfx)] for sfx in suffixes if entry.endswith(sfx)), None)
        if not sid or sid == keep:
            continue
        path = os.path.join(folder, entry)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        sessions.setdefault(sid, []).append(path)
        if not entry.endswith(".lock"):
            newest[sid] = max(newest.get(sid, 0.0), mtime)
    cutoff = (time.time() if now is None else now) - days * 86400.0
    order = sorted(sessions, key=lambda s: newest.get(s, 0.0), reverse=True)
    room = keep_max - (1 if keep else 0)
    removed = 0
    for pos, sid in enumerate(order):
        if pos < room and newest.get(sid, 0.0) >= cutoff:
            continue
        for path in sessions[sid]:
            with contextlib.suppress(OSError):
                os.remove(path)
                removed += 1
    return removed


def last_index_file(cache: str, session_id: Any = None) -> str:
    """Where the hook leaves the rows it last showed. F3 (W6). WI-9:
    the session's own file when ``session_id`` is valid."""
    return session_state_file(cache, LAST_INDEX_NAME, session_id)


def save_last_index(
    cache: str,
    session_id: Any,
    shown: Sequence[IndexLine],
    candidates: Sequence[IndexLine] = (),
) -> None:
    """Write the rows the model was just shown, for the MCP fetch to read.

    Only the rows that SURVIVED the cap are written, because a rank the model
    can read off the block is a rank it was shown; a row dropped by the cap was
    never on its screen and must not be fetchable by number.

    ``ids`` maps the name of EVERY candidate the store returned to its id
    (review (a) #1). Arm ET's trigger leg reads it, so a row for a memory the
    index did not show still names a real id. It is never a source of ranks:
    ``load_last_index`` reads ``rows`` only. It costs no request, because the
    store already sent these ids with the candidates.

    Best effort, like every other write on this path: a hook that fails a prompt
    because a cache file could not be written is worse than a fetch that cannot
    resolve a rank.
    """
    if not shown:
        return
    path = last_index_file(cache, session_id)
    doc = {
        "session": session_id if isinstance(session_id, str) else None,
        "rows": [{"rank": int(ln.rank), "id": int(ln.mid), "name": ln.title} for ln in shown],
        "ids": _candidate_ids(candidates),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _candidate_ids(candidates: Sequence[IndexLine]) -> Dict[str, int]:
    """Name -> id of every candidate. The first (best ranked) wins a duplicate
    name, as in the rows; the frozen corpus has none (review (a) #8)."""
    ids: Dict[str, int] = {}
    for ln in candidates:
        if ln.title and int(ln.mid) > 0:
            ids.setdefault(ln.title, int(ln.mid))
    return ids


def load_last_ids(cache: str, session_id: Any) -> Dict[str, int]:
    """Name -> id for every candidate of this session's last index, or ``{}``.

    Read by arm ET's trigger leg only, for a row's id, never for a rank. The
    shown rows are laid over the candidate map, so a name the model saw carries
    the id it saw. Every entry is re-validated, as in ``load_last_index``, and
    another session's file reads as empty.
    """
    try:
        with open(session_read_file(cache, LAST_INDEX_NAME, session_id), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    sid = _valid_sid(session_id)
    if not isinstance(doc, dict) or not sid or _valid_sid(doc.get("session")) != sid:
        return {}
    out: Dict[str, int] = {}
    raw = doc.get("ids")
    for name, mid in raw.items() if isinstance(raw, dict) else ():
        try:
            value = int(str(mid).strip())
        except (TypeError, ValueError):
            continue
        if isinstance(name, str) and name and value > 0:
            out[name] = value
    for row in load_last_index(cache, session_id):
        if row["name"]:
            out[row["name"]] = row["id"]
    return out


def load_last_index(cache: str, session_id: Any = None) -> List[Dict[str, Any]]:
    """The rows of the last index, or ``[]``. Read by the MCP server (F3) and by
    the trigger leg's rows (W8), for their ids.

    Every field is re-validated: this file is written by another process, and a
    fetch that trusted a malformed row would resolve a rank to the wrong memory
    and answer as though nothing was wrong.

    ``session_id`` is passed by a caller that knows its session, which the MCP
    server does not: an index written for a different session is then ``[]``,
    so a row can never carry an id another session was shown.
    """
    try:
        with open(session_read_file(cache, LAST_INDEX_NAME, session_id), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    sid = _valid_sid(session_id)
    stored = _valid_sid(doc.get("session")) if isinstance(doc, dict) else None
    if sid and stored != sid:
        return []
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            rank = int(str(item.get("rank")).strip())
            mid = int(str(item.get("id")).strip())
        except (TypeError, ValueError):
            continue
        if rank < 1 or mid < 1:
            continue
        name = item.get("name")
        out.append({"rank": rank, "id": mid, "name": name if isinstance(name, str) else ""})
    return out


def shown_set_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    """DP-6. True when the index, the fetch and the trigger leg share the
    session's shown-set. Off unless the variable is set."""
    env = os.environ if environ is None else environ
    return switch_on(env, SHOWN_SET_ENV)


def shown_set_file(cache: str, session_id: Any = None) -> str:
    """Where the session's shown-set lives. DP-6 (W8). WI-9: the
    session's own file when ``session_id`` is valid."""
    return session_state_file(cache, SHOWN_SET_NAME, session_id)


def save_shown_set(cache: str, state: Mapping[str, Any], path: Optional[str] = None) -> bool:
    """Write the shown-set. True only when it reached the disk.

    The caller holds ``session_lock`` on the file it writes; ``record_shown``
    is the form for a caller that holds nothing. WI-9: ``path`` is the file the
    caller locked; without it the file of the state's own session.
    """
    path = path or shown_set_file(cache, state.get("session"))
    doc: Dict[str, Any] = {"session": _valid_sid(state.get("session"))}
    for kind in SHOWN_KINDS:
        doc[kind] = sorted({x for x in (state.get(kind) or ()) if isinstance(x, str) and x})
    # WI-17. Written only when the state carries it: another writer of the same
    # session (the fetch, the APPLY dedupe) then keeps the list, and a run that
    # never set the row dedupe never grows the file.
    for kind in SHOWN_OPTIONAL_KINDS:
        if kind in state:
            doc[kind] = sorted({x for x in (state.get(kind) or ()) if isinstance(x, str) and x})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_shown(cache: str, session_id: Any, kind: str, names: Iterable[str]) -> bool:
    """Add ``names`` to ``kind`` of the shown-set: load, add and save under
    ``session_lock`` on the set's own file, the lock the trigger hook uses for
    its own state.

    The index, the fetch and a trigger call of one session are separate
    processes and can overlap, so a write without the lock could drop another
    writer's names. True when the set reached the disk, or when there was
    nothing to add.
    """
    if kind not in SHOWN_KINDS and kind not in SHOWN_OPTIONAL_KINDS:
        raise ValueError(f"unknown shown-set kind {kind!r}")
    add = {n for n in names if isinstance(n, str) and n}
    if not add:
        return True
    # WI-9. A caller with a session id writes that session's file. A caller
    # with none (the MCP fetch) adds to the newest file, which in a folder with
    # one session is that session's file.
    if _valid_sid(session_id):
        path = shown_set_file(cache, session_id)
    else:
        path = _newest_state_file(cache, SHOWN_SET_NAME)
    with session_lock(path):
        state = load_shown_set(cache, session_id, None if _valid_sid(session_id) else path)
        state[kind] = sorted(set(state.get(kind) or ()) | add)
        return save_shown_set(cache, state, path)


def reset_shown_rows(cache: str, session_id: Any) -> bool:
    """WI-17: empty the session's shown-set, under the same lock as
    ``record_shown``. A compaction removes the earlier index rows, APPLY lines
    and fetched texts from the context, so the next index must be full again.
    Every list is emptied: the dedupe also skips a memory in ``apply``."""
    sid = _valid_sid(session_id)
    # WI-9. Only the caller's own file. With no session id that is the single
    # file, never the newest file of some session.
    path = shown_set_file(cache, sid)
    with session_lock(path):
        state = load_shown_set(
            cache,
            session_id,
            None if sid else session_read_file(cache, SHOWN_SET_NAME, None, False),
        )
        kinds = tuple(SHOWN_KINDS) + tuple(SHOWN_OPTIONAL_KINDS)
        if not any(state.get(k) for k in kinds):
            return True
        return save_shown_set(cache, _empty_shown(sid), path)


def main_reset_rows(stdin=None, environ: Optional[Dict[str, str]] = None) -> int:
    """``--reset-rows``: the SessionStart hook for source ``compact``. Reads the
    hook JSON for the session id. Always returns 0 and prints nothing."""
    with contextlib.suppress(BaseException):
        env = dict(os.environ if environ is None else environ)
        doc = json.loads((stdin or sys.stdin).read() or "{}")
        sid = doc.get("session_id") if isinstance(doc, dict) else None
        reset_shown_rows(cache_dir(env), sid)
    return 0


# ── log ─────────────────────────────────────────────────────────────────────

try:
    _fcntl: Any = importlib.import_module("fcntl")  # POSIX only
except ImportError:  # pragma: no cover — no flock on this platform
    _fcntl = None


@contextlib.contextmanager
def session_lock(path: Optional[str]):
    """Serialize load, decide and save of one session's seen-set across hook
    processes. Claude Code runs parallel tool calls, so two PreToolUse hooks
    of one session overlap; without the lock both read the same old seen-set,
    inject the same hits twice, and the last writer drops the other's
    additions. The lock is ``<session file>.lock`` and is held only around
    the dedupe, never around the HTTP call. The wait for it is bounded by
    LOCK_WAIT_S: a hook process paused while holding it cannot hold this one
    past its budget. With no session file, no flock on this platform, any
    OSError, or the bound reached, the hook proceeds unlocked (fail open);
    the worst case is one hit shown twice."""
    if not path or _fcntl is None:
        yield
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path + ".lock", "a")
    except OSError:
        yield
        return
    locked = False
    deadline = time.monotonic() + LOCK_WAIT_S
    try:
        while True:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # the holder hangs: proceed unlocked, fail open
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _fcntl.flock(fh, _fcntl.LOCK_UN)
        fh.close()


def log_line(
    cache: str, event: str, session_id: Any, hits: int, chars: int, ms: int, status: str
) -> None:
    """Append one line. Never raises. Carries no query text and no memory body."""
    sid = session_id if isinstance(session_id, str) and _SESSION_ID_RE.match(session_id) else "-"
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} event={event} session={sid} hits={hits} chars={chars} ms={ms} {status}\n"
    try:
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "recall.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


# ── the hook ────────────────────────────────────────────────────────────────


def query_for(payload: Dict[str, Any], max_chars: int = QUERY_MAX_CHARS) -> Optional[str]:
    """The search text for this hook call, or None when the event/tool is not
    one this hook recalls for. Trimmed to ``max_chars``.

    ``max_chars`` is a parameter and not the constant, for change P2 of
    W4: arm E sends 2,000 characters of the prompt to the index
    route. The default is the constant, so every caller that does not ask for
    more sends exactly what it sent before.
    """
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        q = payload.get("prompt")
        if not isinstance(q, str):
            q = payload.get("user_input")  # tolerate an older field name
    elif event == "PreToolUse":
        tool = payload.get("tool_name")
        if tool not in RECALL_TOOLS:
            return None
        ti = payload.get("tool_input")
        if not isinstance(ti, dict):
            return None
        if tool == "Bash":
            q = ti.get("command")
        else:
            snippet = ti.get("new_string") if tool == "Edit" else ti.get("content")
            snippet = snippet if isinstance(snippet, str) else ""
            fp = ti.get("file_path")
            q = f"{fp if isinstance(fp, str) else ''} {snippet[:TOOL_INPUT_SNIPPET]}"
    else:
        return None
    if not isinstance(q, str):
        return None
    q = _collapse(q)[: max(1, int(max_chars))]
    return q or None


def recall(
    query: str,
    k: int,
    environ: Optional[Mapping[str, str]] = None,
    md_only: bool = False,
    root: Optional[str] = None,
) -> List[Hit]:
    """Search the store: at most ``k`` hits, with any hit whose score is below
    ``NOBLIVION_RECALL_MIN_SCORE`` dropped (a scoreless hit passes). The hook and
    the MCP tool both come through here, so they share the threshold.

    ``md_only`` (the hook) keeps only mirrored markdown memory files
    (``Hit.md``) and drops every other row BEFORE the ``k`` cut, so a dropped
    row never takes a place. It asks the store for ``k * MD_ONLY_OVERFETCH``
    candidates (at most K_MAX), so up to ``k`` memory-file hits remain. The
    MCP tool leaves it False and gets every row, mined rows included.
    Raises RecallError with a short reason on failure."""
    env = os.environ if environ is None else environ
    timeout_s = _float_env(env, "NOBLIVION_RECALL_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    min_score = _float_env(env, "NOBLIVION_RECALL_MIN_SCORE", DEFAULT_MIN_SCORE)
    top_k = max(k, min(K_MAX, k * MD_ONLY_OVERFETCH)) if md_only else k
    project = recall_project(env)
    where = session_root(env) if root is None else root
    payload = store_get(lambda base: search_url(base, query, top_k, project, where), env, timeout_s)
    # An answer that names another namespace is refused: the header must never
    # name one namespace while the rows come from another.
    if isinstance(payload, dict) and "namespace" in payload and payload["namespace"] != project:
        raise RecallError("namespace_mismatch")
    hits = [
        h
        for h in parse_hits(payload)
        if (h.score is None or h.score >= min_score) and (h.md or not md_only)
    ]
    return hits[:k]


def index_mode(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when this hook should serve a ranked index instead of hits."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_ENV)


def index_k(environ: Optional[Mapping[str, str]] = None) -> int:
    """How many candidates the index asks for: ``NOBLIVION_RECALL_INDEX_K``,
    default 35, bounded to 1..100. An unreadable value is the default, because
    this hook never fails a prompt over a malformed setting."""
    env = os.environ if environ is None else environ
    raw = (env.get(INDEX_K_ENV) or "").strip()
    if not raw:
        return INDEX_K_DEFAULT
    try:
        return max(1, min(int(raw), INDEX_K_MAX))
    except ValueError:
        return INDEX_K_DEFAULT


def index_cap(environ: Optional[Mapping[str, str]] = None) -> int:
    """The output cap for the index shape, default INDEX_OUTPUT_MAX_CHARS."""
    env = os.environ if environ is None else environ
    raw = (env.get(INDEX_CAP_ENV) or "").strip()
    if not raw:
        return INDEX_OUTPUT_MAX_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return INDEX_OUTPUT_MAX_CHARS


def index_query_chars(environ: Optional[Mapping[str, str]] = None) -> int:
    """P2. How many characters of the prompt reach the index query.

    Default ``QUERY_MAX_CHARS``, which is exactly what arm D and the live install
    send, so an unset variable changes nothing. Arm E sets 2,000, because the
    measurement in ``design-precision.md`` is that the first 300 characters of a
    real prompt are the greeting and the setup, and the words that name the
    memory arrive later. An unreadable value is the default: this hook never
    fails a prompt over a malformed setting.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(INDEX_QUERY_CHARS_ENV) or "").strip()
    if not raw:
        return QUERY_MAX_CHARS
    try:
        return max(1, min(int(raw), INDEX_QUERY_CHARS_MAX))
    except ValueError:
        return QUERY_MAX_CHARS


def index_hygiene(environ: Optional[Mapping[str, str]] = None) -> bool:
    """P3. True when the index overfetches and then drops index and closed rows."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_HYGIENE_ENV)


def index_rerank(environ: Optional[Mapping[str, str]] = None) -> bool:
    """P1h. True when the store's candidates are re-ranked locally."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_RERANK_ENV)


def index_rule_rows(environ: Optional[Mapping[str, str]] = None) -> bool:
    """F1 with F3. True when the row leads with the memory's rule and a fetch
    also accepts a rank. Off unless the variable is set."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_RULE_ROWS_ENV)


def index_apply(environ: Optional[Mapping[str, str]] = None) -> bool:
    """F2. True when the top rows carry their apply text (APPLY_TIERS). Off
    unless the variable is set; honoured only together with F1."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_APPLY_ENV)


def index_row_dedupe(environ: Optional[Mapping[str, str]] = None) -> bool:
    """WI-17. True when a later index of a session leaves out the rows the
    session was already shown. Off unless the variable is set."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_ROW_DEDUPE_ENV)


def index_min_score(environ: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """WI-6. The relevance floor of the index, or None for no floor.

    ``NOBLIVION_RECALL_INDEX_MIN_SCORE`` is a cosine, so a value outside -1..1, a
    value that is not a finite number and an empty value are all "no floor":
    this hook never fails a prompt, or empties an index, over a malformed
    setting.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(INDEX_MIN_SCORE_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not -1.0 <= value <= 1.0:  # False for nan too
        return None
    return value


def index_check_line(environ: Optional[Mapping[str, str]] = None) -> bool:
    """WI-6. True when the rule header carries the verbalised check line. Off
    unless the variable is set; honoured only together with F1."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_CHECK_LINE_ENV)


def index_drop_no_rule(environ: Optional[Mapping[str, str]] = None) -> bool:
    """WI-18. True when a row with no rule text and no summary is left out of
    the index. Off unless the variable is set; honoured only together with F1."""
    env = os.environ if environ is None else environ
    return switch_on(env, INDEX_DROP_NO_RULE_ENV)


def drop_no_rule_index(lines: Sequence[IndexLine]) -> Tuple[List[IndexLine], int]:
    """WI-18. The rows that have rule text or a summary, in the order given,
    and how many rows were dropped. The rows must be annotated first (see
    INDEX_DROP_NO_RULE_ENV and ``IndexLine.has_rule_text``).
    """
    kept = [ln for ln in lines if ln.has_rule_text()]
    return kept, len(lines) - len(kept)


def floor_index(lines: Sequence[IndexLine], floor: float) -> Tuple[List[IndexLine], int]:
    """WI-6. The rows at or above the floor, in the order given, and how many of
    them carry no score. A row with no score is kept (see INDEX_MIN_SCORE_ENV).
    """
    kept = [ln for ln in lines if ln.score is None or ln.score >= floor]
    return kept, sum(1 for ln in kept if ln.score is None)


def memory_dir(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The memory folder P3, P1h and F1 read, or None when none is set.

    ``NOBLIVION_RECALL_MEMORY_DIR``, else ``NOBLIVION_MEMORY_DIR``. The hook
    entry (``run``) fills the first from the session's working dir through
    ``session_env``. Never a search. Without a folder the local steps fall back
    to the store's own answer, and the log line says which fallback ran.
    """
    env = os.environ if environ is None else environ
    raw = _memory_dir_env(env)
    if not raw:
        return None
    path = os.path.expanduser(raw)
    return path if os.path.isdir(path) else None


# ── the local corpus (P3 and P1h) ───────────────────────────────────────────


class BM25:
    """BM25Okapi over the whole corpus, as ``rank_bm25`` implements it.

    The store's lexical leg is this class with the store's tokenizer. P1h is
    this class with ``tokens_sub``. Two details matter and are easy to get wrong:

    - the inverse document frequency of a term in more than half the documents is
      NEGATIVE, and rank_bm25 replaces every negative value with
      ``epsilon * mean(idf)`` where the mean is taken over the values BEFORE the
      replacement, negatives included;
    - the document-frequency table covers the whole corpus even when only a few
      documents are scored, because a term that is rare in the corpus must stay
      rare when the candidate list is short.
    """

    __slots__ = ("idf", "tf", "dl", "avgdl", "n")

    def __init__(
        self,
        corpus_tokens: Sequence[Sequence[str]],
        k1: float = BM25_K1,
        b: float = BM25_B,
        epsilon: float = BM25_EPSILON,
    ):
        self.n = len(corpus_tokens)
        self.tf = [collections.Counter(d) for d in corpus_tokens]
        self.dl = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        df: collections.Counter = collections.Counter()
        for counts in self.tf:
            df.update(counts.keys())
        idf: Dict[str, float] = {}
        total = 0.0
        negative: List[str] = []
        for word, count in df.items():
            value = math.log(self.n - count + 0.5) - math.log(count + 0.5)
            idf[word] = value
            total += value
            if value < 0:
                negative.append(word)
        if idf:
            floor = epsilon * (total / len(idf))
            for word in negative:
                idf[word] = floor
        self.idf = idf

    def scores(
        self,
        query_tokens: Sequence[str],
        indexes: Optional[Sequence[int]] = None,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ) -> Dict[int, float]:
        """``{document index: score}`` for ``indexes`` (default every document).

        Only the documents asked for are scored. P1h scores the store's
        candidates, which is at most 100 of 674, and that is what keeps the
        added latency inside its budget.
        """
        wanted = range(self.n) if indexes is None else indexes
        out: Dict[int, float] = {}
        for i in wanted:
            tf = self.tf[i]
            dl = self.dl[i]
            total = 0.0
            for word in query_tokens:
                freq = tf.get(word, 0)
                if not freq:
                    continue
                weight = self.idf.get(word)
                if weight is None:
                    continue
                total += weight * freq * (k1 + 1) / (freq + k1 * (1 - b + b * dl / self.avgdl))
            out[i] = total
        return out


def _rrf_order(
    lists: Sequence[Sequence[int]],
    tiebreak: Sequence[int],
    scores: Optional[Dict[int, float]] = None,
) -> List[int]:
    """Reciprocal rank fusion of rank lists, as ``retrieval_engine``'s
    ``joint_rrf`` does it: ``1 / (k + rank)`` summed with one weight each, then
    ordered by the fused score and, on a tie, by the row's id ascending.

    The score is rounded to six decimals before the comparison, because the
    offline replay rounds there and a tie that the replay saw must be a tie here.
    ``scores``, when given, receives the fused score of every item (G).
    """
    fused: Dict[int, float] = collections.defaultdict(float)
    for ranked in lists:
        for rank, item in enumerate(ranked):
            fused[item] += 1.0 / (RRF_K + rank + 1)
    if scores is not None:
        scores.update(fused)
    return [i for i, _ in sorted(fused.items(), key=lambda kv: (-round(kv[1], 6), tiebreak[kv[0]]))]


def rerank_index(
    lines: Sequence[IndexLine],
    query: str,
    corpus: Sequence[MemoryFile],
    by_name: Mapping[str, MemoryFile],
    bm25: BM25,
) -> Tuple[List[IndexLine], int]:
    """P1h. Re-rank the store's candidates locally. Returns ``(rows, joined)``.

    Two rank lists are fused:

    - the store's COSINE order, rebuilt by sorting the rows by the ``score``
      field, which is a quantized cosine and NOT the order the store returned
      (that order is the store's own fusion). A row with no score is put last,
      because a candidate the store could not score cannot be trusted above one
      it could;
    - the local sub-token BM25 order over the same candidates.

    A row whose title does not name a file in the local corpus keeps its place in
    the cosine list and scores zero in the BM25 list. That is the honest handling:
    the row is a real candidate, and this hook has nothing to say about its text.
    ``joined`` counts the rows that did find their file, so a gate can see when
    the join is failing instead of reading a silently degraded ranking as a win.
    """
    index_of = {md.name: i for i, md in enumerate(corpus)}
    ids = [int(ln.mid) for ln in lines]
    # The cosine leg. -1.0 stands in for a missing score: the store's own score
    # field is a signed cosine, so -1.0 is below every real value.
    scored = [
        (-round(ln.score if ln.score is not None else -1.0, 6), ids[i], i)
        for i, ln in enumerate(lines)
    ]
    cosine_order = [i for _, _, i in sorted(scored)]
    # The BM25 leg, over the whole corpus's document frequencies.
    wanted = []
    joined = 0
    for i, ln in enumerate(lines):
        md = by_name.get(ln.title)
        if md is None:
            continue
        joined += 1
        wanted.append((i, index_of[md.name]))
    lexical = bm25.scores(tokens_sub(query), [j for _, j in wanted])
    row_score = {i: lexical.get(j, 0.0) for i, j in wanted}
    lists = [cosine_order]
    if row_score and max(row_score.values()) > 0:
        lists.append(
            [
                i
                for _, _, i in sorted(
                    (-round(row_score.get(i, 0.0), 6), ids[i], i) for i in range(len(lines))
                )
            ]
        )
    fused: Dict[int, float] = {}
    order = _rrf_order(lists, ids, fused)
    for i in order:
        lines[i].fused = fused[i]  # G: the score the trust factor scales
    return [lines[i] for i in order], joined


def hygiene_index(lines: Sequence[IndexLine], by_name: Mapping[str, MemoryFile]) -> List[IndexLine]:
    """P3. Drop the rows P3 drops, and keep every row it cannot classify.

    A row whose title names no local file is KEPT. The alternative, dropping what
    cannot be classified, would quietly shorten the list whenever the corpus and
    the pool disagree, and the size of that disagreement is exactly what gate
    G-0a exists to measure.
    """
    out: List[IndexLine] = []
    for ln in lines:
        md = by_name.get(ln.title)
        if md is not None and md.dropped:
            continue
        out.append(ln)
    return out


def renumber(lines: Sequence[IndexLine]) -> List[IndexLine]:
    """Ranks 1..n with no holes, after a drop or a re-rank.

    A rendered row shows its rank, and a list numbered 1, 2, 4 tells the model
    that row 3 was hidden from it. Called only when a change actually ran, so an
    arm that sets neither variable keeps the store's own numbers.
    """
    return [
        IndexLine(
            rank=i,
            mid=ln.mid,
            title=ln.title,
            summary=ln.summary,
            score=ln.score,
            rule=ln.rule,
            apply_block=ln.apply_block,
            fused=ln.fused,
            trust=ln.trust,
            trials=ln.trials,
            trust_prior=ln.trust_prior,
            keyword=ln.keyword,
        )
        for i, ln in enumerate(lines, start=1)
    ]


def annotate_index(lines: Sequence[IndexLine], by_name: Mapping[str, MemoryFile]) -> int:
    """W6 (F1) and W7 (F2). Put each row's LOCAL ``rule:`` and ``apply:`` on it.

    Returns the number of rows that found their file, which gate G-7c reads: a
    join that is quietly failing renders rows with summaries on them and would
    otherwise look exactly like a rule shape that works.

    The fields are set in place rather than copied into new rows, because
    ``rerank_index`` hands back the SAME objects in a new order and a copy here
    would have to be threaded through it. ``renumber`` carries both fields, so
    the order of the two steps does not matter.
    """
    joined = 0
    for ln in lines:
        md = by_name.get(ln.title)
        if md is None:
            continue
        joined += 1
        ln.rule = md.rule
        ln.apply_block = md.apply_block
    return joined


def recall_index(
    query: str,
    k: int,
    environ: Optional[Mapping[str, str]] = None,
    root: Optional[str] = None,
    include_mined: bool = False,
) -> List[IndexLine]:
    """(R1) — ask the store for a ranked index.

    Unlike ``recall()`` this applies NO ``NOBLIVION_RECALL_MIN_SCORE`` floor. The
    index exists to offer breadth, and the store already ranks the pool. A
    second floor here would silently cut the candidates.
    ``root`` defaults to ``session_root(environ)``. Each returned line carries
    the answer's ``mode`` (``hybrid`` or ``keyword``, design doc section 8.4).
    Raises RecallError with a short reason, like every other call on this path.
    """
    env = os.environ if environ is None else environ
    timeout_s = _float_env(env, "NOBLIVION_RECALL_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    project = recall_project(env)
    where = session_root(env) if root is None else root
    payload = store_get(
        lambda base: index_url(base, query, k, project, where, include_mined), env, timeout_s
    )
    if isinstance(payload, dict) and "namespace" in payload and payload["namespace"] != project:
        raise RecallError("namespace_mismatch")
    return parse_index(payload)[:k]


def fetch_memory_text(
    memory_id: Any, environ: Optional[Mapping[str, str]] = None, root: Optional[str] = None
) -> Dict[str, Any]:
    """(R1) — the full text of one memory, by id.

    The other half of the index: the MCP tool calls this when the model asks for
    a candidate by id. Returns the store's answer as a dict; the caller renders
    it. Raises RecallError on any transport or shape failure.
    """
    env = os.environ if environ is None else environ
    timeout_s = _float_env(env, "NOBLIVION_RECALL_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    project = recall_project(env)
    where = session_root(env) if root is None else root
    try:
        fetch_url("", memory_id, project)
    except (TypeError, ValueError):
        raise RecallError("bad_id") from None
    payload = store_get(lambda base: fetch_url(base, memory_id, project, where), env, timeout_s)
    if not isinstance(payload, dict):
        raise RecallError("bad_shape")
    if "namespace" in payload and payload["namespace"] != project:
        raise RecallError("namespace_mismatch")
    return payload


def index_header(
    n: int,
    ms: int,
    project: str,
    rule_rows: bool,
    tier_k: int = 0,
    reduced: bool = False,
    check_line: bool = False,
) -> str:
    """The header of whichever index shape is being rendered.

    The rule shape's header names no persona, no candidate count and no
    latency: W6 fixes its wording, and every word of it is an
    instruction to the model. The title shape's header is unchanged, so arm D's
    first line is byte-identical.

    ``check_line`` is WI-6's: the rule header gets INDEX_CHECK_LINE as a second
    line. The first line is unchanged, so a gate that looks for the rule header
    still finds it.
    """
    if not rule_rows:
        return INDEX_HEADER.format(persona=project, n=n, ms=int(ms))
    tiers = INDEX_RULE_HEADER_TIERS.format(k=int(tier_k)) if tier_k > 0 else ""
    # WI-17: ``reduced`` is True only when the row dedupe left rows out.
    template = INDEX_RULE_HEADER_REDUCED if reduced else INDEX_RULE_HEADER
    head = template.format(tiers=tiers)
    return f"{head}\n{INDEX_CHECK_LINE}" if check_line else head


def render_index_capped(
    lines: Sequence[IndexLine],
    ms: int,
    cap: Optional[int],
    project: str = PERSONA,
    rule_rows: bool = False,
    tiers: Sequence[Tuple[int, int]] = (),
    reduced: bool = False,
    check_line: bool = False,
) -> Tuple[str, List[IndexLine]]:
    """The index text and the rows it surfaces, capped like ``render_capped``.

    Rows are taken in rank order and one whose whole line no longer fits is left
    out, so a cap shortens the list from the BOTTOM: the candidates the ranker
    liked least are the ones lost.

    ``rule_rows`` selects W6's F1 shape for the header and for every row. It is
    False everywhere it is not passed, so arm D and a live install are untouched.

    ``tiers`` is W7's F2 block: a row within the tiers carries its apply lines,
    and the row and its lines are ONE unit for the cap, so a row is never shown
    with half its block. Only honoured with ``rule_rows``: the arm D header
    promises no block. The header names the last rank that actually carries one.

    ``reduced`` is WI-17's: the rows are what is left after the session's row
    dedupe, so the rule header says that earlier rows still apply.

    ``check_line`` is WI-6's second header line. Only honoured with
    ``rule_rows``, and counted against the cap like the rest of the header.
    """
    if not lines:
        return "", []
    if not rule_rows:
        tiers = ()
        check_line = False

    def unit(ln: IndexLine) -> str:
        head = ln.render(rule_rows)
        block = ln.render_apply(apply_limit(ln.rank, tiers)) if tiers else ""
        return f"{head}\n{block}" if block else head

    def carried(shown_rows: Sequence[IndexLine]) -> int:
        return max(
            (
                ln.rank
                for ln in shown_rows
                if tiers and ln.render_apply(apply_limit(ln.rank, tiers))
            ),
            default=0,
        )

    # Budget with the longest header this call can print, so the header that is
    # printed at the end (with the real tier count) is never longer than it.
    header = index_header(len(lines), ms, project, rule_rows, apply_k(tiers), reduced, check_line)
    if cap is None:
        header = index_header(
            len(lines), ms, project, rule_rows, carried(lines), reduced, check_line
        )
        return ("\n".join([header] + [unit(ln) for ln in lines]), list(lines))
    used = len(header)
    shown: List[IndexLine] = []
    rendered: List[str] = []
    for ln in lines:
        line = unit(ln)
        if used + 1 + len(line) > cap:
            # STOP, do not skip. Review round 2 on #2222: continuing would let a
            # shorter lower-ranked row in while a longer higher-ranked one is
            # dropped, so the list would have a hole and the docstring above
            # would be false. The rows returned are always a prefix of the rank
            # order, which is what "shortens from the BOTTOM" means.
            break
        shown.append(ln)
        rendered.append(line)
        used += 1 + len(line)
    if not shown:
        return "", []
    header = index_header(len(shown), ms, project, rule_rows, carried(shown), reduced, check_line)
    return "\n".join([header] + rendered), shown


def _apply_trust(
    lines: Sequence[IndexLine],
    environ: Mapping[str, str],
    by_name: Mapping[str, MemoryFile],
    cache: str,
    sid: Any,
    event: str,
) -> Tuple[List[IndexLine], str]:
    """(G): the trust factor, only with NOBLIVION_RECALL_INDEX_TRUST.
    Any error leaves the rows as they are and the log note names it."""
    if not (environ.get("NOBLIVION_RECALL_INDEX_TRUST") or "").strip():
        return list(lines), ""
    try:
        return _sibling_module("trust_rank").apply_index_trust(
            lines, environ, by_name, cache, sid, event
        )
    except Exception as exc:  # noqa: BLE001 - fail open; the log line names it
        return list(lines), f":trust_off:{type(exc).__name__}"


def _record_shown_events(
    cache: str, sid: Any, event: str, shown: Sequence[IndexLine], environ: Mapping[str, str]
) -> str:
    """(E): the rows just shown, as ``recall`` events (exposure).
    Only when ``trust_events.enabled`` (on by default, design doc section
    12.3), after the emit, and never raises. Returns a log note when events
    were asked for and not written."""
    try:
        mod = _sibling_module("trust_events")
        if not mod.enabled(environ):
            return ""
        if mod.record_index(cache, sid, event, [ln.mid for ln in shown], environ):
            return ""
        return ":trust_events_unwritable" if event in mod.RECALL_EVENTS and _valid_sid(sid) else ""
    except Exception as exc:  # noqa: BLE001 - fail open; the log line names it
        return f":trust_events_off:{type(exc).__name__}"


def emit(event: str, text: str, stdout) -> int:
    """Write the context in the shape Claude Code reads for ``event``.
    Returns the number of characters of context handed over."""
    if not text:
        return 0
    if event == "PreToolUse":
        doc = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
        stdout.write(json.dumps(doc) + "\n")
    else:
        stdout.write(text + "\n")
    stdout.flush()
    return len(text)


def _serve_index(
    query: str, event: str, sid: Any, cache: str, t0: float, stdout, environ: Dict[str, str]
) -> None:
    """The ranked-index shape of one hook call (R1).

    TWO DELIBERATE DIFFERENCES from the hit shape:

    - **No session dedupe.** A hit is dropped once it has been shown, because
      showing the same memory twice is waste. An index is a MENU, re-ranked
      every turn, and a candidate that was listed last turn can be the right
      candidate this turn. Suppressing it would leave the model choosing from a
      list with silent holes. The cost of that choice is exactly what the pilot
      measures: this list is re-read by every later API call in the session.
    - **No min-score floor** (see ``recall_index``).

    Everything else is the hit path's: fail open on any error, exit 0, one log
    line, and the same neutral rendering. The log's ``hits`` field counts the
    rows shown, so ``chars`` still totals the injected characters for B1.

    W4 adds two optional steps between the store's answer and the
    rendering, each behind its own variable and both off for arm D:

    - P3 (``NOBLIVION_RECALL_INDEX_HYGIENE``) asks for INDEX_CANDIDATE_TOP_K
      candidates and drops the index, topic and closed-status rows;
    - P1h (``NOBLIVION_RECALL_INDEX_RERANK``) re-ranks what is left with a local
      sub-token BM25 fused with the store's cosine order.

    W6 adds a third, which changes no ranking at all:

    - F1 (``NOBLIVION_RECALL_INDEX_RULE_ROWS``) renders each row as its id, the
      local file's ``rule:`` line and its name, and leaves the rows it showed in
      the recall cache so the MCP fetch can read a rank as an id (F3).

    W7 adds a fourth, on top of F1 and never without it:

    - F2 (``NOBLIVION_RECALL_INDEX_APPLY``) puts the file's ``apply:`` text under
      the top rows in the tiers of ``APPLY_TIERS``. The log note
      ``:applyNofK`` counts the rows that carried a block, of the K the tiers
      allow; ``:apply_off:no_rule_rows`` says F2 was asked for without F1.

    W8 adds DP-6 (``NOBLIVION_RECALL_SHOWN_SET``), honoured only with F2: a
    memory the session already fetched, or already gave a block, keeps its ROW
    and loses its block (``:shownN``, N blocks dropped), and the names that
    carried a block are added to the shown-set after the emit. The rows are
    never deduped, for the reason in the first point above.

    WI-17 adds the one exception to that first point, behind its own variable
    (``NOBLIVION_RECALL_INDEX_ROW_DEDUPE``) and only with F1: a row whose memory
    was an index row earlier in this session, or carried an APPLY block, is
    left out. The rows that stay keep their order AND their rank, so a row's
    APPLY tier does not change because a row above it was left out. The log
    note ``:rowsNofM`` counts the rows left of the M ranked. With none left the
    hook prints nothing, which is what every other empty answer on this path
    does, and the last index file is not rewritten: "the index you were last
    shown" is still the one on the model's screen. The rows that were shown are
    added to the shown-set's ``row`` list after the emit. It needs a valid
    session id: without one the set on disk could be any session's.

    WI-6 adds two more, each behind its own variable:

    - the relevance floor (``NOBLIVION_RECALL_INDEX_MIN_SCORE``): a row whose
      store cosine is under the floor is left out before the cut to k, so the
      index can have fewer than k rows or none. With none the hook prints
      nothing and the last index file is not rewritten. It needs no memory
      folder, so it also runs in the arm D shape, and it asks the store for
      INDEX_CANDIDATE_TOP_K candidates like P3 and P1h. Log note
      ``:floorNofM``;
    - the check line (``NOBLIVION_RECALL_INDEX_CHECK_LINE``), only with F1: the
      rule header gets INDEX_CHECK_LINE as a second line.

    WI-18 adds one more (``NOBLIVION_RECALL_INDEX_DROP_NO_RULE``), only with F1:
    a row that would render INDEX_ROW_NO_RULE (no ``rule:`` and no usable
    summary) is left out after the floor and before the cut to k, so the next
    row with text takes its place. The rows are annotated before the drop, and
    the store is asked for INDEX_CANDIDATE_TOP_K candidates like the floor.
    With no memory folder no row has a rule, so only the rows with a summary
    stay. Log note ``:norule_dropN``; ``:drop_no_rule_off:no_rule_rows`` says it
    was asked for without F1.

    All three read the run's memory folder, and all three fall back to the
    store's own list if they cannot. The log line's status carries the fallback,
    the join count and the annotated-row count, because an arm E run that
    silently served arm D's ranking, or rows with descriptions where rules were
    promised, would look like a result and be an artefact.
    """
    k = index_k(environ)
    hygiene = index_hygiene(environ)
    rerank = index_rerank(environ)
    rule_rows = index_rule_rows(environ)
    apply_on = index_apply(environ)
    floor = index_min_score(environ)
    drop_asked = index_drop_no_rule(environ)
    drop_on = drop_asked and rule_rows
    local = hygiene or rerank or rule_rows
    # P3. Ask for more than will be shown, so that a dropped row costs the list
    # nothing: INDEX_CANDIDATE_TOP_K rows, not the shown k, and the route bounds
    # it again.
    # F1 alone does NOT overfetch: it changes the text of a row and never its
    # place, so a rule-rows-only run must stay comparable to arm D's ranking.
    # WI-6: the floor overfetches too. Without the deeper list a row under the
    # floor would have no row to give its place to, and k would not be a cap.
    # WI-18: the no-rule drop overfetches for the same reason.
    ask = (
        max(k, INDEX_CANDIDATE_TOP_K) if (hygiene or rerank or floor is not None or drop_on) else k
    )
    try:
        lines = recall_index(query, ask, environ)
        candidates = list(lines)
    except RecallError as exc:
        log_line(cache, event, sid, 0, 0, int((time.monotonic() - t0) * 1000), f"fail:{exc.reason}")
        return
    # P3 and P1h. Both read the run's memory folder. Either one failing leaves
    # the store's own list, and the log line NAMES the fallback: an arm E run
    # that quietly served arm D's ranking would be a silent validity failure,
    # which is the same failure the 2.0 s fail-open timeout can cause.
    note = ""
    keyword = any(ln.keyword for ln in lines)
    by_name: Mapping[str, MemoryFile] = {}
    if local:
        folder = memory_dir(environ)
        if folder is None:
            note = ":local_off:no_memory_dir"
        else:
            try:
                corpus = load_memory_corpus(folder)
                by_name = corpus_by_name(corpus)
                if hygiene:
                    lines = hygiene_index(lines, by_name)
                if rerank and keyword:
                    # Design doc section 8.4: the store ranked by BM25 alone
                    # and sent no cosine, so its order is kept.
                    note = ":rerank_off:keyword"
                elif rerank:
                    bm25 = BM25([md.tokens for md in corpus])
                    lines, joined = rerank_index(lines, query, corpus, by_name, bm25)
                    note = f":joined{joined}of{len(lines)}"
            except (OSError, ValueError, ZeroDivisionError) as exc:
                note = f":local_off:{type(exc).__name__}"
                by_name = {}
    # (G). After the re-rank and before the floor and the cut to k.
    lines, trust_note = _apply_trust(lines, environ, by_name, cache, sid, event)
    note += trust_note
    if floor is not None and keyword:
        # Design doc section 8.4: no cosine, so no floor can judge a row.
        note += ":floor_off:keyword"
    elif floor is not None:
        # WI-6. On the ranked list and before the cut to k, so k is a cap and a
        # row under the floor gives its place to the next one that passes.
        ranked = len(lines)
        lines, unscored = floor_index(lines, floor)
        note += f":floor{len(lines)}of{ranked}"
        if unscored:
            note += f":unscored{unscored}"
    if drop_on:
        # WI-18. After the floor and before the cut to k, so a row with no rule
        # text gives its place to the next row that has some. Annotated here,
        # on the whole ranked list, because the drop reads the rule; the
        # annotation after the cut then only counts.
        annotate_index(lines, by_name)
        lines, no_rule = drop_no_rule_index(lines)
        note += f":norule_drop{no_rule}"
    elif drop_asked:
        note += ":drop_no_rule_off:no_rule_rows"
    if local or floor is not None:
        # Renumbered after a drop or a re-rank, and after the floor: a printed
        # rank, and the APPLY tier that belongs to a rank, follow the rows that
        # are shown.
        lines = renumber(lines[:k])
    if local:
        if rule_rows:
            # F1. After the cut to k, because only these rows are rendered and
            # the corpus lookup is per row. A row that found no file keeps the
            # store's summary and is counted here, not hidden.
            note += f":ruled{annotate_index(lines, by_name)}of{len(lines)}"
    tiers: Tuple[Tuple[int, int], ...] = ()
    if apply_on and not rule_rows:
        note += ":apply_off:no_rule_rows"
    elif apply_on:
        tiers = tuple(APPLY_TIERS)
    # WI-17. Before DP-6, so DP-6 counts only blocks of rows that will be shown.
    row_on = False
    reduced = False
    if index_row_dedupe(environ):
        if not rule_rows:
            note += ":row_dedupe_off:no_rule_rows"
        elif not _valid_sid(sid):
            note += ":row_dedupe_off:no_session"
        else:
            row_on = True
            had = load_shown_set(cache, sid)
            seen = set(had.get(SHOWN_ROW_KIND) or ()) | set(had["apply"])
            fresh = [ln for ln in lines if not (ln.title and ln.title in seen)]
            note += f":rows{len(fresh)}of{len(lines)}"
            reduced = len(fresh) < len(lines)
            # NOT renumbered: a tier belongs to a rank (see DP-6 below), so a
            # row that was rank 14 carries no block because 13 rows left.
            lines = fresh
    shown_on = bool(tiers) and shown_set_on(environ)
    if shown_on:
        # DP-6 (W8). The block's text is already in the conversation, so it is
        # not sent again; the row stays. The block is NOT handed to the next row:
        # a tier belongs to a rank, so no other row's block changes.
        given = load_shown_set(cache, sid)
        done = set(given["apply"]) | set(given["fetched"])
        dropped = 0
        for ln in lines:
            # Only a rank inside the tiers carries a block, so only there can
            # the dedupe drop one (review (a) #4).
            if ln.apply_block and ln.title in done and apply_limit(ln.rank, tiers) > 0:
                ln.apply_block = ""
                dropped += 1
        note += f":shown{dropped}"
    check_on = False
    if index_check_line(environ):
        if rule_rows:
            check_on = True
        else:
            note += ":check_line_off:no_rule_rows"
    ms = int((time.monotonic() - t0) * 1000)
    text, shown = render_index_capped(
        lines, ms, index_cap(environ), recall_project(environ), rule_rows, tiers, reduced, check_on
    )
    carried: List[str] = []
    if tiers:
        # Counted from what was SHOWN: a row the cap dropped carried nothing.
        carried = [ln.title for ln in shown if ln.render_apply(apply_limit(ln.rank, tiers))]
        note += f":apply{len(carried)}of{apply_k(tiers)}"
    chars = emit(event, text, stdout)
    if rule_rows:
        # F3. The rows the model can now see by number. Written AFTER the emit,
        # so a slow disk cannot delay the prompt, and before the log line, so a
        # run's log and its last index are always from the same turn.
        # WI-17: the MCP fetch reads a small number as the POSITION of a row in
        # the index the model was last shown. A reduced index keeps the old
        # ranks for its tiers, so the file gets the positions 1..n of the rows
        # as printed. With nothing shown the file is left as it is.
        save_last_index(cache, sid, renumber(shown) if row_on else shown, candidates)
    if (
        row_on
        and chars
        and not record_shown(cache, sid, SHOWN_ROW_KIND, [ln.title for ln in shown])
    ):
        # The rows are out and unrecorded, so a later turn repeats them.
        note += ":rows_unwritable"
    if shown_on and chars and not record_shown(cache, sid, "apply", carried):
        # The blocks are out and unrecorded: a later turn or trigger may repeat
        # them. Said in the log, because the dedupe it defeats is the claim.
        note += ":shown_unwritable"
    if chars:
        note += _record_shown_events(cache, sid, event, shown, environ)  # (E)
    if _valid_sid(sid):
        # WI-9. After the emit, so it cannot delay the prompt. No log note: the
        # status of a call must not depend on what other sessions left behind.
        with contextlib.suppress(Exception):
            prune_session_files(cache, sid, environ)
    log_line(cache, event, sid, len(shown), chars, ms, "ok" + note)


def run(stdin_text: str, stdout, environ: Dict[str, str]) -> None:
    t0 = time.monotonic()
    cache = cache_dir(environ)
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        log_line(cache, "-", None, 0, 0, 0, "fail:bad_stdin")
        return
    if not isinstance(payload, dict):
        log_line(cache, "-", None, 0, 0, 0, "fail:bad_stdin")
        return
    event = str(payload.get("hook_event_name") or "-")
    sid = payload.get("session_id")
    environ = session_env(environ, payload)
    if recall_disabled(environ):
        log_line(cache, event, sid, 0, 0, 0, "skip:disabled")
        return
    # P2. The index path may take more of the prompt than the hit path. The hit
    # path is untouched: index_query_chars defaults to QUERY_MAX_CHARS anyway,
    # but the conditional says so, so no future default can leak into it.
    serving_index = index_mode(environ)
    cut = index_query_chars(environ) if serving_index else QUERY_MAX_CHARS
    query = query_for(payload, cut)
    if query is None:
        if event == "UserPromptSubmit":
            reason = "empty_query"
        elif event == "PreToolUse":
            reason = "tool" if payload.get("tool_name") not in RECALL_TOOLS else "empty_query"
        else:
            reason = "event"
        log_line(cache, event, sid, 0, 0, 0, f"skip:{reason}")
        return
    if serving_index:
        _serve_index(query, event, sid, cache, t0, stdout, environ)
        return
    k = K_PROMPT if event == "UserPromptSubmit" else K_TOOL
    try:
        hits = recall(query, k, environ, md_only=True)  # the automatic path: memory files only
    except RecallError as exc:
        log_line(cache, event, sid, 0, 0, int((time.monotonic() - t0) * 1000), f"fail:{exc.reason}")
        return
    ms = int((time.monotonic() - t0) * 1000)
    sfile = session_file(cache, sid)
    with session_lock(sfile):
        seen = load_seen(sfile)
        fresh = [h for h in hits if h.key not in seen]
        if _formatter_loads():
            text, shown = render_full_capped(
                fresh, ms, query, FULL_OUTPUT_MAX_CHARS, recall_project(environ)
            )
        else:  # no shared formatter: the one-line shape
            text, shown = render_capped(fresh, ms, OUTPUT_MAX_CHARS, recall_project(environ))
        chars = emit(event, text, stdout)
        if shown:
            save_seen(sfile, seen | {h.key for h in shown})
    log_line(cache, event, sid, len(shown), chars, ms, "ok")


def label_rows(stdin_text: str, stdout, environ: Dict[str, str]) -> None:
    """: the subject label rows of a ``UserPromptSubmit`` prompt
    (``label_rows.prompt_leg``, from this file's folder), after
    this hook's own output. Off unless ``NOBLIVION_RECALL_LABELS`` is set, so a
    run without it prints what it printed before. Local only, no store call.
    Fails open: a missing module or any error adds nothing."""
    if not switch_on(environ, "NOBLIVION_RECALL_LABELS"):
        return
    try:
        _sibling_module("label_rows").prompt_leg(stdin_text, stdout, environ)
    except Exception:  # noqa: BLE001 - label rows fail open
        return


def main(stdin=None, stdout=None, environ: Optional[Dict[str, str]] = None) -> int:
    """Always returns 0. Nothing here may block a prompt or a tool. An
    exception ``run`` did not expect still leaves the call's one log line,
    ``fail:internal:<type>``: the type only, never the message, which could
    carry a secret."""
    env: Dict[str, str] = {}
    try:
        env = dict(os.environ if environ is None else environ)
        text = (stdin or sys.stdin).read()
        run(text, stdout or sys.stdout, env)
        label_rows(text, stdout or sys.stdout, env)
    except BaseException as exc:  # noqa: BLE001 — fail open: no stdout, exit 0, whatever happened
        with contextlib.suppress(BaseException):
            log_line(cache_dir(env), "-", None, 0, 0, 0, f"fail:internal:{type(exc).__name__}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main_reset_rows() if "--reset-rows" in sys.argv[1:] else main())
