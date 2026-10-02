<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Contributing

## Gates

Every push and pull request runs these gates in CI. Run them locally first.

| Gate | Command |
| --- | --- |
| Lint | `uv run ruff check .` and `uv run ruff format --check .` |
| Tests | `uv run pytest -q` |
| Secrets | `gitleaks git --config .gitleaks.toml --redact .` |
| Forbidden names | `python3 tools/check_forbidden_names.py` and `--history` |
| License headers | `python3 tools/check_spdx.py` |

Install the pre-commit hooks once to run most gates on every commit:

```sh
uv sync
uv tool install pre-commit
pre-commit install
```

## License header rule

The project license is AGPL-3.0-or-later. Every `*.py`, `*.sh`, `*.toml`,
`*.yml`, `*.yaml` and `*.md` file carries this header in its first 5 lines:

```text
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Markdown files use an HTML comment:

```text
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
```

## No internal names

Do not commit internal host names, network addresses, user paths, organisation
names or ticket keys from any private system. Use fictional names in code, tests
and fixtures. `tools/forbidden_names.txt` holds the patterns the scanner
rejects. Add a pattern there when you find a new name that must not appear.
