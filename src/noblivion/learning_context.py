# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bound reconciliation inputs without pretending withheld notes do not exist."""

from __future__ import annotations

import hashlib
import json
import re

from .redaction import (
    NATURAL_SECRET_PATTERN,
    contains_secret,
    learning_secret_signal,
    redact_at_rest,
)

# NOBLIVION-237: older redactors left markers such as "***REDACTED***" in
# legacy memory files. A marker is not a secret, but contains_secret flags the
# word, so the read-only note was withheld and every new note of the job then
# waited on it. Such a note now goes to the model with a neutral omission mark.
# The unchanged secret check then runs again on the result: a live secret, a
# natural-language secret or a failed redaction still withholds the note.
LEGACY_MARKER = re.compile(r"\*{0,3}\[?REDACTED(?:_EMAIL)?\]?\*{0,3}")
OMISSION_MARK = "[...]"
# NOBLIVION-258: after a key the omission mark reads as a value
# (``TOKEN=[...]``, ``Authorization: Bearer [...]``, ``user:[...]@host``), so
# the swapped note still failed the check and the job waited until it gave up.
# Then each marker line is replaced by this line as a whole. It is no value:
# no key, ``=``, ``:`` or ``@``.
WITHHELD_LINE = "[line withheld]"
# The line path is an allowlist and fails closed. It applies only when every
# withheld line is a whole marker line: a name, ``=`` or ``:``, and one of the
# redactor tokens, with spaces or tabs around them and nothing else. Any other
# withheld line withholds the note, as main did: its other text would be cut
# out of every check of the kept lines (``the password. [REDACTED]`` before
# ``is hunter2``), and a line that fails the secret check without a marker is
# no marker line at all.
_MARKER_LINE = re.compile(
    r"[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*(?:=|:)[ \t]*"
    r"(?:\*\*\*REDACTED\*\*\*|\[REDACTED\]|\[REDACTED_EMAIL\])[ \t]*"
)
# In any other withheld line one marker span may be cut out: ``NAME=<marker>``,
# ``NAME: <marker>`` or ``NAME: <scheme> <marker>`` (one word such as Bearer),
# with the name and the "=" or ":", so no key is left before the omission mark.
# The redactor may have masked only the head of a value (``PASSWORD=correct
# horse``, ``app:pw@tail@host``, ``Digest username=...``), so the span must end
# its line: only closing punctuation may follow it, and nothing at all after a
# scheme. No "@", no other line break and no other space may follow it, and
# nothing may be glued to either end (any Unicode word character). In doubt
# the note stays withheld; the cut line must also pass the line check.
_MARKER_SPAN = re.compile(
    r"(?<!\w)[A-Za-z_][A-Za-z0-9_]*"
    r"(?:[ \t]*=[ \t]*|:[ \t]*(?P<scheme>[A-Za-z]+[ \t]+)?)"
    r"(?:\*\*\*REDACTED\*\*\*|\[REDACTED\]|\[REDACTED_EMAIL\])"
    r"(?!\w)"
)
_SPAN_TAIL = re.compile(r"[.,;)\]}\"']*\s*")
_SCHEME_TAIL = re.compile(r"\s*")
# The longest word that counts as plain prose after a marker (_plain_tail).
_PLAIN_WORD_MAX = 24
# A key and a value in the text after a marker (``mode=fast``, ``user: bob``).
_TAIL_KEY_VALUE = re.compile(r"[A-Za-z_]\w*[ \t]*[=:][ \t]*\w")
# Any whitespace but a space or a tab, and any character outside ASCII.
_OTHER_SPACE = re.compile(r"[^\S \t]|[^\x00-\x7f]")
# A marker at the end of a line does not mean the value ended there: the
# redactor masks a value up to the line end, and the rest of it may sit on the
# next line (a quote or a shell continuation that runs on, a folded header, an
# indented continuation, also past a blank line). So every line after a
# withheld marker line or a cut span must be blank, through the end of the text.
# Counting quotes or looking further ahead does not work: an apostrophe evens
# the count, and some values have none.
_BLANK_LINE = re.compile(r"[ \t]*\r?")
# Redactor leftover: upper-case "REDACT" anywhere in the result (a cut-off
# token such as ``[REDACT``) also withholds the note.
_RESIDUE = "REDACT"
# The body of a key block has no marker and no secret word: the redactor masks
# a cut-off block only up to a short line, and it does not know an OpenVPN
# static key or a PuTTY key file. On the line path any key text withholds the
# note, as main did: a run of 40 base64 characters with upper case, lower case
# and a digit, a line of 32 or more hex characters, an armor line, or the
# PuTTY and OpenVPN key words.
_KEY_BASE64 = re.compile(r"[A-Za-z0-9+/]{40,}")
_KEY_HEX_LINE = re.compile(r"(?m)^[^0-9A-Za-z]*[0-9A-Fa-f]{32,}[^0-9A-Za-z]*$")
_KEY_WORDS = re.compile(
    r"(?i)-----(?:BEGIN|END) |putty-user-key-file|(?:public|private)-lines|private-mac"
    r"|openvpn static key"
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def secret_in_text(value, _depth=0):
    """The first string in the value that fails the secret check as raw text.

    NOBLIVION-259: the encoded check reads a line break as the two characters
    "\\n", so a rule that needs a real line start (``pass:`` in YAML) or
    whitespace across a line break does not match. This walks dicts and lists
    and checks each string as it is. A string that holds a JSON dict or list
    (a message body) is walked again after parsing. Only strings are checked:
    contains_secret rejects any other type. It adds to the encoded check,
    which still finds a key and its value in two fields. None when all pass.
    """
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            if (leaf := secret_in_text(item, _depth)) is not None:
                return leaf
        return None
    if not isinstance(value, str):
        return None
    if contains_secret(value):
        return value
    if _depth < 8 and value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return None
        return secret_in_text(parsed, _depth + 1)
    return None


def unsafe_reason(value):
    """rejection_reason of the encoded value, else of its first raw string that fails."""
    reason = rejection_reason(encoded(value))
    if reason is None and (leaf := secret_in_text(value)) is not None:
        reason = rejection_reason(leaf)
    return reason


def _core_passes(note):
    core = {k: v for k, v in note.items() if k not in {"title", "evidence"}}
    return unsafe_reason(core) is None


def neutral_note(note):
    """A read-only note with its legacy markers neutralised and the status reason.

    None when the note is still unsafe. Only a read-only note qualifies: the
    model may mark it unchanged or in conflict, never rewrite it, so the
    neutral text never becomes a stored note.

    A statement with "private key" in any case is withheld on both paths, and
    so is a raw statement with any secret signal other than its markers: the
    redactor changes it, a natural-language secret (also read as one line), a
    failed redaction or key text. Then the markers are replaced by the
    omission mark. That text must pass as encoded note, as raw text and as one
    line, and a marker span after a key must end its line. If not, the line
    path runs. A whole marker line is withheld. In any other line that fails
    the line check, the marker spans are cut out, and the cut line must then
    pass the line check, or the note stays withheld. Every line after a
    withheld line must be blank, through the end: the value may run on. No marker
    and no redactor leftover may remain. The result must then pass the unchanged check on the
    raw statement and on the encoded note, and so must the kept lines without
    the placeholder lines, cut at "\\n" and as one line.

    The line path never adds a secret check of its own beyond these. Text
    that the shared check misses reaches the model, as in any note without a
    marker.
    """
    statement = note.get("statement")
    if not note.get("read_only") or not isinstance(statement, str):
        return None
    # The body of a key block has no marker and passes line by line.
    if "private key" in statement.lower():
        return None
    # NOBLIVION-259: the raw guard runs before the swap too. The swap was
    # checked only as encoded note, so a secret that needs a real line start
    # or spans a line break reached the model.
    if (
        redact_at_rest(statement) != statement
        or re.search(NATURAL_SECRET_PATTERN, statement)
        # Read across lines too: the key word may sit on a withheld line.
        or re.search(NATURAL_SECRET_PATTERN, " ".join(statement.splitlines()))
        or "REDACTION_FAILED" in statement
        or learning_secret_signal(statement)
        or _has_key_text(statement)
    ):
        return None
    text = LEGACY_MARKER.sub(OMISSION_MARK, statement)
    swapped = dict(note, statement=text)
    if (
        _core_passes(swapped)
        and not contains_secret(" ".join(text.splitlines()))
        # The redactor may have masked only the head of a value
        # (``password: [REDACTED] tail9Word``): a marker span after a key
        # must end its line, as on the line path.
        and all(
            not _MARKER_SPAN.search(line) or _cut_span(line) is not None or _plain_tail(line)
            for line in statement.split("\n")
        )
    ):
        return swapped, "ContextLegacyMarkerReplaced"
    lines = []
    raw_lines = statement.split("\n")
    for i, line in enumerate(raw_lines):
        if not _line_withheld(line):
            lines.append((line, False))
            continue
        # The value may run on, also past a blank line: only blank lines
        # through the end of the text may follow.
        if not all(_BLANK_LINE.fullmatch(rest) for rest in raw_lines[i + 1 :]):
            return None
        if _MARKER_LINE.fullmatch(line):
            lines.append((WITHHELD_LINE, True))
        else:
            cut = _cut_span(line)
            if cut is None or _line_withheld(cut):
                return None
            lines.append((cut, False))
    withheld = "\n".join(line for line, _ in lines)
    if LEGACY_MARKER.search(withheld) or _RESIDUE in withheld:
        return None
    # A placeholder line must not hide a key and its value on two kept lines
    # (``Bearer`` and a token, ``Basic`` and a credential): the redactor reads
    # the placeholder as no value, so the kept lines are checked without it.
    # As encoded note too: there the natural-language rule reads across lines,
    # and the placeholder pushes the key word and the value apart.
    kept = "\n".join(line for line, held in lines if not held)
    # And as one line: the natural-language rule stops at "\n" in the raw
    # text, and in the encoded note it finds no word start after "\\n".
    joined = " ".join(kept.splitlines())
    result = dict(note, statement=withheld)
    if (
        contains_secret(withheld)
        or contains_secret(kept)
        or contains_secret(joined)
        or not _core_passes(result)
        or not _core_passes(dict(note, statement=kept))
        or not _core_passes(dict(note, statement=joined))
    ):
        return None
    return result, "ContextLegacyLineWithheld"


def _cut_span(line):
    """The line with its marker span cut out, or None when in doubt."""
    span = _MARKER_SPAN.search(line)
    if span is None:
        return None
    tail = line[span.end() :]
    if (
        not _SPAN_TAIL.fullmatch(tail)
        or (span["scheme"] and not _SCHEME_TAIL.fullmatch(tail))
        or "@" in tail
        or _OTHER_SPACE.search(tail)
    ):
        return None
    return line[: span.start()] + OMISSION_MARK + tail


def _plain_tail(line):
    """True when only plain words follow the marker span of this line (NOBLIVION-259).

    ``KEY=[REDACTED] some prose`` is safe when no word after the marker has the
    shape of a value: a digit and a letter in one word, a very long word, an
    "@" or an unusual space. A tail that fails the secret check, or that holds a
    key and a value, is no plain tail. The swap may keep such a line; the line
    path keeps its stricter rule.
    """
    span = _MARKER_SPAN.search(line)
    if span is None or span["scheme"]:
        return False
    tail = line[span.end() :]
    if "@" in tail or re.search(r"[^\S \t]", tail):
        return False
    if contains_secret(tail) or _TAIL_KEY_VALUE.search(tail):
        return False
    return all(
        len(word) <= _PLAIN_WORD_MAX
        and not (re.search(r"[^\W\d_]", word) and re.search(r"\d", word))
        for word in tail.split()
    )


def _has_key_text(text):
    """True when the text holds key body text or the words of a key file."""
    return bool(
        _KEY_WORDS.search(text)
        or _KEY_HEX_LINE.search(text)
        or any(
            re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"[0-9]", run)
            for run in _KEY_BASE64.findall(text)
        )
    )


