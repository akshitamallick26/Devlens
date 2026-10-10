"""Full-pipeline graph-construction tests: a realistic local Git repo ->
ingestion -> parsing -> graph construction -> real GraphEdge rows. Requires
live PostgreSQL — same `db_session` fixture and explicit-skip behavior as
tests/db/, tests/ingestion/, and tests/parsing/. Never substitutes SQLite.

Task 18's false positives live in test_false_positives.py and Task 16's
revision isolation lives in test_revision_isolation.py; both reuse the
helpers in conftest.py. This file covers Tasks 19 (positive edges), 20 (edge
direction), 15 (idempotency), and 24 (transactional failure).
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.graph.service import GraphConstructionError, run_graph_construction
from app.models import AnalysisRun, GraphEdge, RepositoryRevision
from app.parsing.service import run_parsing
from tests.graph.conftest import (
    edges_for_revision,
    ingest,
    ingest_parse_and_build_graph,
    symbols_by_qualified_name,
)

# ---- Task 19: realistic positive-edge fixture ------------------------------


def test_positive_edges_match_expected_evidence(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    ingestion_result, graph_result = ingest_parse_and_build_graph(db_session, graph_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    by_qname = symbols_by_qualified_name(db_session, revision_id)
    edges = edges_for_revision(db_session, revision_id)

    def find(relationship_type: str, source_qname: str, **target_filter: object) -> GraphEdge:
        source = by_qname[source_qname]
        candidates = [
            e
            for e in edges
            if e.relationship_type == relationship_type and e.source_symbol_id == source.id
        ]
        if "target_qname" in target_filter:
            target = by_qname[target_filter["target_qname"]]
            candidates = [e for e in candidates if e.target_symbol_id == target.id]
        if "target_external_ref" in target_filter:
            ref = target_filter["target_external_ref"]
            candidates = [e for e in candidates if e.target_external_ref == ref]
        assert len(candidates) >= 1, (
            f"expected a {relationship_type} edge from {source_qname} "
            f"matching {target_filter}, found none among {len(edges)} edges"
        )
        return candidates[0]

    # imports: main.py's aliased dotted import, aliased from-import, and
    # plain stdlib import.
    e1 = find("imports", "main", target_qname="app.payments")
    e2 = find("imports", "main", target_qname="app.payments.charge")
    e3 = find("imports", "main", target_external_ref="os")
    for e in (e1, e2, e3):
        assert e.confidence is None  # v1 has no calibrated confidence model
        assert e.resolution_method == "ast_resolution"
    assert e1.target_external_ref is None  # internal: external_ref unset
    assert e3.target_symbol_id is None  # external: symbol_id unset, never a fake Symbol

    # imports: false_positives.py's from-import (the binding itself is
    # legitimate, even though it's never successfully called — see
    # test_false_positives.py).
    find("imports", "false_positives", target_qname="app.payments.charge")

    # calls: module-attribute call via the aliased dotted import.
    find("calls", "main.run", target_qname="app.payments.charge")
    # calls: bare call via the aliased from-import (same target, different
    # call site — both evidence rows persist, see the dedup-key assertion
    # below).
    run_to_charge = [
        e
        for e in edges
        if e.relationship_type == "calls"
        and e.source_symbol_id == by_qname["main.run"].id
        and e.target_symbol_id == by_qname["app.payments.charge"].id
    ]
    assert len(run_to_charge) == 2
    assert {e.evidence_start_line for e in run_to_charge} == {7, 8}

    # calls: external attribute call.
    find("calls", "main.run", target_external_ref="os.getcwd")

    # calls: self-method resolution, no MRO.
    find("calls", "app.payments.Payment.process", target_qname="app.payments.Payment.validate")

    # calls: nested/local function resolution.
    find("calls", "app.utils.helper", target_qname="app.utils.helper.inner")

    # inherits: subclass -> base, same file.
    find("inherits", "app.payments.PremiumPayment", target_qname="app.payments.Payment")

    assert graph_result.import_edges >= 4
    assert graph_result.call_edges >= 4
    assert graph_result.inherits_edges == 1


# ---- Task 4 / Task 6.3-6.4: external-edge representation -------------------


def test_external_edges_have_no_symbol_id_and_no_fabricated_certainty(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    """Every GraphEdge row with target_external_ref set: target_symbol_id is
    NULL (never a fake Symbol for external code — Task 4), relationship_type
    is one of the kinds that may legitimately target external code (never
    `inherits` — section 6c), and confidence is NULL — not a fabricated
    "certain" score, and no different from an internal edge's confidence
    (it is not a signal of resolution strength at all — see
    test_confidence_is_not_the_proof_of_resolution)."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, graph_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    edges = edges_for_revision(db_session, revision_id)
    external_edges = [e for e in edges if e.target_external_ref is not None]
    assert len(external_edges) >= 2  # "os" import and "os.getcwd" call, at minimum

    for e in external_edges:
        assert e.target_symbol_id is None
        assert e.relationship_type in ("imports", "calls")  # never "inherits" — section 6c
        assert e.revision_id == revision_id
        assert e.source_symbol_id is not None
        assert e.evidence_file_id is not None
        assert e.evidence_start_line >= 1
        assert e.confidence is None  # not calibrated — never a fabricated "certain" score


