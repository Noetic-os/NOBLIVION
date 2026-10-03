# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label rows in the guard hook (tool time) and the prompt leg of ``hooks/label_rows.py``,
over a real guard table version 2 built from a tmp memory folder.

What these tests hold:
1. A specific label of the call adds the memory as a label row, logged with
   its matched labels (decision ``labels``; ``label_rows`` on a ``rows`` line).
2. CAP: at most 3 rows per call; TRIGGER ROWS FIRST: label rows fill only the
   places the trigger rows leave, after them, and the trigger text is the same
   as without a label index.
3. FULL-DELIVERY DE-DUP: a memory the session already got in full is skipped,
   by NAME (recall shown-set: apply, fetched, trigger) or by STEM (guard full
   rows, earlier label rows). An index menu row does not count. A subagent
   ignores the main agent's shown-set.
4. A LABEL NEVER DENIES: a label match on a memory that has ``violates:``
   shows a row and allows; a real violation denies with no label row.
5. FAIL OPEN: no index (a version 1 table), a broken index, or a missing
   label module give no label rows and change nothing else.
6. OWN BUDGET: label rows charge ``label_chars``, never the trigger rows'
   ``row_chars``; a spent label budget stops label rows, not trigger rows.
7. THE PROMPT LEG is off unless NOBLIVION_RECALL_LABELS is set; it shares the
   state of agent ``main``, so a memory shown at the prompt is not shown again
   at tool time.
8. THE GUARD TABLE is version 2 and its label index covers every memory file,
   rule fields or not; a labeller failure keeps the guard entries.
Every path is a tmp path: no test reads or writes a live file.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from hookload import HOOKS, load_hook

gh = load_hook("guard_hook", "guard_hook_t_label_rows")
gt = load_hook("guard_table", "guard_table_t_label_rows")
lr = load_hook("label_rows", "label_rows_t_label_rows")
corpus = load_hook("corpus", "corpus_t_label_rows")

HEADER = "Memory rows by subject (label rows)"


def _mem(
    folder: Path,
    stem: str,
    body: str,
    name: str = "",
    rule: str = "",
    apply: str = "",
    triggers: str = "",
    violates: str = "",
    desc: str = "a memory",
) -> None:
    fm = [f"name: {name or stem}", f"description: {desc}"]
    if rule:
        fm += [f"rule: {rule}", f"apply: {json.dumps(apply or 'do the thing')}", "scope: tool"]
        if triggers:
            fm.append(f"triggers: [{triggers}]")
        if violates:
            fm += [
                f"violates: {json.dumps(violates)}",
                'example_repeat: "rm -rf /data/x"',
                'example_ok: "rm -rf /tmp/x"',
            ]
    (folder / f"{stem}.md").write_text("---\n" + "\n".join(fm) + "\n---\n" + body + "\n")


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """A data dir and a home folder under ``tmp_path``; no inherited NOBLIVION_* var."""
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
    return {"home": home, "data": data, "config": data / "config.json"}


@pytest.fixture
def mem(tmp_path, hook_env):
    m = tmp_path / "mem"
    m.mkdir()
    _mem(m, "project_host_alpha", "Prometheus reads deploy/prometheus.yml on the host; reload it.")
    _mem(
        m,
        "feedback_named",
        "Run alpha_tool.sh with care.",
        name="Named memory",
        rule="Check alpha_tool.sh before a run.",
    )
    _mem(
        m,
        "feedback_guarded",
        "data_wipe.sh removes the data folder.",
        rule="Never remove the data folder.",
        violates=r"^rm -rf /data",
        triggers="rm -rf /data",
    )
    _mem(
        m,
        "feedback_trig_one",
        "First trigger memory.",
        rule="Rule one for deploycmd.",
        triggers="deploycmd run",
    )
    _mem(
        m,
        "feedback_trig_two",
        "Second trigger memory.",
        rule="Rule two for deploycmd.",
        triggers="deploycmd run",
    )
    for i in range(4):
        _mem(m, f"project_multi_{i}", f"Notes {i} on multi_target.py and its runs.")
    for i in range(9):
        _mem(m, f"project_busy_{i}", f"Busy {i} mentions busy_name.py once.")
    _mem(m, "project_no_rule_fields", "Only text about lonely_file.py.")
    _mem(m, "MEMORY", "index file, never a memory: lonely_file.py")
    return m


