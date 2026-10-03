# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deterministic subject labeller and the label index (``hooks/memory_labels.py``).

What these tests hold:
1. THE LABELLER reads only the text it gets: ticket keys and ``<prefix>-*``
   service names for the prefixes in the config (``labels.ticket_prefixes``,
   ``labels.service_prefixes``; default none), file base names and named
   tools. It makes no host labels. Generic file names, memory file names, the
   memory's own name and attribute access with a file-like suffix
   (``resp.json``, ``sys.path``) are not labels.
2. NAME AND DESCRIPTION are read the way the recall corpus reader reads them,
   so a name in the index is the name the shown-set stores.
3. THE INDEX keeps per label its kind, its document frequency and its
   postings; a memory with no label keeps only its id.
4. THE MATCH RULE: a shared label held by more than the cutoff (8) memories
   never matches alone; a second shared label lets it match only when the
   memories holding all shared labels are at most the cutoff. A tool or a
   document name (``.md``) never matches alone. Rank: the sum of inverse
   document frequencies; ties go to more labels, then the newer file.
"""

from __future__ import annotations

import json
import os

import pytest

from hookload import load_hook

corpus = load_hook("corpus", "corpus_t_memory_labels")

CONFIG = {"labels": {"ticket_prefixes": ["proj"], "service_prefixes": ["app"]}}


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
def L(hook_env):
    """The labeller loaded with the ticket prefix ``proj`` and the service prefix ``app``."""
    hook_env["config"].write_text(json.dumps(CONFIG))
    return load_hook("memory_labels", "memory_labels_t_memory_labels")


@pytest.fixture
def L_default(hook_env):
    """The labeller loaded with no config file: no prefixes."""
    return load_hook("memory_labels", "memory_labels_t_memory_labels_default")


FIXTURE = """---
name: feedback_fixture_memory
description: "On alpha-1 the app-db container needs PROJ-4057 first"
rule: Reload Prometheus on the alpha host after autosync lands prometheus.yml.
apply: "ssh user@192.0.2.191 'curl -X POST http://127.0.0.1:9090/-/reload'"
---
See deploy/prometheus/prometheus.yml and tools/app-merge. Beta (198.51.100.98)
runs gitleaks and /usr/bin/psql. Ticket proj-4057 again. Not labels: README.md,
feedback_other_memory.md, feedback_fixture_memory.md, resp.json, sys.path, console.log.
"""


# 1. the labeller ------------------------------------------------------------
def test_fixture_labels_exact_kinds_and_order(L):
    got = L.memory_labels(FIXTURE, "feedback_fixture_memory")
    assert list(got.items()) == [
        ("app-db", "service"),
        ("PROJ-4057", "key"),
        ("prometheus", "tool"),
        ("prometheus.yml", "file"),
        ("app-merge", "service"),
        ("gitleaks", "tool"),
        ("psql", "tool"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "ssh alpha-1 uptime",
        "curl http://192.0.2.15:8894/health",
        "ssh user@198.51.100.191 ls",
        "on Alpha.",
        "https://beta.example.net",
        "ping 203.0.113.29",
        "the NAS at 192.0.2.85",
    ],
)
def test_no_host_labels_at_all(L, text):
    assert L.labels(text) == {}


def test_keys_and_services(L):
    got = L.labels(
        "feat/PROJ-4451-labels proj-4451 XPROJ-9 PROJ-1234567 "
        "app-devops-2 tools/app-review-request appdemon-github-runner"
    )
    assert got == {
        "PROJ-4451": "key",
        "app-devops-2": "service",
        "app-review-request": "service",
    }


def test_no_prefix_configured_gives_no_key_or_service_labels(L_default):
    assert L_default.TICKET_PREFIXES == () and L_default.SERVICE_PREFIXES == ()
    got = L_default.labels("PROJ-4451 app-devops-2 tools/app-review-request x_y.py")
    assert got == {"x_y.py": "file"}


def test_files_generic_memory_and_attribute_names_are_not_labels(L):
    got = L.labels(
        "cat README.md x.py feedback_a_b.md MEMORY.md resp.json ev.ts sys.path "
        "process.env console.log self.service"
    )
    assert got == {}


def test_files_from_paths_and_name_shapes(L):
    got = L.labels(
        "~/.claude/settings.json systemctl status user@1000.service "
        "deploy/host/app-entity-sweep.timer merge_gate_policy.json run.sh "
        "scripts/run_ci_v2_dispatcher.sh, docs/x/RUNBOOK.md."
    )
    assert [k for k, v in got.items() if v == "file"] == [
        "settings.json",
        "user@1000.service",
        "app-entity-sweep.timer",
        "merge_gate_policy.json",
        "run_ci_v2_dispatcher.sh",
        "runbook.md",
    ]


def test_own_file_name_is_not_a_label_of_the_memory(L):
    text = "see feedback_x_y.md and project_x_y.md and other.py"
    assert "other.py" in L.memory_labels(text, "project_x_y")
    assert "project_x_y.md" not in L.memory_labels(text, "project_x_y")


def test_tools_named_programs_only(L):
    got = L.labels(
        "git status; ls; python x; gitleaks detect; /usr/lib/postgresql/16/bin/pg_dump db; "
        "nvidia-smi; ollama-proxy"
    )
    assert got == {"gitleaks": "tool", "pg_dump": "tool", "nvidia-smi": "tool"}


def test_non_text_gives_no_labels(L):
    assert L.labels(None) == {}  # type: ignore[arg-type]
    assert L.labels("") == {}


def test_source_values(L):
    assert L.source_of("host") == "rule:host"
    assert L.source_of("file") == "rule:file"


# 2. name and description ----------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '---\nname: Quoted name\ndescription: "a: b"\nrule: r\n---\nbody line\n',
        "---\nname: 'single'\nmetadata:\n  type: feedback\n---\n\nbody\n",
        "---\ndescription: no name here\n---\nbody\n",
        "no front matter at all\n",
        "---\nname: never closed\nbody\n",
        "---\nname:\ndescription: empty name\n---\nbody\n",
    ],
)
def test_name_and_description_match_the_corpus_reader(L, text):
    meta, body = corpus.parse_frontmatter(text)
    name, desc, body2 = L.name_and_description(text, "stem_x")
    assert name == corpus.memory_name("stem_x.md", meta)
    want_desc = meta.get("description") if isinstance(meta.get("description"), str) else ""
    assert desc == (want_desc or "").strip()
    assert body2 == body


def test_short_text_prefers_the_description_and_cuts(L):
    assert L.short_text("  a  b ", "x") == "a b"
    assert L.short_text("", "# Title\n\nfirst line here\n") == "Title"
    long = "w " * 200
    assert len(L.short_text(long, "")) <= L.TEXT_CHARS


# 3. the index ---------------------------------------------------------------
def _doc(i, labels, name=None, mtime=0):
    return {
        "id": f"m{i}",
        "name": name or f"m{i}",
        "labels": labels,
        "mtime": mtime,
        "text": f"text {i}",
    }


def test_index_shape(L):
    idx = L.build_index(
        [
            _doc(0, {"a.py": "file"}, name="Other Name"),
            _doc(1, {}),
            _doc(2, {"a.py": "file", "app-db": "service"}),
        ]
    )
    assert L.index_ok(idx)
    assert idx["n"] == 3 and idx["labels"] == ["a.py", "app-db"]
    assert idx["df"] == [2, 1] and idx["post"] == [[0, 2], [2]]
    assert idx["kinds"] == ["file", "service"]
    assert idx["docs"][0] == {"id": "m0", "m": 0, "t": "text 0", "name": "Other Name"}
    assert idx["docs"][1] == {"id": "m1"}  # no label: never matches, id only
    assert "name" not in idx["docs"][2]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"labels": 3},
        {"labels": ["a"], "df": [], "post": [[0]], "docs": [{}], "n": 1},
    ],
)
def test_index_ok_rejects_broken_shapes(L, bad):
    assert not L.index_ok(bad)
    assert L.match(["a"], bad) == []


# 4. the match rule -------------------------------------------------------------
def _index(L, spec):
    """``spec``: list of label dicts, one per memory."""
    return L.build_index([_doc(i, labels, mtime=i) for i, labels in enumerate(spec)])


def test_df_cutoff_single_label(L):
    at = _index(L, [{"at_cut.py": "file"}] * 8 + [{"over.py": "file"}] * 9)
    assert len(L.match(["at_cut.py"], at)) == 8  # df 8: specific
    assert L.match(["over.py"], at) == []  # df 9: never alone


def test_a_second_label_lets_a_common_label_match_only_when_few_hold_both(L):
    spec = [{"app-db": "service", "crontab": "tool"}] * 12 + [{"app-db": "service"}] * 3
    spec += [{"app-db": "service", "app-ssh": "service"}] * 2 + [{"app-ssh": "service"}] * 8
    idx = _index(L, spec)
    # app-db (17) and crontab (12) are common; 12 memories hold both: no match.
    assert L.match(["app-db", "crontab"], idx) == []
    # app-ssh (10) is common too, but only 2 memories hold it with app-db.
    got = L.match(["app-db", "app-ssh"], idx)
    assert sorted(m.id for m in got) == ["m15", "m16"]
    assert all(m.labels == ["app-db", "app-ssh"] for m in got)


def test_weak_labels_never_match_alone(L):
    idx = _index(
        L, [{"gitleaks": "tool"}, {"plan.md": "file"}, {"gitleaks": "tool", "special.py": "file"}]
    )
    assert L.match(["gitleaks"], idx) == []
    assert L.match(["plan.md"], idx) == []
    assert [m.id for m in L.match(["gitleaks", "special.py"], idx)] == ["m2"]
    assert L.weak_label("x.md", "file") and L.weak_label("psql", "tool")
    assert not L.weak_label("x.py", "file") and not L.weak_label("app-db", "service")


def test_rank_by_idf_sum_then_label_count_then_newer_file(L):
    spec = [
        {"rare.py": "file"},  # m0: one rare label
        {"rare.py": "file", "half.py": "file"},  # m1: rare + half: best
        {"half.py": "file"},
        {"half.py": "file"},
        {"other.py": "file"},
    ]
    got = L.match(["rare.py", "half.py"], _index(L, spec))
    assert [m.id for m in got][:2] == ["m1", "m0"]
    assert got[0].score > got[1].score
    # a tie on score: the newer file (larger mtime) first
    tie = L.build_index([_doc(0, {"t.py": "file"}, mtime=5), _doc(1, {"t.py": "file"}, mtime=9)])
    assert [m.id for m in L.match(["t.py"], tie)] == ["m1", "m0"]


def test_unknown_query_labels_match_nothing(L):
    assert L.match(["nothing.py", "PROJ-1"], _index(L, [{"a.py": "file"}])) == []
