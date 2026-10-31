# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stock memory simulation: the index cut, S0, S1, S2 and the calibration."""

from __future__ import annotations

from pathlib import Path

import longtest_path  # noqa: F401 - puts tools on the path

from longtest import stocksim


def folder(tmp_path: Path, lines: int = 250) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    index = []
    for i in range(lines):
        index.append(f"- [Filler note {i}](filler_{i:03d}.md) - widget gadget number {i}")
    index[3] = (
        "- [Queue drain](feedback_queue_drain.md) - drain the queue worker before a migration"
    )
    index[230] = "- [Late note](feedback_late.md) - schema rollback procedure"
    (mem / "MEMORY.md").write_text("\n".join(index) + "\n")
    (mem / "feedback_queue_drain.md").write_text(
        "---\nname: Queue drain\n---\n\nUse --keel-window=1.\n"
    )
    (mem / "feedback_late.md").write_text("---\nname: Late\n---\n\nUse --late-flag=2.\n")
    (mem / "feedback_unlinked.md").write_text(
        "---\nname: Unlinked\n---\n\nThe zeppelin ballast value is --ballast=3.\n"
    )
    for i in range(0, 250, 50):
        (mem / f"filler_{i:03d}.md").write_text(f"---\nname: Filler {i}\n---\n\nnothing\n")
    return mem


def test_index_is_cut_at_200_lines(tmp_path):
    stock = stocksim.Stock(folder(tmp_path))
    assert len(stock.index.splitlines()) == 200
    assert "schema rollback" not in stock.index
    assert stock.index_tokens() == len(stock.index.encode()) // 4


def test_index_is_cut_at_25000_bytes(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("\n".join("- " + "x" * 198 for _ in range(150)) + "\n")
    stock = stocksim.Stock(mem)
    assert len(stock.index.encode()) <= 25_000
    assert len(stock.index.splitlines()) < 150


def test_levels_s0_s1_s2(tmp_path):
    stock = stocksim.Stock(folder(tmp_path))
    got = stock.delivered("please drain the queue worker before the migration", "--keel-window=1")
    assert got == {"S0": False, "S1": True, "S2": True}  # linked on a loaded line
    got = stock.delivered("what is the zeppelin ballast value", "--ballast=3")
    assert got == {"S0": False, "S1": False, "S2": True}  # found by the word search only
    got = stock.delivered("rollback of the schema", "--late-flag=2")
    assert got["S1"] is False  # its index line is past line 200


def test_a_named_file_counts_as_read(tmp_path):
    stock = stocksim.Stock(folder(tmp_path))
    got = stock.delivered("look at feedback_late.md please", "--late-flag=2")
    assert got["S1"] and got["S2"] and not got["S0"]


def test_s_cal_is_a_mix_with_wilson_carried_through():
    got = stocksim.s_cal(0.1, 0.9)
    assert abs(got["rate"] - (0.1 + 0.8 * 8 / 32)) < 1e-3
    assert got["low"] < got["rate"] < got["high"]
    same = stocksim.s_cal(0.5, 0.5)
    assert same["low"] == same["rate"] == same["high"] == 0.5


def test_stock_cost_is_the_index_on_every_call():
    assert stocksim.stock_cost(6000, 40) == 240_000


def test_sanity_gate():
    assert stocksim.sanity([(False, True, True)] * 10)["ok"]
    assert not stocksim.sanity([(False, True, False)] * 10)["ok"]
