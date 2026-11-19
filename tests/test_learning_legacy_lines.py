# SPDX-License-Identifier: AGPL-3.0-or-later
"""A legacy marker after a key withholds its line, not the note (NOBLIVION-258).

The omission mark reads as a value after a key (``TOKEN=[...]``), so such a
read-only note stayed withheld and every new note of its job waited on it until
the job gave up. A whole marker line (``NAME=<marker>``) is now withheld instead.
Any other withheld line, and any other secret signal, withholds the note, as
before.
"""

import base64
import hashlib
import json
import re

import pytest

from noblivion import db, learning_context
from noblivion.learning import LearningLedger
from noblivion.learning_context import (
    LEGACY_MARKER,
    OMISSION_MARK,
    WITHHELD_LINE,
    dependent_decisions,
    encoded,
    isolate_notes,
    neutral_note,
    rejection_reason,
)
from noblivion.learning_worker import ProviderError
from noblivion.redaction import contains_secret, redact_at_rest
from test_learning_review_fixes import ScriptedProvider, candidate, capture, worker

FAKE_GITHUB_TOKEN = "ghp_" + "Ab3" * 12


@pytest.fixture
def ledger(tmp_path):
    conn = db.open_db(tmp_path / "legacy-lines.db", create=True)
    yield LearningLedger(conn)
    conn.close()


def legacy(statement, **changes):
    note = dict(
        note_id="legacy:7",
        logical_key="legacy:7",
        revision=1,
        category="fact",
        scope="project",
        title="Service notes",
        statement=statement,
        evidence=[],
        read_only=True,
    )
    note.update(changes)
    return note


def core(note):
    return {k: v for k, v in note.items() if k not in {"title", "evidence"}}


def ready(ledger):
    ledger.conn.execute("UPDATE learning_jobs SET next_attempt=0 WHERE status='pending'")


def context_text(safe):
    """The context string the worker checks before any call."""
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))


# Whole marker lines: a name, "=" or ":", one redactor token, and spaces or
# tabs around them.
SHAPES = [
    "TOKEN=[REDACTED]",
    "GH_TOKEN=***REDACTED***",
    "PASSWORD=[REDACTED]",
    "  API_TOKEN = [REDACTED]\t",
    "password: ***REDACTED***",
    "TOKEN=[REDACTED_EMAIL]",
]


@pytest.mark.parametrize("shape", SHAPES)
def test_marker_after_a_key_withholds_only_its_line(shape):
    statement = f"The cobalt service listens on port 8443.\n\nRestart it after a deploy.\n{shape}"
    note = legacy(statement)
    # Preconditions: the only signal is the marker, and the omission mark
    # alone fails, so this is the line path and not the whole-note swap.
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    swapped = dict(note, statement=LEGACY_MARKER.sub(OMISSION_MARK, statement))
    assert rejection_reason(encoded(core(swapped))) is not None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert [n["statement"] for n in safe] == [
        f"The cobalt service listens on port 8443.\n\nRestart it after a deploy.\n{WITHHELD_LINE}"
    ]
    assert excluded[0]["reason"] == "ContextLegacyLineWithheld"
    assert excluded[0]["statement_available"] is True
    assert excluded[0]["note_id"] == "legacy:7"
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == []
    assert not contains_secret(safe[0]["statement"])
    assert not contains_secret(context_text(safe))
    assert not contains_secret(json.dumps([{"content": context_text(safe)}]))
    assert not contains_secret(json.dumps([safe, excluded]))


def test_one_line_note_becomes_the_placeholder():
    # Nothing of the note is left but the placeholder. It is still available,
    # so the job does not wait on it.
    safe, excluded = isolate_notes([legacy("TOKEN=[REDACTED]")], max_chars=24000)
    assert safe[0]["statement"] == WITHHELD_LINE
    assert excluded[0]["reason"] == "ContextLegacyLineWithheld"
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == []


def test_marker_lines_of_every_shape_and_safe_notes_pass_the_full_context_check():
    notes = [legacy("The cobalt service uses port 443.", note_id="legacy:1")]
    notes += [
        legacy(f"Deploy notes {i}.\n\nCheck the logs.\n{shape}", note_id=f"legacy:{i + 10}")
        for i, shape in enumerate(SHAPES)
    ]
    # A note the whole-note swap still handles keeps its omission mark.
    notes.append(legacy("Run pytest -p [REDACTED] first.", note_id="legacy:99"))
    safe, excluded = isolate_notes(notes, max_chars=24000)
    assert len(safe) == len(notes)
    assert all(e["statement_available"] for e in excluded)
    reasons = [e["reason"] for e in excluded]
    assert reasons.count("ContextLegacyLineWithheld") == len(SHAPES)
    assert reasons.count("ContextLegacyMarkerReplaced") == 1
    assert safe[-1]["statement"] == f"Run pytest -p {OMISSION_MARK} first."
    text = context_text(safe)
    assert "REDACTED" not in text and not contains_secret(text)
    assert not contains_secret(json.dumps([{"role": "user", "content": text}]))


