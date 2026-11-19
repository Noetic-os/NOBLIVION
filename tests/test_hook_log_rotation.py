# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log rotation of the hook logs (NOBLIVION-69).

``recall.log`` and ``memory-dir.log`` (one line per prompt), the guard log
(it holds command text) and the memory sync log grew without a limit. Each
now moves to ``<name>.1`` past ``LOG_MAX_BYTES``, the way the stop check log
does: a log is at most about twice that size. NOBLIVION-81 added the trust
flush, continuity and error recall logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hookload import load_hook

rh = load_hook("recall_hook", "hooktest_log_rotation_recall")
gh = load_hook("guard_hook", "hooktest_log_rotation_guard")
ms = load_hook("memory_sync_hook", "hooktest_log_rotation_sync")
tf = load_hook("trust_flush", "hooktest_log_rotation_flush")
ch = load_hook("continuity_hook", "hooktest_log_rotation_continuity")
er = load_hook("error_recall_hook", "hooktest_log_rotation_error_recall")


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


def _flush(folder: Path) -> Path:
    tf.log(str(folder), "s-demo-1", "ok:nothing bad=0")
    return folder / tf.LOG_NAME


def _continuity(folder: Path) -> Path:
    ch.log_line(str(folder), "SessionStart", "s-demo-1", 0, 0, 5, "ok")
    return folder / ch.LOG_NAME


def _error_recall(folder: Path) -> Path:
    path = folder / "error-recall-log.jsonl"
    assert er.log_line({"NOBLIVION_ERROR_RECALL_LOG": str(path)}, {"decision": "none"})
    return path


WRITERS = [
    pytest.param(rh, _recall, "recall.log", id="recall"),
    pytest.param(rh, _memory_dir, "memory-dir.log", id="memory-dir"),
    pytest.param(gh, _guard, "guard-log.jsonl", id="guard"),
    pytest.param(ms, _sync, "memory_sync.log", id="memory-sync"),
    pytest.param(tf, _flush, "trust-flush.log", id="trust-flush"),  # NOBLIVION-81
    pytest.param(ch, _continuity, "continuity.log", id="continuity"),
    pytest.param(er, _error_recall, "error-recall-log.jsonl", id="error-recall"),
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


@pytest.mark.parametrize(("mod", "write", "name"), WRITERS)
def test_a_rotation_held_by_another_hook_is_skipped(tmp_path, mod, write, name):
    # NOBLIVION-83: a second hook that saw the old size must not move the new
    # short log over the full .1. The holder of <log>.lock rotates; this one
    # skips and appends.
    fcntl = pytest.importorskip("fcntl")
    folder = tmp_path / "data" / "cache"
    folder.mkdir()
    log = folder / name
    with open(log, "wb") as fh:
        fh.truncate(mod.LOG_MAX_BYTES + 1)
    old = folder / (name + ".1")
    old.write_text("the full rotation\n", encoding="utf-8")
    with open(folder / (name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write(folder)
    assert old.read_text(encoding="utf-8") == "the full rotation\n"
    assert log.stat().st_size > mod.LOG_MAX_BYTES
    write(folder)  # the lock is free again: this line rotates
    assert old.stat().st_size > mod.LOG_MAX_BYTES
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_a_rotation_after_a_stale_size_check_keeps_the_dot_1(tmp_path, monkeypatch):
    # NOBLIVION-83: another hook rotated between this hook's size check and its
    # lock. The size read again under the lock is small, so .1 stays.
    hc = load_hook("hook_config", "hooktest_log_rotation_config")
    log = tmp_path / "demo.log"
    log.write_text("one new line\n", encoding="utf-8")
    old = tmp_path / "demo.log.1"
    old.write_text("the full rotation\n", encoding="utf-8")
    real = hc.os.path.getsize
    sizes = []

    def stale_once(path):
        sizes.append(path)
        return 101 if len(sizes) == 1 else real(path)

    monkeypatch.setattr(hc.os.path, "getsize", stale_once)
    hc.rotate_log(log, 100)
    assert len(sizes) == 2
    assert old.read_text(encoding="utf-8") == "the full rotation\n"
    assert log.read_text(encoding="utf-8") == "one new line\n"


def test_the_limit_matches_the_stop_check_log():
    sc = load_hook("stop_checks", "hooktest_log_rotation_stop")
    assert rh.LOG_MAX_BYTES == gh.LOG_MAX_BYTES == ms.LOG_MAX_BYTES == sc.LOG_MAX
    assert tf.LOG_MAX_BYTES == ch.LOG_MAX_BYTES == er.LOG_MAX_BYTES == sc.LOG_MAX
