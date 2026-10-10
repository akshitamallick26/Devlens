"""Deterministic documentation context assembly (Task 4/6). DB-touching
(reads already-persisted Symbol/GraphEdge/SourceFile/SourceBlob rows) but
AI-free and fully deterministic — same convention app/testing/context.py
already established for "pure" meaning "free of AI/network side effects,"
not "no DB." Every query filters by both `repository_id` AND
`revision_id` explicitly (Task 19/11) — no cross-revision leakage is
possible by construction.

Evidence ids are assigned only to concrete source-bearing material (module/
symbol/caller/callee excerpts, existing documentation text) — the identical
scoping app/testing/citations.py's own `Evidence` already uses (plain
structured facts like counts/paths/signatures are rendered directly in the
documentation task, never needing their own citation id, since they are
not something the model could misattribute or fabricate a variant of).
"""

import uuid

from sqlalchemy import String, func, select
from sqlalchemy.orm import Session

from app.automation.content import load_lines_for_files
from app.documentation.models import (
    ArchitectureOverviewFacts,
    DocumentationContextPackage,
    EvidenceItem,
    EvidenceSet,
    ModuleFacts,
    RepositoryOverviewFacts,
    SymbolFact,
    SymbolFacts,
)
from app.models import GraphEdge, Repository, RepositoryRevision, SourceBlob, SourceFile, Symbol

# Task 6: explicit, enforced budgets. Never silently exceeded — every cap
# below sets `DocumentationContextPackage.truncated = True` when it binds.
MAX_EVIDENCE_ITEMS = 20
MAX_SOURCE_CHARS_PER_ITEM = 2_000
MAX_TOTAL_SOURCE_CHARS = 20_000
MAX_SOURCE_FILES = 15
MAX_SYMBOLS = 30
MAX_GRAPH_RELATIONSHIPS = 30
MAX_TOP_LEVEL_MODULES = 50

# Fixed candidate paths, never a filesystem walk (Task 15).
README_CANDIDATES = ("README.md", "README.rst", "README.txt", "README")


def load_chunk_texts(
    db: Session, revision_id: uuid.UUID, requests: list[tuple[uuid.UUID, int, int]]
) -> list[str | None]:
    """Same contract as app/rag/persistence.py::load_chunk_texts, but loads
    only the files named in `requests` (one query) instead of every file in
    the revision. `None` for storage_key-backed files — never fabricated."""
    lines_by_file = load_lines_for_files(db, revision_id, {file_id for file_id, _, _ in requests})
    results: list[str | None] = []
    for file_id, start_line, end_line in requests:
        lines = lines_by_file.get(file_id)
        results.append("\n".join(lines[start_line - 1 : end_line]) if lines is not None else None)
    return results


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class _EvidenceBuilder:
    """Deterministic D<n> assignment (always the same call order -> always
    the same ids for the same inputs) plus both source budgets enforced in
    exactly one place."""

    def __init__(self) -> None:
        self._items: list[EvidenceItem] = []
        self._total_chars = 0
        self.truncated = False

    def add(
        self,
        *,
        repository_id: uuid.UUID,
        revision_id: uuid.UUID,
        file_path: str | None,
        symbol_id: uuid.UUID | None,
        qualified_name: str | None,
        graph_edge_relationship: str | None,
        line_start: int | None,
        line_end: int | None,
        kind: str,
        content: str | None,
    ) -> str | None:
        if len(self._items) >= MAX_EVIDENCE_ITEMS:
            self.truncated = True
            return None

        if content is not None:
            content, item_was_truncated = _truncate(content, MAX_SOURCE_CHARS_PER_ITEM)
            if item_was_truncated:
                self.truncated = True
            if self._total_chars + len(content) > MAX_TOTAL_SOURCE_CHARS:
                self.truncated = True
                return None
            self._total_chars += len(content)

        evidence_id = f"D{len(self._items) + 1}"
        self._items.append(
            EvidenceItem(
                evidence_id=evidence_id,
                repository_id=repository_id,
                revision_id=revision_id,
                file_path=file_path,
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                graph_edge_relationship=graph_edge_relationship,
                line_start=line_start,
                line_end=line_end,
                kind=kind,
                content=content,
            )
        )
        return evidence_id

    def build(self) -> EvidenceSet:
        return EvidenceSet(items=tuple(self._items))


