# SPDX-License-Identifier: AGPL-3.0-or-later
"""Query delivery preserves identity and fails closed for invalid context."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "learning_context_test", ROOT / "hooks/learning_context.py"
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def test_query_context_passes_exact_scope_query_and_session(monkeypatch):
    urls = []
    envs = []
    context = "- Set fixture_39 to 739. [claim:c1 revision:2 support:e1 source:s1 span:0:26]"

    def get(build, env, timeout):
        urls.append(build("http://127.0.0.1:1"))
        envs.append(env)
        return {"context": context, "reason": None}

    recall = SimpleNamespace(
        store_get=get,
        session_env=lambda env, event: dict(env, _learning_session_id=event["session_id"]),
    )
    monkeypatch.setattr(hook, "load", lambda *args: recall)
    event = dict(
        hook_event_name="UserPromptSubmit", session_id="caller-b", prompt="fixture_39 setting"
    )
    result = hook.run(
        event, {"NOBLIVION_RECALL_PROJECT": "demo", "NOBLIVION_RECALL_ROOT": "root-b"}, codex=True
    )
    assert result["hookSpecificOutput"]["additionalContext"].endswith(context)
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert parse_qs(urlsplit(urls[0]).query) == {
        "project": ["demo"],
        "root": ["root-b"],
        "query": ["fixture_39 setting"],
        "session_id": ["caller-b"],
        "limit_chars": ["6000"],
    }
    assert envs[0]["_learning_session_id"] == "caller-b"


@pytest.mark.parametrize(
    "response",
    [{"context": "forbidden", "reason": "memory use disabled"}, {"context": "x" * 6001}, {}, None],
)
def test_invalid_or_disabled_context_is_not_delivered(monkeypatch, response):
    recall = SimpleNamespace(store_get=lambda *args: response, session_env=lambda env, event: env)
    monkeypatch.setattr(hook, "load", lambda *args: recall)
    assert (
        hook.run(
            dict(hook_event_name="UserPromptSubmit", session_id="caller", prompt="query"),
            {"NOBLIVION_RECALL_ROOT": "root"},
            codex=True,
        )
        == {}
    )


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"hook_event_name": "SessionStart", "session_id": "a"},
        {"hook_event_name": "UserPromptSubmit", "session_id": "a", "prompt": "x" * 8001},
    ],
)
def test_only_bounded_prompt_events_request_context(monkeypatch, event):
    monkeypatch.setattr(hook, "load", lambda *args: pytest.fail("must not load a store client"))
    assert hook.run(event, {"NOBLIVION_RECALL_ROOT": "root"}, codex=True) == {}


def test_exact_8000_character_query_is_delivered_without_truncation(monkeypatch):
    requested = []

    def get(build, *args):
        requested.append(parse_qs(urlsplit(build("http://127.0.0.1:1")).query)["query"][0])
        return {"context": "- eligible fact", "reason": None}

    recall = SimpleNamespace(store_get=get, session_env=lambda env, event: env)
    monkeypatch.setattr(hook, "load", lambda *args: recall)
    query = "bound" + "x" * 7995
    result = hook.run(
        dict(hook_event_name="UserPromptSubmit", session_id="caller", prompt=query),
        {"NOBLIVION_RECALL_ROOT": "root"},
        codex=True,
    )
    assert requested == [query] and result["hookSpecificOutput"]["additionalContext"]
