# SPDX-License-Identifier: AGPL-3.0-or-later
"""A note enters the prompt context only when it shares enough real words (NOBLIVION-160).

Before: one shared word of any kind was enough, so a long prompt (hundreds of words) matched
notes of other work through words such as "status", "summary", "home" or "command".
Now: generic words do not count, and a query of two or more real words needs two of them.
"""

from __future__ import annotations

from noblivion.learning_consolidation import _shares_terms, _terms
from test_learning_consolidation import learn, ledgers  # noqa: F401

ROOT = "demo"


def shown(result):
    return {c["statement"] for c in result["claims"]}


def test_a_note_with_one_shared_word_is_left_out(ledgers):  # noqa: F811
    ledger, store = ledgers
    both = "The cobalt service listens on port 4000."
    one = "The cobalt dashboard shows a chart."
    learn(ledger, 1, both, root=ROOT)
    learn(ledger, 2, one, root=ROOT)
    assert shown(store.context("test", ROOT, "cobalt service port")) == {both}


def test_a_note_that_shares_only_generic_words_is_left_out(ledgers):  # noqa: F811
    ledger, store = ledgers
    generic = "The status summary of the home folder lists each background command."
    real = "The cobalt service reports its status."
    learn(ledger, 1, generic, root=ROOT)
    learn(ledger, 2, real, root=ROOT)
    query = "cobalt service status summary home command background"
    assert shown(store.context("test", ROOT, query)) == {real}


def test_a_query_of_one_real_word_still_matches_on_that_word(ledgers):  # noqa: F811
    ledger, store = ledgers
    one = "The cobalt dashboard shows a chart."
    learn(ledger, 1, one, root=ROOT)
    assert shown(store.context("test", ROOT, "cobalt")) == {one}
    assert shown(store.context("test", ROOT, "the status of it")) == set()


def test_shares_terms_needs_two_real_words_or_all_of_one():
    query = _terms("cobalt service status summary")
    assert query == {"cobalt", "service"}
    assert _shares_terms(query, "the cobalt service")
    assert not _shares_terms(query, "the cobalt dashboard")
    assert not _shares_terms(query, "status summary of home")
    assert _shares_terms(_terms("cobalt"), "the cobalt dashboard")
    assert not _shares_terms(_terms("the status of it"), "the status of it")
