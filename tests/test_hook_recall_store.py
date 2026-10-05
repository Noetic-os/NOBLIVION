# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST hooks against the local store (design doc 0001, sections 3.3,
3.5, 4.0 and 8.4): store discovery through ``store.json``, the listener
proof before the token, fail-open when the store is down or unproven, the
store start request, keyword mode, and the end-to-end path against a real
store with a fake embedder.

Fictional data only. Every socket is on 127.0.0.1.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import closing
from pathlib import Path

import pytest

import recall_helpers
from hookload import load_hook
from noblivion import db, embedding, launcher, store
from recall_helpers import (
    INDEX_FLOOR_OFF,
    FakeStore,
    hook_env,
    index_answer,
    index_row,
    proof_for,
)
from store_helpers import FakeEmbedder
from test_store import Running, fake_service, make_store, wait_for, write_memory

isolated_home = recall_helpers.isolated_home  # a fixture

ROOT = "-work-proj-demo"


@pytest.fixture
def rh():
    return load_hook("recall_hook", "hooktest_recall_store")


def _prompt(text: str, **extra) -> str:
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s-demo-1", "prompt": text, **extra}
    )


def _run(rh, env: dict, payload: str) -> str:
    out = io.StringIO()
    assert rh.main(stdin=io.StringIO(payload), stdout=out, environ=env) == 0
    return out.getvalue()


def _log(env: dict) -> str:
    path = Path(env["NOBLIVION_RECALL_CACHE_DIR"]) / "recall.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _memory_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "projects" / ROOT / "memory"
    write_memory(
        folder,
        "tests use venv",
        "The system python has no pytest; use the project venv.",
        "Run pytest with the venv python of the project, never the system python.",
    )
    write_memory(
        folder,
        "deploy checklist",
        "Check the release notes before a deploy.",
        "Read the changelog, then tag the release.",
    )
    return folder


# -- the store client: proof, discovery, start ---------------------------------------


def test_the_hook_proof_matches_the_store_launcher():
    sc = load_hook("store_client", "hooktest_store_client_parity")
    token, nonce = "ab" * 32, "0123456789abcdef0123456789abcdef"
    assert sc.health_proof(token, nonce) == launcher.health_proof(token, nonce)
    assert sc.proof_ok({"proof": launcher.health_proof(token, nonce)}, token, nonce)
    assert not sc.proof_ok({"proof": launcher.health_proof(token, nonce[::-1])}, token, nonce)
    assert not sc.proof_ok({"proof": None}, token, nonce)
    assert not sc.proof_ok(["not", "a", "dict"], token, nonce)


def test_locate_needs_store_json_and_a_valid_token(tmp_path):
    sc = load_hook("store_client", "hooktest_store_client_locate")
    env = hook_env(tmp_path)
    data = Path(env["NOBLIVION_DATA_DIR"])
    with pytest.raises(sc.StoreUnavailable) as err:
        sc.locate(env)
    assert err.value.reason == sc.DOWN
    data.mkdir(parents=True)
    (data / "store.json").write_text(json.dumps({"pid": 4242, "port": 40123}))
    with pytest.raises(sc.StoreUnavailable) as err:
        sc.locate(env)
    assert err.value.reason == sc.NO_TOKEN
    (data / "token").write_text("not-a-token\n")
    with pytest.raises(sc.StoreUnavailable):
        sc.locate(env)
    (data / "token").write_text("cd" * 32 + "\n")
    assert sc.locate(env) == (40123, "cd" * 32)
    (data / "store.json").write_text(json.dumps({"pid": 4242, "port": True}))
    with pytest.raises(sc.StoreUnavailable):
        sc.locate(env)


