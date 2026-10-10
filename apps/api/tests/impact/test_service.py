"""Full-pipeline Impact Analysis tests: ingestion -> parsing -> graph
construction -> impact traversal, against `impact_fixture_repo`'s real
Python source. Requires live PostgreSQL — same `db_session` fixture and
explicit-skip behavior as tests/db/, tests/graph/, etc. Never substitutes
SQLite. Expected edges/depths below were independently verified by directly
exercising app/graph/python_extractor.py + app/graph/resolver.py against
this fixture's exact source (see the session's own validation trace) since
no live Postgres is available in this environment either.
"""

import uuid
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.impact.service import (
    run_impact_analysis_by_qualified_name,
    run_impact_analysis_by_symbol_id,
)
from app.models import AnalysisRun, Symbol
from tests.graph.conftest import ingest_parse_and_build_graph, symbols_by_qualified_name


def _run_by_qname(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, qualified_name: str
):
    return run_impact_analysis_by_qualified_name(db, repository_id, revision_id, qualified_name)


# ---- commerce: straight calls chain + import-only relationship ------------


def test_direct_and_transitive_impact_chain(db_session: Session, impact_fixture_repo: Path) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(
        db_session, repository_id, revision_id, "commerce.discount.apply_discount"
    )
    assert run_result.status == "completed"
    result = run_result.result
    assert result is not None

    direct_names = {s.qualified_name for s in result.direct_impacts}
    assert direct_names == {"commerce.total", "commerce.total.calculate_total"}
    assert all(s.depth == 1 for s in result.direct_impacts)

    transitive_names = {s.qualified_name for s in result.transitive_impacts}
    assert transitive_names == {"commerce.checkout", "commerce.checkout.checkout_order"}
    assert all(s.depth == 2 for s in result.transitive_impacts)

    assert result.max_depth == 2

    calls_edge = next(
        s for s in result.direct_impacts if s.qualified_name == "commerce.total.calculate_total"
    )
    assert calls_edge.relationship_types == ("calls",)
    imports_edge = next(s for s in result.direct_impacts if s.qualified_name == "commerce.total")
    assert imports_edge.relationship_types == ("imports",)


def test_import_only_relationship_is_preserved(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    """checkout.py does `import commerce.pricing as pricing` but never
    calls anything on it — Impact(commerce.pricing) must show exactly the
    importing module, via "imports" only, with no spurious "calls" edge."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(db_session, repository_id, revision_id, "commerce.pricing")
    assert run_result.status == "completed"
    result = run_result.result
    assert result is not None

    assert [s.qualified_name for s in result.direct_impacts] == ["commerce.checkout"]
    assert result.direct_impacts[0].relationship_types == ("imports",)
    # checkout_order itself never calls into pricing — must not appear.
    assert result.transitive_impacts == ()
    assert result.max_depth == 1


# ---- diamond: branch merging / shortest depth / file aggregation ----------


def test_diamond_merge_shortest_depth_and_file_aggregation(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func")
    assert run_result.status == "completed"
    result = run_result.result
    assert result is not None

    direct_names = {s.qualified_name for s in result.direct_impacts}
    assert direct_names == {"diamond.b", "diamond.b.b_func", "diamond.c", "diamond.c.c_func"}
    assert all(s.depth == 1 for s in result.direct_impacts)

    # diamond.a / diamond.a.a_func are reachable via BOTH b_func and c_func
    # — must appear exactly once each, at depth 2 (branch merging).
    transitive_names = [s.qualified_name for s in result.transitive_impacts]
    assert sorted(transitive_names) == ["diamond.a", "diamond.a.a_func"]
    assert len(transitive_names) == 2  # no duplicates
    assert all(s.depth == 2 for s in result.transitive_impacts)
    assert result.max_depth == 2

    files_by_path = {f.path: f for f in result.impacted_files}
    assert files_by_path["diamond/b.py"].symbol_count == 2
    assert files_by_path["diamond/b.py"].minimum_depth == 1
    assert files_by_path["diamond/c.py"].symbol_count == 2
    assert files_by_path["diamond/c.py"].minimum_depth == 1
    assert files_by_path["diamond/a.py"].symbol_count == 2
    assert files_by_path["diamond/a.py"].minimum_depth == 2
    # The target's own file (diamond/d.py) is not impacted by anything here.
    assert "diamond/d.py" not in files_by_path


# ---- cycle: termination, no duplicates, never includes the target --------


def test_cycle_terminates_without_reintroducing_the_target(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    """x_func -> y_func -> z_func -> x_func via real (never-executed,
    statically-parsed-only) circular imports. Impact(x_func) must terminate
    and never include x_func itself."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(db_session, repository_id, revision_id, "cycle.x.x_func")
    assert run_result.status == "completed"
    result = run_result.result
    assert result is not None

    all_names = {s.qualified_name for s in (*result.direct_impacts, *result.transitive_impacts)}
    assert "cycle.x.x_func" not in all_names  # never its own dependent

    depths = {
        s.qualified_name: s.depth for s in (*result.direct_impacts, *result.transitive_impacts)
    }
    assert depths["cycle.z"] == 1
    assert depths["cycle.z.z_func"] == 1
    assert depths["cycle.y"] == 2
    assert depths["cycle.y.y_func"] == 2
    # The cycle closes back on x_func (already visited, excluded), but the
    # *module* symbol "cycle.x" is a distinct node from "cycle.x.x_func"
    # and is newly reached one hop further out.
    assert depths["cycle.x"] == 3
    assert "cycle.x.x_func" not in depths

    assert len(all_names) == 5
    assert result.max_depth == 3


# ---- revision isolation -----------------------------------------------------


def test_revision_isolation_identical_qualified_name_in_another_revision(
    db_session: Session, impact_fixture_repo: Path, other_revision_repo: Path
) -> None:
    """`other_revision_repo` defines "diamond.d.d_func" too (identical
    qualified name) with "diamond.other.other_func" depending on it. That
    dependent must never appear when analyzing the *original* revision's
    d_func."""
    main_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo, owner="main")
    other_result, _ = ingest_parse_and_build_graph(db_session, other_revision_repo, owner="other")

    main_repository_id = uuid.UUID(main_result.repository_id)
    main_revision_id = uuid.UUID(main_result.revision_id)
    run_result = _run_by_qname(db_session, main_repository_id, main_revision_id, "diamond.d.d_func")
    assert run_result.status == "completed"
    result = run_result.result
    assert result is not None

    all_names = {s.qualified_name for s in (*result.direct_impacts, *result.transitive_impacts)}
    assert "diamond.other.other_func" not in all_names
    assert "diamond.other" not in all_names
    # Sanity: the other revision's data actually exists (so absence above
    # is isolation, not coincidence).
    other_symbols = symbols_by_qualified_name(db_session, uuid.UUID(other_result.revision_id))
    assert "diamond.other.other_func" in other_symbols


