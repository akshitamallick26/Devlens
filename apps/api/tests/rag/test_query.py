"""Pure tests for app/rag/query.py: deterministic, bounded, operator-free
normalization of questions into lexical search terms."""

import re

import pytest

from app.rag.query import (
    MAX_FTS_TERMS,
    MAX_IDENTIFIER_TERMS,
    MAX_QUESTION_CHARS,
    normalize_question,
)

# Characters that carry meaning in tsquery syntax or LIKE patterns.
_OPERATOR_CHARS = re.compile(r"[&|!:*()<>'\"%\\-]")


def test_natural_language_question_keeps_every_word_as_an_independent_term() -> None:
    terms = normalize_question("What does the authentication middleware do?")
    assert terms.fts_terms == ("what", "does", "the", "authentication", "middleware", "do")
    # Filler words are not used as substring patterns against symbol names.
    assert terms.identifier_terms == ("authentication", "middleware")


def test_dotted_and_snake_case_identifiers_stay_whole_for_identifier_matching() -> None:
    terms = normalize_question("Who calls billing.ledger.post_entry?")
    assert "billing.ledger.post_entry" in terms.identifier_terms
    assert "post_entry" in terms.fts_terms


def test_punctuation_quotes_and_tsquery_operators_never_survive() -> None:
    hostile = "auth & token | !admin :* (x) <-> 'quoted' \"dq\" 100% a_b -- ; DROP TABLE x;"
    terms = normalize_question(hostile)
    for term in (*terms.fts_terms, *terms.identifier_terms):
        assert not _OPERATOR_CHARS.search(term), term
    assert "auth" in terms.fts_terms and "token" in terms.fts_terms


def test_like_wildcards_alone_produce_no_terms() -> None:
    assert normalize_question("%").fts_terms == ()
    assert normalize_question("%").identifier_terms == ()
    assert normalize_question("___").identifier_terms == ("___",)  # a literal, escaped later


def test_repeated_words_are_deduplicated_in_first_seen_order() -> None:
    terms = normalize_question("token token TOKEN refresh token")
    assert terms.fts_terms == ("token", "refresh")


@pytest.mark.parametrize("question", ["", "   ", "?!.,;", "\n\t"])
def test_empty_or_punctuation_only_questions_produce_no_terms(question: str) -> None:
    terms = normalize_question(question)
    assert terms.fts_terms == () and terms.identifier_terms == ()


def test_stop_word_only_question_has_no_identifier_terms() -> None:
    # Full-text search drops English stop words itself (plainto_tsquery of a
    # stop word is empty), so these find nothing in either pass.
    assert normalize_question("what is the how does it").identifier_terms == ()


def test_very_long_questions_and_term_counts_are_bounded() -> None:
    long_question = " ".join(f"term{i}" for i in range(5000))
    terms = normalize_question(long_question)
    assert len(terms.fts_terms) == MAX_FTS_TERMS
    assert len(terms.identifier_terms) == MAX_IDENTIFIER_TERMS
    # Characters past the cap are never even tokenized.
    tail_only = "x" * MAX_QUESTION_CHARS + " uniqueterm"
    assert "uniqueterm" not in normalize_question(tail_only).fts_terms


def test_unicode_words_are_kept() -> None:
    terms = normalize_question("Wo wird die Authentifizierung geprüft? héritage 認証")
    assert "geprüft" in terms.fts_terms
    assert "héritage" in terms.fts_terms
    assert "認証" in terms.fts_terms


def test_normalization_is_deterministic() -> None:
    question = "How does refund() call post_entry and validate?"
    assert normalize_question(question) == normalize_question(question)
