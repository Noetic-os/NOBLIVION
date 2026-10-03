#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subject labels for Claude Code memory files, and the label index.

A label names a SUBJECT a memory is about: a ticket key, a service or
container, a file, a named tool. The labeller is deterministic. It
reads only the text it is given: a memory file (front matter and body), a Bash
command, a file path or a prompt. No model call, no network, no daemon, no
ticket text and no repository document. Standard library only.

Label kinds (``labels``):
  * host: not made. The reference rules had a static alias map of one
    private setup's hosts; it has no generic form, so it is removed. The kind
    name stays for index readers.
  * key: a ticket key ``<PREFIX>-<n>``, upper case, for each prefix in the
    config key ``labels.ticket_prefixes`` (``["PROJ"]`` labels ``PROJ-12``).
    No prefix configured: no key labels.
  * service: a ``<prefix>-*`` service, container or repository tool name,
    lower case, for each prefix in the config key ``labels.service_prefixes``
    (``["app"]`` labels ``app-db`` and ``app-merge``). No prefix configured:
    no service labels.
  * file: the base name of a path with a known extension
    (``tools/x/run_ci_v2_dispatcher.sh`` -> ``run_ci_v2_dispatcher.sh``), lower
    case. Generic names (``README.md``, ``x.py``) and memory file names
    (``feedback_*.md``, ``MEMORY.md``) are not labels.
  * tool: a named program from ``TOOL_NAMES`` (``gitleaks``, ``velero``,
    ``nvidia-smi``), as a word or as the last part of a path
    (``/usr/bin/psql``), lower case. Generic programs (``git``, ``ls``,
    ``python``) are not in the list.

The same function labels both sides: a memory when the index is built, and a
command, a path or a prompt when a hook matches. So a label means the same
thing on both sides.