@pytest.mark.parametrize(
    "statement",
    [
        # The marker on the next line is the value of the header: once the
        # line is withheld, only the raw text reads the placeholder as the
        # token. The last line makes the whole-note swap fail.
        "curl -H 'Authorization: Bearer\n[REDACTED]' https://api.example.com\nGH_TOKEN=[REDACTED]",
        # The same with the key of a basic credential.
        "Authorization: Basic\n***REDACTED***\nPASSWORD=[REDACTED]",
    ],
)
def test_a_secret_split_across_lines_stays_withheld(statement):
    note = legacy(statement)
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["reason"] == "ContextRedactedCandidate"
    assert excluded[0]["statement_available"] is False
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == [0]
    assert not contains_secret(json.dumps([safe, excluded]))


def test_a_note_that_is_not_read_only_stays_withheld():
    note = legacy("The cobalt service uses port 443.\nTOKEN=[REDACTED]", read_only=False)
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["reason"] == "ContextRedactedCandidate"
    assert excluded[0]["statement_available"] is False
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == [0]


@pytest.mark.parametrize(
    "statement",
    [
        # A live credential on another line.
        f"TOKEN=[REDACTED]\nThe bot uses {FAKE_GITHUB_TOKEN} today.",
        # A natural-language secret on another line.
        "TOKEN=[REDACTED]\nThe password is fictional-value-9 for now.",
        # A failed redaction.
        "TOKEN=[REDACTED]\n***REDACTION_FAILED_TEXT_WITHHELD***",
        # A natural-language secret split across two lines: withholding the
        # marker line would hide it, but the note is unsafe from the start.
        "The password for the job\n is [REDACTED] now",
    ],
)
def test_an_unsafe_note_stays_withheld(statement):
    note = legacy(statement)
    assert rejection_reason(encoded(core(note))) == "ContextUnsafeCandidate"
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["reason"] == "ContextUnsafeCandidate"
    assert excluded[0]["statement_available"] is False
    assert not contains_secret(json.dumps([safe, excluded]))


@pytest.mark.parametrize(
    "statement",
    [
        # A line-start key that the encoded note does not show as a line start.
        "Deploy notes.\n  pass: Zq8hunter2example\nTOKEN=[REDACTED]",
        # A natural-language secret within the window of the raw text only:
        # each quote takes two characters in the encoded note.
        "TOKEN=[REDACTED]\n\nThe password" + ' "a"' * 12 + " is hunter-example-9",
        # A failed redaction in the raw text.
        "TOKEN=[REDACTED]\n***REDACTION_FAILED_TEXT_WITHHELD***",
    ],
)
def test_a_raw_secret_signal_besides_the_markers_keeps_the_note_withheld(statement):
    # neutral_note is called directly: the encoded check of isolate_notes may
    # miss these shapes, or it already rejects the note (failed redaction).
    assert neutral_note(legacy(statement)) is None


