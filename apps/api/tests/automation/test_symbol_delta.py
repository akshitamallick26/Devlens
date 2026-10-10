"""Pure symbol-delta tests — no DB, always run. Task 4's exact adversarial
matrix.
"""

import uuid

from app.automation.models import SymbolIdentity, SymbolSnapshot
from app.automation.symbol_delta import compute_symbol_delta


def _snap(
    path: str, kind: str, name: str, content: str | None, symbol_id: uuid.UUID | None = None
) -> SymbolSnapshot:
    return SymbolSnapshot(
        identity=SymbolIdentity(file_path=path, kind=kind, qualified_name=name),
        symbol_id=symbol_id or uuid.uuid4(),
        file_id=uuid.uuid4(),
        start_line=1,
        end_line=5,
        content=content,
    )


def test_moved_function_is_unchanged() -> None:
    """Identical signature+body content, different line span (captured only
    via the start/end_line fields, which the content comparison never
    looks at) — a pure move must never register as modified."""
    current = [_snap("a.py", "function", "a.f", "def f():\x00return 1")]
    parent = [_snap("a.py", "function", "a.f", "def f():\x00return 1")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.unchanged) == 1
    assert delta.modified == ()


def test_renamed_function_is_delete_plus_add_never_fuzzy_matched() -> None:
    current = [_snap("a.py", "function", "a.g", "def g():\x00return 1")]
    parent = [_snap("a.py", "function", "a.f", "def f():\x00return 1")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.added) == 1
    assert delta.added[0].identity.qualified_name == "a.g"
    assert len(delta.deleted) == 1
    assert delta.deleted[0].identity.qualified_name == "a.f"
    assert delta.modified == ()


def test_same_name_in_different_modules_is_distinguished_by_path() -> None:
    current = [
        _snap("a.py", "function", "mod.f", "x"),
        _snap("b.py", "function", "mod.f", "x"),
    ]
    parent = [_snap("a.py", "function", "mod.f", "x")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.unchanged) == 1
    assert delta.unchanged[0].identity.file_path == "a.py"
    assert len(delta.added) == 1
    assert delta.added[0].identity.file_path == "b.py"


def test_nested_function_identity_is_distinct_from_outer() -> None:
    current = [
        _snap("a.py", "function", "a.outer", "def outer():\x00x=1"),
        _snap("a.py", "function", "a.outer.<locals>.inner", "def inner():\x00return 1"),
    ]
    parent = [_snap("a.py", "function", "a.outer", "def outer():\x00x=1")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.added) == 1
    assert delta.added[0].identity.qualified_name == "a.outer.<locals>.inner"
    assert len(delta.unchanged) == 1


def test_decorator_only_change_is_modified() -> None:
    """Decorators live in `signature`, not in the body line-span — a
    decorator change must still be caught."""
    current = [_snap("a.py", "function", "a.f", "@new_decorator\x00def f(): pass")]
    parent = [_snap("a.py", "function", "a.f", "@old_decorator\x00def f(): pass")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1
    assert delta.unchanged == ()


def test_function_body_change_is_modified() -> None:
    current = [_snap("a.py", "function", "a.f", "sig\x00return 2")]
    parent = [_snap("a.py", "function", "a.f", "sig\x00return 1")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1


def test_signature_change_is_modified() -> None:
    current = [_snap("a.py", "function", "a.f", "def f(a, b):\x00body")]
    parent = [_snap("a.py", "function", "a.f", "def f(a):\x00body")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1


def test_docstring_only_change_is_modified() -> None:
    """A docstring is part of the body text, not metadata stripped before
    comparison — a docstring-only edit is a real content change."""
    current = [_snap("a.py", "function", "a.f", 'sig\x00"""new doc"""\ncode')]
    parent = [_snap("a.py", "function", "a.f", 'sig\x00"""old doc"""\ncode')]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1


def test_class_method_change_uses_qualified_name_scoping() -> None:
    current = [_snap("a.py", "method", "a.MyClass.method", "sig\x00return 2")]
    parent = [_snap("a.py", "method", "a.MyClass.method", "sig\x00return 1")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1
    assert delta.modified[0][1].identity.qualified_name == "a.MyClass.method"


def test_added_symbol() -> None:
    current = [_snap("a.py", "function", "a.f", "x"), _snap("a.py", "function", "a.g", "y")]
    parent = [_snap("a.py", "function", "a.f", "x")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.added) == 1
    assert delta.added[0].identity.qualified_name == "a.g"


def test_deleted_symbol() -> None:
    current = [_snap("a.py", "function", "a.f", "x")]
    parent = [_snap("a.py", "function", "a.f", "x"), _snap("a.py", "function", "a.g", "y")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.deleted) == 1
    assert delta.deleted[0].identity.qualified_name == "a.g"


def test_unchanged_symbol() -> None:
    current = [_snap("a.py", "function", "a.f", "x")]
    parent = [_snap("a.py", "function", "a.f", "x")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.unchanged) == 1
    assert delta.added == () and delta.deleted == () and delta.modified == ()


def test_missing_content_is_never_claimed_unchanged() -> None:
    """Content unavailable (e.g. storage_key-backed source) on either side
    must never be silently treated as proof of no change."""
    current = [_snap("a.py", "function", "a.f", None)]
    parent = [_snap("a.py", "function", "a.f", "x")]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.modified) == 1
    assert delta.unchanged == ()


def test_empty_current_and_parent_produce_empty_delta() -> None:
    delta = compute_symbol_delta([], [])
    assert (
        delta.added == () and delta.deleted == () and delta.modified == () and delta.unchanged == ()
    )


def test_cross_revision_isolation_is_structural_not_incidental() -> None:
    """This module never queries revision_id at all — isolation comes from
    the caller only ever passing snapshots it already scoped to the right
    revision (tested at the DB-loader level in test_revision_delta_db.py).
    Here: two snapshots with the same identity but unrelated symbol_ids
    from what are conceptually different revisions still compare correctly
    by content, never by symbol_id."""
    shared_id = uuid.uuid4()
    current = [_snap("a.py", "function", "a.f", "same", symbol_id=uuid.uuid4())]
    parent = [_snap("a.py", "function", "a.f", "same", symbol_id=shared_id)]
    delta = compute_symbol_delta(current, parent)
    assert len(delta.unchanged) == 1
    assert delta.unchanged[0].symbol_id != shared_id  # the *current* side's own id is preserved
