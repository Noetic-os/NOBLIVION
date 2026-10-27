# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard by process target (``hooks/process_targets.py``, ``protects_process``, NOBLIVION-127).

What these tests hold:

1. ``targets`` names the processes that a command would signal, for every shape
   of ``kill``, ``pkill``, ``killall`` and ``pgrep | xargs kill``, on a fake
   process table. It names none for a mention (quotes, comments, heredocs), for
   a command that runs on another machine, and for a PID it cannot know.
2. The reader of ``/proc`` parses ``stat`` and ``cmdline`` (a fake tree, no
   real process).
3. The guard hook denies a Bash call that would signal a protected process,
   with the rule as the reason, the process named, the deny log line, and the
   same override as a ``violates`` deny. It allows a call that signals other
   processes, it never reads the process table when no note has the field, a
   protected process is never a rewrite, and a slow read fails open with the
   time limit line.
4. The field is checked, written, read, carried by the table build (a bad one
   dropped alone) and kept by a dedup-style survivor.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook, load_hook_with_config

pt = load_hook("process_targets", "process_targets_t")
gh = load_hook_with_config("guard_hook", {}, "guard_hook_process_t")
gt = load_hook_with_config("guard_table", {}, "guard_table_process_t")
mf = load_hook_with_config("memory_fields", {}, "memory_fields_process_t")

P = pt.Proc
PROCS = [
    P(1, "init", ["/sbin/init"], 0, 1, 1),
    P(500, "ci-runner", ["/opt/ci/bin/ci-runner", "--listen"], 1, 500, 500),
    P(501, "ci-worker", ["/opt/ci/bin/ci-worker", "--job", "42"], 500, 500, 500),
    P(502, "bash", ["/bin/bash", "-e", "/opt/ci/work/step.sh"], 501, 500, 500),
    P(600, "python3", ["python3", "-m", "http.server", "8000"], 1, 600, 600),
    P(601, "sleep", ["sleep", "600"], 1, 601, 601),
    P(700, "node", ["node", "dev-server.js"], 1, 700, 700),
]
CI = {500, 501, 502}
ALL = {p.pid for p in PROCS}


def table():
    return pt.ProcTable(PROCS)


def found(command):
    return {p.pid for p in pt.targets(command, table())}


# 1. the targets ------------------------------------------------------------------
@pytest.mark.parametrize(
    "command,want",
    [
        # kill PID: every PID that exists
        ("kill 500 501 502", CI),
        ("kill -9 501", {501}),
        ("kill -KILL 501", {501}),
        ("kill -SIGTERM 501", {501}),
        ("kill -s KILL 501", {501}),
        ("kill -n 9 501", {501}),
        ("kill --signal TERM 501", {501}),
        ("kill 9999 501", {501}),
        # a negative PID is a process group; -1 is every process
        ("kill -- -500", CI),
        ("kill -TERM -500", CI),
        ("kill -9 -1", ALL),
        # kill $(pgrep ...) and the backtick form
        ("kill $(pgrep -f ci-worker)", {501}),
        ("kill -9 `pgrep ci-runner`", {500}),
        ("kill \"$(pgrep -f 'ci-(runner|worker)')\"", {500, 501}),
        ("kill $(pgrep -f ci-worker | head -1)", {501}),
        ("kill $(pgrep -P 500)", {501}),
        # pgrep ... | xargs kill
        ("pgrep -f ci- | xargs kill", {500, 501}),
        ("pgrep -f ci- | xargs -r -n 1 kill -9", {500, 501}),
        ("pgrep -f ci- | head -1 | xargs kill", {500, 501}),
        ("pgrep -f ci- | xargs -I{} kill {}", {500, 501}),
        # pkill: -f on the command line, else the process name
        ("pkill -f /opt/ci/", CI),
        ("pkill ci-runner", {500}),
        ("pkill -x ci-worker", {501}),
        ("pkill -i CI-RUNNER", {500}),
        ("pkill -fi /OPT/CI/BIN", {500, 501}),
        ("pkill -9 -f ci-worker", {501}),
        ("pkill --signal KILL -f ci-worker", {501}),
        ("pkill -P 500", {501}),
        ("pkill -g 500", CI),
        ("pkill -s 500 bash", {502}),
        ("pkill -v -f /opt/ci/", ALL - CI),
        ("pkill -u ci -f ci-worker", {501}),
        # killall: the name, or the base name of the first argument
        ("killall ci-runner", {500}),
        ("killall /opt/ci/bin/ci-runner", {500}),
        ("killall -r '^ci-'", {500, 501}),
        ("killall -I CI-WORKER", {501}),
        ("killall -g ci-worker", CI),
        ("killall -s KILL ci-runner", {500}),
        ("killall -9 ci-runner sleep", {500, 601}),
        # wrappers, lists, executed strings and substitutions
        ("sudo kill 501", {501}),
        ("sudo -u ci kill 501", {501}),
        ("timeout 5 kill 501", {501}),
        ("timeout -s KILL 5 kill 501", {501}),
        ("env A=1 kill 501", {501}),
        ("A=1 kill 501", {501}),
        ("nohup kill 501 &", {501}),
        ("time -p kill 501", {501}),
        ("command kill 501", {501}),
        ("/bin/kill 501", {501}),
        ("cd /tmp && kill 501", {501}),
        ("ps aux; kill 500 501 502", CI),
        ("true; pkill -f ci-worker || true", {501}),
        ("bash -c 'kill 501'", {501}),
        ('sh -c "kill $(pgrep ci-runner)"', {500}),
        ("bash -lc 'pkill -f ci-worker'", {501}),
        ("su -c 'kill 501'", {501}),
        ("eval kill 501", {501}),
        ("x=$(pkill -f ci-worker)", {501}),
        ("kill 501  # the job hangs", {501}),
    ],
)
def test_the_targets_of_a_command_that_signals(command, want):
    assert found(command) == want


