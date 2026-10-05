#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE reader, writer and checker of the feedback-memory rule fields.

Work item WI-1 ("rule format"). Every feedback memory
carries, LAST in its front matter and in this fixed order:

  rule:            one imperative sentence, one plain line (W5 format)
  apply:           one line holding a double-quoted JSON string (W5 format)
  scope:           tool | stop | file | always
  triggers:        [item, item, ...]  one-line flow list, omitted when empty
  violates:        a JSON string: a Python regex, re.search over the raw Bash
                   command text (optional)
  example_repeat:  a JSON string: a command that breaks the rule (only with violates)
  example_ok:      a JSON string: the compliant command for the same intent (only
                   with violates)
  complies:        a JSON string: a Python regex, re.search over the raw Bash
                   command, that matches a run of the rule's own command (the
                   apply form or its check), optional, only with violates
                   (WI-3d). The guard hook accepts a ``# guard-ok:`` override
                   of this memory only after such a run failed in the session.

Why this serialization. Three hand-rolled single-line parsers read these files:
the reference memory mirror (``parse_frontmatter``),
``recall_hook.parse_frontmatter`` (and ``read_rule_apply``) and
the reference trigger coverage tool (``declared_triggers``). Every new key is ONE
top-level ``key: value`` line, so the two mirror-style parsers read it as one
more string key and never as a sub-key or a nested block. ``triggers:`` is the
one-line flow form, which ``declared_triggers`` splits on commas and strips of
quotes; so an item may hold no comma, no quote and no bracket, and the writer
refuses one that does. An empty list is NOT written: a declared ``triggers:``
REPLACES the derived triggers of the trigger hook, so ``triggers: []`` would
silence a file the derivation fires on today.

A plain trigger item is a command prefix with the meaning
the reference trigger hook (``matches``) gives it: a command segment equals the
item or starts with the item plus a space. Typed items are ``glob:<path glob>``,
``tool:<ToolName>`` and ``phrase:<words in a prompt>``; ``matches`` never fires
on them because no command segment starts with ``glob:``.

Project memories (``project_*.md``) carry two more fields (the
briefing of the continuity hook filters on them):

  status:   open | closed | parked
  project:  a lower-case slug, ``^[a-z0-9][a-z0-9-]{1,39}$`` (``proj-demo``,
            ``devops-iac``, ``global``)

Each is one ``key: value`` line. The writer leaves a ``status:`` or ``project:``
line where it is.

Where a field is read. Every field above, the rule fields too, is read at the
top level of the front matter and also when it sits indented under
``metadata:`` (the Edit tool moves fields there); a top-level line wins. The
three hand-rolled parsers named above read the top level only, so the writer
always writes the rule fields at the top level and removes a copy under
``metadata:``.

Standard library only: hooks import this module on a 2 s budget.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

try:  # Python 3.11+
    import re._parser as _sre_parse_mod  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - older Python
    import sre_parse as _sre_parse_mod  # type: ignore[no-redef]

# The opcode names (MAX_REPEAT, SUBPATTERN, ...) are made at run time, so the
# stubs do not list them. Typed as Any so the checker does not flag each one.
_sre_parse: Any = _sre_parse_mod

RULE_MAX = 160
APPLY_MAX = 400
VIOLATES_MAX = 300
TRIGGER_MAX = 80
TRIGGERS_MAX_ITEMS = 12  # the list after the model's items are added (derived ones never cut)
TRIGGERS_HARD_MAX = 64  # a list longer than this is a format problem
SCOPES = ("tool", "stop", "file", "always")
TYPED = ("glob:", "tool:", "phrase:")

# Fixed order, written last in the front matter.
KEYS = (
    "rule",
    "apply",
    "scope",
    "triggers",
    "violates",
    "example_repeat",
    "example_ok",
    "complies",
)
# The two fields of a project memory. Not part of KEYS: they are not written
# last, ``strip_schema`` keeps them, and the writer leaves their lines in place.
PROJECT_KEYS = ("status", "project")
STATUSES = ("open", "closed", "parked")
PROJECT_SLUG = r"^[a-z0-9][a-z0-9-]{1,39}$"
_PROJECT_SLUG_RE = re.compile(PROJECT_SLUG)
PROJECT_FIX_LINE = (
    'add "status: open|closed|parked" and "project: <slug>" to the front '
    "matter (one line each; the slug is lower-case letters, digits and "
    "hyphens, 2 to 40 characters, for example proj-demo or global)"
)
JSON_KEYS = ("apply", "violates", "example_repeat", "example_ok", "complies")

