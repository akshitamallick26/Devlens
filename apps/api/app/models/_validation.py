"""Shared helper for the application-enforced boundaries documented in
docs/architecture/database-design.md's "Revision-scoped reference
enforcement" section.

This is a best-effort, ORM-level check, not a substitute for a database
constraint: it only fires when SQLAlchemy's `@validates` hook runs (plain
attribute assignment through the ORM), and only when the referenced row is
already visible to the current session. It does **not** fire for bulk
inserts, raw SQL, or when the referenced row hasn't been flushed yet — in
those cases it silently allows the write rather than risk a false positive.
It exists for the handful of relationships where DB-level composite-FK
enforcement was ruled out (see database-design.md for why, per field), not
as a general-purpose validation framework.
"""

from typing import Any

from sqlalchemy.orm import object_session


def validate_same_owner(
    instance: object,
    field_name: str,
    referenced_model: type,
    referenced_id: Any,
    expected_value: Any,
    compare_attr: str,
) -> None:
    if referenced_id is None or expected_value is None:
        return

    session = object_session(instance)
    if session is None:
        return

    referenced = session.get(referenced_model, referenced_id)
    if referenced is not None and getattr(referenced, compare_attr) != expected_value:
        raise ValueError(
            f"{type(instance).__name__}.{field_name} references a "
            f"{referenced_model.__name__} with a different {compare_attr}"
        )
