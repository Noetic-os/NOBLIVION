# SPDX-License-Identifier: AGPL-3.0-or-later
"""A legacy marker after a key withholds its line, not the note (NOBLIVION-258).

The omission mark reads as a value after a key (``TOKEN=[...]``), so such a
read-only note stayed withheld and every new note of its job waited on it until
the job gave up. Each line with a marker is now withheld whole instead. A note
with any other secret signal stays withheld, as before.
"""

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


SHAPES = [
    "TOKEN=[REDACTED]",
    "GH_TOKEN=***REDACTED***",
    "PASSWORD=[REDACTED]",
    "export API_TOKEN=[REDACTED] before the run",
    "Authorization: Bearer [REDACTED]",
    "Authorization: Basic ***REDACTED***",
    "curl -H 'Authorization: Bearer ***REDACTED***' https://api.example.com/v1",
    "postgresql://app:[REDACTED]@db.example.com:5432/app",
    "TOKEN=[REDACTED_EMAIL]",
]


@pytest.mark.parametrize("shape", SHAPES)
def test_marker_after_a_key_withholds_only_its_line(shape):
    statement = f"The cobalt service listens on port 8443.\n{shape}\nRestart it after a deploy."
    note = legacy(statement)
    # Preconditions: the only signal is the marker, and the omission mark
    # alone fails, so this is the line path and not the whole-note swap.
    assert rejection_reason(encoded(core(note))) == "ContextRedactedCandidate"
    swapped = dict(note, statement=LEGACY_MARKER.sub(OMISSION_MARK, statement))
    assert rejection_reason(encoded(core(swapped))) is not None
    safe, excluded = isolate_notes([note], max_chars=24000)
    assert [n["statement"] for n in safe] == [
        f"The cobalt service listens on port 8443.\n{WITHHELD_LINE}\nRestart it after a deploy."
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
    # The reproducer of the issue: nothing of the note is left but the
    # placeholder. It is still available, so the job does not wait on it.
    safe, excluded = isolate_notes([legacy("run `TOKEN=[REDACTED] tool`")], max_chars=24000)
    assert safe[0]["statement"] == WITHHELD_LINE
    assert excluded[0]["reason"] == "ContextLegacyLineWithheld"
    assert dependent_decisions([{"action": "add", "scope": "project"}], excluded) == []


def test_marker_lines_of_every_shape_and_safe_notes_pass_the_full_context_check():
    notes = [legacy("The cobalt service uses port 443.", note_id="legacy:1")]
    notes += [
        legacy(f"Deploy notes {i}.\n{shape}\nCheck the logs.", note_id=f"legacy:{i + 10}")
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
        # A natural-language secret across lines: only the encoded note joins
        # the key word and the verb once the long marker line is withheld.
        "Rotate the password\nexport SERVICE_TOKEN=[REDACTED] for the cobalt deploy job runner\n"
        "this is fine",
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
        "TOKEN=[REDACTED]\nThe password" + ' "a"' * 12 + " is hunter-example-9",
        # A failed redaction in the raw text.
        "TOKEN=[REDACTED]\n***REDACTION_FAILED_TEXT_WITHHELD***",
    ],
)
def test_a_raw_secret_signal_besides_the_markers_keeps_the_note_withheld(statement):
    # neutral_note is called directly: the encoded check of isolate_notes may
    # miss these shapes, or it already rejects the note (failed redaction).
    assert neutral_note(legacy(statement)) is None


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
        "Basic\n[REDACTED]\ndXNlcjpwYXNzd29yZA==\nTOKEN=[REDACTED]",
        "Bearer\n[REDACTED]\nzQ8vLm2Pw7RtXk4NabcdEFGH12\nTOKEN=[REDACTED]",
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
        f"Rotate the password\n{marker_line}\nthen restart the cobalt service after\n"
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
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n{residue}")


@pytest.mark.parametrize("upper", [False, True])
def test_key_text_withholds_a_hex_key_body(upper):
    # An OpenVPN static key body: lines of 32 hex characters, no base64 case mix.
    body = "\r\n".join(line.upper() for line in HEX_LINES) if upper else "\n".join(HEX_LINES)
    assert redact_at_rest(body) == body
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n{body}")


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
    assert_withheld_as_on_main(f"TOKEN=[REDACTED]\n{residue}")


