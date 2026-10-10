"""Deterministic Python structural parsing via the standard library `ast`
module. No LLM, no tree-sitter, no network, no filesystem access, no
database access — pure `str -> ParseResult`. See
docs/architecture/source-intelligence.md for the full design.

Scope, deliberately: structure, not semantics. This module extracts what
modules/classes/functions/methods exist, their nesting, their locations, and
their declared imports. It never claims a call expression resolves to a
particular symbol, never resolves an import to a concrete file, and never
follows inheritance across files — see source-intelligence.md section 18 and
`tests/parsing/test_no_semantic_resolution.py`.
"""

import ast

from app.parsing.result import ParsedImport, ParsedSymbol, ParseResult, ParseSyntaxError

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_RECURSE_INTO_BODY_FIELDS = ("body", "orelse", "finalbody")


def module_dotted_name(repo_relative_path: str) -> str:
    """Derives a Python package/module dotted name from a repository-relative
    path, following standard Python packaging convention: `a/b/c.py` ->
    `a.b.c`; `a/b/__init__.py` -> `a.b` (a package's `__init__.py` represents
    the package itself, not a submodule literally named `__init__`)."""
    path = repo_relative_path
    if path.endswith(".py"):
        path = path[: -len(".py")]
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else repo_relative_path


def _render_arguments(args: ast.arguments) -> str:
    return ast.unparse(args)


def _render_decorators(decorator_list: list[ast.expr]) -> str:
    return "".join(f"@{ast.unparse(d)}\n" for d in decorator_list)


def _render_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    decorators = _render_decorators(node.decorator_list)
    args = _render_arguments(node.args)
    return f"{decorators}{prefix} {node.name}({args}){returns}"


def _render_class_signature(node: ast.ClassDef) -> str:
    parts = [ast.unparse(base) for base in node.bases]
    parts.extend(f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg is not None)
    bases = f"({', '.join(parts)})" if parts else ""
    return f"{_render_decorators(node.decorator_list)}class {node.name}{bases}"


