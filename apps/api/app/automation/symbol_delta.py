"""Deterministic symbol-level delta (Task 2/4). `compute_symbol_delta` is
pure (no SQLAlchemy) — it operates on already-loaded `SymbolSnapshot`s;
`load_symbol_snapshots` is the DB-touching loader that builds them.

Identity (Task 2): `(file_path, kind, qualified_name)` — never a Symbol
row's own UUID, which is freshly generated every revision (even a
byte-identical file gets entirely new Symbol rows when the revision is
re-parsed) and therefore useless for deciding whether "this symbol" is the
same fact across revisions.

Content comparison (Task 4): a symbol's `signature` column already
includes its rendered decorators, parameters, and return type
(app/parsing/python_parser.py's `_render_decorators`/signature builders) —
`Symbol.start_line` itself does NOT include decorator lines (Python's own
`ast.FunctionDef.lineno` points at the `def` line, not the decorator
above it), so comparing only the body's line-span text would silently miss
a decorator-only change. Comparing `signature + body text` together catches
decorator changes, signature changes, docstring changes (part of the body
text), and body changes — while a pure line-number shift with identical
signature+body text correctly compares equal ("moved function" stays
unchanged, never treated as modified).

No LLM, no embeddings, no fuzzy name matching anywhere in this module.
"""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.content import load_lines_for_files
from app.automation.models import SymbolDelta, SymbolIdentity, SymbolSnapshot
from app.models import SourceFile, Symbol


def _symbol_content(
    signature: str | None, lines: list[str] | None, start_line: int, end_line: int
) -> str | None:
    if lines is None:
        return None
    body = "\n".join(lines[start_line - 1 : end_line])
    return f"{signature or ''}\x00{body}"


def load_symbol_snapshots(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, file_ids: set[uuid.UUID]
) -> list[SymbolSnapshot]:
    """Bounded to `file_ids` — callers pass only the added/deleted/modified
    file ids from `revision_delta.compute_file_delta`; symbols belonging to
    unchanged files are never loaded or diffed individually (a file whose
    content_hash is unchanged is guaranteed, by the parser's own
    determinism, to produce identical symbols — re-diffing them would be
    pure waste, not a correctness requirement)."""
    if not file_ids:
        return []
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.file_id.in_(file_ids))
    ).all()
    lines_by_file = load_lines_for_files(db, revision_id, file_ids)

    snapshots = []
    for symbol, path in rows:
        content = _symbol_content(
            symbol.signature, lines_by_file.get(symbol.file_id), symbol.start_line, symbol.end_line
        )
        snapshots.append(
            SymbolSnapshot(
                identity=SymbolIdentity(
                    file_path=path, kind=symbol.kind, qualified_name=symbol.qualified_name
                ),
                symbol_id=symbol.id,
                file_id=symbol.file_id,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                content=content,
            )
        )
    return snapshots


def compute_symbol_delta(
    current: list[SymbolSnapshot], parent: list[SymbolSnapshot]
) -> SymbolDelta:
    """Duplicate identities within one revision (the same (file, kind,
    qualified_name) appearing more than once — rare, e.g. a conditionally
    redefined top-level function) are grouped rather than silently
    overwritten; each group is compared positionally by sorted symbol_id so
    the result is deterministic even in that edge case."""
    current_by_identity: dict[SymbolIdentity, list[SymbolSnapshot]] = defaultdict(list)
    for snapshot in current:
        current_by_identity[snapshot.identity].append(snapshot)
    parent_by_identity: dict[SymbolIdentity, list[SymbolSnapshot]] = defaultdict(list)
    for snapshot in parent:
        parent_by_identity[snapshot.identity].append(snapshot)
    for group in current_by_identity.values():
        group.sort(key=lambda s: s.symbol_id)
    for group in parent_by_identity.values():
        group.sort(key=lambda s: s.symbol_id)

    added: list[SymbolSnapshot] = []
    deleted: list[SymbolSnapshot] = []
    modified: list[tuple[SymbolSnapshot, SymbolSnapshot]] = []
    unchanged: list[SymbolSnapshot] = []

    all_identities = sorted(
        set(current_by_identity) | set(parent_by_identity),
        key=lambda i: (i.file_path, i.kind, i.qualified_name),
    )
    for identity in all_identities:
        current_group = current_by_identity.get(identity, [])
        parent_group = parent_by_identity.get(identity, [])
        paired = min(len(current_group), len(parent_group))
        for i in range(paired):
            cur, par = current_group[i], parent_group[i]
            if cur.content is None or par.content is None or cur.content != par.content:
                modified.append((par, cur))
            else:
                unchanged.append(cur)
        added.extend(current_group[paired:])
        deleted.extend(parent_group[paired:])

    return SymbolDelta(
        added=tuple(added),
        deleted=tuple(deleted),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
    )
