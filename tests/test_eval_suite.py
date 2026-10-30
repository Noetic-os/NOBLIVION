# SPDX-License-Identifier: AGPL-3.0-or-later
"""The zero-token suite (NOBLIVION-145), end to end on fictional data.

The runs start the real store (keyword mode, no model) and the real hooks. A
fixture folder stands in for the frozen gate 0 and test 4 inputs, which stay
outside the repository. The last tests break a hook in a copy of the build and
require the report to see it: a suite that cannot fail measures nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "eval_suite.py"
SLUG = "-proj-alpha"
OTHER_SLUG = "-proj-beta"
PROMPT = "Please run the zebra migration rollback for the billing tables"
OFF_TOPIC = "tell me about tomato soup"
NOTICE = "<task-notification>zebra migration rollback finished</task-notification>"
CANARY = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"


def load_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("eval_suite_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def note(name: str, rule: str, extra: str = "", body: str = "Body text.") -> str:
    return (
        f"---\nname: {name}\ndescription: {rule}\ntype: feedback\nrule: {rule}\n"
        f'apply: "{rule} Do it first."\nscope: tool\n{extra}---\n\n{body}\n'
    )


def write_corpus(root: Path) -> None:
    memory = root / SLUG / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    (memory / "feedback_zebra_rollback.md").write_text(
        note(
            "zebra-rollback",
            "Take a zebra migration snapshot before a billing rollback.",
            "triggers: [zebra migrate]\n",
            "The zebra migration rollback of the billing tables needs a snapshot first.",
        ),
        encoding="utf-8",
    )
    (memory / "feedback_no_force.md").write_text(
        note(
            "no-force",
            "Never push with force to the release branch.",
            "triggers: [git push]\n"
            'violates: "^git\\\\s+push\\\\s+--force\\\\b"\n'
            'example_repeat: "git push --force"\nexample_ok: "git push"\n',
        ),
        encoding="utf-8",
    )
    (memory / "feedback_flaky_widget.md").write_text(
        note(
            "flaky-widget",
            "The widget importer raises FlakyWidgetError when its cache is stale.",
            body="FlakyWidgetError means the widget cache is stale: clear it and run again.",
        ),
        encoding="utf-8",
    )


def transcript_lines(session: str, cwd: str) -> list[dict]:
    """A session with a prompt, a safe and a denied Bash call, a failed call and an
    Agent call. Timestamps within the last day, so ``--since-days`` keeps it."""
    import datetime as dt

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)

    def at(sec: int) -> str:
        return (start + dt.timedelta(seconds=sec)).isoformat()

    def common(sec: int) -> dict:
        return {"sessionId": session, "cwd": cwd, "timestamp": at(sec)}

    def assistant(sec: int, mid: str, block: dict) -> dict:
        usage = {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 5}
        message = {"id": mid, "usage": usage, "role": "assistant", "content": [block]}
        return {"type": "assistant", "message": message, **common(sec)}

    def tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
        return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}

    def result(sec: int, tool_id: str, text: str, is_error: bool, extra: object) -> dict:
        block = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": text,
            "is_error": is_error,
        }
        return {
            "type": "user",
            "message": {"role": "user", "content": [block]},
            "toolUseResult": extra,
            **common(sec),
        }

    failure = "Exit code 1\nTraceback (most recent call last):\nFlakyWidgetError: cache is stale"
    return [
        {"type": "user", "message": {"role": "user", "content": PROMPT}, **common(0)},
        {"type": "user", "message": {"role": "user", "content": NOTICE}, **common(0)},
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "prompt": NOTICE},
            **common(0),
        },
        assistant(1, "m1", tool_use("t1", "Bash", {"command": "ls -la"})),
        result(2, "t1", "ok", False, {"stdout": "ok", "stderr": "", "interrupted": False}),
        assistant(3, "m2", tool_use("t2", "Bash", {"command": "git push --force"})),
        result(4, "t2", "denied", True, "Error: denied"),
        assistant(5, "m3", tool_use("t3", "Bash", {"command": "python import_widget.py"})),
        result(6, "t3", failure, True, "Error: " + failure),
        assistant(7, "m4", tool_use("t4", "Agent", {"prompt": PROMPT, "subagent_type": "x"})),
        result(8, "t4", "done", False, {"status": "completed"}),
        assistant(9, "m5", {"type": "text", "text": "All done."}),
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_success",
                "hookEvent": "UserPromptSubmit",
                "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/recall_hook.py"',
                "stdout": "RECORDED ROWS",
                "content": "RECORDED ROWS",
                "durationMs": 200,
            },
            **common(10),
        },
        assistant(11, "m6", {"type": "text", "text": "Later call."}),
    ]


def write_transcripts(root: Path, slug: str = SLUG) -> None:
    folder = root / slug
    folder.mkdir(parents=True)
    session = "11111111-1111-4111-8111-111111111111"
    lines = transcript_lines(session, "/proj/alpha")
    (folder / f"{session}.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )


def write_bench(root: Path, home: str = "/frozen/home") -> None:
    """A folder with the layout of the frozen inputs, on fictional cases."""
    gate0 = root / "gate0"
    corpus = gate0 / "fx" / "base" / ".claude" / "projects"
    write_corpus(corpus)
    slug = "-frozen-home"
    corpus.joinpath(SLUG).rename(corpus / slug)
    repo = gate0 / "fx" / "base" / "tools-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    url = f"https://x-access-token:{CANARY}@example.invalid/org/repo.git"
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    w10 = gate0 / "fx" / "w10" / ".claude" / "projects"
    write_corpus(w10)
    w10.joinpath(SLUG).rename(w10 / slug)
    cases = [
        {
            "id": "1a",
            "W": "W1",
            "lesson": "feedback_zebra_rollback.md",
            "cwd": home,
            "prompt": PROMPT,
        },
        {
            "id": "1b",
            "W": "W1",
            "lesson": "feedback_flaky_widget.md",
            "cwd": home,
            "prompt": OFF_TOPIC,
        },
        {
            "id": "3a",
            "W": "W3",
            "lesson": "feedback_no_force.md",
            "cwd": home,
            "prompt": OFF_TOPIC,
            "hazard_commands": ["git push --force"],
        },
        {
            "id": "9a",
            "W": "W9",
            "lesson": None,
            "cwd": home + "/tools-repo",
            "prompt": OFF_TOPIC,
            "canary": CANARY,
            "hazard_commands": ["git remote -v"],
        },
        {
            "id": "10a",
            "W": "W10",
            "lesson": "feedback_saved.md",
            "cwd": home,
            "prompt": "",
            "session": [
                "Remember this for later: the quokka batch runs only on a Tuesday night.",
                "<new session> When does the quokka batch run?",
            ],
            "recall_queries": ["quokka batch night"],
        },
    ]
    (gate0 / "candidates.json").write_text(json.dumps(cases), encoding="utf-8")
    results = root / "test4" / "results"
    results.mkdir(parents=True)
    run = root / "test4" / "raw" / "test4" / "test4-s1a-B-1"
    run.mkdir(parents=True)
    (run / "cands40.json").write_text(json.dumps({"prompt": PROMPT}), encoding="utf-8")
    record = {
        "runid": "test4-s1a-B-1",
        "task": "s1a",
        "lesson": "feedback_zebra_rollback.md",
        "init": [{"cwd": home}],
    }
    (results / "test4.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def suite(*argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *argv],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def run_args(tmp: Path, out: Path, *extra: str, transcripts: str = "transcripts") -> list[str]:
    return [
        "run",
        "--out",
        str(out),
        "--transcripts",
        str(tmp / transcripts / "*" / "*.jsonl"),
        "--guard-log",
        str(tmp / "live" / "guard-log.jsonl"),
        "--live-data",
        str(tmp / "live"),
        "--store-python",
        sys.executable,
        "--env",
        "NOBLIVION_EMBED_BACKEND=none",
        "--cold-runs",
        "1",
        *extra,
    ]


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Path:
    """Fictional inputs and a fake live data folder that holds a sentinel."""
    tmp = tmp_path_factory.mktemp("suite")
    write_transcripts(tmp / "transcripts")
    write_transcripts(tmp / "bench-transcripts", "-frozen-home")
    write_corpus(tmp / "corpus")
    write_bench(tmp / "bench")
    live = tmp / "live"
    live.mkdir()
    (live / "token").write_text("sentinel-token", encoding="utf-8")
    entry = {
        "ts": "2100-01-01T00:00:00Z",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "agent_id": "main",
        "tool": "Bash",
        "decision": "deny",
        "text": "git push --force",
    }
    (live / "guard-log.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return tmp


@pytest.fixture(scope="module")
def plain_run(world: Path) -> tuple[subprocess.CompletedProcess, dict]:
    """One run with no frozen inputs."""
    out = world / "plain"
    args = run_args(world, out, "--corpus", str(world / "corpus"), "--bench", "")
    done = suite(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    return done, json.loads((out / "result.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bench_run(world: Path) -> tuple[subprocess.CompletedProcess, dict]:
    """One run with the fixture of the frozen inputs."""
    out = world / "with-bench"
    args = run_args(world, out, "--bench", str(world / "bench"), transcripts="bench-transcripts")
    done = suite(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    return done, json.loads((out / "result.json").read_text(encoding="utf-8"))


def test_no_bench_skips_with_a_clear_message(plain_run) -> None:
    done, doc = plain_run
    for name, label in (("gate0", "gate 0"), ("test4", "test 4")):
        section = doc["sections"][name]
        assert section["status"] == "skipped"
        assert section["reason"].startswith(label + " skipped")
        assert section["reason"] in done.stderr


def test_a_missing_bench_folder_is_a_skip_and_not_a_crash(world) -> None:
    out = world / "absent"
    args = run_args(
        world,
        out,
        "--bench",
        str(world / "not-there"),
        "--corpus",
        str(world / "corpus"),
        "--sections",
        "gate0,test4",
    )
    # --bench and --corpus together are refused only for a real folder: an
    # absent folder must not stop the run, so the corpus is the fallback.
    done = suite(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    doc = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert "frozen inputs absent" in doc["sections"]["gate0"]["reason"]


def test_replays_show_rows_from_the_real_hooks(plain_run) -> None:
    _, doc = plain_run
    sections = doc["sections"]
    shipped = sections["prompt_rows"]["arms"]["shipped"]
    assert shipped["prompts"] == 1
    assert shipped["prompts_with_rows"] == 1
    assert shipped["chars_per_prompt"] > 0
    # The notification fires the hook too, once though it is recorded twice.
    assert shipped["prompt_events"] == 2
    assert shipped["prompt_events_by_kind"] == {"typed": 1, "notification": 1}
    assert shipped["ms_p50"] > 0
    assert sections["guard_calls"]["decisions"]["deny"] == 1
    assert sections["guard_log"]["deny_logged"] == 1
    assert sections["guard_log"]["deny_kept"] == 1
    assert sections["guard_log"]["linked_to_a_transcript"] == 1
    assert sections["error_recall"]["failures"] == 2
    assert sections["subagent"]["agent_calls"] == 1
    assert sections["keyword_mode"]["store_mode"] == "keyword"
    assert doc["store"]["mode"] == "keyword"


def test_re_sent_tokens_are_chars_over_4_times_later_calls(plain_run) -> None:
    _, doc = plain_run
    cost = doc["sections"]["cost"]
    shipped = doc["sections"]["prompt_rows"]["arms"]["shipped"]
    # The two prompt events come before all 6 API calls of the session.
    assert shipped["resent_tokens"] == int(shipped["chars_per_event"] * 2 / 4 * 6)
    # The recorded hook text comes before the last call only: 13 characters.
    hook = cost["by_hook"]["UserPromptSubmit recall_hook.py"]
    assert hook["chars"] == len("RECORDED ROWS")
    assert hook["resent_tokens"] == int(13 / 4 * 1)
    assert cost["api_calls"] == 6
    assert cost["input_tokens"] == 6 * 1010
    assert cost["prompts"] == 1 and cost["prompt_events"] == 2
    assert 0 < cost["replay_share"]


def test_acceptance_121_numbers(plain_run) -> None:
    _, doc = plain_run
    accept = doc["sections"]["prompt_rows"]["acceptance_121"]
    assert accept["prompts"] == 1
    assert accept["chars_met"] is (accept["chars_per_prompt"] <= 1500)
    assert accept["rows_per_prompt"] > 0
    assert "hub_share" in accept
    assert "hub_notes_max_session_share" in accept
    assert "not measured" in accept["labelled_precision"]


def test_hook_time_and_listing_are_measured(plain_run) -> None:
    _, doc = plain_run
    timing = doc["sections"]["hook_time"]
    assert timing["wait_ms_by_event"]["UserPromptSubmit"] > 0
    assert timing["wait_per_turn_s"] is None  # the fixture session has no Stop record
    listing = doc["sections"]["mcp_listing"]
    assert listing["stable"] is True
    assert listing["schema_chars"] > 100


def test_files_are_private_and_hold_no_private_text(world, plain_run) -> None:
    _, doc = plain_run
    out = world / "plain"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for name in ("result.json", "summary.txt"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    text = (out / "result.json").read_text(encoding="utf-8")
    for secret in (PROMPT, "git push --force", "import_widget", "FlakyWidgetError: cache"):
        assert secret not in text


def test_the_live_data_folder_is_not_changed(world, plain_run, bench_run) -> None:
    live = world / "live"
    assert sorted(p.name for p in live.iterdir()) == ["guard-log.jsonl", "token"]
    assert (live / "token").read_text(encoding="utf-8") == "sentinel-token"


def test_a_stopped_run_stops_its_store(world) -> None:
    work = world / "stopped-work"
    args = run_args(world, world / "stopped", "--corpus", str(world / "corpus"))
    proc = subprocess.Popen(
        [sys.executable, str(TOOL), *args, "--work-dir", str(work)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    store_file = work / "home" / ".claude" / "plugins" / "data" / "noblivion-noblivion"
    store_file = store_file / "store.json"
    end = time.monotonic() + 60
    while time.monotonic() < end and not store_file.is_file():
        time.sleep(0.1)
    assert store_file.is_file()
    pid = json.loads(store_file.read_text(encoding="utf-8"))["pid"]
    proc.terminate()
    assert proc.wait(30) == 143
    end = time.monotonic() + 20
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    pytest.fail("the store outlived the run")


def test_bytecode_stays_out_of_the_build_under_test(world, broken_build) -> None:
    out = world / "no-bytecode"
    args = run_args(
        world,
        out,
        "--corpus",
        str(world / "corpus"),
        "--plugin-root",
        str(broken_build),
        "--sections",
        "mcp_listing,guard_calls",
    )
    done = suite(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    assert not list(broken_build.rglob("__pycache__"))


def test_a_work_dir_inside_the_live_folder_is_refused(world) -> None:
    done = suite(
        *run_args(
            world,
            world / "refused",
            "--corpus",
            str(world / "corpus"),
            "--work-dir",
            str(world / "live" / "work"),
        )
    )
    assert done.returncode != 0
    assert "live data folder" in done.stderr


def test_gate0_runs_through_the_hooks(bench_run) -> None:
    _, doc = bench_run
    gate = doc["sections"]["gate0"]
    assert gate["status"] == "ok", gate.get("reason")
    cases = gate["cases"]
    assert cases["1a"]["pass"] is True
    assert cases["1a"]["checks"][0]["rank"] == 1
    assert cases["1b"]["pass"] is False
    assert cases["3a"]["pass"] is True
    assert cases["9a"]["pass"] is True
    assert cases["10a"]["pass"] is True
    assert gate["passed"] == 4 and gate["total"] == 5
    assert CANARY not in json.dumps(doc)


def test_test4_slots_use_the_frozen_prompt(bench_run) -> None:
    _, doc = bench_run
    slots = doc["sections"]["test4"]
    assert slots["delivered"] == 1 and slots["total"] == 1
    assert slots["slots"]["s1a"]["rows"] >= 1


def test_compare_two_equal_runs_passes(world, bench_run) -> None:
    path = world / "with-bench" / "result.json"
    done = suite("compare", str(path), str(path))
    assert done.returncode == 0, done.stdout
    assert "delivery: not lower" in done.stdout
    assert "the two runs measured the same code" in done.stdout


def test_freeze_then_replay_the_same_input(world) -> None:
    first = world / "frozen"
    done = suite(
        *run_args(
            world,
            first,
            "--corpus",
            str(world / "corpus"),
            "--freeze-inputs",
            "--sections",
            "prompt_rows",
        )
    )
    assert done.returncode == 0, done.stderr[-2000:]
    inputs = first / "inputs"
    assert stat.S_IMODE(inputs.stat().st_mode) == 0o700
    files = [p for p in inputs.rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    second = world / "refrozen"
    args = [a for a in run_args(world, second)]
    args = args[: args.index("--transcripts")] + args[args.index("--transcripts") + 2 :]
    args = args[: args.index("--guard-log")] + args[args.index("--guard-log") + 2 :]
    done = suite(
        *args,
        "--corpus",
        str(world / "corpus"),
        "--inputs",
        str(inputs),
        "--sections",
        "prompt_rows",
    )
    assert done.returncode == 0, done.stderr[-2000:]
    before = json.loads((first / "result.json").read_text(encoding="utf-8"))
    after = json.loads((second / "result.json").read_text(encoding="utf-8"))
    assert before["inputs"]["transcripts_digest"] == after["inputs"]["transcripts_digest"]
    assert before["inputs"]["guard_log_lines"] == after["inputs"]["guard_log_lines"] == 1
    # Rows and not characters: a store with no seed draws new note ids.
    assert (
        before["sections"]["prompt_rows"]["arms"]["shipped"]["rows_per_prompt"]
        == after["sections"]["prompt_rows"]["arms"]["shipped"]["rows_per_prompt"]
        > 0
    )


# -- compare ---------------------------------------------------------------------


def result(**sections) -> dict:
    return {
        "build": {"label": "x", "code_digest": "a", "version": "1"},
        "inputs": {"transcripts_digest": "d", "corpus_digest": "c"},
        "store": {"mode": "hybrid", "memories": 3},
        "embedding_model": "m",
        "settings": {},
        "seconds": 1.0,
        "sections": sections,
    }


def gate(**passed) -> dict:
    return {
        "status": "ok",
        "passed": sum(passed.values()),
        "cases": {
            k: {"pass": v, "checks": [{"check": "prompt_rows", "pass": v}]}
            for k, v in passed.items()
        },
    }


def test_compare_exits_1_when_a_gate0_case_drops() -> None:
    tool = load_tool()
    text, code = tool.compare(
        result(gate0=gate(a=True, b=True)), result(gate0=gate(a=True, b=False)), False
    )
    assert code == 1
    assert "DELIVERY DROPPED: gate0 b" in text


def test_compare_exits_1_when_a_test4_slot_drops() -> None:
    tool = load_tool()
    before = {"status": "ok", "delivered": 1, "slots": {"s1a": {"delivered": True}}}
    after = {"status": "ok", "delivered": 0, "slots": {"s1a": {"delivered": False}}}
    text, code = tool.compare(result(test4=before), result(test4=after), False)
    assert code == 1
    assert "test4 s1a" in text


def test_compare_gives_no_verdict_when_the_section_is_skipped_after() -> None:
    tool = load_tool()
    skipped = {"status": "skipped", "reason": "no bench"}
    text, code = tool.compare(result(gate0=gate(a=True)), result(gate0=skipped), False)
    assert code == 2
    assert "no delivery verdict for: gate0" in text


def test_compare_refuses_different_inputs_without_the_flag() -> None:
    tool = load_tool()
    before = result(gate0=gate(a=True))
    after = result(gate0=gate(a=True))
    after["inputs"]["corpus_digest"] = "other"
    text, code = tool.compare(before, after, False)
    assert code == 2
    assert "inputs differ: corpus_digest" in text
    assert tool.compare(before, after, True)[1] == 0


def test_compare_refuses_a_different_store_mode() -> None:
    tool = load_tool()
    before = result(gate0=gate(a=True))
    after = result(gate0=gate(a=True))
    after["store"]["mode"] = "keyword"
    assert tool.compare(before, after, False)[1] == 2


def test_compare_fails_when_the_later_run_is_degraded() -> None:
    tool = load_tool()
    after = result(gate0=gate(a=True), prompt_rows={"status": "degraded"})
    text, code = tool.compare(result(gate0=gate(a=True)), after, False)
    assert code == 2
    assert "after: prompt_rows is degraded" in text


def test_compare_lists_a_lost_check_without_failing() -> None:
    tool = load_tool()
    before = gate(a=True)
    before["cases"]["a"]["checks"].append({"check": "guard_bash[0]", "pass": True})
    after = gate(a=True)
    after["cases"]["a"]["checks"].append({"check": "guard_bash[0]", "pass": False})
    text, code = tool.compare(result(gate0=before), result(gate0=after), False)
    assert code == 0
    assert "a/guard_bash[0]" in text


def test_compare_marks_a_gain_and_a_loss() -> None:
    tool = load_tool()
    assert tool.delta_text(100, 80, "down").endswith("better")
    assert tool.delta_text(100, 120, "down").endswith("worse")
    assert tool.delta_text(5, 6, "up").endswith("better")
    assert tool.delta_text(5, 5, "up") == "same"
    assert tool.delta_text(None, 5, "up") == ""


def test_fail_states_counts_a_fail_open_and_a_keyword_answer() -> None:
    tool = load_tool()
    lines = [
        "ts event=UserPromptSubmit session=a hits=1 chars=9 ms=12 ok:joined1of1",
        "ts event=UserPromptSubmit session=a hits=0 chars=0 ms=2001 fail:timeout",
        "ts event=UserPromptSubmit session=a hits=1 chars=9 ms=12 ok:rerank_off:keyword",
    ]
    assert tool.fail_states(lines) == {"ok": 1, "fail": 1, "keyword": 1}


def test_model_text_is_what_the_model_reads() -> None:
    tool = load_tool()
    context = json.dumps({"hookSpecificOutput": {"additionalContext": "ROWS"}})
    deny = json.dumps(
        {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "NO"}}
    )
    assert tool.model_text("PreToolUse", context) == "ROWS"
    assert tool.model_text("PreToolUse", deny) == "NO"
    assert tool.model_text("UserPromptSubmit", "plain rows") == "plain rows"
    assert tool.model_text("PreToolUse", "plain output") == ""
    assert tool.decision_of(deny) == "deny"
    assert tool.decision_of(context) == "rows"
    assert tool.decision_of("") == "silent"


def test_found_reads_the_name_field_and_the_rule() -> None:
    tool = load_tool()
    assert tool.found("- [id 1] text (feedback-a-b)\n", "feedback_a_b.md", "") == (True, 1)
    rows = "- [id 1] x (other)\n- [id 2] y (quokka)\n"
    assert tool.found(rows, "n.md", "", "quokka") == (True, 2)
    assert tool.found("- [id 1] Do the safe thing today\n", "n.md", "Do the safe thing") == (
        True,
        1,
    )
    assert tool.found("nothing here\n", "note_x.md", "A rule") == (False, None)


def test_verdict_ignores_an_information_check() -> None:
    tool = load_tool()
    checks = [
        {"check": "memory_md_unchanged", "pass": True},
        {"check": "new_session_rows(info)", "pass": False},
        {"check": "new_session_found", "pass": True},
    ]
    assert tool.verdict({"W": "W10"}, checks) is True
    checks[2]["pass"] = False
    assert tool.verdict({"W": "W10"}, checks) is False


# -- a broken build must show ----------------------------------------------------


@pytest.fixture(scope="module")
def broken_build(tmp_path_factory) -> Path:
    """A copy of the build whose prompt hook prints nothing and fails its log line."""
    root = tmp_path_factory.mktemp("broken") / "build"
    root.mkdir()
    for name in ("hooks", "mcp", "src", "config", "skills", ".claude-plugin"):
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    hook = root / "hooks" / "recall_hook.py"
    hook.write_text(
        hook.read_text(encoding="utf-8")
        + "\n\ndef main(stdin=None, stdout=None, environ=None):\n    return 0\n",
        encoding="utf-8",
    )
    return root


def test_a_silent_prompt_hook_is_a_failed_run_and_a_delivery_drop(
    world, bench_run, broken_build
) -> None:
    out = world / "broken-run"
    done = suite(
        *run_args(
            world,
            out,
            "--bench",
            str(world / "bench"),
            "--plugin-root",
            str(broken_build),
            transcripts="bench-transcripts",
        )
    )
    assert done.returncode == 3, done.stderr[-2000:]
    assert "prompt_rows" in done.stderr
    report = suite("compare", str(world / "with-bench" / "result.json"), str(out / "result.json"))
    assert report.returncode == 1
    assert "DELIVERY DROPPED" in report.stdout
    assert "gate0 1a" in report.stdout and "test4 s1a" in report.stdout


def test_a_session_with_no_memory_folder_is_left_out_and_counted(world) -> None:
    out = world / "no-folder"
    args = run_args(
        world,
        out,
        "--corpus",
        str(world / "corpus"),
        "--sections",
        "prompt_rows,guard_calls,error_recall,subagent",
        transcripts="bench-transcripts",
    )
    done = suite(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    doc = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert doc["sections"]["guard_calls"]["status"] == "skipped"
    arms = doc["sections"]["prompt_rows"]
    assert arms["transcripts_without_memory_folder"] == 1
    assert arms["status"] == "degraded" or arms["arms"]["shipped"]["prompts"] == 0


def test_a_build_without_the_tool_can_be_measured(world, broken_build) -> None:
    # --plugin-root reads hooks from that tree, and the replay tools from this one.
    assert not (broken_build / "tools").exists()
    env = dict(os.environ)
    done = suite(
        *run_args(
            world,
            world / "other-build",
            "--corpus",
            str(world / "corpus"),
            "--plugin-root",
            str(broken_build),
            "--label",
            "other",
            "--sections",
            "mcp_listing",
        ),
        env=env,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    doc = json.loads((world / "other-build" / "result.json").read_text(encoding="utf-8"))
    assert doc["build"]["label"] == "other"
    assert doc["build"]["version"]
