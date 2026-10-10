"""Deterministic documentation-target discovery (Task 14). No LLM
involvement anywhere in this module. Targets are always exact UUIDs
supplied by the caller — never a name-based lookup — so "ambiguous
target" (mentioned in the hardening brief as an example error case) cannot
occur in this design: there is no code path that could resolve to more
than one candidate. See docs/architecture/documentation-intelligence.md
section 14.
"""

import uuid

from sqlalchemy.orm import Session

from app.documentation.models import TargetResolution
from app.models import SourceFile, Symbol

_NO_TARGET_TYPES = ("repository_overview", "architecture_overview")


def resolve_target(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_type: str,
    *,
    source_file_id: uuid.UUID | None,
    symbol_id: uuid.UUID | None,
) -> TargetResolution:
    if artifact_type in _NO_TARGET_TYPES:
        if source_file_id is not None or symbol_id is not None:
            return TargetResolution(
                status="unexpected_target",
                target_file_id=None,
                target_symbol_id=None,
                detail=f"{artifact_type} does not accept a target",
            )
        return TargetResolution(
            status="resolved", target_file_id=None, target_symbol_id=None, detail=None
        )

    if artifact_type == "module_documentation":
        if source_file_id is None:
            return TargetResolution(
                status="missing_target",
                target_file_id=None,
                target_symbol_id=None,
                detail="module_documentation requires source_file_id",
            )
        source_file = db.get(SourceFile, source_file_id)
        if source_file is None:
            return TargetResolution(
                status="not_found",
                target_file_id=None,
                target_symbol_id=None,
                detail=f"no SourceFile {source_file_id}",
            )
        if source_file.repository_id != repository_id:
            return TargetResolution(
                status="wrong_repository",
                target_file_id=None,
                target_symbol_id=None,
                detail=f"SourceFile {source_file_id} belongs to a different repository",
            )
        if source_file.revision_id != revision_id:
            return TargetResolution(
                status="wrong_revision",
                target_file_id=None,
                target_symbol_id=None,
                detail=f"SourceFile {source_file_id} belongs to a different revision",
            )
        return TargetResolution(
            status="resolved", target_file_id=source_file.id, target_symbol_id=None, detail=None
        )

    # symbol_documentation
    if symbol_id is None:
        return TargetResolution(
            status="missing_target",
            target_file_id=None,
            target_symbol_id=None,
            detail="symbol_documentation requires symbol_id",
        )
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        return TargetResolution(
            status="not_found",
            target_file_id=None,
            target_symbol_id=None,
            detail=f"no Symbol {symbol_id}",
        )
    if symbol.repository_id != repository_id:
        return TargetResolution(
            status="wrong_repository",
            target_file_id=None,
            target_symbol_id=None,
            detail=f"Symbol {symbol_id} belongs to a different repository",
        )
    if symbol.revision_id != revision_id:
        return TargetResolution(
            status="wrong_revision",
            target_file_id=None,
            target_symbol_id=None,
            detail=f"Symbol {symbol_id} belongs to a different revision",
        )
    return TargetResolution(
        status="resolved", target_file_id=None, target_symbol_id=symbol.id, detail=None
    )
