# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stop-hook check table and the lesson capture.

The Stop hook blocks a stop ONCE with the unmet rules as the reason, never when
``stop_hook_active`` is true, and fails open on any fault. The UserPromptSubmit
mode marks a correction prompt; the Stop hook then asks for a memory with
``rule:`` and ``apply:`` fields. Every test points the home folder, the data dir,
the log, the marker folder and the memory folder at tmp paths, so no test
touches the live files.

The hook reads the ``stop.*`` config keys once, at import. The tests write a
``config.json`` in the tmp data dir and then load the module: the shared
checkout and the worktree prefix are fictional paths, and the deploy check gets
a fictional host only in the tests that need it (it is off by default).
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hookload import HOOKS, load_hook

HOOK = HOOKS / "stop_checks.py"
ALIAS = "stop_checks_t_stop_checks"

WT = "/home/user/wt-"  # the worktree prefix of the test config
SHARED = "/home/user/widget"  # the shared checkout of the test config
BASE_CONFIG = {"stop": {"worktree_prefixes": [WT], "shared_checkouts": [SHARED]}}
DEPLOY_HOSTS = ["alpha.example", "192.0.2.10"]

RULED = """---
name: feedback_x
description: a lesson
metadata:
  type: feedback
rule: Check the .env file for credentials before you say none exist.
apply: "grep -c NAME /home/user/widget/.env"
scope: tool
triggers: [ssh]
---
body
"""
NO_APPLY = RULED.replace('apply: "grep -c NAME /home/user/widget/.env"\n', "")


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
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
    monkeypatch.setenv("NOBLIVION_STOP_CHECK_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("NOBLIVION_STOP_CHECK_STATE", str(tmp_path / "state"))
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(mem))
    return data


def load_sc(data: Path, monkeypatch, config: dict | None = None):
    """Write ``config`` (default ``BASE_CONFIG``) to the data dir, then load the hook."""
    cfg = BASE_CONFIG if config is None else config
    (data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    mod = load_hook("stop_checks", ALIAS)
    # pytest's tmp_path lives under /tmp, which the hook skips as scratch space
    monkeypatch.setattr(mod, "SKIP_PREFIXES", ("/dev/", "/proc/"))
    return mod


@pytest.fixture
def sc(hook_env, monkeypatch):
    return load_sc(hook_env, monkeypatch)


@pytest.fixture
def sc_deploy(hook_env, monkeypatch):
    """The hook with deploy hosts in the config: the deploy check is on."""
    cfg = {"stop": dict(BASE_CONFIG["stop"], deploy_hosts=DEPLOY_HOSTS)}
    return load_sc(hook_env, monkeypatch, cfg)


class T:
    """A transcript builder in the Claude Code JSONL shape."""

    def __init__(self, cwd="/home/user"):
        self.rows, self.n, self.cwd = [], 0, cwd

    def prompt(self, text):
        self.rows.append(
            {
                "type": "user",
                "isSidechain": False,
                "cwd": self.cwd,
                "message": {"role": "user", "content": text},
            }
        )
        return self

    def tool(self, name, error=False, **inp):
        self.n += 1
        tid = f"toolu_{self.n}"
        use = {"type": "tool_use", "id": tid, "name": name, "input": inp}
        self.rows.append(
            {
                "type": "assistant",
                "isSidechain": False,
                "cwd": self.cwd,
                "message": {"role": "assistant", "content": [use]},
            }
        )
        res = {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": "ok"}
        self.rows.append(
            {
                "type": "user",
                "isSidechain": False,
                "cwd": self.cwd,
                "message": {"role": "user", "content": [res]},
            }
        )
        return self

    def bash(self, command, **kw):
        return self.tool("Bash", command=command, **kw)

    def say(self, text):
        self.rows.append(
            {
                "type": "assistant",
                "isSidechain": False,
                "cwd": self.cwd,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
        return self

    def write(self, path):
        path.write_text("\n".join(json.dumps(r) for r in self.rows) + "\n", encoding="utf-8")
        return path

    def turn(self, sc):
        return sc.turn_from_rows(self.rows)


def _ctx(sc, **kw):
    kw.setdefault("live", False)
    kw.setdefault("memory_dir", sc.memory_dir())
    return sc.Ctx(**kw)


def _run_hook(event, args=(), env=None):
    return subprocess.run(
        [sys.executable, str(HOOK), *args],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **(env or {})},
    )


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True)


def _commit(repo: Path, *args: str) -> None:
    _git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", *args)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", str(path))
    _commit(path, "--allow-empty", "-m", "init")
    return path


# ---------------------------------------------------------------- bounds and fail-open


def test_stop_hook_active_never_blocks(tmp_path, sc):
    t = T().prompt("do it").bash("echo hi").say("I deferred the docs fix.")
    path = t.write(tmp_path / "t.jsonl")
    res = _run_hook({"session_id": "s1", "transcript_path": str(path), "stop_hook_active": True})
    assert res.returncode == 0 and res.stdout.strip() == ""
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["verdict"] == "pass-stop-hook-active"


def test_same_stop_blocks_once_when_not_active(tmp_path, sc):
    t = T().prompt("do it").bash("echo hi").say("I deferred the docs fix.")
    path = t.write(tmp_path / "t.jsonl")
    res = _run_hook({"session_id": "s1", "transcript_path": str(path), "stop_hook_active": False})
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["decision"] == "block" and "push notification" in out["reason"]
    assert "once" in out["reason"]


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '{"transcript_path": 5}'])
def test_fail_open_on_bad_input(stdin):
    res = subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0 and res.stdout.strip() == ""


