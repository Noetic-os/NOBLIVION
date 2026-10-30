# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tools/eval/heldout.py (NOBLIVION-144), on made-up transcripts and notes."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from noblivion import db
from noblivion.redaction import redact_at_rest
from store_helpers import add_memory

TOOLS = Path(__file__).resolve().parent.parent / "tools"
HOOKS = TOOLS.parent / "hooks"
SECRET = "password=hunter2widget9"


def _load(path: Path, alias: str):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


ho = _load(TOOLS / "eval" / "heldout.py", "heldout_under_test")
rt = _load(TOOLS / "replay_transcripts.py", "heldout_test_transcripts")
signals = _load(HOOKS / "trust_signals.py", "heldout_test_signals")


def _when(n: int) -> str:
    start = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    return (start + _dt.timedelta(minutes=n)).isoformat().replace("+00:00", "Z")


def _user(text: str, n: int) -> dict:
    return {"type": "user", "timestamp": _when(n), "message": {"role": "user", "content": text}}


def _reply(text: str, n: int, task: str = "") -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if task:
        content.append({"type": "tool_use", "name": "Agent", "input": {"prompt": task}})
    return {
        "type": "assistant",
        "timestamp": _when(n),
        "message": {"role": "assistant", "content": content},
    }


def _note(folder: Path, name: str, body: str) -> None:
    (folder / f"{name}.md").write_text(body, encoding="utf-8")


def _world(tmp_path: Path) -> SimpleNamespace:
    """Two projects, 40 candidates of each stratum in 30 sessions, one note store."""
    projects = tmp_path / "projects"
    home_dir = projects / "-home-user"
    other_dir = projects / "-home-user-gadget"
    memory = home_dir / "memory"
    memory.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    _note(memory, "MEMORY", "- [widget checks](feedback_widget_checks.md)\n")
    _note(memory, "feedback_widget_checks", "---\nrule: Run the widget checks.\n---\nwidget\n")
    _note(memory, "feedback_only_file", "---\nrule: Lamp lamp lamp.\n---\nwidget lamp\n")
    records: dict[Path, list[dict]] = {}
    for n in range(40):
        path = home_dir / f"s{n % 15:02d}.jsonl"
        recs = records.setdefault(path, [])
        recs.append(_user(f"please repair widget number {n} with care", n * 10))
        recs.append(
            _reply(f"I repaired the widget {n}.", n * 10 + 1, f"check the lamp in room {n}")
        )
        recs.append(_user(f"/home/user/plans/step{n}.md", n * 10 + 2))
        note = f"<task-notification><summary>job {n} done</summary></task-notification>"
        recs.append(_user(note, n * 10 + 3))
        recs.append(_user(f"no, that is wrong for widget {n}, I said blue", n * 10 + 4))
        other = other_dir / f"t{n % 15:02d}.jsonl"
        records.setdefault(other, []).append(_user(f"tune the gadget model {n} {SECRET}", n * 10))
    for path, recs in records.items():
        path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    conn = db.open_db(tmp_path / "noblivion.db", create=True)
    ids = {
        name: add_memory(conn, name, body, root="-home-user", index_text=body)
        for name, body in (
            ("feedback_widget_checks", "Run the widget checks."),
            ("feedback_only_store", "Keep the lamp clean."),
        )
    }
    conn.close()
    notes = ho.Notes(tmp_path / "noblivion.db")
    errors = [
        {"decision": "show", "query": f"error {n}", "ids": ["feedback_widget_checks"]}
        for n in range(60)
    ]
    guards = [
        {"text": f"run tool {n}", "label_rows": {"feedback_only_store": ["lamp"]}}
        for n in range(70)
    ]
    paths = rt.main_transcripts([str(projects / "*" / "*.jsonl")])
    prompts = list(ho.scan(paths, "-home-user", rt, signals, None))
    return SimpleNamespace(
        projects=projects,
        ids=ids,
        inputs=ho.Inputs(
            prompts=prompts,
            errors=errors,
            guards=guards,
            projects=projects,
            notes=notes,
            signals=signals,
            home=tmp_path / "nothing",
        ),
    )


