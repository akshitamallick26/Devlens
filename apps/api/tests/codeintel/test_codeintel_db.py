"""DB tests for app/codeintel/service.py. Requires live PostgreSQL (same
skip behavior as every other DB suite). Real-pipeline cases use the git
fixture repository; graph-shape cases (cycles, limits) build rows directly
with the overview tests' builder so expectations are exact."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.codeintel import service
from app.core.config import get_settings
from app.pipeline.service import analyze_revision
from tests.automation.conftest import ingest_revision
from tests.overview.test_service_db import _Builder

NO_EMBEDDINGS = get_settings().model_copy(update={"embedding_api_key": None})


def _analyzed(db: Session, repo: Path) -> tuple[uuid.UUID, uuid.UUID]:
    result = ingest_revision(db, repo, owner="codeintel")
    repository_id, revision_id = uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)
    analyze_revision(db, repository_id, revision_id, run_rag_stage=False, settings=NO_EMBEDDINGS)
    return repository_id, revision_id


def _symbol_id(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, name: str
) -> uuid.UUID:
    found = service.search_symbols(db, repository_id, revision_id, name, limit=5).symbols
    exact = [s for s in found if s.qualified_name == name]
    assert exact, f"{name} not found"
    return exact[0].symbol_id


# ---- Files and source ----


def test_directory_browsing_lists_children_not_the_whole_tree(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _analyzed(db_session, automation_fixture_repo)
    root = service.browse_directory(db_session, repository_id, revision_id, "")
    assert [d.name for d in root.directories] == ["app"]
    assert root.directories[0].file_count == 3
    assert root.files == ()

    app_dir = service.browse_directory(db_session, repository_id, revision_id, "app", limit=2)
    assert app_dir.total_files == 3
    assert [f.path for f in app_dir.files] == ["app/__init__.py", "app/caller.py"]
    page_two = service.browse_directory(
        db_session, repository_id, revision_id, "app", limit=2, offset=2
    )
    assert [f.path for f in page_two.files] == ["app/math_utils.py"]


def test_source_ranges_are_bounded_and_exact(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _analyzed(db_session, automation_fixture_repo)
    file_id = next(
        f.file_id
        for f in service.browse_directory(db_session, repository_id, revision_id, "app").files
        if f.path == "app/math_utils.py"
    )
    whole = service.get_source_range(db_session, repository_id, revision_id, file_id)
    assert whole.lines[0] == "def add(a, b):"
    # "def add...\n    return a + b\n\n\ndef unused():\n    return 1\n" is 6 lines.
    assert whole.total_lines == 6

    monkeypatch.setattr(service, "MAX_SOURCE_LINES", 2)
    capped = service.get_source_range(db_session, repository_id, revision_id, file_id, 1, 5)
    assert capped.lines == ("def add(a, b):", "    return a + b")
    assert capped.truncated is True
    # A range past the end is cut at the last line, not padded.
    tail = service.get_source_range(db_session, repository_id, revision_id, file_id, 6, 9)
    assert tail.lines == ("    return 1",)
    assert tail.end_line == 6
    with pytest.raises(service.InvalidRequestError):
        service.get_source_range(db_session, repository_id, revision_id, file_id, 4, 2)


def test_file_symbols_and_symbol_detail(db_session: Session, automation_fixture_repo: Path) -> None:
    repository_id, revision_id = _analyzed(db_session, automation_fixture_repo)
    add_id = _symbol_id(db_session, repository_id, revision_id, "app.math_utils.add")
    detail = service.get_symbol_detail(db_session, repository_id, revision_id, add_id)
    assert detail.symbol.kind == "function"
    assert detail.symbol.file_path == "app/math_utils.py"
    assert detail.dependent_count >= 1  # app.caller.compute calls it

    file_detail = service.get_file_detail(
        db_session, repository_id, revision_id, detail.symbol.file_id
    )
    names = [s.qualified_name for s in file_detail.symbols]
    assert "app.math_utils.add" in names and "app.math_utils.unused" in names
    assert file_detail.source_available


def test_reads_reject_ids_from_another_revision(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _analyzed(db_session, automation_fixture_repo)
    add_id = _symbol_id(db_session, repository_id, revision_id, "app.math_utils.add")
    other = ingest_revision(db_session, automation_fixture_repo, owner="codeintel-other")
    other_repo, other_rev = uuid.UUID(other.repository_id), uuid.UUID(other.revision_id)
    with pytest.raises(service.NotFoundError):
        service.get_symbol_detail(db_session, other_repo, other_rev, add_id)
    with pytest.raises(service.RevisionConflictError):
        service.get_symbol_detail(db_session, repository_id, other_rev, add_id)


# ---- Graph neighbourhood on the real graph ----


def test_real_graph_edges_keep_dependent_to_dependency_direction(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _analyzed(db_session, automation_fixture_repo)
    add_id = _symbol_id(db_session, repository_id, revision_id, "app.math_utils.add")
    compute_id = _symbol_id(db_session, repository_id, revision_id, "app.caller.compute")

    dependents = service.graph_neighborhood(
        db_session, repository_id, revision_id, add_id, "dependents", 1, 50
    )
    calls = [e for e in dependents.edges if e.relationship_type == "calls"]
    assert any(e.source == compute_id and e.target == add_id for e in calls)
    assert {n.symbol_id: n.depth for n in dependents.nodes}[add_id] == 0
    via = {n.symbol_id: n.reached_via for n in dependents.nodes}
    assert via[add_id] == "focus" and via[compute_id] == "dependents"

    dependencies = service.graph_neighborhood(
        db_session, repository_id, revision_id, compute_id, "dependencies", 1, 50
    )
    assert any(e.source == compute_id and e.target == add_id for e in dependencies.edges)


# ---- Graph shape: cycles, depth, limits ----


def _chain(builder: "_Builder") -> tuple[object, list[object]]:
    rev = builder.revision("8")
    f = builder.file(rev, "pkg/m.py")
    a, b, c, d = (builder.symbol(rev, f, f"pkg.m.{n}") for n in "abcd")
    builder.edge(rev, a, b)  # a -> b -> c -> a (cycle), c -> d
    builder.edge(rev, b, c)
    builder.edge(rev, c, a)
    builder.edge(rev, c, d)
    return rev, [a, b, c, d]


def test_cycles_terminate_and_depth_is_shortest(db_session: Session, builder: "_Builder") -> None:
    rev, (a, b, c, d) = _chain(builder)
    result = service.graph_neighborhood(
        db_session,
        builder.repository.id,
        rev.id,
        a.id,
        "dependencies",
        4,
        50,  # type: ignore[attr-defined]
    )
    depths = {n.qualified_name: n.depth for n in result.nodes}
    assert depths == {"pkg.m.a": 0, "pkg.m.b": 1, "pkg.m.c": 2, "pkg.m.d": 3}
    pairs = {(e.source, e.target) for e in result.edges}
    assert (c.id, a.id) in pairs  # type: ignore[attr-defined]  # the cycle-closing edge is returned
    assert result.truncated is False


def test_depth_bound_stops_traversal(db_session: Session, builder: "_Builder") -> None:
    rev, (a, b, c, d) = _chain(builder)
    result = service.graph_neighborhood(
        db_session,
        builder.repository.id,
        rev.id,
        a.id,
        "dependencies",
        1,
        50,  # type: ignore[attr-defined]
    )
    assert {n.qualified_name for n in result.nodes} == {"pkg.m.a", "pkg.m.b"}
    assert result.max_depth_reached == 1


def test_node_limit_truncates_and_drops_dangling_edges(
    db_session: Session,
    builder: "_Builder",
) -> None:
    rev = builder.revision("9")
    f = builder.file(rev, "hub/h.py")
    hub = builder.symbol(rev, f, "hub.h.hub")
    for i in range(5):
        builder.edge(rev, builder.symbol(rev, f, f"hub.h.caller{i}"), hub)
    result = service.graph_neighborhood(
        db_session, builder.repository.id, rev.id, hub.id, "dependents", 1, 3
    )
    assert len(result.nodes) == 3
    assert result.truncated is True
    node_ids = {n.symbol_id for n in result.nodes}
    assert all(e.source in node_ids and e.target in node_ids for e in result.edges)
    assert all(e.target == hub.id for e in result.edges)


def test_bounds_are_validated(db_session: Session, builder: "_Builder") -> None:
    rev, (a, *_rest) = _chain(builder)
    for kwargs in ({"depth": 0}, {"depth": 5}, {"limit": 0}, {"limit": 201}, {"direction": "up"}):
        args = {"direction": "both", "depth": 2, "limit": 10, **kwargs}
        with pytest.raises(service.InvalidRequestError):
            service.graph_neighborhood(
                db_session,
                builder.repository.id,
                rev.id,
                a.id,
                **args,  # type: ignore[arg-type,attr-defined]
            )


def test_ids_from_revision_a_never_resolve_through_revision_b_of_the_same_repository(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    from tests.automation.conftest import modify_and_commit

    repository_id, rev_a = _analyzed(db_session, automation_fixture_repo)
    add_a = _symbol_id(db_session, repository_id, rev_a, "app.math_utils.add")
    file_a = service.get_symbol_detail(db_session, repository_id, rev_a, add_a).symbol.file_id

    modify_and_commit(automation_fixture_repo, "app/extra.py", "def extra():\n    return 2\n", "b")
    second = ingest_revision(db_session, automation_fixture_repo, owner="codeintel")
    rev_b = uuid.UUID(second.revision_id)
    assert uuid.UUID(second.repository_id) == repository_id and rev_b != rev_a

    with pytest.raises(service.NotFoundError):
        service.get_file_detail(db_session, repository_id, rev_b, file_a)
    with pytest.raises(service.NotFoundError):
        service.get_source_range(db_session, repository_id, rev_b, file_a)
    with pytest.raises(service.NotFoundError):
        service.get_symbol_detail(db_session, repository_id, rev_b, add_a)
    with pytest.raises(service.NotFoundError):
        service.graph_neighborhood(db_session, repository_id, rev_b, add_a, "both", 2, 50)
    # Revision B was never analyzed: no symbols, no findings run, no tests.
    assert service.search_symbols(db_session, repository_id, rev_b, "add").symbols == ()
    assert service.list_findings(db_session, repository_id, rev_b).findings_run_id is None
    assert service.list_generated_tests(db_session, repository_id, rev_b).total_count == 0


def test_both_directions_mark_reach_and_keep_external_names_out_of_nodes(
    db_session: Session,
    builder: "_Builder",
) -> None:
    rev = builder.revision("7")
    f = builder.file(rev, "pkg/n.py")
    focus = builder.symbol(rev, f, "pkg.n.focus")
    caller = builder.symbol(rev, f, "pkg.n.caller")
    callee = builder.symbol(rev, f, "pkg.n.callee")
    base = builder.symbol(rev, f, "pkg.n.Base", kind="class")
    builder.edge(rev, caller, focus)  # caller -> focus (caller is a dependent)
    builder.edge(rev, focus, callee)  # focus -> callee (callee is a dependency)
    builder.edge(rev, focus, base, relationship="inherits")  # subclass -> base
    builder.edge(rev, focus, None, relationship="imports", external_ref="os")

    result = service.graph_neighborhood(
        db_session, builder.repository.id, rev.id, focus.id, "both", 2, 50
    )
    via = {n.qualified_name: (n.reached_via, n.depth) for n in result.nodes}
    assert via == {
        "pkg.n.focus": ("focus", 0),
        "pkg.n.caller": ("dependents", 1),
        "pkg.n.callee": ("dependencies", 1),
        "pkg.n.Base": ("dependencies", 1),
    }
    edges = {(e.source, e.target, e.relationship_type) for e in result.edges}
    assert edges == {
        (caller.id, focus.id, "calls"),
        (focus.id, callee.id, "calls"),
        (focus.id, base.id, "inherits"),
    }
    assert result.focus_external_references == ("os",)
    assert all(n.qualified_name != "os" for n in result.nodes)


def test_per_hop_edge_cap_truncates_traversal(
    db_session: Session,
    builder: "_Builder",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "MAX_EDGE_ROWS_PER_STEP", 3)
    rev = builder.revision("6")
    f = builder.file(rev, "hub/c.py")
    hub = builder.symbol(rev, f, "hub.c.hub")
    for i in range(6):
        builder.edge(rev, builder.symbol(rev, f, f"hub.c.caller{i}"), hub)
    result = service.graph_neighborhood(
        db_session, builder.repository.id, rev.id, hub.id, "dependents", 2, 200
    )
    assert result.truncated is True
    assert len(result.edges) == 3
    assert len(result.nodes) == 4  # the focus plus the 3 callers on the capped edges
