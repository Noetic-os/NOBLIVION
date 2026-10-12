-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Durable background learning. Notes here are authoritative; Markdown is a projection.
CREATE TABLE learning_controls (
    project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    use_memory INTEGER CHECK (use_memory IN (0,1)),
    contribute INTEGER CHECK (contribute IN (0,1)),
    paused INTEGER CHECK (paused IN (0,1)), updated_at REAL NOT NULL,
    PRIMARY KEY (project,root,session_id)
) STRICT;
CREATE TABLE learning_batches (
    batch_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
    project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0,1)), created_at REAL NOT NULL
) STRICT;
CREATE TABLE learning_cursors (
    project TEXT NOT NULL, root TEXT NOT NULL, client TEXT NOT NULL,
    session_id TEXT NOT NULL, path_id TEXT NOT NULL, offset INTEGER NOT NULL CHECK (offset>=0),
    updated_at REAL NOT NULL, PRIMARY KEY (project,root,client,session_id,path_id)
) STRICT;
CREATE TABLE learning_sources (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    source_client TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    content TEXT NOT NULL, sha256 TEXT NOT NULL, observed_at TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK (json_valid(metadata)),
    status TEXT NOT NULL DEFAULT 'eligible' CHECK (status IN ('eligible','excluded','forgotten')),
    created_at REAL NOT NULL
) STRICT;
CREATE INDEX learning_sources_scope ON learning_sources(project,root,session_id);
CREATE INDEX learning_sources_revoked_hash ON learning_sources(project,root,sha256)
    WHERE status IN ('excluded','forgotten');
CREATE TABLE learning_jobs (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE REFERENCES learning_batches(batch_id),
    project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','leased','done','exhausted','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_owner TEXT, lease_until REAL,
    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
    progress TEXT NOT NULL DEFAULT '{"next_window":0,"decisions":[]}' CHECK (json_valid(progress)),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
) STRICT;
CREATE INDEX learning_jobs_due ON learning_jobs(status,next_attempt,created_at);
CREATE TABLE learning_job_sources (
    job_id TEXT NOT NULL REFERENCES learning_jobs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES learning_sources(id),
    ordinal INTEGER NOT NULL, PRIMARY KEY (job_id,source_id)
) STRICT;
CREATE TABLE learning_budget (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES learning_jobs(id),
    lease_token TEXT NOT NULL, tokens INTEGER NOT NULL CHECK (tokens>=0),
    seconds REAL NOT NULL CHECK (seconds>=0), cost REAL NOT NULL CHECK (cost>=0),
    status TEXT NOT NULL CHECK (status IN ('reserved','settled','uncertain')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
) STRICT;
CREATE INDEX learning_budget_window ON learning_budget(created_at);
CREATE TABLE learning_notes (
    note_id TEXT PRIMARY KEY, project TEXT NOT NULL, root TEXT NOT NULL,
    logical_key TEXT NOT NULL, path TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision>0),
    status TEXT NOT NULL CHECK (status IN ('active','superseded','excluded','forgotten')),
    scope TEXT NOT NULL CHECK (scope IN ('project','general')), category TEXT NOT NULL,
    title TEXT NOT NULL, statement TEXT NOT NULL, observed_at TEXT NOT NULL, effective_at TEXT,
    file_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE (project,root,path)
) STRICT;
CREATE UNIQUE INDEX learning_notes_current ON learning_notes(project,root,logical_key)
    WHERE status='active';
CREATE TABLE learning_revisions (
    note_id TEXT NOT NULL REFERENCES learning_notes(note_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    created_at REAL NOT NULL, PRIMARY KEY (note_id,revision)
) STRICT;
CREATE TABLE learning_evidence (
    note_id TEXT NOT NULL REFERENCES learning_notes(note_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, source_id TEXT NOT NULL REFERENCES learning_sources(id),
    source_hash TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    quote TEXT NOT NULL, PRIMARY KEY (note_id,revision,source_id,start,end)
) STRICT;
CREATE INDEX learning_evidence_source ON learning_evidence(source_id);
CREATE TABLE learning_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT REFERENCES learning_jobs(id),
    note_id TEXT REFERENCES learning_notes(note_id), action TEXT NOT NULL,
    detail TEXT NOT NULL CHECK (json_valid(detail)), created_at REAL NOT NULL
) STRICT;
CREATE TABLE learning_outbox (
    note_id TEXT PRIMARY KEY REFERENCES learning_notes(note_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, operation TEXT NOT NULL CHECK (operation IN ('write','remove')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','written','indexed')),
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
) STRICT;
