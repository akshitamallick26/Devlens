"""DB-touching persistence for SecurityFinding/SecurityFindingObservation
rows. The AnalysisRun lifecycle is owned by app/security/service.py, which
calls these functions as steps within that lifecycle — the same
create/call/commit layering every other phase in this codebase uses.

Idempotency (Task 6/16): SecurityFinding is upserted by its unique
`(repository_id, fingerprint)` key via a single atomic `INSERT ... ON
CONFLICT DO UPDATE`, the same pattern app/rag/embeddings.py already
established for Embedding's own upsert-by-unique-key. A reobservation
updates the finding's informational fields (title/message/severity/CVSS/
references) to the latest scanner output and bumps `last_seen_run_id` —
but deliberately never touches `status`, so a user's own "resolved" /
"ignored" / "false_positive" workflow decision is never silently
overwritten by a later scan.

SecurityFindingObservation is never upserted — it is append-only history
(domain-model.md section 7's explicit design), so every analysis run that
observes a fingerprint gets its own new row. Uncontrolled *duplicate* rows
within one run are prevented upstream, in app/security/osv.py and
app/security/semgrep.py's own parsing (`seen_observation_keys`), not here.
"""

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import SecurityFinding, SecurityFindingObservation
from app.security.models import GraphResolution, NormalizedFinding, NormalizedObservation


def upsert_finding(
    db: Session, repository_id: uuid.UUID, finding: NormalizedFinding, analysis_run_id: uuid.UUID
) -> uuid.UUID:
    references = list(finding.references) or None
    stmt = (
        pg_insert(SecurityFinding)
        .values(
            repository_id=repository_id,
            analyzer=finding.analyzer,
            rule_id=finding.rule_id,
            fingerprint=finding.fingerprint,
            severity=finding.severity,
            title=finding.title,
            message=finding.message,
            cvss_score=finding.cvss_score,
            cvss_vector=finding.cvss_vector,
            scanner_confidence=finding.scanner_confidence,
            references=references,
            first_seen_run_id=analysis_run_id,
            last_seen_run_id=analysis_run_id,
        )
        .on_conflict_do_update(
            index_elements=["repository_id", "fingerprint"],
            set_={
                "severity": finding.severity,
                "title": finding.title,
                "message": finding.message,
                "cvss_score": finding.cvss_score,
                "cvss_vector": finding.cvss_vector,
                "scanner_confidence": finding.scanner_confidence,
                "references": references,
                "last_seen_run_id": analysis_run_id,
                # `status` is deliberately absent from this SET clause —
                # a reobservation must never overwrite a user's workflow
                # decision (open/resolved/ignored/false_positive).
            },
        )
        .returning(SecurityFinding.id)
    )
    finding_id = db.execute(stmt).scalar_one()
    db.flush()
    return finding_id


def persist_observation(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    finding_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    observation: NormalizedObservation,
    resolution: GraphResolution,
) -> SecurityFindingObservation:
    row = SecurityFindingObservation(
        repository_id=repository_id,
        finding_id=finding_id,
        revision_id=revision_id,
        analysis_run_id=analysis_run_id,
        file_id=resolution.file_id,
        symbol_id=resolution.symbol_id,
        start_line=observation.start_line,
        end_line=observation.end_line,
        raw_finding=observation.raw_finding,
        affected_package=observation.affected_package,
        installed_version=observation.installed_version,
        affected_range=observation.affected_range,
        fixed_version=observation.fixed_version,
        resolution_status=resolution.status,
    )
    db.add(row)
    db.flush()
    return row
