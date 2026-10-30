# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scale fixture generator (NOBLIVION-146): counts, shapes, frozen queries.

The hooks read these notes in the long-run test, so the tests use the hooks' own
parsers on the output: a guard rule that the table skips, or an error quote that
the gate refuses, would pass any check written here and still measure nothing.

The test runs on the Python 3.9 lane too, so byte identity is proved on 3.9, on
3.10 and 3.12, and on macOS, against one golden hash.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from hookload import load_hook

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "eval" / "scale_fixture.py"
WORDS = ROOT / "tools" / "eval" / "scale_fixture_words.json"
LESSONS = ROOT / "tools" / "eval" / "scale_fixture_lessons.json"
SEED = 20261030

_spec = importlib.util.spec_from_file_location("scale_fixture_t", TOOL)
assert _spec is not None and _spec.loader is not None
sf = importlib.util.module_from_spec(_spec)
sys.modules["scale_fixture_t"] = sf  # a dataclass resolves its annotations here
_spec.loader.exec_module(sf)


def memory_of(out: Path, size: int, shard: int = 0) -> list[Path]:
    return sorted((out / f"scale-{size:05d}" / f"shard-{shard:02d}" / "projects").glob("*/memory"))


def notes_in(folder: Path) -> list[Path]:
    return [p for p in folder.glob("*.md") if not p.name.startswith(("MEMORY", "topic_"))]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("fixture") / "out"
    sf.generate(folder, SEED, [30, 200, 1000, 5000])
    return folder


@pytest.fixture(scope="module")
def lessons(out) -> list[dict]:
    return read_json(out / "lessons.json")