@pytest.mark.parametrize(
    "command",
    [
        # no protected process: other targets, or none
        "kill 601",
        "kill 9999",
        "kill -l",
        "kill -l 9",
        "kill %1",
        "kill $PID",
        "kill 0",
        "kill $(cat app.pid)",
        "pkill -f http.server",
        "pkill sleep",
        "pkill -x ci",
        "pkill -P $$",
        "pkill -g 0",
        "pkill",
        "killall node",
        "killall ci",
        "killall -l",
        # a mention is not a command
        "echo 'kill 500'",
        'echo "pkill -f ci-runner"',
        "grep -n 'pkill -f ci-runner' notes.txt",
        "git commit -m 'kill 501 was wrong'",
        "# kill 500",
        "ls  # pkill -f ci-runner",
        "cat <<EOF\nkill 500\nEOF",
        "cat > notes.md <<'EOF'\npkill -f ci-runner\nEOF",
        # another machine or another process table
        "ssh example-host 'kill 501'",
        "ssh example-host kill 501",
        "docker exec box kill 501",
        "kubectl exec pod -- kill 501",
        # no signal at all
        "pgrep -f ci-runner",
        "ps aux | grep ci-runner",
        "man kill",
        "which pkill",
        "command -v kill",
        "skill 501",
        "ls -la",
    ],
)
def test_no_protected_target(command):
    assert not (found(command) & CI), command


def test_the_targets_of_a_mention_are_none_at_all():
    for command in ("echo 'kill 500'", "# kill 500", "ssh example-host 'kill 501'", "kill $PID"):
        assert found(command) == set(), command


def test_a_pid_that_does_not_exist_is_ignored_and_the_others_count():
    assert found("kill 9998 9999") == set()
    assert found("kill 9999 502") == {502}


def test_a_pattern_python_cannot_read_is_matched_as_text():
    procs = [P(10, "a[b", ["a[b", "x"]), P(11, "ab", ["ab"])]
    got = pt.targets("pkill 'a[b'", pt.ProcTable(procs))
    assert [p.pid for p in got] == [10]


def test_the_cheap_test_needs_a_kill_word():
    assert pt.may_signal("pkill -f x") and pt.may_signal("kill 1") and pt.may_signal("killall x")
    assert not pt.may_signal("ps aux") and not pt.may_signal("skill 1")


# 2. the reader of /proc ------------------------------------------------------------
def _fake_proc(root: Path, pid: int, comm: str, argv: list, ppid=1, pgid=None, sid=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    pg = pid if pgid is None else pgid
    sd = pid if sid is None else sid
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pg} {sd} 0 -1 4194560 100 0 0 0")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))


def test_the_reader_parses_stat_and_cmdline(tmp_path):
    _fake_proc(tmp_path, 500, "ci-runner", ["/opt/ci/bin/ci-runner", "--listen"])
    _fake_proc(tmp_path, 501, "odd (name) x", ["/opt/ci/bin/ci-worker"], ppid=500, pgid=500)
    _fake_proc(tmp_path, 2, "kthreadd", [])
    (tmp_path / "self").mkdir()
    (tmp_path / "501" / "junk").write_text("")
    tab = pt.ProcTable(root=str(tmp_path))
    p = tab.get(501)
    assert (p.pid, p.ppid, p.pgid, p.name) == (501, 500, 500, "odd (name) x")
    assert p.cmdline == "/opt/ci/bin/ci-worker"
    assert tab.get(2).cmdline == "[kthreadd]"
    assert tab.get(9999) is None
    assert {q.pid for q in tab.all()} == {2, 500, 501}
    assert {q.pid for q in tab.group(500)} == {500, 501}
    assert {q.pid for q in pt.targets("pkill -f ci-", pt.ProcTable(root=str(tmp_path)))} == {
        500,
        501,
    }
    assert {q.pid for q in pt.targets("kill 501 9999", pt.ProcTable(root=str(tmp_path)))} == {501}


