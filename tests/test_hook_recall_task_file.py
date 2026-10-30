# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: rank on the text of a task file (NOBLIVION-158).

When the prompt is, or starts with, the path of a readable ``.md`` or ``.txt``
file under the home folder, the query is the first 2,000 characters of that file.
A path outside the home folder, an unreadable path, another extension and a
credential-like name keep the query on the prompt text. Every test runs on a tmp
home folder, cache folder and data folder, and the store call is replaced.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from hookload import load_hook
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp home and a tmp cache folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_task_file")

SID = "task-file-test-session"
TASK = "Rotate the signing keys for the release pipeline, then update the runbook."


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))
    return folder


def _file(home, name, text=TASK):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _query_sent(monkeypatch, tmp_path, prompt, **extra):
    """The query the hook sends to the store for ``prompt``."""
    seen = []

    def fake(query, k, environ=None, *args, **kwargs):
        seen.append(query)
        return []

    monkeypatch.setattr(hook, "recall_index", fake)
    env = hook_env(tmp_path, NOBLIVION_RECALL_INDEX="1", **extra)
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt=prompt))
    hook.run(payload, io.StringIO(), env)
    return seen[0] if seen else None


def _log(tmp_path) -> str:
    return (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()[-1]


# ── the text of the file is the query ───────────────────────────────────────


def test_a_bare_md_path_ranks_on_the_file_text(home, monkeypatch, tmp_path):
    path = _file(home, "plans/release.md")
    assert _query_sent(monkeypatch, tmp_path, str(path)) == TASK
    assert _log(tmp_path).endswith(" ok:taskfile") or ":taskfile" in _log(tmp_path).split()[-1]


def test_a_tilde_txt_path_and_text_after_it_rank_on_the_file_text(home, monkeypatch, tmp_path):
    _file(home, "task.txt")
    assert _query_sent(monkeypatch, tmp_path, "~/task.txt please do this") == TASK


def test_a_quoted_path_ranks_on_the_file_text(home, monkeypatch, tmp_path):
    path = _file(home, "quoted.md")
    assert _query_sent(monkeypatch, tmp_path, f'"{path}"') == TASK


def test_the_hook_sends_at_most_2000_characters_of_a_large_file(home, monkeypatch, tmp_path):
    path = _file(home, "big.md", ("word " * 400_000))
    query = _query_sent(monkeypatch, tmp_path, str(path))
    assert query is not None and len(query) == hook.TASK_FILE_CHARS == 2000


def test_the_hook_reads_at_most_the_byte_cap_of_a_large_file(home, monkeypatch):
    path = _file(home, "big.md", "x" * 1_000_000)
    reads = []
    real_read = os.read

    def spy(fd, n):
        data = real_read(fd, n)
        reads.append((n, len(data)))
        return data

    monkeypatch.setattr(hook.os, "read", spy)
    assert len(hook.task_file_text(str(path))) == 2000
    assert reads == [(hook.TASK_FILE_BYTES, hook.TASK_FILE_BYTES)]


def test_a_file_of_multi_byte_text_gives_2000_characters(home):
    path = _file(home, "wide.md", "éè " * 5000)
    assert len(hook.task_file_text(str(path))) == 2000


def test_white_space_of_the_file_is_collapsed(home):
    path = _file(home, "ws.md", "# Plan\n\n\n  step   one\n\tstep two\n")
    assert hook.task_file_text(str(path)) == "# Plan step one step two"


# ── everything else keeps the prompt text ───────────────────────────────────


def test_a_path_outside_the_home_folder_keeps_the_prompt(home, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere" / "task.md"
    outside.parent.mkdir()
    outside.write_text(TASK, encoding="utf-8")
    assert _query_sent(monkeypatch, tmp_path, str(outside)) == str(outside)
    assert ":taskfile" not in _log(tmp_path)


def test_a_link_that_leaves_the_home_folder_keeps_the_prompt(home, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere" / "task.md"
    outside.parent.mkdir()
    outside.write_text(TASK, encoding="utf-8")
    link = home / "link.md"
    link.symlink_to(outside)
    assert _query_sent(monkeypatch, tmp_path, str(link)) == str(link)


def test_an_unreadable_path_keeps_the_prompt(home, monkeypatch, tmp_path):
    missing = home / "missing.md"
    assert _query_sent(monkeypatch, tmp_path, str(missing)) == str(missing)
    (home / "dir.md").mkdir()
    assert _query_sent(monkeypatch, tmp_path, str(home / "dir.md")) == str(home / "dir.md")


def test_a_file_without_read_permission_keeps_the_prompt(home, monkeypatch, tmp_path):
    path = _file(home, "locked.md")
    path.chmod(0)
    if os.access(path, os.R_OK):  # running as root
        pytest.skip("the file is readable for this user")
    assert _query_sent(monkeypatch, tmp_path, str(path)) == str(path)


def test_another_extension_keeps_the_prompt(home, monkeypatch, tmp_path):
    path = _file(home, "task.py")
    assert _query_sent(monkeypatch, tmp_path, str(path)) == str(path)
    path = _file(home, "task.md.bak")
    assert _query_sent(monkeypatch, tmp_path, str(path)) == str(path)


def test_a_path_in_the_middle_of_the_prompt_keeps_the_prompt(home, monkeypatch, tmp_path):
    path = _file(home, "task.md")
    prompt = f"please read {path}"
    assert _query_sent(monkeypatch, tmp_path, prompt) == prompt


def test_a_credential_like_name_or_key_folder_keeps_the_prompt(home):
    assert hook.task_file_text(str(_file(home, "api-token.md"))) is None
    assert hook.task_file_text(str(_file(home, "Secrets.txt"))) is None
    assert hook.task_file_text(str(_file(home, ".ssh/notes.md"))) is None


def test_a_fifo_is_not_read(home):
    fifo = home / "pipe.md"
    os.mkfifo(fifo)
    assert hook.task_file_text(str(fifo)) is None  # returns, does not block


def test_an_empty_file_keeps_the_prompt(home, monkeypatch, tmp_path):
    path = _file(home, "empty.md", " \n ")
    assert _query_sent(monkeypatch, tmp_path, str(path)) == str(path)


def test_the_switch_off_keeps_the_prompt(home, monkeypatch, tmp_path):
    path = _file(home, "task.md")
    sent = _query_sent(monkeypatch, tmp_path, str(path), NOBLIVION_RECALL_TASK_FILE="0")
    assert sent == str(path)


def test_a_tool_call_query_is_not_replaced(home, monkeypatch, tmp_path):
    path = _file(home, "task.md")
    seen = []
    monkeypatch.setattr(hook, "recall_index", lambda q, *a, **k: seen.append(q) or [])
    env = hook_env(tmp_path, NOBLIVION_RECALL_INDEX="1")
    payload = json.dumps(
        dict(
            hook_event_name="PreToolUse",
            session_id=SID,
            tool_name="Bash",
            tool_input={"command": f"cat {path}"},
        )
    )
    hook.run(payload, io.StringIO(), env)
    assert TASK not in seen
