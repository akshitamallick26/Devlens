"""Deterministic file classification for repository ingestion.

No AST parsing, no language taxonomy — just enough to decide "can/should
this file's content be stored," plus a small, bounded extension->language
hint for the next (parsing) phase to use. See
docs/architecture/repository-ingestion.md sections 8-9.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.ingestion.git_adapter import file_size, is_symlink, safe_join

# The same heuristic Git itself uses to decide "is this binary" for diffs:
# a NUL byte anywhere in a bounded initial sample. Deterministic, no magic-byte
# library, no LLM.
_BINARY_SNIFF_BYTES = 8192

# Small and explicitly bounded — not a language taxonomy, just enough for the
# parsing phase to route a file to the right parser. Unmapped extensions get
# `language = None`, which the schema already supports.
_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
}

# Not exhaustive — a hint for later phases to deprioritize, not a guarantee.
_GENERATED_FILENAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
    }
)
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".generated.ts", ".generated.py")


class FileCategory(StrEnum):
    TEXT = "text"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    category: FileCategory
    size_bytes: int
    is_generated: bool = False
    language: str | None = None
    skip_reason: str | None = None


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _looks_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _GENERATED_FILENAMES:
        return True
    return any(name.endswith(suffix) for suffix in _GENERATED_SUFFIXES)


def _guess_language(path: str) -> str | None:
    return _LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower())


def classify_file(repo_dir: Path, relative_path: str, *, max_file_bytes: int) -> InventoryEntry:
    """Classifies one tracked file without reading its full content when it
    can be avoided (size and symlink checks come first)."""
    if is_symlink(repo_dir, relative_path):
        return InventoryEntry(
            path=relative_path,
            category=FileCategory.UNSUPPORTED,
            size_bytes=0,
            skip_reason="symlink",
        )

    size = file_size(repo_dir, relative_path)
    if size > max_file_bytes:
        return InventoryEntry(
            path=relative_path,
            category=FileCategory.UNSUPPORTED,
            size_bytes=size,
            skip_reason=f"exceeds max file size ({max_file_bytes} bytes)",
        )

    with safe_join(repo_dir, relative_path).open("rb") as handle:
        sample = handle.read(_BINARY_SNIFF_BYTES)
    if _looks_binary(sample):
        return InventoryEntry(path=relative_path, category=FileCategory.BINARY, size_bytes=size)

    return InventoryEntry(
        path=relative_path,
        category=FileCategory.TEXT,
        size_bytes=size,
        is_generated=_looks_generated(relative_path),
        language=_guess_language(relative_path),
    )
