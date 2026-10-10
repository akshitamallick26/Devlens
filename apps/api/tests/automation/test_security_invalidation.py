"""Pure security-invalidation decision tests — no DB, always run."""

import uuid

from app.automation.models import FileDelta, FileEntry
from app.automation.security_invalidation import compute_security_invalidation


def _entry(path: str) -> FileEntry:
    return FileEntry(path=path, file_id=uuid.uuid4(), content_hash="hash")


def test_no_parent_always_requires_rerun() -> None:
    delta = FileDelta(added=(), deleted=(), modified=(), unchanged=())
    plan = compute_security_invalidation(delta, has_parent=False)
    assert plan.rerun_required is True


def test_no_file_changes_does_not_require_rerun() -> None:
    delta = FileDelta(added=(), deleted=(), modified=(), unchanged=(_entry("a.py"),))
    plan = compute_security_invalidation(delta, has_parent=True)
    assert plan.rerun_required is False
    assert "revision-scoped" in (plan.detail or "")


def test_manifest_change_requires_rerun() -> None:
    delta = FileDelta(added=(_entry("requirements.txt"),), deleted=(), modified=(), unchanged=())
    plan = compute_security_invalidation(delta, has_parent=True)
    assert plan.rerun_required is True
    assert "manifest" in plan.reason


def test_source_change_requires_rerun() -> None:
    delta = FileDelta(added=(_entry("app/foo.py"),), deleted=(), modified=(), unchanged=())
    plan = compute_security_invalidation(delta, has_parent=True)
    assert plan.rerun_required is True
    assert "source" in plan.reason.lower()


def test_both_manifest_and_source_change_requires_rerun() -> None:
    delta = FileDelta(
        added=(_entry("requirements.txt"), _entry("app/foo.py")),
        deleted=(),
        modified=(),
        unchanged=(),
    )
    plan = compute_security_invalidation(delta, has_parent=True)
    assert plan.rerun_required is True
    assert "manifest" in plan.reason and "source" in plan.reason.lower()


def test_unrelated_file_change_still_requires_rerun_conservatively() -> None:
    delta = FileDelta(added=(_entry("README.md"),), deleted=(), modified=(), unchanged=())
    plan = compute_security_invalidation(delta, has_parent=True)
    assert plan.rerun_required is True