@pytest.mark.parametrize(
    "statement",
    [
        # NOBLIVION-259: the general path. A line-start key and a key whose
        # value is on the next line pass the encoded check only.
        "Deploy notes:\n  pass: Zq8hunter2example\nend",
        "curl -H 'Authorization: Bearer\nabcDEF123456ghijkl7890'",
        # The whole-note swap (Jira comment of 2026-11-16).
        "Deploy notes.\n  pass: Zq8hunter2example\nsee [REDACTED]",
        '{\n "api_key":\n "[REDACTED]",\n "client_secret":\n "Zq8hunter2examplevalue"\n}',
        # The redactor masked only the head of the value (comment of 2026-11-17).
        "db password: [REDACTED] tail9Word more",
    ],
)
def test_a_secret_that_only_the_raw_text_shows_stays_withheld(statement):
    safe, excluded = isolate_notes([legacy(statement)], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


# Synthetic base64 that the redactor masks after a BEGIN line only. The BEGIN
# lines are split so that no secret scanner reads this file as a key.
BASE64_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7Zq8Xy"
BEGIN_LINE = "-----BEGIN PRIVATE " + "KEY-----"


@pytest.mark.parametrize(
    "statement",
    [
        # The marker line between the BEGIN line and the key text cuts the
        # block: the redactor masks nothing in the raw text. The guard on
        # "private key" and the check of the kept lines each withhold it.
        f"{BEGIN_LINE}\n[REDACTED]\n{BASE64_LINE}\n{BASE64_LINE}\nTOKEN=[REDACTED]",
        # The BEGIN line holds the marker, so only the key text is kept, and
        # no line of it reads as a key: only the guard withholds it.
        f"{BEGIN_LINE} TOKEN=[REDACTED]\n{BASE64_LINE}\n{BASE64_LINE}",
        # The guard reads the words in any case.
        f"{BEGIN_LINE.title()} TOKEN=[REDACTED]\n{BASE64_LINE}\n{BASE64_LINE}",
    ],
)
def test_a_key_block_stays_withheld(statement):
    note = legacy(statement)
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["reason"] == "ContextRedactedCandidate"
    assert excluded[0]["statement_available"] is False
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == [0]
    assert BASE64_LINE not in json.dumps([safe, excluded])


@pytest.mark.parametrize(
    "statement",
    [
        "Basic\nTOKEN=[REDACTED]\n\ndXNlcjpwYXNzd29yZA==",
        "Bearer\nTOKEN=[REDACTED]\n\nzQ8vLm2Pw7RtXk4NabcdEFGH12",
    ],
)
def test_a_placeholder_line_does_not_hide_a_key_and_its_value(statement):
    note = legacy(statement)
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    # Precondition: the text with the placeholder passes every other check,
    # because the redactor reads the placeholder as no value. Only the kept
    # lines, joined without it, show the key and its value.
    lines = statement.split("\n")
    with_placeholder = "\n".join(WITHHELD_LINE if "REDACTED" in line else line for line in lines)
    assert not contains_secret(with_placeholder)
    assert rejection_reason(encoded(core(dict(note, statement=with_placeholder)))) is None
    assert contains_secret("\n".join(line for line in lines if "REDACTED" not in line))
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


def test_the_kept_lines_also_pass_as_encoded_note():
    marker_line = "TOKEN=[REDACTED]"
    statement = (
        f"Rotate the password\n{marker_line}\n\nthen restart the cobalt service after\n"
        "it is Zq8hunter2example"
    )
    note = legacy(statement)
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    # Precondition: the natural-language rule reads across lines in the
    # encoded note only. With the placeholder the key word and the value are
    # too far apart; the kept lines pass as raw text and fail as encoded note.
    with_placeholder = statement.replace(marker_line, WITHHELD_LINE)
    kept = statement.replace(f"{marker_line}\n", "")
    assert rejection_reason(encoded(core(dict(note, statement=with_placeholder)))) is None
    assert not contains_secret(with_placeholder) and not contains_secret(kept)
    assert rejection_reason(encoded(core(dict(note, statement=kept)))) is not None
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


def assert_withheld_as_on_main(statement):
    note = legacy(statement)
    # Main withholds every such note: its whole-note swap fails.
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["reason"] == "ContextRedactedCandidate"
    assert excluded[0]["statement_available"] is False


@pytest.mark.parametrize(
    "body",
    [
        "seed: " + "9f8e7d6c5b4a39281706f5e4d3c2b1a0" * 2,
        '{"kty": "OKP", "d": "KvipEEs_ZO1kDYx-KY0tSA8Do2EMvCszR0Mh7FkCSkh"}',
        "the token is ab12cd34ef",
    ],
)
def test_a_key_format_of_the_learning_check_is_an_unsafe_note(body):
    # NOBLIVION-262: a hit that only the learning check finds is a live
    # secret, never a legacy marker: a plain note and a marker note are both
    # withheld as unsafe, and the marker note never takes the line path.
    for statement in (body, f"{body}\n\nTOKEN=[REDACTED]"):
        note = legacy(statement)
        assert rejection_reason(encoded(core(note))) == "ContextUnsafeCandidate"
        assert neutral_note(note) is None
        safe, excluded = isolate_notes([note], max_chars=24000)
        assert safe == [] and excluded[0]["reason"] == "ContextUnsafeCandidate"


# Synthetic key text. The header lines are split so that no secret scanner
# reads this file as a key.
RSA_BEGIN = "-----BEGIN RSA PRIVATE " + "KEY-----"
OVPN_BEGIN = "-----BEGIN OpenVPN " + "Static key V1-----"
PPK_HEADER = "PuTTY-User-Key-" + "File-3: ssh-ed25519"
HEX_LINES = [f"{0x9F8E7D6C5B4A39281706F5E4D3C2B1A0 * (i + 3) % (1 << 128):032x}" for i in range(4)]


def test_key_text_withholds_a_cut_off_key_body():
    # A short line ends the cut-off block for the redactor: it masks the
    # header and the first line, and the body lines after the short line stay.
    # They hold no marker and no secret word, so each one passes on its own.
    residue = redact_at_rest(f"{RSA_BEGIN}\n{BASE64_LINE}\nAb3=\n{BASE64_LINE}\n{BASE64_LINE}")
    assert BASE64_LINE in residue and "PRIVATE" not in residue.upper()
    assert not contains_secret(BASE64_LINE)
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n\n{residue}")


@pytest.mark.parametrize("upper", [False, True])
def test_key_text_withholds_a_hex_key_body(upper):
    # An OpenVPN static key body: lines of 32 hex characters, no base64 case mix.
    body = "\r\n".join(line.upper() for line in HEX_LINES) if upper else "\n".join(HEX_LINES)
    assert redact_at_rest(body) == body
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n\n{body}")


@pytest.mark.parametrize(
    "residue",
    [
        # A cut-off PuTTY key file: its body line is too short for a run.
        f"{PPK_HEADER}\nEncryption: none\nPublic-Lines: 2\nAAAAC3NzaC1lZDI1",
        # An armor line with a cut-off hex body.
        f"{OVPN_BEGIN}\n9f8e7d6c5b4a",
    ],
)
def test_key_text_withholds_key_file_words(residue):
    assert redact_at_rest(residue) == residue
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n\n{residue}")


def test_key_text_keeps_a_commit_hash_in_prose():
    # A hash in prose is no key body: lower-case hex inside a line.
    statement = "Fixed in commit " + "9f8e7d6c5b4a3928170" * 2 + "ab today.\n\nTOKEN=[REDACTED]"
    result = neutral_note(legacy(statement))
    assert result is not None and result[1] == "ContextLegacyLineWithheld"


@pytest.mark.parametrize(
    "words",
    [
        "the password",
        # Within the window of the raw text only: each quote takes two
        # characters in the encoded note.
        "the password" + ' "a"' * 12,
    ],
)
def test_the_kept_lines_are_read_as_one_line(words):
    # The value line starts with the verb. NOBLIVION-262: the shared check now
    # reads a key word and the verb at the start of the next line.
    statement = f"TOKEN=[REDACTED]\n\n{words}\nTOKEN=[REDACTED]\n\nis hunter2"
    assert contains_secret(f"{words}\nis hunter2")
    assert_withheld_as_on_main(statement)


def test_the_kept_lines_are_read_as_one_line_in_the_encoded_note():
    # Only the encoded note reads the next field as the value after "is".
    statement = "TOKEN=[REDACTED]\n\nRotate the password\nTOKEN=[REDACTED]\n\nis"
    assert not contains_secret("Rotate the password is")
    assert_withheld_as_on_main(statement)


def test_the_raw_statement_is_read_as_one_line():
    # The key word is the name of the marker line, so the kept lines do not
    # hold it.
    statement = "PASSWORD=[REDACTED]\n\nis hunter2"
    assert not contains_secret("is hunter2")
    assert_withheld_as_on_main(statement)


def test_lines_are_cut_at_the_newline_only():
    # A kept line keeps its own "\r".
    result = neutral_note(legacy("Service on 8443.\r\nRestart.\n\nTOKEN=[REDACTED]"))
    assert result is not None and result[1] == "ContextLegacyLineWithheld"
    assert result[0]["statement"] == f"Service on 8443.\r\nRestart.\n\n{WITHHELD_LINE}"


@pytest.mark.parametrize(
    "statement",
    [
        # Any other line break stays inside the line. After a span it may
        # carry the rest of a value, so the note stays withheld.
        "Service on 8443.\r\nTOKEN=[REDACTED]\r\nRestart.",
        "Service on 8443.\u2028TOKEN=[REDACTED]\u2028Restart.",
        "Service on 8443.\u0085TOKEN=[REDACTED]\u0085Restart.",
        # A blank line follows: only the break or space after the span is left.
        "Service on 8443.\r\nTOKEN=[REDACTED]\r\n\r\nRestart.",
        "Service on 8443.\nTOKEN=[REDACTED]\u00a0\n\nRestart.",
        "Service on 8443.\nTOKEN=[REDACTED]\u2028\n\nRestart.",
    ],
)
def test_another_line_break_or_space_after_a_span_withholds_the_note(statement):
    assert_withheld_as_on_main(statement)


def test_a_cut_line_is_checked_in_its_parts():
    # The redactor reads "pass:" at a line start after "\n" only. The cut line
    # fails the part check, so the note stays withheld.
    statement = "Deploy notes.\r  pass: Zq8hunter2example TOKEN=[REDACTED]"
    assert redact_at_rest(statement) == statement
    # NOBLIVION-262: the shared check now reads each part after a "\r" too.
    assert contains_secret("Deploy notes.\r  pass: Zq8hunter2example [...]")
    assert_withheld_as_on_main(statement)


@pytest.mark.parametrize("line_break", ["\r", "\u2028", "\u0085"])
def test_a_part_after_another_line_break_is_checked_as_a_line(line_break):
    statement = f"Deploy notes.{line_break}  pass: Zq8hunter2example\nTOKEN=[REDACTED]"
    # Precondition: the redactor reads a line start after "\n" only, so the
    # raw statement passes it. NOBLIVION-262: the shared check reads every
    # other line break as "\n", so the first line fails it as its part does.
    assert redact_at_rest(statement) == statement
    assert contains_secret(statement.split("\n")[0])
    assert contains_secret("  pass: Zq8hunter2example")
    # The line is withheld for the part, and it is no marker line.
    assert neutral_note(legacy(statement)) is None


def test_the_placeholder_reads_as_no_value():
    # After a key the placeholder must not be read as a value, nor be one.
    assert not set(WITHHELD_LINE) & set("=:@")
    assert not re.search(r"(?i)token|secret|pass|key|auth|bearer|basic", WITHHELD_LINE)
    assert WITHHELD_LINE.splitlines() == [WITHHELD_LINE]
    assert not contains_secret(WITHHELD_LINE)


def test_a_line_that_fails_on_its_own_withholds_the_note(monkeypatch):
    # A line with no marker that fails the check on its own is no marker
    # line, so the note stays withheld. No such line passes the raw check of
    # the whole statement today, so the checks are replaced to reach the rule.
    real = learning_context.contains_secret
    monkeypatch.setattr(
        learning_context, "contains_secret", lambda text: "flagged" in text or real(text)
    )
    monkeypatch.setattr(learning_context, "redact_at_rest", lambda text: text)
    assert neutral_note(legacy("Deploy notes.\nplain line\nTOKEN=[REDACTED]")) is not None
    assert neutral_note(legacy("Deploy notes.\nflagged line\nTOKEN=[REDACTED]")) is None


def test_the_whole_note_swap_is_tried_first():
    result = neutral_note(legacy("Run pytest -p ***REDACTED*** first.\nThen deploy."))
    assert result is not None
    note, reason = result
    assert reason == "ContextLegacyMarkerReplaced"
    assert note["statement"] == f"Run pytest -p {OMISSION_MARK} first.\nThen deploy."


def test_title_and_evidence_are_not_part_of_the_line_check():
    note = legacy(
        "The cobalt service uses port 443.\nTOKEN=[REDACTED]",
        title="[REDACTED]",
        evidence=[{"quote": "[REDACTED]"}],
    )
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe[0]["statement"] == f"The cobalt service uses port 443.\n{WITHHELD_LINE}"
    assert safe[0]["title"] == "Existing memory" and safe[0]["evidence"] == []
    assert excluded[0]["statement_available"] is True
    assert not contains_secret(json.dumps([safe, excluded]))


# -- NOBLIVION-258 review 3: allowlist, fail closed ---------------------------


@pytest.mark.parametrize(
    ("held", "cut"),
    [
        # The span ends the line.
        ("export API_TOKEN=[REDACTED]", "export [...]"),
        ("Set it in the env file as API_TOKEN=[REDACTED]", "Set it in the env file as [...]"),
        # A scheme word goes with the span; nothing may follow it.
        ("Authorization: Bearer [REDACTED]", "[...]"),
        # Only closing punctuation follows the span.
        ("- set TOKEN=[REDACTED_EMAIL].", "- set [...]."),
        ("(see TOKEN: ***REDACTED***);", "(see [...]);"),
    ],
)
def test_a_marker_span_is_cut_out_of_a_line(held, cut):
    statement = f"The cobalt service uses port 443.\n\nRestart it.\n\n{held}"
    # Precondition: the line is withheld and no whole marker line.
    assert learning_context._line_withheld(held)
    assert not learning_context._MARKER_LINE.fullmatch(held)
    result = neutral_note(legacy(statement))
    assert result is not None and result[1] == "ContextLegacyLineWithheld"
    assert result[0]["statement"] == (f"The cobalt service uses port 443.\n\nRestart it.\n\n{cut}")


@pytest.mark.parametrize(
    "held",
    [
        # A bare marker: no name.
        "see [REDACTED] here",
        # A marker glued to text on either end.
        "TOKEN=[REDACTED]abc",
        "2FA_TOKEN=[REDACTED]",
        # A value after the span on the same line: the rest fails.
        "TOKEN=[REDACTED] password=Zq8hunter2example",
        # One marker is cut, the other has no name.
        "TOKEN=[REDACTED] and [REDACTED]",
        # Text after the span: it may be the rest of a value.
        "Run `TOKEN=[REDACTED] tool` before the deploy.",
        "- TOKEN=[REDACTED_EMAIL], then restart.",
        "A=[REDACTED] B: [REDACTED]",
        # Anything after a scheme span, even a closing quote.
        "curl -H 'Authorization: Basic ***REDACTED***' https://example.com",
        'curl -H "Authorization: Bearer [REDACTED]"',
        # A marker as the user of a credential, the store's other shape.
        'curl -u "[REDACTED]:$PASS" https://example.com',
        # A cut-off token is no span.
        "TOKEN=***REDACTED*",
    ],
)
def test_a_line_that_cannot_be_cut_withholds_the_note(held):
    statement = f"The cobalt service uses port 443.\nTOKEN=[REDACTED]\n\n{held}\n\nRestart it."
    assert learning_context._line_withheld(held)
    note = legacy(statement)
    # Main withholds every such note; the value case is unsafe from the start.
    assert rejection_reason(encoded(core(note))) is not None
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


@pytest.mark.parametrize(
    ("statement", "kept_fails"),
    [
        # The marker line holds the key word, so the kept lines do not.
        ("TOKEN=[REDACTED]\n\nthe password. [REDACTED]\nis hunter2", False),
        # A bare marker line between a key word and its value. NOBLIVION-262:
        # the kept lines ("the secret", "is hunter2") now fail on their own.
        ("TOKEN=[REDACTED]\n\nthe secret\n[REDACTED]\nis hunter2", True),
    ],
)
def test_a_held_line_with_other_text_does_not_cut_a_secret_apart(statement, kept_fails):
    assert kept_fails == contains_secret(
        "\n".join(line for line in statement.split("\n")[1:] if "REDACTED" not in line)
    )
    assert_withheld_as_on_main(statement)


def test_a_redactor_leftover_outside_the_marker_lines_withholds_the_note():
    # A cut-off token is no marker for LEGACY_MARKER and passes the check.
    leftover = "Old notes end in [REDACT here."
    assert not LEGACY_MARKER.search(leftover) and not contains_secret(leftover)
    assert neutral_note(legacy("Deploy notes.\nTOKEN=[REDACTED]")) is not None
    assert_withheld_as_on_main(f"Deploy notes.\nTOKEN=[REDACTED]\n\n{leftover}")


@pytest.mark.parametrize("words", ["private " + "key", "PRIVATE " + "KEY", "Private " + "Key"])
def test_private_key_words_withhold_the_note_before_the_swap(words):
    # Precondition: the omission mark alone passes, so the whole-note swap
    # would show the note.
    statement = f"Rotate the {words} [REDACTED] every month."
    swapped = statement.replace("[REDACTED]", OMISSION_MARK)
    assert rejection_reason(encoded(core(legacy(swapped)))) is None
    assert_withheld_as_on_main(statement)


# Review of 4178fb2: the redactor masked only the head of a value. Each raw
# text goes through the redactor first, as at rest.
@pytest.mark.parametrize(
    "raw",
    [
        # 1. A value with spaces: the tail after the masked word stays.
        "Set it with PASSWORD=correct horse battery staple before deploy.",
        "Set it with PASSWORD=abcd1234\u00a0wxyz9876 here",
        # 2. A password with "@": the tail after the first "@" stays.
        "Connect via postgres://app:Xy7@Kq9mPw3z@db:5432/prod for reports.",
        # 3. A quoted value across lines (a whole marker line, then the tail).
        'Export notes.\nAPI_TOKEN="ab12cd34\nef56gh78Qz"\nthen run the job.',
        # 4. A scheme with parameters: only the first one is masked.
        'Authorization: Digest username="bob", realm="r", response="66' + "29fae4" * 5 + '"',
        # 5. A value continued after "\r".
        "TOKEN=abc12345\rZq9xLm2Pw7Rt\nrun it.",
    ],
)
def test_a_value_masked_only_in_part_stays_withheld(raw):
    stored = redact_at_rest(raw)
    assert LEGACY_MARKER.search(stored)
    note = legacy(stored)
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


@pytest.mark.parametrize(
    "held",
    [
        # 6. Non-ASCII text glued to either end of the span.
        "x \u00e9TOKEN=[REDACTED]",
        "x TOKEN=[REDACTED]\u00e9q9Zk",
        "x TOKEN=[REDACTED]\uff17\uff18",
    ],
)
def test_non_ascii_text_glued_to_a_span_withholds_the_note(held):
    assert_withheld_as_on_main(f"Deploy notes.\nTOKEN=[REDACTED]\n\n{held}")


RUN_ON_RAWS = [
    # A quote that an apostrophe later on evens out (quote counting fails).
    "Run: export DB_PASSWORD='Xy7Qp9Lm2\nPw7RtZq9x' (it's in the vault)\nthen deploy.",
    # A quoted value over three lines (looking one line ahead fails).
    'Export API_TOKEN="ab12cd34\nef56gh78Qz\nij90kl12"\nthen run.',
    # A shell line continuation.
    "Run: export TOKEN=ab12cd34\\\nef56gh78Qz\nthen run.",
    # A folded header: a scheme span, then the indented rest.
    "Header was\nAuthorization: Bearer abcdefgh12345678\n  ijklmnop90123456\nend.",
    "Header was\nAuthorization: Basic dXNlcjpwYXNzd29yZA\n  ZXhhbXBsZXRhaWw=\nend.",
    # A whole marker line with an indented continuation.
    "config:\n  password: Xy7Qp9Lm2\n    Pw7RtZq9x\n  user: bob",
    # Review of da3a9f9: the same shapes with a blank or spaces-only line
    # inside. A blank line does not end the value.
    "Export API_TOKEN='ab12cd34\n\nef56gh78Qz' now.",
    'Export API_TOKEN="ab12cd34\n  \nef56gh78Qz" now.',
    "Run: export TOKEN=ab12cd34\n\nthen run.",
    "Header was\nAuthorization: Bearer abcdefgh12345678\n\n  ijklmnop90123456\nend.",
    "Header was\nAuthorization: Basic dXNlcjpwYXNzd29yZA\n \n  ZXhhbXBsZXRhaWw=\nend.",
    "config:\npassword: Xy7Qp9Lm2\n\nuser: bob",
]


# Review of 431302c: a marker at the end of a line does not mean the value
# ended there. The redactor masks a value up to the line end, and the rest may
# sit on the next line. Each raw text goes through the redactor first.
@pytest.mark.parametrize("raw", RUN_ON_RAWS)
def test_a_value_that_runs_on_after_the_marker_stays_withheld(raw):
    stored = redact_at_rest(raw)
    assert LEGACY_MARKER.search(stored)
    note = legacy(stored)
    # Precondition: a later line is not blank, also past a blank line.
    lines = stored.split("\n")
    held = [i for i, line in enumerate(lines) if learning_context._line_withheld(line)]
    assert held and any(rest.strip() for i in held for rest in lines[i + 1 :])
    assert neutral_note(note) is None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert safe == [] and excluded[0]["statement_available"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Only blank lines or the end of the text may follow.
        ("Run it.\n\nRun: export TOKEN=ab12cd34\n\n", "Run it.\n\nRun: export [...]\n\n"),
        ("Run: export TOKEN=ab12cd34", "Run: export [...]"),
        ("Header was\nAuthorization: Bearer abcdefgh12345678", "Header was\n[...]"),
        ("config:\npassword: Xy7Qp9Lm2\n \n\t\n", f"config:\n{WITHHELD_LINE}\n \n\t\n"),
    ],
)
def test_only_blank_lines_or_the_end_may_follow_the_marker(raw, expected):
    stored = redact_at_rest(raw)
    result = neutral_note(legacy(stored))
    assert result is not None and result[1] == "ContextLegacyLineWithheld"
    assert result[0]["statement"] == expected


