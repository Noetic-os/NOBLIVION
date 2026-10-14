# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split one stored memory text into section-level chunks (NOBLIVION-103).

The index keeps one ``memories`` row per file. A long file has one vector that
covers only its first lines, so a later section is never found. This module
cuts the stored text of a row into chunks. The store keeps each chunk as a row
of ``memory_chunks`` with the id of its parent row, and ranks the chunks.

The input is the stored content of a row, so it is already redacted:
``# <name>``, an optional description, the ``[claude_code_md: <path>]`` marker
line, then the body. The marker stays on the parent row only. No chunk holds it.

Rules (``CHUNKER_VERSION`` 1):

- A text of at most ``CHUNK_CAP`` characters (title, description and body, with
  no marker line) is one chunk: chunk 0 holds all of it, byte for byte the text
  that the file row embeds (``embedding.embed_text``). Its vector and its keyword
  score are those of the file row before the chunks, so a short note ranks as it
  did, and its vector is not computed a second time.
- A longer text has chunk 0 as the summary: the title and the description. A
  text with no title and marker has no summary chunk, and chunk 0 is its
  first section.
- The body is cut at Markdown headings (not inside a code fence). Each section
  is one chunk. The heading line stays in the chunk text.
- A section longer than ``CHUNK_CAP`` characters is packed from its paragraphs,
  and a long list from its lines, then from its sentences. A chunk that
  continues a section starts with the last sentence of the chunk before it.
- A section shorter than ``MIN_CHUNK`` characters joins the section after it
  when both fit in one chunk, because a very short text makes a weak vector.
- ``search_text`` is what BM25 and the embedding read: the file title and the
  heading path in front of the chunk text.

Change a rule, then raise ``CHUNKER_VERSION``: ``db.ensure_chunks`` cuts every
row again when the version in ``meta`` differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = 1
CHUNK_CAP = 1200  # characters of chunk text, before the overlap sentence
MIN_CHUNK = 300  # a shorter section joins its neighbour when both fit under the cap
MAX_OVERLAP = 240  # the overlap sentence is dropped when it is longer than this
MAX_CHUNKS = 400  # a row with more sections than this keeps its first ones only
TITLE_CHARS = 120

_MARKER_LINE_RE = re.compile(r"^\[(?:claude_code_md|transcript_mined): [^\]\n]*\]$")
_MARKER_RE = re.compile(r"^\[(?:claude_code_md|transcript_mined): [^\]\n]*\][ \t]*$", re.M)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str  # heading path, "A > B"; empty for the summary and for text before a heading
    text: str  # the text as the reader sees it
    search_text: str  # what BM25 and the embedding read


@dataclass(frozen=True)
class Chunked:
    title: str
    has_summary: bool
    chunks: tuple[Chunk, ...]


@dataclass
class _Piece:
    heading: str
    text: str
    continued: bool = False  # starts with the overlap sentence of the piece before it


def title_of(content: str) -> str:
    """The title of a stored text: its first line, without a leading ``# ``."""
    for line in content.splitlines():
        line = line.strip()
        if line:
            return (line[2:].strip() if line.startswith("# ") else line)[:TITLE_CHARS]
    return ""


def _split_head(content: str) -> tuple[str, str]:
    """``(head, body)``: the title and description, and the rest."""
    marker = _MARKER_RE.search(content)
    if marker is not None:
        return content[: marker.start()].strip(), content[marker.end() :].strip("\n")
    return "", content.strip("\n")


def _sections(body: str) -> list[tuple[str, list[str]]]:
    """The body cut at headings: ``(heading path, lines)``. The heading line is the first line."""
    out: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    path = ""
    lines: list[str] = []
    fence = ""
    for line in body.split("\n"):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            mark = fence_match.group(1)
            if not fence:
                fence = mark
            elif mark == fence:
                fence = ""
        heading = None if fence or fence_match else _HEADING_RE.match(line)
        if heading is not None:
            if any(part.strip() for part in lines):
                out.append((path, lines))
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading.group(2).strip()))
            path = " > ".join(name for _, name in stack)
            lines = [line]
        else:
            lines.append(line)
    if any(part.strip() for part in lines):
        out.append((path, lines))
    return out


def _blocks(lines: list[str]) -> list[str]:
    """Paragraphs: runs of lines between blank lines. A code fence stays in one block."""
    blocks: list[str] = []
    current: list[str] = []
    fence = ""
    for line in lines:
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            mark = fence_match.group(1)
            if not fence:
                fence = mark
            elif mark == fence:
                fence = ""
        if not line.strip() and not fence:
            if current:
                blocks.append("\n".join(current).strip("\n"))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip("\n"))
    return [b for b in blocks if b.strip()]


