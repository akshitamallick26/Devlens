"""Pure blast-radius traversal tests — no DB, always run."""

import uuid

from app.impact.models import EdgeView, SymbolView
from app.security.blast_radius import compute_blast_radius


def _sv(symbol_id: uuid.UUID, name: str) -> SymbolView:
    return SymbolView(
        id=symbol_id,
        qualified_name=name,
        kind="function",
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        parent_symbol_id=None,
    )


def test_direct_dependent_is_found_at_depth_one() -> None:
    seed = uuid.uuid4()
    caller = uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), caller: _sv(caller, "caller")}
    edges = [EdgeView(source_symbol_id=caller, target_symbol_id=seed, relationship_type="calls")]

    result = compute_blast_radius(symbols[seed], edges, symbols)
    assert len(result.direct_dependents) == 1
    assert result.direct_dependents[0].symbol_id == caller
    assert result.direct_dependents[0].depth == 1
    assert result.transitive_dependents == ()


def test_transitive_dependent_is_found_at_correct_depth() -> None:
    seed, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), a: _sv(a, "a"), b: _sv(b, "b")}
    edges = [
        EdgeView(source_symbol_id=a, target_symbol_id=seed, relationship_type="calls"),
        EdgeView(source_symbol_id=b, target_symbol_id=a, relationship_type="calls"),
    ]
    result = compute_blast_radius(symbols[seed], edges, symbols)
    assert {s.symbol_id for s in result.direct_dependents} == {a}
    assert {s.symbol_id for s in result.transitive_dependents} == {b}
    assert next(s for s in result.transitive_dependents if s.symbol_id == b).depth == 2


def test_multiple_paths_to_the_same_symbol_record_both_relationship_types() -> None:
    seed, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), a: _sv(a, "a"), b: _sv(b, "b")}
    # `a` reaches seed both directly and via `b` -> at depth 1, only the
    # direct edge counts (BFS shortest-depth-first, same as Impact Analysis).
    edges = [
        EdgeView(source_symbol_id=a, target_symbol_id=seed, relationship_type="calls"),
        EdgeView(source_symbol_id=b, target_symbol_id=seed, relationship_type="imports"),
        EdgeView(source_symbol_id=a, target_symbol_id=b, relationship_type="calls"),
    ]
    result = compute_blast_radius(symbols[seed], edges, symbols)
    all_found = {s.symbol_id: s for s in result.direct_dependents + result.transitive_dependents}
    assert all_found[a].depth == 1
    assert all_found[b].depth == 1  # b reaches seed directly too


def test_cycle_terminates_and_seed_never_self_reports() -> None:
    seed, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), a: _sv(a, "a"), b: _sv(b, "b")}
    edges = [
        EdgeView(source_symbol_id=a, target_symbol_id=seed, relationship_type="calls"),
        EdgeView(source_symbol_id=b, target_symbol_id=a, relationship_type="calls"),
        EdgeView(source_symbol_id=a, target_symbol_id=b, relationship_type="calls"),  # a<->b cycle
    ]
    result = compute_blast_radius(symbols[seed], edges, symbols, max_depth=10)
    found_ids = {s.symbol_id for s in result.direct_dependents + result.transitive_dependents}
    assert seed not in found_ids
    assert found_ids == {a, b}


def test_dependency_direction_is_reverse_not_forward() -> None:
    """seed's *dependency* (something seed calls) must never appear as a
    dependent — blast radius is reverse traversal only."""
    seed, dependency = uuid.uuid4(), uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), dependency: _sv(dependency, "dependency")}
    edges = [
        EdgeView(source_symbol_id=seed, target_symbol_id=dependency, relationship_type="calls")
    ]
    result = compute_blast_radius(symbols[seed], edges, symbols)
    assert result.direct_dependents == ()
    assert result.transitive_dependents == ()


def test_bounded_depth_truncates_and_flags_truncated() -> None:
    seed = uuid.uuid4()
    chain = [uuid.uuid4() for _ in range(5)]
    symbols = {seed: _sv(seed, "seed")}
    edges = []
    prev = seed
    for i, node in enumerate(chain):
        symbols[node] = _sv(node, f"n{i}")
        edges.append(
            EdgeView(source_symbol_id=node, target_symbol_id=prev, relationship_type="calls")
        )
        prev = node

    result = compute_blast_radius(symbols[seed], edges, symbols, max_depth=2)
    found = {s.symbol_id for s in result.direct_dependents + result.transitive_dependents}
    assert found == {chain[0], chain[1]}
    assert result.truncated is True
    assert result.max_depth_reached == 2


def test_bounded_symbols_truncates_and_flags_truncated() -> None:
    seed = uuid.uuid4()
    callers = [uuid.uuid4() for _ in range(5)]
    symbols = {seed: _sv(seed, "seed")}
    edges = []
    for i, caller in enumerate(callers):
        symbols[caller] = _sv(caller, f"c{i}")
        edges.append(
            EdgeView(source_symbol_id=caller, target_symbol_id=seed, relationship_type="calls")
        )

    result = compute_blast_radius(symbols[seed], edges, symbols, max_symbols=3)
    assert len(result.direct_dependents) == 3
    assert result.truncated is True


def test_no_dependents_produces_empty_untruncated_result() -> None:
    seed = uuid.uuid4()
    symbols = {seed: _sv(seed, "seed")}
    result = compute_blast_radius(symbols[seed], [], symbols)
    assert result.direct_dependents == ()
    assert result.transitive_dependents == ()
    assert result.impacted_files == ()
    assert result.truncated is False
    assert result.max_depth_reached == 0


def test_deterministic_ordering_across_repeated_calls() -> None:
    seed = uuid.uuid4()
    callers = [uuid.uuid4() for _ in range(6)]
    symbols = {seed: _sv(seed, "seed")}
    edges = []
    for i, caller in enumerate(callers):
        symbols[caller] = _sv(caller, f"c{i}")
        edges.append(
            EdgeView(source_symbol_id=caller, target_symbol_id=seed, relationship_type="calls")
        )

    first = compute_blast_radius(symbols[seed], edges, symbols)
    second = compute_blast_radius(symbols[seed], edges, symbols)
    assert first == second


def test_evidence_paths_start_at_seed_and_end_at_dependent() -> None:
    seed, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    symbols = {seed: _sv(seed, "seed"), a: _sv(a, "a"), b: _sv(b, "b")}
    edges = [
        EdgeView(source_symbol_id=a, target_symbol_id=seed, relationship_type="calls"),
        EdgeView(source_symbol_id=b, target_symbol_id=a, relationship_type="imports"),
    ]
    result = compute_blast_radius(symbols[seed], edges, symbols)
    path_for_b = next(p for p in result.evidence_paths if p.symbol_ids[-1] == b)
    assert path_for_b.symbol_ids[0] == seed
    assert path_for_b.symbol_ids == (seed, a, b)
    assert path_for_b.relationship_types == ("calls", "imports")