@pytest.fixture
def env(tmp_path, mem, hook_env, monkeypatch):
    table = tmp_path / "guard-table.json"
    gt.rebuild(mem, table)
    e = {
        "NOBLIVION_GUARD_TABLE": str(table),
        "NOBLIVION_GUARD_STATE_DIR": str(tmp_path / "state"),
        "NOBLIVION_GUARD_LOG": str(tmp_path / "log.jsonl"),
        "NOBLIVION_RECALL_CACHE_DIR": str(tmp_path / "rc"),
        "NOBLIVION_GUARD_WEAK_SHARE": "0",
    }
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gh, "DEFAULT_LOG", tmp_path / "must-not-exist-log")
    monkeypatch.setattr(gh, "DEFAULT_STATE", tmp_path / "must-not-exist-state")
    yield e
    assert not (tmp_path / "must-not-exist-log").exists()
    assert not (tmp_path / "must-not-exist-state").exists()


def bash(cmd, sid="s1", agent=None):
    ev = {
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": "/tmp",
    }
    if agent:
        ev["agent_id"] = agent
    return ev


def call(env, event) -> Any:
    out = io.StringIO()
    assert gh.main(stdin=io.StringIO(json.dumps(event)), stdout=out, environ=env) == 0
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"] if text else None


def ctx(out) -> str:
    return (out or {}).get("additionalContext", "")


def log(env):
    p = Path(env["NOBLIVION_GUARD_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def label_ids(text: str):
    if HEADER not in text:
        return []
    block = text[text.index(HEADER) :]
    return [line.split()[2] for line in block.splitlines() if line.startswith("- Memory ")]


def shown_set(env, sid, **kinds):
    d = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "by-session"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"session": sid, "apply": [], "fetched": [], "trigger": []}
    doc.update(kinds)
    (d / f"{sid}.shown_set.json").write_text(json.dumps(doc))


# 1. a label row -----------------------------------------------------------
def test_specific_label_adds_the_memory_and_logs_its_labels(env):
    out = call(env, bash("cat /etc/prometheus/prometheus.yml"))
    assert out.get("permissionDecision") is None
    assert label_ids(ctx(out)) == ["project_host_alpha"]
    assert "Labels: prometheus.yml" in ctx(out)
    assert "Summary:" in ctx(out)  # no rule fields: the description, not "Rule:"
    line = log(env)[-1]
    assert line["decision"] == "labels" and line["ids"] == ["project_host_alpha"]
    assert line["label_rows"] == {"project_host_alpha": ["prometheus.yml"]}


def test_a_label_held_by_more_than_eight_memories_never_matches_alone(env):
    assert call(env, bash("python busy_name.py")) is None
    assert log(env) == []


def test_index_file_is_not_in_the_index_and_a_no_rule_memory_is(env):
    out = call(env, bash("cat lonely_file.py"))
    assert label_ids(ctx(out)) == ["project_no_rule_fields"]


# 2. cap and order ---------------------------------------------------------
def test_at_most_three_label_rows(env):
    out = call(env, bash("python multi_target.py"))
    assert len(label_ids(ctx(out))) == 3
    assert len(log(env)[-1]["label_rows"]) == 3


def test_trigger_rows_first_then_label_rows_in_the_free_places(env, tmp_path):
    cmd = "deploycmd run --target multi_target.py"
    new = ctx(call(env, bash(cmd, sid="a")))
    # The same call against the same table without its label index.
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    del table["label_index"]
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps(table))
    old = ctx(
        call(
            dict(
                env,
                NOBLIVION_GUARD_TABLE=str(v1),
                NOBLIVION_GUARD_STATE_DIR=str(tmp_path / "s2"),
                NOBLIVION_GUARD_LOG=str(tmp_path / "v1-log.jsonl"),
            ),
            bash(cmd, sid="a"),
        )
    )
    assert old and HEADER not in old
    assert new.startswith(old + "\n" + HEADER)  # trigger text unchanged, label rows after
    assert len(label_ids(new)) == 1  # 2 trigger rows + 1 label row = 3
    line = [x for x in log(env) if x["decision"] == "rows"][-1]
    assert sorted(line["ids"]) == ["feedback_trig_one", "feedback_trig_two"]
    assert len(line["label_rows"]) == 1


