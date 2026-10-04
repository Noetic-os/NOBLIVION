# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-31: every hook uses the project memory folder of the session.

Before, the guard table, the guard hook, the stop checks, continuity, the
memory fields hook and the memory sync hook used the memory folder of the
home folder unless ``NOBLIVION_MEMORY_DIR`` was set. These tests start each
session in a project under ``tmp_path``, with ``HOME`` pointed at another tmp
folder, and set no ``NOBLIVION_MEMORY_DIR``. The hooks run as Claude Code
runs them: a fresh ``python3`` process with the hook JSON on stdin.

The optional global folder (``NOBLIVION_GLOBAL_MEMORY_DIR``) is read after
the project folder, and a project file wins over a global file of the same
name: in the guard table, in the recall corpus and in the store's pool.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook
from noblivion import config as store_config
from noblivion import ranking
from store_helpers import add_memory

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

hc = load_hook("hook_config", "hook_config_t_project_everywhere")
gt = load_hook("guard_table", "guard_table_t_project_everywhere")
rh = load_hook("recall_hook", "recall_hook_t_project_everywhere")
sc = load_hook("stop_checks", "stop_checks_t_project_everywhere")
ch = load_hook("continuity_hook", "continuity_hook_t_project_everywhere")
ms = load_hook("memory_sync_hook", "memory_sync_hook_t_project_everywhere")
fh = load_hook("memory_fields_hook", "memory_fields_hook_t_project_everywhere")
er = load_hook("error_recall_hook", "error_recall_hook_t_project_everywhere")

STASH = r"^git\s+stash\s+pop\s*$"
DROP = r"^git\s+stash\s+drop\s*$"
FORCE = r"^git\s+push\s+--force\s*$"
CORRECTION = "no, that is wrong"
RULED = (
    "---\nname: check env\ndescription: a lesson\ntype: feedback\n"
    'rule: Check the env first.\napply: "Run env | head."\nscope: tool\n'
    "triggers: [env]\n---\n\nBody.\n"
)

CLAUDE_ENVS = (
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
    "CLAUDE_PLUGIN_DATA",
    "XDG_DATA_HOME",
)


def memory(name: str, violates: str, repeat: str, ok: str, rule: str = "Do the safe thing.") -> str:
    return "\n".join(
        [
            "---",
            f"name: {name}",
            "description: a lesson",
            "type: feedback",
            f"rule: {rule}",
            'apply: "Use the safe command."',
            "scope: tool",
            "triggers: [git stash]",
            "violates: " + json.dumps(violates),
            "example_repeat: " + json.dumps(repeat),
            "example_ok: " + json.dumps(ok),
            "---",
            "",
            "Body text.",
            "",
        ]
    )


def git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fresh home, a data dir and a git project with a subfolder, all under
    ``tmp_path``. The project is not the home folder. No ``NOBLIVION_*``
    variable is inherited."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "work" / "project"
    for d in (home, data, project / "sub"):
        d.mkdir(parents=True)
    git("init", "-q", str(project))
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name in CLAUDE_ENVS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    folder = home / ".claude" / "projects" / hc.project_slug(project) / "memory"
    folder.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_STOP_CHECK_LOG": str(tmp_path / "stop-log.jsonl"),
        "NOBLIVION_STOP_CHECK_STATE": str(tmp_path / "stop-state"),
        "NOBLIVION_STOP_CHECK_MODE": "enforce",  # the lesson message is read from the block
        # The in-process hooks bound their cache folder at import time.
        "NOBLIVION_RECALL_CACHE_DIR": str(data / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return {"home": home, "data": data, "project": project, "folder": folder, "env": env}


def run(hook: str, event: dict, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / hook), *args],
        input=json.dumps(event),
        env=env,
        cwd="/",
        capture_output=True,
        text=True,
        timeout=60,
    )


def bash_event(cwd: Path, command: str) -> dict:
    return {
        "session_id": "s31",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }


