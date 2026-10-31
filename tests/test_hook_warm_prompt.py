# SPDX-License-Identifier: AGPL-3.0-or-later
"""The warm prompt hook (NOBLIVION-162, step 2): an ``mcp_tool`` hook for the prompt command.

What these tests hold:

1. ``hooks.json`` names the tool of the MCP server of the plugin with the event fields, and the
   prompt command steps aside (a shell test of the flag file) when the flag file is there.
2. With the flag on, the tool answers what ``dispatch.py UserPromptSubmit`` prints for the same
   event, in a forked child that leaves the server as it was.
3. A store that is down or slow ends the prompt with no rows and no error. A fault in the child
   gives ``isError`` (a non-blocking error in Claude Code), never a blocking answer.
4. With the flag off the tool answers an empty text, is not listed, and forks nothing.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

import recall_helpers
from hookload import HOOKS, load_hook
from recall_helpers import INDEX_FLOOR_OFF, FakeStore, hook_env

dispatch = load_hook("dispatch", "hooktest_warm_dispatch")

ROOT = HOOKS.parent
CWD = "/work/proj/demo"
PY = sys.executable
PROMPTS = (
    "merge the pull request for the dispatcher change",
    "restart the store and check the embedding model",
    "why did the guard hook deny the git push",
)


def _load_mcp():
    path = ROOT / "mcp" / "recall_mcp.py"
    name = "hooktest_warm_mcp"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp = _load_mcp()
needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="the warm path forks")


def _hooks() -> list[dict[str, Any]]:
    doc = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    return [h for g in doc["hooks"]["UserPromptSubmit"] for h in g["hooks"]]


def _event(text: str, session: str) -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "prompt": text,
        "cwd": CWD,
        "transcript_path": CWD + "/t.jsonl",
        "permission_mode": "default",
    }


# -- hooks.json -----------------------------------------------------------------------------


def test_the_mcp_tool_hook_names_the_tool_of_the_plugin_server_and_the_event_fields():
    command, tool = _hooks()
    assert command["type"] == "command" and tool["type"] == "mcp_tool"
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    (key,) = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert tool["server"] == f"plugin:{plugin['name']}:{key}"
    assert tool["tool"] == mcp.PROMPT_HOOK_NAME
    assert tool["input"] == {k: "${" + k + "}" for k in dispatch.WARM_FIELDS}
    assert set(tool["input"]) == set(mcp.PROMPT_HOOK_SCHEMA["inputSchema"]["properties"])
    assert tool["timeout"] == command["timeout"] == dispatch.timeout_of("UserPromptSubmit")
    assert "async" not in tool and "onFailure" not in tool


def test_warm_event_keeps_the_fields_the_event_had():
    args = {k: "x" for k in dispatch.WARM_FIELDS}
    assert json.loads(dispatch.warm_event(args)) == args
    # Claude Code puts "" for a ${path} the event does not have; an extra key is not passed on
    args = {"session_id": "s", "prompt": "p", "cwd": "", "transcript_path": "", "other": "o"}
    assert json.loads(dispatch.warm_event(args)) == {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s",
        "prompt": "p",
    }


@pytest.mark.skipif(os.name == "nt", reason="the hook command is a shell command")
@pytest.mark.parametrize("flag", ["data", "override", "none"])
def test_the_prompt_command_steps_aside_when_the_flag_file_is_there(tmp_path, flag):
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    marker = tmp_path / "ran"
    (plugin / "hooks" / "dispatch.py").write_text(
        f"import sys\nopen({str(marker)!r}, 'a').write(sys.argv[1])\n", encoding="utf-8"
    )
    data, other = tmp_path / "data", tmp_path / "other"
    data.mkdir()
    other.mkdir()
    env = {"PATH": os.environ["PATH"], "CLAUDE_PLUGIN_DATA": str(data)}
    if flag == "data":
        (data / dispatch.WARM_FLAG).write_text("")
    elif flag == "override":  # NOBLIVION_DATA_DIR wins, as in hook_config.data_dir
        env["NOBLIVION_DATA_DIR"] = str(other)
        (data / dispatch.WARM_FLAG).write_text("")
    command = _hooks()[0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
    run = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == "" and run.stderr == ""
    assert marker.exists() is (flag != "data")
    if flag == "override":
        (other / dispatch.WARM_FLAG).write_text("")
        marker.unlink()
        subprocess.run(["sh", "-c", command], env=env, check=True)
        assert not marker.exists()


# -- the same inputs through both paths -------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FakeStore(tmp_path / "store-data")

    def search(path, query):
        project = (query.get("project") or [None])[0]
        text = "# feedback_x\n\n[claude_code_md: feedback_x.md]\n\nthe hit shape"
        return {"namespace": project, "results": [text]}

    s.route("/api/memories/search", search)
    yield s
    s.stop()


def _world(tmp_path: Path, name: str, store: FakeStore, **extra: str) -> dict[str, str]:
    base = tmp_path / name
    recall_helpers.write_store_files(base / "data", store.port, store.token)
    (base / "home").mkdir(parents=True, exist_ok=True)
    extra = {"NOBLIVION_RECALL_TIMEOUT_S": "2.0", **extra}
    return hook_env(
        base,
        HOME=str(base / "home"),
        NOBLIVION_STOP_CHECK_STATE=str(base / "stop"),
        NOBLIVION_TRUST_REPORT_FILE=str(base / "trust.json"),
        NOBLIVION_GUARD_TABLE=str(base / "table.json"),
        CLAUDE_PROJECT_DIR=CWD,
        **INDEX_FLOOR_OFF,
        **extra,
    )


def _flag(env: dict[str, str]) -> None:
    Path(env["NOBLIVION_DATA_DIR"], dispatch.WARM_FLAG).write_text("", encoding="utf-8")


def _flat(text: str) -> str:
    """The recall header names the milliseconds of the store call: not part of the comparison."""
    return re.sub(r", \d+ ms\)", ", N ms)", text)


def _command(env: dict[str, str], event: dict[str, Any]) -> tuple[int, str]:
    run = subprocess.run(
        [PY, str(HOOKS / "dispatch.py"), "UserPromptSubmit"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return run.returncode, run.stdout


def _tool(env: dict[str, str], event: dict[str, Any]) -> dict[str, Any]:
    """The ``tools/call`` result of the hook, through the JSON-RPC handler of the server."""
    args = {k: v for k, v in event.items() if k in dispatch.WARM_FIELDS}
    reply = mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": mcp.PROMPT_HOOK_NAME, "arguments": args},
        },
        env,
    )
    assert reply is not None
    return reply["result"]


@needs_fork
@pytest.mark.parametrize("text", PROMPTS)
def test_the_tool_answers_what_the_command_prints(tmp_path, store, text):
    old_env = _world(tmp_path, "old", store)
    new_env = _world(tmp_path, "new", store)
    _flag(new_env)
    code, expected = _command(old_env, _event(text, "sess-1"))
    result = _tool(new_env, _event(text, "sess-1"))
    assert code == 0 and "the hit shape" in expected
    assert result["isError"] is False
    assert _flat(result["content"][0]["text"]) == _flat(expected)


@needs_fork
def test_a_second_prompt_of_a_session_sees_the_state_the_first_one_wrote(tmp_path, store):
    """The rows already shown stay out, as for two commands: the state is on disk."""
    old_env = _world(tmp_path, "old", store)
    new_env = _world(tmp_path, "new", store)
    _flag(new_env)
    for text in (PROMPTS[0], PROMPTS[0], PROMPTS[1]):
        _, expected = _command(old_env, _event(text, "sess-2"))
        got = _tool(new_env, _event(text, "sess-2"))["content"][0]["text"]
        assert _flat(got) == _flat(expected)


@needs_fork
def test_a_store_that_is_down_ends_the_prompt_with_no_rows_and_no_error(tmp_path, store):
    old_env = _world(tmp_path, "old", store)
    new_env = _world(tmp_path, "new", store)
    _flag(new_env)
    store.stop()
    code, expected = _command(old_env, _event(PROMPTS[0], "sess-3"))
    result = _tool(new_env, _event(PROMPTS[0], "sess-3"))
    assert code == 0 and expected == ""
    assert result == {"content": [{"type": "text", "text": ""}], "isError": False}


@needs_fork
def test_a_slow_store_ends_the_prompt_in_time_with_no_rows_and_no_error(tmp_path, store):
    def slow(path, query):
        time.sleep(3)
        return {"results": ["# feedback_x\n\n[claude_code_md: feedback_x.md]\n\nlate"]}

    store.route("/api/memories/search", slow)
    store.route("/api/memories/index", slow)
    env = _world(tmp_path, "new", store, NOBLIVION_RECALL_TIMEOUT_S="0.3")
    _flag(env)
    started = time.monotonic()
    result = _tool(env, _event(PROMPTS[0], "sess-4"))
    assert time.monotonic() - started < 2.5
    assert result["isError"] is False and "late" not in result["content"][0]["text"]
    assert "the hit shape" not in result["content"][0]["text"]


# -- the flag --------------------------------------------------------------------------------


def test_the_flag_off_answers_nothing_lists_no_tool_and_forks_nothing(tmp_path, store, monkeypatch):
    env = _world(tmp_path, "off", store)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("forked with the flag off")

    monkeypatch.setattr(os, "fork", forbidden)
    assert _tool(env, _event(PROMPTS[0], "sess-5")) == {
        "content": [{"type": "text", "text": ""}],
        "isError": False,
    }
    tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, env)["result"]["tools"]
    assert [t["name"] for t in tools] == [mcp.TOOL_NAME, mcp.REMEMBER_NAME]
    assert store.store_paths == []


def test_the_flag_on_lists_the_tool_after_the_two_others(tmp_path, store):
    env = _world(tmp_path, "on", store)
    _flag(env)
    tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, env)["result"]["tools"]
    assert [t["name"] for t in tools] == [mcp.TOOL_NAME, mcp.REMEMBER_NAME, mcp.PROMPT_HOOK_NAME]


# -- the forked child ------------------------------------------------------------------------


@pytest.fixture
def fake_hooks(tmp_path, monkeypatch):
    """The dispatcher over a folder of small test modules."""
    folder = tmp_path / "fake-hooks"
    folder.mkdir()
    monkeypatch.setattr(dispatch, "HOOKS_DIR", folder)
    monkeypatch.setattr(dispatch, "_LOADED", {})
    monkeypatch.setattr(dispatch, "RESERVE_S", 0.2)

    monkeypatch.setitem(sys.modules, "dispatch", dispatch)  # the server uses this module
    shutil.copy(HOOKS / "hook_config.py", folder)  # the flag test reads the data dir through it

    def make(name: str, body: str, **step: Any) -> None:
        (folder / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
        steps, field = dispatch.PLAN["UserPromptSubmit"]
        monkeypatch.setitem(
            dispatch.PLAN,
            "UserPromptSubmit",
            ((*(() if name == "only" else steps), dispatch.Step(None, name, (), 2, **step)), field),
        )

    return make


@needs_fork
def test_the_child_leaves_the_server_as_it_was(fake_hooks, monkeypatch):
    fake_hooks(
        "only",
        """
        import os, sys
        COUNT = 0
        def main():
            global COUNT
            COUNT += 1
            os.environ["LEAK"] = "yes"
            os.chdir("/")
            sys.modules["leaked_module"] = sys
            print("count=%d env=%s" % (COUNT, os.environ.get("PARENT_ONLY")))
            return 0
        """,
    )
    monkeypatch.setenv("PARENT_ONLY", "p")
    cwd = os.getcwd()
    for _ in range(2):  # a fresh module each time: the count is 1 again
        code, out, err = dispatch.run_forked("UserPromptSubmit", "{}")
        assert (code, err) == (0, "")
        assert out.strip() == "count=1 env=p"
    assert "LEAK" not in os.environ and os.getcwd() == cwd and "leaked_module" not in sys.modules


@needs_fork
def test_the_child_cannot_write_to_the_stdout_of_the_server(fake_hooks, capfd):
    fake_hooks(
        "only",
        "import os\n"
        "def main():\n"
        "    os.write(1, b'junk on fd 1\\n')\n"
        "    print('answer')\n"
        "    return 0\n",
    )
    code, out, err = dispatch.run_forked("UserPromptSubmit", "{}")
    assert (code, out.strip()) == (0, "answer")
    assert "junk" not in capfd.readouterr().out


@needs_fork
def test_the_child_gets_the_environment_it_is_given(fake_hooks):
    fake_hooks("only", "import os\ndef main():\n    print(os.environ.get('GIVEN'))\n    return 0\n")
    out = dispatch.run_forked("UserPromptSubmit", "{}", {"GIVEN": "g"})[1]
    assert out.strip() == "g"


@needs_fork
@pytest.mark.parametrize(
    "body, fragment",
    [
        (
            "def main():\n    import sys\n    sys.stderr.write('stop it')\n    sys.exit(2)\n",
            "stop it",
        ),
        ("def main():\n    raise RuntimeError('boom')\n", "boom"),
    ],
)
def test_a_module_fault_is_a_tool_error_never_a_block(fake_hooks, tmp_path, store, body, fragment):
    fake_hooks("only", body)
    env = _world(tmp_path, "fault", store)
    _flag(env)
    result = _tool(env, _event(PROMPTS[0], "sess-6"))
    if fragment == "boom":  # the dispatcher drops a failed module: no rows, no error
        assert result == {"content": [{"type": "text", "text": ""}], "isError": False}
    else:  # exit 2 would block a command hook; the tool cannot block
        assert result["isError"] is True and fragment in result["content"][0]["text"]


@needs_fork
def test_a_child_that_dies_or_overruns_gives_exit_one_and_is_reaped(monkeypatch):
    monkeypatch.setattr(dispatch, "run_event", lambda *a: os._exit(3))
    code, out, err = dispatch.run_forked("UserPromptSubmit", "{}")
    assert (code, out) == (1, "") and "no answer" in err
    monkeypatch.setattr(dispatch, "run_event", lambda *a: time.sleep(30))
    monkeypatch.setattr(dispatch, "timeout_of", lambda event: 0.5)
    started = time.monotonic()
    code, out, err = dispatch.run_forked("UserPromptSubmit", "{}")
    assert time.monotonic() - started < 3
    assert (code, out) == (1, "") and "ran out of time" in err
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)


def test_no_fork_gives_exit_one_so_the_prompt_goes_on(monkeypatch):
    monkeypatch.delattr(os, "fork")
    code, out, err = dispatch.run_forked("UserPromptSubmit", "{}")
    assert (code, out) == (1, "") and "no fork" in err
