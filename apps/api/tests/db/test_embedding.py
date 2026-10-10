import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisRun, CodeChunk, Embedding, Repository, RepositoryRevision, SourceFile
from tests.conftest import make_repo_and_revision

DIMENSION = 1536


def _chunk(
    repository: Repository, revision: RepositoryRevision, source_file: SourceFile
) -> CodeChunk:
    return CodeChunk(
        repository_id=repository.id,
        revision_id=revision.id,
        file_id=source_file.id,
        chunk_kind="symbol_body",
        start_line=1,
        end_line=3,
    )


def _embedding(chunk: CodeChunk, **kwargs) -> Embedding:
    defaults = dict(
        chunk_id=chunk.id,
        provider="voyageai",
        model_name="voyage-code-3",
        model_version="",
        dimension=DIMENSION,
        vector=[0.0] * DIMENSION,
    )
    defaults.update(kwargs)
    return Embedding(**defaults)


def test_embedding_is_unique_per_chunk_provider_model_and_version(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    chunk = _chunk(repository, revision, source_file)
    db_session.add(chunk)
    db_session.flush()

    db_session.add(_embedding(chunk))
    db_session.flush()

    db_session.add(_embedding(chunk))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_two_model_versions_can_coexist_for_the_same_chunk(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    """The point of a separate Embedding table: re-embedding with a newer
    model must not require destroying the old embedding first."""
    chunk = _chunk(repository, revision, source_file)
    db_session.add(chunk)
    db_session.flush()

    db_session.add_all(
        [
            _embedding(chunk, model_name="voyage-code-2"),
            _embedding(chunk, model_name="voyage-code-3"),
        ]
    )
    db_session.flush()  # must not raise


def test_different_providers_of_the_same_model_name_can_coexist(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    """provider is part of the identity, not just model_name — two
    providers happening to use the same model_name string are distinct."""
    chunk = _chunk(repository, revision, source_file)
    db_session.add(chunk)
    db_session.flush()

    db_session.add_all(
        [
            _embedding(chunk, provider="voyageai"),
            _embedding(chunk, provider="local"),
        ]
    )
    db_session.flush()  # must not raise


def test_analysis_run_from_another_revision_is_rejected_by_the_application_layer(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    """Application-validated two-hop check: Embedding has no revision_id of
    its own, so `_validate_analysis_run_id` looks up both the chunk and the
    run to compare their revisions."""
    chunk = _chunk(repository, revision, source_file)
    db_session.add(chunk)
    db_session.flush()

    _other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    foreign_run = AnalysisRun(
        repository_id=other_revision.repository_id,
        revision_id=other_revision.id,
        analysis_type="rag_indexing",
        analyzer_name="voyageai",
        analyzer_version="voyage-code-3",
    )
    db_session.add(foreign_run)
    db_session.flush()

    embedding = _embedding(chunk)
    db_session.add(embedding)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision"):
        embedding.analysis_run_id = foreign_run.id