def denied(res: subprocess.CompletedProcess) -> bool:
    assert res.returncode == 0, res.stderr
    return '"deny"' in res.stdout


def start(cwd: Path, env: dict) -> None:
    """SessionStart: the guard table rebuild with the hook JSON on stdin."""
    event = {"session_id": "s31", "hook_event_name": "SessionStart", "cwd": str(cwd)}
    res = run("guard_table.py", event, env, "--rebuild")
    assert res.returncode == 0 and res.stderr == "", res.stderr


# ── the project folder ──────────────────────────────────────────────────────


def test_a_guard_rule_in_the_project_folder_fires(world):
    (world["folder"] / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    sub = world["project"] / "sub"
    start(sub, world["env"])
    table = gt.table_path(world["folder"], world["env"])
    assert table.parent == world["data"] / "guard-tables" and table.is_file()
    assert denied(run("guard_hook.py", bash_event(sub, "git stash pop"), world["env"]))
    assert not denied(run("guard_hook.py", bash_event(sub, "git stash list"), world["env"]))
    assert not (world["home"] / ".claude" / "projects" / hc.project_slug(world["home"])).exists()


def test_the_guard_hook_builds_a_missing_project_table(world):
    (world["folder"] / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    assert denied(run("guard_hook.py", bash_event(world["project"], "git stash pop"), world["env"]))
    assert gt.table_path(world["folder"], world["env"]).is_file()


def test_two_projects_keep_two_tables(world, tmp_path):
    other = tmp_path / "work" / "other"
    other.mkdir(parents=True)
    git("init", "-q", str(other))
    other_folder = world["home"] / ".claude" / "projects" / hc.project_slug(other) / "memory"
    other_folder.mkdir(parents=True)
    (world["folder"] / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    (other_folder / "feedback_drop.md").write_text(
        memory("feedback_drop", DROP, "git stash drop", "git stash list")
    )
    start(world["project"], world["env"])
    start(other, world["env"])
    env = world["env"]
    assert denied(run("guard_hook.py", bash_event(world["project"], "git stash pop"), env))
    assert not denied(run("guard_hook.py", bash_event(world["project"], "git stash drop"), env))
    assert denied(run("guard_hook.py", bash_event(other, "git stash drop"), env))
    assert not denied(run("guard_hook.py", bash_event(other, "git stash pop"), env))


def _transcript(path: Path, cwd: Path, calls: list) -> Path:
    rows = [{"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": CORRECTION}}]
    for n, (name, inp) in enumerate(calls, start=1):
        use = {"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}
        res = {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": False, "content": "ok"}
        rows.append({"type": "assistant", "cwd": str(cwd), "message": {"content": [use]}})
        rows.append({"type": "user", "cwd": str(cwd), "message": {"content": [res]}})
    text = {"type": "text", "text": "Done."}
    rows.append({"type": "assistant", "cwd": str(cwd), "message": {"content": [text]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _stop(world, tmp_path, calls: list, env: dict | None = None) -> subprocess.CompletedProcess:
    env = world["env"] if env is None else env
    sub = world["project"] / "sub"
    mark = {"session_id": "s31", "prompt": CORRECTION, "cwd": str(sub)}
    assert run("stop_checks.py", mark, env, "--mark-correction").returncode == 0
    path = _transcript(tmp_path / "t.jsonl", sub, calls)
    event = {
        "session_id": "s31",
        "hook_event_name": "Stop",
        "transcript_path": str(path),
        "cwd": str(sub),
        "stop_hook_active": False,
    }
    return run("stop_checks.py", event, env)


def test_the_lesson_message_names_the_project_folder(world, tmp_path):
    res = _stop(world, tmp_path, [("Bash", {"command": "echo fixed"})])
    reason = json.loads(res.stdout)["reason"]
    assert f"feedback memory in {world['folder']} with rule:" in reason
    assert hc.project_slug(world["home"]) + "/" not in reason


def test_a_lesson_written_in_the_project_folder_passes(world, tmp_path):
    f = world["folder"] / "feedback_check_env.md"
    f.write_text(RULED)
    res = _stop(world, tmp_path, [("Write", {"file_path": str(f), "content": RULED})])
    assert res.returncode == 0 and res.stdout.strip() == ""


def test_continuity_uses_the_project_folder(world):
    (world["folder"] / "feedback_trap.md").write_text(
        memory("feedback_trap", STASH, "git stash pop", "git stash list", "Never pop the stash.")
    )
    env = dict(world["env"], NOBLIVION_CONTINUITY="1")
    event = {
        "session_id": "s31",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(world["project"] / "sub"),
    }
    text = ch.run(json.dumps(event), env, index=lambda *a, **k: [])
    assert "Never pop the stash." in text
    assert ch.session_env(env, event)["NOBLIVION_RECALL_MEMORY_DIR"] == str(world["folder"])


def test_memory_sync_and_fields_hooks_see_a_project_write(world):
    f = world["folder"] / "feedback_x.md"
    f.write_text("---\nname: x\ntype: feedback\n---\nBody.\n")
    event = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(f)},
        "cwd": str(world["project"]),
    }
    assert ms.target_path(event, world["env"]) == f.resolve()
    assert fh.target_path(event) == f.resolve()
    elsewhere = dict(event, cwd=str(world["home"]))
    assert ms.target_path(elsewhere, world["env"]) is None
    assert fh.target_path(elsewhere) is None


# ── the global folder ───────────────────────────────────────────────────────


@pytest.fixture
def shared(world, tmp_path) -> dict:
    """A global folder with one rule of its own and one rule whose file name
    the project folder also has."""
    folder = tmp_path / "rules" / "memory"
    folder.mkdir(parents=True)
    (folder / "feedback_force.md").write_text(
        memory("feedback_force", FORCE, "git push --force", "git push", "Global: no force push.")
    )
    (folder / "feedback_stash.md").write_text(
        memory("feedback_stash", DROP, "git stash drop", "git stash list", "Global stash rule.")
    )
    (world["folder"] / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list", "Project stash rule.")
    )
    env = dict(world["env"], NOBLIVION_GLOBAL_MEMORY_DIR=str(folder))
    return {"folder": folder, "env": env}


def test_guards_read_the_global_folder_and_the_project_wins(world, shared):
    env = shared["env"]
    cwd = world["project"] / "sub"
    start(cwd, env)
    assert denied(run("guard_hook.py", bash_event(cwd, "git push --force"), env))
    assert denied(run("guard_hook.py", bash_event(cwd, "git stash pop"), env))
    # The global feedback_stash.md is shadowed by the project file.
    assert not denied(run("guard_hook.py", bash_event(cwd, "git stash drop"), env))
    table = json.loads(gt.table_path(world["folder"], env).read_text())
    assert table["sources"] == [str(world["folder"]), str(shared["folder"])]


def test_without_the_global_folder_its_rules_are_not_read(world, shared):
    cwd = world["project"]
    start(cwd, world["env"])
    assert not denied(run("guard_hook.py", bash_event(cwd, "git push --force"), world["env"]))


def test_recall_reads_both_folders_and_the_project_wins(world, shared):
    env = rh.session_env(shared["env"], {"cwd": str(world["project"])})
    assert rh.memory_dirs(env) == [str(world["folder"]), str(shared["folder"])]
    corpus = {md.rel_path: md for md in rh.load_session_corpus(env)}
    assert set(corpus) == {"feedback_force.md", "feedback_stash.md"}
    assert corpus["feedback_stash.md"].rule == "Project stash rule."
    assert corpus["feedback_force.md"].rule == "Global: no force push."
    alone = rh.session_env(world["env"], {"cwd": str(world["project"])})
    assert [md.rel_path for md in rh.load_session_corpus(alone)] == ["feedback_stash.md"]


def test_a_lesson_written_in_the_global_folder_passes(world, shared, tmp_path):
    f = shared["folder"] / "feedback_check_env.md"
    f.write_text(RULED)
    calls = [("Write", {"file_path": str(f), "content": RULED})]
    res = _stop(world, tmp_path, calls, env=shared["env"])
    assert res.returncode == 0 and res.stdout.strip() == ""


def test_the_global_lesson_message_still_names_the_project_folder(world, shared, tmp_path):
    res = _stop(world, tmp_path, [("Bash", {"command": "echo fixed"})], env=shared["env"])
    assert f"feedback memory in {world['folder']} with rule:" in json.loads(res.stdout)["reason"]


def test_the_config_key_sets_the_global_folder(world, tmp_path):
    cfg = world["data"] / "config.json"
    cfg.write_text(json.dumps({"global_memory_dir": "~/rules/memory"}))
    env = {"HOME": str(world["home"]), "NOBLIVION_DATA_DIR": str(world["data"])}
    assert hc.global_memory_dir(env) == world["home"] / "rules" / "memory"
    assert store_config.global_memory_dir(env) == world["home"] / "rules" / "memory"
    assert hc.global_memory_dir({"NOBLIVION_DATA_DIR": str(tmp_path / "none")}) is None


def test_the_store_indexes_the_global_folder_and_shares_its_root(world, shared):
    env = shared["env"]
    settings = store_config.load_settings(env)
    assert shared["folder"] in settings.resolved_memory_dirs()
    assert "rules" in store_config.load_store_settings(env).shared_roots
    assert store_config.load_store_settings(world["env"]).shared_roots == ()


def test_the_store_pool_lets_a_project_file_shadow_a_shared_root(tmp_path):
    from noblivion import db

    conn = db.open_db(tmp_path / "db" / "noblivion.db", create=True)
    try:
        own = add_memory(conn, "feedback_stash", "project stash rule venv", root="-proj")
        other = add_memory(conn, "feedback_stash", "global stash rule venv", root="rules")
        extra = add_memory(conn, "feedback_force", "global force rule venv", root="rules")
        index = ranking.RankIndex()
        index.refresh(conn)
        got = index.rank(
            "venv", project="claude_code", top_k=10, root="-proj", shared_roots=["rules"]
        )
        assert {h.row.id for h in got.hits} == {own, extra}
        from_rules = index.rank("venv", project="claude_code", top_k=10, root="rules")
        assert {h.row.id for h in from_rules.hits} == {other, extra}
    finally:
        conn.close()


# ── a stranger's install ────────────────────────────────────────────────────


def test_a_stranger_install_resolves_one_folder_in_every_hook(tmp_path, monkeypatch):
    """A fresh home, a repository in tmp, sessions in a subfolder and in a
    linked worktree: recall, guards, stop checks, continuity, memory sync,
    memory fields and error recall all name the main checkout's folder."""
    home = tmp_path / "stranger"
    data = tmp_path / "stranger-data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name in CLAUDE_ENVS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    repo = tmp_path / "code" / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", str(repo))
    git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init")
    (repo / "pkg" / "mod").mkdir(parents=True)
    linked = tmp_path / "code" / "linked"
    git("-C", str(repo), "worktree", "add", "-q", str(linked))
    folder = home / ".claude" / "projects" / hc.project_slug(repo) / "memory"
    folder.mkdir(parents=True)
    (folder / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_STOP_CHECK_LOG": str(tmp_path / "stop-log.jsonl"),
        "NOBLIVION_STOP_CHECK_STATE": str(tmp_path / "stop-state"),
        "NOBLIVION_STOP_CHECK_MODE": "enforce",  # the lesson message is read from the block
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    tables = set()
    for cwd in (str(repo / "pkg" / "mod"), str(linked)):
        event = {"cwd": cwd}
        assert rh.session_env(env, event)["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
        assert gt.memory_dir(cwd, env) == folder
        assert gt.source_dirs(cwd, env) == [folder]
        tables.add(gt.table_path(gt.memory_dir(cwd, env), env))
        assert sc.memory_dir(cwd) == folder
        assert ch.session_env(env, event)["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
        assert ms.memory_dir(env, cwd) == folder
        assert fh.memory_dir(cwd) == folder
        assert er.memory_dir(env, cwd) == folder
    assert len(tables) == 1
    # End to end: the table built from the subfolder guards the worktree.
    start(repo / "pkg" / "mod", env)
    assert denied(run("guard_hook.py", bash_event(linked, "git stash pop"), env))
    mark = {"session_id": "s31", "prompt": CORRECTION, "cwd": str(linked)}
    assert run("stop_checks.py", mark, env, "--mark-correction").returncode == 0
    path = _transcript(tmp_path / "t.jsonl", linked, [("Bash", {"command": "echo fixed"})])
    stop = {"session_id": "s31", "transcript_path": str(path), "cwd": str(linked)}
    res = run("stop_checks.py", stop, env)
    assert f"feedback memory in {folder} with rule:" in json.loads(res.stdout)["reason"]
    assert not (home / ".claude" / "projects" / hc.project_slug(home)).exists()


def test_every_hook_keeps_the_start_project_after_cd(tmp_path, monkeypatch):
    """NOBLIVION-37: a session starts in a linked worktree of repo A and runs
    ``cd`` into repo B. The event ``cwd`` is B, ``CLAUDE_PROJECT_DIR`` is the
    worktree. Every hook names the folder of A's main checkout, as Claude
    Code does."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name in CLAUDE_ENVS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    repo_a = tmp_path / "code" / "a"
    repo_b = tmp_path / "code" / "b"
    for repo in (repo_a, repo_b):
        repo.mkdir(parents=True)
        git("init", "-q", str(repo))
        git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init")
    linked = tmp_path / "code" / "a-linked"
    git("-C", str(repo_a), "worktree", "add", "-q", str(linked))
    folder = home / ".claude" / "projects" / hc.project_slug(repo_a) / "memory"
    folder.mkdir(parents=True)
    (folder / "feedback_stash.md").write_text(
        memory("feedback_stash", STASH, "git stash pop", "git stash list")
    )
    b_folder = home / ".claude" / "projects" / hc.project_slug(repo_b) / "memory"
    b_folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(linked))
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NOBLIVION_DATA_DIR": str(data),
        "CLAUDE_PROJECT_DIR": str(linked),
        "NOBLIVION_STOP_CHECK_LOG": str(tmp_path / "stop-log.jsonl"),
        "NOBLIVION_STOP_CHECK_STATE": str(tmp_path / "stop-state"),
        "NOBLIVION_STOP_CHECK_MODE": "enforce",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    cwd = str(repo_b)
    event = {"cwd": cwd}
    assert rh.session_env(env, event)["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
    assert gt.memory_dir(cwd, env) == folder
    assert sc.memory_dir(cwd) == folder
    assert ch.session_env(env, event)["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
    assert ms.memory_dir(env, cwd) == folder
    assert fh.memory_dir(cwd) == folder
    assert er.memory_dir(env, cwd) == folder
    # End to end: the rule of A guards a command run from B.
    start(repo_b, env)
    assert denied(run("guard_hook.py", bash_event(repo_b, "git stash pop"), env))
    # Without CLAUDE_PROJECT_DIR the hooks use B's folder, which has no rule.
    plain = {k: v for k, v in env.items() if k != "CLAUDE_PROJECT_DIR"}
    assert gt.memory_dir(cwd, plain) == b_folder
    start(repo_b, plain)
    assert not denied(run("guard_hook.py", bash_event(repo_b, "git stash pop"), plain))
