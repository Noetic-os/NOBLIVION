-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema version 1. Copied from docs/design/0001-architecture.md, section 6.2.
-- The runner sets PRAGMA user_version and binds :random_base.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
-- keys: content_rev, vector_rev (counters, section 6.3), db_id (random,
--       set at create), embed_backend, embed_model, embed_dim,
--       redactor_version (section 5.4), created_at, last_maintenance_at,
--       dedup_consent and embed_consent (section 10.2)

CREATE TABLE memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused
    project     TEXT NOT NULL,                       -- namespace, default 'claude_code'
    root        TEXT NOT NULL,                       -- memory folder key, section 5.1
    path        TEXT NOT NULL,                       -- relative to root, section 5
    source_type TEXT NOT NULL
                CHECK (source_type IN ('claude_code_md', 'transcript_mined')),
    category    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,                       -- redacted text, with source marker
    hash        TEXT NOT NULL,                       -- sha256 hex of the raw file bytes
    weight      REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 4),
    pinned      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    labels      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels)),
    archived_at TEXT,                                -- set by dedup
    deleted_at  TEXT,                                -- file gone; purged after grace
    rev         INTEGER NOT NULL,                    -- content_rev of the last change
    created_at  TEXT NOT NULL,                       -- UTC ISO-8601
    updated_at  TEXT NOT NULL,
    UNIQUE (project, root, path)
) STRICT;
CREATE INDEX memories_live ON memories (project, root, source_type)
    WHERE archived_at IS NULL AND deleted_at IS NULL;
CREATE INDEX memories_rev ON memories (rev);
CREATE INDEX memories_hash ON memories (hash);

CREATE TABLE vectors (
    memory_id    INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL CHECK (dim > 0),
    content_hash TEXT NOT NULL,          -- sha256 of the text that was embedded
    blob         BLOB NOT NULL,          -- float32 little-endian, L2-normalised
    rev          INTEGER NOT NULL,       -- vector_rev of the last change
    CHECK (length(blob) = dim * 4),
    PRIMARY KEY (memory_id, model)
) STRICT;
CREATE INDEX vectors_rev ON vectors (rev);

CREATE TABLE feedback_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- ingest order
    session_id       TEXT NOT NULL,
    memory_id        INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL
                     CHECK (kind IN ('recall', 'use', 'load_bearing', 'contradiction')),
    citation_capable INTEGER NOT NULL CHECK (citation_capable IN (0, 1)),
    ts               TEXT NOT NULL,      -- event time from the client, UTC
    received_at      TEXT NOT NULL,      -- store time
    UNIQUE (session_id, memory_id, kind)
) STRICT;
CREATE INDEX feedback_events_memory ON feedback_events (memory_id, kind);
CREATE INDEX feedback_events_ts ON feedback_events (ts);

CREATE TABLE feedback (
    memory_id           INTEGER PRIMARY KEY REFERENCES memories (id) ON DELETE CASCADE,
    trust_0             REAL NOT NULL,
    trials              INTEGER NOT NULL DEFAULT 0,
    use_pos             REAL NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    trust_score         REAL NOT NULL,
    last_recalled_at    TEXT,
    last_used_at        TEXT,
    folded_event_id     INTEGER NOT NULL DEFAULT 0   -- highest feedback_events.id counted
) STRICT;

-- No foreign keys on purpose: the undo record must outlive the rows.
CREATE TABLE dedup_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    root         TEXT NOT NULL,
    kept_id      INTEGER NOT NULL,
    archived_id  INTEGER NOT NULL,
    kept_path    TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    archive_to   TEXT NOT NULL,          -- archive file path, relative to the memory folder
    status       TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'undone')),
    judge_model  TEXT NOT NULL,
    verdict      TEXT NOT NULL CHECK (json_valid(verdict)),
    created_at   TEXT NOT NULL,
    undone_at    TEXT
) STRICT;

CREATE TABLE dedup_vetoes (
    root       TEXT NOT NULL,
    path_a     TEXT NOT NULL,            -- path_a < path_b
    path_b     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (root, path_a, path_b)
) STRICT;

CREATE TABLE miner_state (
    transcript TEXT PRIMARY KEY,         -- path relative to the projects folder
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    offset     INTEGER NOT NULL,         -- bytes already mined
    mined_at   TEXT NOT NULL
) STRICT;

-- Ids start at a random base, so an id from an older database (a reset,
-- a reinstall) almost never names a row in a new one.
INSERT INTO sqlite_sequence (name, seq) VALUES ('memories', :random_base);
-- :random_base is a random integer in 1,000,000 .. 1,000,000,000.
