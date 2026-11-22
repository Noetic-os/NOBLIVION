# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plain words and trailing dots do not join a note to a long prompt (NOBLIVION-283).

Fictional data only.
"""

from noblivion.learning_consolidation import _shares_terms, _terms

LONG = (
    "check if the otter ledger has been fixed and also check this statement: "
    "the last heron measurement still stands. Cost 1.40x with 1.94x the input tokens. "
    "Todays log shows no sign that the gap has closed."
)


def test_plain_words_are_not_terms():
    terms = _terms(LONG)
    for word in ("check", "been", "has", "also", "last", "still", "statement", "todays"):
        assert word not in terms


def test_a_trailing_dot_does_not_stay_on_a_word():
    assert "closed" in _terms(LONG)
    assert "closed." not in _terms(LONG)


def test_two_plain_words_do_not_join_a_note_to_a_long_prompt():
    assert not _shares_terms(_terms(LONG), "Check that my statement has been logged as a fact.")


def test_a_long_prompt_needs_three_real_words():
    terms = _terms(LONG)
    assert len(terms) >= 12
    assert not _shares_terms(terms, "The otter ledger is empty.")
    assert _shares_terms(terms, "The otter ledger cost 1.40x of the heron measurement.")


def test_a_short_prompt_still_needs_two_real_words():
    assert _shares_terms(_terms("otter ledger"), "The otter ledger is empty.")
    assert not _shares_terms(_terms("otter ledger"), "The otter sleeps.")
