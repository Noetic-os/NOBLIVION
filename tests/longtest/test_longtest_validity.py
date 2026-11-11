# SPDX-License-Identifier: AGPL-3.0-or-later
"""The validity gate of the report: an arm that did not fire gives INVALID, never a
win or a loss."""

from __future__ import annotations

import argparse
import json

import longtest_path  # noqa: F401 - puts tools on the path

from longtest import cli, profiles, report, runner
from longtest.util import append_jsonl

PARAMS = profiles.LARGE.params


def row(build, size, day, lesson, variant, hit, claims=()):
    """One replay row as ``replay.ShardRun.row`` and ``replay.run_stock`` write it."""
    stock = build.startswith("S")
    return {
        "build": build,
        "size": size,
        "shard": 0,
        "day": day,
        "qid": f"L{lesson:02d}-{variant}",
        "variant": variant,
        "spelling": "",
        "lesson": f"L{lesson:02d}",
        "channel": "stock" if stock else "pull",
        "claims": list(claims),
        "expect": "answer",
        "ok": True,
        "hit": hit,
        "named": hit,
        "rank": 1 if hit else None,
        "chars": 400 if hit else 0,
        "rows": 3 if hit else 0,
        "ms": 300.0 if hit else 0.6,
        "decision": "",
    }


