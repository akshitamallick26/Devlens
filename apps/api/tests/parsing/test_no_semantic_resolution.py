"""Explicitly proves this phase does NOT build a call graph, does not
resolve imports to concrete symbols, and does not follow inheritance across
files. This distinction matters because a future impact-analysis feature's
correctness depends entirely on the graph never claiming more than it can
prove — see docs/architecture/source-intelligence.md section 18.
"""

from pathlib import Path

from app.parsing.parser import parse_source_file
from app.parsing.result import ParsedSymbol, ParseResult


def test_a_call_expression_produces_no_symbol_and_no_relationship() -> None:
    """`charge()` is a call to a name that happens to match a function
    defined in the same file. The parser must record the `charge` function
    definition as a symbol — and nothing whatsoever about the call site: no
    second symbol for the call, no edge, no field linking the two."""
    source = "def charge():\n    pass\n\ncharge()\n"
    result = parse_source_file(source, "mod.py", "python")

    assert result.status == "parsed"
    kinds = [s.kind for s in result.symbols]
    assert kinds.count("function") == 1  # the definition only
    assert not any(s.name == "charge()" for s in result.symbols)

    # Nothing in ParsedSymbol can even express "this calls that" — there is
    # no such field. The type itself doesn't support recording resolution.
    assert not hasattr(ParsedSymbol, "calls")
    assert not hasattr(ParsedSymbol, "resolved_target")
    assert not hasattr(ParsedSymbol, "target_symbol_id")


def test_a_call_to_an_undefined_name_does_not_raise_or_fabricate_anything() -> None:
    """Calling something that doesn't exist anywhere in this file (or
    anywhere DevLens has looked) must parse identically to calling something
    that does — the parser has no way to know the difference, and must not
    pretend otherwise."""
    result = parse_source_file("totally_undefined_name()\n", "mod.py", "python")
    assert result.status == "parsed"
    assert [s.kind for s in result.symbols] == ["module"]  # nothing fabricated


def test_identically_named_functions_in_different_scopes_are_not_conflated() -> None:
    """Two functions named `charge` in unrelated scopes must never be
    treated as "the same symbol" just because the names match — that would
    be exactly the false-positive resolution this phase must not produce."""
    source = (
        "class A:\n"
        "    def charge(self):\n"
        "        pass\n\n"
        "class B:\n"
        "    def charge(self):\n"
        "        pass\n"
    )
    result = parse_source_file(source, "mod.py", "python")
    charges = [s for s in result.symbols if s.name == "charge"]
    assert len(charges) == 2
    assert {c.qualified_name for c in charges} == {"mod.A.charge", "mod.B.charge"}
    assert charges[0].qualified_name != charges[1].qualified_name


def test_import_is_recorded_as_structure_only_not_as_a_resolved_reference() -> None:
    """`import payments` must be recorded as the literal string 'payments',
    never silently matched up against any Symbol that happens to exist with
    that name — resolving an import to a concrete file/symbol is graph-phase
    work, explicitly out of scope here."""
    result = parse_source_file("import payments\n", "mod.py", "python")
    imp = result.imports[0]
    assert imp.module == "payments"
    # ParsedImport has no field that could hold a resolved target — the type
    # itself makes the "not resolved" claim structurally, not just by convention.
    assert not hasattr(imp, "resolved_file_id")
    assert not hasattr(imp, "target_symbol_id")


def test_class_inheritance_is_textual_only_not_a_resolved_relationship() -> None:
    """`class Admin(User)` records the base class name "User" as plain text
    inside `signature` — it is never matched against any `User` class
    elsewhere, in this file or any other."""
    source = "class User:\n    pass\n\nclass Admin(User):\n    pass\n"
    result = parse_source_file(source, "mod.py", "python")
    admin = next(s for s in result.symbols if s.name == "Admin")
    assert admin.signature == "class Admin(User)"
    # No field anywhere records "Admin's base resolves to symbol X".
    assert not hasattr(admin, "base_symbol_id")
    assert not hasattr(admin, "resolved_bases")


def test_no_graph_edge_producing_api_is_reachable_from_this_phase() -> None:
    """Structural guard: nothing in the parsing package actually imports the
    GraphEdge model (as opposed to merely mentioning the word in a docstring
    explaining its absence, which several modules here do deliberately)."""
    import ast

    import app.parsing.parser
    import app.parsing.python_parser
    import app.parsing.result
    import app.parsing.service

    for module in (
        app.parsing.result,
        app.parsing.python_parser,
        app.parsing.parser,
        app.parsing.service,
    ):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        imported_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "GraphEdge" not in imported_names


def test_parse_result_has_no_graph_shaped_fields() -> None:
    result: ParseResult = parse_source_file("def f(): pass\n", "mod.py", "python")
    for attr in ("edges", "graph", "calls", "call_graph", "resolved_imports"):
        assert not hasattr(result, attr)
