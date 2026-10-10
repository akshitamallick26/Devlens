"""Security-analysis invalidation decision (Task 9). Pure — no DB, no
scanner invocation. `SecurityFindingObservation` rows are always
revision-scoped (app/security/service.py never retrieves them cross-
revision), so the current revision genuinely has none of its own until
`app.security.service.run_security_analysis` is actually run for it — this
module only decides/reports whether that's advisable, it never runs it.
"""

from app.automation.models import FileDelta, SecurityInvalidationPlan
from app.security.manifests import is_supported_manifest_path

_PYTHON_SUFFIX = ".py"


def _changed_paths(file_delta: FileDelta) -> list[str]:
    return [
        *(e.path for e in file_delta.added),
        *(e.path for e in file_delta.deleted),
        *(current.path for _, current in file_delta.modified),
    ]


def compute_security_invalidation(
    file_delta: FileDelta, has_parent: bool
) -> SecurityInvalidationPlan:
    if not has_parent:
        return SecurityInvalidationPlan(
            rerun_required=True,
            reason="no parent revision — the current revision has no security observations yet",
            detail=None,
        )

    changed_paths = _changed_paths(file_delta)
    manifest_changed = any(is_supported_manifest_path(p) for p in changed_paths)
    source_changed = any(p.endswith(_PYTHON_SUFFIX) for p in changed_paths)

    if not changed_paths:
        return SecurityInvalidationPlan(
            rerun_required=False,
            reason="no file changes detected relative to the parent revision",
            detail=(
                "security findings are still revision-scoped: this revision will have no "
                "SecurityFindingObservation rows of its own until a scan is explicitly run "
                "against it, even though no relevant content changed"
            ),
        )

    if manifest_changed and source_changed:
        reason = "dependency manifest and Python source both changed"
    elif manifest_changed:
        reason = "a dependency manifest changed"
    elif source_changed:
        reason = "Python source changed"
    else:
        reason = "other files changed; neither scanner's input is known to be affected"

    return SecurityInvalidationPlan(rerun_required=True, reason=reason, detail=None)
