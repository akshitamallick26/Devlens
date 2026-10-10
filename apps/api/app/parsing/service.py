"""Parsing orchestration: SourceFile rows -> Symbol rows, for one revision.

Mirrors the transaction/commit pattern established in
app/ingestion/service.py deliberately, not as a new design: the AnalysisRun
is created and committed early (durable, independent of what follows), the
actual Symbol writes happen in one transaction that's either fully committed
or fully rolled back, and a separate small commit records failure if the
pipeline itself breaks. Parsing never performs database writes from inside
the parser (app/parsing/python_parser.py) — this module is the only place
that touches a Session.
"""

import logging
import platform
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import (
    AnalysisRun,
    RepositoryRevision,
    SessionContextRef,
    SourceBlob,
    SourceFile,
    Symbol,
)
from app.parsing.parser import parse_source_file
from app.parsing.result import ParsedSymbol

logger = logging.getLogger("devlens.parsing")

ANALYZER_NAME = "devlens-python-ast-parser"


class ParsingError(Exception):
    pass


class RevisionNotFoundError(ParsingError):
    pass


class HistoricalReferenceError(ParsingError):
    """The re-parse would remove a symbol that an investigation session
    breadcrumb still references. Nothing is changed."""


# A symbol's deterministic identity within one revision (source-intelligence.md
# section 8): the same (file, kind, qualified_name). Equal keys in one file
# (a property getter and setter, a redefinition) are told apart by source
# position order.
_IdentityKey = tuple[uuid.UUID, str, str]


@dataclass(frozen=True)
class ParsingRunResult:
    revision_id: str
    analysis_run_id: str
    status: str
    files_parsed: int
    files_failed: int
    files_skipped: int
    symbol_count: int


def _log_event(event: str, **fields: object) -> None:
    """Same structured-ish logging convention as app/ingestion/service.py.
    Never passes source content — only identifiers/counts."""
    logger.info(event, extra={"event": event, **fields})


def _analyzer_version() -> str:
    return f"python-ast@{platform.python_version()}"


def _position(line: int, column: int | None) -> tuple[int, int]:
    return (line, column if column is not None else -1)


def _existing_ids_by_identity(
    db: Session, revision_id: uuid.UUID
) -> dict[_IdentityKey, list[uuid.UUID]]:
    """The revision's current Symbol ids, grouped by identity key and
    ordered by source position (id as a final tie-break). One query."""
    grouped: dict[_IdentityKey, list[tuple[tuple[int, int], str, uuid.UUID]]] = defaultdict(list)
    rows = db.execute(
        select(
            Symbol.id,
            Symbol.file_id,
            Symbol.kind,
            Symbol.qualified_name,
            Symbol.start_line,
            Symbol.start_column,
        ).where(Symbol.revision_id == revision_id)
    ).all()
    for symbol_id, file_id, kind, qualified_name, start_line, start_column in rows:
        grouped[(file_id, kind, qualified_name)].append(
            (_position(start_line, start_column), str(symbol_id), symbol_id)
        )
    return {key: [entry[2] for entry in sorted(entries)] for key, entries in grouped.items()}


def _assign_symbol_ids(
    file_id: uuid.UUID,
    parsed_symbols: list[ParsedSymbol],
    existing: dict[_IdentityKey, list[uuid.UUID]],
) -> list[uuid.UUID]:
    """Re-parsing an immutable revision reproduces the same symbols, so each
    one keeps the id it already has: breadcrumbs, generated tests and
    documentation that hold a Symbol id stay valid. Only symbols that did
    not exist before get a new id."""
    indexes_by_key: dict[_IdentityKey, list[int]] = defaultdict(list)
    for index, parsed in enumerate(parsed_symbols):
        indexes_by_key[(file_id, parsed.kind, parsed.qualified_name)].append(index)
    ids: dict[int, uuid.UUID] = {}
    for key, indexes in indexes_by_key.items():
        previous = existing.get(key, [])
        ordered = sorted(
            indexes,
            key=lambda i: _position(parsed_symbols[i].start_line, parsed_symbols[i].start_column),
        )
        for rank, index in enumerate(ordered):
            ids[index] = previous[rank] if rank < len(previous) else uuid.uuid4()
    return [ids[index] for index in range(len(parsed_symbols))]


def _build_symbol_rows(
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    file_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    parsed_symbols: list[ParsedSymbol],
    symbol_ids: list[uuid.UUID],
) -> list[Symbol]:
    """Ids are assigned client-side (rather than relying on the server
    default) so a child's `parent_symbol_id` can be set before any row is
    flushed — safe because the parser always emits a symbol before its
    children (a pre-order walk), so each parent's id is already known by the
    time its children are built."""
    rows: list[Symbol] = []
    ids_by_qualified_name: dict[str, uuid.UUID] = {}

    for parsed, symbol_id in zip(parsed_symbols, symbol_ids, strict=True):
        ids_by_qualified_name[parsed.qualified_name] = symbol_id
        parent_id = (
            ids_by_qualified_name.get(parsed.parent_qualified_name)
            if parsed.parent_qualified_name is not None
            else None
        )
        rows.append(
            Symbol(
                id=symbol_id,
                repository_id=repository_id,
                revision_id=revision_id,
                file_id=file_id,
                analysis_run_id=analysis_run_id,
                kind=parsed.kind,
                name=parsed.name,
                qualified_name=parsed.qualified_name,
                signature=parsed.signature,
                start_line=parsed.start_line,
                start_column=parsed.start_column,
                end_line=parsed.end_line,
                end_column=parsed.end_column,
                parent_symbol_id=parent_id,
                analyzer=_analyzer_version(),
            )
        )
    return rows


