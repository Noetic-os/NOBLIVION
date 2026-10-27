# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-125: ``recall.shared_roots`` defaults to the home root.

The home root is the root of the Claude Code project folder of the user's
home folder, named by the hooks' own rule. When the key is not set, the store
shares that root with every session. An explicit list (``[]`` too) is used as
given. A session whose own root is the home root sees no change.

Fictional data only. ``HOME`` is a folder in the test's tmp dir.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest

from hookload import load_hook
from noblivion import config, embedding, labels, ranking, rest, store
from recall_helpers import INDEX_FLOOR_OFF, hook_env
from store_helpers import FakeEmbedder, add_memory, fake_vector, mark_installed
from test_hook_recall_store import load_hook_mcp
from test_store import Running, fake_service, wait_for, write_memory

CLAUDE_ENVS = (
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT",
    "XDG_DATA_HOME",
)
OWN_ROOT = "-work-proj-alpha"
THIRD_ROOT = "-work-proj-third"
PROMPT = "how often do we rotate the gateway signing keys"


class WideEmbedder(FakeEmbedder):
    """The fake bag-of-words embedder with 4096 slots instead of 16, so a
    cosine follows the shared words and the floor test has a clear margin."""

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [fake_vector(t, 4096) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return fake_vector(text, 4096)


@pytest.fixture
def hc():
    return load_hook("hook_config", "hooktest_shared_roots_hook_config")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh home folder outside git. No ``NOBLIVION_*`` or Claude Code
    variable is inherited."""
    folder = tmp_path / "home"
    folder.mkdir()
    for name in list(os.environ):
        if name.startswith("NOBLIVION_") or name in CLAUDE_ENVS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(folder))
    return folder


def _env(tmp_path: Path, cfg: dict | None) -> dict[str, str]:
    path = tmp_path / "config.json"
    if cfg is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(cfg), encoding="utf-8")
    return {"NOBLIVION_CONFIG": str(path), "NOBLIVION_DATA_DIR": str(tmp_path / "data")}


def _shared(tmp_path: Path, cfg: dict | None) -> tuple[str, ...]:
    return config.load_store_settings(_env(tmp_path, cfg)).shared_roots


# -- the setting ---------------------------------------------------------------------------


def test_the_home_root_is_the_root_a_home_session_sends(home, hc):
    rh = load_hook("recall_hook", "hooktest_shared_roots_recall_home")
    sent = rh.session_root(rh.session_env({}, {"cwd": str(home)}))
    assert sent == hc.project_slug(home)
    assert config.home_root() == sent


def test_the_default_is_the_home_root_when_the_key_is_not_set(home, hc, tmp_path):
    want = (hc.project_slug(home),)
    assert _shared(tmp_path, None) == want  # no config file
    assert _shared(tmp_path, {"recall": {"timeout_s": 2}}) == want  # file without the key
    assert _shared(tmp_path, {"recall": {"shared_roots": None}}) == want
    assert _shared(tmp_path, {"recall": {"shared_roots": "-work-shared"}}) == want  # bad value


def test_an_explicit_empty_list_turns_the_default_off(home, tmp_path):
    assert _shared(tmp_path, {"recall": {"shared_roots": []}}) == ()
    assert _shared(tmp_path, {"recall.shared_roots": []}) == ()
    assert _shared(tmp_path, {"recall": {"shared_roots": [""]}}) == ()


def test_an_explicit_list_is_used_as_given(home, tmp_path):
    got = _shared(tmp_path, {"recall": {"shared_roots": ["-work-b", "-work-a"]}})
    assert got == ("-work-b", "-work-a")


def test_the_global_folder_root_is_added_once(home, hc, tmp_path):
    root = hc.project_slug(home)
    same = home / ".claude" / "projects" / root / "memory"
    assert _shared(tmp_path, {"global_memory_dir": str(same)}) == (root,)
    other = {"global_memory_dir": str(tmp_path / "rules" / "memory")}
    assert _shared(tmp_path, other) == (root, "rules")


def test_no_hooks_folder_means_no_default(home, tmp_path, monkeypatch):
    monkeypatch.setattr(labels, "hooks_dirs", lambda env=None: [tmp_path / "no-hooks"])
    monkeypatch.setattr(config, "_HOOK_CONFIG", {})
    assert config.home_root() is None
    assert _shared(tmp_path, None) == ()


def test_a_home_session_pool_does_not_change(home, hc, tmp_path):
    from noblivion import db

    root = hc.project_slug(home)
    shared = _shared(tmp_path, None)
    assert shared == (root,)
    assert ranking.RankIndex.pool_key("claude_code", root, shared, False) == (
        ranking.RankIndex.pool_key("claude_code", root, (), False)
    )
    conn = db.open_db(tmp_path / "db" / "noblivion.db", create=True)
    try:
        mine = add_memory(conn, "feedback_keys", "rotate signing keys venv", root=root)
        add_memory(conn, "feedback_other", "third root signing keys venv", root=THIRD_ROOT)
        index = ranking.RankIndex()
        index.refresh(conn)
        before = index.rank("signing keys", project="claude_code", top_k=10, root=root)
        after = index.rank(
            "signing keys", project="claude_code", top_k=10, root=root, shared_roots=shared
        )
        assert [h.row.id for h in after.hits] == [h.row.id for h in before.hits] == [mine]
    finally:
        conn.close()


# -- end to end: a session in another root ---------------------------------------------------


@pytest.fixture
def three_roots(home, hc, tmp_path):
    """A real store (fake embedder) over three memory folders: the session's
    own root, the home root and a third root that nobody shares. The store
    settings come from ``load_store_settings`` with no config file."""
    projects = tmp_path / "projects"
    own = projects / OWN_ROOT / "memory"
    home_folder = projects / hc.project_slug(home) / "memory"
    third = projects / THIRD_ROOT / "memory"
    write_memory(own, "alpha build", "Build alpha with make.", "Run make in the alpha folder.")
    write_memory(
        home_folder,
        "rotate signing keys",
        "Rotate the gateway signing keys every 90 days.",
        "Rotate the gateway signing keys every 90 days and keep two keys valid.",
    )
    write_memory(
        home_folder,
        "lunch rota",
        "Lunch rota: who cooks soup, bread and salad each weekday.",
        "A signing sheet for lunch hangs on a kitchen door, next to a kettle and a fridge.",
    )
    write_memory(
        third,
        "third signing keys",
        "Rotate the gateway signing keys every 30 days.",
        "Rotate the gateway signing keys every 30 days and keep one key valid.",
    )
    loaded = config.load_store_settings(_env(tmp_path, None))
    assert loaded.shared_roots == (hc.project_slug(home),)
    settings = config.Settings(
        data_dir=tmp_path / "data",
        namespace="claude_code",
        memory_dirs=(own, home_folder, third),
        delete_grace_days=14,
        archive_retention_days=90,
    )
    mark_installed(settings.data_dir)
    st = store.Store(
        settings,
        replace(loaded, port=0, idle_exit_s=0, index_interval_s=0),
        embedding_service=fake_service(WideEmbedder()),
        labeller=None,
    )
    run = Running(st)
    assert wait_for(lambda: run.store.index_state == store.INDEX_IDLE)
    assert wait_for(lambda: run.store.embedding.state == embedding.STATE_READY)
    yield run, {"own": own, "home": home_folder, "third": third}
    run.stop()


def _titles(run: Running, path: str, root: str) -> list[str]:
    status, body = run.get(f"{path}?{urlencode({'q': PROMPT, 'root': root})}")
    assert status == 200
    if path.endswith("/index"):
        return [row["title"] for row in body["results"]]
    return [body["results"][0]]


def _prompt_recall(tmp_path: Path, folder: Path, session: str, floor: str) -> str:
    """The prompt hook's output for one new session in ``folder``'s root."""
    rh = load_hook("recall_hook", f"hooktest_shared_roots_recall_{session}")
    env = hook_env(
        tmp_path,
        NOBLIVION_RECALL_CACHE_DIR=str(tmp_path / "cache" / session),
        NOBLIVION_RECALL_MEMORY_DIR=str(folder),
        NOBLIVION_RECALL_MIN_SCORE=floor,
        **INDEX_FLOOR_OFF,
    )
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": PROMPT}
    out = io.StringIO()
    assert rh.main(stdin=io.StringIO(json.dumps(payload)), stdout=out, environ=env) == 0
    return out.getvalue()


def test_another_root_gets_the_home_note_and_not_the_third_root(three_roots, tmp_path):
    run, folders = three_roots
    # The ranked index (the subagent rules and the MCP index read it too).
    titles = _titles(run, rest.INDEX_PATH, OWN_ROOT)
    assert "rotate signing keys" in titles
    assert "third signing keys" not in titles
    # Search (the MCP recall tool and the note-text prompt path).
    text = _titles(run, rest.SEARCH_PATH, OWN_ROOT)[0]
    assert "every 90 days" in text and "every 30 days" not in text
    # The MCP recall tool of a session in the own root.
    env = hook_env(tmp_path, NOBLIVION_RECALL_MEMORY_DIR=str(folders["own"]), **INDEX_FLOOR_OFF)
    result = load_hook_mcp().noblivion_recall(environ=env, query=PROMPT)
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "rotate signing keys" in text and "third signing keys" not in text


def test_the_home_note_passes_the_same_floor_as_any_note(three_roots, tmp_path):
    run, folders = three_roots
    # No floor: both home notes that share a prompt word are shown.
    open_out = _prompt_recall(tmp_path, folders["own"], "s-open", "0")
    assert "every 90 days" in open_out and "kettle" in open_out
    assert "every 30 days" not in open_out
    # A floor: the strong home note passes it, the weak one does not.
    out = _prompt_recall(tmp_path, folders["own"], "s-floor", "0.3")
    assert out.startswith("GROUNDED MEMORY")
    assert "every 90 days" in out
    assert "kettle" not in out
    assert "every 30 days" not in out


def test_a_home_session_gets_its_notes_once(three_roots, tmp_path):
    run, folders = three_roots
    home_root = folders["home"].parent.name
    titles = _titles(run, rest.INDEX_PATH, home_root)
    assert "rotate signing keys" in titles
    assert len(titles) == len(set(titles))
    assert "third signing keys" not in titles and "alpha build" not in titles
    out = _prompt_recall(tmp_path, folders["home"], "s-home", "0")
    assert out.count("every 90 days and keep two keys valid") == 1
    assert "every 30 days" not in out
