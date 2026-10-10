"""Deterministic, conservative discovery of existing Python tests and their
evidence-based relevance to a test target. DB-touching (queries already-
persisted Symbol/GraphEdge/SourceFile rows) but no new parsing: ingestion
and parsing never special-case `test_*.py` files (confirmed by forensic
audit — app/ingestion/inventory.py classifies them as ordinary Python,
app/parsing/service.py parses them like any other file), so a test
function's `calls` edge to whatever it tests is already sitting in
GraphEdge from the existing graph-construction pipeline. This module reuses
that data; it does not run a second, parallel AST pass over test files.

The LLM plays no role here (Task 4's explicit prohibition) — every
relevance reason below is a concrete, checkable fact.
"""

import re
import uuid
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GraphEdge, SourceFile, Symbol
from app.testing.models import ExistingTestRef

_TEST_FILE_PATTERN = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py)$")

# Bounded — never return an unbounded list of "related" tests for a
# popular target symbol.
MAX_EXISTING_TESTS_PER_TARGET = 5

# Relevance reasons in strongest-first order, used only to pick a
# deterministic tie-break among candidates, never exposed as a numeric score.
_REASON_STRENGTH = {
    "graph_relationship": 0,
    "imports_target_module": 1,
    "same_module": 2,
    "identifier_overlap": 3,
}
# Only these establish that a test relates to a target. `same_module`
# (file name/directory resemblance) and `identifier_overlap` (name
# substring) are annotations on an already-qualified test, never evidence
# on their own: a file or test merely *named* after the target proves
# nothing.
_QUALIFYING_REASONS = frozenset({"graph_relationship", "imports_target_module"})

# The final segment of a base class expression that marks a unittest-style
# class: unittest.TestCase, IsolatedAsyncioTestCase, django's TestCase /
# SimpleTestCase / TransactionTestCase.
_TESTCASE_BASE = re.compile(r"(^|\.)\w*TestCase$")
_CLASS_SIGNATURE = re.compile(r"class\s+\w+\((?P<bases>.*)\)\s*$", re.DOTALL)


def is_test_file_path(path: str) -> bool:
    return bool(_TEST_FILE_PATTERN.search(path))


def _signature_bases(signature: str | None) -> list[str]:
    """Base-class expressions from a persisted class signature
    (`class Name(base1, pkg.base2)`, as rendered by the parser), split on
    top-level commas only. Keyword arguments (metaclass=...) are dropped."""
    if not signature:
        return []
    match = _CLASS_SIGNATURE.search(signature.splitlines()[-1])
    if match is None:
        return []
    bases: list[str] = []
    current: list[str] = []
    depth = 0
    for char in match.group("bases"):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            bases.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    bases.append("".join(current).strip())
    return [b for b in bases if b and "=" not in b]


def _is_unittest_class(class_symbol: Symbol, inherits_external: set[uuid.UUID]) -> bool:
    if class_symbol.id in inherits_external:
        return True
    return any(_TESTCASE_BASE.search(base) for base in _signature_bases(class_symbol.signature))


def _classify_test_symbol(
    symbol: Symbol, symbols_by_id: dict[uuid.UUID, Symbol], unittest_class_ids: set[uuid.UUID]
) -> str | None:
    """Returns one of TEST_SYMBOL_KINDS, or None if `symbol` isn't a test
    by the conventions the runners themselves use:

    - pytest function: a `test_*` function defined at module level (the
      parser records the module symbol as its parent; nested functions are
      not collected by pytest and are not tests);
    - unittest method: a `test_*` method of a class deriving from a
      *TestCase base;
    - pytest method: a `test_*` method of a `Test*` class that is not a
      unittest class."""
    if not symbol.name.startswith("test_"):
        return None
    parent = symbols_by_id.get(symbol.parent_symbol_id) if symbol.parent_symbol_id else None
    if symbol.kind == "function" and (parent is None or parent.kind == "module"):
        return "pytest_function"
    if symbol.kind == "method" and parent is not None and parent.kind == "class":
        if parent.id in unittest_class_ids:
            return "unittest_method"
        if parent.name.startswith("Test"):
            return "pytest_method"
    return None


