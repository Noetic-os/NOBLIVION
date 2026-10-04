# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noblivion trust timesplit``: the time-split test for trust ranking
(NOBLIVION-38). Synthetic events only.

What these tests hold:

1. The fit uses the events before the cut only, with the store's formula
   and the hook's factor.
2. When older use predicts newer use, the verdict is ``gain``; when it does
   not, ``no gain``; with few test sessions, ``too little data``.
3. The per-prompt test reads the hook's trust log and re-sorts its base
   order the way the hook does.
4. The command runs on a store and changes no setting: trust ranking stays
   off by default.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import __main__ as cli
from noblivion import config, db, trust
from noblivion import trust_timesplit as ts
from store_helpers import add_memory

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _events(n_train: int, n_test: int, test_used: int = 1, notes: int = 5) -> list[ts.Event]:
    """Each session shows notes 1..notes. In training sessions note 1 is
    used; in test sessions note ``test_used`` is used."""
    out: list[ts.Event] = []
    for i in range(n_train + n_test):
        sid = f"s{i:03d}"
        start = T0 + timedelta(hours=i)
        used = 1 if i < n_train else test_used
        for mid in range(1, notes + 1):
            out.append(ts.Event(sid, mid, "recall", start))
        out.append(ts.Event(sid, used, "use", start + timedelta(minutes=5)))
    return out


PRIORS = {mid: 0.5 for mid in range(1, 6)}


def test_factor_matches_the_hook() -> None:
    rank = load_hook("trust_rank", "trust_rank_t_timesplit")
    assert (ts.F_MIN, ts.F_MAX, ts.N_MIN_TRIALS) == (rank.F_MIN, rank.F_MAX, rank.N_MIN_TRIALS)
    for args in ((0.9, 10, 0.5), (0.1, 10, 0.5), (0.6, 4, 0.5), (0.55, 6, 0.5)):
        assert ts.factor(*args) == pytest.approx(rank.factor(*args))


def test_pick_cut_is_the_start_of_the_first_test_session() -> None:
    events = _events(7, 3)
    assert ts.pick_cut(events, 0.7) == T0 + timedelta(hours=7)
    assert ts.pick_cut(_events(1, 0), 0.7) is None


def test_fit_ignores_events_after_the_cut() -> None:
    events = _events(10, 10, test_used=2)
    cut = T0 + timedelta(hours=10)
    before = [e for e in events if e.ts < cut]
    assert ts.fit(events, cut, PRIORS) == ts.fit(before, cut, PRIORS)
    fits = ts.fit(events, cut, PRIORS)
    assert fits[1].trials == 10
    assert fits[1].trust == pytest.approx(trust.trust_score(0.5, 10, 10.0, 0))
    assert fits[2].trials == 0 and fits[2].factor == 1.0


def test_a_contradiction_lowers_the_fit() -> None:
    cut = T0 + timedelta(days=1)
    events = [ts.Event(f"s{i}", 1, "use", T0) for i in range(6)]
    plain = ts.fit(events, cut, {1: 0.5})[1]
    events.append(ts.Event("s0", 1, "contradiction", T0))
    assert ts.fit(events, cut, {1: 0.5})[1].trust < plain.trust


def test_gain_when_older_use_predicts_newer_use() -> None:
    result = ts.run(_events(70, 30), PRIORS, shuffles=20)
    unit = result["session"]
    assert unit["units"] == 30
    assert unit["mrr_on"] == pytest.approx(1.0)
    assert unit["mrr_off"] < 0.7
    assert unit["better"] > unit["worse"]
    assert result["verdict"] == "gain"
    assert result["verdict_from"] == "session"


def test_no_gain_when_it_does_not() -> None:
    result = ts.run(_events(70, 30, test_used=2), PRIORS, shuffles=20)
    unit = result["session"]
    assert unit["mrr_on"] < unit["mrr_off"]
    assert result["verdict"] == "no gain"


def test_too_little_data() -> None:
    assert ts.run(_events(20, 5), PRIORS, shuffles=5)["verdict"] == "too little data"
    assert ts.run([], {})["verdict"] == "too little data"


def test_the_off_arm_is_the_same_with_any_trust() -> None:
    a = ts.run(_events(70, 30), PRIORS, shuffles=10)["session"]
    b = ts.run(_events(70, 30, test_used=1), PRIORS, shuffles=10)["session"]
    assert a["mrr_off"] == b["mrr_off"]


def test_a_contradicted_use_is_not_relevant() -> None:
    events = _events(70, 30)
    events += [
        ts.Event(f"s{i:03d}", 1, "contradiction", T0 + timedelta(hours=i, minutes=6))
        for i in range(70, 100)
    ]
    assert ts.run(events, PRIORS, shuffles=5)["session"]["units"] == 0