SCHEMA_LINE = (
    "A feedback memory carries, last in its front matter: rule: <one imperative "
    'sentence>; apply: "<JSON string, the exact shape>"; scope: tool|stop|file|always; '
    "triggers: [command prefix, glob:<path>, tool:<Name>, phrase:<words>] (no comma or "
    "quote in an item; may be left out only for scope always or stop); optional "
    'violates: "<Python regex over the raw Bash command>" with example_repeat: '
    '"<command that breaks the rule>" and example_ok: "<compliant command>"; optional '
    'complies: "<Python regex that matches a run of the rule\'s own command>" '
    "(it must match example_ok)."
)

# About 30 everyday commands. A `violates:` regex runs on EVERY Bash call, so
# one that matches any of these would deny ordinary work.
BENIGN_COMMANDS: Tuple[str, ...] = (
    "ls",
    "ls -la",
    "pwd",
    "git status",
    "git log --oneline -5",
    "git diff",
    "git diff --stat",
    "git branch -a",
    "git fetch origin",
    "git show HEAD --stat",
    "git worktree list",
    "git add tools/foo.py",
    'git commit -m "fix typo in readme"',
    "cat README.md",
    "head -20 notes.txt",
    "tail -n 50 app.log",
    "sed -n 1,20p main.py",
    "wc -l data.csv",
    "pytest -q",
    "python3 -m pytest -q tests/test_foo.py",
    "grep -rn foo .",
    "rg TODO src",
    'find . -name "*.py"',
    'python3 -c "print(1)"',
    "docker ps",
    "docker logs --tail 50 web",
    "docker compose ps",
    "echo hello",
    "mkdir -p build",
    "cp a.txt b.txt",
    "make test",
    "npm test",
    "pip list",
    "df -h",
    "jq . data.json",
    "gh pr list",
    "gh pr checks 123",
    "gh pr view 123",
    "gh run list",
    "gh run view 123",
    "tools/merge 123",
    "tools/ready 123",
    "git push",
    "docker compose up -d",
)

# Program names that fire on nearly every command of their tool. A MODEL-written
# plain trigger that is only one of these is too broad and is dropped by the
# annotator. A DERIVED bare head is kept: the derivation takes one only when the
# corpus names it rarely (the reference trigger coverage tool).
BARE_HEADS = frozenset(
    (
        "git",
        "docker",
        "python",
        "python3",
        "bash",
        "sh",
        "ls",
        "cat",
        "cd",
        "echo",
        "grep",
        "sed",
        "awk",
        "find",
        "rg",
        "gh",
        "npm",
        "pip",
        "make",
        "curl",
        "ssh",
        "sudo",
        "env",
        "pytest",
        "kubectl",
        "psql",
        "uv",
        "node",
        "head",
        "tail",
    )
)