# 3. full-delivery de-dup ----------------------------------------------------
def test_shown_set_by_name_skips_a_memory_whose_name_differs_from_its_stem(env):
    shown_set(env, "s1", apply=["Named memory"])
    assert call(env, bash("bash alpha_tool.sh", sid="s1")) is None
    shown_set(env, "s2", fetched=["Named memory"])
    assert call(env, bash("bash alpha_tool.sh", sid="s2")) is None
    shown_set(env, "s3", trigger=["feedback_named"])  # a stem in the set counts too
    assert call(env, bash("bash alpha_tool.sh", sid="s3")) is None


def test_an_index_menu_row_is_not_a_delivery(env):
    shown_set(env, "s1", row=["Named memory"])
    assert label_ids(ctx(call(env, bash("bash alpha_tool.sh", sid="s1")))) == ["feedback_named"]


def test_a_shown_set_of_another_session_does_not_count(env):
    d = Path(env["NOBLIVION_RECALL_CACHE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "shown_set.json").write_text(json.dumps({"session": "other", "apply": ["Named memory"]}))
    assert label_ids(ctx(call(env, bash("bash alpha_tool.sh", sid="s1")))) == ["feedback_named"]


def test_a_subagent_ignores_the_main_agents_shown_set(env):
    shown_set(env, "s1", apply=["Named memory"])
    out = call(env, bash("bash alpha_tool.sh", sid="s1", agent="sub-1"))
    assert label_ids(ctx(out)) == ["feedback_named"]


def test_guard_full_row_and_earlier_label_row_skip_by_stem(env):
    assert label_ids(ctx(call(env, bash("cat prometheus.yml")))) == ["project_host_alpha"]
    assert call(env, bash("cat prometheus.yml")) is None  # no repeat of a label row
    st = gh.SessionState(env, "s9", "main")
    st.full["feedback_named"] = 1  # shown in full by a trigger row
    st.close(save=True)
    assert call(env, bash("bash alpha_tool.sh", sid="s9")) is None


def test_delivered_names_reader_matches_the_corpus_reader(env):
    cache = env["NOBLIVION_RECALL_CACHE_DIR"]
    cases = [
        (
            "own",
            {"session": "own", "apply": ["a"], "fetched": ["b"], "trigger": ["c"], "row": ["d"]},
        ),
        ("other", {"session": "someone", "apply": ["x"]}),
    ]
    for sid, doc in cases:
        d = Path(cache) / "by-session"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.shown_set.json").write_text(json.dumps(doc))
    (Path(cache) / "shown_set.json").write_text(json.dumps({"session": "single", "apply": ["s"]}))
    (Path(cache) / "by-session" / "broken.shown_set.json").write_text("{not json")
    for sid in ("own", "other", "single", "nobody", "broken"):
        want = corpus.load_shown_set(cache, sid)
        names = set(want["apply"]) | set(want["fetched"]) | set(want["trigger"])
        assert lr.delivered_names(env, sid) == names, sid
    # No session id: the corpus reader would read the NEWEST file of any session;
    # the label leg reads nothing rather than borrow another session's set.
    assert lr.delivered_names(env, None) == set()
    assert lr.delivered_names(env, "../x") == set()


# 4. a label never denies ------------------------------------------------------
def test_label_match_on_a_memory_with_violates_shows_a_row_and_allows(env):
    out = call(env, bash("bash data_wipe.sh --dry-run"))
    assert out.get("permissionDecision") is None
    assert label_ids(ctx(out)) == ["feedback_guarded"]


def test_a_violation_denies_without_label_rows(env):
    out = call(env, bash("rm -rf /data && bash data_wipe.sh && cat prometheus.yml"))
    assert out["permissionDecision"] == "deny"
    assert HEADER not in out["permissionDecisionReason"] and HEADER not in ctx(out)
    assert log(env)[-1]["decision"] == "deny" and "label_rows" not in log(env)[-1]


def test_label_candidates_are_never_fed_to_the_deny_matcher(env, monkeypatch):
    seen = []
    real = gh._gt().match

    def spy(command, table=None):
        seen.append(command)
        return real(command, table)

    monkeypatch.setattr(gh._gt(), "match", spy)
    call(env, bash("cat prometheus.yml"))
    assert seen == ["cat prometheus.yml"]


# 5. fail open -------------------------------------------------------------------
def _with_table(env, tmp_path, mutate):
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    mutate(table)
    p = tmp_path / "mutated.json"
    p.write_text(json.dumps(table))
    return dict(env, NOBLIVION_GUARD_TABLE=str(p))


def test_version_1_table_gives_no_label_rows(env, tmp_path):
    e = _with_table(env, tmp_path, lambda t: t.pop("label_index"))
    assert call(e, bash("cat prometheus.yml")) is None
    trig = ctx(call(e, bash("deploycmd run")))
    assert trig and HEADER not in trig


def test_broken_index_gives_no_label_rows(env, tmp_path):
    e = _with_table(env, tmp_path, lambda t: t.update(label_index={"labels": 3, "docs": "x"}))
    assert call(e, bash("cat prometheus.yml")) is None
    assert [x["decision"] for x in log(e)] == []  # no error line either


def test_missing_label_module_gives_no_label_rows(env, monkeypatch):
    real = gh._load

    def no_labels(name):
        if name == "label_rows":
            raise ImportError(name)
        return real(name)

    monkeypatch.setattr(gh, "_load", no_labels)
    assert call(env, bash("cat prometheus.yml")) is None
    assert ctx(call(env, bash("deploycmd run")))  # trigger rows still show


def test_switch_off(env):
    assert call(dict(env, NOBLIVION_GUARD_LABELS="0"), bash("cat prometheus.yml")) is None


# 6. own budget ----------------------------------------------------------------
def test_label_rows_have_their_own_budget(env):
    assert label_ids(ctx(call(env, bash("cat prometheus.yml", sid="b"))))
    st = gh.SessionState(env, "b", "main")
    used, row_chars = st.label_chars, st.row_chars
    st.close(save=False)
    assert used > 0 and row_chars == 0  # trigger budget not charged
    # Less than one row of room left in the label budget: no more label rows.
    e = dict(env, NOBLIVION_GUARD_LABEL_SESSION_CHARS=str(used + lr.MIN_ROOM - 1))
    assert call(e, bash("bash alpha_tool.sh", sid="b")) is None
    # the default budget has room
    assert label_ids(ctx(call(env, bash("bash alpha_tool.sh", sid="b"))))
    assert ctx(call(e, bash("deploycmd run", sid="b")))  # trigger rows still show


# 7. the prompt leg --------------------------------------------------------------
def _prompt(sid="p1", text="Fix deploy/prometheus.yml on the host"):
    return json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text})


