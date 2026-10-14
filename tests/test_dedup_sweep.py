# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-108: judge parse failures, SUPERSEDE, the trigger union limit and
``noblivion dedup sweep``. Fictional memories, scripted judge replies, no
network: the opener of every ``OpenRouterJudge`` is a fixture."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

import pytest

from noblivion import dedup
from test_dedup import (
    FILES,
    LOSER,
    ROOT,
    SURVIVOR,
    VECTORS,
    FakeJudge,
    _plan_and_apply,
    _Response,
    build_garden,
    garden,  # noqa: F401  (a fixture)
    grant,
    no_network,  # noqa: F401  (a fixture)
    plan_files,
    run,
    run_id_of,
    snapshot,
)

OK = '{"verdict": "RELATED", "survivor": "none", "reason": "different lessons"}'


def body(content, *, cost=0.001, prompt=100, completion=20) -> bytes:
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        usage["cost"] = cost
    return json.dumps({"choices": [{"message": {"content": content}}], "usage": usage}).encode()


def scripted(replies: list[bytes]):
    """An opener that answers with ``replies`` in order, and records requests."""
    seen: list[dict] = []

    def opener(request: urllib.request.Request, timeout):
        seen.append(json.loads(request.data))
        return _Response(replies[len(seen) - 1])

    return opener, seen


def judge_of(opener) -> dedup.OpenRouterJudge:
    return dedup.OpenRouterJudge(
        FakeJudge.model, "test-placeholder-not-a-key", opener=opener, min_interval_s=0
    )


# -- parse robustness ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "verdict", "survivor"),
    [
        # reasoning text before and after the object
        (
            'Let me think.\n{"verdict": "MERGE", "survivor": "B", "reason": "r"}\nDone.',
            "MERGE",
            "B",
        ),
        # braces in the thinking text that do not open an object
        ('A {note} and {x: 1}. {"verdict": "RELATED", "survivor": "none"}', "RELATED", "none"),
        # a think block that holds an example object, then the real answer
        (
            '<think>maybe {"verdict": "MERGE", "survivor": "A"}</think>'
            '{"verdict": "CONTRADICT", "survivor": "none"}',
            "CONTRADICT",
            "none",
        ),
        # two objects: the last one with a verdict is the answer
        (
            '{"verdict": "MERGE", "survivor": "A"} no, wait: '
            '{"verdict": "SUPERSEDE", "survivor": "B", "reason": "r"}',
            "SUPERSEDE",
            "B",
        ),
        # the text is a list of parts, one of them empty
        ([{"type": "text", "text": ""}, {"type": "text", "text": OK}], "RELATED", "none"),
    ],
)
def test_parse_verdict_accepts_text_around_the_object(content, verdict, survivor):
    v = dedup.parse_verdict(content)
    assert (v.verdict, v.survivor, v.error) == (verdict, survivor, "")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "the judge answer is not text"),
        ("", "the judge answer is not text"),
        ([{"type": "text", "text": "  "}], "the judge answer is not text"),
        ("I think they are the same.", "the judge answer is not JSON"),
        ("<think>no end and " + OK, "the judge answer is not JSON"),  # an unclosed think block
        ('{"verdict": "MAYBE"}', "the judge answer does not match the schema"),
        ("[1]", "the judge answer is not an object"),
    ],
)
def test_parse_failure_is_unsure_with_the_parse_error(content, reason):
    v = dedup.parse_verdict(content)
    assert (v.verdict, v.error, v.reason) == ("UNSURE", dedup.PARSE_ERROR, reason)


def test_judge_retries_once_with_a_json_only_instruction():
    opener, seen = scripted(
        [
            body("Let me weigh both notes...", cost=0.002, prompt=300, completion=400),
            body(f"```json\n{OK}\n```", cost=0.003, prompt=310, completion=40),
        ]
    )
    v = judge_of(opener).judge("sys", "user")
    assert (v.verdict, v.error) == ("RELATED", "")
    assert len(seen) == 2
    assert seen[0]["max_tokens"] == dedup.MAX_OUTPUT_TOKENS
    assert seen[1]["max_tokens"] == dedup.RETRY_MAX_OUTPUT_TOKENS
    assert seen[1]["messages"][:2] == seen[0]["messages"]
    assert seen[1]["messages"][-1] == {"role": "user", "content": dedup.RETRY_INSTRUCTION}
    assert "ONE JSON object only" in dedup.RETRY_INSTRUCTION
    assert (v.prompt_tokens, v.completion_tokens) == (610, 440)  # both calls are counted
    assert v.cost == pytest.approx(0.005)


