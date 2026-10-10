"""Pure, deterministic chunking — no DB, no LLM, no embeddings, no
app.parsing import. See docs/architecture/rag-codebase-qa.md section 2.

`chunk_file` is a pure function of (file identity, file content, the
revision's already-parsed SymbolSpans for this file): same inputs always
produce the same chunks, in the same order.
"""

import uuid
from dataclasses import dataclass

from app.rag.models import ChunkCandidate, SymbolSpan

# A fixed v1 policy constant, not environment-driven — same "code-level
# decision, not a per-request tuning knob" approach as
# app/impact/context.py's budget constants.
MAX_CHUNK_LINES = 200


def _decorator_extended_start(lines: list[str], start_line: int) -> int:
    """A Symbol's own `start_line` excludes its decorator lines (confirmed
    directly against app/parsing/python_parser.py's actual output, not
    assumed) — chunking a decorated function without its decorators would
    destroy real semantic meaning (`@app.route(...)`, `@validates`, etc.
    change what the function means). Scans backward from the line
    immediately before `start_line` for simple, single-line `@...`
    decorators, extending the chunk to include them.

    Documented limitation: multi-line decorator argument lists
    (`@foo(\\n    bar,\\n)`) are not detected by this simple heuristic —
    each continuation line doesn't itself start with "@", so the scan
    stops at the first one. Accepted for v1 as a rare case; the common
    single-line-decorator case is handled correctly."""
    effective_start = start_line
    index = start_line - 2  # 0-indexed line immediately before start_line
    while index >= 0 and lines[index].strip().startswith("@"):
        effective_start = index + 1
        index -= 1
    return effective_start


@dataclass(frozen=True)
class _Span:
    start_line: int
    end_line: int


def _split_span(span: _Span, max_lines: int) -> list[_Span]:
    """Splits one line range into consecutive, non-overlapping,
    gap-free sub-ranges of at most `max_lines` each. The last sub-range may
    be shorter; nothing is ever dropped."""
    if span.end_line - span.start_line + 1 <= max_lines:
        return [span]
    pieces: list[_Span] = []
    current = span.start_line
    while current <= span.end_line:
        sub_end = min(current + max_lines - 1, span.end_line)
        pieces.append(_Span(current, sub_end))
        current = sub_end + 1
    return pieces


def chunk_file(
    file_id: uuid.UUID,
    file_path: str,
    content: str,
    symbols: list[SymbolSpan],
    max_chunk_lines: int = MAX_CHUNK_LINES,
) -> list[ChunkCandidate]:
    """One `symbol_body` (or several `sliding_window` pieces, if oversized)
    chunk per non-module symbol, plus exactly one whole-file `file_summary`
    chunk (or several, if the file itself is oversized) per file —
    deliberately covering module-level code that has no Symbol (imports,
    top-level statements) and giving file-level retrieval context, at the
    documented cost of overlapping symbol-level chunks' text (section 2's
    "known limitation": trading minor duplication for not computing exact
    gap-only line ranges between symbols).

    `symbols` is expected to include every Symbol in this file (module
    included) — `chunk_file` itself decides which become which chunk kind.
    """
    lines = content.splitlines()
    line_count = max(len(lines), 1)

    chunks: list[ChunkCandidate] = []

    non_module_symbols = sorted(
        (s for s in symbols if s.kind != "module"),
        key=lambda s: (s.start_line, s.qualified_name),
    )
    for symbol in non_module_symbols:
        effective_start = _decorator_extended_start(lines, symbol.start_line)
        pieces = _split_span(_Span(effective_start, symbol.end_line), max_chunk_lines)
        for index, piece in enumerate(pieces):
            chunks.append(
                ChunkCandidate(
                    chunk_kind="symbol_body" if len(pieces) == 1 else "sliding_window",
                    file_id=file_id,
                    file_path=file_path,
                    symbol_id=symbol.symbol_id,
                    qualified_name=symbol.qualified_name,
                    start_line=piece.start_line,
                    end_line=piece.end_line,
                    chunk_index=index,
                    chunk_count=len(pieces),
                )
            )

    module_symbol = next((s for s in symbols if s.kind == "module"), None)
    whole_file_pieces = _split_span(_Span(1, line_count), max_chunk_lines)
    for index, piece in enumerate(whole_file_pieces):
        chunks.append(
            ChunkCandidate(
                chunk_kind="file_summary",
                file_id=file_id,
                file_path=file_path,
                symbol_id=module_symbol.symbol_id if module_symbol is not None else None,
                qualified_name=module_symbol.qualified_name if module_symbol is not None else None,
                start_line=piece.start_line,
                end_line=piece.end_line,
                chunk_index=index,
                chunk_count=len(whole_file_pieces),
            )
        )

    # Deterministic ordering: by start_line, then chunk_kind as a stable
    # tiebreak (file_summary pieces and a symbol starting on the same line
    # are possible — e.g. a one-line file), then qualified_name, then
    # chunk_index. Never relies on dict/list insertion order alone beyond
    # this explicit sort.
    chunks.sort(key=lambda c: (c.start_line, c.chunk_kind, c.qualified_name or "", c.chunk_index))
    return chunks
