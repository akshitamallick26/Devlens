"""Pure, adversarial chunking tests — no DB, always run. Uses the real
parser (app.parsing.python_parser) to produce realistic SymbolSpans, since
chunking's whole job is to react correctly to what the parser actually
produces (confirmed against real parser output, not guessed).
"""

import uuid

from app.parsing.python_parser import parse_python_source
from app.rag.chunking import MAX_CHUNK_LINES, chunk_file
from app.rag.models import SymbolSpan

FILE_ID = uuid.uuid4()


def _spans_from_source(source: str) -> list[SymbolSpan]:
    parsed = parse_python_source(source, "mod.py")
    return [
        SymbolSpan(
            symbol_id=uuid.uuid4(),
            qualified_name=s.qualified_name,
            kind=s.kind,
            start_line=s.start_line,
            end_line=s.end_line,
        )
        for s in parsed.symbols
    ]


def test_empty_file_produces_one_file_summary_chunk() -> None:
    chunks = chunk_file(FILE_ID, "empty.py", "", [])
    assert len(chunks) == 1
    assert chunks[0].chunk_kind == "file_summary"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 1
    assert chunks[0].symbol_id is None


def test_syntax_error_file_has_no_symbols_but_still_gets_a_file_summary_chunk() -> None:
    """A file the parser couldn't parse at all produces zero SymbolSpans —
    chunking must still represent it, never silently vanish it."""
    content = "def broken(:\n    pass\n"
    chunks = chunk_file(FILE_ID, "broken.py", content, [])
    assert len(chunks) == 1
    assert chunks[0].chunk_kind == "file_summary"
    assert chunks[0].symbol_id is None
    assert chunks[0].end_line == 2


def test_nested_functions_each_get_their_own_chunk() -> None:
    source = "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    chunks = chunk_file(FILE_ID, "mod.py", source, _spans_from_source(source))
    symbol_body_names = {c.qualified_name for c in chunks if c.chunk_kind == "symbol_body"}
    assert "mod.outer" in symbol_body_names
    assert "mod.outer.inner" in symbol_body_names


def test_classes_and_methods_get_separate_chunks() -> None:
    source = "class C:\n    def m(self):\n        return 1\n"
    chunks = chunk_file(FILE_ID, "mod.py", source, _spans_from_source(source))
    kinds_by_qname = {
        c.qualified_name: c.chunk_kind for c in chunks if c.chunk_kind == "symbol_body"
    }
    assert "mod.C" in kinds_by_qname
    assert "mod.C.m" in kinds_by_qname


def test_decorators_are_included_in_the_chunk_span() -> None:
    """Symbol.start_line excludes decorator lines (confirmed directly
    against the parser) — the chunk must still include them, or a
    semantically important decorator silently vanishes from retrieval."""
    source = "@decorator\n@another.decorator(arg=1)\ndef f():\n    return 1\n"
    spans = _spans_from_source(source)
    f_span = next(s for s in spans if s.qualified_name == "mod.f")
    assert f_span.start_line == 3  # confirms the parser's own exclusion

    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    f_chunk = next(
        c for c in chunks if c.qualified_name == "mod.f" and c.chunk_kind == "symbol_body"
    )
    assert f_chunk.start_line == 1  # extended backward to include both decorators
    assert f_chunk.end_line == 4


def test_decorator_extension_does_not_reach_past_an_unrelated_preceding_line() -> None:
    source = "x = 1\ndef f():\n    return 1\n"
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    f_chunk = next(c for c in chunks if c.qualified_name == "mod.f")
    assert f_chunk.start_line == 2  # not extended into the unrelated "x = 1" line


def test_docstrings_are_part_of_the_symbol_chunk_not_a_separate_one() -> None:
    source = 'def f():\n    """A docstring."""\n    return 1\n'
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    f_chunk = next(c for c in chunks if c.qualified_name == "mod.f")
    assert f_chunk.start_line == 1
    assert f_chunk.end_line == 3
    # No separate "doc_comment" chunk kind is produced in v1.
    assert not any(c.chunk_kind == "doc_comment" for c in chunks)