class _Extractor:
    """Walks the module body once, tracking the enclosing-container stack
    (module/class/function) needed to tell a method from a plain function and
    to build dotted qualified names from actual AST nesting — never from
    names alone. See source-intelligence.md section 7 (qualified names)."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self.symbols: list[ParsedSymbol] = []
        self.imports: list[ParsedImport] = []

    def run(self, tree: ast.Module, module_end_line: int) -> None:
        self.symbols.append(
            ParsedSymbol(
                kind="module",
                name=self._module_name,
                qualified_name=self._module_name,
                start_line=1,
                start_column=0,
                end_line=module_end_line,
                end_column=None,
                signature=None,
                parent_qualified_name=None,
            )
        )
        self._walk(tree.body, qualified_prefix=self._module_name, container_kind="module")

    def _walk(self, stmts: list[ast.stmt], *, qualified_prefix: str, container_kind: str) -> None:
        for stmt in stmts:
            if isinstance(stmt, _FUNCTION_NODES):
                self._extract_function(
                    stmt, qualified_prefix=qualified_prefix, container_kind=container_kind
                )
            elif isinstance(stmt, ast.ClassDef):
                self._extract_class(stmt, qualified_prefix=qualified_prefix)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self._extract_import(stmt)
            else:
                self._recurse_generic(
                    stmt, qualified_prefix=qualified_prefix, container_kind=container_kind
                )

    def _recurse_generic(
        self, stmt: ast.stmt, *, qualified_prefix: str, container_kind: str
    ) -> None:
        """Descends into compound statements (if/for/while/try/with/...)
        that aren't themselves symbols, so a def/class nested inside one is
        still found — without hand-listing every statement type."""
        for field_name in _RECURSE_INTO_BODY_FIELDS:
            nested = getattr(stmt, field_name, None)
            if nested:
                self._walk(nested, qualified_prefix=qualified_prefix, container_kind=container_kind)
        for handler in getattr(stmt, "handlers", []):
            self._walk(
                handler.body, qualified_prefix=qualified_prefix, container_kind=container_kind
            )

    def _extract_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        qualified_prefix: str,
        container_kind: str,
    ) -> None:
        # No separate "async function" kind in the schema's allow-list —
        # async-ness is preserved in `signature` ("async def ...") instead.
        # See source-intelligence.md's schema-observations section.
        kind = "method" if container_kind == "class" else "function"
        qualified_name = f"{qualified_prefix}.{node.name}"
        self.symbols.append(
            ParsedSymbol(
                kind=kind,
                name=node.name,
                qualified_name=qualified_name,
                start_line=node.lineno,
                start_column=node.col_offset,
                end_line=node.end_lineno if node.end_lineno is not None else node.lineno,
                end_column=node.end_col_offset,
                signature=_render_function_signature(node),
                parent_qualified_name=qualified_prefix,
            )
        )
        self._walk(node.body, qualified_prefix=qualified_name, container_kind="function")

    def _extract_class(self, node: ast.ClassDef, *, qualified_prefix: str) -> None:
        qualified_name = f"{qualified_prefix}.{node.name}"
        self.symbols.append(
            ParsedSymbol(
                kind="class",
                name=node.name,
                qualified_name=qualified_name,
                start_line=node.lineno,
                start_column=node.col_offset,
                end_line=node.end_lineno if node.end_lineno is not None else node.lineno,
                end_column=node.end_col_offset,
                signature=_render_class_signature(node),
                parent_qualified_name=qualified_prefix,
            )
        )
        self._walk(node.body, qualified_prefix=qualified_name, container_kind="class")

    def _extract_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.imports.append(
                    ParsedImport(
                        module=alias.name,
                        imported_name=None,
                        alias=alias.asname,
                        level=0,
                        is_wildcard=False,
                        line=node.lineno,
                    )
                )
            return

        for alias in node.names:
            is_wildcard = alias.name == "*"
            self.imports.append(
                ParsedImport(
                    module=node.module,
                    imported_name=None if is_wildcard else alias.name,
                    alias=alias.asname,
                    level=node.level,
                    is_wildcard=is_wildcard,
                    line=node.lineno,
                )
            )


def parse_python_source(content: str, repo_relative_path: str) -> ParseResult:
    """Parses one Python source file's content. Never raises for invalid
    Python — a `SyntaxError` is captured and returned as a structured
    `ParseSyntaxError`, never silently swallowed and never used as an excuse
    to guess at malformed source's structure."""
    try:
        tree = ast.parse(content, filename=repo_relative_path)
        module_end_line = content.count("\n") + 1 if content else 1
        extractor = _Extractor(module_dotted_name(repo_relative_path))
        extractor.run(tree, module_end_line)
    except SyntaxError as exc:
        return ParseResult(
            status="failed",
            language="python",
            syntax_error=ParseSyntaxError(
                message=str(exc.msg),
                line=exc.lineno,
                column=exc.offset,
                end_line=exc.end_lineno,
                end_column=exc.end_offset,
            ),
        )
    except ValueError as exc:
        # ast.parse raises ValueError (not SyntaxError) for a small set of
        # malformed-source cases (e.g. a null byte in the source) — still a
        # parse failure, not a crash.
        return ParseResult(
            status="failed",
            language="python",
            syntax_error=ParseSyntaxError(
                message=str(exc), line=None, column=None, end_line=None, end_column=None
            ),
        )
    except RecursionError as exc:
        # Pathologically deep nesting (adversarial or just generated code)
        # can exceed Python's own recursion limit during `ast.parse` itself
        # or during this module's AST walk — both are covered by this one
        # try block. Parsing is an untrusted-input boundary (same posture as
        # repository-ingestion.md section 3): this is a parse failure to
        # record, never a crash that takes down the whole run.
        return ParseResult(
            status="failed",
            language="python",
            syntax_error=ParseSyntaxError(
                message=f"source too deeply nested to parse: {exc}",
                line=None,
                column=None,
                end_line=None,
                end_column=None,
            ),
        )

    return ParseResult(
        status="parsed", language="python", symbols=extractor.symbols, imports=extractor.imports
    )
