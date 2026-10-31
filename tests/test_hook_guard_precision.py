# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard precision (NOBLIVION-155).

(a) A deny rule with 3 or more overrides and an override rate of 50 percent or
    more in 14 days shows as a row only. The session-start line names it. The
    credential guard is never quarantined.
(b) ``where:`` (path globs over the working directory, ``git -C`` and ``cd``
    targets), and the default for a note that names a git repository by its path.
(c) ``python3 -m X``, a venv python and ``uv run X`` match the rules of ``X``.
(d) The lint of a deny regex that ends in a fixed path.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from hookload import load_hook_with_config

gh = load_hook_with_config("guard_hook", {}, "guard_hook_precision_t")
gt = load_hook_with_config("guard_table", {}, "guard_table_precision_t")
gq = load_hook_with_config("guard_quarantine", {}, "guard_quarantine_precision_t")
mf = load_hook_with_config("memory_fields", {}, "memory_fields_precision_t")
tsl = load_hook_with_config("trust_session_line", {}, "trust_session_line_precision_t")
mfh = load_hook_with_config("memory_fields_hook", {}, "memory_fields_hook_precision_t")

PUSH = r"^git\s+push\b"
RULE = "feedback_demo_push"


@pytest.fixture(autouse=True)
def hook_env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


def entry(mid=RULE, violates=PUSH, **extra: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": mid,
        "rule": f"Rule of {mid}.",
        "apply": f"apply of {mid}",
        "scope": "tool",
        "triggers": [],
        "violates": violates,
        "complies": "",
        "run_first": "",
    }
    e.update(extra)
    return e


def stamp(ago_days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_days * 86400))


def write_log(path: Path, rows: list[tuple[str, str, float]]) -> None:
    """``(decision, rule id, days ago)`` lines in the format of the guard log."""
    with open(path, "w", encoding="utf-8") as fh:
        for decision, rid, ago in rows:
            rec = {
                "ts": stamp(ago),
                "session_id": "s0",
                "agent_id": "main",
                "tool": "Bash",
                "decision": decision,
                "ids": [rid],
                "text": "git push",
            }
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def env(tmp_path):
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"version": 4, "entries": [entry()]}))
    return {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
    }


def set_table(env, entries) -> None:
    Path(env["NOBLIVION_GUARD_TABLE"]).write_text(json.dumps({"version": 4, "entries": entries}))


def call(env, cmd, cwd="/tmp", sid="s1", tool="Bash", **ti) -> dict[str, Any] | None:
    ev = {
        "session_id": sid,
        "tool_name": tool,
        "tool_input": {"command": cmd, **ti} if tool == "Bash" else ti,
        "cwd": cwd,
    }
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(ev)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def denied(out) -> bool:
    return bool(out) and out.get("permissionDecision") == "deny"


def log_decisions(env) -> list[str]:
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x)["decision"] for x in p.read_text().splitlines()] if p.exists() else []


# --------------------------------------------------------------------------
# (a) quarantine
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "denies,overrides,want",
    [
        (3, 3, True),
        (10, 5, True),  # exactly 50 percent
        (10, 4, False),  # 40 percent
        (53, 32, True),  # 60 percent
        (2, 2, False),  # fewer than 3 overrides
        (0, 3, True),  # overrides with no logged deny: the rate is 100 percent
        (100, 2, False),
    ],
)
def test_the_quarantine_rule(denies, overrides, want):
    assert gq.is_quarantined(denies, overrides) is want


def test_a_rule_overridden_most_of_the_time_shows_as_a_row_not_a_deny(env, tmp_path):
    assert denied(call(env, "git push origin main"))  # control: no log, the rule denies
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, d) for d in (1, 2, 3, 4)] + [("override", RULE, d) for d in (1, 2, 3)],
    )
    out = call(env, "git push origin main", sid="s2")
    assert out is not None and not denied(out)
    assert RULE in out["additionalContext"]  # the row
    assert "quarantined" in log_decisions(env)
    assert "deny" not in log_decisions(env)[7:]  # no new deny line after the 7 seeded lines