The label index (``build_index``) is the ``label_index`` key of the guard
table (``guard_table.build``, table version 2). It covers EVERY
memory file, not only the files with rule fields. ``match`` ranks the memories
that share labels with a query: a match is specific when one shared label is
held by at most ``cutoff`` memories (``DF_CUTOFF``, 8); a label held by more
memories needs a second shared label. Score = sum of the inverse document
frequency of the shared labels; ties go to more shared labels, then the newer
file. A tool or a document name (``.md``) is a weak label: it never matches
alone. Without a specific label, the shared labels must be held together by
at most ``cutoff`` memories.
"""

from __future__ import annotations

import importlib.util
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LABELLER_VERSION = 1  # provenance in the index ("labeller"): which rules built it
INDEX_VERSION = 1  # the index shape; a reader refuses another (fail open)
DF_CUTOFF = 8  # a label of more memories never matches alone
SOURCE_PREFIX = "rule"  # memory_labels.source = "rule:<kind>" (the deterministic leg)
TEXT_CHARS = 120  # the short render text of an index row

KIND_HOST, KIND_KEY, KIND_SERVICE, KIND_FILE, KIND_TOOL = "host", "key", "service", "file", "tool"
# Index and topic files are not memories (guard_table._NOT_MEMORY):
# neither store labels them.
INDEX_FILE_RX = re.compile(r"^(MEMORY|MEMORY_ARCHIVE|topic_.*)\.md$")


# --------------------------------------------------------------------------
# ticket keys and services: the prefixes come from the config (hook_config).
# --------------------------------------------------------------------------
def _hook_config():
    """``hook_config.py`` from this file's folder (data dir, config file)."""
    path = Path(__file__).resolve().parent / "hook_config.py"
    spec = importlib.util.spec_from_file_location("hook_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PREFIX_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
_NEVER = re.compile(r"(?!x)x")  # matches nothing


def valid_prefixes(values: Any) -> Tuple[str, ...]:
    """The items of ``values`` that have the prefix shape (a letter, then up
    to 31 letters or digits), stripped, in order. ``()`` for a value that is
    not a list or a tuple."""
    if not isinstance(values, (list, tuple)):
        return ()
    got = (v.strip() for v in values if isinstance(v, str))
    return tuple(p for p in got if _PREFIX_SHAPE.match(p))


def _prefixes(key: str) -> Tuple[str, ...]:
    """The valid prefixes of the config list ``key``; ``()`` on any fault."""
    try:
        got = _hook_config().string_list(key)
    except Exception:  # noqa: BLE001 - labels fail open
        return ()
    return valid_prefixes(list(got))


class Rules:
    """The prefix rules of one config: the ticket key and the service
    patterns. ``rules(ticket, service)`` builds one from explicit lists (the
    indexer passes the store's config); the module default ``RULES`` reads
    the hook config once, at import."""

    __slots__ = ("ticket_prefixes", "service_prefixes", "key_rx", "service_rx")

    def __init__(self, ticket_prefixes: Iterable[str] = (), service_prefixes: Iterable[str] = ()):
        self.ticket_prefixes = tuple(p.upper() for p in valid_prefixes(list(ticket_prefixes)))
        self.service_prefixes = tuple(p.lower() for p in valid_prefixes(list(service_prefixes)))
        self.key_rx = (
            re.compile(
                r"(?<![A-Za-z0-9_])("
                + "|".join(map(re.escape, self.ticket_prefixes))
                + r")-(\d{1,6})(?![0-9])",
                re.IGNORECASE,
            )
            if self.ticket_prefixes
            else _NEVER
        )
        self.service_rx = (
            re.compile(
                r"(?<![A-Za-z0-9_.-])((?:"
                + "|".join(map(re.escape, self.service_prefixes))
                + r")-[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
            )
            if self.service_prefixes
            else _NEVER
        )


def rules(ticket_prefixes: Iterable[str] = (), service_prefixes: Iterable[str] = ()) -> Rules:
    """The rules for these prefix lists (config ``labels.ticket_prefixes`` and
    ``labels.service_prefixes``). A prefix of the wrong shape is dropped."""
    return Rules(ticket_prefixes, service_prefixes)


RULES = Rules(_prefixes("labels.ticket_prefixes"), _prefixes("labels.service_prefixes"))
TICKET_PREFIXES = RULES.ticket_prefixes
SERVICE_PREFIXES = RULES.service_prefixes
_KEY_RX = RULES.key_rx
_SERVICE_RX = RULES.service_rx

# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
FILE_EXTENSIONS = frozenset(
    (
        "py sh bash md json jsonl yml yaml toml sql service timer socket mount conf cfg ini txt "
        "js mjs cjs ts tsx html css csv j2 go rs xml rules plist ps1 log"
    ).split()
)
# An extension that is also a common attribute name (``resp.json``, ``ev.ts``,
# ``self.service``, ``console.log``). A token with one is a file only inside a
# path (``deploy/x.timer``) or when its name has ``_``, ``-``, ``@`` or a digit
# (``merge_gate_policy.json``, ``user@1000.service``).
AMBIGUOUS_EXTENSIONS = frozenset(
    "json ts js service timer socket mount log sql html css xml rules".split()
)
_NAMEISH_RX = re.compile(r"[_@0-9-]")
_PATH_TOKEN_RX = re.compile(r"[A-Za-z0-9_.@~+/-]+")
_BASENAME_RX = re.compile(r"^[A-Za-z0-9_@+][A-Za-z0-9_.@+-]*\.([A-Za-z0-9]+)$")
_LETTER_RX = re.compile(r"[a-z]")
# Names too generic to name a subject: they would match alone at a low count.
GENERIC_FILES = frozenset(
    """
readme.md changelog.md index.md agents.md todo.md notes.md __init__.py __main__.py setup.py
conftest.py main.py app.py test.py tests.py utils.py util.py helpers.py config.py settings.py
run.sh test.sh build.sh install.sh start.sh index.js index.ts index.html package.json
package-lock.json tsconfig.json requirements.txt setup.cfg .env x.py y.py a.py b.py foo.py bar.py
t.py tmp.py out.txt output.txt log.txt result.json results.json data.json tmp.txt input.txt
file.txt example.py script.py script.sh sample.json out.json tmp.json test.json a.txt b.txt
x.txt x.json x.sh
""".split()
)
# A memory file name is the memory itself, not a subject.
_MEMORY_FILE_RX = re.compile(
    r"^(?:(?:feedback|project|reference|user|topic)_[a-z0-9_.-]*|memory(?:_archive)?)\.md$"
)

# --------------------------------------------------------------------------
# tools: named programs that identify a subject. A program of every second
# command (git, ls, python, ssh, curl, grep, jq ...) is left out: as a label
# it would carry no subject.
# --------------------------------------------------------------------------
TOOL_NAMES = frozenset(
    """
gitleaks pyright ruff mypy velero restic borg rclone tailscale nvidia-smi rocm-smi ollama vllm
llama-server llama.cpp prometheus grafana alertmanager loki promtail node_exporter cadvisor
journalctl systemctl crontab logrotate certbot nginx caddy traefik postgres psql pg_dump
pg_restore pgvector redis-cli rabbitmqctl qdrant kubectl helm k3s docker-compose buildx skopeo
trivy hadolint shellcheck actionlint semgrep bandit gh-review ffmpeg whisper piper sox
smbclient nfsstat exportfs mount.nfs sshfs autossh wireguard wg-quick ufw iptables nft fail2ban
apparmor sudoers visudo useradd usermod
""".split()
)


def _add(out: Dict[str, str], label: str, kind: str) -> None:
    if label and label not in out:
        out[label] = kind


def labels(text: str, rules: Optional[Rules] = None) -> Dict[str, str]:
    """``{label: kind}`` for ``text``, in order of first appearance. Never
    raises on text; a non-string gives no labels. ``rules`` (default
    ``RULES``, the hook config) gives the ticket and service prefixes."""
    out: Dict[str, str] = {}
    if not isinstance(text, str) or not text:
        return out
    r = RULES if rules is None else rules
    found: List[Tuple[int, str, str]] = []
    for m in r.key_rx.finditer(text):
        found.append((m.start(), f"{m.group(1).upper()}-{int(m.group(2))}", KIND_KEY))
    for m in r.service_rx.finditer(text):
        found.append((m.start(), m.group(1), KIND_SERVICE))
    for m in _PATH_TOKEN_RX.finditer(text):
        # One pass for files and tools: the last path part of a token.
        tok = m.group(0)
        base = tok.rstrip(".,:;-/").rsplit("/", 1)[-1]
        low = base.lower()
        if low in TOOL_NAMES:
            found.append((m.start() + tok.rfind(base), low, KIND_TOOL))
            continue
        if "." not in base:
            continue
        bm = _BASENAME_RX.match(base)
        ext = bm.group(1).lower() if bm else ""
        if ext not in FILE_EXTENSIONS:
            continue
        if (
            ext in AMBIGUOUS_EXTENSIONS
            and "/" not in tok
            and not _NAMEISH_RX.search(base[: -len(ext) - 1])
        ):
            continue
        if (
            low in GENERIC_FILES
            or _MEMORY_FILE_RX.match(low)
            or not _LETTER_RX.search(low.rsplit(".", 1)[0])
        ):
            continue
        found.append((m.start() + tok.rfind(base), low, KIND_FILE))
    for _pos, label, kind in sorted(found, key=lambda f: f[0]):
        _add(out, label, kind)
    return out


def memory_labels(text: str, stem: str = "", rules: Optional[Rules] = None) -> Dict[str, str]:
    """The labels of one memory file: ``labels`` of its whole text (front
    matter and body). The file's own name is not a label of it. The store's
    indexer calls this too (``noblivion.labels``), so a row holds the labels
    the guard table holds."""
    out = labels(text, rules)
    own = f"{stem}.md".lower() if stem else ""
    if own in out:
        del out[own]
    return out


def source_of(kind: str) -> str:
    """The ``memory_labels.source`` value for a label kind."""
    return f"{SOURCE_PREFIX}:{kind}"


_FM_KEY_RX = re.compile(r"^([A-Za-z_][\w\-]*):\s*(.*)$")


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def name_and_description(text: str, stem: str) -> Tuple[str, str, str]:
    """``(name, description, body)`` of a memory file, read the way
    ``recall_hook.parse_frontmatter`` and ``memory_name`` read
    them (top-level ``key: value`` lines between the first two ``---`` lines),
    so a name here is the name the recall shown-set stores. The name falls
    back to ``stem``; a file without closed front matter is all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return stem, "", text
    top: Dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            name = (top.get("name") or "").strip() or stem
            return (
                name,
                (top.get("description") or "").strip(),
                "\n".join(lines[i + 1 :]).lstrip("\n"),
            )
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        m = _FM_KEY_RX.match(line)
        if m and m.group(2).strip():
            top[m.group(1)] = _unquote(m.group(2))
    return stem, "", text


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------
def short_text(description: str, body: str, n: int = TEXT_CHARS) -> str:
    """The short render text of an index row: the description, else the
    first body line that is not a heading, cut at ``n`` characters."""
    text = " ".join(str(description or "").split())
    if not text:
        for line in str(body or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                text = " ".join(line.split())
                break
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def build_index(docs: Iterable[Mapping[str, Any]], cutoff: int = DF_CUTOFF) -> Dict[str, Any]:
    """The label index over ``docs``: each a mapping with ``id`` (the file
    stem), ``name`` (front matter name), ``labels`` (a ``labels`` dict),
    ``mtime`` (int) and ``text`` (the short render text).

    Shape (compact, because every guard hook call parses it):
    ``{"version", "labeller", "cutoff", "n", "labels": [label], "kinds": [kind],
    "df": [n], "post": [[doc number]], "docs": [{"id", "m", "t", "name"?}]}``.
    ``name`` is stored only when it differs from the stem; a memory with no
    label keeps only its ``id`` (it counts in ``n``, it never matches)."""
    rows: List[Dict[str, Any]] = []
    post: Dict[str, List[int]] = {}
    kinds: Dict[str, str] = {}
    for d in docs:
        i = len(rows)
        row: Dict[str, Any] = {"id": str(d.get("id") or "")}
        labs = d.get("labels") or {}
        if labs:  # a memory with no label never matches: its id is enough
            row["m"], row["t"] = int(d.get("mtime") or 0), str(d.get("text") or "")
            name = str(d.get("name") or "")
            if name and name != row["id"]:
                row["name"] = name
        rows.append(row)
        for label, kind in labs.items():
            post.setdefault(label, []).append(i)
            kinds.setdefault(label, kind)
    order = sorted(post)
    return {
        "version": INDEX_VERSION,
        "labeller": LABELLER_VERSION,
        "cutoff": int(cutoff),
        "n": len(rows),
        "labels": order,
        "kinds": [kinds[x] for x in order],
        "df": [len(post[x]) for x in order],
        "post": [post[x] for x in order],
        "docs": rows,
    }


def index_ok(index: Any) -> bool:
    """True when ``index`` has the shape ``build_index`` writes, version
    ``INDEX_VERSION``."""
    if not isinstance(index, dict) or index.get("version") != INDEX_VERSION:
        return False
    try:
        n = len(index["labels"])
        return (
            isinstance(index["docs"], list)
            and len(index["df"]) == n
            and len(index["post"]) == n
            and int(index.get("n", -1)) == len(index["docs"])
        )
    except (KeyError, TypeError, ValueError):
        return False


_LOOKUP: Dict[int, Tuple[Any, Dict[str, int]]] = {}


def _lookup(index: Mapping[str, Any]) -> Dict[str, int]:
    got = _LOOKUP.get(id(index))
    if got is not None and got[0] is index:
        return got[1]
    table = {label: i for i, label in enumerate(index.get("labels") or [])}
    _LOOKUP.clear()
    _LOOKUP[id(index)] = (index, table)
    return table


def weak_label(label: str, kind: str) -> bool:
    """A label that names how, not what: a tool, or a document name (a
    ``.md`` file such as a plan or a report that many memories cite). It never
    matches alone, at any count; it can be the second label of a match."""
    return kind == KIND_TOOL or (kind == KIND_FILE and label.endswith(".md"))


class Match:
    """One memory that shares labels with the query."""

    __slots__ = ("doc", "id", "name", "labels", "score", "mtime", "text")

    def __init__(self, doc: int, row: Mapping[str, Any], labels_: List[str], score: float):
        self.doc = doc
        self.id = str(row.get("id") or "")
        self.name = str(row.get("name") or self.id)
        self.labels = labels_
        self.score = score
        self.mtime = int(row.get("m") or 0)
        self.text = str(row.get("t") or "")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "labels": list(self.labels),
            "score": round(self.score, 3),
        }


def match(
    query_labels: Iterable[str], index: Mapping[str, Any], cutoff: Optional[int] = None
) -> List[Match]:
    """The memories that share a specific label set with the query, best
    first. A memory matches when it shares one SPECIFIC label: held by at most
    ``cutoff`` memories (default: the index's own cutoff) and not weak
    (``weak_label``). Without one, it needs a second shared label, and the
    shared labels together must be held by at most ``cutoff`` memories (so
    ``example-host`` plus ``crontab`` does not match the dozens of memories that
    name both). Score: the sum of ln(N / df) over the shared labels. Ties:
    more shared labels first, then the newer file, then the id."""
    if not index_ok(index):
        return []
    cut = int(index.get("cutoff", DF_CUTOFF) if cutoff is None else cutoff)
    look = _lookup(index)
    n = max(1, int(index.get("n") or 0))
    dfs, posts, docs = index["df"], index["post"], index["docs"]
    shared: Dict[int, List[int]] = {}
    for label in dict.fromkeys(query_labels):
        li = look.get(label)
        if li is None:
            continue
        for d in posts[li]:
            shared.setdefault(d, []).append(li)
    out: List[Match] = []
    kinds, labels_ = index.get("kinds") or [], index["labels"]
    sets: Dict[int, set] = {}
    for d, lis in shared.items():
        if not any(
            dfs[li] <= cut and not weak_label(labels_[li], kinds[li] if li < len(kinds) else "")
            for li in lis
        ):
            # No specific label: a second shared label is needed, and the
            # memories that hold ALL the shared labels must be few.
            if len(lis) < 2:
                continue
            for li in lis:
                if li not in sets:
                    sets[li] = set(posts[li])
            if len(set.intersection(*(sets[li] for li in lis))) > cut:
                continue
        score = sum(math.log(n / max(1, dfs[li])) for li in lis)
        out.append(Match(d, docs[d], [labels_[li] for li in lis], score))
    out.sort(key=lambda m: (-m.score, -len(m.labels), -m.mtime, m.id))
    return out


def labels_of_command(command: str, strip: Optional[Any] = None) -> Dict[str, str]:
    """The labels of a Bash command. ``strip`` (optional) removes data that is
    not the command, such as heredoc bodies
    (``guard_table.without_heredoc_bodies``)."""
    if not isinstance(command, str):
        return {}
    if strip is not None:
        try:
            command = strip(command)
        except Exception:  # noqa: BLE001, S110 - the raw command is still labelled
            pass
    return labels(command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``--labels TEXT``: print the labels of TEXT as JSON."""
    import json
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if "--labels" in args and args.index("--labels") + 1 < len(args):
        print(json.dumps(labels(args[args.index("--labels") + 1]), indent=1))
        return 0
    print((__doc__ or "").split("\n\n")[0], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
