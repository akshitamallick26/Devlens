"""Pure context-assembly tests — no DB, no AI provider, always run.
Constructs ImpactAnalysisResult/SourceSpan/EdgeEvidence directly.
"""

import uuid

from app.impact.context import MAX_EVIDENCE_ITEMS, MAX_TOTAL_CONTENT_CHARS, assemble_context
from app.impact.models import ImpactAnalysisResult, ImpactedFile, ImpactedSymbol, SymbolView
from app.impact.source import EdgeEvidence, SourceSpan

REVISION_ID = uuid.uuid4()


def _target() -> SymbolView:
    return SymbolView(
        id=uuid.uuid4(),
        qualified_name="target",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="target.py",
        parent_symbol_id=None,
    )


def _impacted(name: str, depth: int, rel_types: tuple[str, ...] = ("calls",)) -> ImpactedSymbol:
    return ImpactedSymbol(
        symbol_id=uuid.uuid4(),
        qualified_name=name,
        symbol_kind="function",
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        depth=depth,
        relationship_types=rel_types,
        parent_symbol_id=None,
    )


def _source(symbol_id: uuid.UUID, file_path: str, content: str = "def f(): pass") -> SourceSpan:
    return SourceSpan(
        symbol_id=symbol_id, file_path=file_path, line_start=1, line_end=2, content=content
    )


def _result(
    target: SymbolView,
    direct: tuple[ImpactedSymbol, ...],
    transitive: tuple[ImpactedSymbol, ...] = (),
) -> ImpactAnalysisResult:
    return ImpactAnalysisResult(
        revision_id=REVISION_ID,
        target_symbol_id=target.id,
        target_symbol=target,
        direct_impacts=direct,
        transitive_impacts=transitive,
        impacted_files=(
            ImpactedFile(file_id=uuid.uuid4(), path="a.py", symbol_count=1, minimum_depth=1),
        ),
        max_depth=max((s.depth for s in (*direct, *transitive)), default=0),
    )


def test_target_is_always_first_and_prioritized() -> None:
    target = _target()
    direct = _impacted("caller", 1)
    result = _result(target, (direct,))
    package = assemble_context(
        result,
        _source(target.id, target.file_path),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path)},
        {},
    )
    assert package.evidence.items[0].type == "target_source"
    assert package.evidence.items[0].evidence_id == "E1"


def test_direct_impacts_appear_before_transitive_impacts() -> None:
    target = _target()
    direct = _impacted("caller", 1)
    transitive = _impacted("deep_caller", 2)
    result = _result(target, (direct,), (transitive,))
    package = assemble_context(
        result,
        _source(target.id, target.file_path),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path)},
        {},
        {transitive.symbol_id: _source(transitive.symbol_id, transitive.file_path)},
    )
    types_in_order = [item.type for item in package.evidence.items]
    direct_index = types_in_order.index("direct_dependent")
    transitive_index = types_in_order.index("transitive_dependent")
    assert direct_index < transitive_index


def test_evidence_ids_are_sequential_and_stable() -> None:
    target = _target()
    direct = _impacted("caller", 1)
    result = _result(target, (direct,))
    package = assemble_context(
        result,
        _source(target.id, target.file_path),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path)},
        {},
    )
    ids = [item.evidence_id for item in package.evidence.items]
    assert ids == [f"E{i + 1}" for i in range(len(ids))]
    assert len(ids) == len(set(ids))  # no duplicate evidence ids


def test_impact_path_evidence_uses_the_real_edge_location() -> None:
    target = _target()
    direct = _impacted("caller", 1, rel_types=("calls",))
    edge = EdgeEvidence(
        source_symbol_id=direct.symbol_id,
        file_path="caller.py",
        line_start=42,
        line_end=42,
        relationship_type="calls",
    )
    result = _result(target, (direct,))
    package = assemble_context(
        result,
        _source(target.id, target.file_path),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path)},
        {direct.symbol_id: edge},
    )
    impact_path_items = [e for e in package.evidence.items if e.type == "impact_path"]
    assert len(impact_path_items) == 1
    assert impact_path_items[0].file_path == "caller.py"
    assert impact_path_items[0].line_start == 42
    assert impact_path_items[0].relationship_type == "calls"