def _rows(world: SimpleNamespace):
    return lambda prompt, sid: list(world.ids.values())


def _built(tmp_path: Path) -> tuple[Path, SimpleNamespace]:
    world = _world(tmp_path)
    groups, keys, task_key, _stats = ho.make_set(world.inputs, _rows(world))
    out = ho.private_dir(tmp_path / "set")
    ho.write_set(out, groups, keys, task_key, {"seed": ho.SEED}, ho.SEED)
    return out, world


def _fill(out: Path, value: str = "y") -> None:
    """Answer every block: ``y`` marks all notes of a block, ``n`` none."""
    path = out / ho.LABEL_FILE
    text = path.read_text(encoding="utf-8")
    for g in ho.load_groups(out):
        if g["items"]:
            size = len(g["items"])
            answer = "n" if value == "n" else ("y" if size == 1 else ho.LETTERS[:size])
            text = text.replace(
                f"Answer: {ho.FIELD} #{g['field']}", f"Answer: {answer} #{g['field']}"
            )
    path.write_text(text, encoding="utf-8")


def _blocks(out: Path) -> list[dict]:
    return [g for g in ho.load_groups(out) if g["items"]]


# -- the stock simulation -----------------------------------------------------------


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "memory"
    folder.mkdir()
    return folder


def test_index_cut_holds_200_lines_and_25_kb():
    assert ho.index_cut("\n".join(["x"] * 300)).count("\n") == 199
    assert len(ho.index_cut("y" * 40000).encode()) == 25000


def test_a_note_linked_past_the_index_cut_is_not_opened_by_its_link(tmp_path):
    folder = _folder(tmp_path)
    filler = "\n".join(f"- line {n}" for n in range(ho.INDEX_LINES))
    _note(folder, "MEMORY", f"{filler}\n- [gamma](feedback_far.md)\n")
    _note(folder, "feedback_far", "no shared content here\n")
    assert ho.StockFolder(folder).top("gamma") == []


def test_links_open_three_files_at_most_and_the_search_adds_five(tmp_path):
    folder = _folder(tmp_path)
    index = "".join(f"- [gamma](feedback_link{n}.md)\n" for n in range(5))
    _note(folder, "MEMORY", index)
    for n in range(5):
        _note(folder, f"feedback_link{n}", "plain text\n")
    for n in range(7):
        _note(folder, f"feedback_word{n}", "gamma delta\n" if n < 2 else "gamma\n")
    top = ho.StockFolder(folder).top("gamma delta")
    assert len(top) == ho.TOP
    assert top[:2] == ["feedback_word0.md", "feedback_word1.md"]
    assert sum(1 for name in top if "link" in name) == 0


def test_a_topic_page_gives_its_links_and_is_never_a_hit(tmp_path):
    folder = _folder(tmp_path)
    _note(folder, "MEMORY", "- [notes](topic_all.md)\n")
    _note(folder, "topic_all", "- [gamma rule](feedback_a.md)\n- [other](feedback_b.md)\n")
    _note(folder, "feedback_a", "plain text\n")
    _note(folder, "feedback_b", "plain text\n")
    assert ho.StockFolder(folder).top("gamma") == ["feedback_a.md"]


def test_the_search_counts_distinct_words_as_substrings(tmp_path):
    folder = _folder(tmp_path)
    _note(folder, "MEMORY", "")
    _note(folder, "feedback_one", "deploying the widgets\n")
    _note(folder, "feedback_two", "deploy deploy deploy\n")
    assert ho.StockFolder(folder).top("deploy widget") == ["feedback_one.md", "feedback_two.md"]


def test_a_named_note_opens_first_and_a_later_note_not_at_all(tmp_path):
    folder = _folder(tmp_path)
    _note(folder, "MEMORY", "")
    _note(folder, "feedback_one", "gamma gamma delta\n")
    _note(folder, "feedback_two", "plain text\n")
    stock = ho.StockFolder(folder)
    assert stock.top("gamma delta feedback_two.md")[0] == "feedback_two.md"
    assert stock.top("gamma", lambda name: name != "feedback_one.md") == []


