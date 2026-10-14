# SPDX-License-Identifier: AGPL-3.0-or-later
"""Section-level chunks of a stored memory text (NOBLIVION-103). Fictional data only."""

from __future__ import annotations

import re

from noblivion import chunking, embedding

MARKER = "[claude_code_md: reference_demo.md]"


def stored(body: str, name: str = "Demo", description: str = "A demo note") -> str:
    return f"# {name}\n\n{description}\n\n{MARKER}\n\n{body}"


def sentence(n: int) -> str:
    return f"Step {n} of the rollout checks the queue depth and then waits for the pager to clear."


def test_a_short_text_is_one_chunk_with_no_marker_line():
    text = stored("Run the checks before the deploy.")
    result = chunking.split_content(text)
    assert len(result.chunks) == 1 and result.title == "Demo"
    chunk = result.chunks[0]
    assert chunk.index == 0 and chunk.heading == ""
    # The text that the file row embeds (``embedding.embed_text``).
    assert chunk.text == embedding.embed_text(text)
    assert chunk.text == "# Demo\n\nA demo note\n\n\nRun the checks before the deploy."
    assert chunk.search_text == chunk.text
    assert "claude_code_md" not in chunk.text


def test_a_long_text_has_a_summary_chunk_and_one_chunk_per_heading_section():
    body = "\n\n".join(
        f"## Part {n}\n{' '.join(sentence(i) for i in range(5))}" for n in range(1, 4)
    )
    result = chunking.split_content(stored(body))
    assert result.has_summary
    assert [c.index for c in result.chunks] == [0, 1, 2, 3]
    assert result.chunks[0].text == "# Demo\n\nA demo note"
    assert [c.heading for c in result.chunks[1:]] == ["Part 1", "Part 2", "Part 3"]
    assert result.chunks[2].text.startswith("## Part 2\n")
    # The title and the heading path are in front of the text that BM25 and the model read.
    assert result.chunks[2].search_text.startswith("Demo > Part 2\n## Part 2")
    assert not any("claude_code_md" in c.text for c in result.chunks)


def test_a_nested_heading_gives_a_heading_path():
    body = (
        "# Top\n"
        + "alpha " * 150
        + "\n\n## Inner\n"
        + "beta " * 150
        + "\n\n# Next\n"
        + "gamma " * 150
    )
    result = chunking.split_content(stored(body))
    assert [c.heading for c in result.chunks[1:]] == ["Top", "Top > Inner", "Next"]


def test_a_hash_line_in_a_code_fence_is_not_a_heading():
    code = "```sh\n# not a heading\necho hi\n```"
    body = "## Real\n" + "word " * 100 + "\n" + code + "\n\n## Other\n" + "tail " * 200
    result = chunking.split_content(stored(body))
    assert [c.heading for c in result.chunks[1:]] == ["Real", "Other"]
    assert "# not a heading" in result.chunks[1].text


def test_a_long_section_is_cut_under_the_cap_with_a_sentence_of_overlap():
    body = "## Long\n" + " ".join(sentence(i) for i in range(40))
    result = chunking.split_content(stored(body))
    parts = result.chunks[1:]
    assert len(parts) >= 3
    assert all(len(c.text) <= chunking.CHUNK_CAP + chunking.MAX_OVERLAP + 1 for c in parts)
    assert all(c.heading == "Long" for c in parts)
    # The first sentence of a continuing chunk is the last sentence of the chunk before it.
    for before, after in zip(parts, parts[1:], strict=False):
        last = re.split(r"(?<=[.!?])\s+", before.text.strip())[-1]
        assert after.text.startswith(last)


def test_a_long_list_is_cut_at_its_lines_and_nothing_is_lost():
    items = [f"- item {n}: the owner of service {n} is team {n % 7}" for n in range(120)]
    body = "## Inventory\n" + "\n".join(items)
    result = chunking.split_content(stored(body))
    joined = "\n".join(c.text for c in result.chunks)
    assert all(item in joined for item in items)
    assert all(len(c.text) <= chunking.CHUNK_CAP + chunking.MAX_OVERLAP + 1 for c in result.chunks)


def test_a_word_with_no_space_is_cut_at_the_cap():
    result = chunking.split_content(stored("## Blob\n" + "x" * 3000))
    assert all(len(c.text) <= chunking.CHUNK_CAP + chunking.MAX_OVERLAP + 1 for c in result.chunks)
    assert sum(c.text.count("x") for c in result.chunks) >= 3000


def test_a_short_section_joins_the_next_one_when_both_fit():
    body = "## Tiny\nOne line.\n\n## Next\n" + "word " * 100 + "\n\n## Last\n" + "tail " * 200
    result = chunking.split_content(stored(body))
    assert [c.heading for c in result.chunks[1:]] == ["Tiny", "Last"]
    assert "## Next" in result.chunks[1].text and "One line." in result.chunks[1].text


def test_text_before_the_first_heading_is_a_chunk_with_no_heading():
    body = "Intro line. " * 60 + "\n\n## Later\n" + "word " * 200
    result = chunking.split_content(stored(body))
    assert [c.heading for c in result.chunks[1:]] == ["", "Later"]


def test_a_row_with_no_marker_has_no_summary_chunk():
    text = "Operator correction on 2026-01-01 in session abc. " * 40
    result = chunking.split_content(text)
    assert not result.has_summary
    assert result.chunks[0].index == 0 and result.chunks[0].text.startswith("Operator correction")


def test_the_chunks_are_numbered_from_zero_and_empty_text_has_none():
    assert chunking.split_content("").chunks == ()
    assert chunking.split_content("\n\n  \n").chunks == ()
    body = "\n\n".join(f"## S{n}\n" + "word " * 200 for n in range(12))
    assert [c.index for c in chunking.split_content(stored(body)).chunks] == list(range(13))


def test_a_huge_text_keeps_the_first_chunks_only():
    body = "\n\n".join(f"## S{n}\n" + "word " * 200 for n in range(chunking.MAX_CHUNKS + 50))
    assert len(chunking.split_content(stored(body)).chunks) == chunking.MAX_CHUNKS


def test_the_cut_is_deterministic():
    body = "\n\n".join(f"## S{n}\n" + " ".join(sentence(i) for i in range(12)) for n in range(5))
    assert chunking.split_content(stored(body)) == chunking.split_content(stored(body))


def test_title_of_reads_the_first_line():
    assert chunking.title_of("# A title\n\nbody") == "A title"
    assert chunking.title_of("\n\nplain first line\nmore") == "plain first line"
    assert chunking.title_of("") == ""
