"""Pure security-explanation context assembly — no SQLAlchemy, no DB, no AI
provider. Combines an already-persisted finding/observation view and an
already-computed BlastRadiusResult into one deterministic
SecurityContextPackage, under the identical "degrade to metadata-only,
never omit, never truncate mid-excerpt" budget discipline every other
evidence-assembly module in this codebase already established.

Priority order, matching the S1..S5 example in the phase spec: the finding
itself, then observations, then affected source, then dependency metadata,
then graph paths.
"""

import uuid
from dataclasses import dataclass

from app.security.citations import Evidence, EvidenceSet
from app.security.models import BlastRadiusResult, FindingView, ObservationView

MAX_EVIDENCE_ITEMS = 12
MAX_TOTAL_CONTENT_CHARS = 20_000
MAX_OBSERVATIONS_IN_CONTEXT = 5
MAX_GRAPH_PATHS_IN_CONTEXT = 3


@dataclass(frozen=True)
class SecurityContextPackage:
    evidence: EvidenceSet


@dataclass
class _Candidate:
    kind: str
    file_path: str | None
    symbol_id: uuid.UUID | None
    line_start: int | None
    line_end: int | None
    content: str | None = None


def _dependency_metadata_text(observation: ObservationView) -> str | None:
    if observation.affected_package is None:
        return None
    parts = [f"package={observation.affected_package}"]
    if observation.installed_version:
        parts.append(f"installed_version={observation.installed_version}")
    if observation.affected_range:
        parts.append(f"affected_range={observation.affected_range}")
    if observation.fixed_version:
        parts.append(f"fixed_version={observation.fixed_version}")
    return " ".join(parts)


def assemble_security_context(
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    finding: FindingView,
    observations: tuple[ObservationView, ...],
    affected_source: dict[uuid.UUID, str | None],
    blast_radius: BlastRadiusResult | None,
    symbol_names: dict[uuid.UUID, str],
) -> SecurityContextPackage:
    candidates: list[_Candidate] = [
        _Candidate(
            kind="finding",
            file_path=None,
            symbol_id=None,
            line_start=None,
            line_end=None,
            content=(
                f"analyzer={finding.analyzer} rule_id={finding.rule_id} "
                f"severity={finding.severity or 'unknown'} status={finding.status}\n"
                f"{finding.title}\n{finding.message}"
            ),
        )
    ]

    for observation in observations[:MAX_OBSERVATIONS_IN_CONTEXT]:
        location = (
            f"{observation.file_path}:{observation.start_line}-{observation.end_line}"
            if observation.start_line is not None
            else (observation.file_path or "(no location)")
        )
        candidates.append(
            _Candidate(
                kind="observation",
                file_path=observation.file_path,
                symbol_id=observation.symbol_id,
                line_start=observation.start_line,
                line_end=observation.end_line,
                content=f"{location} (resolution={observation.resolution_status})",
            )
        )

    for observation in observations[:MAX_OBSERVATIONS_IN_CONTEXT]:
        content = affected_source.get(observation.id)
        if content is not None:
            candidates.append(
                _Candidate(
                    kind="affected_source",
                    file_path=observation.file_path,
                    symbol_id=observation.symbol_id,
                    line_start=observation.start_line,
                    line_end=observation.end_line,
                    content=content,
                )
            )

    for observation in observations[:MAX_OBSERVATIONS_IN_CONTEXT]:
        dependency_text = _dependency_metadata_text(observation)
        if dependency_text is not None:
            candidates.append(
                _Candidate(
                    kind="dependency_metadata",
                    file_path=observation.file_path,
                    symbol_id=None,
                    line_start=None,
                    line_end=None,
                    content=dependency_text,
                )
            )

    if blast_radius is not None:
        for path in blast_radius.evidence_paths[:MAX_GRAPH_PATHS_IN_CONTEXT]:
            names = [symbol_names.get(sid, str(sid)) for sid in path.symbol_ids]
            candidates.append(
                _Candidate(
                    kind="graph_path",
                    file_path=None,
                    symbol_id=path.symbol_ids[-1] if path.symbol_ids else None,
                    line_start=None,
                    line_end=None,
                    content=" -> ".join(names) + f" (via {', '.join(path.relationship_types)})",
                )
            )

    accepted: list[Evidence] = []
    total_chars = 0
    for candidate in candidates:
        if len(accepted) >= MAX_EVIDENCE_ITEMS:
            break
        content = candidate.content
        content_len = len(content) if content else 0
        if content is not None and total_chars + content_len > MAX_TOTAL_CONTENT_CHARS:
            content = None  # degrade to metadata-only; never omit, never truncate mid-excerpt
        else:
            total_chars += content_len
        accepted.append(
            Evidence(
                evidence_id=f"S{len(accepted) + 1}",
                repository_id=repository_id,
                revision_id=revision_id,
                kind=candidate.kind,
                file_path=candidate.file_path,
                symbol_id=candidate.symbol_id,
                qualified_name=symbol_names.get(candidate.symbol_id)
                if candidate.symbol_id
                else None,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                content=content,
            )
        )

    return SecurityContextPackage(evidence=EvidenceSet(items=tuple(accepted)))
