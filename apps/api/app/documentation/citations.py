"""Documentation evidence/citation validation — pure, no DB, no AI
provider. "D<n>" prefix, deliberately distinct from Impact Intelligence's
"E<n>" (app/impact/evidence.py), RAG's "Q<n>" (app/rag/citations.py), and
Testing's "T<n>" (app/testing/citations.py). Re-implemented here rather
than generalizing any of those three already-approved modules for a
cosmetic prefix difference — the validation logic and policy are
identical by design, verified by an equivalent test suite. See
docs/architecture/documentation-intelligence.md section 5.
"""

import re

from app.documentation.models import EvidenceSet

# Case-insensitive match, case-sensitive validation — identical two-step
# distinction to the other three citation modules: flag a lowercase "[d1]"
# as an attempted-but-unknown citation rather than silently ignoring it as
# plain text, while never treating it as equivalent to a real "D1".
_CITATION_PATTERN = re.compile(r"\[(D\d+)\]", re.IGNORECASE)


def extract_cited_ids(text: str) -> list[str]:
    return _CITATION_PATTERN.findall(text)


def validate_citations(cited_ids: list[str], evidence: EvidenceSet) -> tuple[list[str], list[str]]:
    """Splits into (valid, invalid) against the real evidence set. Never
    raises — an invalid id is data to sanitize, never a reason to fail the
    whole response."""
    valid_ids = evidence.valid_ids()
    valid = [cid for cid in cited_ids if cid in valid_ids]
    invalid = [cid for cid in cited_ids if cid not in valid_ids]
    return valid, invalid


def sanitize_citations(text: str, evidence: EvidenceSet) -> str:
    """Replaces every citation marker not present in `evidence` with
    "[unverified]", leaving valid citations untouched."""
    valid_ids = evidence.valid_ids()

    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in valid_ids else "[unverified]"

    return _CITATION_PATTERN.sub(_replace, text)