def test_prompt_leg_is_off_unless_switched_on(env):
    out = io.StringIO()
    assert lr.prompt_leg(_prompt(), out, dict(env)) == 0
    assert out.getvalue() == ""
    assert lr.prompt_leg(_prompt(), out, dict(env, NOBLIVION_RECALL_LABELS="1")) > 0
    assert label_ids(out.getvalue()) == ["project_host_alpha"]


def test_prompt_leg_respects_the_recall_disable_switch(env):
    out = io.StringIO()
    e = dict(env, NOBLIVION_RECALL_LABELS="1", NOBLIVION_RECALL_DISABLE="1")
    assert lr.prompt_leg(_prompt("p2"), out, e) == 0
    assert HEADER not in out.getvalue()


def test_prompt_and_tool_time_share_the_main_agents_state(env):
    e = dict(env, NOBLIVION_RECALL_LABELS="1")
    out = io.StringIO()
    assert lr.prompt_leg(_prompt("p3"), out, e) > 0
    assert label_ids(out.getvalue()) == ["project_host_alpha"]
    assert call(e, bash("cat prometheus.yml", sid="p3")) is None  # already given at the prompt
    line = log(e)[0]
    assert line["tool"] == "UserPromptSubmit" and line["decision"] == "labels"
    assert line["text"] == ""