def test_large_function_is_split_into_gap_free_non_overlapping_sliding_windows() -> None:
    body_lines = "\n".join(f"    x{i} = {i}" for i in range(MAX_CHUNK_LINES + 50))
    source = f"def big():\n{body_lines}\n    return x0\n"
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    big_chunks = sorted(
        (c for c in chunks if c.qualified_name == "mod.big"), key=lambda c: c.start_line
    )
    assert len(big_chunks) >= 2
    assert all(c.chunk_kind == "sliding_window" for c in big_chunks)

    big_symbol = next(s for s in spans if s.qualified_name == "mod.big")
    # No gap, no overlap, exact coverage of the symbol's own span.
    assert big_chunks[0].start_line == big_symbol.start_line
    assert big_chunks[-1].end_line == big_symbol.end_line
    for first, second in zip(big_chunks, big_chunks[1:], strict=False):
        assert second.start_line == first.end_line + 1
    # chunk_index/chunk_count are consistent and ordered.
    assert [c.chunk_index for c in big_chunks] == list(range(len(big_chunks)))
    assert all(c.chunk_count == len(big_chunks) for c in big_chunks)


def test_duplicate_function_names_in_different_files_do_not_collide() -> None:
    source = "def apply_discount():\n    return 1\n"
    spans = _spans_from_source(source)
    file_a = uuid.uuid4()
    file_b = uuid.uuid4()
    chunks_a = chunk_file(file_a, "a/discount.py", source, spans)
    chunks_b = chunk_file(file_b, "b/discount.py", source, spans)
    a_chunk = next(c for c in chunks_a if c.qualified_name == "mod.apply_discount")
    b_chunk = next(c for c in chunks_b if c.qualified_name == "mod.apply_discount")
    assert a_chunk.file_id == file_a
    assert b_chunk.file_id == file_b
    assert a_chunk.file_path != b_chunk.file_path


def test_unicode_source_is_handled_correctly() -> None:
    source = 'def café():\n    """Uses émojis 🎉 and ünïcödé."""\n    return "héllo wörld"\n'
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    f_chunk = next(c for c in chunks if c.chunk_kind == "symbol_body")
    assert f_chunk.start_line == 1
    assert f_chunk.end_line == 3


def test_ordering_is_deterministic_across_repeated_calls() -> None:
    source = (
        "class B:\n    def m(self):\n        pass\n\n\n"
        "class A:\n    def m(self):\n        pass\n\n\n"
        "def top_level():\n    pass\n"
    )
    spans = _spans_from_source(source)
    first = chunk_file(FILE_ID, "mod.py", source, spans)
    second = chunk_file(FILE_ID, "mod.py", source, spans)
    assert first == second
    assert [c.start_line for c in first] == sorted(c.start_line for c in first)


def test_line_boundary_correctness_for_adjacent_symbols() -> None:
    source = "def f():\n    return 1\ndef g():\n    return 2\n"
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    f_chunk = next(c for c in chunks if c.qualified_name == "mod.f")
    g_chunk = next(c for c in chunks if c.qualified_name == "mod.g")
    assert f_chunk.start_line == 1
    assert f_chunk.end_line == 2
    assert g_chunk.start_line == 3
    assert g_chunk.end_line == 4


def test_module_symbol_produces_file_summary_not_a_redundant_symbol_body() -> None:
    source = "import os\n\n\ndef f():\n    return os.getcwd()\n"
    spans = _spans_from_source(source)
    chunks = chunk_file(FILE_ID, "mod.py", source, spans)
    # The module symbol itself never becomes a "symbol_body" chunk — only
    # the file_summary chunk represents it.
    assert not any(c.qualified_name == "mod" and c.chunk_kind == "symbol_body" for c in chunks)
    file_summary = next(c for c in chunks if c.chunk_kind == "file_summary")
    assert file_summary.qualified_name == "mod"  # traceable to the module symbol
    assert file_summary.start_line == 1
