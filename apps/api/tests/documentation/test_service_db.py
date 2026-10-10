"""Hardening-style DB tests for app/documentation/service.py — end-to-end
orchestration, target validation error mapping, retry/failure semantics,
and revision isolation (Task 19). Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite. No test here calls a real LLM — `llm_provider=None` or a
tests/ai/fakes.py::FakeProvider, never a network client.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import AIProviderUnavailableError
from app.documentation import service
from app.models import AnalysisRun, GeneratedArtifact, SourceBlob, SourceFile, Symbol
from tests.ai.fakes import FakeProvider
from tests.automation.conftest import analyze_revision, ingest_revision, modify_and_commit
from tests.conftest import make_repo_and_revision


def test_repository_overview_end_to_end_uses_fallback_without_provider(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)

    outcome = service.generate_documentation(
        db_session, None, repository_id, revision_id, "repository_overview"
    )
    assert outcome.generation_method == "deterministic_fallback"
    assert outcome.provider == ""
    assert "Repository Overview" in outcome.title
    assert outcome.analysis_run_id is not None

    run = db_session.get(AnalysisRun, outcome.analysis_run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.analysis_type == "documentation_generation"


def test_provider_failure_persists_as_fallback_never_as_llm_generated(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)

    outcome = service.generate_documentation(
        db_session,
        FakeProvider(error=AIProviderUnavailableError("network down")),
        repository_id,
        revision_id,
        "repository_overview",
        provider_name="anthropic",
        model_name="claude-x",
        model_version="1",
    )
    assert outcome.generation_method == "deterministic_fallback"
    assert (outcome.provider, outcome.model_name, outcome.model_version) == ("", "", "")

    artifact = db_session.get(GeneratedArtifact, outcome.artifact_id)
    assert artifact is not None
    assert artifact.generation_method == "deterministic_fallback"
    assert artifact.provider == ""


def test_symbol_documentation_end_to_end(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)

    symbol = db_session.execute(
        select(Symbol)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.qualified_name == "app.math_utils.add")
    ).scalar_one()

    outcome = service.generate_documentation(
        db_session,
        None,
        repository_id,
        revision_id,
        "symbol_documentation",
        symbol_id=symbol.id,
    )
    assert outcome.target_symbol_id == symbol.id
    assert "app.math_utils.add" in outcome.title


def test_invalid_artifact_type_raises(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    with pytest.raises(service.InvalidArtifactTypeError):
        service.generate_documentation(
            db_session,
            None,
            uuid.UUID(result.repository_id),
            uuid.UUID(result.revision_id),
            "not_a_real_type",
        )


def test_revision_not_found_raises(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    with pytest.raises(service.RevisionNotFoundError):
        service.generate_documentation(
            db_session,
            None,
            uuid.UUID(result.repository_id),
            uuid.uuid4(),
            "repository_overview",
        )


def test_missing_target_raises(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    with pytest.raises(service.MissingTargetError):
        service.generate_documentation(
            db_session,
            None,
            uuid.UUID(result.repository_id),
            uuid.UUID(result.revision_id),
            "module_documentation",
        )


def test_unexpected_target_raises(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    with pytest.raises(service.UnexpectedTargetError):
        service.generate_documentation(
            db_session,
            None,
            uuid.UUID(result.repository_id),
            uuid.UUID(result.revision_id),
            "repository_overview",
            source_file_id=uuid.uuid4(),
        )


def test_target_not_found_raises(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    with pytest.raises(service.TargetNotFoundError):
        service.generate_documentation(
            db_session,
            None,
            uuid.UUID(result.repository_id),
            uuid.UUID(result.revision_id),
            "symbol_documentation",
            symbol_id=uuid.uuid4(),
        )


def test_target_from_another_repository_is_a_conflict(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    existing = service.generate_documentation(
        db_session, None, repository_id, revision_id, "repository_overview"
    )

    # A fully valid repository B: symbols(revision_id, file_id) is a
    # composite FK to source_files, so the symbol needs a real file in B's
    # revision for the fixture to reach the service at all.
    other_repo, other_revision = make_repo_and_revision(db_session, "other-owner")
    blob = SourceBlob(content_hash="d" * 40, inline_content="def f():\n    pass\n", size_bytes=18)
    db_session.add(blob)
    db_session.flush()
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="other.py",
        content_hash=blob.content_hash,
        size_bytes=18,
    )
    db_session.add(other_file)
    db_session.flush()
    other_symbol = Symbol(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        file_id=other_file.id,
        kind="function",
        name="f",
        qualified_name="other.f",
        start_line=1,
        end_line=2,
        analyzer="tree-sitter-python@0.21",
    )
    db_session.add(other_symbol)
    db_session.flush()

    def documentation_state() -> tuple[object, ...]:
        db_session.expire_all()
        artifacts = db_session.execute(
            select(
                GeneratedArtifact.id,
                GeneratedArtifact.repository_id,
                GeneratedArtifact.revision_id,
                GeneratedArtifact.content_hash,
                GeneratedArtifact.evidence_ids,
                GeneratedArtifact.updated_at,
            ).order_by(GeneratedArtifact.id)
        ).all()
        runs = db_session.execute(
            select(AnalysisRun.id, AnalysisRun.status)
            .where(AnalysisRun.analysis_type == "documentation_generation")
            .order_by(AnalysisRun.id)
        ).all()
        return tuple(artifacts), tuple(runs)

    before = documentation_state()
    with pytest.raises(service.TargetConflictError, match="belongs to a different repository"):
        service.generate_documentation(
            db_session,
            None,
            repository_id,
            revision_id,
            "symbol_documentation",
            symbol_id=other_symbol.id,
        )

    # Rejected before any run, context or artifact: nothing is written, and
    # repository A's existing artifact is untouched.
    assert documentation_state() == before
    assert [a.artifact_id for a in service.list_artifacts(db_session, repository_id)] == [
        existing.artifact_id
    ]
    assert service.list_artifacts(db_session, other_repo.id) == ()
    reread = service.get_artifact(db_session, repository_id, existing.artifact_id)
    assert (reread.content, reread.updated_at) == (existing.content, existing.updated_at)


def test_retry_after_failed_context_assembly_creates_a_new_failed_run_not_an_artifact(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated context assembly failure")

    monkeypatch.setattr(service, "assemble_repository_overview_context", _boom)
    with pytest.raises(service.DocumentationError):
        service.generate_documentation(
            db_session, None, repository_id, revision_id, "repository_overview"
        )

    runs = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.repository_id == repository_id)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "documentation_generation")
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == "failed"

    artifacts = (
        db_session.execute(
            select(GeneratedArtifact)
            .where(GeneratedArtifact.repository_id == repository_id)
            .where(GeneratedArtifact.revision_id == revision_id)
        )
        .scalars()
        .all()
    )
    assert artifacts == []  # no ambiguous partial artifact left behind

    monkeypatch.undo()
    retry_outcome = service.generate_documentation(
        db_session, None, repository_id, revision_id, "repository_overview"
    )
    assert retry_outcome.generation_method == "deterministic_fallback"
    runs_after_retry = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.repository_id == repository_id)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "documentation_generation")
        )
        .scalars()
        .all()
    )
    assert len(runs_after_retry) == 2  # append-only — the failed attempt stays in history


def test_r1_documentation_never_includes_r2_symbols(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    first = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(first.repository_id)
    r1_id = uuid.UUID(first.revision_id)
    analyze_revision(db_session, repository_id, r1_id)

    # The architecture overview lists only symbols with graph degree >= 1
    # (most-connected first), so the R2-only symbol must take part in a
    # graph edge. Otherwise it could never appear in either overview, and
    # the R1 assertion below would pass even if R1 leaked R2's data.
    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b\n\n\ndef brand_new_symbol():\n    return add(40, 2)\n",
        "add a new symbol",
    )
    second = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    r2_id = uuid.UUID(second.revision_id)
    analyze_revision(db_session, repository_id, r2_id)

    r1_outcome = service.generate_documentation(
        db_session, None, repository_id, r1_id, "architecture_overview"
    )
    r2_outcome = service.generate_documentation(
        db_session, None, repository_id, r2_id, "architecture_overview"
    )

    assert "brand_new_symbol" not in r1_outcome.content
    assert "brand_new_symbol" in r2_outcome.content
    assert r1_outcome.artifact_id != r2_outcome.artifact_id
    assert r1_outcome.revision_id == r1_id
    assert r2_outcome.revision_id == r2_id

    # Re-reading R1's artifact after R2 was generated must still show R1's
    # content, unchanged — no silent reuse of R2's evidence.
    reread_r1 = service.get_artifact(db_session, repository_id, r1_outcome.artifact_id)
    assert "brand_new_symbol" not in reread_r1.content
    assert reread_r1.revision_id == r1_id


def test_list_artifacts_scoped_by_revision(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    first = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(first.repository_id)
    r1_id = uuid.UUID(first.revision_id)
    analyze_revision(db_session, repository_id, r1_id)
    service.generate_documentation(db_session, None, repository_id, r1_id, "repository_overview")

    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "x"
    )
    second = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    r2_id = uuid.UUID(second.revision_id)
    analyze_revision(db_session, repository_id, r2_id)
    service.generate_documentation(db_session, None, repository_id, r2_id, "repository_overview")

    r1_only = service.list_artifacts(db_session, repository_id, revision_id=r1_id)
    assert len(r1_only) == 1
    assert r1_only[0].revision_id == r1_id

    all_artifacts = service.list_artifacts(db_session, repository_id)
    assert len(all_artifacts) == 2
