# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-30: the hooks find the memory folder the way Claude Code does.

Claude Code names the auto-memory folder after the git repository's main
checkout, so a subfolder and a linked worktree share the folder of the main
checkout. ``autoMemoryDirectory`` and ``CLAUDE_CONFIG_DIR`` move it. These
tests use real ``git init`` and ``git worktree add``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import recall_helpers
from hookload import HOOKS, load_hook
from noblivion import config as store_config

hc = load_hook("hook_config", "hook_config_memory_dir_resolver")
rh = load_hook("recall_hook", "recall_hook_memory_dir_resolver")
er = load_hook("error_recall_hook", "error_recall_hook_memory_dir_resolver")

TEST_CLASSIFICATION = "coherent"  # one of: "coherent" | "atomic" | "invariant"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

CLAUDE_ENVS = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
    "NOBLIVION_MEMORY_DIR",
    "NOBLIVION_RECALL_MEMORY_DIR",
    "NOBLIVION_ERROR_RECALL_MEMORY_DIR",
    "NOBLIVION_DATA_DIR",
    "CLAUDE_PLUGIN_DATA",
    "XDG_DATA_HOME",
    "NOBLIVION_CONFIG",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in CLAUDE_ENVS:
        monkeypatch.delenv(name, raising=False)
    no_policy = tmp_path / "no-managed-settings.json"
    for module in (hc, rh._sibling_module("hook_config"), er._CFG):
        monkeypatch.setattr(module, "managed_settings_path", lambda: no_policy)
    return home


def git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "work" / "main"
    main.mkdir(parents=True)
    git("init", "-q", str(main))
    git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "init")
    (main / "sub" / "deep").mkdir(parents=True)
    return main


def default_folder(home: Path, project: Path) -> Path:
    return home / ".claude" / "projects" / hc.project_slug(project) / "memory"


def test_subfolder_shares_the_repo_root_folder(home, repo):
    assert hc.resolve_memory_dir(str(repo / "sub" / "deep"), {}) == default_folder(home, repo)
    assert hc.resolve_memory_dir(str(repo), {}) == default_folder(home, repo)


def test_linked_worktree_shares_the_main_checkout_folder(home, repo, tmp_path):
    linked = tmp_path / "work" / "linked"
    git("-C", str(repo), "worktree", "add", "-q", str(linked))
    (linked / "pkg").mkdir()
    assert hc.resolve_memory_dir(str(linked), {}) == default_folder(home, repo)
    assert hc.resolve_memory_dir(str(linked / "pkg"), {}) == default_folder(home, repo)


def test_bare_repository_worktree_names_the_bare_folder(home, tmp_path):
    bare = tmp_path / "bare.git"
    git("init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed))
    git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", "init")
    git("-C", str(seed), "push", "-q", "origin", "HEAD")
    tree = tmp_path / "tree"
    git("-C", str(bare), "worktree", "add", "-q", str(tree))
    assert hc.resolve_memory_dir(str(tree), {}) == default_folder(home, bare)


def test_outside_git_the_cwd_names_the_folder(home, tmp_path):
    plain = tmp_path / "plain" / "dir"
    plain.mkdir(parents=True)
    assert hc.resolve_memory_dir(str(plain), {}) == default_folder(home, plain)


def test_a_broken_git_file_falls_back_to_the_folder_that_holds_it(home, tmp_path):
    # A .git file whose gitdir is missing: Claude Code keeps the folder that
    # holds .git (here the cwd), and never fails.
    broken = tmp_path / "broken"
    (broken / "sub").mkdir(parents=True)
    (broken / ".git").write_text("gitdir: /nowhere/.git/worktrees/x\n", encoding="utf-8")
    assert hc.resolve_memory_dir(str(broken), {}) == default_folder(home, broken)
    assert hc.resolve_memory_dir(str(broken / "sub"), {}) == default_folder(home, broken)
    # A worktree whose gitdir does not point back is not trusted either.
    (broken / ".git").write_text("garbage", encoding="utf-8")
    assert hc.resolve_memory_dir(str(broken), {}) == default_folder(home, broken)


def test_a_linked_worktree_whose_gitdir_does_not_point_back_keeps_its_own_folder(
    home, repo, tmp_path
):
    linked = tmp_path / "work" / "linked"
    git("-C", str(repo), "worktree", "add", "-q", str(linked))
    gitdir = (linked / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
    (Path(gitdir) / "gitdir").write_text("/elsewhere/.git\n", encoding="utf-8")
    assert hc.resolve_memory_dir(str(linked), {}) == default_folder(home, linked)


def test_auto_memory_directory_in_user_settings(home, repo):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMemoryDirectory": "~/my-memory"}), encoding="utf-8")
    assert hc.resolve_memory_dir(str(repo / "sub"), {}) == home / "my-memory"


def test_auto_memory_directory_in_project_settings_wins_over_user(home, repo, tmp_path):
    user = home / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"autoMemoryDirectory": "~/user-memory"}), encoding="utf-8")
    local = repo / ".claude" / "settings.local.json"
    local.parent.mkdir()
    target = tmp_path / "abs-memory"
    local.write_text(json.dumps({"autoMemoryDirectory": str(target)}), encoding="utf-8")
    assert hc.resolve_memory_dir(str(repo), {}) == target


def test_an_invalid_auto_memory_directory_gives_the_default_folder(home, repo):
    # A relative path is rejected, and Claude Code does not read the next
    # settings file: it uses the default folder.
    local = repo / ".claude" / "settings.local.json"
    local.parent.mkdir()
    local.write_text(json.dumps({"autoMemoryDirectory": "rel/dir"}), encoding="utf-8")
    user = home / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"autoMemoryDirectory": "~/user-memory"}), encoding="utf-8")
    assert hc.resolve_memory_dir(str(repo), {}) == default_folder(home, repo)
    for bad in ("~", "~/", "~/../escape"):
        local.write_text(json.dumps({"autoMemoryDirectory": bad}), encoding="utf-8")
        assert hc.resolve_memory_dir(str(repo), {}) == default_folder(home, repo)


