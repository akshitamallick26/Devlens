"""Pure QA context-assembly tests — no DB, always run."""

import uuid

from app.rag.context import MAX_EVIDENCE_ITEMS, MAX_TOTAL_CONTENT_CHARS, assemble_qa_context
from app.rag.models import GraphExpandedSymbol, HybridResult

REPO_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()


def _hybrid(name: str, sources: tuple[str, ...] = ("lexical",)) -> HybridResult:
    return HybridResult(
        chunk_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        symbol_id=uuid.uuid4(),
        qualified_name=name,
        start_line=1,
        end_line=2,
        rrf_score=0.5,
        sources=sources,
    )


def _expanded(name: str, direction: str = "callers") -> GraphExpandedSymbol:
    return GraphExpandedSymbol(
        symbol_id=uuid.uuid4(),
        qualified_name=name,
        kind="function",
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        start_line=1,
        end_line=2,
        relationship_type="calls",
        direction=direction,
        seed_symbol_id=uuid.uuid4(),
    )


def test_hybrid_results_come_before_graph_expansion() -> None:
    hybrid = _hybrid("target")
    expanded = _expanded("caller")
    package = assemble_qa_context(
        REPO_ID,
        REVISION_ID,
        [hybrid],
        {hybrid.chunk_id: "def target(): pass"},
        [expanded],
        {expanded.symbol_id: "def caller(): pass"},
    )
    sources = [item.retrieval_source for item in package.evidence.items]
    assert sources.index("lexical") < sources.index("graph")


def test_evidence_ids_are_sequential_and_stable() -> None:
    hybrid = _hybrid("target")
    package = assemble_qa_context(REPO_ID, REVISION_ID, [hybrid], {}, [], {})
    assert package.evidence.items[0].evidence_id == "Q1"


def test_hybrid_source_is_hybrid_when_found_by_both_retrievers() -> None:
    hybrid = _hybrid("target", sources=("lexical", "vector"))
    package = assemble_qa_context(REPO_ID, REVISION_ID, [hybrid], {}, [], {})
    assert package.evidence.items[0].retrieval_source == "hybrid"


def test_graph_evidence_always_has_a_retrieval_reason() -> None:
    expanded = _expanded("caller", direction="callers")
    package = assemble_qa_context(REPO_ID, REVISION_ID, [], {}, [expanded], {})
    item = package.evidence.items[0]
    assert "callers" in item.retrieval_reason
    assert "calls" in item.retrieval_reason
    assert item.relationship_type == "calls"


def test_missing_content_degrades_to_metadata_never_fabricated() -> None:
    hybrid = _hybrid("target")
    package = assemble_qa_context(REPO_ID, REVISION_ID, [hybrid], {}, [], {})  # no content supplied
    assert package.evidence.items[0].content is None
    assert package.evidence.items[0].file_path == "target.py"  # metadata still present


def test_context_is_bounded_by_max_evidence_items() -> None:
    many_hybrid = [_hybrid(f"f{i}") for i in range(MAX_EVIDENCE_ITEMS + 5)]
    package = assemble_qa_context(REPO_ID, REVISION_ID, many_hybrid, {}, [], {})
    assert len(package.evidence.items) <= MAX_EVIDENCE_ITEMS


def test_oversized_content_degrades_to_metadata_not_omitted() -> None:
    hybrid = _hybrid("target")
    huge = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    package = assemble_qa_context(REPO_ID, REVISION_ID, [hybrid], {hybrid.chunk_id: huge}, [], {})
    assert len(package.evidence.items) == 1  # still present
    assert package.evidence.items[0].content is None  # degraded, not fabricated/truncated


def test_deterministic_repeated_assembly_is_identical() -> None:
    hybrid = _hybrid("target")
    expanded = _expanded("caller")
    chunk_content: dict[uuid.UUID, str | None] = {hybrid.chunk_id: "x"}
    symbol_content: dict[uuid.UUID, str | None] = {expanded.symbol_id: "y"}
    args = (REPO_ID, REVISION_ID, [hybrid], chunk_content, [expanded], symbol_content)
    first = assemble_qa_context(*args)
    second = assemble_qa_context(*args)
    assert first == second
