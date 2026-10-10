"""A small, explicit language dispatch — not a generic parser framework with
factories or registries. One language is implemented (Python); everything
else returns a "skipped" result. Adding a second language later means adding
one more `elif` here and a new `<language>_parser.py` module, not touching
this function's shape.
"""

from app.parsing.python_parser import parse_python_source
from app.parsing.result import ParseResult


def parse_source_file(content: str, repo_relative_path: str, language: str | None) -> ParseResult:
    """Pure function: no database, no filesystem, no network. `language`
    normally comes from `SourceFile.language` (set during ingestion)."""
    if language == "python":
        return parse_python_source(content, repo_relative_path)
    return ParseResult(status="skipped", language=language)
