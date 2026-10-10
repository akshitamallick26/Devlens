"""Pure, revision-scoped resolution logic — no database session, no I/O.
`RevisionTables` is built once per revision by the caller (app/graph/service.py)
from already-persisted `Symbol`/`SourceFile` rows; everything in this module
operates only on that in-memory snapshot. See
docs/architecture/graph-construction.md sections 8a, 9, and 13.
"""

from collections import defaultdict
from dataclasses import dataclass

from app.models import Symbol
from app.parsing.python_parser import module_dotted_name
from app.parsing.result import ParsedImport


def _is_package_path(path: str) -> bool:
    return path == "__init__.py" or path.endswith("/__init__.py")


@dataclass(frozen=True)
class RevisionTables:
    """Built fresh for exactly one revision — see graph-construction.md
    section 9. Holds no state across revisions and is never cached between
    `run_graph_construction()` calls."""

    symbol_by_qualified_name: dict[str, Symbol]


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving any single reference (an import, a call
    receiver, an inheritance base). One shared shape for all three, since
    the possible outcomes are identical: a specific internal symbol, a
    proven-external name, or nothing provable."""

    kind: str  # "internal" | "external" | "unresolved"
    symbol: Symbol | None = None
    external_ref: str | None = None


_UNRESOLVED = Resolution(kind="unresolved")


def build_revision_tables(symbols: list[Symbol]) -> RevisionTables:
    """Groups by key first and keeps only unambiguous entries — a qualified
    name (or module dotted name) that maps to more than one Symbol in this
    revision is dropped from the table entirely rather than arbitrarily
    picking one, per graph-construction.md section 9 / Task 17."""
    candidates: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in symbols:
        candidates[symbol.qualified_name].append(symbol)

    symbol_by_qualified_name = {
        qualified_name: matches[0]
        for qualified_name, matches in candidates.items()
        if len(matches) == 1
    }
    return RevisionTables(symbol_by_qualified_name=symbol_by_qualified_name)


def _resolve_module(dotted_name: str, tables: RevisionTables) -> Resolution:
    symbol = tables.symbol_by_qualified_name.get(dotted_name)
    if symbol is not None and symbol.kind == "module":
        return Resolution(kind="internal", symbol=symbol)
    return Resolution(kind="external", external_ref=dotted_name)


def _resolve_from_import(module_name: str | None, name: str, tables: RevisionTables) -> Resolution:
    if module_name is None:
        return _UNRESOLVED
    # Check the specific target FIRST, before gatekeeping on the base module's
    # own symbol. This matters for namespace packages (no __init__.py): a
    # package directory can have no module Symbol of its own while a file
    # directly inside it (e.g. "app/sibling.py") still does. Requiring the
    # base's own module symbol here would wrongly call that case "external".
    candidate = tables.symbol_by_qualified_name.get(f"{module_name}.{name}")
    if candidate is not None:
        return Resolution(kind="internal", symbol=candidate)

    module_symbol = tables.symbol_by_qualified_name.get(module_name)
    if module_symbol is None or module_symbol.kind != "module":
        # The base module itself isn't known to be internal either -> the
        # whole reference is external.
        return Resolution(kind="external", external_ref=f"{module_name}.{name}")
    # The module is internal but doesn't define `name` directly (a re-export,
    # a dynamically-created attribute, ...) — proven NOT external, but not
    # provably anything specific either. Unresolved, not a guess.
    return _UNRESOLVED


def _compute_relative_base(
    importer_module: str, importer_is_package: bool, level: int
) -> list[str] | None:
    parts = importer_module.split(".") if importer_module else []
    anchor = parts if importer_is_package else parts[:-1]
    cut = len(anchor) - (level - 1)
    if cut < 0:
        return None
    return anchor[:cut]


@dataclass(frozen=True)
class ResolvedImport:
    """One `ParsedImport`, resolved. `resolution` is the specific import
    target and drives the `imports` GraphEdge (most specific proven target —
    Task 5). `binding_resolution` is what `bound_name` itself refers to in
    the importer's namespace and drives the call/inheritance fallback table.

    These two differ for exactly one case: a plain, unaliased dotted import
    (`import a.b.c`) binds the name "a", not "a.b.c" — Python's import
    machinery binds the top-level package, with `.b.c` reachable only as
    attributes on it. Reusing the "a.b.c" resolution for later `a.foo()`
    lookups would search "a.b.c.foo" instead of "a.foo", and could even
    resolve to a same-named but unrelated symbol there — a false positive.
    Every other import form (aliased, `from`, relative) binds a name that
    already corresponds 1:1 with its resolved target, so the two fields
    coincide there."""

    bound_name: str
    resolution: Resolution
    line: int
    binding_resolution: Resolution


def resolve_imports_for_file(
    imports: list[ParsedImport], importer_path: str, tables: RevisionTables
) -> list[ResolvedImport]:
    importer_module = module_dotted_name(importer_path)
    importer_is_package = _is_package_path(importer_path)
    resolved: list[ResolvedImport] = []

    for imp in imports:
        if imp.is_wildcard:
            continue

        if imp.level == 0:
            if imp.imported_name is None:
                if imp.module is None:
                    continue
                edge_resolution = _resolve_module(imp.module, tables)
                if imp.alias is not None:
                    # `import a.b.c as x` binds x directly to a.b.c itself.
                    bound_name = imp.alias
                    binding_resolution = edge_resolution
                else:
                    # `import a.b.c` binds "a" (the top-level package), not
                    # "a.b.c" — re-resolve the binding separately.
                    bound_name = imp.module.split(".")[0]
                    binding_resolution = (
                        edge_resolution
                        if "." not in imp.module
                        else _resolve_module(bound_name, tables)
                    )
                resolved.append(
                    ResolvedImport(bound_name, edge_resolution, imp.line, binding_resolution)
                )
            else:
                bound_name = imp.alias or imp.imported_name
                resolution = _resolve_from_import(imp.module, imp.imported_name, tables)
                resolved.append(ResolvedImport(bound_name, resolution, imp.line, resolution))
            continue

        # Relative import (always a `from` form per Python's grammar).
        if imp.imported_name is None:
            continue  # defensive — the parser never produces this for level > 0
        bound_name = imp.alias or imp.imported_name
        base_parts = _compute_relative_base(importer_module, importer_is_package, imp.level)
        if base_parts is None:
            resolved.append(ResolvedImport(bound_name, _UNRESOLVED, imp.line, _UNRESOLVED))
            continue
        if imp.module is not None:
            base_parts = [*base_parts, *imp.module.split(".")]
        absolute_base = ".".join(base_parts) if base_parts else None
        resolution = _resolve_from_import(absolute_base, imp.imported_name, tables)
        resolved.append(ResolvedImport(bound_name, resolution, imp.line, resolution))

    return resolved


def import_binding_table(resolved_imports: list[ResolvedImport]) -> dict[str, Resolution]:
    """The file-wide bound-name -> Resolution lookup used as the final
    fallback by `resolve_name_chain` (see graph-construction.md section 13).
    If the same name is bound more than once in a file (unusual, but legal —
    e.g. a conditional import), the last one wins, matching Python's own
    runtime behavior for repeated bindings."""
    return {ri.bound_name: ri.binding_resolution for ri in resolved_imports}


def _enclosing_scopes(scope_qualified_name: str, module_name: str) -> list[str]:
    """[scope_qualified_name, its parent, ..., module_name] — stops exactly
    at the file's own module boundary, since Python modules don't lexically
    nest inside their package for name-resolution purposes."""
    scopes = [scope_qualified_name]
    current = scope_qualified_name
    while current != module_name and "." in current:
        current = current.rsplit(".", 1)[0]
        scopes.append(current)
    return scopes


def resolve_name_chain(
    name_chain: tuple[str, ...],
    scope_qualified_name: str,
    module_name: str,
    tables: RevisionTables,
    import_bindings: dict[str, Resolution],
    locally_bound_names: dict[str, frozenset[str]],
    self_parameter_name: str | None,
) -> Resolution:
    """Resolves a single name-chain reference (a call's receiver, or an
    inheritance base). Caller-specific acceptance (must the result be a
    class? a function/method?) is applied by the caller, not here — this
    function only proves *what a name refers to*, not whether that's a
    sensible target for the relationship being built.
    """
    if len(name_chain) == 1:
        name = name_chain[0]
        for scope in _enclosing_scopes(scope_qualified_name, module_name):
            symbol = tables.symbol_by_qualified_name.get(f"{scope}.{name}")
            locally_bound = name in locally_bound_names.get(scope, frozenset())
            if symbol is not None and locally_bound:
                # A nested def/class AND a plain rebinding share this name in
                # the identical scope (e.g. `def charge(): ...` followed by
                # `charge = get_handler()`). Which one a call actually hits
                # depends on their relative statement order, which this
                # resolver does not track — ambiguous, not a guess.
                return _UNRESOLVED
            if symbol is not None:
                return Resolution(kind="internal", symbol=symbol)
            if locally_bound:
                # Shadowed by a plain variable/parameter at this scope level
                # — Python would use that binding, not any outer-scope
                # function of the same name. Stop here; do not keep
                # searching further out.
                return _UNRESOLVED
        return import_bindings.get(name, _UNRESOLVED)

    if len(name_chain) == 2:
        first, second = name_chain
        if self_parameter_name is not None and first == self_parameter_name:
            # Only a REAL method's own first parameter counts as a self/cls
            # receiver — not just "whatever this function's first positional
            # parameter happens to be named". A plain function's or nested
            # function's first parameter is an arbitrary, unproven name;
            # treating it as self would resolve `x.foo()` against the
            # enclosing scope's own namespace purely because `x` is first in
            # the parameter list, with no basis for that at all.
            enclosing_class = (
                scope_qualified_name.rsplit(".", 1)[0] if "." in scope_qualified_name else None
            )
            scope_symbol = tables.symbol_by_qualified_name.get(scope_qualified_name)
            class_symbol = (
                tables.symbol_by_qualified_name.get(enclosing_class) if enclosing_class else None
            )
            if (
                scope_symbol is not None
                and scope_symbol.kind == "method"
                and class_symbol is not None
                and class_symbol.kind == "class"
            ):
                # "self.X" — resolved only against the enclosing class's OWN
                # directly-defined members, never a base class (no MRO
                # computation — graph-construction.md section 11).
                symbol = tables.symbol_by_qualified_name.get(f"{enclosing_class}.{second}")
                return (
                    Resolution(kind="internal", symbol=symbol)
                    if symbol is not None
                    else _UNRESOLVED
                )
            # Not actually a method — fall through to ordinary attribute
            # resolution below, where being a parameter at all makes it a
            # local shadow (see the loop immediately below).

        for scope in _enclosing_scopes(scope_qualified_name, module_name):
            if first in locally_bound_names.get(scope, frozenset()):
                # "first" is a local variable or parameter somewhere in the
                # enclosing lexical scope chain — it shadows any file-level
                # import of the same name, so it is never resolved from
                # import bindings (graph-construction.md section 8 / Task 8).
                return _UNRESOLVED

        base = import_bindings.get(first)
        if base is None or base.kind == "unresolved":
            # The receiver isn't a name this file's imports prove anything
            # about — never resolved from general scope presence alone.
            return _UNRESOLVED
        if base.kind == "external":
            return Resolution(kind="external", external_ref=f"{base.external_ref}.{second}")
        # base.kind == "internal": only a module or a class is a sensible
        # namespace to look a further attribute up inside.
        assert base.symbol is not None
        if base.symbol.kind not in ("module", "class"):
            return _UNRESOLVED
        symbol = tables.symbol_by_qualified_name.get(f"{base.symbol.qualified_name}.{second}")
        return Resolution(kind="internal", symbol=symbol) if symbol is not None else _UNRESOLVED

    return _UNRESOLVED  # chains longer than two segments are not supported