def run_parsing(db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID) -> ParsingRunResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="parsing",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    _log_event("parsing_started", repository_id=str(repository_id), revision_id=str(revision_id))

    try:
        result = _parse_revision(db, repository_id, revision, run)
        db.commit()
        _log_event(
            "parsing_completed",
            revision_id=str(revision_id),
            files_parsed=result.files_parsed,
            files_failed=result.files_failed,
            symbol_count=result.symbol_count,
        )
        return result
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        revision.latest_analysis_status = "failed"
        db.commit()
        _log_event("parsing_failed", revision_id=str(revision_id), reason=str(exc))
        raise ParsingError(str(exc)) from exc


def _parse_revision(
    db: Session, repository_id: uuid.UUID, revision: RepositoryRevision, run: AnalysisRun
) -> ParsingRunResult:
    # Idempotency strategy: rebuild every symbol of this revision, keeping
    # the id of each symbol the new parse reproduces (see
    # _assign_symbol_ids), inserting new ones and deleting only those that
    # vanished. Scoped to `revision_id`, so it never touches another
    # revision's symbols. Graph edges of vanished symbols cascade away; the
    # pipeline reports the graph outdated after any re-parse and rebuilds it.
    existing_ids = _existing_ids_by_identity(db, revision.id)
    previous_ids = {symbol_id for ids in existing_ids.values() for symbol_id in ids}
    parsed_rows: list[Symbol] = []

    files = (
        db.execute(
            select(SourceFile)
            .where(SourceFile.revision_id == revision.id)
            .order_by(SourceFile.path)
        )
        .scalars()
        .all()
    )

    files_parsed = 0
    files_failed = 0
    files_skipped = 0
    symbol_count = 0
    failures: dict[str, dict[str, object]] = {}

    for source_file in files:
        blob = db.get(SourceBlob, source_file.content_hash)
        if blob is None or blob.inline_content is None:
            # Content lives externally (storage_key) — not supported by this
            # phase's parser input path. Treated the same as an unsupported
            # language: skipped, not a failure.
            source_file.parsing_status = "skipped"
            files_skipped += 1
            continue

        parse_result = parse_source_file(
            blob.inline_content, source_file.path, source_file.language
        )

        if parse_result.status == "skipped":
            source_file.parsing_status = "skipped"
            files_skipped += 1
            continue

        if parse_result.status == "failed":
            source_file.parsing_status = "failed"
            files_failed += 1
            if parse_result.syntax_error is not None:
                failures[source_file.path] = {
                    "message": parse_result.syntax_error.message,
                    "line": parse_result.syntax_error.line,
                    "column": parse_result.syntax_error.column,
                }
            continue

        symbol_rows = _build_symbol_rows(
            repository_id,
            revision.id,
            source_file.id,
            run.id,
            parse_result.symbols,
            _assign_symbol_ids(source_file.id, parse_result.symbols, existing_ids),
        )
        parsed_rows.extend(symbol_rows)
        source_file.parsing_status = "parsed"
        files_parsed += 1
        symbol_count += len(symbol_rows)

    _write_symbols(db, revision.id, parsed_rows, previous_ids)

    revision.latest_analysis_status = "completed"
    run.status = "completed"
    run.finished_at = datetime.now(UTC)
    if failures:
        # Per-file syntax errors are an expected, handled outcome, not a run
        # failure (Task 11) — but worth recording. SourceFile has no column
        # of its own for "why did parsing fail" (a documented schema gap —
        # see source-intelligence.md), so the aggregate detail is attached to
        # the run that found it instead, using AnalysisRun.error_detail for
        # exactly what its own docstring says it's for: structured context,
        # not necessarily a failed run.
        run.error_detail = {"failed_files": failures}

    return ParsingRunResult(
        revision_id=str(revision.id),
        analysis_run_id=str(run.id),
        status="completed",
        files_parsed=files_parsed,
        files_failed=files_failed,
        files_skipped=files_skipped,
        symbol_count=symbol_count,
    )


def _write_symbols(
    db: Session, revision_id: uuid.UUID, rows: list[Symbol], previous_ids: set[uuid.UUID]
) -> None:
    """Inserts new symbols, updates kept ones in place, then deletes the
    vanished ones. In that order, every parent_symbol_id already exists when
    it is written, and no kept row is ever deleted, so nothing referencing it
    is cascaded or set to NULL."""
    kept = [row for row in rows if row.id in previous_ids]
    vanished = previous_ids - {row.id for row in kept}
    if vanished:
        # A breadcrumb is a historical record and cannot be nulled (its
        # one-hot CHECK) or deleted, so the re-parse is refused instead.
        referenced = db.execute(
            select(func.count())
            .select_from(SessionContextRef)
            .where(SessionContextRef.symbol_id.in_(vanished))
        ).scalar_one()
        if referenced:
            raise HistoricalReferenceError(
                f"re-parse would remove {len(vanished)} symbol(s), including symbols referenced "
                f"by {referenced} investigation session breadcrumb(s); nothing was changed"
            )

    db.add_all(row for row in rows if row.id not in previous_ids)
    db.flush()
    if kept:
        db.execute(
            update(Symbol),
            [
                {
                    "id": row.id,
                    "analysis_run_id": row.analysis_run_id,
                    "kind": row.kind,
                    "name": row.name,
                    "qualified_name": row.qualified_name,
                    "signature": row.signature,
                    "start_line": row.start_line,
                    "start_column": row.start_column,
                    "end_line": row.end_line,
                    "end_column": row.end_column,
                    "parent_symbol_id": row.parent_symbol_id,
                    "analyzer": row.analyzer,
                }
                for row in kept
            ],
        )
    if vanished:
        db.execute(
            delete(Symbol).where(Symbol.revision_id == revision_id).where(Symbol.id.in_(vanished))
        )