def test_two_overrides_or_a_low_rate_keep_the_deny(env, tmp_path):
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 1)] * 2 + [("override", RULE, 1)] * 2,
    )
    assert denied(call(env, "git push origin main"))
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 1)] * 20 + [("override", RULE, 1)] * 3,
    )
    assert denied(call(env, "git push origin main", sid="s2"))


def test_events_older_than_14_days_do_not_count(env, tmp_path):
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 20)] * 5 + [("override", RULE, 20)] * 5,
    )
    assert denied(call(env, "git push origin main"))
    assert gq.quarantined(Path(env["NOBLIVION_GUARD_LOG"])) == {}


def test_the_rotated_log_counts_too(env, tmp_path):
    write_log(tmp_path / "log.jsonl.1", [("override", RULE, 3)] * 3 + [("deny", RULE, 3)] * 3)
    write_log(tmp_path / "log.jsonl", [])
    assert RULE in gq.quarantined(Path(env["NOBLIVION_GUARD_LOG"]))


def test_only_the_overridden_rule_is_quarantined(env, tmp_path):
    other = entry("feedback_other_push", r"^git\s+push\s+--force")
    set_table(env, [entry(), other])
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 1)] * 3 + [("override", RULE, 1)] * 3,
    )
    out = call(env, "git push --force origin main")
    assert denied(out)  # the other rule still denies
    assert "feedback_other_push" in out["permissionDecisionReason"]


def test_the_switch_turns_the_quarantine_off(env, tmp_path):
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 1)] * 3 + [("override", RULE, 1)] * 3,
    )
    env = dict(env, NOBLIVION_GUARD_QUARANTINE="0")
    assert denied(call(env, "git push origin main"))


def test_a_log_that_cannot_be_read_quarantines_nothing(env, tmp_path):
    (tmp_path / "log.jsonl").write_text("not json\n{broken\n")
    assert denied(call(env, "git push origin main"))