def test_judge_gives_up_after_one_retry():
    opener, seen = scripted([body("thinking"), body(None), body(OK)])
    v = judge_of(opener).judge("sys", "user")
    assert (v.verdict, v.error) == ("UNSURE", dedup.PARSE_ERROR)
    assert len(seen) == 2  # not three


def test_judge_does_not_retry_a_good_answer_or_an_http_error():
    opener, seen = scripted([body(OK)])
    assert judge_of(opener).judge("s", "u").verdict == "RELATED"
    assert len(seen) == 1

    seen2: list[object] = []

    def failing(request, timeout):
        seen2.append(request)
        raise OSError("down (fictional)")

    v = judge_of(failing).judge("s", "u")
    assert (v.verdict, v.error) == ("UNSURE", "OSError") and len(seen2) == 1


# -- fixture corpora -------------------------------------------------------------


def pair_files(i: int, *, extra: str = "") -> dict[str, str]:
    text = (
        "---\nname: {n}\ndescription: Fictional note {i} {n}\ntype: reference\n"
        "{extra}---\nBody of note {i} {n}.\n"
    )
    return {
        f"reference_p{i}_a.md": text.format(n="alpha", i=i, extra=extra),
        f"reference_p{i}_b.md": text.format(n="beta", i=i, extra=extra),
    }


def pair_vectors(i: int, n: int) -> dict[str, list[float]]:
    a = [0.0] * n
    a[i] = 1.0
    b = list(a)
    b[(i + 1) % n] = 0.2  # cosine about 0.98 with a, 0.2 with the next pair
    return {f"reference_p{i}_a.md": a, f"reference_p{i}_b.md": b}


@pytest.fixture
def pairs_garden(tmp_path, no_network):
    """Five pairs. Each pair is on its own axis, so only the pair is a candidate
    (the 0.2 leak to the next axis is far under the 0.75 threshold)."""
    n = 5
    files: dict[str, str] = {}
    vectors: dict[str, list[float]] = {}
    for i in range(n):
        files.update(pair_files(i))
        vectors.update(pair_vectors(i, n))
    g = build_garden(tmp_path, files, vectors)
    yield g
    g["conn"].close()


class CostJudge:
    """Answers the same verdict for every pair and reports a cost."""

    model = FakeJudge.model

    def __init__(self, answer: dict, cost: float | None = 0.01) -> None:
        self.answer, self.cost = answer, cost
        self.calls = 0

    def judge(self, system: str, user: str) -> dedup.Verdict:
        self.calls += 1
        v = dedup.parse_verdict(json.dumps(self.answer))
        v.prompt_tokens, v.completion_tokens, v.cost = 100, 20, self.cost
        return v


MERGE_A = {"verdict": "MERGE", "survivor": "A", "reason": "same lesson in both notes"}


def test_plan_summary_counts_parse_failures_apart_from_unsure(pairs_garden):
    g = pairs_garden
    grant(g)
    replies = [
        body(f"Both notes teach one thing.\n{OK}\nThat is my answer."),  # text around JSON
        body("The first note says"),  # no JSON, and
        body(""),  # an empty text on the retry: a parse failure
        body('{"verdict": "UNSURE", "survivor": "none", "reason": "too little text"}'),
    ]
    opener, seen = scripted(replies)
    code, out = run(
        g, ["plan", "--yes", "--max-pairs", "3"], factory=lambda _d, _k: judge_of(opener)
    )
    assert code == 0, out
    assert "RELATED=1, UNSURE=1; parse_failed=1" in out
    plan = json.loads(plan_files(g)[0].read_text())
    assert len(seen) == 4  # the failed pair was asked twice
    by_verdict = [(p["verdict"], p["error"]) for p in plan["pairs"]]
    assert by_verdict == [("RELATED", ""), ("UNSURE", "parse"), ("UNSURE", "")]
    assert plan["usage"]["completion_tokens"] == 80


# -- SUPERSEDE -------------------------------------------------------------------

SUPERSEDE_A = {"verdict": "SUPERSEDE", "survivor": "A", "reason": "B marks its own claim wrong"}