def test_prompt_leg_ignores_other_events_and_bad_input(env):
    e = dict(env, NOBLIVION_RECALL_LABELS="1")
    out = io.StringIO()
    other = json.dumps({"hook_event_name": "PreToolUse", "prompt": "prometheus.yml"})
    assert lr.prompt_leg(other, out, e) == 0
    assert lr.prompt_leg("{not json", out, e) == 0
    assert lr.prompt_leg(_prompt(text="nothing specific here"), out, e) == 0
    assert out.getvalue() == ""


# 8. the guard table -------------------------------------------------------------
def test_table_version_2_index_covers_every_memory_file(env, mem):
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    assert table["version"] == 2 and "label_error" not in table
    ids = {d["id"] for d in table["label_index"]["docs"]}
    files = {p.stem for p in mem.glob("*.md") if p.stem != "MEMORY"}
    assert ids == files
    assert "project_host_alpha" not in {e["id"] for e in table["entries"]}  # no rule fields
    assert {"version", "built_at", "source", "entries", "skipped"} <= set(table)


def test_labeller_failure_keeps_the_guard_entries(mem, monkeypatch):
    def broken():
        raise ImportError("memory_labels")

    monkeypatch.setattr(gt, "_ml", broken)
    table = gt.build(mem)
    assert "label_index" not in table and "ImportError" in table["label_error"]
    assert {e["id"] for e in table["entries"]} >= {"feedback_guarded", "feedback_trig_one"}
    assert gt.match("rm -rf /data/x", table)


def test_label_step_is_fast_in_process(env):
    import time

    table = gh._gt().load_table(Path(env["NOBLIVION_GUARD_TABLE"]))
    t0 = time.perf_counter()
    for _ in range(50):
        lr.candidates(
            "ssh alpha.example 'cat /etc/prometheus/prometheus.yml && python multi_target.py'",
            "command",
            table,
            {},
        )
    assert (time.perf_counter() - t0) / 50 < 0.030


def test_trigger_budget_trims_every_trigger_row_and_label_rows_still_show(env):
    e = dict(env, NOBLIVION_GUARD_ROWS_SESSION_CHARS="1")
    out = call(e, bash("deploycmd run --target multi_target.py", sid="t"))
    assert label_ids(ctx(out)) and not ctx(out).startswith("Memory rules for this action")
    assert log(e)[-1]["decision"] == "labels"
    st = gh.SessionState(e, "t", "main")
    assert st.row_chars == 0 and st.rows == {} and st.label_chars > 0
    st.close(save=False)


def test_trigger_and_label_rows_together_stay_under_the_show_bound(env, mem):
    long = " ".join(
        f"Sentence {i} about the long trigger memory and its many details." for i in range(40)
    )
    for k in ("a", "b"):
        _mem(
            mem, f"feedback_long_{k}", long, rule=f"Rule {k} for longcmd go.", triggers="longcmd go"
        )
    gt.rebuild(mem, Path(env["NOBLIVION_GUARD_TABLE"]))
    out = call(env, bash("longcmd go --file multi_target.py", sid="L"))
    text = ctx(out)
    assert text.startswith("Memory rules for this action")
    assert len(text) <= gh.ROWS_MAX_CHARS


