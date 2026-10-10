"""DB-touching persistence for GeneratedTest/TestExecution rows. Pure CRUD
helpers — the AnalysisRun lifecycle (create -> commit -> run -> complete/
fail -> commit) is owned by app/testing/service.py, which calls these
functions as steps within that lifecycle, the same create/call/commit
layering app/rag/qa.py uses relative to app/rag/persistence.py.
"""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import GeneratedTest, SourceBlob, TestExecution
from app.testing.models import ExecutionResult, GeneratedTestOutput, ValidationResult

ANALYZER_NAME = "devlens-test-generation"


def _store_source_blob(db: Session, source: str) -> str:
    """Content-addressed, same mechanism as SourceFile's own blobs
    (domain-model.md section 8: "it's still a blob of text with an
    identity, no reason to duplicate the mechanism"). Reuses an existing
    blob by hash rather than ever writing a duplicate."""
    content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    blob = db.get(SourceBlob, content_hash)
    if blob is None:
        blob = SourceBlob(
            content_hash=content_hash,
            inline_content=source,
            size_bytes=len(source.encode("utf-8")),
        )
        db.add(blob)
        db.flush()
    return content_hash


def persist_generated_test(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    target_symbol_id: uuid.UUID,
    output: GeneratedTestOutput,
    provider_name: str,
    model_name: str,
    model_version: str | None,
    analysis_run_id: uuid.UUID | None,
) -> GeneratedTest:
    content_hash = _store_source_blob(db, output.source)
    generated_test = GeneratedTest(
        repository_id=repository_id,
        revision_id=revision_id,
        target_symbol_id=target_symbol_id,
        analysis_run_id=analysis_run_id,
        proposed_path=output.file_path,
        content_hash=content_hash,
        language="python",
        status="generated",
        provider=provider_name,
        model_name=model_name,
        model_version=model_version,
        rationale=output.rationale,
        cited_evidence_ids=list(output.cited_evidence_ids),
    )
    db.add(generated_test)
    db.flush()
    return generated_test


def record_validation(generated_test: GeneratedTest, result: ValidationResult) -> None:
    generated_test.validation_status = "passed" if result.passed else "failed"
    generated_test.validation_errors = (
        [f"{issue.code}: {issue.detail}" for issue in result.issues] if result.issues else None
    )
    generated_test.status = "ready" if result.passed else "validation_failed"


_EXECUTION_TO_GENERATED_TEST_STATUS = {
    "passed": "passed",
    "failed": "failed",
    "error": "execution_failed",
    "timeout": "execution_failed",
}


def record_execution(
    db: Session,
    generated_test: GeneratedTest,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    execution: ExecutionResult,
    analysis_run_id: uuid.UUID | None,
    started_at: datetime,
) -> TestExecution:
    generated_test.status = _EXECUTION_TO_GENERATED_TEST_STATUS[execution.status]
    test_execution = TestExecution(
        generated_test_id=generated_test.id,
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_run_id=analysis_run_id,
        status=execution.status,
        duration_ms=execution.duration_ms,
        exit_code=execution.exit_code,
        stdout=execution.stdout,
        stderr=execution.stderr,
        timed_out=execution.timed_out,
        validation_status=generated_test.validation_status,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    db.add(test_execution)
    db.flush()
    return test_execution
