"""DB-dependent retrieval tests: lexical, vector, hybrid, graph expansion.
Requires live PostgreSQL — same `db_session` fixture and explicit-skip
behavior as every other DB-dependent suite. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CodeChunk, Embedding, Symbol
from app.rag.embeddings import run_embedding_generation
from app.rag.graph_expansion import expand_graph
from app.rag.hybrid import combine_hybrid
from app.rag.lexical import search_lexical
from app.rag.vector import search_vector
from tests.ai.fakes import FakeEmbeddingProvider
from tests.rag.conftest import ingest_parse_graph_and_chunk


def _symbol_id(db: Session, revision_id: uuid.UUID, qualified_name: str) -> uuid.UUID:
    symbol = (
        db.execute(
            select(Symbol)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.qualified_name == qualified_name)
        )
        .scalars()
        .one()
    )
    return symbol.id


# ---- lexical -----------------------------------------------------------------


def test_lexical_full_text_search_finds_docstring_content(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    results = search_lexical(db_session, repository_id, revision_id, "authentication")
    assert len(results) > 0
    assert any("auth" in r.file_path for r in results)


def test_lexical_identifier_search_finds_exact_function_name(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    results = search_lexical(db_session, repository_id, revision_id, "apply_discount")
    assert any(r.qualified_name == "checkout.pricing.apply_discount" for r in results)


def test_lexical_search_is_revision_scoped(db_session: Session, rag_fixture_repo: Path) -> None:
    first_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo, owner="owner-a")
    second_result, _, _ = ingest_parse_graph_and_chunk(
        db_session, rag_fixture_repo, owner="owner-b"
    )
    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)

    results = search_lexical(db_session, first_repo, first_rev, "apply_discount")
    assert len(results) > 0
    chunk_ids_in_second = {
        c.id
        for c in db_session.execute(
            select(CodeChunk).where(CodeChunk.revision_id == second_rev)
        ).scalars()
    }
    assert all(r.chunk_id not in chunk_ids_in_second for r in results)


# ---- vector --------------------------------------------------------------------


def test_vector_search_finds_the_most_similar_chunk(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    provider = FakeEmbeddingProvider(dimension=1536)
    embed_result = run_embedding_generation(
        db_session, provider, "fake", "fake-model", "", 1536, repository_id, revision_id
    )
    assert embed_result.status == "completed"
    assert embed_result.embedded_count > 0

    apply_discount_symbol = _symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")
    chunk = (
        db_session.execute(select(CodeChunk).where(CodeChunk.symbol_id == apply_discount_symbol))
        .scalars()
        .one()
    )
    text = "def apply_discount(amount):\n    return amount * 0.9"
    query_vector = provider.embed([text])[0]

    results = search_vector(
        db_session, repository_id, revision_id, query_vector, "fake", "fake-model", ""
    )
    assert any(r.chunk_id == chunk.id for r in results)


def test_vector_search_is_revision_scoped(db_session: Session, rag_fixture_repo: Path) -> None:
    first_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo, owner="owner-a")
    second_result, _, _ = ingest_parse_graph_and_chunk(
        db_session, rag_fixture_repo, owner="owner-b"
    )
    provider = FakeEmbeddingProvider(dimension=1536)

    for result in (first_result, second_result):
        run_embedding_generation(
            db_session,
            provider,
            "fake",
            "fake-model",
            "",
            1536,
            uuid.UUID(result.repository_id),
            uuid.UUID(result.revision_id),
        )

    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)

    query_vector = provider.embed(["def apply_discount(amount):\n    return amount * 0.9"])[0]
    results = search_vector(
        db_session, first_repo, first_rev, query_vector, "fake", "fake-model", ""
    )

    second_chunk_ids = {
        c.id
        for c in db_session.execute(
            select(CodeChunk).where(CodeChunk.revision_id == second_rev)
        ).scalars()
    }
    assert all(r.chunk_id not in second_chunk_ids for r in results)


def test_vector_search_breaks_distance_ties_deterministically(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    """Several chunks given the *identical* vector (a zero-distance tie
    against the query) must still come back in the same order every time —
    ordering by distance alone is not enough to guarantee that under ties.
    """
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    provider = FakeEmbeddingProvider(dimension=1536)
    run_embedding_generation(
        db_session, provider, "fake", "fake-model", "", 1536, repository_id, revision_id
    )
    # FakeEmbeddingProvider derives a vector purely from text content, so
    # querying with the exact vector of one chunk does not itself produce
    # ties with *other* chunks' (different-text) vectors. Force genuine
    # ties directly by overwriting several embeddings to the same vector.
    tied_vector = [0.42] * 1536
    chunk_ids = [
        row[0]
        for row in db_session.execute(
            select(CodeChunk.id).where(CodeChunk.revision_id == revision_id).limit(3)
        ).all()
    ]
    for chunk_id in chunk_ids:
        db_session.execute(
            update(Embedding).where(Embedding.chunk_id == chunk_id).values(vector=tied_vector)
        )
    db_session.flush()

    first = search_vector(
        db_session, repository_id, revision_id, tied_vector, "fake", "fake-model", ""
    )
    second = search_vector(
        db_session, repository_id, revision_id, tied_vector, "fake", "fake-model", ""
    )
    assert [r.chunk_id for r in first] == [r.chunk_id for r in second]


def test_vector_search_handles_missing_embeddings_without_fabricating(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    # No embeddings generated at all for this revision.
    results = search_vector(
        db_session, repository_id, revision_id, [0.1] * 1536, "fake", "fake-model", ""
    )
    assert results == []


# ---- hybrid (DB-backed combination) --------------------------------------------


def test_hybrid_combines_lexical_and_vector_results(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    provider = FakeEmbeddingProvider(dimension=1536)
    run_embedding_generation(
        db_session, provider, "fake", "fake-model", "", 1536, repository_id, revision_id
    )

    lexical_results = search_lexical(db_session, repository_id, revision_id, "apply_discount")
    query_vector = provider.embed(["apply_discount"])[0]
    vector_results = search_vector(
        db_session, repository_id, revision_id, query_vector, "fake", "fake-model", ""
    )
    combined = combine_hybrid(lexical_results, vector_results)
    assert len(combined) > 0


# ---- graph expansion ------------------------------------------------------------


def test_graph_expansion_finds_callees_and_callers(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    calculate_total_id = _symbol_id(db_session, revision_id, "checkout.service.calculate_total")
    expanded = expand_graph(db_session, repository_id, revision_id, [calculate_total_id])

    qualified_names = {e.qualified_name for e in expanded}
    assert "auth.login.authenticate" in qualified_names
    assert "checkout.pricing.apply_discount" in qualified_names
    callees = {e.qualified_name: e.direction for e in expanded}
    assert callees["auth.login.authenticate"] == "callees"
    assert callees["checkout.pricing.apply_discount"] == "callees"


def test_graph_expansion_finds_callers_via_reverse_traversal(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    authenticate_id = _symbol_id(db_session, revision_id, "auth.login.authenticate")
    expanded = expand_graph(db_session, repository_id, revision_id, [authenticate_id])

    by_name = {e.qualified_name: e.direction for e in expanded}
    assert by_name["checkout.service.calculate_total"] == "callers"
    assert by_name["auth.login._decode"] == "callees"


def test_graph_expansion_never_reintroduces_the_seed(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    authenticate_id = _symbol_id(db_session, revision_id, "auth.login.authenticate")
    expanded = expand_graph(db_session, repository_id, revision_id, [authenticate_id])
    assert authenticate_id not in {e.symbol_id for e in expanded}


def test_graph_expansion_terminates_on_a_genuine_mutual_recursion_cycle(
    db_session: Session, rag_cyclic_repo: Path
) -> None:
    """`is_even` calls `is_odd` calls `is_even` — a true cycle, not just a
    seed re-appearing. Expansion must terminate (not loop forever) and must
    never report the seed itself as one of its own discovered results."""
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_cyclic_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    is_even_id = _symbol_id(db_session, revision_id, "mutual.is_even")
    expanded = expand_graph(
        db_session, repository_id, revision_id, [is_even_id], max_depth=10, max_symbols=50
    )

    by_name = {e.qualified_name: e.direction for e in expanded}
    assert by_name["mutual.is_odd"] in ("callees", "callers")
    assert is_even_id not in {e.symbol_id for e in expanded}
    assert len(expanded) <= 50


def test_graph_expansion_is_revision_scoped(db_session: Session, rag_fixture_repo: Path) -> None:
    first_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo, owner="owner-a")
    second_result, _, _ = ingest_parse_graph_and_chunk(
        db_session, rag_fixture_repo, owner="owner-b"
    )

    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)

    authenticate_id_first = _symbol_id(db_session, first_rev, "auth.login.authenticate")
    authenticate_id_second = _symbol_id(db_session, second_rev, "auth.login.authenticate")
    assert authenticate_id_first != authenticate_id_second

    expanded = expand_graph(db_session, first_repo, first_rev, [authenticate_id_first])
    assert all(e.symbol_id != authenticate_id_second for e in expanded)
