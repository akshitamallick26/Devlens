"""Deterministic discovery of supported dependency-manifest paths. Actual
file materialization is shared with app/security/semgrep.py's source
scanning via app/security/materialize.py.
"""

import re

# Python-only in v1, exactly as specified — never claim support for a
# format this list doesn't name. Order matters only for readability; all
# patterns are checked independently.
_SUPPORTED_MANIFEST_PATTERNS = (
    re.compile(r"(^|/)requirements\.txt$"),
    re.compile(r"(^|/)requirements/[^/]+\.txt$"),
    re.compile(r"(^|/)pyproject\.toml$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)uv\.lock$"),
    re.compile(r"(^|/)Pipfile\.lock$"),
)


def is_supported_manifest_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in _SUPPORTED_MANIFEST_PATTERNS)