def test_pick_skips_a_row_that_cannot_fit_and_takes_a_later_one(env, mem):
    _mem(mem, "project_long_desc", "body", desc="A long summary. " * 40)
    gt.rebuild(mem, Path(env["NOBLIVION_GUARD_TABLE"]))
    table = json.loads(Path(env["NOBLIVION_GUARD_TABLE"]).read_text())
    L = lr._lab()
    m_big = L.Match(0, {"id": "project_long_desc", "t": "x"}, ["big.py"], 9.0)
    m_small = L.Match(1, {"id": "no_such_file_here", "t": "short"}, ["a.py"], 1.0)
    big = lr.render_row(m_big, table, "q", "command", compact=True)
    small = lr.render_row(m_small, table, "q", "command", compact=True)
    header = lr.HEADER.format(what="call")
    room = lr.MIN_ROOM
    assert len(header) + 1 + len(big) > room >= len(header) + 1 + len(small)
    st = gh.SessionState(env, "pk", "main")
    try:
        got = lr.pick([m_big, m_small], st, env, "pk", "main", table, "q", "command", room=room)
    finally:
        st.close(save=False)
    assert got is not None and got.ids == ["no_such_file_here"]


def test_each_leg_has_its_own_switch(env):
    e = dict(env, NOBLIVION_GUARD_LABELS="0", NOBLIVION_RECALL_LABELS="1")
    out = io.StringIO()
    # the tool switch does not stop the prompt leg ...
    assert lr.prompt_leg(_prompt("sw1"), out, e) > 0
    # ... and stops the tool leg
    assert call(e, bash("bash alpha_tool.sh", sid="sw1")) is None
    for off in ("0", "off", "false", "no"):
        out = io.StringIO()
        lr.prompt_leg(_prompt("sw2"), out, dict(env, NOBLIVION_RECALL_LABELS=off))
        assert out.getvalue() == "", off


def test_an_index_of_another_version_is_refused(env, tmp_path):
    e = _with_table(env, tmp_path, lambda t: t["label_index"].update(version=99))
    assert call(e, bash("cat prometheus.yml")) is None


def test_index_files_pattern_matches_the_guard_table():
    assert lr._lab().INDEX_FILE_RX.pattern == gt._NOT_MEMORY.pattern


def test_defaults_resolve_under_the_data_dir_and_nothing_else_is_written(mem, tmp_path):
    """Both legs with NO path variable and no data dir variable: every default
    (table, state, log, recall cache) must resolve under the default data dir
    ``$HOME/.local/share/noblivion``. A fresh process, so no module was loaded
    with another HOME."""
    import subprocess

    home = tmp_path / "fresh-home"
    data = home / ".local" / "share" / "noblivion"
    data.mkdir(parents=True)
    gt.rebuild(mem, data / "guard-table.json")
    before = set(tmp_path.rglob("*"))
    code = (
        "import importlib.util, io, json, sys\n"
        "def load(n):\n"
        f"    s = importlib.util.spec_from_file_location(n, {str(HOOKS)!r} + '/' + n + '.py')\n"
        "    m = importlib.util.module_from_spec(s); sys.modules[n] = m\n"
        "    s.loader.exec_module(m); return m\n"
        "lr = load('label_rows'); gh = load('guard_hook')\n"
        "import os\n"
        "ev = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'h1',\n"
        "      'prompt': 'fix deploy/prometheus.yml'}\n"
        "n = lr.prompt_leg(json.dumps(ev), sys.stdout, dict(os.environ))\n"
        "ev = {'session_id': 'h1', 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',\n"
        "      'tool_input': {'command': 'bash alpha_tool.sh'}, 'cwd': '/tmp'}\n"
        "gh.main(stdin=io.StringIO(json.dumps(ev)), stdout=sys.stdout, environ=dict(os.environ))\n"
    )
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "NOBLIVION_RECALL_LABELS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    res = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr
    assert label_ids(res.stdout.split("\n{")[0]) == ["project_host_alpha"]  # the prompt leg ran
    assert "feedback_named" in res.stdout  # the tool leg ran
    made = set(tmp_path.rglob("*")) - before
    assert made and all(str(p).startswith(str(data)) for p in made)
    assert (data / "guard-log.jsonl").is_file()
    assert any((data / "guard-state").glob("*.json"))
    assert not (home / ".claude").exists()