def test_key_text_keeps_a_commit_hash_in_prose():
    # A hash in prose is no key body: lower-case hex inside a line.
    statement = "TOKEN=[REDACTED]\nFixed in commit " + "9f8e7d6c5b4a3928170" * 2 + "ab today."
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
    # The value line starts with the verb. In the raw text the rule stops at
    # "\n", and in the encoded note "\\nis" has no word start before "is".
    statement = f"TOKEN=[REDACTED]\n{words}\nfoo. [REDACTED]\nis hunter2"
    assert not contains_secret(f"{words}\nis hunter2")
    assert_withheld_as_on_main(statement)


def test_the_kept_lines_are_read_as_one_line_in_the_encoded_note():
    # Only the encoded note reads the next field as the value after "is".
    statement = "TOKEN=[REDACTED]\nRotate the password\nfoo. [REDACTED]\nis"
    assert not contains_secret("Rotate the password is")
    assert_withheld_as_on_main(statement)


def test_the_raw_statement_is_read_as_one_line():
    # The key word sits on the withheld line, so the kept lines do not hold it.
    statement = "the password TOKEN=[REDACTED]\nis hunter2"
    assert not contains_secret("is hunter2")
    assert_withheld_as_on_main(statement)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # Lines are cut at "\n" only. The "\r" of a withheld line goes with
        # it; a kept line keeps its own.
        (
            "Service on 8443.\r\nTOKEN=[REDACTED]\r\nRestart.",
            f"Service on 8443.\r\n{WITHHELD_LINE}\nRestart.",
        ),
        # Any other line break stays inside the line, so the marker withholds
        # the whole run: no part of it is kept because of how it was cut.
        ("Service on 8443. TOKEN=[REDACTED] Restart.", WITHHELD_LINE),
        ("Service on 8443.\u0085TOKEN=[REDACTED]\u0085Restart.", WITHHELD_LINE),
        (
            "Service on 8443.\rTOKEN=[REDACTED]\rRestart.\nCheck the logs.",
            f"{WITHHELD_LINE}\nCheck the logs.",
        ),
    ],
)
def test_lines_are_cut_at_the_newline_only(statement, expected):
    result = neutral_note(legacy(statement))
    assert result is not None
    assert result[0]["statement"] == expected
    assert result[1] == "ContextLegacyLineWithheld"


@pytest.mark.parametrize("line_break", ["\r", " ", "\u0085"])
def test_a_part_after_another_line_break_is_checked_as_a_line(line_break):
    statement = f"Deploy notes.{line_break}  pass: Zq8hunter2example\nTOKEN=[REDACTED]"
    # Precondition: the redactor reads a line start after "\n" only, so the
    # raw statement and its first line pass; the part after the break fails.
    assert redact_at_rest(statement) == statement
    assert not contains_secret(statement.split("\n")[0])
    assert contains_secret("  pass: Zq8hunter2example")
    result = neutral_note(legacy(statement))
    assert result is not None
    assert result[0]["statement"] == f"{WITHHELD_LINE}\n{WITHHELD_LINE}"
    assert "Zq8hunter2example" not in json.dumps(result)


def test_the_placeholder_reads_as_no_value():
    # After a key the placeholder must not be read as a value, nor be one.
    assert not set(WITHHELD_LINE) & set("=:@")
    assert not re.search(r"(?i)token|secret|pass|key|auth|bearer|basic", WITHHELD_LINE)
    assert WITHHELD_LINE.splitlines() == [WITHHELD_LINE]
    assert not contains_secret(WITHHELD_LINE)


def test_a_line_that_fails_on_its_own_is_withheld(monkeypatch):
    # A line with no marker that fails the check on its own is withheld too.
    # No such line passes the raw check of the whole statement today, so the
    # whole-statement check is replaced here to reach the line rule.
    real = learning_context.contains_secret
    monkeypatch.setattr(
        learning_context, "contains_secret", lambda text: "flagged" in text or real(text)
    )
    monkeypatch.setattr(learning_context, "redact_at_rest", lambda text: text)
    result = neutral_note(legacy("Deploy notes.\nflagged line\nTOKEN=[REDACTED]"))
    assert result is not None
    assert result[0]["statement"] == f"Deploy notes.\n{WITHHELD_LINE}\n{WITHHELD_LINE}"


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


# -- worker ---------------------------------------------------------------------


@pytest.mark.parametrize("exhausted", [False, True])
def test_dependency_deferred_job_rebuilds_its_context_and_proceeds(ledger, monkeypatch, exhausted):
    record = capture(ledger, "new", "The indigo service uses port 8443.")
    notes = [legacy("The cobalt service uses port 443.\nexport GH_TOKEN=***REDACTED***")]
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