def test_request_start_runs_the_launcher_detached(tmp_path):
    sc = load_hook("store_client", "hooktest_store_client_start")
    env = hook_env(tmp_path, NOBLIVION_STORE_AUTOSTART="1")
    calls = []

    class Child:
        returncode = None

    def popen(argv, **kw):
        calls.append((argv, kw))
        return Child()

    assert sc.request_start(env, popen=popen) == "no_launcher"
    exe = Path(env["NOBLIVION_DATA_DIR"]) / "venv" / "bin" / "noblivion"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    assert sc.request_start(env, popen=popen) == "spawned"
    argv, kw = calls[-1]
    assert argv == [str(exe), "ensure-running"]
    assert kw["start_new_session"] is True
    assert kw["env"]["NOBLIVION_DATA_DIR"] == env["NOBLIVION_DATA_DIR"]
    # A fresh spawn stamp: a start is under way, no second launcher.
    (Path(env["NOBLIVION_DATA_DIR"]) / "spawn.stamp").touch()
    assert sc.request_start(env, popen=popen) == "starting"
    assert len(calls) == 1
    # The SessionStart leg ignores the stamp; the launcher checks it itself.
    assert sc.request_start(env, check_stamp=False, popen=popen) == "spawned"
    assert sc.request_start(dict(env, NOBLIVION_STORE_AUTOSTART="0"), popen=popen) == "off"
    # A configured entry point wins; a relative one is refused.
    other = tmp_path / "bin" / "noblivion"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o700)
    assert sc.launcher_bin(dict(env, NOBLIVION_BIN=str(other))) == str(other)
    assert sc.launcher_bin(dict(env, NOBLIVION_BIN="bin/noblivion")) is None

    def boom(argv, **kw):
        raise OSError("no exec")

    assert sc.request_start(env, check_stamp=False, popen=boom) == "error"


def test_session_start_hook_prints_nothing_and_exits_0(tmp_path, capsys):
    sc = load_hook("store_client", "hooktest_store_client_main")
    env = hook_env(tmp_path, NOBLIVION_STORE_AUTOSTART="1")
    assert sc.main(stdin=io.StringIO('{"hook_event_name": "SessionStart"}'), environ=env) == 0
    assert sc.main(stdin=io.StringIO("not json"), environ=env) == 0
    assert capsys.readouterr().out == ""


# -- fail open: store down or unproven -------------------------------------------------


def test_no_store_json_fails_open_as_store_down(rh, tmp_path):
    env = hook_env(tmp_path)
    assert _run(rh, env, _prompt("how do I run the tests")) == ""
    assert "fail:store_down" in _log(env)


@pytest.mark.parametrize("proof", ["wrong", "none", "echo"])
def test_a_listener_without_the_token_gets_no_token_and_no_prompt(rh, tmp_path, proof):
    env = hook_env(tmp_path)
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"]), proof=proof) as fake:
        fake.route("/api/memories/", {"results": ["# leaked\n\n[claude_code_md: x.md]\n\nboo"]})
        out = _run(rh, env, _prompt("how do I run the tests"))
    assert out == ""
    assert "fail:foreign_listener" in _log(env)
    # Only the proof request was sent, with no token and no prompt text.
    assert [p for p, _q, _h in fake.requests] == ["/health"]
    _path, query, headers = fake.requests[0]
    assert "authorization" not in headers
    assert set(query) == {"nonce"}
    assert len(query["nonce"][0]) == 32


def test_a_store_without_an_answer_fails_open(rh, tmp_path):
    env = hook_env(tmp_path)
    fake = FakeStore(Path(env["NOBLIVION_DATA_DIR"]))
    fake.stop()  # store.json names a port nobody listens on
    assert _run(rh, env, _prompt("how do I run the tests")) == ""
    assert "fail:store_down" in _log(env)


def test_the_token_goes_only_to_a_proven_store_and_the_proof_is_kept(rh, tmp_path):
    env = hook_env(tmp_path, NOBLIVION_RECALL_MEMORY_DIR=str(tmp_path / "projects" / ROOT / "m"))
    entry = (
        "# tests use venv\n\nUse the venv.\n\n[claude_code_md: feedback_tests.md]\n\nRun pytest."
    )
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"])) as fake:
        fake.route("/api/memories/search", {"results": [entry], "namespace": "claude_code"})
        hits = rh.recall("run the tests", 3, env)
        rh.recall("run the tests again", 3, env)
    assert [h.title for h in hits] == ["tests use venv"]
    paths = [p for p, _q, _h in fake.requests]
    assert paths == ["/health", "/api/memories/search", "/api/memories/search"]
    for _path, query, headers in fake.requests[1:]:
        assert headers["authorization"] == f"Bearer {fake.token}"
        assert query["root"] == [ROOT]
        assert query["project"] == ["claude_code"]


