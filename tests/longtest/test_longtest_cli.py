# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run folder helpers and the pilot gates."""

from __future__ import annotations

import json

from longtest import cli, replay, runner


def test_host_settings_reads_switches_only(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "NOBLIVION_CONTINUITY": "1",
                    "NOBLIVION_DATA_DIR": "/live/data",  # a path: left out
                    "NOBLIVION_MINER": "0",
                    "ANTHROPIC_API_KEY": "x",  # not ours: left out
                    "OTHER": "1",
                }
            }
        )
    )
    got = runner.host_settings(tmp_path)
    assert got == {"NOBLIVION_CONTINUITY": "1", "NOBLIVION_MINER": "0"}
    assert runner.host_settings(tmp_path / "none") == {}


def test_pointer_rank_names_a_note_by_title_or_answer():
    text = (
        "GROUNDED MEMORY\n- [feedback] Drain the queue worker before a schema migration: snippet\n"
    )
    assert replay.pointer_rank(
        text, "--x=1", "Drain the queue worker before a schema migration"
    ) == (
        True,
        1,
    )
    assert replay.pointer_rank("nothing", "--x=1", "Some title") == (False, None)
    assert replay.pointer_rank("- [id 3] use --x=1\n", "--x=1", "T") == (True, 1)


def row(variant, build, hit, channel="prompt", expect="answer"):
    return {"variant": variant, "build": build, "hit": hit, "channel": channel, "expect": expect}


def test_sanity_gate_needs_pull_delivery_for_builds_and_stock():
    rows = [
        {**row("V1", b, True, "pull" if b != "S2" else "stock"), "size": 30}
        for b in ("B0", "B1", "S2")
    ] * 20
    assert cli.sanity_gate(rows)["ok"]
    rows += [{**row("V1", "B1", False, "pull"), "size": 30}] * 5
    rows += [{**row("V1", "B1", False, "pull"), "size": 200}] * 50  # larger sizes do not count
    got = cli.sanity_gate(rows)
    assert not got["ok"] and got["B1"]["delivery"] == 0.8


def test_channels_fired_skips_negatives_and_benign_rows():
    rows = [
        row("CMD", "B1", True, "guard"),
        row("CMD", "B0", False, "guard"),
        row("V7", "B1", True, expect="no_rows"),
        {"variant": "BENIGN", "build": "B1", "channel": "guard"},
    ]
    got = cli.channels_fired(rows)
    assert got == {"CMD:guard": {"B0": 0, "B1": 1, "n": 1}}


def test_parse_sizes_refuses_an_unknown_size():
    import pytest

    assert cli.parse_sizes("30,200") == [30, 200]
    with pytest.raises(SystemExit):
        cli.parse_sizes("31")


def test_installed_version_reads_the_plugin_list_then_the_cache(tmp_path):
    data = tmp_path / "plugins" / "data" / "noblivion-noblivion"
    data.mkdir(parents=True)
    for v in ("0.1.9", "0.1.18", "0.1.15"):
        (tmp_path / "plugins" / "cache" / "mk" / "noblivion" / v).mkdir(parents=True)
    assert runner.installed_version(data) == "0.1.18"  # the highest, not the first
    (tmp_path / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"noblivion@mk": [{"version": "0.1.16"}]}})
    )
    assert runner.installed_version(data) == "0.1.16"


def test_a_task_counts_as_done_only_with_its_marker(tmp_path):
    (tmp_path / "out").mkdir()
    run = runner.Run(tmp_path)
    run.append_many("replay", [{"qid": "a"}, {"qid": "b"}])
    assert run.done(0) == set()  # rows without a marker: the task runs again
    run.append("replay-done", {"day": 0, "build": "B1", "size": 30, "shard": 1})
    run.append("replay-done", {"day": 1, "build": "B0", "size": 30, "shard": 1})
    assert run.done(0) == {("B1", 30, 1)}
