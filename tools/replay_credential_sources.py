# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count the calls the credential guard would deny for its secret sources (NOBLIVION-72).

The guard denies a Bash, Read or Grep call that would print a secret source: an
`.env*` file, the AWS credentials file, the netrc file, the Docker and kube config
files, a private key file, a file in /run/secrets, or the whole environment (a bare
`env` or `printenv`). A false deny costs the user a turn, so the rule is measured on
real calls before it ships. This tool reads every Bash, Read and Grep call of the
given transcripts and runs each one through the same code as the hook
(``guard_hook._credential_check``), with these changes:

* The git probes are off, so only the new class can deny.
* A file counts by its name and not by its content or by whether it exists. A call
  in a transcript ran in another folder at another time, so the file may be gone.
  The count is therefore an upper bound: a call to a file that never existed is
  counted.
* A grep with a pattern that matches no line of the file would pass in the hook, because
  the hook reads the file. The file is gone, so two counts are made: ``strict`` (every
  grep that can print counts) and ``typical`` (a grep counts when its pattern matches a
  line of a made-up file with the usual names: ``API_KEY``, ``TOKEN``, ``PASSWORD``,
  ``DATABASE_URL``, ``DEBUG``, ``PORT`` and a comment). ``typical`` is the estimate.

The report holds counts only. It never prints a command, a path or a value. With
``--private-dir`` the tool also writes one line per would-deny call to a file in that
folder (mode 0700, file mode 0600): the tool, the label, the first word and a
skeleton of the command in which every word that is not a flag or a plain path is
replaced by ``_``. A person reads the skeleton to judge the false denies.

Usage::

    python tools/replay_credential_sources.py "~/.claude/projects/*/*.jsonl" \\
        --private-dir /private/replay-72
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import time
import types
from collections import Counter
from collections.abc import Iterator, Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
TOOLS = ("Bash", "Read", "Grep")
#: the made-up file for the ``typical`` count: no value, only the usual names
TYPICAL_LINES = [
    "# settings",
    "API_KEY=x",
    "SECRET_KEY=x",
    "TOKEN=x",
    "PASSWORD=x",
    "DATABASE_URL=x",
    "DEBUG=1",
    "PORT=8080",
    "HOST=localhost",
]
MODE = {"typical": False}
_PATH_WORD = re.compile(r"^[\w./~*{},+-]{1,80}$")
_OPERATORS = frozenset(("|", "||", "&&", "&", ";", ">", ">>", "<", "2>&1"))
_TOKENISH = re.compile(r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_.~%+-]{20,}")