@pytest.mark.parametrize("blank", ["\u00a0", "\x0b", "\x0c", "\u3000", "\u200b"])
def test_a_line_of_a_non_ascii_or_exotic_space_is_not_blank(blank):
    # Only spaces, tabs and a CR make a blank line. Any other character may be
    # the start of a value that runs on.
    note = legacy(f"Deploy.\nTOKEN=[REDACTED]\n{blank}\nPw7RtZq9x")
    assert neutral_note(note) is None
    assert neutral_note(legacy(f"Deploy.\nTOKEN=[REDACTED]\n{blank}")) is None


def review_reproducers():
    """The synthetic inputs of the three review rounds, each after a marker line."""
    marker = "TOKEN=[REDACTED]"
    raw = hashlib.sha512(b"k").digest() * 4
    b64 = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def wrap(text, width):
        return [text[i : i + width] for i in range(0, len(text), width)]

    def b64url(seed):
        return base64.urlsafe_b64encode(hashlib.sha512(seed).digest()).decode()[:43]

    hexes = [hashlib.md5(bytes([i])).hexdigest() for i in range(8)]
    seed_hex = hashlib.sha256(b"p").hexdigest()

    def colons(text):
        return ":".join(text[j : j + 2] for j in range(0, len(text), 2))

    bodies = [
        # Round 3.
        "\n".join(wrap(b64, 70)[:5]),
        "\n".join(wrap(b64, 36)[:6]),
        "\r".join(wrap(b64, 64)[:5]),
        " ".join(wrap(b64, 64)[:5]),
        "\r".join(hexes),
        " ".join(hexes),
        "\n".join(f"{a} {b}" for a, b in zip(hexes[::2], hexes[1::2], strict=True)),
        "priv:\n" + "\n".join("    " + colons(h[:30]) + ":" for h in hexes),
        "\n".join(h[:31] for h in hexes),
        '{"kty":"EC","crv":"P-256","d":"' + url[:43] + '","x":"' + url[43:86] + '"}',
        '"d": "' + url[:43] + '"',
        "AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOpQrSt",
        "\n".join(line.lower() for line in wrap(b64, 64)[:5]),
        "AGE-SECRET-" + "KEY-1" + "QYQSZQGP" * 8,
        "the password\n[REDACTED]\n= hunter2",
        "the password\nfoo. [REDACTED]\nis hunter2",
        "the password. [REDACTED]\nis hunter2",
        "the password.\n[REDACTED]\nis hunter2",
        "the secret\n[REDACTED]\nis hunter2",
        "the token\n[REDACTED]\nis hunter2",
        "password:\n[REDACTED]\nhunter2",
        "the password\rwas hunter2",
        "read EC key\npriv:\n    " + colons(seed_hex[:30]) + ":\n    " + colons(seed_hex[30:60]),
        "seed: " + hashlib.sha256(b"s").hexdigest(),
        *(
            json.dumps({"kty": "OKP", "crv": "Ed25519", "d": b64url(b"e%d" % i)}, indent=2)
            for i in range(8)
        ),
        # Round 2.
        "\n".join(HEX_LINES),
        "\r\n".join(line.upper() for line in HEX_LINES),
        "\n".join("> " + BASE64_LINE for _ in range(3)),
        "\n".join("+" + BASE64_LINE for _ in range(3)),
        redact_at_rest(f"{RSA_BEGIN}\n{BASE64_LINE}\nAb3=\n{BASE64_LINE}\n{BASE64_LINE}"),
        f"{PPK_HEADER}\nEncryption: none\nPublic-Lines: 2\nAAAAC3NzaC1lZDI1",
        "the password\nfoo. [REDACTED]\nis hunter2",
        # Round 1.
        "kind: Secret data:   db: QUJDREVGR0hJSktMTU5PUA==",
        "Authorization: Bearer\n[REDACTED]\nabcd1234efgh5678ijkl",
        "Basic\n[REDACTED]\ndXNlcjpwYXNzd29yZA==",
        "sk-proj-\n[REDACTED]\nabcdefghijklmnopqrstuvwx",
        "password=\n[REDACTED]\nhunter2hunter2",
        "postgres://admin:\n[REDACTED]\nS3cretPw@db.local",
        f"{BEGIN_LINE}\n[REDACTED]\n{BASE64_LINE}\n{BASE64_LINE}",
        *(f"note{brk}PASS: Zx9qLm2Pw7Rt" for brk in ["\x0b", "\x0c", "\x1c", "\x85", "\r", " "]),
        "Rotate the password\nexport SERVICE_TOKEN=[REDACTED] for the cobalt deploy job runner\n"
        "this is fine",
        # Controls.
        "Run the tests.",
        "The cobalt service uses port 443.",
    ]
    return (
        [f"{body}\n\n{marker}" for body in bodies]
        + [redact_at_rest(raw) for raw in RUN_ON_RAWS]
        + [
            "the password TOKEN=[REDACTED]\nis hunter2",
            "Rotate the password\nTOKEN=[REDACTED]\n" + "x" * 37 + "\nit is hunter2example",
            "Deploy notes.\r  pass: Zq8hunter2example\nTOKEN=[REDACTED]",
        ]
    )


