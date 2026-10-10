"""Pure QA context assembly — no SQLAlchemy, no DB, no AI provider. See
docs/architecture/rag-codebase-qa.md section 9.

Combines bounded hybrid-retrieval results and bounded graph-expansion
results into one deterministic `QAContextPackage`, under the identical
"degrade to metadata-only, never omit, never truncate mid-excerpt" budget
discipline app/impact/context.py already established (and this phase
inherits deliberately, not by coincidence).
"""

import uuid
from dataclasses import dataclass

from app.rag.citations import Evidence, EvidenceSet
from app.rag.models import GraphExpandedSymbol, HybridResult

MAX_EVIDENCE_ITEMS = 12
MAX_TOTAL_CONTENT_CHARS = 20_000


@dataclass(frozen=True)
class QAContextPackage:
    evidence: EvidenceSet


@dataclass
class _Candidate:
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    line_start: int | None
    line_end: int | None
    chunk_id: uuid.UUID | None
    retrieval_reason: str
    retrieval_source: str
    relationship_type: str | None
    content: str | None = None


def assemble_qa_context(
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    hybrid_results: list[HybridResult],
    hybrid_content: dict[uuid.UUID, str | None],
    graph_expanded: list[GraphExpandedSymbol],
    graph_content: dict[uuid.UUID, str | None],
) -> QAContextPackage:
    candidates: list[_Candidate] = []

    # Priority 1: hybrid retrieval results, already RRF-ordered.
    for result in hybrid_results:
        source = "hybrid" if len(result.sources) > 1 else result.sources[0]
        candidates.append(
            _Candidate(
                file_path=result.file_path,
                symbol_id=result.symbol_id,
                qualified_name=result.qualified_name,
                line_start=result.start_line,
                line_end=result.end_line,
                chunk_id=result.chunk_id,
                retrieval_reason=f"matched query via {'+'.join(result.sources)}",
                retrieval_source=source,
                relationship_type=None,
                content=hybrid_content.get(result.chunk_id),
            )
        )

    # Priority 2: graph-expanded symbols, each with an explicit reason for
    # why it was included — never expansion without an attributable reason.
    for expanded in graph_expanded:
        candidates.append(
            _Candidate(
                file_path=expanded.file_path,
                symbol_id=expanded.symbol_id,
                qualified_name=expanded.qualified_name,
                line_start=expanded.start_line,
                line_end=expanded.end_line,
                chunk_id=None,
                retrieval_reason=(
                    f"{expanded.direction} of a retrieved symbol via {expanded.relationship_type}"
                ),
                retrieval_source="graph",
                relationship_type=expanded.relationship_type,
                content=graph_content.get(expanded.symbol_id),
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
                evidence_id=f"Q{len(accepted) + 1}",
                repository_id=repository_id,
                revision_id=revision_id,
                file_path=candidate.file_path,
                symbol_id=candidate.symbol_id,
                qualified_name=candidate.qualified_name,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                chunk_id=candidate.chunk_id,
                retrieval_reason=candidate.retrieval_reason,
                retrieval_source=candidate.retrieval_source,
                relationship_type=candidate.relationship_type,
                content=content,
            )
        )

    return QAContextPackage(evidence=EvidenceSet(items=tuple(accepted)))