def test_the_credential_guard_is_never_quarantined(env, tmp_path):
    repo = tmp_path / "cred"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        [
            "remote",
            "add",
            "origin",
            "https://bot-user:s3cretTokenValue0123456789@example.invalid/o/r.git",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    rows = [("credential-deny", "credential-url", 1)] * 6
    rows += [("credential-override", "credential-source", 1)] * 6
    rows += [("override", "credential-url", 1)] * 6 + [("deny", "credential-url", 1)] * 6
    write_log(tmp_path / "log.jsonl", rows)
    assert gq.quarantined(Path(env["NOBLIVION_GUARD_LOG"])) == {}
    assert gq.exempt("credential-url") and gq.exempt("credential-source")
    out = call(env, "cat .git/config", cwd=str(repo))
    assert denied(out) and out["permissionDecisionReason"].startswith("Credential guard:")
    assert gq.line(env) == ""  # nothing is named at session start either


def test_the_session_start_line_names_the_quarantined_rule(env, tmp_path):
    write_log(
        tmp_path / "log.jsonl",
        [("deny", RULE, 1)] * 53 + [("override", RULE, 1)] * 32,
    )
    line = gq.line(env)
    assert RULE in line and "overridden 32 of 53" in line and "Fix or remove" in line
    assert RULE in "\n".join(tsl.lines(dict(env, NOBLIVION_TRUST_REPORT_FILE="/none")))
    assert gq.line(dict(env, NOBLIVION_GUARD_QUARANTINE="0")) == ""
    write_log(tmp_path / "log.jsonl", [("deny", RULE, 1)] * 5)
    assert gq.line(env) == ""


def test_the_session_start_line_stays_short_with_many_rules(env, tmp_path):
    rows = []
    for i in range(7):
        rows += [("deny", f"feedback_r{i}", 1)] * 4 + [("override", f"feedback_r{i}", 1)] * 4
    write_log(tmp_path / "log.jsonl", rows)
    line = gq.line(env)
    assert "7 deny rule" in line and "and 4 more" in line


# --------------------------------------------------------------------------
# (b) where:
# --------------------------------------------------------------------------
def test_where_problems():
    assert mf.where_problems({"where": "/srv/app/**, ~/work, **/tool"}) == []
    assert mf.where_problems({"where": "**"}) == []
    assert mf.where_problems({"where": "app/src"})  # relative
    assert mf.where_problems({"where": ", ,"})
    assert mf.where_problems({"where": ",".join(f"/a/{i}" for i in range(9))})
    assert mf.where_problems({}) == []


def test_where_is_read_and_written():
    base = "---\nname: n\ndescription: d\ntype: feedback\n---\nbody\n"
    text = mf.write_fields(
        base, {"rule": "Do x.", "apply": "x", "scope": "tool", "where": "/srv/a"}
    )
    assert mf.read_fields(text)["where"] == "/srv/a"
    assert 'where: "/srv/a"' in text


def test_where_match_globs():
    assert gt.where_match(["/srv/app"], "/srv/app")
    assert gt.where_match(["/srv/app"], "/srv/app/src/x")
    assert not gt.where_match(["/srv/app"], "/srv/app2")
    assert gt.where_match(["/srv/*/api"], "/srv/p1/api/v2")
    assert not gt.where_match(["/srv/*/api"], "/srv/p1/x/api2")
    assert gt.where_match(["/srv/**/api"], "/srv/p1/x/api")
    assert gt.where_match(["**/proj"], "/a/b/proj/src")
    assert gt.where_match(["~/work"], os.path.expanduser("~/work/a"))
    assert gt.where_match(["**"], "/anywhere")
    assert gt.where_match(["/nope", "/srv/app"], "/srv/app/x")


def test_command_dirs():
    assert gt.command_dirs("git status", "/srv/a") == ["/srv/a"]
    assert gt.command_dirs("git -C /srv/b status", "/srv/a") == ["/srv/b"]
    assert gt.command_dirs("git -C sub status", "/srv/a") == ["/srv/a/sub"]
    assert gt.command_dirs("cd /srv/c && git push", "/srv/a") == ["/srv/c"]
    assert gt.command_dirs("cd .. && git push", "/srv/a/b") == ["/srv/a"]
    assert gt.command_dirs("git -C /srv/b status; ls", "/srv/a") == ["/srv/b", "/srv/a"]
    assert gt.command_dirs("cd /srv/c; cd ..; ls", "/srv/a") == ["/srv"]
    assert gt.command_dirs('cd "$X" && git push', "/srv/a") == [None]
    assert gt.command_dirs("sudo -u x git -C /srv/b push", "/srv/a") == ["/srv/b"]
    assert gt.command_dirs("bash -c 'cd /srv/d && git push'", "/srv/a")[-1] == "/srv/d"


@pytest.mark.parametrize(
    "cmd,cwd,fires",
    [
        ("git push origin main", "/srv/app", True),  # cwd inside
        ("git push origin main", "/srv/app/src", True),
        ("git push origin main", "/srv/other", False),  # cwd outside
        ("git -C /srv/app push origin main", "/srv/other", True),  # git -C inside
        ("git -C /srv/other push origin main", "/srv/app", False),  # git -C outside wins
        ("cd /srv/app && git push origin main", "/srv/other", True),  # cd inside
        ("cd /srv/other && git push origin main", "/srv/app", False),  # cd outside wins
        ('cd "$DIR" && git push origin main', "/srv/other", True),  # unknown: keep firing
    ],
)
def test_a_note_with_where_fires_only_inside_its_glob(env, cmd, cwd, fires):
    set_table(env, [entry(where=["/srv/app"])])
    assert denied(call(env, cmd, cwd=cwd)) is fires


def test_where_scopes_rows_and_file_rows_too(env):
    row = entry("feedback_row", "", triggers=["acme-deploy"], where=["/srv/app"])
    row2 = entry("feedback_file", "", triggers=["glob:**/*.md"], where=["/srv/app"])
    set_table(env, [row, row2])
    assert "feedback_row" in (call(env, "acme-deploy now", cwd="/srv/app") or {}).get(
        "additionalContext", ""
    )
    assert call(env, "acme-deploy now", cwd="/srv/other", sid="s2") is None
    out = call(env, "", tool="Edit", sid="s3", file_path="/srv/app/docs/a.md")
    assert out is not None and "feedback_file" in out["additionalContext"]
    assert call(env, "", tool="Edit", sid="s4", file_path="/srv/other/a.md") is None


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_a_note_that_names_a_repository_has_it_as_its_own_project(tmp_path):
    repo = _repo(tmp_path / "work" / "alpha")
    other = _repo(tmp_path / "work" / "beta")
    fields = {
        "rule": "Push alpha with the release script.",
        "apply": f"cd {repo} && tools/release.sh",
        "violates": rf"git\s+-C\s+{repo}\s+push",
    }
    assert gt.named_repos(fields) == [str(repo)]
    nothing = {"rule": "Use short output.", "apply": "pytest -q"}
    assert gt.named_repos(nothing) == []
    both = dict(fields, violates=rf"git\s+-C\s+{repo}\s+push|{other}/tools/x")
    assert gt.named_repos(both) == sorted([str(repo), str(other)])
    # NOBLIVION-173: a path in the remedy text is not the place of the hazard.
    remedy = {"rule": "x", "apply": f"cd {repo} && tools/release.sh", "example_ok": f"ls {repo}"}
    assert gt.named_repos(remedy) == []
    assert gt.named_repos(remedy, ["docker cp", f"{repo}/tools/release.sh"]) == [str(repo)]
    gone = {"rule": "x", "violates": r"cd /no/such/place/anywhere"}
    assert gt.named_repos(gone) == []
    (tmp_path / "home" / "x").mkdir()
    assert (
        gt.named_repos({"rule": "x", "violates": r"^ls ~/x"}) == []
    )  # the home folder is no repository


def test_a_rule_that_names_a_repository_fires_only_in_it(env, tmp_path):
    repo = _repo(tmp_path / "work" / "alpha")
    elsewhere = _repo(tmp_path / "work" / "beta")
    (repo / "src").mkdir()
    set_table(env, [entry(repos=[str(repo)])])
    assert denied(call(env, "git push origin main", cwd=str(repo)))
    assert denied(call(env, "git push origin main", cwd=str(repo / "src"), sid="s2"))
    assert not denied(call(env, "git push origin main", cwd=str(elsewhere), sid="s3"))
    assert denied(call(env, f"git -C {repo} push origin main", cwd=str(elsewhere), sid="s4"))
    assert not denied(call(env, f"cd {elsewhere} && git push", cwd=str(repo), sid="s5"))


def test_a_linked_work_tree_is_the_same_project(env, tmp_path):
    repo = _repo(tmp_path / "work" / "alpha")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    tree = tmp_path / "work" / "alpha-wt"
    subprocess.run(git + ["worktree", "add", "-q", "-b", "b1", str(tree)], check=True)
    assert str(repo) in gt.repo_roots(str(tree))
    set_table(env, [entry(repos=[str(repo)])])
    assert denied(call(env, "git push origin b1", cwd=str(tree)))


def test_an_explicit_where_replaces_the_named_repository():
    e = {"repos": ["/srv/alpha"], "where": ["/srv/beta"]}
    assert gt.where_ok(e, ["/srv/beta/x"])
    assert not gt.where_ok(e, ["/srv/alpha"])
    assert gt.where_ok({"repos": ["/srv/alpha"], "where": ["**"]}, ["/anywhere"])
    assert gt.where_ok({}, ["/anywhere"])


def test_the_table_build_carries_where_and_the_named_repository(tmp_path):
    repo = _repo(tmp_path / "work" / "alpha")
    folder = tmp_path / "mem"
    folder.mkdir()
    base = "---\nname: n\ndescription: d\ntype: feedback\n---\nbody\n"

    def note(name, **f):
        fields = {
            "rule": "Do not push by hand.",
            "apply": "use the script",
            "scope": "tool",
            "triggers": ["git push"],
        }
        fields.update(
            violates=PUSH,
            example_repeat="git push origin main",
            example_ok="tools/release.sh",
        )
        fields.update(f)
        (folder / name).write_text(mf.write_fields(base, fields))

    note("feedback_where.md", where="/srv/app/**, ~/work")
    note("feedback_named.md", violates=rf"git\s+-C\s+{repo}\s+push")
    note("feedback_remedy.md", apply=f"cd {repo} && tools/release.sh")
    note("feedback_plain.md")
    note("feedback_badwhere.md", where="relative/path")
    table = gt.build(folder)
    by = {e["id"]: e for e in table["entries"]}
    assert by["feedback_where"]["where"] == ["/srv/app/**", "~/work"]
    assert "repos" not in by["feedback_where"]
    assert by["feedback_named"]["repos"] == [str(repo)]
    assert "where" not in by["feedback_plain"] and "repos" not in by["feedback_plain"]
    assert "repos" not in by["feedback_remedy"]  # NOBLIVION-173
    assert "where" not in by["feedback_badwhere"]  # dropped alone: the rule fires everywhere
    assert any(s["id"] == "feedback_badwhere" and "where" in s for s in table["skipped"])
    assert "where field is skipped" in gt.skipped_line(table)


# --------------------------------------------------------------------------
# (c) interpreter forms
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "form",
    [
        "pytest tests",
        "python3 -m pytest tests",
        "python -m pytest tests",
        ".venv/bin/python -m pytest tests",
        "/srv/x/.venv/bin/python3.12 -m pytest tests",
        "uv run pytest tests",
        "uv run --with rich pytest tests",
        "uv run python -m pytest tests",
        ".venv/bin/pytest tests",
        "cd x && uv run pytest tests",
        "FOO=1 python3 -m pytest tests",
    ],
)
def test_every_form_of_pytest_matches_the_deny_rule(form):
    table = {"entries": [entry("feedback_quiet", r"^pytest\b(?!.*\s-q\b)")]}
    assert [h["id"] for h in gt.match(form, table)] == ["feedback_quiet"]
    assert gt.match(form.replace("tests", "-q tests"), table) == []


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -m pytestx tests",
        "python3 -c 'pytest tests'",
        "python3 tools/pytest_helper.py",
        "uv pip install pytest",
        "uv run ruff check",
        "echo uv run pytest",
        "grep -rn 'python3 -m pytest' docs",
    ],
)
def test_other_commands_do_not_match_the_pytest_rule(cmd):
    table = {"entries": [entry("feedback_quiet", r"^pytest\b(?!.*\s-q\b)")]}
    assert gt.match(cmd, table) == []


