# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedup sweep (design doc 0001, section 10). Fictional memories, a fake
judge, and no network: every test that could reach OpenRouter replaces the
opener and asserts on what it got."""

from __future__ import annotations

import io
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from noblivion import db, dedup, embedding, indexer

MODEL_ID = f"fastembed:{embedding.DEFAULT_MODEL}"
ROOT = "-fict-garden"

SURVIVOR = """\
---
name: Water the fern before noon
description: The fern in the fictional greenhouse wilts when watered late
type: reference
---
Water the fern before noon. Late water stays on the leaves overnight.
"""
LOSER = """\
---
name: Fern watering time
description: Water the greenhouse fern in the morning
type: reference
---
The fern wants water in the morning, never in the evening.
"""
OTHER = """\
---
name: Compost turning
description: Turn the compost heap every week
type: reference
---
Turn the heap weekly. See [the fern note](reference_fern_time.md).
"""
INDEX = """\
# Memory index
- [Fern before noon](reference_fern_noon.md)
- [Fern time](reference_fern_time.md)
- [Compost](reference_compost.md)
"""
FILES = {
    "reference_fern_noon.md": SURVIVOR,
    "reference_fern_time.md": LOSER,
    "reference_compost.md": OTHER,
    "MEMORY.md": INDEX,
}
VECTORS = {
    "reference_fern_noon.md": [1.0, 0.0, 0.0, 0.0],
    "reference_fern_time.md": [0.95, 0.31, 0.0, 0.0],  # cosine about 0.95
    "reference_compost.md": [0.0, 0.0, 1.0, 0.0],
}


class FakeJudge:
    model = "fake/judge-model"

    def __init__(self, answer: dict | None = None, exc: Exception | None = None) -> None:
        self.answer = answer or {"verdict": "MERGE", "survivor": "A", "reason": "same lesson"}
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    def judge(self, system: str, user: str) -> dedup.Verdict:
        self.calls.append((system, user))
        if self.exc is not None:
            raise self.exc
        return dedup.parse_verdict(json.dumps(self.answer))


@pytest.fixture
def no_network(monkeypatch):
    """Record every attempt to open a URL or a socket, and fail it."""
    calls: list[object] = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs)
        raise AssertionError("network call in a test")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return calls


@pytest.fixture
def garden(tmp_path, no_network):
    """A fictional memory folder, indexed, with vectors, files 1 hour old."""
    folder = tmp_path / "projects" / ROOT / "memory"
    folder.mkdir(parents=True)
    old = time.time() - 3600
    for name, text in FILES.items():
        path = folder / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (old, old))
    data = tmp_path / "data"
    env = {
        "NOBLIVION_DATA_DIR": str(data),
        "NOBLIVION_MEMORY_DIRS": str(folder),
        "NOBLIVION_DEDUP_MODEL": FakeJudge.model,
        dedup.KEY_ENV: "test-placeholder-not-a-key",
    }
    conn = db.open_db(data / "noblivion.db", create=True)
    indexer.scan(conn, [folder])
    ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM memories")}
    with db.write_tx(conn):
        for name, vector in VECTORS.items():
            conn.execute(
                "INSERT INTO vectors (memory_id, model, dim, content_hash, blob, rev) "
                "VALUES (?, ?, ?, 'h', ?, 1)",
                (ids[name], MODEL_ID, len(vector), embedding.vector_to_blob(vector)),
            )
    yield {"folder": folder, "data": data, "env": env, "conn": conn, "ids": ids}
    conn.close()


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def run(g, argv, *, judge=None, stdin="", factory=None):
    out = io.StringIO()
    if factory is None and judge is not None:

        def factory(_ds, _key):
            return judge

    code = dedup.main(
        argv, env=g["env"], stdin=io.StringIO(stdin), stdout=out, judge_factory=factory
    )
    return code, out.getvalue()


def grant(g):
    dedup.grant_consent(g["conn"], FakeJudge.model)


def plan_files(g) -> list[Path]:
    return sorted((g["data"] / "dedup").glob("plan-*.json"))


def run_id_of(g) -> str:
    (path,) = plan_files(g)
    return json.loads(path.read_text())["run_id"]


# -- pair selection -------------------------------------------------------------


def _row(mid, path, vector, root=ROOT, category="reference"):
    return (mid, root, path, category, vector)


def test_pair_selection_threshold_and_filters():
    rows = [
        _row(1, "reference_a.md", [1.0, 0.0]),
        _row(2, "reference_b.md", [0.83, 0.5577634]),  # cosine 0.83
        _row(3, "reference_c.md", [0.81, -0.5864299]),  # cosine 0.81 to a
        _row(4, "MEMORY.md", [1.0, 0.0], category="index"),
        _row(5, "topic_x.md", [1.0, 0.0], category="topic"),
        _row(6, "feedback_a.md", [1.0, 0.0], category="feedback"),
        _row(7, "reference_d.md", [1.0, 0.0], root="-other-root"),
    ]
    pairs = dedup.select_pairs(rows, min_cosine=0.82)
    assert [(c.path_a, c.path_b) for c in pairs] == [("reference_a.md", "reference_b.md")]
    assert pairs[0].cosine == pytest.approx(0.83, abs=1e-4)
    assert dedup.select_pairs(rows, min_cosine=0.84) == []
    vetoed = dedup.select_pairs(
        rows, min_cosine=0.82, vetoes={(ROOT, "reference_a.md", "reference_b.md")}
    )
    assert vetoed == []


def test_recent_files_are_not_candidates(garden):
    for p in garden["folder"].iterdir():
        os.utime(p)  # now
    code, out = run(garden, ["pairs"])
    assert code == 0 and "0 candidate pairs" in out


def test_pairs_lists_candidates_without_consent(garden, no_network):
    code, out = run(garden, ["pairs"])
    assert code == 0
    assert "reference_fern_noon.md <> reference_fern_time.md" in out
    assert "1 candidate pairs" in out
    assert no_network == []


# -- consent and refusal --------------------------------------------------------


@pytest.mark.parametrize("answer", ["", "no\n", "y\n"])
def test_plan_without_consent_sends_nothing(garden, no_network, answer):
    before = snapshot(garden["folder"])
    # The real OpenRouter judge, so a missing consent check would reach urlopen.
    code, out = run(garden, ["plan"], stdin=answer, factory=dedup.openrouter_judge)
    assert code == dedup.EXIT_REFUSED
    assert "NOBLIVION dedup consent" in out and "nothing was sent" in out
    assert no_network == []
    assert plan_files(garden) == []
    assert snapshot(garden["folder"]) == before
    assert dedup.read_consent(garden["conn"]) is None


@pytest.mark.parametrize(
    ("drop", "message"),
    [("NOBLIVION_DEDUP_MODEL", "no judge model is set"), (dedup.KEY_ENV, "is not set")],
)
def test_plan_refuses_without_model_or_key(garden, no_network, drop, message):
    grant(garden)
    del garden["env"][drop]
    judge = FakeJudge()
    code, out = run(garden, ["plan"], judge=judge)
    assert code == dedup.EXIT_REFUSED and message in out
    assert judge.calls == [] and no_network == []


def test_judge_off_refuses(garden):
    grant(garden)
    garden["env"]["NOBLIVION_DEDUP_JUDGE"] = "off"
    judge = FakeJudge()
    code, out = run(garden, ["plan"], judge=judge)
    assert code == dedup.EXIT_REFUSED and judge.calls == []


def test_consent_is_per_model(garden):
    dedup.grant_consent(garden["conn"], "fake/other-model")
    assert not dedup.has_consent(garden["conn"], FakeJudge.model)
    judge = FakeJudge()
    code, _ = run(garden, ["plan"], judge=judge, stdin="no\n")
    assert code == dedup.EXIT_REFUSED and judge.calls == []


def test_yes_records_consent_and_plans(garden):
    judge = FakeJudge()
    code, out = run(garden, ["plan"], judge=judge, stdin="yes\nyes\n")
    assert code == 0, out
    assert dedup.has_consent(garden["conn"], FakeJudge.model)
    assert len(judge.calls) == 1 and len(plan_files(garden)) == 1
    assert "1 judge calls" in out


def test_consent_command_and_revoke(garden):
    code, _ = run(garden, ["consent"], stdin="yes\n")
    assert code == 0 and dedup.has_consent(garden["conn"], FakeJudge.model)
    code, _ = run(garden, ["consent", "--revoke"])
    assert code == 0 and dedup.read_consent(garden["conn"]) is None


# -- plan and dry run -----------------------------------------------------------


def test_dry_run_makes_no_network_call_and_changes_nothing(garden, no_network):
    """A dry run builds no judge: the pairs and the cost estimate only. The
    network is patched to raise, and no consent, model or key is needed."""
    before = snapshot(garden["folder"])
    del garden["env"][dedup.KEY_ENV]

    def factory(_ds, _key):
        raise AssertionError("a dry run must not build a judge")

    code, out = run(garden, ["plan", "--dry-run"], factory=factory)
    assert code == 0, out
    assert "reference_fern_time.md" in out and "1 judge calls" in out
    assert "dry run; nothing was sent" in out and "MERGE" not in out
    assert no_network == []
    assert plan_files(garden) == []
    assert snapshot(garden["folder"]) == before
    assert dedup.read_consent(garden["conn"]) is None
    status = json.loads((garden["data"] / "cache" / "dedup-last-run.json").read_text())
    assert status["mode"] == "dry-run" and status["pairs"] == 1


@pytest.mark.parametrize("answer", ["", "no\n", "y\n"])
def test_plan_asks_after_the_estimate_and_sends_nothing_without_yes(garden, answer):
    grant(garden)
    judge = FakeJudge()
    code, out = run(garden, ["plan"], judge=judge, stdin=answer)
    assert code == dedup.EXIT_REFUSED
    assert "1 judge calls" in out and "type yes to send 1 pairs" in out
    assert "not confirmed; nothing was sent" in out
    assert judge.calls == [] and plan_files(garden) == []


def test_plan_sends_after_yes_and_writes_the_status(garden):
    grant(garden)
    judge = FakeJudge()
    code, out = run(garden, ["plan"], judge=judge, stdin="yes\n")
    assert code == 0, out
    assert len(judge.calls) == 1 and len(plan_files(garden)) == 1
    status = json.loads((garden["data"] / "cache" / "dedup-last-run.json").read_text())
    assert status["mode"] == "plan" and status["proposals"] == 1
    assert status["run_id"] == run_id_of(garden) and status["outcome"] == "ok"


def test_the_status_file_follows_apply_and_undo(garden, tmp_path):
    status = tmp_path / "status.json"
    garden["env"][dedup.STATUS_FILE_ENV] = str(status)
    run_id, _ = _plan_and_apply(garden)
    doc = json.loads(status.read_text())
    assert doc["mode"] == "apply" and doc["merged"] == 1 and doc["run_id"] == run_id
    assert "failed" not in doc
    assert run(garden, ["undo", run_id])[0] == 0
    doc = json.loads(status.read_text())
    assert doc["mode"] == "undo" and doc["merged"] == 0


def test_consent_text_says_the_front_matter_is_sent():
    text = dedup.consent_text("m/x")
    assert "(version 2)" in text
    assert "name and description fields are sent" in " ".join(text.split())


def test_a_consent_of_the_old_text_version_asks_again(garden):
    dedup.grant_consent(garden["conn"], FakeJudge.model)
    record = dedup.read_consent(garden["conn"])
    record["version"] = 1
    with db.write_tx(garden["conn"]):
        db.set_meta(garden["conn"], dedup.CONSENT_KEY, json.dumps(record))
    assert not dedup.has_consent(garden["conn"], FakeJudge.model)


def test_plan_sends_names_as_a_and_b_and_scrubbed_text(garden):
    grant(garden)
    loser = garden["folder"] / "reference_fern_time.md"
    loser.write_text(LOSER + f"Notes in {Path.home()}/garden, host 10.1.2.3\n")
    os.utime(loser, (time.time() - 3600,) * 2)
    judge = FakeJudge()
    assert run(garden, ["plan", "--yes"], judge=judge)[0] == 0
    system, user = judge.calls[0]
    assert system.startswith("You are a judge")
    assert "File A (kind: reference)" in user and "File B (kind: reference)" in user
    assert "reference_fern" not in user  # file names are not sent
    assert str(Path.home()) not in user and "10.1.2.3" not in user
    assert "~/garden" in user and "<ip>" in user


def test_too_long_file_is_unsure_and_not_sent(garden):
    grant(garden)
    loser = garden["folder"] / "reference_fern_time.md"
    loser.write_text(LOSER + "x" * (dedup.MAX_FILE_CHARS + 1))
    os.utime(loser, (time.time() - 3600,) * 2)
    judge = FakeJudge()
    assert run(garden, ["plan", "--yes"], judge=judge)[0] == 0
    assert judge.calls == []
    (pair,) = json.loads(plan_files(garden)[0].read_text())["pairs"]
    assert pair["verdict"] == "UNSURE" and pair["sent"] is False


def test_max_pairs_caps_judge_calls(garden):
    grant(garden)
    judge = FakeJudge()
    code, out = run(garden, ["plan", "--max-pairs", "0"], judge=judge)
    assert code == 0 and judge.calls == [] and "judging the best 0" in out


# -- judge errors ---------------------------------------------------------------


@pytest.mark.parametrize(
    "judge",
    [
        FakeJudge(exc=RuntimeError("boom")),
        FakeJudge(answer={"verdict": "DELETE EVERYTHING", "survivor": "A"}),
    ],
)
def test_judge_error_leaves_files_untouched(garden, judge):
    grant(garden)
    before = snapshot(garden["folder"])
    assert run(garden, ["plan", "--yes"], judge=judge)[0] == 0
    (pair,) = json.loads(plan_files(garden)[0].read_text())["pairs"]
    assert pair["verdict"] == "UNSURE"
    code, out = run(garden, ["apply", run_id_of(garden)])
    assert code == 0 and "nothing changed" in out
    assert snapshot(garden["folder"]) == before


def test_openrouter_judge_network_error_is_unsure(garden):
    def opener(request, timeout):
        raise urllib.error.URLError("unreachable")

    grant(garden)
    before = snapshot(garden["folder"])

    def factory(ds, key):
        return dedup.OpenRouterJudge(ds.model, key, opener=opener, min_interval_s=0)

    assert run(garden, ["plan", "--yes"], factory=factory)[0] == 0
    (pair,) = json.loads(plan_files(garden)[0].read_text())["pairs"]
    assert pair["verdict"] == "UNSURE" and pair["error"] == "URLError"
    assert snapshot(garden["folder"]) == before


@pytest.mark.parametrize(
    ("content", "verdict", "survivor"),
    [
        ('{"verdict": "MERGE", "survivor": "B", "reason": "r"}', "MERGE", "B"),
        (
            '```json\n{"verdict": "related", "survivor": "none", "reason": "r"}\n```',
            "RELATED",
            "none",
        ),
        ("not json", "UNSURE", "none"),
        ('{"verdict": "MERGE", "survivor": "C"}', "UNSURE", "none"),
        ("[1, 2]", "UNSURE", "none"),
        (None, "UNSURE", "none"),
    ],
)
def test_parse_verdict(content, verdict, survivor):
    v = dedup.parse_verdict(content)
    assert (v.verdict, v.survivor) == (verdict, survivor)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_openrouter_request_shape_rate_limit_and_retry():
    seen: list[urllib.request.Request] = []
    sleeps: list[float] = []
    answer = {
        "choices": [{"message": {"content": '{"verdict": "RELATED", "survivor": "none"}'}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9, "cost": 0.0002},
    }

    def opener(request, timeout):
        seen.append(request)
        if len(seen) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "slow", {"Retry-After": "2"}, None)
        return _Response(json.dumps(answer).encode())

    judge = dedup.OpenRouterJudge(
        "fake/judge-model", "test-placeholder-not-a-key", opener=opener, sleep=sleeps.append,
        min_interval_s=0.5, clock=lambda: 100.0,
    )  # fmt: skip
    v = judge.judge("sys", "user")
    assert (v.verdict, v.prompt_tokens, v.completion_tokens, v.cost) == ("RELATED", 120, 9, 0.0002)
    assert sleeps == [2.0, 0.5]  # Retry-After, then the minimum interval
    body = json.loads(seen[-1].data)
    assert seen[-1].full_url == dedup.OPENROUTER_CHAT_URL
    assert seen[-1].get_header("Authorization") == "Bearer test-placeholder-not-a-key"
    assert body["provider"] == {"data_collection": "deny"}
    assert body["temperature"] == 0 and body["model"] == "fake/judge-model"
    assert "placeholder" not in repr(judge)


def test_openrouter_judge_needs_model_and_key():
    with pytest.raises(dedup.DedupError):
        dedup.OpenRouterJudge("", "k")
    with pytest.raises(dedup.DedupError):
        dedup.OpenRouterJudge("m", "")


def test_prompt_is_checked_by_sha256(tmp_path):
    prompt = dedup.load_prompt()
    assert prompt.sha256 == dedup.PROMPT_SHA256 and "{text_a}" in prompt.user_template
    bad = tmp_path / "p.txt"
    bad.write_bytes(dedup.PROMPT_FILE.read_bytes() + b" ")
    with pytest.raises(dedup.DedupError, match="sha256"):
        dedup.load_prompt(bad)
    rendered = dedup.render_user(prompt, "reference", "{text_b} {kind}", "B text")
    assert "{text_b} {kind}" in rendered  # memory text is never a placeholder


# -- apply, archive, undo -------------------------------------------------------


def _plan_and_apply(g, answer=None):
    grant(g)
    assert run(g, ["plan", "--yes"], judge=FakeJudge(answer))[0] == 0
    run_id = run_id_of(g)
    code, out = run(g, ["apply", run_id])
    assert code == 0, out
    return run_id, out


def test_merge_archives_the_loser_and_undo_restores_exactly(garden):
    folder, conn, ids = garden["folder"], garden["conn"], garden["ids"]
    before = snapshot(folder)
    run_id, out = _plan_and_apply(garden)
    assert "merged reference_fern_time.md into reference_fern_noon.md" in out

    merged = (folder / "reference_fern_noon.md").read_text()
    assert merged.startswith(SURVIVOR.rstrip("\n"))
    assert "Merged from reference_fern_time on" in merged
    assert "> Description: Water the greenhouse fern in the morning" in merged
    assert "never in the evening" in merged
    assert not (folder / "reference_fern_time.md").exists()
    archived = folder / ".archive" / "dedup" / run_id / "reference_fern_time.md"
    assert archived.read_text() == LOSER
    assert "(reference_fern_noon.md)" in (folder / "reference_compost.md").read_text()
    index = (folder / "MEMORY.md").read_text()
    assert "Fern time" not in index and "Fern before noon" in index

    run_dir = garden["data"] / "dedup" / run_id
    assert (run_dir / "undo.sh").stat().st_mode & 0o777 == 0o700
    assert (run_dir / "step-001.tar.gz").is_file()
    phases = [r["phase"] for r in dedup.read_wal(run_dir)]
    assert phases == ["begin", "done"]
    action = conn.execute("SELECT status, kept_id, archived_id FROM dedup_actions").fetchone()
    assert tuple(action) == (
        "done",
        ids["reference_fern_noon.md"],
        ids["reference_fern_time.md"],
    )

    # The indexer picks the change up: the survivor keeps its id, the loser
    # row stays archived (not deleted), so both keep their trust history.
    indexer.scan(conn, [folder])
    row = {
        r["path"]: r for r in conn.execute("SELECT id, path, archived_at, deleted_at FROM memories")
    }
    assert row["reference_fern_noon.md"]["id"] == ids["reference_fern_noon.md"]
    assert row["reference_fern_time.md"]["archived_at"] is not None
    assert row["reference_fern_time.md"]["deleted_at"] is None

    code, out = run(garden, ["undo", run_id])
    assert code == 0, out
    assert snapshot(folder) == before  # byte for byte, no archive folder left
    loser = conn.execute(
        "SELECT archived_at FROM memories WHERE id = ?", (ids["reference_fern_time.md"],)
    ).fetchone()
    assert loser["archived_at"] is None
    assert conn.execute("SELECT status FROM dedup_actions").fetchone()[0] == "undone"
    veto = conn.execute("SELECT root, path_a, path_b FROM dedup_vetoes").fetchone()
    assert tuple(veto) == (ROOT, "reference_fern_noon.md", "reference_fern_time.md")
    code, out = run(garden, ["pairs"])
    assert "0 candidate pairs" in out  # an undone pair is never proposed again


def test_survivor_b_and_merge_cap(garden):
    folder = garden["folder"]
    _plan_and_apply(garden, {"verdict": "MERGE", "survivor": "B", "reason": "r"})
    assert (folder / "reference_fern_time.md").is_file()
    assert not (folder / "reference_fern_noon.md").exists()


def test_apply_skips_a_file_changed_since_the_plan(garden):
    grant(garden)
    assert run(garden, ["plan", "--yes"], judge=FakeJudge())[0] == 0
    (garden["folder"] / "reference_fern_time.md").write_text(LOSER + "edit\n")
    before = snapshot(garden["folder"])
    code, out = run(garden, ["apply", run_id_of(garden)])
    assert code == 0 and "changed since the plan" in out
    assert snapshot(garden["folder"]) == before


def test_only_merge_acts(garden):
    before = snapshot(garden["folder"])
    _plan_and_apply(garden, {"verdict": "SUPERSEDE", "survivor": "A", "reason": "r"})
    assert snapshot(garden["folder"]) == before


def test_undo_stops_when_a_file_changed(garden):
    run_id, _ = _plan_and_apply(garden)
    survivor = garden["folder"] / "reference_fern_noon.md"
    survivor.write_text(survivor.read_text() + "a later edit\n")
    code, out = run(garden, ["undo", run_id])
    assert code == dedup.EXIT_REFUSED
    assert "reference_fern_noon.md changed since the merge" in out
    assert not (garden["folder"] / "reference_fern_time.md").exists()


def test_failed_step_rolls_back_and_sets_the_latch(garden, monkeypatch):
    folder, conn = garden["folder"], garden["conn"]
    before = snapshot(folder)
    grant(garden)
    assert run(garden, ["plan", "--yes"], judge=FakeJudge())[0] == 0
    run_id = run_id_of(garden)
    real_replace = os.replace

    def failing_replace(src, dst):
        if ".archive" in str(dst):
            raise OSError("disk full (fictional)")
        return real_replace(src, dst)

    monkeypatch.setattr(dedup.os, "replace", failing_replace)
    code, out = run(garden, ["apply", run_id])
    monkeypatch.setattr(dedup.os, "replace", real_replace)
    assert code == dedup.EXIT_LATCH and "step 1 failed" in out
    assert snapshot(folder) == before
    status = json.loads((garden["data"] / "cache" / "dedup-last-run.json").read_text())
    assert status["failed"] == run_id and status["outcome"] == "refused"
    assert conn.execute("SELECT status FROM dedup_actions").fetchone()[0] == "failed"
    assert (
        conn.execute("SELECT count(*) FROM memories WHERE archived_at IS NOT NULL").fetchone()[0]
        == 0
    )
    code, out = run(garden, ["apply", run_id])
    assert code == dedup.EXIT_LATCH and "latch" in out
    assert run(garden, ["clear-latch"])[0] == 0
    code, out = run(garden, ["apply", run_id])
    assert code == 0 and "merged" in out


def test_pending_action_from_a_crash_is_rolled_back(garden, monkeypatch):
    folder, conn = garden["folder"], garden["conn"]
    before = snapshot(folder)
    grant(garden)
    assert run(garden, ["plan", "--yes"], judge=FakeJudge())[0] == 0
    run_id = run_id_of(garden)

    class Crash(BaseException):
        pass

    real_replace = os.replace

    def crash(src, dst):
        if ".archive" in str(dst):
            raise Crash
        return real_replace(src, dst)

    monkeypatch.setattr(dedup.os, "replace", crash)
    with pytest.raises(Crash):
        run(garden, ["apply", run_id])
    monkeypatch.setattr(dedup.os, "replace", real_replace)
    assert conn.execute("SELECT status FROM dedup_actions").fetchone()[0] == "pending"
    assert snapshot(folder) != before  # the survivor was written
    code, out = run(garden, ["undo", run_id])
    assert code == 0 and "rolled back an unfinished step" in out
    assert snapshot(folder) == before
    assert conn.execute("SELECT status FROM dedup_actions").fetchone()[0] == "failed"


def test_bad_run_id_is_refused(garden):
    code, out = run(garden, ["apply", "../../etc"])
    assert code == dedup.EXIT_REFUSED and "not a run id" in out


def test_settings_have_no_default_model(tmp_path):
    ds = dedup.load_dedup_settings({"NOBLIVION_DATA_DIR": str(tmp_path)})
    assert ds.model == "" and ds.judge == "openrouter" and ds.min_cosine == 0.75
    ds = dedup.load_dedup_settings(
        {"NOBLIVION_DATA_DIR": str(tmp_path), "NOBLIVION_DEDUP_JUDGE": "ollama"}
    )
    assert ds.judge == "off"
