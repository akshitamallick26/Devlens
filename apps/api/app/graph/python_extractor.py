"""Graph-candidate extraction for Python: a second, graph-specific AST pass,
deliberately separate from app/parsing/python_parser.py (Task 3's explicit
separation — that module's job is symbol extraction, this one's is call and
inheritance *candidates*). Pure function: no database, no filesystem, no
network. See docs/architecture/graph-construction.md sections 6 and 13.
"""

import ast
from collections import defaultdict

from app.graph.result import CallCandidate, GraphExtractionResult, InheritanceCandidate

_ASSIGNMENT_RECURSE_FIELDS = ("body", "orelse", "finalbody")


def _name_chain(expr: ast.expr) -> tuple[str, ...] | None:
    """`charge` -> ("charge",); `self.charge` -> ("self", "charge");
    `a.b.c` -> ("a", "b", "c"). Anything whose base isn't a plain `Name`
    (a call result, a subscript, ...) returns None — a dynamic receiver with
    nothing provable to extract, per graph-construction.md section 11."""
    parts: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return None


def _all_parameter_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg is not None:
        names.append(args.vararg.arg)
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return names


class _GraphCandidateVisitor(ast.NodeVisitor):
    """Walks the module tracking an explicit scope stack (module -> function
    -> function -> ...), collecting call/inheritance candidates and, per
    scope, the plain (non-import) names bound directly in it — needed so
    resolution can detect when a local variable shadows an outer-scope
    function of the same name (graph-construction.md section 13).

    Deliberately does NOT give comprehensions or lambdas their own tracked
    scope — a documented simplification, not an oversight; see
    graph-construction.md section 13's closing note.
    """

    def __init__(self, module_qualified_name: str) -> None:
        self._scope_stack: list[str] = [module_qualified_name]
        self.calls: list[CallCandidate] = []
        self.inheritances: list[InheritanceCandidate] = []
        self._bound_names: dict[str, set[str]] = defaultdict(set)
        self._first_parameter: dict[str, str | None] = {}

    @property
    def _scope(self) -> str:
        return self._scope_stack[-1]

    def result(self) -> GraphExtractionResult:
        return GraphExtractionResult(
            status="extracted",
            calls=self.calls,
            inheritances=self.inheritances,
            locally_bound_names={k: frozenset(v) for k, v in self._bound_names.items()},
            first_parameter_by_scope=dict(self._first_parameter),
        )

    # -- function/class definitions: these introduce new scopes ----------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified_name = f"{self._scope}.{node.name}"
        parameter_names = _all_parameter_names(node.args)
        for name in parameter_names:
            self._bound_names[qualified_name].add(name)
        # The *first* positional parameter specifically (posonly or regular —
        # never **kwargs/*args), used to recognize a `self`-like receiver
        # without hardcoding the literal name "self".
        positional = (*node.args.posonlyargs, *node.args.args)
        self._first_parameter[qualified_name] = positional[0].arg if positional else None
        # Decorators and default values execute in the ENCLOSING scope, not
        # the function's own body — visited before pushing the new scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

        self._scope_stack.append(qualified_name)
        for stmt in node.body:
            self.visit(stmt)
        self._scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = f"{self._scope}.{node.name}"
        resolution_scope = self._scope  # bases resolve in the ENCLOSING scope

        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            chain = _name_chain(base)
            if chain is not None:
                self.inheritances.append(
                    InheritanceCandidate(
                        subclass_qualified_name=qualified_name,
                        scope_qualified_name=resolution_scope,
                        name_chain=chain,
                        line=base.lineno,
                        column=base.col_offset,
                        end_line=getattr(base, "end_lineno", None),
                        end_column=getattr(base, "end_col_offset", None),
                    )
                )
            else:
                self.visit(base)  # dynamic base — still look for calls inside it
        for keyword in node.keywords:
            self.visit(keyword.value)

        self._scope_stack.append(qualified_name)
        for stmt in node.body:
            self.visit(stmt)
        self._scope_stack.pop()

    # -- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        chain = _name_chain(node.func)
        if chain is not None:
            self.calls.append(
                CallCandidate(
                    scope_qualified_name=self._scope,
                    name_chain=chain,
                    line=node.lineno,
                    column=node.col_offset,
                    end_line=getattr(node, "end_lineno", None),
                    end_column=getattr(node, "end_col_offset", None),
                )
            )
        self.generic_visit(node)  # still descend — arguments may contain nested calls

    # -- plain-binding tracking, for shadowing -----------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind(node.target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._bind(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._bind(node.target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars)
        self.generic_visit(node)

    def _bind(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self._bound_names[self._scope].add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind(elt)
        elif isinstance(target, ast.Starred):
            self._bind(target.value)
        # ast.Attribute (self.x = ...) / ast.Subscript (d[k] = ...) targets
        # don't bind a plain name — nothing to record.


def extract_graph_candidates(content: str, module_qualified_name: str) -> GraphExtractionResult:
    """`module_qualified_name` must be the same dotted name
    `app.parsing.python_parser.module_dotted_name()` computed for this file,
    so scope-qualified names line up exactly with the persisted Symbol
    table's qualified names."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        # Already recorded as a parsing failure by app/parsing/ — nothing
        # new to extract. A syntax error can't contain provable graph
        # evidence.
        return GraphExtractionResult(status="failed")

    visitor = _GraphCandidateVisitor(module_qualified_name)
    try:
        for stmt in tree.body:
            visitor.visit(stmt)
    except RecursionError:
        return GraphExtractionResult(status="failed")
    return visitor.result()
