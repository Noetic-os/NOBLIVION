<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Contributing

Thank you for your help. This file tells you how to set up the project,
which checks a change must pass, and which rules apply to every file.

## Set up

You need `python3` 3.10 or newer, [uv](https://docs.astral.sh/uv/) and
[gitleaks](https://github.com/gitleaks/gitleaks).

1. Clone the repository.
2. Install the development environment:

   ```sh
   uv sync
   ```

3. Install the pre-commit hooks. They run most gates on every commit:

   ```sh
   uv tool install pre-commit
   pre-commit install
   ```

## Code layout

| Folder | What it holds | Python |
| --- | --- | --- |
| `hooks/` | the Claude Code hooks | 3.9 or newer, standard library only |
| `mcp/` | the MCP server | 3.9 or newer, standard library only |
| `src/noblivion/` | the store and the `noblivion` command | 3.10 or newer, in the store venv |
| `tests/` | the tests | the development environment |
| `tools/` | the gate scripts | standard library only |
| `docs/` | the user documentation and the design document | |

The hooks run on every prompt and every tool call. Keep them fast. Do not
import a third-party package in `hooks/` or `mcp/`.

## Gates

Every push and every pull request runs these gates in CI. Run them on your
machine first.

| Gate | Command |
| --- | --- |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format --check .` |
| Tests | `uv run pytest -q` |
| Secrets | `gitleaks git --config .gitleaks.toml --redact .` |
| Forbidden names, tracked files | `python3 tools/check_forbidden_names.py` |
| Forbidden names, full history | `python3 tools/check_forbidden_names.py --history` |
| License headers | `python3 tools/check_spdx.py` |
| CHANGELOG entry, no owner placeholder | `python3 tools/check_changelog.py --base origin/main` |

A pull request merges only when all gates pass.

CI runs the tests on Linux with Python 3.10 and 3.12, and on macOS with
3.12. A second job runs the test files that do not import the package
(the hooks, MCP and Codex adapter tests) on Python 3.9, on Linux and macOS.
Keep code in `hooks/`, `mcp/` and `codex/` valid for 3.9: no `match`, no
`zip(strict=...)`, no `X | Y` type outside an annotation.

A pull request that changes `src/`, `hooks/`, `mcp/` or `codex/` must add a
line under `## Unreleased` in `CHANGELOG.md`. For a change that a user
cannot see, add the line `Changelog: none` to the commit message instead.

The test suite includes multi-process store tests. They are marked
`slow`. To skip them on your machine, run `uv run pytest -q -m "not slow"`.
CI always runs them.

## License header rule

The project license is AGPL-3.0-or-later. Every tracked `*.py`, `*.sh`,
`*.toml`, `*.yml`, `*.yaml` and `*.md` file carries this header in its
first 5 lines:

```text
# SPDX-License-Identifier: AGPL-3.0-or-later
```

A Markdown file uses an HTML comment:

```text
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
```

`tools/check_spdx.py` fails when a file has no header.

## No internal names

The repository is public. Do not commit internal host names, network
addresses, user paths, organisation names or ticket keys from a private
system. This rule applies to code, tests, fixtures, docs and commit
messages.

1. Use fictional names in code, tests and fixtures.
2. Use `Path.home()` or `~` for a home folder. Never write a literal user
   path.
3. Run `python3 tools/check_forbidden_names.py` before each push.
4. When you find a new name that must not appear, add a pattern to the
   private list (see below).

The real forbidden-names list is private, so the repository does not hold
it. The scanner reads the list from the first source that is set:

1. The environment variable `NOBLIVION_FORBIDDEN_NAMES`: the patterns, one
   case-insensitive Python regex per line. CI sets it from the repository
   secret of the same name.
2. The environment variable `NOBLIVION_FORBIDDEN_NAMES_FILE`: the path of a
   list file.
3. `tools/forbidden_names.local.txt`: a local list file. Git ignores it.
4. `tools/forbidden_names.example.txt`: generic examples only.

When the scanner falls back to the example file, it prints a notice: the
real names are then not checked. A pull request from a fork gets no secrets,
so its CI run uses the example file. A maintainer runs the full check after
the merge. When the list comes from a private source, a finding names the
pattern number, not the matched text.

The `--history` scan checks every commit, not only the last one. A name in
an old commit fails the gate too. Fix it before you push.

## Settings and docs

Each `NOBLIVION_*` environment variable must have a row in
[docs/configuration.md](docs/configuration.md). The test
`tests/test_docs_configuration.py` fails when a name in the code is missing
from that file, or when that file names a variable the code does not read.
When you add, rename or remove a setting, update the doc in the same pull
request.

Write docs in plain English: short sentences, active voice, one idea per
sentence, and numbered steps for procedures.
