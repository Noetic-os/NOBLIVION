# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bound reconciliation inputs without pretending withheld notes do not exist."""

from __future__ import annotations

import hashlib
import json
import re

from .redaction import NATURAL_SECRET_PATTERN, contains_secret, redact_at_rest

# NOBLIVION-237: older redactors left markers such as "***REDACTED***" in
# legacy memory files. A marker is not a secret, but contains_secret flags the
# word, so the read-only note was withheld and every new note of the job then
# waited on it. Such a note now goes to the model with a neutral omission mark.
# The unchanged secret check then runs again on the result: a live secret, a
# natural-language secret or a failed redaction still withholds the note.
LEGACY_MARKER = re.compile(r"\*{0,3}\[?REDACTED(?:_EMAIL)?\]?\*{0,3}")
OMISSION_MARK = "[...]"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def neutral_note(note):
    """A read-only note with its legacy markers replaced, or None when still unsafe.

    Only a read-only note qualifies: the model may mark it unchanged or in
    conflict, never rewrite it, so the replaced text never becomes a stored note.
    """
    statement = note.get("statement")
    if not note.get("read_only") or not isinstance(statement, str):
        return None
    result = dict(note, statement=LEGACY_MARKER.sub(OMISSION_MARK, statement))
    core = {k: v for k, v in result.items() if k not in {"title", "evidence"}}
    return result if rejection_reason(encoded(core)) is None else None


def rejection_reason(text):
    """Classify without retaining the rejected body or weakening the shared gate."""
    if not contains_secret(text):
        return None
    if (
        redact_at_rest(text) != text
        or re.search(NATURAL_SECRET_PATTERN, text)
        or "REDACTION_FAILED" in text
    ):
        return "ContextUnsafeCandidate"
    return "ContextRedactedCandidate"


def isolate_notes(notes, *, max_chars):
    """Return safe notes and explicit, body-free dependencies for omitted notes.

    Safe statements remain usable when only an ancillary title or evidence quote
    is unsafe. Corrections still require independently validated old evidence.
    A withheld statement cannot be declared unrelated by guessing from its title.
    """
    safe, excluded = [], []
    for original in notes:
        note = dict(original)
        raw = encoded(original)
        ancillary_reason = None
        core = {k: v for k, v in note.items() if k not in {"title", "evidence"}}
        reason = rejection_reason(encoded(core))
        if reason == "ContextRedactedCandidate" and (neutral := neutral_note(note)):
            # Visible in status, but the statement is available: no deferral.
            note, reason, ancillary_reason = neutral, None, "ContextLegacyMarkerReplaced"
        if reason is None:
            if "title" in note and contains_secret(encoded(note["title"])):
                ancillary_reason = rejection_reason(encoded(note["title"]))
                note["title"] = "Existing memory"
            if "evidence" in note:
                note["evidence"] = [
                    ref for ref in note["evidence"] if not contains_secret(encoded(ref))
                ]
                if len(note["evidence"]) != len(original["evidence"]):
                    ancillary_reason = rejection_reason(encoded(original["evidence"]))
                    note["evidence_withheld"] = True
            if len(encoded(safe + [note])) > max_chars:
                reason = "ContextSizeLimit"
        if reason is None:
            safe.append(note)
            if ancillary_reason is None:
                continue
        dependency = {
            "reason": reason or ancillary_reason,
            "statement_available": reason is None,
            "fingerprint": hashlib.sha256(raw.encode()).hexdigest(),
        }
        for key in ("note_id", "revision", "logical_key", "scope", "category", "read_only"):
            if key in original and not contains_secret(encoded(original[key])):
                dependency[key] = original[key]
        title = original.get("title", "")
        if isinstance(title, str) and not contains_secret(title):
            dependency["title"] = title[:160]
        dependency["evidence_refs"] = [
            metadata
            for ref in original.get("evidence", [])[:8]
            if (
                metadata := {
                    key: ref[key]
                    for key in ("source_id", "source_hash", "start", "end")
                    if key in ref and not contains_secret(encoded(ref[key]))
                }
            )
        ]
        excluded.append(dependency)
    return safe, excluded


def dependent_decisions(decisions, excluded):
    """Only scope exclusions establish independence from an unavailable statement."""
    return [
        index
        for index, decision in enumerate(decisions)
        if decision["action"] in {"add", "update", "supersede"}
        and any(
            not note.get("statement_available")
            and (
                note.get("read_only")
                or note.get("scope") is None
                or note["scope"] == decision["scope"]
            )
            for note in excluded
        )
    ]