def test_interpreter_form():
    f = gt.interpreter_form
    assert f("python3 -m pytest -x") == "pytest -x"
    assert f("uv run --python 3.12 pytest -x") == "pytest -x"
    assert f("uv run -- pytest") == "pytest"
    assert f(".venv/bin/pytest") == "pytest"
    assert f("pytest") == ""
    assert f("python3 script.py") == ""
    assert f("python3 -u -m pytest") == ""
    assert f("uv run") == ""
    assert f("") == ""


def test_the_rewrite_acts_on_every_form_of_the_program(env):
    rule = entry(
        "feedback_quiet",
        r"^pytest\b(?!.*\s-q\b)",
        rewrite="add-flag -q after pytest",
        complies=r"^pytest\s+-q\b",
    )
    set_table(env, [rule])
    env = dict(env, NOBLIVION_GUARD_REWRITE="1")
    for k, cmd in enumerate(
        [
            "python3 -m pytest tests",
            ".venv/bin/python -m pytest tests",
            "uv run pytest tests",
            "cd x && uv run pytest tests",
            ".venv/bin/pytest tests",
        ]
    ):
        out = call(env, cmd, sid=f"s{k}")
        assert out is not None, cmd
        new = out["updatedInput"]["command"]
        assert " -q" in new and new.replace(" -q", "", 1) == cmd, (cmd, new)


