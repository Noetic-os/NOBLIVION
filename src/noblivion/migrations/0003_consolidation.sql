-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Independently owned evidence-linked derivatives; no executable skill state.
ALTER TABLE learning_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'extract'
    CHECK (kind IN ('extract','consolidate','draft'));
CREATE TABLE learning_claims (
    claim_id TEXT PRIMARY KEY, project TEXT NOT NULL, root TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision>0), statement TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('explicit_statement','observation','inference')),
    category TEXT NOT NULL, scope TEXT NOT NULL CHECK (scope IN ('project','general')),
    status TEXT NOT NULL CHECK (status IN ('staged','active','conflicted','merged','withdrawn','forgotten')),
    observed_at TEXT NOT NULL, effective_at TEXT,
    job_id TEXT REFERENCES learning_jobs(id), config_fingerprint TEXT NOT NULL,
    semantic_relation TEXT NOT NULL DEFAULT 'unreviewed',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
) STRICT;
CREATE INDEX learning_claims_scope ON learning_claims(project,root,status);
CREATE TABLE learning_claim_revisions (
    claim_id TEXT NOT NULL REFERENCES learning_claims(claim_id), revision INTEGER NOT NULL,
    action TEXT NOT NULL, snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    created_at REAL NOT NULL, PRIMARY KEY (claim_id,revision)
) STRICT;
CREATE TABLE learning_claim_supports (
    claim_id TEXT NOT NULL REFERENCES learning_claims(claim_id), claim_revision INTEGER NOT NULL,
    support_id TEXT NOT NULL, note_id TEXT NOT NULL REFERENCES learning_notes(note_id),
    note_revision INTEGER NOT NULL, source_id TEXT NOT NULL REFERENCES learning_sources(id),
    source_hash TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL,
    PRIMARY KEY (claim_id,claim_revision,support_id)
) STRICT;
CREATE INDEX learning_claim_support_note ON learning_claim_supports(note_id);
CREATE INDEX learning_claim_support_source ON learning_claim_supports(source_id);
CREATE TABLE learning_claim_edges (
    parent_id TEXT NOT NULL REFERENCES learning_claims(claim_id), parent_revision INTEGER NOT NULL,
    child_id TEXT NOT NULL REFERENCES learning_claims(claim_id),
    relation TEXT NOT NULL CHECK (relation IN ('support','equivalent','conflict')),
    PRIMARY KEY (parent_id,child_id,relation)
) STRICT;
CREATE TABLE learning_drafts (
    draft_id TEXT PRIMARY KEY, project TEXT NOT NULL, root TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('review_required','reviewed','rejected','withdrawn','forgotten')),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active=0),
    document TEXT NOT NULL, qualification TEXT NOT NULL CHECK (json_valid(qualification)),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
) STRICT;
CREATE TABLE learning_draft_supports (
    draft_id TEXT NOT NULL REFERENCES learning_drafts(draft_id), support_id TEXT NOT NULL,
    note_id TEXT NOT NULL REFERENCES learning_notes(note_id), note_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES learning_sources(id), source_hash TEXT NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL,
    PRIMARY KEY (draft_id,support_id)
) STRICT;
CREATE INDEX learning_draft_support_note ON learning_draft_supports(note_id);
CREATE TABLE learning_context_cache (
    cache_id TEXT PRIMARY KEY, project TEXT NOT NULL, root TEXT NOT NULL, session_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)), updated_at REAL NOT NULL
) STRICT;