def test_a_namespace_mismatch_is_refused(rh, tmp_path):
    env = hook_env(tmp_path)
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"])) as fake:
        fake.route("/api/memories/index", index_answer([index_row(1, 7, "x")], namespace="other"))
        with pytest.raises(rh.RecallError) as err:
            rh.recall_index("anything", 5, env)
    assert err.value.reason == "namespace_mismatch"


def test_a_store_down_asks_the_launcher(rh, tmp_path, monkeypatch):
    env = hook_env(tmp_path, NOBLIVION_STORE_AUTOSTART="1")
    asked = []
    monkeypatch.setattr(rh._STORE, "request_start", lambda e, **kw: asked.append(e) or "spawned")
    with pytest.raises(rh.RecallError) as err:
        rh.recall_index("anything", 5, env)
    assert err.value.reason == "store_down"
    assert len(asked) == 1


def test_transport_guard_is_loopback_only(rh):
    assert rh.transport_reason("http://127.0.0.1:8894/x") is None
    assert rh.transport_reason("http://[::1]:8894/x") is None
    assert rh.transport_reason("http://localhost:8894/x") == "plaintext_url"
    assert rh.transport_reason("http://10.1.2.3:8894/x") == "plaintext_url"
    assert rh.transport_reason("http://203.0.113.7:8894/x") == "plaintext_url"
    assert rh.transport_reason("https://example.invalid/x") is None
    assert rh.transport_reason("ftp://127.0.0.1/x") == "bad_url"


# -- the session root ---------------------------------------------------------------------


def test_session_env_derives_the_memory_folder_and_root_from_cwd(rh, isolated_home):
    cwd = "/work/proj/demo"
    folder = isolated_home / ".claude" / "projects" / ROOT / "memory"
    env = rh.session_env({}, {"cwd": cwd})
    assert env == {"NOBLIVION_RECALL_ROOT": ROOT}  # no folder yet: root only
    folder.mkdir(parents=True)
    env = rh.session_env({}, {"cwd": cwd})
    assert env["NOBLIVION_RECALL_MEMORY_DIR"] == str(folder)
    assert rh.session_root(env) == ROOT
    # An env folder wins, and names the root.
    env = rh.session_env(
        {"NOBLIVION_RECALL_MEMORY_DIR": "/data/projects/-other/memory"}, {"cwd": cwd}
    )
    assert rh.session_root(env) == "-other"
    assert rh.session_env({}, {"cwd": "relative/dir"}) == {}
    assert rh.session_root({}) is None


# -- keyword mode (section 8.4) ------------------------------------------------------------


def test_keyword_mode_keeps_the_store_order_and_skips_the_floor(rh, tmp_path):
    folder = _memory_folder(tmp_path)
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_RECALL_INDEX_RERANK="1",
        NOBLIVION_RECALL_INDEX_MIN_SCORE="0.5",
        NOBLIVION_RECALL_MEMORY_DIR=str(folder),
    )
    rows = [
        index_row(1, 21, "deploy checklist", "Check the release notes.", score=None),
        index_row(2, 22, "tests use venv", "Use the project venv.", score=None),
    ]
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"])) as fake:
        fake.route("/api/memories/index", index_answer(rows, mode="keyword"))
        out = _run(rh, env, _prompt("venv pytest python tests"))
    assert out.index("deploy checklist") < out.index("tests use venv")
    log = _log(env)
    assert ":rerank_off:keyword" in log and ":floor_off:keyword" in log


# -- the default index floor and the model of the answer (NOBLIVION-48) ---------------------


@pytest.mark.parametrize(
    ("named", "note", "low_shown"),
    [
        ({"model": "BAAI/bge-small-en-v1.5"}, ":floor1of2", False),
        ({"model": "example/other-embedder"}, ":floor_off:model", True),
        ({}, ":floor_off:no_model", True),  # the answer of a store of an older version
        ({"model": None}, ":floor_off:no_model", True),
    ],
)
def test_the_default_index_floor_is_for_the_measured_model(rh, tmp_path, named, note, low_shown):
    env = hook_env(tmp_path, NOBLIVION_RECALL_INDEX="1")
    assert "NOBLIVION_RECALL_INDEX_MIN_SCORE" not in env
    assert rh.THRESHOLD_MODEL == "BAAI/bge-small-en-v1.5"
    rows = [
        index_row(1, 21, "deploy checklist", "Check the release notes.", score=0.71),
        index_row(2, 22, "tests use venv", "Use the project venv.", score=0.40),
    ]
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"])) as fake:
        fake.route("/api/memories/index", {**index_answer(rows), **named})
        out = _run(rh, env, _prompt("venv pytest python tests"))
    assert "deploy checklist" in out
    assert ("tests use venv" in out) is low_shown
    assert _log(env).split()[-1] == "ok" + note