def test_the_line_path_shows_only_text_a_plain_note_shows():
    shown = 0
    for statement in review_reproducers():
        result = neutral_note(legacy(statement))
        if result is None or result[1] != "ContextLegacyLineWithheld":
            continue
        shown += 1
        # Independent of the code under test: whatever line was withheld, the
        # value may run on, also past a blank line, so every later line is
        # blank.
        raw_lines = statement.split("\n")
        for i, line in enumerate(raw_lines):
            if learning_context._line_withheld(line):
                assert all(not rest.strip() for rest in raw_lines[i + 1 :]), statement
        # Every withheld line was a whole marker line or a line whose cut
        # passes the line check; no marker and no leftover remains.
        for line in statement.split("\n"):
            if learning_context._line_withheld(line) and not (
                learning_context._MARKER_LINE.fullmatch(line)
            ):
                cut = learning_context._MARKER_SPAN.sub(OMISSION_MARK, line)
                assert not learning_context._line_withheld(cut), statement
        assert "REDACT" not in result[0]["statement"]
        # The kept text, as a plain note without a marker, passes the shared
        # check: the line path shows nothing that such a note would not show.
        kept = "\n".join(
            line for line in result[0]["statement"].split("\n") if line != WITHHELD_LINE
        )
        assert not contains_secret(kept)
        safe, excluded = isolate_notes([legacy(kept)], max_chars=10**6)
        assert [note["statement"] for note in safe] == [kept] and excluded == []
    # Not vacuous: some inputs take the line path. NOBLIVION-262: the shared
    # check now finds the key formats of 10 reproducers (JWK "d", age key,
    # seed hex, colon hex, cluster Secret after U+2028), so 9 of 19 remain.
    assert shown >= 9