def test_stop_words_and_short_words_are_no_content_words():
    assert ho.content_words("the widget with a lamp and 1234") == {"widget", "lamp"}


# -- the sample ---------------------------------------------------------------------


def test_the_set_has_the_ordered_counts_and_split(tmp_path):
    world = _world(tmp_path)
    groups, keys, task_key, stats = ho.make_set(world.inputs, _rows(world))
    counts = ho.counts_of(groups)
    assert {cls: c["groups"] for cls, c in counts.items()} == {
        "prompt": 150,
        "error": 40,
        "label": 50,
        "correction": 50,
    }
    assert len(keys["dev"]) == 50 and len(keys["sealed"]) == 100
    assert not set(keys["dev"]) & set(keys["sealed"])
    assert {k["stratum"] for k in keys["dev"].values()} == set(ho.STRATA)
    assert all(stats["strata"][s]["prompts"] == ho.PER_STRATUM for s in ho.STRATA)
    assert sum(1 for v in task_key.values() if "why" in v) == 50


def test_a_seed_gives_the_same_set_twice(tmp_path):
    world = _world(tmp_path)
    assert ho.make_set(world.inputs, _rows(world))[0] == ho.make_set(world.inputs, _rows(world))[0]


def test_a_session_stays_on_one_side_of_the_split():
    moment = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    prompts = [
        ho.Prompt(ho.STRATA[n % 5], f"text {n}", f"s{n % 40}", "p", moment) for n in range(150)
    ]
    sides = ho.split_prompts(prompts, ho.SEED)
    assert sorted(sides.values()).count("dev") == 50
    seen: dict[str, set[str]] = {}
    for p in prompts:
        seen.setdefault(p.session, set()).add(sides[id(p)])
    assert all(len(v) == 1 for v in seen.values())


def test_a_short_stratum_stops_the_build(tmp_path):
    world = _world(tmp_path)
    world.inputs.prompts = [p for p in world.inputs.prompts if p.stratum != "subagent_task"]
    with pytest.raises(SystemExit, match="subagent_task"):
        ho.make_set(world.inputs, _rows(world))


def test_before_keeps_only_older_prompts_and_all_strata_are_found(tmp_path):
    world = _world(tmp_path)
    assert {p.stratum for p in world.inputs.prompts} == set(ho.STRATA)
    paths = rt.main_transcripts([str(world.projects / "*" / "*.jsonl")])
    limit = _dt.datetime(2026, 1, 1, 1, tzinfo=_dt.timezone.utc)
    kept = list(ho.scan(paths, "-home-user", rt, signals, limit))
    assert kept and all(p.moment < limit for p in kept)
    assert len(kept) < len(world.inputs.prompts)


def test_correction_sample_marks_why_and_stays_apart_from_the_prompts(tmp_path):
    world = _world(tmp_path)
    taken = ho.pick_prompts(world.inputs.prompts, ho.SEED)
    picked = ho.pick_corrections(world.inputs.prompts, taken, ho.SEED, signals)
    assert len(picked) == ho.CORRECTIONS
    assert not {ho.norm(p.text) for p in taken} & {ho.norm(c.text) for c, _, _ in picked}
    assert {why for _, why, _ in picked} >= {"detector", "random"}


def test_error_shows_and_label_rows_lose_their_duplicates():
    row = {"decision": "show", "query": "Boom 1", "ids": ["a"]}
    shows = ho.distinct_error_shows([row, dict(row, query="boom 1"), {"decision": "no-hit"}])
    assert len(shows) == 1 and shows[0]["shows"] == 2
    call = {"text": "run x", "label_rows": {"a": ["x"], "b": ["x"]}}
    assert len(ho.distinct_label_rows([call, call, {"text": "y"}])) == 2


# -- blind labels -------------------------------------------------------------------


def test_a_note_of_both_arms_is_one_item(tmp_path):
    world = _world(tmp_path)
    moment = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    prompt = ho.Prompt("home", "repair the widget", "s", "-home-user", moment)
    doc, arms, unknown = ho.pool_prompt(
        prompt,
        "g",
        [world.ids["feedback_widget_checks"], 999999],
        ["feedback_widget_checks.md", "feedback_only_file.md"],
        world.inputs,
    )
    assert sorted(it["note"] for it in doc["items"]) == [
        "feedback_only_file",
        "feedback_widget_checks",
    ]
    assert arms == {
        "noblivion": ["feedback_widget_checks"],
        "stock_s2": ["feedback_widget_checks", "feedback_only_file"],
    }
    assert unknown == [999999]