def test_a_set_index_floor_applies_to_a_store_that_names_no_model(rh, tmp_path):
    env = hook_env(tmp_path, NOBLIVION_RECALL_INDEX="1", NOBLIVION_RECALL_INDEX_MIN_SCORE="0.5")
    rows = [
        index_row(1, 21, "deploy checklist", "Check the release notes.", score=0.71),
        index_row(2, 22, "tests use venv", "Use the project venv.", score=0.40),
    ]
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"])) as fake:
        fake.route("/api/memories/index", index_answer(rows))
        out = _run(rh, env, _prompt("venv pytest python tests"))
    assert "deploy checklist" in out and "tests use venv" not in out
    assert _log(env).split()[-1] == "ok:floor1of2"


# -- end to end against a real store --------------------------------------------------------


@pytest.fixture
def real_store(tmp_path):
    folder = _memory_folder(tmp_path)
    run = Running(make_store(tmp_path))
    assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
    assert wait_for(lambda: run.store.embedding.state == embedding.STATE_READY)
    yield run, folder
    run.stop()


def _store_env(tmp_path: Path, folder: Path, **extra: str) -> dict:
    # The fake embedder's cosines are not on the scale of the real model, so
    # the default floors (measured for the real model) do not fit them.
    extra.setdefault("NOBLIVION_RECALL_MIN_SCORE", "0")
    extra = {**INDEX_FLOOR_OFF, **extra}
    return hook_env(tmp_path, NOBLIVION_RECALL_MEMORY_DIR=str(folder), **extra)


def test_end_to_end_prompt_recall_from_a_real_store(rh, tmp_path, real_store):
    run, folder = real_store
    env = _store_env(tmp_path, folder)
    assert Path(env["NOBLIVION_DATA_DIR"]) == run.store.settings.data_dir
    out = _run(rh, env, _prompt("how do I run pytest in the venv"))
    assert out.startswith("GROUNDED MEMORY (local memory store, namespace claude_code, ")
    assert "never the system python" in out
    assert "ok" in _log(env).split()[-1]
    # The same prompt again: the hit was shown in this session already.
    assert _run(rh, env, _prompt("how do I run pytest in the venv")) == ""


def test_end_to_end_ranked_index_rule_rows_and_mcp_fetch(tmp_path, real_store):
    run, folder = real_store
    env = _store_env(
        tmp_path,
        folder,
        NOBLIVION_RECALL_INDEX="1",
        NOBLIVION_RECALL_INDEX_RERANK="1",
        NOBLIVION_RECALL_INDEX_HYGIENE="1",
        NOBLIVION_RECALL_INDEX_RULE_ROWS="1",
    )
    rh = load_hook("recall_hook", "hooktest_recall_store_e2e_index")
    out = _run(rh, env, _prompt("release notes changelog before a deploy"))
    assert "deploy checklist" in out
    assert ":joined" in _log(env)
    ids = rh.load_last_ids(env["NOBLIVION_RECALL_CACHE_DIR"], "s-demo-1")
    mid = ids["deploy checklist"]
    mcp = load_hook_mcp()
    result = mcp.noblivion_recall(environ=env, fetch_id=mid)
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert text.startswith(f"GROUNDED MEMORY {mid}: deploy checklist")
    assert "tag the release" in text


def test_end_to_end_mcp_search_and_include_mined(tmp_path, real_store):
    run, folder = real_store
    with closing(db.connect(run.store.settings.db_path, create=True)) as conn:
        from store_helpers import add_memory

        add_memory(
            conn,
            "mined rollout note",
            "kubectl rollout restart",
            root=ROOT,
            source_type=db.SOURCE_MINED,
        )
    env = _store_env(tmp_path, folder)
    mcp = load_hook_mcp()
    found = mcp.noblivion_recall("pytest venv", 3, env)
    assert found["isError"] is False and "tests use venv" in found["content"][0]["text"]
    plain = mcp.noblivion_recall("kubectl rollout restart", 3, env)
    assert "mined rollout note" not in plain["content"][0]["text"]
    mined = mcp.noblivion_recall("kubectl rollout restart", 3, env, include_mined=True)
    assert mined["isError"] is False
    assert "mined rollout note" in mined["content"][0]["text"]


