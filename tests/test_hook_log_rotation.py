# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log rotation of the hook logs (NOBLIVION-69).

``recall.log`` and ``memory-dir.log`` (one line per prompt), the guard log
(it holds command text) and the memory sync log grew without a limit. Each
now moves to ``<name>.1`` past ``LOG_MAX_BYTES``, the way the stop check log
does: a log is at most about twice that size.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hookload import load_hook

rh = load_hook("recall_hook", "hooktest_log_rotation_recall")
gh = load_hook("guard_hook", "hooktest_log_rotation_guard")
ms = load_hook("memory_sync_hook", "hooktest_log_rotation_sync")


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    (tmp_path / "data").mkdir()


def _recall(folder: Path) -> Path:
    rh.log_line(str(folder), "UserPromptSubmit", "s-demo-1", 0, 0, 5, "ok")
    return folder / "recall.log"


def _memory_dir(folder: Path) -> Path:
    rh.log_memory_dir_missing(str(folder), "UserPromptSubmit", "s-demo-1", "/work/demo")
    return folder / rh.MEMORY_DIR_LOG


def _guard(folder: Path) -> Path:
    path = folder / "guard-log.jsonl"
    assert gh.log_line({"NOBLIVION_GUARD_LOG": str(path)}, "s-demo-1", "Bash", "allow", [], "ls")
    return path


def _sync(folder: Path) -> Path:
    ms.log_line(folder, "PostToolUse", result="ok")
    return folder / ms.LOG_NAME


WRITERS = [
    pytest.param(rh, _recall, "recall.log", id="recall"),
    pytest.param(rh, _memory_dir, "memory-dir.log", id="memory-dir"),
    pytest.param(gh, _guard, "guard-log.jsonl", id="guard"),
    pytest.param(ms, _sync, "memory_sync.log", id="memory-sync"),
]


@pytest.mark.parametrize(("mod", "write", "name"), WRITERS)
def test_a_log_past_the_limit_moves_to_dot_1(tmp_path, mod, write, name):
    folder = tmp_path / "data" / "cache"
    folder.mkdir()
    log = folder / name
    big = mod.LOG_MAX_BYTES + 1
    with open(log, "wb") as fh:
        fh.truncate(big)  # sparse: no 5 MB write
    old = folder / (name + ".1")
    old.write_text("an older rotation\n", encoding="utf-8")
    assert write(folder) == log
    assert old.stat().st_size == big  # the full log replaced the older rotation
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(("mod", "write", "name"), WRITERS)
def test_a_log_under_the_limit_is_appended_to(tmp_path, mod, write, name):
    folder = tmp_path / "data" / "cache"
    folder.mkdir()
    log = folder / name
    with open(log, "wb") as fh:
        fh.truncate(mod.LOG_MAX_BYTES)  # at the limit, not past it
    write(folder)
    assert log.stat().st_size > mod.LOG_MAX_BYTES
    assert not (folder / (name + ".1")).exists()
    write(folder)  # now past the limit: the next line rotates
    assert (folder / (name + ".1")).exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_the_limit_matches_the_stop_check_log():
    sc = load_hook("stop_checks", "hooktest_log_rotation_stop")
    assert rh.LOG_MAX_BYTES == gh.LOG_MAX_BYTES == ms.LOG_MAX_BYTES == sc.LOG_MAX