def test_trust_order_is_the_hook_reorder() -> None:
    fits = {1: ts.Fit(0.9, 10, 0.5), 2: ts.Fit(0.5, 0, 0.5), 3: ts.Fit(0.5, 0, 0.5)}
    # rank 3 * 1.25 = 2/63 * 1.25 > 2/61: note 1 moves to the top
    assert ts.trust_order([2, 3, 1], fits) == [1, 2, 3]
    # far down the list the factor cannot lift it to the top
    base = list(range(10, 40)) + [1]
    fits.update({m: ts.Fit(0.5, 0, 0.5) for m in base if m != 1})
    assert ts.trust_order(base, fits)[0] == 10


def _log(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n[1]\n")
    return path


def test_per_prompt_test_from_the_hook_log(tmp_path: Path) -> None:
    events = _events(70, 30)
    cut = ts.pick_cut(events, 0.7)
    rows = [
        {
            "ts": (T0 + timedelta(hours=i, seconds=1)).isoformat(),
            "session": f"s{i:03d}",
            "base": [2, 3, 1, 4, 5],
        }
        for i in range(100)
    ]
    rows.append({"ts": "bad", "session": "s100", "base": [1]})
    rows.append({"ts": rows[0]["ts"], "session": 5, "base": [1]})
    log = ts.read_rank_log([_log(tmp_path / "trust-rank.jsonl", rows), tmp_path / "missing"])
    assert len(log) == 100
    result = ts.run(events, PRIORS, log=log, shuffles=5)
    unit = result["prompt"]
    assert unit["units"] == 30  # only the log lines of test sessions
    assert unit["mrr_off"] == pytest.approx(1 / 3, abs=1e-4)
    assert unit["mrr_on"] == pytest.approx(1.0)
    assert result["verdict_from"] == "prompt"
    assert result["verdict"] == "gain"
    assert cut is not None


def test_render_names_both_arms_and_the_default() -> None:
    text = ts.render(ts.run(_events(70, 30), PRIORS, shuffles=5))
    assert "MRR" in text and "off" in text and "on" in text
    assert "Verdict: gain" in text
    assert "Trust ranking stays off until you turn it on" in text


# -- the command ----------------------------------------------------------------


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    for key in ("CLAUDE_PLUGIN_DATA", "NOBLIVION_CONFIG", "NOBLIVION_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(folder))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))
    db.open_db(folder / "noblivion.db", create=True).close()
    return folder


def _fill(folder: Path) -> None:
    conn = db.open_db(folder / "noblivion.db")
    try:
        ids = [add_memory(conn, f"note_{i}", f"Rule number {i}.") for i in range(5)]
        for ev in _events(70, 30):
            batch = trust.Batch(
                session_id=ev.session,
                root=None,
                events=(
                    trust.Event(
                        trust.STORED_KIND.get(ev.kind, ev.kind),
                        db.format_ts(ev.ts),
                        mv_id=ids[ev.memory_id - 1],
                    ),
                ),
                rejected=0,
            )
            trust.store_batch(conn, batch)
    finally:
        conn.close()


def test_command_runs_on_a_store(data, capsys) -> None:
    _fill(data)
    assert cli.main(["trust", "timesplit", "--json", "--no-rank-log"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["test_sessions"] == 30
    assert result["session"]["units"] == 30
    assert result["verdict"] == "gain"
    assert result["prompt"] is None


def test_command_text_and_cut_option(data, capsys) -> None:
    _fill(data)
    assert cli.main(["trust", "timesplit", "--cut", "2026-06-03", "--k", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Trust time-split test: cut 2026-06-03T00:00:00Z")
    assert "hit@1" in out


def test_command_reads_the_default_rank_log(data, tmp_path, capsys) -> None:
    _fill(data)
    cache = tmp_path / "cache"
    cache.mkdir()
    rows = [
        {"ts": (T0 + timedelta(hours=i, seconds=1)).isoformat(), "session": f"s{i:03d}"}
        for i in range(70, 100)
    ]
    conn = db.open_db(data / "noblivion.db")
    ids = [r[0] for r in conn.execute("SELECT id FROM memories ORDER BY id")]
    conn.close()
    for row in rows:
        row["base"] = [ids[1], ids[2], ids[0], ids[3], ids[4]]
    _log(cache / "trust-rank.jsonl", rows)
    assert cli.main(["trust", "timesplit", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["prompt"]["units"] == 30
    assert result["verdict_from"] == "prompt"


def test_command_without_a_database(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "none"))
    assert cli.main(["trust", "timesplit"]) == 6


def test_command_refuses_a_bad_share(data) -> None:
    with pytest.raises(SystemExit):
        cli.main(["trust", "timesplit", "--train-share", "1.5"])


def test_trust_ranking_stays_off_by_default(tmp_path) -> None:
    env = {"NOBLIVION_CONFIG": str(tmp_path / "none.json"), "NOBLIVION_DATA_DIR": str(tmp_path)}
    assert config.load_store_settings(env).trust_ranking == "off"
    rank = load_hook("trust_rank", "trust_rank_t_timesplit_off")
    assert rank.mode(env) == rank.MODE_OFF


def test_docs_explain_the_command() -> None:
    text = (Path(__file__).resolve().parent.parent / "docs" / "trust.md").read_text()
    assert "noblivion trust timesplit" in text
    assert "stays off" in text
