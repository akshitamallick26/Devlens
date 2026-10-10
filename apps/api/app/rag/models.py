"""Pure data shapes shared across app/rag/ — no SQLAlchemy, no DB. See
docs/architecture/rag-codebase-qa.md. `SymbolSpan` is the DB-agnostic
input app/rag/persistence.py builds from real `Symbol` rows before handing
it to the pure `chunking.py`, which never imports SQLAlchemy or
app.parsing — keeping the chunker independently testable and decoupled
from both.
"""

import uuid
from dataclasses import dataclass

CHUNK_KINDS = ("symbol_body", "sliding_window", "file_summary")


@dataclass(frozen=True)
class SymbolSpan:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str  # Symbol.kind — "module", "function", "class", ...
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ChunkCandidate:
    """Pure, deterministic output of chunking.py — not yet persisted.
    `chunk_index`/`chunk_count` are only meaningful for a symbol split into
    multiple sliding_window pieces (both are 0/1 for a single whole chunk)."""

    chunk_kind: str
    file_id: uuid.UUID
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    start_line: int
    end_line: int
    chunk_index: int
    chunk_count: int


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk found by a single retriever (lexical or vector), before
    hybrid combination — `rank` is 1-based, within that retriever's own
    result list only."""

    chunk_id: uuid.UUID
    file_id: uuid.UUID
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    start_line: int
    end_line: int
    rank: int


@dataclass(frozen=True)
class HybridResult:
    chunk_id: uuid.UUID
    file_id: uuid.UUID
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    start_line: int
    end_line: int
    rrf_score: float
    sources: tuple[str, ...]  # e.g. ("lexical",), ("vector",), ("lexical", "vector")


@dataclass(frozen=True)
class GraphExpandedSymbol:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    relationship_type: str
    direction: (
        str  # "callers" | "callees" | "importers" | "imports" | "subclasses" | "base_classes"
    )
    seed_symbol_id: uuid.UUID  # which retrieved symbol this expansion came from