def _load_readme(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[str | None, str | None]:
    rows = db.execute(
        select(SourceFile.path, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path.in_(README_CANDIDATES))
    ).all()
    by_path = dict(rows)
    for candidate in README_CANDIDATES:
        if candidate in by_path and by_path[candidate] is not None:
            return candidate, by_path[candidate]
    return None, None


def _symbol_fact(
    symbol: Symbol, file_path: str, relationship_type: str | None = None
) -> SymbolFact:
    return SymbolFact(
        symbol_id=symbol.id,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        file_path=file_path,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        signature=symbol.signature,
        relationship_type=relationship_type,
    )


def assemble_repository_overview_context(
    db: Session, repository: Repository, revision: RepositoryRevision
) -> DocumentationContextPackage:
    repository_id, revision_id = repository.id, revision.id
    builder = _EvidenceBuilder()

    file_count = db.execute(
        select(func.count())
        .select_from(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
    ).scalar_one()
    symbol_count = db.execute(
        select(func.count())
        .select_from(Symbol)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
    ).scalar_one()
    graph_edge_count = db.execute(
        select(func.count())
        .select_from(GraphEdge)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
    ).scalar_one()

    language_rows = db.execute(
        select(SourceFile.language, func.count())
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.language.is_not(None))
        .group_by(SourceFile.language)
    ).all()
    files_by_language = tuple(
        sorted(
            ((lang, count) for lang, count in language_rows if lang is not None),
            key=lambda item: (-item[1], item[0]),
        )
    )

    top_level = (
        select(func.split_part(SourceFile.path, "/", 1).label("module"))
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path.contains("/"))
        .subquery()
    )
    top_level_rows = (
        db.execute(
            select(top_level.c.module)
            .distinct()
            .order_by(top_level.c.module)
            .limit(MAX_TOP_LEVEL_MODULES + 1)
        )
        .scalars()
        .all()
    )
    if len(top_level_rows) > MAX_TOP_LEVEL_MODULES:
        builder.truncated = True
    top_level_modules = tuple(top_level_rows[:MAX_TOP_LEVEL_MODULES])

    readme_path, readme_content = _load_readme(db, repository_id, revision_id)
    if readme_content is not None:
        builder.add(
            repository_id=repository_id,
            revision_id=revision_id,
            file_path=readme_path,
            symbol_id=None,
            qualified_name=None,
            graph_edge_relationship=None,
            line_start=None,
            line_end=None,
            kind="existing_documentation",
            content=readme_content,
        )

    facts = RepositoryOverviewFacts(
        repository_name=f"{repository.owner}/{repository.name}",
        default_branch=repository.default_branch,
        revision_commit_sha=revision.commit_sha,
        file_count=file_count,
        files_by_language=files_by_language,
        top_level_modules=top_level_modules,
        symbol_count=symbol_count,
        graph_edge_count=graph_edge_count,
        existing_readme_path=readme_path,
    )
    return DocumentationContextPackage(
        repository_id=repository_id,
        revision_id=revision_id,
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=builder.build(),
        truncated=builder.truncated,
    )


