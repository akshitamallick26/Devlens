"""Pure traversal-algorithm tests — no database, always run. Constructs
EdgeView/SymbolView directly rather than going through a real pipeline;
tests/impact/test_service.py covers the full ingestion->parsing->graph-
construction->impact pipeline against a realistic fixture.
"""

import uuid

from app.impact.models import EdgeView, SymbolView, TargetResolution
from app.impact.traversal import compute_impact, resolve_target

REVISION_ID = uuid.uuid4()
FILE_ID = uuid.uuid4()


def _symbol(name: str) -> SymbolView:
    return SymbolView(
        id=uuid.uuid4(),
        qualified_name=name,
        kind="function",
        file_id=FILE_ID,
        file_path=f"{name}.py",
        parent_symbol_id=None,
    )


def _edge(source: SymbolView, target: SymbolView, relationship_type: str = "calls") -> EdgeView:
    return EdgeView(
        source_symbol_id=source.id, target_symbol_id=target.id, relationship_type=relationship_type
    )


def _symbols_by_id(*symbols: SymbolView) -> dict[uuid.UUID, SymbolView]:
    return {s.id: s for s in symbols}


# ---- direct / transitive impact, depth ------------------------------------


def test_direct_impact() -> None:
    a, b = _symbol("a"), _symbol("b")
    edges = [_edge(a, b)]
    result = compute_impact(REVISION_ID, b, edges, _symbols_by_id(a, b))
    assert [s.qualified_name for s in result.direct_impacts] == ["a"]
    assert result.transitive_impacts == ()
    assert result.max_depth == 1


def test_transitive_impact_with_correct_depths() -> None:
    a, b, c, d = _symbol("a"), _symbol("b"), _symbol("c"), _symbol("d")
    edges = [_edge(a, b), _edge(b, c), _edge(c, d)]
    result = compute_impact(REVISION_ID, d, edges, _symbols_by_id(a, b, c, d))

    depths = {
        s.qualified_name: s.depth for s in (*result.direct_impacts, *result.transitive_impacts)
    }
    assert depths == {"c": 1, "b": 2, "a": 3}
    assert [s.qualified_name for s in result.direct_impacts] == ["c"]
    assert [s.qualified_name for s in result.transitive_impacts] == ["b", "a"]
    assert result.max_depth == 3


# ---- diamond / branch merging / shortest depth ----------------------------


def test_diamond_merge_produces_shortest_depth_and_no_duplicates() -> None:
    """A -> B, A -> C, B -> D, C -> D. Impact(D): B=1, C=1, A=2 — A is
    reached via two paths (through B and through C) but must appear once,
    at its shortest depth, per the task's own worked example."""
    a, b, c, d = _symbol("a"), _symbol("b"), _symbol("c"), _symbol("d")
    edges = [_edge(a, b), _edge(a, c), _edge(b, d), _edge(c, d)]
    result = compute_impact(REVISION_ID, d, edges, _symbols_by_id(a, b, c, d))

    all_impacts = (*result.direct_impacts, *result.transitive_impacts)
    by_name = {s.qualified_name: s for s in all_impacts}
    assert len(all_impacts) == 3  # a appears exactly once, not twice
    assert by_name["b"].depth == 1
    assert by_name["c"].depth == 1
    assert by_name["a"].depth == 2


# ---- cycle handling --------------------------------------------------------


def test_multi_node_cycle_terminates_without_duplicates() -> None:
    """X -> Y -> Z -> X. Changing X must terminate and return {Y, Z} —
    never re-including X itself."""
    x, y, z = _symbol("x"), _symbol("y"), _symbol("z")
    edges = [_edge(x, y), _edge(y, z), _edge(z, x)]
    result = compute_impact(REVISION_ID, x, edges, _symbols_by_id(x, y, z))

    all_impacts = (*result.direct_impacts, *result.transitive_impacts)
    names = {s.qualified_name for s in all_impacts}
    assert names == {"y", "z"}
    assert "x" not in names
    by_name = {s.qualified_name: s for s in all_impacts}
    assert by_name["z"].depth == 1  # z -> x directly
    assert by_name["y"].depth == 2  # y -> z -> x


def test_self_cycle_terminates_and_excludes_self() -> None:
    """A -> A. Must terminate and must NOT report A as its own dependent —
    the starting symbol is seeded into `visited` before traversal begins."""
    a = _symbol("a")
    edges = [_edge(a, a)]
    result = compute_impact(REVISION_ID, a, edges, _symbols_by_id(a))

    assert result.direct_impacts == ()
    assert result.transitive_impacts == ()
    assert result.max_depth == 0


# ---- relationship type preservation ----------------------------------------


def test_relationship_type_is_preserved_per_impacted_symbol() -> None:
    a, b, c = _symbol("a"), _symbol("b"), _symbol("c")
    edges = [_edge(a, c, "imports"), _edge(b, c, "calls")]
    result = compute_impact(REVISION_ID, c, edges, _symbols_by_id(a, b, c))

    by_name = {s.qualified_name: s for s in result.direct_impacts}
    assert by_name["a"].relationship_types == ("imports",)
    assert by_name["b"].relationship_types == ("calls",)