# --------------------------------------------------------------------------
# (d) lint
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern,tail",
    [
        (r"docker\s+cp\s+.*:/app/", "/app/"),
        (r"rm\s+-rf\s+/var/lib/demo", "/var/lib/demo"),
        (r"cp\s+.*\s+/tmp/out$", "/tmp/out"),
        (r"cat\s+\./data/x\.txt\b", "./data/x.txt"),
    ],
)
def test_the_lint_reports_a_regex_that_ends_in_a_fixed_path(pattern, tail):
    got = mf.violates_warnings({"violates": pattern})
    assert len(got) == 1 and tail in got[0]


@pytest.mark.parametrize(
    "pattern",
    [
        r"^git\s+push\b",
        r"pkill\s+-f\b",
        r"^tools/run\.sh",  # the path is the command
        r"docker\s+cp\s+.*:[^\s]*$",
        r"git\s+stash\s+pop\s*$",
        r"rm\s+-rf\s+/\S*",
        "",
    ],
)
def test_the_lint_passes_other_regexes(pattern):
    assert mf.violates_warnings({"violates": pattern}) == []


def test_the_table_build_lists_the_lint_and_never_drops_the_rule(tmp_path):
    folder = tmp_path / "mem"
    folder.mkdir()
    base = "---\nname: n\ndescription: d\ntype: feedback\n---\nbody\n"
    fields = {
        "rule": "Do not copy into the app folder.",
        "apply": "use the deploy script",
        "scope": "tool",
        "triggers": ["docker cp"],
        "violates": r"docker\s+cp\s+.*:/app/",
        "example_repeat": "docker cp x c1:/app/",
        "example_ok": "tools/deploy.sh",
    }
    (folder / "feedback_cp.md").write_text(mf.write_fields(base, fields))
    table = gt.build(folder)
    assert [e["id"] for e in table["entries"] if e["violates"]] == ["feedback_cp"]
    assert table["lints"][0]["id"] == "feedback_cp" and "/app/" in table["lints"][0]["lint"]


