# SPDX-License-Identifier: AGPL-3.0-or-later
"""The closed rewrite set of the guard hook (``hooks/guard_rewrite.py``, NOBLIVION-122).

What these tests hold:

1. The grammar is closed: only ``add-flag <flag> after <prefix>`` with a pair of
   the table parses; anything else is a problem. There is no pipe form (the
   matcher splits at pipes, see the module text).
2. A fixture of more than 50 commands: each rewrite is exact, or None when the command is
   not exactly the shape the rewrite knows (no guess). Since NOBLIVION-126 the rewrite
   acts on each simple command of a list or a pipeline, after the wrappers of the deny
   path, and ``python -m <prog>`` counts as ``<prog>``.
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
    ("echo pytest", Q, None),
    ("bash -c 'pytest'", Q, None),
    ("pytest-xdist-report", Q, None),
    ("pytest $(cat args)", Q, None),
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
    ("git log $(echo x)", ONE, None),
    ("git log | tail -f", ONE, None),
    ("pytest > out.txt", Q, None),
    ("pytest -k 'a|b'", Q, None),
    # NOBLIVION-126: each matching simple command of a list or a pipeline, after
    # the wrappers of the deny path; the other bytes stay as they are
    ("cd x && pytest", Q, "cd x && pytest -q"),
    ("cd ~/proj && pytest tests/unit", Q, "cd ~/proj && pytest -q tests/unit"),
    ("make build; pytest tests", Q, "make build; pytest -q tests"),
    ("make build\npytest tests", Q, "make build\npytest -q tests"),
    ("make || pytest tests", Q, "make || pytest -q tests"),
    ("ls | pytest tests", Q, "ls | pytest -q tests"),
    ("pytest tests | tail -5 && echo done", Q, "pytest -q tests | tail -5 && echo done"),
    ("cd x && pytest tests 2>&1 |& tail -5", Q, "cd x && pytest -q tests 2>&1 |& tail -5"),
    ("pytest tests && pytest other", Q, "pytest -q tests && pytest -q other"),
    ("pytest -v a && pytest b", Q, "pytest -v a && pytest -q b"),
    ("cd  'my dir'  &&  pytest  t", Q, "cd  'my dir'  &&  pytest -q  t"),
    ("FOO=1 pytest", Q, "FOO=1 pytest -q"),
    ("env FOO=1 timeout 60 pytest tests", Q, "env FOO=1 timeout 60 pytest -q tests"),
    ("timeout -s KILL 5m pytest", Q, "timeout -s KILL 5m pytest -q"),
    ("sudo -u ci nice -n 5 pytest", Q, "sudo -u ci nice -n 5 pytest -q"),
    (" pytest", Q, " pytest -q"),
    ("pytest # note", Q, "pytest -q # note"),
    ("pytest  # pytest -v here", Q, "pytest -q  # pytest -v here"),
    ("git log && ls", ONE, "git log --oneline && ls"),
    ("git log; ls", ONE, "git log --oneline; ls"),
    ("git log &", ONE, "git log --oneline &"),
    ("cd x && git log 2>&1", ONE, "cd x && git log --oneline 2>&1"),
    # NOBLIVION-126: python -m <prog>, also by path, is <prog>
    ("python3 -m pytest tests", Q, "python3 -m pytest -q tests"),
    (".venv/bin/python -m pytest -x", Q, ".venv/bin/python -m pytest -q -x"),
    ("/usr/bin/python3.12 -m pytest", Q, "/usr/bin/python3.12 -m pytest -q"),
    ("cd x && ~/venv/bin/python3 -m pytest t", Q, "cd x && ~/venv/bin/python3 -m pytest -q t"),
    ("pytest tests", "add-flag -q after python3 -m pytest", "pytest -q tests"),
    (
        "python -m pytest t | tail -3",
        "add-flag -q after python -m pytest",
        "python -m pytest -q t | tail -3",
    ),
    ("cd x && uv run pytest", "add-flag -q after uv run pytest", "cd x && uv run pytest -q"),
    # NOBLIVION-155: uv run <prog> and a venv program are <prog>
    ("uv run pytest", Q, "uv run pytest -q"),
    ("uv run --with rich pytest -x", Q, "uv run --with rich pytest -q -x"),
    ("uv run python3 -m pytest t", Q, "uv run python3 -m pytest -q t"),
    (".venv/bin/pytest tests", Q, ".venv/bin/pytest -q tests"),
    ("uv run pytest -q", Q, None),
    ("uv run --no-such pytest", Q, "uv run --no-such pytest -q"),
    ("uv run ruff check", Q, None),
    ("uv pip install pytest", Q, None),
    # NOBLIVION-126: still left alone
    ("python3 -m pytest -q", Q, None),
    ("python3 -c pytest", Q, None),
    ("python3 -m pytest_x", Q, None),
    ("python3 -u -m pytest", Q, None),
    ("node -m pytest", Q, None),
    ("'pytest' tests", Q, None),
    ("cd x && pytest > log", Q, None),
    ("cd x && pytest | grep FAIL", Q, None),
    ("pytest | tail -5 | grep x", Q, None),
    ("pytest | tail -f", Q, None),
    ("pytest a > log; pytest b", Q, None),
    ("cd x && pytest $ARGS", Q, None),
    ('cd x && pytest "$T"', Q, None),
    ("(cd x && pytest)", Q, None),
    ("{ pytest; } | grep x", Q, None),
    ("if pytest; then echo ok; fi", Q, None),
    ("for f in a b; do pytest; done > log", Q, None),
    ("while true; do pytest; done", Q, None),
    ("x=$(pytest)", Q, None),
    ("echo `pytest`", Q, None),
    ("cd x && bash -c 'pytest tests'", Q, None),
    ("ssh example-host 'pytest tests'", Q, None),
    ("cd x && eval 'pytest tests'", Q, None),
    ("echo 'cd x && pytest tests'", Q, None),
    ("ls # && pytest tests", Q, None),
    ("cat <<EOF\npytest\nEOF\npytest tests", Q, None),
    ("cd x && \\\npytest", Q, None),
    ("git log && ls | head", ONE, "git log --oneline && ls | head"),
    ("cd x && git log | tail -5", ONE, None),
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


@pytest.mark.parametrize("command,spec,want", [f for f in FIXTURE if f[2]])
def test_a_rewrite_only_inserts_the_flag_and_keeps_every_other_byte(command, spec, want):
    # NOBLIVION-126: the other commands of the list stay byte for byte. Each
    # " <flag>" that the rewrite added is removed again from the left: the
    # original command must come back, and nothing else may differ.
    flag = " " + rw.parse(spec)[1][1]
    added = want.count(flag) - command.count(flag)
    assert added >= 1 and len(want) == len(command) + added * len(flag)
    rest = want
    for _ in range(added):  # cut each insertion where the two texts part
        k = next(x for x in range(len(rest)) if x >= len(command) or rest[x] != command[x])
        k = rest.rfind(flag, 0, k + len(flag))
        assert k >= 0, (command, rest)
        rest = rest[:k] + rest[k + len(flag) :]
    assert rest == command


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
    for command, spec in (
        ("git log", ONE),
        ("git log 2>&1", ONE),
        ("pytest tests", Q),
        ("cd . && pytest tests", Q),  # NOBLIVION-126
        ("true; pytest tests | tail -1", Q),
    ):
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