def test_supersede_archives_the_loser_with_a_pointer_and_undo_restores_it(garden):
    folder, conn, ids = garden["folder"], garden["conn"], garden["ids"]
    before = snapshot(folder)
    run_id, out = _plan_and_apply(garden, SUPERSEDE_A)
    assert "superseded reference_fern_time.md by reference_fern_noon.md" in out
    assert "reason: B marks its own claim wrong" in out
    assert "0 merged, 1 superseded" in out

    assert (folder / "reference_fern_noon.md").read_text() == SURVIVOR  # untouched
    assert not (folder / "reference_fern_time.md").exists()
    archived = folder / ".archive" / "dedup" / run_id / "reference_fern_time.md"
    stamped = archived.read_text()
    assert "superseded_by: reference_fern_noon\n---\n" in stamped
    assert stamped.replace("superseded_by: reference_fern_noon\n", "") == LOSER
    assert "(reference_fern_noon.md)" in (folder / "reference_compost.md").read_text()
    assert "Fern time" not in (folder / "MEMORY.md").read_text()
    row = conn.execute("SELECT status, verdict FROM dedup_actions").fetchone()
    assert row["status"] == "done" and json.loads(row["verdict"])["verdict"] == "SUPERSEDE"
    assert conn.execute(
        "SELECT archived_at FROM memories WHERE id = ?", (ids["reference_fern_time.md"],)
    ).fetchone()[0]

    plan = json.loads((garden["data"] / "dedup" / f"plan-{run_id}.json").read_text())
    (step,) = plan["applied"]
    assert (step["action"], step["loser"], step["survivor"]) == (
        "supersede",
        "reference_fern_time.md",
        "reference_fern_noon.md",
    )

    code, out = run(garden, ["undo", run_id])
    assert code == 0, out
    assert snapshot(folder) == before  # byte for byte, no archive folder left
    assert (
        conn.execute(
            "SELECT archived_at FROM memories WHERE id = ?", (ids["reference_fern_time.md"],)
        ).fetchone()[0]
        is None
    )
    assert conn.execute("SELECT status FROM dedup_actions").fetchone()[0] == "undone"
    assert conn.execute("SELECT count(*) FROM dedup_vetoes").fetchone()[0] == 1


def test_supersede_survivor_b_archives_a(garden):
    folder = garden["folder"]
    _plan_and_apply(garden, {**SUPERSEDE_A, "survivor": "B"})
    assert (folder / "reference_fern_time.md").read_text() == LOSER
    assert not (folder / "reference_fern_noon.md").exists()
    archived = next((folder / ".archive").rglob("reference_fern_noon.md"))
    assert "superseded_by: reference_fern_time\n" in archived.read_text()


def test_supersede_without_a_named_survivor_changes_nothing(garden):
    before = snapshot(garden["folder"])
    _, out = _plan_and_apply(garden, {**SUPERSEDE_A, "survivor": "none"})
    assert "the judge named no current file" in out
    assert snapshot(garden["folder"]) == before


def test_undo_of_a_supersede_stops_when_the_archived_copy_changed(garden):
    run_id, _ = _plan_and_apply(garden, SUPERSEDE_A)
    archived = next((garden["folder"] / ".archive").rglob("reference_fern_time.md"))
    archived.write_text(archived.read_text() + "edit\n")
    code, out = run(garden, ["undo", run_id])
    assert code == dedup.EXIT_REFUSED and "changed since the merge" in out
    assert not (garden["folder"] / "reference_fern_time.md").exists()


def test_a_crash_during_a_supersede_is_rolled_back(garden, monkeypatch):
    folder = garden["folder"]
    before = snapshot(folder)
    grant(garden)
    assert run(garden, ["plan", "--yes"], judge=FakeJudge(SUPERSEDE_A))[0] == 0
    real_unlink = dedup.os.unlink

    def failing(path, *a, **k):
        if str(path).endswith("reference_fern_time.md") and ".archive" not in str(path):
            raise OSError("disk full (fictional)")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(dedup.os, "unlink", failing)
    code, out = run(garden, ["apply", run_id_of(garden)])
    monkeypatch.setattr(dedup.os, "unlink", real_unlink)
    assert code == dedup.EXIT_LATCH and "step 1 failed" in out
    assert snapshot(folder) == before  # the stamped copy is gone, the loser is back


# -- trigger union ---------------------------------------------------------------


def test_merge_triggers_keeps_the_first_n_and_names_the_rest():
    kept, dropped = dedup.merge_triggers(["a", "b", "c"], ["b", "d", "e", "a", "f"], 5)
    assert (kept, dropped) == (["a", "b", "c", "d", "e"], ["f"])
    # the survivor's own items are never cut, the list only stops growing
    kept, dropped = dedup.merge_triggers(list("abcdef"), ["a", "x"], 4)
    assert (kept, dropped) == (list("abcdef"), ["x"])
    assert dedup.merge_triggers([], [], 12) == ([], [])


