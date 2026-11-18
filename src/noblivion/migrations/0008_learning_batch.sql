-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema version 8 (NOBLIVION-267): per-session batch learning. A batch item is an
-- ordinary extract job with a range; this table only marks it. The watermark is the
-- highest sequence number a committed batch item of the session covered. Both tables
-- are new, so no existing row changes.
CREATE TABLE learning_batch_items (
    job_id TEXT PRIMARY KEY REFERENCES learning_jobs(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE learning_session_watermarks (
    project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    source_client TEXT NOT NULL,
    last_seq INTEGER NOT NULL CHECK (last_seq>=0),
    job_id TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (project,root,session_id,source_client)
) STRICT;
