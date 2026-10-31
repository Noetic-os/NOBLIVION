# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fixture readers and the analysis of replay rows."""

from __future__ import annotations

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import fixtures, replay
from longtest.buildrunner import Harness

SEED = 20261030


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return fixtures.Fixture.ensure(tmp_path_factory.mktemp("fx") / "out", SEED, [30, 200])


def test_sizes_shards_and_cwd(fx):
    assert fx.sizes() == [30, 200]
    assert len(fx.shards(30)) == 20 and len(fx.shards(200)) == 3
    q = next(fx.queries_for(200, ["V4"]))
    lesson = fx.scale(200)["lessons"][q["lesson"]]
    assert fx.cwd_of(200, q) == f"/work/{lesson['other_project']}"
    v3 = next(fx.queries_for(200, ["V3"]))
    assert fx.cwd_of(200, v3).startswith("/work/")


def test_v5_runs_in_the_shard_of_its_near_lesson(fx):
    q = next(fx.queries_for(200, ["V5"]))
    assert fx.lesson_of(q) == q["near_lesson"]
    assert fx.lesson_shard(200, q["near_lesson"]) in fx.shards(200)


def test_shard_filter_partitions_the_queries(fx):
    total = sum(len(list(fx.queries_for(200, None, s))) for s in fx.shards(200))
    assert total == len(fx.queries)


def test_prompt_expands_the_fixture_root(fx):
    q = next(fx.queries_for(200, ["V6"]))
    assert "{FIXTURE_ROOT}" in q["prompt"] and str(fx.root) in fx.prompt_of(q)
    assert (fx.root / q["task_file"]).is_file()


def test_leakage_check_passes_on_the_frozen_fixture(fx):
    assert fixtures.leakage(fx)["ok"]


def test_leakage_check_catches_an_answer_in_a_query(fx):
    fx.queries.append(
        {
            "id": "BAD",
            "variant": "V1",
            "lesson": "L01",
            "prompt": "the flag is " + fx.lessons["L01"]["answer"],
        }
    )
    try:
        assert fixtures.leakage(fx)["answer_in_query"] == ["BAD"]
    finally:
        fx.queries.pop()


def test_distractors_skip_planted_notes(fx):
    names = {p.name for p in fx.distractors(200, 0, 50)}
    planted = {n for row in fx.lessons.values() for n in fx._planted_names(row)}
    assert names and not names & planted


def test_ensure_refuses_another_seed(fx):
    with pytest.raises(SystemExit):
        fixtures.Fixture.ensure(fx.root, SEED + 1, [30])


def test_hit_rank():
    text = "header\n- [id 1] a: nothing\n- [id 2] b: use --x=1 now\n"
    assert replay.hit_rank(text, "--x=1") == (True, 2)
    assert replay.hit_rank("no rows --x=1", "--x=1") == (True, None)
    assert replay.hit_rank(text, "--y=2") == (False, None)


def row(
    build, qid, lesson, hit, size=200, claims=("C1",), channel="prompt", variant="V3", chars=100
):
    return {
        "build": build,
        "size": size,
        "shard": 0,
        "qid": qid,
        "variant": variant,
        "lesson": lesson,
        "channel": channel,
        "claims": list(claims),
        "expect": "answer",
        "hit": hit,
        "rank": 1 if hit else None,
        "chars": chars,
        "rows": 2,
        "ms": 10.0,
    }


def synthetic(n=40):
    rows = []
    for i in range(n):
        lesson = f"L{i % 10:02d}"
        rows += [
            row("B1", f"q{i}", lesson, True),
            row("B0", f"q{i}", lesson, i % 4 != 0, chars=300),
            row("S2", f"q{i}", lesson, i % 5 == 0, channel="stock"),
        ]
    return rows


def test_rate_table_and_pair_compare():
    rows = synthetic()
    rates = replay.rate_table(rows)
    assert rates["200|C1|B1"]["rate"] == 1.0 and rates["200|C1|S2"]["rate"] == 0.2
    vs = replay.pair_compare(rows, "B1", "S2", resamples=200)["200|C1"]
    assert vs["n"] == 40 and vs["only_first"] == 32 and vs["only_second"] == 0
    assert vs["p"] < 0.001 and abs(vs["diff"] - 0.8) < 1e-9 and vs["low"] >= 0.5


def test_cost_table_and_false_rates():
    rows = synthetic()
    cost = replay.cost_table(rows)
    assert cost["200|B1"]["chars"]["p50"] == 100 and cost["200|B0"]["chars"]["p50"] == 300
    rows.append(
        {
            **row("B1", "n1", "L01", True, claims=(), variant="V5"),
            "false_delivery": True,
            "expect": "none",
        }
    )
    rows.append(
        {
            **row("B1", "n2", "L01", True, claims=(), variant="V5"),
            "false_delivery": False,
            "expect": "none",
        }
    )
    assert replay.false_delivery(rows)["200|V5|B1"]["rate"] == 0.5
    rows.append(
        {"build": "B1", "size": 200, "variant": "BENIGN", "false_deny": True, "channel": "guard"}
    )
    assert replay.false_deny(rows)["200|B1"]["hits"] == 1


def test_rank_table_mrr():
    rows = [
        row("B1", "a", "L01", True, channel="pull", variant="V2"),
        {**row("B1", "b", "L02", True, channel="pull", variant="V2"), "rank": 4},
        row("B1", "c", "L03", False, channel="pull", variant="V2"),
    ]
    got = replay.rank_table(rows, "V2", "pull")["200|B1"]
    assert got["recall_at_1"] == round(1 / 3, 4) and got["recall_at_5"] == round(2 / 3, 4)
    assert got["mrr"] == round((1 + 0.25) / 3, 4)


def test_stock_rows_on_a_real_fixture(fx):
    shard = fx.shards(200)[0]
    rows = replay.run_stock(fx, 200, shard, fx.shard_dir(200, shard), ["V1", "V3"], 3)
    assert {r["build"] for r in rows} == {"S0", "S1", "S2"}
    assert all(r["channel"] == "stock" and r["claims"] is not None for r in rows)
    by = {lvl: sum(r["hit"] for r in rows if r["build"] == lvl) for lvl in ("S0", "S1", "S2")}
    assert by["S0"] <= by["S1"] <= by["S2"]
    assert by["S0"] == 0  # the answer is never in an index


def test_credentials_are_planted_where_the_source_says(fx, tmp_path):
    h = Harness("B1", tmp_path / "build", tmp_path / "root", "python3", None)
    creds = replay.Credentials(h)
    row_ = next(r for r in fx.lessons.values() if r.get("source") == "git_remote")
    folder = creds.plant(row_, "atlas")
    conf = (folder / ".git" / "config").read_text()
    assert row_["canary"] in conf
    other = next(r for r in fx.lessons.values() if r.get("source") == "netrc")
    creds.plant(other, "atlas")
    assert other["canary"] in (h.home / ".netrc").read_text()


def test_load_tool_gives_whole_modules_to_parallel_threads():
    """A second thread must not get a module that is still executing (it did, in the
    first pilot: ``Store`` was missing)."""
    import sys
    import threading

    from longtest.util import load_tool

    sys.modules.pop("lt_race_suite", None)
    seen: list[bool] = []

    def grab() -> None:
        seen.append(hasattr(load_tool("eval_suite.py", "lt_race_suite"), "Store"))

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [True] * 8