def assemble_architecture_overview_context(
    db: Session, repository: Repository, revision: RepositoryRevision
) -> DocumentationContextPackage:
    repository_id, revision_id = repository.id, revision.id
    builder = _EvidenceBuilder()

    module_count = db.execute(
        select(func.count(func.distinct(Symbol.file_id)))
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
    ).scalar_one()

    relationship_rows = db.execute(
        select(GraphEdge.relationship_type, func.count())
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .group_by(GraphEdge.relationship_type)
    ).all()
    relationship_counts = tuple(
        sorted(
            ((rel, count) for rel, count in relationship_rows), key=lambda item: (-item[1], item[0])
        )
    )

    endpoints = (
        select(GraphEdge.source_symbol_id.label("symbol_id"))
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .union_all(
            select(GraphEdge.target_symbol_id.label("symbol_id"))
            .where(GraphEdge.repository_id == repository_id)
            .where(GraphEdge.revision_id == revision_id)
            .where(GraphEdge.target_symbol_id.is_not(None))
        )
        .subquery()
    )
    degree = func.count().label("degree")
    key_symbol_ids: list[uuid.UUID] = list(
        db.execute(
            select(endpoints.c.symbol_id)
            .group_by(endpoints.c.symbol_id)
            .order_by(degree.desc(), func.cast(endpoints.c.symbol_id, String))
            .limit(MAX_SYMBOLS)
        )
        .scalars()
        .all()
    )

    key_symbols: tuple[SymbolFact, ...] = ()
    if key_symbol_ids:
        rows = db.execute(
            select(Symbol, SourceFile.path)
            .join(SourceFile, Symbol.file_id == SourceFile.id)
            .where(Symbol.repository_id == repository_id)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.id.in_(key_symbol_ids))
        ).all()
        by_id = {symbol.id: (symbol, path) for symbol, path in rows}
        ordered = [by_id[sid] for sid in key_symbol_ids if sid in by_id]
        key_symbols = tuple(_symbol_fact(symbol, path) for symbol, path in ordered)

        spans = [
            (symbol.file_id, symbol.start_line, symbol.end_line)
            for symbol, _path in ordered[: min(len(ordered), MAX_GRAPH_RELATIONSHIPS)]
        ]
        texts = load_chunk_texts(db, revision_id, spans)
        for (symbol, path), text in zip(ordered, texts, strict=False):
            if text is None:
                continue
            builder.add(
                repository_id=repository_id,
                revision_id=revision_id,
                file_path=path,
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
                graph_edge_relationship=None,
                line_start=symbol.start_line,
                line_end=symbol.end_line,
                kind="symbol_source",
                content=text,
            )
        if len(ordered) > len(spans):
            builder.truncated = True

    readme_path, readme_content = _load_readme(db, repository_id, revision_id)
    if readme_content is not None:
        builder.add(
            repository_id=repository_id,
            revision_id=revision_id,
            file_path=readme_path,
            symbol_id=None,
            qualified_name=None,
            graph_edge_relationship=None,
            line_start=None,
            line_end=None,
            kind="existing_documentation",
            content=readme_content,
        )

    facts = ArchitectureOverviewFacts(
        module_count=module_count,
        key_symbols=key_symbols,
        relationship_counts=relationship_counts,
        existing_readme_path=readme_path,
    )
    return DocumentationContextPackage(
        repository_id=repository_id,
        revision_id=revision_id,
        artifact_type="architecture_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=builder.build(),
        truncated=builder.truncated,
    )


