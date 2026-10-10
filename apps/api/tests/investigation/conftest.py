import pytest
from sqlalchemy.orm import Session

from app.models import Repository, SecurityFinding


@pytest.fixture
def security_finding(db_session: Session, repository: Repository) -> SecurityFinding:
    finding = SecurityFinding(
        repository_id=repository.id,
        analyzer="osv-scanner",
        rule_id="GHSA-xxxx",
        fingerprint="pkg:npm/left-pad@1.0.0::GHSA-xxxx",
        severity="high",
        title="Vulnerable dependency",
        message="package X has a known vulnerability",
    )
    db_session.add(finding)
    db_session.flush()
    return finding
