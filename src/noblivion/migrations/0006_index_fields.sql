-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema version 6 (NOBLIVION-104): the rule, apply and triggers lines of a memory file.
-- index_text holds them (redacted) for the keyword score only. content, and so what
-- fetch and the index show, does not change. The indexer rewrites the rows that have
-- no index_text yet (indexer.INDEX_FIELDS_VERSION).
ALTER TABLE memories ADD COLUMN index_text TEXT NOT NULL DEFAULT '';