def test_label_file_names_no_arm_and_no_split(tmp_path):
    out, _ = _built(tmp_path)
    text = (out / ho.LABEL_FILE).read_text(encoding="utf-8").lower().partition("## part 1")[2]
    assert text
    for word in ("stock", "noblivion", "sealed", " dev", "arm", "s2"):
        assert word not in text
    assert "noblivion" not in (out / ho.GROUPS).read_text(encoding="utf-8")


def test_the_order_of_a_pool_does_not_depend_on_the_arm(tmp_path):
    world = _world(tmp_path)
    moment = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    prompt = ho.Prompt("home", "repair the widget", "s", "-home-user", moment)
    store = world.ids["feedback_only_store"]
    file = ["feedback_widget_checks.md"]
    one = ho.pool_prompt(prompt, "g", [store], file, world.inputs)[0]
    two = ho.pool_prompt(prompt, "g", [world.ids["feedback_widget_checks"]], [], world.inputs)[0]
    swapped = ho.pool_prompt(
        prompt, "g", [world.ids["feedback_widget_checks"], store], [], world.inputs
    )[0]
    assert [i["id"] for i in one["items"]] == [i["id"] for i in swapped["items"]]
    assert two["items"][0]["id"] in {i["id"] for i in one["items"]}


def test_secrets_never_reach_the_set(tmp_path):
    assert SECRET not in redact_at_rest(SECRET)
    out, _ = _built(tmp_path)
    for name in (ho.LABEL_FILE, ho.GROUPS):
        assert "hunter2widget9" not in (out / name).read_text(encoding="utf-8")


def test_each_block_has_one_field_and_short_text(tmp_path):
    out, _ = _built(tmp_path)
    groups = ho.load_groups(out)
    text = (out / ho.LABEL_FILE).read_text(encoding="utf-8")
    assert text.count(ho.FIELD) == len(_blocks(out))
    assert all(len(g["text"]) <= ho.PROMPT_CHARS + 8 for g in groups)
    assert all(len(it["line"]) <= ho.PROMPT_CHARS for g in groups for it in g["items"])
    assert all(len(g["items"]) <= len(ho.LETTERS) for g in groups)


def test_a_prompt_with_an_empty_pool_has_no_block(tmp_path):
    world = _world(tmp_path)
    world.inputs.projects = tmp_path / "no-memory-here"
    groups, keys, task_key, stats = ho.make_set(world.inputs, lambda p, sid: [])
    assert stats["strata"]["home"]["empty"] == ho.PER_STRATUM
    text = ho.render(groups, tmp_path, ho.SEED)
    assert text.count(ho.FIELD) == sum(1 for g in groups if g["items"])


def test_a_marker_in_real_text_cannot_stop_the_find(tmp_path):
    assert ho.FIELD not in ho.safe("mail me @@ or @@@ soon")


def test_a_bare_path_prompt_shows_the_head_of_its_file(tmp_path):
    moment = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    plan = tmp_path / "plan.md"
    plan.write_text("fix the blue widget", encoding="utf-8")
    prompt = ho.Prompt("file_path", str(plan), "s", "p", moment)
    assert "blue widget" in ho.shown_text(prompt, tmp_path)
    assert "blue widget" in ho.stock_query(prompt, tmp_path)
    assert ho.named_file_head(str(plan), tmp_path / "elsewhere") == ""
    key = tmp_path / "token.md"
    key.write_text("hidden", encoding="utf-8")
    assert ho.named_file_head(str(key), tmp_path) == ""


def test_a_notification_shows_its_summary_not_its_tags():
    text = "<task-notification><task-id>x1</task-id><summary>job done</summary></task-notification>"
    assert ho.strip_notification(text) == "job done"
    assert "task-id" not in ho.strip_notification("<task-id>x1</task-id> plain")


