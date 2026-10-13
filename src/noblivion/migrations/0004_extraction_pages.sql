-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Exact extraction coverage receipts. Candidate payloads are erased on forget.
CREATE TABLE learning_extraction_pages (
    job_id TEXT NOT NULL REFERENCES learning_jobs(id) ON DELETE CASCADE,
    unit_index INTEGER NOT NULL CHECK(unit_index >= 0),
    continuation INTEGER NOT NULL CHECK(continuation >= 0),
    fingerprint TEXT NOT NULL,
    spans TEXT NOT NULL,
    candidates TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processed','no_lesson','saturated','exhausted','rejected')),
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id, unit_index, continuation)
);
CREATE INDEX learning_extraction_pages_status ON learning_extraction_pages(status, job_id);