def test_the_memory_fields_hook_prints_the_lint(tmp_path, monkeypatch):
    mem = tmp_path / "home" / "mem"
    mem.mkdir(parents=True)
    monkeypatch.setenv("NOBLIVION_MEMORY_DIR", str(mem))
    base = "---\nname: n\ndescription: d\ntype: feedback\n---\nbody\n"
    fields = {
        "rule": "Do not copy into the app folder.",
        "apply": "use the deploy script",
        "scope": "tool",
        "triggers": ["docker cp"],
        "violates": r"docker\s+cp\s+.*:/app/",
        "example_repeat": "docker cp x c1:/app/",
        "example_ok": "tools/deploy.sh",
    }
    path = mem / "feedback_cp.md"
    path.write_text(mf.write_fields(base, fields))
    event = {"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(tmp_path)}
    out = mfh.run(json.dumps(event))
    assert "/app/" in out and "fixed path" in out


# --------------------------------------------------------------------------
# the replay of a guard log (tools/replay_guard_quarantine.py)
# --------------------------------------------------------------------------
def test_the_replay_drops_the_overridden_rule_and_keeps_the_others(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "replay_guard_quarantine_t",
        Path(gh.__file__).resolve().parent.parent / "tools" / "replay_guard_quarantine.py",
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    rows = []
    for i in range(12):  # a rule that the model overrides each time
        rows.append(("deny", "feedback_wrong", 10 - i * 0.1))
        rows.append(("override", "feedback_wrong", 10 - i * 0.1 - 0.001))
    rows += [("deny", "feedback_right", 10 - i * 0.2) for i in range(8)]  # never overridden
    rows += [("deny", "credential-url", 5)] * 3 + [("override", "credential-url", 5)] * 5
    log = tmp_path / "replay.jsonl"
    write_log(log, sorted(rows, key=lambda r: -r[2]))
    rep = tool.replay(tool.read([str(log)]))
    by = rep["by_rule_over_the_whole_log"]
    assert by["overridden_rules"] == 1 and by["other_rules"] == 1  # the credential ids are skipped
    assert by["other_rules_dropped"] == 0 and by["other_rules_denies"] == 8
    assert by["overridden_rules_dropped"] == 12 - 3  # the first 3 overrides trip the rule
    assert rep["true_kept_share"] == 1.0
    assert rep["overridden_dropped_share"] == 0.75
    assert "feedback_wrong" not in json.dumps(rep)  # counts only
