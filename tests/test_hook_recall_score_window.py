# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recall hook: score-adaptive rows (NOBLIVION-151).

The row count follows the scores of the prompt:

1. NOTHING when no row reaches the floor (0.68) and the rescue does not apply.
2. THE RESCUE: the store's rank 1 stays when it is at most 0.02 under the floor
   and shares an informative word with the prompt.
3. THE WINDOW (off by default): a row stays only within ``recall.score_window`` of the top
   cosine. The cut to k stays a ceiling: at most 5 rows.
4. THE PULL PATH: the store's top hit from cosine 0.60 up is returned under the
   floor too, marked ``weak match``.

The floor exemption of NOBLIVION-130 is gone. Every test runs on a tmp data dir,
cache folder and memory folder, and the store call is replaced, so no live file
is read or written.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from hookload import load_hook
from recall_helpers import hook_env

TEST_CLASSIFICATION = "coherent"
TEST_CLASSIFICATION_REASON = (
    "drives the real index hook on a tmp cache folder and a tmp memory folder, "
    "with only the store call replaced"
)

hook = load_hook("recall_hook", "hooktest_recall_score_window")

SID = "score-window-test-session"
PROMPT = "deploy rollback"
FLOOR = hook.INDEX_MIN_SCORE_ENV
ROW_RE = re.compile(r"^- \[id (?P<id>\d+)\] ")


@pytest.fixture(autouse=True)
def _no_live_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOBLIVION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOBLIVION_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("NOBLIVION_GUARD_TABLE", str(tmp_path / "guard_table.json"))
    monkeypatch.setenv("NOBLIVION_RECALL_CACHE_DIR", str(tmp_path / "cache"))


def _write(folder: Path, name: str, shared: bool) -> None:
    word = " deploy rollback" if shared else ""
    (folder / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\nmetadata:\n  type: feedback\n"
        f"rule: Keep {name} in mind.\napply: {json.dumps('check ' + name + ' first')}\n"
        f"---\n\nBody of {name}{word}.\n"
    )


def _corpus(tmp_path: Path, names: dict[str, bool]) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir()
    for name, shared in names.items():
        _write(folder, name, shared)
    for i in range(6):  # filler, so the query words are rare in the corpus
        _write(folder, f"feedback_filler{i}", False)
    return folder


def _serve_rows(monkeypatch, tmp_path, rows, shared=(), prompt=PROMPT, **extra):
    """Run the hook on ``rows`` ((name, cosine) in the store's order).
    ``shared``: the names whose file holds the prompt words. Returns the output."""
    names = {name: name in shared for name, _score in rows}
    corpus = _corpus(tmp_path, names)

    def fake(query, k, environ=None, *args, **kwargs):
        return [
            hook.IndexLine(rank=pos, mid=19000 + pos, title=name, summary="", score=score)
            for pos, (name, score) in enumerate(rows, start=1)
        ][:k]

    monkeypatch.setattr(hook, "recall_index", fake)
    env = hook_env(
        tmp_path,
        NOBLIVION_GUARD_TABLE=str(tmp_path / "guard_table.json"),
        NOBLIVION_RECALL_INDEX="1",
        **{
            FLOOR: "0.68",
            hook.MEMORY_DIR_ENV: str(corpus),
            hook.INDEX_RULE_ROWS_ENV: "1",
            hook.INDEX_RERANK_ENV: "1",
            hook.SCORE_WINDOW_ENV: "0.05",
            **extra,
        },
    )
    payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id=SID, prompt=prompt))
    out = io.StringIO()
    hook.run(payload, out, env)
    return out.getvalue().rstrip("\n")


def _names(text: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].rstrip(")") for ln in text.splitlines() if ROW_RE.match(ln)]


def _status(tmp_path) -> str:
    log = (tmp_path / "cache" / "recall.log").read_text().strip().splitlines()
    return log[-1].rsplit(" ", 1)[1]


# ── nothing under the floor ─────────────────────────────────────────────────


def test_a_prompt_with_the_top_cosine_under_the_floor_shows_no_rows(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.64), ("feedback_b", 0.63), ("feedback_c", 0.60)]
    # no shared word: the rescue does not apply to rank 1 at 0.64 either
    text = _serve_rows(monkeypatch, tmp_path, rows)
    assert _names(text) == []


# ── the rescue ──────────────────────────────────────────────────────────────