def _line_withheld(line):
    """True when a line holds a marker or fails the secret check.

    Lines are cut at "\\n" only. Any other line break (``\\r``, U+2028,
    U+0085) stays inside the line, so a marker withholds the whole run and a
    kept line is raw text. Each part between such breaks is checked on its
    own too: a rule that reads a line start (``pass: value``) sees "\\n" only.
    """
    return bool(LEGACY_MARKER.search(line)) or any(
        contains_secret(part) for part in [line, *line.splitlines()]
    )


def rejection_reason(text):
    """Classify without retaining the rejected body or weakening the shared gate."""
    if not contains_secret(text):
        return None
    if (
        redact_at_rest(text) != text
        or re.search(NATURAL_SECRET_PATTERN, text)
        or "REDACTION_FAILED" in text
        # NOBLIVION-262: a key format or prose form that only the learning
        # check finds is a live secret, not a legacy marker.
        or learning_secret_signal(text)
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
        # NOBLIVION-259: as encoded note and as raw text. The raw check runs
        # on the note that goes to the model, so after a neutral swap it reads
        # the swapped statement, not the markers of the original.
        reason = unsafe_reason(core)
        if reason == "ContextRedactedCandidate" and (neutral := neutral_note(note)):
            # Visible in status, but the statement is available: no deferral.
            (note, ancillary_reason), reason = neutral, None
        if reason is None:
            if "title" in note and (title_reason := unsafe_reason(note["title"])):
                ancillary_reason = title_reason
                note["title"] = "Existing memory"
            if "evidence" in note:
                note["evidence"] = [ref for ref in note["evidence"] if unsafe_reason(ref) is None]
                if len(note["evidence"]) != len(original["evidence"]):
                    ancillary_reason = unsafe_reason(original["evidence"])
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