_TOP_KEY = re.compile(r"^([A-Za-z_][\w\-]*):[ \t]*(.*)$")
_NESTED_KEY = re.compile(r"^[ \t]+([A-Za-z_][\w\-]*):[ \t]*(.*)$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ITEM_FORBIDDEN = re.compile(r"[,\"'\[\]{}\n\r\t]|: | #")
_ITEM_BAD_START = re.compile(r"^[#&*!|>%@`\-?]")


class FrontmatterError(ValueError):
    """The text has no closed front matter, so there is nowhere to write."""


class BodyChanged(RuntimeError):
    """A write would have changed the body. Never expected; always fatal."""


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------
def split(text: str) -> Optional[Tuple[List[str], int]]:
    """``(lines, close index)`` or None when there is no closed front matter.
    The same rule as the reference rule annotator (``split_frontmatter``)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines, i
    return None


def strip_schema(text: str) -> str:
    """``text`` with every schema key removed: the file as it was before any
    annotation, for deriving triggers the same way on a re-run."""
    if split(text) is None:
        return text
    return write_fields(text, {k: None for k in KEYS})


def body_of(text: str) -> str:
    """Every byte after the closing ``---`` line. The whole text when none."""
    s = split(text)
    if s is None:
        return text
    lines, close = s
    return "\n".join(lines[close + 1 :])


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def _decode_json_string(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == '"':
        try:
            val = json.loads(raw)
            if isinstance(val, str):
                return val
        except ValueError:
            pass
        if len(raw) >= 2 and raw[-1] == '"':
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def _flow_items(inner: str) -> List[str]:
    """The same split ``declared_triggers`` does, so both agree on an item."""
    out = []
    for part in inner.split(","):
        v = part.strip().strip("\"'")
        if v:
            out.append(v)
    return out


def empty_fields() -> Dict[str, object]:
    return {
        "rule": "",
        "apply": "",
        "scope": "",
        "triggers": [],
        "violates": "",
        "example_repeat": "",
        "example_ok": "",
        "complies": "",
        "status": "",
        "project": "",
    }


def _plain_value(raw: str) -> str:
    """A ``status:`` or ``project:`` value: outer space and one pair of quotes off."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


_LIST_ITEM = re.compile(r"^\s*-\s+(.+?)\s*$")


def _raw_fields(
    fm: List[str],
) -> Tuple[Dict[str, Tuple[str, Optional[List[str]]]], Dict[str, Tuple[str, Optional[List[str]]]]]:
    """``(top, nested)``: for each schema key the front matter lines ``fm``
    carry, its raw value and, for a block-list ``triggers:``, its items.
    ``top`` holds the top-level lines, ``nested`` the lines indented under
    ``metadata:``. A rule key that is written twice keeps its last line; a
    ``status:`` or ``project:`` line keeps its first."""
    top: Dict[str, Tuple[str, Optional[List[str]]]] = {}
    nested: Dict[str, Tuple[str, Optional[List[str]]]] = {}
    parent = ""  # the top-level key whose indented block we are in
    i = 0
    while i < len(fm):
        line = fm[i]
        i += 1
        m = _TOP_KEY.match(line)
        if m:
            key, value, where = m.group(1), m.group(2).rstrip(), top
            parent = key if not value.strip() else ""
        else:
            n = _NESTED_KEY.match(line)
            if not n or parent != "metadata":
                continue
            key, value, where = n.group(1), n.group(2).rstrip(), nested
        if key in PROJECT_KEYS:
            where.setdefault(key, (value, None))
            continue
        if key not in KEYS:
            continue
        items: Optional[List[str]] = None
        if key == "triggers":
            if value.strip() == "":
                items = []
                while i < len(fm):
                    item = _LIST_ITEM.match(fm[i])
                    if not item:
                        break
                    v = item.group(1).strip().strip("\"'")
                    if v:
                        items.append(v)
                    i += 1
            elif not re.match(r"^\[(.*)\]$", value.strip()):
                continue  # neither a flow list nor a block list: not read
        where[key] = (value, items)
    return top, nested


def _decode(key: str, value: str, items: Optional[List[str]]) -> object:
    if key == "triggers":
        if items is not None:
            return items
        return _flow_items(re.match(r"^\[(.*)\]$", value.strip()).group(1))  # type: ignore[union-attr]
    if key in PROJECT_KEYS:
        return _plain_value(value)
    if key in JSON_KEYS:
        return _decode_json_string(value)
    if key == "scope":
        return value.strip().strip("\"'").lower()
    if value.strip().startswith('"'):  # a quoted rule, see rule_line_value
        return _decode_json_string(value)
    return value.strip()


def read_fields(text: str) -> Dict[str, object]:
    """The eight schema fields of one memory file, plus ``status`` and
    ``project``. Absent fields read as ``""`` (``[]`` for triggers). Every
    field is read at the top level of the front matter and also from an
    indented line under ``metadata:`` (the Edit tool moves fields there); for
    each field a top-level value wins. Never raises."""
    out = empty_fields()
    s = split(text)
    if s is None:
        return out
    lines, close = s
    top, nested = _raw_fields(lines[1:close])
    for where in (nested, top):  # top last: it wins
        for key, (value, items) in where.items():
            got = _decode(key, value, items)
            if got:
                out[key] = got
    return out


def nested_keys(text: str) -> List[str]:
    """Every field of this module (the rule fields, ``status``, ``project``)
    that has a line under ``metadata:``, also when the top level has one too.
    The three hand-rolled parsers named in the module text never read such a
    line, so the fields hook asks for it to be moved."""
    s = split(text)
    if s is None:
        return []
    lines, close = s
    _, nested = _raw_fields(lines[1:close])
    return [k for k in PROJECT_KEYS + KEYS if k in nested]


def nested_problem(keys: List[str]) -> str:
    """The problem line for ``nested_keys``, ``""`` for none."""
    if not keys:
        return ""
    return (
        ", ".join(k + ":" for k in keys)
        + (" sits" if len(keys) == 1 else " sit")
        + " under metadata: and not at the top level"
    )


def nested_fix_line(keys: List[str]) -> str:
    """The exact instruction that moves ``keys`` to the top level."""
    quoted = ", ".join(f'"{k}:"' for k in keys)
    extra = ' (with the "- item" lines of triggers)' if "triggers" in keys else ""
    return (
        f"move the {'line' if len(keys) == 1 else 'lines'} {quoted}{extra} out of the "
        "metadata: block to the top level of the front matter: no indent, directly above "
        "the closing ---. The recall and trigger hooks read the top level only"
    )


def nested_rule_keys(text: str) -> List[str]:
    """The rule fields (``KEYS``) this file carries under ``metadata:`` and not
    at the top level: the fields that only this reader finds."""
    s = split(text)
    if s is None:
        return []
    lines, close = s
    top, nested = _raw_fields(lines[1:close])
    return [k for k in KEYS if k in nested and k not in top]


# --------------------------------------------------------------------------
# checking
# --------------------------------------------------------------------------
def nested_quantifier(pattern: str) -> bool:
    """True when a repeat that can run more than once holds another such
    repeat, as in ``(.*)*`` or ``(a+)+``: the shape that backtracks
    exponentially. Uses the standard library's own regex parser."""
    try:
        tree = _sre_parse.parse(pattern)
    except Exception:  # noqa: BLE001 - a bad pattern is reported elsewhere
        return False
    repeats = {_sre_parse.MAX_REPEAT, _sre_parse.MIN_REPEAT}
    poss = getattr(_sre_parse, "POSSESSIVE_REPEAT", None)
    if poss is not None:
        repeats.add(poss)

    def walk(items, inside: bool) -> bool:
        for op, av in items:
            if op in repeats:
                lo, hi, sub = av
                many = hi is _sre_parse.MAXREPEAT or hi > 1
                if many and inside:
                    return True
                if walk(sub, inside or many):
                    return True
            elif op is _sre_parse.SUBPATTERN:
                if walk(av[-1], inside):
                    return True
            elif op is _sre_parse.BRANCH:
                for alt in av[1]:
                    if walk(alt, inside):
                        return True
            elif op in (_sre_parse.ASSERT, _sre_parse.ASSERT_NOT):
                if walk(av[1], inside):
                    return True
            elif op is getattr(_sre_parse, "ATOMIC_GROUP", None):
                if walk(av, inside):
                    return True
            elif op is _sre_parse.GROUPREF_EXISTS:
                for sub in av[1:]:
                    if sub is not None and walk(sub, inside):
                        return True
        return False

    return walk(list(tree), False)


def broad_unbounded_repeats(pattern: str) -> int:
    """How many unbounded repeats (``*``, ``+``, ``{n,}``) of a BROAD atom the
    pattern holds: ``.``, a negated class such as ``[^x]``, ``\\S``, ``\\W``
    or ``\\D``. Two of them can split the same text in many ways, so the
    search time grows as a power of the input length: the reviewer's
    ``curl\\s.*\\s.*\\s.*\\s.*\\s.*-k\\b`` ran over 10 s on 6 KB."""
    try:
        tree = _sre_parse.parse(pattern)
    except Exception:  # noqa: BLE001
        return 0
    repeats = {_sre_parse.MAX_REPEAT, _sre_parse.MIN_REPEAT}
    poss = getattr(_sre_parse, "POSSESSIVE_REPEAT", None)
    if poss is not None:
        repeats.add(poss)
    not_cats = {
        _sre_parse.CATEGORY_NOT_SPACE,
        _sre_parse.CATEGORY_NOT_WORD,
        _sre_parse.CATEGORY_NOT_DIGIT,
    }

    def broad(items) -> bool:
        items = list(items)
        if len(items) != 1:
            return False
        op, av = items[0]
        if op is _sre_parse.ANY or op is _sre_parse.NOT_LITERAL:
            return True
        if op is _sre_parse.IN:
            return any(
                o is _sre_parse.NEGATE or (o is _sre_parse.CATEGORY and a in not_cats)
                for o, a in av
            )
        return False

    def walk(items) -> int:
        n = 0
        for op, av in items:
            if op in repeats:
                lo, hi, sub = av
                if hi is _sre_parse.MAXREPEAT and broad(sub):
                    n += 1
                n += walk(sub)
            elif op is _sre_parse.SUBPATTERN:
                n += walk(av[-1])
            elif op is _sre_parse.BRANCH:
                n += max((walk(alt) for alt in av[1]), default=0)
            elif op in (_sre_parse.ASSERT, _sre_parse.ASSERT_NOT):
                n += walk(av[1])
        return n

    return walk(list(tree))


REGEX_PROBE_BYTES = 10_000
REGEX_PROBE_SECONDS = 0.25  # search time over all probes, measured in the child
REGEX_PROBE_KILL = 1.0  # the child is killed after this, so no caller hangs

_PROBE_CHILD = (
    "import json,re,sys,time\n"
    "d=json.load(sys.stdin);rx=re.compile(d['p']);t=time.perf_counter()\n"
    "for s in d['i']: rx.search(s)\n"
    "print(time.perf_counter()-t)\n"
)


def regex_probe_inputs(example: str) -> List[str]:
    n = REGEX_PROBE_BYTES
    ex = (example or "x").strip() or "x"
    head = ex[: max(1, len(ex) // 2)]
    return [
        ((ex + " ") * (n // (len(ex) + 1) + 1))[:n],
        ("a " * n)[:n],
        " " * n,
        (head + " x" * n)[:n],
        (ex + "-" * n)[:n],
    ]


#: ``(old, new)``, set by a caller that keeps the probe times between its runs
#: (``guard_table.rebuild``). ``old`` holds the search time of each probe that
#: passed in an earlier run, by the hash of the probe input (the pattern and
#: the probe texts). Such a probe is not run again. ``new`` gets every probe
#: that passes in this run. A probe that fails is never kept: a busy machine
#: can cause it, so it is measured again. None: every probe runs.
PROBE_TIMES: Optional[Tuple[Dict[str, float], Dict[str, float]]] = None


def regex_too_slow(pattern: str, example: str = "", name: str = "violates") -> str:
    """``""`` when ``pattern`` searches 10 KB probe inputs fast, else the reason.
    Runs in a child process that is killed after ``REGEX_PROBE_KILL`` seconds."""
    payload = json.dumps({"p": pattern, "i": regex_probe_inputs(example)})
    times, key = PROBE_TIMES, ""
    if times is not None:
        key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        if key in times[0] and times[0][key] <= REGEX_PROBE_SECONDS:
            times[1][key] = times[0][key]
            return ""
    try:
        r = subprocess.run(
            [sys.executable, "-c", _PROBE_CHILD],
            input=payload,
            capture_output=True,
            text=True,
            timeout=REGEX_PROBE_KILL,
        )
    except subprocess.TimeoutExpired:
        return f"{name} runs longer than {REGEX_PROBE_KILL:g} s on {REGEX_PROBE_BYTES} bytes"
    except OSError as exc:
        return f"{name} could not be timed: {exc}"
    try:
        secs = float(r.stdout.strip())
    except ValueError:
        return f"{name} could not be timed"
    if secs > REGEX_PROBE_SECONDS:
        return (
            f"{name} takes {secs:.2f} s on {REGEX_PROBE_BYTES} bytes; "
            f"the limit is {REGEX_PROBE_SECONDS} s"
        )
    if times is not None:
        times[1][key] = round(secs, 4)
    return ""


def trigger_problem(item: object) -> str:
    """The plain-word reason ``item`` cannot be a trigger, or ``""``."""
    if not isinstance(item, str) or not item.strip():
        return "a trigger item is empty"
    if item != item.strip():
        return f"trigger {item!r} has leading or trailing space"
    if len(item) > TRIGGER_MAX:
        return f"trigger {item[:40]!r}... is longer than {TRIGGER_MAX} characters"
    if _ITEM_FORBIDDEN.search(item):
        return (
            f"trigger {item!r} holds a comma, quote, bracket, tab, ': ' or ' #', "
            "which the one-line list cannot carry"
        )
    if _ITEM_BAD_START.match(item):
        return f"trigger {item!r} starts with a punctuation mark"
    for prefix in TYPED:
        if item.startswith(prefix):
            rest = item[len(prefix) :]
            if not rest.strip() or rest != rest.strip():
                return f"trigger {item!r} has nothing after {prefix!r}"
            if prefix == "tool:" and not _TOOL_NAME.match(rest):
                return f"trigger {item!r} is not a tool name"
            if prefix == "glob:" and re.search(r"\s", rest):
                return f"trigger {item!r} is a glob with a space"
            return ""
    if re.match(r"^[a-z]+:", item):
        return f"trigger {item!r} has an unknown type prefix; use glob:, tool: or phrase:"
    return ""


def rule_apply_problems(fields: Dict[str, object], kind: str = "feedback") -> List[str]:
    out: List[str] = []
    rule = fields.get("rule") or ""
    apply_v = fields.get("apply") or ""
    if kind == "feedback":
        if not rule:
            out.append("rule is missing")
        if not apply_v:
            out.append("apply is missing")
    if rule:
        if "\n" in str(rule):
            out.append("rule is more than one line")
        if len(str(rule)) > RULE_MAX:
            out.append(f"rule is longer than {RULE_MAX} characters")
    if apply_v and len(str(apply_v)) > APPLY_MAX:
        out.append(f"apply is longer than {APPLY_MAX} characters")
    return out


def scope_problems(fields: Dict[str, object], kind: str = "feedback") -> List[str]:
    out: List[str] = []
    scope = fields.get("scope") or ""
    triggers = fields.get("triggers") or []
    if kind == "feedback" and not scope:
        out.append("scope is missing")
    if scope and scope not in SCOPES:
        out.append(f"scope {scope!r} is not one of tool, stop, file, always")
    if not isinstance(triggers, list):
        out.append("triggers is not a list")
        return out
    if len(triggers) > TRIGGERS_HARD_MAX:
        out.append(f"triggers has more than {TRIGGERS_HARD_MAX} items")
    for item in triggers:
        p = trigger_problem(item)
        if p:
            out.append(p)
    if scope in ("tool", "file") and not triggers:
        out.append(f"scope {scope} needs at least one trigger")
    if scope == "file" and triggers and not any(str(t).startswith("glob:") for t in triggers):
        out.append("scope file needs at least one glob: trigger")
    if (
        scope == "tool"
        and triggers
        and not any(not str(t).startswith(("glob:", "phrase:")) for t in triggers)
    ):
        out.append("scope tool needs a command prefix or a tool: trigger")
    return out


def violates_problems(fields: Dict[str, object]) -> List[str]:
    out: List[str] = []
    pat = fields.get("violates") or ""
    rep = fields.get("example_repeat") or ""
    ok = fields.get("example_ok") or ""
    if not pat:
        if rep or ok:
            out.append("example_repeat and example_ok are allowed only with violates")
        return out
    if not isinstance(pat, str):
        return ["violates is not a string"]
    if not rep:
        out.append("violates needs example_repeat")
    if not ok:
        out.append("violates needs example_ok")
    for name, ex in (("example_repeat", rep), ("example_ok", ok)):
        if ex and re.match(r"^\s*Bash\s", str(ex)):
            out.append(f"{name} is an action line; give the raw command without 'Bash '")
    if len(pat) > VIOLATES_MAX:
        out.append(f"violates is longer than {VIOLATES_MAX} characters")
    if re.match(r"^\^?\s*(?:\\s\*)?Bash\b", pat):
        out.append("violates starts with Bash; it must match the raw command, not an action line")
    try:
        rx = re.compile(pat)
    except re.error as exc:
        out.append(f"violates does not compile: {exc}")
        return out
    slow_shape = False
    if nested_quantifier(pat):
        out.append("violates has a nested quantifier such as (.*)* or (.+)+")
        slow_shape = True
    if broad_unbounded_repeats(pat) > 1:
        out.append(
            "violates has more than one unbounded repeat of a broad class "
            "such as .* or \\S+ or [^x]+"
        )
        slow_shape = True
    if rx.search(""):
        out.append("violates matches the empty command, so it matches every command")
    if rep and not rx.search(str(rep)):
        out.append("violates does not match its example_repeat")
    if ok and rx.search(str(ok)):
        out.append("violates matches its example_ok")
    hits = [c for c in BENIGN_COMMANDS if rx.search(c)]
    if hits:
        out.append("violates matches everyday commands: " + "; ".join(hits[:3]))
    if not slow_shape:  # a known slow shape is not run at all
        slow = regex_too_slow(pat, str(rep))
        if slow:
            out.append(slow)
    return out


def complies_problems(fields: Dict[str, object]) -> List[str]:
    """Problems of the optional ``complies:`` regex (WI-3d). It must match a
    run of the rule's own command: so it must match ``example_ok``, must not
    match ``example_repeat``, and must match no everyday command (a failing
    ``git status`` must never unlock the override). Same shape and time limits
    as ``violates``."""
    pat = fields.get("complies") or ""
    if not pat:
        return []
    if not isinstance(pat, str):
        return ["complies is not a string"]
    if not fields.get("violates"):
        return ["complies is allowed only with violates"]
    rep = str(fields.get("example_repeat") or "")
    ok = str(fields.get("example_ok") or "")
    out: List[str] = []
    if len(pat) > VIOLATES_MAX:
        out.append(f"complies is longer than {VIOLATES_MAX} characters")
    if re.match(r"^\^?\s*(?:\\s\*)?Bash\b", pat):
        out.append("complies starts with Bash; it must match the raw command, not an action line")
    try:
        rx = re.compile(pat)
    except re.error as exc:
        out.append(f"complies does not compile: {exc}")
        return out
    slow_shape = False
    if nested_quantifier(pat):
        out.append("complies has a nested quantifier such as (.*)* or (.+)+")
        slow_shape = True
    if broad_unbounded_repeats(pat) > 1:
        out.append(
            "complies has more than one unbounded repeat of a broad class "
            "such as .* or \\S+ or [^x]+"
        )
        slow_shape = True
    if rx.search(""):
        out.append("complies matches the empty command, so it matches every command")
    if ok and not rx.search(ok):
        out.append("complies does not match its example_ok")
    if rep and rx.search(rep):
        out.append("complies matches its example_repeat")
    hits = [c for c in BENIGN_COMMANDS if rx.search(c)]
    if hits:
        out.append("complies matches everyday commands: " + "; ".join(hits[:3]))
    if not slow_shape:
        slow = regex_too_slow(pat, ok, name="complies")
        if slow:
            out.append(slow)
    return out


def status_project_problems(fields: Dict[str, object], kind: str = "feedback") -> List[str]:
    """Problems of ``status`` and ``project``. Kind ``"project"`` needs both.
    Every other kind may leave them out; a value that is present must be valid."""
    out: List[str] = []
    status = fields.get("status") or ""
    project = fields.get("project") or ""
    if not status:
        if kind == "project":
            out.append("status is missing")
    elif status not in STATUSES:
        out.append(f"status {status!r} is not one of open, closed, parked")
    if not project:
        if kind == "project":
            out.append("project is missing")
    elif not isinstance(project, str) or not _PROJECT_SLUG_RE.match(project) or "\n" in project:
        out.append(f"project {project!r} is not a lower-case slug ({PROJECT_SLUG})")
    return out


def status_project_problems_of(problems: List[str]) -> List[str]:
    """The items of ``problems`` that ``status_project_problems`` wrote."""
    return [p for p in problems if p.startswith(("status ", "project "))]


def check_fields(fields: Dict[str, object], kind: str = "feedback") -> List[str]:
    """Every problem with ``fields``, in plain words. ``[]`` means clean.
    ``kind`` is ``"feedback"`` (rule, apply and scope required), ``"project"``
    (status and project required) or any other memory kind. For every kind a
    field that is present is checked."""
    return (
        rule_apply_problems(fields, kind)
        + scope_problems(fields, kind)
        + violates_problems(fields)
        + complies_problems(fields)
        + status_project_problems(fields, kind)
    )


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def rule_line_value(rule: str) -> str:
    """The text after ``rule: ``. A plain line, the W5 format, unless the rule
    holds ``": "`` or ``" #"``, which a YAML reader takes as a mapping or a
    comment (``# pyright: ignore[...]``, ``if: always()``). Such a rule is
    written in double quotes. The two mirror-style parsers strip exactly one
    pair of outer quotes, so they return the rule unchanged only when quoting
    adds no escape; a rule that would need one (a quote, a backslash, a control
    character) is refused."""
    if "\n" in rule or "\r" in rule:
        raise ValueError("rule must be one line")
    if ": " not in rule and " #" not in rule and not rule.endswith(":"):
        return rule
    quoted = json.dumps(rule, ensure_ascii=False)
    if quoted != '"' + rule + '"':
        raise ValueError(
            "rule holds ': ' or ' #' and a character that quoting would escape; rewrite the rule"
        )
    return quoted


def _serialize(fields: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    rule = str(fields.get("rule") or "")
    if rule:
        out.append("rule: " + rule_line_value(rule))
    apply_v = str(fields.get("apply") or "")
    if apply_v:
        out.append("apply: " + json.dumps(apply_v))
    scope = str(fields.get("scope") or "")
    if scope:
        if scope not in SCOPES:
            raise ValueError(f"scope {scope!r} is not in {SCOPES}")
        out.append("scope: " + scope)
    triggers = list(fields.get("triggers") or [])
    if triggers:
        for item in triggers:
            p = trigger_problem(item)
            if p:
                raise ValueError(p)
        out.append("triggers: [" + ", ".join(triggers) + "]")
    for key in ("violates", "example_repeat", "example_ok", "complies"):
        v = str(fields.get(key) or "")
        if v:
            out.append(f"{key}: " + json.dumps(v))
    return out


def _set_project_key(fm: List[str], key: str, value: str, have: str) -> List[str]:
    """``fm`` (the front matter lines) with ``key`` set to ``value``; ``""``
    removes it. Unchanged when the file already reads as ``value``."""
    if value == have:
        return fm
    problems = status_project_problems({key: value})
    if problems:
        raise ValueError(problems[0])
    out: List[str] = []
    done = False
    parent = ""
    for line in fm:
        m = _TOP_KEY.match(line)
        n = None if m else _NESTED_KEY.match(line)
        hit = (m is not None and m.group(1) == key) or (
            n is not None and parent == "metadata" and n.group(1) == key
        )
        if m:
            parent = m.group(1) if not m.group(2).strip() else ""
        if not hit:
            out.append(line)
            continue
        if value and not done:  # the first line keeps its place and its indent
            out.append(line[: len(line) - len(line.lstrip())] + f"{key}: {value}")
            done = True
    if value and not done:
        out.append(f"{key}: {value}")
    return out


def write_fields(text: str, fields: Dict[str, object]) -> str:
    """``text`` with the schema fields written last in its front matter.

    ``fields`` is merged over what the file already carries: a key left out
    keeps its value, a key given as ``""``/``[]``/None is removed. A rule field
    that sits under ``metadata:`` moves to the top level. Every other
    front matter line and the body stay byte for byte. The result is checked:
    a body change raises ``BodyChanged``. Idempotent.

    ``status`` and ``project`` are not moved to the end. A line the file has
    stays where it is, also under ``metadata:``, and stays byte for byte when
    the value does not change. A new one is written at the top level, before
    the rule fields.
    """
    s = split(text)
    if s is None:
        raise FrontmatterError("no closed front matter")
    lines, close = s
    merged = read_fields(text)
    fm = lines[1:close]
    for k, v in fields.items():
        if k in PROJECT_KEYS:
            fm = _set_project_key(fm, k, str(v or ""), str(merged[k]))
            continue
        if k not in KEYS:
            raise KeyError(f"unknown field {k!r}")
        merged[k] = v if v is not None else ([] if k == "triggers" else "")
    kept: List[str] = []
    parent = ""
    i = 0
    while i < len(fm):
        line = fm[i]
        m = _TOP_KEY.match(line)
        if m:
            parent = m.group(1) if not m.group(2).strip() else ""
        else:  # a rule field under metadata: moves to the top level
            m = _NESTED_KEY.match(line) if parent == "metadata" else None
        if m and m.group(1) in KEYS:
            i += 1
            if m.group(1) == "triggers" and not m.group(2).strip():
                while i < len(fm) and re.match(r"^\s*-\s", fm[i]):
                    i += 1
            continue
        kept.append(line)
        i += 1
    new = "\n".join([lines[0], *kept, *_serialize(merged), *lines[close:]])
    if body_of(new) != body_of(text):
        raise BodyChanged("the write would change the body")
    return new