def test_rank_1_at_most_0_02_under_the_floor_with_a_shared_word_shows(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.67), ("feedback_b", 0.60)]
    text = _serve_rows(monkeypatch, tmp_path, rows, shared={"feedback_a"})
    assert _names(text) == ["feedback_a"]
    assert ":rescue1" in _status(tmp_path)


def test_rank_1_under_the_floor_without_a_shared_word_does_not_show(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.67), ("feedback_b", 0.60)]
    assert _names(_serve_rows(monkeypatch, tmp_path, rows)) == []
    assert ":rescue" not in _status(tmp_path)


def test_rank_1_more_than_0_02_under_the_floor_is_not_rescued(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.65), ("feedback_b", 0.60)]
    assert _names(_serve_rows(monkeypatch, tmp_path, rows, shared={"feedback_a"})) == []


def test_only_the_store_rank_1_is_rescued(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.90), ("feedback_b", 0.67)]
    text = _serve_rows(
        monkeypatch, tmp_path, rows, shared={"feedback_b"}, **{hook.SCORE_WINDOW_ENV: "0"}
    )
    assert _names(text) == ["feedback_a"]


def test_a_word_of_more_than_half_the_notes_is_no_shared_word(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.67)]
    text = _serve_rows(monkeypatch, tmp_path, rows, prompt="keep in mind the body of it")
    assert _names(text) == []


def test_without_the_rerank_nothing_is_rescued(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.67)]
    text = _serve_rows(
        monkeypatch, tmp_path, rows, shared={"feedback_a"}, **{hook.INDEX_RERANK_ENV: "0"}
    )
    assert _names(text) == []


def test_rescue_ids_unit():
    def line(mid, score, lexical):
        ln = hook.IndexLine(rank=mid, mid=mid, title=f"t{mid}", summary="", score=score)
        ln.lexical = lexical
        return ln

    assert hook.rescue_ids([], 0.68) == set()
    assert hook.rescue_ids([line(1, 0.66, 0.4)], 0.68) == {1}
    assert hook.rescue_ids([line(1, 0.66, 0.0)], 0.68) == set()
    assert hook.rescue_ids([line(1, 0.66, None)], 0.68) == set()
    assert hook.rescue_ids([line(1, 0.70, 0.4)], 0.68) == set(), "at the floor already"
    assert hook.rescue_ids([line(1, None, 0.4)], 0.68) == set()
    # not rank 1: the first of the list is the only one that counts
    assert hook.rescue_ids([line(1, 0.95, 0.4), line(2, 0.67, 0.4)], 0.68) == set()


# ── the window ──────────────────────────────────────────────────────────────


def test_a_row_stays_only_within_the_window_of_the_top_cosine(monkeypatch, tmp_path):
    rows = [
        ("feedback_a", 0.80),
        ("feedback_b", 0.76),
        ("feedback_c", 0.74),
        ("feedback_d", 0.70),
    ]
    text = _serve_rows(monkeypatch, tmp_path, rows)
    assert _names(text) == ["feedback_a", "feedback_b"]
    assert ":window2of4" in _status(tmp_path)


def test_the_window_edge_is_inside(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.80), ("feedback_b", 0.75), ("feedback_c", 0.7499)]
    assert _names(_serve_rows(monkeypatch, tmp_path, rows)) == ["feedback_a", "feedback_b"]


def test_a_wider_window_keeps_more_rows(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.80), ("feedback_b", 0.74), ("feedback_c", 0.70)]
    text = _serve_rows(monkeypatch, tmp_path, rows, **{hook.SCORE_WINDOW_ENV: "0.10"})
    assert _names(text) == ["feedback_a", "feedback_b", "feedback_c"]


def test_the_default_shows_rows_far_under_the_top_cosine(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.95), ("feedback_b", 0.70)]
    text = _serve_rows(monkeypatch, tmp_path, rows, **{hook.SCORE_WINDOW_ENV: ""})
    assert _names(text) == ["feedback_a", "feedback_b"]


def test_a_window_of_zero_is_no_window(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.95), ("feedback_b", 0.70)]
    text = _serve_rows(monkeypatch, tmp_path, rows, **{hook.SCORE_WINDOW_ENV: "0"})
    assert _names(text) == ["feedback_a", "feedback_b"]
    assert ":window" not in _status(tmp_path)


