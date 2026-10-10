import uuid

from app.graph.python_extractor import extract_graph_candidates
from app.graph.resolver import (
    build_revision_tables,
    import_binding_table,
    resolve_imports_for_file,
    resolve_name_chain,
)
from app.models import Symbol
from app.parsing.python_parser import parse_python_source
from app.parsing.result import ParsedImport


def make_symbol(**kwargs: object) -> Symbol:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        start_line=1,
        end_line=1,
        analyzer="test",
    )
    defaults.update(kwargs)
    return Symbol(**defaults)


def symbols_from_source(source: str, path: str) -> list[Symbol]:
    parsed = parse_python_source(source, path)
    return [
        make_symbol(kind=s.kind, name=s.name, qualified_name=s.qualified_name)
        for s in parsed.symbols
    ]


def resolve_call(source: str, module_name: str, tables, import_bindings=None):
    extraction = extract_graph_candidates(source, module_name)
    import_bindings = import_bindings or {}
    results = []
    for call in extraction.calls:
        res = resolve_name_chain(
            call.name_chain,
            call.scope_qualified_name,
            module_name,
            tables,
            import_bindings,
            extraction.locally_bound_names,
            extraction.first_parameter_by_scope.get(call.scope_qualified_name),
        )
        results.append((call.scope_qualified_name, call.name_chain, res))
    return results


# ---- import resolution ----------------------------------------------------


def test_plain_import_of_internal_module() -> None:
    symbols = symbols_from_source("def f():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name=None,
                alias=None,
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].bound_name == "app"  # `import app.payments` binds "app", not "app.payments"
    assert resolved[0].resolution.kind == "internal"


def test_plain_import_with_alias_binds_the_alias() -> None:
    symbols = symbols_from_source("def f():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name=None,
                alias="p",
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].bound_name == "p"
    assert resolved[0].resolution.kind == "internal"
    assert resolved[0].resolution.symbol.qualified_name == "app.payments"


def test_plain_dotted_import_binds_the_top_level_package_not_the_submodule() -> None:
    """`import app.payments` binds the name "app" (the top-level package),
    not "app.payments" — Python's import machinery binds the top-level
    package, with .payments reachable only as an attribute on it. A later
    bare `app.foo()` must resolve against "app", never be misdirected into
    "app.payments" just because that was the more specific import target."""
    symbols = symbols_from_source("def foo():\n    pass\n", "app/__init__.py")
    symbols += symbols_from_source("def foo():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name=None,
                alias=None,
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    # The edge target is still the most specific proven target (Task 5).
    assert resolved[0].resolution.symbol.qualified_name == "app.payments"
    # But the *binding* used for subsequent attribute lookups is "app" itself.
    assert resolved[0].binding_resolution.symbol.qualified_name == "app"

    source = "def checkout():\n    app.foo()\n"
    results = resolve_call(
        source, "checkout_mod", tables, import_bindings=import_binding_table(resolved)
    )
    _, _, res = results[0]
    assert res.kind == "internal"
    assert res.symbol.qualified_name == "app.foo"  # never "app.payments.foo"


def test_import_of_external_package() -> None:
    tables = build_revision_tables([])
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="numpy", imported_name=None, alias="np", level=0, is_wildcard=False, line=1
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].resolution.kind == "external"
    assert resolved[0].resolution.external_ref == "numpy"


def test_from_import_of_internal_symbol() -> None:
    symbols = symbols_from_source("def charge():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name="charge",
                alias=None,
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].resolution.kind == "internal"
    assert resolved[0].resolution.symbol.qualified_name == "app.payments.charge"


def test_from_import_with_alias() -> None:
    symbols = symbols_from_source("def charge():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name="charge",
                alias="process_charge",
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].bound_name == "process_charge"
    assert resolved[0].resolution.symbol.qualified_name == "app.payments.charge"


def test_from_import_of_external_module() -> None:
    tables = build_revision_tables([])
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="os", imported_name="getcwd", alias=None, level=0, is_wildcard=False, line=1
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].resolution.kind == "external"
    assert resolved[0].resolution.external_ref == "os.getcwd"


def test_from_import_of_name_not_found_in_internal_module_is_unresolved() -> None:
    symbols = symbols_from_source("def charge():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name="nonexistent",
                alias=None,
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].resolution.kind == "unresolved"