# ---- target resolution: not-found / ambiguous ------------------------------


def test_target_not_found_is_explicit(db_session: Session, impact_fixture_repo: Path) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(db_session, repository_id, revision_id, "does.not.exist")
    assert run_result.status == "not_found"
    assert run_result.result is None

    # No AnalysisRun was recorded for a target that was never analyzed.
    runs = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "impact_analysis")
        )
        .scalars()
        .all()
    )
    assert runs == []


def test_ambiguous_qualified_name_does_not_guess(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    # Manually introduce a genuine ambiguity: the schema permits two Symbol
    # rows sharing a qualified_name within one revision (no UNIQUE
    # constraint — see impact-analysis.md section 0); construct that state
    # directly rather than relying on the normal pipeline to produce it.
    existing = symbols_by_qualified_name(db_session, revision_id)["diamond.d.d_func"]
    duplicate = Symbol(
        repository_id=existing.repository_id,
        revision_id=revision_id,
        file_id=existing.file_id,
        kind="function",
        name="d_func",
        qualified_name="diamond.d.d_func",
        start_line=99,
        end_line=99,
        analyzer="test",
    )
    db_session.add(duplicate)
    db_session.flush()

    run_result = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func")
    assert run_result.status == "ambiguous"
    assert run_result.result is None
    assert {c.id for c in run_result.candidates} == {existing.id, duplicate.id}


# ---- target resolution by Symbol.id ----------------------------------------


def test_run_by_symbol_id_matches_run_by_qualified_name(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    target_symbol = symbols_by_qualified_name(db_session, revision_id)["diamond.d.d_func"]
    by_id = run_impact_analysis_by_symbol_id(
        db_session, repository_id, revision_id, target_symbol.id
    )
    by_name = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func")

    assert by_id.status == by_name.status == "completed"
    assert by_id.result is not None and by_name.result is not None
    assert by_id.result.direct_impacts == by_name.result.direct_impacts
    assert by_id.result.transitive_impacts == by_name.result.transitive_impacts


# ---- AnalysisRun integration / determinism / query efficiency -------------


def test_analysis_run_is_recorded_on_success(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    run_result = _run_by_qname(
        db_session, repository_id, revision_id, "commerce.discount.apply_discount"
    )
    assert run_result.analysis_run_id is not None

    run = db_session.get(AnalysisRun, run_result.analysis_run_id)
    assert run is not None
    assert run.analysis_type == "impact_analysis"
    assert run.status == "completed"
    assert run.revision_id == revision_id


def test_repeated_analysis_is_deterministic(db_session: Session, impact_fixture_repo: Path) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    first = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func").result
    second = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func").result
    assert first is not None and second is not None
    assert first.direct_impacts == second.direct_impacts
    assert first.transitive_impacts == second.transitive_impacts
    assert first.impacted_files == second.impacted_files


def test_query_count_does_not_scale_with_impacted_symbol_count(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    """Not one SQL round-trip per visited node — impact-analysis.md section
    9. A handful of fixed queries regardless of how many symbols the
    traversal actually reaches."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, impact_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    queries: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        # SAVEPOINT bookkeeping comes from the test session's
        # create_savepoint join mode, not from the code under test.
        if not statement.lstrip().upper().startswith(("SAVEPOINT", "RELEASE SAVEPOINT")):
            queries.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        run_result = _run_by_qname(db_session, repository_id, revision_id, "diamond.d.d_func")
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert run_result.status == "completed"
    assert run_result.result is not None
    assert len(run_result.result.direct_impacts) + len(run_result.result.transitive_impacts) >= 6
    # A small, fixed number of queries: revision lookup, symbol lookup for
    # resolution, edge load, symbol load, AnalysisRun insert, AnalysisRun
    # update — not one per impacted symbol (6+).
    assert len(queries) < 10