# -- status, import and the sealed split --------------------------------------------


def _args(out: Path, **kw) -> SimpleNamespace:
    return SimpleNamespace(out=str(out), relabel=False, split="dev", open_sealed=False, **kw)


def test_status_counts_done_and_left(tmp_path, capsys):
    out, _ = _built(tmp_path)
    blocks = _blocks(out)
    path = out / ho.LABEL_FILE
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"Answer: {ho.FIELD} #", "Answer: n #", 3), encoding="utf-8")
    assert ho.status(_args(out)) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1].split()
    assert last[1] == str(len(blocks)) and last[3] == "3" and last[5] == str(len(blocks) - 3)


@pytest.mark.parametrize(
    ("value", "size", "want"),
    [
        ("n", 3, ["n", "n", "n"]),
        ("ac", 3, ["y", "n", "y"]),
        ("ca", 3, ["y", "n", "y"]),
        ("y", 1, ["y"]),
        ("a", 1, ["y"]),
        ("n", 1, ["n"]),
        ("y", 3, None),
        ("ad", 3, None),
        ("aa", 3, None),
        ("maybe", 3, None),
        ("", 3, None),
    ],
)
def test_answers(value, size, want):
    assert ho.parse_answer(value, size) == want


def test_a_block_answer_gives_one_label_per_note_and_takes_spaces_and_commas(tmp_path):
    groups = [
        {"field": "0a1b2c3d", "items": [{"id": "1"}, {"id": "2"}, {"id": "3"}]},
        {"field": "0a1b2c3e", "items": [{"id": "4"}]},
        {"field": "0a1b2c3f", "items": [{"id": "5"}]},
    ]
    path = tmp_path / "labels.md"
    path.write_text(
        "Answer: a, c #0a1b2c3d\nAnswer: y #0a1b2c3e\nAnswer: @@ #0a1b2c3f\n", encoding="utf-8"
    )
    labels, done, bad = ho.read_fields(path, groups)
    assert labels == {"1": "y", "2": "n", "3": "y", "4": "y"}
    assert done == {"0a1b2c3d", "0a1b2c3e"} and bad == []


def test_a_bad_field_is_named_and_not_taken(tmp_path, capsys):
    out, _ = _built(tmp_path)
    path = out / ho.LABEL_FILE
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"Answer: {ho.FIELD} #", "Answer: maybe #", 1), encoding="utf-8")
    assert ho.status(_args(out)) == 1
    assert "bad fields on lines" in capsys.readouterr().out
    assert ho.import_labels(_args(out)) == 1
    assert ho.load_labels(out) == {}


def test_a_doubled_block_counts_as_bad(tmp_path):
    line = "Answer: y #0a1b2c3d"
    path = tmp_path / "labels.md"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    groups = [{"field": "0a1b2c3d", "items": [{"id": "1"}]}]
    labels, done, bad = ho.read_fields(path, groups)
    assert labels == {} and done == set() and bad == [2]


def test_import_keeps_one_label_per_item(tmp_path, capsys):
    out, _ = _built(tmp_path)
    original = (out / ho.LABEL_FILE).read_text(encoding="utf-8")
    _fill(out, "y")
    assert ho.import_labels(_args(out)) == 0
    first = ho.load_labels(out)
    assert set(first.values()) == {"y"}
    assert len(first) == sum(len(g["items"]) for g in ho.load_groups(out))
    (out / ho.LABEL_FILE).write_text(original, encoding="utf-8")
    _fill(out, "n")
    assert ho.import_labels(_args(out)) == 1
    assert ho.load_labels(out) == first
    relabel = _args(out)
    relabel.relabel = True
    assert ho.import_labels(relabel) == 0
    assert set(ho.load_labels(out).values()) == {"n"}


def test_report_refuses_the_sealed_split_without_the_flag(tmp_path, capsys):
    out, _ = _built(tmp_path)
    _fill(out, "y")
    ho.import_labels(_args(out))
    capsys.readouterr()
    args = _args(out)
    args.split = "sealed"
    assert ho.report(args) == 2
    seen = capsys.readouterr()
    assert seen.out == "" and "refused" in seen.err
    assert not (out / ho.SEALED_LOG).exists()
    with pytest.raises(ho.SealedError):
        ho.load_split(out, "sealed")