def test_end_to_end_keyword_store(rh, tmp_path):
    folder = _memory_folder(tmp_path)
    # BM25 needs a pool where a term is rare: with two rows every idf is 0.
    write_memory(folder, "lunch order", "Order the soup on Fridays.", "Soup and bread.")
    write_memory(folder, "plant care", "Water the fern weekly.", "The fern likes shade.")
    service = embedding.EmbeddingService(embedding.EmbeddingSettings(backend="none"))
    run = Running(make_store(tmp_path, embedding_service=service))
    try:
        assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
        env = _store_env(
            tmp_path, folder, NOBLIVION_RECALL_INDEX="1", NOBLIVION_RECALL_INDEX_MIN_SCORE="0.9"
        )
        out = _run(rh, env, _prompt("changelog release deploy"))
    finally:
        run.stop()
    assert "deploy checklist" in out
    assert ":floor_off:keyword" in _log(env)


# -- a hybrid answer over a pool where most rows have no vector (NOBLIVION-49) --------------

NO_VECTOR = "unembeddable"  # the fake embedder fails on a text that holds this word


@pytest.fixture
def backfill_store(tmp_path):
    """50 memory files, and only 10 of them get a vector, as during a first
    backfill. The word "kiwi" is in one file with a vector ("note 44") and in
    one file without ("note 03")."""
    folder = tmp_path / "projects" / ROOT / "memory"
    for n in range(50):
        fruit = "kiwi" if n in (3, 44) else "plum"
        tail = "" if n >= 40 else f" {NO_VECTOR}"
        write_memory(folder, f"note {n:02d}", f"Topic {n:02d}.", f"A {fruit} fact.{tail}")
    service = fake_service(FakeEmbedder(fail_on=NO_VECTOR))
    run = Running(make_store(tmp_path, embedding_service=service))
    assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)

    def vectors() -> int:
        with closing(db.connect(run.store.settings.db_path, create=False)) as conn:
            return conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    assert wait_for(lambda: vectors() == 10)
    assert wait_for(lambda: run.store.embedding.state == embedding.STATE_READY)
    yield run, folder
    run.stop()


def test_hit_path_gets_no_row_that_has_no_vector_and_matches_no_query_word(
    rh, tmp_path, backfill_store
):
    run, folder = backfill_store
    status, body = run.get(f"/api/memories/index?q=kiwi&project=claude_code&top_k=50&root={ROOT}")
    assert status == 200 and body["mode"] == "hybrid"
    unscored = [r["title"] for r in body["results"] if r["score"] is None]
    assert unscored == ["note 03"]
    assert len(body["results"]) == 11  # the 10 rows with a vector, and "note 03"
    # The hook floor keeps a hit with no score. A floor that no fake cosine
    # reaches leaves only those hits, and the only one is the row that matches.
    env = _store_env(tmp_path, folder, NOBLIVION_RECALL_MIN_SCORE="0.99")
    hits = rh.recall("kiwi", rh.K_PROMPT, env, md_only=True)
    assert [(h.title, h.score) for h in hits] == [("note 03", None)]


def test_index_floor_gets_no_row_that_has_no_vector_and_matches_no_query_word(
    rh, tmp_path, backfill_store
):
    run, folder = backfill_store
    env = _store_env(
        tmp_path, folder, NOBLIVION_RECALL_INDEX="1", NOBLIVION_RECALL_INDEX_MIN_SCORE="0.99"
    )
    out = _run(rh, env, _prompt("kiwi"))
    assert "note 03" in out
    assert [n for n in range(50) if f"note {n:02d}" in out] == [3]
    assert ":floor1of11:unscored1" in _log(env)


def test_end_to_end_store_stopped_fails_open(rh, tmp_path, real_store):
    run, folder = real_store
    env = _store_env(tmp_path, folder)
    run.stop()
    assert not (run.store.settings.data_dir / "store.json").exists()
    assert _run(rh, env, _prompt("how do I run pytest in the venv")) == ""
    assert "fail:store_down" in _log(env)