# 9. the file-tool leg (Read, Grep, Glob paths; Edit/Write content) ----------
FT = "NOBLIVION_GUARD_LABELS_FILE_TOOLS"


def tool_ev(tool, ti, sid="ft"):
    return {
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": ti,
        "cwd": "/tmp",
    }


def test_file_tool_leg_is_off_by_default_and_costs_nothing(env, tmp_path):
    # Off: no row, no log line, and the table is not even read (a missing
    # table would log "table missing").
    e = dict(env, NOBLIVION_GUARD_TABLE=str(tmp_path / "no-table.json"))
    assert call(e, tool_ev("Read", {"file_path": "/etc/prometheus/prometheus.yml"})) is None
    assert call(env, tool_ev("Read", {"file_path": "/etc/prometheus/prometheus.yml"})) is None
    edit = {"file_path": "/tmp/notes.txt", "old_string": "x", "new_string": "see prometheus.yml"}
    assert call(env, tool_ev("Edit", edit)) is None
    assert log(env) == []


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Read", {"file_path": "/etc/prometheus/prometheus.yml"}),
        ("Read", {"file_path": "prometheus.yml"}),
        ("Grep", {"pattern": "scrape_interval", "path": "/etc/prometheus/prometheus.yml"}),
        ("Grep", {"pattern": "job", "path": "/etc", "glob": "prometheus.yml"}),
        ("Glob", {"pattern": "**/prometheus.yml"}),
    ],
)
def test_file_tool_leg_matches_read_grep_glob_inputs(env, tool, ti):
    out = call(dict(env, **{FT: "1"}), tool_ev(tool, ti))
    assert out.get("permissionDecision") is None
    assert label_ids(ctx(out)) == ["project_host_alpha"]
    assert "file read or search" in ctx(out)
    line = log(env)[-1]
    assert line["tool"] == tool and line["decision"] == "labels"
    assert line["label_rows"] == {"project_host_alpha": ["prometheus.yml"]}


@pytest.mark.parametrize(
    "tool,ti",
    [
        (
            "Edit",
            {"file_path": "/tmp/notes.txt", "old_string": "x", "new_string": "see prometheus.yml"},
        ),
        (
            "Edit",
            {
                "file_path": "/tmp/notes.txt",
                "old_string": "reload prometheus.yml",
                "new_string": "y",
            },
        ),
        ("Write", {"file_path": "/tmp/notes.txt", "content": "reload prometheus.yml after it"}),
        (
            "MultiEdit",
            {
                "file_path": "/tmp/notes.txt",
                "edits": [
                    {"old_string": "a", "new_string": "b"},
                    {"old_string": "c", "new_string": "prometheus.yml"},
                ],
            },
        ),
    ],
)
def test_file_tool_leg_matches_edit_and_write_content(env, tool, ti):
    out = call(dict(env, **{FT: "1"}), tool_ev(tool, ti))
    assert label_ids(ctx(out)) == ["project_host_alpha"]
    line = log(env)[-1]
    # the path, never content
    assert line["decision"] == "labels" and line["text"] == "/tmp/notes.txt"


def test_file_tool_leg_reads_a_bounded_slice_of_content(env):
    e = dict(env, **{FT: "1"})
    far = "x" * lr.FILE_CONTENT_CHARS + " prometheus.yml"
    assert call(e, tool_ev("Write", {"file_path": "/tmp/n.txt", "content": far})) is None
    near = "prometheus.yml " + "x" * lr.FILE_CONTENT_CHARS
    out = call(e, tool_ev("Write", {"file_path": "/tmp/n.txt", "content": near}, sid="b"))
    assert label_ids(ctx(out)) == ["project_host_alpha"]