def test_the_explicit_flag_opens_the_sealed_split_and_leaves_a_trace(tmp_path, capsys):
    out, _ = _built(tmp_path)
    _fill(out, "y")
    ho.import_labels(_args(out))
    capsys.readouterr()
    args = _args(out)
    args.split = "sealed"
    args.open_sealed = True
    assert ho.report(args) == 0
    assert json.loads(capsys.readouterr().out)["prompts"] == 100
    assert (out / ho.SEALED_LOG).exists()


def test_tuning_code_gets_the_dev_prompts_only(tmp_path):
    out, _ = _built(tmp_path)
    groups, key = ho.load_split(out)
    sealed = json.loads((out / ho.KEYS["sealed"]).read_text(encoding="utf-8"))
    assert len(groups) == 50 and all(g["cls"] == "prompt" for g in groups)
    assert not {g["group"] for g in groups} & set(sealed)
    assert set(key) == {g["group"] for g in groups}


def test_report_refuses_a_dev_split_with_labels_left(tmp_path, capsys):
    out, _ = _built(tmp_path)
    assert ho.report(_args(out)) == 2
    assert "no label" in capsys.readouterr().err


def test_arm_metrics_on_a_known_table():
    groups = [
        {"group": "a", "items": [{"id": "1", "note": "x"}, {"id": "2", "note": "y"}]},
        {"group": "b", "items": [{"id": "3", "note": "z"}]},
        {"group": "c", "items": []},
    ]
    key = {
        "a": {"arms": {"noblivion": ["x", "y"], "stock_s2": ["y"]}},
        "b": {"arms": {"noblivion": ["z"], "stock_s2": []}},
        "c": {"arms": {"noblivion": [], "stock_s2": []}},
    }
    labels = {"1": "y", "2": "n", "3": "n"}
    one = ho.arm_metrics(groups, key, labels, "noblivion")
    assert (one["delivery"]["hits"], one["delivery"]["n"]) == (1, 1)
    assert (one["precision"]["hits"], one["precision"]["n"]) == (1, 3)
    assert (one["false_delivery"]["hits"], one["false_delivery"]["n"]) == (1, 2)
    two = ho.arm_metrics(groups, key, labels, "stock_s2")
    assert two["delivery"]["hits"] == 0 and two["false_delivery"]["hits"] == 0


def test_the_detector_counts_come_from_the_task_key():
    groups = [
        {"group": "a", "cls": "correction", "items": [{"id": "1"}]},
        {"group": "b", "cls": "correction", "items": [{"id": "2"}]},
        {"group": "c", "cls": "correction", "items": [{"id": "3"}]},
    ]
    task_key = {"a": {"detector": True}, "b": {"detector": True}, "c": {"detector": False}}
    out = ho.task_metrics(groups, task_key, {"1": "y", "2": "n", "3": "y"})
    assert out["detector_precision"]["share"] == 0.5
    assert out["detector_recall_enriched_sample"]["share"] == 0.5


# -- private data -------------------------------------------------------------------


def test_folder_and_files_are_private(tmp_path):
    out, _ = _built(tmp_path)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_set_refuses_a_folder_inside_a_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(SystemExit, match="git work tree"):
        ho.private_dir(tmp_path / "deep" / "set")


def test_a_set_is_never_overwritten(tmp_path):
    out, _ = _built(tmp_path)
    args = SimpleNamespace(out=str(out))
    with pytest.raises(SystemExit, match="built once"):
        ho.build(args)