def _triggers(prefix: str, n: int) -> str:
    return "triggers:\n" + "".join(f"  - phrase:{prefix} {k}\n" for k in range(n))


def test_a_merge_with_a_long_trigger_union_trims_it_and_records_the_rest(tmp_path, no_network):
    survivor = (
        "---\nname: Alpha\ndescription: Alpha lesson\ntype: reference\nscope: tool\n"
        + _triggers("shared", 2).replace("\n  - phrase:shared 1", "")
        + "".join(f"  - phrase:own {k}\n" for k in range(8))
        + "---\nAlpha body.\n"
    )
    loser = (
        "---\nname: Beta\ndescription: Beta lesson\ntype: reference\nscope: tool\n"
        + "triggers:\n  - phrase:shared 0\n"
        + "".join(f"  - phrase:other {k}\n" for k in range(8))
        + "---\nBeta body.\n"
    )
    files = {"reference_alpha.md": survivor, "reference_beta.md": loser}
    vectors = {"reference_alpha.md": [1.0, 0.0], "reference_beta.md": [0.98, 0.2]}
    g = build_garden(tmp_path, files, vectors)
    try:
        before = snapshot(g["folder"])
        run_id, out = _plan_and_apply(g, MERGE_A)
        assert "merged reference_beta.md into reference_alpha.md" in out
        merged = (g["folder"] / "reference_alpha.md").read_text()
        fields = dedup.load_memory_fields().read_fields(merged)
        assert len(fields["triggers"]) == 12 == dedup.load_memory_fields().TRIGGERS_MAX_ITEMS
        assert fields["triggers"][:9] == ["phrase:shared 0"] + [f"phrase:own {k}" for k in range(8)]
        assert fields["triggers"][9:] == [f"phrase:other {k}" for k in range(3)]
        expected = [f"phrase:other {k}" for k in range(3, 8)]
        assert "5 trigger items did not fit" in out and expected[0] in out
        plan = json.loads((g["data"] / "dedup" / f"plan-{run_id}.json").read_text())
        assert plan["applied"][0]["dropped_triggers"] == expected
        # the dropped items stay in the archived loser, and undo restores everything
        archived = next((g["folder"] / ".archive").rglob("reference_beta.md"))
        assert all(item in archived.read_text() for item in expected)
        assert run(g, ["undo", run_id])[0] == 0
        assert snapshot(g["folder"]) == before
    finally:
        g["conn"].close()


# -- sweep -----------------------------------------------------------------------


def sweep(g, judge, *args, **kw):
    return run(g, ["sweep", *args], judge=judge, **kw)


def test_sweep_stops_at_the_cost_cap(pairs_garden):
    g = pairs_garden
    grant(g)
    judge = CostJudge({"verdict": "RELATED", "survivor": "none", "reason": "r"}, cost=0.01)
    code, out = sweep(g, judge, "--daily-cost-cap", "0.025")
    assert code == 0, out
    assert judge.calls == 2  # 0.01 + 0.01 spent, a third pair could pass 0.025
    assert "stop: daily cost cap $0.0250 reached; 3 pairs not judged" in out
    plan = json.loads(plan_files(g)[0].read_text())
    assert len(plan["pairs"]) == 2 and "daily cost cap" in plan["stopped"]
    assert plan["usage"]["cost_usd"] == pytest.approx(0.02)
    # a second sweep on the same day starts with that spend
    judge2 = CostJudge({"verdict": "RELATED", "survivor": "none", "reason": "r"}, cost=0.01)
    code, out = sweep(g, judge2, "--daily-cost-cap", "0.02")
    assert judge2.calls == 0 and "the daily cost cap is reached; nothing was sent" in out
    assert code == 0


def test_spent_today_sums_only_todays_plans(pairs_garden):
    g = pairs_garden
    settings = dedup.config.load_settings(g["env"])
    directory = g["data"] / "dedup"
    directory.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for name, day, cost in (
        ("a", today, 0.25),
        ("b", today, None),
        ("c", "2020-01-01", 9.0),
    ):
        doc = {"created_at": day + "T10:00:00Z", "usage": {"cost_usd": cost}}
        (directory / f"plan-{name}.json").write_text(json.dumps(doc))
    (directory / "plan-broken.json").write_text("{")
    assert dedup.spent_today(settings) == pytest.approx(0.25)


def test_sweep_refuses_a_judge_that_reports_no_cost(pairs_garden):
    g = pairs_garden
    grant(g)
    judge = CostJudge({"verdict": "RELATED", "survivor": "none", "reason": "r"}, cost=None)
    code, out = sweep(g, judge)
    assert code == 0 and judge.calls == 1
    assert "the judge reported no cost, so the daily cost cap cannot be kept" in out


