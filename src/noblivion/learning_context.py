# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bound reconciliation inputs without pretending withheld notes do not exist."""

from __future__ import annotations

import hashlib
import json
import re

from .redaction import NATURAL_SECRET_PATTERN, contains_secret, redact_at_rest


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
