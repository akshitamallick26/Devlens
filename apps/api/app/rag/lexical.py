"""DB-touching lexical retrieval — Postgres full-text search plus exact/
substring identifier matching, revision-scoped and bounded. See
docs/architecture/rag-codebase-qa.md section 5.

No persisted/indexed tsvector column: `to_tsvector` is computed ad hoc,
scoped by revision first — acceptable at v1 corpus sizes, a documented
future optimization candidate otherwise (section 15).
"""

import uuid
from functools import reduce

from sqlalchemy import ColumnElement, Integer, case, func, literal, select
from sqlalchemy.orm import Session

from app.models import CodeChunk, SourceBlob, SourceFile, Symbol
from app.rag.models import RetrievedChunk
from app.rag.query import QueryTerms, normalize_question

DEFAULT_LEXICAL_TOP_K = 20


def search_lexical(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    query: str,
    top_k: int = DEFAULT_LEXICAL_TOP_K,
) -> list[RetrievedChunk]:
    """Two independent, deliberately different-grained passes:

    - Full-text search over whole-file content, surfacing docstrings/
      comments/natural-language mentions — represented by that file's
      `file_summary` chunk (file-level grain: this is the only way to
      search comment/docstring text without storing chunk text separately
      or re-deriving it per row, which would reintroduce the N+1 pattern
      app/rag/embeddings.py's own hardening removed).
    - Exact/substring identifier matching against `Symbol.qualified_name`
      (symbol-level grain: pinpoints the specific function/class/method a
      name query refers to).

    Both passes use terms from `normalize_question` (any matching term
    qualifies; more matching terms rank higher), never the raw question.
    Concatenated, full-text first; a chunk found by both appears once.
    Bounded to `top_k` total, revision- and repository-scoped throughout.
    A question with no usable terms returns nothing rather than everything.
    """
    terms = normalize_question(query)
    fts_chunks = _full_text_search(db, repository_id, revision_id, terms, top_k)
    seen_chunk_ids = {c.chunk_id for c in fts_chunks}

    remaining = top_k - len(fts_chunks)
    identifier_chunks = (
        _identifier_search(db, repository_id, revision_id, terms, seen_chunk_ids, remaining)
        if remaining > 0
        else []
    )

    combined = fts_chunks + identifier_chunks
    return [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            file_id=c.file_id,
            file_path=c.file_path,
            symbol_id=c.symbol_id,
            qualified_name=c.qualified_name,
            start_line=c.start_line,
            end_line=c.end_line,
            rank=rank,
        )
        for rank, c in enumerate(combined, start=1)
    ]


def _any_term_tsquery(terms: tuple[str, ...]) -> ColumnElement[object]:
    """`plainto_tsquery(t1) || plainto_tsquery(t2) || ...`: a document
    matches if it contains any term's lexemes. Each term is a bound
    parameter passed through plainto_tsquery, which never interprets
    operators, so no user text can inject tsquery syntax. A stop-word term
    yields an empty tsquery, which OR-combination ignores."""
    parts = [func.plainto_tsquery("english", literal(term)) for term in terms]
    return reduce(lambda left, right: left.op("||")(right), parts)


def _full_text_search(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    terms: QueryTerms,
    top_k: int,
) -> list[RetrievedChunk]:
    if not terms.fts_terms:
        return []
    document = func.to_tsvector("english", SourceBlob.inline_content)
    tsquery = _any_term_tsquery(terms.fts_terms)
    # ts_rank_cd over an OR query rewards documents covering more of the
    # question's terms, more densely; ties break on chunk id.
    rank = func.ts_rank_cd(document, tsquery).label("rank")

    rows = db.execute(
        select(
            CodeChunk.id,
            CodeChunk.file_id,
            SourceFile.path,
            CodeChunk.symbol_id,
            Symbol.qualified_name,
            CodeChunk.start_line,
            CodeChunk.end_line,
            rank,
        )
        .select_from(SourceFile)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .join(
            CodeChunk,
            (CodeChunk.file_id == SourceFile.id)
            & (CodeChunk.chunk_kind == "file_summary")
            # The first file_summary piece only — for a file oversized
            # enough to need multiple pieces, ts_rank_cd ranks the whole
            # blob, not a specific piece, so there's no principled way to
            # pick a different one; a documented v1 imprecision for that
            # rare case (section 15).
            & (CodeChunk.start_line == 1),
        )
        .outerjoin(Symbol, CodeChunk.symbol_id == Symbol.id)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceBlob.inline_content.is_not(None))
        .where(document.op("@@")(tsquery))
        .order_by(rank.desc(), CodeChunk.id.asc())
        .limit(top_k)
    ).all()
    return [
        RetrievedChunk(
            chunk_id=row[0],
            file_id=row[1],
            file_path=row[2],
            symbol_id=row[3],
            qualified_name=row[4],
            start_line=row[5],
            end_line=row[6],
            rank=index,
        )
        for index, row in enumerate(rows, start=1)
    ]


def _identifier_search(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    terms: QueryTerms,
    exclude_chunk_ids: set[uuid.UUID],
    top_k: int,
) -> list[RetrievedChunk]:
    """Symbols whose qualified name contains any identifier term (case-
    insensitive substring, LIKE wildcards in the term escaped), ranked by
    how many terms match, then by shorter (more specific) names."""
    if not terms.identifier_terms:
        return []
    matches = [
        Symbol.qualified_name.icontains(term, autoescape=True) for term in terms.identifier_terms
    ]
    score: ColumnElement[int] = literal(0, Integer)
    for match in matches:
        score = score + case((match, 1), else_=0)
    matched_terms = score.label("matched_terms")
    rows = db.execute(
        select(
            CodeChunk.id,
            CodeChunk.file_id,
            SourceFile.path,
            CodeChunk.symbol_id,
            Symbol.qualified_name,
            CodeChunk.start_line,
            CodeChunk.end_line,
            matched_terms,
        )
        .join(SourceFile, CodeChunk.file_id == SourceFile.id)
        .join(Symbol, CodeChunk.symbol_id == Symbol.id)
        .where(CodeChunk.repository_id == repository_id)
        .where(CodeChunk.revision_id == revision_id)
        .where(reduce(lambda left, right: left | right, matches))
        .order_by(
            matched_terms.desc(),
            func.length(Symbol.qualified_name).asc(),
            CodeChunk.id.asc(),
        )
        .limit(top_k + len(exclude_chunk_ids))
    ).all()
    results: list[RetrievedChunk] = []
    for row in rows:
        if row[0] in exclude_chunk_ids:
            continue
        results.append(
            RetrievedChunk(
                chunk_id=row[0],
                file_id=row[1],
                file_path=row[2],
                symbol_id=row[3],
                qualified_name=row[4],
                start_line=row[5],
                end_line=row[6],
                rank=len(results) + 1,
            )
        )
        if len(results) >= top_k:
            break
    return results
