# SPDX-License-Identifier: AGPL-3.0-or-later
"""The guard table compiler and its matcher (``hooks/guard_table.py``).

What these tests hold:

1. REBUILD reads a memory folder through ``read_fields`` and writes one table:
   every memory with a rule is an entry, index and topic files are not.
2. THE WRITE IS ATOMIC: a failed write leaves the old table and no temp file.
3. A BAD REGEX IS SKIPPED, NEVER FATAL: a ``violates`` that fails
   ``check_fields`` is not a guard, is listed in ``skipped`` with the reason,
   and the other guards still build.
4. MATCH TESTS EACH SHELL SEGMENT: the anchored stash regex fires on
   ``cd x && git stash pop``; quoted text and heredoc bodies do not fire.
5. THE CLI FAILS OPEN: ``--rebuild`` exits 0 and prints nothing, on success and
   on failure; a failure never replaces a good table.
8. A SKIPPED RULE IS NAMED (NOBLIVION-50): when the build skipped a rule,
   ``--rebuild`` prints one ``systemMessage`` line with the rule and the reason.
9. THE STAMP (NOBLIVION-50): the table records the names, sizes and change
   times of the memory files; ``stale`` is true after any change of them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

gt = load_hook("guard_table", "guard_table_t_table")
MODULE = HOOKS / "guard_table.py"
FIELDS_HOOK = HOOKS / "memory_fields_hook.py"

STASH = r"^git\s+stash\s+pop\s*$"
WRITE_TREE = r"git\s+add\s+-A\s+&&\s+git\s+write-tree"


def memory(
    name: str, violates: str = "", repeat: str = "", ok: str = "", trigger: str = "git stash"
) -> str:
    lines = [
        "---",
        f"name: {name}",
        "description: a lesson",
        "type: feedback",
        "rule: Do the safe thing.",
        'apply: "Use the safe command."',
        "scope: tool",
        f"triggers: [{trigger}]",
    ]
    if violates:
        lines += [
            "violates: " + json.dumps(violates),
            "example_repeat: " + json.dumps(repeat),
            "example_ok: " + json.dumps(ok),
        ]
    return "\n".join(lines + ["---", "", "Body text.", ""])


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited settings.
    No test writes the default table."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "default-guard-table.json"))
    return {"home": home, "data": data}


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    (d / "feedback_write_tree.md").write_text(
        memory(
            "feedback_write_tree",
            WRITE_TREE,
            "git add -A && git write-tree",
            "git add tools/x.py && git write-tree",
            trigger="git write-tree",
        )
    )
    (d / "feedback_rows_only.md").write_text(memory("feedback_rows_only"))
    (d / "MEMORY.md").write_text("- [x](feedback_stash.md)\n")
    (d / "topic_git.md").write_text(memory("topic_git", STASH, "git stash pop", "git stash list"))
    return d


@pytest.fixture
def table_file(tmp_path, monkeypatch):
    p = tmp_path / "out" / "guard-table.json"
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(p))
    return p


# 1. rebuild ------------------------------------------------------------------
def test_rebuild_writes_one_entry_per_memory_and_skips_index_files(folder, table_file):
    table = gt.rebuild(folder)
    assert table_file.is_file()
    on_disk = json.loads(table_file.read_text())
    assert on_disk == table
    ids = sorted(e["id"] for e in table["entries"])
    assert ids == ["feedback_rows_only", "feedback_stash", "feedback_write_tree"]
    by_id = {e["id"]: e for e in table["entries"]}
    assert by_id["feedback_stash"]["violates"] == STASH
    assert by_id["feedback_stash"]["rule"] == "Do the safe thing."
    assert by_id["feedback_stash"]["apply"] == "Use the safe command."
    assert by_id["feedback_stash"]["scope"] == "tool"
    assert by_id["feedback_stash"]["triggers"] == ["git stash"]
    assert by_id["feedback_rows_only"]["violates"] == ""
    assert table["skipped"] == []


def test_rebuild_reads_the_regex_unescaped(folder, table_file):
    """The parse returns violates JSON-escaped; the table must hold the
    decoded regex, so ``\\s`` is one backslash in the compiled pattern."""
    table = gt.rebuild(folder)
    pat = {e["id"]: e["violates"] for e in table["entries"]}["feedback_stash"]
    assert "\\\\" not in pat
    assert gt.match("git stash pop", table)


def test_rebuild_of_a_missing_folder_raises_and_keeps_the_old_table(tmp_path, folder, table_file):
    gt.rebuild(folder)
    before = table_file.read_bytes()
    with pytest.raises(FileNotFoundError):
        gt.rebuild(tmp_path / "absent")
    assert table_file.read_bytes() == before


def test_rebuild_hook_is_silent_without_a_memory_folder(tmp_path, monkeypatch, capsys):
    """A fresh install has no memory folder: the SessionStart rebuild prints
    no error line and writes no table (NOBLIVION-27)."""
    out = tmp_path / "table.json"
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(out))
    assert gt.main(["--rebuild"]) == 0
    assert capsys.readouterr() == ("", "")
    assert not out.exists() and gt.load_table(out) == {"entries": []}
    assert gt.main(["--rebuild", "--report"]) == 0
    assert "no memory folder" in capsys.readouterr().out


def test_the_table_carries_what_the_guard_hook_needs(folder, table_file):
    """The table carries what the guard hook needs and nothing it has to recompute."""
    table = gt.rebuild(folder)
    assert set(table) >= {"version", "built_at", "source", "entries", "skipped"}
    assert all(
        set(e) == {"id", "rule", "apply", "scope", "triggers", "violates", "complies", "run_first"}
        for e in table["entries"]
    )


# 2. atomic write -------------------------------------------------------------
def test_write_atomic_replaces_and_leaves_no_temp(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    p = d / "t.json"
    p.write_text("old")
    gt.write_atomic(p, {"entries": [1]})
    assert json.loads(p.read_text()) == {"entries": [1]}
    assert [x.name for x in d.iterdir()] == ["t.json"]


def test_a_failed_write_keeps_the_old_table_and_no_temp(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    p = d / "t.json"
    p.write_text('{"entries": []}')
    with pytest.raises(TypeError):
        gt.write_atomic(p, {"entries": [object()]})
    assert p.read_text() == '{"entries": []}'
    assert [x.name for x in d.iterdir()] == ["t.json"]


def test_write_atomic_uses_os_replace(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def spy(a, b):
        calls.append((Path(a).parent, Path(b)))
        return real(a, b)

    monkeypatch.setattr(gt.os, "replace", spy)
    p = tmp_path / "t.json"
    gt.write_atomic(p, {"entries": []})
    assert calls == [(tmp_path, p)]


# 3. a bad regex is skipped ---------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        r"(a+)+b",  # nested quantifier: polynomial / exponential
        r"git",  # matches everyday commands
        r"git stash (",  # does not compile
    ],
)
def test_a_violates_that_fails_check_fields_is_skipped_not_fatal(folder, table_file, bad):
    (folder / "feedback_bad.md").write_text(memory("feedback_bad", bad, "git stash pop aab", "ls"))
    table = gt.rebuild(folder)
    by_id = {e["id"]: e for e in table["entries"]}
    assert by_id["feedback_bad"]["violates"] == ""
    skipped = {s["id"]: s for s in table["skipped"]}
    assert skipped["feedback_bad"]["violates"] == bad
    assert skipped["feedback_bad"]["problems"]
    assert by_id["feedback_stash"]["violates"] == STASH  # the rest still builds
    assert [h["id"] for h in gt.match("git stash pop", table)] == ["feedback_stash"]


def test_the_skip_is_decided_by_check_fields(folder, table_file, monkeypatch):
    """A regex that compiles and matches its own example is still dropped when
    check_fields refuses it: the table never second-guesses the checker."""
    mf = gt._mf()
    monkeypatch.setattr(
        mf,
        "check_fields",
        lambda fields, kind="feedback": (
            ["refused for the test"] if fields.get("violates") == STASH else []
        ),
    )
    table = gt.rebuild(folder)
    assert {e["id"]: e["violates"] for e in table["entries"]}["feedback_stash"] == ""
    assert [s["id"] for s in table["skipped"]] == ["feedback_stash"]
    assert gt.match("git stash pop", table) == []


# 4. segment split and match --------------------------------------------------
@pytest.mark.parametrize(
    "cmd, segs",
    [
        ("cd x && git stash pop", ["cd x", "git stash pop"]),
        ("a || b; c | d", ["a", "b", "c", "d"]),
        ("a\nb", ["a", "b"]),
        ("echo 'x && y' ; ls", ["echo 'x && y'", "ls"]),
        ('echo "x; y | z"', ['echo "x; y | z"']),
        ("make 2>&1 | tail & sleep 1", ["make 2>&1", "tail", "sleep 1"]),
        ("(cd x; git stash pop)", ["cd x", "git stash pop"]),
        ('x=$(git log | head -1) && echo "$x"', ["x=$(git log | head -1)", 'echo "$x"']),
        ("cat <<'EOF' > f\ngit stash pop\nEOF\necho done", ["cat <<'EOF' > f", "echo done"]),
        ("echo a \\; b", ["echo a \\; b"]),
    ],
)
def test_segments(cmd, segs):
    assert gt.segments(cmd) == segs


def test_the_stash_regex_fires_on_a_later_segment(folder, table_file):
    table = gt.rebuild(folder)
    hits = gt.match("cd /srv/wt-x && git stash pop", table)
    assert [(h["id"], h["via"], h["segment"]) for h in hits] == [
        ("feedback_stash", "segment", "git stash pop")
    ]


def test_a_leading_assignment_does_not_hide_the_command(folder, table_file):
    table = gt.rebuild(folder)
    assert [h["id"] for h in gt.match("GIT_DIR=.git git stash pop", table)] == ["feedback_stash"]


WORKTREE = r"git\s+worktree\s+remove"
PUSH_FORCE = r"git\s+push\s+--force"


@pytest.fixture
def git_folder(folder):
    (folder / "feedback_worktree.md").write_text(
        memory(
            "feedback_worktree",
            WORKTREE,
            "git worktree remove /x",
            "git worktree list",
            trigger="git worktree",
        )
    )
    (folder / "feedback_push.md").write_text(
        memory(
            "feedback_push",
            PUSH_FORCE,
            "git push --force origin b",
            "git push origin b",
            trigger="git push",
        )
    )
    return folder


@pytest.mark.parametrize(
    "cmd, ids",
    [
        ("git worktree remove /x", ["feedback_worktree"]),
        ("git -C /repo worktree remove --force /x", ["feedback_worktree"]),
        ("git -C /r push --force origin b", ["feedback_push"]),
        ('git -C "/a b" push --force x', ["feedback_push"]),
        ("git -C '/a b' -c user.name=x --no-pager push --force x", ["feedback_push"]),
        (
            "git --git-dir=/r/.git --work-tree /r --no-optional-locks -P stash pop",
            ["feedback_stash"],
        ),
        ("git --bare --literal-pathspecs -c 'k=a b' stash pop", ["feedback_stash"]),
        ("env -C /r git -C /r stash pop", ["feedback_stash"]),
        ("env A=1 B='x y' git stash pop", ["feedback_stash"]),
        ("sudo -u builder nohup setsid timeout 30 git -C /r stash pop", ["feedback_stash"]),
        ("cd r && git -C /r add -A && git -C /r write-tree", ["feedback_write_tree"]),
    ],
)
def test_git_global_options_and_wrappers_do_not_hide_the_command(git_folder, table_file, cmd, ids):
    table = gt.rebuild(git_folder)
    assert sorted(h["id"] for h in gt.match(cmd, table)) == ids


@pytest.mark.parametrize(
    "cmd",
    [
        "git -C /r status",
        "git -C /r stash list",
        "git -C /r worktree list",
        "git -C /r push origin b",
        "git -C /r stash pop --index",
        "echo git -Pfoo push --force",
    ],
)
def test_normalization_adds_no_hit_on_other_forms(git_folder, table_file, cmd):
    table = gt.rebuild(git_folder)
    assert gt.match(cmd, table) == []


def test_the_raw_segment_is_still_matched(tmp_path, table_file):
    table = {"entries": [{"id": "m", "violates": r"git\s+-C\s+/r\s+status"}]}
    assert [h["id"] for h in gt.match("git -C /r status", table)] == ["m"]


@pytest.mark.parametrize(
    "cmd",
    [
        'git commit -m "then git stash pop"',
        "echo 'cd x && git stash pop'",
        "cat > notes.md <<'EOF'\ngit stash pop\ngit add -A && git write-tree\nEOF",
        "git stash pop --index",
        "git stash list",
        "",
    ],
)
def test_no_hit_on_quoted_text_heredoc_bodies_or_other_forms(folder, table_file, cmd):
    table = gt.rebuild(folder)
    assert gt.match(cmd, table) == []


def test_a_regex_that_spans_segments_hits_via_whole(folder, table_file):
    table = gt.rebuild(folder)
    hits = gt.match("cd repo && git add -A && git write-tree", table)
    assert [(h["id"], h["via"], h["segment"]) for h in hits] == [
        ("feedback_write_tree", "whole", "")
    ]


def test_one_hit_per_memory_and_the_hit_carries_the_rule(folder, table_file):
    table = gt.rebuild(folder)
    hits = gt.match("git stash pop\ngit stash pop", table)
    assert len(hits) == 1
    assert hits[0]["rule"] == "Do the safe thing."
    assert hits[0]["apply"] == "Use the safe command."
    assert hits[0]["regex"] == STASH


def test_match_reads_the_table_path_and_fails_open(folder, table_file, tmp_path, monkeypatch):
    assert gt.match("git stash pop") == []  # no table yet
    gt.rebuild(folder)
    assert [h["id"] for h in gt.match("git stash pop")] == ["feedback_stash"]
    table_file.write_text("{not json")
    assert gt.match("git stash pop") == []  # broken table
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "none.json"))
    assert gt.match("git stash pop") == []


def test_the_default_table_is_in_the_data_dir(hook_env, monkeypatch, tmp_path):
    """Without ``NOBLIVION_GUARD_TABLE`` each project memory folder has its own
    table, ``<data dir>/guard-tables/<slug of the folder>.json`` (NOBLIVION-31)."""
    monkeypatch.delenv("NOBLIVION_GUARD_TABLE")
    mod = load_hook("guard_table", "guard_table_t_table_default")
    tables = hook_env["data"] / "guard-tables"
    a, b = tmp_path / "a" / "memory", tmp_path / "b" / "memory"
    assert mod.table_path(a) == tables / (mod._CFG.project_slug(str(a)) + ".json")
    assert mod.table_path(a) != mod.table_path(b)
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(a))
    assert mod.table_path() == mod.table_path(a)


def test_each_regex_is_compiled_once(folder, table_file, monkeypatch):
    table = gt.rebuild(folder)
    gt._COMPILED.clear()
    calls = []
    real = gt.re.compile
    monkeypatch.setattr(gt.re, "compile", lambda p, *a: calls.append(p) or real(p, *a))
    for _ in range(5):
        gt.match("cd x && git stash pop", table)
    assert sorted(calls) == sorted([STASH, WRITE_TREE])


# 5. CLI -----------------------------------------------------------------------
def _cli(env_extra, *args):
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(MODULE), *args], capture_output=True, text=True, env=env, timeout=60
    )


def test_cli_rebuild_is_silent_on_success(folder, tmp_path):
    out = tmp_path / "t.json"
    r = _cli({"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(out)}, "--rebuild")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert len(json.loads(out.read_text())["entries"]) == 3


def test_cli_rebuild_without_a_memory_folder_is_silent_and_keeps_the_old_table(tmp_path):
    out = tmp_path / "t.json"
    out.write_text('{"entries": ["old"]}')
    r = _cli(
        {
            "NOBLIVION_MEMORY_DIR": str(tmp_path / "absent"),
            "NOBLIVION_GUARD_TABLE": str(out),
        },
        "--rebuild",
    )
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""  # no memory yet is not an error (NOBLIVION-27)
    assert out.read_text() == '{"entries": ["old"]}'


def test_cli_rebuild_fails_open_on_an_unwritable_table(folder, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = _cli(
        {"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(blocker / "t.json")},
        "--rebuild",
    )
    assert r.returncode == 0 and r.stdout == ""
    assert "rebuild failed" in r.stderr


def test_cli_match_prints_hits(folder, tmp_path):
    out = tmp_path / "t.json"
    env = {"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(out)}
    _cli(env, "--rebuild")
    r = _cli(env, "--match", "cd x && git stash pop")
    assert r.returncode == 0
    assert [h["id"] for h in json.loads(r.stdout)] == ["feedback_stash"]


# 6. the memory fields hook calls rebuild --------------------------------------
def test_the_memory_fields_hook_rebuilds_the_table_after_a_memory_write(folder, tmp_path):
    out = tmp_path / "t.json"
    target = folder / "feedback_stash.md"
    event = {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(tmp_path)}
    env = {**os.environ, "NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(out)}
    r = subprocess.run(
        [sys.executable, str(FIELDS_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0
    assert [e["id"] for e in json.loads(out.read_text())["entries"] if e["violates"]] == [
        "feedback_stash",
        "feedback_write_tree",
    ]


# 7. quoted data, comments and executed strings -------------------------------
# Unanchored regexes of the kind a real table holds.
LIVE = {
    "worktree_remove": r"git\s+worktree\s+remove",
    "pr_merge": r"gh\s+pr\s+merge\b",
    "push_force": r"git\s+push\s+--force",
    "merge_override": r"tools/safe-merge\s+.*--override-reason",
    "pkill_f": r"pkill\s+-f\s+",
    "modules_pop": r"sys\.modules\.pop\s*\(",
    "stash_pop": r"^git\s+stash\s+pop\s*$",
    "runner_restart": r"docker\s+restart\b[^|;&]*runner",
    "grep_404": r"grep\s+404\b",
    "service_user": (
        r"docker\s+exec\s+(?!(?:-u|--user)[\s=]appuser\s)[^|;&]*\s(?:test\s+-[rwx]\s|/run/secrets/)"
    ),
}
LIVE_TABLE = {
    "entries": [{"id": k, "violates": v, "rule": "r", "apply": "a"} for k, v in LIVE.items()]
}


def _ids(cmd):
    return sorted(h["id"] for h in gt.match(cmd, LIVE_TABLE))


@pytest.mark.parametrize(
    "cmd",
    [
        # false denies a review found
        'grep -rn "gh pr merge" tools/ | head',
        "grep -rn 'git worktree remove' /srv/widget/CLAUDE.md",
        'git commit -m "docs: use worktree_guard, never bare git worktree remove"',
        "echo done  # git push --force is forbidden",
        "python3 -c 'import sys; print(1)'  # not gh pr merge",
        'gh pr create --title x --body "merge with tools/safe-merge 12 --override-reason why"',
        'rg -n "pkill -f " tools/',
        'grep -n "sys.modules.pop(" tests/*.py',
        # more mention shapes
        "echo done # a; git push --force origin b",
        "git log --grep 'git worktree remove'",
        "bash -c 'echo \"git worktree remove /x\"'",
        "python3 -c \"import os; os.system('git worktree remove /x')\"",
        "docker exec c psql -c 'select 1' # gh pr merge",
        "git commit -F - <<'EOF'\ngit push --force origin b\nEOF",
        "git commit -m \"$(cat <<'EOF'\nnever git worktree remove\nEOF\n)\"",
        "cat <<EOF > f\ndon't git push --force\nEOF\necho ok",
        "echo ${#x} 'gh pr merge'",
    ],
)
def test_a_mention_in_quotes_or_a_comment_does_not_hit(cmd):
    assert gt.match(cmd, LIVE_TABLE) == []


@pytest.mark.parametrize(
    "cmd, ids",
    [
        # executed strings are command lines of their own
        ("bash -c 'git worktree remove /x'", ["worktree_remove"]),
        ('sh -c "git stash pop"', ["stash_pop"]),
        ("zsh -lc 'git stash pop'", ["stash_pop"]),
        ("eval 'git stash pop'", ["stash_pop"]),
        ("ssh alpha.example 'git -C /r worktree remove /x'", ["worktree_remove"]),
        (
            'ssh -o BatchMode=yes alpha.example "cd /r && git push --force github b"',
            ["push_force"],
        ),
        ("sudo -u builder bash -c 'cd /r && git worktree remove /x'", ["worktree_remove"]),
        ("docker exec c sh -c 'test -r /run/secrets/x'", ["service_user"]),
        ("kubectl exec p -- sh -c 'git stash pop'", ["stash_pop"]),
        ('bash -c \'bash -c "sh -c \\"git stash pop\\""\'', ["stash_pop"]),
        ('echo "$(git worktree remove /x)"', ["worktree_remove"]),
        ("echo `git worktree remove /x`", ["worktree_remove"]),
        # live commands with quoted arguments still hit
        ('git worktree remove "$SW"', ["worktree_remove"]),
        ("git -C '/a b' push --force origin b", ["push_force"]),
        ('docker restart "acme-github-runner-1"', ["runner_restart"]),
        ("git commit -m 'x' && gh pr merge 12 --squash", ["pr_merge"]),
        ("echo 'a' # c\ngit worktree remove /x", ["worktree_remove"]),
        ("cat <<EOF > f\ndon't\nEOF\ngit worktree remove /y", ["worktree_remove"]),
        # a live grep 404: the regex is broad, this is not a mention
        ("curl -s http://localhost:11434/api/tags | grep 404", ["grep_404"]),
    ],
)
def test_a_live_or_executed_command_still_hits(cmd, ids):
    assert _ids(cmd) == ids


def test_executed_strings_recurse_three_levels_then_stay_unmasked():
    views = gt.command_views("bash -c 'sh -c \"eval x\"'")
    assert [v[0].replace(gt.MASK, "_") for v in views][1:] == ['sh -c "eval x"', "eval x"]
    deep = (
        'bash -c \'bash -c "bash -c \\"bash -c \\\\\\"echo '
        '\\\\\\\\\\\\\\"gh pr merge\\\\\\\\\\\\\\"\\\\\\"\\""\''
    )
    assert len(gt.command_views(deep)) == 4  # the top line plus three levels


def test_masking_keeps_positions():
    cmd = 'git commit -m "x git push" # y'
    masked, raw, dead = gt.command_views(cmd)[0]
    assert len(masked) == len(raw) == len(cmd) == len(dead)
    assert masked.startswith('git commit -m "') and masked[15:25] == gt.MASK * 10


# 8. the compliant form (complies:) and the command to run first ---------------
GATE_V = (
    r"(?:\bpython[\d.]*\s+|^\s*)(?:[\w./-]{0,200}/)?lint_changed_lines\.py"
    r"(?![^;&|\n]*--base[=\s]+(?![\"']?HEAD)\S)"
)
GATE_REPEAT = "python3 tools/lint_changed_lines.py --base HEAD^"
GATE_OK = (
    'cd repo && python3 tools/lint_changed_lines.py --base "$(git merge-base HEAD github/main)"'
)
GATE_C = r"\bgit\s+merge-base\b"


def gate_memory(complies: str = GATE_C) -> str:
    text = memory("feedback_gate", GATE_V, GATE_REPEAT, GATE_OK, trigger="python3 tools/lint")
    if complies:
        text = text.replace(
            "\n---\n\nBody", "\ncomplies: " + json.dumps(complies) + "\n---\n\nBody"
        )
    return text


def test_complies_and_run_first_are_built(folder, table_file):
    (folder / "feedback_gate.md").write_text(gate_memory())
    table = gt.rebuild(folder)
    e = {x["id"]: x for x in table["entries"]}["feedback_gate"]
    assert e["violates"] == GATE_V and e["complies"] == GATE_C
    # the shortest piece of example_ok that complies: the substitution itself
    assert e["run_first"] == "git merge-base HEAD github/main"
    stash = {x["id"]: x for x in table["entries"]}["feedback_stash"]
    assert stash["complies"] == "" and stash["run_first"] == ""
    assert table["skipped"] == []


def test_run_first_keeps_the_quoted_text_of_example_ok():
    # the masked view blanks quoted data; run_first must be the command as written
    ok = "gh label list --search widget -q '.[] | .name' && echo done"
    got = gt.first_command(ok, r"\bgh\s+label\s+list\b", r"\bgh\s+label\s+create\b")
    assert got == "gh label list --search widget -q '.[] | .name'"
    assert "\x00" not in got


def test_run_first_is_never_a_loop_body_cut_out_of_its_loop():
    ok = "ps -eo pid | while read p; do sed -n 1p /proc/$p/cgroup; done | sort"
    got = gt.first_command(
        ok, r"\bsed\b[^;&|\n]*\s/proc/\$\w+/cgroup\b", r"\bdocker\s+exec\s+\S+\s+ps\b"
    )
    assert got == ok


def test_a_bad_complies_is_dropped_and_the_violates_is_kept(folder, table_file):
    (folder / "feedback_gate.md").write_text(gate_memory(r"\bgit\b"))  # matches git status
    table = gt.rebuild(folder)
    e = {x["id"]: x for x in table["entries"]}["feedback_gate"]
    assert e["violates"] == GATE_V
    assert e["complies"] == "" and e["run_first"] == ""
    sk = {s["id"]: s for s in table["skipped"]}["feedback_gate"]
    assert sk["complies"] == r"\bgit\b"
    assert any("complies" in p for p in sk["problems"])
    assert not any("violates" in p for p in sk["problems"])


@pytest.mark.parametrize(
    "cmd",
    [
        "git merge-base HEAD github/main",
        "git -C fixture/repo merge-base HEAD github/main",
        "BASE=$(git -C fixture/repo merge-base HEAD github/main); echo $BASE",
        GATE_OK,
        "cd repo && git merge-base HEAD origin/main",
    ],
)
def test_complies_match_finds_a_run_of_the_rules_own_command(folder, table_file, cmd):
    (folder / "feedback_gate.md").write_text(gate_memory())
    table = gt.rebuild(folder)
    assert gt.complies_match(cmd, table) == ["feedback_gate"]


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git log --oneline -5",
        "echo git-merge-base",
        # a command that violates the rule is never a compliant run
        "git merge-base HEAD github/main; python3 tools/lint_changed_lines.py --base HEAD^",
    ],
)
def test_complies_match_ignores_other_commands_and_violations(folder, table_file, cmd):
    (folder / "feedback_gate.md").write_text(gate_memory())
    table = gt.rebuild(folder)
    assert gt.complies_match(cmd, table) == []


def test_complies_match_fails_open_on_a_broken_entry():
    table = {
        "entries": [
            {"id": "a", "violates": "x", "complies": "(unclosed"},
            {"id": "b", "violates": "", "complies": "git"},
            {"id": "c", "violates": "zz", "complies": r"\bgit\s+merge-base\b"},
        ]
    }
    assert gt.complies_match("git merge-base HEAD x", table) == ["c"]
    assert gt.complies_match("", table) == []


def test_a_hit_carries_complies_and_run_first(folder, table_file):
    (folder / "feedback_gate.md").write_text(gate_memory())
    table = gt.rebuild(folder)
    h = {x["id"]: x for x in gt.match(GATE_REPEAT, table)}["feedback_gate"]
    assert h["complies"] == GATE_C and h["run_first"] == "git merge-base HEAD github/main"


# 9. status: and project: -----------------------------------------------------
def test_a_bad_status_or_project_line_never_switches_off_a_guard(folder, table_file):
    """``check_fields`` reports a wrong ``status:`` on any memory. That is a
    briefing problem; the guard regex of the same file must stay in the table."""
    p = folder / "feedback_stash.md"
    text = p.read_text()
    p.write_text(text.replace("---\n", "---\nstatus: done\nproject: Bad Slug\n", 1))
    mf = gt._mf()
    problems = mf.check_fields(mf.read_fields(p.read_text()), "feedback")
    assert len(mf.status_project_problems_of(problems)) == 2
    table = gt.rebuild(folder)
    assert {e["id"]: e["violates"] for e in table["entries"]}["feedback_stash"] == STASH
    assert "feedback_stash" not in [s["id"] for s in table["skipped"]]
    assert [h["id"] for h in gt.match("git stash pop", table)] == ["feedback_stash"]


def test_a_guard_regex_under_metadata_is_in_the_table(folder, table_file):
    """The Edit tool moves the rule fields under ``metadata:``. The guard of
    such a file must still fire."""
    mf = gt._mf()
    p = folder / "feedback_stash.md"
    text = p.read_text()
    before = gt.rebuild(folder)
    fields = mf.read_fields(text)
    lines, close = mf.split(mf.strip_schema(text))
    block = ["metadata:"] + ["  " + ln for ln in mf._serialize(fields)]
    p.write_text("\n".join(lines[:close] + block + lines[close:]))
    assert mf.nested_rule_keys(p.read_text()) and mf.read_fields(p.read_text()) == fields
    table = gt.rebuild(folder)
    assert table["entries"] == before["entries"]
    assert [h["id"] for h in gt.match("git stash pop", table)] == ["feedback_stash"]


# 10. integration review reproducers -------------------------------------------
REVIEW = {
    "compose_obs": r"docker\s+compose\s+.*-p\s+obs\b",
    "merge_override": r"tools/safe-merge\s+.*--override-reason",
    "pr_merge": r"gh\s+pr\s+merge\b",
    "stash_pop": r"^git\s+stash\s+pop\s*$",
    "runner_restart": r"docker\s+restart\b[^|;&]*runner",
    "docker_cp": r"^docker cp\s+\S+\s+app-core",
}
REVIEW_TABLE = {
    "entries": [{"id": k, "violates": v, "rule": "r", "apply": "a"} for k, v in REVIEW.items()]
}


def _rids(cmd):
    return sorted(h["id"] for h in gt.match(cmd, REVIEW_TABLE))


# a raw match that starts on a live command and ends in the quoted data of
# ANOTHER simple command is a mention, not a violation
@pytest.mark.parametrize(
    "cmd",
    [
        'docker compose -p acme-observability logs | command grep -c "-p obs"',
        'tools/safe-merge 1931 -- --squash && git commit -m "merged without --override-reason"',
        'docker compose ps; echo "use -p obs never"',
        "docker compose ps\necho 'never -p obs'",
    ],
)
def test_review_5_a_raw_match_that_ends_in_the_quotes_of_another_command_does_not_hit(cmd):
    assert gt.match(cmd, REVIEW_TABLE) == []


@pytest.mark.parametrize(
    "cmd, ids",
    [
        ('docker restart "acme-github-runner-1"', ["runner_restart"]),
        ('tools/safe-merge 12 "--override-reason" why', ["merge_override"]),
        ('docker compose -p obs logs | grep -c "x"', ["compose_obs"]),
        ('docker compose -p obs logs | grep -c "-p obs"', ["compose_obs"]),
        (
            'tools/safe-merge 12 "--override-reason" why && echo "no --override-reason"',
            ["merge_override"],
        ),
    ],
)
def test_review_5_a_quoted_argument_of_the_live_command_still_hits(cmd, ids):
    assert _rids(cmd) == ids
    assert gt.match(cmd, REVIEW_TABLE)[0]["via"] in ("raw", "segment", "whole")


# a heredoc terminator is any shell word, not only an identifier
@pytest.mark.parametrize(
    "cmd",
    [
        "cat > x.md <<'END-DOC'\nnever run gh pr merge\nEND-DOC",
        "cat > x.md <<\\EOF\nnever run gh pr merge\nEOF",
        'cat > x.md <<"END OF DOC"\nnever run gh pr merge\nEND OF DOC\necho ok',
        "cat > x.md <<-EOF.1\n\tgh pr merge 1\n\tEOF.1",
    ],
)
def test_review_6_a_heredoc_with_a_non_identifier_terminator_is_data(cmd):
    assert gt.match(cmd, REVIEW_TABLE) == []


@pytest.mark.parametrize(
    "cmd",
    [
        "cat > x.md <<'END-DOC'\ntext\nEND-DOC\ngh pr merge 1",
        "cat > x.md <<\\EOF\ntext\nEOF\ngh pr merge 1",
        "echo $((1<<2))\ngh pr merge 1",
        "echo $((x<<2)) && gh pr merge 1",
    ],
)
def test_review_6_a_command_after_the_heredoc_still_hits(cmd):
    assert _rids(cmd) == ["pr_merge"]


# a quote character in a ``#`` comment opens no quote
@pytest.mark.parametrize(
    "cmd",
    [
        "echo a # don't\ncat > f <<'EOF'\ngh pr merge\nEOF",
        "echo a # don't\ncat > f <<'EOF'\ngh pr merge\nEOF\necho b # it's done",
        'echo a # say "x\ncat > f <<EOF\ngh pr merge 1\nEOF',
        "x=$(echo a # don't\n)\ncat > f <<'EOF'\ngh pr merge\nEOF",
    ],
)
def test_review_7_an_apostrophe_in_a_comment_does_not_hide_a_heredoc(cmd):
    assert gt.match(cmd, REVIEW_TABLE) == []


@pytest.mark.parametrize(
    "cmd, ids",
    [
        ("echo a # don't\ngh pr merge 1", ["pr_merge"]),
        ("echo a # don't\ncat > f <<'EOF'\nx\nEOF\ngh pr merge 1", ["pr_merge"]),
        ("echo a#b 'c' && gh pr merge 1", ["pr_merge"]),
        ("echo ${#x} $# && gh pr merge 1", ["pr_merge"]),
        ("echo a # b; gh pr merge 1", []),
    ],
)
def test_review_7_a_comment_ends_at_the_line_end(cmd, ids):
    assert _rids(cmd) == ids


def test_review_7_segments_leave_a_comment_out():
    assert gt.segments("echo a # don't; x\nls") == ["echo a", "ls"]
    assert gt.segments("echo a#b ${#x}") == ["echo a#b ${#x}"]


# ordinary command shapes do not bypass a guard
@pytest.mark.parametrize(
    "cmd, ids",
    [
        ("gh pr \\\n merge 1", ["pr_merge"]),
        ("gh \\\n  pr \\\n  merge 1 --squash", ["pr_merge"]),
        ("command git stash pop", ["stash_pop"]),
        ("if x; then git stash pop; fi", ["stash_pop"]),
        ("{ git stash pop; }", ["stash_pop"]),
        ("exec git stash pop", ["stash_pop"]),
        ("time git stash pop", ["stash_pop"]),
        ("time -p git stash pop", ["stash_pop"]),
        ("nice -n 19 git stash pop", ["stash_pop"]),
        ("for i in 1 2; do git stash pop; done", ["stash_pop"]),
        ("if x; then :; else git stash pop; fi", ["stash_pop"]),
        ("! git stash pop", ["stash_pop"]),
        ("if git stash pop; then echo ok; fi", ["stash_pop"]),
        ("while ! git stash pop; do sleep 1; done", ["stash_pop"]),
        ("command docker cp f app-core:/app/f", ["docker_cp"]),
        ("sudo command nice git stash pop", ["stash_pop"]),
        # a heredoc body line that ends in a backslash does not eat the terminator
        ("cat > f <<'EOF'\nfoo \\\nEOF\ngh pr merge 1", ["pr_merge"]),
    ],
)
def test_review_8_a_wrapper_word_or_a_line_continuation_does_not_hide_the_command(cmd, ids):
    assert _rids(cmd) == ids


@pytest.mark.parametrize(
    "cmd",
    [
        "command -v git",
        "echo 'gh pr \\\n merge 1'",
        "cat > f <<'EOF'\ngh pr \\\n merge 1\nEOF",
        "echo then git stash pop",
        "time",
    ],
)
def test_review_8_adds_no_hit_on_other_forms(cmd):
    assert gt.match(cmd, REVIEW_TABLE) == []


# 8. NOBLIVION-50: a skipped rule is named at session start --------------------
FORCE_ALL = r"git\s+push\s+.*--force|.*"  # the last branch matches every command


def test_cli_rebuild_names_a_skipped_rule_in_one_line_for_the_user(folder, tmp_path):
    """A deny rule that the build drops does not block. The SessionStart
    rebuild says so in one ``systemMessage`` line, in the shape of the store
    start hook: the same line for the user and for the model."""
    (folder / "feedback_no_force.md").write_text(
        memory("feedback_no_force", FORCE_ALL, "git push --force origin main", "ls")
    )
    out = tmp_path / "t.json"
    r = _cli({"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(out)}, "--rebuild")
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.count("\n") == 1
    doc = json.loads(r.stdout)
    line = doc["systemMessage"]
    table = json.loads(out.read_text())
    assert gt.match("git push --force origin main", table) == []  # the rule is off
    (row,) = table["skipped"]
    assert line.startswith("NOBLIVION: 1 rule is skipped and does not block: feedback_no_force (")
    assert row["problems"][0] in line and "\n" not in line
    assert doc == {
        "systemMessage": line,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line},
    }


def test_cli_rebuild_prints_nothing_for_a_folder_with_no_rules(tmp_path):
    mem = tmp_path / "plain"
    mem.mkdir()
    (mem / "user_profile.md").write_text("---\nname: profile\ntype: user\n---\n\nA note.\n")
    out = tmp_path / "t.json"
    r = _cli({"NOBLIVION_MEMORY_DIR": str(mem), "NOBLIVION_GUARD_TABLE": str(out)}, "--rebuild")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert gt.skipped_line(json.loads(out.read_text())) == ""


def test_cli_report_keeps_its_plain_text(folder, tmp_path):
    (folder / "feedback_no_force.md").write_text(
        memory("feedback_no_force", FORCE_ALL, "git push --force origin main", "ls")
    )
    env = {"NOBLIVION_MEMORY_DIR": str(folder), "NOBLIVION_GUARD_TABLE": str(tmp_path / "t.json")}
    r = _cli(env, "--rebuild", "--report")
    assert "  skipped feedback_no_force: " in r.stdout and "systemMessage" not in r.stdout


def test_the_skipped_line_is_short_and_names_the_kind_of_each_row():
    rows = [
        {"id": f"feedback_r{i}", "violates": "x", "problems": [f"reason {i}", "second", "third"]}
        for i in range(7)
    ]
    rows.append({"id": "feedback_c", "complies": "y", "problems": ["complies is bad"]})
    odd_row = {
        "id": "feedback\nodd  name",
        "violates": "",
        "problems": ["unreadable:\n" + "x" * 900],
    }
    rows.append(odd_row)
    line = gt.skipped_line({"skipped": rows})
    assert line.startswith(
        "NOBLIVION: 8 rules are skipped and do not block: feedback_r0 (reason 0; 2 more problems); "
        "feedback_r1 (reason 1; 2 more problems); feedback_r2 (reason 2; 2 more problems); "
        "and 5 more. 1 complies field is skipped (the rule still blocks): "
        "feedback_c (complies is bad). "
    )
    assert "\n" not in line and len(line) < 700
    odd = gt.skipped_line({"skipped": rows[-1:]})
    assert "feedback odd name (unreadable: xxx" in odd and "\n" not in odd and len(odd) < 400
    assert gt.skipped_line({"skipped": []}) == "" and gt.skipped_line({}) == ""


# 9. NOBLIVION-50: the folder stamp ---------------------------------------------
def test_the_table_stamp_follows_the_memory_files_only(folder, table_file):
    assert gt.stale(folder, table_file)  # no table yet
    table = gt.rebuild(folder)
    assert table["stamp"] == gt.folder_stamp(folder) and not gt.stale(folder, table_file)
    # index files, topic files and other files are not in the table
    (folder / "MEMORY.md").write_text("- changed\n")
    (folder / "topic_git.md").write_text("changed")
    (folder / "notes.txt").write_text("not a memory")
    (folder / "sub").mkdir()
    assert not gt.stale(folder, table_file)
    table.pop("stamp")  # a table of an older version
    table_file.write_text(json.dumps(table))
    assert gt.stale(folder, table_file)


@pytest.mark.parametrize("change", ["rm", "edit", "mv", "new", "same size and mtime"])
def test_a_changed_memory_folder_makes_the_table_stale(folder, table_file, change):
    gt.rebuild(folder)
    target = folder / "feedback_stash.md"
    if change == "rm":
        target.unlink()
    elif change == "edit":
        target.write_text(target.read_text() + "One more line.\n")
    elif change == "mv":
        target.rename(folder / "feedback_moved.md")
    elif change == "new":
        (folder / "project_new.md").write_text(memory("project_new"))
    else:  # ``cp -p`` of another text: only the change time differs
        st = target.stat()
        target.write_text(target.read_text().replace("Body text.", "Body TEXT."))
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert (target.stat().st_size, target.stat().st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert gt.stale(folder, table_file)
    gt.rebuild(folder)
    assert not gt.stale(folder, table_file)


def test_the_stamp_covers_the_global_folder(folder, tmp_path, table_file):
    shared = tmp_path / "shared"
    gt.rebuild([folder, shared])  # the global folder does not exist yet
    assert not gt.stale([folder, shared], table_file)
    assert gt.stale(folder, table_file)  # another folder list
    shared.mkdir()
    assert gt.stale([folder, shared], table_file)
    gt.rebuild([folder, shared])
    (shared / "feedback_everywhere.md").write_text(memory("feedback_everywhere"))
    assert gt.stale([folder, shared], table_file)


def test_the_stamp_of_200_memory_files_is_cheap(tmp_path):
    """The guard hook takes the stamp on every call: one ``scandir`` and one
    ``stat`` per file, no file is read. Measured: under 1 ms for 200 files."""
    import time

    mem = tmp_path / "many"
    mem.mkdir()
    for i in range(200):
        (mem / f"feedback_n{i}.md").write_text(memory(f"feedback_n{i}"))
    best = 9.0
    for _ in range(5):
        t0 = time.perf_counter()
        gt.folder_stamp(mem)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05