def test_managed_settings_win(home, repo, tmp_path, monkeypatch):
    policy = tmp_path / "managed-settings.json"
    policy.write_text(json.dumps({"autoMemoryDirectory": "/srv/policy-memory"}), encoding="utf-8")
    monkeypatch.setattr(hc, "managed_settings_path", lambda: policy)
    user = home / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"autoMemoryDirectory": "~/user-memory"}), encoding="utf-8")
    assert hc.resolve_memory_dir(str(repo), {}) == Path("/srv/policy-memory")


def test_claude_config_dir_moves_the_folder_and_its_settings(home, repo, tmp_path):
    config = tmp_path / "cfg"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    want = config / "projects" / hc.project_slug(repo) / "memory"
    assert hc.resolve_memory_dir(str(repo / "sub"), env) == want
    # The user settings file is read from the config dir too.
    config.mkdir(exist_ok=True)
    (config / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "~/cfg-memory"}), encoding="utf-8"
    )
    assert hc.resolve_memory_dir(str(repo), env) == home / "cfg-memory"


def test_project_dir_name_needs_claude_config_dir(home, repo, tmp_path):
    config = tmp_path / "cfg"
    pinned = {"CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_PROJECT_DIR_NAME": "work"}
    assert hc.resolve_memory_dir(str(repo), pinned) == config / "projects" / "work" / "memory"
    alone = {"CLAUDE_CODE_PROJECT_DIR_NAME": "work"}
    assert hc.resolve_memory_dir(str(repo), alone) == default_folder(home, repo)
    for bad in ("con", "a/b", "x" * 65):
        env = dict(pinned, CLAUDE_CODE_PROJECT_DIR_NAME=bad)
        want = config / "projects" / hc.project_slug(repo) / "memory"
        assert hc.resolve_memory_dir(str(repo), env) == want


def test_noblivion_memory_dir_wins_over_everything(home, repo):
    env = {"NOBLIVION_MEMORY_DIR": "~/pinned", "CLAUDE_CONFIG_DIR": "/srv/cfg"}
    assert hc.resolve_memory_dir(str(repo), env) == home / "pinned"
    assert hc.resolve_memory_dir("relative", {}) is None


def test_project_slug_matches_claude_code():
    # Reference values computed with Claude Code's JavaScript rule (node).
    assert hc.project_slug("/srv/u/my_repo.v2") == "-srv-u-my-repo-v2"
    assert hc.project_slug("/a\U0001f600") == "-a--"  # one "-" per UTF-16 unit
    long_path = "/x/" + "a" * 250 + "/é\U0001f600"
    slug = hc.project_slug(long_path)
    assert slug == ("-x-" + "a" * 197) + "-wv2d6l"


def test_session_env_gives_a_subfolder_the_repo_folder(home, repo):
    folder = default_folder(home, repo)
    folder.mkdir(parents=True)
    env = rh.session_env({}, {"cwd": str(repo / "sub" / "deep")})
    assert env["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
    assert rh.session_root(env) == hc.project_slug(repo)


def test_the_recall_hook_logs_one_line_when_the_folder_is_missing(home, repo, tmp_path):
    recall_helpers.write_store_files(tmp_path / "data", 9)
    envp = recall_helpers.hook_env(tmp_path, HOME=str(home), NOBLIVION_RECALL_TIMEOUT_S="1.0")
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "x",
        "cwd": str(repo / "sub"),
    }
    p = subprocess.run(
        [sys.executable, str(HOOKS / "recall_hook.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=envp,
        timeout=20,
    )
    assert p.returncode == 0
    cache = tmp_path / "cache"
    lines = (cache / "memory-dir.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "memory_dir_missing root=" + hc.project_slug(repo) in lines[0]  # the repo, not sub
    # recall.log still has one line for the call.
    assert (cache / "recall.log").read_text(encoding="utf-8").count("\n") == 1
    # With the folder there, no line.
    default_folder(home, repo).mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(HOOKS / "recall_hook.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=envp,
        timeout=20,
    )
    assert len((cache / "memory-dir.log").read_text(encoding="utf-8").splitlines()) == 1


def test_error_recall_finds_the_main_checkout_folder_from_a_worktree(home, repo, tmp_path):
    linked = tmp_path / "work" / "linked"
    git("-C", str(repo), "worktree", "add", "-q", str(linked))
    folder = default_folder(home, repo)
    folder.mkdir(parents=True)
    assert er.memory_dir({}, str(linked)) == folder


def test_error_recall_logs_a_missing_folder(home, repo, tmp_path):
    log = tmp_path / "error-recall.jsonl"
    env = {"NOBLIVION_ERROR_RECALL_LOG": str(log)}
    assert er.memory_dir(env, str(repo)) == er.DEFAULT_MEMORY_DIR
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["note"] == "memory_dir_missing"
    assert rec["root"] == hc.project_slug(repo)


def test_store_default_folders_follow_claude_config_dir(home, tmp_path):
    config = tmp_path / "cfg"
    under = config / "projects" / "-p" / "memory"
    under.mkdir(parents=True)
    (home / ".claude" / "projects" / "-q" / "memory").mkdir(parents=True)
    custom = home / "custom-memory"
    custom.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "~/custom-memory"}), encoding="utf-8"
    )
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    assert store_config.default_memory_dirs(env) == sorted([under, custom])