def test_relative_import_from_sibling_module() -> None:
    symbols = symbols_from_source("def charge():\n    pass\n", "app/payments.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="payments",
                imported_name="charge",
                alias=None,
                level=1,
                is_wildcard=False,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved[0].resolution.kind == "internal"
    assert resolved[0].resolution.symbol.qualified_name == "app.payments.charge"


def test_bare_relative_import_from_package_init() -> None:
    """`from . import sibling` inside app/__init__.py."""
    # Both app/sibling.py's module AND app/__init__.py's own module symbol
    # must be present — a real pipeline always has both (parsing creates one
    # module Symbol per successfully-parsed file, including __init__.py).
    symbols = symbols_from_source("def helper():\n    pass\n", "app/sibling.py")
    symbols += symbols_from_source("", "app/__init__.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module=None, imported_name="sibling", alias=None, level=1, is_wildcard=False, line=1
            )
        ],
        "app/__init__.py",
        tables,
    )
    assert resolved[0].resolution.kind == "internal"
    assert resolved[0].resolution.symbol.qualified_name == "app.sibling"


def test_bare_relative_import_resolves_without_a_namespace_packages_own_symbol() -> None:
    """Namespace packages (PEP 420, no __init__.py) have no module Symbol of
    their own. `from . import sibling` must still resolve via the specific
    target "app.sibling", not be gated on "app" itself having a symbol."""
    symbols = symbols_from_source("def helper():\n    pass\n", "app/sibling.py")
    tables = build_revision_tables(symbols)
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module=None, imported_name="sibling", alias=None, level=1, is_wildcard=False, line=1
            )
        ],
        "app/__init__.py",
        tables,
    )
    assert resolved[0].resolution.kind == "internal"
    assert resolved[0].resolution.symbol.qualified_name == "app.sibling"


def test_relative_import_level_from_a_package_itself_vs_a_plain_module() -> None:
    """A package's own __init__.py uses itself as the level-1 anchor; a
    plain module uses its parent as the level-1 anchor — a classic Python
    gotcha this resolver must get right."""
    symbols = symbols_from_source("def h():\n    pass\n", "app/target.py")
    tables = build_revision_tables(symbols)

    # From app/sub/__init__.py (a package): level=2 reaches "app".
    resolved_from_package = resolve_imports_for_file(
        [
            ParsedImport(
                module="target", imported_name="h", alias=None, level=2, is_wildcard=False, line=1
            )
        ],
        "app/sub/__init__.py",
        tables,
    )
    assert resolved_from_package[0].resolution.kind == "internal"

    # From app/sub/mod.py (a plain module): level=2 also reaches "app"
    # (level=1 would be "app.sub", its own parent).
    resolved_from_module = resolve_imports_for_file(
        [
            ParsedImport(
                module="target", imported_name="h", alias=None, level=2, is_wildcard=False, line=1
            )
        ],
        "app/sub/mod.py",
        tables,
    )
    assert resolved_from_module[0].resolution.kind == "internal"


def test_relative_import_exceeding_package_depth_is_unresolved() -> None:
    tables = build_revision_tables([])
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="x", imported_name="y", alias=None, level=5, is_wildcard=False, line=1
            )
        ],
        "app/mod.py",
        tables,
    )
    assert resolved[0].resolution.kind == "unresolved"


def test_wildcard_import_produces_no_binding() -> None:
    tables = build_revision_tables([])
    resolved = resolve_imports_for_file(
        [
            ParsedImport(
                module="app.payments",
                imported_name=None,
                alias=None,
                level=0,
                is_wildcard=True,
                line=1,
            )
        ],
        "app/checkout.py",
        tables,
    )
    assert resolved == []


# ---- call resolution: local scope ------------------------------------------


def test_local_function_call_resolves() -> None:
    source = "def charge():\n    pass\n\ndef checkout():\n    charge()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    scope, chain, res = next(r for r in results if r[1] == ("charge",))
    assert res.kind == "internal"
    assert res.symbol.qualified_name == "mod.charge"


def test_nested_function_call_resolves() -> None:
    source = "def outer():\n    def inner():\n        pass\n    inner()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = results[0]
    assert res.kind == "internal"
    assert res.symbol.qualified_name == "mod.outer.inner"


def test_self_method_call_resolves() -> None:
    source = (
        "class C:\n"
        "    def checkout(self):\n"
        "        self.charge()\n"
        "    def charge(self):\n"
        "        pass\n"
    )
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = results[0]
    assert res.kind == "internal"
    assert res.symbol.qualified_name == "mod.C.charge"


# ---- false positives at the resolver level ---------------------------------


def test_local_variable_shadows_outer_function_name() -> None:
    source = (
        "def charge():\n    pass\n\ndef checkout():\n    charge = get_handler()\n    charge()\n"
    )
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, chain, res = next(r for r in results if r[1] == ("charge",))
    assert res.kind == "unresolved"