@pytest.fixture(scope="module")
def queries(out) -> list[dict]:
    text = (out / "queries.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def small(tmp_path) -> Path:
    """A fixture of two scales, for a test that breaks it."""
    folder = tmp_path / "small"
    sf.generate(folder, SEED, [30, 200])
    return folder


# -- size, determinism, the output folder ----------------------------------------


def test_each_size_is_exact_and_verifies(out):
    assert sf.verify_output(out) == []
    for size, scale in sf.SCALES.items():
        if size == 20000:
            continue
        for shard in range(scale.shards):
            folders = memory_of(out, size, shard)
            assert len(folders) == scale.projects
            assert sum(len(notes_in(f)) for f in folders) == size


def test_project_sizes_follow_the_plan_table():
    for size in (5000, 20000):
        scale = sf.SCALES[size]
        sizes = sf.project_sizes(scale)
        assert len(sizes) == scale.projects and sum(sizes) == size
        assert sizes == sorted(sizes, reverse=True)
        # Zipf with exponent 1: rank r holds 1/r of the top project.
        assert sizes[0] / sizes[1] == pytest.approx(2, abs=0.01)
        assert sizes[0] / sizes[-1] == pytest.approx(scale.projects, rel=0.01)
    assert (sf.SCALES[5000].projects, sf.SCALES[20000].projects) == (25, 50)


def test_every_lesson_is_in_exactly_one_shard(out):
    for size in (30, 200):
        scale = sf.SCALES[size]
        detail = read_json(out / scale.folder / "scale.json")
        assert len(detail["lessons"]) == 60
        per_shard = [0] * scale.shards
        for row in detail["lessons"].values():
            per_shard[row["shard"]] += 1
        assert per_shard == [scale.lessons_per_shard] * scale.shards


def test_same_seed_same_bytes_other_seed_other_bytes(tmp_path):
    first, second, other = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    sf.generate(first, SEED, [30, 200])
    sf.generate(second, SEED, [30, 200])
    sf.generate(other, SEED + 1, [30])
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    for name in files:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    assert (other / "lessons.json").read_bytes() != (first / "lessons.json").read_bytes()


def test_golden_hash_holds_on_every_lane(tmp_path):
    """The same bytes on each Python and each system. A change to the generator
    or its frozen files changes this hash: that is a new fixture, not a fix."""
    manifest = sf.generate(tmp_path / "golden", SEED, [30])
    assert manifest["scales"]["30"]["tree_sha256"] == (
        "d3d172fc82152e97619baaa87d60dd362b0fc4bae070421610ca36b39521cc8b"
    )
    assert (
        manifest["queries_sha256"]
        == "dadf79b34a0eb9754b1a0aaf16c03a85815fa4470e8ba1a86d88cd54cbd0e12e"
    )


def test_frozen_inputs_stay_frozen():
    assert (
        hashlib.sha256(WORDS.read_bytes()).hexdigest()
        == "2bd8b8341da18321ee62a484ac029072825bc0c74b48ac213a6594896f9ee920"
    )
    assert (
        hashlib.sha256(LESSONS.read_bytes()).hexdigest()
        == "3124ee9bfd4198b9f555f64caaf807649b2ea25d80c9ac8d936cf69e25e8b72b"
    )


def test_output_holds_no_output_path(out):
    needle = str(out).encode()
    for name in ("manifest.json", "lessons.json", "queries.jsonl"):
        assert needle not in (out / name).read_bytes()
    assert needle not in (out / "scale-00030" / "scale.json").read_bytes()


def test_output_folder_inside_the_repo_is_refused():
    with pytest.raises(SystemExit, match="outside"):
        sf.generate(ROOT / "tools" / "eval" / "out", SEED, [30])
    assert not (ROOT / "tools" / "eval" / "out").exists()


def test_output_folder_in_the_claude_folder_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(sf.Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(SystemExit, match="outside"):
        sf.generate(tmp_path / ".claude" / "projects" / "x", SEED, [30])


def test_output_folder_with_files_is_refused(tmp_path):
    (tmp_path / "kept.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="not empty"):
        sf.generate(tmp_path, SEED, [30])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept.txt"]


def test_main_needs_a_folder_and_a_known_scale(tmp_path):
    with pytest.raises(SystemExit):
        sf.main([])
    with pytest.raises(SystemExit):
        sf.main(["--out", str(tmp_path / "x"), "--scales", "31"])


def test_verify_of_a_folder_with_no_fixture_reports_it(tmp_path):
    assert "no manifest.json" in sf.verify_output(tmp_path)[0]


def test_main_builds_and_verifies(tmp_path, capsys):
    folder = tmp_path / "x"
    assert sf.main(["--out", str(folder), "--scales", "30"]) == 0
    assert sf.main(["--verify", str(folder)]) == 0
    assert "verify: 0 problems" in capsys.readouterr().out


# -- the planted lessons ---------------------------------------------------------


def test_sixty_lessons_of_the_planned_kinds(lessons):
    counts: dict[str, int] = {}
    for row in lessons:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    assert counts == {
        "prompt": 20,
        "guard": 10,
        "credential": 5,
        "error": 10,
        "subagent": 5,
        "continuity": 5,
        "label": 5,
    }
    answers = [row["answer"] for row in lessons]
    assert len(set(answers)) == 60
    assert all(re.fullmatch(r"--[a-z]+-[a-z]+=\d{5}", a) for a in answers)


def test_each_lesson_names_its_notes_and_queries(lessons, queries):
    known = {q["id"] for q in queries}
    for row in lessons:
        assert len(row["near_miss_notes"]) == 3
        assert row["target_note"] and row["duplicate_note"]
        assert row["query_ids"] and set(row["query_ids"]) <= known


def test_lessons_are_the_same_bytes_at_every_scale(out, lessons):
    by_name = {}
    for size in (30, 200, 1000, 5000):
        detail = read_json(out / f"scale-{size:05d}" / "scale.json")
        for row in lessons:
            home = detail["lessons"][row["id"]]
            folders = memory_of(out, size, home["shard"])
            for name in [row["target_note"], row["duplicate_note"], *row["near_miss_notes"]]:
                found = [f / name for f in folders if (f / name).is_file()]
                assert len(found) == 1, (size, name)
                data = found[0].read_bytes()
                assert by_name.setdefault(name, data) == data, (size, name)


def test_near_miss_shares_the_topic_but_not_the_fact(out, lessons):
    folders = memory_of(out, 1000)
    for row in lessons:
        target = next(f / row["target_note"] for f in folders if (f / row["target_note"]).exists())
        topic = set(row["topic"].lower().split())
        for name in row["near_miss_notes"]:
            near = next(f / name for f in folders if (f / name).exists())
            text = near.read_text(encoding="utf-8")
            assert row["answer"] not in text
            assert topic <= set(re.findall(r"[a-z0-9]+", text.lower()))
            assert text != target.read_text(encoding="utf-8")


def test_duplicate_holds_the_fact_in_a_near_copy(out, lessons):
    folders = memory_of(out, 1000)
    for row in lessons:
        duplicate = row["duplicate_note"]
        path = next(f / duplicate for f in folders if (f / duplicate).exists())
        text = path.read_text(encoding="utf-8")
        target_path = path.parent / row["target_note"]
        assert row["answer"] in text and row["answer"] in target_path.read_text(encoding="utf-8")
        assert path.parent == target_path.parent  # or stock would read the copy


def test_answer_shows_in_the_field_a_row_prints(out, lessons):
    """A row prints the rule, or the description of a note with no rule. An answer
    only in the body would be delivered and never seen."""
    fields = load_hook("memory_fields", "memory_fields_t_scale")
    folders = memory_of(out, 1000)
    for row in lessons:
        path = next(f / row["target_note"] for f in folders if (f / row["target_note"]).exists())
        text = path.read_text(encoding="utf-8")
        read = fields.read_fields(text)
        shown = read["rule"] or text.split("description: ", 1)[1].split("\n", 1)[0]
        assert row["answer"] in shown


def test_a_changed_note_is_found_by_verify(small):
    victim = sorted(notes_in(memory_of(small, 30)[0]))[0]
    victim.write_text(victim.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert any("hash differs" in p for p in sf.verify_output(small))


def test_answer_in_a_filler_note_is_found(small):
    rows = read_json(small / "lessons.json")
    detail = read_json(small / "scale-00030" / "scale.json")
    home = detail["lessons"]["L01"]
    folder = memory_of(small, 30, home["shard"])[0]
    planted = {n for r in rows for n in [r["target_note"], r["duplicate_note"]]}
    filler = next(p for p in sorted(notes_in(folder)) if p.name not in planted)
    filler.write_text(filler.read_text(encoding="utf-8") + rows[0]["answer"], encoding="utf-8")
    assert any("holds the answer" in p for p in sf.verify_output(small))


# -- stock-fail placement ---------------------------------------------------------


def test_small_is_the_negative_control(out):
    detail = read_json(out / "scale-00030" / "scale.json")
    assert {row["placement"] for row in detail["lessons"].values()} == {"linked"}
    for shard in range(20):
        folder = memory_of(out, 30, shard)[0]
        lines = (folder / "MEMORY.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 30
        linked = {re.search(r"\]\(([^)]+)\)", line).group(1) for line in lines}
        assert linked == {p.name for p in notes_in(folder)}


def test_medium_1_index_is_199_lines_then_overflow(out):
    detail = read_json(out / "scale-00200" / "scale.json")
    for shard in detail["shards_detail"]:
        main = shard["projects"][0]
        assert main["shape"] == "line_cap"
        assert main["index_lines"] > sf.LINE_CAP
    past = [row for row in detail["lessons"].values() if row["placement"] == "past_line_200"]
    assert len(past) >= 10
    for row in past:
        folder = memory_of(out, 200, row["shard"])[0]
        lines = (folder / "MEMORY.md").read_text(encoding="utf-8").split("\n")
        number = next(i for i, line in enumerate(lines, 1) if f"]({row['target']})" in line)
        assert number > sf.LINE_CAP
        # The line cap binds here, not the byte cap.
        assert len("\n".join(lines[: sf.LINE_CAP]).encode()) < sf.BYTE_CAP


def test_medium_2_has_indexes_that_reach_25_kb_before_line_200(out):
    detail = read_json(out / "scale-01000" / "scale.json")
    capped = [p for p in detail["shards_detail"][0]["projects"] if p["shape"] == "byte_cap"]
    assert len(capped) >= 2
    for project in capped:
        path = out / "scale-01000" / "shard-00" / "projects" / project["slug"] / "memory"
        lines = (path / "MEMORY.md").read_text(encoding="utf-8").split("\n")
        assert len("\n".join(lines[: sf.LINE_CAP]).encode()) > sf.BYTE_CAP
    assert any(p["topic_files"] for p in detail["shards_detail"][0]["projects"])
    past = [row for row in detail["lessons"].values() if row["placement"] == "past_25kb"]
    assert len(past) >= 5


def test_large_scale_offers_all_five_stock_fail_placements(out):
    detail = read_json(out / "scale-05000" / "scale.json")
    assert set(detail["placements"]) == set(sf._ALL_FIVE)
    shapes = {p["shape"] for p in detail["shards_detail"][0]["projects"]}
    assert {"line_cap", "byte_cap", "topic", "flat"} <= shapes


def test_long_index_lines_keep_clear_of_the_v3_words(out, lessons, queries):
    """C1 needs a prompt that shares no word with the index line. A byte-cap index
    has long lines, so its made-up hook text must keep to that too."""
    v3 = {q["lesson"]: sf.content_stems(q["prompt"]) for q in queries if q["variant"] == "V3"}
    detail = read_json(out / "scale-01000" / "scale.json")["lessons"]
    checked = 0
    for row in lessons:
        home = detail[row["id"]]
        if home["placement"] != "past_25kb":
            continue
        index = memory_of(out, 1000)
        text = "\n".join(
            (f / "MEMORY.md").read_text(encoding="utf-8")
            for f in index
            if f.parent.name == f"-work-{home['note_project']}"
        )
        line = next(x for x in text.split("\n") if f"]({row['target_note']})" in x)
        assert len(line) > 150
        assert not sf.shared_stems(v3[row["id"]], sf.content_stems(line))
        checked += 1
    assert checked >= 5


def test_answers_stay_out_of_every_index_and_topic_file(out, lessons):
    pattern = re.compile("|".join(re.escape(row["answer"]) for row in lessons))
    for size in (30, 200, 1000, 5000):
        for shard in range(sf.SCALES[size].shards):
            for folder in memory_of(out, size, shard):
                for path in [folder / "MEMORY.md", *folder.glob("topic_*.md")]:
                    assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_verify_finds_a_link_moved_into_the_loaded_index(small):
    detail = read_json(small / "scale-00200" / "scale.json")
    lid, row = next(
        (k, v) for k, v in detail["lessons"].items() if v["placement"] == "past_line_200"
    )
    folder = memory_of(small, 200, row["shard"])[0]
    index = folder / "MEMORY.md"
    text = index.read_text(encoding="utf-8")
    index.write_text(f"- [moved]({row['target']})\n" + text, encoding="utf-8")
    assert any(p.startswith(f"{lid}: past_line_200") for p in sf.verify_output(small))


def test_verify_finds_a_linked_unlinked_note(small):
    detail = read_json(small / "scale-00200" / "scale.json")
    lid, row = next((k, v) for k, v in detail["lessons"].items() if v["placement"] == "unlinked")
    folder = memory_of(small, 200, row["shard"])[0]
    index = folder / "MEMORY.md"
    text = index.read_text(encoding="utf-8")
    index.write_text(text + f"- [late]({row['target']})\n", encoding="utf-8")
    assert any(p.startswith(f"{lid}: unlinked") for p in sf.verify_output(small))


def test_verify_finds_a_missing_note(small):
    victim = sorted(notes_in(memory_of(small, 30)[0]))[0]
    victim.unlink()
    problems = sf.verify_output(small)
    assert any("the file is missing" in p for p in problems)
    assert any("notes, not" in p for p in problems)


# -- the survey ----------------------------------------------------------------


def _parse(path: Path) -> dict:
    """Read a note as the corpus survey reads it."""
    text = path.read_text(encoding="utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    out = {}
    for line in front.splitlines():
        match = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip()
    out["size"] = len(text)
    return out


@pytest.mark.parametrize("size", [1000, 5000])
def test_field_mix_follows_the_survey_and_the_record_is_true(out, size):
    detail = read_json(out / f"scale-{size:05d}" / "scale.json")
    rows = [_parse(p) for f in memory_of(out, size) for p in notes_in(f)]
    assert len(rows) == size
    for name, share in detail["field_mix"]["survey"].items():
        actual = sum(bool(row.get(name)) for row in rows) / size
        assert actual == pytest.approx(detail["field_mix"]["actual"][name], abs=1e-4)
        assert actual == pytest.approx(share, abs=0.01), name
    for row in rows:
        if row.get("rule"):
            assert row.get("apply") and row.get("scope")
        assert row.get("name") and row.get("description")
    for kind, share in sf.SURVEY["type_shares"].items():
        have = sum(row.get("type", "other") == kind for row in rows) / size
        assert have == pytest.approx(share, abs=0.02), kind


@pytest.mark.parametrize("size", [1000, 5000])
def test_note_sizes_follow_the_survey(out, size):
    sizes = sorted(_parse(p)["size"] for f in memory_of(out, size) for p in notes_in(f))
    assert sum(sizes) / size == pytest.approx(sf.SURVEY["size_mean_chars"], rel=0.05)
    assert sizes[size // 2] == pytest.approx(2518, rel=0.05)
    assert sizes[int(size * 0.95)] == pytest.approx(5509, rel=0.08)
    assert sizes[0] >= 480 and sizes[-1] <= 12800


def test_manifest_records_inputs_and_hashes(out):
    manifest = read_json(out / "manifest.json")
    assert manifest["seed"] == SEED
    assert manifest["generator_sha256"] == hashlib.sha256(TOOL.read_bytes()).hexdigest()
    assert manifest["words_sha256"] == hashlib.sha256(WORDS.read_bytes()).hexdigest()
    assert manifest["lessons_source_sha256"] == hashlib.sha256(LESSONS.read_bytes()).hexdigest()
    assert manifest["queries_total"] == 620
    for size, row in manifest["scales"].items():
        listing = (out / f"scale-{int(size):05d}" / "files.sha256").read_bytes()
        assert hashlib.sha256(listing).hexdigest() == row["tree_sha256"]


# -- the frozen queries ----------------------------------------------------------


def test_query_counts_match_the_plan(queries):
    counts: dict[str, int] = {}
    for query in queries:
        counts[query["variant"]] = counts.get(query["variant"], 0) + 1
    assert counts == {
        "V1": 60,
        "V2": 60,
        "V3": 60,
        "V4": 60,
        "V5": 100,
        "V6": 60,
        "V7": 60,
        "CMD": 60,
        "CRED": 30,
        "ERR": 30,
        "SUB": 10,
        "CONT": 15,
        "LABEL": 15,
    }
    assert len({q["id"] for q in queries}) == len(queries)


def test_v2_shares_no_content_stem_with_its_note(out, lessons, queries):
    folders = memory_of(out, 1000)
    v2 = {q["lesson"]: q["prompt"] for q in queries if q["variant"] == "V2"}
    for row in lessons:
        for name in (row["target_note"], row["duplicate_note"]):
            path = next(f / name for f in folders if (f / name).exists())
            content = sf.note_content(name, path.read_text(encoding="utf-8"))
            assert not sf.shared_stems(sf.content_stems(v2[row["id"]]), sf.content_stems(content))


def test_the_stem_check_catches_a_changed_ending():
    for left, right in (
        ("deploy", "deployment"),
        ("retry", "retries"),
        ("connect", "connections"),
        ("migrate", "migrating"),
        ("backup", "backups"),
    ):
        assert sf.shared_stems(sf.content_stems(left), sf.content_stems(right)), (left, right)
    assert not sf.shared_stems(sf.content_stems("queue"), sf.content_stems("database"))
    assert not sf.content_stems("how do we and the of")
    assert sf.content_stems("relay_routes.yaml") >= {"relay", "yaml"}


def test_generation_stops_on_a_v2_that_shares_a_word():
    words = sf.Words(json.loads(WORDS.read_bytes()))
    source = json.loads(LESSONS.read_bytes())
    source[0]["v2"] = "how do I drain a queue"
    built = sf.build_lessons(SEED, words, source)
    problems = sf.lesson_problems(built, words)
    assert any("V2 shares stems" in p for p in problems)


def test_generation_stops_on_a_v3_that_names_the_index_line():
    words = sf.Words(json.loads(WORDS.read_bytes()))
    source = json.loads(LESSONS.read_bytes())
    source[0]["v3"] = "Please drain the queue tonight."
    problems = sf.lesson_problems(sf.build_lessons(SEED, words, source), words)
    assert any("V3 shares stems with the index line" in p for p in problems)


def test_v3_gives_a_word_search_a_way_in(out, lessons, queries):
    """S2, the stock upper bound, opens notes by shared words. A V3 that shares no
    word with its note would make the SMALL negative control fail by design."""
    folders = memory_of(out, 1000)
    v3 = {q["lesson"]: q["prompt"] for q in queries if q["variant"] == "V3"}
    for row in lessons:
        name = row["target_note"]
        path = next(f / name for f in folders if (f / name).exists())
        content = sf.note_content("", path.read_text(encoding="utf-8"))
        assert sf.shared_stems(sf.content_stems(v3[row["id"]]), sf.content_stems(content))


def test_v4_is_v3_from_another_folder(queries):
    v3 = {q["lesson"]: q["prompt"] for q in queries if q["variant"] == "V3"}
    v4 = {q["lesson"]: q for q in queries if q["variant"] == "V4"}
    for lesson, query in v4.items():
        assert query["prompt"] == v3[lesson] and query["cwd"] == "other"


def test_v4_folder_differs_from_the_note_project(out):
    for size in (200, 1000, 5000):
        for row in read_json(out / f"scale-{size:05d}" / "scale.json")["lessons"].values():
            assert row["other_project"] != row["note_project"]
    small = read_json(out / "scale-00030" / "scale.json")["lessons"]
    assert {row["other_project"] for row in small.values()} == {sf.OUTSIDE_PROJECT}


def test_v5_has_no_applicable_note(queries, lessons):
    v5 = [q for q in queries if q["variant"] == "V5"]
    assert len({q["prompt"] for q in v5}) == 100
    near = {row["id"] for row in lessons}
    assert all(q["expect"] == "none" and q["near_lesson"] in near for q in v5)


def test_v6_task_file_holds_the_v3_text(out, queries):
    v3 = {q["lesson"]: q["prompt"] for q in queries if q["variant"] == "V3"}
    for query in (q for q in queries if q["variant"] == "V6"):
        assert query["prompt"] == "{FIXTURE_ROOT}/" + query["task_file"]
        text = (out / query["task_file"]).read_text(encoding="utf-8")
        assert text.strip() == v3[query["lesson"]]


def test_v7_is_a_task_notification(queries):
    v7 = [q for q in queries if q["variant"] == "V7"]
    assert len(v7) == 60
    for query in v7:
        assert query["prompt"].startswith("<task-notification>\n<task-id>")
        assert query["prompt"].endswith("</task-notification>")
        assert "<status>completed</status>" in query["prompt"]
        assert query["expect"] == "no_rows"


def test_no_query_holds_an_answer_or_a_canary(queries, lessons):
    secrets = [row["answer"] for row in lessons] + [
        row["canary"] for row in lessons if "canary" in row
    ]
    blob = json.dumps(queries)
    assert not [s for s in secrets if s in blob]


# -- the hooks can read what is planted --------------------------------------------


@pytest.fixture(scope="module")
def guard_table(out, lessons):
    """The guard table over the planted guard notes and some filler rules."""
    table = load_hook("guard_table", "guard_table_t_scale")
    folder = out.parent / "guard_memory"
    folder.mkdir()
    sources = memory_of(out, 1000)
    wanted = [r for r in lessons if r["kind"] == "guard"]
    for row in wanted:
        for name in (row["target_note"], row["duplicate_note"]):
            shutil.copy(next(f / name for f in sources if (f / name).exists()), folder)
    filler = [p for f in sources for p in notes_in(f) if "violates:" in p.read_text("utf-8")]
    filler = [p for p in filler if not any(r["target_note"] in p.name for r in wanted)]
    for path in sorted(filler)[:12]:
        shutil.copy(path, folder)
    return table, table.build(folder)


def test_guard_table_keeps_every_planted_rule(guard_table):
    _, built = guard_table
    assert built["skipped"] == []
    assert sum(1 for e in built["entries"] if e["violates"]) >= 20


def test_plain_hazard_matches_only_its_own_notes(guard_table, lessons, queries):
    table, built = guard_table
    by_id = {row["id"]: row for row in lessons}
    plain = [q for q in queries if q.get("spelling") == "plain"]
    assert len(plain) == 10
    for query in plain:
        row = by_id[query["lesson"]]
        hits = {hit["id"] for hit in table.match(query["command"], built)}
        wanted = {row["target_note"][:-3], row["duplicate_note"][:-3]}
        assert hits == wanted, query["id"]


def test_six_spellings_per_guard_lesson(queries):
    by_lesson: dict[str, list[str]] = {}
    for query in (q for q in queries if q["variant"] == "CMD"):
        by_lesson.setdefault(query["lesson"], []).append(query["spelling"])
    assert len(by_lesson) == 10
    assert all(
        names == ["plain", "cd", "full_path", "env", "sudo", "other_tool"]
        for names in by_lesson.values()
    )


def test_a_hazard_uses_no_real_tool_and_no_filler_guard_tool(lessons):
    words = json.loads(WORDS.read_bytes())
    real = set(words["tools"]) | set(words["guard_tools"])
    for row in (r for r in lessons if r["kind"] == "guard"):
        assert row["hazard"].split()[0] not in real
        assert row["other_tool"].split()[0] not in real


def test_every_planted_rule_passes_the_field_checks(out, lessons):
    fields = load_hook("memory_fields", "memory_fields_t_scale_2")
    folders = memory_of(out, 1000)
    for row in lessons:
        if row["kind"] == "guard":
            continue  # the guard table checks these
        path = next(f / row["target_note"] for f in folders if (f / row["target_note"]).exists())
        read = fields.read_fields(path.read_text(encoding="utf-8"))
        if read["rule"]:
            kind = "feedback" if path.name.startswith("feedback_") else "other"
            problems = [p for p in fields.check_fields(read, kind) if "status" not in p]
            assert problems == [], path.name


def test_filler_rules_pass_the_field_checks(out):
    fields = load_hook("memory_fields", "memory_fields_t_scale_3")
    checked = 0
    for folder in memory_of(out, 1000):
        for path in sorted(folder.glob("feedback_*.md"))[:40]:
            text = path.read_text(encoding="utf-8")
            read = fields.read_fields(text)
            if not read["rule"] or read["violates"]:
                continue
            checked += 1
            assert fields.check_fields(read, "feedback") == [], path.name
    assert checked > 20


def test_rewrite_notes_are_valid(out):
    fields = load_hook("memory_fields", "memory_fields_t_scale_4")
    found = 0
    for folder in memory_of(out, 5000):
        for path in folder.glob("feedback_*.md"):
            text = path.read_text(encoding="utf-8")
            if "\nrewrite: " in text:
                found += 1
                assert fields.rewrite_problems(fields.read_fields(text)) == []
    assert found >= 2


def test_error_quotes_pass_the_quote_gate(out, lessons, queries):
    """Each variant changes the path and the number, and the gate still sees
    the quoted message. A variant it refuses would measure nothing."""
    hook = load_hook("error_recall_hook", "error_recall_hook_t_scale")
    folders = memory_of(out, 1000)
    by_id = {row["id"]: row for row in lessons}
    variants = [q for q in queries if q["variant"] == "ERR"]
    assert len({q["error"] for q in variants}) == 30
    for query in variants:
        row = by_id[query["lesson"]]
        path = next(f / row["target_note"] for f in folders if (f / row["target_note"]).exists())
        text = path.read_text(encoding="utf-8")
        lines = hook.error_lines(query["error"], True)
        assert lines, query["id"]
        memory = {"body": text.split("\n---\n", 1)[1]}
        assert hook.quotes_error(memory, hook.query_of(lines)), query["id"]


def test_label_lessons_get_their_file_label(out, lessons):
    hook = load_hook("memory_labels", "memory_labels_t_scale")
    folders = memory_of(out, 1000)
    rows = [row for row in lessons if row["kind"] == "label"]
    assert len(rows) == 5
    for row in rows:
        name = row["target_note"]
        path = next(f / name for f in folders if (f / name).exists())
        labels = hook.memory_labels(path.read_text(encoding="utf-8"), path.stem)
        assert row["label_file"] in labels, name


def test_generic_label_queries_name_a_generic_file(lessons, queries):
    hook = load_hook("memory_labels", "memory_labels_t_scale_2")
    generic = [q for q in queries if q["id"].endswith("-LABEL-generic")]
    assert len(generic) == 5
    for query in generic:
        assert hook.labels(query["prompt"]) == {}, query["id"]


def test_canary_lines_are_real_credential_shapes(lessons):
    hook = load_hook("credential_guard", "credential_guard_t_scale")
    rows = [row for row in lessons if row["kind"] == "credential"]
    assert len(rows) == 5
    remotes = [row for row in rows if row["source"] in ("git_remote", "git_pushurl")]
    assert len(remotes) == 2
    for row in remotes:
        assert hook.credential_url(row["canary_line"].split(" = ", 1)[1])
    for row in rows:
        assert row["canary"] in row["canary_line"]


def test_no_note_holds_a_canary(out, lessons):
    canaries = [row["canary"] for row in lessons if "canary" in row]
    assert len(canaries) == 5
    for folder in memory_of(out, 1000):
        for path in folder.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert not [c for c in canaries if c in text], path.name


def test_subagent_queries_have_a_full_and_a_meta_form(queries):
    rows = [q for q in queries if q["variant"] == "SUB"]
    assert len(rows) == 10
    for query in rows:
        full = query["id"].endswith("-full")
        assert ("prompt" in query["tool_input"]) is full
        assert query["tool_input"]["description"]


def test_continuity_scripts_end_in_a_restore(queries):
    rows = [q for q in queries if q["variant"] == "CONT"]
    assert len(rows) == 15
    for query in rows:
        source = query["id"].rsplit("-", 1)[1]
        assert query["steps"][-1] == {"event": "SessionStart", "source": source}
        assert ("PreCompact" in [s["event"] for s in query["steps"]]) is (source == "compact")