def test_multiple_relationship_types_to_the_same_symbol_are_combined() -> None:
    a, b = _symbol("a"), _symbol("b")
    edges = [_edge(a, b, "calls"), _edge(a, b, "references")]
    result = compute_impact(REVISION_ID, b, edges, _symbols_by_id(a, b))

    assert len(result.direct_impacts) == 1  # one impacted symbol, not two
    assert result.direct_impacts[0].relationship_types == ("calls", "references")


# ---- determinism / ordering -------------------------------------------------


def test_ordering_is_depth_then_file_path_then_qualified_name_then_symbol_id() -> None:
    target = _symbol("target")
    # Two depth-1 dependents in different files, deliberately out of
    # alphabetical insertion order, to prove the sort — not insertion
    # order — determines the result.
    zeta = SymbolView(
        id=uuid.uuid4(),
        qualified_name="zeta",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="z_file.py",
        parent_symbol_id=None,
    )
    alpha = SymbolView(
        id=uuid.uuid4(),
        qualified_name="alpha",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a_file.py",
        parent_symbol_id=None,
    )
    edges = [_edge(zeta, target), _edge(alpha, target)]
    result = compute_impact(REVISION_ID, target, edges, _symbols_by_id(target, zeta, alpha))
    assert [s.qualified_name for s in result.direct_impacts] == ["alpha", "zeta"]


def test_repeated_computation_is_identical() -> None:
    a, b, c, d = _symbol("a"), _symbol("b"), _symbol("c"), _symbol("d")
    edges = [_edge(a, b), _edge(a, c), _edge(b, d), _edge(c, d)]
    symbols = _symbols_by_id(a, b, c, d)
    first = compute_impact(REVISION_ID, d, edges, symbols)
    second = compute_impact(REVISION_ID, d, edges, symbols)
    assert first == second


# ---- Task 11 adversarial: direction -----------------------------------------


def test_impact_is_never_the_forward_direction() -> None:
    """A -> B (A depends on B). Impact(B) must contain A (A is the
    dependent). Impact(A) must NOT contain B — B is a dependency of A, not
    a dependent of it. Mixing these up is exactly the class of bug this
    test exists to catch."""
    a, b = _symbol("a"), _symbol("b")
    edges = [_edge(a, b)]
    symbols = _symbols_by_id(a, b)

    impact_of_b = compute_impact(REVISION_ID, b, edges, symbols)
    assert [s.qualified_name for s in impact_of_b.direct_impacts] == ["a"]

    impact_of_a = compute_impact(REVISION_ID, a, edges, symbols)
    assert impact_of_a.direct_impacts == ()
    assert impact_of_a.transitive_impacts == ()


# ---- target resolution ------------------------------------------------------


def test_resolve_target_not_found() -> None:
    assert resolve_target([]) == TargetResolution(status="not_found")


def test_resolve_target_found() -> None:
    a = _symbol("a")
    resolution = resolve_target([a])
    assert resolution.status == "found"
    assert resolution.symbol == a


def test_resolve_target_ambiguous_does_not_guess() -> None:
    a1, a2 = _symbol("a"), _symbol("a")
    resolution = resolve_target([a1, a2])
    assert resolution.status == "ambiguous"
    assert set(resolution.candidates) == {a1, a2}
    assert resolution.symbol is None


# ---- file aggregation --------------------------------------------------------


def test_file_aggregation_counts_and_minimum_depth() -> None:
    target = _symbol("target")
    file_id = uuid.uuid4()
    sibling_a = SymbolView(
        id=uuid.uuid4(),
        qualified_name="helper_a",
        kind="function",
        file_id=file_id,
        file_path="shared.py",
        parent_symbol_id=None,
    )
    sibling_b = SymbolView(
        id=uuid.uuid4(),
        qualified_name="helper_b",
        kind="function",
        file_id=file_id,
        file_path="shared.py",
        parent_symbol_id=None,
    )
    deep_caller = _symbol("deep_caller")
    edges = [
        _edge(sibling_a, target),
        _edge(sibling_b, target),
        _edge(deep_caller, sibling_a),
    ]
    result = compute_impact(
        REVISION_ID, target, edges, _symbols_by_id(target, sibling_a, sibling_b, deep_caller)
    )

    shared_file = next(f for f in result.impacted_files if f.path == "shared.py")
    assert shared_file.symbol_count == 2
    assert shared_file.minimum_depth == 1

    deep_caller_file = next(f for f in result.impacted_files if f.path == "deep_caller.py")
    assert deep_caller_file.symbol_count == 1
    assert deep_caller_file.minimum_depth == 2


def test_target_own_file_not_counted_unless_another_impacted_symbol_lives_there() -> None:
    target = _symbol("target")
    dependent = SymbolView(
        id=uuid.uuid4(),
        qualified_name="dependent_in_same_file",
        kind="function",
        file_id=target.file_id,
        file_path=target.file_path,
        parent_symbol_id=None,
    )
    edges = [_edge(dependent, target)]
    result = compute_impact(REVISION_ID, target, edges, _symbols_by_id(target, dependent))

    # The target's own file DOES appear here, but only because `dependent`
    # (an actually-impacted symbol) lives there — not because of the target
    # itself, which the traversal never includes in its own impact set.
    assert len(result.impacted_files) == 1
    assert result.impacted_files[0].path == target.file_path
    assert result.impacted_files[0].symbol_count == 1