# -- worker ---------------------------------------------------------------------


@pytest.mark.parametrize("exhausted", [False, True])
def test_dependency_deferred_job_rebuilds_its_context_and_proceeds(ledger, monkeypatch, exhausted):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    notes = [legacy("The cobalt service uses port 443.\nGH_TOKEN=***REDACTED***")]
    calls = []

    def context(*args, **kwargs):
        calls.append(1)
        return notes

    ledger.reconciliation_context = context
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    messages_seen = []
    original = provider.complete

    def checked(messages, **kwargs):
        messages_seen.append(json.dumps(messages))
        return original(messages, **kwargs)

    provider.complete = checked
    run = worker(ledger, provider)
    # The release before this fix: the note is withheld and the job waits.
    monkeypatch.setattr(learning_context, "neutral_note", lambda note: None)
    first = run.run_once()
    assert first["status"] == "deferred" and first["error"] == "ContextDependencyDeferred"
    progress = json.loads(ledger.conn.execute("SELECT progress FROM learning_jobs").fetchone()[0])
    assert progress["dependency_deferred"] is True
    assert provider.calls == ["extract"]
    monkeypatch.undo()
    if exhausted:
        # A job that ran out of deferrals: the operator retries it by error.
        ledger.conn.execute(
            "UPDATE learning_jobs SET status='exhausted',last_error='ContextDependencyDeferred'"
        )
        assert len(ledger.retry_by_error("demo", "scope", "ContextDependencyDeferred")) == 1
    ready(ledger)
    second = run.run_once()
    assert second["status"] == "done" and second["results"][0]["action"] == "add"
    # The context was built again; the extraction receipts were kept.
    assert len(calls) == 2 and provider.calls == ["extract", "semantic"]
    assert WITHHELD_LINE in messages_seen[-1] and "REDACTED" not in messages_seen[-1]
    assert ledger.status()["context_rejections"]["ContextLegacyLineWithheld"] >= 1
    row = ledger.conn.execute("SELECT attempts,progress FROM learning_jobs").fetchone()
    assert not contains_secret(row["progress"])
    assert "dependency_deferred" not in json.loads(row["progress"])


