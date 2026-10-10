"""Pure context assembly — no SQLAlchemy, no DB, no AI provider. See
docs/architecture/impact-intelligence.md section 3.

Takes an already-computed `ImpactAnalysisResult` plus already-extracted
source/edge-evidence (built by app/impact/source.py) and produces a
bounded, deterministic `ContextPackage`. The same inputs always produce
the same evidence set, IDs, and ordering.
"""

import uuid
from dataclasses import dataclass, field

from app.impact.evidence import Evidence, EvidenceSet
from app.impact.models import ImpactAnalysisResult
from app.impact.source import EdgeEvidence, SourceSpan

# Fixed v1 policy, not environment-driven — a code-level decision, not a
# per-request tuning knob (impact-intelligence.md section 3).
MAX_EVIDENCE_ITEMS = 12
MAX_TOTAL_CONTENT_CHARS = 20_000


@dataclass(frozen=True)
class ContextPackage:
    target_qualified_name: str
    evidence: EvidenceSet
    deterministic_summary: str


@dataclass
class _Candidate:
    type: str
    symbol_id: uuid.UUID | None
    file_path: str
    line_start: int | None
    line_end: int | None
    relationship_type: str | None
    depth: int | None
    content: str | None = field(default=None)


def _render_summary(result: ImpactAnalysisResult) -> str:
    lines = [
        f"{len(result.direct_impacts)} direct dependents, "
        f"{len(result.transitive_impacts)} transitive dependents, "
        f"max depth {result.max_depth}",
        "Impacted files:",
    ]
    for impacted_file in result.impacted_files:
        lines.append(
            f"  {impacted_file.path} (symbol_count={impacted_file.symbol_count}, "
            f"minimum_depth={impacted_file.minimum_depth})"
        )
    return "\n".join(lines)


def assemble_context(
    result: ImpactAnalysisResult,
    target_source: SourceSpan | None,
    direct_impact_sources: dict[uuid.UUID, SourceSpan],
    direct_impact_edges: dict[uuid.UUID, EdgeEvidence],
    transitive_impact_sources: dict[uuid.UUID, SourceSpan] | None = None,
) -> ContextPackage:
    transitive_impact_sources = transitive_impact_sources or {}
    candidates: list[_Candidate] = []

    # Priority 1: target source.
    if target_source is not None:
        candidates.append(
            _Candidate(
                type="target_source",
                symbol_id=result.target_symbol_id,
                file_path=target_source.file_path,
                line_start=target_source.line_start,
                line_end=target_source.line_end,
                relationship_type=None,
                depth=0,
                content=target_source.content,
            )
        )

    # Priority 2: direct impacts, in compute_impact's own deterministic
    # order — the specific connecting edge first (proves *why*), then the
    # dependent's own source.
    for impacted in result.direct_impacts:
        edge = direct_impact_edges.get(impacted.symbol_id)
        if edge is not None:
            candidates.append(
                _Candidate(
                    type="impact_path",
                    symbol_id=impacted.symbol_id,
                    file_path=edge.file_path,
                    line_start=edge.line_start,
                    line_end=edge.line_end,
                    relationship_type=edge.relationship_type,
                    depth=impacted.depth,
                )
            )
        source = direct_impact_sources.get(impacted.symbol_id)
        candidates.append(
            _Candidate(
                type="direct_dependent",
                symbol_id=impacted.symbol_id,
                file_path=source.file_path if source else impacted.file_path,
                line_start=source.line_start if source else None,
                line_end=source.line_end if source else None,
                relationship_type=impacted.relationship_types[0]
                if impacted.relationship_types
                else None,
                depth=impacted.depth,
                content=source.content if source else None,
            )
        )

    # Priority 3: shortest-depth-first transitive impacts (already ordered
    # that way by compute_impact). Source only when the caller fetched it;
    # metadata is always present regardless.
    for impacted in result.transitive_impacts:
        source = transitive_impact_sources.get(impacted.symbol_id)
        candidates.append(
            _Candidate(
                type="transitive_dependent",
                symbol_id=impacted.symbol_id,
                file_path=source.file_path if source else impacted.file_path,
                line_start=source.line_start if source else None,
                line_end=source.line_end if source else None,
                relationship_type=impacted.relationship_types[0]
                if impacted.relationship_types
                else None,
                depth=impacted.depth,
                content=source.content if source else None,
            )
        )

    # Priority 4: compact file-level summary, one item, metadata only.
    if result.impacted_files:
        candidates.append(
            _Candidate(
                type="file_context",
                symbol_id=None,
                file_path="(impacted files summary)",
                line_start=None,
                line_end=None,
                relationship_type=None,
                depth=None,
                content="\n".join(
                    f"{f.path} (symbol_count={f.symbol_count}, minimum_depth={f.minimum_depth})"
                    for f in result.impacted_files
                ),
            )
        )

    # Budget enforcement, Task 12 of the adversarial-hardening pass: an
    # item that doesn't fit the remaining character budget is DEGRADED to
    # metadata-only (content=None), never omitted entirely and never
    # truncated mid-excerpt. Omitting it outright would silently drop even
    # the file/location grounding a model could still use; truncating would
    # risk handing it a syntactically broken, misleading fragment. Every
    # candidate still gets an evidence_id as long as the ITEM-COUNT budget
    # allows it — only the character budget affects `content`.
    accepted: list[Evidence] = []
    total_chars = 0
    for candidate in candidates:
        if len(accepted) >= MAX_EVIDENCE_ITEMS:
            break
        content_len = len(candidate.content) if candidate.content else 0
        content = candidate.content
        if content is not None and total_chars + content_len > MAX_TOTAL_CONTENT_CHARS:
            content = None  # degrade to metadata-only; a smaller later item may still fit
        else:
            total_chars += content_len
        accepted.append(
            Evidence(
                evidence_id=f"E{len(accepted) + 1}",
                type=candidate.type,
                symbol_id=candidate.symbol_id,
                file_path=candidate.file_path,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                relationship_type=candidate.relationship_type,
                depth=candidate.depth,
                content=content,
            )
        )

    return ContextPackage(
        target_qualified_name=result.target_symbol.qualified_name,
        evidence=EvidenceSet(items=tuple(accepted)),
        deterministic_summary=_render_summary(result),
    )