def test_file_tool_leg_never_denies_and_shows_no_trigger_rows(env):
    # The guarded memory's file name, read: a label row at most, never a deny.
    out = call(dict(env, **{FT: "1"}), tool_ev("Read", {"file_path": "/srv/data_wipe.sh"}))
    assert out.get("permissionDecision") is None
    assert label_ids(ctx(out)) == ["feedback_guarded"]
    out = call(dict(env, **{FT: "1"}), tool_ev("Grep", {"pattern": "deploycmd run"}, sid="t2"))
    assert out is None  # trigger words are not label rows
    assert all(x["decision"] != "deny" for x in log(env))


def test_file_tool_leg_shares_the_budget_and_de_dup_of_the_tool_leg(env):
    e = dict(env, **{FT: "1"})
    assert label_ids(ctx(call(e, bash("cat prometheus.yml", sid="d")))) == ["project_host_alpha"]
    assert call(e, tool_ev("Read", {"file_path": "/x/prometheus.yml"}, sid="d")) is None


def test_file_tool_leg_is_stopped_by_the_tool_leg_switch(env):
    e = dict(env, **{FT: "1", "NOBLIVION_GUARD_LABELS": "0"})
    assert call(e, tool_ev("Read", {"file_path": "/x/prometheus.yml"})) is None
    for off in ("0", "off", "false", "no", ""):
        ev = tool_ev("Read", {"file_path": "/x/prometheus.yml"}, sid=off or "e")
        assert call(dict(env, **{FT: off}), ev) is None


def test_file_tool_leg_ignores_bad_input(env):
    e = dict(env, **{FT: "1"})
    for ti in ({}, {"file_path": 3}, {"pattern": None}, {"file_path": ""}):
        assert call(e, tool_ev("Read", ti)) is None
    assert call(e, tool_ev("Edit", {"file_path": "/tmp/a.txt", "new_string": 5})) is None
    assert call(e, tool_ev("NotebookRead", {"file_path": "/x/prometheus.yml"})) is None


def test_file_tool_leg_off_does_not_load_the_label_module(env, monkeypatch):
    loaded = []
    real = gh._load
    monkeypatch.setattr(gh, "_load", lambda n: loaded.append(n) or real(n))
    assert call(env, tool_ev("Read", {"file_path": "/x/prometheus.yml"})) is None
    assert loaded == []


# 10. trust events: a label row is never a use --------------------------------
def _trust_uses(env, sid):
    p = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "by-session" / f"{sid}.trust-events.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return sorted(r["path"] for r in rows if r.get("kind") == "use")


@pytest.mark.parametrize("mode", ["", "a", "b", "c"])
def test_a_guard_call_with_trigger_and_label_rows_records_a_use_only_for_trigger_rows(env, mode):
    """Trigger rows on a guard call are a ``use`` (none in trust mode ``a``);
    a label row beside them is never a use, in any trust mode. A call with
    label rows only records nothing."""
    e = dict(env, NOBLIVION_TRUST_EVENTS="1", NOBLIVION_RECALL_TRUST_MODE=mode)
    sid = "tm" + (mode or "0")
    text = ctx(call(e, bash("deploycmd run --target multi_target.py", sid=sid)))
    assert len(label_ids(text)) == 1 and "feedback_trig_one" in text
    want = [] if mode == "a" else ["feedback_trig_one.md", "feedback_trig_two.md"]
    assert _trust_uses(e, sid) == want
    sid = "tl" + (mode or "0")
    out = call(e, bash("cat /etc/prometheus/prometheus.yml", sid=sid))
    assert label_ids(ctx(out)) == ["project_host_alpha"]
    assert _trust_uses(e, sid) == []