def _unittest_class_ids(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    classes: list[Symbol],
) -> set[uuid.UUID]:
    """Test-file classes deriving from a *TestCase base. Two deterministic
    sources, both already persisted: an external-ref `inherits` edge (when
    graph construction resolved the base, e.g. `from unittest import
    TestCase`) or the base expressions in the parser's class signature
    (needed for dotted bases such as `unittest.TestCase`, which graph
    construction records only as an import of `unittest`)."""
    if not classes:
        return set()
    rows = db.execute(
        select(GraphEdge.source_symbol_id)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(GraphEdge.relationship_type == "inherits")
        .where(GraphEdge.source_symbol_id.in_({c.id for c in classes}))
        .where(GraphEdge.target_external_ref.ilike("%TestCase%"))
    ).all()
    inherits_external = {row[0] for row in rows}
    return {c.id for c in classes if _is_unittest_class(c, inherits_external)}


def _same_module(test_path: str, target_path: str) -> bool:
    test_p, target_p = PurePosixPath(test_path), PurePosixPath(target_path)
    if test_p.parent == target_p.parent:
        return True
    stem = target_p.stem
    return test_p.stem in (f"test_{stem}", f"{stem}_test")


def find_existing_tests_for_targets(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    target_symbol_ids: set[uuid.UUID],
) -> dict[uuid.UUID, tuple[ExistingTestRef, ...]]:
    """Returns, for each target symbol id in `target_symbol_ids`, a bounded,
    deterministically ordered tuple of existing tests related to it by
    qualifying evidence (a direct `calls` edge, or the test's file importing
    the target's module). A target with no related test is present with an
    empty tuple, never simply absent. Every query is scoped to the
    repository and revision."""
    empty: dict[uuid.UUID, tuple[ExistingTestRef, ...]] = {sid: () for sid in target_symbol_ids}
    if not target_symbol_ids:
        return {}

    def edge_scope() -> list[Any]:
        return [GraphEdge.repository_id == repository_id, GraphEdge.revision_id == revision_id]

    def symbol_scope() -> list[Any]:
        return [Symbol.repository_id == repository_id, Symbol.revision_id == revision_id]

    target_rows = (
        db.execute(select(Symbol).where(*symbol_scope()).where(Symbol.id.in_(target_symbol_ids)))
        .scalars()
        .all()
    )
    targets_by_id = {s.id: s for s in target_rows}
    if not targets_by_id:
        return empty

    # Test files are selected in SQL by path convention; only their paths
    # and the targets' paths are loaded, never every path in the revision.
    test_file_rows = db.execute(
        select(SourceFile.id, SourceFile.path)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path.regexp_match(_TEST_FILE_PATTERN.pattern))
    ).all()
    if not test_file_rows:
        return empty
    file_paths_by_id = dict(test_file_rows)
    test_file_ids = set(file_paths_by_id)
    target_file_ids = {s.file_id for s in targets_by_id.values()}
    file_paths_by_id.update(
        db.execute(
            select(SourceFile.id, SourceFile.path)
            .where(SourceFile.repository_id == repository_id)
            .where(SourceFile.revision_id == revision_id)
            .where(SourceFile.id.in_(target_file_ids))
        ).all()
    )

    test_symbols = (
        db.execute(
            select(Symbol)
            .where(*symbol_scope())
            .where(Symbol.file_id.in_(test_file_ids))
            .order_by(Symbol.qualified_name, Symbol.id)
        )
        .scalars()
        .all()
    )
    if not test_symbols:
        return empty
    symbols_by_id = {s.id: s for s in test_symbols}
    unittest_class_ids = _unittest_class_ids(
        db, repository_id, revision_id, [s for s in test_symbols if s.kind == "class"]
    )

    test_symbol_kind: dict[uuid.UUID, str] = {}
    for symbol in test_symbols:
        kind = _classify_test_symbol(symbol, symbols_by_id, unittest_class_ids)
        if kind is not None:
            test_symbol_kind[symbol.id] = kind
    if not test_symbol_kind:
        return empty

    # "graph_relationship": a `calls` edge from the test symbol to the
    # target (source = dependent = the test, target = the dependency).
    calls_rows = db.execute(
        select(GraphEdge.source_symbol_id, GraphEdge.target_symbol_id)
        .where(*edge_scope())
        .where(GraphEdge.relationship_type == "calls")
        .where(GraphEdge.source_symbol_id.in_(test_symbol_kind))
        .where(GraphEdge.target_symbol_id.in_(target_symbol_ids))
    ).all()
    calls_target_ids: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for source_id, target_id in calls_rows:
        if target_id is not None:
            calls_target_ids[source_id].add(target_id)

    # "imports_target_module": something in the test's file imports a
    # symbol defined in *that target's* file (the target, a sibling, or the
    # module symbol itself). File-granular, since an import's source is the
    # enclosing module or function. Imported symbols are resolved to their
    # files in one query.
    imports_rows = db.execute(
        select(GraphEdge.source_symbol_id, Symbol.file_id)
        .join(Symbol, Symbol.id == GraphEdge.target_symbol_id)
        .where(*edge_scope())
        .where(*symbol_scope())
        .where(GraphEdge.relationship_type == "imports")
        .where(GraphEdge.source_symbol_id.in_(symbols_by_id))
        .where(Symbol.file_id.in_(target_file_ids))
    ).all()
    importing_test_files_by_target_file: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for source_id, imported_file_id in imports_rows:
        importing_test_files_by_target_file[imported_file_id].add(symbols_by_id[source_id].file_id)

    result: dict[uuid.UUID, list[ExistingTestRef]] = {sid: [] for sid in target_symbol_ids}
    for symbol in test_symbols:
        kind = test_symbol_kind.get(symbol.id)
        if kind is None:
            continue
        test_path = file_paths_by_id[symbol.file_id]
        display_name = symbol.name
        if kind != "pytest_function":
            parent = symbols_by_id.get(symbol.parent_symbol_id) if symbol.parent_symbol_id else None
            if parent is not None:
                display_name = f"{parent.name}.{symbol.name}"

        for target_id, target in targets_by_id.items():
            target_path = file_paths_by_id.get(target.file_id)
            reasons: list[str] = []
            if target_id in calls_target_ids.get(symbol.id, set()):
                reasons.append("graph_relationship")
            if symbol.file_id in importing_test_files_by_target_file.get(target.file_id, set()):
                reasons.append("imports_target_module")
            if not _QUALIFYING_REASONS.intersection(reasons):
                continue
            if target_path is not None and _same_module(test_path, target_path):
                reasons.append("same_module")
            # Guarded by a minimum length so a trivially short target name
            # (e.g. "run") doesn't spuriously "overlap" with nearly every
            # test function.
            if len(target.name) >= 3 and target.name.lower() in symbol.name.lower():
                reasons.append("identifier_overlap")
            result[target_id].append(
                ExistingTestRef(
                    file_id=symbol.file_id,
                    file_path=test_path,
                    symbol_id=symbol.id,
                    test_identifier=display_name,
                    kind=kind,
                    relevance_reasons=tuple(reasons),
                )
            )

    bounded: dict[uuid.UUID, tuple[ExistingTestRef, ...]] = {}
    for target_id, refs in result.items():
        refs.sort(
            key=lambda r: (
                min(_REASON_STRENGTH[reason] for reason in r.relevance_reasons),
                r.test_identifier,
                r.symbol_id,
            )
        )
        bounded[target_id] = tuple(refs[:MAX_EXISTING_TESTS_PER_TARGET])
    return bounded
