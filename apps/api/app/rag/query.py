"""Pure, deterministic normalization of a natural-language question into
lexical search terms. No DB, no I/O. See docs/architecture/rag-codebase-qa.md
section 5.

Why: Postgres `plainto_tsquery(question)` ANDs every non-stop-word lexeme,
so "How does authentication work?" only matched documents containing both
"authentication" and "work". Lexical retrieval instead matches documents
containing *any* useful term and ranks those matching more terms higher
(`lexical.py`).

Safety: terms are word tokens only (`\\w+`, Unicode-aware); no character a
tsquery or LIKE pattern treats as an operator ever survives
tokenization, and every term reaches SQL as a bound parameter. Input and
term counts are bounded, so a pathological question cannot produce an
unbounded query.
"""

import re
from dataclasses import dataclass

MAX_QUESTION_CHARS = 2000
MAX_FTS_TERMS = 32
MAX_IDENTIFIER_TERMS = 8
MIN_IDENTIFIER_TERM_CHARS = 3

_WORD = re.compile(r"\w+", re.UNICODE)
# A code identifier possibly qualified with dots ("billing.ledger.post_entry").
_IDENTIFIER = re.compile(r"\w+(?:\.\w+)*", re.UNICODE)

# Interrogative and filler words common in developer questions. Full-text
# search already drops English stop words itself; this list only keeps
# such words from being used as substring patterns against symbol names,
# where "what" or "does" would match unrelated identifiers.
_QUESTION_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "explain",
        "find",
        "for",
        "from",
        "get",
        "gets",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "show",
        "so",
        "some",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "used",
        "uses",
        "using",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "work",
        "works",
        "would",
        "you",
        "your",
    }
)


@dataclass(frozen=True)
class QueryTerms:
    # Distinct lowercase word tokens, in first-seen order: each becomes one
    # plainto_tsquery(term) OR-ed with the others.
    fts_terms: tuple[str, ...]
    # Identifier-like tokens (dotted names kept whole) for substring matching
    # against Symbol.qualified_name.
    identifier_terms: tuple[str, ...]


def _distinct(values: list[str], limit: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))[:limit]


def normalize_question(question: str) -> QueryTerms:
    text = question[:MAX_QUESTION_CHARS]
    fts_terms = _distinct([w.lower() for w in _WORD.findall(text)], MAX_FTS_TERMS)
    identifier_terms = _distinct(
        [
            token.lower()
            for token in _IDENTIFIER.findall(text)
            if len(token) >= MIN_IDENTIFIER_TERM_CHARS and token.lower() not in _QUESTION_STOP_WORDS
        ],
        MAX_IDENTIFIER_TERMS,
    )
    return QueryTerms(fts_terms=fts_terms, identifier_terms=identifier_terms)