def test_the_reader_skips_a_pid_it_is_told_to_skip(tmp_path):
    _fake_proc(tmp_path, 500, "ci-runner", ["ci-runner"])
    tab = pt.ProcTable(root=str(tmp_path), skip=(500,))
    assert tab.get(500) is None and tab.all() == []


def test_the_system_table_is_empty_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pt.ProcTable.system().all() == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_the_system_table_never_holds_the_hook_process():
    import os

    tab = pt.ProcTable.system()
    assert tab.get(os.getpid()) is None
    assert os.getpid() not in {p.pid for p in tab.all()}


# 3. the guard hook -----------------------------------------------------------------
PROTECT = r"/opt/ci/bin/ci-(?:runner|worker)\b"


def entry(mid, violates="", protects="", rewrite="", complies="", run_first=""):
    e: dict[str, Any] = {
        "id": mid,
        "rule": f"rule of {mid}.",
        "apply": f"apply of {mid}",
        "scope": "tool",
        "triggers": [],
        "violates": violates,
        "complies": complies,
        "run_first": run_first,
    }
    if protects:
        e["protects_process"] = protects
    if rewrite:
        e["rewrite"] = rewrite
    return e


TABLE = [
    # the measured note: it forbids one spelling and now protects the target
    entry("feedback_keep_ci_runner", violates=r"\bpkill\s+-f\b", protects=PROTECT),
    entry("feedback_no_force", violates=r"git\s+push\s+.*--force(?!-with-lease)"),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import os

    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setattr(gh, "_proc_table", table)


def write_table(env, entries):
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"version": 2, "entries": entries}))


@pytest.fixture
def env(tmp_path):
    e = {
        "NOBLIVION_GUARD_TABLE": str(tmp_path / "table.json"),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        "NOBLIVION_CONFIG": str(tmp_path / "no-config.json"),
        "NOBLIVION_TRUST_EVENTS": "0",
    }
    write_table(e, [dict(x) for x in TABLE])
    return e


