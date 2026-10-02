# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a tracked file, a given path or the git history holds a forbidden name.

The patterns live in tools/forbidden_names.txt: one case-insensitive Python regex
per line. Lines that start with "#" and blank lines are ignored. The list file is
excluded from every scan, because it must hold the names it forbids.

Usage:
    python tools/check_forbidden_names.py            # scan all tracked files
    python tools/check_forbidden_names.py PATH ...   # scan the given paths
    python tools/check_forbidden_names.py --history  # scan `git log -p --all`

The history mode scans commit messages and patch content. It does not scan the
author or committer identity of a commit.

Exit status: 0 when clean, 1 when a name is found, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

LIST_FILE = "tools/forbidden_names.txt"
EXCLUDED = frozenset({LIST_FILE})


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def load_patterns(path: Path) -> list[re.Pattern[str]]:
    patterns = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            raise SystemExit(f"{path}:{lineno}: bad regex {line!r}: {exc}") from exc
    if not patterns:
        raise SystemExit(f"{path}: no patterns found")
    return patterns


def first_hit(text: str, patterns: Iterable[re.Pattern[str]]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def is_excluded(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").removeprefix("./") in EXCLUDED


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def scan_file(display: str, path: Path, patterns: list[re.Pattern[str]]) -> Iterator[str]:
    hit = first_hit(display, patterns)
    if hit:
        yield f"{display}:0: forbidden name {hit!r} in the file path"
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return
    if b"\0" in data:
        return  # binary file
    for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        hit = first_hit(line, patterns)
        if hit:
            yield f"{display}:{lineno}: forbidden name {hit!r}"


def scan_paths(root: Path, paths: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    findings: list[str] = []
    for given in paths:
        path = Path(given)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = given
        if is_excluded(rel):
            continue
        findings.extend(scan_file(rel, path, patterns))
    return findings


def scan_history(root: Path, patterns: list[re.Pattern[str]]) -> list[str]:
    cmd = [
        "git",
        "log",
        "-p",
        "--all",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--format=%x00commit %H%n%B",
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, check=True)
    findings: list[str] = []
    commit = "?"
    current_file: str | None = None
    lineno = 0
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("\0commit "):
            commit = line[len("\0commit ") :][:12]
            current_file = None
            lineno = 0
            continue
        if line.startswith("diff --git "):
            current_file = ""
            continue
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            current_file = line[6:]
        lineno += 1
        if current_file is not None and is_excluded(current_file):
            continue
        hit = first_hit(line, patterns)
        if hit:
            if current_file is None:
                where = "(commit message)"
            else:
                where = current_file or "(diff header)"
            findings.append(f"commit {commit} {where}: forbidden name {hit!r}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="paths to scan (default: all tracked files)")
    parser.add_argument("--history", action="store_true", help="scan `git log -p --all`")
    parser.add_argument("--names-file", help=f"pattern list (default: {LIST_FILE})")
    args = parser.parse_args(argv)

    root = repo_root()
    names_file = Path(args.names_file) if args.names_file else root / LIST_FILE
    patterns = load_patterns(names_file)

    if args.history:
        if args.paths:
            parser.error("--history takes no paths")
        findings = scan_history(root, patterns)
    elif args.paths:
        findings = scan_paths(root, args.paths, patterns)
    else:
        files = [root / p for p in tracked_files(root)]
        findings = scan_paths(root, [str(p) for p in files], patterns)

    for finding in findings:
        print(finding)
    if findings:
        print(f"check_forbidden_names: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
