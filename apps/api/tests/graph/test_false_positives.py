"""Task 18's explicit false-positive scenarios: full pipeline (ingestion ->
parsing -> graph construction) against `graph_fixture_repo`'s
false_positives.py, asserting each named scenario produces no `calls` edge
at all rather than a wrong one. Requires live PostgreSQL — same `db_session`
fixture and explicit-skip behavior as tests/db/, tests/ingestion/, and
tests/parsing/. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from tests.graph.conftest import (
    edges_for_revision,
    ingest_parse_and_build_graph,
    symbols_by_qualified_name,
)


def test_false_positive_scenarios_produce_no_wrong_edges(
    db_session: Session, graph_fixture_repo: Path
) -> None:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, graph_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    by_qname = symbols_by_qualified_name(db_session, revision_id)
    edges = edges_for_revision(db_session, revision_id)
    calls_edges = [e for e in edges if e.relationship_type == "calls"]

    false_positive_scopes = [
        "false_positives.checkout",  # local variable shadows an imported name
        "false_positives.unknown_receiver",  # receiver type unknown, no inference
        "false_positives.unrelated_textual_match",  # same name elsewhere, never imported here
        "false_positives.instance_without_type_inference",  # no type inference on local variables
        "false_positives.Unrelated.run",  # self.process() — Unrelated never defines process
        "false_positives.three_segment_chain_is_not_guessed",  # chains >2 segments unsupported
    ]
    for scope in false_positive_scopes:
        source_symbol = by_qname[scope]
        offending = [e for e in calls_edges if e.source_symbol_id == source_symbol.id]
        assert offending == [], f"expected no calls edges from {scope}, found {offending}"

    # The self-method guard specifically: Unrelated.run must not have been
    # accidentally linked to Payment.process just because both are named
    # "process" somewhere in the revision.
    assert not any(
        e.target_symbol_id == by_qname["app.payments.Payment.process"].id for e in calls_edges
    )