def bash(cmd):
    return {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"}


def call(env, command, time_limit=None):
    out = io.StringIO()
    kw = {} if time_limit is None else {"time_limit": time_limit}
    assert gh.main(stdin=io.StringIO(json.dumps(bash(command))), stdout=out, environ=env, **kw) == 0
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def lines(env):
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.parametrize(
    "command",
    [
        "kill 76 501",  # the measured miss: ps, then kill of the CI job
        "kill 500 501 502",
        "kill -9 -500",
        "kill $(pgrep -f ci-worker)",
        "pgrep -f ci- | xargs kill",
        "pkill ci-runner",
        "killall ci-worker",
        "cd /tmp && sudo kill -9 501",
        "bash -c 'kill 500'",
    ],
)
def test_a_command_that_would_signal_a_protected_process_is_denied(env, command):
    out = call(env, command)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert reason.startswith(
        "rule of feedback_keep_ci_runner.\nApply: apply of feedback_keep_ci_runner"
    )
    assert "Memory: feedback_keep_ci_runner" in reason
    assert "which this rule protects" in reason and "/opt/ci/bin/ci-" in reason
    assert gh.MARKER in reason  # the override line of a violates deny
    row = lines(env)[-1]
    assert row["decision"] == "deny" and row["ids"] == ["feedback_keep_ci_runner"]
    assert row["process"]["feedback_keep_ci_runner"] in CI


@pytest.mark.parametrize(
    "command",
    ["kill 601", "kill 9999", "pkill sleep", "killall node", "echo 'kill 501'", "ls", "kill $PID"],
)
def test_a_command_that_signals_no_protected_process_runs(env, command):
    assert call(env, command) is None
    assert lines(env) == []


def test_the_violates_spelling_still_denies(env):
    out = call(env, "pkill -f no-such-process")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "which this rule protects" not in out["permissionDecisionReason"]  # no target


def test_the_override_of_a_violates_deny_works_for_a_process_deny(env):
    assert call(env, "kill 501  # guard-ok: the job hangs, I stop it on purpose") is None
    assert [r["decision"] for r in lines(env)] == ["override"]
    out = call(env, "kill 501  # guard-ok:")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert gh.BAD_MARKER_LINE in out["permissionDecisionReason"]


def test_a_retry_is_denied_every_time_and_counted(env):
    for _ in range(4):
        out = call(env, "kill 501")["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("Denied 4 times")


def test_a_note_without_violates_protects_too(env):
    write_table(env, [entry("project_keep_runner", protects=PROTECT)])
    assert call(env, "kill 500")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert call(env, "kill 600") is None


def test_a_protected_process_is_never_a_rewrite(env):
    write_table(
        env,
        [
            entry(
                "feedback_quiet_and_keep",
                violates=r"^pytest\b(?!.*\s-q\b)",
                rewrite="add-flag -q after pytest",
                protects=PROTECT,
            )
        ],
    )
    assert call(env, "pytest tests") is None  # a rewrite note: no deny with the switches off
    out = call(env, "pytest tests; kill 501")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out
    e = dict(env, NOBLIVION_GUARD_REWRITE="1")
    out = call(e, "pytest tests; kill 501")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out


def test_the_process_table_is_not_read_without_the_field(env, monkeypatch):
    def boom():
        raise AssertionError("the process table was read")

    monkeypatch.setattr(gh, "_proc_table", boom)
    write_table(env, [entry("feedback_no_force", violates=r"git\s+push\s+.*--force")])
    assert call(env, "kill 501") is None
    write_table(env, [dict(x) for x in TABLE])
    assert call(env, "ls -la") is None  # no kill word: not read either


def test_a_slow_process_table_fails_open_with_the_time_limit_line(env, monkeypatch):
    class Slow(pt.ProcTable):
        def all(self):
            time.sleep(2)
            return []

    monkeypatch.setattr(gh, "_proc_table", lambda: Slow([]))
    t0 = time.monotonic()
    doc = call(env, "pkill ci-runner", time_limit=0.2)
    assert time.monotonic() - t0 < 1.5
    assert doc == {
        "systemMessage": gh.TIMEOUT_LINE,
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": gh.TIMEOUT_LINE},
    }
    assert lines(env)[-1]["error"] == "timeout"


def test_off_linux_the_hook_finds_no_target_and_prints_nothing(env, monkeypatch):
    monkeypatch.setattr(gh, "_proc_table", lambda: gh._pt().ProcTable.system())
    monkeypatch.setattr(sys, "platform", "darwin")
    assert call(env, "kill 1") is None
    assert lines(env) == []


# 4. the field --------------------------------------------------------------------
BASE = "---\nname: x\ndescription: d\nmetadata:\n  type: feedback\n---\nbody\n"
FIELDS = {
    "rule": "Do not stop the CI runner.",
    "apply": "kill only the PID of a process you started",
    "scope": "tool",
    "triggers": ["kill", "pkill"],
    "protects_process": PROTECT,
}


def test_the_field_is_written_and_read_back():
    text = mf.write_fields(BASE, FIELDS)
    assert 'protects_process: "/opt/ci/bin/ci-(?:runner|worker)\\\\b"' in text
    got = mf.read_fields(text)
    assert got["protects_process"] == PROTECT
    assert mf.check_fields(got) == []
    assert (
        mf.read_fields(mf.write_fields(text, {"protects_process": ""})).get("protects_process", "")
        == ""
    )


@pytest.mark.parametrize(
    "pat,word",
    [
        ("[", "does not compile"),
        ("^$|", "empty command line"),
        (".", "every everyday process"),
        ("(a+)+", "nested quantifier"),
        ("x" * 301, "longer than 300"),
    ],
)
def test_the_memory_check_names_a_bad_field(pat, word):
    got = mf.check_fields(dict(FIELDS, protects_process=pat))
    assert any(word in p for p in got), got


def test_the_table_build_carries_the_field_and_drops_a_bad_one_alone(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    (folder / "feedback_keep.md").write_text(mf.write_fields(BASE, FIELDS))
    bad = dict(
        FIELDS,
        violates=r"\bpkill\s+-f\b",
        example_repeat="pkill -f runner",
        example_ok="kill 4711",
        protects_process="(a+)+",
    )
    (folder / "feedback_bad.md").write_text(mf.write_fields(BASE, bad))
    built = gt.build(folder)
    by_id = {e["id"]: e for e in built["entries"]}
    assert by_id["feedback_keep"]["protects_process"] == PROTECT
    assert "protects_process" not in by_id["feedback_bad"]
    assert by_id["feedback_bad"]["violates"] == r"\bpkill\s+-f\b"  # the deny stays
    rows = [r for r in built["skipped"] if r["id"] == "feedback_bad"]
    assert rows and "protects_process" in rows[0]
    assert "protects_process field is skipped" in gt.skipped_line(built)


def test_a_note_with_only_the_field_is_an_entry(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    line = "protects_process: " + json.dumps(PROTECT)
    (folder / "project_keep.md").write_text(f"---\nname: x\ndescription: d\n{line}\n---\nbody\n")
    built = gt.build(folder)
    assert [e.get("protects_process") for e in built["entries"]] == [PROTECT]
