-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema version 7 (NOBLIVION-143): a capture job names the sources it reads by a range of
-- per-session sequence numbers, not by one learning_job_sources row per source. One
-- session has at most one pending job, and new sources extend its range. seq is 0 for a
-- source stored before this version: those jobs keep their explicit rows.
ALTER TABLE learning_sources ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
CREATE INDEX learning_sources_seq ON learning_sources(project,root,session_id,source_client,seq)
    WHERE seq>0;
CREATE TABLE learning_job_ranges (
    job_id TEXT PRIMARY KEY REFERENCES learning_jobs(id) ON DELETE CASCADE,
    project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    source_client TEXT NOT NULL,
    first_seq INTEGER NOT NULL CHECK (first_seq>0), last_seq INTEGER NOT NULL,
    CHECK (last_seq>=first_seq)
) STRICT;
CREATE INDEX learning_job_ranges_scope
    ON learning_job_ranges(project,root,session_id,source_client);