def test_nested_function_shadowed_by_same_named_rebinding_in_same_scope() -> None:
    """A nested function and a later plain rebinding share the identical
    name in the identical scope. Which one a call actually hits depends on
    statement order, which this resolver doesn't track — must be
    unresolved, never a guess at the (possibly wrong) nested function."""
    source = (
        "def checkout():\n"
        "    def charge():\n"
        "        pass\n"
        "    charge = get_handler()\n"
        "    charge()\n"
    )
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, chain, res = next(r for r in results if r[1] == ("charge",) and r[0] == "mod.checkout")
    assert res.kind == "unresolved"


def test_same_method_name_on_two_different_classes_resolve_independently() -> None:
    source = (
        "class A:\n"
        "    def checkout(self):\n"
        "        self.charge()\n"
        "    def charge(self):\n"
        "        pass\n\n"
        "class B:\n"
        "    def checkout(self):\n"
        "        self.charge()\n"
        "    def charge(self):\n"
        "        pass\n"
    )
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    a_result = next(r for r in results if r[0] == "mod.A.checkout")
    b_result = next(r for r in results if r[0] == "mod.B.checkout")
    assert a_result[2].symbol.qualified_name == "mod.A.charge"
    assert b_result[2].symbol.qualified_name == "mod.B.charge"
    assert a_result[2].symbol.id != b_result[2].symbol.id


def test_plain_functions_first_parameter_is_never_treated_as_self() -> None:
    """A plain top-level function's first parameter is not `self` just
    because it's first in the parameter list — `service.helper()` must not
    resolve to an unrelated top-level `helper` function merely because
    `helper` happens to be defined in the same module."""
    source = "def helper():\n    pass\n\ndef run(service):\n    service.helper()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = next(r for r in results if r[1] == ("service", "helper"))
    assert res.kind == "unresolved"


def test_parameter_shadows_an_imported_name_in_attribute_call() -> None:
    """A parameter named the same as an imported module shadows the import
    within the function body — `payments.charge()` must not resolve to the
    imported `payments` module's `charge` just because the names match."""
    payments_symbols = symbols_from_source("def charge():\n    pass\n", "payments.py")
    main_source = "import payments\n\ndef run(payments):\n    payments.charge()\n"
    main_symbols = symbols_from_source(main_source, "main.py")
    tables = build_revision_tables(payments_symbols + main_symbols)
    resolved_imports = resolve_imports_for_file(
        [
            ParsedImport(
                module="payments",
                imported_name=None,
                alias=None,
                level=0,
                is_wildcard=False,
                line=1,
            )
        ],
        "main.py",
        tables,
    )
    import_bindings = import_binding_table(resolved_imports)
    results = resolve_call(main_source, "main", tables, import_bindings=import_bindings)
    _, _, res = next(r for r in results if r[1] == ("payments", "charge"))
    assert res.kind == "unresolved"


def test_unknown_attribute_receiver_is_unresolved() -> None:
    source = "def checkout(service):\n    service.charge()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = results[0]
    assert res.kind == "unresolved"


def test_unresolved_import_base_cannot_resolve_an_attribute_call() -> None:
    source = "def checkout():\n    payments.charge()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    # "payments" is not bound by any import at all.
    results = resolve_call(source, "mod", tables, import_bindings={})
    _, _, res = results[0]
    assert res.kind == "unresolved"


def test_function_call_out_of_lexical_scope_does_not_resolve() -> None:
    """Two unrelated top-level functions each define a nested helper with
    the same name; calling it from one must never resolve to the other's."""
    source = (
        "def a():\n    def tool():\n        pass\n    tool()\n\n"
        "def b():\n    def tool():\n        return 1\n"
    )
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = results[0]
    assert res.kind == "internal"
    assert res.symbol.qualified_name == "mod.a.tool"  # never mod.b.tool


def test_chain_longer_than_two_segments_is_unresolved() -> None:
    source = "def f():\n    a.b.c.charge()\n"
    symbols = symbols_from_source(source, "mod.py")
    tables = build_revision_tables(symbols)
    results = resolve_call(source, "mod", tables)
    _, _, res = results[0]
    assert res.kind == "unresolved"


def test_ambiguous_module_name_collision_resolves_to_nothing() -> None:
    """Two distinct files whose module dotted name collides (an
    __init__.py vs. a same-named plain module) must not let either resolve
    — see resolver.build_revision_tables's collision handling."""
    sym_a = make_symbol(kind="module", name="app.b", qualified_name="app.b")
    sym_b = make_symbol(kind="module", name="app.b", qualified_name="app.b")
    tables = build_revision_tables([sym_a, sym_b])
    assert "app.b" not in tables.symbol_by_qualified_name
