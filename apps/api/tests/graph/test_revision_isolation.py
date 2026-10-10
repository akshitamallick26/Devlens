"""Task 16's critical revision-isolation test: two unrelated repositories
each define the identical qualified name `mod.helper`. Graph construction
for one revision must never reference the other revision's Symbol rows.
Requires live PostgreSQL — same `db_session` fixture and explicit-skip
behavior as tests/db/, tests/ingestion/, and tests/parsing/. Never
substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from tests.graph.conftest import (
    edges_for_revision,
    ingest_parse_and_build_graph,
    symbols_by_qualified_name,
)


def test_revision_isolation_with_colliding_symbol_names(
    db_session: Session, colliding_name_repo_a: Path, colliding_name_repo_b: Path
) -> None:
    """Two unrelated revisions each define `mod.helper` and `mod.caller`
    (identical qualified names). Graph construction for revision A must
    never reference revision B's Symbol rows, or vice versa."""
    result_a, _ = ingest_parse_and_build_graph(db_session, colliding_name_repo_a, owner="owner-a")
    result_b, _ = ingest_parse_and_build_graph(db_session, colliding_name_repo_b, owner="owner-b")

    revision_a_id = uuid.UUID(result_a.revision_id)
    revision_b_id = uuid.UUID(result_b.revision_id)

    symbols_a = symbols_by_qualified_name(db_session, revision_a_id)
    symbols_b = symbols_by_qualified_name(db_session, revision_b_id)
    assert symbols_a["mod.helper"].id != symbols_b["mod.helper"].id  # same name, distinct rows

    edges_a = edges_for_revision(db_session, revision_a_id)
    edges_b = edges_for_revision(db_session, revision_b_id)
    assert len(edges_a) >= 1
    assert len(edges_b) >= 1

    symbol_ids_b = {s.id for s in symbols_b.values()}
    symbol_ids_a = {s.id for s in symbols_a.values()}
    for edge in edges_a:
        assert edge.revision_id == revision_a_id
        if edge.target_symbol_id is not None:
            assert edge.target_symbol_id not in symbol_ids_b
            assert edge.target_symbol_id in symbol_ids_a
    for edge in edges_b:
        assert edge.revision_id == revision_b_id
        if edge.target_symbol_id is not None:
            assert edge.target_symbol_id not in symbol_ids_a
            assert edge.target_symbol_id in symbol_ids_b

    # The one `calls` edge in each revision resolves to that revision's own
    # "mod.helper", not the other repository's.
    call_a = next(e for e in edges_a if e.relationship_type == "calls")
    call_b = next(e for e in edges_b if e.relationship_type == "calls")
    assert call_a.target_symbol_id == symbols_a["mod.helper"].id
    assert call_b.target_symbol_id == symbols_b["mod.helper"].id
