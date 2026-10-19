# SPDX-License-Identifier: AGPL-3.0-or-later
"""The closed rewrite set of the guard hook (``hooks/guard_rewrite.py``, NOBLIVION-122).

What these tests hold:

1. The grammar is closed: only ``add-flag <flag> after <prefix>`` with a pair of
   the table parses; anything else is a problem. There is no pipe form (the
   matcher splits at pipes, see the module text).
2. A fixture of more than 50 commands: each rewrite is exact, or None when the command is
   not exactly the shape the rewrite knows (no guess).
3. Every rewrite is idempotent: it never applies to its own result.
4. A rewrite never changes the exit status of a command, passing or failing. Run
   with stub programs in a real shell.
5. The write-time check (``problems``) judges a memory by its examples.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hookload import load_hook

rw = load_hook("guard_rewrite", "guard_rewrite_t")

Q = "add-flag -q after pytest"
ONE = "add-flag --oneline after git log"

# (command, spec, expected rewrite or None)
FIXTURE = [
    # add-flag: the flag goes right after the prefix; the rest is untouched
    ("pytest", Q, "pytest -q"),
    ("pytest tests/", Q, "pytest -q tests/"),
    ("pytest  tests/ -x", Q, "pytest -q  tests/ -x"),
    ("pytest tests/ 2>&1 | tail -5", Q, "pytest -q tests/ 2>&1 | tail -5"),
    ("pytest -k 'a or b'", Q, "pytest -q -k 'a or b'"),
    ("python -m pytest tests", "add-flag -q after python -m pytest", "python -m pytest -q tests"),
    ("uv run pytest tests", "add-flag -q after uv run pytest", "uv run pytest -q tests"),
    ("git log", ONE, "git log --oneline"),
    ("git log main..HEAD", ONE, "git log --oneline main..HEAD"),
    ("git log -5 | cat", ONE, None),
    ("git status", "add-flag -sb after git status", "git status -sb"),
    # add-flag: refused, the command is not exactly the shape (or has the flag)
    ("pytest -q", Q, None),
    ("pytest -v tests", Q, None),
    ("pytest -vv", Q, None),
    ("pytest -xq", Q, None),
    ("pytest --collect-only", Q, None),
    ("cd x && pytest", Q, None),
    ("FOO=1 pytest", Q, None),
    (" pytest", Q, None),
    ("echo pytest", Q, None),
    ("bash -c 'pytest'", Q, None),
    ("pytest-xdist-report", Q, None),
    ("pytest $(cat args)", Q, None),
    ("pytest # note", Q, None),
    ("cat <<EOF\npytest\nEOF", Q, None),
    ("git log --oneline", ONE, None),
    ("git log --format=%h", ONE, None),
    ("git log -p", ONE, None),
    ("git status -s", "add-flag -sb after git status", None),
    # the rest of the line: nothing, 2>&1, and one window stage are fine
    ("git log 2>&1", ONE, "git log --oneline 2>&1"),
    ("pytest tests 2>&1 | tail -5", Q, "pytest -q tests 2>&1 | tail -5"),
    ("pytest tests | tail -n 5", Q, "pytest -q tests | tail -n 5"),
    ("git log | head -20", ONE, None),  # only a test runner may feed a tail
    ("git log | tail -n 5", ONE, None),
    ("git log | cat", ONE, None),
    ("git status 2>&1 | head -5", "add-flag -sb after git status", None),
    ("pytest tests | head -5", Q, None),
    # anything longer is left alone: a later stage may read the output
    ("git log | grep Author", ONE, None),
    ("git log main..HEAD | wc -l", ONE, None),
    ("git log | head -3 | sort", ONE, None),
    ("git log > out.txt", ONE, None),
    ("git log && ls", ONE, None),
    ("git log; ls", ONE, None),
    ("git log &", ONE, None),
    ("git log $(echo x)", ONE, None),
    ("git log | tail -f", ONE, None),
    ("pytest > out.txt", Q, None),
    ("pytest tests && pytest other", Q, None),
    ("pytest -k 'a|b'", Q, None),
    ("git push origin main", ONE, None),
    ("git branch -D x", ONE, None),
    ("ls -la", ONE, None),
    ("", ONE, None),
    ("   ", ONE, None),
]


@pytest.mark.parametrize("command,spec,want", FIXTURE)
def test_the_fixture(command, spec, want):
    assert rw.apply(command, spec) == want


@pytest.mark.parametrize("command,spec,want", [f for f in FIXTURE if f[2]])
def test_every_rewrite_is_idempotent(command, spec, want):
    assert rw.apply(want, spec) is None
    assert rw.apply(rw.apply(command, spec), spec) is None


def test_the_fixture_has_at_least_30_commands_and_both_outcomes():
    assert len(FIXTURE) >= 30
    assert sum(1 for f in FIXTURE if f[2]) >= 10
    assert sum(1 for f in FIXTURE if not f[2]) >= 20


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "append-head 20",
        "add-flag -20",
        "add-flag -20 after git log",  # a truncating flag is not in the table
        "add-flag -n after git log",
        "add-flag -q",
        "add-flag -q after",
        "add-flag -q after rm",
        "add-flag --force after git push",
        "add-flag -q after git log",  # a pair that is not in the table
        "add-flag -q after pytest; rm -rf x",
        "sh -c 'x'",
        "head 20",
        None,
        7,
    ],
)
def test_a_spec_outside_the_grammar_never_applies(spec):
    assert rw.apply("pytest tests", spec) is None
    assert rw.apply("git log", spec) is None
    assert rw.parse(spec)[2]


def test_the_grammar_gives_the_kind_and_the_argument():
    assert rw.parse(Q) == ("flag", (("pytest",), "-q"), "")
    assert rw.parse(ONE) == ("flag", (("git", "log"), "--oneline"), "")


def _stubs(tmp_path: Path) -> dict:
    """Stub programs: ``pytest`` exits 3 and ``git`` prints 100 lines and exits
    with $GIT_RC. ``ls`` is the real one."""
    for name, body in (
        ("pytest", "#!/bin/sh\necho ran\nexit 3\n"),
        (
            "git",
            "#!/bin/sh\ni=0\nwhile [ $i -lt 100 ]; do echo line$i; i=$((i+1)); done\n"
            'exit "${GIT_RC:-0}"\n',
        ),
    ):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def _rc(command: str, env: dict) -> int:
    return subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, timeout=30
    ).returncode


def test_a_rewrite_never_changes_the_status_of_a_command(tmp_path):
    env = _stubs(tmp_path)
    for command, spec in (("git log", ONE), ("git log 2>&1", ONE), ("pytest tests", Q)):
        new = rw.apply(command, spec)
        assert new is not None and new != command
        for rc in ("0", "5"):
            e = {**env, "GIT_RC": rc}
            assert _rc(command, e) == _rc(new, e), (command, rc)
    assert _rc("pytest tests", env) == 3  # the stub fails; the rewrite keeps the 3


def _fields(**over):
    f = {
        "violates": r"^pytest\b(?!.*\s-q\b)",
        "example_repeat": "pytest tests",
        "example_ok": "pytest -q tests",
        "complies": r"^pytest\s+-q\b",
        "rewrite": Q,
    }
    f.update(over)
    return f


def test_problems_accepts_a_rewrite_that_its_examples_prove():
    assert rw.problems(_fields()) == []
    assert rw.problems(_fields(rewrite="")) == []


@pytest.mark.parametrize(
    "over,word",
    [
        ({"rewrite": "add-flag -q after rm"}, "table"),
        ({"violates": ""}, "only with violates"),
        ({"example_repeat": "make test"}, "does not apply to example_repeat"),
        ({"violates": r"^pytest\b"}, "still matches violates"),
        ({"complies": r"^pytest\s+--tb"}, "does not match complies"),
        ({"example_ok": "pytest tests"}, "applies to example_ok"),
    ],
)
def test_problems_names_what_is_wrong(over, word):
    got = rw.problems(_fields(**over))
    assert got and any(word in p for p in got), got
