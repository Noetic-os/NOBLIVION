# SPDX-License-Identifier: AGPL-3.0-or-later
"""Landing rules are guard-only unless the prompt names the action (NOBLIVION-164).

A rule for landing work (worktree, push, pull request, merge, the full test
suite) cost tokens in every later call of a task that never lands code, and
the model followed it whether the task needed it or not. The guard shows the
rule at the command. The prompt shows it only when the prompt names a landing
action, because that task needs the lesson before the command.

What these tests hold:

1. CLASSIFICATION. A note is a landing note when its scope is ``tool`` and at
   least half of its command triggers are landing commands. A ``phrase:``,
   ``glob:`` or ``tool:`` trigger does not count. Other scopes never are.
2. PROMPT. A prompt that names no landing action shows no landing row, and the
   next row takes its place. A prompt that names one keeps every row.
3. SWITCH. ``NOBLIVION_RECALL_GUARD_ONLY_LANDING=0`` gives the rows before.
4. GUARD. The guard shows each landing note at its command in six spellings,
   built from the real guard table of the memory files.

Fictional notes only. Every test runs on a tmp data dir and cache folder, and
the store call is replaced.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from hookload import load_hook, load_hook_with_config
from recall_helpers import INDEX_FLOOR_OFF, hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook and the real guard hook on tmp memory files, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_landing")
corpus = hook._CORPUS
gh = load_hook_with_config("guard_hook", {}, "guard_hook_t_landing")
gt = load_hook("guard_table", "guard_table_t_landing")

SID = "landing-test-session"
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")


# 1. classification -----------------------------------------------------------


@pytest.mark.parametrize(
    "trigger",
    [
        "git push",
        "git push origin main",
        "Git Worktree add",
        "git worktree",
        "git merge --no-ff",
        "gh pr",
        "gh pr create --fill",
        "gh pr merge 12",
        "tools/team-merge",
        "python3 tools/merge_gate.py",
        "pytest full suite",
    ],
)
def test_a_landing_command_is_one(trigger):
    assert corpus.landing_command(trigger)


@pytest.mark.parametrize(
    "trigger",
    [
        "pytest -q",
        "pytest",
        "git pull",
        "git branch --merged",
        "gh api",
        "gh run list",
        "docker ps",
        "",
    ],
)
def test_other_commands_are_not_landing_commands(trigger):
    assert not corpus.landing_command(trigger)


def test_a_landing_note_has_scope_tool_and_mostly_landing_triggers():
    assert corpus.landing_note("tool", ["git push", "gh pr create"])
    assert corpus.landing_note("tool", ["git push", "docker ps"])  # half
    assert not corpus.landing_note("tool", ["git push", "docker ps", "ls"])  # a third
    assert not corpus.landing_note("tool", ["pytest -q"])
    assert not corpus.landing_note("tool", [])
    assert not corpus.landing_note("stop", ["git push"])
    assert not corpus.landing_note("", ["git push"])
    # A prefixed trigger is not a command.
    assert not corpus.landing_note("tool", ["phrase:git push", "glob:**/merge/*", "tool:Bash"])
    assert corpus.landing_note("tool", ["git push", "phrase:ship it", "glob:*.md"])


@pytest.mark.parametrize(
    "prompt",
    [
        "Merge all three now",
        "push the fix",
        "Open a pull request for this",
        "PR 2431 is green",
        "cut the next release",
        "deploy it",
        "rebase onto main",
        "make a worktree",
        "run the whole test suite",
        "Run the test suite",
    ],
)
def test_a_prompt_that_names_a_landing_action(prompt):
    assert corpus.prompt_names_landing(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "fix the retry backoff",
        "why does parse_duration fail",
        "the pushover app crashes",
        "express",
    ],
)
def test_a_prompt_that_names_none(prompt):
    assert not corpus.prompt_names_landing(prompt)


# 2. the prompt rows ----------------------------------------------------------

# The store's answer, in rank order: (title, score).
ROWS: list[tuple[str, float]] = [
    ("feedback_land_push", 0.80),  # landing: git push
    ("feedback_land_tree", 0.79),  # landing: git worktree add
    ("feedback_pytest_quiet", 0.78),  # scope tool, not landing
    ("feedback_mixed_gh", 0.77),  # scope tool, a third of the commands
    ("feedback_plain", 0.76),  # no scope
    ("feedback_stop_pr", 0.75),  # scope stop, gh pr trigger
    ("feedback_more", 0.74),
]
LANDING = ("feedback_land_push", "feedback_land_tree")


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _note(folder: Path, base: str, rule: str, scope: str = "", triggers: tuple[str, ...] = ()):
    fm = ["---", f"name: {base}", f"description: about {base}", "metadata:", "  type: feedback"]
    fm.append(f"rule: {rule}")
    fm.append("apply: " + json.dumps(f"the exact command for {base}"))
    if scope:
        fm.append(f"scope: {scope}")
    if triggers:
        fm.append("triggers: [" + ", ".join(triggers) + "]")
    fm += ["---", "", f"BODY-OF-{base}.", ""]
    (folder / f"{base}.md").write_text("\n".join(fm))


@pytest.fixture
def memory(tmp_path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir()
    _note(
        folder,
        "feedback_land_push",
        "Run git push only from a worktree branch.",
        "tool",
        ("git push", "phrase:push the fix"),
    )
    _note(
        folder,
        "feedback_land_tree",
        "Run git worktree add from the remote head before the first edit.",
        "tool",
        ("git worktree add", "git worktree"),
    )
    _note(folder, "feedback_pytest_quiet", "Run pytest with -q.", "tool", ("pytest",))
    _note(
        folder,
        "feedback_mixed_gh",
        "Page gh api output.",
        "tool",
        ("gh api", "gh pr view", "docker ps"),
    )
    _note(folder, "feedback_plain", "Keep the diff small.")
    _note(folder, "feedback_stop_pr", "Name the ticket in the PR.", "stop", ("gh pr create",))
    _note(folder, "feedback_more", "Say what you checked.")
    return folder


@pytest.fixture
def daemon(monkeypatch):
    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=t, summary="s", score=sc)
            for pos, (t, sc) in enumerate(ROWS, start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)


def _env(tmp_path, memory, **extra: str) -> dict[str, str]:
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_RECALL_RULES_ONLY="1",
        **INDEX_FLOOR_OFF,
        **{hook.MEMORY_DIR_ENV: str(memory), hook.INDEX_RULE_ROWS_ENV: "1"},
    )
    env.update(extra)
    return env


def _serve(env: dict[str, str], prompt: str) -> str:
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt=prompt))
    out = io.StringIO()
    hook.run(payload, out, dict(env))
    return out.getvalue().rstrip("\n")


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in text.splitlines() if ROW_RE.match(ln)]


def _status(tmp_path) -> str:
    line = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]
    return line.rsplit(" ", 1)[1]


def test_a_task_that_names_no_landing_action_shows_no_landing_row(tmp_path, memory, daemon):
    text = _serve(_env(tmp_path, memory), "fix the retry backoff in the client")
    names = _names(text)
    assert not set(names) & set(LANDING)
    assert names == [
        "feedback_pytest_quiet",
        "feedback_mixed_gh",
        "feedback_plain",
        "feedback_stop_pr",
        "feedback_more",
    ]
    assert ":landing_drop2" in _status(tmp_path)


def test_the_next_row_takes_the_place_of_a_landing_row(tmp_path, memory, daemon):
    env = _env(tmp_path, memory, NOBLIVION_RECALL_PROMPT_ROWS="3")
    assert _names(_serve(env, "fix the retry backoff")) == [
        "feedback_pytest_quiet",
        "feedback_mixed_gh",
        "feedback_plain",
    ]


@pytest.mark.parametrize(
    "prompt",
    ["Finish the fix, push it and open a PR", "Merge PR 12 now", "run the test suite"],
)
def test_a_task_that_names_a_landing_action_keeps_the_rows(tmp_path, memory, daemon, prompt):
    names = _names(_serve(_env(tmp_path, memory), prompt))
    assert names[:2] == list(LANDING)
    assert ":landing_drop" not in _status(tmp_path)


def test_the_switch_off_gives_the_rows_before(tmp_path, memory, daemon):
    env = _env(tmp_path, memory, **{hook.GUARD_ONLY_LANDING_ENV: "0"})
    assert hook.guard_only_landing(env) is False
    assert _names(_serve(env, "fix the retry backoff"))[:2] == list(LANDING)
    assert ":landing_drop" not in _status(tmp_path)


def test_the_config_key_works_and_the_env_var_wins(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"guard_only_landing": 0}}))
    env = {"NOBLIVION_CONFIG": str(cfg)}
    assert hook.guard_only_landing(env) is False
    assert hook.guard_only_landing({**env, hook.GUARD_ONLY_LANDING_ENV: "1"}) is True
    assert hook.guard_only_landing({"NOBLIVION_CONFIG": str(tmp_path / "none.json")}) is True


def test_a_row_with_no_local_file_stays(tmp_path, memory, daemon, monkeypatch):
    rows = [("A store capture with no file", 0.9)] + ROWS
    monkeypatch.setattr(
        hook,
        "recall_index",
        lambda q, k, environ=None, *a, **kw: [
            hook.IndexLine(rank=i, mid=19100 + i, title=t, summary="s", score=sc)
            for i, (t, sc) in enumerate(rows, start=1)
        ][:k],
    )
    # With rules only on, a row with no file is already left out; with it off the row stays.
    env = _env(tmp_path, memory, NOBLIVION_RECALL_RULES_ONLY="0")
    names = _names(_serve(env, "fix the retry backoff"))
    assert "A store capture with no file" in names
    assert not set(names) & set(LANDING)


def test_the_corpus_cache_keeps_scope_and_triggers(tmp_path, memory):
    cache = str(tmp_path / "corpus_cache.json")
    for _ in range(2):  # the second read comes from the cache
        files = {md.name: md for md in corpus.load_memory_corpus(str(memory), cache)}
        push = files["feedback_land_push"]
        assert push.scope == "tool"
        assert push.triggers == ("git push", "phrase:push the fix")
        assert corpus.landing_note(push.scope, push.triggers)
        assert not corpus.landing_note(
            files["feedback_plain"].scope, files["feedback_plain"].triggers
        )


# 4. the guard shows the rule at the command ----------------------------------

SPELLINGS = {
    "feedback_land_push": [
        "git push",
        "git push origin HEAD:main",
        "cd /srv/repo && git push",
        "git -C /srv/repo push origin main",
        "GIT_TERMINAL_PROMPT=0 git push",
        "/usr/bin/git push --force-with-lease",
    ],
    "feedback_land_tree": [
        "git worktree add ../wt -b topic origin/main",
        "cd /srv/repo && git worktree add ../wt",
        "git -C /srv/repo worktree add /srv/wt",
        "/usr/bin/git worktree add x",
        "env A=1 git worktree add x",
        "bash -c 'git worktree add x'",
    ],
    "feedback_land_pr": [
        "gh pr create --fill",
        "gh pr merge 12 --squash",
        "cd /srv/repo && gh pr create",
        "GH_TOKEN=x gh pr create",
        "/usr/bin/gh pr merge 3",
        "sudo gh pr merge 3 --auto",
    ],
    "feedback_land_gate": [
        "tools/team-merge 12",
        "python3 tools/team-merge 12",
        "./tools/team-merge 12",
        "bash tools/team-merge 12",
        "cd /srv/repo && tools/team-merge 12",
        "/srv/repo/tools/team-merge 12",
    ],
}


@pytest.fixture(scope="module")
def guard_table(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("guard")
    folder = tmp / "memory"
    folder.mkdir()
    _note(
        folder,
        "feedback_land_push",
        "Run git push only from a worktree branch, never from the shared checkout.",
        "tool",
        ("git push",),
    )
    _note(
        folder,
        "feedback_land_tree",
        "Run git worktree add from the remote head before the first edit.",
        "tool",
        ("git worktree add", "git worktree"),
    )
    _note(
        folder,
        "feedback_land_pr",
        "Run gh pr create with a full body, and gh pr merge only after the gate.",
        "tool",
        ("gh pr", "gh pr create", "gh pr merge"),
    )
    _note(
        folder,
        "feedback_land_gate",
        "Run tools/team-merge and read its verdict before you merge.",
        "tool",
        ("tools/team-merge", "phrase:merge now"),
    )
    table = tmp / "table.json"
    gt.rebuild([folder], table)
    for mid in SPELLINGS:
        entry = next(e for e in json.loads(table.read_text())["entries"] if e["id"] == mid)
        assert corpus.landing_note(entry["scope"], entry["triggers"])
    return table


@pytest.mark.parametrize("mid,command", [(m, c) for m, cmds in SPELLINGS.items() for c in cmds])
def test_the_guard_shows_a_landing_rule_at_its_command(tmp_path, guard_table, mid, command):
    env = {
        "NOBLIVION_GUARD_TABLE": str(guard_table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }
    event = {
        "session_id": "g1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/tmp",
    }
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert mid in context


# 5. a landing note is a strong row at a landing action ------------------------


@pytest.fixture(scope="module")
def shared_trigger_table(tmp_path_factory):
    """Five notes share the trigger ``gh pr``, so it is weak (4 or more notes), and no rule
    names a word of the command: the grading alone would keep every row away."""
    tmp = tmp_path_factory.mktemp("shared")
    folder = tmp / "memory"
    folder.mkdir()
    for i in range(5):
        _note(
            folder,
            f"feedback_gate_{i}",
            f"Check gate number {i} before the change goes out.",
            "tool",
            ("gh pr", f"gh pr create{' --draft' * (i % 2)}"),
        )
    _note(folder, "feedback_other", "Keep the output short.", "tool", ("gh run list",))
    table = tmp / "table.json"
    gt.rebuild([folder], table)
    return table


def _guard(tmp_path, table, command, sid="g2", **extra):
    env = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        **extra,
    }
    event = {
        "session_id": sid,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/tmp",
    }
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""


def test_a_landing_note_with_a_shared_trigger_shows_at_a_landing_action(
    tmp_path, shared_trigger_table
):
    text = _guard(tmp_path, shared_trigger_table, "gh pr create --fill")
    assert len(re.findall(r"- Memory feedback_gate_\d", text)) == gh.MAX_ROWS


def test_it_shows_the_next_notes_at_the_next_landing_action(tmp_path, shared_trigger_table):
    first = _guard(tmp_path, shared_trigger_table, "gh pr create --fill")
    second = _guard(tmp_path, shared_trigger_table, "gh pr merge 3")
    ids = lambda t: set(re.findall(r"- Memory (feedback_gate_\d)", t))  # noqa: E731
    assert ids(first) and ids(second) and not ids(first) & ids(second)
    assert len(ids(first) | ids(second)) == 5
    assert not _guard(tmp_path, shared_trigger_table, "gh pr create --fill")  # all shown once


def test_it_does_not_show_at_a_read(tmp_path, shared_trigger_table):
    assert "feedback_gate_" not in _guard(tmp_path, shared_trigger_table, "gh pr view 3")
    assert "feedback_gate_" not in _guard(tmp_path, shared_trigger_table, "git worktree list")


def test_the_guard_switch_off_gives_the_rows_before(tmp_path, shared_trigger_table):
    off = {gh.LANDING_ENV: "0"}
    assert "feedback_gate_" not in _guard(tmp_path, shared_trigger_table, "gh pr create", **off)


def test_a_landing_action_is_not_a_read():
    for form in (
        "git push origin main",
        "git worktree add x",
        "gh pr merge 3",
        "tools/team-merge 3",
    ):
        assert corpus.landing_action(form)
    for form in (
        "git worktree list",
        "gh pr view 3",
        "gh pr checks 3",
        "git status",
        "git branch --merged",
    ):
        assert not corpus.landing_action(form)
