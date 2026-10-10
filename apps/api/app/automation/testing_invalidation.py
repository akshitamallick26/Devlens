"""Generated-test invalidation decision (Task 10). Read-only: identifies
which `GeneratedTest` rows belong to a now-stale target and which current
symbols would be reasonable regeneration candidates. Never migrates a
`GeneratedTest` row's `revision_id` to the current revision, and never
triggers regeneration itself — that remains an explicit, separate
operation (app/testing/service.py::generate_tests_for_target), per this
phase's "automation only identifies what requires rerunning" constraint.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.models import SymbolDelta, TestingInvalidationPlan
from app.models import GeneratedTest


def compute_testing_invalidation(
    db: Session,
    repository_id: uuid.UUID,
    parent_revision_id: uuid.UUID | None,
    symbol_delta: SymbolDelta,
) -> TestingInvalidationPlan:
    changed_target_symbol_ids = tuple(
        sorted(
            {s.symbol_id for s in symbol_delta.added}
            | {cur.symbol_id for _, cur in symbol_delta.modified}
        )
    )

    if parent_revision_id is None:
        return TestingInvalidationPlan(
            stale_generated_test_ids=(),
            changed_target_symbol_ids=changed_target_symbol_ids,
            detail="no parent revision — there are no prior GeneratedTest rows to invalidate",
        )

    # A GeneratedTest from the parent revision is stale exactly when its
    # target symbol was deleted or materially changed — never when the
    # target is merely unchanged or when the test itself looks "old" by
    # some other heuristic.
    stale_parent_symbol_ids = {s.symbol_id for s in symbol_delta.deleted} | {
        parent.symbol_id for parent, _ in symbol_delta.modified
    }
    stale_ids: tuple[uuid.UUID, ...] = ()
    if stale_parent_symbol_ids:
        rows = db.execute(
            select(GeneratedTest.id)
            .where(GeneratedTest.repository_id == repository_id)
            .where(GeneratedTest.revision_id == parent_revision_id)
            .where(GeneratedTest.target_symbol_id.in_(stale_parent_symbol_ids))
        ).all()
        stale_ids = tuple(sorted(row[0] for row in rows))

    return TestingInvalidationPlan(
        stale_generated_test_ids=stale_ids,
        changed_target_symbol_ids=changed_target_symbol_ids,
        detail=None,
    )
