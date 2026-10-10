"""End-to-end RAG/Codebase Q&A orchestration tests. Requires live
PostgreSQL — same `db_session` fixture and explicit-skip behavior as every
other DB-dependent suite. Never substitutes SQLite. Uses FakeProvider/
FakeEmbeddingProvider — no real LLM or embedding API call.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.rag.embeddings import run_embedding_generation
from app.rag.qa import QAAnswer, RevisionNotFoundError, run_qa
from tests.ai.fakes import FakeEmbeddingProvider, FakeProvider
from tests.rag.conftest import ingest_parse_graph_and_chunk

_VALID_ANSWER_KWARGS: dict[str, object] = dict(
    answer="`calculate_total` authenticates the request [Q1], then applies the discount [Q2].",
    evidence=["Q1", "Q2"],
    limitations="",
    related_symbols=["auth.login.authenticate", "checkout.pricing.apply_discount"],
    suggested_next_questions=[],
)


def _ids(db: Session, repo_path: Path, owner: str = "rag-test"):
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db, repo_path, owner=owner)
    return uuid.UUID(ingestion_result.repository_id), uuid.UUID(ingestion_result.revision_id)


def test_retrieval_only_when_no_llm_provider_configured(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    result = run_qa(
        db_session,
        None,
        None,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        "How does authentication work?",
    )
    assert result.analysis_status == "retrieval_only"
    assert result.answer is None
    assert len(result.evidence_set.items) > 0  # retrieval work still happened


def test_answered_when_llm_provider_succeeds(db_session: Session, rag_fixture_repo: Path) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    llm = FakeProvider(response=QAAnswer.model_validate(_VALID_ANSWER_KWARGS))

    result = run_qa(
        db_session,
        llm,
        None,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        "How does calculate_total work?",
    )
    assert result.analysis_status == "answered"
    assert result.answer is not None
    assert set(result.answer.evidence) <= set(result.evidence_map)


def test_llm_failure_falls_back_to_retrieval_only(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    llm = FakeProvider(error=RuntimeError("simulated failure"))

    result = run_qa(
        db_session,
        llm,
        None,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        # A question with real lexical evidence in the fixture, so the test
        # proves retrieved evidence survives the LLM failure (the previous
        # "a question" has no matching term and correctly retrieves nothing).
        "How does authentication work?",
    )
    assert result.analysis_status == "retrieval_only"
    assert result.answer is None
    assert len(result.evidence_set.items) > 0  # retrieval work survives the LLM failure


def test_vector_retrieval_failure_degrades_to_lexical_only(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    failing_embeddings = FakeEmbeddingProvider(error=RuntimeError("embedding provider down"))

    result = run_qa(
        db_session,
        None,
        failing_embeddings,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        "apply_discount",
    )
    # Must not raise — lexical retrieval alone still produces evidence.
    assert result.analysis_status == "retrieval_only"
    assert len(result.evidence_set.items) > 0


def test_full_pipeline_with_vector_and_graph_expansion(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    embeddings = FakeEmbeddingProvider(dimension=1536)
    run_embedding_generation(
        db_session, embeddings, "fake", "fake-model", "", 1536, repository_id, revision_id
    )
    llm = FakeProvider(response=QAAnswer.model_validate(_VALID_ANSWER_KWARGS))

    result = run_qa(
        db_session,
        llm,
        embeddings,
        "fake",
        "fake-model",
        "",
        repository_id,
        revision_id,
        "What does calculate_total call?",
    )
    assert result.analysis_status == "answered"
    sources = {item.retrieval_source for item in result.evidence_set.items}
    assert sources  # at least one retrieval source present


def test_revision_not_found_is_explicit(db_session: Session, rag_fixture_repo: Path) -> None:
    repository_id, _ = _ids(db_session, rag_fixture_repo)
    with pytest.raises(RevisionNotFoundError):
        run_qa(
            db_session,
            None,
            None,
            "openai",
            "m",
            "",
            repository_id,
            uuid.uuid4(),
            "q",
        )


def test_zero_retrieval_results_does_not_crash_or_fabricate(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    """A question matching nothing lexically or via vector search (no
    embedding provider configured here, so vector search is skipped
    entirely) must still produce a valid, non-fabricated result — either
    retrieval_only with empty evidence, or an LLM answer whose
    `limitations` field honestly reflects the lack of evidence, never a
    confident answer invented from nothing."""
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    result = run_qa(
        db_session,
        None,
        None,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        "zzzznonexistentqueryxyzzzz qwxyzzznomatch123",
    )
    assert result.analysis_status == "retrieval_only"
    assert result.answer is None
    assert result.evidence_set.items == ()


def test_zero_retrieval_results_with_llm_configured_does_not_fabricate(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, rag_fixture_repo)
    llm = FakeProvider(
        response=QAAnswer.model_validate(
            {
                "answer": "No relevant evidence was found for this question.",
                "evidence": [],
                "limitations": "No matching evidence was retrieved.",
                "related_symbols": [],
                "suggested_next_questions": [],
            }
        )
    )
    result = run_qa(
        db_session,
        llm,
        None,
        "openai",
        "m",
        "",
        repository_id,
        revision_id,
        "zzzznonexistentqueryxyzzzz qwxyzzznomatch123",
    )
    assert result.analysis_status == "answered"
    assert result.evidence_set.items == ()
    assert result.answer is not None
    assert result.answer.evidence == []


def test_cross_revision_contamination_never_occurs(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    repo_a, rev_a = _ids(db_session, rag_fixture_repo, owner="owner-a")
    repo_b, rev_b = _ids(db_session, rag_fixture_repo, owner="owner-b")
    assert rev_a != rev_b

    result = run_qa(
        db_session,
        None,
        None,
        "openai",
        "m",
        "",
        repo_a,
        rev_a,
        "apply_discount",
    )
    for item in result.evidence_set.items:
        assert item.revision_id == rev_a
        assert item.revision_id != rev_b
