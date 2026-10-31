# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen builds and the worker protocol, with a stub build (no store)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import buildrunner, runner

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "mcp").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    (repo / "hooks" / "recall_hook.py").write_text(
        "import json\n\ndef main(stdin, stdout, env):\n"
        "    event = json.loads(stdin.read())\n"
        "    stdout.write('- [id 1] note: ' + event['prompt'] + ' '\n"
        "                 + env['NOBLIVION_MEMORY_DIR'])\n"
    )
    (repo / "hooks" / "guard_hook.py").write_text(
        "import json\n\ndef main(stdin, stdout, env):\n"
        "    event = json.loads(stdin.read())\n"
        "    stdout.write(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny',\n"
        "        'permissionDecisionReason': event['tool_input']['command']}}))\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", "x"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_export_build_freezes_a_commit(tmp_path):
    repo, sha = make_repo(tmp_path)
    info = buildrunner.export_build(repo, "HEAD", tmp_path / "b1")
    assert info["commit"] == sha
    assert (tmp_path / "b1" / "hooks" / "recall_hook.py").is_file()
    assert buildrunner.build_version(tmp_path / "b1") == "9.9.9"
    assert oct((tmp_path / "b1").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(SystemExit):
        buildrunner.export_build(repo, "HEAD", tmp_path / "b1")  # not empty


def harness(tmp_path):
    repo, _ = make_repo(tmp_path)
    buildrunner.export_build(repo, "HEAD", tmp_path / "b1")
    h = buildrunner.Harness("B1", tmp_path / "b1", tmp_path / "root", sys.executable, None)
    h.config_dir.joinpath("projects", "-work-a", "memory").mkdir(parents=True)
    return h


def test_harness_environment_is_built_from_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("SOMETHING_ELSE", "v")
    env = harness(tmp_path).environ()
    assert "ANTHROPIC_API_KEY" not in env and "SOMETHING_ELSE" not in env
    assert env["NOBLIVION_DATA_DIR"].startswith(str(tmp_path / "root"))
    assert env["HOME"] == str(tmp_path / "root" / "home")
    assert env["PYTHONPATH"].endswith("src")


def test_worker_runs_the_hooks_of_its_build(tmp_path):
    h = harness(tmp_path)
    with buildrunner.Worker(h) as w:
        assert w.call("ping")["out"] == "pong"
        r = w.call("prompt", sid="s1", cwd="/work/a", prompt="hello there")
        assert r["ok"] and "hello there" in r["out"]
        assert str(h.config_dir / "projects" / "-work-a" / "memory") in r["out"]
        # a job may name a project folder apart from the cwd
        r = w.call("prompt", sid="s2", cwd="/tmp/x", prompt="p", project="-work-a")
        assert str(h.config_dir / "projects" / "-work-a" / "memory") in r["out"]
        g = w.call("guard", sid="s3", cwd="/work/a", tool="Bash", tool_input={"command": "rm x"})
        assert json.loads(g["out"])["hookSpecificOutput"]["permissionDecision"] == "deny"
        bad = w.call("error", sid="s4", cwd="/work/a", command="c", error="e")
        assert bad["ok"] is False and bad["error"]  # a missing hook fails the job, not the worker
        assert w.call("ping")["out"] == "pong"
    assert (h.config_dir / "projects" / "-work-a" / "s1.jsonl").is_file()  # the transcript


def test_worker_writes_nothing_outside_the_harness(tmp_path):
    h = harness(tmp_path)
    before = {p for p in Path(tmp_path).rglob("*")}
    with buildrunner.Worker(h) as w:
        w.call("prompt", sid="s1", cwd="/work/a", prompt="x")
    new = {p for p in Path(tmp_path).rglob("*")} - before
    assert all(str(tmp_path / "root") in str(p) for p in new)


def test_snapshot_and_changed_files(tmp_path):
    (tmp_path / "a").write_text("1")
    before = runner.snapshot_tree(tmp_path)
    assert runner.changed_files(before, runner.snapshot_tree(tmp_path)) == []
    (tmp_path / "a").write_text("22")
    (tmp_path / "b").write_text("x")
    assert len(runner.changed_files(before, runner.snapshot_tree(tmp_path))) == 2
    assert runner.changed_files(before, runner.snapshot_tree(tmp_path), lambda n: True) == []


def test_determinism_hash_ignores_time_and_order():
    rows = [
        {
            "qid": "a",
            "build": "B1",
            "channel": "prompt",
            "hit": True,
            "rank": 1,
            "rows": 2,
            "chars": 10,
            "size": 30,
            "ms": 5.0,
        },
        {
            "qid": "b",
            "build": "B1",
            "channel": "prompt",
            "hit": False,
            "rank": None,
            "rows": 0,
            "chars": 0,
            "size": 30,
            "ms": 7.0,
        },
    ]
    again = [dict(r, ms=r["ms"] + 100) for r in reversed(rows)]
    assert runner.determinism_hash(rows, 30) == runner.determinism_hash(again, 30)
    again[0]["hit"] = not again[0]["hit"]
    assert runner.determinism_hash(rows, 30) != runner.determinism_hash(again, 30)