def test_fail_open_on_missing_transcript(tmp_path):
    res = _run_hook({"session_id": "s1", "transcript_path": str(tmp_path / "nope.jsonl")})
    assert res.returncode == 0 and res.stdout.strip() == ""


def test_fail_open_on_internal_error(tmp_path, sc, monkeypatch, capsys):
    path = T().prompt("x").say("I deferred it.").write(tmp_path / "t.jsonl")

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(sc, "read_turn", boom)
    event = {"session_id": "s1", "transcript_path": str(path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert sc.main([]) == 0
    assert capsys.readouterr().out == ""
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["verdict"] == "fail-open" and row["error"] == "RuntimeError"


def test_the_time_limit_is_not_swallowed_by_a_fail_open_handler(sc, monkeypatch):
    """``Timeout`` is a BaseException, so the ``except Exception`` of
    ``has_rule_apply`` cannot swallow the one-shot alarm."""

    class SlowFields:
        @staticmethod
        def read_fields(_text):
            time.sleep(2)
            return {}

    monkeypatch.setattr(sc, "_fields_mod", lambda: SlowFields)
    old = signal.signal(signal.SIGALRM, sc._alarm)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    t0 = time.monotonic()
    try:
        with pytest.raises(sc.Timeout):
            sc.has_rule_apply("---\nrule: x\napply: y\n---\n")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert time.monotonic() - t0 < 1.5
    assert issubclass(sc.Timeout, BaseException) and not issubclass(sc.Timeout, Exception)


def test_fail_open_on_time_limit(tmp_path, sc, monkeypatch, capsys):
    path = T().prompt("x").say("I deferred it.").write(tmp_path / "t.jsonl")
    monkeypatch.setattr(sc, "TIME_LIMIT", 0.2)

    def slow(*a, **k):
        time.sleep(2)

    monkeypatch.setattr(sc, "read_turn", slow)
    event = {"session_id": "s1", "transcript_path": str(path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    t0 = time.monotonic()
    assert sc.main([]) == 0
    assert time.monotonic() - t0 < 1.5
    assert capsys.readouterr().out == ""


def test_a_broken_checker_does_not_stop_the_others(sc, monkeypatch):
    def bad(turn, ctx):
        raise ValueError("x")

    monkeypatch.setattr(sc, "CHECKS", (sc.Check("bad", "r", bad),) + tuple(sc.CHECKS))
    turn = T().prompt("x").say("I deferred the docs fix.").turn(sc)
    ctx = _ctx(sc)
    fired = sc.run_checks(turn, ctx)
    assert "notify" in fired and "bad" not in fired and ctx.notes["bad_error"] == "ValueError"


def test_large_transcript_is_fast(tmp_path, sc):
    t = T().prompt("start")
    big = "x" * 200_000
    for i in range(150):  # about 30 MB of tool results after the prompt
        content = [{"type": "tool_result", "tool_use_id": f"t{i}", "content": big}]
        t.rows.append({"type": "user", "isSidechain": False, "message": {"content": content}})
        t.rows.append({"type": "attachment", "attachment": {"x": big[:1000]}})
    t.bash("echo hi").say("Done.")
    path = t.write(tmp_path / "big.jsonl")
    assert path.stat().st_size > 25_000_000
    t0 = time.monotonic()
    res = _run_hook({"session_id": "s1", "transcript_path": str(path)})
    assert res.returncode == 0
    assert time.monotonic() - t0 < 2.5  # includes interpreter start


def test_turn_stops_at_the_last_prompt_and_skips_stop_feedback(tmp_path, sc):
    t = (
        T()
        .prompt("first")
        .tool("Write", file_path=f"{WT}x/a.py", content="x")
        .say("one")
        .prompt("second")
        .bash("echo 1")
        .say("two")
        .prompt("Stop hook feedback:\n- fix it")
        .bash("echo 2")
        .say("three")
    )
    turn = sc.read_turn(str(t.write(tmp_path / "t.jsonl")))
    assert turn.prompt == "second" and [n for _, n, _ in turn.calls] == ["Bash", "Bash"]
    assert turn.reply == "three" and not turn.truncated


def test_failed_tool_calls_are_not_changes(sc):
    t = T().prompt("x").tool("Write", error=True, file_path=f"{WT}x/a.py", content="x").say("ok")
    assert sc.changes_of(t.turn(sc)) == []


# ---------------------------------------------------------------- (a) commit


def test_commit_live_dirty_worktree_blocks_then_clean_passes(tmp_path, sc):
    repo = _git_repo(tmp_path / "wt-demo")
    f = repo / "a.py"
    f.write_text("x = 1\n")
    t = (
        T(cwd=str(repo))
        .prompt("do it")
        .tool("Write", file_path=str(f), content="x = 1\n")
        .say("Done.")
    )
    ctx = _ctx(sc, live=True, deadline=time.monotonic() + 5)
    reason = sc.check_commit(t.turn(sc), ctx)
    assert reason and "Commit them locally" in reason and "a.py" in reason
    _git("-C", str(repo), "add", "a.py")
    _commit(repo, "-m", "a")
    assert sc.check_commit(t.turn(sc), _ctx(sc, live=True, deadline=time.monotonic() + 5)) is None


def test_commit_live_ignores_other_dirty_files(tmp_path, sc):
    repo = _git_repo(tmp_path / "wt-demo")
    (repo / "other.py").write_text("dirty from someone else\n")
    f = repo / "a.py"
    f.write_text("x\n")
    _git("-C", str(repo), "add", "a.py")
    _commit(repo, "-m", "a")
    t = (
        T(cwd=str(repo))
        .prompt("x")
        .tool("Edit", file_path=str(f), old_string="x", new_string="x")
        .say("ok")
    )
    assert sc.check_commit(t.turn(sc), _ctx(sc, live=True, deadline=time.monotonic() + 5)) is None


def test_shared_checkout_is_reported_not_asked_to_commit(tmp_path, hook_env, monkeypatch):
    # Adapted: the shared checkout comes from the config key stop.shared_checkouts.
    repo = _git_repo(tmp_path / "widget")
    sc = load_sc(hook_env, monkeypatch, {"stop": {"shared_checkouts": [str(repo)]}})
    f = repo / "b.py"
    f.write_text("y\n")
    t = T().prompt("x").tool("Write", file_path=str(f), content="y\n").say("ok")
    reason = sc.check_commit(t.turn(sc), _ctx(sc, live=True, deadline=time.monotonic() + 5))
    assert "shared checkout" in reason and "Do not commit there" in reason
    assert "Commit them locally" not in reason


def test_no_shared_checkout_by_default(tmp_path, hook_env, monkeypatch):
    # New contract: with no config, no checkout is shared, so an edit asks for a commit.
    repo = _git_repo(tmp_path / "widget")
    sc = load_sc(hook_env, monkeypatch, {})
    assert sc.SHARED_CHECKOUTS == ()
    f = repo / "b.py"
    f.write_text("y\n")
    t = T().prompt("x").tool("Write", file_path=str(f), content="y\n").say("ok")
    reason = sc.check_commit(t.turn(sc), _ctx(sc, live=True, deadline=time.monotonic() + 5))
    assert "Commit them locally" in reason and "shared checkout" not in reason


def test_commit_replay_proxy(sc):
    wt = f"{WT}proxy-demo"
    base = T(cwd=wt).prompt("x").tool("Write", file_path=f"{wt}/a.py", content="x")
    assert sc.check_commit(base.turn(sc), _ctx(sc))  # change, no commit
    done = (
        T(cwd=wt)
        .prompt("x")
        .tool("Write", file_path=f"{wt}/a.py", content="x")
        .bash(f'git -C {wt} -c user.name="$(git config user.name)" commit -q -m m')
    )
    assert sc.check_commit(done.turn(sc), _ctx(sc)) is None  # commit after the change
    same = (
        T(cwd=wt)
        .prompt("x")
        .bash(f"cd {wt} && sed -i 's/a/b/' a.py && git add a.py && git commit -m m")
    )
    assert sc.check_commit(same.turn(sc), _ctx(sc)) is None  # same command, commit after write
    restored = (
        T(cwd=wt)
        .prompt("x")
        .bash(
            f"cd {wt} && cp a.py /tmp/a.bak && sed -i 's/a/b/' a.py && pytest -q; "
            "cp /tmp/a.bak a.py"
        )
    )
    assert sc.check_commit(restored.turn(sc), _ctx(sc)) is None


def test_worktree_prefix_is_off_by_default(hook_env, monkeypatch):
    # New contract: with no stop.worktree_prefixes, a path whose folder is gone is no change.
    sc = load_sc(hook_env, monkeypatch, {})
    t = T().prompt("x").tool("Write", file_path=f"{WT}gone/a.py", content="x")
    assert sc.changes_of(t.turn(sc)) == []
    assert sc.check_commit(t.turn(sc), _ctx(sc)) is None


def test_scratch_and_non_repo_paths_are_not_changes(sc):
    t = (
        T()
        .prompt("x")
        .tool("Write", file_path="/tmp/claude-1000/x/scratchpad/a.py", content="x")
        .tool("Write", file_path="/home/user/.claude/handoff/n.md", content="x")
        .bash(f"ssh alpha.example 'cd {SHARED} && git apply /tmp/p.patch > {SHARED}/x'")
    )
    assert sc.changes_of(t.turn(sc)) == []


# ---------------------------------------------------------------- Bash parsing units


@pytest.mark.parametrize(
    "cmd,cwd,expected",
    [
        (f"echo x > {WT}a/f.py", "/", [f"{WT}a/f.py"]),
        (f"cd {WT}a && sed -i 's/a/b/' src/x.py", "/", [f"{WT}a/src/x.py"]),
        (f"W={WT}a; cat > $W/t.py <<'EOF'\nimport os\nEOF", "/", [f"{WT}a/t.py"]),
        (
            f"python3 - {WT}a/h.py <<'EOF'\nimport sys\np=sys.argv[1]\ns=open(p).read()\n"
            "open(p,'w').write(s)\nEOF",
            "/",
            [f"{WT}a/h.py"],
        ),
        (
            f"python3 -c \"import pathlib; pathlib.Path('{WT}a/q.py').write_text('x')\"",
            "/",
            [f"{WT}a/q.py"],
        ),
        (
            f"python3 - <<'EOF'\nsrc=open('{WT}a/read.py').read()\n"
            "open('/tmp/out.txt','w').write(src)\nEOF",
            "/",
            ["/tmp/out.txt"],
        ),
        (f'grep -n "ssh\\|rsync" {WT}a/x.py | head -4', "/", []),
        (f"cp {WT}a/x.py /tmp/x.bak", "/", ["/tmp/x.bak"]),
        (f"git -C {WT}a apply /tmp/p.patch", "/", [f"{WT}a/"]),
        (f"git -C {WT}a apply --check /tmp/p.patch", "/", []),
        (f"git -C {WT}a checkout -q --detach origin/main", "/", []),
        (f"cat {WT}a/x.py 2>&1 | tail -3", "/", []),
    ],
)
def test_bash_write_paths(sc, cmd, cwd, expected):
    assert sc.bash_write_paths(cmd, cwd) == expected


@pytest.mark.parametrize(
    "cmd,cwd,expected",
    [
        (f"git -C {WT}a commit -m x", "/", [f"{WT}a"]),
        (
            f"cd {WT}b && git add -A && git commit -q -F - <<'EOF'\nmsg\nEOF",
            "/",
            [f"{WT}b"],
        ),
        ('git -c user.name="$(git config user.name)" commit -q -m m', f"{WT}c", [f"{WT}c"]),
        (f"env -C {WT}d git commit -m x", "/", [f"{WT}d"]),
        ("git log --oneline -3 | grep commit", f"{WT}e", []),
        ("echo 'git commit' > /tmp/x", "/", []),
        (
            f'cd {WT}f\ngit -c user.name="A B" -c user.email="x@y" commit -q -m "fix: x"',
            "/",
            [f"{WT}f"],
        ),
    ],
)
def test_commit_dirs(sc, cmd, cwd, expected):
    assert sc.commit_dirs(cmd, cwd) == expected


# ---------------------------------------------------------------- (b) tests


def test_tests_fires_on_code_change_without_test(sc):
    t = (
        T()
        .prompt("x")
        .tool("Edit", file_path=f"{WT}a/src/m.py", old_string="a", new_string="b")
        .say("ok")
    )
    assert "no test ran" in sc.check_tests(t.turn(sc), _ctx(sc))


def test_tests_reason_names_the_configured_test_command(hook_env, monkeypatch):
    # New contract: stop.test_command sets the command the reason names.
    cfg = {"stop": dict(BASE_CONFIG["stop"], test_command="make check")}
    sc = load_sc(hook_env, monkeypatch, cfg)
    t = T().prompt("x").tool("Edit", file_path=f"{WT}a/src/m.py", old_string="a", new_string="b")
    assert "`make check`" in sc.check_tests(t.turn(sc), _ctx(sc))


def test_tests_passes_when_a_test_runs_after(sc):
    t = (
        T()
        .prompt("x")
        .tool("Edit", file_path=f"{WT}a/src/m.py", old_string="a", new_string="b")
        .bash(f"{SHARED}/.venv/bin/python -m pytest -q --no-cov {WT}a/tests/t.py")
    )
    assert sc.check_tests(t.turn(sc), _ctx(sc)) is None


def test_tests_fires_when_the_test_ran_before_the_last_change(sc):
    t = (
        T()
        .prompt("x")
        .bash("pytest -q tests/")
        .tool("Edit", file_path=f"{WT}a/src/m.py", old_string="a", new_string="b")
    )
    assert sc.check_tests(t.turn(sc), _ctx(sc))


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test",
        "go test ./...",
        "cargo test",
        "make test",
        "npx vitest run",
        "bash -c 'cd /x && pytest -q'",
    ],
)
def test_test_command_shapes(sc, cmd):
    assert sc.TEST_RE.search(cmd)


def test_tests_ignores_docs_and_scratch(sc):
    t = (
        T()
        .prompt("x")
        .tool("Write", file_path=f"{WT}a/docs/a.md", content="x")
        .tool("Write", file_path="/tmp/claude-1000/s/scratchpad/a.py", content="x")
    )
    assert sc.check_tests(t.turn(sc), _ctx(sc)) is None


# ---------------------------------------------------------------- (c) notify


@pytest.mark.parametrize(
    "reply",
    [
        "I deferred the documentation fix to the next session.",
        "I stopped before the rebuild because context is high.",
        "I am stopping here. Run /clear.",
        "The choice changes the setup, so I cannot proceed on a guess.",
        "- Deferred: the ticket closeout.",
    ],
)
def test_notify_fires_without_push(sc, reply):
    assert sc.check_notify(T().prompt("x").say(reply).turn(sc), _ctx(sc))


def test_notify_passes_with_push(sc):
    t = (
        T()
        .prompt("x")
        .tool("PushNotification", message="not done: docs")
        .say("I deferred the docs fix.")
    )
    assert sc.check_notify(t.turn(sc), _ctx(sc)) is None


@pytest.mark.parametrize(
    "reply",
    [
        "The heavy lane is SKIPPED by design.",
        "PROJ-12 stays blocked until the ledger is fixed.",
        "Tests: `12 passed, 3 skipped`.",
        "```\n3 skipped\nI deferred\n```\nAll done.",
        "The merge is blocked on a stale verdict; the watcher reports when it clears.",
    ],
)
def test_notify_does_not_fire_on_other_work(sc, reply):
    assert sc.check_notify(T().prompt("x").say(reply).turn(sc), _ctx(sc)) is None


# ---------------------------------------------------------------- (d) deploy
# Adapted: the private merge wrapper is gone, so the merge command is ``gh pr merge``; the
# deploy check reads the hosts of the config key stop.deploy_hosts (fictional here).


MERGE = "gh pr merge 2205 --squash 2>&1 | tail -5"


def test_deploy_is_off_by_default(sc):
    # New contract: no stop.deploy_hosts in the config, so the deploy check never fires.
    assert sc.DEPLOY_HOSTS == ()
    t = T().prompt("merge it").bash(MERGE).say("PR #2205 is merged. Squash commit abc.")
    assert sc.check_deploy(t.turn(sc), _ctx(sc)) is None


def test_deploy_fires_after_merge_without_check(sc_deploy):
    sc = sc_deploy
    t = T().prompt("merge it").bash(MERGE).say("PR #2205 is merged. Squash commit abc.")
    reason = sc.check_deploy(t.turn(sc), _ctx(sc))
    assert "deploy check" in reason and "alpha.example" in reason


@pytest.mark.parametrize(
    "probe",
    [
        'ssh alpha.example "docker exec app-db cat /app/BUILD_SHA"',
        "timeout 30 ssh deploy@192.0.2.10 'docker inspect -f {{.Created}} app-db'",
        "gh run list --workflow deploy-staging.yml --limit 3",
    ],
)
def test_deploy_passes_with_a_check_after(sc_deploy, probe):
    sc = sc_deploy
    t = T().prompt("merge it").bash(MERGE).bash(probe).say("PR #2205 is merged and live.")
    assert sc.check_deploy(t.turn(sc), _ctx(sc)) is None


def test_deploy_probe_on_another_host_does_not_count(sc_deploy):
    sc = sc_deploy
    probe = 'ssh beta.example "docker exec app-db cat /app/BUILD_SHA"'
    t = T().prompt("merge it").bash(MERGE).bash(probe).say("PR #2205 is merged and live.")
    assert sc.check_deploy(t.turn(sc), _ctx(sc))


def test_deploy_check_in_the_same_command_counts(sc_deploy):
    sc = sc_deploy
    cmd = MERGE + " && ssh alpha.example 'docker exec app-db cat /app/BUILD_SHA'"
    t = T().prompt("m").bash(cmd).say("Merged.")
    assert sc.check_deploy(t.turn(sc), _ctx(sc)) is None


@pytest.mark.parametrize(
    "reply",
    [
        "The merge gate refused and merged nothing.",
        "PR #2205 is not merged yet.",
        "PR #2205 is merged. The deploy is not verified yet; the build runs now.",
        "Merged. The change touches tests only, so it does not deploy.",
    ],
)
def test_deploy_reply_shapes_that_pass(sc_deploy, reply):
    sc = sc_deploy
    t = T().prompt("m").bash(MERGE).say(reply)
    assert sc.check_deploy(t.turn(sc), _ctx(sc)) is None


def test_deploy_needs_a_merge_command_this_turn(sc_deploy):
    sc = sc_deploy
    heredoc = "cat > /home/user/.claude/handoff/h.md <<'EOF'\nnext: gh pr merge 2197 --squash\nEOF"
    t = T().prompt("status?").bash(heredoc).say("PR #2195 is merged.")
    assert sc.check_deploy(t.turn(sc), _ctx(sc)) is None


# ---------------------------------------------------------------- correction marker


@pytest.mark.parametrize(
    "prompt",
    [
        "no, that is wrong",
        "No. Use the worktree.",
        "wrong folder this is the right one: /x",
        "WRONG API key, use the other one",
        "I told you to commit first",
        "I have told you often enough.",
        "you did it again",
        "stop doing that",
        "stop asking me and fix it",
        "why are you stopping? keep going!!!",
        "you are using the wrong key - try again",
        "that is not what I asked",
        "You are not communicating in the way I asked.",
        "I asked you to FIX it!",
    ],
)
def test_correction_positive(sc, prompt):
    assert sc.is_correction(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "no push, no PR",
        "No push, no PR, no merge. Build locally.",
        "no need to ask, proceed",
        "try again",
        "credentials added to .env - try again",
        "stop the watcher.",
        "whats wrong with alpha? I cant connect",
        "yes do it, and review it closely - for anything you missed.",
        "lift the freeze - what the hell is 4111 anyway?",
        "<task-notification> no, wrong </task-notification>",
        "Stop hook feedback:\n- wrong",
        "continue",
        "",
    ],
)
def test_correction_negative(sc, prompt):
    assert sc.is_correction(prompt) is None


def test_mark_hook_writes_and_clears_marker(tmp_path):
    marker = tmp_path / "state" / "sess-1.correction.json"
    res = _run_hook(
        {"session_id": "sess-1", "prompt": "no, that is wrong: the value is hunter2-value"},
        args=["--mark-correction"],
    )
    assert res.returncode == 0 and res.stdout == "" and marker.exists()
    assert json.loads(marker.read_text())["phrase"].lower().startswith("no")
    # the marker holds no prompt text, only the matched phrase
    assert sorted(json.loads(marker.read_text())) == ["phrase", "ts"]
    assert "hunter2-value" not in marker.read_text()
    _run_hook({"session_id": "sess-1", "prompt": "continue"}, args=["--mark-correction"])
    assert not marker.exists()


def test_mark_hook_fails_open():
    res = subprocess.run(
        [sys.executable, str(HOOK), "--mark-correction"],
        input="{bad",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


# ---------------------------------------------------------------- lesson check


def _stop(tmp_path, t, session="sess-2", active=False):
    path = t.write(tmp_path / "t.jsonl")
    event = {"session_id": session, "transcript_path": str(path), "stop_hook_active": active}
    return _run_hook(event)


def _mark(session="sess-2", prompt="no, that is wrong"):
    return _run_hook({"session_id": session, "prompt": prompt}, args=["--mark-correction"])


def _mem() -> Path:
    return Path(os.environ["NOBLIVION_MEMORY_DIR"])


def test_lesson_blocks_once_then_marker_is_gone(tmp_path):
    _mark()
    res = _stop(tmp_path, T().prompt("no, that is wrong").bash("echo fixed").say("Fixed it."))
    out = json.loads(res.stdout)
    assert "corrected you" in out["reason"] and "rule:" in out["reason"]
    assert not (tmp_path / "state" / "sess-2.correction.json").exists()
    res2 = _stop(tmp_path, T().prompt("no, that is wrong").bash("echo fixed").say("Fixed it."))
    assert res2.stdout.strip() == ""


def test_lesson_passes_when_a_ruled_memory_was_written(tmp_path):
    _mark()
    f = _mem() / "feedback_check_env.md"
    f.write_text(RULED)
    t = (
        T()
        .prompt("no, that is wrong")
        .tool("Write", file_path=str(f), content=RULED)
        .say("Saved the lesson.")
    )
    res = _stop(tmp_path, t)
    assert res.stdout.strip() == ""
    assert not (tmp_path / "state" / "sess-2.correction.json").exists()
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["notes"]["lesson"].startswith("captured")


def test_lesson_still_blocks_when_the_memory_lacks_apply(tmp_path):
    _mark()
    f = _mem() / "feedback_half.md"
    f.write_text(NO_APPLY)
    t = (
        T()
        .prompt("no, that is wrong")
        .tool("Write", file_path=str(f), content=NO_APPLY)
        .say("Saved.")
    )
    assert "corrected you" in json.loads(_stop(tmp_path, t).stdout)["reason"]


def test_lesson_counts_a_memory_written_by_bash(tmp_path):
    _mark()
    f = _mem() / "feedback_bash.md"
    f.write_text(RULED)
    t = T().prompt("no, that is wrong").bash(f"cat > {f} <<'EOF'\n{RULED}EOF").say("Saved.")
    assert _stop(tmp_path, t).stdout.strip() == ""


def test_lesson_counts_a_memory_named_in_a_python_command(tmp_path):
    _mark()
    f = _mem() / "feedback_py.md"
    f.write_text(RULED)
    cmd = (
        "python3 - <<'EOF'\nimport pathlib, os\nd = pathlib.Path(os.environ['M'])\n"
        "(d / 'feedback_py.md').write_text(TEXT)\nEOF"
    )
    t = T().prompt("no, that is wrong").bash(cmd).say("Saved.")
    assert _stop(tmp_path, t).stdout.strip() == ""


def test_lesson_ignores_a_memory_another_session_touched(tmp_path):
    _mark()
    time.sleep(0.05)
    (_mem() / "feedback_other.md").write_text(RULED)
    t = T().prompt("no, that is wrong").bash("echo fixed").say("Fixed.")
    assert "corrected you" in json.loads(_stop(tmp_path, t).stdout)["reason"]


def test_lesson_index_files_do_not_count(tmp_path):
    _mark()
    f = _mem() / "MEMORY.md"
    f.write_text(RULED)
    t = T().prompt("no, that is wrong").tool("Write", file_path=str(f), content=RULED).say("Saved.")
    assert "corrected you" in json.loads(_stop(tmp_path, t).stdout)["reason"]


def test_lesson_needs_a_marker(sc):
    t = T().prompt("please continue").say("Done.")
    assert sc.check_lesson(t.turn(sc), _ctx(sc, marker=None)) is None


def test_lesson_stop_hook_active_never_blocks(tmp_path):
    _mark()
    res = _stop(tmp_path, T().prompt("no, that is wrong").say("ok"), active=True)
    assert res.stdout.strip() == ""


# ---------------------------------------------------------------- enable and shadow


def test_disabled_checks_log_as_shadow(tmp_path):
    t = T().prompt("x").say("I deferred the docs fix.")
    res = _run_hook(
        {"session_id": "s", "transcript_path": str(t.write(tmp_path / "t.jsonl"))},
        env={"NOBLIVION_STOP_CHECKS": "commit,tests"},
    )
    assert res.stdout.strip() == ""
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[-1])
    assert row["verdict"] == "pass" and row["shadow"] == ["notify"]


def test_shadow_mode_never_blocks(tmp_path):
    t = T().prompt("x").say("I deferred the docs fix.")
    res = _run_hook(
        {"session_id": "s", "transcript_path": str(t.write(tmp_path / "t.jsonl"))},
        env={"NOBLIVION_STOP_CHECK_MODE": "shadow"},
    )
    assert res.stdout.strip() == ""


def test_enabled_checks_parse(sc, monkeypatch):
    monkeypatch.setenv("NOBLIVION_STOP_CHECKS", "all")
    assert sc.enabled_checks() == {c.name for c in sc.CHECKS}
    monkeypatch.setenv("NOBLIVION_STOP_CHECKS", "none")
    assert sc.enabled_checks() == set()
    monkeypatch.delenv("NOBLIVION_STOP_CHECKS")
    assert sc.enabled_checks() == set(sc.DEFAULT_ON)


def test_has_rule_apply_uses_the_fields_reader(sc):
    assert sc._fields_mod() is not None  # the fields module sits next to this hook
    assert sc.has_rule_apply(RULED) and not sc.has_rule_apply(NO_APPLY)