def _hard_split(text: str, cap: int) -> list[str]:
    """Cut ``text`` at whitespace so that no part is longer than ``cap``."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > cap:
        cut = rest.rfind(" ", 0, cap + 1)
        if cut < cap // 2:
            cut = cap
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def _atoms(block: str, cap: int) -> list[tuple[str, str]]:
    """A block as parts that each fit under ``cap``: ``(text, joiner before it)``.

    A block that fits is one part. A long block is cut at its lines, a long line at
    its sentences, and a sentence with no space at white space.
    """
    if len(block) <= cap:
        return [(block, "\n\n")]
    atoms: list[tuple[str, str]] = []
    for line in block.split("\n"):
        if len(line) <= cap:
            parts = [line]
        else:
            parts = []
            for sentence in _SENTENCE_END_RE.split(line):
                parts.extend(_hard_split(sentence, cap) if len(sentence) > cap else [sentence])
        for n, part in enumerate(parts):
            joiner = "\n\n" if not atoms else ("\n" if n == 0 else " ")
            atoms.append((part, joiner))
    return atoms


def _last_sentence(text: str) -> str:
    flat = text.strip()
    if not flat:
        return ""
    last = _SENTENCE_END_RE.split(flat)[-1].strip()
    return last if 0 < len(last) <= MAX_OVERLAP and last != flat else ""


def _pack(path: str, lines: list[str], cap: int) -> list[_Piece]:
    """One section as pieces of at most ``cap`` characters (the overlap sentence comes on top)."""
    text = "\n".join(lines).strip("\n")
    if len(text) <= cap:
        return [_Piece(path, text)]
    pieces: list[_Piece] = []
    current = ""
    for block in _blocks(lines):
        for part, joiner in _atoms(block, cap):
            if current and len(current) + len(joiner) + len(part) > cap:
                pieces.append(_Piece(path, current, continued=bool(pieces)))
                current = part
            else:
                current = f"{current}{joiner}{part}" if current else part
    if current:
        pieces.append(_Piece(path, current, continued=bool(pieces)))
    # The overlap: a piece that continues a section starts with the last sentence of the piece
    # before it, so a statement cut at the border is whole in one of the two.
    for i in range(1, len(pieces)):
        overlap = _last_sentence(pieces[i - 1].text)
        if overlap:
            pieces[i].text = f"{overlap}\n{pieces[i].text}"
    return pieces


def _merge_short(pieces: list[_Piece], cap: int, minimum: int) -> list[_Piece]:
    merged: list[_Piece] = []
    for piece in pieces:
        last = merged[-1] if merged else None
        if (
            last is not None
            and not piece.continued
            and len(last.text) < minimum
            and len(last.text) + 2 + len(piece.text) <= cap
        ):
            last.text = f"{last.text}\n\n{piece.text}"
            last.heading = last.heading or piece.heading
            continue
        merged.append(piece)
    return merged


def search_text_of(title: str, heading: str, text: str) -> str:
    """The text that BM25 and the embedding read for a body chunk."""
    lead = f"{title} > {heading}" if title and heading else title or heading
    return f"{lead}\n{text}" if lead else text


def split_content(content: str, *, cap: int = CHUNK_CAP, minimum: int = MIN_CHUNK) -> Chunked:
    """Cut one stored text into chunks. A text with no content gives no chunk."""
    head, body = _split_head(content.replace("\r\n", "\n"))
    title = title_of(head) if head else title_of(body)
    whole = "\n".join(
        line
        for line in content.replace("\r\n", "\n").split("\n")
        if not _MARKER_LINE_RE.match(line.strip())
    ).strip()
    if whole and len(whole) <= cap:
        return Chunked(title, bool(head), (Chunk(0, "", whole, whole),))
    chunks: list[Chunk] = []
    if head:
        chunks.append(Chunk(0, "", head, head))
    pieces: list[_Piece] = []
    for path, lines in _sections(body):
        pieces.extend(_pack(path, lines, cap))
    for piece in _merge_short(pieces, cap, minimum):
        if len(chunks) >= MAX_CHUNKS:
            break
        text = piece.text.strip()
        if text:
            chunks.append(
                Chunk(len(chunks), piece.heading, text, search_text_of(title, piece.heading, text))
            )
    return Chunked(title, bool(head), tuple(chunks))