def _load(name: str) -> types.ModuleType:
    spec = spec_from_file_location(f"replay72_{name}", HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def calls(paths: Sequence[str]) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Every Bash, Read and Grep call of the transcripts: ``(tool, input, cwd)``."""
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else "/"
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    ti = block.get("input")
                    if block.get("name") in TOOLS and isinstance(ti, dict):
                        yield str(block["name"]), ti, cwd


def skeleton(cmd: str) -> str:
    """The command with every word that is not a flag or a plain path replaced by ``_``."""
    out = []
    for w in cmd[:400].split():
        keep = (
            w in _OPERATORS
            or (w.startswith("-") and "=" not in w and len(w) < 24)
            or (_PATH_WORD.match(w) and not _TOKENISH.search(w))
        )
        out.append(w if keep else "_")
    return " ".join(out)


def judge(gh: Any, tool: str, ti: dict[str, Any], cwd: str) -> tuple[str, str, float]:
    """``(outcome, label, seconds)``. Outcome: ``none``, ``deny`` or ``override``."""
    start = time.perf_counter()
    d = gh._credential_check(tool, ti, cwd, {})
    took = time.perf_counter() - start
    if d is None:
        return "none", "", took
    kind = d.log[0] if d.log else ""
    outcome = "override" if kind == "credential-override" else "deny"
    ids = d.log[1] if d.log else []
    label = str(d.extra.get("what", ""))
    return (outcome if "credential-source" in ids else "git-url"), label, took


def patch(gh: Any) -> None:
    """The same code as the hook, with the git probes off and a name-only test of the file."""
    cg = gh._load("credential_guard")

    class NoGit(cg.Probe):  # type: ignore[name-defined, misc]
        def config_entries(self, cwd: str, git_dir: Any, scope: Sequence[str]) -> list:
            return []

        def cred_configs(self, cwd: str) -> list:
            return []

        def file_lines(self, path: str) -> list:
            return list(TYPICAL_LINES) if MODE["typical"] else []

    real = cg._source_label

    def by_name(path: str) -> str:
        label = real(path)
        base = os.path.basename(path)
        if not label and (base in cg._KEY_NAMES or base.endswith(cg._KEY_SUFFIXES)):
            return "a private key file"
        return label

    cg.Probe = NoGit
    cg.secret_source = lambda p: by_name(os.path.normpath(p))
    # strict: no file lines, so a grep that prints counts as a deny; typical: the made-up lines
    emit = cg.would_emit
    cg.would_emit = lambda *a, **k: emit(*a, **k) if MODE["typical"] else True
    load = gh._load
    gh._load = lambda name: cg if name == "credential_guard" else load(name)


def run(args: argparse.Namespace) -> dict[str, Any]:
    gh = _load("guard_hook")
    patch(gh)
    paths = sorted(
        {p for pat in args.patterns for p in glob.glob(os.path.expanduser(pat), recursive=True)}
    )
    seen: dict[tuple[str, str], tuple[str, str, str, float]] = {}  # strict, typical, label, seconds
    total: Counter = Counter()
    strict: Counter = Counter()
    typical: Counter = Counter()
    by_label: Counter = Counter()
    by_head: Counter = Counter()
    secs: list[float] = []
    private: list[dict[str, str]] = []
    for tool, ti, cwd in calls(paths):
        total[tool] += 1
        text = ti.get("command") if tool == "Bash" else ti.get("file_path") or ti.get("path")
        if not isinstance(text, str) or not text.strip():
            continue
        key = (tool, text[:20000])
        if key not in seen:
            MODE["typical"] = False
            out_s, label, took = judge(gh, tool, ti, cwd)
            out_t = out_s
            if out_s in ("deny", "override"):
                MODE["typical"] = True
                out_t = judge(gh, tool, ti, cwd)[0]
                MODE["typical"] = False
            seen[key] = (out_s, out_t, label, took)
            secs.append(took)
            if out_t in ("deny", "override") and args.private_dir:
                head = text.split()[0] if tool == "Bash" and text.split() else tool
                private.append(
                    {
                        "tool": tool,
                        "outcome": out_t,
                        "label": label,
                        "head": os.path.basename(head)[:40],
                        "skeleton": skeleton(text) if tool == "Bash" else os.path.basename(text),
                    }
                )
        out_s, out_t, label, _ = seen[key]
        if out_s in ("deny", "override"):
            strict[(tool, out_s)] += 1
        if out_t in ("deny", "override"):
            typical[(tool, out_t)] += 1
            by_label[label] += 1
            if tool == "Bash":
                by_head[os.path.basename(text.split()[0])[:20] if text.split() else ""] += 1
    n_calls = sum(total.values())
    report: dict[str, Any] = {
        "transcripts": len(paths),
        "calls": dict(total),
        "unique": dict(Counter(t for t, _ in seen)),
        "would_deny_strict": {f"{t}:{o}": n for (t, o), n in sorted(strict.items())},
        "would_deny_typical": {f"{t}:{o}": n for (t, o), n in sorted(typical.items())},
        "would_deny_strict_total": sum(n for (_t, o), n in strict.items() if o == "deny"),
        "would_deny_typical_total": sum(n for (_t, o), n in typical.items() if o == "deny"),
        "would_override_total": sum(n for (_t, o), n in typical.items() if o == "override"),
        "typical_share_of_calls": round(
            sum(n for (_t, o), n in typical.items() if o == "deny") / max(n_calls, 1), 5
        ),
        "unique_would_deny_typical": sum(1 for v in seen.values() if v[1] == "deny"),
        "by_label_typical": dict(by_label.most_common()),
        "bash_first_word_typical": dict(by_head.most_common(12)),
        "check_ms": {
            "p50": round(statistics.median(secs) * 1000, 3) if secs else 0.0,
            "p99": round(sorted(secs)[max(int(len(secs) * 0.99) - 1, 0)] * 1000, 3)
            if secs
            else 0.0,
            "max": round(max(secs) * 1000, 3) if secs else 0.0,
        },
    }
    if args.private_dir:
        folder = Path(args.private_dir)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        out = folder / "would_deny.jsonl"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in private:
                fh.write(json.dumps(row) + "\n")
        os.chmod(out, 0o600)
        report["private_rows"] = len(private)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("patterns", nargs="+", help="transcript globs (JSON lines)")
    ap.add_argument(
        "--private-dir", help="private folder for the skeleton of each would-deny command"
    )
    args = ap.parse_args(argv)
    print(json.dumps(run(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