def test_missing_source_does_not_fabricate_content() -> None:
    """A symbol with no SourceSpan available (e.g. storage_key-backed
    content) must never get invented content — the evidence item is still
    built (metadata-only) with content=None."""
    target = _target()
    direct = _impacted("caller", 1)
    result = _result(target, (direct,))
    package = assemble_context(result, None, {}, {})  # no source for anything
    for item in package.evidence.items:
        assert item.content is None or item.type == "file_context"


def test_context_is_bounded_by_max_evidence_items() -> None:
    target = _target()
    many_direct = tuple(_impacted(f"caller_{i}", 1) for i in range(MAX_EVIDENCE_ITEMS + 10))
    result = _result(target, many_direct)
    sources = {d.symbol_id: _source(d.symbol_id, d.file_path) for d in many_direct}
    package = assemble_context(result, _source(target.id, target.file_path), sources, {})
    assert len(package.evidence.items) <= MAX_EVIDENCE_ITEMS


def test_context_is_bounded_by_max_total_content_chars() -> None:
    target = _target()
    huge_content = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    direct = _impacted("caller", 1)
    result = _result(target, (direct,))
    package = assemble_context(
        result,
        _source(target.id, target.file_path, content=huge_content),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path, content=huge_content)},
        {},
    )
    total_chars = sum(len(item.content) for item in package.evidence.items if item.content)
    assert total_chars <= MAX_TOTAL_CONTENT_CHARS


# ---- Task 12: context-size behavior (degrade, never omit, never truncate) --


def test_target_source_alone_exceeding_the_budget_degrades_to_metadata_only() -> None:
    """The target is Priority 1 and must still appear — with its real
    file/line location — even when its own source text alone is too large
    for the whole character budget. It is never silently dropped, and its
    content is never truncated mid-excerpt."""
    target = _target()
    huge_content = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    result = _result(target, ())
    package = assemble_context(
        result, _source(target.id, target.file_path, content=huge_content), {}, {}
    )
    target_items = [e for e in package.evidence.items if e.type == "target_source"]
    assert len(target_items) == 1
    assert target_items[0].evidence_id == "E1"
    assert target_items[0].file_path == target.file_path
    assert target_items[0].content is None  # degraded, not omitted, not truncated


def test_an_oversized_snippet_among_several_is_degraded_while_smaller_ones_still_fit() -> None:
    """A later, smaller candidate still gets its content included even
    though an earlier, oversized one had to degrade — evidence_ids stay
    sequential and valid throughout."""
    target = _target()
    first = _impacted("huge_caller", 1)
    second = _impacted("small_caller", 1)
    result = _result(target, (first, second))
    huge_content = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    package = assemble_context(
        result,
        _source(target.id, target.file_path),
        {
            first.symbol_id: _source(first.symbol_id, first.file_path, content=huge_content),
            second.symbol_id: _source(
                second.symbol_id, second.file_path, content="def small(): pass"
            ),
        },
        {},
    )
    by_symbol = {e.symbol_id: e for e in package.evidence.items if e.type == "direct_dependent"}
    assert by_symbol[first.symbol_id].content is None  # degraded
    assert by_symbol[second.symbol_id].content == "def small(): pass"  # still fit, unaffected

    ids = [item.evidence_id for item in package.evidence.items]
    assert ids == [f"E{i + 1}" for i in range(len(ids))]  # still sequential, no gaps
    assert len(ids) == len(set(ids))  # still no duplicates


def test_deterministic_repeated_assembly_is_identical() -> None:
    target = _target()
    direct = _impacted("caller", 1)
    transitive = _impacted("deep_caller", 2)
    result = _result(target, (direct,), (transitive,))
    args = (
        result,
        _source(target.id, target.file_path),
        {direct.symbol_id: _source(direct.symbol_id, direct.file_path)},
        {},
        {transitive.symbol_id: _source(transitive.symbol_id, transitive.file_path)},
    )
    first = assemble_context(*args)
    second = assemble_context(*args)
    assert first == second