def assemble_module_context(
    db: Session,
    repository: Repository,
    revision: RepositoryRevision,
    target_file: SourceFile,
) -> DocumentationContextPackage:
    repository_id, revision_id = repository.id, revision.id
    builder = _EvidenceBuilder()

    symbol_rows = (
        db.execute(
            select(Symbol)
            .where(Symbol.repository_id == repository_id)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.file_id == target_file.id)
            .order_by(Symbol.start_line, Symbol.id)
            .limit(MAX_SYMBOLS)
        )
        .scalars()
        .all()
    )
    symbol_ids = {s.id for s in symbol_rows}
    symbols = tuple(_symbol_fact(s, target_file.path) for s in symbol_rows)

    imports: tuple[str, ...] = ()
    outbound_dependencies: tuple[str, ...] = ()
    inbound_dependents: tuple[str, ...] = ()

    if symbol_ids:
        edge_rows = db.execute(
            select(
                GraphEdge.relationship_type,
                GraphEdge.source_symbol_id,
                GraphEdge.target_symbol_id,
                GraphEdge.target_external_ref,
            )
            .where(GraphEdge.repository_id == repository_id)
            .where(GraphEdge.revision_id == revision_id)
            .where(
                GraphEdge.source_symbol_id.in_(symbol_ids)
                | GraphEdge.target_symbol_id.in_(symbol_ids)
            )
            .order_by(GraphEdge.relationship_type, GraphEdge.source_symbol_id, GraphEdge.id)
            .limit(MAX_GRAPH_RELATIONSHIPS * 4)
        ).all()

        import_targets: set[str] = set()
        outbound_target_ids: set[uuid.UUID] = set()
        inbound_source_ids: set[uuid.UUID] = set()
        for relationship_type, source_id, target_id, external_ref in edge_rows:
            if relationship_type == "imports" and source_id in symbol_ids:
                if external_ref:
                    import_targets.add(external_ref)
            elif relationship_type == "calls" and source_id in symbol_ids and target_id is not None:
                outbound_target_ids.add(target_id)
            elif (
                relationship_type == "calls"
                and target_id in symbol_ids
                and source_id not in symbol_ids
            ):
                inbound_source_ids.add(source_id)
        imports = tuple(sorted(import_targets))[:MAX_GRAPH_RELATIONSHIPS]

        if outbound_target_ids:
            rows = (
                db.execute(
                    select(SourceFile.path)
                    .join(Symbol, Symbol.file_id == SourceFile.id)
                    .where(Symbol.id.in_(outbound_target_ids))
                    .where(Symbol.repository_id == repository_id)
                    .where(Symbol.revision_id == revision_id)
                    .where(SourceFile.path != target_file.path)
                    .distinct()
                )
                .scalars()
                .all()
            )
            outbound_dependencies = tuple(sorted(rows))[:MAX_GRAPH_RELATIONSHIPS]

        if inbound_source_ids:
            rows = (
                db.execute(
                    select(SourceFile.path)
                    .join(Symbol, Symbol.file_id == SourceFile.id)
                    .where(Symbol.id.in_(inbound_source_ids))
                    .where(Symbol.repository_id == repository_id)
                    .where(Symbol.revision_id == revision_id)
                    .where(SourceFile.path != target_file.path)
                    .distinct()
                )
                .scalars()
                .all()
            )
            inbound_dependents = tuple(sorted(rows))[:MAX_GRAPH_RELATIONSHIPS]

    module_text = load_chunk_texts(
        db,
        revision_id,
        [(target_file.id, 1, target_file.line_count or 10_000)],
    )[0]
    if module_text is not None:
        builder.add(
            repository_id=repository_id,
            revision_id=revision_id,
            file_path=target_file.path,
            symbol_id=None,
            qualified_name=None,
            graph_edge_relationship=None,
            line_start=1,
            line_end=target_file.line_count,
            kind="module_source",
            content=module_text,
        )

    facts = ModuleFacts(
        file_path=target_file.path,
        language=target_file.language,
        imports=imports,
        symbols=symbols,
        outbound_dependencies=outbound_dependencies,
        inbound_dependents=inbound_dependents,
    )
    return DocumentationContextPackage(
        repository_id=repository_id,
        revision_id=revision_id,
        artifact_type="module_documentation",
        target_file_id=target_file.id,
        target_symbol_id=None,
        facts=facts,
        evidence=builder.build(),
        truncated=builder.truncated,
    )


