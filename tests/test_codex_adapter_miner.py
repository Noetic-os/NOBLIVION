# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex journals remain consumable by the package's shared miner."""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_adapter_helpers import adapter, event
from codex_adapter_helpers import cfg as cfg
from noblivion import db, miner


def test_codex_journal_uses_shared_miner_record_shape(
    cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "_invoke", lambda *args: {})
    cfg["recall_root"] = "shared-project-root"
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", prompt="Run the fixture build command."),
    )
    adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PostToolUse",
            tool_name="exec_command",
            tool_use_id="call_fixture",
            tool_input={"cmd": "make missing-target"},
            tool_response={
                "exit_code": 2,
                "output": "make: *** No rule to make target 'missing-target'.",
            },
        ),
    )
    adapter.handle_hook(
        cfg,
        event(cfg, "Stop", last_assistant_message="The build target failed."),
    )
    adapter.handle_hook(
        cfg,
        event(cfg, "UserPromptSubmit", prompt="No, use the generated build target."),
    )
    adapter.handle_hook(
        cfg,
        event(
            cfg,
            "PostToolUse",
            session_id="thr_second_codex_session",
            tool_name="exec_command",
            tool_use_id="call_second_fixture",
            tool_input={"cmd": "make other-missing-target"},
            tool_response={
                "exit_code": 2,
                "output": "make: *** No rule to make target 'other-missing-target'.",
            },
        ),
    )

    folder = Path(cfg["state_dir"]) / "transcripts" / cfg["recall_root"]
    journals = sorted(folder.glob("codex-*.jsonl"))
    assert len(journals) == 2
    journal = journals[0]
    stats = miner.MineStats()
    with journal.open("rb") as source:
        scan = miner.mine_stream(
            source,
            session_id="codex_fixture",
            offset=0,
            miner=miner.Miner(stats),
            stats=stats,
        )
    assert scan.candidates
    assert stats.bad_lines == 0

    conn = db.open_db(Path(cfg["data_dir"]) / "noblivion.db", create=True)
    try:
        mined = miner.run(
            conn,
            miner.list_transcripts(str(folder / "codex-*.jsonl")),
            project=cfg["namespace"],
            max_run_s=None,
        )
        rows = conn.execute(
            "SELECT root, path, category FROM memories WHERE source_type = ? ORDER BY path",
            (db.SOURCE_MINED,),
        ).fetchall()
    finally:
        conn.close()
    assert mined.inserted == 3
    assert {row["root"] for row in rows} == {cfg["recall_root"]}
    assert {row["category"] for row in rows} == {
        miner.KIND_TOOL_ERROR,
        miner.KIND_CORRECTION,
    }
    assert len({row["path"].split("#", 1)[0] for row in rows}) == 2
    assert all(row["path"].startswith("codex-") for row in rows)
