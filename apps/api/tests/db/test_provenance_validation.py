"""Tests for the `@validates` application-level backstops added during the
database-hardening pass, for relationships where DB-level composite-FK
enforcement was ruled out (see "Revision-scoped reference enforcement" in
docs/architecture/database-design.md for why, per field). Each of these
raises ValueError from Python before the write ever reaches the database —
distinct from the IntegrityError raised by an actual DB constraint.
"""

import pytest
from sqlalchemy.orm import Session

from app.models import (
    AnalysisRun,
    CodeChunk,
    GeneratedArtifact,
    InvestigationSession,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SecurityFindingObservation,
    SourceFile,
    Symbol,
)
from tests.conftest import make_repo_and_revision


def test_code_chunk_symbol_from_another_revision_is_rejected(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="src/other.py",
        content_hash=source_file.content_hash,
        size_bytes=source_file.size_bytes,
    )
    db_session.add(other_file)
    db_session.flush()
    foreign_symbol = Symbol(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        file_id=other_file.id,
        kind="function",
        name="foo",
        qualified_name="other.foo",
        start_line=1,
        end_line=2,
        analyzer="tree-sitter-python@0.21",
    )
    db_session.add(foreign_symbol)
    db_session.flush()

    chunk = CodeChunk(
        repository_id=repository.id,
        revision_id=revision.id,
        file_id=source_file.id,
        chunk_kind="sliding_window",
        start_line=1,
        end_line=5,
    )
    db_session.add(chunk)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision_id"):
        chunk.symbol_id = foreign_symbol.id


def test_generated_artifact_related_file_from_another_revision_is_rejected(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="src/other.py",
        content_hash=source_file.content_hash,
        size_bytes=source_file.size_bytes,
    )
    db_session.add(other_file)
    db_session.flush()

    artifact = GeneratedArtifact(
        repository_id=repository.id,
        revision_id=revision.id,
        kind="documentation",
        title="README",
        format="markdown",
        content_hash=source_file.content_hash,
    )
    db_session.add(artifact)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision_id"):
        artifact.related_file_id = other_file.id


def test_security_finding_observation_file_from_another_revision_is_rejected(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="src/other.py",
        content_hash=source_file.content_hash,
        size_bytes=source_file.size_bytes,
    )
    db_session.add(other_file)
    db_session.flush()

    finding = SecurityFinding(
        repository_id=repository.id,
        analyzer="semgrep",
        rule_id="python.lang.security.audit",
        fingerprint="fp-1",
        severity="medium",
        title="x",
        message="y",
    )
    db_session.add(finding)
    db_session.flush()

    observation = SecurityFindingObservation(
        repository_id=repository.id,
        finding_id=finding.id,
        revision_id=revision.id,
        raw_finding={"tool": "semgrep"},
    )
    db_session.add(observation)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision_id"):
        observation.file_id = other_file.id


def test_security_finding_first_seen_run_from_another_repository_is_rejected(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    foreign_run = AnalysisRun(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        analysis_type="security_scan",
        analyzer_name="semgrep",
        analyzer_version="1.0.0",
    )
    db_session.add(foreign_run)
    db_session.flush()

    finding = SecurityFinding(
        repository_id=repository.id,
        analyzer="semgrep",
        rule_id="python.lang.security.audit",
        fingerprint="fp-2",
        severity="medium",
        title="x",
        message="y",
    )
    db_session.add(finding)
    db_session.flush()

    with pytest.raises(ValueError, match="different repository_id"):
        finding.first_seen_run_id = foreign_run.id


def test_repository_current_revision_from_another_repository_is_rejected(
    db_session: Session, repository: Repository
) -> None:
    """The strongest reasonable mechanism for this deliberately-special
    circular pointer: a DB composite FK can't work (it would try to null
    `id`, the primary key), so this is an ORM-level check instead."""
    _other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")

    with pytest.raises(ValueError, match="different repository"):
        repository.current_revision_id = other_revision.id


def test_investigation_session_current_revision_from_another_repository_is_rejected(
    db_session: Session, repository: Repository
) -> None:
    _other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")

    session_row = InvestigationSession(repository_id=repository.id, title="Investigation")
    db_session.add(session_row)
    db_session.flush()

    with pytest.raises(ValueError, match="different repository"):
        session_row.current_revision_id = other_revision.id
