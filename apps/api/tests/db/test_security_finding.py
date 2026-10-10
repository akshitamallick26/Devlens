import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Repository, SecurityFinding


def _finding(repository: Repository, fingerprint: str) -> SecurityFinding:
    return SecurityFinding(
        repository_id=repository.id,
        analyzer="osv-scanner",
        rule_id="GHSA-xxxx",
        fingerprint=fingerprint,
        severity="high",
        title="Vulnerable dependency",
        message="package X has a known vulnerability",
    )


def test_fingerprint_is_unique_per_repository(db_session: Session, repository: Repository) -> None:
    db_session.add(_finding(repository, "pkg:npm/left-pad@1.0.0::GHSA-xxxx"))
    db_session.flush()

    db_session.add(_finding(repository, "pkg:npm/left-pad@1.0.0::GHSA-xxxx"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_fingerprint_allowed_across_different_repositories(db_session: Session) -> None:
    """Two unrelated repositories can share a vulnerable dependency without
    colliding — dedup is scoped per repository, not global."""
    repo_a = Repository(owner="a", name="repo", url="https://x/a/repo", default_branch="main")
    repo_b = Repository(owner="b", name="repo", url="https://x/b/repo", default_branch="main")
    db_session.add_all([repo_a, repo_b])
    db_session.flush()

    fingerprint = "pkg:npm/left-pad@1.0.0::GHSA-xxxx"
    db_session.add_all([_finding(repo_a, fingerprint), _finding(repo_b, fingerprint)])
    db_session.flush()  # must not raise
