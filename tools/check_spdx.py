# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a tracked source or text file has no SPDX license header.

Every tracked *.py, *.sh, *.toml, *.yml, *.yaml and *.md file must carry the
header in its first 5 lines, written in the comment syntax of the file type:

    # SPDX-License-Identifier: AGPL-3.0-or-later        (py, sh, toml, yml, yaml)
    <!-- SPDX-License-Identifier: AGPL-3.0-or-later --> (md)

A Markdown file that starts with YAML front matter (``---``, as a Claude Code
skill must) may carry the ``#`` form inside the front matter instead.

Usage:
    python tools/check_spdx.py            # check all tracked files
    python tools/check_spdx.py PATH ...   # check the given paths

Exit status: 0 when every file has the header, 1 when a file does not.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LICENSE_ID = "AGPL-3.0-or-later"
HEAD_LINES = 5
_ID = re.escape(f"SPDX-License-Identifier: {LICENSE_ID}")
_HASH = re.compile(rf"^\s*#\s*{_ID}\s*$")
_HTML = re.compile(rf"^\s*<!--\s*{_ID}\s*-->\s*$")
RULES: dict[str, re.Pattern[str]] = {
    ".py": _HASH,
    ".sh": _HASH,
    ".toml": _HASH,
    ".yml": _HASH,
    ".yaml": _HASH,
    ".md": _HTML,
}
EXCLUDED_NAMES = frozenset({"LICENSE"})


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [root / p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def has_header(path: Path, rule: re.Pattern[str]) -> bool:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index in range(HEAD_LINES):
            line = handle.readline()
            if not line:
                break
            text = line.rstrip("\r\n")
            if index == 0 and rule is _HTML and text == "---":
                rule = _HASH  # YAML front matter: a YAML comment
            if rule.match(text):
                return True
    return False


def display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def offenders(paths: list[Path]) -> list[str]:
    bad = []
    for path in paths:
        if path.name in EXCLUDED_NAMES or not path.is_file():
            continue
        rule = RULES.get(path.suffix.lower())
        if rule is None:
            continue
        if not has_header(path, rule):
            bad.append(display(path))
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="paths to check (default: all tracked files)")
    args = parser.parse_args(argv)

    if args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        paths = tracked_files(repo_root())

    bad = offenders(paths)
    for name in bad:
        print(
            f"{name}: missing 'SPDX-License-Identifier: {LICENSE_ID}' in the first "
            f"{HEAD_LINES} lines"
        )
    if bad:
        print(f"check_spdx: {len(bad)} file(s) without the header", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