def step(size, day, b1_hits, s2_hits, n=60):
    """The rows of one step at one size: the sanity set (V1) and the claim C1 (V3)."""
    rows = []
    for i in range(n):
        rows.append(row("B1", size, day, i, "V1", i < b1_hits))
        rows.append(row("S2", size, day, i, "V1", i < s2_hits))
        rows.append(row("B1", size, day, i, "V3", i < b1_hits, ["C1"]))
        rows.append(row("S2", size, day, i, "V3", i < s2_hits // 4, ["C1"]))
    return rows


class FakeFixture:
    """The queries of ``step``: lessons 0 to n-1 with the variants V1, V3 and V5, on one
    shard. Stands in for ``Run.fixture`` where a test writes its rows by hand."""

    def __init__(self, n=60, variants=("V1", "V3", "V5")):
        self.n, self.variants = n, variants

    def shards(self, size):
        return [0]

    def queries_for(self, size, variants=None, shard=None):
        for v in self.variants:
            if variants is None or v in variants:
                for i in range(self.n):
                    yield {"id": f"L{i:02d}-{v}", "variant": v}


def expected(sizes, n=60, variants=("V1", "V3")):
    return runner.stock_expected(FakeFixture(n, variants), sizes, None, None, None)


def test_gate_passes_when_every_arm_fired_on_every_step():
    rows = [r for day in (1, 2, 3) for r in step(5000, day, 60, 55)]
    rows += step(5000, 0, 0, 0)  # the pilot is not a judged step
    gate = report.validity(rows, params=PARAMS, expected=expected([5000]))
    assert gate["5000"]["ok"] and gate["5000"]["causes"] == []
    assert sorted(gate["5000"]["steps"]) == ["1", "2", "3"]
    cell = gate["5000"]["steps"]["2"]
    assert cell["B1"] == {"n": 60, "delivery": 1.0, "floor": 0.95, "invalid": 0, "ok": True}
    assert cell["S2"] == {
        "n": 60,
        "delivery": 0.917,
        "floor": None,
        "queries": 120,
        "expected": 120,
        "errors": 0,
        "invalid": 0,
        "ok": True,
    }


def test_the_gate_of_s2_is_mechanical_and_its_rate_is_reported():
    """Ruling 2026-11-11: S2 cannot latch, so its gate is every query evaluated and no
    error; its sanity rate (0.70 to 0.80 at the large scales) has no floor."""
    rows = step(5000, 1, 60, 45) + step(20000, 1, 60, 42)  # S2 at 0.75 and 0.70
    gate = report.validity(rows, params=PARAMS, expected=expected([5000, 20000]))
    assert gate["5000"]["ok"] and gate["20000"]["ok"], gate
    assert gate["5000"]["steps"]["1"]["S2"]["delivery"] == 0.75  # reported
    assert gate["20000"]["steps"]["1"]["S2"]["delivery"] == 0.7
    # the B1 gate stays the floor of the recall tool
    low = report.validity(step(5000, 1, 56, 45), params=PARAMS, expected=expected([5000]))
    assert low["5000"]["causes"] == ["step 1: B1 sanity delivery 0.933 of 60, under the floor 0.95"]
    # a query that stock did not evaluate fails the gate
    lost = [r for r in rows if not (r["build"] == "S2" and r["qid"] == "L07-V3")]
    got = report.validity(lost, params=PARAMS, expected=expected([5000, 20000]))["5000"]
    assert got["causes"] == [
        "step 1: S2 evaluated 119 queries, the step replays 120 (1 missing, 0 not in the step)"
    ]
    # a row with an error, and a query with two rows, fail it too
    bad = step(5000, 1, 60, 45)
    next(r for r in bad if r["build"] == "S2").update(ok=False, error="KeyError")
    bad.append(dict(next(r for r in bad if r["build"] == "S2" and r["variant"] == "V3")))
    got = report.validity(bad, params=PARAMS, expected=expected([5000]))["5000"]
    assert got["causes"] == [
        "step 1: S2 has 1 queries with more than one row",
        "step 1: S2 has 1 rows with an error",
    ]
    assert got["steps"]["1"]["S2"]["invalid"] == 1 and got["steps"]["1"]["B1"]["ok"] is True
    # with no expected set the gate still needs rows and no query twice
    assert report.validity(step(5000, 1, 60, 45), params=PARAMS)["5000"]["ok"] is True
    none = [r for r in step(5000, 1, 60, 45) if r["build"] != "S2"]
    assert "step 1: S2 has no rows" in report.validity(none, params=PARAMS)["5000"]["causes"]
    # the first run keeps its floor for S2 at the small scale
    assert report.sanity_floor("S2", profiles.PLAN2.params) == 0.9
    assert report.sanity_floor("S2", PARAMS) is None


def test_gate_names_the_step_the_arm_and_the_floor():
    rows = step(5000, 1, 60, 55) + step(5000, 2, 0, 55) + step(20000, 1, 56, 47)
    gate = report.validity(rows, params=PARAMS, sizes=[5000, 20000, 1000])
    assert not gate["5000"]["ok"]
    assert gate["5000"]["causes"] == ["step 2: B1 sanity delivery 0.0 of 60, under the floor 0.95"]
    assert gate["20000"]["causes"] == [
        "step 1: B1 sanity delivery 0.933 of 60, under the floor 0.95"
    ]
    assert gate["1000"] == {"ok": False, "causes": ["no judged step"], "steps": {}}
    # an arm with no sanity row has proved nothing
    silent = [r for r in step(5000, 1, 60, 55) if not (r["build"] == "B1" and r["variant"] == "V1")]
    assert report.validity(silent, params=PARAMS)["5000"]["causes"] == [
        "step 1: B1 sanity delivery no sanity rows, under the floor 0.95"
    ]
    # the prompt channel of a build is not its sanity set
    prompt = [dict(r, channel="prompt") for r in step(5000, 1, 60, 55)]
    assert report.validity(prompt, params=PARAMS)["5000"]["steps"]["1"]["B1"]["n"] == 0


def test_gate_fails_a_step_that_holds_an_invalid_row():
    """A row under the hook latch, or one whose store did not answer, is not a
    miss of the build: the step is not judged, even with the sanity floor reached."""
    for fail in ("timeout", "store_hung"):
        rows = step(5000, 1, 60, 55) + step(5000, 2, 60, 55)
        bad = next(r for r in rows if r["day"] == 2 and r["build"] == "B1" and r["variant"] == "V3")
        bad.update(fail=fail, hit=False)
        gate = report.validity(rows, params=PARAMS)["5000"]
        assert gate["ok"] is False and gate["steps"]["1"]["B1"]["ok"] is True
        cell = gate["steps"]["2"]
        assert cell["B1"] == {"n": 60, "delivery": 1.0, "floor": 0.95, "invalid": 1, "ok": False}
        assert cell["S2"]["invalid"] == 0 and cell["S2"]["ok"] is True
        assert gate["causes"] == [
            f"step 2: B1 has 1 invalid rows, fail reasons {{'{fail}': 1}} "
            "(the hook latch, or a store that did not answer)"
        ]
    summary = {"b1_vs_s2": {"5000|C1": comparison(0.3, 0.6, 1e-6)}, "false_delivery_v5": {}}
    soak = {"checks": dict.fromkeys(("budget_recall", "lag_p95", "drift_zero", "error_rate"), True)}
    trend = {"5000": {"ok": True, "nights": 7}}
    got = report.verdicts_large(summary, soak, trend, validity={"5000": gate})
    assert "step 2: B1 has 1 invalid rows" in got["scales"]["LARGE-1"]["invalid_because"][0]
    assert got["scales"]["LARGE-1"]["verdict"] == "INVALID"
    # a row that began under the stamp is invalid whatever its reason, and so is a row
    # whose store was gone; a skip of the product is not a fail
    cases = (({"latched": True, "fail": ""}, 1), ({"fail": "store_down"}, 1), ({"fail": ""}, 0))
    for extra, invalid in cases:
        rows = step(5000, 1, 60, 55)
        rows[0].update(extra)
        assert (
            report.validity(rows, params=PARAMS)["5000"]["steps"]["1"]["B1"]["invalid"] == invalid
        )


def test_prompt_delivery_is_reported_apart_from_the_sanity_set():
    rows = step(5000, 1, 60, 55)
    rows += [dict(row("B1", 5000, 1, i, "V1", i < 52), channel="prompt") for i in range(60)]
    assert report.prompt_delivery(rows, "B1") == {"n": 60, "delivery": 0.867}
    assert report.sanity_delivery(rows, "B1") == {"n": 60, "delivery": 1.0}
    assert report.prompt_delivery(rows, "S2") == {"n": 0, "delivery": None}


def comparison(low, high, p):
    return {"n": 60, "p": p, "diff": (low + high) / 2, "low": low, "high": high}


def test_an_invalid_scale_is_not_judged():
    summary = {
        "b1_vs_s2": {
            "5000|C1": comparison(-0.9, -0.7, 1e-9),  # the latch: B1 gave nothing
            "5000|C2": comparison(-0.3, -0.1, 0.01),
            "20000|C1": comparison(0.3, 0.6, 1e-6),
            "20000|C2": comparison(0.0, 0.2, 0.5),
        },
        "false_delivery_v5": {},
    }
    soak = {"checks": dict.fromkeys(("budget_recall", "lag_p95", "drift_zero", "error_rate"), True)}
    trend = {"5000": {"ok": False, "nights": 7}, "20000": {"ok": True, "nights": 7}}
    gate = report.validity(
        step(5000, 1, 0, 55) + step(20000, 1, 60, 55), params=PARAMS, sizes=[5000, 20000]
    )
    got = report.verdicts_large(summary, soak, trend, validity=gate)
    bad, good = got["scales"]["LARGE-1"], got["scales"]["LARGE-2"]
    assert bad["verdict"] == "INVALID"
    assert bad["dr1_c1"] is None and bad["dr3"] is None and bad["dr6"] is None
    assert bad["invalid_because"] == ["step 1: B1 sanity delivery 0.0 of 60, under the floor 0.95"]
    assert got["claims"]["5000|C1"]["judged"] is False and got["claims"]["5000|C1"]["lose"] is None
    assert good["verdict"] == "WIN" and "invalid_because" not in good
    assert got["claims"]["20000|C1"]["judged"] is True and got["validity"] is gate
    # without the gate the same rows read as a loss: that was the report of the 7-day run
    assert report.verdicts_large(summary, soak, trend)["scales"]["LARGE-1"]["verdict"] == "LOSE"


def test_report_command_gives_invalid_for_an_arm_that_did_not_fire(tmp_path, capsys, monkeypatch):
    run_dir = tmp_path / "run"
    (run_dir / "out").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "t", "profile": "large-project", "sizes": [5000, 20000]})
    )
    rows = []
    for day in (1, 2, 3):
        rows += step(5000, day, 0 if day == 2 else 60, 55)  # the latch took night 2
        rows += step(20000, day, 60, 55)
    for r in rows + step(5000, 0, 60, 55):
        append_jsonl(run_dir / "out" / "replay.jsonl", r)
    for valid in (True, False):
        append_jsonl(
            run_dir / "out" / "latency.jsonl",
            {"ts": "t", "build": "B1", "size": 5000, "p50": 0.5, "latched": 0 if valid else 99}
            | {"valid": valid},
        )
    append_jsonl(run_dir / "out" / "latency.jsonl", {"ts": "t", "build": "soak", "p50": 0.5})
    monkeypatch.setattr(runner.Run, "fixture", lambda self: FakeFixture(variants=("V1", "V3")))
    assert cli.cmd_report(argparse.Namespace(run_dir=str(run_dir))) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["LARGE-1"]["verdict"] == "INVALID"
    assert "step 2: B1 sanity delivery 0.0 of 60" in printed["LARGE-1"]["invalid_because"][0]
    assert printed["LARGE-2"]["verdict"] not in ("INVALID", "LOSE", "NO DATA")
    doc = json.loads((run_dir / "final_report.json").read_text())["verdicts"]
    assert doc["validity"]["5000"]["steps"]["2"]["B1"]["delivery"] == 0.0
    assert doc["validity"]["20000"]["ok"] is True
    lat = doc["latency"]
    assert (lat["valid"], lat["invalid"], lat["unchecked"]) == (1, 1, 1)
    assert lat["invalid_rows"][0]["latched"] == 99


def test_pilot_gate_still_gates_stock_at_the_small_scale_only():
    rows = [dict(r, day=0) for r in step(5000, 0, 60, 42)]
    assert cli.sanity_gate(rows, builds=("B1",))["ok"] is True  # stock is reported, not gated
    assert cli.sanity_gate(rows, builds=("B0", "B1"))["ok"] is False  # no B0 rows, S2 at 0.7
