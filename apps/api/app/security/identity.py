"""Deterministic finding-identity (fingerprint) functions — Task 6. Pure,
no DB, no randomness. `fingerprint` is unique per `(repository_id,
fingerprint)` at the database layer (see app/models/security.py); these
functions define exactly what goes into that string so identical scanner
output, re-run against the same repository, always dedupes to the same
SecurityFinding row rather than creating uncontrolled duplicates.
"""

_OSV_PREFIX = "osv-scanner"
_SEMGREP_PREFIX = "semgrep"


def osv_fingerprint(ecosystem: str, package_name: str, advisory_id: str) -> str:
    """Deliberately excludes installed version and manifest path: the same
    underlying advisory against the same package is one finding (one
    "ongoing condition," per domain-model.md section 7) regardless of which
    lockfile pins it or at what version — those vary per *observation*, not
    per finding identity. Findings are deduplicated per-repository (not
    per-revision): the same vulnerability persisting across ten commits is
    one finding, not ten."""
    return f"{_OSV_PREFIX}:{ecosystem}:{package_name}:{advisory_id}"


def semgrep_fingerprint(
    rule_id: str, file_path: str, start_line: int, scanner_fingerprint: str | None
) -> str:
    """Prefers Semgrep's own `extra.fingerprint` field when the scanner
    supplies one (an AST-shape-based hash, stable across pure line-number
    shifts from unrelated edits elsewhere in the file) — falling back to
    `rule_id:path:start_line` only when it doesn't. The fallback is
    deliberately more fragile (domain-model.md section 16 already flags
    this: a Semgrep location-based fingerprint is fragile compared to OSV's
    package+version+advisory key) — a revision that shifts a flagged line
    by one, with the scanner's own fingerprint unavailable, will register
    as a "new" finding under the fallback. This is a stated, known
    limitation, not silently hidden."""
    if scanner_fingerprint:
        return f"{_SEMGREP_PREFIX}:{rule_id}:{scanner_fingerprint}"
    return f"{_SEMGREP_PREFIX}:{rule_id}:{file_path}:{start_line}"
