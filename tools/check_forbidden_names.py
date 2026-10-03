# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a tracked file, a given path or the git history holds a forbidden name.

The patterns are case-insensitive Python regexes, one per line. Lines that
start with "#" and blank lines are ignored. The real list is private, so the
repository does not hold it. The script takes the list from the first source
that is set:

1. ``--names-file PATH``.
2. The environment variable ``NOBLIVION_FORBIDDEN_NAMES``: the patterns,
   one per line. CI sets it from a repository secret.
3. The environment variable ``NOBLIVION_FORBIDDEN_NAMES_FILE``: the path of a
   list file.
4. ``tools/forbidden_names.local.txt``: a local list. Git ignores this file.
5. ``tools/forbidden_names.example.txt``: generic examples only. The script
   prints a notice when it falls back to this file, because the real names are
   then not checked. This happens in a pull request from a fork, where CI gets
   no secrets.

When the list comes from a private source (1 to 4), a finding names the
pattern number, not the matched text, so a CI log does not show the list.

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
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ENV_PATTERNS = "NOBLIVION_FORBIDDEN_NAMES"
ENV_FILE = "NOBLIVION_FORBIDDEN_NAMES_FILE"
LOCAL_FILE = "tools/forbidden_names.local.txt"
EXAMPLE_FILE = "tools/forbidden_names.example.txt"
# The list files hold the names they forbid, so no scan reads them.
EXCLUDED = frozenset({LOCAL_FILE, EXAMPLE_FILE})
FALLBACK_NOTICE = (
    f"NOTICE: {ENV_PATTERNS} and {ENV_FILE} are not set and there is no {LOCAL_FILE}. "
    f"The scan uses the generic examples in {EXAMPLE_FILE} only, so the real "
    "forbidden names are NOT checked. This is expected in a pull request from a fork."
)


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def parse_patterns(text: str, source: str, private: bool) -> list[re.Pattern[str]]:
    patterns = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            shown = "" if private else f" {line!r}"
            raise SystemExit(f"{source}:{lineno}: bad regex{shown}: {exc}") from exc
    if not patterns:
        raise SystemExit(f"{source}: no patterns found")
    return patterns


def load_patterns(path: Path, private: bool = True) -> list[re.Pattern[str]]:
    return parse_patterns(path.read_text(encoding="utf-8"), str(path), private)


def resolve_patterns(
    root: Path, names_file: str | None, environ: dict[str, str] | None = None
) -> tuple[list[re.Pattern[str]], bool]:
    """Return the patterns, and True when they come from a private source."""
    env = os.environ if environ is None else environ
    if names_file:
        return load_patterns(Path(names_file)), True
    inline = env.get(ENV_PATTERNS, "")
    if inline.strip():
        return parse_patterns(inline, f"${ENV_PATTERNS}", True), True
    from_env = env.get(ENV_FILE, "").strip()
    if from_env:
        return load_patterns(Path(from_env)), True
    local = root / LOCAL_FILE
    if local.is_file():
        return load_patterns(local), True
    print(FALLBACK_NOTICE, file=sys.stderr)
    if env.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Forbidden names::{FALLBACK_NOTICE}")
    return load_patterns(root / EXAMPLE_FILE, private=False), False


def first_hit(text: str, patterns: Iterable[re.Pattern[str]], private: bool = False) -> str | None:
    for number, pattern in enumerate(patterns, 1):
        match = pattern.search(text)
        if match:
            return f"pattern #{number}" if private else repr(match.group(0))
    return None


def is_excluded(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").removeprefix("./") in EXCLUDED


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def scan_file(
    display: str, path: Path, patterns: list[re.Pattern[str]], private: bool = False
) -> Iterator[str]:
    hit = first_hit(display, patterns, private)
    if hit:
        yield f"{display}:0: forbidden name {hit} in the file path"
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return
    if b"\0" in data:
        return  # binary file
    for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        hit = first_hit(line, patterns, private)
        if hit:
            yield f"{display}:{lineno}: forbidden name {hit}"


def scan_paths(
    root: Path, paths: list[str], patterns: list[re.Pattern[str]], private: bool = False
) -> list[str]:
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
        findings.extend(scan_file(rel, path, patterns, private))
    return findings


def scan_history(root: Path, patterns: list[re.Pattern[str]], private: bool = False) -> list[str]:
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
        hit = first_hit(line, patterns, private)
        if hit:
            if current_file is None:
                where = "(commit message)"
            else:
                where = current_file or "(diff header)"
            findings.append(f"commit {commit} {where}: forbidden name {hit}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="paths to scan (default: all tracked files)")
    parser.add_argument("--history", action="store_true", help="scan `git log -p --all`")
    parser.add_argument(
        "--names-file", help=f"pattern list file (default: ${ENV_PATTERNS}, see the docstring)"
    )
    args = parser.parse_args(argv)

    root = repo_root()
    patterns, private = resolve_patterns(root, args.names_file)

    if args.history:
        if args.paths:
            parser.error("--history takes no paths")
        findings = scan_history(root, patterns, private)
    elif args.paths:
        findings = scan_paths(root, args.paths, patterns, private)
    else:
        files = [root / p for p in tracked_files(root)]
        findings = scan_paths(root, [str(p) for p in files], patterns, private)

    for finding in findings:
        print(finding)
    if findings:
        print(f"check_forbidden_names: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