def test_no_prompt_shows_more_than_5_rows(monkeypatch, tmp_path):
    rows = [(f"feedback_r{i}", 0.80 - i * 0.002) for i in range(9)]
    text = _serve_rows(monkeypatch, tmp_path, rows, **{hook.PROMPT_ROWS_ENV: "5"})
    assert len(_names(text)) == 5


def test_a_rescued_row_far_under_the_top_cosine_leaves_through_the_window(monkeypatch, tmp_path):
    rows = [("feedback_a", 0.67), ("feedback_b", 0.90)]
    text = _serve_rows(monkeypatch, tmp_path, rows, shared={"feedback_a"})
    assert _names(text) == ["feedback_b"]


def test_window_index_unit():
    def line(mid, score):
        return hook.IndexLine(rank=mid, mid=mid, title=f"t{mid}", summary="", score=score)

    lines = [line(1, 0.9), line(2, 0.86), line(3, None), line(4, 0.84)]
    kept, cut = hook.window_index(lines, 0.05)
    assert [int(ln.mid) for ln in kept] == [1, 2, 3] and cut == 1
    assert hook.window_index(lines, 0.0) == (lines, 0)
    only_none = [line(1, None), line(2, None)]
    assert hook.window_index(only_none, 0.05) == (only_none, 0)


# ── the setting ─────────────────────────────────────────────────────────────


def test_the_window_is_off_by_default_and_the_shipped_config_says_so():
    root = Path(__file__).resolve().parent.parent
    shipped = json.loads((root / "config" / "config.default.json").read_text())
    assert shipped["recall"]["score_window"] == hook.SCORE_WINDOW_DEFAULT == 0
    assert hook.score_window({}) == 0.0


def test_the_window_reads_the_config_key_and_the_env_wins(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"score_window": 0.04}}))
    env = {"NOBLIVION_CONFIG": str(cfg)}
    assert hook.score_window(env) == 0.04
    assert hook.score_window({**env, hook.SCORE_WINDOW_ENV: "0.06"}) == 0.06


@pytest.mark.parametrize("raw", ["x", "-0.1", "3", "nan", "inf"])
def test_a_malformed_window_is_the_default(raw):
    assert hook.score_window({hook.SCORE_WINDOW_ENV: raw}) == hook.SCORE_WINDOW_DEFAULT


def test_the_floor_exemption_is_gone():
    assert not [n for n in dir(hook) if "exempt" in n.lower()]


# ── the pull path ───────────────────────────────────────────────────────────

SEP = "\n---\n"


def _entry(name: str) -> str:
    return f"# {name}\n\nthe {name} note\n\n[claude_code_md: {name}.md]\n\nbody of {name}"


def _payload(scores):
    blob = SEP.join(_entry(n) for n in ("alpha", "beta", "gamma"))
    return {
        "results": [blob],
        "scores": scores,
        "namespace": "claude_code",
        "model": hook.THRESHOLD_MODEL,
    }


ENV = {"NOBLIVION_RECALL_ROOT": "r"}


def test_the_pull_path_returns_a_top_hit_at_0_60_as_a_weak_match(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.61, 0.50, 0.40]))
    hits = hook.recall("q", 5, ENV, md_only=True, weak_match=True)
    assert [h.title for h in hits] == ["alpha"] and hits[0].weak
    assert hook.render(hits, 1).splitlines()[1].endswith("(score 0.61, weak match)")


def test_the_pull_path_returns_nothing_under_0_60(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.59, 0.50, 0.40]))
    assert hook.recall("q", 5, ENV, md_only=True, weak_match=True) == []


def test_a_top_hit_over_the_floor_is_not_marked(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.80, 0.70, 0.40]))
    hits = hook.recall("q", 5, ENV, md_only=True, weak_match=True)
    assert [h.title for h in hits] == ["alpha", "beta"] and not any(h.weak for h in hits)


def test_the_hook_path_does_not_return_a_weak_match(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.61, 0.50, 0.40]))
    assert hook.recall("q", 5, ENV, md_only=True) == []


def test_a_weak_top_hit_goes_before_the_hits_over_the_floor(monkeypatch):
    monkeypatch.setattr(hook, "store_get", lambda *_a, **_k: _payload([0.64, 0.70, 0.40]))
    hits = hook.recall("q", 5, ENV, md_only=True, weak_match=True)
    assert [h.title for h in hits] == ["alpha", "beta"] and hits[0].weak and not hits[1].weak