def assemble_symbol_context(
    db: Session,
    repository: Repository,
    revision: RepositoryRevision,
    target_symbol: Symbol,
    target_file_path: str,
) -> DocumentationContextPackage:
    repository_id, revision_id = repository.id, revision.id
    builder = _EvidenceBuilder()

    target_text = load_chunk_texts(
        db, revision_id, [(target_symbol.file_id, target_symbol.start_line, target_symbol.end_line)]
    )[0]
    if target_text is not None:
        builder.add(
            repository_id=repository_id,
            revision_id=revision_id,
            file_path=target_file_path,
            symbol_id=target_symbol.id,
            qualified_name=target_symbol.qualified_name,
            graph_edge_relationship=None,
            line_start=target_symbol.start_line,
            line_end=target_symbol.end_line,
            kind="symbol_source",
            content=target_text,
        )

    edge_rows = db.execute(
        select(GraphEdge.relationship_type, GraphEdge.source_symbol_id, GraphEdge.target_symbol_id)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(
            (GraphEdge.source_symbol_id == target_symbol.id)
            | (GraphEdge.target_symbol_id == target_symbol.id)
        )
        .order_by(GraphEdge.relationship_type, GraphEdge.source_symbol_id, GraphEdge.id)
        .limit(MAX_GRAPH_RELATIONSHIPS * 4)
    ).all()

    caller_ids: list[tuple[uuid.UUID, str]] = []
    callee_ids: list[tuple[uuid.UUID, str]] = []
    inherits_from_ids: list[uuid.UUID] = []
    for relationship_type, source_id, target_id in edge_rows:
        if (
            relationship_type == "calls"
            and target_id == target_symbol.id
            and source_id != target_symbol.id
        ):
            caller_ids.append((source_id, relationship_type))
        elif (
            relationship_type == "calls" and source_id == target_symbol.id and target_id is not None
        ):
            callee_ids.append((target_id, relationship_type))
        elif (
            relationship_type == "inherits"
            and source_id == target_symbol.id
            and target_id is not None
        ):
            inherits_from_ids.append(target_id)

    caller_ids = caller_ids[:MAX_GRAPH_RELATIONSHIPS]
    callee_ids = callee_ids[:MAX_GRAPH_RELATIONSHIPS]

    related_ids = (
        {sid for sid, _ in caller_ids} | {sid for sid, _ in callee_ids} | set(inherits_from_ids)
    )
    related_by_id: dict[uuid.UUID, tuple[Symbol, str]] = {}
    if related_ids:
        rows = db.execute(
            select(Symbol, SourceFile.path)
            .join(SourceFile, Symbol.file_id == SourceFile.id)
            .where(Symbol.repository_id == repository_id)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.id.in_(related_ids))
        ).all()
        related_by_id = {symbol.id: (symbol, path) for symbol, path in rows}

    # One bounded query for every file a caller/callee lives in, not one per symbol.
    related_lines = load_lines_for_files(
        db, revision_id, {symbol.file_id for symbol, _path in related_by_id.values()}
    )

    def _facts_and_evidence(
        pairs: list[tuple[uuid.UUID, str]], kind: str
    ) -> tuple[SymbolFact, ...]:
        facts_list: list[SymbolFact] = []
        for sid, rel in pairs:
            entry = related_by_id.get(sid)
            if entry is None:
                continue
            symbol, path = entry
            facts_list.append(_symbol_fact(symbol, path, relationship_type=rel))
            lines = related_lines.get(symbol.file_id)
            if lines is not None:
                text = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
                builder.add(
                    repository_id=repository_id,
                    revision_id=revision_id,
                    file_path=path,
                    symbol_id=symbol.id,
                    qualified_name=symbol.qualified_name,
                    graph_edge_relationship=rel,
                    line_start=symbol.start_line,
                    line_end=symbol.end_line,
                    kind=kind,
                    content=text,
                )
        return tuple(facts_list)

    callers = _facts_and_evidence(caller_ids, "caller")
    callees = _facts_and_evidence(callee_ids, "callee")
    inherits_from = tuple(
        related_by_id[sid][0].qualified_name for sid in inherits_from_ids if sid in related_by_id
    )

    facts = SymbolFacts(
        symbol_id=target_symbol.id,
        qualified_name=target_symbol.qualified_name,
        kind=target_symbol.kind,
        file_path=target_file_path,
        start_line=target_symbol.start_line,
        end_line=target_symbol.end_line,
        signature=target_symbol.signature,
        callers=callers,
        callees=callees,
        inherits_from=inherits_from,
    )
    return DocumentationContextPackage(
        repository_id=repository_id,
        revision_id=revision_id,
        artifact_type="symbol_documentation",
        target_file_id=None,
        target_symbol_id=target_symbol.id,
        facts=facts,
        evidence=builder.build(),
        truncated=builder.truncated,
    )