def test_the_scratch_copy_takes_the_allowlist_and_never_the_token(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    conn = db.open_db(data / "noblivion.db", create=True)
    conn.close()
    (data / "token").write_text("x", encoding="utf-8")
    (data / ".credentials.json").write_text("x", encoding="utf-8")
    (data / "config.json").write_text("{}", encoding="utf-8")
    (data / "models").mkdir()
    (data / "models" / "model.onnx").write_text("m", encoding="utf-8")
    target = tmp_path / "scratch"
    ho.copy_store_inputs(data, target)
    names = sorted(p.name for p in target.iterdir())
    assert names == ["config.json", "models", "noblivion.db"]
    assert stat.S_IMODE((target / "noblivion.db").stat().st_mode) == 0o600
    assert sorted(p.name for p in data.iterdir() if p.name == "token") == ["token"]


# -- the replay ---------------------------------------------------------------------

FAKE_HOOK = """
import json
def store_get(build, environ, timeout_s):
    return {"results": ["[claude_code_md: born_late.md]"]}
def session_env(env, payload):
    return dict(env)
def main(stdin, stdout, env):
    payload = json.loads(stdin.read())
    with open(env["NOBLIVION_RECALL_CACHE_DIR"] + "/recall.log", "a") as fh:
        fh.write(f"t event=x session=y {env.get('FAKE_LOG', 'ok:x')}\\n")
    shown = store_get(None, env, 1.0)["results"]
    if shown:
        stdout.write("HEADER\\n")
        for n in range(7):
            stdout.write(f"- [id {n + 1}] a rule (name{n})\\n  APPLY: x\\n")
"""
FAKE_SUB = """
import sys
rh = sys.modules["recall_hook"]
def _rules_for(text, label, sid, t0, env):
    return "HEADER\\n- [id 42] a rule (subname)\\n"
"""


def _replay(tmp_path: Path, births=None, env=None) -> object:
    hooks = tmp_path / "fake_hooks"
    hooks.mkdir()
    (hooks / "recall_hook.py").write_text(FAKE_HOOK, encoding="utf-8")
    (hooks / "subagent_rules_hook.py").write_text(FAKE_SUB, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return ho.Replay(hooks, env or {}, work, tmp_path / "projects", births, rt)


MOMENT = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


def test_replay_gives_the_row_ids_cut_to_the_top_five(tmp_path):
    replay = _replay(tmp_path)
    prompt = ho.Prompt("home", "repair the widget", "s", "-home-user", MOMENT)
    assert replay.rows(prompt, "heldout-a") == [1, 2, 3, 4, 5]
    replay.check()


def test_replay_of_a_subagent_task_uses_the_subagent_hook(tmp_path):
    replay = _replay(tmp_path)
    prompt = ho.Prompt("subagent_task", "repair the widget", "s", "-home-user", MOMENT)
    assert replay.rows(prompt, "heldout-a") == [42]


def test_replay_drops_a_note_born_after_the_prompt(tmp_path):
    births = {"born_late.md": MOMENT + _dt.timedelta(days=1)}
    replay = _replay(tmp_path, births)
    prompt = ho.Prompt("home", "repair the widget", "s", "-home-user", MOMENT)
    assert replay.rows(prompt, "heldout-a") == []
    later = ho.Prompt("home", "repair it", "s", "-home-user", MOMENT + _dt.timedelta(days=2))
    assert len(replay.rows(later, "heldout-b")) == ho.TOP


def test_a_failed_hook_call_stops_the_build(tmp_path):
    replay = _replay(tmp_path, env={"FAKE_LOG": "fail:timeout"})
    prompt = ho.Prompt("home", "repair the widget", "s", "-home-user", MOMENT)
    replay.rows(prompt, "heldout-a")
    with pytest.raises(SystemExit, match="failed open"):
        replay.check()


def test_replay_leaves_the_real_recall_hook_module_alone(tmp_path):
    marker = object()
    sys.modules["recall_hook"] = marker
    try:
        _replay(tmp_path)
        assert sys.modules["recall_hook"] is marker
    finally:
        sys.modules.pop("recall_hook", None)


def test_scope_sends_the_memory_folder_or_the_root(tmp_path):
    replay = _replay(tmp_path)
    (tmp_path / "projects" / "-with" / "memory").mkdir(parents=True)
    assert replay._scope("-with")["NOBLIVION_MEMORY_DIR"].endswith("/-with/memory")
    bare = replay._scope("-without")
    assert bare["NOBLIVION_RECALL_ROOT"] == "-without" and "NOBLIVION_MEMORY_DIR" not in bare