def test_sweep_never_asks_and_needs_the_consent_the_user_gave(pairs_garden):
    g = pairs_garden
    judge = CostJudge(MERGE_A)
    before = snapshot(g["folder"])
    code, out = sweep(g, judge, stdin="yes\n")  # a "yes" on stdin is not read
    assert code == dedup.EXIT_REFUSED and judge.calls == 0
    assert "A sweep never asks; nothing was sent" in out
    assert snapshot(g["folder"]) == before
    g["env"].pop(dedup.KEY_ENV)
    code, out = sweep(g, judge)
    assert code == dedup.EXIT_REFUSED and f"{dedup.KEY_ENV} is not set" in out
    g["env"]["NOBLIVION_DEDUP_JUDGE"] = "off"
    code, out = sweep(g, judge)
    assert code == dedup.EXIT_REFUSED and judge.calls == 0


def test_sweep_plans_applies_reports_each_merge_and_undoes(pairs_garden):
    g = pairs_garden
    grant(g)
    before = snapshot(g["folder"])
    judge = CostJudge(MERGE_A, cost=0.001)
    code, out = sweep(g, judge, "--max-pairs", "3")
    assert code == 0, out
    assert judge.calls == 3
    for i in range(3):
        assert f"merged reference_p{i}_b.md into reference_p{i}_a.md ({ROOT})" in out
    assert "cosine=0.9" in out and "reason: same lesson in both notes" in out
    assert "sweep" in out and "3 judged, 3 merged, 0 superseded" in out
    run_id = run_id_of(g)
    assert f"undo: noblivion dedup undo {run_id}" in out
    assert not (g["folder"] / "reference_p0_b.md").exists()
    assert (g["folder"] / "reference_p3_b.md").exists()  # beyond --max-pairs
    status = json.loads((g["data"] / "cache" / "dedup-last-run.json").read_text())
    assert (status["mode"], status["merged"], status["run_id"]) == ("sweep", 3, run_id)
    plan = json.loads(plan_files(g)[0].read_text())
    assert len(plan["applied"]) == 3 and plan["stopped"] == ""
    code, out = run(g, ["undo", run_id])
    assert code == 0 and "3 steps undone" in out
    assert snapshot(g["folder"]) == before


def test_sweep_applies_a_supersede_too(garden):
    grant(garden)
    code, out = sweep(garden, CostJudge(SUPERSEDE_A, cost=0.001))
    assert code == 0, out
    assert "superseded reference_fern_time.md by reference_fern_noon.md" in out
    assert "0 merged, 1 superseded" in out
    assert (garden["folder"] / "reference_fern_noon.md").read_text() == SURVIVOR


def test_sweep_with_no_pairs_sends_nothing(garden):
    grant(garden)
    garden["conn"].execute("DELETE FROM vectors")
    garden["conn"].commit()
    judge = CostJudge(MERGE_A)
    code, out = sweep(garden, judge)
    assert code == 0 and judge.calls == 0 and "no candidate pair" in out
    assert not plan_files(garden)


def test_sweep_stops_before_any_spend_when_the_latch_is_set(garden):
    grant(garden)
    dedup._set_latch(dedup.config.load_settings(garden["env"]), "20260101T000000Z-abcdef", "x")
    judge = CostJudge(MERGE_A)
    code, out = sweep(garden, judge)
    assert code == dedup.EXIT_LATCH and judge.calls == 0 and "latch" in out
    status = json.loads((garden["data"] / "cache" / "dedup-last-run.json").read_text())
    assert status["outcome"] == "refused"


def test_the_daily_cost_cap_is_a_setting(tmp_path):
    env = {"NOBLIVION_DATA_DIR": str(tmp_path)}
    assert dedup.load_dedup_settings(env).daily_cost_cap_usd == dedup.DEFAULT_DAILY_COST_CAP_USD
    (tmp_path / "config.json").write_text('{"dedup": {"daily_cost_cap_usd": 1.25}}')
    assert dedup.load_dedup_settings(env).daily_cost_cap_usd == 1.25
    (tmp_path / "config.json").write_text('{"dedup": {"daily_cost_cap_usd": -1}}')
    assert dedup.load_dedup_settings(env).daily_cost_cap_usd == dedup.DEFAULT_DAILY_COST_CAP_USD


def test_the_fixture_files_are_the_ones_of_the_dedup_tests():
    assert set(FILES) == set(VECTORS) | {"MEMORY.md"}