def test_started_job_without_the_flag_keeps_its_stored_context(ledger):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    calls = []

    def context(*args, **kwargs):
        calls.append(1)
        return [legacy("The cobalt service uses port 443.")]

    ledger.reconciliation_context = context
    provider = ScriptedProvider(ledger, [candidate(record, category="fact")], "distinct")
    scripted = provider.complete
    semantic = []

    def complete(messages, **kwargs):
        if "sources" in json.loads(messages[-1]["content"]):
            return scripted(messages, **kwargs)
        semantic.append(1)
        if len(semantic) == 1:
            raise ProviderError("length", tokens=2048)
        return scripted(messages, **kwargs)

    provider.complete = complete
    run = worker(ledger, provider, max_seconds=1000)
    assert run.run_once()["status"] == "deferred"
    progress = json.loads(ledger.conn.execute("SELECT progress FROM learning_jobs").fetchone()[0])
    assert "dependency_deferred" not in progress and progress["existing_notes"]
    stored = progress["existing_notes"]
    ready(ledger)
    from dataclasses import replace

    from noblivion.learning import BudgetLimits
    from noblivion.learning_worker import LearningWorker, WorkerSettings

    recovered = LearningWorker(
        ledger,
        provider,
        WorkerSettings(max_output_tokens=8192, timeout_seconds=180),
        replace(BudgetLimits(), max_seconds=1000),
    )
    assert recovered.run_once()["status"] == "done"
    # No second build: the stored context of the started job was used.
    assert len(calls) == 1
    row = ledger.conn.execute("SELECT progress FROM learning_jobs").fetchone()
    assert json.loads(row["progress"])["existing_notes"] == stored