def test_mcp_tool_says_the_store_is_not_running(tmp_path):
    mcp = load_hook_mcp()
    env = hook_env(tmp_path)
    result = mcp.noblivion_recall("pytest venv", 3, env)
    assert result["isError"] is True
    assert result["content"][0]["text"] == "noblivion_recall: memory store not running (store_down)"
    with FakeStore(Path(env["NOBLIVION_DATA_DIR"]), proof="wrong"):
        result = mcp.noblivion_recall(environ=env, fetch_id=5)
    assert result["content"][0]["text"].endswith("memory store not running (foreign_listener)")


def test_mcp_schema_has_include_mined_and_refuses_unknown_keys():
    mcp = load_hook_mcp()
    schema = mcp.TOOL_SCHEMA["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["include_mined"]["type"] == "boolean"
    assert mcp.TOOL_NAME == "noblivion_recall"


def load_hook_mcp():
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "mcp" / "recall_mcp.py"
    name = f"hooktest_recall_mcp_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_proof_helper_in_the_tests_matches_the_launcher():
    assert proof_for("ef" * 32, "ab" * 16) == launcher.health_proof("ef" * 32, "ab" * 16)
    assert os.environ.get("NOBLIVION_STORE_AUTOSTART") in (None, "", "0")


def test_session_env_reads_recall_settings_from_the_config_file(rh, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "recall": {"timeout_s": 1.5, "min_score": 0.4, "index_k": 12},
                "namespace": "demo_space",
            }
        ),
        encoding="utf-8",
    )
    env = rh.session_env({"NOBLIVION_CONFIG": str(config)}, {})
    assert env["NOBLIVION_RECALL_TIMEOUT_S"] == "1.5"
    assert env["NOBLIVION_RECALL_MIN_SCORE"] == "0.4"
    assert env["NOBLIVION_RECALL_INDEX_K"] == "12"
    assert env["NOBLIVION_RECALL_PROJECT"] == "demo_space"
    # An env var wins over the file; NOBLIVION_PROJECT is the doc's name.
    env = rh.session_env(
        {
            "NOBLIVION_CONFIG": str(config),
            "NOBLIVION_RECALL_TIMEOUT_S": "0.7",
            "NOBLIVION_PROJECT": "other_space",
        },
        {},
    )
    assert env["NOBLIVION_RECALL_TIMEOUT_S"] == "0.7"
    assert env["NOBLIVION_RECALL_PROJECT"] == "other_space"
    assert rh.session_env({"NOBLIVION_CONFIG": str(tmp_path / "missing.json")}, {}) == {
        "NOBLIVION_CONFIG": str(tmp_path / "missing.json")
    }


def test_session_start_hook_starts_a_real_store_through_the_launcher(rh, tmp_path):
    """The SessionStart leg runs the real ``noblivion ensure-running``; the
    store it starts passes the hook's listener proof."""
    import shutil
    import signal
    import sys

    from test_store import store_env

    exe = shutil.which("noblivion", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("no noblivion entry point next to the test python")
    env = dict(store_env(tmp_path), NOBLIVION_BIN=exe, NOBLIVION_STORE_AUTOSTART="1")
    sc = rh._STORE
    data = Path(env["NOBLIVION_DATA_DIR"])
    assert sc.main(stdin=io.StringIO("{}"), environ=env) == 0
    try:
        assert wait_for(lambda: sc.read_store_json(data) is not None, 20)
        base, token = sc.connect(env, lambda url, budget: rh.http_get_json(url, "", budget), 2.0)
        assert base.startswith("http://127.0.0.1:") and token == sc.read_token(data)
        # A second start finds the proven store and starts nothing new.
        pid = sc.read_store_json(data)["pid"]
        import subprocess

        again = subprocess.run(
            [exe, "ensure-running", "--json"], env=env, capture_output=True, text=True, timeout=30
        )
        assert json.loads(again.stdout) == {"state": "running"}
    finally:
        info = sc.read_store_json(data)
        if info is not None:
            os.kill(info["pid"], signal.SIGTERM)
            wait_for(lambda: sc.read_store_json(data) is None, 20)
    assert info is not None and info["pid"] == pid