def test_confidence_is_not_the_proof_of_resolution(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    """The signal that distinguishes resolved-internal / unresolved /
    external is edge existence and target_symbol_id/target_external_ref —
    never `confidence`, which is NULL (not calibrated) on every persisted
    edge regardless of resolution kind. An unresolved reference (Task 18's
    false-positive scenarios) never gets a row with a low/null confidence
    written for it — it gets no row at all."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, graph_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    by_qname = symbols_by_qualified_name(db_session, revision_id)
    edges = edges_for_revision(db_session, revision_id)
    assert len(edges) > 0

    # Every persisted edge, internal or external, has confidence = NULL — it
    # cannot be the signal that distinguishes them, and it is never a
    # non-NULL value masquerading as a real score.
    assert all(e.confidence is None for e in edges)

    # The unresolved `service.charge()` call in false_positives.py produced
    # no GraphEdge row at all (not a row with a low/null confidence).
    unknown_receiver_symbol = by_qname["false_positives.unknown_receiver"]
    assert not any(e.source_symbol_id == unknown_receiver_symbol.id for e in edges)


# ---- Task 20: edge direction ------------------------------------------------


def test_edge_direction_is_dependent_to_dependency(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    """source = the dependent (importer/caller/subtype); target = the
    dependency (imported/callee/supertype) — never reversed."""
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, graph_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    by_qname = symbols_by_qualified_name(db_session, revision_id)
    edges = edges_for_revision(db_session, revision_id)

    imports_edge = next(
        e
        for e in edges
        if e.relationship_type == "imports"
        and e.source_symbol_id == by_qname["main"].id
        and e.target_symbol_id == by_qname["app.payments"].id
    )
    assert imports_edge.source_symbol_id == by_qname["main"].id  # importer
    assert imports_edge.target_symbol_id == by_qname["app.payments"].id  # imported

    calls_edge = next(
        e
        for e in edges
        if e.relationship_type == "calls"
        and e.source_symbol_id == by_qname["app.payments.Payment.process"].id
    )
    assert calls_edge.source_symbol_id == by_qname["app.payments.Payment.process"].id  # caller
    assert calls_edge.target_symbol_id == by_qname["app.payments.Payment.validate"].id  # callee

    inherits_edge = next(e for e in edges if e.relationship_type == "inherits")
    assert inherits_edge.source_symbol_id == by_qname["app.payments.PremiumPayment"].id  # subtype
    assert inherits_edge.target_symbol_id == by_qname["app.payments.Payment"].id  # supertype


# ---- Task 15: idempotency ---------------------------------------------------


def test_repeated_graph_construction_is_idempotent(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    ingestion_result = ingest(db_session, graph_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    run_parsing(db_session, repository_id, revision_id)

    first = run_graph_construction(db_session, repository_id, revision_id)
    edge_count_after_first = db_session.scalar(
        select(func.count()).select_from(GraphEdge).where(GraphEdge.revision_id == revision_id)
    )

    second = run_graph_construction(db_session, repository_id, revision_id)
    edge_count_after_second = db_session.scalar(
        select(func.count()).select_from(GraphEdge).where(GraphEdge.revision_id == revision_id)
    )

    assert first.import_edges == second.import_edges
    assert first.call_edges == second.call_edges
    assert first.inherits_edges == second.inherits_edges
    assert edge_count_after_first == edge_count_after_second

    runs = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "graph_build")
        )
        .scalars()
        .all()
    )
    assert len(runs) == 2  # history preserved, one row per attempt
    assert all(r.status == "completed" for r in runs)


# ---- Task 24: transactional failure -----------------------------------------


def test_graph_build_failure_rolls_back_and_preserves_prior_state(
    db_session: Session, graph_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingestion_result = ingest(db_session, graph_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    run_parsing(db_session, repository_id, revision_id)

    run_graph_construction(db_session, repository_id, revision_id)
    edge_count_before_failure = db_session.scalar(
        select(func.count()).select_from(GraphEdge).where(GraphEdge.revision_id == revision_id)
    )
    assert edge_count_before_failure > 0

    import app.graph.service as graph_service

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated extraction failure")

    monkeypatch.setattr(graph_service, "extract_graph_candidates", _boom)

    with pytest.raises(GraphConstructionError):
        run_graph_construction(db_session, repository_id, revision_id)

    edge_count_after_failure = db_session.scalar(
        select(func.count()).select_from(GraphEdge).where(GraphEdge.revision_id == revision_id)
    )
    assert (
        edge_count_after_failure == edge_count_before_failure
    )  # the rolled-back DELETE didn't stick

    revision = db_session.get(RepositoryRevision, revision_id)
    assert revision is not None
    assert revision.latest_analysis_status == "failed"

    failed_runs = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "graph_build")
            .where(AnalysisRun.status == "failed")
        )
        .scalars()
        .all()
    )
    assert len(failed_runs) == 1
    assert failed_runs[0].error_message is not None
