-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema version 5 (NOBLIVION-103): section-level chunks of a memory row.
-- A memories row stays one per file, with the content and the source marker.
-- A chunk row holds one section of that row, and chunk_vectors holds its vector.
-- The runner fills memory_chunks for the existing rows (db._seed_version_5).

CREATE TABLE memory_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused
    parent_id    INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL CHECK (chunk_index >= 0),  -- 0 is the summary
    heading      TEXT NOT NULL DEFAULT '',            -- heading path, "A > B"
    text         TEXT NOT NULL,                       -- the section, as stored (redacted)
    search_text  TEXT NOT NULL,                       -- what BM25 and the embedding read
    rev          INTEGER NOT NULL,                    -- content_rev of the last change
    UNIQUE (parent_id, chunk_index)
) STRICT;
CREATE INDEX memory_chunks_rev ON memory_chunks (rev);

CREATE TABLE chunk_vectors (
    chunk_id     INTEGER NOT NULL REFERENCES memory_chunks (id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL CHECK (dim > 0),
    content_hash TEXT NOT NULL,          -- sha256 of the text that was embedded
    blob         BLOB NOT NULL,          -- float32 little-endian, L2-normalised
    rev          INTEGER NOT NULL,       -- vector_rev of the last change
    CHECK (length(blob) = dim * 4),
    PRIMARY KEY (chunk_id, model)
) STRICT;
CREATE INDEX chunk_vectors_rev ON chunk_vectors (rev);
